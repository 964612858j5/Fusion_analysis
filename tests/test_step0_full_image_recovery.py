"""The full image comes back after the production run that released it.

Found on a live session (2026-09-03): with the full image open, Apply
released the viewer ("Background correction is running"), the run ended,
the placeholder still said running, and "Reopen full image" produced a
black canvas. The canvas was black because BOTH layer toggles were off and
looked identical to on; the placeholder was stale because nothing told the
view the run had ended. Three claims, one per cause:

  1. the two layer toggles show their state (glyph + :checked rule), and
     the toolbar label says so when a layer -- or both -- is hidden;
  2. when the run that released the view ends, the view is rebuilt if it
     is on screen, at the viewport it had, and otherwise the placeholder
     says the run has finished;
  3. the viewer records where it was at release time, and a rebuild or a
     dataset change forgets it.

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

    def __init__(self, released=False, viewport=None):
        self.calls = []
        self.finished_calls = 0
        self.stack = None
        self.released = released
        self.released_viewport_l0 = viewport

    def show_source(self, channel, method, params=(), *, viewport_l0=None,
                    tint=None, nucleus=None):
        self.calls.append({"channel": channel, "method": method,
                           "params": tuple(params), "viewport": viewport_l0})
        return True

    def production_finished(self):
        self.finished_calls += 1

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
    def __init__(self, bbox):
        self._current_bbox = bbox
        self.channel, self.method, self.params = "CD3", None, ()
        self.teardown_waits = []

    def set_tint(self, rgb):
        pass

    def set_selection(self, **kw):
        pass

    def jump_to(self, *a):
        pass

    def set_marker_visible(self, v):
        pass


def _tab_with_controller(bbox=(100, 200, 400, 700)):
    """A real Step0ExploreTab over a fake stack whose controller reports
    `bbox` as its current level-0 viewport."""
    made = {}

    class _View(QtWidgets.QLabel):
        """Enough of ExploreView for a release to freeze it."""

        def __init__(self):
            super().__init__("fake")
            self.status_text = None
            self.mouse_enabled = (True, True)
            view = self

            class _Box:
                @staticmethod
                def setMouseEnabled(x, y):
                    view.mouse_enabled = (x, y)

            self.view_box = _Box()

        def set_status_text(self, text):
            self.status_text = text

    class _Stack:
        def __init__(self, controller):
            self.controller = controller
            self.provider = self
            self.view = _View()
            self.caches = ()
            self.overlay = None

        def level_shape(self, _l):
            return (1000, 1000)

        def teardown(self, *, wait_for_floor=False):
            self.controller.teardown_waits.append(wait_for_floor)

    def factory(path, channel, parent_widget=None, **_kw):
        ctl = _Ctl(made.get("bbox", bbox))
        made["ctl"] = ctl
        stack = _Stack(ctl)
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


# ── 2. the run ends -> the view comes back ───────────────────────────────

def test_a_visible_released_view_is_rebuilt_where_it_was(app):
    tab = _RecordingTab(released=True, viewport=(100, 200, 500, 300))
    page = _page(app, tab)
    page._preview_stack.setCurrentIndex(sp.PREVIEW_PAGE_FULL_IMAGE)

    page._on_production_worker_finished()

    assert len(tab.calls) == 1
    assert tab.calls[0]["viewport"] == (100, 200, 500, 300)
    assert tab.finished_calls == 0


def test_a_hidden_released_view_only_gets_a_truthful_placeholder(app):
    tab = _RecordingTab(released=True, viewport=(1, 2, 3, 4))
    page = _page(app, tab)
    page._preview_stack.setCurrentIndex(sp.PREVIEW_PAGE_COMPARE)

    page._on_production_worker_finished()

    assert tab.calls == [], "a hidden view was rebuilt"
    assert tab.finished_calls == 1


def test_nothing_happens_while_another_run_still_holds_the_gpu(
        app, monkeypatch):
    tab = _RecordingTab(released=True)
    page = _page(app, tab)
    page._preview_stack.setCurrentIndex(sp.PREVIEW_PAGE_FULL_IMAGE)
    monkeypatch.setattr(page, "production_correction_busy",
                        lambda: "on-demand background correction")

    page._on_production_worker_finished()

    assert tab.calls == [] and tab.finished_calls == 0


def test_a_view_that_was_never_released_is_left_alone(app):
    """The placeholder may be up because nothing was ever opened, or because
    a dataset switch tore the stack down; a finishing run is not a reason
    to build in either case."""
    tab = _RecordingTab(released=False)
    page = _page(app, tab)
    page._preview_stack.setCurrentIndex(sp.PREVIEW_PAGE_FULL_IMAGE)

    page._on_production_worker_finished()

    assert tab.calls == [] and tab.finished_calls == 0


def test_the_reopen_button_restores_the_released_viewport(app):
    tab = _RecordingTab(released=True, viewport=(7, 8, 90, 60))
    page = _page(app, tab)

    page._reopen_full_image()

    assert tab.calls[-1]["viewport"] == (7, 8, 90, 60)


def test_the_reopen_button_opens_on_the_whole_slide_when_nothing_is_known(app):
    tab = _RecordingTab(released=True, viewport=None)
    page = _page(app, tab)

    page._reopen_full_image()

    assert tab.calls[-1]["viewport"] is None


def test_a_finishing_worker_thread_reaches_the_page_on_the_gui_thread(app):
    """`QThread.finished` is the signal watched -- it fires once the thread
    is no longer running, whatever way the run ended -- and the slot must
    run on the GUI thread, because it rebuilds widgets."""
    tab = _RecordingTab(released=True, viewport=(1, 2, 3, 4))
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
    assert tab.calls[-1]["viewport"] == (1, 2, 3, 4)


def test_a_worker_from_a_previous_dataset_is_ignored(app):
    """The watch goes through `_gen_slot`: a run of dataset A finishing
    after the page moved to dataset B must not build anything."""
    tab = _RecordingTab(released=True)
    page = _page(app, tab)
    page._preview_stack.setCurrentIndex(sp.PREVIEW_PAGE_FULL_IMAGE)
    worker = _Run()
    page._watch_production_worker(worker)
    page._dataset_gen += 1

    worker.start()
    assert worker.wait(5000)
    QtTest.QTest.qWait(100)

    assert tab.calls == [] and tab.finished_calls == 0


# ── 3. the viewer records where it was ───────────────────────────────────

def test_release_records_the_viewport_and_the_released_state(app):
    tab, _made = _tab_with_controller(bbox=(100, 200, 400, 700))
    tab.set_dataset("/data/a.ome.tif")
    tab.show_source("CD3", None, ())
    assert tab.released is False and tab.released_viewport_l0 is None

    tab.release_for_production("patch background correction")

    assert tab.released is True
    # (y0, x0, y1, x1) -> (y0, x0, w, h)
    assert tab.released_viewport_l0 == (100, 200, 500, 300)
    # The frame stays up and carries the message; the placeholder is not
    # used for a release any more.
    assert tab.frozen_view is not None
    assert "running" in tab.frozen_view.status_text
    tab.teardown()


def test_a_rebuild_forgets_the_release(app):
    tab, _made = _tab_with_controller()
    tab.set_dataset("/data/a.ome.tif")
    tab.show_source("CD3", None, ())
    tab.release_for_production("patch background correction")

    assert tab.show_source("CD3", None, ()) is True

    assert tab.released is False
    assert tab.released_viewport_l0 is None
    tab.teardown()


def test_a_dataset_change_forgets_the_release(app):
    tab, _made = _tab_with_controller()
    tab.set_dataset("/data/a.ome.tif")
    tab.show_source("CD3", None, ())
    tab.release_for_production("patch background correction")

    tab.set_dataset("/data/b.ome.tif")

    assert tab.released is False
    assert tab.released_viewport_l0 is None
    tab.teardown()


def test_no_viewport_yet_means_none(app):
    """`_current_bbox` is None until the first range event; a release in
    that window records nothing rather than a garbage rectangle."""
    tab, _made = _tab_with_controller(bbox=None)
    tab.set_dataset("/data/a.ome.tif")
    tab.show_source("CD3", None, ())

    tab.release_for_production("patch background correction")

    assert tab.released is True
    assert tab.released_viewport_l0 is None
    tab.teardown()


def test_production_finished_replaces_the_running_text_only_when_released(app):
    tab, _made = _tab_with_controller()
    tab.set_dataset("/data/a.ome.tif")
    before = tab._placeholder.text()

    tab.production_finished()                 # not released: no-op
    assert tab._placeholder.text() == before

    tab.show_source("CD3", None, ())
    tab.release_for_production("patch background correction")
    tab.production_finished()

    # The frame is still on screen, so the message belongs on ITS badge --
    # the placeholder is not even visible.
    text = tab.frozen_view.status_text
    assert text == et.PLACEHOLDER_RUN_FINISHED
    assert "finished" in text and "Reopen full image" in text
    assert "is running" not in text
    tab.teardown()


def test_production_finished_uses_the_placeholder_when_nothing_was_built(app):
    """A release before anything was ever built has no frame to write on,
    so the placeholder is still the right surface."""
    tab, _made = _tab_with_controller()
    tab.set_dataset("/data/a.ome.tif")
    tab._released = True                      # released with no stack

    tab.production_finished()

    assert tab.frozen_view is None
    assert tab._placeholder.text() == et.PLACEHOLDER_RUN_FINISHED
    tab.teardown()


def test_the_finished_placeholder_promises_no_tab(app):
    text = et.PLACEHOLDER_RUN_FINISHED
    assert "trial" not in text.lower()
    assert " tab" not in text.lower()


# ── the whole-slide worker's SHADOWED finished signal ────────────────────
#
# `WsiCorrectionWorker` declares `finished = pyqtSignal(str, dict)`, which
# SHADOWS `QThread.finished`. That business signal is emitted from inside
# `run()`, while `isRunning()` is still true, so a rebuild started from it
# would be refused by the busy gate and never retried. The watcher must
# therefore bind the base class's signal explicitly, and this module proves
# the difference rather than trusting the spelling.

class _FakeWsiWorker(QtCore.QThread):
    """Mirrors the real worker's shape: a business `finished(str, dict)`
    that shadows `QThread.finished`, emitted from inside run(), followed by
    a controllable delay before the thread actually exits."""

    finished = QtCore.pyqtSignal(str, dict)

    def __init__(self):
        super().__init__()
        self.may_exit = threading.Event()
        self.emitted = threading.Event()

    def run(self):
        self.finished.emit("/tmp/corrected.zarr", {"CD3": "tophat"})
        self.emitted.set()
        self.may_exit.wait(10)


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
        before = len(tab.calls)
        real_physical()
        if len(tab.calls) > before:
            timeline.append("rebuild")

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


def test_the_real_save_path_rebuilds_only_after_the_thread_exits(
        app, monkeypatch, tmp_path):
    """The whole chain, driven: the business signal and the store swap run
    while the thread is STILL running, and the rebuild happens only after it
    physically exits.

    This is the case the shadowed signal name breaks. `WsiCorrectionWorker`
    declares `finished(str, dict)`; connecting the watcher to
    `worker.finished` would fire it here, inside run(), with `isRunning()`
    still true -- the busy gate would refuse the rebuild and nothing would
    ask again.
    """
    tab = _RecordingTab(released=True, viewport=(11, 22, 33, 44))
    watched = []
    real_watch = sp.Step0Page._watch_production_worker
    monkeypatch.setattr(
        sp.Step0Page, "_watch_production_worker",
        lambda self, w: (watched.append(w), real_watch(self, w))[0])

    page, timeline = _drive_real_save(app, monkeypatch, tmp_path, tab)
    worker = page._wsi_worker

    # Released first, then the worker that was actually started is the one
    # handed to the watcher -- both driven, neither read off the source.
    assert timeline.index("release") == 0
    assert watched and watched[0] is worker

    assert _wait_until(lambda: worker.emitted.is_set())
    QtTest.QTest.qWait(60)               # let the business signal land

    assert "logical_finished" in timeline
    assert "apply_corrected_store" in timeline
    assert worker.isRunning() is True
    assert tab.calls == [], (
        f"rebuilt while the worker was still running: {timeline}")
    # The store swap really happened -- the real handler ran, not a stub.
    assert page.loader._corrected_zarr_path == "/tmp/corrected.zarr"
    assert page.loader._corrected_decisions == {"CD3": "tophat"}

    worker.may_exit.set()
    assert worker.wait(5000)
    assert _wait_until(lambda: "rebuild" in timeline)

    assert timeline.index("logical_finished") < \
        timeline.index("apply_corrected_store") < \
        timeline.index("physical_finished") < timeline.index("rebuild")
    assert len(tab.calls) == 1
    assert tab.calls[-1]["viewport"] == (11, 22, 33, 44)


def test_a_cancelled_or_failed_run_still_restores_exactly_once(app):
    """`QThread.finished` fires once however the run ended, so cancel and
    error need no separate wiring -- and must not double-restore."""
    tab = _RecordingTab(released=True, viewport=(1, 1, 2, 2))
    page = _page(app, tab)
    page._preview_stack.setCurrentIndex(sp.PREVIEW_PAGE_FULL_IMAGE)

    class _Cancelled(QtCore.QThread):
        """A run that ended by cancellation: its business signal fires from
        inside run(), and only `QThread.finished` marks the physical end."""

        canceled = QtCore.pyqtSignal(str)
        finished = QtCore.pyqtSignal(str, dict)      # SHADOWS QThread's

        def run(self):
            self.canceled.emit("/tmp/partial.zarr")

    worker = _Cancelled()
    page._watch_production_worker(worker)
    page._wsi_worker = worker

    worker.start()
    assert worker.wait(5000)
    assert _wait_until(lambda: bool(tab.calls))

    assert len(tab.calls) == 1


def test_the_last_worker_to_finish_is_the_one_that_restores(app):
    """Two production runs: the first to finish must not restore while the
    second still holds the GPU; the second must."""
    tab = _RecordingTab(released=True, viewport=(4, 5, 6, 7))
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
    assert tab.calls == [], "restored while the second run still held the GPU"

    second.may_exit.set()
    assert second.wait(5000)
    # Deliberately NOT clearing `_ondemand_workers`: the real page keeps
    # finished handles and decides with `isRunning()`, so the gate must open
    # on its own once the thread has exited.
    assert _wait_until(lambda: bool(tab.calls))

    assert len(tab.calls) == 1
    assert tab.calls[-1]["viewport"] == (4, 5, 6, 7)


def test_a_hidden_full_image_is_not_built_by_a_wsi_finish(app):
    tab = _RecordingTab(released=True, viewport=(1, 2, 3, 4))
    page = _page(app, tab)
    page._preview_stack.setCurrentIndex(sp.PREVIEW_PAGE_COMPARE)
    worker = _FakeWsiWorker()
    page._watch_production_worker(worker)
    page._wsi_worker = worker

    worker.start()
    assert _wait_until(lambda: worker.emitted.is_set())
    worker.may_exit.set()
    assert worker.wait(5000)
    assert _wait_until(lambda: tab.finished_calls > 0)

    assert tab.calls == []               # no hidden viewer was built
    assert tab.finished_calls == 1       # only the placeholder was corrected


def test_the_finish_handler_checks_the_gate_itself(app):
    """`_show_full_image` has a busy gate of its own, so the two together
    keep the behaviour right even if one is removed. This pins the handler's
    OWN check by watching whether it calls the rebuild at all."""
    tab = _RecordingTab(released=True, viewport=(1, 2, 3, 4))
    page = _page(app, tab)
    page._preview_stack.setCurrentIndex(sp.PREVIEW_PAGE_FULL_IMAGE)
    shown = []
    page._show_full_image = lambda **kw: shown.append(kw)

    class _Busy:
        @staticmethod
        def isRunning():
            return True

    page._batch_worker = _Busy()
    page._on_production_worker_finished()
    assert shown == [], "rebuilt while a run still held the GPU"
    assert tab.finished_calls == 0

    page._batch_worker = None
    page._on_production_worker_finished()
    assert shown == [{"viewport_l0": (1, 2, 3, 4)}]
