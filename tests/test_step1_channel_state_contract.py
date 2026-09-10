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


def test_a_first_tick_gives_the_channel_weight_one(app):
    """A channel the user just asked to see and cannot see is not an answer.

    The first tick a marker ever gets in this dataset sets its weight to 1.0.
    Only the first: from then on the number is the user's, and this panel does
    not replace it. (This module previously pinned the opposite -- a first tick
    leaving the weight at 0 -- which is what made a newly ticked channel
    invisible until the user found the slider.)
    """
    w = _window(app)
    try:
        assert w.config.channel_weight("CD3") == 0.0     # nobody has said yet
        w.config.set_channel_visible("CD3", True)
        assert w.config.channel_weight("CD3") == 1.0
        assert "CD3" in w.config.visible_channels()
    finally:
        w.close()


def test_a_weight_never_moves_a_tick(app):
    w = _window(app)
    try:
        w.config._rows["CD3"].spin.setValue(0.5)
        assert "CD3" not in w.config.visible_channels()

        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(0.0)
        assert "CD3" in w.config.visible_channels()   # ticked at zero is legal
    finally:
        w.close()


def test_re_ticking_brings_back_the_weight_unchanged(app):
    w = _window(app)
    try:
        w.config._rows["CD3"].spin.setValue(0.35)
        w.config.set_channel_visible("CD3", True)
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
        w.config._rows["CD3"].spin.setValue(1.0)
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
        w.config._rows["CD3"].spin.setValue(1.0)
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
        w.config._rows["CD3"].spin.setValue(1.0)
        window = {"CD3": {"min": 0.0, "max": 1.0, "gamma": 1.0},
                  "DAPI": {"min": 0.0, "max": 1.0, "gamma": 1.0}}
        monkeypatch.setattr(type(w), "_load_step0_remap_params",
                            lambda self: (window, "x"))
        monkeypatch.setattr(type(w), "_display_mapping",
                            lambda self, *a, **k: window)

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
        w.config._rows["CD3"].spin.setValue(1.0)
        window = {"CD3": {"min": 0.0, "max": 1.0, "gamma": 1.0}}
        monkeypatch.setattr(type(w), "_display_mapping",
                            lambda self, *a, **k: window)
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


def test_a_new_dataset_starts_from_zero(app):
    """A new dataset's markers start unticked at 0 -- and un-initialised, so
    the first tick in the NEW dataset answers 1.0 rather than inheriting the
    previous slide's 0.35."""
    w = _window(app)
    try:
        w.config._rows["CD3"].spin.setValue(0.35)

        # What loading another dataset does.
        w.config.load_panel({"markers": {"CD3": 0.0, "CD8": 0.0}}, "DAPI")

        assert w.config.channel_weight("CD3") == 0.0
        assert w.config.weight_initialized_channels() == []
        w.config.set_channel_visible("CD3", True)
        assert w.config.channel_weight("CD3") == 1.0
    finally:
        w.close()


def test_reset_zeroes_the_weights_and_leaves_the_ticks_alone(app):
    """It says weights, so it does weights: removing channels from the
    configuration is a different act, and the user did not ask for it."""
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(0.4)
        w.config._rows["CD8"].spin.setValue(0.9)
        before = list(w.config.visible_channels())

        w.config.zero_marker_weights()

        assert list(w.config.visible_channels()) == before
        assert w.config.channel_weight("CD3") == 0.0
        assert w.config.channel_weight("CD8") == 0.0
    finally:
        w.close()


def test_a_channel_row_has_no_hover_text_on_its_controls(app):
    """Removed on the user's call: a popup over the tick box, the colour
    swatch and the weight slider of every row in a long channel list is in
    the way of the work rather than an explanation of it.

    (This test used to assert the slider's tooltip WORDING. The rules it
    recited are in the module docstring now, read once instead of hovered
    over per channel.)
    """
    w = _window(app)
    try:
        for ch in ("CD3", "CD8", "DAPI"):
            row = w.config._rows[ch]
            assert row.checkbox.toolTip() == ""
            assert row.swatch.toolTip() == ""
            assert row.slider.toolTip() == ""
            assert row.spin.toolTip() == ""
    finally:
        w.close()


def test_the_nucleus_row_stays_read_only_without_saying_so_on_hover(app):
    """A disabled slider and a read-only box say it by themselves."""
    w = _window(app)
    try:
        row = w.config._rows["DAPI"]
        assert row.slider.isEnabled() is False
        assert row.spin.isReadOnly() is True
        assert row.slider.toolTip() == ""
    finally:
        w.close()


