"""Step3's masks: which run, which mask where, and its labels by viewer tile
-- block 4a (the data layer; no Qt, not wired into Step3 yet).

  * `list_runs` / `choose_run`: the finished runs of the current ROI
    workspace, and which one Step3 shows;
  * `resolve_masks`: a run's cell and nucleus masks, classified BY METHOD
    (Step2 names every primary output `global_mask*`, and block N records its
    pyramid as `cell`, but a nuclei-only method's primary output is nuclei),
    each with its bbox from the metadata only -- when the evidence is not
    there the mask is not shown and the reason says why -- and a pyramid only
    when it passes the full check against the viewer's levels;
  * `ensure_pyramid`: build a missing pyramid into the run directory; when
    that directory cannot be written, into memory for this session; when
    that fails too, level 0 only (plan ruling 8a);
  * `read_label_tile`: one uint32 label tile on the viewer's own tile grid
    (the world rect of `Step1GpuBinding._world_rect`); a coarse level
    without a pyramid is `Unavailable`, never zeros sampled from level 0;
  * `outline_reference` / `fill_colour`: the numeric reference block 4b's
    shaders are held to.

Nothing here writes outside the run directory, and Step2's files are read
only -- a pyramid is the one thing written, through `label_pyramid.build`.
"""

import dataclasses
import json
import os
import re
from typing import Any, Dict, Optional, Tuple

import numpy as np

from . import label_pyramid

CELL = "cell"
NUCLEUS = "nucleus"

# Method -> what its outputs are (plan block 4a's table). "primary" is
# Step2's `global_mask*`, "nuclei" its `global_nuclei_mask*`.
_PRIMARY_IS_CELL = frozenset({
    "cellpose_wholecell_fusion", "mesmer_whole_cell",
})
_PRIMARY_IS_NUCLEUS = frozenset({
    "cellpose_nuclei_dapi", "stardist_nuclei_dapi", "mesmer_nuclei",
})
# Block N2: the expansions keep their nuclei too (runs from before it have no
# nuclei file: their nucleus mask is "not kept, re-run Step2").
_EXPANSIONS = frozenset({"cellpose_nuclei_expansion", "stardist_nuclei_expansion"})
_PRIMARY_CELL_AND_NUCLEI = frozenset({
    "mesmer_nuclear_guided", "cellpose_nuclei_expansion", "stardist_nuclei_expansion",
    "cellpose_nuclei_hq", "cellpose_nuclei_hq2", "cellpose_nuclei_csd",
})

# The kind block N records for each Step2 file, whatever the method.
_RECORDED_KIND = {"primary": CELL, "nuclei": NUCLEUS}


# ── records ──────────────────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class Run:
    run_id: str
    method: str
    created_at: str
    run_dir: str                 # real, absolute path
    meta: Dict[str, Any]
    meta_path: str
    active: bool = False


@dataclasses.dataclass(frozen=True)
class Pyramid:
    group: Any                   # a zarr group, on disk or in memory
    attrs: Dict[str, Any]
    where: str                   # "disk" | "memory"


@dataclasses.dataclass(frozen=True)
class MaskSource:
    kind: str                    # CELL | NUCLEUS (what it IS, by method)
    mask_path: str               # the level-0 uint32 zarr, real path
    bbox: Tuple[int, int, int, int]   # y0, y1, x0, x1 in slide pixels
    pyramid_path: str            # where its pyramid is recorded / belongs
    pyramid_kind: str            # the kind block N records for this file
    pyramid: Optional[Pyramid] = None
    pyramid_reason: Optional[str] = None   # why `pyramid` is None


@dataclasses.dataclass(frozen=True)
class Unavailable:
    reason: str


@dataclasses.dataclass(frozen=True)
class LabelTile:
    labels: np.ndarray           # uint32, (tile rows, tile cols) of the level
    world_rect: Tuple[float, float, float, float]   # x0, x1, y0, y1


