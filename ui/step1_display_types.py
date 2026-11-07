"""The display types the Rust, Step3 and CPU viewer code shares with the
Python GPU path.

Moved UNCHANGED (block A6, docs/v17_small_review_items_plan.md §2) out of
`step1_gpu_layer` and `step1_gpu_binding`, which import them back under the
same names (identical objects), so the Rust core binding, the core labels,
Step3's label binding and the mount no longer import the Python GPU modules
for them. Snapshots, label layers, the label texture cache, the ROI polygon
check and the binding budgets; no Qt, no GL import (a `gl` object is passed in).
"""

from __future__ import annotations

import collections
import math
from dataclasses import dataclass, field
from typing import Any, Dict, Hashable, Mapping, Optional, Sequence, Tuple

import numpy as np

from ..core import resource_tiers as _tiers


MODE_OVERLAY = "overlay"
MODE_FUSION = "fusion"


class Step1GpuLayerError(RuntimeError):
    """A diagnostic G1 renderer failure; callers must not silently fall back."""


@dataclass(frozen=True)
class DisplaySnapshot:
    """A complete caller-owned C1 display input, not mutable GPU state."""

    mode: str
    mappings: Mapping[str, Tuple[float, float, float]]
    weights: Mapping[str, float] = field(default_factory=dict)
    colors: Mapping[str, Tuple[float, float, float]] = field(default_factory=dict)
    groups: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    group_weights: Mapping[str, float] = field(default_factory=dict)
    nucleus: Tuple[str, float] = ("", 0.0)


@dataclass(frozen=True)
class ViewportSnapshot:
    """Read-only world and physical-output requirements from the current owner."""

    world_rect: Tuple[float, float, float, float]
    logical_size: Tuple[int, int]
    device_pixel_ratio: float = 1.0
    repaint_request: Optional[Any] = None
    #: THE ANALYSIS REGION, in the same `(x0, x1, y0, y1)` level-0 order as
    #: `world_rect` -- not the source table's own `(y0, y1, x0, x1)`; the
    #: owner converts once, here it is one rectangle in one order.
    #:
    #: Why the final output needs it: a coarse plane is drawn over the whole
    #: world area of every texel it has, and a texel whose block only PARTLY
    #: meets the region is a real sample, so it paints up to a whole block
    #: beyond the region's edge (measured: 22 640 opaque pixels outside a
    #: 600x600 region at a 64-pixel stride). The region is a display input,
    #: not a source rule: `None` means a whole slide and clips nothing.
    roi_world_rect: Optional[Tuple[float, float, float, float]] = None
    #: THE DRAWN SHAPE, in level-0 world coordinates as `(x, y)` points --
    #: the order Step0 stores `polygon_fullres` in. The rectangle above is
    #: its bounding box and stays in force; this narrows the picture to what
    #: the user actually drew, which for a real ROI is a good deal less: the
    #: nine-point region from the 2026-09-22 machine covers 79.4 % of its
    #: own bbox, and a raw channel (no Step0 polygon mask) filled the other
    #: 20.6 %. `None` keeps G3.2c's rectangle behaviour exactly.
    #:
    #: FIRST VERSION: one simple polygon, no holes. Self-intersecting or
    #: multi-ring input is not supported and is not silently approximated --
    #: see `sanitize_roi_polygon`.
    roi_polygon_world: Optional[Tuple[Tuple[float, float], ...]] = None

    @property
    def physical_size(self) -> Tuple[int, int]:
        width = max(1, int(round(self.logical_size[0] * self.device_pixel_ratio)))
        height = max(1, int(round(self.logical_size[1] * self.device_pixel_ratio)))
        return width, height


#: Block 4b: the label textures' own GPU budget (user ruling 2026-09-26),
#: beside -- never inside -- the raw textures' 512 MB.
LABEL_TEXTURE_BYTES = _tiers.GPU_LABEL_TEXTURE_BYTES  # (block A8 / A5: one place)
#: The largest outline radius the draw pass loops over, in screen pixels.
MAX_LABEL_RADIUS = 8
LABEL_OUTLINE = "outline"
LABEL_FILL = "fill"


@dataclass(frozen=True)
class LabelPlane:
    """One uint32 label tile and where it lies (level-0 world, x0 x1 y0 y1)."""

    identity: Hashable
    world_rect: Tuple[float, float, float, float]
    ids: np.ndarray


