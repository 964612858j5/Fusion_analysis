"""Overlay is display; weight is fusion. They must not leak into each other.

The channel row carries three separate states: which channel is being edited,
which channels are drawn, and how much each contributes to the fusion. The
overlay reads the checkbox, the channel colour and the Intensity window's
Min/Max/Gamma, and never the fusion weight — so a ticked channel is always
visible, whatever its weight, and moving a weight slider costs no disk read.

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
    shape = (256, 256)

    def __init__(self):
        self._names = ["DAPI", "CD3", "CD8"]
        self.ch_map = {c: i for i, c in enumerate(self._names)}
        self.reads = []

    def channel_names(self):
        return list(self._names)

    @staticmethod
    def _norm(arr):
        arr = arr.astype(np.float32)
        nz = arr[arr > 0]
        if nz.size < 100:
            return np.zeros_like(arr)
        lo, hi = np.percentile(nz, [1.0, 99.5])
        if hi <= lo:
            return np.zeros_like(arr)
        return np.clip((arr - lo) / (hi - lo), 0.0, 1.0)

    def read_region(self, channel, y0, y1, x0, x1, downsample=1, normalize=True):
        self.reads.append(channel)
        ds = max(1, int(downsample))
        rng = np.random.default_rng(abs(hash(channel)) % 1000)
        return rng.random(((y1 - y0) // ds or 1, (x1 - x0) // ds or 1),
                          dtype=np.float32)


def _window(app, cached=("DAPI", "CD3", "CD8")):
    from block01.ui.main_window import MainWindow
    w = MainWindow()
    w.loader = _Loader()
    chans = w.loader.channel_names()
    w.config.set_channels(chans)
    w.config.load_panel({"markers": {"CD3": 0.0, "CD8": 0.0}}, "DAPI")
    w.config.set_nucleus("DAPI", 1.0)

    w._all_patches = [(0, 32, 0, 32)]
    w._preview_patch_idx = 0
    rng = np.random.default_rng(3)
    w._patch_channel_cache[0] = {
        ch: rng.random((32, 32), dtype=np.float32) + 0.1 for ch in cached}
    w._patch_load_ready.add(0)
    return w


def test_a_new_dataset_shows_dapi_and_nothing_else(app):
    w = _window(app)
    try:
        assert w.config.visible_channels() == ["DAPI"]
        assert w.config.current_channel() == "DAPI"
        assert w.config.channel_weight("DAPI") == 1.0
        assert w.config.channel_weight("CD3") == 0.0
        assert w.config.channel_weight("CD8") == 0.0
        assert w._step1_preview_mode == "overlay"
    finally:
        w.close()


def test_selecting_a_hidden_channel_shows_it_and_makes_it_current(app):
    w = _window(app)
    try:
        seen = []
        w.config.current_channel_changed.connect(seen.append)
        w.config._rows["CD3"].selected.emit("CD3")   # what a click emits

        assert w.config.current_channel() == "CD3"
        assert "CD3" in w.config.visible_channels()
        assert seen == ["CD3"]
    finally:
        w.close()


def test_ticking_another_channel_does_not_move_the_selection(app):
    w = _window(app)
    try:
        assert w.config.current_channel() == "DAPI"
        w.config._rows["CD8"].checkbox.setChecked(True)   # what a tick does

        assert set(w.config.visible_channels()) == {"DAPI", "CD8"}
        assert w.config.current_channel() == "DAPI"
    finally:
        w.close()


def test_the_overlay_ignores_the_fusion_weight(app):
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w._render_overlay_patch(reset_view=True)
        before = w.prev_img.image.copy()

        w.config._rows["CD3"].spin.setValue(1.0)
        w._render_overlay_patch(reset_view=False)
        assert np.array_equal(w.prev_img.image, before)

        w.config._rows["CD3"].spin.setValue(0.0)
        w._render_overlay_patch(reset_view=False)
        assert np.array_equal(w.prev_img.image, before)
    finally:
        w.close()


def test_a_channel_with_zero_weight_is_still_drawn_when_ticked(app):
    w = _window(app)
    try:
        w.config.set_channel_visible("DAPI", False)
        w.config.set_channel_visible("CD3", True)
        assert w.config.channel_weight("CD3") == 0.0

        w._render_overlay_patch(reset_view=True)
        assert w.prev_img.image is not None
        assert w.prev_img.image.max() > 0
    finally:
        w.close()


def test_the_overlay_follows_the_channel_colour(app):
    w = _window(app)
    try:
        w.config.set_channel_visible("DAPI", False)
        w.config.set_channel_visible("CD3", True)
        w.config.set_channel_color("CD3", "#ff0000")
        w._render_overlay_patch(reset_view=True)
        red = w.prev_img.image.copy()

        w.config.set_channel_color("CD3", "#00ff00")
        w._render_overlay_patch(reset_view=False)
        green = w.prev_img.image

        assert red[:, :, 0].max() > 0 and red[:, :, 1].max() == 0
        assert green[:, :, 1].max() > 0 and green[:, :, 0].max() == 0
    finally:
        w.close()


def test_the_overlay_follows_the_intensity_window(app, monkeypatch):
    """No manual cache clearing: the panel must react to the real signal."""
    w = _window(app)
    try:
        w.config.set_channel_visible("DAPI", False)
        w.config.set_channel_visible("CD3", True)
        w.config.set_channel_color("CD3", "#ffffff")

        window = {"min": 0.0, "max": 1.0, "gamma": 1.0}
        monkeypatch.setattr(type(w), "_load_step0_remap_params",
                            lambda self: ({"CD3": dict(window)}, "x"))
        w._render_overlay_patch(reset_view=True)
        wide = w.prev_img.image.copy()

        # Exactly what moving the Intensity slider does.
        window["max"] = 0.3
        w._step0.display_mapping_changed.emit("CD3")
        narrow = w.prev_img.image

        assert narrow.mean() > wide.mean()      # a tighter window is brighter
    finally:
        w.close()


def test_a_mapping_change_drops_only_that_channels_pixels(app, monkeypatch):
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        window = {"CD3": {"min": 0.0, "max": 1.0, "gamma": 1.0},
                  "DAPI": {"min": 0.0, "max": 1.0, "gamma": 1.0}}
        monkeypatch.setattr(type(w), "_load_step0_remap_params",
                            lambda self: (window, "x"))
        w._render_overlay_patch(reset_view=True)
        assert {key[1] for key in w._overlay_display_cache} == {"DAPI", "CD3"}

        window["CD3"] = {"min": 0.0, "max": 0.4, "gamma": 1.0}
        w._step0.display_mapping_changed.emit("CD3")

        cached = {key[1] for key in w._overlay_display_cache}
        assert "DAPI" in cached                     # untouched channel kept
        keys = [k for k in w._overlay_display_cache if k[1] == "CD3"]
        assert len(keys) == 1                       # re-cached under the new window
    finally:
        w.close()


def test_the_display_cache_is_keyed_by_the_window_not_by_its_source(app, monkeypatch):
    """Two different windows from the same source are two different pictures."""
    w = _window(app)
    try:
        w.config.set_channel_visible("DAPI", False)
        w.config.set_channel_visible("CD3", True)
        w.config.set_channel_color("CD3", "#ffffff")
        window = {"CD3": {"min": 0.0, "max": 1.0, "gamma": 1.0}}
        monkeypatch.setattr(type(w), "_load_step0_remap_params",
                            lambda self: (window, "x"))
        w._render_overlay_patch(reset_view=True)
        wide = w.prev_img.image.copy()

        # Same channel, same source, different numbers, and no invalidation:
        # a key made of labels would hand back the old pixels here.
        window["CD3"] = {"min": 0.0, "max": 0.3, "gamma": 1.0}
        w._render_overlay_patch(reset_view=False)
        assert not np.array_equal(w.prev_img.image, wide)
    finally:
        w.close()


def test_unticking_every_channel_says_so_instead_of_looking_broken(app):
    w = _window(app)
    try:
        for ch in list(w.config.all_channels):
            w.config.set_channel_visible(ch, False)
        w._render_overlay_patch(reset_view=True)

        assert w.prev_img.image is None
        assert "No visible channels" in w.prev_status.text()
        assert "Loading" not in w.prev_status.text()
    finally:
        w.close()


def test_dapi_can_be_unticked_without_touching_the_fusion_nucleus(app):
    w = _window(app)
    try:
        w.config.set_channel_visible("DAPI", False)
        assert "DAPI" not in w.config.visible_channels()
        # Fusion still knows what its nucleus is and what it weighs.
        assert w.config.get_nucleus() == ("DAPI", 1.0)
    finally:
        w.close()


def test_a_weight_edit_neither_clears_the_cache_nor_reads_the_disk(app):
    w = _window(app)
    try:
        started = []
        w._start_loader_for = lambda idx, needed=None: started.append((idx, needed))
        cached_before = dict(w._patch_channel_cache[0])
        reads_before = list(w.loader.reads)

        w.config._rows["CD3"].spin.setValue(0.6)
        w.config._rows["CD8"].spin.setValue(0.3)

        assert w._patch_channel_cache[0].keys() == cached_before.keys()
        assert w.loader.reads == reads_before
        assert started == []
    finally:
        w.close()


def test_the_channels_to_hold_are_the_ticked_ones_and_the_current_one(app):
    w = _window(app)
    try:
        assert w._needed_channels() == ["DAPI"]

        w.config.set_channel_visible("CD8", True)
        assert set(w._needed_channels()) == {"DAPI", "CD8"}

        # Selecting also ticks, so the set grows by the selected channel.
        w.config._rows["CD3"].selected.emit("CD3")
        assert set(w._needed_channels()) == {"DAPI", "CD8", "CD3"}

        # A weight has no say in what is held.
        w.config._rows["CD3"].spin.setValue(0.0)
        assert "CD3" in w._needed_channels()
    finally:
        w.close()


def test_switching_modes_keeps_the_channel_state_and_the_camera(app):
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(0.7)
        w._render_overlay_patch(reset_view=True)
        w.prev_vb.setRange(xRange=[2, 12], yRange=[3, 13], padding=0)
        camera = [list(r) for r in w.prev_vb.viewRange()]

        started = []
        w._start_loader_for = lambda idx, needed=None: started.append(idx)
        w.set_preview_mode("fusion")
        assert w._step1_preview_mode == "fusion"
        assert set(w.config.visible_channels()) == {"DAPI", "CD3"}
        assert w.config.channel_weight("CD3") == 0.7
        assert w.config.current_channel() == "DAPI"

        w.set_preview_mode("overlay")
        assert [list(r) for r in w.prev_vb.viewRange()] == camera
        assert started == []
    finally:
        w.close()


def test_the_display_state_survives_a_session_round_trip(app):
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config.set_channel_visible("DAPI", False)
        w.config.set_channel_color("CD3", "#123456")
        w.config._rows["CD8"].selected.emit("CD8")
        w.set_preview_mode("fusion")

        w.step0_output = {"output_dir": "/tmp", "step1_dir": "/tmp"}
        payload = w._step1_session_payload()
        assert payload["channel_visibility"]["CD3"] is True
        assert payload["channel_visibility"]["DAPI"] is False
        assert payload["channel_colors"]["CD3"] == "#123456"
        assert payload["current_channel"] == "CD8"
        assert payload["preview_mode"] == "fusion"

        # A fresh panel forgets everything, then the session puts it back.
        w.config.load_panel({"markers": {"CD3": 0.0, "CD8": 0.0}}, "DAPI")
        w.set_preview_mode("overlay", force=True)
        assert w.config.visible_channels() == ["DAPI"]

        w._apply_step1_display_state(payload)
        assert set(w.config.visible_channels()) == {"CD3", "CD8"}
        assert w.config.channel_color("CD3") == "#123456"
        assert w.config.current_channel() == "CD8"
        assert w._step1_preview_mode == "fusion"
    finally:
        w.close()


def test_a_new_dataset_does_not_inherit_the_previous_slide_colours(app):
    w = _window(app)
    try:
        w.config.set_channel_color("CD3", "#123456")
        w.config.set_channel_visible("CD3", True)

        w.config.load_panel({"markers": {"CD3": 0.0, "CD8": 0.0}}, "DAPI")

        assert w.config.channel_color("CD3") != "#123456"
        assert w.config.visible_channels() == ["DAPI"]
    finally:
        w.close()


def test_ticking_a_channel_reads_only_that_channel(app):
    w = _window(app, cached=("DAPI",))
    try:
        asked = []
        w._start_loader_for = lambda idx, needed=None: asked.append(list(needed or []))
        w.config.set_channel_visible("CD8", True)

        assert asked == [["CD8"]]          # not the whole needed set
        assert "DAPI" in w._patch_channel_cache[0]   # kept, not evicted
    finally:
        w.close()


def test_a_loaded_channel_is_merged_into_what_is_already_held(app):
    w = _window(app, cached=("DAPI",))
    try:
        w.config.set_channel_visible("CD8", True)
        arr = np.ones((32, 32), np.float32)
        w._on_patch_loaded(0, {"CD8": arr})

        held = w._patch_channel_cache[0]
        assert set(held) == {"DAPI", "CD8"}
        assert np.array_equal(held["CD8"], arr)
    finally:
        w.close()


def test_selecting_a_channel_points_the_intensity_window_at_it(app):
    w = _window(app)
    try:
        asked = []
        w._step0.focus_intensity_on = lambda ch: asked.append(ch) or True
        w.config._rows["CD3"].selected.emit("CD3")
        assert asked == ["CD3"]
    finally:
        w.close()


def test_the_intensity_button_opens_the_shared_window_on_the_current_channel(app):
    w = _window(app)
    try:
        opened, focused = [], []
        w._step0.show_intensity_window = lambda: opened.append(True)
        w._step0.focus_intensity_on = lambda ch: focused.append(ch) or True
        w.config._rows["CD8"].selected.emit("CD8")
        w._btn_step1_intensity.click()

        assert opened == [True]
        assert focused[-1] == "CD8"
    finally:
        w.close()


class _BlockingLoader(_Loader):
    """A loader that can be held inside one channel's read."""

    def __init__(self, block_channel):
        super().__init__()
        import threading
        self.gate = threading.Event()
        self.block_channel = block_channel
        self.entered = threading.Event()

    def read_region(self, channel, y0, y1, x0, x1, downsample=1, normalize=True):
        if channel == self.block_channel:
            self.entered.set()
            self.gate.wait(10)
        return super().read_region(channel, y0, y1, x0, x1,
                                   downsample=downsample, normalize=normalize)