# ── the first tick's default, and every zero that is not an absence ──────
#
# A marker starts a dataset at 0 because nobody has said anything about it.
# A user can also deliberately set one to 0.00, and `Reset weights` sets all
# of them to 0, and a project file can carry an explicit 0. Same number, four
# different facts, and only the first may be replaced by a default -- so the
# panel records WHO said it, apart from the numbers. `weight == 0` can never
# be the test: it would overwrite the decision with the default.

def test_all_three_enable_entries_answer_the_same(app):
    """The checkbox, `set_channel_visible` and clicking a hidden row are one
    decision in one place, not three rules that can drift apart."""
    w = _window(app)
    try:
        # 1. the checkbox itself
        w.config._rows["CD3"].checkbox.setChecked(True)
        assert w.config.channel_weight("CD3") == 1.0

        # 2. the programmatic tick
        w.config.set_channel_visible("CD8", True)
        assert w.config.channel_weight("CD8") == 1.0
    finally:
        w.close()


def test_clicking_a_hidden_row_selects_ticks_and_weighs_it(app):
    w = _window(app)
    try:
        w.config.set_current_channel("CD3", auto_show=True)

        assert w.config.current_channel() == "CD3"
        assert "CD3" in w.config.visible_channels()
        assert w.config.channel_weight("CD3") == 1.0
    finally:
        w.close()


def test_an_edited_weight_survives_unticking_and_re_ticking(app):
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(0.35)
        w.config.set_channel_visible("CD3", False)
        w.config.set_channel_visible("CD3", True)

        assert w.config.channel_weight("CD3") == pytest.approx(0.35)
    finally:
        w.close()


def test_a_deliberate_zero_survives_unticking_and_re_ticking(app):
    """The case a `weight == 0` test would get wrong: the user said 0.00, and
    a re-tick must not read that as "nobody has said anything"."""
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(0.0)
        assert w.config.channel_weight("CD3") == 0.0

        w.config.set_channel_visible("CD3", False)
        w.config.set_channel_visible("CD3", True)

        assert w.config.channel_weight("CD3") == 0.0
        assert "CD3" in w.config.visible_channels()
    finally:
        w.close()


def test_reset_weights_zeros_are_answers_too(app):
    """`Reset weights` is the user saying zero. Re-ticking after it keeps the
    0 -- and the button still does not move a single tick."""
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config.set_channel_visible("CD8", True)
        ticks = list(w.config.visible_channels())

        w.config.zero_marker_weights()

        assert list(w.config.visible_channels()) == ticks
        w.config.set_channel_visible("CD3", False)
        w.config.set_channel_visible("CD3", True)
        assert w.config.channel_weight("CD3") == 0.0
    finally:
        w.close()


def test_a_weight_still_never_moves_a_tick_and_a_tick_never_moves_a_weight(app):
    """The first tick is a one-off initialisation, not weights and ticks tied
    back together."""
    w = _window(app)
    try:
        w.config._rows["CD3"].spin.setValue(0.5)
        assert "CD3" not in w.config.visible_channels()   # weight, no tick

        w.config.set_channel_visible("CD3", True)
        assert w.config.channel_weight("CD3") == pytest.approx(0.5), \
            "an already-weighted channel is not re-initialised by its tick"

        w.config._rows["CD3"].spin.setValue(0.0)
        assert "CD3" in w.config.visible_channels()       # zero stays ticked

        w.config.set_channel_visible("CD3", False)
        assert w.config.channel_weight("CD3") == 0.0      # untick keeps it
    finally:
        w.close()


def test_the_nucleus_weight_is_not_touched_by_the_first_tick_rule(app):
    """It comes from the Step0 handoff and is read-only in Step1."""
    w = _window(app)
    try:
        w.config.set_nucleus("DAPI", 0.6)
        w.config.set_channel_visible("DAPI", False)
        w.config.set_channel_visible("DAPI", True)

        assert w.config.channel_weight("DAPI") == pytest.approx(0.6)
        assert w.config.get_nucleus() == ("DAPI", pytest.approx(0.6))
    finally:
        w.close()


def test_a_visibility_observer_already_sees_the_final_weight(app):
    """The atomicity requirement: whoever handles the tick reads 1.0, not a
    weight of 0 that changes a moment later. Anything else shows a blank frame
    first, or saves an intermediate state."""
    w = _window(app)
    try:
        seen = []
        w.config.visibility_changed.connect(
            lambda ch, vis: seen.append((ch, vis, w.config.channel_weight(ch))))

        w.config.set_channel_visible("CD3", True)

        assert seen == [("CD3", True, 1.0)]
    finally:
        w.close()


