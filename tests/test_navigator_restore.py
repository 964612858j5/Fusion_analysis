"""The Tissue Preview button always brings back the ONE navigator.

It can be out of sight two different ways, and neither is what `show()` fixes.
Collapsed to its header bar it is still a shown window, so `show()` does
nothing and the body stays hidden. Minimised by the window manager it is still
`isVisible()` — Qt counts an icon as shown — so `show()` is a no-op and
`raise_()` raises something nobody can see. The button then either did nothing
or, in Step0's toggle, "hid" an already invisible window.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets  # noqa: E402
from PyQt5.QtCore import Qt  # noqa: E402


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


def _is_up(popup):
    """Actually in front of the user: shown, not iconified, not collapsed."""
    return (popup.isVisible() and not popup.isMinimized()
            and not popup.is_minimized())


def test_a_hidden_navigator_comes_back(app):
    w = _window(app)
    try:
        w._step0.show_tissue_navigator()
        popup = w._step0._tissue_navigator_popup
        popup.hide()

        w._step0.show_tissue_navigator()
        assert _is_up(popup)
        assert w._step0._tissue_navigator_popup is popup
    finally:
        w.close()


def test_a_navigator_collapsed_to_its_bar_comes_back(app):
    w = _window(app)
    try:
        w._step0.show_tissue_navigator()
        popup = w._step0._tissue_navigator_popup
        popup.minimize_to_bar()
        assert popup.is_minimized()

        w._step0.show_tissue_navigator()
        assert not popup.is_minimized()
        assert popup.overview.isVisible()
        assert _is_up(popup)
        assert w._step0._tissue_navigator_popup is popup
    finally:
        w.close()


def test_a_navigator_minimised_by_the_window_manager_comes_back(app):
    w = _window(app)
    try:
        w._step0.show_tissue_navigator()
        popup = w._step0._tissue_navigator_popup
        popup.setWindowState(popup.windowState() | Qt.WindowMinimized)
        assert popup.isMinimized()

        w._step0.show_tissue_navigator()
        assert not popup.isMinimized()
        assert _is_up(popup)
        assert w._step0._tissue_navigator_popup is popup
    finally:
        w.close()


@pytest.mark.parametrize("put_away", ["hide", "bar", "window"])
def test_the_step1_button_restores_it_too(app, put_away):
    w = _window(app)
    try:
        w._show_tissue_navigator()
        popup = w._step0._tissue_navigator_popup
        if put_away == "hide":
            popup.hide()
        elif put_away == "bar":
            popup.minimize_to_bar()
        else:
            popup.setWindowState(popup.windowState() | Qt.WindowMinimized)

        w._show_tissue_navigator()

        assert _is_up(popup)
        assert w._step0._tissue_navigator_popup is popup
    finally:
        w.close()


@pytest.mark.parametrize("put_away", ["bar", "window"])
def test_the_step0_toggle_restores_rather_than_hides(app, put_away):
    """A put-away window is not "up", so the toggle's job is to bring it back.
    Hiding it again left the user pressing a button that did nothing."""
    w = _window(app)
    try:
        w._step0.toggle_tissue_navigator()
        popup = w._step0._tissue_navigator_popup
        if put_away == "bar":
            popup.minimize_to_bar()
        else:
            popup.setWindowState(popup.windowState() | Qt.WindowMinimized)

        w._step0.toggle_tissue_navigator()

        assert _is_up(popup)
    finally:
        w.close()


def test_the_toggle_still_hides_a_navigator_that_is_up(app):
    w = _window(app)
    try:
        w._step0.toggle_tissue_navigator()
        popup = w._step0._tissue_navigator_popup
        assert _is_up(popup)

        w._step0.toggle_tissue_navigator()
        assert not popup.isVisible()
    finally:
        w.close()


def test_restoring_keeps_the_camera_the_rois_and_the_patches(app):
    """Restoring is not rebuilding: same popup, same overview, same view."""
    w = _window(app)
    try:
        w._step0.show_tissue_navigator()
        popup = w._step0._tissue_navigator_popup
        overview = popup.overview
        overview.vb.setRange(xRange=(10, 40), yRange=(5, 35), padding=0)
        camera = [list(axis) for axis in overview.vb.viewRange()]
        rois = list(overview._rois)
        patches = list(overview._patches)

        popup.minimize_to_bar()
        popup.setWindowState(popup.windowState() | Qt.WindowMinimized)
        w._show_tissue_navigator()

        assert popup.overview is overview
        assert [list(a) for a in overview.vb.viewRange()] == camera
        assert list(overview._rois) == rois
        assert list(overview._patches) == patches
        from block01.ui.widgets.tissue_navigator_popup import TissueNavigatorPopup
        assert len(w.findChildren(TissueNavigatorPopup)) == 1
    finally:
        w.close()
