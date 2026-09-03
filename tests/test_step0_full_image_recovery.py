"""The full image comes back after the production run that suspended it.

Found on a live session (2026-09-03): with the full image open, Apply
released the viewer ("Background correction is running"), the run ended,
the placeholder still said running, and "Reopen full image" produced a
black canvas. The canvas was black because BOTH layer toggles were off and
looked identical to on; the placeholder was stale because nothing told the
view the run had ended. A production run no longer tears the viewer down
at all: it SUSPENDS the live stack and resumes it in place, so there is
nothing to rebuild and no flash. Three claims:

  1. the two layer toggles show their state (glyph + :checked rule), and
     the toolbar label says so when a layer -- or both -- is hidden;
  2. when the run that suspended the view ends -- however it ends, and only
     once no other run holds the GPU -- the view is resumed, and, if it is
     on screen, the current selection is re-applied to the live stack;
  3. the viewer suspends and resumes its controller in place; a dataset
     change or a teardown ends the suspension with the stack.

Own module, like the other full-image suites: the page-heavy Step0 modules
crash pyqtgraph offscreen when run after the background-correction module.
"""

import os
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtCore, QtTest, QtWidgets  # noqa: E402

from block01.ui.step0 import step0_explore_tab as et  # noqa: E402
from block01.ui.step0 import step0_page as sp  # noqa: E402

from test_step0_background_correction_tab import app  # noqa: E402,F401


# ── fakes ────────────────────────────────────────────────────────────────

class _RecordingTab:
    """Stands in for Step0ExploreTab on the page side."""

    def __init__(self, released=False):
        self.calls = []
        self.resume_calls = 0
        self.stack = None
        self.released = released

    def show_source(self, channel, method, params=(), *, viewport_l0=None,
                    tint=None, nucleus=None):
        self.calls.append({"channel": channel, "method": method,
                           "params": tuple(params), "viewport": viewport_l0})
        return True

    def resume_from_production(self):
        self.resume_calls += 1
        self.released = False

    def set_dataset(self, _path):
        pass

    def teardown(self, **_kw):
        pass


def _page(app, tab=None):
    page = sp.Step0Page()
    page.current_channel = "CD3"
    page.nucleus_channel = "DAPI"
    page._explore_tab = tab if tab is not None else _RecordingTab()
    return page


class _Run(QtCore.QThread):
    """A thread whose run() returns at once. A bare QThread's default run()
    is an event loop that never ends, so its `finished` never fires."""

    def run(self):
        pass


class _Ctl:
    def __init__(self):
        self.channel, self.method, self.params = "CD3", None, ()
        self.teardown_waits = []
        self.suspended = False
        self.suspend_reasons = []
        self.resume_calls = 0

    def suspend_for_production(self, reason):
        self.suspended = True
        self.suspend_reasons.append(reason)
        return {}

    def resume_from_production(self):
        self.suspended = False
        self.resume_calls += 1

    def set_tint(self, rgb):
        pass

    def set_selection(self, **kw):
        pass

    def jump_to(self, *a):
        pass

    def set_marker_visible(self, v):
        pass


def _tab_with_controller():
    """A real Step0ExploreTab over a fake stack with a suspendable
    controller."""
    made = {}

    class _Stack:
        def __init__(self, controller):
            self.controller = controller
            self.provider = self
            self.view = QtWidgets.QLabel("fake")
            self.caches = ()
            self.overlay = None
            self.torn_down = False

        def level_shape(self, _l):
            return (1000, 1000)

        def teardown(self, *, wait_for_floor=False):
            self.torn_down = True
            self.controller.teardown_waits.append(wait_for_floor)

    def factory(path, channel, parent_widget=None, **_kw):
        ctl = _Ctl()
        made["ctl"] = ctl
        stack = _Stack(ctl)
        made["stack"] = stack
        stack.view.setParent(parent_widget)
        return stack

    class _Page:
        current_channel = "CD3"

    return et.Step0ExploreTab(_Page(), stack_factory=factory), made


# ── 1. the toggles show their state ──────────────────────────────────────

