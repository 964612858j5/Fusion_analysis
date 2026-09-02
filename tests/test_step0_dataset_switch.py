"""Cross-dataset correctness for the Step0 Background Correction page.

Three separate guarantees, tested separately because they fail separately:

  1. A committed switch CLEARS the previous dataset -- no pixels, metrics or
     caches of dataset A survive into dataset B, not even until B's first
     result arrives.
  2. The dataset GENERATION, captured when a worker is created, drops every
     late signal that worker emits after the switch. This is about state
     mutation, not about resources.
  3. `_stop_bg_workers` releases the RESOURCES: it requests a stop and then
     waits for the thread to actually finish, and reports what did not.
     A thread that refuses to finish aborts the switch instead of being
     abandoned or destroyed while running.

Plus: the load is transactional -- a failure before the commit point leaves
dataset A fully loaded and displayed.

Own module (not appended to test_step0_background_correction_tab.py) for the
same reason test_step0_full_image.py is: that file already builds 32
`Step0Page` instances and more pages tip a whole-file run into the known
pyqtgraph/offscreen segfault.

No sleeps anywhere: the fake workers block on `threading.Event`.
"""

import os
import threading

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtCore  # noqa: E402

from block01.ui.step0 import step0_page as sp  # noqa: E402

from test_step0_background_correction_tab import (  # noqa: E402
    _GpuPathLoader,
    app,            # noqa: F401  (pytest fixture)
)


# ── helpers ───────────────────────────────────────────────────────────────

class _RecordingExploreTab:
    """Stands in for Step0ExploreTab and records WHEN it was rebound.

    `set_dataset` snapshots the page's loader identity, which is what makes
    the ordering assertion possible: the old stack must be torn down while
    the page still holds the OLD loader.
    """

    def __init__(self, page):
        self._page = page
        self.calls = []          # [(path, loader_at_call_time)]
        self.released = []

    def set_dataset(self, path):
        self.calls.append((path, getattr(self._page, "loader", None)))

    def release_for_production(self, reason):
        self.released.append(reason)

    def teardown(self, **_kw):
        pass


class _BlockingWorker(QtCore.QThread):
    """A real QThread that runs until it is told to stop.

    `stop()`/`cancel()` are the two spellings the page uses (batch/preview vs
    preload); both release the thread, and both record that they were called.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._release = threading.Event()
        self.started_running = threading.Event()
        self.stop_calls = []

    def run(self):
        self.started_running.set()
        self._release.wait(30)

    def stop(self):
        self.stop_calls.append("stop")
        self._release.set()

    def cancel(self):
        self.stop_calls.append("cancel")
        self._release.set()

    def start_and_wait_until_running(self):
        self.start()
        assert self.started_running.wait(10)
        return self


class _UnstoppableWorker(_BlockingWorker):
    """Stop is requested and observed, but the thread reports NOT finished.

    Overriding `wait` (rather than actually hanging a thread) keeps the test
    deterministic: what is under test is the page's reaction to
    `wait() == False`, not the OS scheduler.
    """

    def wait(self, *_a, **_kw):          # noqa: D102
        self._release.set()              # never leave a real thread running
        super().wait(10000)
        return False


def _fresh_page(tmp_path, monkeypatch, loader=None):
    """A page in the state 'dataset A is loaded and displayed'."""
    page = sp.Step0Page()
    page.loader = loader if loader is not None else _GpuPathLoader()
    page.ome_path = str(tmp_path / "A.tif")
    page.patches = [(0, 32, 0, 32)]
    page.current_patch_idx = 0
    page.nucleus_channel = "DAPI"
    page._rebuild_channel_list()
    page.current_channel = "CD3"
    return page


def _display_a_payload(page):
    """Put dataset A's pixels and metrics on screen, the way a finished
    patch does -- by driving the page's own patch-done slot."""
    disp = np.linspace(0.0, 1.0, 32 * 32, dtype=np.float32).reshape(32, 32)
    metrics = {"snr": 4.0, "bg_cv": 0.25}
    payload = {
        "original_disp": disp, "tophat_disp": disp, "cucim_disp": disp,
        "original_metrics": metrics, "tophat_metrics": metrics,
        "cucim_metrics": metrics,
        "nucleus_disp": None,
    }
    page._on_batch_patch_done("CD3", 0, payload)
    page._process_completed = True
    page._computed_channels = {"CD3"}
    assert page._last_payload is payload
    return payload


