"""F4-1 (docs/f4_regions_downstream_plan.md v2 §C): the analysis snapshot,
its rasters and the cell rules -- the core layer Step1 / Step2 / Step4 will
use. Synthetic documents (the R1 test layout) and synthetic label images.
"""

import os

import numpy as np
import pytest

from test_regions_r1 import COLS, ROWS, SHAPE, SID, _finished, _key, _label, _place, _study, project  # noqa: F401

from block01.core.regions import analysis as A
from block01.core.regions import document as D
from block01.core.regions import geometry as G
from block01.core.regions import lock as K
from block01.core.regions import objects as O
from block01.core.regions import raster as R

REAL = "/sdb1/Fusion/TMA_cores"


def _ticked(placement="identity"):
    s, d = _finished(placement)
    for key in d["positions"]:
        D.set_run(d, key, True)
    return s, d


def _core_of(d, x, y):
    return next(u for u in d["cores"] if _label(d, u) == f"X{x}-Y{y:02d}")


# ── selection ───────────────────────────────────────────────────────────

def test_only_ticked_confirmed_included_cores_with_an_outline_run():
    s, d = _finished()
    sel, omitted = A.select_cores(d, s)
    assert sel == [] and {o["reason"] for o in omitted} == {"not_ticked"}      # zero ticks: nothing
    for key in d["positions"]:
        D.set_run(d, key, True)
    D.set_qc(d, _key(1, 1), "excluded", ["torn"])
    d["cores"][_core_of(d, 1, 2)]["mapping"] = "review"                        # after a merge
    D.unbind(d, _core_of(d, 2, 1))
    D.set_qc(d, _key(2, 2), "accepted")
    sel, omitted = A.select_cores(d, s)
    why = {o["label"]: o["reason"] for o in omitted}
    assert why["X1-Y01"] == "qc_excluded" and why["X1-Y02"] == "review" and why["X2-Y01"] == "not_bound"
    labels = [next(o["label"] for o in [{"label": _label(d, c["uid"])}]) for c in sel]
    assert "X2-Y02" in labels and "X1-Y03" in labels                           # unreviewed QC runs too
    assert all(c["region_id"].startswith("reg_") and len(c["polygon"]) >= 3 for c in sel)
    assert [c["position_id"] for c in sel] == sorted(c["position_id"] for c in sel)


def test_overlapping_selected_cores_are_found():
    s, d = _ticked()
    D.translate_shape(d, d["cores"][_core_of(d, 1, 2)]["pieces"][0], -120, 0)
    sel, _ = A.select_cores(d, s)
    assert A.overlapping(sel)


def test_an_exclusion_invalidates_segmentation_only_for_all_or_segmentation_channels():
    seg = ["DAPI", "PanCK"]
    assert A.seg_invalid("all", seg)
    assert A.seg_invalid(["DAPI", "CD8"], seg)
    assert not A.seg_invalid(["CD8"], seg)


# ── the snapshot ────────────────────────────────────────────────────────

def test_the_snapshot_freezes_the_save_and_its_identity_ignores_history(project):
    s, d = _ticked()
    cy, cx = _place(1, 1)
    fold = D.add_exclusion(d, G.circle_polygon(cy, cx, 30, SHAPE), ["fold"])
    cd8 = D.add_exclusion(d, G.circle_polygon(cy + 40, cx, 20, SHAPE), ["blur"], channels=["CD8"])
    K.save(project, d, s)
    snap = A.build_snapshot(project, SID, ["DAPI"], study=s)
    assert snap["mode"] == "tma" and len(snap["cores"]) == len(d["cores"])
    c = snap["cores"][0]
    assert c["label"] == "X1-Y01" and c["donor_block_id"] == "D11" and c["tma_unique_sample_id"] == "S11"
    ex = {e["uid"]: e for e in snap["exclusions"]}
    assert ex[fold]["seg_invalid"] and not ex[cd8]["seg_invalid"] and ex[cd8]["channels"] == ["CD8"]
    assert len(snap["positions"]) == ROWS * COLS and any(p["reason"] == "empty" for p in snap["positions"])
    path = A.save_snapshot(os.path.join(project, "run", "regions_snapshot.json"), snap)
    assert A.load_snapshot(path) == snap and A.is_current(project, snap, s)
    D.update_exclusion(d, fold, note="a remark")                               # history only
    K.save(project, d, s)
    assert A.is_current(project, snap, s)
    D.translate_shape(d, fold, 5, 0)                                           # geometry
    K.save(project, d, s)
    assert not A.is_current(project, snap, s)
    assert A.load_snapshot(path)["identity"] == snap["identity"]                # the frozen copy stays


