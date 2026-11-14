"""Waits taken off the GUI thread (docs/v17_small_review_items_plan.md §3):

  * the Stop button imports no torch;
  * a retired fusion job is not waited for: it is kept until it physically
    ends, then released;
  * Regions' jobs are only asked to stop on close; the window refuses and
    retries by itself until they have ended, then closes;
  * a session that could not be fully saved is asked about ONCE, after every
    gate that retries has passed: Yes closes (no notice window left), No
    keeps the window and its notices working.
"""

import os
import sys
import threading
import types

import pytest

pytest.importorskip("block01.qt")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from block01.qt import QtCore, QtGui, QtTest, QtWidgets  # noqa: E402

from block01.ui import main_window as mw  # noqa: E402
from block01.ui.tma import workers as tma_workers  # noqa: E402
from block01.utils import error_hook  # noqa: E402
from test_step1_fusion_isolation import _window  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(autouse=True)
def _not_closing():
    yield
    error_hook._state["closing"] = False


class _Blocked(QtCore.QThread):
    """A thread that runs until it is let go."""

    def __init__(self):
        super().__init__()
        self.go = threading.Event()
        self.waited = []

    def run(self):
        self.go.wait(10)

    def stop(self):
        return None

    def wait(self, *a):
        self.waited.append(a)
        return super().wait(*a)


def _end(thread, app):
    thread.go.set()
    QtCore.QThread.wait(thread, 5000)
    for _ in range(5):
        app.processEvents()


# ── Stop ─────────────────────────────────────────────────────────────────

# ── the fusion job ───────────────────────────────────────────────────────

def _fusion_ns(worker):
    ns = types.SimpleNamespace(
        _fusion_token=object(), _fusion_worker=worker, _rm_pending_fuse=None,
        _fusion_exit_actions=[], _fusion_stopping=False, _retired_fusion_workers=[],
        _close_fusion_dialog=lambda: None)
    ns._drop_retired_fusion_worker = types.MethodType(
        mw.MainWindow._drop_retired_fusion_worker, ns)
    return ns


def test_a_retired_fusion_job_is_kept_not_waited_for(app):
    worker = _Blocked()
    worker.start()
    ns = _fusion_ns(worker)
    try:
        assert mw.MainWindow._retire_fusion_worker(ns, "test") is True
        assert worker.waited == [], "the GUI thread waited for the fusion job"
        assert ns._fusion_token is None and ns._fusion_worker is None
        assert ns._retired_fusion_workers == [worker]
    finally:
        _end(worker, app)
    assert ns._retired_fusion_workers == [], "kept after it physically ended"


def test_a_fusion_job_that_already_ended_is_not_kept(app):
    worker = _Blocked()
    worker.go.set()
    worker.start()
    QtCore.QThread.wait(worker, 5000)
    ns = _fusion_ns(worker)
    mw.MainWindow._retire_fusion_worker(ns, "test")
    assert ns._retired_fusion_workers == []


# ── Regions on close ─────────────────────────────────────────────────────

def test_regions_shutdown_with_no_wait_answers_at_once(app):
    job = _Blocked()
    job.start()
    tma_workers.LIVE.add(job) if isinstance(tma_workers.LIVE, set) else tma_workers.LIVE.append(job)
    try:
        assert tma_workers.shutdown(0) is False
        assert job.isRunning(), "a zero wait must not end the job"
    finally:
        _end(job, app)
        try:
            tma_workers.LIVE.remove(job)
        except (KeyError, ValueError):
            pass
    assert tma_workers.shutdown(0) is True


class _Regions:
    def __init__(self, busy):
        self.busy = busy
        self.calls = []

    def shutdown(self, timeout_ms=15000):
        self.calls.append(timeout_ms)
        return self.busy.pop(0) if self.busy else True


def _closes(w, monkeypatch):
    """Record each closeEvent's answer and the final acceptance."""
    answers, finalized = [], []
    real_close_event = type(w).closeEvent
    real_finalize = w._display.finalize_close

    def close_event(event):
        real_close_event(w, event)
        answers.append(event.isAccepted())
    monkeypatch.setattr(w, "closeEvent", close_event)
    monkeypatch.setattr(w._display, "finalize_close",
                        lambda reason: finalized.append(reason) or real_finalize(reason))
    return answers, finalized


def _run_until(app, done, seconds=8.0):
    timer = QtCore.QElapsedTimer()
    timer.start()
    while not done() and timer.elapsed() < seconds * 1000:
        QtTest.QTest.qWait(50)
    for _ in range(5):
        app.processEvents()


