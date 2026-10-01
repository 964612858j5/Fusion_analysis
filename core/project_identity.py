"""Project identity and coordinates (block A3; plan v2.3 §6.1-§6.2, §6.8;
v2.4 §5.1a P2; frozen text: `docs/v16_contracts_draft.md` §2-§3).

ONE interpretation of who a slide / region is and how its coordinate frames
relate. New code and TMA code use these functions instead of writing
``x / 2**level``, ``x - bbox_x0`` or ``x * mpp`` themselves; existing code is
not retrofitted (v2.2 ruling 9).

Identity
  * ``slide_id`` -- ``slide_`` + sha256 of the slide signature v1 (file size,
    OME-XML, every level's shape and dtype, the first and last 4 MiB of the
    file), first 16 hex. An IDENTITY, not an integrity check: an edit in the
    middle of the file that keeps all of those goes unnoticed. The method
    name travels with the id, so a later method makes new ids without
    invalidating old ones.
  * ``workspace_id`` -- today's ``roi_id``: the directory one Step0 Save made.
    Not a region.
  * ``region_id`` -- ``reg_`` + sha256 of (slide_id, type, bbox_fullres, the
    normalised polygon), first 16 hex. Fixed by geometry: the same place on
    the same slide is one region whichever workspace or display name.
  * ``segmentation_run_id`` = today's ``run_id``; ``cell_id`` = the
    LabelStore label; the stable cell key is
    ``(segmentation_run_id, region_id, cell_id)``.

Coordinates (arrays (y, x), points (x, y))
  * ``global_pixel``: level-0 index; an INTEGER INDEX IS THE PIXEL CENTRE.
  * ``pyramid_level``: pixel i of level L has its centre at global
    ``(i + 0.5) * s_L - 0.5`` with ``s_L = (H0 / H_L, W0 / W_L)`` -- exact, per
    axis, never a nominal factor.
  * ``physical_um`` = global * (dy_um, dx_um), only from OME PhysicalSize;
    unavailable (None) otherwise -- never an assumed mpp.
  * ``region_local`` = global - (bbox_y0, bbox_x0).
  * ``viewer_world`` = global + 0.5 (pixel j covers [j, j+1)); picking is
    ``floor(world)``. An adapter rule; the viewer is not changed.
  * bbox half-open ``[x0, x1) x [y0, y1)``; the legacy ``bbox_fullres`` is
    ``[y0, y1, x0, x1]`` with the same meaning.

Project schema
  ``project_schema_version`` (an integer, today 1) in ``project_manifest.json``.
  Absent = a legacy project, read as today. Anything else unknown raises
  `ProjectSchemaError`; nothing ever guesses.
"""

import hashlib
import json
import os
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

PROJECT_SCHEMA_VERSION = 1
LEGACY = 0                      # `read_project_schema` for a manifest without the key
SLIDE_ID_METHOD = "slide_sig_v1"
_SIG_EDGE_BYTES = 4 << 20
TRANSFORMS_SCHEMA_VERSION = 1

# §6.5: the types in use today, then the reserved ones. Nothing else.
REGION_TYPES_NOW = ("full_wsi", "roi")
REGION_TYPES_RESERVED = ("TMA_core", "tumor_region", "blur", "fold", "niche",
                         "manual_annotation")
REGION_TYPES = REGION_TYPES_NOW + REGION_TYPES_RESERVED

# How many slide signatures were computed in this process (tests check the
# fingerprint cache with it).
signature_computations = 0


class ProjectSchemaError(ValueError):
    """A project whose schema this program does not know."""


# ── project schema ──────────────────────────────────────────────────────

def check_project_schema(value, where="project_manifest.json") -> int:
    """`LEGACY` for None (no key), 1 for 1; anything else raises."""
    if value is None:
        return LEGACY
    if isinstance(value, bool) or not isinstance(value, int) or value != PROJECT_SCHEMA_VERSION:
        raise ProjectSchemaError(
            f"{where}: project_schema_version {value!r} is not supported "
            f"(this program reads version {PROJECT_SCHEMA_VERSION} and legacy "
            f"projects without the field)")
    return value