def test_the_snapshot_refuses_nothing_selected_a_broken_save_and_overlaps(project):
    s, d = _finished()
    K.save(project, d, s)
    with pytest.raises(A.AnalysisError, match="no core is selected"):
        A.build_snapshot(project, SID, ["DAPI"], study=s)
    s, d = _ticked()
    K.save(project, d, s)
    with open(K.table_path(project), "ab") as f:
        f.write(b"x")
    with pytest.raises(A.AnalysisError, match="interrupted"):
        A.build_snapshot(project, SID, ["DAPI"], study=s)
    D.translate_shape(d, d["cores"][_core_of(d, 1, 2)]["pieces"][0], -120, 0)
    K.save(project, d, s)
    with pytest.raises(A.AnalysisError, match="overlap"):
        A.build_snapshot(project, SID, ["DAPI"], study=s)
    other = dict(s, map_sheet=dict(s["map_sheet"], sha256="cd" * 32))
    with pytest.raises(A.AnalysisError, match="map sheet"):
        A.build_snapshot(project, SID, ["DAPI"], study=other)


def test_a_project_without_a_map_sheet_gives_an_roi_snapshot(project):
    d = D.new_document(SID, SHAPE)
    D.add_exclusion(d, G.circle_polygon(400, 400, 50, SHAPE), ["blur"])
    K.save(project, d, None)
    snap = A.build_snapshot(project, SID, ["DAPI"])
    assert snap["mode"] == "roi" and snap["cores"] == [] and len(snap["exclusions"]) == 1
    assert snap["map_sheet"] is None


@pytest.mark.skipif(not os.path.isdir(REAL), reason="real map sheets not on this machine")
def test_the_project_name_is_read_and_older_studies_are_completed_from_the_same_file():
    from block01.core.tma import map_sheet as ms
    path = os.path.join(REAL, "TMA138.1.xlsx")
    m = ms.parse(path)
    assert m.project_name == "TMA138" and m.to_dict()["project_name"] == "TMA138"
    old = {k: v for k, v in m.to_dict().items() if k != "project_name"}      # a study written before
    assert A.map_metadata(old)["project_name"] == "TMA138"
    assert A.map_metadata(dict(old, sha256="00" * 32))["project_name"] == ""  # another file: not guessed
    assert A.map_metadata(m.to_dict())["block_name"] == "TMA138.1"


# ── rasters ─────────────────────────────────────────────────────────────

def _snap_from(s, d, seg=("DAPI",)):
    sel, _ = A.select_cores(d, s)
    excl = [{"uid": u, "type": "fold", "reasons": e["reasons"], "channels": e["channels"],
             "polygon": e["polygon"], "bbox": G.bbox_of(e["polygon"], SHAPE),
             "seg_invalid": A.seg_invalid(e["channels"], seg)} for u, e in d["exclusions"].items()]
    return {"mode": "tma", "slide_shape": list(SHAPE), "cores": sel, "exclusions": excl}


def test_tiles_of_any_size_see_the_same_boundary_pixels():
    s, d = _ticked()
    r = R.AnalysisRaster(_snap_from(s, d))
    whole = r.core_index((0, SHAPE[0], 0, SHAPE[1]))
    for step in (97, 256):
        tiled = np.zeros_like(whole)
        for y in range(0, SHAPE[0], step):
            for x in range(0, SHAPE[1], step):
                win = (y, min(y + step, SHAPE[0]), x, min(x + step, SHAPE[1]))
                tiled[win[0]:win[1], win[2]:win[3]] = r.core_index(win)
        assert np.array_equal(tiled, whole)
    poly = A.select_cores(d, s)[0][0]["polygon"]
    box = G.bbox_of(poly, SHAPE)
    assert np.array_equal(whole[box[0]:box[1], box[2]:box[3]] == 1, G.fill(poly, box))   # RoiMask's pixels
    assert not r.has_core((0, 40, 0, 40)) and r.has_core((0, SHAPE[0], 0, SHAPE[1]))


# ── cell rules ──────────────────────────────────────────────────────────

