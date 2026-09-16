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
from block01.ui.main_window import STEP1_PREVIEW_FUSION  # noqa: E402


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


def _ensure_source_file(path):
    """Give the synthetic slide a real file.

    Block01 keys its display namespaces on a source VERSION -- path plus
    `size:mtime_ns` -- and fails closed when it cannot read one, so a path
    that does not exist gets a one-bind ephemeral identity and no restoration
    across binds. A harness that wants to exercise A -> B -> A therefore has
    to put something on disk, exactly as a real slide is.
    """
    import pathlib
    p = pathlib.Path(path)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"synthetic slide " + p.name.encode())
    return str(p)


def _window(app, path="/tmp/dataset.ome.tiff"):
    from block01.ui.main_window import MainWindow
    path = _ensure_source_file(path)
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
    # What a real load does at its commit point: Block01 is told which slide
    # this is, so every window, colour and frame is checked against it. A
    # test that skipped this left the shared state unbound and its display
    # windows in a nameless namespace.
    page._bind_panels_to_dataset()

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
    # Weights and participation are entered in STEP1's public row; a test
    # that drives those widgets is in Step1 when it does, and then walks.
    w._set_step_active(1)
    return w


def _thumb(w):
    """What the ONE Tissue Preview is drawing."""
    return w._step0.overview.img_item.image


def _mean_rgb(img):
    arr = np.asarray(img)
    return tuple(float(v) for v in arr.reshape(-1, 3).mean(axis=0))


def _pump(w, ms=600):
    """Let the frame clock reach its slot; the compose is already inline.

    A test that has switched delivery off is held to it: nothing is delivered
    here, so "a frame is in flight" stays a state the test can inspect.
    """
    deadline = time.monotonic() + ms / 1000.0
    co = w._display.coordinator
    frames = w._tissue_frames
    quiet = 0
    while time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        if frames.auto and frames.pending():
            frames.deliver()
            quiet = 0
            continue
        # A first display window is computed on the seed thread now, so a
        # channel appearing for the first time has no frame until it lands --
        # and the frame it then asks for has to be waited for too, which is
        # what the two quiet rounds are.
        if _seeding(w) or co.frame_stats()["pending"]:
            quiet = 0
            time.sleep(0.002)
            continue
        quiet += 1
        if quiet >= 2:
            return
        time.sleep(0.004)
    QtWidgets.QApplication.processEvents()
    if frames.auto:
        frames.deliver()


def _seeding(w):
    """Is Block01 still working out something the next frame needs?

    Both threads: the whole-slide read and the automatic display window. A
    test that waits only for the frame clock races them.
    """
    for worker in (w._display._seed_worker, w._display._read_worker):
        if worker is not None and worker.is_busy():
            return True
    return False


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


def test_downstream_steps_inherit_the_configuration_not_the_pixels(app):
    """Step2/Step3 draw the spec Step1 was drawing, and draw it AGAIN.

    An earlier cut kept the composed RGB and re-published it. That is a
    screenshot: nothing the user changed afterwards could alter pixels that
    were already baked, so the Tissue Preview went dead past Step1. What is
    inherited now is the semantic render spec -- mode, channels, weights,
    fusion config -- with colours and display windows read live on every
    frame.
    """
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        _goto(w, 1)
        step1_frame = np.array(_thumb(w), copy=True)

        _goto(w, 2)
        published = w._display.coordinator.last_published()
        assert published["owner"] == bd.STEP2
        # Same configuration, so the same picture -- COMPOSED, not replayed.
        assert np.array_equal(_thumb(w), step1_frame)
        assert published["mode"] == tissue_compose.MODE_OVERLAY
        context = w._downstream_contexts[bd.STEP2]
        assert context.has_spec()
        assert "CD3" in context.spec()["channels"]
    finally:
        w.close()


def test_a_colour_change_in_step2_changes_the_picture(app):
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        w._step0._apply_channel_color("CD3", (1.0, 0.0, 0.0))
        _goto(w, 1)
        _goto(w, 2)
        before = np.array(_thumb(w), copy=True)
        assert _mean_rgb(before)[2] < _mean_rgb(before)[0]

        w._display.state.set_color("CD3", "#0000ff")
        _pump(w)

        after = _thumb(w)
        assert not np.array_equal(after, before), "Step2 kept a baked picture"
        assert _mean_rgb(after)[2] > _mean_rgb(before)[2]
    finally:
        w.close()


def test_a_mapping_drag_in_step2_and_step3_publishes_intermediate_frames(app):
    """The hard requirement, downstream: Min/Max/Gamma is live in EVERY step.

    Driven through the global Intensity entry -- the same shared state the
    window's controls write -- not by editing a context's dictionary.
    """
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        _goto(w, 1)
        for step in (2, 3):
            _goto(w, step)
            frames = []
            handle = w._display.coordinator.frame_published.connect(
                lambda r: frames.append(np.array(r["rgb"], copy=True)))
            lo, hi, _g = w._display.state.mapping_or_seed("CD3")
            for i in range(1, 8):
                w._step0.set_display_mapping(
                    "CD3", lo, hi - (hi - lo) * 0.09 * i)
                _pump(w, 120)
            w._display.coordinator.frame_published.disconnect(handle)

            assert len(frames) >= 4, (step, len(frames))
            assert len({f.tobytes() for f in frames}) >= 3, step
            assert w._display.coordinator.active_context_id() == (
                bd.STEP2 if step == 2 else bd.STEP3)
    finally:
        w.close()


