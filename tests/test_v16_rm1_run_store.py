"""Block RM-1: run folders, their three files and the chain (run_store)."""

import json
import os
from datetime import datetime

import pytest

from block01.utils import run_store as rs


@pytest.fixture
def ws(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "project_manifest.json").write_text("{}")
    roi = project / "rois" / "roi_1"
    roi.mkdir(parents=True)
    return str(project), str(roi)


T0 = datetime(2026, 10, 3, 12, 0, 0)


def _publish(roi, kind, upstream=None, key=None, now=T0, summary=""):
    r = rs.new_run(roi, kind, now=now)
    rs.write_params(r, {"reuse_key": key or {}, "summary": summary})
    rs.write_inputs(r, upstream, slide_id="s1" if upstream is None else "")
    rs.publish(r)
    return r


def test_new_run_names_and_same_second(ws):
    _, roi = ws
    a = rs.new_run(roi, "correct", now=T0)
    b = rs.new_run(roi, "correct", now=T0)
    assert os.path.basename(a) == "correct_20261003_120000"
    assert os.path.basename(b) == "correct_20261003_120000-2"
    assert rs.kind_of(a) == "correct"
    with pytest.raises(ValueError):
        rs.new_run(roi, "version")


def test_a_run_exists_only_after_done(ws):
    _, roi = ws
    r = rs.new_run(roi, "correct", now=T0)
    rs.write_params(r, {})
    rs.write_inputs(r, None, slide_id="s1")
    assert rs.list_runs(roi) == []
    rs.publish(r)
    assert rs.list_runs(roi) == [r]
    assert rs.read_inputs(r) == {"upstream": "raw", "slide_id": "s1"}


def test_inputs_are_project_relative_and_chain_resolves(ws):
    project, roi = ws
    c = _publish(roi, "correct")
    f = _publish(roi, "fuse", upstream=c)
    s = _publish(roi, "segment", upstream=f)
    assert rs.read_inputs(f)["upstream"] == "rois/roi_1/runs/" + os.path.basename(c)
    assert rs.chain(s) == [s, f, c]
    assert rs.correct_run_of(s) == c
    assert rs.downstream_of(c) == [s, f]
    assert rs.list_runs(roi, "fuse", upstream=c) == [f]


def test_copied_project_resolves_inside_the_copy(ws, tmp_path):
    import shutil
    project, roi = ws
    c = _publish(roi, "correct")
    f = _publish(roi, "fuse", upstream=c)
    moved = str(tmp_path / "moved")
    shutil.move(project, moved)                     # the original is gone
    f2 = os.path.join(moved, "rois", "roi_1", "runs", os.path.basename(f))
    assert rs.upstream_of(f2) == os.path.join(moved, "rois", "roi_1", "runs",
                                              os.path.basename(c))


def test_rel_refuses_a_path_outside_the_project(ws, tmp_path):
    project, _ = ws
    with pytest.raises(ValueError):
        rs.rel(project, str(tmp_path / "elsewhere"))


def test_find_same_compares_upstream_and_key(ws):
    _, roi = ws
    c1 = _publish(roi, "correct")
    c2 = _publish(roi, "correct", now=datetime(2026, 10, 3, 12, 0, 1))
    f = _publish(roi, "fuse", upstream=c1, key={"kind": "fused", "w": 0.5})
    assert rs.find_same(roi, "fuse", c1, {"kind": "fused", "w": 0.5}) == f
    assert rs.find_same(roi, "fuse", c1, {"kind": "fused", "w": 0.8}) == ""
    assert rs.find_same(roi, "fuse", c2, {"kind": "fused", "w": 0.5}) == ""
    assert rs.find_same(roi, "fuse", c1, {"kind": "dapi_input", "w": 0.5}) == ""


def test_cleanup_removes_unfinished_runs_only(ws):
    _, roi = ws
    done = _publish(roi, "correct")
    stale = os.path.join(rs.runs_dir(roi), "fuse_20200101_000000")
    os.makedirs(stale)                              # a killed process left it
    removed = rs.cleanup_incomplete(roi)
    assert removed == [stale] and os.path.isdir(done)
    mine = rs.new_run(roi, "fuse", now=T0)
    rs.discard(mine)
    assert not os.path.exists(mine)
    rs.discard(done)                                # a published run is never discarded
    assert os.path.isdir(done)


def test_display_name_and_session(ws):
    _, roi = ws
    c = _publish(roi, "correct", summary="tophat r=50")
    assert rs.display_name(c) == "10-03 12:00 · tophat r=50"
    rs.update_session(roi, viewing="rois/roi_1/runs/x")
    rs.update_session(roi, editing={"correct_run": "a", "geometry_revision": 2})
    sess = json.load(open(rs.session_path(roi)))
    assert sess == {"viewing": "rois/roi_1/runs/x",
                    "editing": {"correct_run": "a", "geometry_revision": 2}}