def manifest_path(project_dir) -> str:
    return os.path.join(project_dir, "project_manifest.json")


def read_project_schema(project_dir) -> int:
    """The project's schema version: `LEGACY` when the manifest has no
    ``project_schema_version`` (or there is no manifest), 1 when current;
    `ProjectSchemaError` for an unknown value or a manifest that is not JSON."""
    path = manifest_path(project_dir)
    if not os.path.exists(path):
        return LEGACY
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise ProjectSchemaError(f"{path}: unreadable project manifest ({exc})") from exc
    if not isinstance(data, dict):
        raise ProjectSchemaError(f"{path}: the project manifest is not an object")
    return check_project_schema(data.get("project_schema_version"), path)


def project_dir_of_workspace(workspace_dir) -> str:
    """``<project>/rois/<workspace_id>`` -> ``<project>``."""
    return os.path.dirname(os.path.dirname(os.path.abspath(workspace_dir)))


# ── slide identity and the raw-source description (P2) ──────────────────

def fingerprint(path) -> str:
    st = os.stat(path)
    return f"{st.st_size}:{st.st_mtime_ns}"


def slide_signature(path, level_shapes: Sequence[Sequence[int]], dtype) -> str:
    """sha256 hex of signature v1 (module docstring)."""
    global signature_computations
    import tifffile
    signature_computations += 1
    size = os.path.getsize(path)
    with tifffile.TiffFile(path) as tf:
        xml = tf.ome_metadata or ""
    h = hashlib.sha256()
    h.update(SLIDE_ID_METHOD.encode())
    h.update(str(int(size)).encode())
    h.update(b"\0xml\0" + xml.encode("utf-8"))
    h.update(json.dumps([[int(v) for v in s] for s in level_shapes]).encode())
    h.update(str(np.dtype(dtype)).encode())
    with open(path, "rb") as f:
        h.update(b"\0head\0" + f.read(_SIG_EDGE_BYTES))
        f.seek(max(0, size - _SIG_EDGE_BYTES))
        h.update(b"\0tail\0" + f.read(_SIG_EDGE_BYTES))
    return h.hexdigest()


def source_kind(source) -> str:
    """The raw-source kind. Today only ``"ome_tiff"``; an unknown source is
    an error, never a default."""
    from ..sources.ome_tiff import OmeTiffSource
    if isinstance(source, OmeTiffSource):
        return "ome_tiff"
    raise ValueError(f"no raw-source kind for {type(source).__name__}")


def describe_raw_source(source, path) -> Dict:
    """P2: the persisted description of a raw source (without its id)."""
    shapes = [list(source.level_shape(lv)) for lv in range(source.level_count())]
    phys = source.physical_size()
    return {
        "kind": source_kind(source),
        "path": os.path.abspath(path),
        "fingerprint": fingerprint(path),
        "level_count": int(source.level_count()),
        "level_shapes_yx": shapes,
        "coarsest_level_shape_yx": list(shapes[-1]),
        "dtype": str(np.dtype(source.dtype(0))),
        "channel_count": len(source.channel_names()),
        "physical_size_yx_um": [float(phys[0]), float(phys[1])] if phys else None,
    }


def describe_slide(path, cached: Optional[Dict[str, Dict]] = None) -> Tuple[str, Dict]:
    """``(slide_id, description)`` of the OME-TIFF at `path`.

    `cached` is the manifest's ``sources`` mapping: an entry for the same
    path with the same ``size:mtime_ns`` fingerprint is reused without
    hashing again."""
    path = os.path.abspath(path)
    fp = fingerprint(path)
    for sid, entry in (cached or {}).items():
        if (isinstance(entry, dict) and entry.get("path") == path
                and entry.get("fingerprint") == fp
                and entry.get("slide_id_method") == SLIDE_ID_METHOD):
            return sid, dict(entry)
    from ..sources.ome_tiff import OmeTiffSource
    with OmeTiffSource(path) as src:
        desc = describe_raw_source(src, path)
    sid = "slide_" + slide_signature(path, desc["level_shapes_yx"], desc["dtype"])[:16]
    desc["slide_id_method"] = SLIDE_ID_METHOD
    return sid, desc