class _BusyThread:
    """Occupies a patch's loader slot without doing anything."""

    def isRunning(self):
        return True

    def stop(self):
        pass

    def wait(self, *_a):
        return True


def _pump(w, predicate, tries=200):
    for _ in range(tries):
        QtWidgets.QApplication.processEvents()
        if predicate():
            return True
        QtWidgets.QApplication.instance().thread().msleep(10)
    return False


def test_a_tick_made_during_a_load_is_not_lost(app):
    """The second tick must be served once the first loader is off the slot."""
    w = _window(app, cached=())
    try:
        w.loader = _BlockingLoader("DAPI")
        w._patch_load_ready.discard(0)
        w._ensure_channels_cached(0)                 # starts the DAPI read
        assert w.loader.entered.wait(5)

        w.config.set_channel_visible("CD3", True)    # ticked mid-flight
        assert "CD3" not in (w._patch_channel_cache.get(0) or {})

        w.loader.gate.set()
        got = _pump(w, lambda: "CD3" in (w._patch_channel_cache.get(0) or {}))

        assert got, "the tick made during the load was dropped"
        assert set(w._patch_channel_cache[0]) >= {"DAPI", "CD3"}
    finally:
        for thread in list(w._patch_loaders.values()):
            thread.stop()
            thread.wait(3000)
        w.close()


