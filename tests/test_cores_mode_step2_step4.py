"""Cores found without a map sheet through Step1, Step2 and Step4
(docs/v17_cores_without_map_downstream_plan.md §3.4-3.6, §5.4-6).

* Step1: random patches stay in the ticked cores, and are refused -- not
  sent back to the whole tissue -- when the cores cannot run;
* Step2, both routes: Step2's own freeze (a map-less Save without
  exclusions) and a Step1 hand-over's frozen snapshot; the run keeps only
  the cells in the cores and records the cores' uids, so does provenance;
* Step4: per-cell core columns by uid, a row for every saved core, the
  provenance's uids; a TMA run's output unchanged.
"""

import json
import os
import types

import numpy as np
import pytest

from test_regions_r1 import SHAPE, SID, project  # noqa: F401
from test_f5_manual_regions import _window

from block01.core.regions import analysis as A
from block01.core.regions import document as D
from block01.core.regions import geometry as G
from block01.core.regions import lock as K


def _map_less_save(project, n=3, ticked=None):
    d = D.new_document(SID, SHAPE)
    for cy, cx in [(200, 200), (200, 700), (800, 200), (800, 700)][:n]:
        D.add_drawn_core(d, G.circle_polygon(cy, cx, 120, SHAPE))
    if ticked is not None:
        D.set_core_runs(d, [u for i, u in enumerate(sorted(d["cores"])) if i not in ticked], False)
    K.save(project, d, None)
    return d


# ── Step1: random patches ───────────────────────────────────────────────

def _random(project, monkeypatch):
    from block01.ui.main_window import MainWindow
    w = types.SimpleNamespace(_regions_files_present=lambda: True,
                              _regions_context=lambda: {"project_dir": project, "slide_id": SID})
    from block01.core.tma import study as st
    monkeypatch.setattr(st, "load_study", lambda pd: None)
    return lambda roi=None: MainWindow._random_patch_cores(w, roi)


def test_random_patches_stay_in_the_ticked_cores_or_are_refused(project, monkeypatch):
    rnd = _random(project, monkeypatch)
    d = _map_less_save(project, 3, ticked={0, 2})
    cores, excl, why = rnd()
    assert why == "" and len(cores) == 2 and excl == []
    D.set_core_runs(d, list(d["cores"]), False)                             # Run none
    K.save(project, d, None)
    cores, excl, why = rnd()
    assert cores is None and "Random patches go inside the selected cores" in why
    D.set_core_runs(d, list(d["cores"]), True)
    K.save(project, d, None)
    cores, _e, why = rnd({"bbox_fullres": [1300, 1400, 1000, 1100]})        # no core in this ROI
    assert cores is None and "inside the selected cores" in why
    near = lambda cy, cx: min(d["cores"], key=lambda u: sum(  # noqa: E731
        (p - q) ** 2 for p, q in zip(D.core_centre(d, u), (cy, cx))))
    D.translate_shape(d, d["cores"][near(200, 700)]["pieces"][0], 0, -450)   # onto the (200, 200) core
    K.save(project, d, None)
    cores, _e, why = rnd()
    assert cores is None and "overlap" in why
    K.save(project, D.new_document(SID, SHAPE), None)                       # no cores: the whole tissue
    assert rnd() == (None, None, "")


# ── Step2: the manual route ─────────────────────────────────────────────

def test_step2_freezes_the_cores_of_a_map_less_save(project, monkeypatch, tmp_path):
    d = _map_less_save(project, 3)
    freeze, _f = _window(project, monkeypatch, tmp_path, None)
    asked = []
    req, why = freeze(ask=lambda t: asked.append(t) or True)
    assert why == "" and not asked
    snap = req["snapshot"]
    assert snap["mode"] == "cores" and len(snap["cores"]) == 3 and snap["exclusions"] == []
    assert req["identity"] == snap["identity"] and req["selection_mode"] == "ticked"
    D.set_core_runs(d, list(d["cores"]), False)
    K.save(project, d, None)
    assert freeze(ask=lambda t: asked.append(t) or False) == (None, None)
    assert "No core is selected" in asked[-1] and "Cores without an outline" in asked[-1]
    req, why = freeze(ask=lambda t: True)
    assert req["selection_mode"] == "all_eligible" and len(req["snapshot"]["cores"]) == 3


