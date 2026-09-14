"""The first Tissue Preview frame, through the REAL Load state machine.

Nothing here is assembled by hand. The test types a path into Step0's own
path box and presses Step0's own Load button; the loader is injected at the
ONE place a loader is constructed (`step0_page.OMETIFFLoader`), so the whole
commit block runs as it does for a user: the generation moves, the viewers
are unbound, `_reset_dataset_view_state` runs, the channel list is rebuilt,
the panels are bound to the new dataset, the landing view is entered and the
navigator auto-opens.

In particular the test never sets `current_channel`, never sets a display
window, never sets visibility and never calls `install_tissue_lowres`: if the
picture appears, it appeared because the load lifecycle produced it.

No ROI, no patch, and Step1 is never entered.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import os
import threading
import time
import weakref

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets  # noqa: E402

SLIDE_H, SLIDE_W = 2048, 1024
LOW_H, LOW_W = 64, 32
CHANNELS = ["DAPI", "CD3", "CD8", "CD20", "CD68", "Ki67", "PanCK",
            "FoxP3", "CD45", "CD31"]


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Slide:
    """What `OMETIFFLoader(path)` hands back, for a synthetic slide.

    Faithful where it matters: many channels, a whole-slide low-resolution
    read at a real downsample, nothing pre-loaded, and a barrier the test can
    hold the reads at. Every read records the THREAD it ran on.
    """

    def __init__(self, path, _name_map=None):
        self.filepath = str(path)
        self.shape = (SLIDE_H, SLIDE_W)
        self._names = list(CHANNELS)
        self.ch_map = {c: i for i, c in enumerate(self._names)}
        self.reads = []
        self.lowres_reads = []          # (channel, thread id)
        self.hold = False

    def channel_names(self):
        return list(self._names)

    def overview_downsample(self):
        return SLIDE_H // LOW_H

    def set_correction_config(self, *_a, **_k):
        return None

    def set_corrected_zarr_store(self, *_a, **_k):
        return None

    def _pattern(self, channel, h, w):
        i = self.ch_map.get(channel, 0)
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        base = (yy * (i + 1) + xx * (10 - i)) / float(h + w)
        return (base * 200.0 + 10.0 * (i + 1)).astype(np.float32)

    def read_region(self, channel, y0, y1, x0, x1, downsample=1,
                    normalize=True):
        self.reads.append((channel, threading.get_ident()))
        ds = max(1, int(downsample))
        return self._pattern(channel, (y1 - y0) // ds or 1,
                             (x1 - x0) // ds or 1)

    def read_region_lowres(self, channel, y0, y1, x0, x1, downsample,
                           normalize=False):
        end = time.monotonic() + 3.0
        while self.hold and time.monotonic() < end:
            time.sleep(0.005)
        self.lowres_reads.append((channel, threading.get_ident()))
        ds = max(1, int(downsample))
        return self._pattern(channel, (y1 - y0) // ds or 1,
                             (x1 - x0) // ds or 1)

    def lowres_count(self, channel):
        return len([c for c, _t in self.lowres_reads if c == channel])


class _Timeline:
    """Every step of the first frame, in order, as the code takes it."""

    def __init__(self, window):
        self.events = []
        self._w = window
        co = window._display.coordinator
        page = window._step0
        self._co, self._page = co, page
        self._real_request = co.request_frame
        self._real_snapshot = page.tissue_render_snapshot
        self._real_install = page.install_tissue_lowres
        self._real_seed = window._display.request_mapping_seed
        self._real_publish = co._publish if hasattr(co, "_publish") else None

        def request(**kw):
            self.events.append(("request", kw.get("kind"), kw.get("channel"),
                                co._input_rev, co._pending_rev))
            return self._real_request(**kw)

        def snapshot(computed_only=True):
            out = self._real_snapshot(computed_only=computed_only)
            self.events.append(("snapshot",
                                "none" if out is None else "ready",
                                None if out is None else out.get("channel"),
                                None if out is None else
                                tuple(sorted(out.get("loading") or ())),
                                None))
            return out

        def install(channel, token, array):
            ok = self._real_install(channel, token, array)
            self.events.append(("install", channel, bool(ok),
                                threading.get_ident(), None))
            return ok

        def seed(channel, nucleus=False):
            self.events.append(("seed_requested", channel, nucleus, None, None))
            return self._real_seed(channel, nucleus=nucleus)

        co.request_frame = request
        page.tissue_render_snapshot = snapshot
        page.install_tissue_lowres = install
        window._display.request_mapping_seed = seed

    def restore(self):
        self._co.request_frame = self._real_request
        self._page.tissue_render_snapshot = self._real_snapshot
        self._page.install_tissue_lowres = self._real_install
        self._w._display.request_mapping_seed = self._real_seed

    def kinds(self, name):
        return [e for e in self.events if e[0] == name]


def _load(app, monkeypatch, tmp_path, name="slideA", hold=False):
    """Press Step0's own Load button on a synthetic slide."""
    from block01.ui.main_window import MainWindow
    from block01.ui.step0 import step0_page as sp

    ome = tmp_path / f"{name}.ome.tif"
    ome.write_bytes(b"synthetic slide")
    out = tmp_path / f"{name}_out"
    made = {}

    def _factory(path, name_map=None):
        slide = _Slide(path, name_map)
        slide.hold = hold
        made["slide"] = slide
        return slide

    monkeypatch.setattr(sp, "OMETIFFLoader", _factory)
    w = MainWindow()
    w.resize(1500, 950)
    w.show()
    w._set_step_active(0)
    QtWidgets.QApplication.processEvents()
    page = w._step0
    page._ome_path_edit.setText(str(ome))
    page._out_path_edit.setText(str(out))
    page._panel_csv_edit.setText("")
    timeline = _Timeline(w)
    page._btn_load.click()                       # THE REAL ENTRY
    QtWidgets.QApplication.processEvents()
    return w, made["slide"], timeline


