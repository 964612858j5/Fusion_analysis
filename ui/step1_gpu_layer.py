"""Step1 G1 test-only GPU composition layer.

This is an isolated validation renderer, not a production display path.  It
owns only Qt-context-local GL names and a bounded GPU texture cache.  Callers
supply immutable source/display/viewport snapshots; this module never imports
or queries the Step1 coordinator, provider, scheduler, ViewBox controller,
shared display state, MainWindow, or the C1 CPU composer.

Qt owns the widget/context/event lifecycle.  PyOpenGL is imported lazily only
from ``initializeGL`` while this widget's exact Qt context is current.
"""

from __future__ import annotations

import collections
import dataclasses
import math
import pathlib
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Hashable, List, Mapping, Optional, Sequence, Tuple

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets


MODE_OVERLAY = "overlay"
MODE_FUSION = "fusion"
_SHADER_DIR = pathlib.Path(__file__).with_name("shaders")


class Step1GpuLayerError(RuntimeError):
    """A diagnostic G1 renderer failure; callers must not silently fall back."""


@dataclass(frozen=True)
class RawPlane:
    """One caller-owned raw plane and its existing identity/geometry."""

    identity: Hashable
    world_rect: Tuple[float, float, float, float]
    values: np.ndarray
    valid: Optional[np.ndarray] = None
    #: Block A9 S2c: the valid pixels as one rectangle `(y0, y1, x0, x1)` in
    #: the plane's own pixels -- how a uint8/uint16 plane carries validity
    #: (its values have no NaN). None: every pixel valid (or `valid` says).
    valid_rect: Optional[Tuple[int, int, int, int]] = None


def _valid_world_rect(plane) -> Optional[Tuple[float, float, float, float]]:
    """Block A9 S2c: `plane.valid_rect` as a world rectangle (the whole
    plane when it has none), or None when it holds no valid pixel."""
    x0, x1, y0, y1 = plane.world_rect
    if plane.valid_rect is None:
        return (x0, x1, y0, y1)
    h, w = np.asarray(plane.values).shape
    vy0, vy1, vx0, vx1 = plane.valid_rect
    if vy1 <= vy0 or vx1 <= vx0:
        return None
    sx, sy = (x1 - x0) / w, (y1 - y0) / h
    return (x0 + vx0 * sx, x0 + vx1 * sx, y0 + vy0 * sy, y0 + vy1 * sy)


def _pixel_size(plane) -> Tuple[float, float]:
    """World units per texel of a plane (A9 §35: equal = one pyramid level)."""
    x0, x1, y0, y1 = plane.world_rect
    h, w = np.asarray(plane.values).shape[:2]
    return round((x1 - x0) / w, 9), round((y1 - y0) / h, 9)


def _plane_quad_ndc(plane_rect, view_rect, target_size) -> Tuple[float, float, float, float]:
    """Block A9 §32: the NDC rectangle a plane pass rasterises -- the plane's
    world rect mapped like the fragment shader maps pixels (x right, world
    y DOWN the screen), padded by one physical pixel on every side and
    clamped to the target. Conservative: every pixel whose centre the
    shader's half-open test would accept lies inside it; the shader still
    decides each pixel exactly as the fullscreen triangle did."""
    vx0, vx1, vy0, vy1 = view_rect
    px0, px1, py0, py1 = plane_rect
    width, height = target_size
    sx = 2.0 / max(vx1 - vx0, 1e-12)
    sy = 2.0 / max(vy1 - vy0, 1e-12)
    pad_x, pad_y = 2.0 / max(width, 1), 2.0 / max(height, 1)
    x0 = (px0 - vx0) * sx - 1.0 - pad_x
    x1 = (px1 - vx0) * sx - 1.0 + pad_x
    y0 = (vy1 - py1) * sy - 1.0 - pad_y      # world bottom edge -> NDC low
    y1 = (vy1 - py0) * sy - 1.0 + pad_y
    clamp = lambda v: min(1.0, max(-1.0, v))  # noqa: E731
    return clamp(x0), clamp(x1), clamp(y0), clamp(y1)


#: Block A9 S2c: the integer formats a raw plane is uploaded in unchanged.
INTEGER_PLANE_DTYPES = (np.dtype(np.uint8), np.dtype(np.uint16))


def _is_integer_plane(plane) -> bool:
    return np.asarray(plane.values).dtype in INTEGER_PLANE_DTYPES


@dataclass(frozen=True)
class ChannelSource:
    """Caller-selected per-channel coarse/fine data; no source lookup occurs here."""

    channel: str
    coarse: Tuple[RawPlane, ...] = ()
    fine: Tuple[RawPlane, ...] = ()
    selected_level: str = "coarse"
    #: The level the viewport WANTS, for measurement only (block A9-O3-5,
    #: codex): with coarse drawn first, what is drawn is not what is owed.
    #: "" means the drawn level.
    target_level: str = ""

    def selected_planes(self) -> Tuple[RawPlane, ...]:
        if self.selected_level == "coarse":
            return self.coarse
        if self.selected_level == "fine":
            # Fine draws after coarse so valid fine coverage replaces that
            # channel's coarse signal only; another channel is independent.
            return self.coarse + self.fine
        raise Step1GpuLayerError(f"unknown selected level {self.selected_level!r}")


@dataclass(frozen=True)
class SourceDescriptor:
    channels: Tuple[ChannelSource, ...]

    def by_channel(self) -> Dict[str, ChannelSource]:
        result = {item.channel: item for item in self.channels}
        if len(result) != len(self.channels):
            raise Step1GpuLayerError("source descriptor contains duplicate channel names")
        return result


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
from ..core import resource_tiers as _tiers
from ..utils import perf_trace
from ..viewer import coverage_probe
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


class _TextureLru:
    """A context-local raw texture LRU keyed only by supplied plane identity."""

    def __init__(self, max_bytes: int):
        if max_bytes <= 0:
            raise ValueError("max_raw_texture_bytes must be positive")
        self.max_bytes = int(max_bytes)
        self.records: "collections.OrderedDict[Hashable, _TextureRecord]" = collections.OrderedDict()
        self.bytes = 0
        self.peak_bytes = 0
        self.peak_textures = 0
        self.hits = 0
        self.misses = 0
        self.uploads = 0
        self.evictions = 0
        self.upload_submit_ms = collections.deque(maxlen=256)   # A9 §35: bounded
        #: Block A9 §35: bytes another store holds under the same budget
        self.external_bytes = lambda: 0

    @staticmethod
    def plane_bytes(plane: RawPlane) -> int:
        values = np.asarray(plane.values)
        if values.ndim != 2:
            raise Step1GpuLayerError("raw plane values must be two dimensional")
        # block A9 S2c: an integer plane costs its own bytes; anything else
        # is uploaded as float32
        itemsize = (values.dtype.itemsize if values.dtype in INTEGER_PLANE_DTYPES
                    else np.dtype(np.float32).itemsize)
        return int(values.shape[0] * values.shape[1] * itemsize)

    def _validate_identity(self, plane: RawPlane) -> None:
        try:
            hash(plane.identity)
        except TypeError as exc:
            raise Step1GpuLayerError("raw plane identity must be caller-supplied and hashable") from exc
        x0, x1, y0, y1 = plane.world_rect
        if not x1 > x0 or not y1 > y0:
            raise Step1GpuLayerError("raw plane world rect must have positive extent")
        values = np.asarray(plane.values)
        if values.ndim != 2 or values.shape[0] == 0 or values.shape[1] == 0:
            raise Step1GpuLayerError("raw plane values must be a nonempty HxW array")
        if plane.valid is not None and np.asarray(plane.valid).shape != values.shape:
            raise Step1GpuLayerError("raw plane valid mask shape must match values")
        if values.dtype in INTEGER_PLANE_DTYPES and plane.valid is not None:
            raise Step1GpuLayerError("an integer raw plane carries its validity as valid_rect")
        if plane.valid_rect is not None:
            y0, y1, x0, x1 = plane.valid_rect
            if not (0 <= y0 <= y1 <= values.shape[0] and 0 <= x0 <= x1 <= values.shape[1]):
                raise Step1GpuLayerError("raw plane valid_rect must lie inside its values")

    def is_resident(self, plane: RawPlane) -> bool:
        return plane.identity in self.records

    @perf_trace.timed("gpu.upload")
    def prepare(self, gl, planes: Sequence[RawPlane], *, mandatory: Sequence[RawPlane] = (),
                budget_ms: Optional[float] = None) -> None:
        """Make a submitted active set resident or fail before issuing passes.

        Block A9 §32 (Odon: upload only what is drawn, a bounded amount per
        frame). `budget_ms` None uploads everything (the G1 contract). With
        a budget, every `mandatory` plane (a channel's complete coarse) is
        still uploaded, and the others only until `budget_ms` has been
        spent; the rest stay out (`self.deferred` counts them) and are
        skipped by the passes, which fall back to the coarser planes below
        them. A plane already resident with the same identity is checked
        only against its record, not re-validated pixel by pixel.
        """
        self.deferred = 0
        required: Dict[Hashable, RawPlane] = {}
        must = {plane.identity for plane in mandatory}
        for plane in planes:
            record = self.records.get(plane.identity)
            if record is not None and plane.identity not in required:
                if record.world_rect != plane.world_rect:
                    raise Step1GpuLayerError("resubmitted identity has incompatible raw texture metadata")
                required[plane.identity] = plane
                continue
            self._validate_identity(plane)
            previous = required.get(plane.identity)
            if previous is not None:
                if (np.asarray(previous.values).shape != np.asarray(plane.values).shape or
                        previous.world_rect != plane.world_rect):
                    raise Step1GpuLayerError("one supplied identity has incompatible raw geometry")
            required[plane.identity] = plane
        total_required = sum(record.byte_count if record is not None else self.plane_bytes(plane)
                             for record, plane in ((self.records.get(identity), plane)
                                                   for identity, plane in required.items()))
        if total_required + self.external_bytes() > self.max_bytes:
            raise Step1GpuLayerError(
                f"active raw working set {total_required} bytes exceeds cache budget {self.max_bytes}"
            )
        for identity, plane in required.items():
            record = self.records.get(identity)
            if record is not None:
                expected = self.plane_bytes(plane)
                shape = np.asarray(plane.values).shape
                if (record.byte_count != expected or record.height != shape[0] or
                        record.width != shape[1] or record.world_rect != plane.world_rect):
                    raise Step1GpuLayerError("resubmitted identity has incompatible raw texture metadata")
        missing = [(identity, plane) for identity, plane in required.items()
                   if identity not in self.records]
        missing_bytes = sum(self.plane_bytes(plane) for _identity, plane in missing)
        while self.bytes + missing_bytes + self.external_bytes() > self.max_bytes:
            victim = next((identity for identity in self.records if identity not in required), None)
            if victim is None:
                raise Step1GpuLayerError("cache cannot fit active raw working set without deleting active texture")
            record = self.records.pop(victim)
            gl.glDeleteTextures([record.texture])
            self.bytes -= record.byte_count
            self.evictions += 1
        for identity in required:
            if identity in self.records:
                self.records.move_to_end(identity)
                self.hits += 1
        # mandatory first, then the rest in the order given (coarse -> fine)
        missing.sort(key=lambda item: item[0] not in must)
        started = time.perf_counter()
        for identity, plane in missing:
            if (budget_ms is not None and identity not in must and
                    (time.perf_counter() - started) * 1000.0 >= budget_ms):
                self.deferred += 1
                continue
            self.misses += 1
            integer = _is_integer_plane(plane)
            if integer:
                values = np.ascontiguousarray(np.asarray(plane.values))
            else:
                values = np.ascontiguousarray(np.asarray(plane.values), dtype=np.float32).copy()
                if plane.valid is not None:
                    values[~np.asarray(plane.valid, dtype=bool)] = np.nan
                if plane.valid_rect is not None:
                    y0, y1, x0, x1 = plane.valid_rect
                    keep = np.zeros(values.shape, bool)
                    keep[y0:y1, x0:x1] = True
                    values[~keep] = np.nan
            upload_started = time.perf_counter()
            texture = _as_name(gl.glGenTextures(1))
            gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
            gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
            if not integer:
                gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_R32F, values.shape[1], values.shape[0],
                                0, gl.GL_RED, gl.GL_FLOAT, values)
            elif values.dtype == np.uint8:
                gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_R8UI, values.shape[1], values.shape[0],
                                0, gl.GL_RED_INTEGER, gl.GL_UNSIGNED_BYTE, values)
            else:
                gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_R16UI, values.shape[1], values.shape[0],
                                0, gl.GL_RED_INTEGER, gl.GL_UNSIGNED_SHORT, values)
            self.upload_submit_ms.append((time.perf_counter() - upload_started) * 1000.0)
            byte_count = self.plane_bytes(plane)
            self.records[identity] = _TextureRecord(
                texture=texture, width=values.shape[1], height=values.shape[0],
                byte_count=byte_count, world_rect=plane.world_rect, integer=integer,
            )
            self.bytes += byte_count
            self.peak_bytes = max(self.peak_bytes, self.bytes)
            self.peak_textures = max(self.peak_textures, len(self.records))
            self.uploads += 1
        _check_gl(gl, "raw texture upload")

    def texture_for(self, plane: RawPlane) -> int:
        record = self.records.get(plane.identity)
        if record is None:
            raise Step1GpuLayerError("submitted raw plane is not resident")
        self.records.move_to_end(plane.identity)
        return record.texture

    def is_integer(self, plane: RawPlane) -> bool:
        record = self.records.get(plane.identity)
        return bool(record is not None and record.integer)

    def clear(self, gl) -> int:
        count = len(self.records)
        if self.records:
            gl.glDeleteTextures([record.texture for record in self.records.values()])
        self.records.clear()
        self.bytes = 0
        return count

    def stats(self) -> Dict[str, int]:
        return {
            "budget_bytes": self.max_bytes,
            "bytes": self.bytes,
            "peak_bytes": self.peak_bytes,
            "textures": len(self.records),
            "peak_textures": self.peak_textures,
            "hits": self.hits,
            "misses": self.misses,
            "uploads": self.uploads,
            "upload_submit_ms": tuple(self.upload_submit_ms),
            "evictions": self.evictions,
        }


