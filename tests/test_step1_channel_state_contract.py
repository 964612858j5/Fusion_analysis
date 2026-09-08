"""ONE channel state, and both previews plus every Save read it.

The state table lives in `ui/step0/config_panel.py`: selected, checked, weight,
colour, Min/Max/Gamma. What is pinned here is that nothing reads a different
subset of it — the overlay used to ignore the weight while the fusion ignored
the tick, so one panel of controls described two pictures and neither was the
one a Save would write.

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
    shape = (256, 256)

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
        rng = np.random.default_rng(abs(hash(channel)) % 1000)
        return rng.random(((y1 - y0) or 1, (x1 - x0) or 1), dtype=np.float32)


def _window(app, size=32):
    from block01.ui.main_window import MainWindow
    w = MainWindow()
    w.loader = _Loader()
    w.config.set_channels(w.loader.channel_names())
    w.config.load_panel({"markers": {"CD3": 0.0, "CD8": 0.0}}, "DAPI")
    w.config.set_nucleus("DAPI", 1.0)
    w._all_patches = [(0, size, 0, size)]
    w._preview_patch_idx = 0
    rng = np.random.default_rng(3)
    w._patch_channel_cache[0] = {
        ch: rng.random((size, size), dtype=np.float32) + 0.1
        for ch in ("DAPI", "CD3", "CD8")}
    w._patch_load_ready.add(0)
    return w


def _image(w):
    return None if w.prev_img.image is None else np.asarray(w.prev_img.image).copy()


# ── the tick / weight contract ───────────────────────────────────────────

def test_a_new_dataset_shows_only_the_nucleus(app):
    w = _window(app)
    try:
        assert w.config.visible_channels() == ["DAPI"]
        assert w.config.channel_weight("DAPI") == 1.0
    finally:
        w.close()


def test_ticking_a_channel_at_zero_makes_it_contribute(app):
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        assert w.config.channel_weight("CD3") > 0
        weighted = set()
        for data in w._effective_fusion_config()["groups"].values():
            weighted.update(data["channels"])
        assert "CD3" in weighted
    finally:
        w.close()


def test_a_weight_above_zero_ticks_and_a_weight_of_zero_unticks(app):
    w = _window(app)
    try:
        w.config._rows["CD3"].spin.setValue(0.5)
        assert "CD3" in w.config.visible_channels()

        w.config._rows["CD3"].spin.setValue(0.0)
        assert "CD3" not in w.config.visible_channels()
    finally:
        w.close()


def test_re_ticking_restores_the_weight_the_user_had(app):
    w = _window(app)
    try:
        w.config._rows["CD3"].spin.setValue(0.35)
        w.config.set_channel_visible("CD3", False)
        w.config.set_channel_visible("CD3", True)
        assert w.config.channel_weight("CD3") == pytest.approx(0.35)
    finally:
        w.close()


def test_ticking_another_channel_does_not_move_the_selection(app):
    w = _window(app)
    try:
        w.config._rows["CD3"].selected.emit("CD3")
        assert w.config.current_channel() == "CD3"

        w.config._rows["CD8"].checkbox.setChecked(True)
        assert w.config.current_channel() == "CD3"
    finally:
        w.close()


# ── both previews, one configuration ─────────────────────────────────────

def test_the_weight_changes_the_overlay_and_the_fusion_alike(app):
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(1.0)

        w.set_preview_mode("overlay", force=True, reconcile=False)
        w._render_overlay_patch(reset_view=True)
        overlay_full = _image(w)
        w.set_preview_mode("fusion", force=True, reconcile=False)
        w._render_current_patch(reset_view=True)
        fusion_full = _image(w)

        reads = len(w.loader.reads)
        w.config._rows["CD3"].spin.setValue(0.25)

        w.set_preview_mode("overlay", force=True, reconcile=False)
        w._render_overlay_patch(reset_view=False)
        overlay_quarter = _image(w)
        w.set_preview_mode("fusion", force=True, reconcile=False)
        w._render_current_patch(reset_view=False)
        fusion_quarter = _image(w)

        assert not np.array_equal(overlay_full, overlay_quarter)
        assert not np.array_equal(fusion_full, fusion_quarter)
        assert len(w.loader.reads) == reads          # no disk read for a weight
    finally:
        w.close()


def test_an_unticked_channel_reaches_neither_preview_nor_the_saved_config(app):
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(0.8)
        w.config.set_channel_visible("CD3", False)

        effective = w._effective_fusion_config()
        weighted = set()
        for data in effective["groups"].values():
            weighted.update(data["channels"])
        assert "CD3" not in weighted
        assert "CD3" not in w._fusion_weighted_channels()
        # ...and the panel still remembers it for when it is ticked again.
        assert w.config.channel_weight("CD3") == pytest.approx(0.8)
        assert "CD3" in w.config.get_full_config()["groups"]["markers"]["channels"]
    finally:
        w.close()


def test_unticking_the_nucleus_takes_it_out_of_the_fusion(app):
    w = _window(app)
    try:
        w.config.set_channel_visible("DAPI", False)
        assert w._effective_fusion_config()["nucleus"]["weight"] == 0.0
    finally:
        w.close()


def test_nothing_ticked_says_so_rather_than_looking_broken(app):
    w = _window(app)
    try:
        w.config.set_channel_visible("DAPI", False)
        w.set_preview_mode("overlay", force=True, reconcile=False)
        w._render_overlay_patch(reset_view=True)

        assert "tick" in w.prev_status.text().lower()
    finally:
        w.close()


def test_a_mode_switch_keeps_every_piece_of_state(app):
    w = _window(app)
    try:
        w.config._rows["CD3"].selected.emit("CD3")
        w.config._rows["CD3"].spin.setValue(0.6)
        w.config.set_channel_color("CD3", "#123456")
        before = (w.config.current_channel(), list(w.config.visible_channels()),
                  w.config.channel_weight("CD3"), w.config.channel_color("CD3"))

        w.set_preview_mode("fusion", force=True, reconcile=False)
        w.set_preview_mode("overlay", force=True, reconcile=False)

        assert (w.config.current_channel(), list(w.config.visible_channels()),
                w.config.channel_weight("CD3"),
                w.config.channel_color("CD3")) == before
    finally:
        w.close()


# ── colour, one answer ───────────────────────────────────────────────────

def test_a_step1_colour_reaches_the_intensity_window(app):
    w = _window(app)
    try:
        w.config.set_channel_color("CD3", "#ff0000")
        assert w._step0._channel_color_hex("CD3") == "#ff0000"
    finally:
        w.close()


def test_a_colour_picked_in_the_intensity_window_comes_back(app):
    w = _window(app)
    try:
        w._step0._apply_channel_color("CD3", (0.0, 1.0, 0.0))
        assert w.config.channel_color("CD3").lower() == "#00ff00"
    finally:
        w.close()


# ── coalescing ───────────────────────────────────────────────────────────

def test_a_burst_of_weight_changes_draws_once_and_shows_the_last_one(app):
    """Latest wins. Ten slider steps must not be ten full-array composites,
    and what is left on screen is the state the user stopped on."""
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.set_preview_mode("overlay", force=True, reconcile=False)
        draws = []
        real = w._refresh_patch_preview
        w._refresh_patch_preview = lambda reset_view=False: (
            draws.append(reset_view) or real(reset_view=reset_view))

        for value in (0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.15, 0.1):
            w.config._rows["CD3"].spin.setValue(value)
        assert draws == []                       # nothing drawn mid-drag

        QtWidgets.QApplication.processEvents()
        deadline = time.monotonic() + 2.0
        while not draws and time.monotonic() < deadline:
            QtWidgets.QApplication.processEvents()
            time.sleep(0.01)

        assert len(draws) == 1
        coalesced = _image(w)

        w._render_overlay_patch(reset_view=False)
        assert np.array_equal(coalesced, _image(w))   # the last state, drawn
    finally:
        w.close()


def test_the_coalesced_redraw_costs_no_disk_read(app):
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        reads = len(w.loader.reads)
        for value in (0.9, 0.5, 0.2):
            w.config._rows["CD3"].spin.setValue(value)
        QtWidgets.QApplication.processEvents()
        assert len(w.loader.reads) == reads
    finally:
        w.close()


def _settle(w, timeout=2.0):
    """Let the coalesced redraw land."""
    deadline = time.monotonic() + timeout
    while w._prev_timer.isActive() and time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.005)
    QtWidgets.QApplication.processEvents()


@pytest.mark.parametrize("mode", ["overlay", "fusion"])
def test_the_intensity_window_changes_both_previews(app, mode, monkeypatch):
    """Both map through Min/Max/Gamma, so both follow it. Only the overlay
    used to redraw, leaving the fusion preview showing the window the user had
    just moved away from."""
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        window = {"CD3": {"min": 0.0, "max": 1.0, "gamma": 1.0},
                  "DAPI": {"min": 0.0, "max": 1.0, "gamma": 1.0}}
        monkeypatch.setattr(type(w), "_load_step0_remap_params",
                            lambda self: (window, "x"))
        monkeypatch.setattr(type(w), "_display_mapping", lambda self: window)

        w.set_preview_mode(mode, force=True, reconcile=False)
        _settle(w)                       # nothing else pending
        w._refresh_patch_preview(reset_view=True)
        wide = _image(w)
        assert wide is not None

        window["CD3"] = {"min": 0.0, "max": 0.3, "gamma": 1.0}
        w._step0.display_mapping_changed.emit("CD3")
        _settle(w)

        assert not np.array_equal(wide, _image(w))
    finally:
        w.close()


def test_a_burst_of_mapping_changes_publishes_once(app, monkeypatch):
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        window = {"CD3": {"min": 0.0, "max": 1.0, "gamma": 1.0}}
        monkeypatch.setattr(type(w), "_display_mapping", lambda self: window)
        w.set_preview_mode("fusion", force=True, reconcile=False)
        draws = []
        real = w._refresh_patch_preview
        w._refresh_patch_preview = lambda reset_view=False: (
            draws.append(1) or real(reset_view=reset_view))

        for top in (0.9, 0.7, 0.5, 0.3, 0.2):
            window["CD3"] = {"min": 0.0, "max": top, "gamma": 1.0}
            w._step0.display_mapping_changed.emit("CD3")
        assert draws == []

        _settle(w)
        assert len(draws) == 1
    finally:
        w.close()


def test_a_new_dataset_does_not_inherit_the_last_slides_weights(app):
    """Ticking a marker on a new slide must not bring back the weight it had
    on the previous one."""
    w = _window(app)
    try:
        w.config._rows["CD3"].spin.setValue(0.35)
        assert w.config.channel_weight("CD3") == pytest.approx(0.35)

        # What loading another dataset does.
        w.config.load_panel({"markers": {"CD3": 0.0, "CD8": 0.0}}, "DAPI")
        assert w.config.channel_weight("CD3") == 0.0

        w.config.set_channel_visible("CD3", True)
        assert w.config.channel_weight("CD3") == pytest.approx(
            w.config.DEFAULT_TICK_WEIGHT)
    finally:
        w.close()


def test_reset_unticks_the_markers_and_forgets_their_weights(app):
    """A reset that re-ticking can undo is not a reset."""
    w = _window(app)
    try:
        w.config._rows["CD3"].spin.setValue(0.4)
        w.config._rows["CD8"].spin.setValue(0.9)

        w.config.zero_marker_weights()

        assert w.config.visible_channels() == ["DAPI"]
        assert w.config.channel_weight("CD3") == 0.0

        w.config.set_channel_visible("CD3", True)
        assert w.config.channel_weight("CD3") == pytest.approx(
            w.config.DEFAULT_TICK_WEIGHT)
    finally:
        w.close()


def test_the_weight_control_says_what_it_now_does(app):
    w = _window(app)
    try:
        tip = w.config._rows["CD3"].slider.toolTip().lower()
        assert "not used by the overlay" not in tip
        assert "overlay" in tip and "fusion" in tip
    finally:
        w.close()