def _pump(ms=1500):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.005)


def _close(w, timeline=None):
    if timeline is not None:
        timeline.restore()
    w.hide()
    w._display.shutdown("test")
    w.close()


def _thumbnail(w):
    """The PUBLISHED tissue frame, or None.

    The panel also draws its own plain overview array (2-D, its own read);
    the frame this suite is about is the composed RGB the coordinator
    publishes, so a 2-D image means "no frame yet" rather than "a picture".
    """
    img = w._step0.overview.img_item.image
    if img is None:
        return None
    arr = np.asarray(img)
    return arr if arr.ndim == 3 and arr.shape[-1] == 3 else None


# ── the first frame ─────────────────────────────────────────────────────────

def test_a_real_load_produces_the_first_frame_by_itself(app, monkeypatch,
                                                        tmp_path):
    """Load, and wait. No second click, no step change, no ROI, no patch."""
    w, slide, timeline = _load(app, monkeypatch, tmp_path)
    try:
        page = w._step0
        # the load chose these, not the test
        assert page.current_channel, "the load installed no current channel"
        assert page.patches == [] and not page.rois
        assert page._tissue_navigator_popup is not None, \
            "the load did not open the Tissue Preview"
        assert w._display.state.display_visible(page.current_channel) is True

        _pump(2000)

        img = _thumbnail(w)
        assert img is not None and float(img.max()) > 0.0, timeline.events
        assert page.patches == [] and not page.rois
        assert w._display.coordinator.active_context_id() is not None
        # ...and the picture is of the channel the load landed on
        snap = page.tissue_render_snapshot(computed_only=False)
        assert snap["channel"] == page.current_channel
    finally:
        _close(w, timeline)


def test_the_first_frame_timeline_is_request_read_seed_publish(app, monkeypatch,
                                                               tmp_path):
    """The order the contract names, recorded from the real objects."""
    w, slide, timeline = _load(app, monkeypatch, tmp_path)
    try:
        _pump(2000)
        page = w._step0
        channel = page.current_channel
        events = timeline.events

        # 1. a snapshot that could not draw yet
        assert [e for e in events if e[0] == "snapshot" and e[1] == "none"], \
            events
        # 2. the array arrives and is installed on the GUI thread
        installs = [e for e in events if e[0] == "install" and e[2]]
        assert installs, events
        assert all(e[3] == threading.get_ident() for e in installs)
        # 3. its arrival asks for a frame
        assert [e for e in events
                if e[0] == "request" and e[1] == "lowres"], events
        # 4. a window is seeded and asks for a frame
        assert [e for e in events if e[0] == "seed_requested"], events
        assert [e for e in events if e[0] == "request" and e[1] == "seed"], \
            events
        # 5. a snapshot that CAN draw, and a picture
        assert [e for e in events if e[0] == "snapshot" and e[1] == "ready"], \
            events
        assert w._display.coordinator._stats["published"] >= 1
        assert float(_thumbnail(w).max()) > 0.0
        # ...and the read never ran on the GUI thread
        assert all(t != threading.get_ident()
                   for _c, t in slide.lowres_reads), slide.lowres_reads
        assert slide.lowres_count(channel) == 1, slide.lowres_reads
    finally:
        _close(w, timeline)


def test_a_load_whose_reads_are_held_still_draws_when_they_finish(app,
                                                                  monkeypatch,
                                                                  tmp_path):
    """The popup opens on a slide whose reads have not finished. Loading,
    then -- with no further user input at all -- a picture."""
    w, slide, timeline = _load(app, monkeypatch, tmp_path, hold=True)
    try:
        page = w._step0
        _pump(400)
        assert page.current_channel not in page._slide_lowres
        assert _thumbnail(w) is None, "a frame was published with no pixels"
        published_before = w._display.coordinator._stats["published"]

        slide.hold = False                      # the read finishes
        _pump(2500)                             # and NOTHING else happens

        img = _thumbnail(w)
        assert img is not None and float(img.max()) > 0.0, timeline.events
        assert w._display.coordinator._stats["published"] > published_before
        assert page.patches == [] and not page.rois
    finally:
        slide.hold = False
        _close(w, timeline)


def test_a_preview_hidden_while_the_read_runs_draws_when_reopened(app,
                                                                  monkeypatch,
                                                                  tmp_path):
    w, slide, timeline = _load(app, monkeypatch, tmp_path, hold=True)
    try:
        page = w._step0
        popup = page._tissue_navigator_popup
        assert popup is not None
        popup.hide()
        slide.hold = False
        _pump(1200)

        page.show_tissue_navigator()            # the user's own gesture
        _pump(1500)

        img = _thumbnail(w)
        assert img is not None and float(img.max()) > 0.0
        assert page.patches == []
    finally:
        slide.hold = False
        _close(w, timeline)