def test_a_first_tick_does_not_announce_a_separate_weight_change(app):
    """One logical act, one notification. `config_changed` would schedule a
    second redraw of a state the visibility handler has already drawn."""
    w = _window(app)
    try:
        cfg_signals = []
        w.config.config_changed.connect(lambda: cfg_signals.append(1))

        w.config.set_channel_visible("CD3", True)

        assert cfg_signals == []
        assert w.config.channel_weight("CD3") == 1.0
    finally:
        w.close()


def test_a_first_tick_redraws_once_and_reads_nothing_twice(app):
    """One logical act, one repaint.

    The channel's pixels are already in hand, so the only thing that could
    draw twice is the state changing twice -- a weight announced separately
    from the tick schedules a second, coalesced redraw of what the first one
    already showed. Waited out past the coalescing window rather than only
    pumping events, so a pending timer is not simply missed.
    """
    from PyQt5 import QtTest
    w = _window(app)
    try:
        # Building the panel already scheduled one coalesced redraw
        # (`load_panel` announces the config it just loaded). Let it happen
        # before counting, or the tick inherits it and the count says two for
        # a reason that has nothing to do with the tick.
        QtTest.QTest.qWait(200)
        w.loader.reads.clear()
        redraws = []
        original = w._refresh_patch_preview

        def counting(*a, **kw):
            redraws.append(1)
            return original(*a, **kw)

        w._refresh_patch_preview = counting
        w.config.set_channel_visible("CD3", True)
        QtTest.QTest.qWait(200)

        assert w.loader.reads == [], \
            f"a cached channel was read again: {w.loader.reads}"
        assert len(redraws) == 1, f"{len(redraws)} redraws for one tick"
        assert w.config.channel_weight("CD3") == 1.0
    finally:
        w.close()


def test_the_saved_config_carries_the_first_tick_weight(app):
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)

        eff = w.config.effective_config()
        markers = eff["groups"]["markers"]["channels"]
        assert markers["CD3"] == 1.0

        w.config.set_channel_visible("CD3", False)
        eff = w.config.effective_config()
        assert "CD3" not in eff["groups"]["markers"]["channels"], \
            "an unticked channel is out of what gets fused"
        full = w.config.get_full_config()
        assert full["groups"]["markers"]["channels"]["CD3"] == 1.0, \
            "but the full config and the panel still show its weight"
        assert w.config.channel_weight("CD3") == 1.0
    finally:
        w.close()


def test_weights_loaded_from_a_file_are_answers_including_zero(app):
    """An explicit 0 in a project's fusion config is somebody's decision. A
    first tick afterwards must not overwrite it with 1.0."""
    w = _window(app)
    try:
        w.config.apply_full_config({
            "nucleus": {"channel": "DAPI", "weight": 1.0},
            "groups": {"markers": {"group_weight": 1.0,
                                   "channels": {"CD3": 0.0, "CD8": 0.4}}},
        })

        w.config.set_channel_visible("CD3", True)
        w.config.set_channel_visible("CD8", True)

        assert w.config.channel_weight("CD3") == 0.0
        assert w.config.channel_weight("CD8") == pytest.approx(0.4)
    finally:
        w.close()


def test_a_new_dataset_does_not_inherit_the_previous_history(app):
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(0.0)
        assert w.config.weight_initialized_channels() == ["CD3"]

        w.config.load_panel({"markers": {"CD3": 0.0, "CD8": 0.0}}, "DAPI")

        assert w.config.weight_initialized_channels() == []
        w.config.set_channel_visible("CD3", True)
        assert w.config.channel_weight("CD3") == 1.0
    finally:
        w.close()


def test_a_removed_channel_takes_its_history_with_it(app):
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(0.0)

        w.config.set_channels(["DAPI", "CD8"])
        assert w.config.weight_initialized_channels() == []

        w.config.set_channels(["DAPI", "CD3", "CD8"])
        w.config.set_channel_visible("CD3", True)
        assert w.config.channel_weight("CD3") == 1.0, \
            "a channel that left and came back is new again"
    finally:
        w.close()


