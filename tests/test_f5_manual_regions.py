"""Step2 runs with manual parameters follow the TMA cores too
(docs/f5_tma_display_plan.md 9.2): the cores are frozen at the start from
the last Save on the chosen fused input's own region and fusion settings;
with no Run tick at all the user is asked, and Yes runs every eligible core
(the same Save); refusals for a TMA project without a usable Save, a method
without per-cell core rules, an input without fusion settings; the worker
checks the frozen snapshot like a hand-over's and the run records where its
cores came from."""

import json
import os
import types

import pytest

from test_regions_r1 import SHAPE, SID, _finished, project  # noqa: F401

from block01.core.regions import analysis as A
from block01.core.regions import document as D
from block01.core.regions import lock as K

FCFG = {"nucleus": {"channel": "DAPI", "weight": 1.0}, "groups": {}}


def _tick(d, n):
    for key in sorted(d["positions"])[:n]:
        D.set_run(d, key, True)


# ── the selection rule ──────────────────────────────────────────────────

def test_no_tick_at_all_is_told_apart_and_all_eligible_leaves_only_the_tick_out(project):
    s, d = _finished()
    K.save(project, d, s)
    with pytest.raises(A.NothingTicked):
        A.build_snapshot(project, SID, ["DAPI"], study=s)
    snap = A.build_snapshot(project, SID, ["DAPI"], study=s, select=A.SELECT_ALL_ELIGIBLE)
    assert snap["selection_mode"] == "all_eligible" and len(snap["cores"]) == len(d["cores"])
    _tick(d, len(d["positions"]))
    K.save(project, d, s)
    ticked_all = A.build_snapshot(project, SID, ["DAPI"], study=s)
    assert ticked_all["selection_mode"] == "ticked"
    assert ticked_all["identity"] == snap["identity"]           # the same cores: the same identity
    assert A.is_current(project, snap, s)                        # rebuilt the way it was chosen


def test_all_eligible_still_leaves_out_review_and_qc_excluded_cores(project):
    s, d = _finished()
    keys = sorted(d["positions"])
    uid = next(u for u, c in d["cores"].items() if c["position_key"] == keys[0])
    d["cores"][uid]["mapping"] = "review"
    d["positions"][keys[1]]["qc"] = "excluded"
    K.save(project, d, s)
    snap = A.build_snapshot(project, SID, ["DAPI"], study=s, select=A.SELECT_ALL_ELIGIBLE)
    got = {c["position_key"] for c in snap["cores"]}
    assert keys[0] not in got and keys[1] not in got and len(got) == len(d["cores"]) - 2
    why = {o["position_key"]: o["reason"] for o in snap["omitted"]}
    assert why[keys[0]] == "review" and why[keys[1]] == "qc_excluded"


def test_ticked_cores_that_cannot_run_are_refused_not_widened(project):
    s, d = _finished()
    keys = sorted(d["positions"])
    D.set_run(d, keys[0], True)
    uid = next(u for u, c in d["cores"].items() if c["position_key"] == keys[0])
    d["cores"][uid]["mapping"] = "review"
    K.save(project, d, s)
    with pytest.raises(A.AnalysisError, match="no ticked core can run") as e:
        A.build_snapshot(project, SID, ["DAPI"], study=s)
    assert not isinstance(e.value, A.NothingTicked)


# ── Step2's freeze (the main window's half) ─────────────────────────────

def _window(project, monkeypatch, tmp_path, study, files=True, fusion=FCFG):
    from block01.core import tile_tissue
    from block01.core.tma import study as st
    from block01.ui.main_window import MainWindow
    monkeypatch.setattr(st, "load_study", lambda pd: study)
    run = tmp_path / "fuse_run"
    run.mkdir(exist_ok=True)
    (run / "params.json").write_text(json.dumps({"fusion_settings": {"hash": "fh", "fusion_config": fusion}
                                                 if fusion else {}}))
    fused = str(run / "fused_Full WSI.zarr")
    shape = SHAPE
    monkeypatch.setattr(tile_tissue, "fused_region", lambda p: ((0, shape[0], 0, shape[1]), "s", "DAPI"))
    w = types.SimpleNamespace(_regions_files_present=lambda: files,
                              _regions_context=lambda: {"project_dir": project, "slide_id": SID})
    return (lambda method="stardist_nuclei_expansion", ask=lambda t: True:
            MainWindow._step2_manual_regions(w, method, fused, None, ask)), fused


