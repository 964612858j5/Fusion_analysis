"""The shared Intensity window and the first Tissue Preview frame.

Four regressions found in manual acceptance, all of them in the lifecycle
AROUND the shared display objects rather than in what they compute:

* Reset in the Intensity inspector moved the widget and nothing else. Auto
  worked, because Auto ends in the one publishing path (`_collect_params_
  from_controls` -> `params_changed` -> `ChannelDisplayState`) and Reset
  stopped at its own `_refresh_preview()`. So the main viewer and the Tissue
  Preview went on drawing the window the user had just reset away.

* Entering Step1 opened one tiny nameless window per channel. They were the
  rows' `f` participation boxes: built, styled and connected, but never added
  to the row's layout, so they had no parent -- and `setVisible(True)` on a
  parentless QWidget is Qt's definition of a new top-level window.

* Selecting a channel Step0 had never drawn read its whole slide ON THE GUI
  THREAD, from the workbench's pixel provider. That is 170-230 ms of frozen
  interface per channel at best, and on a slide whose read is slow or fails,
  no histogram at all -- while the same channel was fine in Step0, which had
  already read it.

* The first Tissue Preview frame has to arrive with no ROI and no patch, from
  arrivals alone: a low-res array, then a display-window seed.

Real objects throughout: a real MainWindow, the real public dock, the real
Intensity inspector, the real navigator popup and the real step transitions.
The loader is synthetic but faithful -- many channels, a whole-slide
low-resolution read at a real downsample, nothing pre-loaded, and a barrier
the test can hold reads at.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import os
import pathlib
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets  # noqa: E402

SLIDE_H, SLIDE_W = 2048, 1024
LOW_H, LOW_W = 64, 32
CHANNELS = ["DAPI", "CD3", "CD8", "CD20", "CD68", "Ki67", "PanCK",
            "FoxP3", "CD45", "CD31"]
#: every marker -- the ones a fresh page has never drawn
COLD = [c for c in CHANNELS if c not in ("DAPI", "CD3")]


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _SlideLoader:
    """A slide with many channels and a whole-slide low-resolution read.

    NOTHING is pre-loaded: every channel starts cold, which is the state the
    "most channels have no signal" report is about. `hold` blocks the reads
    so a test can see what the interface does WHILE one is outstanding; it
    releases itself after two seconds so a regression cannot hang the suite.
    """

    shape = (SLIDE_H, SLIDE_W)

    def __init__(self, path):
        p = pathlib.Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_bytes(b"synthetic slide")
        self.filepath = str(p)
        self._names = list(CHANNELS)
        self.ch_map = {c: i for i, c in enumerate(self._names)}
        self.reads, self.lowres_reads, self.blocked = [], [], []
        self.hold = False

    def channel_names(self):
        return list(self._names)

    def overview_downsample(self):
        return SLIDE_H // LOW_H

    def _pattern(self, channel, h, w):
        i = self.ch_map.get(channel, 0)
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        base = (yy * (i + 1) + xx * (10 - i)) / float(h + w)
        return (base * 200.0 + 10.0 * (i + 1)).astype(np.float32)

    def read_region(self, channel, y0, y1, x0, x1, downsample=1,
                    normalize=True):
        self.reads.append(channel)
        ds = max(1, int(downsample))
        return self._pattern(channel, (y1 - y0) // ds or 1,
                             (x1 - x0) // ds or 1)

    def read_region_lowres(self, channel, y0, y1, x0, x1, downsample,
                           normalize=False):
        end = time.monotonic() + 2.0
        waited = 0.0
        while self.hold and time.monotonic() < end:
            time.sleep(0.005)
            waited += 0.005
        if waited:
            self.blocked.append((channel, round(waited, 3)))
        self.lowres_reads.append(channel)
        ds = max(1, int(downsample))
        return self._pattern(channel, (y1 - y0) // ds or 1,
                             (x1 - x0) // ds or 1)


def _window(app, path="/tmp/b8_lifecycle/a.ome.tiff", show=True):
    from block01.ui.main_window import MainWindow
    w = MainWindow()
    if show:
        w.resize(1500, 950)
        w.show()
    w._set_step_active(0)                    # the user is in Step0 already
    QtWidgets.QApplication.processEvents()
    loader = _SlideLoader(path)
    w.loader = loader
    page = w._step0
    page.loader = loader
    page.ome_path = loader.filepath
    page.nucleus_channel = "DAPI"
    page.patches = []                        # NO patch and NO ROI, ever
    page._rebuild_channel_list()
    page.current_channel = "CD3"
    page.overview.loader = loader
    page.overview.full_h, page.overview.full_w = SLIDE_H, SLIDE_W
    page._bind_panels_to_dataset()
    w.config.set_channels(loader.channel_names())
    w.config.load_panel({"markers": {c: 0.0 for c in CHANNELS[1:]}}, "DAPI")
    w.config.set_nucleus("DAPI", 1.0)
    QtWidgets.QApplication.processEvents()
    return w, loader


def _close(w):
    w.hide()
    w._display.shutdown("test")
    w.close()


def _pump(ms=600):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.005)


def _preview_rgb(w):
    """The pixels the ONE Tissue Preview publishes right now.

    Through the coordinator's own synchronous path, so these are the frame's
    real pixels -- the active context, the shared mapping and the shared
    colour -- rather than a picture assembled by the test.
    """
    frame = w._display.coordinator.render_now()
    if not isinstance(frame, dict):
        return None
    rgb = frame.get("rgb")
    return None if rgb is None else np.asarray(rgb).copy()


def _tops():
    return [(type(t).__name__, t.objectName(), t.windowTitle(), t.isVisible())
            for t in QtWidgets.QApplication.topLevelWidgets()]


# ── 1. Reset is an edit, and an edit is published ───────────────────────────

def _workbench(w, channel):
    """The REAL inspector, pointed at `channel` the way a step points it.

    Waits for the pixels: the inspector's provider is resident-only, so a
    channel's array arrives from a worker rather than from inside the open.
    """
    w._display.show_intensity(channel)
    wb = w._step0._cond_workbench
    end = time.monotonic() + 3.0
    while time.monotonic() < end:
        QtWidgets.QApplication.processEvents()
        if wb._active == channel and wb._raw.get(channel) is not None:
            break
        time.sleep(0.005)
    _pump(150)
    return wb


def test_reset_publishes_the_window_it_resets_to(app):
    w, loader = _window(app)
    try:
        state = w._display.state
        # the channel whose window is about to move has to be ON SCREEN for
        # the picture to be able to answer at all
        state.set_display_visible("CD3", True, origin="test")
        wb = _workbench(w, "CD3")
        assert wb._active == "CD3"
        # A window nobody would arrive at by accident, set through the REAL
        # controls, so "did anything change?" is a real question.
        wb._sp_min.setValue(7.0)
        wb._sp_max.setValue(33.0)
        _pump(200)
        assert state.mapping("CD3") == (7.0, 33.0, 1.0)
        before_rgb = _preview_rgb(w)

        seen_map, seen_params = [], []
        state.mapping_changed.connect(seen_map.append)
        wb.params_changed.connect(seen_params.append)
        wb._btn_reset.click()
        _pump(400)

        after = state.mapping("CD3")
        assert after != (7.0, 33.0, 1.0), "Reset published nothing"
        assert after == (float(wb._sp_min.value()), float(wb._sp_max.value()),
                         pytest.approx(wb._sl_gamma.value() / 100.0))
        # EXACTLY ONCE, like every other edit
        assert seen_params == ["CD3"], seen_params
        assert seen_map == ["CD3"], seen_map
        # ...and the PIXELS really moved: a Reset that only moved the
        # spinboxes is exactly the bug this test is about.
        after_rgb = _preview_rgb(w)
        if before_rgb is not None and after_rgb is not None:
            assert after_rgb.shape == before_rgb.shape
            assert not np.array_equal(after_rgb, before_rgb), \
                "the Tissue Preview kept drawing the old window"
    finally:
        _close(w)


def test_reset_touches_nothing_but_the_mapping(app):
    w, loader = _window(app)
    try:
        state, fusion = w._display.state, w._display.fusion
        wb = _workbench(w, "CD3")
        wb._sp_min.setValue(7.0)
        wb._sp_max.setValue(33.0)
        _pump(200)
        page = w._step0
        before = (dict(state.display_visibility()), state.selected_channel(),
                  fusion.draft_snapshot(), dict(page._channel_decisions),
                  dict(page._channel_methods), len(loader.reads))

        wb._btn_reset.click()
        _pump(400)

        assert (dict(state.display_visibility()), state.selected_channel(),
                fusion.draft_snapshot(), dict(page._channel_decisions),
                dict(page._channel_methods), len(loader.reads)) == before
    finally:
        _close(w)


def test_reset_means_the_same_thing_in_step0_and_step1(app):
    """ONE Intensity widget, so the two steps cannot disagree."""
    w, loader = _window(app)
    try:
        state = w._display.state
        w._set_step_active(0)
        wb0 = _workbench(w, "CD3")
        wb0._sp_min.setValue(9.0)
        wb0._sp_max.setValue(44.0)
        _pump(200)
        wb0._btn_reset.click()
        _pump(300)
        from_step0 = state.mapping("CD3")

        w._set_step_active(1)
        _pump(200)
        wb1 = _workbench(w, "CD3")
        assert wb1 is wb0                      # the same widget, both steps
        wb1._sp_min.setValue(9.0)
        wb1._sp_max.setValue(44.0)
        _pump(200)
        wb1._btn_reset.click()
        _pump(300)

        assert state.mapping("CD3") == from_step0
    finally:
        _close(w)


# ── 2. no stray top-level windows ───────────────────────────────────────────

STEP_WALK = (0, 1, 0, 1, 2, 3, 1)


def test_the_step_walk_opens_no_window_of_its_own(app, capsys):
    """The report: entering Step1 opened a handful of tiny nameless windows.

    They were the rows' `f` boxes, parentless and shown. What is pinned is
    that a step change adds no top-level widget at all -- and the rows' own
    controls are never top-level, whatever step is on screen.
    """
    w, loader = _window(app)
    try:
        dock = w._channel_dock
        capsys.readouterr()
        before = _tops()
        for step in STEP_WALK:
            w._set_step_active(step)
            _pump(150)
            new = [t for t in _tops() if t not in before]
            assert [t for t in new if t[3]] == [], (step, new)
            # the row controls stay inside their row
            for cid in dock.channel_order():
                row = dock.row(cid)
                for name in ("checkbox", "fusion_box", "slider", "spin",
                             "method_cb", "swatch"):
                    widget = getattr(row, name)
                    assert widget.parentWidget() is not None, (step, cid, name)
                    assert not widget.isWindow(), (step, cid, name)
            # one dock, one Intensity, one navigator
            assert w._display.channel_dock() is dock
        noise = capsys.readouterr()
        for text in (noise.out, noise.err):
            assert "has been deleted" not in text, text
            assert "QThread: Destroyed" not in text, text
    finally:
        _close(w)


def test_every_row_control_is_laid_out_by_the_row(app):
    """The structural half: a control that is in no layout has no parent, and
    a parentless control that is shown IS a window."""
    w, loader = _window(app)
    try:
        dock = w._channel_dock
        row = dock.row("CD3")
        laid_out = set()
        lay = row._lay
        for i in range(lay.count()):
            item = lay.itemAt(i)
            if item.widget() is not None:
                laid_out.add(id(item.widget()))
        for name in ("checkbox", "state_slot", "swatch", "name_label",
                     "slider", "spin", "fusion_box", "method_cb"):
            widget = getattr(row, name)
            assert id(widget) in laid_out or widget.parentWidget() is not None, \
                name
    finally:
        _close(w)


# ── 3. any channel, in any step, without a patch ────────────────────────────

def test_a_cold_channel_selected_in_step1_never_reads_on_the_gui_thread(app):
    """The inspector switches AT ONCE, empty, and the read happens behind it.

    The provider used to read the whole slide inline: with the loader's reads
    held, the GUI thread simply stopped there.
    """
    w, loader = _window(app)
    try:
        w._set_step_active(1)
        wb = _workbench(w, "CD3")
        _pump(300)
        loader.hold = True
        started = time.monotonic()
        w._display.state.set_selected_channel("Ki67", origin="test")
        _pump(250)
        blocked_ms = int((time.monotonic() - started) * 1000)

        # the switch happened, the pixels have not
        assert wb._active == "Ki67"
        assert wb._raw.get("Ki67") is None, "the read was taken inline"
        assert blocked_ms < 1200, blocked_ms
        # ...and no previous channel's histogram is being passed off as this
        # channel's
        assert wb._histogram is not None

        loader.hold = False
        _pump(1200)
        assert wb._raw.get("Ki67") is not None, "the array never arrived"
        assert wb._raw["Ki67"].shape == (LOW_H, LOW_W)
        # ...and it is a real window over real pixels, not a placeholder
        window = wb._params["Ki67"]
        assert window["max"] > window["min"]
    finally:
        loader.hold = False
        _close(w)


def test_eight_cold_channels_all_end_up_with_real_pixels(app):
    """One by one through the REAL public row, with no patch ever drawn."""
    w, loader = _window(app)
    try:
        w._set_step_active(1)
        wb = _workbench(w, "CD3")
        page = w._step0
        assert page.patches == []
        results = {}
        for ch in COLD:
            w._display.state.set_selected_channel(ch, origin="test")
            _pump(500)
            arr = wb._raw.get(ch)
            window = wb._params.get(ch) or {}
            results[ch] = (arr is not None and arr.shape == (LOW_H, LOW_W)
                           and float(np.ptp(arr)) > 0.0
                           and float(window.get("max", 0.0))
                           > float(window.get("min", 0.0))
                           and wb._histogram is not None)
        assert len(results) >= 8
        assert all(results.values()), results
        assert page.patches == []
    finally:
        _close(w)


def test_a_late_array_never_lands_on_the_channel_that_is_showing(app):
    """A's array, arriving while the user is on B."""
    w, loader = _window(app)
    try:
        w._set_step_active(1)
        wb = _workbench(w, "CD3")
        w._display.state.set_selected_channel("CD20", origin="test")
        _pump(500)
        b_pixels = wb._raw.get("CD20")
        assert b_pixels is not None

        # CD68's array, delivered now -- the user is on CD20
        late = loader._pattern("CD68", LOW_H, LOW_W)
        wb.deliver_pixels("CD68", late)

        assert wb._active == "CD20"
        assert np.array_equal(wb._raw["CD20"], b_pixels)
        assert wb._raw.get("CD68") is not None      # filed, not drawn
    finally:
        _close(w)