def test_the_toggles_change_glyph_with_their_state(app):
    page = _page(app)

    page._btn_full_marker.setChecked(False)
    page._btn_full_nucleus.setChecked(False)
    assert page._btn_full_marker.text() == "○ Marker"
    assert page._btn_full_nucleus.text() == "○ DAPI"

    page._btn_full_marker.setChecked(True)
    page._btn_full_nucleus.setChecked(True)
    assert page._btn_full_marker.text() == "● Marker"
    assert page._btn_full_nucleus.text() == "● DAPI"


def test_the_toggles_have_a_checked_rule_in_their_stylesheet(app):
    """The shared toolbar style has no :checked rule, so under it on and
    off were indistinguishable. The layer toggles must not use it."""
    page = _page(app)
    for btn in (page._btn_full_marker, page._btn_full_nucleus):
        assert "QPushButton:checked" in btn.styleSheet(), btn.text()
    assert "QPushButton:checked" not in page._btn_full_reopen.styleSheet()


def test_the_label_says_which_layers_are_hidden(app):
    page = _page(app)
    page._enter_full_image("original")
    base = page._full_source_lbl.text()
    assert "hidden" not in base

    page._btn_full_nucleus.setChecked(False)
    assert page._full_source_lbl.text().startswith(base)
    assert "DAPI hidden" in page._full_source_lbl.text()

    page._btn_full_marker.setChecked(False)
    assert "both layers hidden" in page._full_source_lbl.text()

    page._btn_full_marker.setChecked(True)
    page._btn_full_nucleus.setChecked(True)
    assert page._full_source_lbl.text() == base


def test_reopening_keeps_the_hidden_layer_hint(app):
    """The hint survives `_show_full_image` rewriting the label: that is the
    moment the user sees the black canvas and reads the toolbar."""
    page = _page(app)
    page._btn_full_marker.setChecked(False)
    page._btn_full_nucleus.setChecked(False)

    page._enter_full_image("original")

    text = page._full_source_lbl.text()
    assert "CD3" in text and "Original" in text
    assert "both layers hidden" in text


# ── 2. the run ends -> the view resumes ──────────────────────────────────

def test_a_visible_suspended_view_is_resumed_and_its_selection_reapplied(app):
    """Resume first; then the current selection goes to the live stack --
    an Apply changed the parameters, and that is a `set_selection`, not a
    rebuild. The camera is not moved."""
    tab = _RecordingTab(released=True)
    page = _page(app, tab)
    page._preview_stack.setCurrentIndex(sp.PREVIEW_PAGE_FULL_IMAGE)

    page._on_production_worker_finished()

    assert tab.resume_calls == 1
    assert len(tab.calls) == 1
    assert tab.calls[0]["viewport"] is None
    assert tab.released is False


def test_a_hidden_suspended_view_is_resumed_without_being_re_shown(app):
    """Resuming is cheap wherever the view is -- the stack is live, nothing
    is read -- so the hidden view is unlocked too. Re-applying the
    selection is left to the next time it is shown."""
    tab = _RecordingTab(released=True)
    page = _page(app, tab)
    page._preview_stack.setCurrentIndex(sp.PREVIEW_PAGE_COMPARE)

    page._on_production_worker_finished()

    assert tab.resume_calls == 1
    assert tab.calls == []


def test_nothing_happens_while_another_run_still_holds_the_gpu(
        app, monkeypatch):
    tab = _RecordingTab(released=True)
    page = _page(app, tab)
    page._preview_stack.setCurrentIndex(sp.PREVIEW_PAGE_FULL_IMAGE)
    monkeypatch.setattr(page, "production_correction_busy",
                        lambda: "on-demand background correction")

    page._on_production_worker_finished()

    assert tab.resume_calls == 0 and tab.calls == []


def test_a_view_that_was_never_suspended_is_left_alone(app):
    """The placeholder may be up because nothing was ever opened, or because
    a dataset switch tore the stack down; a finishing run is not a reason
    to build or to resume in either case."""
    tab = _RecordingTab(released=False)
    page = _page(app, tab)
    page._preview_stack.setCurrentIndex(sp.PREVIEW_PAGE_FULL_IMAGE)

    page._on_production_worker_finished()

    assert tab.resume_calls == 0 and tab.calls == []


def test_the_reopen_button_does_not_move_the_camera(app):
    tab = _RecordingTab()
    page = _page(app, tab)

    page._reopen_full_image()

    assert tab.calls[-1]["viewport"] is None


