"""Block RM-2: downstream along the chain -- segment and quant runs, Step2's
Input, Step3's list, Step4 through the chain, deletion (application §13 RM-2)."""

import json
import os
import types
from datetime import datetime

import pytest

pytest.importorskip("PyQt5")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtWidgets  # noqa: E402

from block01.utils import run_store as rs  # noqa: E402
from block01.utils import trash  # noqa: E402
from test_v16_rm1_run_store import (  # noqa: E402,F401  (fixtures and helpers)
    app, _rm_window, _correct_run, _fuse_run, T0)


def _t(minute, second=0):
    return datetime(2026, 10, 3, 12, minute, second)


def _segment_run(ws, fuse, now, method="stardist"):
    r = rs.new_run(ws, "segment", suffix=method, now=now)
    with open(os.path.join(r, "segmentation_meta.json"), "w") as f:
        json.dump({"run_id": os.path.basename(r), "method": method,
                   "created_at": now.isoformat(), "rois": []}, f)
    rs.write_params(r, {"kind": "segment", "summary": method,
                        "segmentation_config": {"method": method}})
    rs.write_inputs(r, fuse)
    rs.publish(r)
    return r


# ── the Step2 worker: a segment run, its three files ────────────────────

def _worker_stub(tmp_path, zarr_path, method="stardist_nuclei_expansion"):
    from block01.workers.segment_merge_worker import SegmentMergeWorker
    ns = types.SimpleNamespace(
        roi_dir="", project_output_dir="", zarr_path=zarr_path, method=method,
        seg_config={"method": method, "prob_thresh": 0.5}, param_file="",
        parameter_source="manual", n_rows=2, n_cols=2, overlap_px=64,
        created_at="2026-10-03T12:30:00", seg_config_method=method)
    ns._name_method = lambda: method
    return SegmentMergeWorker, ns


def test_a_step2_run_is_a_segment_run_on_its_fuse_run(app, tmp_path):
    w, ws = _rm_window(app, tmp_path)
    try:
        c = _correct_run(ws)
        f = _fuse_run(ws, c)
        cls, ns = _worker_stub(tmp_path, os.path.join(f, "fused_R1.zarr"))
        ns.roi_dir = ws
        run_id, out_dir, _ = cls._create_output_dir(ns)
        assert os.path.dirname(out_dir) == rs.runs_dir(ws)
        assert run_id.startswith("segment_") and run_id.endswith("_stardist_nuclei_expansion")
        assert not rs.is_done(out_dir)                         # published at the end
        ns.output_dir = out_dir
        cls._rm_publish_segment_run(ns)
        assert rs.is_done(out_dir) and rs.upstream_of(out_dir) == f
        params = rs.read_params(out_dir)
        assert params["segmentation_config"]["prob_thresh"] == 0.5     # what Run used
        assert params["input_zarr_path"].startswith("rois/ws1/runs/")   # relative
    finally:
        w.close()


def test_a_step2_run_on_something_else_is_not_published(app, tmp_path):
    w, ws = _rm_window(app, tmp_path)
    try:
        os.makedirs(rs.runs_dir(ws))                      # a workspace has runs/
        cls, ns = _worker_stub(tmp_path, str(tmp_path / "somewhere" / "fused.zarr"))
        ns.roi_dir = ws
        _rid, out_dir, _ = cls._create_output_dir(ns)
        ns.output_dir = out_dir
        with pytest.raises(RuntimeError, match="not a published Step1 result"):
            cls._rm_publish_segment_run(ns)
        assert not rs.is_done(out_dir)
    finally:
        w.close()


def test_two_methods_on_one_fuse_run_and_a_third_on_a_new_one(app, tmp_path):
    """§13: three inputs.json, each right; two params.json each its own."""
    w, ws = _rm_window(app, tmp_path)
    try:
        c = _correct_run(ws)
        f1 = _fuse_run(ws, c)
        f2 = _fuse_run(ws, c, now=_t(5))
        cls = None
        out = []
        for fuse, method, cfg in ((f1, "stardist", {"prob_thresh": 0.5}),
                                  (f1, "cellpose", {"diameter": 30}),
                                  (f2, "mesmer", {"maxima": 0.1})):
            cls, ns = _worker_stub(tmp_path, os.path.join(fuse, "fused_R1.zarr"), method)
            ns.roi_dir = ws
            ns.seg_config = dict(cfg, method=method)
            _rid, ns.output_dir, _ = cls._create_output_dir(ns)
            cls._rm_publish_segment_run(ns)
            out.append(ns.output_dir)
        assert [rs.upstream_of(r) for r in out] == [f1, f1, f2]
        assert [rs.read_params(r)["segmentation_config"] for r in out] == [
            {"prob_thresh": 0.5, "method": "stardist"}, {"diameter": 30, "method": "cellpose"},
            {"maxima": 0.1, "method": "mesmer"}]
        assert all(rs.correct_run_of(r) == c for r in out)
    finally:
        w.close()


# ── Step3's list ────────────────────────────────────────────────────────

def test_step3_lists_published_segment_runs_with_their_fuse_run(app, tmp_path):
    from block01.core import step3_masks
    w, ws = _rm_window(app, tmp_path)
    try:
        c = _correct_run(ws)
        f = _fuse_run(ws, c)
        s1 = _segment_run(ws, f, _t(10))
        s2 = _segment_run(ws, f, _t(20), method="cellpose")
        os.makedirs(os.path.join(rs.runs_dir(ws), "segment_20261003_123000_x"))  # unfinished
        runs = step3_masks.list_runs(ws)
        assert [os.path.basename(r.run_dir) for r in runs] == [os.path.basename(s2),
                                                               os.path.basename(s1)]
        assert runs[0].display == "10-03 12:20 · cellpose"
        assert runs[0].upstream_display == rs.display_name(f)
        assert w._step3_run_tag(runs[0]) == rs.display_name(f)        # no "unknown"
    finally:
        w.close()


