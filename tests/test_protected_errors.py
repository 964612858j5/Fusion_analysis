"""Errors a protected path catches are reported, not swallowed
(docs/v17_small_review_items_plan.md §4), on synthetic projects only:

  * `error_hook.report`: one log entry and one notice through the real
    notifier; before `install`, the log and stderr only; a notice queued
    before `set_closing(True)` is dropped at delivery and its reservation
    released; `set_closing(False)` shows notices again;
  * the session save on a step change / close: one failing write does not
    skip the others, and "nothing to write" is not a failure; a failing
    Step1 session write never raises;
  * Regions: an unreadable Step0 handoff is refused BEFORE the page changes;
    an empty hash registers no handoff;
  * a Step0 Save whose slide cannot be identified is published with the
    reason, and the user is told how to repair it;
  * the Intensity lock, provenance registration, the quantification pixel
    size: reported / recorded.
"""

import json
import os
import types

import pytest

pytest.importorskip("block01.qt")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from block01.qt import QtCore, QtWidgets  # noqa: E402

from block01.core import provenance as prov  # noqa: E402
from block01.core.regions import document, quant_apply  # noqa: E402
from block01.utils import error_hook  # noqa: E402
from block01.utils import run_store as rs  # noqa: E402
from test_v16_rm1_run_store import app, _rm_window  # noqa: E402,F401


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    monkeypatch.setitem(error_hook._state, "closing", False)
    monkeypatch.setitem(error_hook._state, "notifier", None)
    monkeypatch.setitem(error_hook._state, "log", error_hook.ErrorLog(tmp_path / "errors.log"))
    # closing a test window whose save was made to fail asks "Close anyway?"
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.Yes))
    yield


def _pump(app, n=5):
    for _ in range(n):
        app.processEvents()


@pytest.fixture
def reports(monkeypatch, app, tmp_path):
    """Records each report AND passes it to the real one; afterwards every
    report is in the log exactly once and a notice was shown."""
    notifier = error_hook.Notifier()
    error_hook._state["notifier"] = notifier
    asked = []
    real_ask = notifier.ask
    notifier.ask = lambda sig, text, logged: asked.append(text) or real_ask(sig, text, logged)
    seen = []
    real_report = error_hook.report

    def spy(exc, where="", **k):
        seen.append((where, exc))
        real_report(exc, where, **k)
    monkeypatch.setattr(error_hook, "report", spy)
    yield seen
    _pump(app)
    if not seen:
        assert not (tmp_path / "errors.log").exists() and asked == []
        return
    log = (tmp_path / "errors.log").read_text()
    for where in {w for w, _ in seen}:
        assert log.count(f"caught in {where}\n") == sum(1 for w, _ in seen if w == where), where
    assert log.count("--- ") == len(seen)
    # a notice was asked for (the test window's own close may already have
    # dismissed it: an accepted close closes open notices)
    assert asked and any(t.startswith(seen[0][0]) for t in asked)
    if isinstance(notifier._box, QtWidgets.QMessageBox):
        notifier._box.close()
        _pump(app)


# ── report / set_closing ────────────────────────────────────────────────

def test_report_logs_once_and_notices_once(app, tmp_path):
    notifier = error_hook.Notifier()
    error_hook._state["notifier"] = notifier
    try:
        raise OSError("disk full")
    except OSError as exc:
        error_hook.report(exc, "a write")
    _pump(app)
    log = (tmp_path / "errors.log").read_text()
    assert log.count("OSError: disk full") == 1 and "a write" in log
    box = notifier._box
    assert isinstance(box, QtWidgets.QMessageBox) and "a write" in box.text()
    box.close()
    _pump(app)
    assert notifier._box is None