def test_a_second_load_refuses_the_first_slides_late_results(app, monkeypatch,
                                                             tmp_path):
    """A loaded, then B loaded at once: A's array and A's seed arrive late."""
    w, slide_a, timeline = _load(app, monkeypatch, tmp_path, name="A",
                                 hold=True)
    try:
        page = w._step0
        display = w._display
        a_token = page._dataset_token()
        a_binding = display.state.binding()
        a_channel = page.current_channel
        slide_a.hold = False

        # B, through the same real entry
        ome_b = tmp_path / "B.ome.tif"
        ome_b.write_bytes(b"synthetic slide B")
        page._ome_path_edit.setText(str(ome_b))
        page._out_path_edit.setText(str(tmp_path / "B_out"))
        page._btn_load.click()
        QtWidgets.QApplication.processEvents()
        b_token = page._dataset_token()
        assert b_token != a_token
        _pump(1200)
        b_store = dict(page._slide_lowres)

        # A's low-res read, arriving now
        display._on_lowres_read({"channel": a_channel, "token": a_token,
                                 "array": np.full((LOW_H, LOW_W), 7.0,
                                                  np.float32)})
        # ...and A's display-window seed
        display._on_mapping_seeded(
            {"binding": a_binding, "channel": a_channel, "nucleus": False,
             "min": 1.0, "max": 2.0, "gamma": 1.0})
        _pump(300)

        for ch, entry in page._slide_lowres.items():
            assert entry[0] == b_token, (ch, entry[0])
        assert not np.array_equal(
            page._slide_lowres.get(a_channel, (None, np.zeros(1)))[1],
            np.full((LOW_H, LOW_W), 7.0, np.float32))
        assert set(page._slide_lowres) >= set(b_store)
    finally:
        _close(w, timeline)


# ── one owner per (dataset, channel) ────────────────────────────────────────

def test_a_channel_a_viewer_is_reading_is_not_read_again(app, monkeypatch,
                                                         tmp_path):
    """The double-producer risk, stated as a rule.

    `ensure_tissue_lowres` used to answer "still missing" for a channel a
    viewer had just taken, and Block01 read it a second time on its own
    thread: one channel, two decodes, two arrivals.
    """
    w, slide, timeline = _load(app, monkeypatch, tmp_path)
    try:
        page = w._step0
        display = w._display
        channel = "CD68"                      # nothing has drawn it
        taken = []
        monkeypatch.setattr(page, "_request_overview_async",
                            lambda ch: (taken.append(ch), True)[1])
        monkeypatch.setattr(page, "_overview_read_pending",
                            lambda ch: ch in taken)
        fallback = []
        monkeypatch.setattr(display, "_request_lowres_reads",
                            lambda chs: fallback.extend(chs))

        status = page.tissue_lowres_status([channel])
        assert status == {channel: page.LOWRES_REQUESTED}
        display.ensure_lowres([channel])
        display.ensure_lowres([channel])       # asked again while in flight

        assert taken == [channel], taken
        assert fallback == [], "Block01 read a channel the viewer owns"
    finally:
        _close(w, timeline)


def test_a_channel_no_viewer_takes_is_read_exactly_once(app, monkeypatch,
                                                        tmp_path):
    w, slide, timeline = _load(app, monkeypatch, tmp_path)
    try:
        page = w._step0
        display = w._display
        channel = "FoxP3"
        monkeypatch.setattr(page, "_request_overview_async", lambda ch: False)
        monkeypatch.setattr(page, "_overview_read_pending", lambda ch: False)

        assert page.tissue_lowres_status([channel]) == {
            channel: page.LOWRES_UNAVAILABLE}
        display.ensure_lowres([channel])
        display.ensure_lowres([channel])
        _pump(1500)

        assert slide.lowres_count(channel) == 1, slide.lowres_reads
        assert page._slide_lowres.get(channel) is not None
    finally:
        _close(w, timeline)


def test_eight_cold_channels_are_each_read_once(app, monkeypatch, tmp_path):
    """Step1 selects them one by one; every channel costs exactly one read."""
    w, slide, timeline = _load(app, monkeypatch, tmp_path)
    try:
        _pump(1200)
        w._set_step_active(1)
        w._display.show_intensity(w._step0.current_channel)
        _pump(600)
        cold = [c for c in CHANNELS if slide.lowres_count(c) == 0][:8]
        assert len(cold) == 8

        for ch in cold:
            w._display.state.set_selected_channel(ch, origin="test")
            _pump(600)

        wb = w._step0._cond_workbench
        for ch in cold:
            assert slide.lowres_count(ch) == 1, (ch, slide.lowres_reads)
            assert wb._raw.get(ch) is not None, ch
        assert w._step0.patches == []
    finally:
        _close(w, timeline)


# ── threads ─────────────────────────────────────────────────────────────────