def _switch_to_b(page, tmp_path, monkeypatch, *, new_loader=None, raises=None):
    """Drive the real `_reload_from_paths` for a second dataset.

    Only two things are stubbed, and neither belongs to this round's
    behaviour: the loader constructor (no OME-TIFF on disk) and the
    post-commit navigator auto-open / overview repaint, which pull in
    unrelated widget machinery.
    """
    b = tmp_path / "B.tif"
    b.write_bytes(b"not-really-a-tiff")
    made = new_loader if new_loader is not None else _GpuPathLoader()

    def _ctor(*_a, **_kw):
        if raises is not None:
            raise raises
        return made

    monkeypatch.setattr(sp, "OMETIFFLoader", _ctor)
    warnings = []
    monkeypatch.setattr(sp.QMessageBox, "warning",
                        lambda *a, **k: warnings.append(a[1:3]))
    monkeypatch.setattr(sp.QMessageBox, "critical",
                        lambda *a, **k: warnings.append(a[1:3]))
    monkeypatch.setattr(sp.QMessageBox, "information",
                        lambda *a, **k: warnings.append(a[1:3]))
    monkeypatch.setattr(type(page), "_auto_open_tissue_navigator",
                        lambda self: None)
    monkeypatch.setattr(type(page.overview), "_load_overview",
                        lambda self: None)
    page._ome_path_edit.setText(str(b))
    page._out_path_edit.setText(str(tmp_path / "out"))
    page._panel_csv_edit.setText("")
    page._reload_from_paths()
    return made, warnings


# ── 1. the switch clears dataset A ────────────────────────────────────────

def test_a_committed_switch_clears_pixels_metrics_and_caches(app, tmp_path,
                                                             monkeypatch):
    page = _fresh_page(tmp_path, monkeypatch)
    _display_a_payload(page)
    page._preview_cache[("CD20", 0)] = {"original_metrics": {"snr": 1,
                                                             "bg_cv": 1}}
    page._preload_cache = {0: {"CD3": np.zeros((4, 4), np.float32)}}
    # Dataset A's batch selection: CD3 ticked, cucim. B has a CD3 too.
    page._channel_methods = {"CD3": "cucim"}
    page._channel_colors = {"CD3": (1.0, 0.0, 0.0)}
    assert page._orig_img.image is not None

    _switch_to_b(page, tmp_path, monkeypatch)

    assert page._last_payload is None
    for img in (page._orig_img, page._top_img, page._cu_img):
        assert img.image is None
    assert "—" in page._metrics_original.text()
    assert "—" in page._metrics_tophat.text()
    assert "—" in page._metrics_cucim.text()
    assert page._preview_status.text().startswith("Select a channel")
    assert page._preview_cache == {}
    assert page._preload_cache == {}
    assert page._computed_channels == set()
    assert page._process_completed is False
    # Channel names repeat across datasets: B must not inherit A's ticks.
    assert page._channel_methods == {}
    # ...but a display preference is not dataset state and is left alone.
    assert page._channel_colors == {"CD3": (1.0, 0.0, 0.0)}
    assert page._full_image_source == "original"
    assert page._preview_stack.currentIndex() == sp.PREVIEW_PAGE_COMPARE


def test_the_clearing_happens_before_the_new_loader_is_bound(app, tmp_path,
                                                             monkeypatch):
    """Not just 'ends up clear': A's pixels must be gone before B exists."""
    page = _fresh_page(tmp_path, monkeypatch)
    _display_a_payload(page)
    seen = {}
    real = type(page)._reset_dataset_view_state

    def spy(self):
        seen["loader_at_reset"] = self.loader
        return real(self)

    monkeypatch.setattr(type(page), "_reset_dataset_view_state", spy)
    old_loader = page.loader
    made, _ = _switch_to_b(page, tmp_path, monkeypatch)

    assert seen["loader_at_reset"] is old_loader
    assert page.loader is made


