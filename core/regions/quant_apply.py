"""Step4 with a Regions result (docs/f4_regions_downstream_plan.md §F.9).

What Step2 froze for a region -- its Regions snapshot and the per-object
facts (core, fraction inside it, edge flags, measurement-only exclusion
bits) -- read back, checked, and turned into the h5ad/CSV additions:

- `load_regions(job)`: read and check everything before any output exists;
- `apply_measurement_qc(result, rin)`: after quantification, every
  expression value of an object on a channel of a measurement-only exclusion
  it touches becomes NaN (all expression layers, X with them, the
  distribution statistics by marker name), recorded in a uint8 sink array
  ``measurement_qc``; morphology and the nuclear summary are never touched;
- `obs_frame`, `tma_cores_frame`, `uns_regions`, `provenance_summary`: the
  typed columns, the per-position table, the uns record and the provenance.

Objects are matched by label id: output row r is label ``result.ids[r]``,
its facts row ``result.ids[r] - 1``. No caching, no Qt.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from . import analysis as RA
from . import geometry as G

FORMAT_VERSION = 1
QC_LAYER = "measurement_qc"
QC_CODES = {"0": "not removed by a measurement-only exclusion (a value may still be NaN "
                 "for other reasons, e.g. no nucleus or an empty cytoplasm)",
            "1": "removed (NaN): the object touches a measurement-only exclusion on this channel"}
QC_RULE = ("an object with at least one pixel in a measurement-only exclusion gets NaN for "
           "every expression statistic of that exclusion's channels; morphology and the "
           "nuclear summary are kept")
BLOCK_ROWS = 65536

#: object flags written by Step2 (`core/seam_merge.py`)
FLAG_TOUCHES_CORE_EDGE, FLAG_ANALYSIS_INCLUDE, FLAG_TOUCHES_REGION_EDGE = 1, 2, 4


class RegionsInputError(ValueError):
    """The run's Regions record cannot be used; the message says why."""


@dataclass
class RegionsInput:
    record: Dict
    snapshot: Dict
    snapshot_path: str
    facts_path: str
    core: np.ndarray            # int32, facts row -> index into snapshot["cores"] (-1: none)
    core_fraction: np.ndarray   # float32
    flags: np.ndarray           # uint8
    meas_bits: np.ndarray       # uint64
    uids: List[str]             # measurement-only exclusion uid of bit k
    channel_masks: np.ndarray   # bool (K, C): exclusion k's channels among the job's
    pixel_um: Optional[tuple]   # (y, x) micrometres per pixel, or None
    channel_names: List[str] = field(default_factory=list)
    areas_px: Dict[str, int] = field(default_factory=dict)   # position_key -> core area

    @property
    def tma(self) -> bool:
        return self.snapshot.get("mode") == "tma"


def _path(p, run_dir):
    p = str(p or "")
    if not p:
        return ""
    if os.path.isabs(p):
        return p
    for base in (run_dir, _project_dir(run_dir)):
        if base and os.path.exists(os.path.join(base, p)):
            return os.path.join(base, p)
    return os.path.join(run_dir, p)


def _project_dir(run_dir):
    try:
        from ...utils import run_store
        return run_store.project_dir_of(run_dir) or ""
    except Exception:                                       # noqa: BLE001
        return ""


def _pixel_size(job) -> Optional[tuple]:
    """The slide's physical pixel size from the project's recorded
    description of it (opened only when that record is missing)."""
    try:
        from ..project_identity import describe_slide
        project = _project_dir(job.run_dir)
        manifest = {}
        if project:
            with open(os.path.join(project, "project_manifest.json"), encoding="utf-8") as f:
                manifest = json.load(f)
        _sid, desc = describe_slide(job.slide, manifest.get("sources"))
        phys = desc.get("physical_size_yx_um")
        if phys and float(phys[0]) > 0 and float(phys[1]) > 0:
            return float(phys[0]), float(phys[1])
    except Exception:                                       # noqa: BLE001
        pass
    return None


def outline_area_px(polygons, shape) -> int:
    """Pixels of a core: its pieces filled as the analysis fills a polygon
    (`geometry.fill`) and joined pixel by pixel -- the pieces' own area,
    without the keyhole bridges the analysis outline walks between them."""
    polys = [p for p in polygons if len(p) >= 3]
    if not polys:
        return 0
    box = G.bbox_of([q for p in polys for q in p], shape)
    mask = np.zeros((box[1] - box[0], box[3] - box[2]), dtype=bool)
    for p in polys:
        mask |= G.fill(p, box)
    return int(mask.sum())