def test_the_cell_rules_keep_assign_flag_and_drop():
    core = np.zeros((40, 60), np.uint16)
    core[:, :30] = 1
    core[:, 40:] = 2
    seg = np.zeros((40, 60), bool)
    seg[30:, 40:] = True
    cd8 = np.zeros((40, 60), bool)
    cd8[:5, :5] = True
    lab = np.zeros((40, 60), np.int32)
    lab[10:20, 10:20] = 1          # wholly in core 1
    lab[10:20, 24:34] = 2          # 6 of 10 columns in core 1: kept, on the edge
    lab[22:30, 27:37] = 3          # 3 of 10 columns in core 1: no core
    lab[32:38, 45:50] = 4          # in core 2, on a fold: dropped
    lab[0:4, 0:4] = 5              # under a CD8-only exclusion: kept, CD8 invalid
    t = O.ObjectTally()
    for win in ((0, 40, 0, 31), (0, 40, 31, 60)):                       # an object across two windows
        sl = (slice(win[0], win[1]), slice(win[2], win[3]))
        t.add(lab[sl], core[sl], seg[sl], [(0, ["CD8"], cd8[sl])])
    v = t.verdicts()
    assert v[1].keep and v[1].core == 0 and v[1].analysis_include and not v[1].touches_core_edge
    assert v[2].keep and v[2].core == 0 and abs(v[2].core_fraction - 0.6) < 1e-9 and v[2].touches_core_edge
    assert not v[2].analysis_include
    assert not v[3].keep and v[3].reason == "no_core"
    assert not v[4].keep and v[4].reason == "exclusion"
    assert v[5].keep and v[5].excluded_channels == {"CD8"}
    qc = O.measurement_qc(v, [1, 5], ["DAPI", "CD8"])
    assert qc.tolist() == [[0, 0], [0, O.MEASUREMENT_EXCLUDED]]
    assert O.drop_counts(v) == {"no_core": 1, "outside_region": 0, "exclusion": 1}


def test_exactly_half_in_a_core_is_not_enough():
    core = np.zeros((4, 4), np.uint16)
    core[:, :2] = 1
    lab = np.ones((4, 4), np.int32)
    t = O.ObjectTally()
    t.add(lab, core, np.zeros((4, 4), bool))
    assert t.verdicts()[1].reason == "no_core"


def test_one_original_object_is_judged_at_its_global_place_with_the_region_edge():
    snap = {"mode": "tma", "slide_shape": [200, 200],
            "cores": [{"polygon": [[20, 20], [120, 20], [120, 120], [20, 120]]}],
            "exclusions": [{"polygon": [[100, 100], [119, 100], [119, 119], [100, 119]], "channels": "all",
                            "seg_invalid": True}]}
    r = R.AnalysisRaster(snap)
    m = np.ones((10, 10), bool)
    v = O.evaluate_object(m, 50, 50, r, region_bbox=[0, 200, 0, 200])
    assert v.keep and v.core == 0 and v.analysis_include and not v.touches_region_edge
    assert O.evaluate_object(m, 105, 105, r).reason == "exclusion"
    assert O.evaluate_object(m, 150, 150, r).reason == "no_core"
    edge = O.evaluate_object(m, 20, 20, r, region_bbox=[20, 200, 0, 200])
    assert edge.keep and edge.touches_region_edge
    roi = R.AnalysisRaster(dict(snap, mode="roi", cores=[]))         # no map sheet: the parent is the domain
    assert O.evaluate_object(m, 150, 150, roi).keep


# ── F4-1 review fixes (codex_f4_impl1_1.out) ────────────────────────────

def test_a_chosen_map_sheet_before_find_cores_is_refused_not_analysed_whole(project):
    s, _ = _finished()
    d = D.new_document(SID, SHAPE)                                   # map chosen, cores not found
    K.save(project, d, s)
    with pytest.raises(A.AnalysisError, match="cores are not found yet"):
        A.build_snapshot(project, SID, ["DAPI"], study=s)


def test_the_parent_region_limits_cores_and_is_the_roi_domain(project):
    s, d = _ticked()
    K.save(project, d, s)
    cy, cx = _place(1, 1)
    par = {"region_id": "reg_x", "bbox_fullres": [0, int(cy) + 1, 0, SHAPE[1]], "polygon_fullres": None}
    snap = A.build_snapshot(project, SID, ["DAPI"], study=s, parent=par)
    assert [c["label"] for c in snap["cores"]] == ["X1-Y01", "X2-Y01", "X3-Y01"]      # only row 1 meets it
    assert {o["reason"] for o in snap["omitted"]} >= {"outside_region"}
    r = R.AnalysisRaster(snap)
    idx = r.core_index((0, SHAPE[0], 0, SHAPE[1]))
    assert idx[int(cy) + 5:, :].max() == 0 and idx[int(cy) - 5, int(cx)] == 1        # cut at the parent edge
    roi = {"mode": "roi", "slide_shape": list(SHAPE), "cores": [], "exclusions": [],
           "parent": {"bbox_fullres": [10, 50, 10, 50], "polygon_fullres": None}}
    rr = R.AnalysisRaster(roi)
    m = np.ones((5, 5), bool)
    assert O.evaluate_object(m, 20, 20, rr).keep and O.evaluate_object(m, 20, 20, rr).core is None
    assert O.evaluate_object(m, 60, 60, rr).reason == "outside_region"
    assert not rr.has_core((60, 80, 60, 80)) and rr.has_core((0, 30, 0, 30))