def test_the_freeze_asks_only_without_ticks_and_no_means_no_run(project, monkeypatch, tmp_path):
    s, d = _finished()
    K.save(project, d, s)
    freeze, _f = _window(project, monkeypatch, tmp_path, s)
    asked = []
    assert freeze(ask=lambda t: asked.append(t) or False) == (None, None)
    assert asked and "No TMA core is selected" in asked[0]
    req, why = freeze(ask=lambda t: True)
    assert why == "" and req["selection_mode"] == "all_eligible"
    assert req["identity"] == req["snapshot"]["identity"] and req["input_kind"] == "nucleus"
    assert req["fusion_settings_hash"] == "fh" and req["snapshot"]["seg_channels"] == ["DAPI"]
    _tick(d, 2)
    K.save(project, d, s)
    asked.clear()
    req, why = freeze(ask=lambda t: asked.append(t) or True)
    assert not asked and req["selection_mode"] == "ticked" and len(req["snapshot"]["cores"]) == 2


def test_the_freeze_refuses_what_cannot_follow_the_cores(project, monkeypatch, tmp_path):
    s, d = _finished()
    freeze, _f = _window(project, monkeypatch, tmp_path, s)
    assert "Save in Regions first" in freeze()[1]                  # TMA, never saved
    K.save(project, d, s)
    assert "cannot follow the TMA cores" in freeze(method="a_method_without_an_input_kind")[1]
    freeze2, _f = _window(project, monkeypatch, tmp_path, s, fusion=None)
    assert "no fusion settings" in freeze2()[1]
    freeze3, _f = _window(project, monkeypatch, tmp_path, None)     # not a TMA project
    assert freeze3() == (None, "")
    freeze4, _f = _window(project, monkeypatch, tmp_path, s, files=False)
    assert freeze4() == (None, "")


# ── the worker runs the frozen request like a hand-over ─────────────────

LOOPS = ["segment_one_zarr", "run"]


@pytest.mark.parametrize("loop", LOOPS)
def test_a_manual_run_follows_the_snapshot_step2_froze(tmp_path, monkeypatch, loop):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("block01.qt")
    import numpy as np
    from test_f4_step2 import _cells, _snapshot
    from test_step2_ownership_move import _labels
    from test_step2_skip_empty_tiles import _run, _worker
    from block01.workers.segment_merge_worker import SegmentMergeWorker as W
    lab = _labels(3)
    h, w = lab.shape
    worker, zp = _worker(tmp_path, monkeypatch, lab, 3, 4, 8, name="m")
    snap = _snapshot(h, w, core_box=(0, 50, 60, 113))
    snap["selection_mode"] = "all_eligible"
    worker._regions_request = {"snapshot": snap, "identity": snap["identity"], "input_kind": "fusion_fusion_nucleus",
                               "selection_mode": "all_eligible", "fusion_settings_hash": "fh"}
    monkeypatch.setattr(W, "_workspace_slide_id", lambda self: snap["slide_id"])
    if loop == "segment_one_zarr":
        worker._regions = W._regions_begin(worker)
    n = _run(worker, zp, loop)
    cells = _cells(worker, loop)
    core = np.zeros_like(cells, bool)
    core[0:50, 60:113] = True
    ids = np.unique(cells[cells > 0])
    assert n == ids.size and all(core[cells == i].mean() > 0.5 for i in ids)
    meta = worker._regions_run_meta()
    assert meta["source"] == "step2" and meta["selection_mode"] == "all_eligible"
    assert meta["fusion_settings_hash"] == "fh"
    assert os.path.exists(os.path.join(worker.output_dir, "regions_snapshot.json"))