def test_the_reads_are_off_the_gui_thread_and_the_widgets_are_on_it(
        app, monkeypatch, tmp_path):
    """Who runs where, recorded rather than assumed."""
    gui = threading.get_ident()
    w, slide, timeline = _load(app, monkeypatch, tmp_path)
    try:
        page = w._step0
        display = w._display
        threads = {"wake": [], "deliver": [], "histogram": []}
        real_wake = page.wake_intensity_pixels
        monkeypatch.setattr(
            page, "wake_intensity_pixels",
            lambda ch: (threads["wake"].append(threading.get_ident()),
                        real_wake(ch))[1])
        display.show_intensity(page.current_channel)
        _pump(800)
        wb = page._cond_workbench
        real_deliver = wb.deliver_pixels
        monkeypatch.setattr(
            wb, "deliver_pixels",
            lambda ch, arr: (threads["deliver"].append(threading.get_ident()),
                             real_deliver(ch, arr))[1])
        real_hist = wb._load_params_into_controls
        monkeypatch.setattr(
            wb, "_load_params_into_controls",
            lambda name, rebuild_histogram=True: (
                threads["histogram"].append(threading.get_ident()),
                real_hist(name, rebuild_histogram))[1])

        w._display.state.set_selected_channel("CD31", origin="test")
        _pump(1500)

        assert slide.lowres_reads, "nothing was read"
        on_gui = [c for c, t in slide.lowres_reads if t == gui]
        assert not on_gui, (gui, slide.lowres_reads)
        assert threads["wake"] and all(t == gui for t in threads["wake"])
        assert threads["deliver"] and all(t == gui for t in threads["deliver"])
        assert threads["histogram"] and all(t == gui
                                            for t in threads["histogram"])
    finally:
        _close(w, timeline)


def test_the_gui_callback_itself_does_no_reading(app, monkeypatch, tmp_path):
    """The provider's own cost, measured on the GUI thread with the read
    held: it must return at once rather than wait for the decode."""
    w, slide, timeline = _load(app, monkeypatch, tmp_path)
    try:
        page = w._step0
        display = w._display
        display.show_intensity(page.current_channel)
        _pump(600)
        slide.hold = True
        started = time.perf_counter()
        answer = page._workbench_pixels_async("CD45")
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        assert answer is None, "the provider answered with a fresh decode"
        assert elapsed_ms < 50.0, elapsed_ms
        assert slide.lowres_count("CD45") == 0
        slide.hold = False
        _pump(1200)
        assert slide.lowres_count("CD45") == 1
    finally:
        slide.hold = False
        _close(w, timeline)


# ── every arrival reaches the preview that is drawing ───────────────────────

def _stats_reset(w):
    co = w._display.coordinator
    co._timer.stop()
    co._pending_rev = None
    co._in_flight = False
    for key in co._stats:
        co._stats[key] = 0
    return co


def test_a_viewer_owned_arrival_asks_for_a_frame_for_any_channel(
        app, monkeypatch, tmp_path):
    """The gap this closes.

    A viewer's overview worker finishing a channel used to ask for a frame
    only when that channel was Step0's current one or the nucleus. A channel
    another step is drawing -- a Step1 overlay's, a fusion participant's --
    arrived, woke the Intensity inspector, and then waited for an unrelated
    input to be drawn.
    """
    w, slide, timeline = _load(app, monkeypatch, tmp_path)
    try:
        page = w._step0
        display = w._display
        _pump(1200)
        w._set_step_active(1)
        _pump(300)
        channel = "CD20"                       # neither current nor nucleus
        assert page.current_channel != channel
        assert page.nucleus_channel != channel
        page._slide_lowres.pop(channel, None)
        display._announced_lowres.pop(channel, None)

        class _Record:
            arr = slide._pattern(channel, LOW_H, LOW_W)
            source = "viewer"

        page._resident_overview_record = lambda ch: (
            _Record() if ch == channel else None)
        co = _stats_reset(w)
        requests = []
        real_request = co.request_frame
        co.request_frame = lambda **kw: (requests.append(kw),
                                         real_request(**kw))[1]
        try:
            page._on_channel_overview_ready("src", channel, 0, True)
            _pump(400)
        finally:
            co.request_frame = real_request

        assert page._slide_lowres.get(channel) is not None
        assert [r for r in requests
                if r.get("kind") == "lowres" and r.get("channel") == channel], \
            requests
    finally:
        _close(w, timeline)


def test_a_fallback_owned_arrival_asks_for_a_frame_too(app, monkeypatch,
                                                       tmp_path):
    w, slide, timeline = _load(app, monkeypatch, tmp_path)
    try:
        page, display = w._step0, w._display
        _pump(1000)
        channel = "Ki67"
        page._slide_lowres.pop(channel, None)
        display._announced_lowres.pop(channel, None)
        co = _stats_reset(w)
        requests = []
        real_request = co.request_frame
        co.request_frame = lambda **kw: (requests.append(kw),
                                         real_request(**kw))[1]
        try:
            display._on_lowres_read(
                {"channel": channel, "token": display._lowres_token(),
                 "array": slide._pattern(channel, LOW_H, LOW_W)})
            _pump(300)
            arrivals = [r for r in requests
                        if r.get("kind") == "lowres"
                        and r.get("channel") == channel]
            assert arrivals, requests
            # ONE ARRIVAL IS ANNOUNCED ONCE, however many producers report
            # it: two readers finishing the same channel must not produce two
            # frames.
            assert display.notify_lowres_arrived(channel, owner="view") is False
            assert display.notify_lowres_arrived(channel, owner="fallback") \
                is False
            _pump(100)
            assert [r for r in requests
                    if r.get("kind") == "lowres"
                    and r.get("channel") == channel] == arrivals
        finally:
            co.request_frame = real_request
    finally:
        _close(w, timeline)