def test_a_finishing_worker_thread_reaches_the_page_on_the_gui_thread(app):
    """`QThread.finished` is the signal watched -- it fires once the thread
    is no longer running, whatever way the run ended -- and the slot must
    run on the GUI thread, because it touches widgets."""
    tab = _RecordingTab(released=True)
    page = _page(app, tab)
    page._preview_stack.setCurrentIndex(sp.PREVIEW_PAGE_FULL_IMAGE)
    seen = []
    real = page._on_production_worker_finished

    def spy():
        seen.append(QtCore.QThread.currentThread() is app.thread())
        real()

    page._on_production_worker_finished = spy
    worker = _Run()
    page._watch_production_worker(worker)

    worker.start()
    assert worker.wait(5000)
    QtTest.QTest.qWait(100)

    assert seen == [True]
    assert tab.resume_calls == 1


def test_a_worker_from_a_previous_dataset_is_ignored(app):
    """The watch goes through `_gen_slot`: a run of dataset A finishing
    after the page moved to dataset B must not touch anything."""
    tab = _RecordingTab(released=True)
    page = _page(app, tab)
    page._preview_stack.setCurrentIndex(sp.PREVIEW_PAGE_FULL_IMAGE)
    worker = _Run()
    page._watch_production_worker(worker)
    page._dataset_gen += 1

    worker.start()
    assert worker.wait(5000)
    QtTest.QTest.qWait(100)

    assert tab.resume_calls == 0 and tab.calls == []


# ── 3. the viewer suspends and resumes its controller in place ───────────

def test_release_suspends_the_controller_and_keeps_the_stack(app):
    tab, made = _tab_with_controller()
    tab.set_dataset("/data/a.ome.tif")
    tab.show_source("CD3", None, ())
    assert tab.released is False

    tab.release_for_production("patch background correction")

    assert tab.released is True
    assert tab.stack is made["stack"]
    assert made["ctl"].suspended is True
    assert made["ctl"].suspend_reasons == ["patch background correction"]
    assert made["stack"].torn_down is False
    assert tab._layout.indexOf(made["stack"].view) >= 0
    tab.teardown()


def test_resume_reverses_the_release_in_place(app):
    tab, made = _tab_with_controller()
    tab.set_dataset("/data/a.ome.tif")
    tab.show_source("CD3", None, ())
    tab.release_for_production("patch background correction")

    tab.resume_from_production()

    assert tab.released is False
    assert made["ctl"].suspended is False
    assert made["ctl"].resume_calls == 1
    assert tab.stack is made["stack"]
    assert tab.build_attempts == 1
    tab.teardown()


def test_a_dataset_change_ends_the_suspension_with_the_stack(app):
    tab, made = _tab_with_controller()
    tab.set_dataset("/data/a.ome.tif")
    tab.show_source("CD3", None, ())
    tab.release_for_production("patch background correction")

    tab.set_dataset("/data/b.ome.tif")

    assert tab.released is False
    assert made["stack"].torn_down is True
    tab.teardown()


def test_a_teardown_ends_the_suspension(app):
    tab, made = _tab_with_controller()
    tab.set_dataset("/data/a.ome.tif")
    tab.show_source("CD3", None, ())
    tab.release_for_production("patch background correction")

    tab.teardown()

    assert tab.released is False
    assert made["stack"].torn_down is True


def test_no_placeholder_talks_about_a_released_view(app):
    """There is no "released" or "finished" placeholder any more: the view
    never leaves the screen for a production run."""
    names = [n for n in dir(et) if n.startswith("PLACEHOLDER_")]
    assert "PLACEHOLDER_RELEASED" not in names
    assert "PLACEHOLDER_RUN_FINISHED" not in names
    for n in names:
        text = getattr(et, n).lower()
        assert "released" not in text, n


# ── 4. the real Save path: resume only once the thread has exited ────────
#
# `WsiCorrectionWorker` declares `finished = pyqtSignal(str, dict)`, which
# SHADOWS `QThread.finished`. That business signal is emitted from inside
# `run()`, while `isRunning()` is still true, so a resume started from it
# would be refused by the busy gate and never retried. The watcher must
# therefore bind the base class's signal explicitly, and this module proves
# the difference rather than trusting the spelling.

