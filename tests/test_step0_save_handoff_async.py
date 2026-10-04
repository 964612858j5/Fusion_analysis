"""Block CS P2: Step0 Save prepares off the GUI thread -- the viewer's
hand-off (floor thread + scheduler drain) and the copy of the base correct
run -- and starts only when both are done. The synchronous
`suspend_for_production` that Compare relies on is unchanged."""
import os
import threading
import time
import types

import pytest
from PyQt5 import QtTest, QtWidgets

from block01.utils import run_store as rs


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


# ── the viewer: a hand-off that does not block ──────────────────────────────

class _Timer:
    def stop(self):
        pass


class _Scheduler:
    def __init__(self):
        self.callbacks = []

    def cancel_generation(self, _gen):
        pass

    def notify_when_idle(self, cb):
        self.callbacks.append(cb)


def _controller(floor_threads):
    from block01.viewer.explore_view import ExploreController
    view = types.SimpleNamespace(
        view_box=types.SimpleNamespace(setMouseEnabled=lambda *a: None),
        set_status_text=lambda *a: None)
    c = types.SimpleNamespace(
        _torn_down=False, _suspended=False, _settle_timer=_Timer(),
        _motion_timer=_Timer(), scheduler=_Scheduler(), view_generation=1,
        _settled_generation=2, _cancel_directional_prefetch=lambda: None,
        _overlay=None, _floor_threads=floor_threads, view=view,
        _suspend_badge_text=lambda: "paused")
    c._begin_suspend = types.MethodType(ExploreController._begin_suspend, c)
    c.begin = types.MethodType(ExploreController.begin_suspend_for_production, c)
    return c


def test_the_save_hand_off_returns_at_once_and_completes_later():
    release = threading.Event()
    floor = threading.Thread(target=release.wait, daemon=True)
    floor.start()
    c = _controller([floor])
    t0 = time.perf_counter()
    done, timings = c.begin("whole-slide correction (Save)")
    assert time.perf_counter() - t0 < 0.2              # the caller is not blocked
    assert c._suspended and not done.is_set()
    release.set()                                      # the floor computation ends
    for _ in range(100):
        if c.scheduler.callbacks:
            break
        time.sleep(0.01)
    assert not done.is_set()                           # ...the scheduler is still busy
    c.scheduler.callbacks[0]()                         # ...and reaches idle
    assert done.wait(2)
    assert "floor_join_ms" in timings and "scheduler_drain_ms" in timings


def test_a_cancelled_hand_off_never_reports_ready():
    release = threading.Event()
    floor = threading.Thread(target=release.wait, daemon=True)
    floor.start()
    stop = {"now": False}
    c = _controller([floor])
    done, _ = c.begin("whole-slide correction (Save)", cancelled=lambda: stop["now"])
    stop["now"] = True
    time.sleep(0.2)
    release.set()
    assert not done.wait(0.3)


def test_an_already_suspended_viewer_is_still_waited_for():
    """codex CS P2: "suspended" means the producers stopped, not that the
    work they started has finished."""
    release = threading.Event()
    floor = threading.Thread(target=release.wait, daemon=True)
    floor.start()
    c = _controller([floor])
    c._suspended = True
    done, _ = c.begin("whole-slide correction (Save)")
    assert not done.wait(0.2)
    release.set()
    for _ in range(100):
        if c.scheduler.callbacks:
            break
        time.sleep(0.01)
    c.scheduler.callbacks[0]()
    assert done.wait(2)


def test_a_torn_down_viewer_is_ready_at_once():
    c = _controller([])
    c._torn_down = True
    done, _ = c.begin("whole-slide correction (Save)")
    assert done.is_set()


def test_a_run_left_unfinished_by_a_close_is_removed_on_the_next_open(tmp_path):
    """Closing during the copy leaves an unpublished run (no .done); the
    existing RM rule removes it when the workspace is opened again."""
    proj = tmp_path / "proj"
    ws = proj / "rois" / "ws1"
    ws.mkdir(parents=True)
    (proj / "project_manifest.json").write_text("{}")
    (ws / "roi_manifest.json").write_text("{}")
    run = rs.new_run(str(ws), "correct")
    os.makedirs(os.path.join(run, "corrected_channels.zarr"))
    assert run in [os.path.join(str(ws), "runs", r) for r in
                   os.listdir(os.path.join(str(ws), "runs"))]
    rs.cleanup_incomplete(str(ws))
    assert not os.path.exists(run)


# ── the page: the copy of the base run off the GUI thread ───────────────────

class _Dialog:
    def __init__(self, *_a):
        self.cancel_requested = types.SimpleNamespace(connect=self._connect)
        self._cancel = []

    def _connect(self, fn):
        self._cancel.append(fn)

    def cancel(self):
        for fn in self._cancel:
            fn()

    def set_progress(self, *_a):
        pass

    def show(self):
        pass

    def allow_close(self):
        pass

    def accept(self):
        pass

    def reject(self):
        pass