# ── 2. generation captured at creation drops late signals ────────────────

_LATE_CALLS = [
    ("channel_patch_done", "_on_batch_patch_done",
     ("CD3", 0, {"original_disp": None, "tophat_disp": None,
                 "cucim_disp": None,
                 "original_metrics": {"snr": 9.0, "bg_cv": 9.0},
                 "tophat_metrics": {"snr": 9.0, "bg_cv": 9.0},
                 "cucim_metrics": {"snr": 9.0, "bg_cv": 9.0}})),
    ("channel_done", "_on_batch_channel_done", ("CD3",)),
    ("all_done", "_on_batch_all_done", ()),
    ("progress", "_on_batch_progress", (1, 2, "stale progress")),
    ("error_signal", "_on_batch_error", ("__global__", 0, "stale error")),
    ("canceled", "_on_batch_canceled", ()),
    ("preload_channel", "_on_preload_channel",
     (0, 0, "CD3", np.ones((4, 4), np.float32))),
    ("preload_finished", "_on_preload_finished", (0,)),
]


@pytest.mark.parametrize(("label", "slot_name", "args"), _LATE_CALLS,
                         ids=[c[0] for c in _LATE_CALLS])
def test_a_late_signal_from_the_old_dataset_cannot_write_page_state(
        app, tmp_path, monkeypatch, label, slot_name, args):
    page = _fresh_page(tmp_path, monkeypatch)
    _display_a_payload(page)
    # Connection time: this is where the generation is captured.
    late = page._gen_slot(getattr(page, slot_name))

    _switch_to_b(page, tmp_path, monkeypatch)
    page.current_channel = "CD3"
    page.current_patch_idx = 0
    status_before = page._proc_status.text()

    late(*args)

    assert page._preview_cache == {}
    assert page._preload_cache == {}
    assert page._last_payload is None
    assert page._process_completed is False
    assert page._computed_channels == set()
    assert "—" in page._metrics_original.text()
    assert page._preview_status.text().startswith("Select a channel")
    assert page._proc_status.text() == status_before


def test_the_current_generations_slot_still_delivers(app, tmp_path,
                                                     monkeypatch):
    """The guard must reject the OLD generation only -- proving the previous
    test is not passing because every slot is dead."""
    page = _fresh_page(tmp_path, monkeypatch)
    _switch_to_b(page, tmp_path, monkeypatch)
    page.patches = [(0, 32, 0, 32)]
    fresh = page._gen_slot(page._on_batch_all_done)

    fresh()

    assert page._process_completed is True


def test_a_late_wsi_finish_cannot_rebind_the_new_datasets_loader(
        app, tmp_path, monkeypatch):
    page = _fresh_page(tmp_path, monkeypatch)
    late = page._gen_slot(
        lambda path, decisions: page._on_wsi_finished({}, path, decisions))
    made, _ = _switch_to_b(page, tmp_path, monkeypatch)

    late(str(tmp_path / "stale.zarr"), {"CD3": "tophat"})

    assert made._corrected_zarr_path is None
    assert made._corrected_decisions == {}


def test_a_late_preview_result_cannot_write_the_new_datasets_display(
        app, tmp_path, monkeypatch):
    page = _fresh_page(tmp_path, monkeypatch)
    late = page._gen_slot(page._on_preview_ready)
    _switch_to_b(page, tmp_path, monkeypatch)
    page.current_channel = "CD3"
    disp = np.ones((8, 8), np.float32)
    metrics = {"snr": 7.0, "bg_cv": 7.0}

    late(page._preview_req_id, {"original_disp": disp, "tophat_disp": disp,
                                "cucim_disp": disp, "nucleus_disp": None,
                                "original_metrics": metrics,
                                "tophat_metrics": metrics,
                                "cucim_metrics": metrics})

    assert page._last_payload is None
    assert page._preview_cache == {}