def _wait_until(predicate, deadline_ms=5000):
    """Pump the event loop until `predicate()` or the deadline. No fixed
    sleeps: the loop stops as soon as the condition holds."""
    elapsed = 0
    step = 10
    while elapsed < deadline_ms:
        if predicate():
            return True
        QtTest.QTest.qWait(step)
        elapsed += step
    return predicate()


def _drive_real_save(app, monkeypatch, tmp_path, tab):
    """Drive the REAL `_save_and_continue()` with a whole-slide worker whose
    business `finished(str, dict)` shadows `QThread.finished`, and record
    the real handlers on one timeline.

    Nothing about the ordering is simulated: `_on_wsi_finished` and
    `_apply_corrected_store` are the page's own methods, wrapped only to
    note when they ran.
    """
    from block01.ui.step0 import step0_page as mod
    from block01.ui.step0.step0_page import Step0Page
    from test_step0_background_correction_tab import _GpuPathLoader

    timeline = []

    class _FakeWsiWorker(QtCore.QThread):
        # Mirrors the real worker's signal surface, including the business
        # `finished(str, dict)` that SHADOWS `QThread.finished`.
        finished = QtCore.pyqtSignal(str, dict)
        progress = QtCore.pyqtSignal(int, int, int, int, str, str, int)
        canceled = QtCore.pyqtSignal(str)
        error = QtCore.pyqtSignal(str)

        def __init__(self, *_a, **_kw):
            super().__init__()
            self.may_exit = threading.Event()
            self.emitted = threading.Event()

        def run(self):
            # Emitted from INSIDE run(), exactly like the real worker:
            # isRunning() is still true here.
            self.finished.emit("/tmp/corrected.zarr", {"CD3": "tophat"})
            self.emitted.set()
            self.may_exit.wait(10)

        def stop_after_current_channel(self):
            pass

    class _Signal:
        def connect(self, *_a, **_k):
            pass

    class _Dialog:
        cancel_requested = _Signal()

        def __init__(self, *_a, **_k):
            pass

        def exec_(self):
            timeline.append("dialog.exec_")

        def show(self):
            timeline.append("dialog.show")

        def set_progress(self, *_a, **_k):
            pass

        def allow_close(self):
            pass

        def accept(self):
            pass

        def reject(self):
            pass

    class _Msg:
        @staticmethod
        def information(*a, **k):
            timeline.append(f"dialog:information:{a[2] if len(a) > 2 else ''}")

        @staticmethod
        def warning(*a, **k):
            timeline.append(f"dialog:warning:{a[2] if len(a) > 2 else ''}")

        @staticmethod
        def critical(*a, **k):
            timeline.append(f"dialog:critical:{a[2] if len(a) > 2 else ''}")

        @staticmethod
        def question(*a, **k):
            timeline.append("dialog:question")
            return getattr(mod.QMessageBox, "Yes", 16384)

    monkeypatch.setattr(mod, "QMessageBox", _Msg)
    monkeypatch.setattr(mod, "WsiCorrectionWorker", _FakeWsiWorker)
    monkeypatch.setattr(mod, "_WsiCorrectionProgressDialog", _Dialog)

    page = sp.Step0Page()
    page.loader = _GpuPathLoader()
    page.ome_path = str(tmp_path / "fake.ome.tif")
    page.output_dir = str(tmp_path)
    page.patches = [(0, 32, 0, 32)]
    page.current_patch_idx = 0
    page.nucleus_channel = "DAPI"
    page._rebuild_channel_list()
    page.current_channel = "CD3"
    page._analysis_region_mode = "full_wsi"
    page._explore_tab = tab
    row = page._channel_rows.get("CD3")
    if row is not None:
        row["checkbox"].setChecked(True)
        row["method_cb"].setCurrentText("TopHat")

    # Wrap the REAL handlers, do not replace them.
    real_wsi_finished = page._on_wsi_finished
    real_apply = page._apply_corrected_store
    real_physical = page._on_production_worker_finished

    def wsi_finished(config, zarr_path, decisions):
        timeline.append("logical_finished")
        return real_wsi_finished(config, zarr_path, decisions)

    def apply_store(zarr_path, decisions, only=None):
        timeline.append("apply_corrected_store")
        return real_apply(zarr_path, decisions, only=only)

    def physical():
        timeline.append("physical_finished")
        before = tab.resume_calls
        real_physical()
        if tab.resume_calls > before:
            timeline.append("resume")

    page._on_wsi_finished = wsi_finished
    page._apply_corrected_store = apply_store
    page._on_production_worker_finished = physical
    # `_emit_complete` writes the Step0 hand-off to disk; not this test's
    # subject, and it would need a full project tree.
    monkeypatch.setattr(Step0Page, "_emit_complete",
                        lambda self, *a, **k: timeline.append("emit_complete"))
    monkeypatch.setattr(Step0Page, "_release_explore_for_production",
                        lambda self, reason: timeline.append("release"))

    page._preview_stack.setCurrentIndex(sp.PREVIEW_PAGE_FULL_IMAGE)
    page._save_and_continue()
    return page, timeline