def _cores_snapshot(h, w, core_box=(0, 50, 60, 113)):
    y0, y1, x0, x1 = core_box
    poly = [[x0, y0], [x1 - 1, y0], [x1 - 1, y1 - 1], [x0, y1 - 1]]
    snap = {"snapshot_version": 1, "mode": "cores", "slide_id": "slide_0000000000000000",
            "slide_shape": [h, w], "parent": {"region_id": None, "bbox_fullres": [0, h, 0, w],
                                              "polygon_fullres": None},
            "cores": [{"uid": "core_aaaa", "position_key": None, "polygon": poly, "donor_block_id": "",
                       "tma_unique_sample_id": "", "annotation": "", "qc": "unreviewed",
                       "centre_yx": [(y0 + y1) / 2, (x0 + x1) / 2]}],
            "exclusions": [], "map_sheet": None, "seg_channels": [], "policies": {}}
    snap["identity"] = A.identity(snap)
    return snap


@pytest.mark.parametrize("route", ["step2", "contract"])
def test_a_run_keeps_only_the_cells_in_the_cores_by_either_route(tmp_path, monkeypatch, route):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("block01.qt")
    from test_f4_step2 import _cells
    from test_step2_ownership_move import _labels
    from test_step2_skip_empty_tiles import _run, _worker
    from block01.workers.segment_merge_worker import SegmentMergeWorker as W
    lab = _labels(3)
    h, w = lab.shape
    worker, zp = _worker(tmp_path, monkeypatch, lab, 3, 4, 8, name=route)
    snap = _cores_snapshot(h, w)
    kind = "fusion_fusion_nucleus"
    if route == "step2":
        worker._regions_request = {"snapshot": snap, "identity": snap["identity"], "input_kind": kind,
                                   "selection_mode": "ticked", "fusion_settings_hash": "fh"}
    else:
        worker._contract = {"method": worker.method, "regions": {"snapshot": snap, "identity": snap["identity"],
                                                                 "input_kind": kind, "path": "x"}}
    monkeypatch.setattr(W, "_workspace_slide_id", lambda self: snap["slide_id"])
    worker._regions = W._regions_begin(worker)                     # the identity check passes
    assert worker._regions["raster"].tma and worker._regions["source"] == route
    if route == "contract":
        # the hand-over's snapshot passes the same gate; the cells rule is the
        # raster's, run below through Step2's own route
        assert W._regions_run_meta(worker)["core_uids"] == ["core_aaaa"]
        return
    n = _run(worker, zp, "segment_one_zarr")
    cells = _cells(worker, "segment_one_zarr")
    core = np.zeros_like(cells, bool)
    core[0:50, 60:113] = True
    ids = np.unique(cells[cells > 0])
    assert n == ids.size and ids.size and all(core[cells == i].mean() > 0.5 for i in ids)
    meta = worker._regions_run_meta()
    assert meta["core_uids"] == ["core_aaaa"] and meta["position_keys"] == [None]


def _prov(monkeypatch, tmp_path):
    from block01.core import provenance as prov
    seen = {}
    monkeypatch.setattr(prov, "find_workspace", lambda d: (str(tmp_path), str(tmp_path / "ws")))
    monkeypatch.setattr(prov, "_load", lambda p: {"source_ome": "x"})
    monkeypatch.setattr(prov, "ensure_raw_slide", lambda pd, s: "slide_1")
    monkeypatch.setattr(prov, "workspace_regions", lambda ws, sid: {})
    monkeypatch.setattr(prov, "load_entries", lambda pd: [])
    monkeypatch.setattr(prov, "lookup", lambda *a: None)
    monkeypatch.setattr(prov, "safe_register", lambda *a, **k: seen.update(k) or "id")
    return prov, seen