def test_fusion_holds_the_channels_it_weighs(app):
    w = _window(app)
    try:
        w.set_preview_mode("fusion")
        assert set(w._needed_channels()) == {"DAPI"}

        w.config._rows["CD3"].spin.setValue(0.8)     # weighted but not ticked
        assert "CD3" in w._needed_channels()

        w.set_preview_mode("overlay")
        assert "CD3" not in w._needed_channels()     # overlay draws what is ticked
    finally:
        w.close()


def test_a_newly_weighted_channel_is_read_without_dropping_the_rest(app):
    w = _window(app, cached=("DAPI",))
    try:
        w.set_preview_mode("fusion")
        asked = []
        w._start_loader_for = lambda idx, needed=None: asked.append(list(needed or []))

        w.config._rows["CD8"].spin.setValue(0.5)

        assert asked == [["CD8"]]
        assert "DAPI" in w._patch_channel_cache[0]
    finally:
        w.close()


def test_fusion_says_preparing_instead_of_faking_a_nucleus_only_result(app):
    w = _window(app, cached=("DAPI",))
    try:
        w.set_preview_mode("fusion")
        w._start_loader_for = lambda idx, needed=None: None   # nothing arrives
        w.config._rows["CD3"].spin.setValue(0.9)
        w.prev_img.clear()

        w._render_current_patch(reset_view=True)

        assert w.prev_img.image is None            # no fabricated picture
        assert "Preparing fusion" in w.prev_status.text()
    finally:
        w.close()