def test_a_host_set_weight_is_not_overwritten_by_the_first_tick(app):
    """`set_channel_weight` is somebody naming a number.

    Found by `test_step1_fusion_core`, which builds a configuration through
    this API and then ticks the channels: the first-tick default overwrote
    every weight it had just been given. A programmatic weight is an answer
    like a user's edit or a file's value -- 0 included -- so the tick leaves
    it alone.
    """
    w = _window(app)
    try:
        w.config.set_channel_weight("CD3", 0.5)
        w.config.set_channel_weight("CD8", 0.0)

        w.config.set_channel_visible("CD3", True)
        w.config.set_channel_visible("CD8", True)

        assert w.config.channel_weight("CD3") == pytest.approx(0.5)
        assert w.config.channel_weight("CD8") == 0.0
    finally:
        w.close()


def test_the_nucleus_is_never_in_the_weight_history(app):
    """Its weight is the handoff's answer and read-only here, so the history
    -- which exists to decide first ticks -- has nothing to say about it, and
    a session must not carry it as if it did."""
    w = _window(app)
    try:
        assert "DAPI" not in w.config.weight_initialized_channels()

        w.config.set_nucleus("DAPI", 0.4)
        w.config.set_channel_weight("DAPI", 0.4)
        w.config.set_channel_visible("DAPI", False)
        w.config.set_channel_visible("DAPI", True)

        assert "DAPI" not in w.config.weight_initialized_channels()
        assert w.config.channel_weight("DAPI") == pytest.approx(0.4)
    finally:
        w.close()


def test_an_external_weight_reaches_the_groups_and_not_just_the_row(app):
    """One number on screen and a different one in the picture is the split
    this panel exists to prevent.

    `set_channel_weight` is the same act as moving the slider, so it has the
    same two consequences: the weight is an answer (no first-tick default
    over it) AND it is this channel's weight in every group it belongs to.
    Marking only the first left the row showing 0.5 while `get_groups`, the
    overlay, the fusion preview and every Save still read the 0 the group was
    loaded with.
    """
    w = _window(app)
    try:
        w.config.set_channel_weight("CD3", 0.5)
        w.config.set_channel_visible("CD3", True)

        assert w.config.channel_weight("CD3") == pytest.approx(0.5)
        assert w.config.get_groups()["markers"]["CD3"] == pytest.approx(0.5)
        eff = w.config.effective_config()
        assert eff["groups"]["markers"]["channels"]["CD3"] == pytest.approx(0.5)
    finally:
        w.close()


def test_a_first_tick_reaches_the_groups_too(app):
    """Same requirement for the default itself: what the tick puts on the row
    is what gets fused and saved."""
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)

        assert w.config.get_groups()["markers"]["CD3"] == 1.0
        eff = w.config.effective_config()
        assert eff["groups"]["markers"]["channels"]["CD3"] == 1.0
    finally:
        w.close()


def test_the_panels_own_row_syncing_claims_nothing(app):
    """`_sync_row_weight` is the row catching up with the model, not an
    answer about the channel: it must not defeat the first-tick default, and
    it must not flatten an old project's per-group weights by marking the
    channel edited."""
    w = _window(app)
    try:
        w.config.apply_full_config({
            "nucleus": {"channel": "DAPI", "weight": 1.0},
            "groups": {
                "a": {"group_weight": 1.0, "channels": {"CD3": 0.2}},
                "b": {"group_weight": 1.0, "channels": {"CD3": 0.8}},
            },
        })
        # The row shows the largest of the two; the groups keep their own.
        assert w.config.channel_weight("CD3") == pytest.approx(0.8)
        groups = w.config.get_groups()
        assert groups["a"]["CD3"] == pytest.approx(0.2)
        assert groups["b"]["CD3"] == pytest.approx(0.8)

        # And an explicit external set is an edit: it applies to both.
        w.config.set_channel_weight("CD3", 0.5)
        groups = w.config.get_groups()
        assert groups["a"]["CD3"] == pytest.approx(0.5)
        assert groups["b"]["CD3"] == pytest.approx(0.5)
    finally:
        w.close()


# ── the REAL Step0 -> Step1 initialisation path ──────────────────────────
#
# The manual acceptance test failed while all of the above passed, because
# all of the above builds the panel by calling `config.load_panel()` and the
# production handoff does not stop there. `_load_step0_roi_result()` ran:
#
#     set_channels -> load_panel -> set_nucleus -> _zero_marker_weights()
#
# and that last line is the Reset weights BUTTON. Once the weights somebody
# has given are remembered apart from the numbers, pressing it on every load
# marked every marker as already answered, so the first tick of each kept 0
# and the channel the user had just asked for stayed invisible. The
# handoff-contract module could not see it either: it stubbed that one line
# out with a no-op.
#
# These drive the real method with the real ConfigPanel. Nothing here may be
# replaced by a `load_panel()` call: the bug lived in what came after it.