def test_a_participating_channel_appears_in_the_frame_when_it_lands(
        app, monkeypatch, tmp_path):
    """Step1, six participants, and the one that was still reading.

    Before its array lands the composed picture cannot contain it; after it
    lands -- with no user input of any kind -- a new frame is published that
    does, and the Intensity window is left on whatever channel it was
    showing.
    """
    w, slide, timeline = _load(app, monkeypatch, tmp_path)
    try:
        page, display = w._step0, w._display
        state, fusion = display.state, display.fusion
        _pump(1200)
        w._set_step_active(1)
        participants = ["DAPI", "CD3", "CD8", "CD68", "Ki67", "CD20"]
        for ch in participants:
            state.set_display_visible(ch, True, origin="test")
            fusion.set_fusion_enabled(ch, True, origin="test")
            fusion.edit_channel_weight(ch, 1.0, origin="test")
        state.set_selected_channel("CD3", origin="test")
        display.show_intensity("CD3")
        _pump(1200)
        wb = page._cond_workbench
        assert wb._active == "CD3"

        late = "CD20"
        page._slide_lowres.pop(late, None)
        display._announced_lowres.pop(late, None)
        snap = page.tissue_render_snapshot(computed_only=True)
        assert snap is None or late not in (snap.get("arrays") or {})

        # the viewer's worker finishes it -- and nothing else happens
        class _Record:
            arr = slide._pattern(late, LOW_H, LOW_W)
            source = "viewer"

        page._resident_overview_record = lambda ch: (
            _Record() if ch == late else None)
        co = _stats_reset(w)
        requests = []
        real_request = co.request_frame
        co.request_frame = lambda **kw: (requests.append(kw),
                                         real_request(**kw))[1]
        try:
            page._on_channel_overview_ready("src", late, 0, True)
            _pump(1200)
        finally:
            co.request_frame = real_request

        assert page._slide_lowres.get(late) is not None
        # the arrival asked for a frame FOR THIS CHANNEL, though it is
        # neither Step0's current channel nor the nucleus...
        assert [r for r in requests
                if r.get("kind") == "lowres" and r.get("channel") == late], \
            requests
        # ...and the picture can now be composed with it in
        snap = page.tissue_render_snapshot(computed_only=False)
        assert snap is not None
        assert page.tissue_lowres_array(late) is not None
        # the inspector was not dragged onto the channel that happened to
        # finish reading
        assert wb._active == "CD3"
        assert page.patches == []
    finally:
        _close(w, timeline)


def test_an_arrival_is_refused_unless_it_is_this_slide_and_really_here(
        app, monkeypatch, tmp_path):
    """The two things the one entry checks before it fans anything out."""
    w, slide, timeline = _load(app, monkeypatch, tmp_path)
    try:
        page, display = w._step0, w._display
        _pump(1000)
        co = _stats_reset(w)
        requests = []
        real_request = co.request_frame
        co.request_frame = lambda **kw: (requests.append(kw),
                                         real_request(**kw))[1]
        woken = []
        real_wake = page.wake_intensity_pixels
        page.wake_intensity_pixels = lambda ch: (woken.append(ch),
                                                 real_wake(ch))[1]
        try:
            resident = page.current_channel
            assert page.tissue_lowres_array(resident) is not None
            # forget that it was already announced, so the refusal below is
            # the thing being measured rather than the de-duplication
            display._announced_lowres.pop(resident, None)
            # ANOTHER SLIDE: refused even though the array is right here
            assert display.notify_lowres_arrived(
                resident, owner="view",
                token=("another-slide", "/tmp/z.tiff")) is False

            # NOT RESIDENT: a claim is not an arrival
            absent = "CD45"
            page._slide_lowres.pop(absent, None)
            page._resident_overview_record = lambda ch: None
            assert page.tissue_lowres_array(absent) is None
            assert display.notify_lowres_arrived(absent, owner="view") is False

            _pump(150)
            assert requests == [], requests
            assert woken == [], woken
        finally:
            co.request_frame = real_request
            page.wake_intensity_pixels = real_wake
    finally:
        _close(w, timeline)


# ── the cap is a queue, not a claim ─────────────────────────────────────────