def test_report_before_install_writes_the_log_and_stderr(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(error_hook._state, "log", None)
    monkeypatch.setattr(error_hook, "log_path", lambda: tmp_path / "home.log")
    error_hook.report(ValueError("early"), "a script")
    assert "ValueError: early" in (tmp_path / "home.log").read_text()
    assert "early" in capsys.readouterr().err


def test_a_notice_queued_before_the_close_is_dropped_and_released(app):
    notifier = error_hook.Notifier()
    error_hook._state["notifier"] = notifier
    error_hook.report(ValueError("late"), "queued")
    assert notifier._box == "pending"
    error_hook.set_closing(True)
    _pump(app)
    assert notifier._box is None, "the reservation was not released"
    error_hook.set_closing(False)                       # a refused close
    error_hook.report(ValueError("late"), "queued")     # the same error again
    _pump(app)
    assert isinstance(notifier._box, QtWidgets.QMessageBox)
    notifier._box.close()
    _pump(app)


def test_set_closing_closes_an_open_notice(app):
    notifier = error_hook.Notifier()
    error_hook._state["notifier"] = notifier
    error_hook.report(KeyError("open"), "shown")
    _pump(app)
    box = notifier._box
    assert box.isVisible()
    error_hook.set_closing(True)
    _pump(app)
    assert notifier._box is None and not box.isVisible()


# ── the session save ────────────────────────────────────────────────────

def test_one_failing_write_does_not_skip_the_others(app, tmp_path, monkeypatch, reports):
    w, ws = _rm_window(app, tmp_path)
    try:
        def boom():
            raise OSError("drafts")
        monkeypatch.setattr(w, "_rm_save_page_drafts", boom)
        failures = w._rm_save_view(3)
        assert "the Step2 / Step4 drafts" in failures
        assert [where for where, _ in reports] == ["session: the Step2 / Step4 drafts not saved"]
        # the write after it still happened
        assert rs.load_session(ws)["view"]["current_step"] == 3
    finally:
        w.close()


def test_nothing_to_write_is_not_a_failure(app, tmp_path, reports):
    from test_step1_fusion_isolation import _window
    w = _window(app, tmp_path)                          # no workspace at all
    try:
        assert w._rm_save_view(1) == []
        assert reports == []
    finally:
        w.close()


def test_a_failing_step1_session_write_never_raises(app, tmp_path, monkeypatch, reports):
    w, ws = _rm_window(app, tmp_path)
    try:
        def boom():
            raise RuntimeError("payload")
        monkeypatch.setattr(w, "_step1_session_payload", boom)
        failures = []
        w._save_step1_session(failures)                 # no exception
        assert failures == ["the Step1 draft"]
        assert reports and reports[0][0] == "Step1 session not saved"
        monkeypatch.setattr(w, "_step1_session_payload", lambda: {"fusion_draft": {}})
        monkeypatch.setattr(rs, "save_session", lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
        failures = []
        w._save_step1_session(failures)
        assert failures == ["the Step1 draft"]
    finally:
        w.close()


def test_the_step0_draft_reports_its_failure(app, tmp_path, monkeypatch, reports):
    w, ws = _rm_window(app, tmp_path)
    try:
        page = w._step0
        page._roi_context = {"roi_dir": ws}
        monkeypatch.setattr(rs, "save_session", lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
        failures = []
        assert page.rm_save_draft(failures) is False
        assert failures == ["the Step0 draft"]
        assert reports and reports[0][0] == "Step0 draft not saved"
    finally:
        w.close()


# ── Regions ─────────────────────────────────────────────────────────────

def test_an_empty_hash_registers_no_handoff():
    d = {"handoffs": {}}
    document.set_session(d, "ws1", "/x/step0_roi_result.json", "")
    assert d["handoffs"] == {}
    assert d["session"]["workspace_id"] == "ws1"
    document.set_session(d, "ws1", "/x/step0_roi_result.json", "abc")
    assert list(d["handoffs"]) == ["ws1|abc"]


def test_an_unreadable_handoff_is_refused_before_the_page_changes(app, tmp_path, monkeypatch):
    from test_step1_fusion_isolation import _window
    w = _window(app, tmp_path)                          # its handoff file does not exist
    try:
        monkeypatch.setattr(w, "_handoff_ready", lambda retry, refuse: "ready")
        refused, contexts = [], []
        monkeypatch.setattr(w, "_regions_refused", lambda text: refused.append(text))
        monkeypatch.setattr(w._regions, "set_context", lambda ctx: contexts.append(ctx))
        before = w._stack.currentWidget()
        w._go_to_regions()
        assert refused and "could not be read" in refused[0]
        assert w._stack.currentWidget() is before and contexts == []
    finally:
        w.close()


# ── Step0 Save without a slide identity ─────────────────────────────────

def _publish(tmp_path, monkeypatch, slide_id_of, manifest="{}"):
    from block01.ui.step0 import step0_page as sp
    proj = tmp_path / "proj"
    ws = proj / "rois" / "ws1"
    ws.mkdir(parents=True)
    (proj / "project_manifest.json").write_text(manifest)
    run = rs.new_run(str(ws), "correct")
    os.makedirs(os.path.join(run, "corrected_channels.zarr"))
    monkeypatch.setattr(sp.workspace_session, "slide_id_of", slide_id_of)
    warned = []
    later = []
    monkeypatch.setattr(QtCore.QTimer, "singleShot",
                        staticmethod(lambda ms, fn: later.append(fn)))
    page = types.SimpleNamespace(_roi_context={"project_dir": str(proj)},
                                 _rm_save_sigs={}, _rm_pending_run=run)
    page._rm_sigs = sp.Step0Page._rm_sigs                 # a staticmethod
    page._warn_slide_id_unavailable = lambda run, why: warned.append(why)
    spec = {"config": {}, "raw_path": str(tmp_path / "gone.ome.tiff"),
            "analysis_region_type": "full_wsi", "rois": [], "patches": []}
    sp.Step0Page._rm_publish_correct_run(page, run, spec)
    for fn in later:
        fn()
    return run, warned


def test_a_save_without_slide_identity_is_published_with_the_reason(tmp_path, monkeypatch, reports):
    run, warned = _publish(tmp_path, monkeypatch, lambda path, sources=None: None)
    assert rs.is_done(run), "the run must still be published"
    inputs = rs.read_inputs(run)
    assert inputs["slide_id"] == "" and "could not be identified" in inputs["slide_id_unavailable"]
    assert len(warned) == 1 and "could not be identified" in warned[0]


def test_an_identity_that_raises_is_reported_and_recorded(tmp_path, monkeypatch, reports):
    def boom(path, sources=None):
        raise OSError("unreadable")
    run, warned = _publish(tmp_path, monkeypatch, boom)
    assert rs.is_done(run)
    assert "OSError: unreadable" in rs.read_inputs(run)["slide_id_unavailable"]
    assert [where for where, _ in reports] == ["Step0 Save: slide identity"]
    assert len(warned) == 1


def test_an_unreadable_manifest_keeps_the_computed_id(tmp_path, monkeypatch, reports):
    def slide_id_of(path, sources=None):
        assert sources is None
        return "sid-1"
    run, warned = _publish(tmp_path, monkeypatch, slide_id_of, manifest="{not json")
    inputs = rs.read_inputs(run)
    assert inputs["slide_id"] == "sid-1" and "slide_id_unavailable" not in inputs
    assert warned == []
    assert [where for where, _ in reports] == ["Step0 Save: project manifest"]


# ── the smaller sites ───────────────────────────────────────────────────

def test_an_intensity_lock_that_fails_is_reported(reports):
    from block01.ui.block01_display import Block01DisplayServices

    class _Port:
        def apply_intensity_policy(self, editable):
            raise ValueError("lock")

    ns = types.SimpleNamespace(intensity_policy=lambda step: {"editable": False},
                               _intensity_panel=None, _intensity_content=_Port())
    Block01DisplayServices.apply_intensity_policy(ns, 1)
    assert [where for where, _ in reports] == ["Intensity lock not applied"]

    class _Gone:
        def apply_intensity_policy(self, editable):
            raise RuntimeError("wrapped C/C++ object has been deleted")

    ns._intensity_content = _Gone()
    Block01DisplayServices.apply_intensity_policy(ns, 1)        # a deleted widget stays quiet
    assert len(reports) == 1


def test_a_registration_that_fails_is_reported(monkeypatch, reports):
    monkeypatch.setattr(prov, "register", lambda *a, **k: (_ for _ in ()).throw(OSError("ro")))
    assert prov.safe_register("/nowhere", "fused", "loc", "tok") is None
    assert [where for where, _ in reports] == ["provenance: fused not registered"]


def test_an_unknown_pixel_size_says_why(tmp_path):
    job = types.SimpleNamespace(run_dir=str(tmp_path / "nowhere"), slide=str(tmp_path / "gone.tif"))
    pixel, why = quant_apply._pixel_size_and_why(job)
    assert pixel is None and why
    assert quant_apply._pixel_size(job) is None


def test_the_slide_warning_is_non_modal_and_tells_how_to_repair(app):
    from block01.ui.step0 import step0_page as sp
    parent = QtWidgets.QWidget()
    try:
        sp.Step0Page._warn_slide_id_unavailable(parent, "correct_x", "gone")
        boxes = [b for b in parent.findChildren(QtWidgets.QMessageBox)]
        assert len(boxes) == 1 and boxes[0].isVisible()
        assert boxes[0].windowModality() == QtCore.Qt.NonModal
        assert "Save Step0 once more" in boxes[0].text()
        boxes[0].close()
    finally:
        parent.deleteLater()


# ── through the REAL report: logged once, noticed once ──────────────────

def test_a_failing_site_logs_and_notices_through_the_real_report(app, tmp_path, monkeypatch):
    notifier = error_hook.Notifier()
    error_hook._state["notifier"] = notifier
    w, ws = _rm_window(app, tmp_path)
    try:
        page = w._step0
        page._roi_context = {"roi_dir": ws}
        monkeypatch.setattr(rs, "save_session",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
        failures = []
        assert page.rm_save_draft(failures) is False
        _pump(app)
        log = (tmp_path / "errors.log").read_text()
        assert log.count("OSError: disk full") == 1 and "Step0 draft not saved" in log
        assert isinstance(notifier._box, QtWidgets.QMessageBox)
        assert "Step0 draft not saved" in notifier._box.text()
        notifier._box.close()
        _pump(app)
    finally:
        monkeypatch.undo()
        w.close()


def test_a_pending_restore_and_unchanged_content_are_not_failures(app, tmp_path, monkeypatch,
                                                                  reports):
    w, ws = _rm_window(app, tmp_path)
    try:
        w._rm_auto_step1 = True                          # a restore still pending
        failures = []
        w._save_step1_session(failures)
        assert failures == []
        w._rm_auto_step1 = False
        monkeypatch.setattr(w, "_step1_session_payload", lambda: {"fusion_draft": {}})
        assert w._rm_save_view(1) == []
        assert w._rm_save_view(1) == [], "an unchanged session is not a failure"
        assert reports == []
    finally:
        w.close()


def test_fusion_end_still_unlocks_when_the_session_cannot_be_saved(app, tmp_path, monkeypatch,
                                                                   reports):
    from block01.ui import main_window as mw
    w, ws = _rm_window(app, tmp_path)
    try:
        def boom():
            raise RuntimeError("payload")
        monkeypatch.setattr(w, "_step1_session_payload", boom)
        monkeypatch.setattr(mw, "mark_roi_step",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("index")))
        w.step0_output = dict(w.step0_output, project_output_dir=str(tmp_path / "proj"))
        unlocked = []
        monkeypatch.setattr(w, "_unlock_ui", lambda: unlocked.append(True))
        monkeypatch.setattr(w, "_after_fusion_thread_exit", lambda fn: fn())
        monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                            staticmethod(lambda *a, **k: None))
        w._rm_pending_fuse = None
        w._on_fusion_done(str(tmp_path / "fused.zarr"))
        assert unlocked == [True], "the UI stayed locked"
        wheres = [where for where, _ in reports]
        assert "Step1: the ROI's step status not updated" in wheres
        assert "Step1 session not saved" in wheres
    finally:
        w.close()


def test_noting_the_run_being_viewed_reports_its_failure(app, tmp_path, monkeypatch, reports):
    w, ws = _rm_window(app, tmp_path)
    try:
        run = rs.new_run(ws, "fuse")
        rs.publish(run)
        monkeypatch.setattr(rs, "update_session",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("ro")))
        w._rm_note_viewing(run)
        assert [where for where, _ in reports] == ["Step1: session.json not updated"]
    finally:
        monkeypatch.undo()
        w.close()


def test_the_pixel_size_reason_reaches_the_provenance():
    import numpy as np

    def rin(pixel_um, why):
        return quant_apply.RegionsInput(
            record={}, snapshot={"identity": "x", "mode": "plain"}, snapshot_path="s",
            facts_path="f", core=np.zeros(0, np.int32), core_fraction=np.zeros(0, np.float32),
            flags=np.zeros(0, np.uint8), meas_bits=np.zeros(0, np.uint64), uids=[],
            channel_masks=np.zeros((0, 0), bool), pixel_um=pixel_um, pixel_size_unknown=why)
    unknown = quant_apply.provenance_summary(rin(None, "the slide records no physical pixel size"), {})
    assert unknown["pixel_size_um"] == []
    assert unknown["pixel_size_unknown"] == "the slide records no physical pixel size"
    known = quant_apply.provenance_summary(rin((0.5, 0.5), ""), {})
    assert "pixel_size_unknown" not in known


# ── every remaining site, each through the REAL report ──────────────────

@pytest.fixture
def real(app, tmp_path):
    """The real `report`: returns a check that the site logged once and asked
    for one notice."""
    notifier = error_hook.Notifier()
    error_hook._state["notifier"] = notifier
    asked = []
    real_ask = notifier.ask
    notifier.ask = lambda sig, text, logged: asked.append(text) or real_ask(sig, text, logged)

    def check(where, error_text):
        _pump(app)
        log = (tmp_path / "errors.log").read_text()
        assert log.count(error_text) == 1, log
        assert f"caught in {where}" in log
        assert len(asked) == 1 and asked[0].startswith(where), asked
        assert isinstance(notifier._box, QtWidgets.QMessageBox), "no notice was shown"
        notifier._box.close()
        _pump(app)
    return check


def _boom(exc):
    def raise_(*a, **k):
        raise exc
    return raise_


def test_site_shared_windows(app, tmp_path, monkeypatch, real):
    w, ws = _rm_window(app, tmp_path)
    try:
        page = w._step0
        page._roi_context = {"roi_dir": ws}
        page._shared_pending = None
        monkeypatch.setattr(page, "_records", _boom(KeyError("shared table")))
        failures = []
        assert page.rm_save_shared_windows(failures) is False
        assert failures == ["the shared Intensity windows"]
        real("shared Intensity not saved", "KeyError: 'shared table'")
    finally:
        monkeypatch.undo()
        w.close()


def test_site_interrupted_publish(app, tmp_path, monkeypatch, real):
    from block01.ui import main_window as mw
    w, ws = _rm_window(app, tmp_path)
    try:
        monkeypatch.setattr(mw.FullFusionWorker, "_recover_interrupted_publish",
                            staticmethod(_boom(OSError("rename"))))
        assert w._restorable_fused_zarr(str(tmp_path / "gone.zarr")) == ""
        real("Step1: an interrupted publish could not be recovered", "OSError: rename")
    finally:
        monkeypatch.undo()
        w.close()


def test_site_step0_roi_index(tmp_path, monkeypatch, real):
    import test_v16_artifact_graph as tag
    from block01.core import step0_handoff
    slide = tag._slide(tmp_path)
    project, ctx = tag._workspace(tmp_path, slide)
    monkeypatch.setattr(step0_handoff, "mark_roi_step", _boom(OSError("index")))
    result = tag._handoff(project, ctx, slide, decisions={})
    assert result                                        # the handoff is still published
    real("Step0: the ROI index not updated", "OSError: index")


def test_site_preseg_done(tmp_path, monkeypatch, real):
    from block01.ui.step1_presegmentation import run_job
    finished = []
    job = run_job.PresegRunJob(str(tmp_path / "runs"), {"run_id": "preseg_x", "tasks": []},
                               loader_factory=lambda: None, on_finished=finished.append)
    os.makedirs(job.rdir, exist_ok=True)
    monkeypatch.setattr(run_job.preseg_run, "write_run", lambda *a, **k: None)
    monkeypatch.setattr(run_job.run_store, "publish", _boom(OSError("done marker")))
    job._main()
    assert finished == [{}], "the run still ends and says so"
    real("pre-segmentation: the run's .done not written", "OSError: done marker")


@pytest.mark.parametrize("call, where", [
    (lambda p: prov.register_corrected_channels(p, p, p, p),
     "provenance: corrected channels not registered"),
    (lambda p: prov.register_fused(p, p, {"name": "R"}),
     "provenance: fused not registered"),
    (lambda p: prov.register_segmentation_run(p, {}),
     "provenance: segmentation run not registered"),
    (lambda p: prov.register_step4(p, types.SimpleNamespace(slide=p), {}),
     "provenance: Step4 result not registered"),
])
def test_site_provenance_siblings(tmp_path, monkeypatch, real, call, where):
    monkeypatch.setattr(prov, "ensure_raw_slide", _boom(OSError("registry")))
    monkeypatch.setattr(prov, "find_workspace", lambda path: (str(tmp_path), str(tmp_path)))
    monkeypatch.setattr(prov, "_load", lambda path: {"source_ome": "x"})
    result = call(str(tmp_path))
    assert result in (None, [])
    real(where, "OSError: registry")