def _handoff_window(app, tmp_path):
    """A Step1 panel built the way the application builds it."""
    from block01.ui.main_window import MainWindow
    w = MainWindow()
    w.loader = _Loader()
    w.step0_output = {
        "handoff_schema_version": 1,
        "output_dir": str(tmp_path), "step0_dir": str(tmp_path),
        "step1_dir": str(tmp_path), "step2_dir": str(tmp_path),
        "ome_tiff_path": _Loader.filepath,
        "rois": [{"name": "R1", "bbox_fullres": [0, 64, 0, 64]}],
        "patches": [(0, 32, 0, 32)],
        "panel_groups": {"markers": {"CD3": 0.0, "CD8": 0.0}},
        "corrected_zarr_path": "",
    }
    assert w._load_step0_roi_result(auto=True) is True, \
        "the real handoff did not complete; this test proves nothing"
    return w


def test_the_real_handoff_leaves_markers_unanswered(app, tmp_path):
    w = _handoff_window(app, tmp_path)
    try:
        assert w.config.visible_channels() == ["DAPI"]
        assert w.config.channel_weight("CD3") == 0.0
        assert w.config.channel_weight("CD8") == 0.0
        assert w.config.weight_initialized_channels() == [], (
            "the handoff marked markers as already weighted, so their first "
            "tick will keep 0 -- this is the manual acceptance failure")
    finally:
        w.close()


def test_after_the_real_handoff_the_checkbox_weighs_one(app, tmp_path):
    w = _handoff_window(app, tmp_path)
    try:
        w.config._rows["CD3"].checkbox.setChecked(True)

        assert w.config.channel_weight("CD3") == 1.0
        assert w.config.get_groups()["markers"]["CD3"] == 1.0
        eff = w.config.effective_config()
        assert eff["groups"]["markers"]["channels"]["CD3"] == 1.0
    finally:
        w.close()


def test_after_the_real_handoff_clicking_a_row_weighs_one(app, tmp_path):
    w = _handoff_window(app, tmp_path)
    try:
        w.config.set_current_channel("CD8", auto_show=True)

        assert w.config.current_channel() == "CD8"
        assert "CD8" in w.config.visible_channels()
        assert w.config.channel_weight("CD8") == 1.0
        assert w.config.get_groups()["markers"]["CD8"] == 1.0
    finally:
        w.close()


def test_after_the_real_handoff_an_edited_weight_survives_both_entries(
        app, tmp_path):
    w = _handoff_window(app, tmp_path)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(0.35)

        w.config.set_channel_visible("CD3", False)
        w.config._rows["CD3"].checkbox.setChecked(True)
        assert w.config.channel_weight("CD3") == pytest.approx(0.35)

        w.config.set_channel_visible("CD3", False)
        w.config.set_current_channel("CD3", auto_show=True)
        assert w.config.channel_weight("CD3") == pytest.approx(0.35)
    finally:
        w.close()


def test_after_the_real_handoff_a_deliberate_zero_survives(app, tmp_path):
    w = _handoff_window(app, tmp_path)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(0.0)

        w.config.set_channel_visible("CD3", False)
        w.config.set_channel_visible("CD3", True)
        assert w.config.channel_weight("CD3") == 0.0

        w.config.set_channel_visible("CD3", False)
        w.config.set_current_channel("CD3", auto_show=True)
        assert w.config.channel_weight("CD3") == 0.0
    finally:
        w.close()


def test_the_reset_button_still_remembers_its_zeros_after_a_real_handoff(
        app, tmp_path):
    """The button keeps its meaning: pressing it IS the user saying zero, so
    those zeros survive a re-tick. What changed is that nobody presses it on
    the user's behalf during a load."""
    w = _handoff_window(app, tmp_path)
    try:
        w.config.set_channel_visible("CD3", True)
        assert w.config.channel_weight("CD3") == 1.0

        w.config.zero_marker_weights()          # what the button does
        assert w.config.channel_weight("CD3") == 0.0
        assert "CD3" in w.config.visible_channels()

        w.config.set_channel_visible("CD3", False)
        w.config.set_channel_visible("CD3", True)
        assert w.config.channel_weight("CD3") == 0.0
        assert "CD8" in w.config.weight_initialized_channels(), \
            "the button answers for every marker, ticked or not"
    finally:
        w.close()