def test_restoring_a_session_does_not_tick_the_current_channel(app):
    w = _window(app)
    try:
        payload = {
            "channel_visibility": {"DAPI": True, "CD3": False, "CD8": False},
            "channel_colors": {},
            "current_channel": "CD3",
            "preview_mode": "overlay",
        }
        w._apply_step1_display_state(payload)

        assert w.config.current_channel() == "CD3"
        assert "CD3" not in w.config.visible_channels()
        assert w.config.visible_channels() == ["DAPI"]
    finally:
        w.close()


def test_a_click_still_ticks_the_channel_it_selects(app):
    w = _window(app)
    try:
        w.config._rows["CD3"].selected.emit("CD3")
        assert "CD3" in w.config.visible_channels()
    finally:
        w.close()


def test_entering_fusion_asks_for_the_channels_fusion_needs(app):
    """Otherwise fusion sits on "Preparing" with nothing on the way."""
    w = _window(app, cached=("DAPI",))
    try:
        asked = []
        w._start_loader_for = lambda idx, needed=None: asked.append(list(needed or []))
        w.config._rows["CD3"].spin.setValue(0.8)   # weighted, not ticked
        asked.clear()

        w.set_preview_mode("fusion")

        assert asked == [["CD3"]]
    finally:
        w.close()