def test_an_array_from_the_previous_slide_is_refused(app):
    w, loader = _window(app)
    try:
        display = w._display
        display.show_intensity("CD3")
        _pump(300)
        page = w._step0
        stale_token = ("not-this-slide", "/tmp/old.ome.tiff")
        display._on_lowres_read({"channel": "CD8", "token": stale_token,
                                 "array": np.ones((LOW_H, LOW_W), np.float32)})
        _pump(200)

        assert "CD8" not in getattr(page, "_slide_lowres", {})
        assert page._cond_workbench._raw.get("CD8") is None
    finally:
        _close(w)


def test_an_arrival_wakes_the_inspector_and_asks_for_a_frame(app):
    """One arrival, two consumers: the store AND the window waiting on it."""
    w, loader = _window(app)
    try:
        display = w._display
        page = w._step0
        wb = _workbench(w, "CD3")
        w._display.state.set_selected_channel("PanCK", origin="test")
        _pump(100)
        wb._raw["PanCK"] = None
        frames = []
        real_request = display.coordinator.request_frame
        display.coordinator.request_frame = \
            lambda **kw: (frames.append(kw), real_request(**kw))[1]
        try:
            array = loader._pattern("PanCK", LOW_H, LOW_W)
            display._on_lowres_read({"channel": "PanCK",
                                     "token": display._lowres_token(),
                                     "array": array})
            _pump(200)
        finally:
            display.coordinator.request_frame = real_request

        assert page._slide_lowres["PanCK"][1] is array
        assert wb._raw.get("PanCK") is not None
        assert [f for f in frames if f.get("kind") == "lowres"], frames
    finally:
        _close(w)