@dataclasses.dataclass(frozen=True)
class EnsureResult:
    status: str                  # "ready" | "level0_only" | "cancelled"
    source: MaskSource
    reason: Optional[str] = None


# ── runs ─────────────────────────────────────────────────────────────────

def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _real(path):
    return os.path.realpath(os.path.abspath(path))


def _inside(path, root):
    path, root = _real(path), _real(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _run_meta(run_dir):
    for name in ("segmentation_meta.json", "run_metadata.json"):
        path = os.path.join(run_dir, name)
        meta = _load_json(path) if os.path.isfile(path) else None
        if meta is not None:
            return meta, path
    return None, None


def _created_from_name(name):
    m = re.search(r"(\d{8})_(\d{6})", str(name or ""))
    if not m:
        return ""
    d, t = m.groups()
    return f"{d[:4]}-{d[4:6]}-{d[6:]}T{t[:2]}:{t[2:4]}:{t[4:]}"


def list_runs(roi_dir):
    """The finished runs of the ROI workspace at `roi_dir`, newest first.
    From `roi_index.json`'s `segmentation_runs` and the run directories under
    `step2/segmentation_runs` and `step2/segmentation_results`; one entry per
    `run_id` (the directory's real path when there is none). Left out: an
    index entry whose status is not `done`, a directory without
    `segmentation_meta.json` / `run_metadata.json`, a directory outside the
    workspace and a run whose metadata names another ROI. `active` marks the
    index's `active_segmentation_run` when it is one of these."""
    roi_dir = _real(roi_dir)
    index = _load_json(os.path.join(roi_dir, "roi_index.json")) or {}
    manifest = _load_json(os.path.join(roi_dir, "roi_manifest.json")) or {}
    roi_id = str(manifest.get("roi_id") or index.get("roi_id") or "")

    candidates = []                           # (run_dir, index entry or None)
    refused = set()                           # index keys not done
    for key, entry in dict(index.get("segmentation_runs") or {}).items():
        entry = dict(entry or {})
        run_id = str(entry.get("run_id") or key)
        if str(entry.get("status") or "") != "done":
            refused.add(run_id)
            continue
        path = entry.get("path") or os.path.join("step2", "segmentation_runs", run_id)
        candidates.append((path if os.path.isabs(path) else os.path.join(roi_dir, path), entry))
    for sub in ("segmentation_runs", "segmentation_results"):
        base = os.path.join(roi_dir, "step2", sub)
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            path = os.path.join(base, name)
            if os.path.isdir(path):
                candidates.append((path, None))

    runs: Dict[str, Run] = {}
    for path, entry in candidates:
        if not os.path.isdir(path) or not _inside(path, roi_dir):
            continue
        meta, meta_path = _run_meta(path)
        if meta is None:
            continue
        entry = entry or {}
        run_dir = _real(path)
        run_id = str(entry.get("run_id") or meta.get("run_id") or meta.get("result_id")
                     or run_dir)
        if run_id in runs or run_id in refused:
            continue
        if roi_id and meta.get("roi_id") and str(meta.get("roi_id")) != roi_id:
            continue
        runs[run_id] = Run(
            run_id=run_id,
            method=str(meta.get("method") or entry.get("method") or ""),
            created_at=str(meta.get("created_at") or entry.get("created_at")
                           or _created_from_name(os.path.basename(run_dir))),
            run_dir=run_dir, meta=meta, meta_path=meta_path)
    active = str(index.get("active_segmentation_run") or "")
    out = [dataclasses.replace(r, active=(r.run_id == active)) for r in runs.values()]
    out.sort(key=lambda r: r.created_at, reverse=True)
    return out


def choose_run(runs, requested_dir=None, current=None):
    """The run Step3 shows: the one at `requested_dir` (Step2's finished
    dialog) when it is in `runs`; else `current` (a run, or its id) when it
    is still there; else the active one; else the newest; None for no runs.
    A directory that is not one of `runs` -- another workspace's included --
    is never used."""
    runs = list(runs or [])
    if not runs:
        return None
    if requested_dir:
        want = _real(requested_dir)
        for r in runs:
            if r.run_dir == want:
                return r
    if current is not None:
        cur = current.run_id if isinstance(current, Run) else str(current)
        for r in runs:
            if r.run_id == cur:
                return r
    for r in runs:
        if r.active:
            return r
    return runs[0]


# ── masks ────────────────────────────────────────────────────────────────

def _bbox(value):
    try:
        y0, y1, x0, x1 = (int(v) for v in value)
    except (TypeError, ValueError):
        return None
    return (y0, y1, x0, x1)


def _nuclei_beside(mask_path):
    name = os.path.basename(mask_path)
    if not name.startswith("global_mask"):
        return None
    return os.path.join(os.path.dirname(mask_path),
                        "global_nuclei_mask" + name[len("global_mask"):])


def _locate(run, roi_name, roi_bbox):
    """(mask path, bbox, recorded pyramids {"cell", "nucleus"}, label store or
    None) of the run's primary mask for this ROI, or a reason string."""
    meta = run.meta
    rois = meta.get("rois")
    if isinstance(rois, list):
        mine = [r for r in rois if isinstance(r, dict) and r.get("roi_name") == roi_name]
        if not mine:
            return f"this run has no result for ROI '{roi_name}'"
        if len(mine) > 1:
            return f"this run has {len(mine)} results named '{roi_name}'"
        entry = mine[0]
        paths = dict(entry.get("paths") or {})
        # `zarr_path` of the first ROI is Step2's `global_mask.zarr` alias
        # (a link, or a copy where links fail); `paths.mask_zarr` is the file
        # the pyramid was built from.
        mask = paths.get("mask_zarr") or entry.get("zarr_path")
        bbox = _bbox(entry.get("bbox_fullres"))
        if bbox is None:
            region = _load_json(os.path.join(run.run_dir,
                                             f"segmentation_meta_{roi_name}.json")) or {}
            bbox = _bbox(region.get("bbox")) or _bbox(region.get("roi_bbox_fullres"))
        if bbox is None:
            return f"the run does not record where ROI '{roi_name}' is"
        if bbox != tuple(roi_bbox):
            return (f"the run's ROI '{roi_name}' is at {list(bbox)}, the current ROI "
                    f"is at {list(roi_bbox)}")
        recorded = dict(entry.get("label_pyramid") or {})
        store = entry.get("label_store")
    elif meta.get("mode") == "full_wsi":
        mask = meta.get("zarr_path") or dict(meta.get("paths") or {}).get("mask_zarr")
        bbox = tuple(roi_bbox)
        recorded = dict(meta.get("label_pyramid") or {})
        store = meta.get("label_store")
    else:
        return "the run's metadata has neither ROI results nor a whole-image result"
    if not mask:
        return "the run records no uint32 mask (zarr)"
    mask = mask if os.path.isabs(mask) else os.path.join(run.run_dir, mask)
    return mask, bbox, recorded, (store if isinstance(store, dict) else None)


def _from_store(run, store, bbox, recorded, view_level_shapes, out):
    """Block N2: the label store says which array is the cells and which the
    nuclei -- no guessing from the method. A file's pyramid is the one block
    N recorded for it (`global_mask*` as "cell", `global_nuclei_mask*` as
    "nucleus")."""
    reasons = out["reasons"]
    if store.get("complete") is not True:
        reasons[CELL] = reasons[NUCLEUS] = "the run's label store is incomplete — re-run Step2"
        return out
    for kind in (CELL, NUCLEUS):
        entry = store.get(kind)
        if not entry or not entry.get("path"):
            reasons[kind] = f"this run has no {kind} mask"
            continue
        path = entry["path"]
        path = path if os.path.isabs(path) else os.path.join(run.run_dir, path)
        why = _open_mask(path, bbox)
        if why:
            reasons[kind] = why
            continue
        role = "nuclei" if os.path.basename(path).startswith("global_nuclei_mask") else "primary"
        out[kind] = _source(kind, path, bbox,
                            recorded.get("nucleus" if role == "nuclei" else "cell"),
                            _RECORDED_KIND[role], view_level_shapes)
    return out


def _open_mask(path, bbox):
    """None when the mask is a uint32 zarr of the bbox's size, else why not."""
    import zarr
    if not os.path.exists(path):
        tiffs = [n for n in os.listdir(os.path.dirname(path)) if n.endswith(".ome.tiff")] \
            if os.path.isdir(os.path.dirname(path)) else []
        if tiffs:
            return ("this run has only an OME-TIFF mask (float32: labels above 2^24 "
                    "are lost); re-run Step2")
        return f"the mask {os.path.basename(path)} is missing"
    try:
        arr = zarr.open(path, mode="r")
    except Exception as exc:  # noqa: BLE001 -- unreadable: the reason is shown
        return f"the mask {os.path.basename(path)} cannot be read ({exc})"
    if getattr(arr, "dtype", None) != np.dtype("<u4") or len(arr.shape) != 2:
        return f"the mask {os.path.basename(path)} is not a 2-D uint32 array"
    y0, y1, x0, x1 = bbox
    if tuple(arr.shape) != (y1 - y0, x1 - x0):
        return (f"the mask is {tuple(arr.shape)}, its region is "
                f"{(y1 - y0, x1 - x0)}")
    return None


def check_pyramid(group, attrs, mask_path, bbox, view_level_shapes, kind):
    """None when the pyramid (`label_pyramid.read` already passed, or an
    in-memory group) is whole and belongs to this mask on this viewer's
    levels; otherwise the reason it is not used."""
    if attrs.get("complete") is not True:
        return "the pyramid is not complete"
    if attrs.get("kind") != kind:
        return f"the pyramid is recorded as '{attrs.get('kind')}', expected '{kind}'"
    level0 = dict(attrs.get("level0") or {})
    l0 = attrs.get("level0_abs") or level0.get("path") or ""
    if not l0 or _real(l0) != _real(mask_path):
        return "the pyramid was built from another mask"
    if _bbox(attrs.get("roi_bbox")) != tuple(bbox):
        return "the pyramid's region is not this mask's region"
    view = [list(map(int, s)) for s in view_level_shapes]
    if [list(map(int, s)) for s in (attrs.get("raw_level_shapes") or [])] != view:
        return "the pyramid's levels are not the viewer's levels"
    expected = {lv["level"]: lv for lv in label_pyramid.level_grid(view, bbox)}
    recorded = {}
    for lv in attrs.get("levels") or []:
        try:
            recorded[int(lv["level"])] = lv
        except (KeyError, TypeError, ValueError):
            return "the pyramid's level list is malformed"
    if set(recorded) != set(expected):
        missing = sorted(set(expected) - set(recorded))
        return f"the pyramid lacks level(s) {missing}" if missing else \
            "the pyramid records levels the viewer does not have"
    for level, want in expected.items():
        lv = recorded[level]
        if list(lv.get("origin") or []) != want["origin"] or \
                list(lv.get("shape") or []) != want["shape"]:
            return f"the pyramid's level {level} is not on the viewer's grid"
        try:
            arr = group[str(lv.get("path") or level)]
        except Exception:  # noqa: BLE001 -- a missing array is a reason
            return f"the pyramid's level {level} is missing"
        if list(arr.shape) != want["shape"]:
            return f"the pyramid's level {level} is {list(arr.shape)}, expected {want['shape']}"
        if arr.dtype != np.dtype("<u4"):
            return f"the pyramid's level {level} is {arr.dtype}, not uint32"
    return None


def _disk_pyramid(path, mask_path, bbox, view_level_shapes, kind):
    """(Pyramid, None) or (None, reason) for the pyramid at `path`."""
    import zarr
    if not path or not os.path.exists(path):
        return None, "no label pyramid"
    attrs = label_pyramid.read(path)
    if attrs is None:
        return None, "the label pyramid is incomplete or its mask changed"
    try:
        group = zarr.open_group(path, mode="r")
    except Exception as exc:  # noqa: BLE001
        return None, f"the label pyramid cannot be opened ({exc})"
    reason = check_pyramid(group, attrs, mask_path, bbox, view_level_shapes, kind)
    if reason:
        return None, reason
    return Pyramid(group=group, attrs=attrs, where="disk"), None


def _source(kind, mask_path, bbox, recorded_path, pyramid_kind, view_level_shapes):
    mask_path = _real(mask_path)
    path = recorded_path or label_pyramid.pyramid_path_for(mask_path)
    pyr, why = _disk_pyramid(path, mask_path, bbox, view_level_shapes, pyramid_kind)
    return MaskSource(kind=kind, mask_path=mask_path, bbox=tuple(bbox), pyramid_path=path,
                      pyramid_kind=pyramid_kind, pyramid=pyr, pyramid_reason=why)


def resolve_masks(run, roi_name, roi_bbox, view_level_shapes):
    """{"cell": MaskSource | None, "nucleus": MaskSource | None,
    "reasons": {kind: why it is None}} of `run` for the current ROI (`roi_name`,
    its `roi_bbox` = y0, y1, x0, x1 in slide pixels) on a viewer whose levels
    are `view_level_shapes`."""
    out = {CELL: None, NUCLEUS: None, "reasons": {}}
    reasons = out["reasons"]
    method = str(run.meta.get("method") or run.method or "")
    if method in _PRIMARY_IS_CELL:
        primary_kind, has_nuclei = CELL, False
    elif method in _PRIMARY_IS_NUCLEUS:
        primary_kind, has_nuclei = NUCLEUS, False
    elif method in _PRIMARY_CELL_AND_NUCLEI:
        primary_kind, has_nuclei = CELL, True
    else:
        reasons[CELL] = reasons[NUCLEUS] = f"unknown method '{method}': not shown"
        return out
    other = NUCLEUS if primary_kind == CELL else CELL
    if not has_nuclei:
        reasons[other] = f"{method} makes no {other} mask"
    roi_bbox = _bbox(roi_bbox)
    if roi_bbox is None:
        reasons[primary_kind] = "the current ROI has no bbox"
        if has_nuclei:
            reasons[NUCLEUS] = reasons[primary_kind]
        return out
    located = _locate(run, roi_name, roi_bbox)
    if isinstance(located, str):
        reasons[primary_kind] = located
        if has_nuclei:
            reasons[NUCLEUS] = located
        return out
    mask, bbox, recorded, store = located
    if store is not None:
        out["reasons"].clear()
        return _from_store(run, store, bbox, recorded, view_level_shapes, out)
    why = _open_mask(mask, bbox)
    if why:
        reasons[primary_kind] = why
        if has_nuclei:
            reasons[NUCLEUS] = why
        return out
    out[primary_kind] = _source(primary_kind, mask, bbox, recorded.get("cell"),
                                _RECORDED_KIND["primary"], view_level_shapes)
    if has_nuclei:
        nuclei = _nuclei_beside(_real(mask))
        why = _open_mask(nuclei, bbox) if nuclei else "no nucleus mask beside the cell mask"
        if why and method in _EXPANSIONS and (not nuclei or not os.path.exists(nuclei)):
            why = "this run was made before nuclei were kept — re-run Step2"
        if why:
            reasons[NUCLEUS] = why
        else:
            out[NUCLEUS] = _source(NUCLEUS, nuclei, bbox, recorded.get("nucleus"),
                                   _RECORDED_KIND["nuclei"], view_level_shapes)
    return out


# ── pyramids ─────────────────────────────────────────────────────────────

def _os_error(exc):
    """The OSError behind `exc`, if any: zarr's directory store re-raises a
    failed write (no permission, disk full) as `KeyError(key) from` it."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, OSError):
            return exc
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return None


def ensure_pyramid(source, raw_level_shapes, cancel_check=None):
    """Give `source` a pyramid (block 4b's reader thread calls this). A source
    that has one is returned as it is. Otherwise its pyramid is built into
    the run directory; when that cannot be written (OSError), into memory
    for this session; when that fails too the source stays level 0 only.
    A cancel ends it at once (no memory fallback), leaving no partial
    directory and not touching a pyramid that was already there. A pyramid
    that `label_pyramid.read` accepts is never replaced."""
    if source.pyramid is not None:
        return EnsureResult("ready", source)
    shapes = [list(map(int, s)) for s in raw_level_shapes]
    args = (source.mask_path, shapes, list(source.bbox), source.pyramid_kind)
    path = source.pyramid_path
    if os.path.exists(path) and label_pyramid.read(path) is not None:
        return EnsureResult("level0_only", source,
                            f"{source.pyramid_reason}; the pyramid there is kept")
    try:
        label_pyramid.build(args[0], path, *args[1:], cancel_check=cancel_check)
        pyr, why = _disk_pyramid(path, source.mask_path, source.bbox, shapes,
                                 source.pyramid_kind)
        if pyr is None:
            return EnsureResult("level0_only", dataclasses.replace(source, pyramid_reason=why),
                                why)
        return EnsureResult("ready", dataclasses.replace(source, pyramid=pyr,
                                                         pyramid_reason=None))
    except label_pyramid.Cancelled:
        return EnsureResult("cancelled", source, "cancelled")
    except Exception as exc:  # noqa: BLE001 -- level 0 only, the reason is shown
        os_exc = _os_error(exc)
        if os_exc is None:
            why = f"the pyramid could not be built ({exc!r})"
            return EnsureResult("level0_only", dataclasses.replace(source, pyramid_reason=why),
                                why)
        disk_why = f"the run directory cannot be written ({os_exc})"
    try:
        group = label_pyramid.build_in_memory(*args, cancel_check=cancel_check)
    except label_pyramid.Cancelled:
        return EnsureResult("cancelled", source, "cancelled")
    except Exception as exc:  # noqa: BLE001 -- MemoryError included
        why = f"{disk_why}; in memory it failed too ({exc!r})"
        return EnsureResult("level0_only", dataclasses.replace(source, pyramid_reason=why), why)
    attrs = dict(group.attrs)
    why = check_pyramid(group, attrs, source.mask_path, source.bbox, shapes,
                        source.pyramid_kind)
    if why:
        return EnsureResult("level0_only", dataclasses.replace(source, pyramid_reason=why), why)
    pyr = Pyramid(group=group, attrs=attrs, where="memory")
    return EnsureResult("ready", dataclasses.replace(source, pyramid=pyr,
                                                     pyramid_reason=disk_why))


# ── tiles ────────────────────────────────────────────────────────────────

def _overlap(a0, a1, b0, b1):
    return max(a0, b0), min(a1, b1)


def read_label_tile(source, level, tx, ty, tile_size, view_level_shapes):
    """The labels of viewer tile (`level`, `tx`, `ty`) of size `tile_size`,
    as `LabelTile` (uint32, the tile's rows x columns on that level, 0 where
    the mask does not reach) with its world rect -- x0 = tx * T * ds_x, width
    = columns * ds_x, per-axis unrounded ratios, as the viewer places its own
    tiles. Level 0 reads the mask (slide pixel minus the bbox origin); level
    L the pyramid's level L (minus its origin). `Unavailable` for a coarse
    level without a pyramid, or a tile outside the level."""
    level, tx, ty, T = int(level), int(tx), int(ty), int(tile_size)
    shapes = [tuple(int(v) for v in s) for s in view_level_shapes]
    if not 0 <= level < len(shapes):
        return Unavailable(f"the viewer has no level {level}")
    h, w = shapes[level]
    r0, c0 = ty * T, tx * T
    r1, c1 = min(r0 + T, h), min(c0 + T, w)
    if tx < 0 or ty < 0 or r1 <= r0 or c1 <= c0:
        return Unavailable(f"tile ({tx}, {ty}) is outside level {level}")
    ds_y, ds_x = shapes[0][0] / h, shapes[0][1] / w
    rect = (c0 * ds_x, c0 * ds_x + (c1 - c0) * ds_x, r0 * ds_y, r0 * ds_y + (r1 - r0) * ds_y)
    if level == 0:
        import zarr
        arr = zarr.open(source.mask_path, mode="r")
        oy, ox = source.bbox[0], source.bbox[2]
        ah, aw = arr.shape
    else:
        if source.pyramid is None:
            return Unavailable(source.pyramid_reason or "no label pyramid")
        lv = next((lv for lv in source.pyramid.attrs.get("levels") or []
                   if int(lv["level"]) == level), None)
        if lv is None:                    # the ROI does not reach this level's grid
            return LabelTile(np.zeros((r1 - r0, c1 - c0), np.uint32), rect)
        arr = source.pyramid.group[str(lv.get("path") or level)]
        oy, ox = (int(v) for v in lv["origin"])
        ah, aw = arr.shape
    out = np.zeros((r1 - r0, c1 - c0), np.uint32)
    a0, a1 = _overlap(r0, r1, oy, oy + ah)
    b0, b1 = _overlap(c0, c1, ox, ox + aw)
    if a1 > a0 and b1 > b0:
        out[a0 - r0:a1 - r0, b0 - c0:b1 - c0] = arr[a0 - oy:a1 - oy, b0 - ox:b1 - ox]
    return LabelTile(out, rect)


# ── display reference (block 4b's shaders are held to these) ─────────────

MAX_OUTLINE_RADIUS = 8

def outline_reference(ids, width):
    """Outline pixels of a SCREEN id image (one label per screen pixel): a
    pixel whose id is not 0 and whose (2*width+1)-square neighbourhood
    (Chebyshev distance <= width) holds a different id. Outside the image
    counts as id 0, so a mask's edge is drawn. `width` is the SCREEN radius,
    0..8 (a logical width 0..4 times the device pixel ratio, block 4b); 0
    draws none."""
    ids = np.asarray(ids, dtype=np.uint32)
    width = int(width)
    if not 0 <= width <= MAX_OUTLINE_RADIUS:
        raise ValueError(f"width is 0..{MAX_OUTLINE_RADIUS}")
    out = np.zeros(ids.shape, dtype=bool)
    if width == 0 or ids.size == 0:
        return out
    h, w = ids.shape
    pad = np.zeros((h + 2 * width, w + 2 * width), np.uint32)
    pad[width:width + h, width:width + w] = ids
    for dy in range(-width, width + 1):
        for dx in range(-width, width + 1):
            if dy or dx:
                out |= pad[width + dy:width + dy + h, width + dx:width + dx + w] != ids
    return out & (ids != 0)


def _hash(ids):
    """lowbias32 (Chris Wellons): uint32 -> uint32, expressible in GLSL."""
    x = np.asarray(ids, dtype=np.uint64) & 0xFFFFFFFF
    x ^= x >> np.uint64(16)
    x = (x * np.uint64(0x7FEB352D)) & np.uint64(0xFFFFFFFF)
    x ^= x >> np.uint64(15)
    x = (x * np.uint64(0x846CA68B)) & np.uint64(0xFFFFFFFF)
    x ^= x >> np.uint64(16)
    return x


def fill_colour(ids):
    """RGBA uint8 per id for the fill mode: a fixed colour from the id's
    integer hash -- each of R, G, B is 55 + (its hash byte * 200) // 255, so
    no cell is near black -- and alpha 255; id 0 is (0, 0, 0, 0)."""
    ids = np.asarray(ids, dtype=np.uint32)
    h = _hash(ids)
    out = np.zeros(ids.shape + (4,), np.uint8)
    for c, shift in enumerate((0, 8, 16)):
        byte = (h >> np.uint64(shift)) & np.uint64(0xFF)
        out[..., c] = (55 + (byte * 200) // 255).astype(np.uint8)
    out[..., 3] = 255
    out[ids == 0] = 0
    return out
