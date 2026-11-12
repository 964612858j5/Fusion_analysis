"""Cores found without a map sheet: only they are analysed
(docs/v17_cores_without_map_downstream_plan.md §3.1-3.3, §5.1-3, §5.6a-c).

The snapshot's `"cores"` mode, the per-core Run tick and its lifecycle, the
raster rule, the display reasons; the TMA snapshot's identity unchanged.
Synthetic documents (the R1 test layout) without a study.
"""

import copy
import hashlib
import json

import pytest

from test_regions_r1 import SHAPE, SID, _finished, project  # noqa: F401

from block01.core.regions import analysis as A
from block01.core.regions import document as D
from block01.core.regions import geometry as G
from block01.core.regions import lock as K
from block01.core.regions import raster as R

BOXES = [(200, 200), (200, 700), (800, 200), (800, 700)]       # core centres (y, x), radius 120


def _doc(n=4):
    d = D.new_document(SID, SHAPE)
    for cy, cx in BOXES[:n]:
        D.add_drawn_core(d, G.circle_polygon(cy, cx, 120, SHAPE))
    return d


def _uid_at(d, cy, cx):
    return min(d["cores"], key=lambda u: sum((a - b) ** 2 for a, b in zip(D.core_centre(d, u), (cy, cx))))


def _snap(project, d, **kw):
    K.save(project, d, None)
    return A.build_snapshot(project, SID, ["DAPI"], **kw)


