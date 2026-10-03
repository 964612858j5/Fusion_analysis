"""Object layer v1 (block A4; plan v2.3 §6.3-§6.5, the A3+A4 application v2
§3.7): ``cells.parquet`` and ``regions.parquet``.

Authority
  * LabelStore: which pixels belong to which cell; nucleus -> cell.
  * ``cells.parquet``: the cells' identity key, centroid, bbox, area, nucleus
    count / area -- generated deterministically from the LabelStore; when the
    two disagree the Parquet is invalid and rebuilt. Step4's h5ad keeps its
    own copies of centroid / bbox / area (never rewritten); they agree with
    this table bitwise.
  * the Step4 h5ad: expression and compartment statistics (never copied here).
  * ``regions.parquet``: a rebuildable table of the regions Step0 committed
    (``roi_config.json`` / ``roi_manifest.json`` of every workspace).

Layout: ``<project>/objects/<segmentation_run_id>/cells.parquet`` and the
project-level ``<project>/objects/regions.parquet``. No ``qc_valid`` / no
``qc_status`` (QC belongs to QualityMask). Nothing here writes analysis
state, the h5ad, the labels or any run file; this is the only module that
imports pyarrow.

cells.parquet v1 (one row per cell label with at least one pixel)::

    segmentation_run_id string, region_id string, cell_id uint32, slide_id string,
    x_global float64, y_global float64,        # centroid, global_pixel, index = centre
    bbox_x0 int32, bbox_y0 int32, bbox_x1 int32, bbox_y1 int32,   # half-open
    cell_area int64, nucleus_count int32, nucleus_area int64      # null: no nucleus layer

The centroid is Step4's own formula (`QuantEngine.morphology`):
``float64(sum_local / n) + origin`` with exact integer sums.
"""

import hashlib
import json
import os
import uuid
from typing import Dict, List, Optional

import numpy as np

from . import provenance as prov
from .project_identity import ProjectSchemaError, describe_slide, read_project_schema

CELLS_SCHEMA = "block01.cells"
REGIONS_SCHEMA = "block01.regions"
SCHEMA_VERSION = 1

CELL_COLUMNS = [("segmentation_run_id", "string"), ("region_id", "string"),
                ("cell_id", "uint32"), ("slide_id", "string"),
                ("x_global", "float64"), ("y_global", "float64"),
                ("bbox_x0", "int32"), ("bbox_y0", "int32"),
                ("bbox_x1", "int32"), ("bbox_y1", "int32"),
                ("cell_area", "int64"), ("nucleus_count", "int32"), ("nucleus_area", "int64")]
REGION_COLUMNS = [("region_id", "string"), ("slide_id", "string"), ("type", "string"),
                  ("name", "string"), ("bbox_x0", "int32"), ("bbox_y0", "int32"),
                  ("bbox_x1", "int32"), ("bbox_y1", "int32"), ("parent_region_id", "string")]


class ObjectTableError(ValueError):
    """The tables cannot be built as asked; the message says why."""


def _schema(columns, name, extra=None):
    import pyarrow as pa
    meta = {"schema": name, "schema_version": str(SCHEMA_VERSION)}
    meta.update(extra or {})
    return pa.schema([pa.field(c, getattr(pa, t)()) for c, t in columns],
                     metadata={k: str(v) for k, v in meta.items()})