@dataclass(frozen=True)
class LabelLayer:
    """One mask (cell or nucleus) and how it is drawn.

    `planes` are drawn in the order given, a later one over an earlier one
    (the caller puts the target level last); `width` is in LOGICAL pixels,
    0..4, and becomes floor(width * device pixel ratio + 0.5) screen pixels,
    at most `MAX_LABEL_RADIUS`."""

    kind: str
    planes: Tuple[LabelPlane, ...] = ()
    color: Tuple[float, float, float] = (0.0, 1.0, 0.0)
    alpha: float = 0.75
    width: float = 1.0
    mode: str = LABEL_OUTLINE
    visible: bool = True
    #: dashed outline (Step3 multi-result plan §4)
    dash: bool = False


@dataclass(frozen=True)
class LabelSnapshot:
    """Every mask layer, drawn in order (a later layer over an earlier one)."""

    layers: Tuple[LabelLayer, ...] = ()


def label_radius(width: float, device_pixel_ratio: float) -> int:
    """Screen radius of a logical outline width: floor(w * dpr + 0.5), 0..8."""
    radius = int(math.floor(float(width) * float(device_pixel_ratio) + 0.5))
    return max(0, min(MAX_LABEL_RADIUS, radius))


class _LabelTextureLru:
    """Block 4b: R32UI label textures, keyed by the caller's plane identity,
    in their own budget. Integer textures, nearest only: an id is fetched,
    never filtered."""

    def __init__(self, max_bytes: int):
        if max_bytes <= 0:
            raise ValueError("max_label_texture_bytes must be positive")
        self.max_bytes = int(max_bytes)
        self.records: "collections.OrderedDict[Hashable, _TextureRecord]" = collections.OrderedDict()
        self.bytes = 0
        self.peak_bytes = 0
        self.uploads = 0
        self.evictions = 0

    @staticmethod
    def plane_bytes(plane: LabelPlane) -> int:
        ids = np.asarray(plane.ids)
        return int(ids.shape[0] * ids.shape[1] * 4)

    @staticmethod
    def _validate(plane: LabelPlane) -> None:
        try:
            hash(plane.identity)
        except TypeError as exc:
            raise Step1GpuLayerError("label plane identity must be hashable") from exc
        x0, x1, y0, y1 = plane.world_rect
        if not x1 > x0 or not y1 > y0:
            raise Step1GpuLayerError("label plane world rect must have positive extent")
        ids = np.asarray(plane.ids)
        if ids.ndim != 2 or ids.shape[0] == 0 or ids.shape[1] == 0 or ids.dtype != np.uint32:
            raise Step1GpuLayerError("label plane ids must be a nonempty HxW uint32 array")

    def prepare(self, gl, planes: Sequence[LabelPlane]) -> None:
        required: Dict[Hashable, LabelPlane] = {}
        for plane in planes:
            self._validate(plane)
            required[plane.identity] = plane
        total = sum(self.plane_bytes(plane) for plane in required.values())
        if total > self.max_bytes:
            raise Step1GpuLayerError(
                f"label working set {total} bytes exceeds the label budget {self.max_bytes}")
        missing = sum(self.plane_bytes(plane) for identity, plane in required.items()
                      if identity not in self.records)
        while self.bytes + missing > self.max_bytes:
            victim = next(identity for identity in self.records if identity not in required)
            record = self.records.pop(victim)
            gl.glDeleteTextures([record.texture])
            self.bytes -= record.byte_count
            self.evictions += 1
        for identity, plane in required.items():
            if identity in self.records:
                self.records.move_to_end(identity)
                continue
            ids = np.ascontiguousarray(np.asarray(plane.ids), dtype=np.uint32)
            texture = _as_name(gl.glGenTextures(1))
            gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
            gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_R32UI, ids.shape[1], ids.shape[0],
                            0, gl.GL_RED_INTEGER, gl.GL_UNSIGNED_INT, ids)
            byte_count = self.plane_bytes(plane)
            self.records[identity] = _TextureRecord(
                texture=texture, width=ids.shape[1], height=ids.shape[0],
                byte_count=byte_count, world_rect=plane.world_rect)
            self.bytes += byte_count
            self.peak_bytes = max(self.peak_bytes, self.bytes)
            self.uploads += 1
        _check_gl(gl, "label texture upload")

    def texture_for(self, plane: LabelPlane) -> int:
        return self.records[plane.identity].texture

    def clear(self, gl) -> int:
        count = len(self.records)
        if self.records:
            gl.glDeleteTextures([record.texture for record in self.records.values()])
        self.records.clear()
        self.bytes = 0
        return count

    def stats(self) -> Dict[str, int]:
        return {"budget_bytes": self.max_bytes, "bytes": self.bytes,
                "peak_bytes": self.peak_bytes, "textures": len(self.records),
                "uploads": self.uploads, "evictions": self.evictions}