def test_provenance_records_the_cores_uids_on_every_path(tmp_path, monkeypatch):
    prov, seen = _prov(monkeypatch, tmp_path)
    snap = {"mode": "cores", "regions": {"content_sha256": "C"}, "cores": [{"uid": "core_a", "position_key": None}]}
    meta = {"run_id": "segment_1", "rois": [{"roi_name": "", "bbox_fullres": None}],
            "seg_config": {"preseg_contract": {"method": "m", "regions": {"identity": "I", "input_kind": "k",
                                                                          "snapshot": snap}}}}
    prov.register_segmentation_run(str(tmp_path / "run"), meta)            # the hand-over route
    assert seen["parameters"]["regions"]["core_uids"] == ["core_a"]
    meta = {"method": "m", "seg_config": {}, "rois": [],
            "regions_run": {"source": "step2", "selection_mode": "ticked", "identity": "I", "input_kind": "k",
                            "content_sha256": "C", "position_keys": [None], "core_uids": ["core_a"],
                            "fusion_settings_hash": "fh", "snapshot": "/x/regions_snapshot.json"}}
    prov.register_segmentation_run(str(tmp_path / "run2"), meta)            # Step2's own freeze
    assert seen["parameters"]["regions"]["core_uids"] == ["core_a"]
    meta["regions_run"].pop("core_uids")                                    # a TMA run: no such key
    prov.register_segmentation_run(str(tmp_path / "run3"), meta)
    assert "core_uids" not in seen["parameters"]["regions"]


# ── Step4 ───────────────────────────────────────────────────────────────

def _step4_snapshot():
    """Two run cores (left / right half of the ROI), one unticked core, one
    degenerate core; the inventory lists all four."""
    import test_f4_step4 as T
    left, right, other = T._rect(T.Y0, T.Y1, T.X0, T.XM), T._rect(T.Y0, T.Y1, T.XM, T.X1), T._rect(0, 8, 0, 8)
    cores, display, inventory = [], [], []
    for i, poly in enumerate((left, right)):
        g = G.region_outline([poly], T.SLIDE_SHAPE)
        cy = (T.Y0 + T.Y1) / 2
        cx = (T.X0 + T.XM) / 2 if i == 0 else (T.XM + T.X1) / 2
        cores.append({"uid": f"core_{i}", "position_key": None, "position_id": None,
                      "polygon": g["polygon_fullres"], "bbox": g["bbox_fullres"], "region_id": f"reg{i}",
                      "label": "", "donor_block_id": "", "tma_unique_sample_id": "", "annotation": "",
                      "core_height": "", "qc": "unreviewed", "centre_yx": [cy, cx]})
        display.append({"uid": f"core_{i}", "position_key": None, "polygons": [poly], "name": "",
                        "run": True, "reason": ""})
        inventory.append({"uid": f"core_{i}", "centre_yx": [cy, cx], "area_px": 0.0, "run": True, "reason": ""})
    display.append({"uid": "core_8", "position_key": None, "polygons": [other], "name": "", "run": False,
                    "reason": "not_ticked"})
    inventory.append({"uid": "core_8", "centre_yx": [4.0, 4.0], "area_px": 64.0, "run": False,
                      "reason": "not_ticked"})
    inventory.append({"uid": "core_9", "centre_yx": [10.0, 10.0], "area_px": 0.0, "run": True,
                      "reason": "degenerate"})
    snap = {"snapshot_version": A.SNAPSHOT_VERSION, "mode": "cores", "slide_id": "slide_00aa",
            "slide_shape": list(T.SLIDE_SHAPE),
            "regions": {"save_count": 1, "content_sha256": "save-sha", "saved_at": "t"},
            "parent": {"region_id": None, "bbox_fullres": list(T.ROI_BBOX), "polygon_fullres": None},
            "map_sheet": None, "cores": cores, "omitted": [], "positions": [], "exclusions": [],
            "display_cores": display, "core_inventory": inventory, "selection_mode": "ticked",
            "seg_channels": ["DAPI"], "policies": dict(A.POLICIES)}
    snap["identity"] = A.identity(snap)
    return snap