def core_areas(snap) -> Dict[str, int]:
    """position_key -> area of every core bound to a map position, from the
    snapshot only: each core's pieces in `display_cores` (the run's cores and
    the others alike); a run core without that entry falls back to its
    analysis outline."""
    shape = tuple(snap["slide_shape"])
    out = {}
    for c in snap.get("display_cores") or []:
        key = c.get("position_key")
        if key and c.get("polygons"):
            out[key] = outline_area_px(c["polygons"], shape)
    for c in snap.get("cores") or []:
        if c["position_key"] not in out:
            out[c["position_key"]] = outline_area_px([c["polygon"]], shape)
    return out


def load_regions(job) -> Optional[RegionsInput]:
    """The job's Regions facts, checked; None for a run without Regions.
    Raises RegionsInputError when they cannot be used."""
    rec = getattr(job, "regions", None)
    if not rec:
        return None
    snap_path = _path(rec.get("snapshot"), job.run_dir)
    facts_path = _path((rec.get("cells") or {}).get("path"), job.run_dir)
    try:
        snap = RA.load_snapshot(snap_path)
    except Exception as exc:                                # noqa: BLE001
        raise RegionsInputError(f"the run's Regions snapshot cannot be read ({exc}) — re-run Step2")
    if snap.get("identity") != rec.get("identity"):
        raise RegionsInputError("the run's Regions snapshot is not the one its labels were made with")
    if "display_cores" not in snap:
        raise RegionsInputError("this run's Regions snapshot is in an older format — re-run Step2")
    par = [int(v) for v in (snap.get("parent") or {}).get("bbox_fullres") or []]
    if par != [int(v) for v in job.bbox]:
        raise RegionsInputError(f"the Regions snapshot is for the region {par}, "
                                f"this result covers {list(job.bbox)}")
    try:
        with np.load(facts_path, allow_pickle=False) as z:
            arrays = {k: z[k] for k in ("core", "core_fraction", "flags", "meas_bits",
                                        "measurement_exclusions", "facts_version")}
    except Exception as exc:                                # noqa: BLE001
        raise RegionsInputError(f"the run's Regions facts cannot be read ({exc}) — re-run Step2")
    if int(arrays["facts_version"]) != 2:
        raise RegionsInputError("this run's Regions facts are in an older format — re-run Step2")
    n = int((rec.get("cells") or {}).get("rows", -1))
    want = {"core": np.int32, "core_fraction": np.float32, "flags": np.uint8, "meas_bits": np.uint64}
    for k, dt in want.items():
        a = arrays[k]
        if a.ndim != 1 or a.shape[0] != n or a.dtype != dt:
            raise RegionsInputError(f"the run's Regions facts ({k}) do not match its {n} objects")
    if n != int(job.n_objects):
        raise RegionsInputError(f"the Regions facts list {n} objects, the labels {job.n_objects}")
    cores = snap.get("cores") or []
    core = arrays["core"]
    if snap.get("mode") == "tma":
        if n and (core.min() < 0 or core.max() >= len(cores)):
            raise RegionsInputError("an object's core is not one of the snapshot's cores")
    elif n and (core != -1).any():
        raise RegionsInputError("a plain-region snapshot cannot assign cores")
    uids = [str(u) for u in arrays["measurement_exclusions"].tolist()]
    by_uid = {e["uid"]: e for e in snap.get("exclusions") or [] if not e.get("seg_invalid")}
    if len(set(uids)) != len(uids) or set(uids) != set(by_uid):
        raise RegionsInputError("the Regions facts' exclusion bits do not match the snapshot")
    if len(uids) > 52:
        raise RegionsInputError(f"{len(uids)} measurement-only exclusions: at most 52 are supported")
    bits = arrays["meas_bits"]
    if n and len(uids) < 64 and (bits >> np.uint64(len(uids))).any():
        raise RegionsInputError("an object carries an exclusion bit the snapshot does not list")
    names = [c.name for c in job.channels]
    masks = np.zeros((len(uids), len(names)), dtype=bool)
    for k, uid in enumerate(uids):
        chans = by_uid[uid]["channels"]
        if chans == "all":
            raise RegionsInputError(f"exclusion {uid}: an all-channel exclusion is not measurement-only")
        unknown = [c for c in chans if c not in names]
        if unknown:
            raise RegionsInputError(f"exclusion {uid} names channels the slide does not have: {unknown}")
        masks[k, [names.index(c) for c in chans]] = True
    return RegionsInput(record=dict(rec), snapshot=snap, snapshot_path=snap_path,
                        facts_path=facts_path, core=core, core_fraction=arrays["core_fraction"],
                        flags=arrays["flags"], meas_bits=bits, uids=uids, channel_masks=masks,
                        pixel_um=_pixel_size(job), channel_names=names,
                        areas_px=core_areas(snap) if snap.get("mode") == "tma" else {})