@dataclass
class _TextureRecord:
    texture: int
    width: int
    height: int
    byte_count: int
    world_rect: Tuple[float, float, float, float]
    #: block A9 S2c: an R8UI / R16UI texture, drawn by the integer program
    integer: bool = False


#: How far a polygon's own bounds may sit from the ROI rectangle before the
#: pair is refused. They come from the same Step0 write, where the bbox IS
#: the polygon's bounding box, so this is a rounding allowance, not a fit.
ROI_POLYGON_BOUNDS_TOLERANCE = 1.0


def sanitize_roi_polygon(points, roi_rect):
    """`(polygon, error)` -- the polygon to clip with, or why there is none.

    The polygon and the rectangle must describe the SAME region: Step0
    writes `bbox_fullres` as the bounding box of `polygon_fullres`, so a
    polygon whose own bounds do not match the rectangle in force is not this
    ROI's -- it is a leftover from another one, and stapling it onto this
    source would clip the picture to the wrong shape.

    A polygon that is present but unusable returns `(None, reason)`. The
    caller must NOT treat that as "no polygon was given": the rectangle
    still clips, and the reason is reported rather than swallowed.
    """
    if points is None:
        return None, ""
    try:
        cleaned = [(float(x), float(y)) for x, y in points]
    except (TypeError, ValueError):
        return None, "polygon points are not (x, y) numbers"
    if len(cleaned) < 3:
        return None, f"polygon has {len(cleaned)} points, need at least 3"
    if not all(math.isfinite(v) for point in cleaned for v in point):
        return None, "polygon contains a non-finite coordinate"
    if roi_rect is not None:
        bx0, bx1, by0, by1 = (float(v) for v in roi_rect)
        xs = [x for x, _y in cleaned]
        ys = [y for _x, y in cleaned]
        tol = ROI_POLYGON_BOUNDS_TOLERANCE
        if (abs(min(xs) - bx0) > tol or abs(max(xs) - bx1) > tol
                or abs(min(ys) - by0) > tol or abs(max(ys) - by1) > tol):
            return None, (
                f"polygon bounds (x {min(xs)}..{max(xs)}, y {min(ys)}..{max(ys)}) "
                f"are not this ROI's rectangle (x {bx0}..{bx1}, y {by0}..{by1})")
    return tuple(cleaned), ""


def _as_name(value) -> int:
    if isinstance(value, (tuple, list, np.ndarray)):
        return int(value[0])
    return int(value)


def _clear_gl_errors(gl) -> int:
    """Drop GL errors that were pending BEFORE this layer's own batch.

    Qt's own `QOpenGLWidget` bookkeeping can leave an error in the queue
    (probed on PySide6 6.11.3: GL_INVALID_ENUM pending when the array upload
    batch started, with every one of this layer's calls clean). `_check_gl`
    reads the queue right after OUR call, so a foreign error gets blamed on
    the upload and the whole submission is rejected -- the picture silently
    keeps its last frame. Draining once at the batch start attributes only
    errors this layer's own calls produce; it does not hide them.
    """
    dropped = 0
    while True:
        error = gl.glGetError()
        if error == gl.GL_NO_ERROR:
            return dropped
        dropped += 1


def _check_gl(gl, operation: str) -> None:
    error = gl.glGetError()
    if error != gl.GL_NO_ERROR:
        raise Step1GpuLayerError(f"OpenGL error after {operation}: 0x{int(error):04x}")


@dataclass(frozen=True)
class BindingBudgets:
    """Explicit G2 retention/request limits; no production default is implied."""

    max_coarse_tiles_per_channel: int
    max_coarse_plane_bytes_per_channel: int
    max_fine_tiles_per_viewport: int
    max_fine_plane_bytes_per_channel: int
    motion_interval_ms: int = 33
    #: Block A9 S2b: the GPU layer's total raw-texture budget. When set,
    #: every plan is ADMITTED against it (complete coarse of every active
    #: channel + the admitted target + carried stand-ins), so a submission
    #: never exceeds it. None keeps the per-channel limits alone.
    max_raw_texture_bytes: Optional[int] = None