def test_identity_ignores_exclusion_ids_and_lists_why_ticked_cores_cannot_run(project):
    s, d = _ticked()
    a = D.add_exclusion(d, G.circle_polygon(300, 300, 30, SHAPE), ["fold"])
    D.add_exclusion(d, G.circle_polygon(600, 600, 30, SHAPE), ["blur"])
    K.save(project, d, s)
    snap = A.build_snapshot(project, SID, ["DAPI"], study=s)
    poly = d["exclusions"][a]["polygon"]
    D.delete_exclusion(d, a)
    D.add_exclusion(d, poly, ["fold"])                                # the same exclusion, a new id
    K.save(project, d, s)
    assert A.is_current(project, snap, s)
    for key in d["positions"]:
        D.set_qc(d, key, "excluded", ["x"])
    K.save(project, d, s)
    with pytest.raises(A.AnalysisError, match="QC excluded"):
        A.build_snapshot(project, SID, ["DAPI"], study=s)


def test_a_window_query_unpacks_only_the_bytes_it_needs():
    sr = R.ShapeRaster([[0, 0], [3999, 0], [3999, 9], [0, 9]], (100, 5000))
    sl, block = sr.part((0, 10, 1003, 1035))
    assert block.shape == (10, 32) and block.all() and sl == (slice(0, 10), slice(0, 32))
    assert np.array_equal(sr.window((0, 10, 3990, 4010))[:, :10], np.ones((10, 10), bool))
    assert not sr.window((0, 10, 3990, 4010))[:, 10:].any()


def test_a_polygon_parent_keeps_its_saved_half_open_box_and_cores_must_meet_its_pixels(project):
    roi = {"mode": "roi", "slide_shape": list(SHAPE), "cores": [], "exclusions": [],
           "parent": {"bbox_fullres": [10, 50, 10, 50], "polygon_fullres": [[10, 10], [50, 10], [50, 50], [10, 50]]}}
    rr = R.AnalysisRaster(roi)
    idx = rr.core_index((0, 60, 0, 60))
    assert idx[49, 49] == 1 and idx[50, 30] == 0 and idx[30, 50] == 0          # the saved box: max excluded
    assert O.evaluate_object(np.ones((3, 3), bool), 30, 50, rr).reason == "outside_region"
    s, d = _finished()
    D.set_run(d, _key(3, 2), True)
    K.save(project, d, s)
    cy, cx = _place(3, 2)                                            # a triangle whose box covers that core
    tri = {"bbox_fullres": [0, int(cy) + 200, 0, int(cx) + 200],
           "polygon_fullres": [[0, 0], [int(cx) - 120, 0], [0, int(cy) - 120]]}
    with pytest.raises(A.AnalysisError) as e:
        A.build_snapshot(project, SID, ["DAPI"], study=s, parent=tri)
    assert [o["reason"] for o in e.value.omitted] == ["outside_region"]


def test_every_ticked_reason_is_kept_on_the_refusal(project):
    s, d = _finished()
    for key in d["positions"]:
        D.set_run(d, key, True)
    for u in list(d["cores"]):
        D.unbind(d, u)
    K.save(project, d, s)
    with pytest.raises(A.AnalysisError) as e:
        A.build_snapshot(project, SID, ["DAPI"], study=s)
    occupied = sum(1 for p in s["map_sheet"]["positions"] if p["donor_block_id"])
    assert len(e.value.omitted) == occupied and {o["reason"] for o in e.value.omitted} == {"not_bound"}
    assert "more" in str(e.value) or occupied <= 10


def test_a_sloping_roi_edge_counts_as_the_region_edge():
    tri = {"mode": "roi", "slide_shape": [200, 200], "cores": [], "exclusions": [],
           "parent": {"bbox_fullres": [0, 200, 0, 200], "polygon_fullres": [[0, 0], [199, 0], [0, 199]]}}
    r = R.AnalysisRaster(tri)
    m = np.ones((10, 10), bool)
    on_slope = O.evaluate_object(m, 95, 95, r, region_bbox=[0, 200, 0, 200])   # mostly inside, crosses the slope
    assert on_slope.touches_region_edge
    assert not O.evaluate_object(m, 20, 20, r, region_bbox=[0, 200, 0, 200]).touches_region_edge