# ── Step2's Input and the chain being viewed ────────────────────────────

def test_step2_input_follows_the_chain_being_viewed(app, tmp_path, monkeypatch):
    """§3: choosing S1 (made on C1) in Step3 makes Step2 show S1's fuse run;
    what is edited (C2) does not change."""
    w, ws = _rm_window(app, tmp_path)
    try:
        project = rs.project_dir_of(ws)
        monkeypatch.setattr(w._step2, "_load_zarr_info", lambda: None)
        c1 = _correct_run(ws)
        f1a = _fuse_run(ws, c1)
        f1b = _fuse_run(ws, c1, now=_t(5))
        s1 = _segment_run(ws, f1a, _t(6))
        c2 = _correct_run(ws, now=_t(7))
        f2 = _fuse_run(ws, c2, now=_t(8))
        rs.update_session(ws, editing={"correct_run": rs.rel(project, c2),
                                       "geometry_revision": 0},
                          viewing=rs.rel(project, c2))
        w._corrected_zarr_path = os.path.join(c2, "corrected_channels.zarr")
        w._rm_bind_step2()
        assert w._step2.selected_fuse_run() == f2
        w._rm_note_viewing(s1)                                   # Step3 chose S1
        w._rm_bind_step2()
        assert [i["run"] for i in w._step2._fuse_items] == [f1b, f1a]   # C1's, newest first
        assert w._step2.selected_fuse_run() == f1a                       # S1's own
        assert w._step2._zarr_edit.text() == os.path.join(f1a, "fused_R1.zarr")
        assert rs.load_session(ws)["editing"]["correct_run"] == rs.rel(project, c2)
    finally:
        w.close()


def test_a_deleted_preseg_run_shows_as_deleted_and_step2_still_works(app, tmp_path,
                                                                    monkeypatch):
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        lambda *a, **k: QtWidgets.QMessageBox.Yes)
    w, ws = _rm_window(app, tmp_path)
    try:
        project = rs.project_dir_of(ws)
        monkeypatch.setattr(w._step2, "_load_zarr_info", lambda: None)
        c = _correct_run(ws)
        pre = rs.new_run(ws, "preseg", now=_t(1))
        rs.write_inputs(pre, c)
        rs.publish(pre)
        f = _fuse_run(ws, c, now=_t(2))
        rs.write_params(f, dict(rs.read_params(f), preseg_run=rs.rel(project, pre)))
        w._corrected_zarr_path = os.path.join(c, "corrected_channels.zarr")
        w._rm_bind_step2()
        assert w._step2._fuse_items[0]["tag"] == "pre-seg"
        assert w._rm_delete_run(pre) is True
        assert rs.list_runs(ws, "fuse") == [f]                   # not a pixel upstream
        w._rm_bind_step2()
        item = w._step2._fuse_items[0]
        assert item["tag"] == "pre-seg deleted" and "was deleted" in item["tooltip"]
        assert w._step2.selected_fuse_run() == f
    finally:
        w.close()


# ── deletion along the chain (§9) ───────────────────────────────────────

def _chain(ws):
    c = _correct_run(ws)
    f = _fuse_run(ws, c, now=_t(1))
    s1 = _segment_run(ws, f, _t(2))
    s2 = _segment_run(ws, f, _t(3), method="cellpose")
    q1 = rs.new_run(ws, "quant", now=_t(4))
    rs.write_inputs(q1, s1)
    rs.publish(q1)
    return c, f, s1, s2, q1


def test_deleting_a_segment_run_leaves_its_fuse_run_and_siblings(app, tmp_path, monkeypatch):
    asked = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        lambda *a, **k: asked.append(a[2]) or QtWidgets.QMessageBox.Yes)
    w, ws = _rm_window(app, tmp_path)
    try:
        c, f, s1, s2, q1 = _chain(ws)
        assert w._rm_delete_run(s1) is True
        assert sorted(rs.list_runs(ws)) == sorted([c, f, s2])
        assert "quant" in asked[0] and "segment" in asked[0]          # its quant run too
        [entry] = trash.entries(rs.project_dir_of(ws))
        assert [i["from"].split("/")[-1] for i in entry["items"]] == \
            [os.path.basename(q1), os.path.basename(s1)]               # downstream first
        assert entry["status"] == "complete"
    finally:
        w.close()


def test_deleting_a_fuse_run_lists_and_moves_everything_below_it(app, tmp_path, monkeypatch):
    asked = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        lambda *a, **k: asked.append(a[2]) or QtWidgets.QMessageBox.Yes)
    w, ws = _rm_window(app, tmp_path)
    try:
        c, f, s1, s2, q1 = _chain(ws)
        assert w._rm_delete_run(f) is True
        for r in (f, s1, s2, q1):
            assert rs.display_name(r) in asked[0]                     # every one listed
        assert rs.list_runs(ws) == [c]
    finally:
        w.close()


def test_nothing_is_deleted_while_a_preseg_run_runs(app, tmp_path, monkeypatch):
    told = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        lambda *a, **k: told.append(a[2]))
    w, ws = _rm_window(app, tmp_path)
    try:
        c, f, s1, s2, q1 = _chain(ws)
        w._preseg_job = types.SimpleNamespace(is_running=lambda: True)
        assert w._rm_delete_run(s1) is False
        assert "pre-segmentation" in told[0] and rs.is_done(s1)
    finally:
        w._preseg_job = None
        w.close()