def _rows(result) -> np.ndarray:
    return result.ids.astype(np.int64) - 1


def _qc_block(rin, rows) -> np.ndarray:
    """bool (rows, channels): the values the exclusions remove."""
    bits = rin.meas_bits[rows]
    m = np.zeros((rows.size, rin.channel_masks.shape[1]), dtype=bool)
    for k in range(len(rin.uids)):
        hit = ((bits >> np.uint64(k)) & np.uint64(1)).astype(bool)
        if hit.any():
            m[hit] |= rin.channel_masks[k]
    return m


def apply_measurement_qc(result, rin, block_rows=BLOCK_ROWS, should_stop=None) -> Dict[str, int]:
    """NaN every expression value the exclusions remove, in the sink, a row
    block at a time, and write the uint8 ``measurement_qc`` sink array.
    `should_stop()` is asked between blocks; True raises QuantStopped."""
    from ..quant_engine import QuantStopped
    n, c = int(result.ids.size), len(result.channel_names)
    root = result.sink.root
    root.create_dataset(QC_LAYER, shape=(n, c), dtype="u1",
                        chunks=(max(1, min(block_rows, n)), max(1, c)), fill_value=0)
    marker_idx = [result.channel_names.index(m) for m in result.dist_markers]
    rows_all = _rows(result)
    cells = values = 0
    for r0 in range(0, n, block_rows):
        if should_stop is not None and should_stop():
            raise QuantStopped()
        r1 = min(n, r0 + block_rows)
        m = _qc_block(rin, rows_all[r0:r1])
        if not m.any():
            continue
        cells += int(m.any(axis=1).sum())
        values += int(m.sum())
        root[QC_LAYER][r0:r1] = m.astype(np.uint8)
        for layer in result.layers:
            a = result.sink.read(layer, r0, r1)
            a[m] = np.nan
            root[layer][r0:r1] = a
        if marker_idx:
            md = m[:, marker_idx]
            if md.any():
                for layer in result.dist_layers:
                    a = result.sink.read(layer, r0, r1)
                    a[md] = np.nan
                    root[layer][r0:r1] = a
    return {"objects_with_removed_values": cells, "removed_values": values}


def _areas(rin, area_px):
    a = np.asarray(area_px, dtype=np.float64)
    if rin.pixel_um is None:
        nan = np.full(a.shape, np.nan)
        return nan, nan.copy()
    um2 = a * rin.pixel_um[0] * rin.pixel_um[1]
    return um2, um2 / 1e6


def obs_frame(result, rin, run_id):
    """The typed obs columns (index = the writer's: label ids as strings)."""
    import pandas as pd
    rows = _rows(result)
    n = rows.size
    idx = pd.Index(result.ids.astype(str))
    snap = rin.snapshot
    cat = lambda v: pd.Categorical([str(x) for x in v])        # noqa: E731
    flags = rin.flags[rows]
    cols = {"slide_id": cat([snap["slide_id"]] * n),
            "segmentation_run_id": cat([str(run_id)] * n)}
    if rin.tma:
        cores = snap["cores"]
        ci = rin.core[rows]
        pick = lambda k: [cores[i].get(k, "") for i in ci]   # noqa: E731
        meta = snap.get("map_sheet") or {}
        area_px = np.array([rin.areas_px.get(cores[i]["position_key"], 0) for i in ci], dtype=np.int64)
        um2, mm2 = _areas(rin, area_px)
        cols.update({
            "core_position_key": cat(pick("position_key")),
            "core_region_id": cat(pick("region_id")),
            "core_position_id": np.asarray(pick("position_id"), dtype=np.int64),
            "core_label": cat(pick("label")),
            "donor_block_id": cat(pick("donor_block_id")),
            "tma_unique_sample_id": cat(pick("tma_unique_sample_id")),
            "tma_annotation_name": cat(pick("annotation")),
            "tma_block_id": cat([meta.get("block_id", "")] * n),
            "tma_block_name": cat([meta.get("block_name", "")] * n),
            "tma_project_name": cat([meta.get("project_name", "")] * n),
            "core_height": cat(pick("core_height")),
            "core_qc": cat(pick("qc")),
            "core_area_px": area_px,
            "core_area_um2": um2,
            "core_area_mm2": mm2,
            "cell_core_fraction": rin.core_fraction[rows].astype(np.float32),
            "cell_touches_core_edge": (flags & FLAG_TOUCHES_CORE_EDGE).astype(bool),
            "analysis_include": (flags & FLAG_ANALYSIS_INCLUDE).astype(bool),
        })
    cols["touches_region_edge"] = (flags & FLAG_TOUCHES_REGION_EDGE).astype(bool)
    return pd.DataFrame(cols, index=idx)


