"""The controls the user actually touches change the pixels actually shown.

These tests drive the REAL widgets — the slider and spin box inside the channel
row, the Intensity window's own parameter change — and read the REAL ImageItem
afterwards, letting the REAL coalescing timer fire. Asserting that a fusion
helper returns different numbers proves nothing about a screen that never got
them: the failure being pinned here was exactly that, a live control whose
value moved while the picture did not.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import os
import time

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
    shape = (128, 128)

    def __init__(self):
        self._names = ["DAPI", "CD3", "CD8"]
        self.ch_map = {c: i for i, c in enumerate(self._names)}
        self.reads = []

    def channel_names(self):
        return list(self._names)

    @staticmethod
    def _norm(arr):
        return np.clip(np.asarray(arr, np.float32), 0.0, 1.0)

    def read_region(self, channel, y0, y1, x0, x1, downsample=1, normalize=True):
        self.reads.append(channel)
        rng = np.random.default_rng(abs(hash(channel)) % 997)
        return rng.random(((y1 - y0) or 1, (x1 - x0) or 1), dtype=np.float32)


def _window(app):
    from block01.ui.main_window import MainWindow
    w = MainWindow()
    w.loader = _Loader()
    w.config.set_channels(w.loader.channel_names())
    w.config.load_panel({"markers": {"CD3": 0.0, "CD8": 0.0}}, "DAPI")
    w.config.set_nucleus("DAPI", 1.0)
    w._all_patches = [(0, 32, 0, 32)]
    w._preview_patch_idx = 0
    rng = np.random.default_rng(5)
    w._patch_channel_cache[0] = {
        ch: rng.random((32, 32), dtype=np.float32) * 0.8 + 0.1
        for ch in ("DAPI", "CD3", "CD8")}
    w._patch_load_ready.add(0)
    w.config.set_channel_visible("CD3", True)          # in the configuration
    return w


def _settle(w, timeout=2.0):
    """Let the coalesced publication happen, as the event loop would."""
    deadline = time.monotonic() + timeout
    while w._prev_timer.isActive() and time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.005)
    QtWidgets.QApplication.processEvents()


def _screen(w):
    """What is on the ImageItem right now."""
    return None if w.prev_img.image is None else np.asarray(w.prev_img.image).copy()


def _drag(w, channel, values):
    """Move the REAL slider through `values` (0..1), as a drag does."""
    row = w.config._rows[channel]
    for value in values:
        row.slider.setValue(int(round(float(value) * 100)))


@pytest.mark.parametrize("mode", ["overlay", "fusion"])
def test_the_weight_slider_changes_the_picture_on_screen(app, mode):
    w = _window(app)
    try:
        w.set_preview_mode(mode, force=True, reconcile=False)
        _drag(w, "CD3", [1.0])
        _settle(w)
        full = _screen(w)
        assert full is not None

        _drag(w, "CD3", [0.25])
        _settle(w)
        quarter = _screen(w)

        _drag(w, "CD3", [0.0])
        _settle(w)
        none_at_all = _screen(w)

        assert not np.array_equal(full, quarter)
        assert not np.array_equal(quarter, none_at_all)
        # And the tick is where the user left it, at every step.
        assert "CD3" in w.config.visible_channels()
    finally:
        w.close()


@pytest.mark.parametrize("mode", ["overlay", "fusion"])
def test_a_drag_publishes_once_and_shows_where_it_stopped(app, mode):
    w = _window(app)
    try:
        w.set_preview_mode(mode, force=True, reconcile=False)
        _drag(w, "CD3", [1.0])
        _settle(w)

        published = []
        real = w.prev_img.setImage
        w.prev_img.setImage = lambda img, **k: (
            published.append(np.asarray(img).copy()) or real(img, **k))
        reads = len(w.loader.reads)

        _drag(w, "CD3", [0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.15, 0.1])
        assert published == []                 # nothing drawn mid-drag
        _settle(w)

        assert len(published) == 1
        assert len(w.loader.reads) == reads    # and no channel was re-read
        assert w.config.channel_weight("CD3") == pytest.approx(0.1)

        # The one frame published is the state the drag ended on.
        w._refresh_patch_preview(reset_view=False)
        assert np.array_equal(published[-1], _screen(w))
    finally:
        w.close()


@pytest.mark.parametrize("mode", ["overlay", "fusion"])
def test_the_intensity_window_changes_the_picture_on_screen(app, mode, monkeypatch):
    """Driven through Step0's own signal, the one the workbench emits."""
    w = _window(app)
    try:
        window = {"CD3": {"min": 0.0, "max": 1.0, "gamma": 1.0},
                  "DAPI": {"min": 0.0, "max": 1.0, "gamma": 1.0}}
        monkeypatch.setattr(type(w), "_display_mapping",
                            lambda self, *a, **k: window)
        w.set_preview_mode(mode, force=True, reconcile=False)
        _drag(w, "CD3", [1.0])
        _settle(w)
        wide = _screen(w)
        assert wide is not None

        window["CD3"] = {"min": 0.0, "max": 0.3, "gamma": 1.0}
        w._step0.display_mapping_changed.emit("CD3")
        _settle(w)

        assert not np.array_equal(wide, _screen(w))
    finally:
        w.close()


def test_five_mapping_changes_publish_one_picture(app, monkeypatch):
    w = _window(app)
    try:
        window = {"CD3": {"min": 0.0, "max": 1.0, "gamma": 1.0}}
        monkeypatch.setattr(type(w), "_display_mapping",
                            lambda self, *a, **k: window)
        w.set_preview_mode("fusion", force=True, reconcile=False)
        _drag(w, "CD3", [1.0])
        _settle(w)

        published = []
        real = w.prev_img.setImage
        w.prev_img.setImage = lambda img, **k: (
            published.append(1) or real(img, **k))

        for top in (0.9, 0.7, 0.5, 0.4, 0.3):
            window["CD3"] = {"min": 0.0, "max": top, "gamma": 1.0}
            w._step0.display_mapping_changed.emit("CD3")
        assert published == []
        _settle(w)

        assert len(published) == 1
    finally:
        w.close()


def test_editing_a_channel_that_is_not_in_the_picture_redraws_nothing(app, monkeypatch):
    """CD8 is neither ticked nor current: its window is nobody's business
    here, and redrawing for it would be work with nothing to show."""
    w = _window(app)
    try:
        window = {"CD8": {"min": 0.0, "max": 1.0, "gamma": 1.0}}
        monkeypatch.setattr(type(w), "_display_mapping",
                            lambda self, *a, **k: window)
        w.set_preview_mode("overlay", force=True, reconcile=False)
        _drag(w, "CD3", [1.0])
        _settle(w)

        published = []
        real = w.prev_img.setImage
        w.prev_img.setImage = lambda img, **k: (
            published.append(1) or real(img, **k))

        window["CD8"] = {"min": 0.0, "max": 0.2, "gamma": 1.0}
        w._step0.display_mapping_changed.emit("CD8")
        _settle(w)

        assert published == []
    finally:
        w.close()


def test_a_weight_drag_starts_no_worker(app):
    w = _window(app)
    try:
        started = []
        w._start_loader_for = lambda idx, needed=None: started.append(needed)
        w.set_preview_mode("fusion", force=True, reconcile=False)

        _drag(w, "CD3", [0.9, 0.5, 0.2])
        _settle(w)

        assert started == []            # everything it needs is already cached
        assert w._fusion_worker is None
    finally:
        w.close()