# ── 4. the first frame, with no ROI and no patch ────────────────────────────

def test_the_first_tissue_frame_arrives_with_no_patch(app):
    """Load, open the window, wait: a picture, from arrivals alone."""
    w, loader = _window(app, path="/tmp/b8_lifecycle/first.ome.tiff")
    try:
        page = w._step0
        assert page.patches == []
        # the very first snapshot may be empty: nothing is resident yet
        first = page.tissue_render_snapshot()
        w._display.show_navigator(step_id=0)
        _pump(1500)

        img = page.overview.img_item.image
        assert img is not None, (first, loader.lowres_reads)
        arr = np.asarray(img)
        assert arr.ndim == 3 and arr.shape[2] == 3
        assert float(arr.max()) > 0.0, "the first frame is blank"
        assert page.patches == []              # no patch was ever needed
        assert w._display.coordinator.active_context_id() is not None
    finally:
        _close(w)


def test_the_first_frame_waits_for_the_read_and_then_appears(app):
    """The read is held when the window opens; the picture follows the
    ARRIVAL -- no second click, no step change, no patch."""
    w, loader = _window(app, path="/tmp/b8_lifecycle/held.ome.tiff")
    try:
        page = w._step0
        state = w._display.state
        # a channel this page has never read, made the one the preview draws
        page.current_channel = "CD68"
        state.set_display_visible("CD68", True, origin="test")
        page._slide_lowres.pop("CD68", None)
        loader.hold = True
        w._display.show_navigator(step_id=0)
        _pump(300)
        # nothing of this channel is drawable yet
        assert "CD68" not in page._slide_lowres
        held = len(loader.blocked)

        loader.hold = False
        _pump(1800)

        assert "CD68" in page._slide_lowres, "the read never completed"
        assert len(loader.blocked) >= held
        img = page.overview.img_item.image
        assert img is not None and float(np.asarray(img).max()) > 0.0
        assert page.patches == []
    finally:
        loader.hold = False
        _close(w)