#: How far a polygon's own bounds may sit from the ROI rectangle before the
#: pair is refused. They come from the same Step0 write, where the bbox IS
#: the polygon's bounding box, so this is a rounding allowance, not a fit.
ROI_POLYGON_BOUNDS_TOLERANCE = 1.0


#: Block A9 §35: the tile slot of the array store (the Step1 tile size)
ARRAY_TILE = 512
#: preferred layers per array block (capped by GL_MAX_ARRAY_TEXTURE_LAYERS)
ARRAY_LAYERS = 256
#: instance record of one array-drawn plane (A9 §35.2): plane rect, valid
#: world rect, texel size, layer -- 44 bytes, the layer a real int32
INSTANCE_DTYPE = np.dtype([("rect", "<f4", 4), ("valid", "<f4", 4),
                           ("size", "<f4", 2), ("layer", "<i4")])


def _array_format(plane) -> str:
    dtype = np.asarray(plane.values).dtype
    if dtype == np.uint8:
        return "u8"
    if dtype == np.uint16:
        return "u16"
    return "f32"


_FORMAT_BYTES = {"u8": 1, "u16": 2, "f32": 4}


#: A9 §35.7: bytes per array block. A block's first use costs the driver
#: ~0.3-1.3 ms per MiB (measured: 256 MiB ~90-290 ms, 64 MiB ~22-70 ms),
#: so blocks are 64 MiB -- the single pass binds each as its own sampler,
#: up to the context's texture units (32 on the 3060 and on Intel)
ARRAY_BLOCK_BYTES = 64 * 1024 * 1024


def _block_layers(max_bytes, max_layers, itemsize) -> int:
    """Layers per block: ARRAY_BLOCK_BYTES worth, the GL limit, and at most
    a quarter of the budget per block (so a small budget holds several)."""
    slot = ARRAY_TILE * ARRAY_TILE * int(itemsize)
    return max(1, min(ARRAY_BLOCK_BYTES // slot, int(max_layers or ARRAY_LAYERS),
                      int(max_bytes) // (4 * slot)))


class _NoGrowth(Exception):
    """A9 §35: a new array block was needed while growth is held back."""


class _LazyRequired:
    """`identity in required`, the full set built on first use only."""

    __slots__ = ("known", "fn", "full")

    def __init__(self, known, fn):
        self.known, self.fn, self.full = known, fn, None

    def __contains__(self, identity) -> bool:
        if self.fn is None:
            return identity in self.known
        if self.full is None:
            self.full = set(self.fn()) | self.known
        return identity in self.full


class _ArrayBlock:
    __slots__ = ("texture", "fmt", "layers", "free", "owners", "vt_index")

    def __init__(self, texture, fmt, layers, vt_index=0):
        self.texture = texture
        self.fmt = fmt
        self.layers = layers
        #: A9 §35.7: the block's sampler slot in its family -- fixed for
        #: its life, never renumbered
        self.vt_index = vt_index
        self.free = list(range(layers - 1, -1, -1))
        self.owners: Dict[int, Hashable] = {}

    @property
    def nbytes(self) -> int:
        return ARRAY_TILE * ARRAY_TILE * _FORMAT_BYTES[self.fmt] * self.layers


class _TileArrayStore:
    """Block A9 §35 (Odon: one quad per tile; batched, because Python pays
    per call): raw planes of at most ARRAY_TILE x ARRAY_TILE in layers of
    GL_TEXTURE_2D_ARRAY blocks, one format per block, drawn instanced.

    Budget: every allocated block counts whole (empty layers included); the
    store and the legacy per-plane LRU share `max_bytes`. A plane needed by
    the submission being prepared is never evicted; a slot is reused only
    by the same format; a block left empty is deleted when its bytes are
    needed. Validity and pixel values are exactly the legacy path's."""

    def __init__(self, max_bytes: int, max_layers: int):
        self.max_bytes = int(max_bytes)
        self.max_layers = int(max_layers)
        self.blocks: List[_ArrayBlock] = []
        self.slots: Dict[Hashable, Tuple[_ArrayBlock, int]] = {}
        #: identity -> prepare() tick it was last required in (LRU by tick)
        self.used: Dict[Hashable, int] = {}
        self.tick = 0
        self._evict_order = None
        #: id(plane) -> plane, for planes already validated (immutable)
        self._validated: Dict[int, object] = {}
        self.meta: Dict[Hashable, Tuple] = {}     # identity -> (rect, valid, (w, h), fmt)
        self.channel_blocks: Dict[str, List[_ArrayBlock]] = {}
        self.external_bytes = lambda: 0
        self.deferred = 0
        self.uploads = 0
        self.evictions = 0
        self.hits = 0
        self.peak_bytes = 0
        self.storage_fallback = False
        #: A9 §35.7: bumped by every upload / eviction (page tables follow)
        self.meta_generation = 0
        #: (block, layer, identity or None) per upload / eviction, in order:
        #: the compositor's metadata texture follows it incrementally
        self.meta_log: List[Tuple[_ArrayBlock, int, Optional[Hashable]]] = []

    @staticmethod
    def eligible(plane) -> bool:
        values = np.asarray(plane.values)
        return (values.ndim == 2 and 0 < values.shape[0] <= ARRAY_TILE
                and 0 < values.shape[1] <= ARRAY_TILE)

    @property
    def allocated_bytes(self) -> int:
        return sum(block.nbytes for block in self.blocks)

    def is_resident(self, plane) -> bool:
        return plane.identity in self.slots

    def slot(self, plane) -> Tuple[_ArrayBlock, int]:
        return self.slots[plane.identity]

    def _layers(self, fmt) -> int:
        return _block_layers(self.max_bytes, self.max_layers, _FORMAT_BYTES[fmt])

    def _new_block(self, gl, fmt) -> _ArrayBlock:
        layers = self._layers(fmt)
        internal, _f, _t = self._gl_format(gl, fmt)
        texture = _as_name(gl.glGenTextures(1))
        gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, texture)
        for name in (gl.GL_TEXTURE_MIN_FILTER, gl.GL_TEXTURE_MAG_FILTER):
            gl.glTexParameteri(gl.GL_TEXTURE_2D_ARRAY, name, gl.GL_NEAREST)
        gl.glTexParameteri(gl.GL_TEXTURE_2D_ARRAY, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_2D_ARRAY, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_2D_ARRAY, gl.GL_TEXTURE_MAX_LEVEL, 0)
        storage = getattr(gl, "glTexStorage3D", None)
        try:
            if storage is None or self.storage_fallback:
                raise AttributeError
            storage(gl.GL_TEXTURE_2D_ARRAY, 1, internal, ARRAY_TILE, ARRAY_TILE, layers)
            _check_gl(gl, "array block storage")
        except Exception:                                  # noqa: BLE001 -- GL < 4.2
            self.storage_fallback = True
            while gl.glGetError() != gl.GL_NO_ERROR:
                pass
            _i, fmt_gl, typ = self._gl_format(gl, fmt)
            gl.glTexImage3D(gl.GL_TEXTURE_2D_ARRAY, 0, internal, ARRAY_TILE, ARRAY_TILE,
                            layers, 0, fmt_gl, typ, None)
            _check_gl(gl, "array block allocation")
        self._warm(gl, texture, fmt, layers)
        family = fmt == "f32"
        taken = {b.vt_index for b in self.blocks if (b.fmt == "f32") == family}
        index = next(i for i in range(len(taken) + 1) if i not in taken)
        block = _ArrayBlock(texture, fmt, layers, index)
        self.blocks.append(block)
        self.peak_bytes = max(self.peak_bytes, self.allocated_bytes)
        return block

    def _warm(self, gl, texture, fmt, layers) -> None:
        """Pay a new block's first-use cost NOW: clear one layer on the GPU,
        write a texel from the CPU, read it back on the GPU (a 1x1 blit) and
        write again. Measured (scratchpad/blockbench2.py, 4090): a fresh
        64 MiB block otherwise costs 22-70 ms on its first upload / draw,
        mid-gesture; warmed like this it costs ~75 ms here and ~0.2 ms
        afterwards. Called where a block is created -- ahead of need, at a
        quiet moment, when `ensure_headroom` keeps a reserve."""
        internal, fmt_gl, typ = self._gl_format(gl, fmt)
        value = np.zeros(1, {"u8": np.uint8, "u16": np.uint16}.get(fmt, np.float32))
        read_fbo, draw_fbo = _as_name(gl.glGenFramebuffers(1)), _as_name(gl.glGenFramebuffers(1))
        target = _as_name(gl.glGenRenderbuffers(1))
        try:
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, read_fbo)
            gl.glFramebufferTextureLayer(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0, texture, 0,
                                         layers - 1)
            if fmt == "f32":
                gl.glClearBufferfv(gl.GL_COLOR, 0, np.zeros(4, np.float32))
            else:
                gl.glClearBufferuiv(gl.GL_COLOR, 0, np.zeros(4, np.uint32))
            gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, texture)
            gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
            gl.glTexSubImage3D(gl.GL_TEXTURE_2D_ARRAY, 0, 0, 0, layers - 1, 1, 1, 1,
                               fmt_gl, typ, value)
            gl.glBindRenderbuffer(gl.GL_RENDERBUFFER, target)
            gl.glRenderbufferStorage(gl.GL_RENDERBUFFER, internal, 1, 1)
            gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, draw_fbo)
            gl.glFramebufferRenderbuffer(gl.GL_DRAW_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                         gl.GL_RENDERBUFFER, target)
            gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, read_fbo)
            gl.glBlitFramebuffer(0, 0, 1, 1, 0, 0, 1, 1, gl.GL_COLOR_BUFFER_BIT, gl.GL_NEAREST)
            gl.glFinish()
            gl.glTexSubImage3D(gl.GL_TEXTURE_2D_ARRAY, 0, 0, 0, layers - 1, 1, 1, 1,
                               fmt_gl, typ, value)
        finally:
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
            gl.glBindRenderbuffer(gl.GL_RENDERBUFFER, 0)
            gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, 0)
            gl.glDeleteFramebuffers(2, [read_fbo, draw_fbo])
            gl.glDeleteRenderbuffers(1, [target])
            while gl.glGetError() != gl.GL_NO_ERROR:     # a refused blit is not fatal
                pass

    @staticmethod
    def _gl_format(gl, fmt):
        if fmt == "u8":
            return gl.GL_R8UI, gl.GL_RED_INTEGER, gl.GL_UNSIGNED_BYTE
        if fmt == "u16":
            return gl.GL_R16UI, gl.GL_RED_INTEGER, gl.GL_UNSIGNED_SHORT
        return gl.GL_R32F, gl.GL_RED, gl.GL_FLOAT

    def _drop_block(self, gl, block) -> None:
        gl.glDeleteTextures([block.texture])
        self.blocks.remove(block)
        for blocks in self.channel_blocks.values():
            if block in blocks:
                blocks.remove(block)

    def _free_slot(self, gl, fmt, channel, required) -> Tuple[_ArrayBlock, int]:
        preferred = [b for b in self.channel_blocks.get(channel, ()) if b.fmt == fmt and b.free]
        # the fullest block first: an empty reserve block is used last
        others = sorted((b for b in self.blocks if b.fmt == fmt and b.free and b not in preferred),
                        key=lambda b: len(b.free))
        for block in preferred + others:
            return block, block.free.pop()
        block_bytes = ARRAY_TILE * ARRAY_TILE * _FORMAT_BYTES[fmt] * self._layers(fmt)
        # room for a new block: delete EMPTY blocks of any format first
        while self.allocated_bytes + self.external_bytes() + block_bytes > self.max_bytes:
            empty = next((b for b in self.blocks if not b.owners), None)
            if empty is None:
                break
            self._drop_block(gl, empty)
        if self.allocated_bytes + self.external_bytes() + block_bytes <= self.max_bytes:
            if not getattr(self, "_may_grow", True):
                raise _NoGrowth()
            block = self._new_block(gl, fmt)
            self.channel_blocks.setdefault(channel, []).append(block)
            return block, block.free.pop()
        # reuse the least recently used slot of this format not needed now
        # (the order is sorted once per prepare, on the first eviction)
        if self._evict_order is None:
            self._evict_order = collections.deque(
                sorted((i for i in self.used if i not in required), key=self.used.__getitem__))
        skipped = []
        try:
            while self._evict_order:
                identity = self._evict_order.popleft()
                if identity not in self.slots:
                    continue
                block, layer = self.slots[identity]
                if block.fmt != fmt:
                    skipped.append(identity)
                    continue
                self._evict(identity)
                block.free.remove(layer)
                return block, layer
        finally:
            self._evict_order.extendleft(reversed(skipped))
        raise Step1GpuLayerError(
            "array store cannot fit the active raw working set without deleting an active tile")

    def _evict(self, identity) -> None:
        block, layer = self.slots.pop(identity)
        self.used.pop(identity, None)
        self.meta.pop(identity, None)
        block.owners.pop(layer, None)
        block.free.append(layer)
        self.evictions += 1
        self.meta_generation += 1
        self.meta_log.append((block, layer, None))

    def prepare(self, gl, items, *, mandatory=frozenset(), budget_ms=None, validate=None,
                also_required=frozenset(), required_fn=None, may_grow=True) -> None:
        """`items`: (channel, plane) of the submission, in draw order;
        `also_required`: identities of the submission not listed (resident
        already) that must not be evicted either; `required_fn`: or a
        function giving the WHOLE submission's identities, called only if an
        eviction is needed (A9 §35: hashing every key every publication was
        the cost)."""
        self._prepare_started = time.perf_counter()
        self.deferred = 0
        self._evict_order = None
        ids = [plane.identity for _channel, plane in items]
        required = _LazyRequired(set(ids) | set(also_required), required_fn)
        slots = self.slots
        # recency: one bulk update (an identity's geometry was validated
        # when it entered; the identity is the caller's promise of the rest)
        self.tick += 1
        resident = [i for i in ids if i in slots]
        self.used.update(dict.fromkeys(resident, self.tick))
        self.hits += len(resident)
        missing = []
        seen = set()
        checked = self._validated
        for channel, plane in items:
            identity = plane.identity
            if identity in slots or identity in seen:
                continue
            # a plane waiting for its upload slot is validated ONCE, not on
            # every publication it waits through (A9 §35: 230 waiting planes
            # cost 9 ms per publication with 69 channels)
            if validate is not None and checked.get(id(plane)) is not plane:
                validate(plane)
                if len(checked) > 50000:
                    checked.clear()
                checked[id(plane)] = plane
            missing.append((channel, plane))
            seen.add(identity)
        missing.sort(key=lambda item: item[1].identity not in mandatory)
        self._may_grow = may_grow
        started = time.perf_counter()
        if perf_trace.enabled():
            perf_trace.mark("gpu.array_prepare", items=len(items), missing=len(missing),
                            scan_ms=round((started - self._prepare_started) * 1000.0, 2))
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
        for channel, plane in missing:
            if (budget_ms is not None and plane.identity not in mandatory
                    and (time.perf_counter() - started) * 1000.0 >= budget_ms):
                self.deferred += 1
                continue
            fmt = _array_format(plane)
            # a channel's complete coarse may always grow the store
            self._may_grow = may_grow or plane.identity in mandatory
            try:
                block, layer = self._free_slot(gl, fmt, channel, required)
            except _NoGrowth:
                # the user is moving: no new block now (its first use costs
                # tens of ms); this and the rest wait, coarser planes stand in
                self.deferred += 1
                continue
            values = self._upload_values(plane, fmt)
            _internal, fmt_gl, typ = self._gl_format(gl, fmt)
            gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, block.texture)
            gl.glTexSubImage3D(gl.GL_TEXTURE_2D_ARRAY, 0, 0, 0, layer,
                               values.shape[1], values.shape[0], 1, fmt_gl, typ, values)
            identity = plane.identity
            block.owners[layer] = identity
            self.slots[identity] = (block, layer)
            self.used[identity] = self.tick
            valid = _valid_world_rect(plane)
            self.meta[identity] = (plane.world_rect, valid,
                                   (values.shape[1], values.shape[0]), fmt)
            self.uploads += 1
            self.meta_generation += 1
            self.meta_log.append((block, layer, identity))
        gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, 0)
        _check_gl(gl, "array tile upload")

    @staticmethod
    def _upload_values(plane, fmt) -> np.ndarray:
        values = np.asarray(plane.values)
        if fmt in ("u8", "u16"):
            return np.ascontiguousarray(values)
        values = np.ascontiguousarray(values, dtype=np.float32).copy()
        if plane.valid is not None:
            values[~np.asarray(plane.valid, dtype=bool)] = np.nan
        if plane.valid_rect is not None:
            y0, y1, x0, x1 = plane.valid_rect
            keep = np.zeros(values.shape, bool)
            keep[y0:y1, x0:x1] = True
            values[~keep] = np.nan
        return values

    def instance(self, plane):
        """(block, record) of a resident plane, or None when it draws nothing."""
        block, layer = self.slots[plane.identity]
        rect, valid, size, _fmt = self.meta[plane.identity]
        if valid is None:
            return None
        return block, (rect, valid, size, layer)

    def ensure_headroom(self, gl, fraction: float = 1.0, family_limits=(6, 4)) -> bool:
        """A9 §35: allocate the NEXT block of a format before it is needed.
        A new 256 MiB block costs the driver ~100 ms on first use (measured:
        a 101-111 ms frame at each new block); allocated here, while the
        user is not moving, and written once, it is ready when tiles land.
        Only when a format's free slots fall below `fraction` of a block
        and the budget (and the single pass's samplers) allow another."""
        made = False
        for fmt in {block.fmt for block in self.blocks}:
            same = [b for b in self.blocks if b.fmt == fmt]
            free = sum(len(b.free) for b in same)
            layers = self._layers(fmt)
            limit = family_limits[1] if fmt == "f32" else family_limits[0]
            family = [b for b in self.blocks if (b.fmt == "f32") == (fmt == "f32")]
            block_bytes = ARRAY_TILE * ARRAY_TILE * _FORMAT_BYTES[fmt] * layers
            if (free >= fraction * layers or len(family) >= limit
                    or self.allocated_bytes + self.external_bytes() + block_bytes > self.max_bytes):
                continue
            self._new_block(gl, fmt)            # touched on the GPU there
            gl.glFinish()
            made = True
        return made

    def release_unused(self, gl, keep) -> int:
        """Drop every tile not in `keep`, then every block left empty."""
        for identity in [i for i in self.slots if i not in keep]:
            self._evict(identity)
        empty = [b for b in self.blocks if not b.owners]
        for block in empty:
            self._drop_block(gl, block)
        return len(empty)

    def clear(self, gl) -> int:
        count = len(self.slots)
        if self.blocks:
            gl.glDeleteTextures([block.texture for block in self.blocks])
        self.blocks.clear()
        self.slots.clear()
        self.used.clear()
        self.meta.clear()
        self.channel_blocks.clear()
        return count

    def stats(self) -> Dict[str, int]:
        return {"array_blocks": len(self.blocks), "array_bytes": self.allocated_bytes,
                "array_peak_bytes": self.peak_bytes, "array_tiles": len(self.slots),
                "array_uploads": self.uploads, "array_evictions": self.evictions,
                "array_storage_fallback": int(self.storage_fallback)}


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