def test_every_batch_worker_signal_is_connected_through_a_generation_slot(
        app, tmp_path, monkeypatch):
    """Behavioural, not source-shaped: start a real on-demand worker, bump the
    generation the way a switch does, then emit each of its signals and
    require that none of them writes the page."""
    page = _fresh_page(tmp_path, monkeypatch)

    class _IdleBatch(QtCore.QThread):
        channel_patch_done = QtCore.pyqtSignal(str, int, dict)
        channel_done = QtCore.pyqtSignal(str)
        all_done = QtCore.pyqtSignal()
        progress = QtCore.pyqtSignal(int, int, str)
        error_signal = QtCore.pyqtSignal(str, int, str)
        canceled = QtCore.pyqtSignal()

        def __init__(self, *_a, **kw):
            super().__init__(kw.get("parent"))

        def run(self):
            return

    monkeypatch.setattr(sp, "BatchProcessWorker", _IdleBatch)
    page._start_ondemand("CD3")
    worker = page._ondemand_workers[-1]

    page._dataset_gen += 1          # what the commit block does
    status_before = page._proc_status.text()
    payload = {"original_disp": None, "tophat_disp": None, "cucim_disp": None,
               "original_metrics": {"snr": 3.0, "bg_cv": 3.0},
               "tophat_metrics": {"snr": 3.0, "bg_cv": 3.0},
               "cucim_metrics": {"snr": 3.0, "bg_cv": 3.0}}
    worker.channel_patch_done.emit("CD3", 0, payload)
    worker.channel_done.emit("CD3")
    worker.all_done.emit()
    worker.progress.emit(1, 2, "stale")
    worker.error_signal.emit("__global__", 0, "stale")
    worker.canceled.emit()
    app.processEvents()

    assert page._preview_cache == {}
    assert page._last_payload is None
    assert page._process_completed is False
    assert page._proc_status.text() == status_before


# ── 3. real workers are stopped AND waited ───────────────────────────────

def test_live_workers_are_stopped_waited_and_their_handles_released(
        app, tmp_path, monkeypatch):
    page = _fresh_page(tmp_path, monkeypatch)
    preload = _BlockingWorker(page).start_and_wait_until_running()
    batch = _BlockingWorker(page).start_and_wait_until_running()
    ondemand = _BlockingWorker(page).start_and_wait_until_running()
    preview = _BlockingWorker(page).start_and_wait_until_running()
    page._preload_worker = preload
    page._batch_worker = batch
    page._ondemand_workers = [ondemand]
    page._preview_worker = preview

    _switch_to_b(page, tmp_path, monkeypatch)

    assert preload.stop_calls == ["cancel"]      # preload's spelling
    for w in (batch, ondemand, preview):
        assert w.stop_calls == ["stop"]
    for w in (preload, batch, ondemand, preview):
        assert not w.isRunning()                 # waited, not just requested
    assert page._preload_worker is None
    assert page._batch_worker is None
    assert page._preview_worker is None
    assert page._ondemand_workers == []


def test_finished_ondemand_workers_are_pruned_without_error(app, tmp_path,
                                                            monkeypatch):
    page = _fresh_page(tmp_path, monkeypatch)
    done = _BlockingWorker(page).start_and_wait_until_running()
    done.stop()
    assert done.wait(10000)
    page._ondemand_workers = [done]

    assert page._stop_bg_workers() == []
    assert page._ondemand_workers == []
    assert done.stop_calls == ["stop"]           # not stopped a second time


def test_a_thread_that_will_not_finish_aborts_the_switch(app, tmp_path,
                                                         monkeypatch):
    page = _fresh_page(tmp_path, monkeypatch)
    payload = _display_a_payload(page)
    old_loader = page.loader
    gen_before = page._dataset_gen
    stubborn = _UnstoppableWorker(page).start_and_wait_until_running()
    page._batch_worker = stubborn

    made, warnings = _switch_to_b(page, tmp_path, monkeypatch)

    assert page.loader is old_loader             # dataset A still valid
    assert page.loader is not made
    assert page._dataset_gen == gen_before       # generation did NOT move
    assert page._last_payload is payload         # A still displayed
    assert page._batch_worker is stubborn        # handle kept, not abandoned
    assert any("did not stop" in str(m) for pair in warnings for m in pair)
    stubborn.stop()
    assert QtCore.QThread.wait(stubborn, 10000)