def test_a_move_that_fails_stops_and_viewing_falls_to_the_survivor(app, tmp_path,
                                                                  monkeypatch):
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        lambda *a, **k: QtWidgets.QMessageBox.Yes)
    warned = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    w, ws = _rm_window(app, tmp_path)
    try:
        project = rs.project_dir_of(ws)
        c, f, s1, s2, q1 = _chain(ws)
        rs.update_session(ws, viewing=rs.rel(project, s2))
        real = os.rename
        calls = []

        def _rename(src, dst):
            calls.append(src)
            if len(calls) == 2:                                    # the second folder
                raise OSError("disk says no")
            return real(src, dst)
        monkeypatch.setattr(trash.os, "rename", _rename)
        w._rm_delete_run(f)
        entry = trash.entries(project)[-1]
        assert entry["status"] == "partial" and len(entry["items"]) == 1
        assert warned and "stopped" in warned[0]
        moved = os.path.join(project, *entry["items"][0]["from"].split("/"))
        survivors = [r for r in (f, s1, s2, q1) if os.path.isdir(r)]
        assert moved not in survivors and all(rs.is_done(r) for r in survivors)
        # what stays is whole: every survivor's upstream is still there
        assert all(rs.upstream_of(r) in (c, f, s1, s2) for r in survivors)
        sess = rs.load_session(ws)
        view = rs.resolve(project, sess["viewing"]) if sess.get("viewing") else ""
        assert view == "" or rs.is_done(view)
        assert rs.validate_session(ws) == []                       # reopened: consistent
    finally:
        w.close()


def test_a_pointer_to_a_run_that_is_gone_is_reset_on_load(tmp_path):
    project = tmp_path / "proj"
    ws = project / "rois" / "ws1"
    ws.mkdir(parents=True)
    (project / "project_manifest.json").write_text("{}")
    c = _correct_run(str(ws))
    rs.update_session(str(ws), viewing="rois/ws1/runs/segment_gone",
                      editing={"correct_run": rs.rel(str(project), c), "geometry_revision": 0})
    assert rs.validate_session(str(ws)) == ["viewing"]
    sess = rs.load_session(str(ws))
    assert sess["viewing"] is None and sess["editing"]["correct_run"] == rs.rel(str(project), c)


# ── Step4 along the chain; quant runs ───────────────────────────────────

def test_step4_writes_a_quant_run_on_its_segment_run(tmp_path):
    from test_quant_sources import build_project
    from block01.workers.feature_extract_worker import NEW_QUANT_RUN, run_extraction
    p = build_project(tmp_path)
    out = os.path.join(p["ws"], "runs", NEW_QUANT_RUN, "Full_WSI")
    res = run_extraction(p["run_dir"], out, roi_name="Full WSI")
    quant = os.path.dirname(res["output_dir"])
    assert rs.kind_of(quant) == "quant" and rs.is_done(quant)
    assert rs.upstream_of(quant) == p["run_dir"]
    assert os.path.isfile(res["h5ad"]) and res["h5ad"].startswith(quant)
    assert rs.read_params(quant)["outputs"]["h5ad"].endswith(".h5ad")
    assert not os.path.exists(os.path.join(p["ws"], "runs", NEW_QUANT_RUN))


def test_step4_reads_the_corrected_product_of_its_own_chain(tmp_path):
    """Two correct runs; the segment run on the first reads the first."""
    from test_quant_sources import build_project
    from block01.core import quant_sources as qs
    p = build_project(tmp_path)
    job = qs.resolve_quant_job(p["run_dir"], open_slide=p["slide"])
    assert job.step0["correct_run"] == os.path.realpath(p["step0"])
    assert job.step0["corrected_product"] == os.path.realpath(p["zarr"])


# ── drafts (§6) and the parameter file's paths (§16.3-6) ────────────────

def test_step2_and_step4_drafts_come_back_once(app, tmp_path, monkeypatch):
    w, ws = _rm_window(app, tmp_path)
    try:
        project = rs.project_dir_of(ws)
        rs.update_session(ws, editing={"correct_run": "x", "geometry_revision": 0})
        monkeypatch.setattr(w._step2, "draft", lambda: {
            "segmentation_config": {"method": "stardist_nuclei_dapi"},
            "param_file": os.path.join(ws, "settings", "segmentation_params", "p.json"),
            "parameter_source": "index"})
        monkeypatch.setattr(w._step4, "draft", lambda: {"statistics": ["mean"]})
        w._rm_save_page_drafts()
        drafts = rs.load_session(ws)["drafts"]
        assert drafts["step2"]["param_file"] == "rois/ws1/settings/segmentation_params/p.json"
        assert drafts["step4"]["edited_against"] == {"correct_run": "x", "geometry_revision": 0}
        got = {}
        monkeypatch.setattr(w._step2, "apply_draft", lambda d: got.setdefault("s2", d) or True)
        monkeypatch.setattr(w._step4, "apply_draft", lambda d: got.setdefault("s4", d) or True)
        w._rm_step2_draft_pending = w._rm_step4_draft_pending = True
        w._rm_save_page_drafts()                       # pending: nothing overwritten
        assert rs.load_session(ws)["drafts"]["step4"]["statistics"] == ["mean"]
        assert w._rm_restore_page_draft("step2", w._step2)
        assert w._rm_restore_page_draft("step4", w._step4)
        assert got["s2"]["param_file"] == os.path.join(
            project, "rois", "ws1", "settings", "segmentation_params", "p.json")
        assert not w._rm_restore_page_draft("step2", w._step2)       # once
    finally:
        w.close()