def test_close_during_a_regions_job_retries_by_itself(app, monkeypatch, tmp_path):
    """The REAL retry timer: one close by the user, the window closes by
    itself once the Regions job has ended."""
    w = _window(app, tmp_path)
    regions = _Regions([False, False])
    w._regions = regions
    saved = []
    monkeypatch.setattr(w, "_rm_save_view", lambda step: saved.append(step) or [])
    answers, finalized = _closes(w, monkeypatch)
    w.show()
    w.close()
    assert answers == [False] and regions.calls == [0], "Regions was waited for"
    assert saved == [], "nothing may be stopped or saved while Regions refuses"
    _run_until(app, lambda: bool(finalized))
    assert regions.calls == [0, 0, 0]
    assert answers[:2] == [False, False] and answers[-1] is not False
    assert finalized and not w.isVisible()


# ── one question about a session that was not saved ─────────────────────

def _failing_close(app, monkeypatch, tmp_path, answer):
    """A save that always fails, a fusion job alive for about a second (the
    window's REAL retry timer runs meanwhile), a real notifier with a notice
    already open; the user closes once."""
    w = _window(app, tmp_path)
    w._regions = None
    notifier = error_hook.Notifier()
    monkeypatch.setitem(error_hook._state, "notifier", notifier)
    monkeypatch.setitem(error_hook._state, "log",
                        error_hook.ErrorLog(tmp_path / "errors.log"))
    monkeypatch.setattr(w, "_rm_save_view", lambda step: ["the Step1 draft"])
    clock = QtCore.QElapsedTimer()
    clock.start()

    class _Job:
        def isRunning(self):
            return clock.elapsed() < 1200

    job = _Job()
    w._retired_fusion_workers = [job]
    monkeypatch.setattr(w, "_drop_retired_fusion_worker", lambda worker: (
        setattr(w, "_retired_fusion_workers", []) if not worker.isRunning() else None))
    asked = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: asked.append(a[2]) or answer))
    error_hook.report(ValueError("could not write"), "session")
    for _ in range(5):
        app.processEvents()
    assert isinstance(notifier._box, QtWidgets.QMessageBox) and notifier._box.isVisible()
    answers, finalized = _closes(w, monkeypatch)
    w.show()
    w.close()
    _run_until(app, lambda: bool(asked) and (bool(finalized) or answer != QtWidgets.QMessageBox.Yes))
    return w, notifier, asked, answers, finalized


def test_yes_is_asked_once_and_leaves_no_notice_window(app, monkeypatch, tmp_path):
    w, notifier, asked, answers, finalized = _failing_close(
        app, monkeypatch, tmp_path, QtWidgets.QMessageBox.Yes)
    assert len(answers) >= 3, "the live job did not make the window retry"
    assert answers[:-1] == [False] * (len(answers) - 1) and answers[-1] is not False
    assert len(asked) == 1, f"asked {len(asked)} times"
    assert "Step1 draft" in asked[0]
    assert finalized and not w.isVisible(), "the window did not close by itself"
    assert notifier._box is None, "a notice window outlived the accepted close"
    assert not [b for b in QtWidgets.QApplication.topLevelWidgets()
                if b.isVisible()], "a top-level window would keep the application alive"


def test_no_keeps_the_window_and_its_notices(app, monkeypatch, tmp_path):
    w, notifier, asked, answers, finalized = _failing_close(
        app, monkeypatch, tmp_path, QtWidgets.QMessageBox.No)
    QtTest.QTest.qWait(700)                                # no retry after No
    assert len(asked) == 1 and answers[-1] is False and not finalized
    assert w.isVisible() and error_hook._state["closing"] is False
    notifier._box.close()
    for _ in range(5):
        app.processEvents()
    error_hook.report(KeyError("later"), "after No")
    for _ in range(5):
        app.processEvents()
    assert isinstance(notifier._box, QtWidgets.QMessageBox), "notices stopped after No"
    notifier._box.close()
    for _ in range(5):
        app.processEvents()
    w._retired_fusion_workers = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.Yes))
    w.close()


def test_no_after_a_running_fusion_was_stopped_unlocks_step1(app, monkeypatch, tmp_path):
    """The close retires a RUNNING fusion job (its completion can then no
    longer unlock Step1); the user answers No to the failed save: the window
    that stays must not stay locked."""
    w = _window(app, tmp_path)
    w._regions = None
    monkeypatch.setattr(w, "_rm_save_view", lambda step: ["the Step1 draft"])
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.No))
    job = _Blocked()
    job.start()
    w._fusion_worker = job
    w._lock_ui()
    assert not w.config.isEnabled()
    answers, finalized = _closes(w, monkeypatch)
    w.show()
    QtCore.QTimer.singleShot(800, job.go.set)            # the job ends a moment later
    w.close()
    _run_until(app, lambda: bool(answers) and len(answers) >= 2
               and not job.isRunning() and w.config.isEnabled())
    assert not finalized and w.isVisible()
    assert w.config.isEnabled() and w.search.isEnabled(), "Step1 stayed locked after No"
    QtCore.QThread.wait(job, 5000)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.Yes))
    w.close()