# ── a frame asks for the channels it draws ───────────────────────────────
#
# MEASURED on the real desk (docs/perf_timeline/prefix_run_excerpt.log): the
# first frame after a patch was drawn showed DAPI alone and held the GUI
# thread for 7493.66 ms, of which the remap it drew was 3.98 ms and the
# composite 19.65 ms. The other 7.47 seconds were `_display_mapping()` called
# with no arguments: `display_mapping_for_preview()` then completes the draft
# for EVERY channel the loader has, computing an automatic window per channel
# from whole-slide pixels, on the GUI thread. The window answered nothing for
# 7.6 s and the second patch the user had already drawn sat in the queue.

def _mapping_requests(w, monkeypatch):
    """Record which channels each frame asks for a display mapping for."""
    asked = []
    page = w._step0
    real = type(page).display_mapping_for_preview

    def spy(self, channels=None, blocking=True):
        asked.append(None if channels is None else list(channels))
        return real(self, channels=channels, blocking=blocking)

    monkeypatch.setattr(type(page), "display_mapping_for_preview", spy)
    return asked


def _auto_window_calls(w, monkeypatch):
    """Every channel an automatic window was computed for."""
    seeded = []
    page = w._step0
    monkeypatch.setattr(type(page), "_auto_display_window",
                        lambda self, channel, blocking=True:
                        (seeded.append(channel), None)[1])
    return seeded


@pytest.mark.parametrize("mode", ["overlay", "fusion"])
def test_a_frame_asks_only_for_the_channels_it_draws(app, monkeypatch, mode):
    w = _window(app)
    try:
        w.set_preview_mode(mode, force=True, reconcile=False)
        # DAPI is the only channel with pixels, which is the state the
        # measured frame was in: one channel ready, 28 not.
        w._patch_channel_cache[0] = {
            "DAPI": w._patch_channel_cache[0]["DAPI"]}
        asked = _mapping_requests(w, monkeypatch)
        seeded = _auto_window_calls(w, monkeypatch)

        w._refresh_patch_preview(reset_view=False)

        assert asked, "the frame did not ask for a mapping at all"
        for request in asked:
            assert request is not None, \
                "a render callback asked for EVERY channel the loader has"
            assert set(request) <= {"DAPI"}, request
        assert set(seeded) <= {"DAPI"}, \
            f"windows were computed for channels this frame does not draw: {seeded}"
    finally:
        w.close()


def test_a_save_still_asks_for_every_channel(app, monkeypatch):
    """The scoping is a render-path rule, not a new global one: a Save is
    about to fuse all of them and each one needs an explicit window."""
    w = _window(app)
    try:
        asked = _mapping_requests(w, monkeypatch)

        w._display_mapping()

        assert asked == [None], asked
    finally:
        w.close()


def test_an_empty_channel_list_means_no_channels(app, monkeypatch):
    """`None` and `[]` cannot mean the same thing here. Read as "all", an
    empty list is how a caller that legitimately has nothing to draw ends up
    seeding a window for every channel the loader has -- the 7.47 s the
    measured frame spent."""
    w = _window(app)
    try:
        page = w._step0
        page.loader = w.loader               # so `None` has a list to read
        page._auto_window_cache.clear()      # nothing memoised yet
        seeded = []
        monkeypatch.setattr(type(page), "_auto_display_window",
                            lambda self, channel, blocking=True:
                            (seeded.append(channel), None)[1])

        assert page.display_mapping_for_preview(channels=[]) == \
            page.display_mapping_draft()
        assert seeded == [], \
            f"an empty list seeded windows for {seeded}"

        page.display_mapping_for_preview(channels=None)
        expected = (set(w.loader.channel_names())
                    - set(page.display_mapping_draft()))
        assert set(seeded) == expected, seeded
    finally:
        w.close()


def test_the_pixel_source_can_refuse_to_read(app):
    """`blocking=False` is what a future background seeder needs: complete
    what is in memory, start no whole-slide read. The render paths do NOT use
    it -- a frame that skipped a window would draw a channel with a
    provisional percentile and stop matching what a Save writes."""
    w = _window(app)
    try:
        page = w._step0
        reads = []
        page._slide_lowres_array = lambda name, blocking=True: (
            reads.append((name, blocking)), None)[1]
        page._preview_provider = None

        assert page._workbench_pixels("CD3", blocking=False) is None
        assert reads == [("CD3", False)], reads
        assert page._auto_display_window("CD3", blocking=False) is None
    finally:
        w.close()
