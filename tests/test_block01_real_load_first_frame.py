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