def test_the_parameter_file_names_the_corrected_product_relatively(app, tmp_path):
    w, ws = _rm_window(app, tmp_path)
    try:
        project = rs.project_dir_of(ws)
        c = _correct_run(ws)
        cfg = {"method": "cellpose_nuclei_hq",
               "hq_source_zarr": os.path.join(c, "corrected_channels.zarr"),
               "multichannel_source_path": os.path.join(c, "corrected_channels.zarr"),
               "raw_ome_path": "/data/slide.ome.tif",
               "params": {"hq_source_zarr": os.path.join(c, "corrected_channels.zarr")}}
        stored = rs.to_records(cfg, project)
        assert stored["hq_source_zarr"].startswith("rois/ws1/runs/correct_")
        assert stored["params"]["hq_source_zarr"].startswith("rois/ws1/runs/correct_")
        assert stored["raw_ome_path"] == "/data/slide.ome.tif"
        path = os.path.join(ws, "settings", "segmentation_params", "p.json")
        os.makedirs(os.path.dirname(path))
        json.dump(stored, open(path, "w"))
        from block01.workers.segment_merge_worker import SegmentMergeWorker
        ns = types.SimpleNamespace(param_file=path)
        got = SegmentMergeWorker._load_param_file_config(ns)
        assert got["hq_source_zarr"] == os.path.join(c, "corrected_channels.zarr")
    finally:
        w.close()


def test_object_tables_find_a_segment_run(tmp_path):
    from block01.core import object_tables as ot
    project = tmp_path / "proj"
    d = project / "rois" / "ws1" / "runs" / "segment_20261003_120000_x"
    d.mkdir(parents=True)
    assert ot._run_dir_of(str(project), "segment_20261003_120000_x") == str(d)


def test_trash_move_stops_at_the_first_failure(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    a, b, c = (project / "rois" / "w" / "runs" / n for n in ("a", "b", "c"))
    for d in (a, b, c):
        d.mkdir(parents=True)
    real, calls = os.rename, []

    def _rename(src, dst):
        calls.append(src)
        if len(calls) == 2:
            raise OSError("no")
        return real(src, dst)
    monkeypatch.setattr(trash.os, "rename", _rename)
    e = trash.move(str(project), "w", "runs", [str(a), str(b), str(c)])
    assert e["status"] == "partial" and [i["from"] for i in e["items"]] == ["rois/w/runs/a"]
    assert e["not_moved"] == ["rois/w/runs/b", "rois/w/runs/c"]
    assert not a.exists() and b.exists() and c.exists()
    assert not hasattr(trash, "resume_incomplete")                    # no resume (§9)


# ── codex RM-2 review 1 ────────────────────────────────────────────────────

def test_step4_of_a_project_run_always_writes_a_new_quant_run(tmp_path):
    from test_quant_sources import build_project
    from block01.workers.feature_extract_worker import run_extraction
    p = build_project(tmp_path)
    first = run_extraction(p["run_dir"], str(tmp_path / "anywhere"), roi_name="Full WSI")
    q1 = os.path.dirname(first["output_dir"])
    assert rs.kind_of(q1) == "quant" and rs.is_done(q1)
    before = os.path.getmtime(first["h5ad"])
    # typing the published quant run's folder writes ANOTHER quant run
    second = run_extraction(p["run_dir"], first["output_dir"], roi_name="Full WSI")
    q2 = os.path.dirname(second["output_dir"])
    assert q2 != q1 and os.path.getmtime(first["h5ad"]) == before
    assert not os.path.exists(tmp_path / "anywhere")


def test_a_fuse_run_brings_its_own_regions_to_step2(app, tmp_path, monkeypatch):
    w, ws = _rm_window(app, tmp_path)
    try:
        monkeypatch.setattr(w._step2, "_load_zarr_info", lambda: None)
        c = _correct_run(ws)
        f = _fuse_run(ws, c)
        rs.write_params(f, dict(rs.read_params(f), geometry={"regions": [
            {"roi_name": "R1", "bbox_fullres": [0, 10, 0, 10], "polygon_fullres": None}]}))
        w._step2.set_rois([{"name": "R1", "bbox_fullres": [5, 50, 5, 50]}])  # edited since
        w._corrected_zarr_path = os.path.join(c, "corrected_channels.zarr")
        w._rm_bind_step2()
        assert w._step2._rois == [{"name": "R1", "bbox_fullres": [0, 10, 0, 10],
                                   "polygon_fullres": None}]
    finally:
        w.close()


def test_a_step2_run_reads_the_corrected_product_of_its_own_chain(app, tmp_path):
    from block01.workers.segment_merge_worker import SegmentMergeWorker
    w, ws = _rm_window(app, tmp_path)
    try:
        c1 = _correct_run(ws)
        f1 = _fuse_run(ws, c1)
        c2 = _correct_run(ws, now=_t(5))
        ns = types.SimpleNamespace(
            zarr_path=os.path.join(f1, "fused_R1.zarr"), roi_dir=ws, param_file="",
            seg_config={"multichannel_source_path": os.path.join(c2, "corrected_channels.zarr")},
            _abs=os.path.abspath)
        ns._load_param_file_config = lambda: {}
        got = SegmentMergeWorker._multichannel_source_path(ns)
        assert got == os.path.join(c1, "corrected_channels.zarr")          # not C2
    finally:
        w.close()


def test_step2_refuses_a_fuse_run_of_another_workspace(app, tmp_path, monkeypatch):
    warned = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    w, ws = _rm_window(app, tmp_path)
    try:
        other = os.path.join(os.path.dirname(ws), "ws2")
        os.makedirs(other)
        open(os.path.join(other, "roi_manifest.json"), "w").write("{}")
        c = _correct_run(other)
        f = _fuse_run(other, c)
        step2 = w._step2
        step2._roi_dir = ws
        step2._zarr_path = os.path.join(f, "fused_R1.zarr")
        step2._run()
        assert warned and "this workspace" in warned[0] and step2._worker is None
    finally:
        w.close()


def test_a_restored_step2_draft_runs_as_shown(app, tmp_path):
    w, ws = _rm_window(app, tmp_path)
    try:
        step2 = w._step2
        assert step2.apply_draft({"segmentation_config": {"method": "stardist_nuclei_dapi"},
                                  "param_file": "/x/p.json", "parameter_source": "index"})
        assert step2._param_source_combo.currentData() == "manual"
        assert step2._method_combo.currentData() == "stardist_nuclei_dapi"
        assert step2._seg_param_file == ""
    finally:
        w.close()


def test_step2_runs_only_the_input_it_shows(app, tmp_path, monkeypatch):
    """codex RM-2 review 2: a full-image fuse run clears the regions; a
    fused.zarr of another chain browsed in by hand is not run under the
    chosen one's name and geometry."""
    warned = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    w, ws = _rm_window(app, tmp_path)
    try:
        monkeypatch.setattr(w._step2, "_load_zarr_info", lambda: None)
        c1 = _correct_run(ws)
        f1 = _fuse_run(ws, c1)
        c2 = _correct_run(ws, now=_t(5))
        f2 = _fuse_run(ws, c2, now=_t(6))
        w._step2.set_rois([{"name": "old", "bbox_fullres": [1, 2, 3, 4]}])
        w._corrected_zarr_path = os.path.join(c1, "corrected_channels.zarr")
        w._rm_bind_step2()
        assert w._step2._rois == []                                 # whole image
        w._step2._roi_dir = ws
        w._step2._zarr_path = os.path.join(f2, "fused_R1.zarr")     # typed in by hand
        w._step2._run()
        assert warned and "chosen there" in warned[0] and w._step2._worker is None
    finally:
        w.close()


# ── delete entries in each step's Load (user ruling 2026-10-03) ──────────

def test_load_plan_lists_preseg_runs_and_deletes_one(app, tmp_path, monkeypatch):
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        lambda *a, **k: QtWidgets.QMessageBox.Yes)
    w, ws = _rm_window(app, tmp_path)
    try:
        c = _correct_run(ws)
        pre = rs.new_run(ws, "preseg", now=_t(1))
        rs.write_inputs(pre, c)
        rs.publish(pre)
        seen = {}

        def _choose(self, entries, runs):
            seen["runs"] = list(runs)
            assert self._rm_delete_run(runs[0]) is True            # its ×
            return None
        monkeypatch.setattr(type(w), "_choose_preseg_plan", _choose)
        assert w._on_load_preseg_plan() is False
        assert seen["runs"] == [pre] and rs.list_runs(ws, "preseg") == []
    finally:
        w.close()