def test_the_capped_channels_are_really_queued_and_all_arrive(app, monkeypatch,
                                                              tmp_path):
    """Ten missing channels, four at a time, and nothing to push them along
    but the arrivals themselves."""
    w, slide, timeline = _load(app, monkeypatch, tmp_path)
    try:
        page, display = w._step0, w._display
        _pump(1000)
        page._slide_lowres.clear()
        display._announced_lowres.clear()
        cap = page._TISSUE_LOWRES_MAX_IN_FLIGHT
        assert cap == 4

        reading = set()
        served = []

        def _take(ch):
            if len(reading) >= cap:
                return False
            reading.add(ch)
            return True

        page._request_overview_async = _take
        page._overview_read_pending = lambda ch: ch in reading

        def _finish_one():
            """One viewer read completes, exactly as the store reports it."""
            ch = sorted(reading)[0]
            reading.discard(ch)
            arr = slide._pattern(ch, LOW_H, LOW_W)
            page._resident_overview_record = lambda c, _ch=ch, _a=arr: (
                type("R", (), {"arr": _a, "source": "viewer"})()
                if c == _ch else None)
            served.append(ch)
            page._on_channel_overview_ready("src", ch, 0, True)
            page._resident_overview_record = lambda c: None

        wanted = list(CHANNELS)
        status = page.tissue_lowres_status(wanted)
        assert len(reading) == cap, reading
        assert all(v == page.LOWRES_REQUESTED for v in status.values()), status
        assert len(page._lowres_queue) == len(wanted) - cap

        # nothing but arrivals from here: no selection, no weight, no step
        for _ in range(len(wanted)):
            if not reading:
                break
            _finish_one()
            _pump(60)

        assert sorted(served) == sorted(wanted), served
        assert not page._lowres_queue, page._lowres_queue
        for ch in wanted:
            assert page._slide_lowres.get(ch) is not None, ch
            assert slide.lowres_count(ch) <= 1, (ch, slide.lowres_reads)
    finally:
        _close(w, timeline)


# ── the first-frame log ─────────────────────────────────────────────────────

def test_the_first_frame_log_is_silent_unless_it_is_asked_for(app, monkeypatch,
                                                              tmp_path):
    from block01.utils import tissue_log

    tissue_log.reset_for_test(None)
    try:
        assert tissue_log.enabled() is False
        assert tissue_log.path() is None
        w, slide, timeline = _load(app, monkeypatch, tmp_path)
        try:
            _pump(1200)
        finally:
            _close(w, timeline)
        # NOTHING was opened, named or written -- anywhere. A log that is off
        # must not decide on a path of its own.
        assert tissue_log.path() is None
        assert tissue_log._handle is None
        assert not list(tmp_path.glob("*.log"))
    finally:
        tissue_log.reset_for_test(None)


def test_the_log_records_the_whole_first_frame_chain(app, monkeypatch,
                                                     tmp_path):
    """Switched on, one real Load: every step of the chain is in the file."""
    from block01.utils import tissue_log

    log_path = tmp_path / "tissue.log"
    tissue_log.reset_for_test(str(log_path))
    try:
        w, slide, timeline = _load(app, monkeypatch, tmp_path, name="logged")
        try:
            _pump(2000)
        finally:
            _close(w, timeline)
        text = log_path.read_text(encoding="utf-8")
    finally:
        tissue_log.reset_for_test(None)

    for event in ("dataset.bind", "navigator.created", "navigator.shown",
                  "frame.request", "lowres.accepted", "seed.begin",
                  "seed.accepted", "frame.dispatch", "frame.publish",
                  "panel.accepted", "panel.paint"):
        assert event in text, (event, text[:2000])
    # THE TWO PICTURE STORES ARE DISTINGUISHABLE, on the paint line itself:
    # a 2-D plain overview and a 3-D composed frame look the same to a user,
    # and a frame overwritten by an overview looks like a frame that never
    # came.
    paints = [ln for ln in text.splitlines() if " panel.paint " in ln]
    assert paints, text[:2000]
    composed = [ln for ln in paints
                if "store=channel_rgb" in ln and "kind=channel_rgb" in ln]
    assert composed, paints[:10]
    for line in paints:
        assert "ndim=" in line and "shape=" in line, line
    # ...and no array was scanned into it
    assert "fingerprint=" in text
    assert len(text.splitlines()) < 4000


# ── the queue belongs to a slide, and keeps the caller's order ──────────────

class _ViewerStore:
    """A stand-in for the shared overview store, with the cap enforced by the
    test rather than by the page: it accepts reads, reports them pending, and
    completes them one at a time in the order they were accepted."""

    def __init__(self, page, slide, cap=4, accept=True):
        self.page, self.slide, self.cap = page, slide, cap
        self.accept = accept
        self.running, self.accepted, self.served = [], [], []
        page._request_overview_async = self.request
        page._overview_read_pending = lambda ch: ch in self.running
        page._resident_overview_record = lambda ch: None

    def request(self, ch):
        if not self.accept or len(self.running) >= self.cap:
            return False
        self.running.append(ch)
        self.accepted.append(ch)
        return True

    def finish(self, ch=None):
        """One accepted read completes, the way the viewer reports it."""
        if ch is None:
            if not self.running:
                return None
            ch = self.running[0]
        self.running.remove(ch)
        array = self.slide._pattern(ch, LOW_H, LOW_W)
        self.page._resident_overview_record = lambda c, _c=ch, _a=array: (
            type("R", (), {"arr": _a, "source": "viewer"})()
            if c == _c else None)
        self.served.append(ch)
        self.page._on_channel_overview_ready("src", ch, 0, True)
        self.page._resident_overview_record = lambda c: None
        return ch