def test_the_worker_refuses_a_broken_or_mismatched_request(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("block01.qt")
    from test_f4_step2 import _snapshot
    from test_step2_ownership_move import _labels
    from test_step2_skip_empty_tiles import _worker
    from block01.workers.segment_merge_worker import SegmentMergeWorker as W
    lab = _labels(3)
    worker, _zp = _worker(tmp_path, monkeypatch, lab, 3, 4, 8, name="b")
    monkeypatch.setattr(W, "_workspace_slide_id", lambda self: "slide_0000000000000000")
    snap = _snapshot(*lab.shape)
    req = {"snapshot": snap, "identity": "not it", "input_kind": "fusion_fusion_nucleus"}
    worker._regions_request = req
    with pytest.raises(RuntimeError, match="identity"):
        W._regions_begin(worker)
    req["identity"] = snap["identity"]
    worker.method = "a_method_without_an_input_kind"
    with pytest.raises(RuntimeError, match="cannot follow"):
        W._regions_begin(worker)
    worker.method = "cellpose_wholecell_fusion"
    worker._contract = {"method": "cellpose_wholecell_fusion", "regions": {"snapshot": snap}}
    with pytest.raises(RuntimeError, match="both"):                # a second snapshot: refused
        W._regions_begin(worker)
    worker._contract = {"method": "cellpose_wholecell_fusion", "halo_px": 8}   # an index hand-over (§11)
    assert W._regions_begin(worker)["source"] == "step2"
    worker._contract = {"method": "cellpose_nuclei_dapi", "halo_px": 8}
    with pytest.raises(RuntimeError, match="hand-over is for"):
        W._regions_begin(worker)
    worker._contract = None
    assert W._regions_begin(worker)["source"] == "step2"


def test_provenance_records_a_manual_runs_cores_and_their_save(tmp_path, monkeypatch):
    from block01.core import provenance as prov
    seen = {}
    monkeypatch.setattr(prov, "find_workspace", lambda d: (str(tmp_path), str(tmp_path / "ws")))
    monkeypatch.setattr(prov, "_load", lambda p: {"source_ome": "x"})
    monkeypatch.setattr(prov, "ensure_raw_slide", lambda pd, s: "slide_1")
    monkeypatch.setattr(prov, "workspace_regions", lambda ws, sid: {})
    monkeypatch.setattr(prov, "load_entries", lambda pd: [])
    monkeypatch.setattr(prov, "lookup", lambda *a: None)
    monkeypatch.setattr(prov, "safe_register", lambda *a, **k: seen.update(k) or "id")
    meta = {"method": "stardist_nuclei_expansion", "seg_config": {}, "rois": [],
            "regions_run": {"source": "step2", "selection_mode": "all_eligible", "identity": "I",
                            "input_kind": "nucleus", "content_sha256": "C", "position_keys": ["k1"],
                            "fusion_settings_hash": "fh", "snapshot": "/x/regions_snapshot.json"}}
    prov.register_segmentation_run(str(tmp_path / "ws" / "runs" / "segment_x"), meta)
    reg = seen["parameters"]["regions"]
    assert reg["source"] == "step2" and reg["selection_mode"] == "all_eligible"
    assert reg["position_keys"] == ["k1"]
    assert {"role": "regions_layout", "content_sha256": "C"} in seen["unresolved_inputs"]


def test_a_tma_project_whose_slide_cannot_be_told_is_refused_not_run_whole(project, monkeypatch, tmp_path):
    from block01.ui.main_window import MainWindow
    s, d = _finished()
    K.save(project, d, s)
    freeze, fused = _window(project, monkeypatch, tmp_path, s)
    w = types.SimpleNamespace(_regions_files_present=lambda: True,
                              _regions_context=lambda: {"project_dir": project, "slide_id": None})
    req, why = MainWindow._step2_manual_regions(w, "stardist_nuclei_expansion", fused, None, lambda t: True)
    assert req is None and "cannot be identified" in why


def test_the_question_counts_the_candidate_that_then_runs(project, monkeypatch, tmp_path):
    s, d = _finished()
    K.save(project, d, s)
    freeze, _f = _window(project, monkeypatch, tmp_path, s)
    asked = []
    req, why = freeze(ask=lambda t: asked.append(t) or True)
    assert f"All {len(req['snapshot']['cores'])} eligible cores" in asked[0]


def test_index_parameters_are_decoupled_from_the_cores_step1_froze():
    """User ruling 2026-10-19: whatever the parameters' source, a run takes
    the cores of the last Regions Save at its start; the hand-over's own
    frozen cores are dropped, the rest of the hand-over is kept."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("block01.qt")
    from block01.core import preseg_contract as pc
    from block01.ui.step2_page import Step2Page
    cfg = {"method": "stardist_nuclei_expansion",
           pc.CONTRACT_KEY: {"method": "stardist_nuclei_expansion", "halo_px": 200,
                             "regions": {"identity": "old", "snapshot": {}}}}
    out = Step2Page._without_handover_regions(cfg)
    assert "regions" not in out[pc.CONTRACT_KEY] and out[pc.CONTRACT_KEY]["halo_px"] == 200
    assert Step2Page._without_handover_regions({"method": "x"}) == {"method": "x"}


def test_an_index_hand_over_starts_with_the_cores_frozen_at_run(tmp_path, monkeypatch):
    """§11 on the worker: a Step1 hand-over (its own Regions dropped by the
    page) plus Step2's freshly frozen snapshot is accepted, and the run's
    source is Step2's freeze (the run itself is covered above)."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("block01.qt")
    from test_f4_step2 import _snapshot
    from test_step2_ownership_move import _labels
    from test_step2_skip_empty_tiles import _worker
    from block01.ui.step2_page import Step2Page
    from block01.core import preseg_contract as pc
    from block01.workers.segment_merge_worker import SegmentMergeWorker as W
    lab = _labels(3)
    h, w = lab.shape
    worker, _zp = _worker(tmp_path, monkeypatch, lab, 3, 4, 8, name="ix")
    snap = _snapshot(h, w, core_box=(0, 50, 60, 113))
    cfg = Step2Page._without_handover_regions(
        {"method": worker.method, pc.CONTRACT_KEY: {"method": worker.method, "halo_px": 8,
                                                     "regions": {"snapshot": {"old": True}}}})
    worker._contract = cfg[pc.CONTRACT_KEY]
    worker._regions_request = {"snapshot": snap, "identity": snap["identity"],
                               "input_kind": "fusion_fusion_nucleus", "selection_mode": "ticked"}
    monkeypatch.setattr(W, "_workspace_slide_id", lambda self: snap["slide_id"])
    reg = W._regions_begin(worker)
    assert reg["source"] == "step2" and reg["snapshot"] is not None
    assert os.path.exists(os.path.join(worker.output_dir, "regions_snapshot.json"))


# ── a slide without TMA cores follows its exclusions (docs/v17_step2_exclusions_plan.md) ──

def _no_map_save(project, *exclusions):
    """A Regions Save without a map sheet, holding `exclusions`
    ((polygon, reasons, channels) each); no study."""
    from block01.core.regions import geometry as G
    d = D.new_document(SID, SHAPE)
    for cy, cx, reasons, channels in exclusions:
        D.add_exclusion(d, G.circle_polygon(cy, cx, 50, SHAPE), reasons, channels=channels)
    K.save(project, d, None)
    return d


def test_a_slide_without_cores_freezes_its_exclusions_without_a_question(project, monkeypatch, tmp_path):
    _no_map_save(project, (400, 400, ["blur"], "all"), (900, 700, ["other"], ["CD3"]))
    freeze, _f = _window(project, monkeypatch, tmp_path, None)
    asked = []
    req, why = freeze(ask=lambda t: asked.append(t) or True)
    assert why == "" and not asked
    snap = req["snapshot"]
    assert snap["mode"] == "roi" and snap["cores"] == [] and snap["map_sheet"] is None
    assert sorted(e["seg_invalid"] for e in snap["exclusions"]) == [False, True]
    assert req["identity"] == snap["identity"] and req["input_kind"] == "nucleus"
    assert req["fusion_settings_hash"] == "fh" and req["selection_mode"] == snap["selection_mode"]


def test_a_slide_without_cores_keeps_the_old_run_without_exclusions_or_a_save(project, monkeypatch, tmp_path):
    freeze, _f = _window(project, monkeypatch, tmp_path, None)
    assert freeze() == (None, "")                                   # no Save at all
    _no_map_save(project)                                           # a Save, no exclusion
    assert freeze() == (None, "")
    s, d = _finished()                                              # a map sheet but no study: unchanged
    K.save(project, d, s)
    assert freeze() == (None, "")


def test_a_slide_without_cores_refuses_what_cannot_follow_its_exclusions(project, monkeypatch, tmp_path):
    _no_map_save(project, (400, 400, ["blur"], "all"))
    freeze, _f = _window(project, monkeypatch, tmp_path, None)
    req, why = freeze(method="a_method_without_an_input_kind")
    assert req is None and "cannot follow the Regions exclusions" in why and "TMA" not in why
    freeze2, _f = _window(project, monkeypatch, tmp_path, None, fusion=None)
    assert "no fusion settings" in freeze2()[1]

    def broken(*a, **k):
        raise A.AnalysisError("the last Regions Save was interrupted: Save again in Regions")
    freeze, _f = _window(project, monkeypatch, tmp_path, None)     # the fused input's settings back
    monkeypatch.setattr(A, "build_snapshot", broken)                # refusal propagation only
    assert "interrupted" in freeze()[1]
    monkeypatch.undo()
    with open(K.saved_path(project), "w", encoding="utf-8") as f:   # an unreadable Save
        f.write("{not json")
    freeze3, _f = _window(project, monkeypatch, tmp_path, None)
    req, why = freeze3()
    assert req is None and "cannot be read" in why


def _roi_snapshot(h, w, box, channels):
    from test_f4_step2 import _identified, _snapshot
    snap = _snapshot(h, w, excl_box=box, channels=channels)
    snap.update(mode="roi", cores=[])
    return _identified(snap)


def _roi_run(tmp_path, monkeypatch, loop, snap, name):
    from test_step2_skip_empty_tiles import _run, _worker
    from block01.workers.segment_merge_worker import SegmentMergeWorker as W
    from test_step2_ownership_move import _labels
    worker, zp = _worker(tmp_path, monkeypatch, _labels(3), 3, 4, 8, name=name)
    if snap is not None:
        worker._regions_request = {"snapshot": snap, "identity": snap["identity"],
                                   "input_kind": "fusion_fusion_nucleus",
                                   "selection_mode": snap.get("selection_mode", "ticked"),
                                   "fusion_settings_hash": "fh"}
        monkeypatch.setattr(W, "_workspace_slide_id", lambda self: snap["slide_id"])
        if loop == "segment_one_zarr":
            worker._regions = W._regions_begin(worker)
    n = _run(worker, zp, loop)
    return worker, n


def _dropped(out_dir):
    """The Regions drop record the run wrote into its segmentation metadata."""
    def find(o):
        if isinstance(o, dict):
            if isinstance(o.get("dropped"), dict) and "exclusion" in o["dropped"]:
                return o["dropped"]
            o = list(o.values())
        if isinstance(o, list):
            for v in o:
                got = find(v)
                if got is not None:
                    return got
        return None
    for name in sorted(os.listdir(out_dir)):
        if name.startswith("segmentation_meta") and name.endswith(".json"):
            with open(os.path.join(out_dir, name), encoding="utf-8") as f:
                got = find(json.load(f))
            if got is not None:
                return got
    raise AssertionError(f"no Regions drop record in {out_dir}")


BOX = (30, 50, 80, 100)                     # in the tissue (columns 60..112)


@pytest.mark.parametrize("loop", LOOPS)
def test_an_object_touching_a_blur_exclusion_is_dropped_whole(tmp_path, monkeypatch, loop):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("block01.qt")
    import numpy as np
    from test_f4_step2 import _cells
    plain, _n = _roi_run(tmp_path, monkeypatch, loop, None, "plain")
    before = _cells(plain, loop)
    y0, y1, x0, x1 = BOX
    inside = np.zeros_like(before, bool)
    inside[y0:y1, x0:x1] = True
    touching = {int(i) for i in np.unique(before[inside]) if i}
    mostly_out = [i for i in touching if inside[before == i].mean() < 0.5]
    assert mostly_out                       # the case the rule is about exists here
    h, w = before.shape
    on, n = _roi_run(tmp_path, monkeypatch, loop, _roi_snapshot(h, w, BOX, "all"), "on")
    after = _cells(on, loop)
    gone = np.isin(before, list(touching))
    np.testing.assert_array_equal(after > 0, (before > 0) & ~gone)   # touching: gone whole; rest kept
    assert n == int(np.unique(after[after > 0]).size)
    assert _dropped(on.output_dir)["exclusion"] == len(touching)     # the run's own drop record
    meta = on._regions_run_meta()
    assert meta["source"] == "step2" and meta["position_keys"] == []
    assert os.path.exists(os.path.join(on.output_dir, "regions_snapshot.json"))


@pytest.mark.parametrize("loop", LOOPS)
def test_a_measurement_only_exclusion_keeps_the_cells_and_marks_them(tmp_path, monkeypatch, loop):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("block01.qt")
    import numpy as np
    from test_f4_step2 import _cells
    plain, _n = _roi_run(tmp_path, monkeypatch, loop, None, "plain")
    before = _cells(plain, loop)
    h, w = before.shape
    on, n = _roi_run(tmp_path, monkeypatch, loop, _roi_snapshot(h, w, BOX, ["CD3"]), "on")
    after = _cells(on, loop)
    np.testing.assert_array_equal(after > 0, before > 0)             # nothing dropped
    prefix = "_t" if loop == "segment_one_zarr" else ""
    facts = np.load(os.path.join(on.output_dir, f"global_cell_regions{prefix}.npz"), allow_pickle=False)
    y0, y1, x0, x1 = BOX
    marked = {int(i) for i in np.unique(after[y0:y1, x0:x1]) if i}
    assert marked
    bits = facts["meas_bits"]
    assert bits.shape == (n,)
    assert all(bits[i - 1] == 1 for i in marked)
    assert all(bits[i - 1] == 0 for i in range(1, n + 1) if i not in marked)
    assert list(facts["measurement_exclusions"]) == ["excl_a"]