def test_a_preview_closed_before_the_array_lands_still_draws_when_reopened(app):
    w, loader = _window(app, path="/tmp/b8_lifecycle/reopen.ome.tiff")
    try:
        page = w._step0
        page._slide_lowres = {}
        loader.hold = True
        w._display.show_navigator(step_id=0)
        _pump(200)
        popup = w._display.ensure_navigator()
        popup.hide()
        loader.hold = False
        _pump(800)

        w._display.show_navigator(step_id=0)
        _pump(1200)
        img = page.overview.img_item.image
        assert img is not None and float(np.asarray(img).max()) > 0.0
        assert page.patches == []
    finally:
        loader.hold = False
        _close(w)


def test_a_hidden_marker_draws_nothing_until_it_is_shown(app):
    """An empty picture is a legitimate answer -- and showing the channel is
    enough to fill it, with no patch and no step change."""
    w, loader = _window(app, path="/tmp/b8_lifecycle/hidden.ome.tiff")
    try:
        page = w._step0
        state = w._display.state
        page.overview.img_item.setImage(np.zeros((4, 4, 3), np.uint8))
        # a marker with a real weight: a channel weighted 0 draws black
        # whatever its visibility says, and that is the fusion model's
        # answer rather than this test's subject.
        w._display.fusion.edit_channel_weight("CD3", 1.0, origin="test")
        state.set_display_visible("DAPI", False, origin="test")
        state.set_display_visible("CD3", False, origin="test")
        w._display.show_navigator(step_id=0)
        _pump(900)
        # nothing is on screen, so there is nothing to draw: the snapshot
        # says so rather than inventing a picture
        snap = page.tissue_render_snapshot(computed_only=False)
        assert snap is None or snap["marker_visible"] is False

        state.set_display_visible("CD3", True, origin="test")
        _pump(1500)

        snap = page.tissue_render_snapshot(computed_only=False)
        assert snap is not None and snap["marker_visible"] is True
        # DRAWABLE, with no patch and no step change: the array is resident
        # and the window is known, which is everything a frame is made of.
        assert snap["arrays"].get("CD3") is not None
        assert snap["mappings"].get("CD3") is not None
        from block01.core import tissue_compose
        rgb = np.asarray(tissue_compose.compose(snap, None))
        assert float(rgb.max()) > 0.0
        assert page.patches == []
    finally:
        _close(w)