def _page(app, tmp_path, monkeypatch):
    import block01.ui.step0.step0_page as sp
    from block01.ui.step0.step0_page import Step0Page
    monkeypatch.setattr(sp, "_WsiCorrectionProgressDialog", _Dialog)
    proj = tmp_path / "proj"
    ws = proj / "rois" / "ws1"
    ws.mkdir(parents=True)
    (proj / "project_manifest.json").write_text("{}")
    (ws / "roi_manifest.json").write_text("{}")
    base_run = rs.new_run(str(ws), "correct")
    base = os.path.join(base_run, "corrected_channels.zarr")
    os.makedirs(base)
    with open(os.path.join(base, "pixels.bin"), "wb") as f:
        f.write(b"x" * 1024)
    rs.publish(base_run)
    page = Step0Page()
    page._roi_context = {"roi_dir": str(ws)}
    return page, base, str(ws)


def _slow_copy(monkeypatch, seconds=0.5, fail=False):
    from block01.ui.step0.step0_page import Step0Page
    real = Step0Page._rm_copy_pixels

    def slow(base, dst):
        time.sleep(seconds)
        if fail:
            raise OSError("disk full")
        real(base, dst)
    monkeypatch.setattr(Step0Page, "_rm_copy_pixels", staticmethod(slow))


def _until(pred, ms=5000):
    for _ in range(ms // 10):
        if pred():
            return True
        QtTest.QTest.qWait(10)
    return False


def test_the_copy_runs_while_the_window_stays_responsive(app, tmp_path, monkeypatch):
    page, base, ws = _page(app, tmp_path, monkeypatch)
    _slow_copy(monkeypatch)
    got = []
    t0 = time.perf_counter()
    page._rm_prepare_save(base, True, None, got.append)
    assert time.perf_counter() - t0 < 0.3              # Save returned at once
    assert page.production_correction_busy() == "whole-slide correction (Save)"
    assert not page._btn_load.isEnabled()
    assert _until(lambda: got)
    zarr_path = got[0]
    assert os.path.isfile(os.path.join(zarr_path, "pixels.bin"))
    assert os.path.dirname(zarr_path) != os.path.dirname(base)   # a new run
    assert page.production_correction_busy() is None
    assert page._btn_load.isEnabled()


def test_cancel_while_preparing_leaves_no_run(app, tmp_path, monkeypatch):
    page, base, ws = _page(app, tmp_path, monkeypatch)
    _slow_copy(monkeypatch, 0.3)
    got = []
    page._rm_prepare_save(base, True, None, got.append)
    run = page._rm_pending_run
    page._wsi_dialog.cancel()
    assert _until(lambda: page.production_correction_busy() is None)
    QtTest.QTest.qWait(50)
    assert got == []
    assert not os.path.exists(run)
    assert page._btn_load.isEnabled()


def test_a_failed_copy_saves_nothing(app, tmp_path, monkeypatch):
    import block01.ui.step0.step0_page as sp
    page, base, ws = _page(app, tmp_path, monkeypatch)
    _slow_copy(monkeypatch, 0.1, fail=True)
    shown = []
    monkeypatch.setattr(sp.QMessageBox, "critical",
                        staticmethod(lambda *a, **k: shown.append(a[2])))
    got = []
    page._rm_prepare_save(base, True, None, got.append)
    run = page._rm_pending_run
    assert _until(lambda: page.production_correction_busy() is None)
    assert got == [] and shown and "disk full" in shown[0]
    assert not os.path.exists(run)


def test_a_save_waits_for_the_viewer_and_the_copy_together(app, tmp_path, monkeypatch):
    from block01.ui.step0.step0_page import Step0Page
    page, base, ws = _page(app, tmp_path, monkeypatch)
    _slow_copy(monkeypatch, 0.1)
    viewer_free = threading.Event()
    monkeypatch.setattr(Step0Page, "_begin_release_explore_for_production",
                        lambda self, reason, cancelled=None: (viewer_free, {}))
    got = []
    page._rm_prepare_save(base, True, "whole-slide correction (Save)", got.append)
    QtTest.QTest.qWait(400)                            # the copy is done by now
    assert got == []                                   # ...but the viewer is not free
    viewer_free.set()
    assert _until(lambda: got)


def test_nothing_slow_stays_synchronous(app, tmp_path, monkeypatch):
    page, base, ws = _page(app, tmp_path, monkeypatch)
    got = []
    page._rm_prepare_save(base, False, None, got.append)    # a new empty run, no copy
    assert len(got) == 1                                     # done before returning
    assert page.production_correction_busy() is None
