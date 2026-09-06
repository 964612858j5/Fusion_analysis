"""Sharing the navigator adds no second teardown and no dangling thread.

The popup has one owner (Step0Page) and one lifecycle. Closing and reopening it,
committing a dataset switch, and closing the main window must not produce a
double delete, a signal into a dead object, or Qt's
"QThread: Destroyed while thread is still running".

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
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
    filepath = "/tmp/dataset.ome.tiff"
    shape = (64, 64)
    ch_map = {"DAPI": 0, "CD3": 1}

    def channel_names(self):
        return ["DAPI", "CD3"]

    def read_region(self, channel, y0, y1, x0, x1, downsample=1):
        ds = max(1, int(downsample))
        return np.zeros(((y1 - y0) // ds or 1, (x1 - x0) // ds or 1), np.float32)


class _QtMessages:
    """Collect Qt's own warnings (QThread destruction, dead receivers)."""

    def __enter__(self):
        self.messages = []
        self._prev = QtCore.qInstallMessageHandler(
            lambda _mode, _ctx, msg: self.messages.append(str(msg)))
        return self

    def __exit__(self, *_exc):
        QtCore.qInstallMessageHandler(self._prev)
        return False

    def offending(self):
        bad = ("QThread: Destroyed while thread is still running",
               "Destroyed while thread is still running",
               "already deleted", "wrapped C/C++ object")
        return [m for m in self.messages if any(b in m for b in bad)]


def _window(app):
    from block01.ui.main_window import MainWindow
    w = MainWindow()
    w.loader = _Loader()
    w._step0.loader = _Loader()
    w._step0.nucleus_channel = "DAPI"
    return w


def test_closing_and_reopening_the_navigator_keeps_one_instance(app):
    from block01.ui.widgets.tissue_navigator_popup import TissueNavigatorPopup

    with _QtMessages() as log:
        w = _window(app)
        try:
            w._show_tissue_navigator()
            popup = w._step0._tissue_navigator_popup
            popup.close()
            w._show_tissue_navigator()
            w._step0.show_tissue_navigator()

            assert w._step0._tissue_navigator_popup is popup
            assert len(w.findChildren(TissueNavigatorPopup)) == 1
        finally:
            w.close()
        QtWidgets.QApplication.processEvents()
    assert log.offending() == []


def test_a_dataset_switch_then_close_leaves_no_running_thread(app):
    with _QtMessages() as log:
        w = _window(app)
        try:
            w._show_tissue_navigator()
            w._active_roi = {"name": "ROI_1", "bbox_fullres": [0, 32, 0, 32]}
            w._rois = [w._active_roi]
            w._on_patches([(0, 16, 0, 16)])
            w._step0.dataset_committed.emit({"gen": 2})
            assert w._patch_loaders == {}
        finally:
            w.close()
        QtWidgets.QApplication.processEvents()
    assert log.offending() == []


def test_the_navigator_survives_step_navigation_without_reparenting(app):
    w = _window(app)
    try:
        w._show_tissue_navigator()
        popup = w._step0._tissue_navigator_popup
        parent_before = popup.parent()
        w._stack.setCurrentIndex(0)
        w._go_to_step0()
        w._show_tissue_navigator()
        assert popup.parent() is parent_before
        assert w._step0._tissue_navigator_popup is popup
    finally:
        w.close()