from test_v16_a6_workspace import page, slides  # noqa: E402,F401  (fixtures)


def test_step0_load_lists_each_workspaces_step0_results(page, tmp_path, slides):
    from test_v16_a6_workspace import _project
    from block01.utils import workspace_session as wsess
    proj, made = _project(tmp_path, slides["a"])
    ws = made[0]["roi_dir"]
    editing = rs.list_runs(ws, "correct")[0]
    older = rs.new_run(ws, "correct", now=datetime(2020, 1, 1))
    rs.publish(older)
    _sid, found = wsess.find_workspaces(proj, slides["a"])
    rows = page._workspace_rows(found)
    assert len(rows) == 3 and len(rows[0]) == 3                    # the workspace
    assert [r[3] for r in rows[1:]] == [editing, older]            # its Step0 results
    assert [r[2] for r in rows[1:]] == ["editing", ""]
    assert "Step0 result" in page._row_text(rows[1])
    deleted = []
    page.delete_run = lambda run, parent=None: deleted.append(run) or True
    # the × of a result row calls the window's deletion (here: recorded)
    assert page.delete_run(rows[2][3]) and deleted == [older]


def test_a_reopened_step1_gets_its_pre_segmentation_back(app, tmp_path, monkeypatch):
    """Acceptance 2026-10-03 #6: methods, ticks, the run's results and the
    result in use come back with the Step1 draft."""
    from block01.core import preseg_run
    w, ws = _rm_window(app, tmp_path)
    try:
        c = _correct_run(ws)
        pre = rs.new_run(ws, "preseg", now=_t(1))
        run = {"run_id": os.path.basename(pre), "tasks": [], "combos": [
            {"combo_id": "c1", "method": "stardist_nuclei_dapi", "params": {}}],
            "source": {"pixel_key": "px"}, "fusion": {"hash": "fh"}}
        preseg_run.write_run(rs.runs_dir(ws), run)
        rs.write_inputs(pre, c)
        rs.publish(pre)
        w._preseg_run = run
        w._preseg_selected = {"run": run, "combo_id": "c1"}
        state = w._rm_preseg_state()
        assert state["run_dir"] == pre and state["used_combo"] == "c1"
        w._preseg_run = w._preseg_selected = None
        w._params_source = "preseg"
        from block01.ui import main_window as mw
        monkeypatch.setattr(mw.preseg_run, "selectable", lambda *a: (True, ""))
        monkeypatch.setattr(w, "_preseg_current", lambda: ("px", "fh"))
        monkeypatch.setattr(w, "_refresh_preseg_results", lambda: None)
        w._params_source = mw.PRESEG_SOURCE
        assert w._rm_restore_preseg(state) is True
        assert w._preseg_run["run_id"] == os.path.basename(pre)
        assert w._preseg_selected["combo_id"] == "c1"
    finally:
        w.close()


def test_run_lists_are_as_wide_as_their_rows(app):
    from block01.ui.step3_mask_bar import Step3MaskBar
    bar = Step3MaskBar()
    long = "10-03 12:00 · stardist_nuclei_expansion · a long region name"
    bar.set_runs([(long, "/r/a\x1fR", "10-03 11:59 · fused · CD3 1, CD8 0.5", True)])
    fm = bar.run_combo.fontMetrics()
    assert bar.run_combo.view().minimumWidth() > fm.horizontalAdvance(long)