def roi_scissor_box(view_rect, roi_rect, size):
    """The output pixels whose CENTRE lies inside `roi_rect`.

    `(x, y, width, height)` in GL framebuffer pixels (origin bottom-left),
    or `None` when there is nothing to clip to. A pixel is kept when its
    centre is inside the region, which is the same rule the rest of the
    pipeline uses for "which sample is this" -- no pixel is half-kept and
    the edge cannot drift with the camera, because it is recomputed from
    the world rectangle every submission.

    The vertical flip is the shader's: `v_screen_uv.y = 0` is the BOTTOM of
    the framebuffer and maps to `world.y = view_rect.w` (see
    `ui/shaders/step1_gpu.frag`, PASS_SOURCE), so world y grows downward on
    screen.
    """
    if roi_rect is None:
        return None
    vx0, vx1, vy0, vy1 = (float(v) for v in view_rect)
    bx0, bx1, by0, by1 = (float(v) for v in roi_rect)
    width, height = int(size[0]), int(size[1])
    if width <= 0 or height <= 0 or vx1 <= vx0 or vy1 <= vy0:
        return (0, 0, 0, 0)
    if bx1 <= bx0 or by1 <= by0:
        return (0, 0, 0, 0)
    step_x = (vx1 - vx0) / width
    step_y = (vy1 - vy0) / height
    # columns: centre = vx0 + (i + 0.5) * step_x
    first_col = math.ceil((bx0 - vx0) / step_x - 0.5)
    stop_col = math.ceil((bx1 - vx0) / step_x - 0.5)
    # rows, from the bottom: centre = vy1 - (j + 0.5) * step_y
    first_row = math.floor((vy1 - by1) / step_y - 0.5) + 1
    stop_row = math.floor((vy1 - by0) / step_y - 0.5) + 1
    x = max(0, min(width, first_col))
    right = max(0, min(width, stop_col))
    y = max(0, min(height, first_row))
    top = max(0, min(height, stop_row))
    return (x, y, max(0, right - x), max(0, top - y))


def _as_name(value) -> int:
    if isinstance(value, (tuple, list, np.ndarray)):
        return int(value[0])
    return int(value)


class _GlTimer:
    """A9 §32 measurement only (`BLOCK01_A9_GLPROF=1`): wall time per GL
    function, reported by `gpu.glprof` after each composition."""

    def __init__(self, gl):
        self._real = gl
        self.totals = collections.Counter()
        self.calls = collections.Counter()

    def __getattr__(self, name):
        value = getattr(self._real, name)
        if not callable(value) or not name.startswith("gl"):
            return value

        def timed(*args, **kwargs):
            started = time.perf_counter()
            try:
                return value(*args, **kwargs)
            finally:
                self.totals[name] += time.perf_counter() - started
                self.calls[name] += 1
        return timed

    def report(self, where):
        top = self.totals.most_common(4)
        perf_trace.mark("gpu.glprof", where=where, **{
            name: f"{seconds * 1000:.2f}/{self.calls[name]}" for name, seconds in top})
        self.totals.clear()
        self.calls.clear()


def configure_pyopengl() -> None:
    """Block A9 §32: PROCESS-WIDE PyOpenGL policy, set before `OpenGL.GL`
    is first imported (later it has no effect). Automatic error checking
    calls glGetError after EVERY GL call -- each one a further GIL release
    and re-acquire on the GUI thread while tile readers run. It is off;
    this layer's explicit `_check_gl` checkpoints (setup, uploads,
    submissions, readback) still raise. `BLOCK01_GL_CHECK=1` keeps
    PyOpenGL's own checking on, for diagnosis."""
    import OpenGL
    if os.environ.get("BLOCK01_GL_CHECK") != "1":
        OpenGL.ERROR_CHECKING = False
    if os.environ.get("BLOCK01_GL_KEEP_GIL", "1") != "0":
        # GL CALLS KEEP THE GIL. ctypes' ordinary function type releases
        # the GIL around every call and re-acquires it afterwards; with the
        # tile readers running, each re-acquisition waited in line behind
        # them -- measured on Kevin: 18 glDisable calls 17 ms, a 21-pass
        # composition 27-70 ms instead of 3-4 ms. A GL call on this thread
        # is microseconds of driver work: holding the GIL through it costs
        # the readers nothing they would notice. PyOpenGL builds every
        # function with `functionTypeFor(dll)`, which honours the
        # library's `FunctionType`; set before `OpenGL.GL` is imported.
        import ctypes
        from OpenGL import platform as gl_platform
        gl_platform.PLATFORM.GL.FunctionType = ctypes.PYFUNCTYPE


def _check_gl(gl, operation: str) -> None:
    error = gl.glGetError()
    if error != gl.GL_NO_ERROR:
        raise Step1GpuLayerError(f"OpenGL error after {operation}: 0x{int(error):04x}")


def _decode(value) -> str:
    return "unavailable" if value is None else bytes(value).decode("ascii", "replace")


