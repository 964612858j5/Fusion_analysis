"""Step0 and Step1 share ONE Tissue Preview / ROI Navigator.

Step1 used to build its own tissue thumbnail (a third `OverviewPanel`) with its
own patch editor, so ROI/patch geometry had two editors and two coordinate
conventions. Step1 now borrows Step0's popup through a single public entry
point; Step0Page keeps sole ownership of the widget's lifecycle.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined into
one process.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets  # noqa: E402


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


def _window(app):
    from block01.ui.main_window import MainWindow
    w = MainWindow()
    w.loader = _Loader()
    w._step0.loader = _Loader()
    w._step0.nucleus_channel = "DAPI"
    return w


def _popups(w):
    from block01.ui.widgets.tissue_navigator_popup import TissueNavigatorPopup
    return [c for c in w.findChildren(TissueNavigatorPopup)]


def test_step0_and_step1_open_the_same_popup_object(app):
    w = _window(app)
    try:
        w._step0.show_tissue_navigator()
        from_step0 = w._step0._tissue_navigator_popup
        w._show_tissue_navigator()
        from_step1 = w._step0._tissue_navigator_popup

        assert from_step1 is from_step0
        assert id(from_step1) == id(from_step0)
    finally:
        w.close()


def test_opening_from_step1_creates_no_second_navigator(app):
    w = _window(app)
    try:
        w._show_tissue_navigator()
        w._show_tissue_navigator()
        w._step0.show_tissue_navigator()

        assert len(_popups(w)) == 1
        popup = w._step0._tissue_navigator_popup
        # One navigator window, one overview inside it, one ROI model.
        from block01.ui.step0.overview_panel import OverviewPanel
        assert len(popup.findChildren(OverviewPanel)) == 1
        assert popup.overview is not w._step0.overview
        assert w._step0._roi_model is not None
    finally:
        w.close()


def test_step1_never_owns_the_popup(app):
    w = _window(app)
    try:
        w._show_tissue_navigator()
        popup = w._step0._tissue_navigator_popup
        # Ownership stays with the page that created it; Step1 holds no handle
        # of its own that could tear it down twice.
        assert popup.parent() is w._step0
        assert not hasattr(w, "_tissue_navigator_popup")
    finally:
        w.close()


def test_the_shared_model_is_the_one_both_overviews_render(app):
    w = _window(app)
    try:
        w._show_tissue_navigator()
        panels = w._step0._registered_roi_overviews()
        assert len(panels) == 2
        assert w._step0.overview in panels
        assert w._step0._tissue_navigator_popup.overview in panels
    finally:
        w.close()