# ── regions ─────────────────────────────────────────────────────────────

def normalize_polygon(polygon) -> Optional[List[List[float]]]:
    """The canonical polygon for hashing, or None for "no polygon".

    None, [] and anything with fewer than 3 distinct vertices -> None.
    Vertices are full-resolution ``(x, y)``, rounded to 1e-3 px; consecutive
    duplicates and a closing vertex are dropped; the order is made
    counter-clockwise (positive signed area in (x, y)); the sequence starts
    at its lexicographically smallest vertex."""
    if not polygon:
        return None
    pts = []
    for p in polygon:
        x, y = (round(float(p[0]), 3), round(float(p[1]), 3))
        x, y = x + 0.0, y + 0.0                   # no -0.0
        if not pts or pts[-1] != (x, y):
            pts.append((x, y))
    while len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()
    if len(set(pts)) < 3:
        return None
    area2 = sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
                for i in range(len(pts)))
    if area2 < 0:
        pts.reverse()
    k = pts.index(min(pts))
    pts = pts[k:] + pts[:k]
    return [[x, y] for x, y in pts]


def _bbox4(bbox_fullres) -> List[int]:
    if bbox_fullres is None or len(bbox_fullres) != 4:
        raise ValueError(f"a region needs bbox_fullres [y0, y1, x0, x1], got {bbox_fullres!r}")
    y0, y1, x0, x1 = (int(v) for v in bbox_fullres)
    if not (y1 > y0 and x1 > x0):
        raise ValueError(f"empty region bbox {[y0, y1, x0, x1]}")
    return [y0, y1, x0, x1]


def region_type(roi: Dict) -> str:
    """The region type of a legacy ROI dict (``type`` /
    ``analysis_region_type``; a ROI without either is ``roi``)."""
    value = roi.get("type") or roi.get("analysis_region_type") or "roi"
    if value not in REGION_TYPES:
        raise ValueError(f"unknown region type {value!r}")
    return value


def region_id(slide_id: str, rtype: str, bbox_fullres, polygon_fullres=None) -> str:
    """``reg_<16 hex>`` fixed by geometry (module docstring)."""
    if rtype not in REGION_TYPES:
        raise ValueError(f"unknown region type {rtype!r}")
    if not slide_id:
        raise ValueError("a region needs a slide_id")
    poly = normalize_polygon(polygon_fullres)
    payload = json.dumps(
        {"slide_id": slide_id, "type": rtype, "bbox_fullres": _bbox4(bbox_fullres),
         "polygon": None if poly is None else [[f"{x:.3f}", f"{y:.3f}"] for x, y in poly]},
        sort_keys=True, separators=(",", ":"))
    return "reg_" + hashlib.sha256(payload.encode()).hexdigest()[:16]


def region_id_of_roi(slide_id: str, roi: Dict) -> str:
    return region_id(slide_id, region_type(roi), roi.get("bbox_fullres"),
                     roi.get("polygon_fullres"))


def bbox_columns(bbox_fullres) -> Dict[str, int]:
    """``[y0, y1, x0, x1]`` -> the object tables' half-open columns."""
    y0, y1, x0, x1 = _bbox4(bbox_fullres)
    return {"bbox_x0": x0, "bbox_y0": y0, "bbox_x1": x1, "bbox_y1": y1}


# ── coordinate transforms ───────────────────────────────────────────────