# ── 4. transactional loading ─────────────────────────────────────────────

def test_a_failed_new_loader_leaves_the_current_dataset_valid(app, tmp_path,
                                                              monkeypatch):
    page = _fresh_page(tmp_path, monkeypatch)
    payload = _display_a_payload(page)
    old_loader = page.loader
    old_path = page.ome_path
    gen_before = page._dataset_gen

    _switch_to_b(page, tmp_path, monkeypatch,
                 raises=RuntimeError("corrupt OME-TIFF"))

    assert page.loader is old_loader
    assert page.ome_path == old_path
    assert page._dataset_gen == gen_before
    assert page._last_payload is payload
    assert page._orig_img.image is not None
    assert page._computed_channels == {"CD3"}


def test_a_running_whole_slide_correction_refuses_the_switch(app, tmp_path,
                                                             monkeypatch):
    """WsiCorrectionWorker has no stop that preserves the zarr it is writing,
    so the page refuses the switch rather than racing it."""
    page = _fresh_page(tmp_path, monkeypatch)
    old_loader = page.loader
    gen_before = page._dataset_gen
    wsi = _BlockingWorker(page).start_and_wait_until_running()
    page._wsi_worker = wsi

    made, warnings = _switch_to_b(page, tmp_path, monkeypatch)

    assert page.loader is old_loader
    assert page.loader is not made
    assert page._dataset_gen == gen_before
    assert wsi.stop_calls == []                  # never asked to stop
    assert any("whole-slide" in str(m) for pair in warnings for m in pair)
    wsi.stop()
    assert wsi.wait(10000)


def test_the_old_loaders_corrected_store_is_released(app, tmp_path,
                                                     monkeypatch):
    page = _fresh_page(tmp_path, monkeypatch)
    page.loader.set_corrected_zarr_store(str(tmp_path / "a.zarr"),
                                         {"CD3": "tophat"})
    old_loader = page.loader

    _switch_to_b(page, tmp_path, monkeypatch)

    assert old_loader._corrected_zarr_path is None
    assert old_loader._corrected_decisions == {}


def test_an_open_full_image_is_unbound_before_the_new_dataset_is_bound(
        app, tmp_path, monkeypatch):
    page = _fresh_page(tmp_path, monkeypatch)
    tab = _RecordingExploreTab(page)
    page._explore_tab = tab
    page._full_image_source = "tophat"
    page._preview_stack.setCurrentIndex(sp.PREVIEW_PAGE_FULL_IMAGE)
    old_loader = page.loader

    made, _ = _switch_to_b(page, tmp_path, monkeypatch)

    # First call unbinds (path None) and happens while the OLD loader is
    # still the page's loader; only afterwards is the new path bound.
    assert tab.calls[0] == (None, old_loader)
    assert tab.calls[-1][0] == page.ome_path
    assert tab.calls[-1][1] is made
    assert page._preview_stack.currentIndex() == sp.PREVIEW_PAGE_COMPARE
    assert page._full_image_source == "original"


# ── the queued-signal case ────────────────────────────────────────────────
#
# Every other generation test either calls the wrapper directly or emits
# from the GUI thread. Neither exercises the claim the design actually
# rests on: that a signal ALREADY SITTING IN QT'S QUEUE when the switch
# happens is dropped. Here the worker thread emits first, the GUI thread
# deliberately does not pump the event loop, the generation moves, and only
# then are events processed.

class _EmitOnceBatch(QtCore.QThread):
    """Emits one full set of signals from the WORKER thread, then stops.

    The cross-thread connections the page makes are queued connections, so
    every emit below parks in the GUI thread's event queue until someone
    pumps it -- which is exactly the window under test.
    """

    channel_patch_done = QtCore.pyqtSignal(str, int, dict)
    channel_done = QtCore.pyqtSignal(str)
    all_done = QtCore.pyqtSignal()
    progress = QtCore.pyqtSignal(int, int, str)
    error_signal = QtCore.pyqtSignal(str, int, str)
    canceled = QtCore.pyqtSignal()

    PAYLOAD = {"original_disp": None, "tophat_disp": None, "cucim_disp": None,
               "original_metrics": {"snr": 8.0, "bg_cv": 8.0},
               "tophat_metrics": {"snr": 8.0, "bg_cv": 8.0},
               "cucim_metrics": {"snr": 8.0, "bg_cv": 8.0}}

    def __init__(self, *_a, **kw):
        super().__init__(kw.get("parent"))
        self.emitted = threading.Event()

    def run(self):
        self.channel_patch_done.emit("CD3", 0, dict(self.PAYLOAD))
        self.channel_done.emit("CD3")
        self.progress.emit(1, 2, "queued progress")
        self.all_done.emit()
        self.error_signal.emit("__global__", 0, "queued error")
        self.canceled.emit()
        self.emitted.set()

    def stop(self):
        pass