def _old_identity(snap):
    """`identity` as it was before this block (HEAD d4e5dc5)."""
    excl = sorted(json.dumps({k: e[k] for k in ("polygon", "channels", "seg_invalid")}, sort_keys=True)
                  for e in snap["exclusions"])
    science = {"v": snap["snapshot_version"], "mode": snap["mode"], "slide_id": snap["slide_id"],
               "parent": {k: snap["parent"].get(k) for k in ("bbox_fullres", "polygon_fullres")},
               "map": (snap["map_sheet"] or {}).get("sha256"),
               "cores": [{k: c[k] for k in ("position_key", "polygon", "donor_block_id",
                                            "tma_unique_sample_id", "annotation", "qc")} for c in snap["cores"]],
               "exclusions": excl, "seg_channels": snap["seg_channels"], "policies": snap["policies"]}
    return hashlib.sha256(json.dumps(science, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# ── the snapshot ────────────────────────────────────────────────────────

def test_found_cores_without_a_map_sheet_are_the_domain(project):
    d = _doc()
    off = _uid_at(d, 800, 700)
    D.set_core_run(d, off, False)
    snap = _snap(project, d)
    assert snap["mode"] == "cores" and snap["map_sheet"] is None and snap["positions"] == []
    assert len(snap["cores"]) == 3 and off not in {c["uid"] for c in snap["cores"]}
    for c in snap["cores"]:
        assert c["position_key"] is None and c["label"] == "" and c["qc"] == "unreviewed"
        assert c["region_id"].startswith("reg_") and len(c["polygon"]) >= 3 and len(c["centre_yx"]) == 2
    assert [o["reason"] for o in snap["omitted"]] == ["not_ticked"] and snap["omitted"][0]["uid"] == off
    inv = {r["uid"]: r for r in snap["core_inventory"]}
    assert set(inv) == set(d["cores"]) and inv[off]["reason"] == "not_ticked" and not inv[off]["run"]
    assert all(r["area_px"] > 0 for r in inv.values())
    shown = {c["uid"]: c for c in snap["display_cores"]}
    assert shown[off]["reason"] == "not_ticked" and not shown[off]["run"]       # not "not_bound"
    assert A.load_snapshot.__name__ and A.has_cores(snap)


def test_no_tick_overlap_outside_and_no_cores(project):
    d = _doc()
    D.set_core_runs(d, list(d["cores"]), False)
    with pytest.raises(A.NothingTicked):
        _snap(project, d)
    every = _snap(project, d, select=A.SELECT_ALL_ELIGIBLE)                  # the tick left out only
    assert len(every["cores"]) == 4 and every["selection_mode"] == "all_eligible"
    D.set_core_runs(d, list(d["cores"]), True)
    # a parent region holding only the top-left core: the others are outside
    snap = _snap(project, d, parent={"region_id": None, "bbox_fullres": [0, 400, 0, 400],
                                     "polygon_fullres": None})
    assert len(snap["cores"]) == 1
    assert sorted(o["reason"] for o in snap["omitted"]) == ["outside_region"] * 3
    with pytest.raises(A.AnalysisError, match="core at"):                   # named by centre, not a label
        _snap(project, d, parent={"region_id": None, "bbox_fullres": [1300, 1400, 1000, 1100],
                                  "polygon_fullres": None})
    pid = d["cores"][_uid_at(d, 200, 700)]["pieces"][0]
    D.translate_shape(d, pid, 0, -450)                                       # onto the first core
    with pytest.raises(A.AnalysisError, match="overlap: core at"):
        _snap(project, d)
    empty = D.new_document(SID, SHAPE)
    assert _snap(project, empty)["mode"] == "roi"                            # no cores: the whole region


def test_a_degenerate_core_has_its_inventory_row(project):
    d = _doc(2)
    flat = _uid_at(d, 200, 700)
    pid = d["cores"][flat]["pieces"][0]
    d["pieces"][pid]["polygon"] = [[700, 200], [710, 200], [720, 200]]       # no area
    snap = _snap(project, d)
    inv = {r["uid"]: r for r in snap["core_inventory"]}
    assert inv[flat]["reason"] == "degenerate" and inv[flat]["area_px"] == 0.0
    assert [c["uid"] for c in snap["cores"]] == [u for u in d["cores"] if u != flat]


def test_identity_tma_unchanged_and_cores_includes_the_uid(project):
    s, d = _finished()
    for key in d["positions"]:
        D.set_run(d, key, True)
    K.save(project, d, s)
    tma = A.build_snapshot(project, SID, ["DAPI"], study=s)
    assert tma["identity"] == _old_identity(tma)                             # byte-identical for TMA
    assert "core_inventory" not in tma
    cores = _snap(project, _doc())
    assert cores["identity"] != _old_identity(cores)                         # an older build refuses it
    other = copy.deepcopy(cores)
    other["cores"][0]["uid"] = "core_other"
    assert A.identity(other) != cores["identity"]


def test_the_raster_takes_the_cores(project):
    d = _doc()
    snap = _snap(project, d)
    r = R.AnalysisRaster(snap)
    assert r.tma
    first = [c["uid"] for c in snap["cores"]].index(_uid_at(d, 200, 200)) + 1
    idx = r.core_index((60, 340, 60, 340))
    assert set(idx.ravel().tolist()) == {0, first}                           # that core and the glass around it
    assert r.core_index((450, 550, 450, 550)).max() == 0                     # between cores: no domain


# ── the Run tick ────────────────────────────────────────────────────────

def test_the_run_tick_lifecycle(project):
    d = _doc()
    assert all(D.core_runs(d, u) for u in d["cores"])                        # on by default
    legacy = copy.deepcopy(d)
    for c in legacy["cores"].values():
        c.pop("run")
    assert all(D.core_runs(legacy, u) for u in legacy["cores"])              # an older document: on
    a, b = _uid_at(d, 200, 200), _uid_at(d, 200, 700)
    D.set_core_run(d, b, False)
    # a merge keeps the receiving core's record, so its tick
    plan = D.merge_plan(d, [d["cores"][a]["pieces"][0], d["cores"][b]["pieces"][0]], None)
    recv = D.merge_pieces(d, [d["cores"][a]["pieces"][0], d["cores"][b]["pieces"][0]])
    assert recv == plan["receiver"] and d["cores"][recv]["run"] == (recv == a)
    D.set_core_run(d, recv, False)
    # Save, its draft, a locked version's draft: the ticks come back
    K.save(project, d, None)
    rec = K.load_saved(project)
    assert rec["run_cores"] == sorted(u for u in d["cores"] if D.core_runs(d, u))
    assert {u: c["run"] for u, c in K.draft_from_saved(rec)["cores"].items()} == \
        {u: c["run"] for u, c in d["cores"].items()}
    locked = dict(rec, version=1)
    back = K.draft_from_locked(locked)
    assert {u: c["run"] for u, c in back["cores"].items()} == {u: c["run"] for u, c in d["cores"].items()}
    assert all(back["pieces"][p]["owner"] == u for u, c in back["cores"].items() for p in c["pieces"])
    # with a map sheet the tick is the position's: attach_map drops it
    s, _ = _finished()
    m = copy.deepcopy(d)
    m["slide_id"] = s["slide_id"]
    D.attach_map(m, s)
    assert all("run" not in c for c in m["cores"].values())
    with pytest.raises(D.RegionsError):
        D.set_core_run(m, next(iter(m["cores"])), True)


def test_a_tma_save_is_unchanged(project):
    s, d = _finished()
    K.save(project, d, s)
    rec = K.load_saved(project)
    assert "run_cores" not in rec and all("run" not in c for c in rec["document"]["cores"].values())


def test_union_undo_redo_and_attach_map_undo_keep_the_ticks():
    from block01.ui.tma.draft import DraftHolder
    d = _doc()
    a, b = _uid_at(d, 200, 200), _uid_at(d, 200, 700)
    h = DraftHolder(d)
    h.do(D.set_core_run, b, False)
    assert not D.core_runs(h.d, b)
    h.undo()
    assert D.core_runs(h.d, b)
    h.redo()
    assert not D.core_runs(h.d, b)
    h.do(D.translate_shape, h.d["cores"][b]["pieces"][0], 0, -300)          # touching, so Union can fuse
    plan = D.merge_plan(h.d, [h.d["cores"][a]["pieces"][0], h.d["cores"][b]["pieces"][0]], None)
    fused = h.do(D.union_pieces, [h.d["cores"][a]["pieces"][0], h.d["cores"][b]["pieces"][0]])
    recv = h.d["pieces"][fused]["owner"]
    assert recv == plan["receiver"] and h.d["cores"][recv]["run"] == (recv == a)   # the receiver's tick
    before = {u: c["run"] for u, c in h.d["cores"].items()}
    s, _ = _finished()
    h.d["slide_id"] = s["slide_id"]
    h.do(D.attach_map, s)
    assert all("run" not in c for c in h.d["cores"].values())
    h.undo()                                                                 # the ticks come back
    assert {u: c["run"] for u, c in h.d["cores"].items()} == before


def test_a_degenerate_core_reaches_step4_with_no_area(project):
    """The real snapshot builder, a degenerate core: its inventory row has
    area 0 and Step4's per-core table keeps it at 0."""
    import types
    from block01.core.regions import quant_apply as qa
    d = _doc(2)
    flat = _uid_at(d, 200, 700)
    d["pieces"][d["cores"][flat]["pieces"][0]]["polygon"] = [[700, 200], [710, 200], [720, 200]]
    snap = _snap(project, d)
    rin = types.SimpleNamespace(snapshot=snap, core=__import__("numpy").zeros(0, "int32"),
                                areas_px=qa.core_areas_by_uid(snap), pixel_um=None)
    res = types.SimpleNamespace(ids=__import__("numpy").zeros(0, "int64"))
    frame = qa.cores_frame(res, rin)
    row = frame[frame["core_uid"] == flat].iloc[0]
    assert row["reason"] == "degenerate" and row["area_px"] == 0 and row["n_obs"] == 0