# ── the clock keeps ticking, and every arrival is an input ──────────────────

def _frame_spy(display):
    """Record what the coordinator is asked to draw, without stopping it."""
    seen = []
    real = display.coordinator.request_frame

    def _spy(**kw):
        seen.append(kw)
        return real(**kw)

    display.coordinator.request_frame = _spy
    return seen, real


def test_a_frame_dropped_for_want_of_pixels_is_drawn_when_they_arrive(app):
    """A snapshot of None must not end the cycle.

    One request, nothing resident, and then the array arrives through the
    REAL reader callback (`_on_lowres_read`) -- the test never asks for a
    frame itself and never installs an array itself. What has to happen is a
    drop, then a lowres request, then a seed, then exactly one publish.
    """
    w, loader = _window(app, path="/tmp/b8_lifecycle/rearm.ome.tiff")
    try:
        display, page = w._display, w._step0
        co = display.coordinator
        channel = "CD31"                       # nothing has drawn it
        page.current_channel = channel
        display.state.set_display_visible(channel, True, origin="test")
        display.fusion.edit_channel_weight(channel, 1.0, origin="test")
        # hold every read: this test is about what happens while NOTHING is
        # resident, and the delivery below is the reader's own callback.
        loader.hold = True
        page._slide_lowres.pop(channel, None)
        _pump(200)
        page._slide_lowres.pop(channel, None)
        assert page.tissue_lowres_array(channel) is None

        # a clean clock: nothing pending, nothing in flight, no counts
        co._timer.stop()
        co._pending_rev = None
        co._in_flight = False
        for key in co._stats:
            co._stats[key] = 0

        co.request_frame(kind="navigator_shown", channel=channel)
        _pump(400)

        assert co._stats["dropped"] >= 1, "the empty page drew something"
        assert co._stats["published"] == 0

        # the array arrives the way the reader delivers it
        display._on_lowres_read(
            {"channel": channel, "token": display._lowres_token(),
             "array": loader._pattern(channel, LOW_H, LOW_W)})
        _pump(1200)

        assert page._slide_lowres.get(channel) is not None
        assert display.state.mapping(channel) is not None, \
            "no window was seeded"
        assert co._stats["published"] >= 1, "the frame never came back"
        assert co._pending_rev is None
    finally:
        loader.hold = False
        _close(w)