def _start_a_worker_that_has_already_emitted(page, monkeypatch):
    monkeypatch.setattr(sp, "BatchProcessWorker", _EmitOnceBatch)
    page._start_ondemand("CD3")
    worker = page._ondemand_workers[-1]
    # Block the GUI thread WITHOUT pumping events: the emits land in the
    # queue and stay there.
    assert worker.emitted.wait(10)
    assert worker.wait(10000)
    return worker


def test_signals_already_queued_when_the_generation_moves_are_dropped(
        app, tmp_path, monkeypatch):
    page = _fresh_page(tmp_path, monkeypatch)
    _start_a_worker_that_has_already_emitted(page, monkeypatch)
    status_before = page._proc_status.text()
    # Proof that they really are still queued and not already delivered:
    assert page._preview_cache == {}

    page._dataset_gen += 1          # what the commit block does
    app.processEvents()             # NOW the queued signals are delivered

    assert page._preview_cache == {}
    assert page._last_payload is None
    assert page._process_completed is False
    assert page._computed_channels == set()
    assert "—" in page._metrics_original.text()
    assert page._proc_status.text() == status_before


def test_the_same_queued_signals_do_arrive_when_the_generation_holds(
        app, tmp_path, monkeypatch):
    """Control for the test above: without the generation bump the very same
    queued emits DO reach the page. Otherwise that test would pass on a
    connection that never delivers anything."""
    page = _fresh_page(tmp_path, monkeypatch)
    _start_a_worker_that_has_already_emitted(page, monkeypatch)

    app.processEvents()

    # The on-demand wiring's real writes: cache, displayed payload, metrics,
    # and the global-error status line. (`all_done` is `lambda: None` on this
    # path, so `_process_completed` is not one of them.)
    assert ("CD3", 0) in page._preview_cache
    assert page._last_payload is not None
    assert "8.00" in page._metrics_original.text()
    assert "queued error" in page._proc_status.text()


# ── dataset-scoped state that is not pixels ──────────────────────────────
#
# Four more things are keyed by channel NAME or patch/ROI BBOX, both of
# which repeat across slides. Inheriting them is not a cosmetic glitch: two
# of them decide where dataset B's OUTPUTS get written and which pixel
# source its calibration is derived from.