def _write_parquet(table, path):
    import pyarrow.parquet as pq
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = os.path.join(os.path.dirname(path), f".{os.path.basename(path)}.tmp.{uuid.uuid4().hex[:8]}")
    try:
        pq.write_table(table, tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# ── the label scan ──────────────────────────────────────────────────────

def label_geometry(labels, n_labels: int, band: Optional[int] = None) -> Dict[str, np.ndarray]:
    """Per label 0..n_labels of a 2-D label array (region-local): pixel
    count, exact integer sums of y and x, and the half-open bbox
    ``[y0, y1) x [x0, x1)``. Read in row bands; nothing else is held."""
    from scipy import ndimage
    h, w = labels.shape
    band = int(band or (labels.chunks[0] if hasattr(labels, "chunks") else 1024))
    size = n_labels + 1
    cnt = np.zeros(size, np.int64)
    sy = np.zeros(size, np.int64)
    sx = np.zeros(size, np.int64)
    y0 = np.full(size, np.iinfo(np.int64).max, np.int64)
    x0 = np.full(size, np.iinfo(np.int64).max, np.int64)
    y1 = np.full(size, -1, np.int64)
    x1 = np.full(size, -1, np.int64)
    cols = np.arange(w, dtype=np.int64)
    for r0 in range(0, h, band):
        block = np.asarray(labels[r0:min(r0 + band, h), :])
        if block.size == 0:
            continue
        if int(block.max()) > n_labels:
            raise ObjectTableError(f"a label {int(block.max())} exceeds the store's "
                                   f"object count {n_labels}")
        flat = block.ravel()
        rows = np.repeat(np.arange(r0, r0 + block.shape[0], dtype=np.int64), w)
        cnt += np.bincount(flat, minlength=size)
        # integer sums: every partial sum is an integer far below 2**53, so the
        # float64 bincount is exact and the cast loses nothing
        sy += np.bincount(flat, weights=rows, minlength=size).astype(np.int64)
        sx += np.bincount(flat, weights=np.tile(cols, block.shape[0]),
                          minlength=size).astype(np.int64)
        for lab, sl in enumerate(ndimage.find_objects(block), start=1):
            if sl is None:
                continue
            ry, rx = sl
            y0[lab] = min(y0[lab], r0 + ry.start)
            y1[lab] = max(y1[lab], r0 + ry.stop)
            x0[lab] = min(x0[lab], rx.start)
            x1[lab] = max(x1[lab], rx.stop)
    return {"count": cnt, "sum_y": sy, "sum_x": sx, "y0": y0, "y1": y1, "x0": x0, "x1": x1}


# ── cells.parquet ───────────────────────────────────────────────────────

def _run_dir_of(project_dir, segmentation_run_id):
    hits = []
    rois = os.path.join(project_dir, "rois")
    for ws in sorted(os.listdir(rois)) if os.path.isdir(rois) else []:
        d = os.path.join(rois, ws, "runs", segmentation_run_id)      # block RM §4
        if os.path.isdir(d):
            hits.append(d)
    if len(hits) != 1:
        raise ObjectTableError(f"segmentation run {segmentation_run_id}: "
                               f"{'not found' if not hits else 'in several workspaces'} "
                               f"in {project_dir} -- the cell key would not be unique")
    return hits[0]


def build_cells(project_dir, segmentation_run_id) -> Dict:
    """Write ``objects/<run>/cells.parquet`` for every region of the run and
    register it. Returns ``{"path", "artifact_id", "rows"}``."""
    import pyarrow as pa
    from . import quant_sources as qs
    project_dir = os.path.abspath(project_dir)
    if read_project_schema(project_dir) != 1:
        raise ObjectTableError(f"{project_dir}: a legacy project (no project_schema_version); "
                               "open it once with the current program first")
    run_dir = _run_dir_of(project_dir, segmentation_run_id)
    _project, ws = prov.find_workspace(run_dir)
    seg_entries = [e for e in prov.load_entries(project_dir)
                   if e["kind"] == "segmentation_run" and e["token"] == segmentation_run_id]
    if any(prov.FLAG_RUN_ID_COLLISION in (e.get("flags") or []) for e in seg_entries):
        raise ObjectTableError(f"segmentation_run_id {segmentation_run_id} is not unique in "
                               "this project; no cell table")
    run = qs.open_run(run_dir)
    manifest = json.load(open(os.path.join(ws, "roi_manifest.json"), encoding="utf-8"))
    pm = json.load(open(os.path.join(project_dir, "project_manifest.json"), encoding="utf-8"))
    sid, _desc = describe_slide(manifest["source_ome"], pm.get("sources"))
    regions = prov.workspace_regions(ws, sid)
    parts = []
    for roi_name, bbox in qs.run_regions(run):
        reg = regions.get(roi_name)
        if reg is None or reg["bbox_fullres"] != [int(v) for v in bbox]:
            raise ObjectTableError(f"region '{roi_name}' {list(bbox)} is not a region the "
                                   "workspace committed in Step0; no region_id is guessed")
        parts.append(_cells_of_region(run, roi_name, bbox, reg["region_id"], sid,
                                      segmentation_run_id))
    table = pa.concat_tables(parts) if len(parts) > 1 else parts[0]
    path = os.path.join(project_dir, "objects", segmentation_run_id, "cells.parquet")
    seg = prov.lookup(project_dir, "segmentation_run", prov.location(project_dir, run_dir),
                      segmentation_run_id)
    aid_hint = f"art_cells_parquet_{uuid.uuid4().hex[:12]}"
    table = table.replace_schema_metadata(dict(table.schema.metadata or {},
                                               artifact_id=aid_hint))
    _write_parquet(table, path)
    aid = prov.register(
        project_dir, "cells_parquet", prov.location(project_dir, path), _sha(path),
        artifact_id=aid_hint, depends_on=[seg] if seg else [],
        unresolved_inputs=[] if seg else [{"role": "segmentation_run", "path": run_dir}],
        operates_on=sorted({regions[r]["region_id"] for r, _b in qs.run_regions(run)}),
        workspace_id=os.path.basename(ws), slide_id=sid,
        parameters={"schema": CELLS_SCHEMA, "schema_version": SCHEMA_VERSION,
                    "segmentation_run_id": segmentation_run_id, "rows": table.num_rows},
        software={"git_commit": prov.git_commit()})
    return {"path": path, "artifact_id": aid, "rows": table.num_rows}


def _cells_of_region(run, roi_name, bbox, region_id, slide_id, run_id):
    import pyarrow as pa
    import zarr
    from . import quant_sources as qs
    store = qs._label_store(run, roi_name)
    entry, compartment = ((store.get(qs.CELL), qs.CELL) if store.get(qs.CELL)
                          else (store.get(qs.NUCLEUS), qs.NUCLEUS))
    path = entry["path"] if os.path.isabs(entry["path"]) else os.path.join(run.run_dir,
                                                                           entry["path"])
    labels = zarr.open(path, mode="r")
    n = int(entry["n_objects"])
    shape = (bbox[1] - bbox[0], bbox[3] - bbox[2])
    if tuple(labels.shape) != shape or str(labels.dtype) != "uint32":
        raise ObjectTableError(f"the label array is {labels.dtype} {tuple(labels.shape)}, "
                               f"the region is uint32 {shape}")
    g = label_geometry(labels, n)
    ids = np.nonzero(g["count"][1:])[0] + 1                       # cells with pixels
    oy, ox = int(bbox[0]), int(bbox[2])
    count = g["count"][ids]
    # Step4's formula: correctly rounded integer division, then the origin
    cx = np.array([sx / c for sx, c in zip(g["sum_x"][ids].tolist(), count.tolist())],
                  np.float64) + ox
    cy = np.array([sy / c for sy, c in zip(g["sum_y"][ids].tolist(), count.tolist())],
                  np.float64) + oy
    nuc_count = nuc_area = None
    npath, tpath, m = qs._nuclei_of(store, compartment, shape, run.run_dir)
    if npath is not None:
        table = np.asarray(zarr.open(tpath, mode="r")[...]).astype(np.int64)
        nuc_px = label_geometry(zarr.open(npath, mode="r"), m)["count"]
        nuc_count = np.bincount(table[1:], minlength=n + 1)[ids].astype(np.int32)
        nuc_area = np.bincount(table[1:], weights=nuc_px[1:], minlength=n + 1)[ids].astype(
            np.int64)
    k = ids.size
    cols = {
        "segmentation_run_id": pa.array([run_id] * k, pa.string()),
        "region_id": pa.array([region_id] * k, pa.string()),
        "cell_id": pa.array(ids.astype(np.uint32), pa.uint32()),
        "slide_id": pa.array([slide_id] * k, pa.string()),
        "x_global": pa.array(cx, pa.float64()),
        "y_global": pa.array(cy, pa.float64()),
        "bbox_x0": pa.array((g["x0"][ids] + ox).astype(np.int32), pa.int32()),
        "bbox_y0": pa.array((g["y0"][ids] + oy).astype(np.int32), pa.int32()),
        "bbox_x1": pa.array((g["x1"][ids] + ox).astype(np.int32), pa.int32()),
        "bbox_y1": pa.array((g["y1"][ids] + oy).astype(np.int32), pa.int32()),
        "cell_area": pa.array(count.astype(np.int64), pa.int64()),
        "nucleus_count": (pa.array(nuc_count, pa.int32()) if nuc_count is not None
                          else pa.nulls(k, pa.int32())),
        "nucleus_area": (pa.array(nuc_area, pa.int64()) if nuc_area is not None
                         else pa.nulls(k, pa.int64())),
    }
    schema = _schema(CELL_COLUMNS, CELLS_SCHEMA)
    return pa.Table.from_arrays([cols[c] for c, _t in CELL_COLUMNS], schema=schema)


# ── regions.parquet ─────────────────────────────────────────────────────

def build_regions(project_dir) -> Dict:
    """Write the project-level ``objects/regions.parquet`` from every
    workspace's committed regions and register it."""
    import pyarrow as pa
    from .project_identity import bbox_columns, region_type
    project_dir = os.path.abspath(project_dir)
    if read_project_schema(project_dir) != 1:
        raise ObjectTableError(f"{project_dir}: a legacy project (no project_schema_version)")
    pm = json.load(open(os.path.join(project_dir, "project_manifest.json"), encoding="utf-8"))
    rows: Dict[str, Dict] = {}
    workspaces, files, slides = [], {}, set()
    rois_dir = os.path.join(project_dir, "rois")
    for ws_id in sorted(os.listdir(rois_dir)) if os.path.isdir(rois_dir) else []:
        ws = os.path.join(rois_dir, ws_id)
        mpath = os.path.join(ws, "roi_manifest.json")
        if not os.path.isfile(mpath):
            continue
        manifest = json.load(open(mpath, encoding="utf-8"))
        slide = manifest.get("source_ome")
        if not slide or not os.path.exists(slide):
            continue
        sid, _ = describe_slide(slide, pm.get("sources"))
        slides.add(sid)
        cpath = os.path.join(ws, "settings", "step0", "roi_config.json")   # block RM §4
        for p in (cpath, mpath):
            if os.path.isfile(p):
                files[prov.location(project_dir, p)["path"]] = _sha(p)
        created = manifest.get("created_at", "")
        for name, reg in prov.workspace_regions(ws, sid).items():
            roi = reg["roi"]
            rid = reg["region_id"]
            row = {"region_id": rid, "slide_id": sid, "type": region_type(roi),
                   "name": str(roi.get("display_name") or name), "parent_region_id": None,
                   "_created": created}
            row.update(bbox_columns(reg["bbox_fullres"]))
            if rid not in rows or created < rows[rid]["_created"]:
                rows[rid] = row
        workspaces.append(ws_id)
    ordered = [rows[k] for k in sorted(rows)]
    schema = _schema(REGION_COLUMNS, REGIONS_SCHEMA)
    table = pa.Table.from_pylist([{c: r[c] for c, _t in REGION_COLUMNS} for r in ordered],
                                 schema=schema)
    path = os.path.join(project_dir, "objects", "regions.parquet")
    _write_parquet(table, path)
    for sid in slides:
        if not os.path.exists(os.path.join(prov.provenance_dir(project_dir), f"{sid}.json")):
            raise ObjectTableError(f"slide {sid} has no raw_slide entry; open the project "
                                   "with the current program first")
    aid = prov.register(
        project_dir, "regions_parquet", prov.location(project_dir, path), _sha(path),
        depends_on=sorted(slides), operates_on=sorted(rows),
        parameters={"schema": REGIONS_SCHEMA, "schema_version": SCHEMA_VERSION,
                    "workspaces": workspaces, "sources": files, "rows": table.num_rows},
        software={"git_commit": prov.git_commit()})
    return {"path": path, "artifact_id": aid, "rows": table.num_rows}


def read_table(path):
    """A table written here, its schema checked (name, version, columns)."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    table = pq.read_table(path)
    meta = {k.decode(): v.decode() for k, v in (table.schema.metadata or {}).items()}
    expected = {CELLS_SCHEMA: CELL_COLUMNS, REGIONS_SCHEMA: REGION_COLUMNS}.get(meta.get("schema"))
    if expected is None or meta.get("schema_version") != str(SCHEMA_VERSION):
        raise ObjectTableError(f"{path}: schema {meta.get('schema')!r} version "
                               f"{meta.get('schema_version')!r} is not supported")
    got = [(f.name, str(f.type)) for f in table.schema]
    want = [(c, str(getattr(pa, t)())) for c, t in expected]
    if got != want:
        raise ObjectTableError(f"{path}: columns {got} are not {want}")
    return table


__all__ = ["build_cells", "build_regions", "read_table", "label_geometry", "ObjectTableError",
           "CELL_COLUMNS", "REGION_COLUMNS", "ProjectSchemaError"]