class SlideFrames:
    """The explicit transforms of one slide: level <-> global <-> physical
    <-> region_local <-> viewer_world. Points are ``(x, y)``; scalars and
    numpy arrays both work."""

    def __init__(self, level_shapes_yx: Sequence[Sequence[int]],
                 physical_size_yx_um: Optional[Sequence[float]] = None):
        self.level_shapes = [tuple(int(v) for v in s) for s in level_shapes_yx]
        h0, w0 = self.level_shapes[0]
        self.scales = [(h0 / h, w0 / w) for h, w in self.level_shapes]
        self.physical = (None if physical_size_yx_um is None
                         else (float(physical_size_yx_um[0]), float(physical_size_yx_um[1])))

    @classmethod
    def from_description(cls, desc: Dict) -> "SlideFrames":
        return cls(desc["level_shapes_yx"], desc.get("physical_size_yx_um"))

    def scale_yx(self, level: int) -> Tuple[float, float]:
        return self.scales[int(level)]

    def level_to_global(self, x, y, level: int):
        sy, sx = self.scale_yx(level)
        return (np.add(x, 0.5) * sx - 0.5, np.add(y, 0.5) * sy - 0.5)

    def global_to_level(self, x, y, level: int):
        sy, sx = self.scale_yx(level)
        return (np.add(x, 0.5) / sx - 0.5, np.add(y, 0.5) / sy - 0.5)

    def _need_physical(self):
        if self.physical is None:
            raise ValueError("physical_um is unavailable: the source records no PhysicalSize")
        return self.physical

    def global_to_physical(self, x, y):
        dy, dx = self._need_physical()
        return (np.multiply(x, dx), np.multiply(y, dy))

    def physical_to_global(self, x_um, y_um):
        dy, dx = self._need_physical()
        return (np.divide(x_um, dx), np.divide(y_um, dy))

    @staticmethod
    def global_to_region_local(x, y, bbox_fullres):
        y0, _y1, x0, _x1 = _bbox4(bbox_fullres)
        return (np.subtract(x, x0), np.subtract(y, y0))

    @staticmethod
    def region_local_to_global(x, y, bbox_fullres):
        y0, _y1, x0, _x1 = _bbox4(bbox_fullres)
        return (np.add(x, x0), np.add(y, y0))

    @staticmethod
    def global_to_viewer_world(x, y):
        return (np.add(x, 0.5), np.add(y, 0.5))

    @staticmethod
    def viewer_world_to_global(wx, wy):
        """The pixel picked at a world point: ``floor(world)``."""
        return (np.floor(wx).astype(np.int64), np.floor(wy).astype(np.int64))


def transforms_entry(desc: Dict) -> Dict:
    """One slide's entry of ``transforms.json``."""
    frames = SlideFrames.from_description(desc)
    phys = desc.get("physical_size_yx_um")
    return {
        "global_pixel": {"shape_yx": list(frames.level_shapes[0]),
                         "center_convention": "integer index = pixel centre"},
        "physical_um": ({"available": True, "size_yx_um": list(phys),
                         "source": "OME PhysicalSizeY/X"} if phys else
                        {"available": False, "size_yx_um": None,
                         "source": "not recorded by the source"}),
        "pyramid_levels": [{"level": lv, "shape_yx": list(shape), "scale_yx": list(scale)}
                           for lv, (shape, scale) in enumerate(zip(frames.level_shapes,
                                                                   frames.scales))],
        "level_to_global": "global = (i + 0.5) * scale - 0.5, per axis",
        "viewer_world": "world = global_pixel + 0.5 (pixel j covers [j, j+1)); pick = floor(world)",
    }


def merged_transforms(existing: Optional[Dict], slide_id: str, desc: Dict) -> Dict:
    """``transforms.json`` with `slide_id`'s entry set (other slides kept)."""
    data = dict(existing or {})
    data["schema_version"] = TRANSFORMS_SCHEMA_VERSION
    slides = dict(data.get("slides") or {})
    slides[slide_id] = transforms_entry(desc)
    data["slides"] = slides
    data["region_local"] = ("global_pixel - (bbox_y0, bbox_x0) of the region "
                            "(regions.parquet / the workspace's roi_config)")
    return data


__all__ = ["PROJECT_SCHEMA_VERSION", "LEGACY", "SLIDE_ID_METHOD", "REGION_TYPES",
           "ProjectSchemaError", "check_project_schema", "read_project_schema",
           "project_dir_of_workspace", "describe_slide", "describe_raw_source",
           "slide_signature", "normalize_polygon", "region_id", "region_id_of_roi",
           "region_type", "bbox_columns", "SlideFrames", "transforms_entry",
           "merged_transforms"]