def test_a_display_window_seed_asks_for_a_frame(app):
    """The other arrival. A channel with pixels but no window is left OUT of
    the picture, so the ordinary "is it in the last frame?" relevance test
    answers no by construction -- the seed has to ask unconditionally."""
    w, loader = _window(app, path="/tmp/b8_lifecycle/seed.ome.tiff")
    try:
        display, page = w._display, w._step0
        state = display.state
        channel = "FoxP3"
        page.current_channel = channel
        state.set_display_visible(channel, True, origin="test")
        page.install_tissue_lowres(channel, page._dataset_token(),
                                   loader._pattern(channel, LOW_H, LOW_W))
        assert state.mapping(channel) is None
        seen, real = _frame_spy(display)
        try:
            display._on_mapping_seeded(
                {"binding": state.binding(), "channel": channel,
                 "nucleus": False, "min": 3.0, "max": 77.0, "gamma": 1.0})
            _pump(300)
        finally:
            display.coordinator.request_frame = real

        assert state.mapping(channel) == (3.0, 77.0, 1.0)
        assert [f for f in seen if f.get("kind") == "seed"], seen
    finally:
        _close(w)


def test_an_array_from_another_slide_reaches_nothing_at_all(app):
    """Refused at the door: not installed, not handed to Intensity, and not
    treated as a reason to redraw."""
    w, loader = _window(app, path="/tmp/b8_lifecycle/stale.ome.tiff")
    try:
        display, page = w._display, w._step0
        wb = _workbench(w, "CD3")
        installs = []
        real_install = page.install_tissue_lowres
        page.install_tissue_lowres = \
            lambda ch, token, arr: (installs.append(ch),
                                    real_install(ch, token, arr))[1]
        seen, real_request = _frame_spy(display)
        try:
            display._on_lowres_read(
                {"channel": "CD8", "token": ("another-slide", "/tmp/z.tiff"),
                 "array": np.ones((LOW_H, LOW_W), np.float32)})
            _pump(200)
        finally:
            page.install_tissue_lowres = real_install
            display.coordinator.request_frame = real_request

        assert installs == [], "a foreign slide's array was installed"
        assert seen == [], "a refused array asked for a frame"
        assert wb._raw.get("CD8") is None
        assert "CD8" not in page._slide_lowres
    finally:
        _close(w)