def test_the_queue_starts_reads_in_the_order_they_were_asked_for(
        app, monkeypatch, tmp_path):
    """Ten channels, four at a time, in the CALLER'S order.

    The queue was a set, so the order a frame's channels were asked for --
    its own first, anything speculative after -- was lost to whatever the
    set iterated.
    """
    w, slide, timeline = _load(app, monkeypatch, tmp_path)
    try:
        page = w._step0
        _pump(1000)
        page._slide_lowres.clear()
        w._display._announced_lowres.clear()
        store = _ViewerStore(page, slide, cap=4)
        wanted = ["CD31", "FoxP3", "Ki67", "CD68", "CD20", "CD8", "CD3",
                  "PanCK", "CD45", "DAPI"]

        page.tissue_lowres_status(wanted)

        assert store.accepted == wanted[:4], store.accepted
        assert list(page._lowres_queue) == wanted[4:], page._lowres_queue

        for _ in range(len(wanted)):
            if not store.running:
                break
            store.finish()
            _pump(40)

        assert store.accepted == wanted, store.accepted
        assert store.served == wanted, store.served
        assert not page._lowres_queue
    finally:
        _close(w, timeline)


def test_a_switch_retires_the_previous_slides_queue(app, monkeypatch,
                                                    tmp_path):
    """A's queued channels are A's promise. After B is loaded they are not
    started -- not by B's arrivals, not by anything."""
    w, slide_a, timeline = _load(app, monkeypatch, tmp_path, name="QA")
    try:
        page, display = w._step0, w._display
        _pump(1000)
        page._slide_lowres.clear()
        store_a = _ViewerStore(page, slide_a, cap=4)
        a_wanted = ["CD31", "FoxP3", "Ki67", "CD68", "CD20", "CD8", "CD3",
                    "PanCK", "CD45", "DAPI"]
        page.tissue_lowres_status(a_wanted)
        assert len(page._lowres_queue) == 6
        a_token = page._dataset_token()

        # B, through the real entry
        ome_b = tmp_path / "QB.ome.tif"
        ome_b.write_bytes(b"synthetic slide B")
        page._ome_path_edit.setText(str(ome_b))
        page._out_path_edit.setText(str(tmp_path / "QB_out"))
        page._btn_load.click()
        QtWidgets.QApplication.processEvents()
        slide_b = page.loader
        assert page._dataset_token() != a_token
        _pump(600)

        store_b = _ViewerStore(page, slide_b, cap=4)
        started_before = list(store_b.accepted)

        # A's read completes now -- late, and about the slide that is gone
        page._resident_overview_record = lambda ch: None
        page._on_channel_overview_ready("src", "CD31", 0, True)
        _pump(200)

        assert store_b.accepted == started_before, \
            "a late arrival of the previous slide started a read of this one"
        # the queue belongs to THIS slide now, and nothing of A's is owed.
        # (B may have queued channels of its own by this point; what must be
        # gone is the STAMP and the reads A was promised.)
        assert page._lowres_queue_token == page._dataset_token()
        assert store_a.served == [], store_a.served
    finally:
        _close(w, timeline)


def test_a_switch_drops_the_previous_slides_announced_arrays(app, monkeypatch,
                                                             tmp_path):
    """The de-duplication record must not keep a slide alive.

    It held the array itself, so every channel of every slide the session had
    touched stayed in memory behind a token that only stopped it being
    misused.
    """
    import gc

    w, slide_a, timeline = _load(app, monkeypatch, tmp_path, name="MA")
    try:
        page, display = w._step0, w._display
        _pump(1200)
        channel = page.current_channel
        array = slide_a._pattern(channel, LOW_H, LOW_W)
        page.install_tissue_lowres(channel, page._dataset_token(), array)
        display._announced_lowres.pop(channel, None)
        display.notify_lowres_arrived(channel, owner="test")
        assert channel in display._announced_lowres
        ref = weakref.ref(array)

        ome_b = tmp_path / "MB.ome.tif"
        ome_b.write_bytes(b"synthetic slide B")
        page._ome_path_edit.setText(str(ome_b))
        page._out_path_edit.setText(str(tmp_path / "MB_out"))
        page._btn_load.click()
        QtWidgets.QApplication.processEvents()
        _pump(600)
        b_channel = page.current_channel
        display.notify_lowres_arrived(b_channel, owner="test")

        assert display._announced_lowres_token == display._lowres_token()
        # NOTHING HERE KEEPS A'S ARRAY ALIVE: the record was emptied at the
        # switch, and what it holds now is weak anyway.
        del array
        page._slide_lowres.pop(channel, None)
        gc.collect()
        assert ref() is None, "the announce record still holds A's array"
    finally:
        _close(w, timeline)


# ── a request is owned only when somebody really took it ────────────────────

