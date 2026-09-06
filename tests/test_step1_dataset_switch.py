"""A committed Step0 dataset switch immediately invalidates the Step1 context.

Measured defect this covers: after Step0 successfully loaded dataset B,
MainWindow still held dataset A's `step0_done` / `_step1_context_ready`, ROI,
patches, caches and loader, so Step1 stayed enterable and displayed A until the
next Save.  `step0_complete` could not fix this: "the dataset changed" and
"Step0 published a handoff" are different events.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined into
one process.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtCore, QtWidgets  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Loader:
    """The smallest loader Step1 state will accept as 'bound to a dataset'."""

    filepath = "/tmp/dataset_a.ome.tiff"
    shape = (64, 64)
    ch_map = {"DAPI": 0, "CD3": 1}

    def channel_names(self):
        return ["DAPI", "CD3"]

    def read_region(self, channel, y0, y1, x0, x1, downsample=1):
        ds = max(1, int(downsample))
        return np.zeros(((y1 - y0) // ds or 1, (x1 - x0) // ds or 1), np.float32)


class _FakeLoaderThread(QtCore.QObject):
    """Stands in for PreviewLoaderThread with the same signal contract."""

    done = QtCore.pyqtSignal(int, object)
    progress = QtCore.pyqtSignal(int, int, int, str)
    error = QtCore.pyqtSignal(int, str)

    def __init__(self, idx):
        super().__init__()
        self._idx = idx
        self.stopped = False

    def isRunning(self):
        return not self.stopped

    def stop(self):
        self.stopped = True

    def wait(self, *_a):
        return True


def _ready_window(app):
    from block01.ui.main_window import MainWindow
    w = MainWindow()
    w.loader = _Loader()
    w.step0_done = True
    w._step1_context_ready = True
    w.step0_output = {
        "step0_manifest_path": "/tmp/a/step0/step0_roi_result.json",
        "step0_dir": "/tmp/a/step0",
        "step1_dir": "/tmp/a/step1",
    }
    w._rois = [{"name": "ROI_1", "bbox_fullres": [0, 32, 0, 32]}]
    w._active_roi = w._rois[0]
    w._on_patches([(0, 16, 0, 16), (16, 32, 16, 32)])
    w._patch_channel_cache[0] = {"DAPI": np.zeros((4, 4), np.float32)}
    w._patch_load_ready.add(0)
    w._patch_seg_results[0] = {"cells": 7}
    w._seg_preview_history["P1"] = {"cellpose": {"history": [{"cells": 7}]}}
    w._p2_params = {"diameter": 30}
    w._params_source = "manual"
    return w


def test_a_committed_switch_locks_step1_and_drops_the_old_dataset(app):
    w = _ready_window(app)
    try:
        w._step0.dataset_committed.emit(
            {"gen": 7, "ome_path": "/tmp/dataset_b.ome.tiff", "output_dir": "/tmp/b"})

        assert w.step0_done is False
        assert w._step1_context_ready is False
        assert w.loader is None
        assert w._rois == []
        assert w._active_roi is None
        assert w._all_patches == []
        assert w._patch_channel_cache == {}
        assert w._patch_load_ready == set()
        assert w._patch_seg_results == {}
        assert w._seg_preview_history == {}
        assert w._preview_patch_idx == -1
        assert w._p2_params is None
        assert w._patch_sel_btns == []
        assert w.prev_img.image is None
    finally:
        w.close()


def test_step1_cannot_be_entered_after_a_committed_switch(app):
    w = _ready_window(app)
    try:
        w._stack.setCurrentIndex(1)
        w._current_step = 1
        w._step0.dataset_committed.emit({"gen": 3})
        # Leaving the user on a Step1 page whose fields were just cleared would
        # not be a locked Step1; the switch sends them back to Step0.
        assert w._stack.currentIndex() == 0
        w._go_to_step1()
        # The bound handoff is gone with step0_output, so the authoritative
        # reader is never even invoked and navigation is refused.
        assert w._stack.currentIndex() != 1
        assert w._step1_context_ready is False
    finally:
        w.close()


def test_the_same_committed_generation_invalidates_only_once(app):
    w = _ready_window(app)
    try:
        w._step0.dataset_committed.emit({"gen": 4})
        w.step0_done = True
        w._step1_context_ready = True
        w.loader = _Loader()
        # A duplicate notification for a generation already handled must not
        # tear down a context that has been re-established since.
        w._step0.dataset_committed.emit({"gen": 4})
        assert w.step0_done is True
        assert w._step1_context_ready is True
        assert w.loader is not None
    finally:
        w.close()


def test_a_failed_load_commits_nothing_and_keeps_the_old_dataset(app, tmp_path, monkeypatch):
    w = _ready_window(app)
    seen = []
    w._step0.dataset_committed.connect(lambda info: seen.append(info))
    try:
        monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                            staticmethod(lambda *a, **k: None))
        monkeypatch.setattr(QtWidgets.QMessageBox, "critical",
                            staticmethod(lambda *a, **k: None))
        gen_before = w._step0._dataset_gen
        w._step0._ome_path_edit.setText(str(tmp_path / "does_not_exist.ome.tiff"))
        w._step0._reload_from_paths()

        assert seen == []                       # pre-commit failure announces nothing
        assert w._step0._dataset_gen == gen_before
        assert w.step0_done is True             # the old dataset is still current
        assert w._step1_context_ready is True
        assert w._all_patches != []
    finally:
        w.close()


def test_a_late_loader_signal_cannot_write_the_old_dataset_into_the_new_one(app):
    w = _ready_window(app)
    try:
        thread = _FakeLoaderThread(0)
        thread.done.connect(w._on_patch_loaded)
        thread.progress.connect(w._on_patch_progress)
        thread.error.connect(w._on_patch_error)
        w._patch_loaders[0] = thread

        w._step0.dataset_committed.emit({"gen": 9})
        assert thread.stopped is True

        # Dataset A's loader finally answers, after B is current.
        thread.done.emit(0, {"DAPI": np.ones((4, 4), np.float32)})
        assert w._patch_channel_cache == {}
        assert w._patch_load_ready == set()
    finally:
        w.close()