class Step1GpuLayer(QtWidgets.QOpenGLWidget):
    """A test-only G1 multi-pass GPU compositor with no production import path."""

    def __init__(self, *, max_raw_texture_bytes: int, require_hardware: bool = True,
                 parent: Optional[QtWidgets.QWidget] = None, labels: bool = False,
                 max_label_texture_bytes: int = LABEL_TEXTURE_BYTES):
        super().__init__(parent)
        self._cache = _TextureLru(max_raw_texture_bytes)
        # block A9-M: which submission the next presented frame shows
        self._a9_frame = 0
        self._a9_pending = None
        if perf_trace.enabled():
            self.frameSwapped.connect(self._a9_presented)
        #: Block 4b: the label layer, Step3 only. Off (the default), nothing
        #: below exists: no target, no program, no texture, and `paintGL`
        #: shows `final` exactly as before.
        self._labels_enabled = bool(labels)
        self._label_cache = (_LabelTextureLru(max_label_texture_bytes)
                             if self._labels_enabled else None)
        self._label_snapshot = LabelSnapshot()
        #: The viewport and the clip `final` was last composed with: a
        #: label-only redraw uses them, so it lands on the same picture.
        self._last_viewport: Optional[ViewportSnapshot] = None
        self._last_clip: Optional[Tuple[Any, bool]] = None
        self._shown_ready = False
        self._label_counts = {"image_submissions": 0, "label_compositions": 0,
                              "label_passes": 0}
        self._require_hardware = bool(require_hardware)
        self._gl = None
        self._programs: Dict[str, int] = {}
        self._uniforms: Dict[str, Dict[str, int]] = {}
        self._vao = 0
        #: The polygon mask's own GL names (G3.2c.1). Created on first use,
        #: destroyed with every other GL name this layer owns.
        self._mask_program = 0
        self._mask_vao = 0
        self._mask_vbo = 0
        #: The stencil the mask writes into. It belongs to the `final`
        #: target and is recreated whenever that target is.
        self._stencil_rbo = 0
        #: Why the last submission's polygon was not used, "" when it was.
        #: A refused polygon is never silently the same as no polygon: this
        #: is set, `submit()` reports it, and the rectangle still clips.
        self._roi_polygon_error = ""
        self._targets: Dict[str, Tuple[int, int]] = {}
        self._target_size: Optional[Tuple[int, int]] = None
        self._initialized = False
        self._init_error: Optional[Exception] = None
        self._disposed = False
        self._capabilities: Dict[str, Any] = {}
        self._submission: Dict[str, Any] = {}
        self._attached_view = None
        self._attached_viewport = None
        self._attached_range = None
        #: Block A9 §32 (Odon: the picture follows the camera every frame):
        #: the last submitted scene, the world rect it was composed for,
        #: and whether the attached camera has moved since.
        self._scene = None
        self._composed_rect = None
        self._camera_dirty = False
        #: Block A9 §32: per-submission upload time budget (None: no limit)
        self.upload_budget_ms: Optional[float] = None
        #: A9 §35: the binding sets this while the user is moving -- no new
        #: array block is created then (mandatory coarse still always is)
        self.hold_growth = False
        #: Block A9 §35: the Step1 binding turns the tile array store on;
        #: every other caller (G1 tests, montage) keeps one texture per plane
        self.use_tile_arrays = False
        self._arrays: Optional[_TileArrayStore] = None
        self._array_vao = 0
        self._array_vbo = 0
        self._max_array_layers = 0
        #: A9 §35.7: the single-pass compositor (None until first used)
        self._vt = None
        self._vt_failed = ""
        self._vt_split = (6, 4)
        #: A9 §35: id(plane) -> plane for planes known to be in the arrays
        self._known_resident: Dict[int, RawPlane] = {}
        self._known_evictions = -1
        fmt = QtGui.QSurfaceFormat()
        fmt.setRenderableType(QtGui.QSurfaceFormat.OpenGL)
        fmt.setVersion(3, 3)
        fmt.setProfile(QtGui.QSurfaceFormat.CoreProfile)
        self.setFormat(fmt)

    # Public G1 boundary -------------------------------------------------

    @property
    def initialized(self) -> bool:
        """Did Qt realize this widget's context and did G1 set itself up?

        Read-only. A caller that must decide between the GPU backend and the
        existing CPU path asks this instead of reaching for private state,
        and it is never allowed to report success on a failed setup.
        """
        return bool(self._initialized)

    @property
    def init_error(self) -> Optional[Exception]:
        """Why realization failed, exactly as raised. None while it has not."""
        return self._init_error

    def attach(self, view_adapter) -> None:
        """Sibling overlay attachment on an existing view; it never changes camera/input state."""
        if self._initialized:
            raise Step1GpuLayerError("attach must happen before QOpenGLWidget realization")
        if not hasattr(view_adapter, "graphics") or not hasattr(view_adapter, "view_box"):
            raise Step1GpuLayerError("attach expects an existing ExploreView-like adapter")
        self._attached_view = view_adapter
        self._attached_viewport = view_adapter.graphics.viewport()
        if self.parent() is not self._attached_viewport:
            self.setParent(self._attached_viewport)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self._attached_viewport.installEventFilter(self)
        view_adapter.view_box.sigRangeChanged.connect(self._attached_range_changed)
        # Block A1 (C1): the layer covers the ViewBox, not the whole viewport,
        # so it follows the ViewBox's own geometry as well.
        view_adapter.view_box.sigResized.connect(self._sync_attached_geometry)
        self._sync_attached_geometry()

    @perf_trace.timed("gpu.submit")
    def submit(self, source_descriptor: SourceDescriptor, display_snapshot: DisplaySnapshot,
               viewport_snapshot: ViewportSnapshot) -> Dict[str, Any]:
        """Synchronously render caller-provided snapshots; no I/O or CPU fallback.

        Block A9 §32: the layer's `upload_budget_ms` bounds each call's
        texture uploads (None, the default: everything, the G1 contract; the
        Step1 binding sets it); `deferred_uploads` in the result says how
        many planes were left for a later submission."""
        self._validate_snapshots(source_descriptor, display_snapshot, viewport_snapshot)
        own_current = QtGui.QOpenGLContext.currentContext() is not self.context()
        if own_current:
            self.makeCurrent()
        try:
            self._require_ready()
            result = self._compose(source_descriptor, display_snapshot, viewport_snapshot,
                                   upload=True, upload_budget_ms=self.upload_budget_ms)
            self._scene = (source_descriptor, display_snapshot, viewport_snapshot)
            self._camera_dirty = False
            if callable(viewport_snapshot.repaint_request):
                viewport_snapshot.repaint_request()
            self.update()
            return result
        finally:
            if own_current:
                self.doneCurrent()

    def residency_generation(self) -> int:
        """Changes whenever a texture is uploaded or evicted (A9 §35)."""
        arrays = self._arrays.meta_generation if self._arrays is not None else 0
        return arrays * 1000003 + self._cache.uploads * 1009 + self._cache.evictions

    def is_resident(self, plane: RawPlane) -> bool:
        """Is this plane's texture on the GPU (drawable now)?"""
        return self._cache.is_resident(plane) or (
            self._arrays is not None and self._arrays.is_resident(plane))

    @property
    def tile_arrays_active(self) -> bool:
        """Block A9 §35: does this layer store tiles in arrays? Only when
        asked to AND its budget holds a useful store (64 float32 slots);
        a smaller budget keeps one texture per plane, as before."""
        return bool(self.use_tile_arrays
                    and self._cache.max_bytes >= 64 * ARRAY_TILE * ARRAY_TILE * 4)

    def _array_store(self) -> Optional[_TileArrayStore]:
        """Block A9 §35: the tile array store, when this layer uses one."""
        if not self.tile_arrays_active:
            return None
        if self._arrays is None:
            self._arrays = _TileArrayStore(self._cache.max_bytes, self._max_array_layers or ARRAY_LAYERS)
            self._arrays.external_bytes = lambda: self._cache.bytes
            self._cache.external_bytes = lambda: (self._arrays.allocated_bytes
                                                  if self._arrays is not None else 0)
        return self._arrays

    def prepare_headroom(self) -> bool:
        """A9 §35: the binding calls this when the user is NOT moving: the
        next array block, if one will soon be needed, is allocated now."""
        if self._arrays is None or not self._initialized or self._disposed:
            return False
        own_current = QtGui.QOpenGLContext.currentContext() is not self.context()
        if own_current:
            self.makeCurrent()
        try:
            with perf_trace.span("gpu.headroom"):
                u, f = getattr(self, "_vt_split", (6, 4))
                return self._arrays.ensure_headroom(self._gl, family_limits=(u, f))
        finally:
            if own_current:
                self.doneCurrent()

    def array_slack_bytes(self, itemsizes=(1, 2, 4)) -> int:
        """Block A9 §35: what whole-block allocation may hold beyond the
        tiles it stores -- one partly filled block per format in use
        (`itemsizes`, bytes per texel); admission keeps it free."""
        if not self.tile_arrays_active:
            return 0
        return sum(_block_layers(self._cache.max_bytes, self._max_array_layers, int(i))
                   * ARRAY_TILE * ARRAY_TILE * int(i) for i in set(itemsizes))

    def _visible_drawn(self, source: ChannelSource, view_rect) -> ChannelSource:
        """Block A9 §32 (Odon: draw list culled to the view): the planes of
        `source` that are resident AND meet the view. What is skipped is
        exactly what could not have put a pixel on screen, or what has no
        texture yet (a coarser plane under it still draws)."""
        vx0, vx1, vy0, vy1 = view_rect

        arrays = self._arrays if self.tile_arrays_active else None

        def keep(plane):
            x0, x1, y0, y1 = plane.world_rect
            return (x1 > vx0 and x0 < vx1 and y1 > vy0 and y0 < vy1
                    and (self._cache.is_resident(plane)
                         or (arrays is not None and arrays.is_resident(plane))))
        coarse = tuple(plane for plane in source.coarse if keep(plane))
        fine = tuple(plane for plane in source.fine if keep(plane))
        if len(coarse) == len(source.coarse) and len(fine) == len(source.fine):
            return source
        return dataclasses.replace(source, coarse=coarse, fine=fine)

    def _compose(self, source_descriptor: SourceDescriptor, display_snapshot: DisplaySnapshot,
                 viewport_snapshot: ViewportSnapshot, *, upload: bool,
                 upload_budget_ms: Optional[float] = None) -> Dict[str, Any]:
        """Compose one picture into `final` (and `shown`) with this context
        current. `upload` False (a camera-only repaint from `paintGL`)
        uploads nothing: it draws what is resident."""
        submit_started = time.perf_counter()
        gl = self._gl
        self._ensure_targets(viewport_snapshot.physical_size)
        self._submission = {"pass_count": 0}
        by_channel = source_descriptor.by_channel()
        mode = display_snapshot.mode
        view_rect = viewport_snapshot.world_rect
        deferred = 0

        def prepare(active_sources):
            nonlocal deferred
            if not upload:
                return
            arrays = self._array_store()
            if arrays is None:
                planes = [plane for source in active_sources for plane in source.selected_planes()]
                mandatory = [plane for source in active_sources for plane in source.coarse]
                with perf_trace.span("gpu.prepare"):
                    self._cache.prepare(gl, planes, mandatory=mandatory,
                                        budget_ms=upload_budget_ms)
                deferred = int(getattr(self._cache, "deferred", 0))
                return
            # Block A9 §35: tiles into the array store, anything else per plane
            items, legacy, legacy_mandatory = [], [], []
            mandatory = set()
            slots = arrays.slots
            # A9 §35: planes KNOWN to be in the arrays, by object (planes are
            # immutable; an eviction forgets them all) -- a publication only
            # touches what is new, without hashing a single key for the rest
            if self._known_evictions != arrays.evictions:
                self._known_resident.clear()
                self._known_evictions = arrays.evictions
            known = self._known_resident
            for source in active_sources:
                channel = source.channel
                coarse = {id(plane) for plane in source.coarse}
                for plane in source.selected_planes():
                    if known.get(id(plane)) is plane:
                        continue
                    identity = plane.identity
                    if identity in slots:
                        known[id(plane)] = plane
                        continue
                    if arrays.eligible(plane):
                        items.append((channel, plane))
                        if id(plane) in coarse:
                            mandatory.add(identity)
                    else:
                        legacy.append(plane)
                        if id(plane) in coarse:
                            legacy_mandatory.append(plane)

            def everything():
                return [plane.identity for source in active_sources
                        for plane in source.selected_planes()]
            with perf_trace.span("gpu.prepare"):
                if legacy:
                    self._cache.prepare(gl, legacy, mandatory=legacy_mandatory,
                                        budget_ms=upload_budget_ms)
                arrays.prepare(gl, items, mandatory=mandatory, budget_ms=upload_budget_ms,
                               validate=self._cache._validate_identity, required_fn=everything,
                               may_grow=not self.hold_growth)
            if self._known_evictions != arrays.evictions:
                self._known_resident.clear()
                self._known_evictions = arrays.evictions
            for _channel, plane in items:
                if plane.identity in slots:
                    known[id(plane)] = plane
            deferred = int(arrays.deferred) + (int(getattr(self._cache, "deferred", 0)) if legacy else 0)

        if mode == MODE_OVERLAY:
            active, missing = self._overlay_active(by_channel, display_snapshot)
            prepare(list(active.values()))
            rows = [(ch, src, {"mapping": display_snapshot.mappings[ch],
                               "weight": min(1.0, float(display_snapshot.weights.get(ch, 0.0) or 0.0)),
                               "color": display_snapshot.colors.get(ch, (1.0, 1.0, 1.0))})
                    for ch, src in active.items()]
            with perf_trace.span("gpu.render", mode="overlay", channels=len(active)):
                drawn = self._render_vt(source_descriptor, rows, viewport_snapshot, fusion=False)
                if not drawn or self._a9_probing():
                    # the per-channel draw list (and the A9 probe's view of it)
                    active = {ch: self._visible_drawn(src, view_rect) for ch, src in active.items()}
                    active = {ch: src for ch, src in active.items() if src.selected_planes()}
                if not drawn:
                    self._render_overlay(active, display_snapshot, viewport_snapshot)
            composed = dict(active)
        elif mode == MODE_FUSION:
            active_groups, active_nucleus, missing = self._fusion_active(by_channel, display_snapshot)
            sources = [src for group in active_groups.values() for src in group.values()]
            if active_nucleus is not None:
                sources.append(active_nucleus)
            prepare(sources)
            fusion_rows = []
            for index, (group, members) in enumerate(active_groups.items()):
                group_weight = min(1.0, max(0.0, float(display_snapshot.group_weights.get(group, 1.0) or 0.0)))
                for ch, src in members.items():
                    fusion_rows.append((ch, src, {
                        "mapping": display_snapshot.mappings[ch],
                        "weight": min(1.0, max(0.0, float(display_snapshot.groups[group].get(ch, 0.0) or 0.0))),
                        "color": (1.0, 0.0, 0.0), "group": index, "group_weight": group_weight}))
            if active_nucleus is not None:
                nucleus_channel, nucleus_weight = display_snapshot.nucleus
                fusion_rows.append((nucleus_channel, active_nucleus, {
                    "mapping": display_snapshot.mappings[nucleus_channel],
                    "weight": min(1.0, max(0.0, float(nucleus_weight))),
                    "color": (0.0, 0.0, 1.0), "group": -1, "group_weight": 0.0}))
            active_groups = {
                group: {ch: drawn for ch, drawn in ((ch, self._visible_drawn(src, view_rect))
                                                    for ch, src in members.items())
                        if drawn.selected_planes()}
                for group, members in active_groups.items()}
            active_groups = {group: members for group, members in active_groups.items() if members}
            if active_nucleus is not None:
                active_nucleus = self._visible_drawn(active_nucleus, view_rect)
                if not active_nucleus.selected_planes():
                    active_nucleus = None
            planes = sum(len(src.selected_planes()) for group in active_groups.values()
                         for src in group.values())
            with perf_trace.span("gpu.render", mode="fusion", planes=planes):
                if not self._render_vt(source_descriptor, fusion_rows, viewport_snapshot, fusion=True):
                    self._render_fusion(active_groups, active_nucleus, display_snapshot, viewport_snapshot)
            composed = {ch: src for group in active_groups.values()
                        for ch, src in group.items()}
            if active_nucleus is not None:
                composed[active_nucleus.channel] = active_nucleus
        else:
            raise Step1GpuLayerError(f"unknown display mode {mode!r}")
        if perf_trace.enabled():
            self._a9_note_submission(composed, display_snapshot, viewport_snapshot)
        if self._labels_enabled:
            # THE SAME SUBMISSION, THE SAME VIEW: the mask is drawn over
            # the picture it belongs to, never a frame behind it.
            self._label_counts["image_submissions"] += 1
            self._last_viewport = viewport_snapshot
            self._compose_labels()
        self._composed_rect = view_rect
        self._submission = {
            "mode": mode,
            "missing_windows": tuple(missing),
            "pass_count": int(self._submission.get("pass_count", 0)),
            "cpu_submit_ms": (time.perf_counter() - submit_started) * 1000.0,
            "cache": self.cache_stats(),
            "physical_size": viewport_snapshot.physical_size,
            "roi_scissor": self._submission.get("roi_scissor"),
            "roi_polygon_points": self._submission.get("roi_polygon_points", 0),
            "roi_polygon_error": self._submission.get("roi_polygon_error", ""),
            "deferred_uploads": deferred,
        }
        _check_gl(gl, "G1 submission")
        if isinstance(gl, _GlTimer):
            gl.report("upload" if upload else "camera")
        return dict(self._submission)

    def readback_rgba_for_test(self) -> np.ndarray:
        """Return the final RGBA8 FBO, flipped once to C1 top-left rows."""
        own_current = QtGui.QOpenGLContext.currentContext() is not self.context()
        if own_current:
            self.makeCurrent()
        try:
            self._require_ready()
            if self._target_size is None:
                raise Step1GpuLayerError("submit before requesting test readback")
            gl = self._gl
            final_fbo, _final_texture = self._targets["final"]
            width, height = self._target_size
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, final_fbo)
            gl.glPixelStorei(gl.GL_PACK_ALIGNMENT, 1)
            pixels = gl.glReadPixels(0, 0, width, height, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE)
            result = np.frombuffer(pixels, dtype=np.uint8).reshape(height, width, 4).copy()
            _check_gl(gl, "G1 framebuffer readback")
            return np.flipud(result)
        finally:
            if own_current:
                self.doneCurrent()

    # Block 4b: the label layer ----------------------------------------

    @property
    def labels_enabled(self) -> bool:
        return self._labels_enabled

    def set_labels(self, snapshot: LabelSnapshot) -> Dict[str, Any]:
        """Draw these masks over the picture as it stands. The channels are
        NOT composed again: `final` stays, only `shown` is redrawn, with the
        viewport and clip of the last submission."""
        if not self._labels_enabled:
            raise Step1GpuLayerError("this layer was built without labels")
        if self._disposed:
            raise Step1GpuLayerError("G1 layer is disposed")
        for layer in snapshot.layers:
            if layer.mode not in (LABEL_OUTLINE, LABEL_FILL):
                raise Step1GpuLayerError(f"unknown label mode {layer.mode!r}")
            for plane in layer.planes:
                _LabelTextureLru._validate(plane)
        # REFUSED BEFORE IT IS KEPT: a snapshot over the budget would
        # otherwise fail every later image submission too.
        needed = {plane.identity: _LabelTextureLru.plane_bytes(plane)
                  for layer in snapshot.layers if layer.visible for plane in layer.planes}
        if sum(needed.values()) > self._label_cache.max_bytes:
            raise Step1GpuLayerError(
                f"label working set {sum(needed.values())} bytes exceeds the label budget "
                f"{self._label_cache.max_bytes}")
        self._label_snapshot = snapshot
        if not self._initialized or self._last_viewport is None or self._target_size is None:
            return self.label_stats()
        own_current = QtGui.QOpenGLContext.currentContext() is not self.context()
        if own_current:
            self.makeCurrent()
        try:
            self._require_ready()
            self._compose_labels()
        finally:
            if own_current:
                self.doneCurrent()
        self.update()
        return self.label_stats()

    def label_stats(self) -> Dict[str, Any]:
        stats = dict(self._label_counts)
        stats["shown"] = bool(self._shown_ready)
        stats["cache"] = self._label_cache.stats() if self._label_cache is not None else {}
        return stats

    def readback_shown_for_test(self) -> np.ndarray:
        """What `paintGL` puts on screen: `shown` when masks are drawn, else
        `final`; RGBA8, top-left rows."""
        return self._readback_rgba("shown" if self._shown_ready else "final")

    def readback_label_ids_for_test(self) -> np.ndarray:
        """The screen id image of the LAST mask layer drawn, top-left rows."""
        own_current = QtGui.QOpenGLContext.currentContext() is not self.context()
        if own_current:
            self.makeCurrent()
        try:
            self._require_ready()
            gl = self._gl
            fbo, _texture = self._targets["ids"]
            width, height = self._target_size
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
            gl.glPixelStorei(gl.GL_PACK_ALIGNMENT, 1)
            pixels = gl.glReadPixels(0, 0, width, height, gl.GL_RED_INTEGER, gl.GL_UNSIGNED_INT)
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
            result = np.frombuffer(pixels, dtype=np.uint32).reshape(height, width).copy()
            _check_gl(gl, "label id readback")
            return np.flipud(result)
        finally:
            if own_current:
                self.doneCurrent()

    def _readback_rgba(self, target: str) -> np.ndarray:
        own_current = QtGui.QOpenGLContext.currentContext() is not self.context()
        if own_current:
            self.makeCurrent()
        try:
            self._require_ready()
            gl = self._gl
            fbo, _texture = self._targets[target]
            width, height = self._target_size
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
            gl.glPixelStorei(gl.GL_PACK_ALIGNMENT, 1)
            pixels = gl.glReadPixels(0, 0, width, height, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE)
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
            result = np.frombuffer(pixels, dtype=np.uint8).reshape(height, width, 4).copy()
            _check_gl(gl, f"{target} readback")
            return np.flipud(result)
        finally:
            if own_current:
                self.doneCurrent()

    def dispose(self) -> Dict[str, Any]:
        """Delete every G1 GL name under the active Qt context; safe twice."""
        if self._disposed:
            return {"already_disposed": True, "raw_textures_remaining": 0,
                    "transient_targets_remaining": 0, "cache_bytes": 0, "threads_created": 0}
        self._disconnect_attachment()
        self._scene = None
        self._camera_dirty = False
        if self.context() is not None and self.context().isValid():
            self.makeCurrent()
            try:
                self._destroy_gl_names()
            finally:
                self.doneCurrent()
        self._disposed = True
        return {"already_disposed": False,
                "raw_textures_remaining": len(self._cache.records) + (
                    len(self._arrays.slots) if self._arrays is not None else 0),
                "transient_targets_remaining": len(self._targets), "cache_bytes": self._cache.bytes,
                "threads_created": 0}

    def roi_polygon_error(self) -> str:
        """Why the last submission's ROI polygon was refused, or ""."""
        return self._roi_polygon_error

    def cache_stats(self) -> Dict[str, int]:
        stats = dict(self._cache.stats())
        if self._arrays is not None:
            # A9 §35: tiles in the array store are textures resident too
            stats.update(self._arrays.stats())
            stats["textures"] = int(stats.get("textures", 0)) + len(self._arrays.slots)
            stats["bytes"] = int(stats.get("bytes", 0)) + self._arrays.allocated_bytes
        return stats

    def environment_report(self) -> Dict[str, Any]:
        return dict(self._capabilities)

    # Qt lifecycle -------------------------------------------------------

    @perf_trace.timed("gpu.init")
    def initializeGL(self) -> None:  # noqa: N802
        context = self.context()
        if context is None or not context.isValid():
            self._init_error = Step1GpuLayerError("Qt did not create a valid G1 OpenGL context")
            return
        try:
            with perf_trace.span("gpu.init.import"):
                configure_pyopengl()
                from OpenGL import GL
                import OpenGL
            self._gl = GL
            if os.environ.get("BLOCK01_A9_GLPROF") == "1":     # A9 §32 measurement only
                self._gl = _GlTimer(GL)
            actual = context.format()
            renderer = _decode(GL.glGetString(GL.GL_RENDERER))
            vendor = _decode(GL.glGetString(GL.GL_VENDOR))
            renderer_lower = f"{vendor} {renderer}".lower()
            self._capabilities = {
                "pyopengl_version": OpenGL.__version__,
                "pyopengl_file": OpenGL.__file__,
                "gl_vendor": vendor,
                "gl_renderer": renderer,
                "gl_version": _decode(GL.glGetString(GL.GL_VERSION)),
                "glsl_version": _decode(GL.glGetString(GL.GL_SHADING_LANGUAGE_VERSION)),
                "actual_format": {"major": actual.majorVersion(), "minor": actual.minorVersion(),
                                  "profile": int(actual.profile())},
                "max_texture_image_units": int(GL.glGetIntegerv(GL.GL_MAX_TEXTURE_IMAGE_UNITS)),
                "max_texture_size": int(GL.glGetIntegerv(GL.GL_MAX_TEXTURE_SIZE)),
                "software_renderer": any(item in renderer_lower for item in ("llvmpipe", "softpipe", "swiftshader", "software")),
                "raw_texture_format": "GL_R32F / GL_RED / GL_FLOAT",
                "transient_format": "GL_RGBA32F",
            }
            if actual.majorVersion() < 3:
                raise Step1GpuLayerError("G1 requires an OpenGL 3.3 Core-capable Qt context")
            if self._require_hardware and self._capabilities["software_renderer"]:
                raise Step1GpuLayerError(f"G1 requires hardware renderer, got {renderer!r}")
            with perf_trace.span("gpu.init.compile"):
                self._compile_programs()
            self._vao = _as_name(GL.glGenVertexArrays(1))
            self._max_array_layers = int(GL.glGetIntegerv(GL.GL_MAX_ARRAY_TEXTURE_LAYERS))
            self._capabilities["max_array_texture_layers"] = self._max_array_layers
            self._setup_array_vao()
            _check_gl(GL, "G1 shader setup")
            self._initialized = True
        except (ImportError, RuntimeError, Step1GpuLayerError) as exc:
            self._init_error = exc if isinstance(exc, Step1GpuLayerError) else Step1GpuLayerError(str(exc))
            self._destroy_gl_names()

    def resizeGL(self, width: int, height: int) -> None:  # noqa: N802
        # FBO allocation follows explicit viewport snapshots in submit().
        # Qt logical resize alone is deliberately not a source/camera owner.
        del width, height

    @staticmethod
    def _a9_expected(display) -> Tuple[str, ...]:
        """Every channel this display should compose (weight > 0), whether
        or not it has a source yet."""
        if display.mode == MODE_FUSION:
            out = {ch for group, members in display.groups.items()
                   if float(display.group_weights.get(group, 1.0) or 0.0) > 0.0
                   for ch, weight in members.items() if float(weight or 0.0) > 0.0}
            nucleus, weight = display.nucleus
            if nucleus and float(weight or 0.0) > 0.0:
                out.add(nucleus)
            return tuple(sorted(out))
        return tuple(sorted(ch for ch, weight in display.weights.items()
                            if float(weight or 0.0) > 0.0))

    @staticmethod
    def _a9_probing() -> bool:
        return perf_trace.enabled() and os.environ.get("BLOCK01_A9_PROBE", "1") != "0"

    def _a9_note_submission(self, composed, display, viewport) -> None:
        """Block A9-M: the coverage of THIS submission -- the planes actually
        composed, against every channel it should compose, clipped to the
        region's rectangle and polygon -- emitted when the frame showing it
        is presented."""
        if os.environ.get("BLOCK01_A9_PROBE", "1") == "0":
            # A9 P3-M, measurement only: frames are still numbered and their
            # presentation marked, but not probed -- the probe's own cost
            # grows with the plane count and dominated 69-channel frames
            self._a9_frame += 1
            self._a9_pending = (self._a9_frame, {"probe": "off"},
                                getattr(self, "_a9_input_seq", None))
            return
        started = time.perf_counter()
        try:
            result = coverage_probe.gpu_frame(
                composed, viewport.world_rect, roi_world=viewport.roi_world_rect,
                polygon=viewport.roi_polygon_world, expected=self._a9_expected(display),
                # A9 M0: the numeric level owed, set by the binding while tracing
                target_index=getattr(self, "_a9_target_index", None))
            result["probe_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
            perf_trace.mark("coverage.probe", where="gpu", probe_ms=result["probe_ms"])
        except Exception as exc:                            # noqa: BLE001 -- measuring only
            result = {"error": str(exc)[:120]}
        self._a9_frame += 1
        self._a9_pending = (self._a9_frame, result, getattr(self, "_a9_input_seq", None))

    def _a9_presented(self) -> None:
        pending, self._a9_pending = self._a9_pending, None
        if pending is None:
            return
        frame, result, seq = pending
        # §30 F6: `seq` = how many received wheel notches this frame includes
        perf_trace.mark("gpu.present", frame=frame, seq=seq)
        coverage_probe.publish(result, "gpu", frame)

    @perf_trace.timed("gpu.paint")
    def paintGL(self) -> None:  # noqa: N802
        if not self._initialized or self._target_size is None:
            return
        if not self._camera_dirty and self._scene is not None and self._attached_range is not None:
            # A9 §32 (codex): a new size or pixel ratio alone also needs a
            # composition at that size -- not the old one stretched
            live = self._live_viewport()
            if live is not None and live.physical_size != self._target_size:
                self._camera_dirty = True
        if self._camera_dirty:
            self._recompose_for_camera()
        gl = self._gl
        final_fbo, final_texture = self._targets["shown" if self._shown_ready else "final"]
        gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, final_fbo)
        gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, self.defaultFramebufferObject())
        # Block A1 (C6): the default framebuffer is in DEVICE pixels; the
        # widget's width / height are logical. At DPR 1 the two are equal.
        ratio = float(self.devicePixelRatioF()) or 1.0
        width = int(round(self.width() * ratio))
        height = int(round(self.height() * ratio))
        source_width, source_height = self._target_size
        gl.glBlitFramebuffer(0, 0, source_width, source_height, 0, 0, width, height,
                             gl.GL_COLOR_BUFFER_BIT, gl.GL_NEAREST)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        del final_texture

    def eventFilter(self, watched, event):  # noqa: N802
        if watched is self._attached_viewport and event.type() in (QtCore.QEvent.Resize, QtCore.QEvent.Move, QtCore.QEvent.Show):
            self._sync_attached_geometry()
        return False

    # Snapshot validation and active selection -------------------------

    def _validate_snapshots(self, source: SourceDescriptor, display: DisplaySnapshot,
                            viewport: ViewportSnapshot) -> None:
        if self._disposed:
            raise Step1GpuLayerError("G1 layer is disposed")
        if display.mode not in (MODE_OVERLAY, MODE_FUSION):
            raise Step1GpuLayerError(f"unsupported display mode {display.mode!r}")
        x0, x1, y0, y1 = viewport.world_rect
        if not x1 > x0 or not y1 > y0:
            raise Step1GpuLayerError("viewport world rect must have positive extent")
        if viewport.logical_size[0] <= 0 or viewport.logical_size[1] <= 0 or viewport.device_pixel_ratio <= 0:
            raise Step1GpuLayerError("viewport dimensions and DPR must be positive")
        source.by_channel()

    def _overlay_active(self, sources: Mapping[str, ChannelSource], display: DisplaySnapshot):
        active, missing = {}, []
        for channel, source in sources.items():
            weight = float(display.weights.get(channel, 0.0) or 0.0)
            if weight <= 0.0:
                continue
            if channel not in display.mappings:
                missing.append(channel)
                continue
            if not source.selected_planes():
                continue
            active[channel] = source
        return active, missing

    def _fusion_active(self, sources: Mapping[str, ChannelSource], display: DisplaySnapshot):
        active_groups: Dict[str, Dict[str, ChannelSource]] = {}
        missing = []
        for group, weights in display.groups.items():
            group_weight = float(display.group_weights.get(group, 1.0) or 0.0)
            if group_weight <= 0.0:
                continue
            group_sources = {}
            for channel, channel_weight in weights.items():
                if float(channel_weight or 0.0) <= 0.0 or channel not in sources:
                    continue
                if channel not in display.mappings:
                    missing.append(channel)
                    continue
                if sources[channel].selected_planes():
                    group_sources[channel] = sources[channel]
            if group_sources:
                active_groups[group] = group_sources
        nucleus_source = None
        nucleus_channel, nucleus_weight = display.nucleus
        if nucleus_channel and float(nucleus_weight or 0.0) > 0.0 and nucleus_channel in sources:
            if nucleus_channel not in display.mappings:
                missing.append(nucleus_channel)
            elif sources[nucleus_channel].selected_planes():
                nucleus_source = sources[nucleus_channel]
        return active_groups, nucleus_source, missing

    # Rendering ----------------------------------------------------------

    def _vt_placeholders(self):
        """1x1x1 R8UI and R32F arrays bound to the single pass's unused
        sampler slots (A9 §35)."""
        if not getattr(self, "_vt_dummy", None):
            gl = self._gl
            names = []
            for internal, fmt, typ, value in (
                    (gl.GL_R8UI, gl.GL_RED_INTEGER, gl.GL_UNSIGNED_BYTE, np.zeros(1, np.uint8)),
                    (gl.GL_R32F, gl.GL_RED, gl.GL_FLOAT, np.zeros(1, np.float32))):
                texture = _as_name(gl.glGenTextures(1))
                gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, texture)
                for name in (gl.GL_TEXTURE_MIN_FILTER, gl.GL_TEXTURE_MAG_FILTER):
                    gl.glTexParameteri(gl.GL_TEXTURE_2D_ARRAY, name, gl.GL_NEAREST)
                gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
                gl.glTexImage3D(gl.GL_TEXTURE_2D_ARRAY, 0, internal, 1, 1, 1, 0, fmt, typ, value)
                names.append(texture)
            gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, 0)
            self._vt_dummy = tuple(names)
        return self._vt_dummy

    def _render_vt(self, descriptor, rows, viewport: ViewportSnapshot, *, fusion: bool) -> bool:
        """Block A9 §35.7: every row in ONE pass, then `_finalize`. False
        when this submission cannot be drawn that way (the caller then
        draws it per channel, exactly as before)."""
        store = self._arrays if self.tile_arrays_active else None
        if (store is None or self._vt_failed or "vt" not in self._programs
                or os.environ.get("BLOCK01_VT", "1") == "0"):
            return False
        from .step1_gpu_vt import VirtualCompositor
        if self._vt is None:
            self._vt = VirtualCompositor()
            self._vt.u_blocks, self._vt.f_blocks = self._vt_split
            self._vt.max_layers = int(self._max_array_layers or 2048)
            self._vt.known_resident = self._known_resident
            self._vt.max_texture = int(self._capabilities.get("max_texture_size", 16384) or 16384)
        vt = self._vt
        key = (id(descriptor), store.meta_generation, fusion,
               tuple(ch for ch, _src, _p in rows))
        t_plan = time.perf_counter()
        plan = vt.last_plan if getattr(vt, "last_key", None) == key else vt.plan(store, rows, fusion, legacy=self._cache)
        t_planned = time.perf_counter()
        if plan is None:
            vt.fallbacks += 1
            if perf_trace.enabled():
                perf_trace.mark("gpu.vt_fallback", why=str(getattr(vt, "why", "?")).replace(" ", "_")[:60])
            return False
        gl = self._gl
        if vt.synced != key:
            if not vt.sync(gl, store, plan, key):
                vt.fallbacks += 1
                return False
            if perf_trace.enabled():
                plan_ms = (t_planned - t_plan) * 1000.0
                sync_ms = (time.perf_counter() - t_planned) * 1000.0
                if plan_ms + sync_ms > 8.0:
                    perf_trace.mark("gpu.vt_slow", plan_ms=round(plan_ms, 2),
                                    sync_ms=round(sync_ms, 2), levels=len(vt.level_slots),
                                    channels=len(vt.channel_slots), rows=len(rows),
                                    written=vt.last_written, layout=vt.last_layout)
        vt.last_key, vt.last_plan = key, plan
        # the rows carry this display (mapping, weights): always fresh
        plan.rows = rows
        vt.upload_params(gl, plan)
        target = "fusion" if fusion else "accum"
        self._bind_target(target)
        gl.glDisable(gl.GL_BLEND)
        program = "vt"
        gl.glUseProgram(self._programs[program])
        self._uniform4(program, "u_view_rect", viewport.world_rect)
        gl.glUniform2f(self._uniform_location(program, "u_target_size"),
                       float(self._target_size[0]), float(self._target_size[1]))
        gl.glUniform1i(self._uniform_location(program, "u_rows"), len(rows))
        gl.glUniform1i(self._uniform_location(program, "u_fusion"), 1 if fusion else 0)
        level_a, level_b = vt.level_uniforms(plan)
        gl.glUniform4fv(self._uniform_location(program, "u_level_a"), len(level_a), level_a)
        gl.glUniform2iv(self._uniform_location(program, "u_level_b"), len(level_b), level_b)
        units = []
        by_family = {(b.fmt == "f32", b.vt_index): b.texture for b in store.blocks}
        # an unused sampler gets a 1x1 placeholder of its own type, never
        # "no texture": a slot going from empty to a new block otherwise
        # cost a ~100 ms frame (the driver re-specialising the program)
        dummy_u, dummy_f = self._vt_placeholders()
        for index in range(vt.u_blocks):
            units.append((f"u_u{index}", gl.GL_TEXTURE_2D_ARRAY,
                          by_family.get((False, index), dummy_u)))
        for index in range(vt.f_blocks):
            units.append((f"u_f{index}", gl.GL_TEXTURE_2D_ARRAY,
                          by_family.get((True, index), dummy_f)))
        units += [("u_pages", gl.GL_TEXTURE_2D_ARRAY, vt.pages_tex),
                  ("u_meta", gl.GL_TEXTURE_2D, vt.meta_tex),
                  ("u_params", gl.GL_TEXTURE_2D, vt.params_tex)]
        for unit, (name, target_kind, texture) in enumerate(units):
            gl.glActiveTexture(gl.GL_TEXTURE0 + unit)
            gl.glBindTexture(target_kind, texture)
            self._uniform1i(program, name, unit)
        self._draw()
        for unit, (_name, target_kind, _texture) in enumerate(units):
            gl.glActiveTexture(gl.GL_TEXTURE0 + unit)
            gl.glBindTexture(target_kind, 0)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glUseProgram(0)
        vt.frames += 1
        self._finalize(target, "final", "final_fusion" if fusion else "final_overlay", viewport)
        return True

    def _render_overlay(self, active: Mapping[str, ChannelSource], display: DisplaySnapshot,
                        viewport: ViewportSnapshot) -> None:
        gl = self._gl
        self._clear_target("accum")
        for channel, source in active.items():
            self._render_signal(source, display.mappings[channel], viewport)
            weight = min(1.0, float(display.weights.get(channel, 0.0) or 0.0))
            self._contribute("accum", weight, display.colors.get(channel, (1.0, 1.0, 1.0)), 0,
                             blend_equation=(gl.GL_FUNC_ADD, gl.GL_MAX))
        self._finalize("accum", "final", "final_overlay", viewport)

    def _render_fusion(self, groups: Mapping[str, Mapping[str, ChannelSource]], nucleus_source: Optional[ChannelSource],
                       display: DisplaySnapshot, viewport: ViewportSnapshot) -> None:
        gl = self._gl
        self._clear_target("fusion")
        for group, sources in groups.items():
            self._clear_target("group")
            for channel, source in sources.items():
                self._render_signal(source, display.mappings[channel], viewport)
                channel_weight = min(1.0, max(0.0, float(display.groups[group].get(channel, 0.0) or 0.0)))
                self._contribute("group", channel_weight, (1.0, 0.0, 0.0), 1,
                                 blend_equation=(gl.GL_FUNC_ADD, gl.GL_MAX))
            group_weight = min(1.0, max(0.0, float(display.group_weights.get(group, 1.0) or 0.0)))
            self._group_resolve(group_weight)
        if nucleus_source is not None:
            nucleus_channel, nucleus_weight = display.nucleus
            self._render_signal(nucleus_source, display.mappings[nucleus_channel], viewport)
            self._contribute("fusion", min(1.0, max(0.0, float(nucleus_weight))), (0.0, 0.0, 1.0), 2,
                             blend_equation=(gl.GL_MAX, gl.GL_MAX))
        self._finalize("fusion", "final", "final_fusion", viewport)

    def _render_signal(self, source: ChannelSource, mapping: Tuple[float, float, float],
                       viewport: ViewportSnapshot) -> None:
        gl = self._gl
        self._clear_target("signal")
        self._bind_target("signal")
        gl.glDisable(gl.GL_BLEND)
        current = None
        arrays = self._arrays if self.tile_arrays_active else None
        planes = source.selected_planes()
        # Planes in order (coarsest first, finest last: a finer plane
        # overwrites); block A9 S2c switches the program per plane between
        # the float one and the integer one. Block A9 §35: a RUN of
        # array-stored planes of one pixel size (one pyramid level of the
        # binding's grid, so disjoint) is drawn instanced, one call per
        # array block.
        index = 0
        while index < len(planes):
            plane = planes[index]
            if arrays is not None and arrays.is_resident(plane):
                size = _pixel_size(plane)
                end = index + 1
                while (end < len(planes) and arrays.is_resident(planes[end])
                       and _pixel_size(planes[end]) == size):
                    end += 1
                if current in ("source", "source_uint"):
                    self._uniform1i(current, "u_quad", 0)
                current = self._draw_array_run(arrays, planes[index:end], mapping, viewport)
                index = end
                continue
            index += 1
            program = "source_uint" if self._cache.is_integer(plane) else "source"
            if program != current:
                if current in ("source", "source_uint"):
                    self._uniform1i(current, "u_quad", 0)
                gl.glUseProgram(self._programs[program])
                self._uniform4(program, "u_view_rect", viewport.world_rect)
                self._uniform3(program, "u_mapping", mapping)
                gl.glUniform2f(self._uniform_location(program, "u_target_size"),
                               float(self._target_size[0]), float(self._target_size[1]))
                self._uniform1i(program, "u_quad", 1)
                current = program
            if program == "source_uint":
                valid = _valid_world_rect(plane)
                if valid is None:
                    continue                      # no valid pixel to draw
                self._uniform4(program, "u_valid_rect", valid)
            gl.glActiveTexture(gl.GL_TEXTURE0)
            gl.glBindTexture(gl.GL_TEXTURE_2D, self._cache.texture_for(plane))
            self._uniform1i(program, "u_raw", 0)
            self._uniform4(program, "u_plane_rect", plane.world_rect)
            self._uniform4(program, "u_quad_ndc",
                           _plane_quad_ndc(plane.world_rect, viewport.world_rect, self._target_size))
            self._draw(vertices=6)
        if current in ("source", "source_uint"):
            # programs keep uniforms: leave the default fullscreen geometry
            self._uniform1i(current, "u_quad", 0)
        gl.glUseProgram(0)

    def _draw_array_run(self, arrays, run, mapping, viewport) -> Optional[str]:
        """One instanced call per array block for `run` (disjoint planes of
        one level). Returns the program left in use."""
        gl = self._gl
        groups: Dict[int, Tuple[_ArrayBlock, list]] = {}
        for plane in run:
            found = arrays.instance(plane)
            if found is None:
                continue                          # no valid pixel to draw
            block, record = found
            groups.setdefault(id(block), (block, []))[1].append(record)
        program = None
        for block, records in groups.values():
            name = "array_float" if block.fmt == "f32" else "array_uint"
            if name != program:
                gl.glUseProgram(self._programs[name])
                self._uniform4(name, "u_view_rect", viewport.world_rect)
                self._uniform3(name, "u_mapping", mapping)
                gl.glUniform2f(self._uniform_location(name, "u_target_size"),
                               float(self._target_size[0]), float(self._target_size[1]))
                self._uniform1i(name, "u_arr", 0)
                program = name
            data = np.empty(len(records), INSTANCE_DTYPE)
            data["rect"] = [r[0] for r in records]
            data["valid"] = [r[1] for r in records]
            data["size"] = [r[2] for r in records]
            data["layer"] = [r[3] for r in records]
            gl.glActiveTexture(gl.GL_TEXTURE0)
            gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, block.texture)
            gl.glBindVertexArray(self._array_vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._array_vbo)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, data.nbytes, data, gl.GL_STREAM_DRAW)
            gl.glDrawArraysInstanced(gl.GL_TRIANGLES, 0, 6, len(records))
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
            gl.glBindVertexArray(0)
            gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, 0)
            self._submission["pass_count"] = int(self._submission.get("pass_count", 0)) + 1
        return program

    def _setup_array_vao(self) -> None:
        """Block A9 §35: the instance attributes of the array programs --
        divisor 1, the layer an INTEGER attribute (glVertexAttribIPointer)."""
        import ctypes
        gl = self._gl
        self._array_vao = _as_name(gl.glGenVertexArrays(1))
        self._array_vbo = _as_name(gl.glGenBuffers(1))
        gl.glBindVertexArray(self._array_vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._array_vbo)
        stride = INSTANCE_DTYPE.itemsize
        for location, (field, count) in enumerate((("rect", 4), ("valid", 4), ("size", 2))):
            gl.glEnableVertexAttribArray(location)
            gl.glVertexAttribPointer(location, count, gl.GL_FLOAT, gl.GL_FALSE, stride,
                                     ctypes.c_void_p(INSTANCE_DTYPE.fields[field][1]))
            gl.glVertexAttribDivisor(location, 1)
        gl.glEnableVertexAttribArray(3)
        gl.glVertexAttribIPointer(3, 1, gl.GL_INT, stride,
                                  ctypes.c_void_p(INSTANCE_DTYPE.fields["layer"][1]))
        gl.glVertexAttribDivisor(3, 1)
        gl.glBindVertexArray(0)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)

    def _contribute(self, target: str, weight: float, color: Tuple[float, float, float], component: int,
                    blend_equation: Tuple[int, int]) -> None:
        gl = self._gl
        self._bind_target(target)
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendEquationSeparate(*blend_equation)
        gl.glBlendFuncSeparate(gl.GL_ONE, gl.GL_ONE, gl.GL_ONE, gl.GL_ONE)
        gl.glUseProgram(self._programs["contribution"])
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._targets["signal"][1])
        self._uniform1i("contribution", "u_input", 0)
        self._uniform3("contribution", "u_color", color)
        self._uniform1f("contribution", "u_weight", weight)
        self._uniform1i("contribution", "u_component", component)
        self._draw()
        gl.glUseProgram(0)
        gl.glDisable(gl.GL_BLEND)

    def _group_resolve(self, group_weight: float) -> None:
        gl = self._gl
        self._bind_target("fusion")
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendEquation(gl.GL_MAX)
        gl.glUseProgram(self._programs["group_resolve"])
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._targets["group"][1])
        self._uniform1i("group_resolve", "u_input", 0)
        self._uniform1f("group_resolve", "u_weight", group_weight)
        self._draw()
        gl.glUseProgram(0)
        gl.glDisable(gl.GL_BLEND)

    def _finalize(self, source: str, target: str, program: str,
                  viewport: Optional[ViewportSnapshot] = None) -> None:
        gl = self._gl
        self._bind_target(target)
        gl.glDisable(gl.GL_BLEND)
        # THE ANALYSIS REGION IS A DISPLAY BOUNDARY, applied once, here, to
        # whatever the passes above produced. Everything outside it is
        # CLEARED rather than left over: the clear happens with the scissor
        # off, the draw with it on, so a pixel outside the region ends this
        # submission at alpha 0 no matter what was in the target before.
        scissor = None
        polygon = None
        polygon_error = ""
        if viewport is not None:
            scissor = roi_scissor_box(viewport.world_rect,
                                      viewport.roi_world_rect,
                                      self._target_size)
            polygon, polygon_error = sanitize_roi_polygon(
                viewport.roi_polygon_world, viewport.roi_world_rect)
            if polygon_error:
                # NOT SILENTLY "no polygon": a polygon was offered and
                # refused, so the reason travels with the submission and the
                # rectangle still clips. The picture is bounded, and the
                # caller can see that the shape it asked for was not used.
                self._roi_polygon_error = polygon_error
        self._submission["roi_polygon_error"] = polygon_error
        if not polygon_error:
            self._roi_polygon_error = ""
        self._submission["roi_polygon_points"] = 0 if polygon is None else len(polygon)
        self._last_clip = None
        if scissor is not None or polygon is not None:
            gl.glDisable(gl.GL_SCISSOR_TEST)
            gl.glDisable(gl.GL_STENCIL_TEST)
            gl.glStencilMask(0xFF)
            gl.glClearColor(0.0, 0.0, 0.0, 0.0)
            gl.glClearStencil(0)
            gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_STENCIL_BUFFER_BIT)
            self._last_clip = (scissor, polygon is not None)
            if scissor is not None and (scissor[2] <= 0 or scissor[3] <= 0):
                # The region is not on screen at all. The cleared target IS
                # the answer; drawing a zero-sized scissor is undefined.
                # THE EARLY RETURN RESTORES STATE TOO -- it used to leave the
                # stencil write mask at 0, which the state gate caught.
                self._submission["roi_scissor"] = tuple(scissor)
                self._restore_clip_state()
                _check_gl(gl, f"G1 {program} roi clip")
                return
        # FROM THE FIRST STATE CHANGE ONWARDS the restore is in a `finally`:
        # a shader error, a lost context or a failed draw -- in the mask as
        # much as in the composition -- must not hand the next frame, or
        # `paintGL`'s blit, a scissor, a stencil test or a closed colour
        # mask that nobody asked for. (G3.2d review: the first version
        # started the `try` after the mask had already been written.)
        try:
            if scissor is not None or polygon is not None:
                if polygon is not None:
                    self._write_polygon_stencil(polygon, viewport)
                else:
                    gl.glStencilMask(0x00)
                if scissor is not None:
                    gl.glEnable(gl.GL_SCISSOR_TEST)
                    gl.glScissor(*scissor)
            gl.glUseProgram(self._programs[program])
            gl.glActiveTexture(gl.GL_TEXTURE0)
            gl.glBindTexture(gl.GL_TEXTURE_2D, self._targets[source][1])
            self._uniform1i(program, "u_input", 0)
            self._draw()
            gl.glUseProgram(0)
        finally:
            if scissor is not None or polygon is not None:
                self._restore_clip_state()
                if scissor is not None:
                    self._submission["roi_scissor"] = tuple(scissor)
        _check_gl(gl, f"G1 {program}")

    def _draw(self, vertices: int = 3) -> None:
        self._gl.glBindVertexArray(self._vao)
        self._gl.glDrawArrays(self._gl.GL_TRIANGLES, 0, int(vertices))
        self._gl.glBindVertexArray(0)
        self._submission["pass_count"] = int(self._submission.get("pass_count", 0)) + 1

    # GL resource management --------------------------------------------

    def _require_ready(self) -> None:
        if self._disposed:
            raise Step1GpuLayerError("G1 layer is disposed")
        if not self._initialized:
            raise self._init_error or Step1GpuLayerError("G1 Qt OpenGL widget is not initialized")
        if QtGui.QOpenGLContext.currentContext() is not self.context():
            raise Step1GpuLayerError("G1 GL operation requires its exact Qt context current")

    def _ensure_mask_resources(self) -> None:
        """The polygon program and its vertex buffer, created once.

        Separate from the five composition programs because it is the only
        one with real vertex input: `step1_gpu.vert` builds its fullscreen
        triangle from `gl_VertexID` and cannot carry a polygon.
        """
        if self._mask_program:
            return
        gl = self._gl
        vertex = (_SHADER_DIR / "step1_gpu_mask.vert").read_text(encoding="utf-8")
        fragment = (_SHADER_DIR / "step1_gpu_mask.frag").read_text(encoding="utf-8")
        self._mask_program = self._link_program(vertex, fragment)
        self._uniforms["mask"] = {}
        self._programs["mask"] = self._mask_program
        self._mask_vao = _as_name(gl.glGenVertexArrays(1))
        self._mask_vbo = _as_name(gl.glGenBuffers(1))
        gl.glBindVertexArray(self._mask_vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._mask_vbo)
        gl.glEnableVertexAttribArray(0)
        gl.glVertexAttribPointer(0, 2, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
        gl.glBindVertexArray(0)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
        _check_gl(gl, "G1 polygon mask setup")

    def _restore_clip_state(self) -> None:
        """Hand the GL state back exactly as it was found.

        Every clipping path goes through here -- including the one that
        returns early because the region is off screen, which is where a
        stencil write mask of 0 was once left behind for the next frame.
        """
        gl = self._gl
        gl.glDisable(gl.GL_SCISSOR_TEST)
        gl.glDisable(gl.GL_STENCIL_TEST)
        gl.glStencilMask(0xFF)
        gl.glStencilFunc(gl.GL_ALWAYS, 0, 0xFF)
        gl.glStencilOp(gl.GL_KEEP, gl.GL_KEEP, gl.GL_KEEP)
        gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)

    def _write_polygon_stencil(self, polygon, viewport) -> int:
        """Stamp the polygon into the stencil buffer. Returns its point count.

        EVEN-ODD, NOT A SOLID FAN. A triangle fan over a concave polygon
        covers area outside the shape as well; with `GL_INVERT` on the low
        bit, every pixel ends with bit 0 set exactly where an odd number of
        fan triangles covered it -- which is the inside of the polygon,
        concave or not, clockwise or anticlockwise.

        The colour mask is off while this runs, so the fan contributes no
        pixels of its own.
        """
        gl = self._gl
        self._ensure_mask_resources()
        data = np.asarray(polygon, dtype=np.float32).reshape(-1)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._mask_vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, data.nbytes, data, gl.GL_DYNAMIC_DRAW)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)

        gl.glEnable(gl.GL_STENCIL_TEST)
        gl.glStencilMask(0x01)
        gl.glStencilFunc(gl.GL_ALWAYS, 0, 0x01)
        gl.glStencilOp(gl.GL_KEEP, gl.GL_KEEP, gl.GL_INVERT)
        gl.glColorMask(gl.GL_FALSE, gl.GL_FALSE, gl.GL_FALSE, gl.GL_FALSE)
        gl.glDisable(gl.GL_BLEND)
        gl.glUseProgram(self._mask_program)
        self._uniform4("mask", "u_view_rect", viewport.world_rect)
        gl.glBindVertexArray(self._mask_vao)
        gl.glDrawArrays(gl.GL_TRIANGLE_FAN, 0, len(polygon))
        gl.glBindVertexArray(0)
        gl.glUseProgram(0)
        gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
        # ...and from here on, draw only where the polygon put bit 0.
        gl.glStencilMask(0x00)
        gl.glStencilFunc(gl.GL_EQUAL, 0x01, 0x01)
        gl.glStencilOp(gl.GL_KEEP, gl.GL_KEEP, gl.GL_KEEP)
        self._submission["pass_count"] = int(self._submission.get("pass_count", 0)) + 1
        return len(polygon)

    def _compile_programs(self) -> None:
        vertex = (_SHADER_DIR / "step1_gpu.vert").read_text(encoding="utf-8")
        fragment = (_SHADER_DIR / "step1_gpu.frag").read_text(encoding="utf-8")
        for name, define in {
            "source": "PASS_SOURCE",
            "source_uint": "PASS_SOURCE_UINT",
            "contribution": "PASS_CONTRIBUTION",
            "group_resolve": "PASS_GROUP_RESOLVE",
            "final_overlay": "PASS_FINAL_OVERLAY",
            "final_fusion": "PASS_FINAL_FUSION",
        }.items():
            source = fragment.replace("\n", f"\n#define {define}\n", 1)
            self._programs[name] = self._link_program(vertex, source)
            self._uniforms[name] = {}
        try:
            from .step1_gpu_vt import sampler_split, shader_source
            self._vt_split = sampler_split(int(self._capabilities.get("max_texture_image_units", 16) or 16))
            template = (_SHADER_DIR / "step1_gpu_vt.frag").read_text(encoding="utf-8")
            self._programs["vt"] = self._link_program(vertex, shader_source(template, *self._vt_split))
            self._uniforms["vt"] = {}
        except Step1GpuLayerError as exc:            # the per-channel path stays
            self._vt_failed = f"vt program: {exc}"[:200]
        array_vertex = (_SHADER_DIR / "step1_gpu_array.vert").read_text(encoding="utf-8")
        for name, define in {"array_uint": "PASS_ARRAY_UINT",
                             "array_float": "PASS_ARRAY_FLOAT"}.items():
            source = fragment.replace("\n", f"\n#define {define}\n", 1)
            self._programs[name] = self._link_program(array_vertex, source)
            self._uniforms[name] = {}
        if self._labels_enabled:
            labels = (_SHADER_DIR / "step1_gpu_labels.frag").read_text(encoding="utf-8")
            for name, define in {"label_ids": "PASS_LABEL_IDS",
                                 "label_draw": "PASS_LABEL_DRAW"}.items():
                source = labels.replace("\n", f"\n#define {define}\n", 1)
                self._programs[name] = self._link_program(vertex, source)
                self._uniforms[name] = {}

    def _link_program(self, vertex_source: str, fragment_source: str) -> int:
        gl = self._gl
        def compile_one(kind, source):
            shader = gl.glCreateShader(kind)
            gl.glShaderSource(shader, source)
            gl.glCompileShader(shader)
            if not gl.glGetShaderiv(shader, gl.GL_COMPILE_STATUS):
                message = gl.glGetShaderInfoLog(shader).decode("utf-8", "replace")
                gl.glDeleteShader(shader)
                raise Step1GpuLayerError(f"G1 shader compile failure: {message}")
            return shader
        vertex = compile_one(gl.GL_VERTEX_SHADER, vertex_source)
        fragment = compile_one(gl.GL_FRAGMENT_SHADER, fragment_source)
        program = gl.glCreateProgram()
        gl.glAttachShader(program, vertex)
        gl.glAttachShader(program, fragment)
        gl.glLinkProgram(program)
        gl.glDeleteShader(vertex)
        gl.glDeleteShader(fragment)
        if not gl.glGetProgramiv(program, gl.GL_LINK_STATUS):
            message = gl.glGetProgramInfoLog(program).decode("utf-8", "replace")
            gl.glDeleteProgram(program)
            raise Step1GpuLayerError(f"G1 shader link failure: {message}")
        return int(program)

    def _ensure_targets(self, size: Tuple[int, int]) -> None:
        if size == self._target_size:
            return
        self._destroy_targets()
        gl = self._gl
        for name, internal, fmt, typ in (
            ("signal", gl.GL_RGBA32F, gl.GL_RGBA, gl.GL_FLOAT),
            ("accum", gl.GL_RGBA32F, gl.GL_RGBA, gl.GL_FLOAT),
            ("group", gl.GL_RGBA32F, gl.GL_RGBA, gl.GL_FLOAT),
            ("fusion", gl.GL_RGBA32F, gl.GL_RGBA, gl.GL_FLOAT),
            ("final", gl.GL_RGBA8, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE),
        ):
            texture = _as_name(gl.glGenTextures(1))
            gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, internal, size[0], size[1], 0, fmt, typ, None)
            fbo = _as_name(gl.glGenFramebuffers(1))
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
            gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0, gl.GL_TEXTURE_2D, texture, 0)
            if gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER) != gl.GL_FRAMEBUFFER_COMPLETE:
                raise Step1GpuLayerError(f"G1 {name} framebuffer incomplete")
            if name == "final":
                # THE POLYGON MASK LIVES HERE, and only here: the transient
                # float targets never need it, and a stencil on all five
                # would be four buffers nobody reads. Recreated with the
                # targets, so it always matches the physical output.
                stencil = _as_name(gl.glGenRenderbuffers(1))
                gl.glBindRenderbuffer(gl.GL_RENDERBUFFER, stencil)
                gl.glRenderbufferStorage(gl.GL_RENDERBUFFER, gl.GL_STENCIL_INDEX8,
                                         size[0], size[1])
                gl.glFramebufferRenderbuffer(gl.GL_FRAMEBUFFER,
                                             gl.GL_STENCIL_ATTACHMENT,
                                             gl.GL_RENDERBUFFER, stencil)
                gl.glBindRenderbuffer(gl.GL_RENDERBUFFER, 0)
                if gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER) != gl.GL_FRAMEBUFFER_COMPLETE:
                    raise Step1GpuLayerError(
                        "G1 final framebuffer incomplete with its stencil")
                self._stencil_rbo = stencil
            self._targets[name] = (fbo, texture)
        if self._labels_enabled:
            self._ensure_label_targets(size)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        self._target_size = size
        self._shown_ready = False
        _check_gl(gl, "G1 transient target allocation")

    def _ensure_label_targets(self, size: Tuple[int, int]) -> None:
        """`ids` (the screen id image) and `shown` (RGBA8, the picture with
        its masks). `shown` shares `final`'s stencil renderbuffer, so a mask
        is clipped by the very polygon the picture was."""
        gl = self._gl
        for name, internal, fmt, typ in (
            ("ids", gl.GL_R32UI, gl.GL_RED_INTEGER, gl.GL_UNSIGNED_INT),
            ("shown", gl.GL_RGBA8, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE),
        ):
            texture = _as_name(gl.glGenTextures(1))
            gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, internal, size[0], size[1], 0, fmt, typ, None)
            fbo = _as_name(gl.glGenFramebuffers(1))
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
            gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                      gl.GL_TEXTURE_2D, texture, 0)
            if name == "shown":
                gl.glFramebufferRenderbuffer(gl.GL_FRAMEBUFFER, gl.GL_STENCIL_ATTACHMENT,
                                             gl.GL_RENDERBUFFER, self._stencil_rbo)
            if gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER) != gl.GL_FRAMEBUFFER_COMPLETE:
                raise Step1GpuLayerError(f"G1 {name} framebuffer incomplete")
            self._targets[name] = (fbo, texture)

    def _compose_labels(self) -> None:
        """`final` -> `shown`, then every visible mask layer over it."""
        layers = [layer for layer in self._label_snapshot.layers
                  if layer.visible and layer.planes]
        if not layers or self._last_viewport is None:
            self._shown_ready = False
            return
        gl = self._gl
        viewport = self._last_viewport
        self._label_cache.prepare(gl, [plane for layer in layers for plane in layer.planes])
        width, height = self._target_size
        gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, self._targets["final"][0])
        gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, self._targets["shown"][0])
        gl.glBlitFramebuffer(0, 0, width, height, 0, 0, width, height,
                             gl.GL_COLOR_BUFFER_BIT, gl.GL_NEAREST)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        self._shown_ready = True
        self._label_counts["label_compositions"] += 1
        scissor, polygon_used = self._last_clip if self._last_clip is not None else (None, False)
        if scissor is not None and (scissor[2] <= 0 or scissor[3] <= 0):
            return                      # the region is off screen: the picture is the answer
        radius_ratio = float(viewport.device_pixel_ratio)
        for layer in layers:
            self._draw_label_ids(layer, viewport)
            self._bind_target("shown")
            try:
                if polygon_used:
                    gl.glEnable(gl.GL_STENCIL_TEST)
                    gl.glStencilMask(0x00)
                    gl.glStencilFunc(gl.GL_EQUAL, 0x01, 0x01)
                    gl.glStencilOp(gl.GL_KEEP, gl.GL_KEEP, gl.GL_KEEP)
                if scissor is not None:
                    gl.glEnable(gl.GL_SCISSOR_TEST)
                    gl.glScissor(*scissor)
                gl.glEnable(gl.GL_BLEND)
                gl.glBlendEquation(gl.GL_FUNC_ADD)
                gl.glBlendFuncSeparate(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA,
                                       gl.GL_ONE, gl.GL_ONE_MINUS_SRC_ALPHA)
                gl.glUseProgram(self._programs["label_draw"])
                gl.glActiveTexture(gl.GL_TEXTURE0)
                gl.glBindTexture(gl.GL_TEXTURE_2D, self._targets["ids"][1])
                self._uniform1i("label_draw", "u_ids", 0)
                self._uniform1i("label_draw", "u_mode", 1 if layer.mode == LABEL_FILL else 0)
                self._uniform1i("label_draw", "u_radius", label_radius(layer.width, radius_ratio))
                self._uniform3("label_draw", "u_color", layer.color)
                self._uniform1f("label_draw", "u_alpha", layer.alpha)
                self._draw()
                self._label_counts["label_passes"] += 1
                gl.glUseProgram(0)
            finally:
                gl.glDisable(gl.GL_BLEND)
                self._restore_clip_state()
        _check_gl(gl, "G1 label composition")

    def _draw_label_ids(self, layer: LabelLayer, viewport: ViewportSnapshot) -> None:
        gl = self._gl
        self._bind_target("ids")
        gl.glDisable(gl.GL_BLEND)
        gl.glClearBufferuiv(gl.GL_COLOR, 0, np.zeros(4, np.uint32))
        gl.glUseProgram(self._programs["label_ids"])
        self._uniform4("label_ids", "u_view_rect", viewport.world_rect)
        gl.glUniform2f(self._uniform_location("label_ids", "u_target_size"),
                       float(self._target_size[0]), float(self._target_size[1]))
        for plane in layer.planes:
            gl.glActiveTexture(gl.GL_TEXTURE0)
            gl.glBindTexture(gl.GL_TEXTURE_2D, self._label_cache.texture_for(plane))
            self._uniform1i("label_ids", "u_labels", 0)
            self._uniform4("label_ids", "u_plane_rect", plane.world_rect)
            self._draw()
            self._label_counts["label_passes"] += 1
        gl.glUseProgram(0)

    def _bind_target(self, name: str) -> None:
        fbo, _texture = self._targets[name]
        width, height = self._target_size
        self._gl.glBindFramebuffer(self._gl.GL_FRAMEBUFFER, fbo)
        self._gl.glViewport(0, 0, width, height)

    def _clear_target(self, name: str) -> None:
        self._bind_target(name)
        self._gl.glDisable(self._gl.GL_BLEND)
        self._gl.glClearColor(0.0, 0.0, 0.0, 0.0)
        self._gl.glClear(self._gl.GL_COLOR_BUFFER_BIT)

    def _uniform_location(self, program: str, name: str) -> int:
        locations = self._uniforms[program]
        if name not in locations:
            location = int(self._gl.glGetUniformLocation(self._programs[program], name))
            if location < 0:
                raise Step1GpuLayerError(f"G1 shader missing required uniform {program}.{name}")
            locations[name] = location
        return locations[name]

    def _uniform1i(self, program: str, name: str, value: int) -> None:
        self._gl.glUniform1i(self._uniform_location(program, name), int(value))

    def _uniform1f(self, program: str, name: str, value: float) -> None:
        self._gl.glUniform1f(self._uniform_location(program, name), float(value))

    def _uniform3(self, program: str, name: str, values) -> None:
        self._gl.glUniform3f(self._uniform_location(program, name), *[float(item) for item in values])

    def _uniform4(self, program: str, name: str, values) -> None:
        self._gl.glUniform4f(self._uniform_location(program, name), *[float(item) for item in values])

    def _destroy_targets(self) -> None:
        if self._gl is None:
            self._targets.clear()
            self._target_size = None
            self._stencil_rbo = 0
            return
        for fbo, texture in self._targets.values():
            self._gl.glDeleteFramebuffers(1, [fbo])
            self._gl.glDeleteTextures([texture])
        if self._stencil_rbo:
            self._gl.glDeleteRenderbuffers(1, [self._stencil_rbo])
            self._stencil_rbo = 0
        self._targets.clear()
        self._target_size = None

    def _destroy_gl_names(self) -> None:
        if self._gl is None:
            return
        self._cache.clear(self._gl)
        if self._arrays is not None:
            self._arrays.clear(self._gl)
            self._arrays = None
        if self._vt is not None:
            self._vt.clear(self._gl)
            self._vt = None
        if getattr(self, "_vt_dummy", None):
            self._gl.glDeleteTextures(list(self._vt_dummy))
            self._vt_dummy = None
        if self._array_vao:
            self._gl.glDeleteVertexArrays(1, [self._array_vao])
            self._array_vao = 0
        if self._array_vbo:
            self._gl.glDeleteBuffers(1, [self._array_vbo])
            self._array_vbo = 0
        if self._label_cache is not None:
            self._label_cache.clear(self._gl)
        self._shown_ready = False
        self._destroy_targets()
        if self._vao:
            self._gl.glDeleteVertexArrays(1, [self._vao])
            self._vao = 0
        if self._mask_vao:
            self._gl.glDeleteVertexArrays(1, [self._mask_vao])
            self._mask_vao = 0
        if self._mask_vbo:
            self._gl.glDeleteBuffers(1, [self._mask_vbo])
            self._mask_vbo = 0
        self._mask_program = 0          # deleted below with every program
        for program in self._programs.values():
            self._gl.glDeleteProgram(program)
        self._programs.clear()
        self._uniforms.clear()

    # Attachment ---------------------------------------------------------

    def _attached_range_changed(self, _view_box, ranges) -> None:
        self._attached_range = (float(ranges[0][0]), float(ranges[0][1]),
                                float(ranges[1][0]), float(ranges[1][1]))
        # Block A9 §32 (Odon app.rs: input moves the camera, the next frame
        # draws it): the picture follows NOW, from what is resident --
        # planning and uploads come later, from the binding.
        if self._scene is not None and self._attached_range != self._composed_rect:
            self._camera_dirty = True
            self.update()

    def _live_viewport(self) -> Optional[ViewportSnapshot]:
        """The last submitted viewport moved to the attached camera, at the
        widget's current size."""
        if self._scene is None or self._attached_range is None:
            return None
        viewport = self._scene[2]
        ratio = float(self.devicePixelRatioF()) or 1.0
        return dataclasses.replace(viewport, world_rect=self._attached_range,
                                   logical_size=(max(1, int(self.width())), max(1, int(self.height()))),
                                   device_pixel_ratio=ratio, repaint_request=None)

    def _recompose_for_camera(self) -> None:
        """paintGL's camera-only composition (context current)."""
        self._camera_dirty = False
        viewport = self._live_viewport()
        if viewport is None or self._disposed:
            return
        descriptor, display, _old = self._scene
        try:
            with perf_trace.span("gpu.camera_frame"):
                self._compose(descriptor, display, viewport, upload=False)
        except Step1GpuLayerError as exc:
            perf_trace.mark("gpu.camera_frame_failed", error=str(exc)[:120].replace(" ", "_"))

    def _sync_attached_geometry(self, *_args) -> None:
        """Cover exactly the ViewBox (block A1, C1).

        The world rectangle every submit draws is the ViewBox's `viewRange()`,
        and pyqtgraph insets the ViewBox inside the graphics viewport (the
        layout's default 9 px margins). Covering the whole viewport stretched
        that rectangle over the margins -- 1.4 % / 2.3 % larger than Step0's
        CPU picture of the same camera. Covering the ViewBox draws it where
        the CPU picture is, margins included.
        """
        if self._attached_viewport is None:
            return
        rect = self._attached_viewport.rect()
        view = self._attached_view
        view_box = getattr(view, "view_box", None) if view is not None else None
        graphics = getattr(view, "graphics", None) if view is not None else None
        if view_box is not None and graphics is not None:
            # `rect()`, not `boundingRect()`: pyqtgraph pads the latter by 0.5 px.
            scene_rect = view_box.mapRectToScene(view_box.rect())
            top_left = graphics.mapFromScene(scene_rect.topLeft())
            width, height = int(round(scene_rect.width())), int(round(scene_rect.height()))
            if width > 0 and height > 0:
                rect = QtCore.QRect(top_left.x(), top_left.y(), width, height)
        self.setGeometry(rect)
        self.raise_()

    def _disconnect_attachment(self) -> None:
        if self._attached_view is not None:
            try:
                self._attached_view.view_box.sigRangeChanged.disconnect(self._attached_range_changed)
            except (TypeError, RuntimeError):
                pass
            try:
                self._attached_view.view_box.sigResized.disconnect(self._sync_attached_geometry)
            except (TypeError, RuntimeError):
                pass
        if self._attached_viewport is not None:
            self._attached_viewport.removeEventFilter(self)
        self._attached_view = None
        self._attached_viewport = None