def test_closing_saves_the_step1_draft_with_its_pre_segmentation(app, tmp_path,
                                                                 monkeypatch):
    """Acceptance 2026-10-03 #6: Use, then close at once -- the draft holds the
    run and the result in use."""
    w, ws = _rm_window(app, tmp_path)
    try:
        monkeypatch.setattr(w, "_step1_session_payload", lambda: {
            "fusion_draft": {}, "preseg": {"run_dir": os.path.join(ws, "runs", "preseg_x"),
                                           "used_combo": "c1", "methods": [],
                                           "selected_ids": [1]}})
        w._rm_save_view(1)                                   # what closeEvent does first
        draft = rs.load_session(ws)["drafts"]["step1"]
        assert draft["preseg"]["used_combo"] == "c1"
        assert draft["preseg"]["run_dir"] == "rois/ws1/runs/preseg_x"   # relative
    finally:
        w.close()


def test_choosing_a_result_in_step3_brings_its_settings_to_step2(app, tmp_path,
                                                                 monkeypatch):
    """User ruling 2026-10-03 (#8, B): once per choice; Step2's own settings
    are kept as its draft first."""
    w, ws = _rm_window(app, tmp_path)
    try:
        project = rs.project_dir_of(ws)
        monkeypatch.setattr(w._step2, "_load_zarr_info", lambda: None)
        c = _correct_run(ws)
        f = _fuse_run(ws, c)
        s1 = _segment_run(ws, f, _t(2), method="stardist_nuclei_dapi")
        rs.write_params(s1, dict(rs.read_params(s1), segmentation_config={
            "method": "stardist_nuclei_dapi", "prob_thresh": 0.61}))
        saved = []
        monkeypatch.setattr(w, "_rm_save_page_drafts", lambda: saved.append(1))
        w._corrected_zarr_path = os.path.join(c, "corrected_channels.zarr")
        w._rm_note_viewing(s1)
        w._rm_bind_step2()
        assert w._step2._method_combo.currentData() == "stardist_nuclei_dapi"
        assert w._step2._param_source_combo.currentData() == "manual"
        assert saved == [1]                                  # Step2's draft kept first
        w._step2._seg_config = {"method": "edited by the user"}
        w._rm_bind_step2()                                   # the same choice again
        assert w._step2._seg_config == {"method": "edited by the user"}
        assert rs.load_session(ws)["viewing"] == rs.rel(project, s1)
    finally:
        w.close()


# ── codex RM-3 review ──────────────────────────────────────────────────────

def test_a_segment_runs_record_names_no_absolute_region_paths(app, tmp_path):
    w, ws = _rm_window(app, tmp_path)
    try:
        c = _correct_run(ws)
        f = _fuse_run(ws, c)
        cls, ns = _worker_stub(tmp_path, os.path.join(f, "fused_R1.zarr"))
        ns.roi_dir = ws
        ns.seg_config = {"method": "stardist", "region_fused_paths": {
            "R1": os.path.join(f, "fused_R1.zarr")}}
        _rid, ns.output_dir, _ = cls._create_output_dir(ns)
        cls._rm_publish_segment_run(ns)
        stored = json.load(open(os.path.join(ns.output_dir, "params.json")))
        assert "region_fused_paths" not in stored["segmentation_config"]
        assert str(tmp_path) not in json.dumps(stored)              # nothing absolute
    finally:
        w.close()


def test_trash_entries_are_purged_after_30_days_and_emptied_on_request(tmp_path):
    project = tmp_path / "proj"
    for n in ("a", "b"):
        (project / "rois" / "w" / "runs" / n).mkdir(parents=True)
    old = trash.move(str(project), "w", "runs", [str(project / "rois/w/runs/a")],
                     now=datetime(2026, 8, 1))
    new = trash.move(str(project), "w", "runs", [str(project / "rois/w/runs/b")],
                     now=datetime(2026, 10, 1))
    assert trash.purge(str(project), now=datetime(2026, 10, 3)) == [old["folder"]]
    assert os.path.isdir(new["folder"]) and trash.size(str(project)) >= 0
    assert trash.empty(str(project)) == [new["folder"]]
    assert trash.entries(str(project)) == []


def test_a_malformed_handoff_schema_is_refused(app, tmp_path):
    w, ws = _rm_window(app, tmp_path)
    try:
        manifest = os.path.join(ws, "settings", "step0", "step0_roi_result.json")
        json.dump({"handoff_schema_version": "not a number"}, open(manifest, "w"))
        w.step0_output = {"step0_manifest_path": manifest}
        assert w._load_step0_roi_result(auto=True) is False
        json.dump({"handoff_schema_version": 1}, open(manifest, "w"))   # an earlier version's
        assert w._load_step0_roi_result(auto=True) is False
    finally:
        w.close()


def test_fusion_settings_are_bound_to_the_correct_run_being_edited(app, tmp_path):
    """User ruling 2026-10-03 (RM-4, option C): only a new Step0 result (new
    pixels) changes what saved fusion settings are bound to."""
    w, ws = _rm_window(app, tmp_path)
    try:
        project = rs.project_dir_of(ws)
        manifest = os.path.join(ws, "settings", "step0", "step0_roi_result.json")
        json.dump({"handoff_schema_version": 2, "source_identity": {"dataset_path": "/s"}},
                  open(manifest, "w"))
        w.step0_output = dict(w.step0_output, step0_manifest_path=manifest)
        c1 = _correct_run(ws)
        w._corrected_zarr_path = os.path.join(c1, "corrected_channels.zarr")
        first = w._settings_binding()
        assert first["correct_run"] == rs.rel(project, c1)
        assert "manifest_digest" not in first                     # no digest any more
        json.dump({"handoff_schema_version": 2, "source_identity": {"dataset_path": "/s"},
                   "channel_remap_config_hash": "an Intensity-only Save"}, open(manifest, "w"))
        assert w._settings_binding() == first                     # same pixels: still bound
        c2 = _correct_run(ws, now=_t(9))
        w._corrected_zarr_path = os.path.join(c2, "corrected_channels.zarr")
        assert w._settings_binding()["correct_run"] == rs.rel(project, c2)
    finally:
        w.close()