def test_a_forced_channel_source_does_not_survive_into_the_next_dataset(
        app, tmp_path, monkeypatch):
    """A's CD3 is forced to raw_ome even though corrected pixels exist. B has
    a CD3 with corrected pixels too -- it must fall back to AUTO-detection
    (corrected_zarr), not inherit A's override.

    Driven through the real save-path stamper, so what is asserted is the
    resolved source identity, not just an empty dict.
    """
    zarr = pytest.importorskip("zarr")
    from block01.core.bg_correction import stamp_corrected_channel_identity

    def _corrected(name):
        path = str(tmp_path / name)
        root = zarr.open_group(path, mode="w")
        g = root.create_group("ROI_1")
        g.attrs["bbox_fullres"] = [0, 64, 0, 64]
        ds = g.create_dataset("CD3", data=np.ones((64, 64), np.float32))
        stamp_corrected_channel_identity(
            ds, "CD3", channel_index=1, correction_method="tophat",
            roi_name="ROI_1", roi_bbox_fullres=[0, 64, 0, 64])
        return path

    class _Loader(_GpuPathLoader):
        filepath = "/data/raw.ome.tif"

    loader_a = _Loader()
    loader_a._corrected_zarr_path = _corrected("a_corrected.zarr")
    page = _fresh_page(tmp_path, monkeypatch, loader=loader_a)
    page.patches = [(0, 64, 0, 64)]

    def _cfg():
        return {"channels": {"CD3": {"min": 0.0, "max": 1.0}},
                "source_policy": {"preview_only": True, "step2_ready": False}}

    page.set_channel_source_request("CD3", "raw_ome")
    cfg_a = _cfg()
    page._apply_source_aware_identity(cfg_a)
    assert (cfg_a["channels"]["CD3"]["calibration_source_identity"]
            ["actual_source_kind"]) == "raw_ome"          # override in force

    loader_b = _Loader()
    _switch_to_b(page, tmp_path, monkeypatch, new_loader=loader_b)
    page.patches = [(0, 64, 0, 64)]
    page.current_patch_idx = 0
    # B's own corrected pixels, produced by B's own Save. (The load path
    # clears the new loader's corrected store, so this is the only way B can
    # have one -- which is exactly the realistic sequence.)
    loader_b.set_corrected_zarr_store(_corrected("b_corrected.zarr"),
                                      {"CD3": "tophat"})

    assert page._channel_source_requests == {}
    cfg_b = _cfg()
    page._apply_source_aware_identity(cfg_b)
    assert (cfg_b["channels"]["CD3"]["calibration_source_identity"]
            ["actual_source_kind"]) == "corrected_zarr"   # auto again


def test_the_next_dataset_cannot_reuse_the_previous_roi_context_at_the_same_bbox(
        app, tmp_path, monkeypatch):
    """The reuse signature is (mode, first ROI bbox) -- it does not mention
    the dataset. B drawing an ROI where A had one must not reuse A's
    roi_context, or B's outputs land in A's roi_dir."""
    page = _fresh_page(tmp_path, monkeypatch)
    rois = [{"bbox_fullres": [0, 64, 0, 64], "name": "ROI_1"}]
    page._roi_context = {"roi_id": "A_20260101_000000",
                         "roi_dir": str(tmp_path / "A_roi"),
                         "step_dirs": {"step0": str(tmp_path / "A_roi" / "step0")}}
    page._roi_context_sig = page._roi_context_signature(rois)
    sig_a = page._roi_context_sig

    _switch_to_b(page, tmp_path, monkeypatch)

    assert page._roi_context is None
    assert page._roi_context_sig is None
    # Same bbox on B -> same signature, so ONLY the cleared context stops the
    # reuse branch. Show that the reuse condition is now false.
    assert page._roi_context_signature(rois) == sig_a
    reuse = (page._roi_context is not None
             and page._roi_context_sig == page._roi_context_signature(rois))
    assert reuse is False


def test_the_last_saved_remap_path_does_not_survive_into_the_next_dataset(
        app, tmp_path, monkeypatch):
    """MainWindow prefers this exact path over re-resolving, so A's path would
    feed A's remap config to B's Step1."""
    page = _fresh_page(tmp_path, monkeypatch)
    page._last_saved_remap_path = str(tmp_path / "A_roi" / "channel_remap.json")

    _switch_to_b(page, tmp_path, monkeypatch)

    # "" is exactly what MainWindow's getattr default is, i.e. "nothing
    # remembered" -- not a path that merely does not exist.
    assert page._last_saved_remap_path == ""


def test_patch_viewports_do_not_survive_into_the_next_dataset(app, tmp_path,
                                                              monkeypatch):
    """Keyed by patch bbox: B's patch at A's bbox is a different patch and
    must fit-to-view rather than restore A's zoom."""
    page = _fresh_page(tmp_path, monkeypatch)
    page._conditioning_patch_viewports = {(0, 32, 0, 32): (1.0, 2.0, 3.0, 4.0)}

    _switch_to_b(page, tmp_path, monkeypatch)

    assert page._conditioning_patch_viewports == {}
    # And the restore lookup now misses, which is what makes it fit-to-view.
    page.patches = [(0, 32, 0, 32)]
    page.current_patch_idx = 0
    assert page._conditioning_patch_viewports.get(
        page._conditioning_patch_key(0)) is None