def test_records_round_trip(ws, tmp_path):
    project, roi = ws
    rec = {"corrected_zarr_path": os.path.join(roi, "runs", "c", "x.zarr"),
           "raw_ome_path": str(tmp_path / "slide.ome.tif"),
           "last_save": {"output_dir": os.path.join(roi, "settings"), "w": 1},
           "name": "/not/a/path/key", "empty_dir": ""}
    stored = rs.to_records(rec, project)
    assert stored["corrected_zarr_path"] == "rois/roi_1/runs/c/x.zarr"
    assert stored["last_save"]["output_dir"] == "rois/roi_1/settings"
    assert stored["raw_ome_path"] == rec["raw_ome_path"]          # outside: absolute
    assert stored["name"] == "/not/a/path/key"
    assert rs.from_records(stored, project) == rec


# ═══ RM-1 acceptance (application §13, §16.5) ═══════════════════════════════

pytest.importorskip("PyQt5")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shutil  # noqa: E402

from PyQt5 import QtWidgets  # noqa: E402

from test_v16_a6_workspace import (  # noqa: E402,F401  (fixtures)
    app, slides, page, _project, _commit_step0, _tree)
from PyQt5 import QtCore  # noqa: E402


class _FakeWsi(QtCore.QThread):
    """WsiCorrectionWorker's contract (a QThread whose business `finished`
    shadows the base one); records what it was asked to do, runs nothing.
    (Moved here from the DV tests, removed with DV in block RM-3.)"""
    progress = QtCore.pyqtSignal(int, int, int, int, str, str, int)
    finished = QtCore.pyqtSignal(str, dict)
    canceled = QtCore.pyqtSignal(str)
    error = QtCore.pyqtSignal(str)
    made = []

    def __init__(self, loader, output_dir, config, rois=None, parent=None,
                 process_channels=None, incremental=False):
        super().__init__()
        self.output_dir, self.process_channels = output_dir, set(process_channels or ())
        self.incremental = incremental
        _FakeWsi.made.append(self)

    def stop_after_current_channel(self):
        pass

    def start(self):
        pass


@pytest.fixture
def dv_page(page, monkeypatch, tmp_path, slides):
    """The A6 page with Save's worker and dialog stubbed."""
    import block01.ui.step0.step0_page as sp
    _FakeWsi.made = []
    monkeypatch.setattr(sp, "WsiCorrectionWorker", _FakeWsi)
    monkeypatch.setattr(sp, "_WsiCorrectionProgressDialog",
                        lambda parent: type("D", (), {"cancel_requested": type(
                            "S", (), {"connect": lambda *a, **k: None})(),
                            "show": lambda self: None, "exec_": lambda self: 0,
                            "set_progress": lambda self, *a: None,
                            "allow_close": lambda self: None,
                            "accept": lambda self: None,
                            "reject": lambda self: None})())
    return page