# ── acceptance 2026-10-04 ──────────────────────────────────────────────────

_CELLPOSE = {"method": "cellpose_wholecell_fusion", "flow_threshold": 0.6,
             "cellprob_threshold": 0.3, "min_size": 22}


@pytest.mark.parametrize("before", ["stardist_nuclei_dapi", "cellpose_wholecell_fusion"])
def test_a_step2_draft_keeps_its_parameters(app, tmp_path, before):
    """#4: the parameters of a restored draft / a result chosen in Step3 were
    put back to the method's defaults (flow 0.6 came back as 0.4)."""
    w, _ws = _rm_window(app, tmp_path)
    try:
        step2 = w._step2
        step2._method_combo.setCurrentIndex(step2._method_combo.findData(before))
        assert step2.apply_draft({"segmentation_config": dict(_CELLPOSE)})
        assert step2._method_combo.currentData() == "cellpose_wholecell_fusion"
        assert step2._cp_flow.value() == pytest.approx(0.6)
        assert step2._cp_prob.value() == pytest.approx(0.3)
        assert step2._cp_minsize.value() == 22
        cfg = step2.get_seg_config()
        assert cfg["flow_threshold"] == pytest.approx(0.6)
        assert cfg["min_size"] == 22
    finally:
        w.close()


def _click_close(combo, row):
    from PyQt5 import QtCore
    from PyQt5.QtCore import Qt
    from PyQt5.QtTest import QTest
    combo.showPopup()
    QtWidgets.QApplication.processEvents()
    view = combo.view()
    rect = view.visualRect(combo.model().index(row, 0))
    QTest.mouseClick(view.viewport(), Qt.LeftButton, Qt.NoModifier,
                     QtCore.QPoint(rect.left() + 8, rect.center().y()))
    for _ in range(3):
        QtWidgets.QApplication.processEvents()
    return view


def test_the_x_in_step3s_run_list_deletes_that_run(app):
    """#6: in a drop-down the popup takes the mouse release, so the x acts on
    the press -- once, for its own row, without choosing the current row."""
    from block01.ui.step3_mask_bar import Step3MaskBar
    bar = Step3MaskBar()
    wanted, chosen = [], []
    bar.delete_requested.connect(wanted.append)
    bar.run_chosen.connect(chosen.append)
    bar.set_runs([("a", "/r/a", "f1", True), ("b", "/r/b", "f1", True)])
    host = QtWidgets.QWidget()
    QtWidgets.QHBoxLayout(host).addWidget(bar.corner())
    host.show()
    try:
        view = _click_close(bar.run_combo, 1)
        assert wanted == ["/r/b"]
        assert chosen == []
        assert not view.isVisible()
    finally:
        host.close()


def test_the_x_in_step2s_input_list_deletes_that_run(app, tmp_path):
    w, _ws = _rm_window(app, tmp_path)
    try:
        step2 = w._step2
        wanted = []
        step2.fuse_delete_requested.connect(wanted.append)
        step2.set_fuse_runs([{"run": "/r/f2", "label": "f2", "tag": "", "zarr": "",
                              "regions": {}, "rois": [], "tooltip": "f2"},
                             {"run": "/r/f1", "label": "f1", "tag": "", "zarr": "",
                              "regions": {}, "rois": [], "tooltip": "f1"}], "")
        step2.show()
        _click_close(step2._fuse_combo, 1)
        assert wanted == ["/r/f1"]
    finally:
        w.close()


def test_a_reopened_step2_shows_the_result_step3_is_using(app, tmp_path, monkeypatch):
    """#7 (user ruling 2026-10-04): on reopening, the result Step3 is using
    wins over the saved Step2 draft, which it replaces."""
    w, ws = _rm_window(app, tmp_path)
    try:
        monkeypatch.setattr(w._step2, "_load_zarr_info", lambda: None)
        c = _correct_run(ws)
        f = _fuse_run(ws, c)
        s = _segment_run(ws, f, _t(2), method="cellpose_wholecell_fusion")
        rs.write_params(s, dict(rs.read_params(s), segmentation_config=dict(_CELLPOSE)))
        rs.update_session(ws, editing={"correct_run": "x", "geometry_revision": 0})
        sess = rs.put_draft(rs.load_session(ws), "step2", {
            "segmentation_config": {"method": "stardist_nuclei_dapi"},
            "edited_against": {"correct_run": "x", "geometry_revision": 0}})
        rs.save_session(ws, sess)
        w._corrected_zarr_path = os.path.join(c, "corrected_channels.zarr")
        w._rm_note_viewing(s)
        w._rm_step2_draft_pending = True                    # an opened workspace
        w._rm_bind_step2()
        assert not w._rm_restore_page_draft("step2", w._step2)
        assert w._step2._method_combo.currentData() == "cellpose_wholecell_fusion"
        assert w._step2._cp_flow.value() == pytest.approx(0.6)
        w._rm_save_page_drafts()                            # the old draft is replaced
        draft = rs.load_session(ws)["drafts"]["step2"]["segmentation_config"]
        assert draft["method"] == "cellpose_wholecell_fusion"
    finally:
        w.close()


