"""Block A4: the object layer v1 (`core/object_tables.py`).

Gates A4-G1 ... A4-G8 of the A3+A4 application v2 §7.3 on a synthetic
project; the test1 result is checked by the acceptance run (execution
record).
"""

import ast
import hashlib
import json
import os
import types

import numpy as np
import pytest

pytest.importorskip("tifffile")
zarr = pytest.importorskip("zarr")
pa = pytest.importorskip("pyarrow")
import pyarrow.parquet as pq  # noqa: E402

from block01.core import object_tables as ot  # noqa: E402
from block01.core import project_identity as pid  # noqa: E402
from block01.core import provenance as prov  # noqa: E402
from block01.core import step3_masks as sm  # noqa: E402
from block01.utils import roi_project as rp  # noqa: E402
from test_quant_sources import ROI_BBOX, build_project  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EMPTY = 7                                    # a label id with no pixel


def _sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def _adopt(p, rois=None):
    """`build_project`'s workspace, committed by Step0 and opened by new code."""
    project = os.path.dirname(os.path.dirname(p["ws"]))
    rois = rois or [("Full WSI", ROI_BBOX)]
    json.dump([{"name": n, "display_name": n, "bbox_fullres": list(b), "polygon_fullres": None,
                "type": "roi"} for n, b in rois],
              open(os.path.join(p["step0"], "roi_config.json"), "w"))
    rp.ensure_project_manifest(project, p["slide"])
    return project


@pytest.fixture
def proj(tmp_path):
    p = build_project(tmp_path, nuclei=True)
    lab_path = os.path.join(p["run_dir"], "global_mask_Full WSI.zarr")
    z = zarr.open(lab_path, mode="r+")
    lab = z[...]
    lab[lab == EMPTY] = 0                    # an empty label, as Step2 can leave
    z[...] = lab
    p["labels"]["Full WSI"] = lab
    p["project"] = _adopt(p)
    return p


def _cells(p):
    out = ot.build_cells(p["project"], os.path.basename(p["run_dir"]))
    return out, ot.read_table(out["path"]).to_pandas()


# ── A4-G1: keys agree with the LabelStore ───────────────────────────────

def test_one_row_per_label_with_pixels(proj):
    out, df = _cells(proj)
    lab = proj["labels"]["Full WSI"]
    present = sorted(int(v) for v in np.unique(lab) if v)
    assert df["cell_id"].tolist() == present and EMPTY not in present
    assert out["rows"] == len(present)
    assert not df.duplicated(["segmentation_run_id", "region_id", "cell_id"]).any()
    sid = pid.describe_slide(proj["slide"])[0]
    assert set(df["slide_id"]) == {sid}
    assert set(df["region_id"]) == {pid.region_id(sid, "roi", ROI_BBOX)}
    assert set(df["segmentation_run_id"]) == {os.path.basename(proj["run_dir"])}


# ── A4-G2: coordinates agree with Step4 and with the raster ─────────────

def test_centroid_bbox_area_equal_step4_bitwise(proj, tmp_path):
    import anndata
    from block01.workers.feature_extract_worker import run_extraction
    _, df = _cells(proj)
    res = run_extraction(proj["run_dir"], str(tmp_path / "q"))
    obs = anndata.read_h5ad(res["h5ad"]).obs
    obs = obs.set_index(obs["cell_id"].astype(np.int64)).loc[df["cell_id"].astype(np.int64)]
    assert np.array_equal(df["x_global"].to_numpy(), obs["centroid_x"].to_numpy())
    assert np.array_equal(df["y_global"].to_numpy(), obs["centroid_y"].to_numpy())
    for mine, theirs in (("bbox_x0", "bbox_min_x"), ("bbox_y0", "bbox_min_y"),
                         ("bbox_x1", "bbox_max_x"), ("bbox_y1", "bbox_max_y"),
                         ("cell_area", "area")):
        assert np.array_equal(df[mine].to_numpy().astype(np.float64),
                              obs[theirs].to_numpy().astype(np.float64)), mine


def test_bbox_is_half_open_in_global_pixels(proj):
    _, df = _cells(proj)
    lab = proj["labels"]["Full WSI"]
    oy, ox = ROI_BBOX[0], ROI_BBOX[2]
    for row in df.itertuples():
        ys, xs = np.nonzero(lab == row.cell_id)
        assert (row.bbox_y0, row.bbox_y1) == (ys.min() + oy, ys.max() + 1 + oy)
        assert (row.bbox_x0, row.bbox_x1) == (xs.min() + ox, xs.max() + 1 + ox)
        assert row.cell_area == ys.size