def test_the_real_save_path_resumes_only_after_the_thread_exits(
        app, monkeypatch, tmp_path):
    """The whole chain, driven: the business signal and the store swap run
    while the thread is STILL running, and the resume happens only after it
    physically exits -- the case the shadowed signal name breaks."""
    tab = _RecordingTab(released=True)
    watched = []
    real_watch = sp.Step0Page._watch_production_worker
    monkeypatch.setattr(
        sp.Step0Page, "_watch_production_worker",
        lambda self, w: (watched.append(w), real_watch(self, w))[0])

    page, timeline = _drive_real_save(app, monkeypatch, tmp_path, tab)
    worker = page._wsi_worker

    assert timeline.index("release") == 0
    assert watched and watched[0] is worker

    assert _wait_until(lambda: worker.emitted.is_set())
    QtTest.QTest.qWait(60)               # let the business signal land

    assert "logical_finished" in timeline
    assert "apply_corrected_store" in timeline
    assert worker.isRunning() is True
    assert tab.resume_calls == 0, (
        f"resumed while the worker was still running: {timeline}")
    assert page.loader._corrected_zarr_path == "/tmp/corrected.zarr"
    assert page.loader._corrected_decisions == {"CD3": "tophat"}

    worker.may_exit.set()
    assert worker.wait(5000)
    assert _wait_until(lambda: "resume" in timeline)

    assert timeline.index("logical_finished") < \
        timeline.index("apply_corrected_store") < \
        timeline.index("physical_finished") < timeline.index("resume")
    assert tab.resume_calls == 1


def test_a_cancelled_or_failed_run_still_resumes_exactly_once(app):
    """`QThread.finished` fires once however the run ended, so cancel and
    error need no separate wiring -- and must not double-resume."""
    tab = _RecordingTab(released=True)
    page = _page(app, tab)
    page._preview_stack.setCurrentIndex(sp.PREVIEW_PAGE_FULL_IMAGE)

    class _Cancelled(QtCore.QThread):
        canceled = QtCore.pyqtSignal(str)
        finished = QtCore.pyqtSignal(str, dict)      # SHADOWS QThread's

        def run(self):
            self.canceled.emit("/tmp/partial.zarr")

    worker = _Cancelled()
    page._watch_production_worker(worker)
    page._wsi_worker = worker

    worker.start()
    assert worker.wait(5000)
    assert _wait_until(lambda: tab.resume_calls > 0)
    QtTest.QTest.qWait(50)

    assert tab.resume_calls == 1


def test_the_last_worker_to_finish_is_the_one_that_resumes(app):
    """Two production runs: the first to finish must not resume while the
    second still holds the GPU; the second must."""
    tab = _RecordingTab(released=True)
    page = _page(app, tab)
    page._preview_stack.setCurrentIndex(sp.PREVIEW_PAGE_FULL_IMAGE)

    class _Held(QtCore.QThread):
        def __init__(self):
            super().__init__()
            self.may_exit = threading.Event()

        def run(self):
            self.may_exit.wait(10)

    first, second = _Held(), _Held()
    page._watch_production_worker(first)
    page._watch_production_worker(second)
    page._batch_worker = first
    page._ondemand_workers = [second]
    first.start()
    second.start()
    assert _wait_until(lambda: first.isRunning() and second.isRunning())

    first.may_exit.set()
    assert first.wait(5000)
    QtTest.QTest.qWait(80)
    assert tab.resume_calls == 0, "resumed while the second run held the GPU"

    second.may_exit.set()
    assert second.wait(5000)
    assert _wait_until(lambda: tab.resume_calls > 0)

    assert tab.resume_calls == 1