def _prepared(page, ms=5000):
    """Block CS P2: a Save that copies a run, or hands the viewer off,
    finishes its preparation off the GUI thread; wait for it."""
    from PyQt5 import QtTest
    for _ in range(ms // 10):
        if page.__dict__.get("_save_prep") is None:
            return
        QtTest.QTest.qWait(10)
    raise AssertionError("the Save preparation did not finish")


def _sigs(page, config_mp, decisions):
    cfg = {"method_params": config_mp, "channel_params": {}}
    return {ch: page._save_signature(cfg, ch, m) for ch, m in decisions.items()}
import test_step1_fusion_isolation as iso  # noqa: E402
from block01.core import step0_handoff  # noqa: E402


def _handoff_run(ctx):
    with open(os.path.join(ctx["step_dirs"]["step0"], "step0_roi_result.json")) as f:
        return os.path.dirname(json.load(f)["corrected_zarr_path"])


def _published_tophat(tmp_path, slides, page):
    """A workspace whose handoff names a published correct run holding CD3 and
    CD8 corrected with TopHat r=25."""
    proj, made = _project(tmp_path, slides["a"], commit=False)
    ctx = made[0]
    _commit_step0(ctx, decisions={"CD3": "tophat", "CD8": "tophat"})
    run = _handoff_run(ctx)
    held = _sigs(page, {"tophat_radius": 25, "cucim_sigma": 30},
                 {"CD3": "tophat", "CD8": "tophat"})
    rs.write_params(run, {"kind": "correct", "corrected": page._rm_sigs(held)})
    with open(os.path.join(run, "corrected_channels.zarr", "marker.bin"), "wb") as f:
        f.write(b"published pixels")
    os.makedirs(os.path.join(run, "corrected_coarse.zarr"))
    return proj, ctx, run, held


def _quiet_emit(monkeypatch):
    """The A6 page fixture's fake `_emit_complete` commits a Step0 of its own
    (a new correct run); these tests count runs, so the emit records only."""
    from block01.ui.step0.step0_page import Step0Page
    emitted = []
    monkeypatch.setattr(Step0Page, "_emit_complete",
                        lambda self, config, zarr_path, decisions:
                        emitted.append(zarr_path) or True)
    return emitted


def _runs(ctx, kind="correct"):
    root = rs.runs_dir(ctx["roi_dir"])
    return sorted(n for n in os.listdir(root) if n.startswith(kind + "_"))


def test_a_correction_change_writes_a_new_run_and_leaves_the_old_one(
        dv_page, tmp_path, slides, monkeypatch):
    import block01.ui.step0.step0_page as sp
    proj, ctx, base, held = _published_tophat(tmp_path, slides, dv_page)
    dv_page._open_existing_workspace()
    monkeypatch.setattr(sp, "read_corrected_zarr_state",
                        lambda path: (dict(held), [(0, 1001, 0, 999)]))
    before = _tree(base)
    dv_page._channel_params = {"CD8": {"tophat_radius": 40}}     # CD8 25 -> 40
    dv_page._save_and_continue()
    _prepared(dv_page)
    w = _FakeWsi.made[-1]
    new = [n for n in _runs(ctx) if n != os.path.basename(base)]
    assert len(new) == 1
    new_dir = os.path.join(rs.runs_dir(ctx["roi_dir"]), new[0])
    assert w.output_dir == new_dir                                # never the published run
    assert w.incremental is True and w.process_channels == {"CD8"}
    assert os.path.isfile(os.path.join(new_dir, "corrected_channels.zarr", "marker.bin"))
    assert os.path.isdir(os.path.join(new_dir, "corrected_coarse.zarr"))
    assert not rs.is_done(new_dir)                               # published with the handoff
    assert _tree(base) == before                                  # the first run, byte for byte


def test_an_intensity_only_save_keeps_the_correct_run(dv_page, tmp_path, slides, monkeypatch):
    import block01.ui.step0.step0_page as sp
    from block01.ui.step0.step0_page import Step0Page
    proj, ctx, base, held = _published_tophat(tmp_path, slides, dv_page)
    dv_page._open_existing_workspace()
    monkeypatch.setattr(sp, "read_corrected_zarr_state",
                        lambda path: (dict(held), [(0, 1001, 0, 999)]))
    monkeypatch.setattr(Step0Page, "_intensity_settings_changed", lambda self: True)
    emitted = _quiet_emit(monkeypatch)
    dv_page._save_and_continue()
    _prepared(dv_page)
    assert _FakeWsi.made == []                                    # nothing recomputed
    assert _runs(ctx) == [os.path.basename(base)]                 # nothing copied, no new run
    assert emitted == [os.path.join(base, "corrected_channels.zarr")]   # the same run


def test_a_withdrawn_channel_is_a_new_run_of_the_same_pixels(dv_page, tmp_path, slides,
                                                             monkeypatch):
    import block01.ui.step0.step0_page as sp
    proj, ctx, base, held = _published_tophat(tmp_path, slides, dv_page)
    dv_page._open_existing_workspace()
    monkeypatch.setattr(sp, "read_corrected_zarr_state",
                        lambda path: (dict(held), [(0, 1001, 0, 999)]))
    dv_page._set_channel_decision("CD8", "original")
    _quiet_emit(monkeypatch)
    dv_page._save_and_continue()
    _prepared(dv_page)
    assert _FakeWsi.made == []                                    # no channel recomputed
    new = [n for n in _runs(ctx) if n != os.path.basename(base)]
    assert len(new) == 1
    assert os.path.isfile(os.path.join(rs.runs_dir(ctx["roi_dir"]), new[0],
                                       "corrected_channels.zarr", "marker.bin"))


def test_publishing_a_correct_run_freezes_its_parameters(dv_page, tmp_path, slides,
                                                         monkeypatch):
    import block01.ui.step0.step0_page as sp
    proj, ctx, base, held = _published_tophat(tmp_path, slides, dv_page)
    dv_page._open_existing_workspace()
    monkeypatch.setattr(sp, "read_corrected_zarr_state",
                        lambda path: (dict(held), [(0, 1001, 0, 999)]))
    dv_page._channel_params = {"CD8": {"tophat_radius": 40}}
    dv_page._save_and_continue()
    _prepared(dv_page)
    run = dv_page._rm_pending_run
    zarr_path = os.path.join(run, "corrected_channels.zarr")
    config = dv_page._clean_correction_config(dv_page._build_config())
    seen = {}

    def _write(spec, **kw):
        seen["spec"] = spec
        spec["publish_run"]()
        return {"config": spec["config"], "rois": spec["rois"], "patches": spec["patches"],
                "manifest": {}, "corrected_report": None}
    monkeypatch.setattr(step0_handoff, "write_handoff", _write)
    dv_page._write_step0_handoff(config, zarr_path)
    assert seen["spec"]["register_corrected"] is True             # A3 at publication
    assert rs.is_done(run) and dv_page._rm_pending_run is None
    params = rs.read_params(run)
    assert params["kind"] == "correct"
    assert params["corrected"]["CD8"][1] == 40                    # the frozen signature
    assert params["geometry"]["rois"] and params["correction_config"] == config
    assert rs.read_inputs(run)["upstream"] == "raw"


def test_a_cancelled_correction_leaves_no_run(dv_page, tmp_path, slides, monkeypatch):
    import block01.ui.step0.step0_page as sp
    proj, ctx, base, held = _published_tophat(tmp_path, slides, dv_page)
    dv_page._open_existing_workspace()
    monkeypatch.setattr(sp, "read_corrected_zarr_state",
                        lambda path: (dict(held), [(0, 1001, 0, 999)]))
    monkeypatch.setattr(sp.QMessageBox, "information", lambda *a, **k: None)
    dv_page._channel_params = {"CD8": {"tophat_radius": 40}}
    dv_page._save_and_continue()
    _prepared(dv_page)
    run = dv_page._rm_pending_run
    dv_page._wsi_dialog = None                                    # the test's dialog is a stub
    dv_page._on_wsi_canceled(os.path.join(run, "corrected_channels.zarr"))
    assert not os.path.exists(run) and _runs(ctx) == [os.path.basename(base)]


def test_an_old_project_is_neither_opened_nor_written(page, tmp_path, slides, monkeypatch):
    import block01.ui.step0.step0_page as sp
    proj = tmp_path / "proj"
    old = proj / "rois" / "roi_old" / "step0"
    old.mkdir(parents=True)
    (old / "step0_roi_result.json").write_text("{}")
    warned = []
    monkeypatch.setattr(sp.QMessageBox, "warning", lambda *a, **k: warned.append(a[1]))
    before = _tree(str(proj))
    assert page._open_existing_workspace() is None
    page._save_and_continue()
    assert warned == ["Not a project of this version"] * 2
    assert _tree(str(proj)) == before and page._seen["created"] == []


# ── the window: fuse runs, Step2's input, session.json ──────────────────────

def _rm_window(app, tmp_path):
    w = iso._window(app, tmp_path)
    proj = tmp_path / "proj"
    ws = proj / "rois" / "ws1"
    (ws / "settings" / "step0").mkdir(parents=True)
    (proj / "project_manifest.json").write_text("{}")
    (ws / "roi_manifest.json").write_text("{}")
    w.step0_output = dict(w.step0_output, roi_dir=str(ws), roi_id="ws1",
                          step0_dir=str(ws / "settings" / "step0"),
                          step1_dir=str(ws / "settings"))
    return w, str(ws)


def _correct_run(ws, now=T0):
    r = rs.new_run(ws, "correct", now=now)
    os.makedirs(os.path.join(r, "corrected_channels.zarr"))
    rs.write_params(r, {"kind": "correct", "corrected": {}})
    rs.write_inputs(r, None, slide_id="s1")
    rs.publish(r)
    return r


def _fuse_run(ws, upstream, regions=("R1",), now=T0, key=None, publish=True):
    r = rs.new_run(ws, "fuse", now=now)
    metas = []
    for name in regions:
        z = os.path.join(r, f"fused_{name}.zarr")
        os.makedirs(z)
        metas.append({"roi_name": name, "zarr_path": z})
    with open(os.path.join(r, "fusion_meta.json"), "w") as f:
        json.dump({"regions": metas}, f)
    if publish:
        rs.write_params(r, {"kind": "fuse", "reuse_key": key or {},
                            "regions": [{"roi_name": n, "zarr_name": f"fused_{n}.zarr"}
                                        for n in regions]})
        rs.write_inputs(r, upstream)
        rs.publish(r)
    return r


class _JobsWorker:
    def __init__(self, order):
        self.provenance_jobs = [("/f1", "/raw", "R1", (0, 1, 0, 1), ["CD3"])]
        self.order = order

    def _register_fused(self, *job):
        self.order.append(("a3", rs.is_done(self.run)))


def test_a_generate_publishes_its_fuse_run_then_registers_a3(app, tmp_path):
    w, ws = _rm_window(app, tmp_path)
    try:
        c = _correct_run(ws)
        run = _fuse_run(ws, c, regions=("R1", "R2"), publish=False)
        order = []
        worker = _JobsWorker(order)
        worker.run = run
        w._pending_fused_zarr_meta = {"artifact_kind": "step1_fused_zarr"}
        w._rm_pending_fuse = {"run": run, "upstream": c, "worker": worker, "params": {
            "kind": "fuse", "reuse_key": {"k": 1},
            "geometry": {"regions": [{"roi_name": "R1"}, {"roi_name": "R2"}]}}}
        first = w._rm_commit_fuse()
        assert first == os.path.join(run, "fused_R1.zarr")
        assert rs.is_done(run) and w._rm_pending_fuse is None
        assert [r["zarr_name"] for r in rs.read_params(run)["regions"]] == \
            ["fused_R1.zarr", "fused_R2.zarr"]                       # multi-ROI
        assert rs.upstream_of(run) == c
        assert order == [("a3", True)]                               # A3 after .done
        with open(os.path.join(run, "fusion_meta.json")) as f:     # relative records
            assert json.load(f)["regions"][0]["zarr_path"].startswith("rois/ws1/runs/")
    finally:
        w.close()


def test_a_region_without_its_product_publishes_nothing(app, tmp_path, monkeypatch):
    monkeypatch.setattr(QtWidgets.QMessageBox, "information", lambda *a, **k: None)
    w, ws = _rm_window(app, tmp_path)
    errors = []
    monkeypatch.setattr(w, "_on_fusion_error", lambda msg: errors.append(msg))
    try:
        c = _correct_run(ws)
        run = _fuse_run(ws, c, regions=("R1",), publish=False)        # R2 failed
        order = []
        worker = _JobsWorker(order)
        worker.run = run
        w._rm_pending_fuse = {"run": run, "upstream": c, "worker": worker, "params": {
            "kind": "fuse", "reuse_key": {},
            "geometry": {"regions": [{"roi_name": "R1"}, {"roi_name": "R2"}]}}}
        w._on_fusion_done("/whatever")
        assert errors and "could not be published" in errors[0]
        assert not os.path.exists(run) and order == []
        assert rs.list_runs(ws, "fuse") == []
    finally:
        w.close()


def test_the_same_generate_reuses_its_fuse_run(app, tmp_path, monkeypatch):
    w, ws = _rm_window(app, tmp_path)
    done = []
    monkeypatch.setattr(w, "_on_fusion_done", lambda path: done.append(path))
    try:
        c = _correct_run(ws)
        meta = {"artifact_kind": "step1_fused_zarr", "fusion_formula_version": 3,
                "fusion_config": {"groups": {"g": {"channels": {"CD3": 0.5}}},
                                  "saved_at": "12:00", "output_dir": "/x"},
                "source_path": "/somewhere", "config_hash": "abc"}
        key = w._rm_fuse_reuse_key(meta)
        f = _fuse_run(ws, c, key=key)
        later = dict(meta, fusion_config=dict(meta["fusion_config"], saved_at="13:00"),
                     source_path="/elsewhere", config_hash="def")
        same = rs.find_same(ws, "fuse", c, w._rm_fuse_reuse_key(later))
        assert same == f                                   # times and paths are not compared
        assert w._rm_reuse_fuse_run(same) is True and done == [os.path.join(f, "fused_R1.zarr")]
        dapi = dict(meta, artifact_kind="step1_dapi_input_zarr")
        assert rs.find_same(ws, "fuse", c, w._rm_fuse_reuse_key(dapi)) == ""   # other kind
        heavier = dict(meta, fusion_config={"groups": {"g": {"channels": {"CD3": 0.8}}}})
        assert rs.find_same(ws, "fuse", c, w._rm_fuse_reuse_key(heavier)) == ""
        c2 = _correct_run(ws, now=datetime(2026, 10, 3, 12, 5))
        assert rs.find_same(ws, "fuse", c2, key) == ""                 # another upstream
    finally:
        w.close()


def test_step2_takes_the_newest_fuse_run_of_the_correct_run_being_edited(app, tmp_path,
                                                                         monkeypatch):
    w, ws = _rm_window(app, tmp_path)
    try:
        c = _correct_run(ws)
        _fuse_run(ws, c)
        f2 = _fuse_run(ws, c, now=datetime(2026, 10, 3, 12, 1))
        w._corrected_zarr_path = os.path.join(c, "corrected_channels.zarr")
        monkeypatch.setattr(w._step2, "_load_zarr_info", lambda: None)
        w._rm_bind_step2()
        assert w._step2._zarr_edit.text() == os.path.join(f2, "fused_R1.zarr")
        assert not hasattr(w._step2, "set_dirty_draft")               # the DV gate is gone
        assert w._step2._region_fused_paths == {"R1": os.path.join(f2, "fused_R1.zarr")}
    finally:
        w.close()


def test_three_layers_of_step1_settings_survive_a_reopen(app, tmp_path, monkeypatch):
    """§13: F1 made with 0.5, Save Fusion Settings 0.8, draft 0.9."""
    w, ws = _rm_window(app, tmp_path)
    try:
        project = rs.project_dir_of(ws)
        c = _correct_run(ws)
        editing = {"correct_run": rs.rel(project, c), "geometry_revision": 0}
        rs.update_session(ws, editing=editing)
        f1 = _fuse_run(ws, c)
        rs.write_params(f1, dict(rs.read_params(f1), fusion_settings={"w": 0.5}))
        w._write_fusion_settings({"w": 0.8, "bound_to": {
            "manifest_path": os.path.join(ws, "settings", "step0", "step0_roi_result.json")}})
        monkeypatch.setattr(w, "_step1_session_payload", lambda: {
            "fusion_draft": {"w": 0.9}, "fusion_zarr_path": os.path.join(f1, "fused_R1.zarr")})
        w._save_step1_session()
        # reopened: three separate records
        assert rs.read_params(f1)["fusion_settings"] == {"w": 0.5}
        with open(os.path.join(ws, "settings", "step1_fusion_settings.json")) as f:
            saved = json.load(f)
        assert saved["w"] == 0.8
        assert saved["bound_to"]["manifest_path"] == \
            "rois/ws1/settings/step0/step0_roi_result.json"            # relative on disk
        draft, why = w._rm_read_step1_draft(rs.session_path(ws))
        assert why == "" and draft["fusion_draft"] == {"w": 0.9}
        assert draft["fusion_zarr_path"] == os.path.join(f1, "fused_R1.zarr")
        assert draft["edited_against"] == editing
    finally:
        w.close()


def test_viewing_history_does_not_change_what_is_edited(app, tmp_path, monkeypatch):
    """§13: edit on C2, look at C1's result, reopen: Step1 is still C2's draft;
    a draft made on C1 is kept but not restored on C2."""
    w, ws = _rm_window(app, tmp_path)
    try:
        project = rs.project_dir_of(ws)
        c1 = _correct_run(ws)
        f1 = _fuse_run(ws, c1)
        c2 = _correct_run(ws, now=datetime(2026, 10, 3, 12, 5))
        rs.update_session(ws, editing={"correct_run": rs.rel(project, c2),
                                       "geometry_revision": 0})
        monkeypatch.setattr(w, "_step1_session_payload", lambda: {"fusion_draft": {"w": 0.9}})
        w._save_step1_session()                                # the draft on C2
        w._rm_note_viewing(f1)                                 # looking at C1's history
        sess = rs.load_session(ws)
        assert sess["viewing"] == rs.rel(project, f1)
        assert sess["editing"]["correct_run"] == rs.rel(project, c2)
        draft, why = w._rm_read_step1_draft(rs.session_path(ws))
        assert why == "" and draft["fusion_draft"] == {"w": 0.9}
        rs.update_session(ws, editing={"correct_run": rs.rel(project, c1),
                                       "geometry_revision": 0})
        draft, why = w._rm_read_step1_draft(rs.session_path(ws))
        assert draft is None and "kept, not restored" in why
        assert rs.load_session(ws)["drafts"]["step1"]["fusion_draft"] == {"w": 0.9}
    finally:
        w.close()


def test_the_fuse_params_name_the_preseg_run_they_used(app, tmp_path):
    w, ws = _rm_window(app, tmp_path)
    try:
        from block01.ui.main_window import PRESEG_SOURCE
        pre = rs.new_run(ws, "preseg", now=T0)
        w._preseg_selected = {"run": {"run_id": os.path.basename(pre)}, "combo_id": "c1"}
        w._params_source = PRESEG_SOURCE
        w._rois = [{"name": "R1", "bbox_fullres": [0, 10, 0, 10]}]
        params = w._rm_fuse_params({"artifact_kind": "x"}, "m", True, {}, {}, {"a": 1},
                                   "/p/m_1.json")
        assert params["preseg_run"] == "rois/ws1/runs/" + os.path.basename(pre)
        assert params["segmentation_params"] == {"file": "m_1.json", "content": {"a": 1}}
    finally:
        w.close()


def test_a_preseg_run_is_marked_done_when_its_job_ends(tmp_path):
    from block01.ui.step1_presegmentation.run_job import PresegRunJob
    project = tmp_path / "proj"
    ws = project / "rois" / "ws1"
    ws.mkdir(parents=True)
    (project / "project_manifest.json").write_text("{}")
    run_dir = rs.new_run(str(ws), "preseg", now=T0)
    job = PresegRunJob(rs.runs_dir(str(ws)), {"run_id": os.path.basename(run_dir),
                                              "tasks": []}, lambda: None)
    job._main()                                                 # no task: ends at once
    assert os.path.isfile(os.path.join(run_dir, "params.json"))
    assert rs.is_done(run_dir)


def test_a_copied_project_reads_its_own_files_only(tmp_path):
    """§16.3-6: the original is moved away first, so a path into it cannot
    pass for a working copy."""
    project = tmp_path / "proj"
    ws = project / "rois" / "ws1"
    step0 = ws / "settings" / "step0"
    step0.mkdir(parents=True)
    (project / "project_manifest.json").write_text("{}")
    c = _correct_run(str(ws))
    manifest = {"roi_dir": str(ws), "step0_dir": str(step0), "step1_dir": str(ws / "settings"),
                "corrected_zarr_path": os.path.join(c, "corrected_channels.zarr"),
                "raw_ome_path": "/data/slide.ome.tif",
                "step0_roi_result_path": str(step0 / "step0_roi_result.json")}
    mpath = str(step0 / "step0_roi_result.json")
    with open(mpath, "w") as f:
        json.dump(step0_handoff.manifest_records(manifest, mpath), f)
    (step0 / "correction_config.json").write_text("{}")
    copy = tmp_path / "copy"
    shutil.copytree(project, copy)
    shutil.move(str(project), str(tmp_path / "gone"))           # the original is not there
    got = step0_handoff.published_handoff(str(copy / "rois" / "ws1" / "settings" / "step0"))
    m = got[4]
    assert got[2] == os.path.join(str(copy), "rois", "ws1", "runs", os.path.basename(c),
                                  "corrected_channels.zarr")
    assert os.path.isdir(got[2])
    for key in ("roi_dir", "step0_dir", "step1_dir", "step0_roi_result_path"):
        assert m[key].startswith(str(copy)), key
    assert m["raw_ome_path"] == "/data/slide.ome.tif"            # outside: as recorded


def test_opening_a_workspace_leaves_unfinished_runs_while_a_job_runs(page, tmp_path, slides):
    proj, made = _project(tmp_path, slides["a"])
    ws = made[0]["roi_dir"]
    stale = os.path.join(rs.runs_dir(ws), "preseg_20200101_000000")
    os.makedirs(stale)
    page.deletion_blocker = lambda: "a pre-segmentation run is running"
    page._open_existing_workspace()
    assert os.path.isdir(stale)                                  # maybe being written
    page.deletion_blocker = lambda: None
    page._open_existing_workspace()
    assert not os.path.exists(stale)


def test_step2_has_no_input_when_the_correct_run_has_no_fuse_run(app, tmp_path, monkeypatch):
    w, ws = _rm_window(app, tmp_path)
    try:
        c1 = _correct_run(ws)
        f1 = _fuse_run(ws, c1)
        monkeypatch.setattr(w._step2, "_load_zarr_info", lambda: None)
        w._corrected_zarr_path = os.path.join(c1, "corrected_channels.zarr")
        w._rm_bind_step2()
        assert w._step2._zarr_edit.text() == os.path.join(f1, "fused_R1.zarr")
        c2 = _correct_run(ws, now=datetime(2026, 10, 3, 12, 5))       # Step0 saved again
        w._corrected_zarr_path = os.path.join(c2, "corrected_channels.zarr")
        w._rm_bind_step2()
        assert w._step2._zarr_edit.text() == "" and w._step2._region_fused_paths == {}
        w._corrected_zarr_path = os.path.join(c1, "corrected_channels.zarr")
        f2 = _fuse_run(ws, c1, now=datetime(2026, 10, 3, 12, 9))      # a newer Generate
        w._rm_bind_step2()
        assert w._step2._zarr_edit.text() == os.path.join(f2, "fused_R1.zarr")
    finally:
        w.close()


def test_a_published_fuse_run_is_restored_whatever_the_settings_say_now(app, tmp_path,
                                                                        monkeypatch):
    w, ws = _rm_window(app, tmp_path)
    try:
        c = _correct_run(ws)
        f = _fuse_run(ws, c, regions=("full",))
        path = os.path.join(f, "fused_full.zarr")
        monkeypatch.setattr(w, "_is_valid_existing_fused_zarr", lambda p: True)
        monkeypatch.setattr(w, "_fused_zarr_body_identity", lambda p: {"complete": True})
        monkeypatch.setattr(w, "_expected_meta_for_current_config",
                            lambda: (_ for _ in ()).throw(AssertionError("not consulted")))
        w._active_roi = None
        w._corrected_zarr_path = os.path.join(c, "corrected_channels.zarr")
        assert w._restorable_fused_zarr(path) == path
        c2 = _correct_run(ws, now=datetime(2026, 10, 3, 12, 5))
        w._corrected_zarr_path = os.path.join(c2, "corrected_channels.zarr")
        assert w._restorable_fused_zarr(path) == ""             # another Step0 result
    finally:
        w.close()


def test_a_preseg_snapshot_is_written_relative_and_read_absolute(tmp_path):
    from block01.core import preseg_run
    project = tmp_path / "proj"
    ws = project / "rois" / "ws1"
    ws.mkdir(parents=True)
    (project / "project_manifest.json").write_text("{}")
    manifest = str(ws / "settings" / "step0" / "step0_roi_result.json")
    run = {"run_id": "preseg_20261003_120000", "tasks": [],
           "source": {"manifest_path": manifest, "raw_ome_path": "/data/s.ome.tif"}}
    d = preseg_run.write_run(rs.runs_dir(str(ws)), run)
    with open(os.path.join(d, "params.json")) as f:
        stored = json.load(f)
    assert stored["source"]["manifest_path"] == "rois/ws1/settings/step0/step0_roi_result.json"
    assert preseg_run.read_run(d)["source"] == run["source"]


def test_an_opened_workspace_does_not_overwrite_its_step1_draft_before_restoring_it(
        app, tmp_path, monkeypatch):
    w, ws = _rm_window(app, tmp_path)
    try:
        rs.update_session(ws, drafts={"step1": {"fusion_draft": {"w": 0.9},
                                                "edited_against": None}})
        monkeypatch.setattr(w, "_step1_session_payload", lambda: {"fusion_draft": {"w": 0}})
        w._rm_auto_step1 = True                     # opened, Step1 not entered yet
        w._save_step1_session()
        assert rs.load_session(ws)["drafts"]["step1"]["fusion_draft"] == {"w": 0.9}
        w._rm_auto_step1 = False                    # restored: autosave resumes
        w._save_step1_session()
        assert rs.load_session(ws)["drafts"]["step1"]["fusion_draft"] == {"w": 0}
    finally:
        w.close()


def test_a_draft_of_another_context_is_kept_not_overwritten(ws):
    _, roi = ws
    sess = {"drafts": {"step1": {"w": 0.9, "edited_against": {"correct_run": "c1"}}}}
    sess = rs.put_draft(sess, "step1", {"w": 0.1, "edited_against": {"correct_run": "c2"}})
    assert sess["drafts"]["step1"]["w"] == 0.1
    assert [k["w"] for k in sess["drafts"]["step1_kept"]] == [0.9]  # not dropped
    sess = rs.put_draft(sess, "step1", {"w": 0.2, "edited_against": {"correct_run": "c2"}})
    assert [k["w"] for k in sess["drafts"]["step1_kept"]] == [0.9]
    assert sess["drafts"]["step1"]["w"] == 0.2
    sess = rs.put_draft(sess, "step1", {"w": 0.3, "edited_against": {"correct_run": "c3"}})
    assert [k["w"] for k in sess["drafts"]["step1_kept"]] == [0.9, 0.2]   # C1 still there
    sess = rs.put_draft(sess, "step1", {"w": 0.4, "edited_against": {"correct_run": "c1"}})
    assert [k["w"] for k in sess["drafts"]["step1_kept"]] == [0.9, 0.2, 0.3]


def test_a_later_step0_save_keeps_the_step1_restore_pending(app, tmp_path, monkeypatch):
    w, ws = _rm_window(app, tmp_path)
    try:
        monkeypatch.setattr(w, "_load_step0_roi_result", lambda auto=False: True)
        w._on_step0_complete({"opened_workspace": True, "roi_dir": ws})
        assert w._rm_auto_step1 is True
        w._on_step0_complete({"roi_dir": ws})                       # a Save in Step0
        assert w._rm_auto_step1 is True                             # still to be restored
    finally:
        w.close()


def test_a_save_never_corrects_into_another_projects_run(dv_page, tmp_path, slides,
                                                        monkeypatch):
    """A handoff that names a correct run of ANOTHER project (a copy made with
    absolute paths) is not a base: the Save writes a new run of its own."""
    import block01.ui.step0.step0_page as sp
    proj, ctx, base, held = _published_tophat(tmp_path, slides, dv_page)
    other = tmp_path / "other" / "rois" / "x" / "runs" / "correct_20200101_000000"
    shutil.copytree(base, other)
    mpath = os.path.join(ctx["step_dirs"]["step0"], "step0_roi_result.json")
    with open(mpath) as f:
        manifest = json.load(f)
    manifest["corrected_zarr_path"] = str(other / "corrected_channels.zarr")
    with open(mpath, "w") as f:
        json.dump(manifest, f)
    dv_page._open_existing_workspace()
    monkeypatch.setattr(sp, "read_corrected_zarr_state", lambda path: ({}, []))
    before = _tree(str(other))
    _quiet_emit(monkeypatch)
    dv_page._save_and_continue()
    _prepared(dv_page)
    w = _FakeWsi.made[-1]
    assert os.path.dirname(w.output_dir) == rs.runs_dir(ctx["roi_dir"])   # its own run
    assert _tree(str(other)) == before                                    # never written