def test_a_weight_change_in_step2_and_step3_changes_the_picture(app):
    """Step2/Step3 have no weight panel and none is faked here.

    The weight goes through `Block01DisplayServices.set_render_weight`, which
    is the one entry every step's weight edit uses; it lands on the render
    spec the active downstream context inherited.
    """
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config.set_channel_visible("CD8", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        w.config._rows["CD8"].spin.setValue(1.0)
        w._step0._apply_channel_color("CD3", (1.0, 0.0, 0.0))
        w._step0._apply_channel_color("CD8", (0.0, 1.0, 0.0))
        _goto(w, 1)
        for step in (2, 3):
            _goto(w, step)
            frames = []
            handle = w._display.coordinator.frame_published.connect(
                lambda r: frames.append(np.array(r["rgb"], copy=True)))
            # Starts at 0.7: the inherited spec already says 1.0, and a
            # write that changes nothing correctly reports that it changed
            # nothing.
            for value in (0.7, 0.5, 0.3, 0.1, 0.0):
                assert w._display.set_render_weight("CD3", value), (step, value)
                _pump(w, 120)
            w._display.coordinator.frame_published.disconnect(handle)

            assert len(frames) >= 4, (step, len(frames))
            reds = [_mean_rgb(f)[0] for f in frames]
            greens = [_mean_rgb(f)[1] for f in frames]
            assert reds[0] > reds[-1], (step, reds)
            assert max(greens) - min(greens) < 1e-6, (step, greens)
            # Put it back for the next step's inheritance.
            w._display.set_render_weight("CD3", 1.0)
            _pump(w, 120)
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

    Step2 and Step3 draw the same inherited spec in the same mode, so a Step2
    frame arriving after the user reached Step3 matches on mode AND on
    dataset. What refuses it is the OWNER and the GENERATION -- the two facts
    a step transition moves atomically. Without them a step that has been
    left keeps drawing.
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
        assert stale["owner"] == bd.STEP2
        w._tissue_frames.auto = True

        _goto(w, 3)
        published_before = dict(co.last_published())
        dropped_before = co.frame_stats()["dropped"]

        w._tissue_frames.submitted.append(stale)
        w._tissue_frames.deliver()

        assert co.frame_stats()["dropped"] > dropped_before, "it was accepted"
        assert co.last_published() == published_before
        assert co.active_context_id() == bd.STEP3
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


def test_the_intensity_window_is_editable_in_every_step(app):
    """One window, one set of Min/Max/Gamma, editable wherever the user is.

    A previous cut disabled it in Step2/Step3 on the reasoning that those
    steps "consume a committed mapping". That was this implementation's
    invention and it contradicted the requirement. What DOES lock the
    controls is a running job holding a frozen copy of the configuration --
    a lock is a job, not a page.
    """
    w = _window(app)
    try:
        w._display.show_intensity("CD3")
        panel = w._display.intensity_panel()
        # the inspector's pixels arrive from a worker now, and the seed is
        # computed from them
        _pump(w, 1500)
        lo, hi, _g = w._display.state.mapping_or_seed("CD3")
        w._step0.set_display_mapping("CD3", lo, hi / 3.0)

        for step in (0, 1, 2, 3, 1, 0):
            _goto(w, step)
            assert w._display.intensity_policy()["editable"] is True, step
            if panel is not None:
                assert panel.isEnabled() is True, step
            # The numbers are the same ones in every step: one store, never
            # copied per step.
            assert w._display.state.mapping("CD3")[1] == pytest.approx(hi / 3.0)

        # ...and the one thing that DOES disable it is a running job.
        w._lock_ui()
        assert w._display.intensity_policy() == {
            "editable": False, "reason": "computation_lock"}
        if panel is not None:
            assert panel.isEnabled() is False
        w._unlock_ui()
        assert w._display.intensity_policy()["editable"] is True
    finally:
        w.close()


def _drag_frames(w, moves, settle=120):
    """Run `moves` and collect the RGB each one actually put on the panel."""
    frames = []
    handle = w._display.coordinator.frame_published.connect(
        lambda r: frames.append(np.array(r["rgb"], copy=True)))
    try:
        for move in moves:
            move()
            _pump(w, settle)
    finally:
        w._display.coordinator.frame_published.disconnect(handle)
    return frames


def test_every_step_publishes_intermediate_frames_for_min_max_and_gamma(app):
    """THE requirement, in all four steps and on all three controls.

    Driven through `set_display_mapping` -- the one public write that the
    Intensity window's own controls reach, and that now lands in Block01's
    shared state rather than in a per-step store. The frames counted are the
    ones the real `OverviewPanel` was given.
    """
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        # Step1 first, so the downstream steps have a spec to inherit.
        _goto(w, 1)
        report = {}
        for step in (0, 1, 2, 3):
            _goto(w, step)
            # A TICK IS PER STEP since the 2026-09-16 ruling, so the channel
            # is shown HERE rather than once at the start; the Min/Max/Gamma
            # under test stay shared, which is what this test is about.
            w._display.state.set_display_visible("CD3", True,
                                                 origin="contract-test")
            _pump(w)
            lo, hi, gamma = w._display.state.mapping_or_seed("CD3")
            counts = {}
            for name, moves in (
                ("min", [(lambda v=v: w._step0.set_display_mapping(
                    "CD3", lo + (hi - lo) * 0.05 * v, hi))
                    for v in range(1, 7)]),
                ("max", [(lambda v=v: w._step0.set_display_mapping(
                    "CD3", lo, hi - (hi - lo) * 0.07 * v))
                    for v in range(1, 7)]),
                ("gamma", [(lambda v=v: w._step0.set_display_mapping(
                    "CD3", lo, hi, 1.0 + 0.12 * v))
                    for v in range(1, 7)]),
            ):
                frames = _drag_frames(w, moves)
                distinct = len({f.tobytes() for f in frames})
                counts[name] = (len(frames), distinct)
                assert len(frames) >= 4, (step, name, len(frames))
                assert distinct >= 3, (step, name, distinct)
                # Back to where this control started, so the next one moves
                # only itself.
                w._step0.set_display_mapping("CD3", lo, hi, gamma)
                _pump(w)
            report[step] = counts
        print(f"[evidence] visible intermediate frames per step: {report}")
    finally:
        w.close()


def test_every_step_with_a_legal_weight_entry_follows_a_weight_drag(app):
    """Weight, through the one global entry, in every step that has one.

    Step1 owns weights in its channel panel; Step2 and Step3 have no weight
    UI and none is faked -- they receive the change through
    `Block01DisplayServices.set_render_weight`, which is the entry a
    downstream weight control would call if one were added. Step0's thumbnail
    is a single channel with no weight at all, so it is not part of this and
    says so rather than being given a fake one.
    """
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config.set_channel_visible("CD8", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        w.config._rows["CD8"].spin.setValue(1.0)
        w._step0._apply_channel_color("CD3", (1.0, 0.0, 0.0))
        w._step0._apply_channel_color("CD8", (0.0, 1.0, 0.0))
        _goto(w, 1)
        assert w._display.render_weight("CD3") == 1.0
        report = {}
        for step in (1, 2, 3):
            _goto(w, step)
            frames = _drag_frames(w, [
                (lambda v=v: w._display.set_render_weight("CD3", v))
                for v in (0.8, 0.6, 0.4, 0.2, 0.0)])
            assert len(frames) >= 4, (step, len(frames))
            reds = [_mean_rgb(f)[0] for f in frames]
            assert reds[0] > reds[-1], (step, reds)
            report[step] = (len(frames), len({f.tobytes() for f in frames}))
            w._display.set_render_weight("CD3", 1.0)
            _pump(w)
        print(f"[evidence] visible weight frames per step: {report}")
        # Step0's THUMBNAIL draws one channel through one window: there is no
        # weight in that picture's arithmetic, and inventing a Step0 weight
        # control would be a fake entry. The canonical weight still exists and
        # still reads the same there -- it is one number for the process --
        # it simply does not appear in what Step0 composes.
        _goto(w, 0)
        assert w._display.render_weight("CD3") == 1.0
        assert "weights" not in (w._step0.tissue_render_snapshot() or {})
    finally:
        w.close()


# ── C. the ownership really is out of Step0 ──────────────────────────────

def test_the_window_and_mapping_owners_are_not_the_step0_page(app):
    from block01.ui.step0.step0_page import Step0Page
    w = _window(app)
    try:
        services = w._display
        assert services.window_owner() is services
        assert not isinstance(services.window_owner(), Step0Page)
        assert services.mapping_owner() is services.state
        assert not isinstance(services.mapping_owner(), Step0Page)
        # A write settles in the STORE, and the store is what everyone reads.
        services.state.set_mapping("CD3", 7.0, 77.0, 1.5, origin="test")
        assert services.state.mapping("CD3") == (7.0, 77.0, 1.5)
        assert w._step0.display_window("CD3") == (7.0, 77.0, 1.5)

        # THE ONE THAT SETTLES OWNERSHIP. With persistence disconnected, a
        # written window exists ONLY in the shared state -- Step0's remap
        # params have never heard of it. The state must still answer it. A
        # state that answered by calling a Step0 getter (which is what the
        # previous cut did, through a lambda) gives Step0's number here, not
        # this one.
        services.state._persist_port = None
        services.state.set_mapping("CD8", 3.0, 33.0, 1.25, origin="test")
        assert services.state.mapping("CD8") == (3.0, 33.0, 1.25)
        assert w._step0.display_window("CD8") == (3.0, 33.0, 1.25)
        assert w._step0._display_mapping_for("CD8") != (3.0, 33.0, 1.25), (
            "the test is not proving anything: Step0's own answer already "
            "matches, so a state delegating to it would look correct")
        # ...and it keeps answering with the seed port gone too.
        services.state._seed_port = None
        assert services.state.mapping("CD8") == (3.0, 33.0, 1.25)
    finally:
        w.close()


def test_the_two_windows_are_constructed_and_held_by_the_services(app):
    w = _window(app)
    try:
        popup = w._display.show_navigator()
        intensity = w._display.show_intensity("CD3")
        assert w._display.navigator() is popup
        assert w._display.intensity_window() is intensity
        # The page reads them through; it cannot replace them.
        assert w._step0._tissue_navigator_popup is popup
        assert w._step0._intensity_window is intensity
        for attr in ("_tissue_navigator_popup", "_intensity_window",
                     "_intensity_panel"):
            with pytest.raises(AttributeError):
                setattr(w._step0, attr, None)
    finally:
        w.close()


def test_no_step_reaches_into_step0_for_the_shared_windows():
    """A grep, deliberately: this is a rule about call sites, and the way it
    came back last time was one call site nobody looked at."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    banned = ("_step0.show_tissue_navigator", "_step0.show_intensity_window",
              "_step0.focus_intensity_on", "_step0._ensure_tissue_navigator",
              "_step0._ensure_intensity_window",
              "_step0._update_tissue_preview",
              "_step0.set_intensity_editing_enabled",
              "_step0.set_navigator_edit_policy")
    offenders = []
    for path in (root / "ui").rglob("*.py"):
        if path.name == "step0_page.py":
            continue
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            code = line.split("#", 1)[0]
            for needle in banned:
                if needle in code:
                    offenders.append(f"{path.name}: {line.strip()}")
    assert not offenders, offenders


def test_the_shared_windows_survive_step0_being_destroyed(app):
    """Really destroyed: removed, deregistered, deleted, and weakref-checked.

    The previous version of this test claimed Step0 had been let go and only
    set the low-resolution source to None -- a name that asserted a lifecycle
    fact it never established. This one takes the page out of the stack, has
    it hand its ports back, deletes the C++ object and runs the event loop,
    then checks a weak reference. What must survive is Block01's: both shared
    windows, the canonical colours and windows, and a downstream context that
    fails CLOSED rather than calling into something that is gone.
    """
    import gc
    import weakref
    import sip

    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        popup = w._display.show_navigator()
        intensity = w._display.show_intensity("CD3")
        _goto(w, 1)
        _goto(w, 2)
        w._display.state.set_mapping("CD3", 4.0, 44.0, 1.1, origin="test")

        page = w._step0
        ref = weakref.ref(page)
        page.release_block01_display()
        w._stack.removeWidget(page)
        page.setParent(None)
        w._step0 = None
        sip.delete(page)
        del page
        QtWidgets.QApplication.processEvents()
        gc.collect()
        QtWidgets.QApplication.processEvents()

        assert ref() is None or sip.isdeleted(ref()), "Step0 was not destroyed"
        # Block01's own, all still here and all still answering.
        assert w._display.navigator() is popup
        assert w._display.intensity_window() is intensity
        assert w._display.state.mapping("CD3") == (4.0, 44.0, 1.1)
        assert w._display.state.color("CD3")
        # ...and the downstream context fails closed rather than reaching
        # into the page that is gone.
        assert w._downstream_contexts[bd.STEP2].tissue_render_snapshot() is None
        assert w._display.coordinator.request_frame(kind="after_destroy")
        QtWidgets.QApplication.processEvents()
    finally:
        w._step0 = None
        w._display.shutdown("test over")


def test_the_real_panel_publish_cost_is_measured_not_assumed(app):
    """What a publish actually costs on the REAL panel.

    The scheduler microbenchmark uses a stand-in panel and says so; this is
    the one that goes through `OverviewPanel.set_channel_image` and the
    pyqtgraph item behind it. Reported rather than tightly bounded -- the
    number depends on the machine -- but bounded loosely enough that a
    publish doing whole-slide work again would fail it.
    """
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        _goto(w, 1)
        panel = w._step0.overview
        real_setter = panel.set_channel_image
        costs = []

        def timed(rgb, token=None):
            t0 = time.perf_counter()
            try:
                return real_setter(rgb, token)
            finally:
                costs.append((time.perf_counter() - t0) * 1000.0)

        panel.set_channel_image = timed
        lo, hi, _g = w._display.state.mapping_or_seed("CD3")
        for i in range(1, 13):
            w._step0.set_display_mapping("CD3", lo, hi - (hi - lo) * 0.05 * i)
            _pump(w, 120)
        panel.set_channel_image = real_setter

        assert len(costs) >= 6, costs
        worst = max(costs)
        print(f"[evidence] real OverviewPanel.set_channel_image over "
              f"{len(costs)} publishes: worst {worst:.2f} ms, "
              f"median {sorted(costs)[len(costs) // 2]:.2f} ms")
        assert worst < 50.0, f"a publish took {worst:.1f} ms on the GUI thread"
    finally:
        w.close()


# ── P0-1. a display window is about PIXELS, so it is per dataset ─────────

def _switch_dataset(w, path, scale=1.0):
    """A dataset commit, as the page performs one: new loader, new token."""
    path = _ensure_source_file(path)
    loader = _Loader(path)
    loader._scale = scale
    base = loader._pattern

    def scaled(channel, h, wd, _base=base, _s=scale):
        return _base(channel, h, wd) * _s

    loader._pattern = scaled
    w.loader = loader
    page = w._step0
    page.loader = loader
    page.ome_path = path
    page._dataset_gen += 1
    page._slide_lowres = {}
    page._display_fallback = {}
    page._display_seeded = set()
    page._bind_panels_to_dataset()
    page.overview.loader = loader
    page.overview.full_h, page.overview.full_w = SLIDE_H, SLIDE_W
    _pump(w)
    return loader


def test_a_display_window_does_not_follow_a_channel_name_across_slides(app):
    """Slide B's CD3 is not slide A's CD3.

    Keyed by channel name alone, B's first read of CD3 hit A's window and B
    was never seeded at all: A's tissue contrast on B's picture, with nothing
    on screen to say so. Colours DO follow the name -- that is an existing
    product preference about display, not about pixels -- and this pins the
    difference.
    """
    w = _window(app, path="/tmp/A.ome.tiff")
    try:
        _goto(w, 0)
        a_token = w._display.state.dataset_token()
        w._step0.set_display_mapping("CD3", 11.0, 99.0, 1.4)
        w._step0._apply_channel_color("CD3", (1.0, 0.0, 0.0))
        assert w._display.state.mapping("CD3") == (11.0, 99.0, 1.4)

        # B: the same channel names, pixels an order of magnitude apart.
        _switch_dataset(w, "/tmp/B.ome.tiff", scale=17.0)
        b_token = w._display.state.dataset_token()
        assert b_token != a_token

        # Nothing of A's is readable as B's.
        assert w._display.state.mapping("CD3") != (11.0, 99.0, 1.4)
        _pump(w)
        seeded = w._display.state.mapping("CD3")
        assert seeded is not None, "B was never seeded"
        assert seeded[1] > 99.0, seeded      # B's own, brighter pixels
        # ...and the colour, which IS a per-name preference, did follow.
        assert w._display.state.color("CD3") == "#ff0000"

        # Back to A: A's namespace is intact, not re-seeded from B.
        _switch_back = w._step0
        _switch_back._dataset_gen -= 1
        _switch_back.ome_path = "/tmp/A.ome.tiff"
        w._display.state.bind_dataset(_switch_back._dataset_token())
        assert w._display.state.mapping("CD3") == (11.0, 99.0, 1.4)
    finally:
        w.close()


def test_a_seed_that_finishes_after_a_switch_is_filed_under_its_own_slide(app):
    w = _window(app, path="/tmp/A.ome.tiff")
    try:
        _goto(w, 0)
        a_binding = w._display.state.binding()
        state = w._display.state
        # A channel A has NO window for: the precondition a seed carries is
        # "this field was absent", and CD3 was seeded the moment Step0 drew.
        assert state.mapping("CD8") is None
        _switch_dataset(w, "/tmp/B.ome.tiff", scale=17.0)
        b_before = state.mapping("CD8")

        # A's seed, arriving now. It carries the BINDING it was started
        # under, which names both the slide and the visit.
        w._display._on_mapping_seeded(
            {"binding": a_binding, "channel": "CD8", "nucleus": False,
             "min": 11.0, "max": 99.0, "gamma": 1.0})

        assert state.mapping("CD8") == b_before, "A leaked into B"
        assert state.mapping_in(a_binding.identity, "CD8") == (11.0, 99.0, 1.0)
    finally:
        w.close()


def test_a_seed_is_refused_when_the_field_is_no_longer_absent(app):
    """A seed is started BECAUSE the window is absent. If it is not absent
    any more -- the user typed a number while the percentile was running --
    the automatic answer is stale and the user's stands."""
    w = _window(app, path="/tmp/A.ome.tiff")
    try:
        _goto(w, 0)
        state = w._display.state
        binding = state.binding()
        assert state.mapping("CD8") is None
        state.set_mapping("CD8", 3.0, 33.0, 1.0, origin="user")

        w._display._on_mapping_seeded(
            {"binding": binding, "channel": "CD8", "nucleus": False,
             "min": 11.0, "max": 99.0, "gamma": 1.0})

        assert state.mapping("CD8") == (3.0, 33.0, 1.0), "the seed overwrote"
    finally:
        w.close()


# ── P0-2. one weight, and it does not roll back ──────────────────────────

def test_a_weight_set_downstream_survives_every_later_step_change(app):
    """The failure this replaces: Step2 -> 0.3, walk on, and it was 1.0 again.

    Each downstream context kept its own writable copy of the spec and
    re-inherited Step1's older one on every transition, so a downstream edit
    was a temporary pixel effect. Nothing is reset in this test -- that is
    the point; the old tests reset the weight at the end of each step and
    hid exactly this.
    """
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config.set_fusion_enabled("CD3", True)   # in the science, too
        w.config._rows["CD3"].spin.setValue(1.0)
        _goto(w, 1)
        _goto(w, 2)

        assert w._display.set_render_weight("CD3", 0.3)
        _pump(w)
        step2_frame = np.array(_thumb(w), copy=True)

        for step in (3, 2, 1):
            _goto(w, step)
            assert w._display.render_weight("CD3") == pytest.approx(0.3), step
            assert w.config.channel_weight("CD3") == pytest.approx(0.3), step
            assert w._display.render_spec()["weights"]["CD3"] == pytest.approx(
                0.3), step
        # The Step1 row and the effective config agree, so a Save would
        # freeze what the user actually chose.
        effective = w._effective_fusion_config()
        weights = [chs.get("CD3") for chs in
                   (g.get("channels", {}) for g
                    in (effective.get("groups") or {}).values())
                   if "CD3" in chs]
        assert weights and all(v == pytest.approx(0.3) for v in weights)
        # ...and the picture is still the 0.3 one.
        _goto(w, 2)
        assert np.array_equal(_thumb(w), step2_frame)
    finally:
        w.close()


def test_a_weight_set_in_step3_reaches_step1_too(app):
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        _goto(w, 1)
        _goto(w, 3)
        assert w._display.set_render_weight("CD3", 0.45)
        _pump(w)
        _goto(w, 1)
        assert w.config.channel_weight("CD3") == pytest.approx(0.45)
        assert w._display.render_weight("CD3") == pytest.approx(0.45)
    finally:
        w.close()


# ── P0-3. real, reachable entries in every step ──────────────────────────

def test_the_global_windows_open_from_a_real_button_in_every_step(app):
    """Clicked, not called. The per-page buttons vanish with their page, so
    Step2 and Step3 had no way back to a closed window."""
    w = _window(app)
    try:
        for step in (0, 1, 2, 3):
            _goto(w, step)
            # Closed first, so this is really re-opening rather than finding
            # a window Step1 happened to leave up.
            if w._display.intensity_window() is not None:
                w._display.intensity_window().hide()
            if w._display.navigator() is not None:
                w._display.navigator().hide()

            # IN THE BLOCK01 CHROME, not merely constructed: the point is
            # that these outlive the page the user is on, so each must be a
            # descendant of the window and NOT of the stacked widget.
            # NO buttons in the chrome: all three that stood there were
            # added without a request. The windows are opened from the
            # pages' own entries, which is where they always were.
            assert not hasattr(w, "_btn_global_tissue")
            assert not hasattr(w, "_btn_global_intensity")
            assert not hasattr(w, "_btn_global_weights")
            w._step0.show_tissue_navigator()
            w._display.show_intensity(w.config.current_channel())

            assert w._display.intensity_window() is not None, step
            assert w._display.intensity_window().isVisible(), step
            assert w._display.navigator() is not None, step
            assert w._display.navigator().isVisible(), step
        # One instance each, across the whole walk.
        assert w._display.navigator() is w._step0._tissue_navigator_popup
    finally:
        w.close()


def test_a_real_weight_spinbox_drag_moves_the_picture_in_every_step(app):
    """The real control, in every step: the shared weight editor's spin box.

    Driven through its `valueChanged` signal exactly as a user's scroll or
    typed value does, not by calling the service.
    """
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config.set_channel_visible("CD8", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        w.config._rows["CD8"].spin.setValue(1.0)
        w._step0._apply_channel_color("CD3", (1.0, 0.0, 0.0))
        w._step0._apply_channel_color("CD8", (0.0, 1.0, 0.0))
        _goto(w, 1)
        # STEP1'S OWN ROW is the weight control. The shared `Weights…` window
        # was never asked for and is gone, so the drag happens where a user
        # actually does it -- and Step2/Step3 draw the published spec, which
        # is what the frames below are about.
        spin = w._channel_dock.row("CD3").spin
        assert spin is not None

        report = {}
        for step in (1,):
            _goto(w, step)
            frames = _drag_frames(w, [
                (lambda v=v: spin.setValue(v))
                for v in (0.8, 0.6, 0.4, 0.2, 0.05)])
            assert len(frames) >= 4, (step, len(frames))
            reds = [_mean_rgb(f)[0] for f in frames]
            assert reds[0] > reds[-1], (step, reds)
            report[step] = (len(frames), len({f.tobytes() for f in frames}))
            assert w.config.channel_weight("CD3") == pytest.approx(0.05), step
        print(f"[evidence] real weight-spinbox frames per step: {report}")
    finally:
        w.close()


def test_a_real_intensity_spinbox_drag_moves_the_picture_in_every_step(app):
    """The real Intensity control, in every step.

    The shared window hosts the Channel Remap inspector; this drives that
    inspector's own Min spin box, which is what a user drags.
    """
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        _goto(w, 1)
        w._display.show_intensity("CD3")
        wb = w._step0._cond_workbench
        wb.set_active_channel("CD3")
        # `_sp_max` is the inspector's real Max spin box -- the widget the
        # user types into or scrolls -- and `valueChanged` is what it emits.
        spin = wb._sp_max
        report = {}
        for step in (0, 1, 2, 3):
            _goto(w, step)
            lo, hi, _g = w._display.state.mapping_or_seed("CD3")
            frames = _drag_frames(w, [
                (lambda v=v: spin.setValue(hi - (hi - lo) * 0.07 * v))
                for v in range(1, 7)])
            assert len(frames) >= 4, (step, len(frames))
            assert len({f.tobytes() for f in frames}) >= 3, step
            report[step] = (len(frames), len({f.tobytes() for f in frames}))
        print(f"[evidence] real Intensity-spinbox frames per step: {report}")
    finally:
        w.close()


# ── P0-4. a channel still loading stays in the configuration ─────────────

def test_a_channel_still_loading_is_not_lost_by_walking_downstream(app):
    """The barrier case.

    Step1 wants CD3 and CD8; only CD3 has arrived when the user walks to
    Step2. The old spec recorded the RESIDENT channels, so CD8 fell out of
    the configuration for good -- never requested again, never in the final
    picture.
    """
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config.set_channel_visible("CD8", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        w.config._rows["CD8"].spin.setValue(1.0)
        w._step0._apply_channel_color("CD3", (1.0, 0.0, 0.0))
        w._step0._apply_channel_color("CD8", (0.0, 1.0, 0.0))
        # DAPI is ticked by default and carries the nucleus weight; its
        # palette default would put green in the mean this test reads, so it
        # is pinned to blue and green means CD8 and nothing else.
        w._step0._apply_nucleus_color((0.0, 0.0, 1.0))

        # The barrier: CD8 cannot be read yet.
        held = {"CD8"}
        real_array = w._step0.tissue_lowres_array

        def gated(channel):
            return None if channel in held else real_array(channel)

        w._step0.tissue_lowres_array = gated
        _goto(w, 1)
        partial = np.array(_thumb(w), copy=True)
        assert _mean_rgb(partial)[0] > 0.0      # CD3 is drawn
        assert _mean_rgb(partial)[1] == 0.0     # CD8 is not, yet
        assert "CD8" in (w._display.render_spec()["channels"])

        _goto(w, 2)
        assert "CD8" in (w._display.render_spec()["channels"])

        # Release it while the user is downstream.
        held.clear()
        w._display.coordinator.request_frame(kind="test")
        _pump(w)

        complete = _thumb(w)
        assert _mean_rgb(complete)[1] > 0.0, "CD8 never arrived in the picture"
        assert not np.array_equal(complete, partial)
    finally:
        w.close()


# ── P1-1. a refused close leaves everything alive ────────────────────────

def test_a_close_that_is_refused_does_not_kill_the_display_services(app):
    from PyQt5.QtGui import QCloseEvent
    w = _window(app)
    try:
        _goto(w, 1)
        popup = w._display.show_navigator()
        intensity = w._display.show_intensity("CD3")

        # Something the close must wait for: a loader that will not stop.
        class _StuckLoader:
            def isRunning(self):
                return True

            def stop(self):
                pass

            def wait(self, _ms=0):
                return False

        w._patch_loaders[99] = _StuckLoader()
        event = QCloseEvent()
        w.closeEvent(event)

        assert not event.isAccepted(), "the close was not refused"
        assert not w._display.is_finalized()
        assert w._display.navigator() is popup
        assert w._display.intensity_window() is intensity
        # ...and it can still draw.
        assert w._display.coordinator.request_frame(kind="after_refusal")
        _pump(w)
        assert w._display.coordinator.frame_stats()["published"] > 0

        # Release the blocker; the second close is accepted and finalises once.
        w._patch_loaders.clear()
        event2 = QCloseEvent()
        w.closeEvent(event2)
        assert w._display.is_finalized()
        assert not w._display.coordinator.request_frame(kind="after_close")
        # Idempotent: a repeated close neither restarts nor re-closes.
        w.closeEvent(QCloseEvent())
        assert w._display.is_finalized()
    finally:
        w.close()


# ── P1-3. the GUI thread does no pixel work ──────────────────────────────

def test_no_read_or_percentile_runs_inside_a_tissue_frame_callback(app):
    """The GUI callback that builds a tissue frame does no pixel work.

    Watched by THREAD and scoped to the callback: a first display window is a
    percentile over a whole-slide array and a missing array is a 170-230 ms
    read, and both used to happen inside the snapshot -- the one place whose
    entire job is to hand values over and return.

    DELIBERATELY NOT IN SCOPE: the Step1 PATCH viewer's own display-mapping
    path (`display_mapping_for_preview` -> `_auto_display_window`) still
    computes a window on the GUI thread. That is the third work item's
    accepted behaviour -- it is documented in `MainWindow._display_mapping`,
    the user has closed it, and this round does not reopen it. What is
    asserted here is the Tissue Preview's own callback, plus that the work
    really did happen on the two worker threads.
    """
    import threading
    from block01.core import display_mapping

    w = _window(app)
    try:
        gui = threading.get_ident()
        inside = {"frame": False}
        offenders = []
        real_seed = display_mapping.seed_display_range
        real_read = _Loader.read_region_lowres

        def watched_seed(arr):
            if inside["frame"] and threading.get_ident() == gui:
                offenders.append("percentile")
            return real_seed(arr)

        def watched_read(self, *a, **k):
            if inside["frame"] and threading.get_ident() == gui:
                offenders.append("read")
            return real_read(self, *a, **k)

        co = w._display.coordinator
        real_snapshot = co._snapshot

        def watched_snapshot(context, rev):
            inside["frame"] = True
            try:
                return real_snapshot(context, rev)
            finally:
                inside["frame"] = False

        co._snapshot = watched_snapshot
        display_mapping.seed_display_range = watched_seed
        _Loader.read_region_lowres = watched_read
        try:
            # A channel NOBODY has a window for, appearing while the watch is
            # on: this is the case that needs a percentile at all.
            w.config.set_channel_visible("CD3", True)
            w.config._rows["CD3"].spin.setValue(1.0)
            _goto(w, 1)
            _pump(w)
            assert w._display.state.mapping("CD3") is not None, (
                "CD3 was never seeded, so this test proves nothing")
            assert w._display._seed_worker is not None
            assert w._display._seed_worker.stats()["computed"] >= 1

            w.config.set_channel_visible("CD8", True)
            w.config._rows["CD8"].spin.setValue(1.0)
            _pump(w)
            assert w._display.state.mapping("CD8") is not None

            _goto(w, 2)
            w.config.set_channel_visible("DAPI", True)
            co.request_frame(kind="test")
            _pump(w)

            for step in (0, 1, 2, 3):
                _goto(w, step)
                lo, hi, _g = w._display.state.mapping_or_seed("CD3")
                for i in range(1, 6):
                    w._step0.set_display_mapping(
                        "CD3", lo, hi - (hi - lo) * 0.08 * i)
                    _pump(w, 120)

            assert offenders == [], offenders
            assert co.frame_stats()["gui_composed"] == 0
            print(f"[evidence] seed thread: "
                  f"{w._display._seed_worker.stats()}; read thread: "
                  f"{w._display._read_worker.stats() if w._display._read_worker else 'unused'}")
        finally:
            display_mapping.seed_display_range = real_seed
            _Loader.read_region_lowres = real_read
            co._snapshot = real_snapshot
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


# ── an atomic install must REACH the views, not just the store ───────────
#
# `install()` used to emit only a completion notice, and no production
# consumer subscribed to it. The display consumers -- Step0's swatch and its
# Channel Remap layer list, the compare panels, the full image, the Tissue
# Preview's frame clock, Step1's viewer -- listen for `color_changed` and
# `mapping_changed`. So a session restore moved the state's answer while
# every view went on drawing the previous one, and a colour-getter assertion
# could not see it, because the getter reads the state.

def test_a_restored_colour_reaches_the_real_views_not_just_the_store(app):
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        _goto(w, 1)
        _pump(w)
        page = w._step0
        # The Channel Remap layer list, fed from its real entry.
        page._sync_step0_to_workbench()
        workbench = page._cond_workbench
        frames_before = w._display.coordinator.frame_stats()["inputs"]

        # The real Step1 restore entry, as a session replay calls it.
        w.config.restore_display_state(colors={"CD3": "#00ccff"},
                                       visibility={"CD3": True},
                                       current_channel="CD3")
        _pump(w)

        state = w._display.state
        assert state.color("CD3") == "#00ccff"
        # ...and the REAL views, not the getter that reads the state:
        assert page._dock_adapter.dock.row("CD3").color() == "#00ccff", \
            "the public row never heard about the restore"
        swatch = page._dock_adapter.dock.row("CD3").swatch.styleSheet()
        assert "#00ccff" in swatch, f"the Step0 swatch still reads {swatch}"
        assert workbench._colors["CD3"].lower() == "#00ccff", \
            "the Channel Remap layer list never heard about the restore"
        assert w.config._rows["CD3"].color().lower() == "#00ccff"
        # ...and the Tissue Preview asked for a frame in the new colour.
        assert w._display.coordinator.frame_stats()["inputs"] > frames_before
        published = w._display.coordinator.last_published()
        assert published.get("color_rev") == state.color_revision()
    finally:
        w.close()


def test_an_atomic_mapping_install_redraws_the_viewers(app):
    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        _goto(w, 1)
        _pump(w)
        state = w._display.state
        seen = []
        state.mapping_changed.connect(seen.append)
        before = np.array(_thumb(w), copy=True)
        lo, hi, _g = state.mapping_or_seed("CD3")

        state.install({"mappings": {"CD3": (lo, hi / 4.0, 1.0)}})
        _pump(w)

        assert seen == ["CD3"], seen
        assert state.mapping("CD3") == (lo, hi / 4.0, 1.0)
        after = _thumb(w)
        assert not np.array_equal(after, before), \
            "the Tissue Preview kept the window the restore replaced"
    finally:
        w.close()


def test_an_install_callback_sees_the_whole_transaction(app):
    """Every compatibility signal is emitted with the transaction already
    written, so a handler can read any field and get the final answer."""
    w = _window(app)
    try:
        _goto(w, 1)
        state = w._display.state
        lo, hi, _g = state.mapping_or_seed("CD3")
        seen = []

        def look(*_a):
            seen.append((state.selected_channel(),
                         state.display_visible("CD8"),
                         state.color("CD3"),
                         state.mapping("CD3")))

        state.state_installed.connect(look)
        state.color_changed.connect(look)
        state.mapping_changed.connect(look)

        state.install({"selection": "CD8",
                       "visibility": {"CD8": True},
                       "colors": {"CD3": "#ff00aa"},
                       "mappings": {"CD3": (lo, hi / 3.0, 1.1)}})

        whole = ("CD8", True, "#ff00aa", (lo, hi / 3.0, 1.1))
        assert seen and all(s == whole for s in seen), seen
        # the completion notice comes first, then the field fan-out
        assert len(seen) == 3
    finally:
        w.close()


def test_a_restore_still_fires_no_user_or_scientific_signal(app):
    w = _window(app)
    try:
        _goto(w, 1)
        forbidden = []
        w.config.visibility_changed.connect(
            lambda *a: forbidden.append(("visibility",) + a))
        w.config.config_changed.connect(lambda: forbidden.append(("config",)))
        w._display.state.visibility_changed.connect(
            lambda *a: forbidden.append(("state-visibility",) + a))
        w._display.state.selection_changed.connect(
            lambda c: forbidden.append(("state-selection", c)))
        weights = {ch: w.config.channel_weight(ch)
                   for ch in ("DAPI", "CD3", "CD8")}
        dirty_before = w._fusion_settings_dirty()

        w.config.restore_display_state(colors={"CD3": "#00ccff"},
                                       visibility={"CD3": True},
                                       current_channel="CD3")
        _pump(w)

        assert forbidden == [], forbidden
        assert {ch: w.config.channel_weight(ch)
                for ch in ("DAPI", "CD3", "CD8")} == weights
        assert all(w.config.weight_provenance(ch) != "explicit"
                   for ch in ("DAPI", "CD3", "CD8"))
        assert w._fusion_settings_dirty() == dirty_before
    finally:
        w.close()


def test_repeating_a_restore_announces_nothing(app):
    w = _window(app)
    try:
        _goto(w, 1)
        payload = dict(colors={"CD3": "#00ccff"}, visibility={"CD3": True},
                       current_channel="CD3")
        w.config.restore_display_state(**payload)
        _pump(w)
        seen = []
        state = w._display.state
        state.state_installed.connect(lambda b: seen.append("installed"))
        state.color_changed.connect(lambda *a: seen.append("color"))
        state.mapping_changed.connect(lambda *a: seen.append("mapping"))

        w.config.restore_display_state(**payload)

        assert seen == [], seen
    finally:
        w.close()


# ── the fusion picture actually carries the ticked markers ──────────────────

def test_a_ticked_marker_reaches_the_fusion_picture_not_just_the_config(app):
    """PIXELS, not only `effective_config()`.

    The report was "the fusion is only DAPI". What closes it is a picture
    that changes when a marker is ticked and changes back when it is
    unticked -- in the shared Tissue Preview, through the real tick box.
    """
    w = _window(app)
    try:
        _goto(w, 1)
        w.set_preview_mode(STEP1_PREVIEW_FUSION, force=True)
        state = w._display.state
        # DAPI alone, in blue: the picture the user reported
        w._step0._apply_nucleus_color((0.0, 0.0, 1.0))
        w._step0._apply_channel_color("CD3", (1.0, 0.0, 0.0))
        for ch in ("CD3", "CD8"):
            state.set_display_visible(ch, False, origin="test")
            w._display.fusion.set_fusion_enabled(ch, False, origin="test")
        _pump(w, 400)
        dapi_only = np.array(_thumb(w), copy=True)
        assert dapi_only is not None

        # THE REAL TICK, on a marker
        frames = _drag_frames(w, [
            lambda: w._channel_dock.row("CD3").checkbox.setChecked(True)],
            settle=400)

        assert w._display.fusion.fusion_enabled("CD3") is True
        assert w._display.fusion.channel_weight("CD3") == pytest.approx(1.0)
        with_marker = np.array(_thumb(w), copy=True)
        assert with_marker.shape == dapi_only.shape
        assert not np.array_equal(with_marker, dapi_only), \
            "ticking a marker did not change the fused picture"
        # the marker's own colour is in the picture now
        assert float(with_marker[..., 0].mean()) > \
            float(dapi_only[..., 0].mean()), \
            "the marker's red never reached the fusion"
        assert frames, "no frame was published for the tick"

        # ...and unticking takes it back out
        w._channel_dock.row("CD3").checkbox.setChecked(False)
        _pump(w, 400)
        back = np.array(_thumb(w), copy=True)
        assert float(back[..., 0].mean()) < float(with_marker[..., 0].mean())
    finally:
        w.close()


def test_two_ticked_markers_both_reach_the_fusion_picture(app):
    w = _window(app)
    try:
        _goto(w, 1)
        w.set_preview_mode(STEP1_PREVIEW_FUSION, force=True)
        w._step0._apply_channel_color("CD3", (1.0, 0.0, 0.0))
        w._step0._apply_channel_color("CD8", (0.0, 1.0, 0.0))
        for ch in ("CD3", "CD8"):
            w._display.state.set_display_visible(ch, False, origin="test")
            w._display.fusion.set_fusion_enabled(ch, False, origin="test")
        _pump(w, 300)

        w._channel_dock.row("CD3").checkbox.setChecked(True)
        _pump(w, 300)
        one = np.array(_thumb(w), copy=True)
        w._channel_dock.row("CD8").checkbox.setChecked(True)
        _pump(w, 400)
        two = np.array(_thumb(w), copy=True)

        # The SECOND marker changes the picture again -- one ticked channel
        # is not the whole fusion. (Which colour channel moves is the
        # palette's business; that the picture moves at all is this test's.)
        assert not np.array_equal(one, two), \
            "the second marker never reached the fused picture"
        assert float(np.abs(two.astype(int) - one.astype(int)).mean()) > 0.0
        used = set()
        for data in (w._display.fusion.effective_config().get("groups")
                     or {}).values():
            used.update((data.get("channels") or {}).keys())
        assert {"CD3", "CD8"} <= used
    finally:
        w.close()