def test_restoring_a_fusion_session_asks_for_the_fusion_channels(app):
    w = _window(app, cached=("DAPI",))
    try:
        w.config._rows["CD3"].spin.setValue(0.8)
        asked = []
        w._start_loader_for = lambda idx, needed=None: asked.append(list(needed or []))

        w._apply_step1_display_state({
            "preview_mode": "fusion",
            "channel_visibility": {"DAPI": True, "CD3": False, "CD8": False},
            "current_channel": "DAPI",
        })

        assert w._step1_preview_mode == "fusion"
        assert ["CD3"] in asked
    finally:
        w.close()


def test_a_replaced_loader_ending_late_starts_nothing(app):
    w = _window(app, cached=("DAPI",))
    try:
        w.config.set_channel_visible("CD3", True)     # records the demand
        stale = object()
        started = []
        w._start_loader_for = lambda idx, needed=None: started.append(idx)

        w._on_patch_loader_finished(0, stale)         # not the slot's thread

        assert started == []
    finally:
        w.close()


def test_a_failed_read_is_not_retried_for_ever(app):
    w = _window(app, cached=("DAPI",))
    try:
        thread = _BusyThread()
        w._patch_loaders[0] = thread
        w._loader_channels[0] = {"CD3"}
        w.config.set_channel_visible("CD3", True)

        w._on_patch_error(0, "read failed")
        started = []
        w._start_loader_for = lambda idx, needed=None: started.append(list(needed or []))
        w._on_patch_loader_finished(0, thread)

        assert started == []
        assert "CD3" in w._failed_channels.get(0, set())
    finally:
        w._patch_loaders.clear()
        w.close()