def test_a_viewer_that_cannot_take_the_read_hands_it_on(app, monkeypatch,
                                                        tmp_path):
    """`prepare_overview_async` returns quietly when a controller cannot
    serve -- a torn-down view, no source bound. That was read as "taken"."""
    w, slide, timeline = _load(app, monkeypatch, tmp_path)
    try:
        page, display = w._step0, w._display
        _pump(1000)
        channel = "CD45"
        page._slide_lowres.pop(channel, None)

        class _TornDown:
            asked = []

            def prepare_overview_async(self, ch):
                self.asked.append(ch)
                return False                # an explicit refusal

            def overview_read_pending(self, ch):
                return False

        class _Working:
            asked = []
            running = set()

            def prepare_overview_async(self, ch):
                self.asked.append(ch)
                self.running.add(ch)
                return None                 # ...but really takes it

            def overview_read_pending(self, ch):
                return ch in self.running

        torn, working = _TornDown(), _Working()
        page._overview_hosts = lambda: [torn, working]
        page._resident_overview_record = lambda ch: None

        assert page._request_overview_async(channel) is True
        assert torn.asked == [channel] and working.asked == [channel]

        # ...and when EVERY viewer refuses, the channel is unavailable, so
        # Block01's own reader takes it.
        page._overview_hosts = lambda: [torn]
        page._overview_read_pending = lambda ch: False
        fallback = []
        monkeypatch.setattr(display, "_request_lowres_reads",
                            lambda chs: fallback.extend(chs))
        status = page.tissue_lowres_status([channel])
        assert status == {channel: page.LOWRES_UNAVAILABLE}, status
        display.ensure_lowres([channel])
        assert fallback == [channel], fallback
    finally:
        _close(w, timeline)


def test_a_late_arrival_of_the_previous_slide_advances_nothing(app,
                                                               monkeypatch,
                                                               tmp_path):
    """The same rule from the other side: A's completion must not consume a
    slot B is queueing behind."""
    w, slide_a, timeline = _load(app, monkeypatch, tmp_path, name="LA")
    try:
        page, display = w._step0, w._display
        _pump(1000)
        a_token = page._dataset_token()

        ome_b = tmp_path / "LB.ome.tif"
        ome_b.write_bytes(b"synthetic slide B")
        page._ome_path_edit.setText(str(ome_b))
        page._out_path_edit.setText(str(tmp_path / "LB_out"))
        page._btn_load.click()
        QtWidgets.QApplication.processEvents()
        _pump(600)
        slide_b = page.loader
        page._slide_lowres.clear()
        store = _ViewerStore(page, slide_b, cap=4)
        wanted = ["CD31", "FoxP3", "Ki67", "CD68", "CD20", "CD8"]
        page.tissue_lowres_status(wanted)
        queued_before = list(page._lowres_queue)
        accepted_before = list(store.accepted)

        # A's array, arriving now, through the real reader callback
        display._on_lowres_read(
            {"channel": "CD3", "token": a_token,
             "array": slide_a._pattern("CD3", LOW_H, LOW_W)})
        _pump(200)

        assert list(page._lowres_queue) == queued_before, page._lowres_queue
        assert store.accepted == accepted_before, store.accepted
        assert "CD3" not in page._slide_lowres or \
            page._slide_lowres["CD3"][0] == page._dataset_token()
    finally:
        _close(w, timeline)


def test_a_drain_started_by_a_late_arrival_ignores_the_old_queue(
        app, monkeypatch, tmp_path):
    """The drain checks the slide ITSELF.

    An arrival is the one thing that starts queued reads, and a late one
    belongs to the slide the user has left. If the drain trusted whatever was
    in the queue, that arrival would start reads of a slide nobody is looking
    at -- against the current loader.
    """
    w, slide_a, timeline = _load(app, monkeypatch, tmp_path, name="DA")
    try:
        page = w._step0
        _pump(800)
        a_token = page._dataset_token()

        ome_b = tmp_path / "DB.ome.tif"
        ome_b.write_bytes(b"synthetic slide B")
        page._ome_path_edit.setText(str(ome_b))
        page._out_path_edit.setText(str(tmp_path / "DB_out"))
        page._btn_load.click()
        QtWidgets.QApplication.processEvents()
        _pump(500)
        slide_b = page.loader
        assert page._dataset_token() != a_token

        store = _ViewerStore(page, slide_b, cap=4)
        # a queue left over from A, exactly as it stood before the switch
        page._lowres_queue = ["CD31", "FoxP3", "Ki67"]
        page._lowres_queue_token = a_token

        started = page._drain_lowres_queue()

        assert started == 0, "the old slide's queue was started"
        assert store.accepted == [], store.accepted
        assert page._lowres_queue == []
        assert page._lowres_queue_token == page._dataset_token()
    finally:
        _close(w, timeline)


def test_the_announce_record_never_keeps_an_array_alive(app, monkeypatch,
                                                        tmp_path):
    """Even without a switch: what the de-duplication holds is weak.

    The record exists to say "this exact array has already been announced",
    which needs identity, not ownership -- and a strong reference here kept a
    whole-slide overview alive for every channel the session ever drew.
    """
    import gc

    w, slide, timeline = _load(app, monkeypatch, tmp_path, name="WK")
    try:
        page, display = w._step0, w._display
        _pump(800)
        channel = "CD68"
        array = slide._pattern(channel, LOW_H, LOW_W)
        page.install_tissue_lowres(channel, page._dataset_token(), array)
        display._announced_lowres.pop(channel, None)
        assert display.notify_lowres_arrived(channel, owner="test") is True
        assert channel in display._announced_lowres
        ref = weakref.ref(array)

        # every other holder of the array lets go; the record is the only
        # thing that could still be keeping it
        del array
        page._slide_lowres.pop(channel, None)
        wb = getattr(page, "_cond_workbench", None)
        if wb is not None:
            wb._raw.pop(channel, None)
        gc.collect()

        assert ref() is None, "the announce record is holding the array"
    finally:
        _close(w, timeline)