def test_a_row_control_that_lost_its_parent_is_never_shown(app):
    """The structural guard behind the tiny nameless windows: a step change
    shows a control only where there is a row to show it in."""
    w, loader = _window(app)
    try:
        dock = w._channel_dock
        row = dock.row("CD3")
        box = row.fusion_box
        w._set_step_active(0)
        _pump(100)
        row._lay.removeWidget(box)
        box.setParent(None)

        row.set_step(1)                 # Step1 shows the participation box

        assert box.parentWidget() is None
        assert not box.isVisible(), "an orphaned control was floated as a window"
        assert box not in QtWidgets.QApplication.topLevelWidgets() or \
            not box.isVisible()
    finally:
        _close(w)


def test_the_clock_comes_back_to_a_frame_it_could_not_draw(app):
    """A request that arrives WHILE a frame is being drawn must still be
    drawn when that frame turns out to be undrawable.

    This is the real first-frame shape: the draw finds nothing resident, and
    the array lands from the reader in the middle of it. If the drop simply
    returns, that queued request stays in `_pending_rev` with no timer behind
    it -- and, with no further user input, the window stays empty for ever.
    """
    w, loader = _window(app, path="/tmp/b8_lifecycle/clock.ome.tiff")
    try:
        co = w._display.coordinator

        class _Context:
            def __init__(self):
                self.calls = 0

            def tissue_render_snapshot(self, computed_only=True):
                self.calls += 1
                if self.calls == 1:
                    # the arrival, landing inside the draw
                    co.request_frame(kind="lowres", channel="CD3")
                    return None
                return None

        context = _Context()
        co.register_context("probe-step", context)
        previous = co.active_context_id()
        co.set_active_context("probe-step")
        try:
            co._timer.stop()
            co._pending_rev = None
            co._in_flight = False
            co._pending_rev = co._input_rev + 1

            co._apply_pending_frame()

            assert context.calls >= 1
            # THE INVARIANT: a queued request is never left without a clock
            # behind it. Either it has already been drawn, or the timer is
            # armed to draw it -- what must never happen is a pending
            # revision and a stopped clock, which is a window that will stay
            # empty until some unrelated input happens along.
            stranded = (co._pending_rev is not None
                        and not co._timer.isActive())
            assert not stranded, \
                "a dropped frame left a queued request with no clock behind it"
        finally:
            co._timer.stop()
            co._pending_rev = None
            co.set_active_context(previous)
            co.unregister_context("probe-step")
    finally:
        _close(w)


def test_a_viewers_completion_reaches_the_inspector_too(app):
    """Both readers end at the same arrival entry.

    A viewer's overview worker used to fill the shared store and redraw the
    thumbnail while the Intensity inspector -- sitting on that very channel
    with an empty histogram -- was told nothing, so which of the two readers
    happened to serve a channel decided whether its histogram filled.
    """
    w, loader = _window(app, path="/tmp/b8_lifecycle/viewer.ome.tiff")
    try:
        page = w._step0
        wb = _workbench(w, "CD3")
        channel = "CD68"
        page.current_channel = channel
        w._display.state.set_selected_channel(channel, origin="test")
        _pump(200)
        wb._raw[channel] = None
        page._slide_lowres.pop(channel, None)

        class _Record:
            arr = loader._pattern(channel, LOW_H, LOW_W)
            source = "viewer"

        # what a viewer's store holds once ITS worker has finished
        page._resident_overview_record = lambda ch: (
            _Record() if ch == channel else None)

        page._on_channel_overview_ready("src", channel, 0, True)

        assert page._slide_lowres.get(channel) is not None, \
            "the viewer's array never reached the shared store"
        assert wb._raw.get(channel) is not None, \
            "the viewer's array never reached the Intensity inspector"
    finally:
        _close(w)