def test_a_failure_does_not_take_a_tick_made_while_it_ran_with_it(app):
    """DAPI fails; the CD8 ticked during that read is still owed."""
    w = _window(app, cached=())
    try:
        thread = _BusyThread()
        w._patch_loaders[0] = thread
        w._loader_channels[0] = {"DAPI"}
        w.config.set_channel_visible("CD8", True)      # ticked mid-flight
        assert w._pending_channel_demand.get(0) == {"CD8"}

        w._on_patch_error(0, "DAPI read failed")

        started = []
        w._start_loader_for = lambda idx, needed=None: started.append(list(needed or []))
        w._on_patch_loader_finished(0, thread)

        assert started == [["CD8"]]                    # not DAPI, not nothing
        assert "DAPI" in w._failed_channels.get(0, set())
    finally:
        w._patch_loaders.clear()
        w.close()


def test_ticking_a_failed_channel_again_asks_for_it_again(app):
    w = _window(app, cached=("DAPI",))
    try:
        w._failed_channels[0] = {"CD3"}
        started = []
        w._start_loader_for = lambda idx, needed=None: started.append(list(needed or []))

        w.config.set_channel_visible("CD3", True)

        assert started == [["CD3"]]
    finally:
        w.close()


def test_a_dataset_switch_forgets_a_pending_demand(app):
    w = _window(app, cached=("DAPI",))
    try:
        thread = _BusyThread()
        w._patch_loaders[0] = thread
        w._loader_channels[0] = {"DAPI"}
        w.config.set_channel_visible("CD3", True)
        assert w._pending_channel_demand.get(0) == {"CD3"}

        w._patch_loaders.clear()
        w._step0.dataset_committed.emit({"gen": 11})

        assert w._pending_channel_demand == {}
        assert w._loader_channels == {}
        assert w._failed_channels == {}
    finally:
        w.close()


def test_restoring_a_session_loads_once_and_draws_once(app):
    w = _window(app, cached=("DAPI",))
    try:
        w.config._rows["CD3"].spin.setValue(0.8)
        loads, draws = [], []
        w._start_loader_for = lambda idx, needed=None: loads.append(list(needed or []))
        real_refresh = w._refresh_patch_preview
        w._refresh_patch_preview = lambda reset_view=False: (
            draws.append(reset_view) or real_refresh(reset_view=reset_view))

        w._apply_step1_display_state({
            "preview_mode": "fusion",
            "channel_visibility": {"DAPI": True, "CD3": False, "CD8": True},
            "channel_colors": {"CD3": "#112233"},
            "current_channel": "CD8",
        })

        assert len(loads) == 1
        assert sorted(loads[0]) == ["CD3", "CD8"]
        assert len(draws) == 1
        assert w._step1_preview_mode == "fusion"
        assert "CD3" not in w.config.visible_channels()   # hidden stays hidden
    finally:
        w.close()


class _FailingBlockingLoader(_Loader):
    """Blocks inside one channel's read, then fails it."""

    def __init__(self, block_channel):
        super().__init__()
        import threading
        self.gate = threading.Event()
        self.block_channel = block_channel
        self.entered = threading.Event()

    def read_region(self, channel, y0, y1, x0, x1, downsample=1, normalize=True):
        if channel == self.block_channel:
            self.entered.set()
            self.gate.wait(10)
            raise RuntimeError(f"{channel} could not be read")
        return super().read_region(channel, y0, y1, x0, x1,
                                   downsample=downsample, normalize=normalize)