# ── A4-G3: coordinates agree with the viewer ────────────────────────────

def test_the_viewer_picks_the_same_cell_at_its_world_point(proj):
    """Step3's own label tile, picked at world = global + 0.5. If this went
    red the §3.2 adapter rule would be wrong -- fix the contract text (with
    the user), never the viewer."""
    _, df = _cells(proj)
    lab = proj["labels"]["Full WSI"]
    oy, ox = ROI_BBOX[0], ROI_BBOX[2]
    source = types.SimpleNamespace(mask_path=os.path.join(proj["run_dir"],
                                                          "global_mask_Full WSI.zarr"),
                                   bbox=tuple(ROI_BBOX), pyramid=None)
    shapes = [(150, 170)]
    frames = pid.SlideFrames(shapes)
    T = 32
    for row in df.itertuples():
        ly = row.bbox_y0 - oy
        lx = int(np.nonzero(lab[ly] == row.cell_id)[0].min())     # its pixel on that row
        wx, wy = frames.global_to_viewer_world(lx + ox, row.bbox_y0)
        tile = sm.read_label_tile(source, 0, int(wx // T), int(wy // T), T, shapes)
        x0, _x1, y0, _y1 = tile.world_rect
        gx, gy = frames.viewer_world_to_global(wx - x0, wy - y0)
        assert tile.labels[int(gy), int(gx)] == row.cell_id


# ── A4-G4: nuclei ───────────────────────────────────────────────────────

def test_nucleus_count_and_area_match_a_per_nucleus_count(proj):
    _, df = _cells(proj)
    nuc, table = proj["nuclei"]["Full WSI"]
    counts, areas = {}, {}
    for n in range(1, table.size):
        c = int(table[n])
        if c:
            counts[c] = counts.get(c, 0) + 1
            areas[c] = areas.get(c, 0) + int((nuc == n).sum())
    for row in df.itertuples():
        assert row.nucleus_count == counts.get(row.cell_id, 0)
        assert row.nucleus_area == areas.get(row.cell_id, 0)


def test_a_run_without_nuclei_has_null_nucleus_columns(tmp_path):
    p = build_project(tmp_path, nuclei=False)
    p["project"] = _adopt(p)
    _, df = _cells(p)
    assert df["nucleus_count"].isna().all() and df["nucleus_area"].isna().all()


# ── A4-G5: schema ───────────────────────────────────────────────────────

def test_the_cells_schema_is_exactly_v1(proj):
    out, _ = _cells(proj)
    t = pq.read_table(out["path"])
    assert [(f.name, str(f.type)) for f in t.schema] == [
        ("segmentation_run_id", "string"), ("region_id", "string"), ("cell_id", "uint32"),
        ("slide_id", "string"), ("x_global", "double"), ("y_global", "double"),
        ("bbox_x0", "int32"), ("bbox_y0", "int32"), ("bbox_x1", "int32"), ("bbox_y1", "int32"),
        ("cell_area", "int64"), ("nucleus_count", "int32"), ("nucleus_area", "int64")]
    meta = {k.decode(): v.decode() for k, v in t.schema.metadata.items()}
    assert meta["schema"] == "block01.cells" and meta["schema_version"] == "1"
    assert meta["artifact_id"] == out["artifact_id"]
    assert "qc_valid" not in t.schema.names


def test_a_table_with_another_schema_is_refused(proj, tmp_path):
    out, _ = _cells(proj)
    t = pq.read_table(out["path"])
    bad = t.append_column("qc_valid", pa.array([True] * t.num_rows))
    path = str(tmp_path / "bad.parquet")
    pq.write_table(bad, path)
    with pytest.raises(ot.ObjectTableError):
        ot.read_table(path)


# ── A4-G6: regions ──────────────────────────────────────────────────────

def test_regions_dedup_across_workspaces_and_keep_every_roi(tmp_path):
    p = build_project(tmp_path, rois=[("A", [0, 60, 0, 70]), ("B", [70, 140, 80, 160])])
    project = _adopt(p, rois=[("A", [0, 60, 0, 70]), ("B", [70, 140, 80, 160])])
    # a second workspace over the same slide and the same region A
    ctx = rp.create_roi_context(project, {"name": "A", "bbox_fullres": [0, 60, 0, 70]},
                                p["slide"], display_name="A again")
    json.dump([{"name": "A again", "bbox_fullres": [0, 60, 0, 70], "polygon_fullres": None,
                "type": "roi"}], open(os.path.join(ctx["step_dirs"]["step0"],
                                                   "roi_config.json"), "w"))
    out = ot.build_regions(project)
    df = ot.read_table(out["path"]).to_pandas()
    sid = pid.describe_slide(p["slide"])[0]
    assert len(df) == 2 and set(df["region_id"]) == {pid.region_id(sid, "roi", [0, 60, 0, 70]),
                                                      pid.region_id(sid, "roi",
                                                                    [70, 140, 80, 160])}
    b = df[df["name"] == "B"].iloc[0]
    assert (b.bbox_x0, b.bbox_y0, b.bbox_x1, b.bbox_y1) == (80, 70, 160, 140)
    assert df["parent_region_id"].isna().all() and "qc_status" not in df.columns
    [entry] = [e for e in prov.load_entries(project) if e["kind"] == "regions_parquet"]
    assert entry["depends_on"] == [sid]
    assert sorted(entry["parameters"]["workspaces"]) == sorted(os.listdir(os.path.join(project,
                                                                                      "rois")))


# ── A4-G7: authority and read-only ──────────────────────────────────────

def test_building_reads_only_and_registers_its_lineage(proj):
    run = proj["run_dir"]
    watched = [os.path.join(run, "segmentation_meta.json"),
               os.path.join(run, "global_mask_Full WSI.zarr", "0.0")]
    before = [_sha(p) for p in watched]
    out, _ = _cells(proj)
    assert [_sha(p) for p in watched] == before
    assert not os.path.exists(os.path.join(proj["project"], "analysis"))
    [e] = [x for x in prov.load_entries(proj["project"]) if x["kind"] == "cells_parquet"]
    assert e["artifact_id"] == out["artifact_id"]
    assert e["operates_on"] and all(r.startswith("reg_") for r in e["operates_on"])
    # build_project's run was made before A3: an unresolved input, not a guess
    assert e["depends_on"] == [] and e["unresolved_inputs"][0]["role"] == "segmentation_run"


def test_a_legacy_project_is_refused(tmp_path):
    p = build_project(tmp_path)
    project = os.path.dirname(os.path.dirname(p["ws"]))
    with pytest.raises(ot.ObjectTableError):
        ot.build_cells(project, os.path.basename(p["run_dir"]))


def test_a_region_the_workspace_did_not_commit_is_refused(tmp_path):
    p = build_project(tmp_path)
    p["project"] = _adopt(p, rois=[("Full WSI", [0, 100, 0, 100])])
    with pytest.raises(ot.ObjectTableError, match="no region_id is guessed"):
        ot.build_cells(p["project"], os.path.basename(p["run_dir"]))


def test_a_colliding_run_id_is_refused(proj):
    prov.register(proj["project"], "segmentation_run",
                  prov.location(proj["project"], proj["run_dir"]),
                  os.path.basename(proj["run_dir"]), flags=[prov.FLAG_RUN_ID_COLLISION])
    with pytest.raises(ot.ObjectTableError, match="not unique"):
        ot.build_cells(proj["project"], os.path.basename(proj["run_dir"]))


# ── A4-G8: pyarrow stays in one module ──────────────────────────────────

def test_only_object_tables_imports_pyarrow():
    users = []
    for base, _dirs, files in os.walk(ROOT):
        rel = os.path.relpath(base, ROOT)
        if rel.split(os.sep)[0] in ("tests", "docs", "envs", ".git") or "__pycache__" in rel:
            continue
        for f in files:
            if not f.endswith(".py"):
                continue
            tree = ast.parse(open(os.path.join(base, f), encoding="utf-8").read())
            for node in ast.walk(tree):
                names = ([a.name for a in node.names] if isinstance(node, ast.Import) else
                         [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                if any(n.split(".")[0] == "pyarrow" for n in names):
                    users.append(os.path.normpath(os.path.join(rel, f)))
    assert sorted(set(users)) == [os.path.join("core", "object_tables.py")]


def test_a_registered_run_is_the_tables_dependency(proj):
    seg = prov.register(proj["project"], "segmentation_run",
                        prov.location(proj["project"], proj["run_dir"]),
                        os.path.basename(proj["run_dir"]))
    _cells(proj)
    [e] = [x for x in prov.load_entries(proj["project"]) if x["kind"] == "cells_parquet"]
    assert e["depends_on"] == [seg] and e["unresolved_inputs"] == []