def test_a_new_step0_result_unsaves_the_fusion_settings_in_the_session(app, tmp_path):
    """RM-4 II.2: inside one session too, a new correct run (new pixels) makes
    the committed fusion settings unsaved; the same run (an Intensity-only
    Save) keeps them."""
    w, ws = _rm_window(app, tmp_path)
    try:
        manifest = os.path.join(ws, "settings", "step0", "step0_roi_result.json")
        json.dump({"handoff_schema_version": 2, "source_identity": {"dataset_path": "/s"}},
                  open(manifest, "w"))
        w.step0_output = dict(w.step0_output, step0_manifest_path=manifest)
        c1 = _correct_run(ws)
        w._corrected_zarr_path = os.path.join(c1, "corrected_channels.zarr")
        snapshot = {"version": 1, "hash": "h", "bound_to": w._settings_binding()}
        w._display.fusion.install_committed_snapshot(snapshot)
        assert not w._rm_check_settings_binding()            # same pixels
        assert w._committed_fusion_settings() is not None
        c2 = _correct_run(ws, now=_t(9))
        w._corrected_zarr_path = os.path.join(c2, "corrected_channels.zarr")
        assert w._rm_check_settings_binding()                # new pixels
        assert w._committed_fusion_settings() is None
    finally:
        w.close()


def test_a_correct_runs_geometry_names_its_folders_relatively(tmp_path):
    """F5: geometry.rois[].roi_dir in a correct run's params.json is
    project-relative."""
    from block01.ui.step0.step0_page import Step0Page
    proj = tmp_path / "proj"
    ws = proj / "rois" / "ws1"
    ws.mkdir(parents=True)
    (proj / "project_manifest.json").write_text("{}")
    (ws / "roi_manifest.json").write_text("{}")
    run = rs.new_run(str(ws), "correct", now=T0)
    fake = types.SimpleNamespace(_roi_context={}, _rm_pending_run=run)
    fake._rm_sigs = Step0Page._rm_sigs
    spec = {"config": {"channel_decisions": {}}, "analysis_region_type": "full_wsi",
            "rois": [{"name": "Full WSI", "roi_dir": str(ws)}], "patches": [],
            "raw_path": "/s.ome.tif"}
    Step0Page._rm_publish_correct_run(fake, run, spec)
    params = rs.read_params(run)
    assert params["geometry"]["rois"][0]["roi_dir"] == "rois/ws1"
    assert rs.is_done(run)


@pytest.mark.parametrize("original_still_there", [False, True])
def test_a_copied_project_reads_its_own_masks(app, tmp_path, original_still_there):
    """F6 (#8): Step2 records its products with absolute paths of the project
    it ran in; a copied / moved project reads its OWN copies (Step3 and Step4
    both go through step3_masks), whether or not the original is still there.
    Paths outside this workspace's runs are left alone."""
    from block01.core import step3_masks, quant_sources
    _w, ws = _rm_window(app, tmp_path)
    _w.close()
    c = _correct_run(ws)
    f = _fuse_run(ws, c)
    s = _segment_run(ws, f, _t(2), method="cellpose_wholecell_fusion")
    name = os.path.basename(s)
    old = f"/elsewhere/proj_old/rois/ws1/runs/{name}"
    if original_still_there:
        old = str(tmp_path / "proj_old" / "rois" / "ws1" / "runs" / name)
        os.makedirs(os.path.join(old, "global_mask_R1.zarr"))
    for sub in ("global_mask_R1.zarr", "global_nuclei_mask_R1.zarr", "label_pyramid_R1.zarr"):
        os.makedirs(os.path.join(s, sub))
    meta = {"run_id": name, "method": "cellpose_wholecell_fusion", "rois": [{
        "roi_name": "R1", "zarr_path": f"{old}/global_mask_R1.zarr",
        "paths": {"mask_zarr": f"{old}/global_mask_R1.zarr",
                  "raw_ome": "/data/slide.ome.tif"},
        "label_pyramid": {"cell": f"{old}/label_pyramid_R1.zarr", "nucleus": None},
        "label_store": {"complete": True,
                        "cell": {"path": f"{old}/global_mask_R1.zarr"},
                        "nucleus": {"path": f"{old}/global_nuclei_mask_R1.zarr"},
                        "nucleus_to_cell": {"path": f"{old}/missing_table.json"}}}]}
    with open(os.path.join(s, "segmentation_meta.json"), "w") as fh:
        json.dump(meta, fh)
    run = step3_masks.load_run(s)
    entry = run.meta["rois"][0]
    assert entry["paths"]["mask_zarr"] == os.path.join(s, "global_mask_R1.zarr")
    assert entry["zarr_path"] == os.path.join(s, "global_mask_R1.zarr")
    assert entry["label_pyramid"]["cell"] == os.path.join(s, "label_pyramid_R1.zarr")
    assert entry["label_store"]["nucleus"]["path"] == os.path.join(
        s, "global_nuclei_mask_R1.zarr")
    # not in this project either: left as recorded
    assert entry["label_store"]["nucleus_to_cell"]["path"] == f"{old}/missing_table.json"
    assert entry["paths"]["raw_ome"] == "/data/slide.ome.tif"
    store = quant_sources._label_store(run, "R1")
    assert store["cell"]["path"] == os.path.join(s, "global_mask_R1.zarr")
    listed = [r for r in step3_masks.list_runs(ws) if r.run_dir == os.path.realpath(s)]
    assert listed and listed[0].meta["rois"][0]["paths"]["mask_zarr"] == \
        os.path.join(s, "global_mask_R1.zarr")
    with open(os.path.join(s, "segmentation_meta.json")) as fh:  # file untouched
        assert json.load(fh) == meta
