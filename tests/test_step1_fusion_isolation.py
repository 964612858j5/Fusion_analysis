"""A full-fusion job belongs to the dataset it was started for.

The top step labels let the user leave Step1 while a fusion is running and load
another dataset, so a late `finished`/`error`/`progress` from the previous
slide's job is a reachable path. It used to be able to stamp the new dataset's
identity onto the old job's zarr, set step1_done, write metadata, mark the ROI
index and pop a success box.

`stop()` is cooperative, so a job that has been asked to stop is not a job that
has ended: the token is dropped immediately (nothing may write again) and the
QThread is held by a strong reference until it PHYSICALLY finishes. The worker's
own `finished(str)` shadows `QThread.finished()`, so the base signal is fetched
explicitly rather than mistaken for thread termination.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import os
import threading

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtCore, QtWidgets  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _FakeFusion(QtCore.QThread):
    """Same signal contract as FullFusionWorker, including the shadowing."""

    progress = QtCore.pyqtSignal(int, int, str)
    finished = QtCore.pyqtSignal(str)      # shadows QThread.finished()
    error = QtCore.pyqtSignal(str)

    def __init__(self, obeys_stop=True):
        super().__init__()
        self._release = threading.Event()
        self._obeys_stop = obeys_stop
        self.stop_called = False

    def stop(self):
        self.stop_called = True
        if self._obeys_stop:
            self._release.set()

    def release(self):
        self._release.set()

    def run(self):
        self._release.wait(10)


class _Loader:
    filepath = "/tmp/dataset.ome.tiff"
    shape = (64, 64)
    ch_map = {"DAPI": 0, "CD3": 1}

    def channel_names(self):
        return ["DAPI", "CD3"]

    def read_region(self, channel, y0, y1, x0, x1, downsample=1):
        ds = max(1, int(downsample))
        return np.zeros(((y1 - y0) // ds or 1, (x1 - x0) // ds or 1), np.float32)


class _QtMessages:
    def __enter__(self):
        self.messages = []
        self._prev = QtCore.qInstallMessageHandler(
            lambda _m, _c, msg: self.messages.append(str(msg)))
        return self

    def __exit__(self, *_exc):
        QtCore.qInstallMessageHandler(self._prev)
        return False

    def thread_warnings(self):
        return [m for m in self.messages
                if "Destroyed while thread is still running" in m]


def _window(app, tmp_path):
    from block01.ui.main_window import MainWindow
    w = MainWindow()
    w.loader = _Loader()
    w._step0.loader = _Loader()
    w._step0.nucleus_channel = "DAPI"
    w.step0_output = {
        "step0_manifest_path": str(tmp_path / "a" / "step0_roi_result.json"),
        "step0_dir": str(tmp_path / "a"),
        "step1_dir": str(tmp_path / "a1"),
    }
    w.step0_done = True
    w._step1_context_ready = True
    w._p2_params = {"method": "cellpose_wholecell_fusion", "diameter": 30}
    return w


def _record_callbacks(w):
    seen = {"done": [], "error": [], "progress": []}
    w._on_fusion_done = lambda path: seen["done"].append(path)
    w._on_fusion_error = lambda msg: seen["error"].append(msg)
    w._on_fusion_progress = lambda d, t, m: seen["progress"].append((d, t, m))
    return seen


def _finish(worker):
    worker.release()
    worker.wait(5000)
    for _ in range(30):
        QtWidgets.QApplication.processEvents()


def test_the_top_step0_label_stays_clickable_while_fusion_runs(app, tmp_path):
    w = _window(app, tmp_path)
    worker = _FakeFusion()
    try:
        w._stack.setCurrentIndex(1)
        w._set_step_active(1)
        w._start_fusion_worker(worker, job_name="fusion", n_rows=1, n_cols=1)

        assert w._step0_lbl.isEnabled()
        w._step0_lbl.mousePressEvent(None)          # exactly what a click does
        assert w._stack.currentIndex() == 0
        # Leaving Step1 is not changing dataset: the job keeps its rights.
        assert worker.stop_called is False
        assert w._fusion_token is not None
    finally:
        _finish(worker)
        w.close()


def test_a_job_that_was_never_interrupted_still_delivers_its_result(app, tmp_path):
    w = _window(app, tmp_path)
    worker = _FakeFusion()
    seen = _record_callbacks(w)
    try:
        w._start_fusion_worker(worker, job_name="fusion", n_rows=1, n_cols=1)
        w._step0_lbl.mousePressEvent(None)          # same dataset, just Step0

        worker.progress.emit(1, 2, "half")
        worker.finished.emit("/tmp/a/fused.zarr")
        for _ in range(20):
            QtWidgets.QApplication.processEvents()

        assert seen["progress"] == [(1, 2, "half")]
        assert seen["done"] == ["/tmp/a/fused.zarr"]
    finally:
        _finish(worker)
        w.close()


def test_a_committed_dataset_switch_stops_and_retires_the_job(app, tmp_path):
    w = _window(app, tmp_path)
    worker = _FakeFusion()
    try:
        w._start_fusion_worker(worker, job_name="fusion", n_rows=1, n_cols=1)
        assert w._fusion_dialog is not None

        w._step0.dataset_committed.emit({"gen": 5})

        assert worker.stop_called is True
        assert w._fusion_token is None
        assert w._fusion_worker is None
        assert w._fusion_dialog is None
        assert w.step0_done is False
        assert w._step1_context_ready is False
    finally:
        _finish(worker)
        w.close()


def test_a_late_answer_from_a_retired_job_changes_nothing(app, tmp_path, monkeypatch):
    w = _window(app, tmp_path)
    worker = _FakeFusion()
    seen = _record_callbacks(w)
    boxes = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: boxes.append(a)))
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical",
                        staticmethod(lambda *a, **k: boxes.append(a)))
    try:
        w._start_fusion_worker(worker, job_name="fusion", n_rows=1, n_cols=1)
        w._step0.dataset_committed.emit({"gen": 6})

        worker.progress.emit(1, 2, "late")
        worker.finished.emit("/tmp/a/fused.zarr")
        worker.error.emit("late failure")
        for _ in range(20):
            QtWidgets.QApplication.processEvents()

        assert seen == {"done": [], "error": [], "progress": []}
        assert boxes == []
        assert w.step1_done is False
        assert w._fused_zarr_path is None
        assert w.step1_output is None
    finally:
        _finish(worker)
        w.close()


def test_an_invalidated_handoff_also_retires_the_job(app, tmp_path):
    w = _window(app, tmp_path)
    worker = _FakeFusion()
    seen = _record_callbacks(w)
    try:
        w._start_fusion_worker(worker, job_name="fusion", n_rows=1, n_cols=1)
        w._step0.handoff_invalidated.emit({
            "step0_manifest_path": w.step0_output["step0_manifest_path"],
            "geometry_revision": 1,
            "reason": "roi_changed",
            "message": "ROI changed.",
        })
        assert worker.stop_called is True
        assert w._step1_context_ready is False

        worker.finished.emit("/tmp/a/fused.zarr")
        for _ in range(20):
            QtWidgets.QApplication.processEvents()
        assert seen["done"] == []
    finally:
        _finish(worker)
        w.close()


def test_a_thread_that_ignores_stop_is_held_until_it_physically_ends(app, tmp_path):
    w = _window(app, tmp_path)
    worker = _FakeFusion(obeys_stop=False)
    try:
        w._start_fusion_worker(worker, job_name="fusion", n_rows=1, n_cols=1)
        w._step0.dataset_committed.emit({"gen": 7})

        # It did not stop, so it is still referenced rather than dropped.
        assert worker.stop_called is True
        assert worker.isRunning()
        assert worker in w._retired_fusion_workers

        worker.release()
        worker.wait(5000)
        for _ in range(30):
            QtWidgets.QApplication.processEvents()
        assert worker.isRunning() is False
        assert worker not in w._retired_fusion_workers
    finally:
        _finish(worker)
        w.close()


def test_business_finished_and_physical_finished_are_different_signals(app, tmp_path):
    w = _window(app, tmp_path)
    worker = _FakeFusion()
    _record_callbacks(w)
    order = []
    try:
        worker.finished.connect(lambda s: order.append(("business", s)))
        physical = QtCore.QThread.finished.__get__(worker, QtCore.QThread)
        physical.connect(lambda: order.append(("physical", None)))

        w._start_fusion_worker(worker, job_name="fusion", n_rows=1, n_cols=1)
        worker.finished.emit("/tmp/a/fused.zarr")
        for _ in range(10):
            QtWidgets.QApplication.processEvents()
        assert order == [("business", "/tmp/a/fused.zarr")]
        assert worker.isRunning()               # the thread is still alive

        _finish(worker)
        assert ("physical", None) in order
    finally:
        _finish(worker)
        w.close()


def test_retiring_with_no_job_running_is_a_no_op(app, tmp_path):
    w = _window(app, tmp_path)
    try:
        assert w._fusion_worker is None
        assert w._retire_fusion_worker("nothing to do") is False
        w._step0.dataset_committed.emit({"gen": 8})
        w._step0.dataset_committed.emit({"gen": 9})
        assert w._fusion_worker is None
        assert w._retired_fusion_workers == []
    finally:
        w.close()


def test_closing_the_window_leaves_no_running_thread_warning(app, tmp_path):
    with _QtMessages() as log:
        w = _window(app, tmp_path)
        worker = _FakeFusion()
        w._start_fusion_worker(worker, job_name="fusion", n_rows=1, n_cols=1)
        w.close()
        assert worker.stop_called is True
        assert worker.isRunning() is False
        del worker
        for _ in range(20):
            QtWidgets.QApplication.processEvents()
    assert log.thread_warnings() == []