def tma_cores_frame(result, rin):
    """One row per map position: its fields, whether it ran and why not,
    the objects it holds and its core's area (TMA only)."""
    import pandas as pd
    snap = rin.snapshot
    counts: Dict[str, int] = {}
    if rin.tma and result.ids.size:
        ci = rin.core[_rows(result)]
        keys = [c["position_key"] for c in snap["cores"]]
        for i, k in zip(*np.unique(ci, return_counts=True)):
            counts[keys[int(i)]] = int(k)
    rows = []
    for p in snap.get("positions") or []:
        key = p["position_key"]
        rows.append({"position_key": key, "position_id": int(p["position_id"]),
                     "label": p.get("label", ""), "occupied": bool(p.get("occupied")),
                     "donor_block_id": p.get("donor_block_id", ""),
                     "tma_unique_sample_id": str(p.get("tma_unique_sample_id", "")),
                     "annotation": p.get("annotation", ""), "core_height": str(p.get("core_height", "")),
                     "spot_slide": str(p.get("spot_slide", "")), "selected": bool(p.get("selected")),
                     "reason": p.get("reason", ""), "n_obs": counts.get(key, 0),
                     "area_px": float(rin.areas_px[key]) if key in rin.areas_px else np.nan})
    frame = pd.DataFrame(rows, columns=["position_key", "position_id", "label", "occupied",
                                        "donor_block_id", "tma_unique_sample_id", "annotation",
                                        "core_height", "spot_slide", "selected", "reason",
                                        "n_obs", "area_px"])
    um2, mm2 = _areas(rin, frame["area_px"].to_numpy())
    frame["area_um2"], frame["area_mm2"] = um2, mm2
    return frame


def uns_regions(rin) -> Dict:
    snap = {k: v for k, v in rin.snapshot.items() if k != "display_cores"}
    return {"format_version": FORMAT_VERSION, "identity": rin.snapshot["identity"],
            "mode": rin.snapshot.get("mode", ""),
            "save_content_sha256": (rin.snapshot.get("regions") or {}).get("content_sha256", ""),
            "map_sha256": (rin.snapshot.get("map_sheet") or {}).get("sha256", "") or "",
            "selection_mode": rin.snapshot.get("selection_mode", RA.SELECT_TICKED),
            "source": str(rin.record.get("source", "")),
            "measurement_exclusion_bits": list(rin.uids),
            "pixel_size_um": list(rin.pixel_um) if rin.pixel_um else [],
            "snapshot_json": json.dumps(snap, sort_keys=True)}


def regions_dropped(rin) -> Dict[str, int]:
    out = {k: int(v) for k, v in (rin.record.get("dropped") or {}).items()}
    out["nuclei"] = int(rin.record.get("nuclei_dropped", 0))
    return out


def provenance_summary(rin, qc_stats) -> Dict:
    u = uns_regions(rin)
    return {"format_version": FORMAT_VERSION, "identity": u["identity"], "mode": u["mode"],
            "save_content_sha256": u["save_content_sha256"], "map_sha256": u["map_sha256"],
            "selection_mode": u["selection_mode"], "source": u["source"],
            "snapshot": rin.snapshot_path, "facts": rin.facts_path,
            "position_keys": [c["position_key"] for c in rin.snapshot.get("cores") or []],
            "measurement_exclusions": [
                {"bit": k, "uid": uid,
                 "channels": [nm for nm, on in zip(rin.channel_names, rin.channel_masks[k]) if on]}
                for k, uid in enumerate(rin.uids)],
            "measurement_rule": QC_RULE, "measurement_qc": dict(qc_stats),
            "dropped_by_step2": regions_dropped(rin),
            "pixel_size_um": u["pixel_size_um"]}