def test_step4_writes_the_cores_by_uid_and_a_row_for_every_saved_core(tmp_path):
    import test_f4_step4 as T
    from block01.workers import feature_extract_worker as few
    p = T._proj(tmp_path)
    lab = p["labels"]["Full WSI"]
    snap = _step4_snapshot()
    T._add_regions(p, snap, T._facts(lab, 40))                    # core index by the half the cell is in
    res = few.run_extraction(p["run_dir"], str(tmp_path / "o"), write_csv=True)
    a = T._read(res["h5ad"])
    o = a.obs
    for c in ("core_position_key", "donor_block_id", "core_label", "core_qc", "tma_block_name"):
        assert c not in o.columns, c                              # no map-sheet columns
    assert str(o["core_uid"].dtype) == "category" and o["cell_core_fraction"].dtype == np.float32
    assert o["core_centre_y"].dtype == np.float64 and o["core_area_px"].dtype == np.int64
    for c in ("cell_touches_core_edge", "analysis_include", "touches_region_edge"):
        assert o[c].dtype == bool, c
    for i, cid in enumerate(o["cell_id"].to_numpy()):
        ys, xs = np.nonzero(lab == cid)
        assert o["core_uid"].iloc[i] == ("core_0" if xs.mean() + T.X0 < T.XM else "core_1")
    t = a.uns["cores"]
    assert list(t["core_uid"]) == ["core_0", "core_1", "core_8", "core_9"]
    assert list(t["selected"]) == [True, True, False, False]
    assert list(t["reason"]) == ["", "", "not_ticked", "degenerate"]
    n_left = int((o["core_uid"] == "core_0").sum())
    assert list(t["n_obs"]) == [n_left, len(o) - n_left, 0, 0]               # zero-cell cores keep their row
    want_left = int(G.fill(snap["cores"][0]["polygon"], snap["cores"][0]["bbox"]).sum())
    assert t["area_px"][0] == want_left and t["area_px"][2] == 64 and t["area_px"][3] == 0
    assert "tma_cores" not in a.uns
    prov = json.loads(a.uns["provenance_json"])["regions"]
    assert prov["core_uids"] == ["core_0", "core_1"] and prov["mode"] == "cores"
    csvs = [v for k, v in res.items() if "csv" in k and v]
    paths = [x for v in csvs for x in (v if isinstance(v, (list, tuple)) else [v])]
    assert paths and any("core_uid" in open(x).readline() for x in paths if str(x).endswith(".csv"))


def test_a_tma_run_is_unchanged(tmp_path):
    """The TMA obs columns, their order and the provenance keys as before."""
    import test_f4_step4 as T
    from block01.workers import feature_extract_worker as few
    p = T._proj(tmp_path)
    lab = p["labels"]["Full WSI"]
    snap = T._snapshot()
    T._add_regions(p, snap, T._facts(lab, 40))
    res = few.run_extraction(p["run_dir"], str(tmp_path / "o"), write_csv=True)
    a = T._read(res["h5ad"])
    cols = list(a.obs.columns)
    assert cols[:5] == ["cell_id", "slide_id", "segmentation_run_id", "core_position_key", "core_region_id"]
    assert "core_uid" not in cols and "core_centre_y" not in cols      # nothing of the map-less columns
    prov = json.loads(a.uns["provenance_json"])["regions"]
    assert "core_uids" not in prov and prov["position_keys"] == ["k0", "k1"]
    assert "cores" not in a.uns and "tma_cores" in a.uns
