"""One channel state, two views of it.

The row carries which channel is being edited, which channels are in the
picture, how strongly each takes part, its colour and its display window. The
overlay and the fusion preview read the SAME answers: the ticked channels, each
at its weight. They used to disagree — the overlay ignored the weight while the
fusion ignored the tick — so one panel described two pictures and neither was
what a Save would write.

Tick and weight cannot contradict the picture: raising a weight above zero
ticks the channel, dropping it to zero unticks it, and ticking a channel at
zero gives it back a weight it can be seen at. Unticking keeps the weight so
re-ticking restores it, and an unticked channel takes part in nothing.
Moving a weight still costs no disk read.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import os

import weakref

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


class _InlineFrames:
    """The frame worker, inline and under the test's control.

    The preview's frames are composed on `PreviewComposeWorker` now, so a test
    that changes state and reads the picture cannot simply pump the event loop:
    it would be racing a thread. This stands in for the thread and runs the
    REAL compose code -- `PreviewComposeWorker._compose` on the real snapshot
    -- so the pixels are the production ones while delivery happens exactly
    when the test says.

    `compute_ms` is what a frame costs on the fake clock, for the tests about
    a computation slower than the input.
    """

    def __init__(self, w, clock=None, compute_ms=0.0, auto=True):
        from block01.core import preview_compose
        from block01.workers.preview_compose_worker import (
            PreviewComposeWorker,
        )
        self._w = w
        self._clock = clock
        self.compute_ms = float(compute_ms)
        self.auto = bool(auto)
        self.submitted = []
        self.delivered = []
        self._composer = PreviewComposeWorker.__new__(PreviewComposeWorker)
        self._composer.cache = preview_compose.PreviewCache()
        self._compose = PreviewComposeWorker._compose
        w._dispatch_frame = self._submit

    def _submit(self, snapshot):
        self.submitted.append(snapshot)
        if self.auto and self._clock is None:
            self.deliver()
        return True

    def pending(self):
        return len(self.submitted)

    def deliver(self, count=None):
        """Compose and deliver the queued snapshots, oldest first."""
        done = 0
        while self.submitted and (count is None or done < count):
            snapshot = self.submitted.pop(0)
            if self._clock is not None and self.compute_ms:
                self._clock.advance_ms(self.compute_ms)
            result = self._compose(self._composer, snapshot)
            self.delivered.append(result)
            self._w._on_preview_frame(result)
            done += 1
        return done


def _settle(w, timeout=2.0):
    """Let the coalesced redraw land.

    A state change schedules ONE composite of where the user stopped rather
    than drawing every intermediate step, so a test that changes state and
    reads the picture has to let that publication happen.
    """
    import time
    deadline = time.monotonic() + timeout
    frames = getattr(w, "_inline_frames", None)
    while time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        if frames is not None and frames.pending():
            frames.deliver()
            continue
        if not w._prev_timer.isActive():
            break
        time.sleep(0.005)
    QtWidgets.QApplication.processEvents()
    if frames is not None:
        frames.deliver()


def _show(w, channel, weight=1.0):
    """Put a channel in the picture: drawn, fused, and weighed this much.

    ONE product gesture with two entries: ticking the row, or giving the row
    a weight -- both show the channel AND put it into the fusion (user
    ruling, 2026-09-16). The separate participation box this helper once
    spoke for is gone; the two owner calls below are what one tick writes,
    and the weight is the number the user asked for.
    """
    w.config.set_channel_visible(channel, True)
    w.config.set_fusion_enabled(channel, True)
    w.config._rows[channel].spin.setValue(weight)


def _hide(w, channel):
    """Take a channel out of the picture AND out of the science.

    The tick is the one gesture that does this: a weight, zero included,
    never takes a channel out.
    """
    w.config.set_channel_visible(channel, False)
    w.config.set_fusion_enabled(channel, False)


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
    # Frames are composed on a worker thread in production; these tests run
    # the same compose code inline so the pixels are real and the delivery is
    # deterministic. See `_InlineFrames`.
    w._inline_frames = _InlineFrames(w)
    # Weights and participation are entered in STEP1's public row; a test
    # that drives those widgets is in Step1 when it does, and then walks.
    w._set_step_active(1)
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
        w.config._rows["CD3"].row_clicked.emit("CD3")   # what a click emits

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


def test_the_overlay_shows_a_channel_at_its_weight(app):
    """Weight is strength on screen as well as contribution on disk."""
    w = _window(app)
    try:
        w.config.set_channel_visible("DAPI", False)
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        w._render_overlay_patch(reset_view=True)
        full = w.prev_img.image.copy()

        w.config._rows["CD3"].spin.setValue(0.25)
        w._render_overlay_patch(reset_view=False)
        quarter = w.prev_img.image.copy()

        assert int(quarter.max()) < int(full.max())
        assert int(quarter.max()) > 0
    finally:
        w.close()


def test_a_weight_of_zero_contributes_nothing_but_keeps_the_tick(app):
    """A ticked channel at 0 is a legal state: in the configuration, adding
    nothing to the picture right now."""
    w = _window(app)
    try:
        w.config.set_channel_visible("DAPI", False)
        _show(w, "CD3", 1.0)
        w.config._rows["CD3"].spin.setValue(0.0)

        assert "CD3" in w.config.visible_channels()
        w._render_overlay_patch(reset_view=True)
        assert w.prev_img.image is None or int(w.prev_img.image.max()) == 0
    finally:
        w.close()


def test_a_weight_ticks_a_channel_and_never_unticks_one(app):
    """User ruling, 2026-09-16: giving a channel a weight is using it.

    The first half of this test used to assert the opposite -- a weight on a
    hidden channel left it hidden, which is how a slider drag could change
    nothing on screen. The second half is unchanged: no weight, zero
    included, ever takes a channel out.
    """
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", False)
        w.config._rows["CD3"].spin.setValue(0.4)
        assert "CD3" in w.config.visible_channels()

        w.config._rows["CD3"].spin.setValue(0.0)
        assert "CD3" in w.config.visible_channels()
    finally:
        w.close()


def test_a_first_tick_weighs_one_and_later_ticks_leave_it_alone(app):
    """The first FUSION tick answers 1.0 so the channel is actually in the
    picture; every tick after that brings back the weight the user left."""
    w = _window(app)
    try:
        w.config.set_channel_visible("DAPI", False)
        w.config.set_fusion_enabled("CD3", True)
        assert w.config.channel_weight("CD3") == 1.0

        w.config._rows["CD3"].spin.setValue(0.6)
        w.config.set_fusion_enabled("CD3", False)
        w.config.set_fusion_enabled("CD3", True)
        assert w.config.channel_weight("CD3") == pytest.approx(0.6)
    finally:
        w.close()


def test_unticking_keeps_the_weight_and_re_ticking_restores_it(app):
    w = _window(app)
    try:
        _show(w, "CD3", 0.6)

        w.config.set_channel_visible("CD3", False)
        assert w.config.channel_weight("CD3") == pytest.approx(0.6)
        assert "CD3" not in w.config.visible_channels()

        w.config.set_channel_visible("CD3", True)
        assert w.config.channel_weight("CD3") == pytest.approx(0.6)
    finally:
        w.close()


def test_a_disabled_channel_takes_part_in_nothing(app):
    """Not the fusion and not what a Save writes — even though the model
    still remembers the weight for when it is enabled again."""
    w = _window(app)
    try:
        _show(w, "CD3", 0.7)
        _hide(w, "CD3")

        effective = w._effective_fusion_config()
        weighted = set()
        for data in effective["groups"].values():
            weighted.update(data["channels"])
        assert "CD3" not in weighted
        assert "CD3" not in w._fusion_weighted_channels()
        assert w.config.channel_weight("CD3") == pytest.approx(0.7)
    finally:
        w.close()


def test_the_overlay_follows_the_channel_colour(app):
    w = _window(app)
    try:
        w.config.set_channel_visible("DAPI", False)
        _show(w, "CD3", 1.0)
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
        _show(w, "CD3", 1.0)
        w.config.set_channel_color("CD3", "#ffffff")

        window = {"min": 0.0, "max": 1.0, "gamma": 1.0}
        monkeypatch.setattr(type(w), "_load_step0_remap_params",
                            lambda self: ({"CD3": dict(window)}, "x"))
        w._render_overlay_patch(reset_view=True)
        wide = w.prev_img.image.copy()

        # Exactly what moving the Intensity slider does.
        window["max"] = 0.3
        w._step0.display_mapping_changed.emit("CD3")
        _settle(w)
        narrow = w.prev_img.image

        assert narrow.mean() > wide.mean()      # a tighter window is brighter
    finally:
        w.close()


def test_a_mapping_change_drops_only_that_channels_pixels(app, monkeypatch):
    w = _window(app)
    try:
        _show(w, "CD3", 1.0)
        window = {"CD3": {"min": 0.0, "max": 1.0, "gamma": 1.0},
                  "DAPI": {"min": 0.0, "max": 1.0, "gamma": 1.0}}
        monkeypatch.setattr(type(w), "_load_step0_remap_params",
                            lambda self: (window, "x"))
        w._render_overlay_patch(reset_view=True)
        assert {key[1] for key in w._overlay_display_cache} == {"DAPI", "CD3"}

        window["CD3"] = {"min": 0.0, "max": 0.4, "gamma": 1.0}
        w._step0.display_mapping_changed.emit("CD3")
        _settle(w)

        cached = {key[1] for key in w._overlay_display_cache}
        assert "DAPI" in cached, \
            "an untouched channel's gray was thrown away"
        assert "CD3" not in cached, \
            "the edited channel's gray was kept and would be redrawn stale"
        # The frame that follows is composed by the worker, which keeps its
        # own cache -- that is where the new window's gray lives now.
        worker_cache = w._inline_frames._composer.cache
        assert (w._preview_patch_idx, "CD3", "gray") in worker_cache
    finally:
        w.close()


def test_the_display_cache_is_keyed_by_the_window_not_by_its_source(app, monkeypatch):
    """Two different windows from the same source are two different pictures."""
    w = _window(app)
    try:
        w.config.set_channel_visible("DAPI", False)
        _show(w, "CD3", 1.0)
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
        w.config._rows["CD3"].row_clicked.emit("CD3")
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
        w.config._rows["CD8"].row_clicked.emit("CD8")
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


def test_a_new_dataset_keeps_the_one_colour_and_resets_the_weights(app):
    """`load_panel` re-deals the WEIGHTS, never the colours.

    It used to re-deal both, from this panel's own palette -- and that is how
    the same channel came to be two colours. Step0 has always treated a
    colour as a per-channel-name display preference that survives a reload
    (`Step0Page._reset_dataset_view_state` says so in as many words, and
    deliberately does not clear it); Step1 cleared and re-dealt, so the moment
    a dataset was opened the two steps disagreed about CD3.

    The product decision is unchanged -- the colour follows the channel name.
    What changed is that there is now ONE store holding it, so both steps
    give the same answer whether or not it is inherited.
    """
    w = _window(app)
    try:
        w.config.set_channel_color("CD3", "#123456")
        w.config.set_channel_visible("CD3", True)

        w.config.load_panel({"markers": {"CD3": 0.0, "CD8": 0.0}}, "DAPI")

        assert w.config.channel_color("CD3") == "#123456"
        assert w._display.state.color("CD3") == "#123456"
        assert w._step0._channel_color_hex("CD3") == "#123456"
        # The weights and the ticks ARE a per-dataset answer, and they are
        # still reset: only DAPI comes back.
        assert w.config.visible_channels() == ["DAPI"]
        assert w.config.channel_weight("CD3") == 0.0
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
        w._step0.focus_intensity_on = lambda ch, color=None: (
            asked.append((ch, color)) or True)
        w.config._rows["CD3"].row_clicked.emit("CD3")
        # The colour goes with it: Step1 owns the overlay colours and the
        # Intensity histogram must not come up in another palette.
        assert [ch for ch, _c in asked] == ["CD3"]
        assert asked[0][1] == w.config.channel_color("CD3")
    finally:
        w.close()


def test_the_intensity_button_opens_the_shared_window_on_the_current_channel(app):
    """Through Block01, not through Step0.

    The spies used to be on `Step0Page.show_intensity_window` and
    `focus_intensity_on`, because that is what this button called -- one step
    reaching into another for a window neither of them owns. The window is
    `Block01DisplayServices`'s now, so that is what the button is checked
    against; Step0 still supplies the panel inside it, which is why the
    channel focus is spied where the CONTENT port answers.
    """
    w = _window(app)
    try:
        focused = []
        w._step0.focus_intensity_channel = lambda ch, color=None: (
            focused.append(ch) or True)
        w.config._rows["CD8"].row_clicked.emit("CD8")
        w._btn_step1_intensity.click()

        assert w._display.intensity_window() is not None
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


def test_both_modes_want_the_same_channels(app):
    """Weighted and ticked are the same set now, so the pixels each mode needs
    are the same pixels — switching modes never drops or re-reads a channel."""
    w = _window(app)
    try:
        w.set_preview_mode("fusion")
        assert set(w._needed_channels()) == {"DAPI"}

        _show(w, "CD3", 0.8)                         # ticked AND weighted
        assert "CD3" in w._needed_channels()

        w.set_preview_mode("overlay")
        assert "CD3" in w._needed_channels()

        _hide(w, "CD3")                              # out of the picture
        assert "CD3" not in w._needed_channels()
        w.set_preview_mode("fusion")
        assert "CD3" not in w._needed_channels()
    finally:
        w.close()


def test_a_newly_weighted_channel_is_read_without_dropping_the_rest(app):
    w = _window(app, cached=("DAPI",))
    try:
        w.set_preview_mode("fusion")
        asked = []
        w._start_loader_for = lambda idx, needed=None: asked.append(list(needed or []))

        _show(w, "CD8", 0.5)

        assert asked and all(a == ["CD8"] for a in asked)
        assert "DAPI" in w._patch_channel_cache[0]
    finally:
        w.close()


def test_fusion_says_preparing_instead_of_faking_a_nucleus_only_result(app):
    w = _window(app, cached=("DAPI",))
    try:
        w.set_preview_mode("fusion")
        w._start_loader_for = lambda idx, needed=None: None   # nothing arrives
        _show(w, "CD3", 0.9)
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
        w.config._rows["CD3"].row_clicked.emit("CD3")
        assert "CD3" in w.config.visible_channels()
    finally:
        w.close()


def test_entering_fusion_asks_for_the_channels_fusion_needs(app):
    """Otherwise fusion sits on "Preparing" with nothing on the way."""
    w = _window(app, cached=("DAPI",))
    try:
        asked = []
        w._start_loader_for = lambda idx, needed=None: asked.append(list(needed or []))
        _show(w, "CD3", 0.8)
        asked.clear()

        w.set_preview_mode("fusion")

        assert asked == [["CD3"]]
    finally:
        w.close()


def test_restoring_a_fusion_session_asks_only_for_what_it_shows(app):
    w = _window(app, cached=("DAPI",))
    try:
        # Weighted and then TAKEN OUT: since the 2026-09-16 ruling a weight
        # enlists the channel, so "a channel the user had hidden" is now
        # reached by unticking it, not by weighting it and leaving the tick
        # alone. What this test measures -- a restore reads only what it
        # shows -- is unchanged.
        w.config._rows["CD3"].spin.setValue(0.8)
        w.config.set_fusion_enabled("CD3", False)
        w.config.set_channel_visible("CD3", False)
        asked = []
        w._start_loader_for = lambda idx, needed=None: asked.append(list(needed or []))

        w._apply_step1_display_state({
            "preview_mode": "fusion",
            "channel_visibility": {"DAPI": True, "CD3": False, "CD8": False},
            "current_channel": "DAPI",
        })

        assert w._step1_preview_mode == "fusion"
        # CD3 keeps its weight for when it is ticked again, and is read for
        # nothing while it is not: a session brings back the picture that was
        # saved, not a channel the user had hidden.
        assert not any("CD3" in a for a in asked)
        assert "CD3" not in w._fusion_weighted_channels()
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
        loads, draws = [], []
        w._start_loader_for = lambda idx, needed=None: loads.append(list(needed or []))
        # After the stub, so the restore is the only thing that asks. CD3 is
        # weighted and then taken out: a weight enlists a channel since the
        # 2026-09-16 ruling, and this test is about a HIDDEN channel not being
        # read.
        w.config._rows["CD3"].spin.setValue(0.8)
        w.config._rows["CD8"].spin.setValue(0.5)
        w.config.set_fusion_enabled("CD3", False)
        w.config.set_channel_visible("CD3", False)
        loads.clear()
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
        assert sorted(loads[0]) == ["CD8"]      # CD3 is hidden: not read
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


# ── a session that can tell its own zeros apart ──────────────────────────
#
# `channel_weights` saves 0.0 for a marker nobody ever enabled AND for one the
# user deliberately set to 0.00. After a restart the first tick has to answer
# 1.0 for the first and leave the second alone, so the session carries which
# weights are ANSWERS alongside the numbers.

def test_a_session_round_trips_which_zeros_are_answers(app):
    w = _window(app)
    try:
        # CD3: the user says 0.00 on purpose. CD8: never enabled at all.
        w.config.set_fusion_enabled("CD3", True)
        w.config._rows["CD3"].spin.setValue(0.0)
        payload = w._step1_session_payload()

        assert payload["channel_weights"]["CD3"] == 0.0
        assert payload["channel_weights"]["CD8"] == 0.0
        assert payload["channel_weight_initialized"] == ["CD3"]
    finally:
        w.close()

    w = _window(app)
    try:
        w._restore_step1_scientific_state(payload)

        w.config.set_fusion_enabled("CD3", False)
        w.config.set_fusion_enabled("CD3", True)
        assert w.config.channel_weight("CD3") == 0.0, \
            "the user's own 0.00 came back as a default"

        w.config.set_fusion_enabled("CD8", True)
        assert w.config.channel_weight("CD8") == 1.0, \
            "a channel nobody ever enabled is still un-initialised"
    finally:
        w.close()


def test_an_empty_history_field_is_not_read_as_a_missing_one(app):
    """A session in which nobody had weighted anything saves an EMPTY list.
    Read as "absent", the conservative migration would mark every weight in
    the saved config as an answer and the next first tick would leave the
    channel invisible at 0."""
    w = _window(app)
    try:
        payload = w._step1_session_payload()
        assert payload["channel_weight_initialized"] == []
    finally:
        w.close()

    w = _window(app)
    try:
        w._restore_step1_scientific_state(payload)

        w.config.set_fusion_enabled("CD3", True)
        assert w.config.channel_weight("CD3") == 1.0
    finally:
        w.close()


def test_an_old_session_without_the_field_keeps_its_stored_weights(app):
    """The migration, conservative on purpose: a session written before this
    field existed has weights that somebody meant, 0 included, so opening the
    project and ticking a channel must not silently rewrite them."""
    w = _window(app)
    try:
        old_session = {
            "fusion_config": {
                "nucleus": {"channel": "DAPI", "weight": 1.0},
                "groups": {"markers": {"group_weight": 1.0,
                                       "channels": {"CD3": 0.0, "CD8": 0.7}}},
            },
            "channel_visibility": {"DAPI": True, "CD3": False, "CD8": False},
        }
        assert "channel_weight_initialized" not in old_session

        w._apply_step1_fusion_config(old_session["fusion_config"])
        w._apply_step1_display_state(old_session)

        w.config.set_channel_visible("CD3", True)
        assert w.config.channel_weight("CD3") == 0.0, \
            "an old session's stored 0 was overwritten with the default"
        w.config.set_channel_visible("CD8", True)
        assert w.config.channel_weight("CD8") == pytest.approx(0.7)
    finally:
        w.close()


def test_restoring_a_session_ticks_nothing_and_weighs_nothing(app):
    """Restoring is not clicking: the ticks and weights that come back are the
    ones that were saved, and the current channel does not get a default
    either."""
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(0.0)
        w.config.set_channel_visible("CD3", False)
        w.config.set_current_channel("CD3", auto_show=True)
        w.config._rows["CD3"].spin.setValue(0.0)
        w.config.set_channel_visible("CD3", False)
        payload = w._step1_session_payload()
        assert payload["channel_visibility"]["CD3"] is False
        assert payload["current_channel"] == "CD3"
    finally:
        w.close()

    w = _window(app)
    try:
        w._apply_step1_fusion_config(payload["fusion_config"])
        w._apply_step1_display_state(payload)

        assert "CD3" not in w.config.visible_channels(), \
            "a channel the user left hidden came back ticked"
        assert w.config.channel_weight("CD3") == 0.0
        assert w.config.current_channel() == "CD3"
    finally:
        w.close()


# ── the same picture, computed once ──────────────────────────────────────
#
# MEASURED (docs/perf_timeline/prefix_run_excerpt.log and the full pre-fix
# run): during a Min/Max drag the overlay's `step1.compose` cost p50 49.96 ms
# per frame while the ONE channel whose window had changed cost about 8 ms to
# map, and the fusion preview's `step1.signals` cost p50 36.31 ms against a
# `fuse` of 4 ms. Both were the same mistake: work already done, done again.

def test_the_overlay_composite_is_numerically_what_it_replaced(app):
    """The optimisation may not change a pixel.

    `tint_and_sum_grays` is the second half of
    `compose_multichannel_overlay`; the first half -- the per-channel remap --
    has already been done by `_overlay_gray` and cached. Handing the grays
    back through the general compositor with an identity window produced
    exactly this, at the price of redoing every map.
    """
    from block01.core.channel_remap import (
        compose_multichannel_overlay, tint_and_sum_grays,
    )
    rng = np.random.default_rng(11)
    # Includes values OUTSIDE [0,1] on purpose: that is the only place the
    # identity window does anything at all (it clips), so a fast path that
    # dropped the clip would agree everywhere else and differ exactly there.
    out_of_range = rng.random((24, 18), dtype=np.float32) * 2.0 - 0.5
    grays = {"DAPI": rng.random((24, 18), dtype=np.float32),
             "CD3": rng.random((24, 18), dtype=np.float32) * 0.35,
             "CD8": np.zeros((24, 18), np.float32),
             "CD20": out_of_range}
    colors = {"DAPI": (0.1, 0.4, 1.0), "CD3": (1.0, 0.2, 0.2),
              "CD8": (0.3, 1.0, 0.3), "CD20": (0.9, 0.9, 0.1)}
    identity = {ch: {"min": 0.0, "max": 1.0, "brightness": 0.0,
                     "contrast": 1.0, "gamma": 1.0} for ch in grays}

    fast = tint_and_sum_grays(grays, colors)
    old = compose_multichannel_overlay(grays, colors, identity)

    assert fast is not None
    # Equal to the last bit but for the identity window's OWN rounding: it
    # computes (x - 0) / (1 - 0) and a gamma of 1.0 in float32, which moves
    # some values by one unit in the last place. The direct path skips that
    # arithmetic, so where they differ it is the more exact of the two.
    assert np.abs(fast - old).max() < 1e-6
    assert fast.dtype == old.dtype == np.float32
    assert fast.shape == old.shape


def test_the_overlay_frame_does_not_remap_the_grays_again(app, monkeypatch):
    """The composite may not re-run the mapping it was handed the output of.

    Watched at the CORE module's own reference, which is what the general
    compositor calls: `main_window` imported `apply_channel_remap` at import
    time for its own per-channel mapping, so counting that one cannot see a
    second pass happening inside `compose_multichannel_overlay`.
    """
    import block01.core.channel_remap as core

    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config.set_channel_visible("CD8", True)
        w._render_overlay_patch(reset_view=False)   # warm the grays

        calls = []
        real = core.apply_channel_remap
        monkeypatch.setattr(core, "apply_channel_remap",
                            lambda arr, params=None: (calls.append(
                                getattr(arr, "shape", None)),
                                real(arr, params))[1])
        w._render_overlay_patch(reset_view=False)

        assert calls == [], (
            "the composite re-ran the per-channel remap over the grays it "
            f"was given: {len(calls)} extra passes")
    finally:
        w.close()


def test_the_overlay_maps_each_channel_once_per_window(app, monkeypatch):
    """And the mapping that DOES belong to a frame happens once: the first
    frame maps what it draws, the second maps nothing.

    Counted where the mapping now lives -- `core.preview_compose`, the one
    implementation the window and the frame worker share.
    """
    import block01.core.preview_compose as pc

    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        calls = []
        real = pc.apply_channel_remap
        monkeypatch.setattr(pc, "apply_channel_remap",
                            lambda arr, params=None: (calls.append(1),
                                                      real(arr, params))[1])

        w._overlay_display_cache.clear()
        w._render_overlay_patch(reset_view=False)
        assert len(calls) > 0, "the frame drew without mapping anything"

        calls.clear()
        w._render_overlay_patch(reset_view=False)

        assert calls == [], "a second identical frame mapped again"
    finally:
        w.close()


def test_a_colour_change_reuses_every_gray(app):
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w._render_overlay_patch(reset_view=False)
        before = {key: w._overlay_display_cache.entry(key)["value"]
                  for key in w._overlay_display_cache.keys()}
        assert before

        w.config.set_channel_color("CD3", "#00ff88")
        w._render_overlay_patch(reset_view=False)

        assert set(w._overlay_display_cache.keys()) == set(before)
        for key, value in before.items():
            assert w._overlay_display_cache.entry(key)["value"] is value, \
                "a colour change recomputed a channel's grayscale"
    finally:
        w.close()


def test_the_fusion_preview_recomputes_only_the_changed_channel(app):
    """A Min/Max drag is one channel's window moving. The other signals are
    the same arrays through the same numbers."""
    w = _window(app)
    try:
        w.set_preview_mode("fusion", force=True, reconcile=False)
        _show(w, "CD3")
        _show(w, "CD8")
        w._render_current_patch(reset_view=False)
        first = {key: w._signal_cache.entry(key)["value"]
                 for key in w._signal_cache.keys()}
        assert len(first) >= 2, first

        # One channel's window moves; the others are untouched.
        window = {"CD3": {"min": 5.0, "max": 900.0, "gamma": 1.0}}
        w._display_mapping = lambda *a, **k: window
        w._render_current_patch(reset_view=False)

        reused = [key for key, signal in first.items()
                  if w._signal_cache.entry(key) is not None
                  and w._signal_cache.entry(key)["value"] is signal]
        assert any(key[1] != "CD3" for key in reused), \
            "an unchanged channel's signal was recomputed"
        changed = w._signal_cache.entry(
            (w._preview_patch_idx, "CD3", "signal"))
        assert changed is not None
        assert changed["value"] is not first.get(
            (w._preview_patch_idx, "CD3", "signal")), \
            "the changed channel was not recomputed"
    finally:
        w.close()


def test_a_signal_cannot_be_served_for_different_pixels(app):
    """The key holds the array's identity, so a patch reload or a dataset
    switch cannot serve yesterday's signal for today's pixels.

    Pixels that have been replaced leave their entries behind until something
    clears them (every path that drops the raw arrays does, and the bound
    catches the rest) -- what matters is that such an entry can never be
    HIT: the new arrays are different objects, so the lookup misses and the
    signal is recomputed.
    """
    w = _window(app)
    try:
        w.set_preview_mode("fusion", force=True, reconcile=False)
        w.config.set_channel_visible("CD3", True)
        w._render_current_patch(reset_view=False)
        old_arr = w._patch_channel_cache[0]["CD3"]
        window = w._display_mapping(channels=["CD3"])
        first = w._preview_channel_signal("CD3", old_arr, window)

        rng = np.random.default_rng(5)
        new_arr = rng.random((32, 32), dtype=np.float32) + 0.1
        w._patch_channel_cache[0]["CD3"] = new_arr

        second = w._preview_channel_signal("CD3", new_arr, window)

        assert second is not first, \
            "the cache served the previous array's signal for new pixels"
        assert w._preview_channel_signal("CD3", new_arr, window) is second, \
            "and the same pixels through the same window are not recomputed"
    finally:
        w.close()


def test_dropping_a_patch_drops_its_signals(app):
    w = _window(app)
    try:
        w.set_preview_mode("fusion", force=True, reconcile=False)
        w.config.set_channel_visible("CD3", True)
        w._render_current_patch(reset_view=False)
        assert w._signal_cache

        w._drop_overlay_cache_for(w._preview_patch_idx)

        assert w._signal_cache.keys() == []
    finally:
        w.close()


def test_a_drag_keeps_one_version_per_channel(app):
    """A slider step is a new window, and the previous one has no reader. On
    a large slide a signal is 4 MiB, so 300 remembered steps would be over a
    gigabyte of history for a picture nobody will ask for again."""
    w = _window(app)
    try:
        arr = np.linspace(0, 1000, 32 * 32, dtype=np.float32).reshape(32, 32)
        for i in range(300):
            window = {"CD3": {"min": float(i), "max": 1000.0, "gamma": 1.0}}
            w._preview_channel_signal("CD3", arr, window)

        key = (w._preview_patch_idx, "CD3", "signal")
        assert w._signal_cache.keys() == [key]
        from block01.core import preview_compose as pc
        assert w._signal_cache.entry(key)["window"] == pc.window_key(
            "CD3", {"CD3": {"min": 299.0, "max": 1000.0, "gamma": 1.0}})
    finally:
        w.close()


def test_dropping_one_patch_keeps_the_others_signals(app):
    w = _window(app)
    try:
        arr = np.linspace(0, 1000, 32 * 32, dtype=np.float32).reshape(32, 32)
        window = {"CD3": {"min": 0.0, "max": 1000.0, "gamma": 1.0}}
        w._preview_patch_idx = 0
        w._preview_channel_signal("CD3", arr, window)
        w._preview_patch_idx = 1
        other = arr.copy()
        w._preview_channel_signal("CD3", other, window)
        assert set(w._signal_cache.keys()) == {(0, "CD3", "signal"),
                                              (1, "CD3", "signal")}

        w._drop_overlay_cache_for(0)

        assert set(w._signal_cache.keys()) == {(1, "CD3", "signal")}
    finally:
        w.close()
