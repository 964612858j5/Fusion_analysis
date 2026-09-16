"""Zooming in and then asking for one more channel keeps the view.

Ticking a channel the patch does not hold yet starts an incremental read. That
read brings pixels, not a new subject: the rectangle on screen is the same one,
so the camera must be exactly where the user put it when the pixels land.
`_preserve_view_after_patch_load` existed for this and was only ever cleared —
nothing wrote it — so every incremental load fell through to a refit.

Switching patches is a different act and still frames the new rectangle.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
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
    shape = (256, 256)

    def __init__(self):
        self._names = ["DAPI", "CD3", "CD8"]
        self.ch_map = {c: i for i, c in enumerate(self._names)}

    def channel_names(self):
        return list(self._names)

    @staticmethod
    def _norm(arr):
        return np.clip(np.asarray(arr, np.float32), 0.0, 1.0)

    def read_region(self, channel, y0, y1, x0, x1, downsample=1, normalize=True):
        rng = np.random.default_rng(abs(hash(channel)) % 997)
        return rng.random(((y1 - y0) or 1, (x1 - x0) or 1), dtype=np.float32)


def _window(app):
    from block01.ui.main_window import MainWindow
    w = MainWindow()
    w.loader = _Loader()
    w.config.set_channels(w.loader.channel_names())
    w.config.load_panel({"markers": {"CD3": 0.0, "CD8": 0.0}}, "DAPI")
    w.config.set_nucleus("DAPI", 1.0)
    w._all_patches = [(0, 32, 0, 32), (32, 64, 32, 64)]
    w._preview_patch_idx = 0
    rng = np.random.default_rng(7)
    w._patch_channel_cache[0] = {"DAPI": rng.random((32, 32), np.float32) + 0.1}
    w._patch_load_ready.add(0)
    # THE STEP THESE TESTS ARE ABOUT. A tick is per step since the 2026-09-16
    # ruling, so the ones the tests make below have to be made here.
    w._set_step_active(1)
    w.set_preview_mode("overlay", force=True, reconcile=False)
    w._render_overlay_patch(reset_view=True)
    return w


def _zoom(w, x=(4.0, 12.0), y=(6.0, 14.0)):
    w.prev_vb.disableAutoRange()
    w.prev_vb.setRange(xRange=x, yRange=y, padding=0)
    return _view(w)


def _view(w):
    """The camera as four numbers, so a comparison is a comparison."""
    return [float(v) for axis in w.prev_vb.viewRange() for v in axis]


def _finish(w, idx, channels):
    """Deliver a load the way the loader thread does, through the guard."""
    thread = w._patch_loaders.get(idx)
    rng = np.random.default_rng(11)
    cache = {ch: rng.random((32, 32), np.float32) + 0.1 for ch in channels}
    w._on_patch_loaded_from(thread, idx, cache)


def test_ticking_a_new_channel_keeps_the_zoom(app):
    w = _window(app)
    try:
        before = _zoom(w)
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        assert 0 in w._patch_loaders           # an incremental read started

        _finish(w, 0, ["CD3"])

        assert _view(w) == pytest.approx(before, abs=1e-6)
        assert "CD3" in w._patch_channel_cache[0]
    finally:
        w.close()


def test_switching_patches_still_frames_the_new_one(app):
    """Not every reset is wrong: a different rectangle deserves a fit."""
    w = _window(app)
    try:
        zoomed = _zoom(w)
        w._select_preview_patch(1)
        _finish(w, 1, ["DAPI"])

        assert _view(w) != pytest.approx(zoomed, abs=1e-6)
    finally:
        w.close()


def test_a_replaced_loader_cannot_restore_the_old_camera(app):
    """The user zooms, ticks a channel, then zooms somewhere else and ticks
    another. The first load must not drag the view back."""
    w = _window(app)
    try:
        _zoom(w, x=(0.0, 8.0), y=(0.0, 8.0))
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        stale = w._patch_loaders.get(0)

        moved = _zoom(w, x=(10.0, 20.0), y=(10.0, 20.0))
        w.config.set_channel_visible("CD8", True)
        w.config._rows["CD8"].spin.setValue(1.0)
        # One loader per patch: the second demand replaces the first.
        w._start_loader_for(0, needed=["CD8"])
        assert w._patch_loaders.get(0) is not stale

        # The replaced loader reports late.
        w._on_patch_loaded_from(stale, 0, {"CD3": np.ones((32, 32), np.float32)})

        assert _view(w) == pytest.approx(moved, abs=1e-6)
    finally:
        w.close()


def test_a_failed_incremental_load_does_not_move_the_camera(app):
    w = _window(app)
    try:
        before = _zoom(w)
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        thread = w._patch_loaders.get(0)

        w._on_patch_error_from(thread, 0, "read failed")

        assert _view(w) == pytest.approx(before, abs=1e-6)
    finally:
        w.close()


def test_the_first_load_of_a_patch_fits_it(app):
    """A patch with nothing cached has nothing to preserve."""
    w = _window(app)
    try:
        w._patch_channel_cache.pop(0, None)
        w._patch_load_ready.discard(0)
        w.prev_img.clear()
        _zoom(w)
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(1.0)

        assert w._preserve_view_after_patch_load.get(0) is None
    finally:
        w.close()


def test_a_camera_saved_for_a_loader_that_is_gone_is_not_spent(app):
    """The entry is bound to the load it was captured for. If that loader is
    no longer the patch's, the view it remembers describes a moment that has
    passed, and refitting is the honest answer."""
    w = _window(app)
    try:
        stale_view = _zoom(w, x=(0.0, 6.0), y=(0.0, 6.0))
        gone = object()
        w._preserve_view_after_patch_load[0] = (gone, {
            "view_range": [[0.0, 6.0], [0.0, 6.0]]})
        w._patch_loaders.pop(0, None)

        moved = _zoom(w, x=(12.0, 24.0), y=(12.0, 24.0))
        assert moved != stale_view

        w._on_patch_loaded(0, {"CD3": np.ones((32, 32), np.float32)})

        assert _view(w) != pytest.approx(stale_view, abs=1e-6)
    finally:
        w.close()
