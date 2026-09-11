"""One Tissue Preview, one colour, one clock -- across Step0, Step1, Step2, Step3.

WHAT THE USER REPORTED, and what each section here pins:

* the same channel came up in two colours, because Step0 dealt one palette and
  Step1's `ConfigPanel` dealt another and cleared its own on every dataset;
* the shared Tissue Preview kept showing Step0's single-channel picture after
  the user walked into Step1, whatever Step1's ticks, weights, colours or
  preview mode said;
* Step1's main viewer followed a weight or a Min/Max drag live while the
  Tissue Preview did not follow it at all.

None of those are a missing signal. They are what a window owned by one step
and drawn from that step's private fields has to do. So what is under test is
the OWNERSHIP: Block01 holds the display state and the render pipeline, each
step registers a render context, and exactly one context draws.

Own module, like the other page-heavy PyQt suites: combined runs crash
pyqtgraph offscreen.
"""

import os
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets  # noqa: E402

from block01.core import tissue_compose  # noqa: E402
from block01.ui import block01_display as bd  # noqa: E402


SLIDE_H, SLIDE_W = 512, 256
LOW_H, LOW_W = 32, 16


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Loader:
    """A slide with a whole-slide low-resolution read and a per-channel PATTERN.

    Patterned, not constant: a weight, a colour or a window change over a flat
    array can be swallowed by any normalisation in the chain, so a test over
    one would pass while showing the wrong picture.
    """

    filepath = "/tmp/dataset.ome.tiff"
    shape = (SLIDE_H, SLIDE_W)

    def __init__(self, path="/tmp/dataset.ome.tiff"):
        self.filepath = path
        self._names = ["DAPI", "CD3", "CD8"]
        self.ch_map = {c: i for i, c in enumerate(self._names)}
        self.reads = []
        self.lowres_reads = []

    def channel_names(self):
        return list(self._names)

    @staticmethod
    def overview_downsample():
        return SLIDE_H // LOW_H

    @staticmethod
    def _norm(arr):
        arr = np.asarray(arr, dtype=np.float32)
        hi = float(arr.max()) or 1.0
        return np.clip(arr / hi, 0.0, 1.0)

    def _pattern(self, channel, h, w):
        i = self.ch_map.get(channel, 0)
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        if i == 0:                       # DAPI: a vertical ramp
            base = yy / max(1.0, h - 1.0)
        elif i == 1:                     # CD3: a horizontal ramp
            base = xx / max(1.0, w - 1.0)
        else:                            # CD8: a diagonal
            base = ((yy + xx) / max(1.0, h + w - 2.0))
        return (base * 200.0 + 10.0).astype(np.float32)

    def read_region(self, channel, y0, y1, x0, x1, downsample=1,
                    normalize=True):
        self.reads.append(channel)
        ds = max(1, int(downsample))
        return self._pattern(channel, (y1 - y0) // ds or 1,
                             (x1 - x0) // ds or 1)

    def read_region_lowres(self, channel, y0, y1, x0, x1, downsample,
                           normalize=False):
        self.lowres_reads.append(channel)
        ds = max(1, int(downsample))
        return self._pattern(channel, (y1 - y0) // ds or 1,
                             (x1 - x0) // ds or 1)


class _InlineTissueFrames:
    """Block01's tissue compose thread, inline and under the test's control.

    The REAL compose code on the REAL snapshot -- `TissueComposeWorker._compose`
    -- so the pixels are production pixels; only the delivery is the test's.
    A thread would make every assertion about "how many frames appeared during
    the drag" a race.
    """

    def __init__(self, coordinator, auto=True):
        from block01.core import preview_compose
        from block01.workers.tissue_compose_worker import TissueComposeWorker
        self._co = coordinator
        self.auto = bool(auto)
        self.submitted = []
        self.delivered = []
        self._worker = TissueComposeWorker.__new__(TissueComposeWorker)
        self._worker.cache = preview_compose.PreviewCache()
        self._compose = TissueComposeWorker._compose
        coordinator._dispatch = self._submit

    def _submit(self, snapshot):
        self.submitted.append(snapshot)
        if self.auto:
            self.deliver()
        return True

    def pending(self):
        return len(self.submitted)

    def deliver(self, count=None):
        done = 0
        while self.submitted and (count is None or done < count):
            snapshot = self.submitted.pop(0)
            result = self._compose(self._worker, snapshot)
            self.delivered.append(result)
            self._co.on_frame(result)
            done += 1
        return done


def _window(app, path="/tmp/dataset.ome.tiff"):
    from block01.ui.main_window import MainWindow
    w = MainWindow()
    loader = _Loader(path)
    w.loader = loader
    # Step0 is the page holding the physical windows and the low-resolution
    # service; give it the same slide, exactly as a real load does.
    page = w._step0
    page.loader = loader
    page.ome_path = path
    page.nucleus_channel = "DAPI"
    page.patches = []
    page._rebuild_channel_list()
    page.current_channel = "CD3"
    page.overview.loader = loader
    page.overview.full_h, page.overview.full_w = SLIDE_H, SLIDE_W

    w.config.set_channels(loader.channel_names())
    w.config.load_panel({"markers": {"CD3": 0.0, "CD8": 0.0}}, "DAPI")
    w.config.set_nucleus("DAPI", 1.0)
    w._all_patches = [(0, 32, 0, 32)]
    w._preview_patch_idx = 0
    rng = np.random.default_rng(3)
    w._patch_channel_cache[0] = {ch: rng.random((32, 32), dtype=np.float32) + 0.1
                                 for ch in loader.channel_names()}
    w._patch_load_ready.add(0)
    w._tissue_frames = _InlineTissueFrames(w._display.coordinator)
    # The patch viewer's own frames stay on the GUI thread here: this module
    # is about the thumbnail, and a second real thread would only add races.
    w._dispatch_frame = lambda snapshot: False
    return w


def _thumb(w):
    """What the ONE Tissue Preview is drawing."""
    return w._step0.overview.img_item.image


def _mean_rgb(img):
    arr = np.asarray(img)
    return tuple(float(v) for v in arr.reshape(-1, 3).mean(axis=0))


def _pump(w, ms=250):
    """Let the frame clock reach its slot; the compose is already inline.

    A test that has switched delivery off is held to it: nothing is delivered
    here, so "a frame is in flight" stays a state the test can inspect.
    """
    deadline = time.monotonic() + ms / 1000.0
    co = w._display.coordinator
    frames = w._tissue_frames
    while time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        if frames.auto and frames.pending():
            frames.deliver()
            continue
        if not co.frame_stats()["pending"]:
            return
        time.sleep(0.002)
    QtWidgets.QApplication.processEvents()
    if frames.auto:
        frames.deliver()


def _goto(w, step):
    w._set_step_active(step)
    _pump(w)


# ── B. one colour, in every step ─────────────────────────────────────────

def test_a_default_colour_is_dealt_once_for_the_whole_process(app):
    """The palette default is ONE deal. Step1 used to make its own from its
    own `_PALETTE`, so a channel nobody had touched was already two colours
    the first time Step1 opened."""
    w = _window(app)
    try:
        for ch in ("CD3", "CD8", "DAPI"):
            shared = w._display.state.color(ch)
            assert w.config.channel_color(ch) == shared, ch
            assert w._step0._channel_color_hex(ch) == shared, ch
    finally:
        w.close()


def test_a_colour_picked_in_step0_survives_the_walk_into_step1(app):
    w = _window(app)
    try:
        w._step0._apply_channel_color("CD3", (1.0, 0.0, 0.0))
        # ...and then Step1 opens its panel for the dataset, which is where
        # the old code re-dealt the palette.
        w.config.load_panel({"markers": {"CD3": 0.0, "CD8": 0.0}}, "DAPI")

        assert w.config.channel_color("CD3") == "#ff0000"
        assert w._display.state.color("CD3") == "#ff0000"
    finally:
        w.close()


def test_a_colour_picked_in_step1_is_the_same_colour_in_step0(app):
    w = _window(app)
    try:
        w.config.set_channel_color("CD3", "#00ff00")

        assert w._step0._channel_color_hex("CD3") == "#00ff00"
        assert w._display.state.color("CD3") == "#00ff00"
    finally:
        w.close()


def test_a_colour_settles_in_one_pass_with_no_loop(app):
    """Two stores emitting at each other is how "who wins" became a question
    of signal order. One write, one fan-out, and an echo changes nothing."""
    w = _window(app)
    try:
        seen = []
        w._display.state.color_changed.connect(
            lambda ch, hexc: seen.append((ch, hexc)))

        w.config.set_channel_color("CD8", "#0000ff")
        # The echo a mirror would send back.
        w._step0.adopt_channel_color("CD8", "#0000ff")
        w.config.set_channel_color("CD8", "#0000ff")

        assert seen == [("CD8", "#0000ff")], seen
    finally:
        w.close()


def test_a_restored_session_puts_its_colours_in_every_step_at_once(app):
    w = _window(app)
    try:
        w.config.restore_display_state(colors={"CD3": "#abcdef"},
                                       visibility={"CD3": True})

        assert w._display.state.color("CD3") == "#abcdef"
        assert w._step0._channel_color_hex("CD3") == "#abcdef"
        assert w.config.channel_color("CD3") == "#abcdef"
    finally:
        w.close()


# ── C. the thumbnail is the ACTIVE step's picture ────────────────────────

def test_step0_draws_its_single_channel(app):
    w = _window(app)
    try:
        _goto(w, 0)
        assert w._display.coordinator.active_context_id() == bd.STEP0
        assert w._display.coordinator.last_published()["mode"] == "step0"
        assert _thumb(w) is not None
    finally:
        w.close()


def test_entering_step1_redraws_the_popup_as_step1(app):
    """The reported symptom: the popup kept Step0's picture."""
    w = _window(app)
    try:
        w._step0.show_tissue_navigator()
        _goto(w, 0)
        step0_frame = np.array(_thumb(w), copy=True)

        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        _goto(w, 1)

        assert w._display.coordinator.active_context_id() == bd.STEP1
        published = w._display.coordinator.last_published()
        assert published["mode"] == tissue_compose.MODE_OVERLAY, published
        assert not np.array_equal(_thumb(w), step0_frame)
    finally:
        w.close()


def test_a_late_step0_frame_cannot_land_on_step1(app):
    w = _window(app)
    try:
        _goto(w, 0)
        # A Step0 frame is composed but not yet delivered -- the real shape of
        # the race: the user moves a slider and walks to Step1 before the
        # worker comes back.
        w._tissue_frames.auto = False
        w._step0._queue_tissue_preview(kind="mapping", channel="CD3")
        _pump(w, 60)
        assert w._tissue_frames.pending() == 1, "no Step0 frame in flight"
        stale = w._tissue_frames.submitted.pop()

        w._tissue_frames.auto = True
        _goto(w, 1)
        on_screen = np.array(_thumb(w), copy=True)

        # ...and NOW the Step0 frame comes back.
        w._tissue_frames.submitted.append(stale)
        w._tissue_frames.deliver()

        assert np.array_equal(_thumb(w), on_screen), "Step0 drew over Step1"
        assert w._display.coordinator.last_published()["owner"] == bd.STEP1
    finally:
        w.close()


def test_a_weight_drag_moves_the_thumbnail_and_leaves_the_other_channel(app):
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config.set_channel_visible("CD8", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        w.config._rows["CD8"].spin.setValue(1.0)
        w._step0._apply_channel_color("CD3", (1.0, 0.0, 0.0))
        w._step0._apply_channel_color("CD8", (0.0, 1.0, 0.0))
        _goto(w, 1)
        frames = []
        w._display.coordinator.frame_published.connect(
            lambda r: frames.append(np.array(r["rgb"], copy=True)))

        for value in (1.0, 0.7, 0.5, 0.3, 0.1, 0.0):
            w.config._rows["CD3"].spin.setValue(value)
            _pump(w, 120)

        assert len(frames) >= 4, f"only {len(frames)} frame(s) during the drag"
        reds = [_mean_rgb(f)[0] for f in frames]
        greens = [_mean_rgb(f)[1] for f in frames]
        assert reds[0] > reds[-1], reds        # the dragged channel faded
        assert max(greens) - min(greens) < 1e-6, greens   # the other did not
        # Weight 0 is not an untick: the channel stays in the configuration.
        assert "CD3" in w.config.visible_channels()
    finally:
        w.close()


def test_a_mapping_drag_moves_the_thumbnail_too(app):
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        _goto(w, 1)
        frames = []
        w._display.coordinator.frame_published.connect(
            lambda r: frames.append(np.array(r["rgb"], copy=True)))
        lo, hi, _g = w._step0._display_mapping_for("CD3")

        for i in range(1, 7):
            w._step0.set_display_mapping("CD3", lo, hi - (hi - lo) * 0.1 * i)
            _pump(w, 120)

        assert len(frames) >= 4, f"only {len(frames)} frame(s)"
        assert len({f.tobytes() for f in frames}) >= 3, "the same picture"
        # The picture ends on the numbers the control ended on.
        assert w._display.state.mapping("CD3") == pytest.approx(
            w._step0._display_mapping_for("CD3"))
    finally:
        w.close()


def test_fusion_uses_the_fusion_core_and_not_a_step0_tint(app):
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        w._step0._apply_channel_color("CD3", (1.0, 0.0, 0.0))
        _goto(w, 1)
        overlay = np.array(_thumb(w), copy=True)

        w.set_preview_mode("fusion", force=True)
        _pump(w)

        published = w._display.coordinator.last_published()
        assert published["mode"] == tissue_compose.MODE_FUSION, published
        fusion = np.array(_thumb(w), copy=True)
        assert not np.array_equal(fusion, overlay)
        # red = cyto, blue = nucleus. DAPI carries the nucleus weight, so the
        # blue channel has to be lit -- a Step0 single-channel tint of a red
        # marker could not produce it.
        assert _mean_rgb(fusion)[2] > 0.0
    finally:
        w.close()


def test_switching_mode_keeps_the_one_popup_its_camera_and_its_artists(app):
    w = _window(app)
    try:
        popup = w._step0.show_tissue_navigator()
        _goto(w, 1)
        view = popup.overview.gview.sceneRect()

        w.set_preview_mode("fusion", force=True)
        _pump(w)

        assert w._step0.show_tissue_navigator() is popup, "the popup was rebuilt"
        assert popup.overview.gview.sceneRect() == view
    finally:
        w.close()


def test_the_thumbnail_does_not_need_a_patch(app):
    """The navigator is whole-slide. Step1 having no patch loaded is about
    the PATCH viewer and must not hold the map empty."""
    w = _window(app)
    try:
        w._patch_channel_cache.clear()
        w._patch_load_ready.clear()
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        _goto(w, 1)

        assert _thumb(w) is not None
        assert w._display.coordinator.last_published()["owner"] == bd.STEP1
    finally:
        w.close()


def test_a_frame_reads_only_the_channels_it_draws(app):
    """One weight change must not read a 29-channel panel."""
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        _goto(w, 1)
        w.loader.lowres_reads.clear()

        w.config._rows["CD3"].spin.setValue(0.4)
        _pump(w)

        assert w.loader.lowres_reads == [], w.loader.lowres_reads
    finally:
        w.close()


# ── D. the whole walk: Step0 -> 1 -> 2 -> 3 -> back ──────────────────────

def test_the_two_shared_windows_are_the_same_objects_in_every_step(app):
    w = _window(app)
    try:
        popup = w._display.show_navigator()
        intensity = w._display.show_intensity("CD3")
        for step in (0, 1, 2, 3, 2, 1, 0):
            _goto(w, step)
            assert w._display.show_navigator() is popup, step
            assert w._display.show_intensity("CD3") is intensity, step
    finally:
        w.close()


def test_every_step_transition_moves_the_context_and_the_generation(app):
    w = _window(app)
    try:
        co = w._display.coordinator
        seen = []
        for step, expected in ((0, bd.STEP0), (1, bd.STEP1), (2, bd.STEP2),
                               (3, bd.STEP3), (0, bd.STEP0)):
            before = co.generation()
            _goto(w, step)
            assert co.active_context_id() == expected, step
            assert co.generation() > before, step
            seen.append(expected)
        assert seen == [bd.STEP0, bd.STEP1, bd.STEP2, bd.STEP3, bd.STEP0]
    finally:
        w.close()


def test_downstream_steps_pin_a_frame_that_was_really_on_screen(app):
    """Step2/Step3 consume geometry they did not produce and choose no
    thumbnail, so they re-publish the last frame -- never Step0's current
    fields dressed up as global state."""
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        _goto(w, 1)
        step1_frame = np.array(_thumb(w), copy=True)

        _goto(w, 2)
        assert np.array_equal(_thumb(w), step1_frame)
        published = w._display.coordinator.last_published()
        assert published["owner"] == bd.STEP2
        assert published["mode"] == tissue_compose.MODE_PINNED

        _goto(w, 3)
        assert np.array_equal(_thumb(w), step1_frame)
    finally:
        w.close()


def test_a_late_frame_from_each_step_is_refused_downstream(app):
    w = _window(app)
    try:
        co = w._display.coordinator
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        for leaving, entering in ((0, 1), (1, 2), (2, 3)):
            _goto(w, leaving)
            w._tissue_frames.auto = False
            co.request_frame(kind="test")
            _pump(w, 60)
            stale = list(w._tissue_frames.submitted)
            w._tissue_frames.submitted.clear()
            w._tissue_frames.auto = True

            _goto(w, entering)
            on_screen = np.array(_thumb(w), copy=True)
            for snapshot in stale:
                w._tissue_frames.submitted.append(snapshot)
            w._tissue_frames.deliver()

            assert np.array_equal(_thumb(w), on_screen), (leaving, entering)
    finally:
        w.close()


def test_a_stale_frame_is_refused_between_two_steps_of_the_SAME_mode(app):
    """The mode check is not enough, and this is the case that proves it.

    Step2 and Step3 both render a pinned frame, so a Step2 frame arriving
    after the user reached Step3 matches on mode AND on dataset. What refuses
    it is the OWNER and the GENERATION -- the two facts a step transition
    moves atomically. Without them a step that has been left keeps drawing.
    """
    w = _window(app)
    try:
        co = w._display.coordinator
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        _goto(w, 1)
        _goto(w, 2)

        w._tissue_frames.auto = False
        co.request_frame(kind="test")
        _pump(w, 60)
        assert w._tissue_frames.pending() == 1
        stale = w._tissue_frames.submitted.pop()
        assert stale["mode"] == tissue_compose.MODE_PINNED
        w._tissue_frames.auto = True

        _goto(w, 3)
        published_before = dict(co.last_published())
        dropped_before = co.frame_stats()["dropped"]

        w._tissue_frames.submitted.append(stale)
        w._tissue_frames.deliver()

        assert co.frame_stats()["dropped"] > dropped_before, "it was accepted"
        assert co.last_published() == published_before
        assert stale["owner"] == bd.STEP2 and co.active_context_id() == bd.STEP3
    finally:
        w.close()


def test_a_drag_dispatches_on_the_clock_not_once_per_input(app):
    """Single-flight, and the evidence is the DISPATCH count.

    A worker that is handed every input still publishes latest-only -- its own
    pending slot replaces -- so the picture looks right while the GUI thread
    builds a snapshot per slider step. What must be bounded is how many
    snapshots are built at all.
    """
    w = _window(app)
    try:
        co = w._display.coordinator
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        _goto(w, 1)
        w._tissue_frames.auto = False
        co.reset_stats()

        lo, hi, _g = w._step0._display_mapping_for("CD3")
        for i in range(1, 201):
            w._step0.set_display_mapping("CD3", lo, hi - (hi - lo) * 0.002 * i)
            assert co.frame_stats()["pending"] <= 1

        stats = co.frame_stats()
        assert stats["inputs"] >= 200, stats
        assert stats["dispatched"] <= 2, stats      # one in flight, at most
        assert stats["max_pending_depth"] <= 1, stats
    finally:
        w._tissue_frames.auto = True
        w.close()


def test_the_mapping_is_one_set_of_numbers_across_the_walk(app):
    w = _window(app)
    try:
        state = w._display.state
        lo, hi, _g = w._step0._display_mapping_for("CD3")
        w._step0.set_display_mapping("CD3", lo, hi / 2.0)
        answers = []
        for step in (0, 1, 2, 3):
            _goto(w, step)
            answers.append(state.mapping("CD3"))
        assert len(set(answers)) == 1, answers
        assert answers[0][1] == pytest.approx(hi / 2.0)
    finally:
        w.close()


def test_downstream_steps_still_follow_a_change_to_the_shared_state(app):
    """Step2/Step3 are read-only, but read-only is about the CONTROLS. A
    legitimate change to the canonical state still has to reach an open
    Tissue Preview, and the navigator must stay read-only while it does."""
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        _goto(w, 1)
        _goto(w, 2)
        before = np.array(_thumb(w), copy=True)

        # A legal global change: the canonical colour of a channel in the
        # pinned picture. Nothing in Step2's UI is faked to do it.
        w._display.state.set_color("CD3", "#ff00ff")
        _pump(w)

        assert w._step0.navigator_edit_policy() == {
            "roi_policy": "read_only", "patch_editable": False}
        # The pinned frame is re-published rather than recomposed, so the
        # picture is stable -- what is pinned here is that the request is
        # ACCEPTED for the active context and does not fall back to Step0.
        assert w._display.coordinator.last_published()["owner"] == bd.STEP2
        assert np.array_equal(_thumb(w), before)
    finally:
        w.close()


def test_downstream_steps_do_not_depend_on_step0_page_state(app):
    """The deferred half of the move, pinned so it cannot rot.

    The two shared windows are still CONSTRUCTED by Step0 (the popup is built
    around the ROI/patch toolbar, the Intensity window hosts the workbench's
    detached inspector), and that is recorded in `Block01DisplayServices`. What
    must not be true is that they still DEPEND on Step0's page state: with
    Step2 active, moving Step0's current channel and its colours must change
    nothing about the picture, because Step0 is not the context that is
    drawing.
    """
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        _goto(w, 1)
        _goto(w, 2)
        before = np.array(_thumb(w), copy=True)

        w._step0.current_channel = "CD8"
        w._step0._channel_colors["CD8"] = (1.0, 1.0, 0.0)
        w._step0._queue_tissue_preview(kind="channel")
        _pump(w)

        assert np.array_equal(_thumb(w), before)
        assert w._display.coordinator.active_context_id() == bd.STEP2
    finally:
        w.close()


def test_the_gui_thread_composes_nothing_during_a_drag(app):
    """The arrays are the worker's. A frame the GUI thread composed is a GUI
    stall of that frame's cost, which at 30 FPS is the whole problem back."""
    w = _window(app)
    try:
        co = w._display.coordinator
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        _goto(w, 1)
        co.reset_stats()

        lo, hi, _g = w._step0._display_mapping_for("CD3")
        for i in range(1, 41):
            w._step0.set_display_mapping("CD3", lo, hi - (hi - lo) * 0.01 * i)
            _pump(w, 60)

        stats = co.frame_stats()
        assert stats["published"] >= 3, stats
        assert stats["gui_composed"] == 0, stats
    finally:
        w.close()


def test_the_intensity_window_follows_the_step_without_forking_its_numbers(app):
    """One window, one set of Min/Max/Gamma; the step decides only the RIGHTS.

    Step0 owns the remap config and Step1 tunes the fusion it feeds, so both
    edit. Step2 and Step3 consume a committed mapping, so they read -- and
    they still READ, showing the canonical values rather than a copy or a
    blank.
    """
    w = _window(app)
    try:
        w._display.show_intensity("CD3")
        panel = w._step0.intensity_panel()
        lo, hi, _g = w._step0._display_mapping_for("CD3")
        w._step0.set_display_mapping("CD3", lo, hi / 3.0)

        for step, editable in ((0, True), (1, True), (2, False), (3, False),
                               (1, True), (0, True)):
            _goto(w, step)
            assert w._display.intensity_policy()["editable"] is editable, step
            if panel is not None:
                assert panel.isEnabled() is editable, step
            # The numbers are the same ones in every step: one store, read
            # through one port, never copied per step.
            assert w._display.state.mapping("CD3")[1] == pytest.approx(hi / 3.0)
    finally:
        w.close()


def test_closing_block01_retires_the_shared_worker_once(app):
    w = _window(app)
    co = w._display.coordinator
    _goto(w, 0)
    w.close()

    assert co._closing
    assert not co.request_frame(kind="after_close")
    assert co._worker is None