def test_the_real_start_path_records_what_the_worker_is_reading(app):
    """Through `_start_loader_for`, not a hand-written record.

    A failure has to know which channels it was reading, and a tick made while
    it ran must not be blamed for the failure or bundled into its retry.
    """
    w = _window(app, cached=())
    try:
        w.loader = _FailingBlockingLoader("DAPI")
        w._patch_load_ready.discard(0)
        requested = []
        real_start = w._start_loader_for

        def _spy(idx, needed=None):
            requested.append(sorted(needed or []))
            return real_start(idx, needed=needed)
        w._start_loader_for = _spy

        w._ensure_channels_cached(0)                     # reads DAPI
        assert w.loader.entered.wait(5)
        assert w._loader_channels.get(0) == {"DAPI"}     # survived the start

        w.config.set_channel_visible("CD8", True)        # ticked mid-flight
        assert w._pending_channel_demand.get(0) == {"CD8"}

        w.loader.gate.set()                              # DAPI fails
        got = _pump(w, lambda: "CD8" in (w._patch_channel_cache.get(0) or {}))

        assert got, "the channel ticked during the failed read was never loaded"
        assert w._failed_channels.get(0) == {"DAPI"}
        assert "DAPI" not in (w._patch_channel_cache.get(0) or {})
        assert requested == [["DAPI"], ["CD8"]]          # DAPI never retried
    finally:
        for thread in list(w._patch_loaders.values()):
            thread.stop()
            thread.wait(3000)
        w.close()


class _SignallingThread(QtCore.QObject):
    """The loader's signal contract, without the thread."""

    done = QtCore.pyqtSignal(int, dict)
    progress = QtCore.pyqtSignal(int, int, int, str)
    error = QtCore.pyqtSignal(int, str)
    finished = QtCore.pyqtSignal()

    def isRunning(self):
        return False

    def stop(self):
        pass

    def wait(self, *_a):
        return True


def test_a_late_error_from_a_replaced_loader_blames_nobody(app):
    """Disconnecting cannot unqueue a signal Qt has already accepted."""
    w = _window(app, cached=("DAPI",))
    try:
        old = _SignallingThread()
        w._connect_patch_loader(old, 0)          # the real wiring
        new = _SignallingThread()
        w._connect_patch_loader(new, 0)
        w._patch_loaders[0] = new                # `new` owns the slot now
        w._loader_channels[0] = {"CD8"}          # what `new` is reading

        old.error.emit(0, "the previous read failed")

        assert w._failed_channels.get(0, set()) == set()
        # The current worker's own failure is still recorded.
        new.error.emit(0, "this read failed")
        assert w._failed_channels.get(0) == {"CD8"}
    finally:
        w._patch_loaders.clear()
        w.close()


def test_a_late_result_from_a_replaced_loader_writes_no_pixels(app):
    w = _window(app, cached=())
    try:
        w._patch_load_ready.discard(0)
        old = _SignallingThread()
        w._connect_patch_loader(old, 0)
        new = _SignallingThread()
        w._connect_patch_loader(new, 0)
        w._patch_loaders[0] = new

        stale = np.full((32, 32), 7.0, np.float32)
        old.done.emit(0, {"DAPI": stale})

        assert w._patch_channel_cache.get(0) in (None, {})
        assert 0 not in w._patch_load_ready

        fresh = np.ones((32, 32), np.float32)
        new.done.emit(0, {"DAPI": fresh})
        assert np.array_equal(w._patch_channel_cache[0]["DAPI"], fresh)
    finally:
        w._patch_loaders.clear()
        w.close()


def test_a_late_result_after_a_dataset_switch_writes_no_pixels(app):
    w = _window(app, cached=())
    try:
        old = _SignallingThread()
        w._connect_patch_loader(old, 0)
        w._patch_loaders[0] = old

        w._step0.dataset_committed.emit({"gen": 21})     # clears everything
        old.done.emit(0, {"DAPI": np.ones((32, 32), np.float32)})

        assert w._patch_channel_cache == {}
        assert w._patch_load_ready == set()
    finally:
        w._patch_loaders.clear()
        w.close()
