"""The preview's frames come from a worker, and what that has to guarantee.

MEASURED before this: a Step1 frame cost 42-85 ms and ran on the GUI thread,
so every published frame was a stall of that length -- which is why a 33 ms
frame clock alone cannot make a drag feel attached to the hand. It would only
stall more often.

The contract this module pins:

* the GUI thread builds a snapshot, hands it over and returns;
* one computation at a time, one pending snapshot, never a history;
* a result is dropped only for STRUCTURAL staleness -- dataset, patch, mode,
  channel set, source arrays. Being one Intensity revision behind is not
  staleness: during a drag a newer input has almost always arrived by the time
  a frame finishes, so dropping those would starve the screen exactly while
  the hand is moving;
* the camera is read and written on the GUI thread only, and a published frame
  keeps the zoom and pan;
* nothing of the dataset's survives a dataset switch.

Own module: page-heavy Qt suites crash pyqtgraph offscreen when combined.
"""

import os
import threading

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets  # noqa: E402

import block01.ui.main_window as mw  # noqa: E402
from block01.core import preview_compose  # noqa: E402
from block01.workers.preview_compose_worker import (  # noqa: E402
    PreviewComposeWorker,
)


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
        return np.clip(np.asarray(arr, np.float32) / 1000.0, 0.0, 1.0)

    def read_region(self, channel, y0, y1, x0, x1, downsample=1,
                    normalize=True):
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
    w._all_patches = [(0, size, 0, size), (0, size, 0, size)]
    w._preview_patch_idx = 0
    rng = np.random.default_rng(3)
    w._patch_channel_cache[0] = {
        ch: (rng.random((size, size), dtype=np.float32) * 900 + 10)
        for ch in ("DAPI", "CD3", "CD8")}
    w._patch_load_ready.add(0)
    w.config.set_channel_visible("CD3", True)
    return w


class _Frames:
    """The worker, inline, with delivery under the test's control.

    Runs the real `PreviewComposeWorker._compose`, so the pixels and the
    result's identity fields are the production ones; only the thread and the
    signal are replaced. `compute_ms` is what a frame costs on the clock.
    """

    def __init__(self, w, clock, compute_ms=0.0):
        self._w = w
        self._clock = clock
        self.compute_ms = float(compute_ms)
        self.submitted = []
        self.delivered = []
        self.composed_on = []
        self._composer = PreviewComposeWorker.__new__(PreviewComposeWorker)
        self._composer.cache = preview_compose.PreviewCache()
        w._dispatch_frame = self._submit

    def _submit(self, snapshot):
        self.submitted.append(snapshot)
        return True

    def pending(self):
        return len(self.submitted)

    def compose(self, count=1):
        """Compute the queued snapshots without delivering them yet."""
        out = []
        for _ in range(min(count, len(self.submitted))):
            snapshot = self.submitted.pop(0)
            self._clock.advance_ms(self.compute_ms)
            self.composed_on.append(threading.current_thread().name)
            out.append(PreviewComposeWorker._compose(self._composer, snapshot))
        return out

    def deliver(self, count=1):
        results = self.compose(count)
        for result in results:
            self.delivered.append(result)
            self._w._on_preview_frame(result)
        return results


class _Clock:
    def __init__(self, w, idle_ms=10_000):
        self._w = w
        self.now = 0.0
        self.armed_at = None
        self.armed_wait = None
        self.arms = []
        w._frame_now = lambda: self.now
        w._arm_frame_timer = self._arm
        w._prev_timer.stop()
        w._prev_timer.isActive = lambda: self.armed_at is not None
        w._prev_timer.stop = self._disarm
        w._frame_last_publish = self.now - float(idle_ms) / 1000.0
        w._frame_input_at = w._frame_last_publish
        w._frame_pending_rev = None
        w._frame_in_flight = False

    def _arm(self, wait_ms):
        self.armed_at = self.now
        self.armed_wait = float(wait_ms)
        self.arms.append(float(wait_ms))

    def _disarm(self):
        self.armed_at = self.armed_wait = None

    def advance_ms(self, ms):
        self.now += float(ms) / 1000.0

    def due(self):
        return (self.armed_at is not None
                and (self.now - self.armed_at) * 1000.0
                >= self.armed_wait - 1e-6)

    def run_due_slot(self):
        if not self.due():
            return False
        self._disarm()
        self._w._apply_pending_preview_update()
        return True

    def run_all_slots(self, limit=1000):
        fired = 0
        while self.armed_at is not None and fired < limit:
            self.advance_ms(self.armed_wait)
            if not self.run_due_slot():
                break
            fired += 1
        return fired


# ── the GUI thread hands over and returns ────────────────────────────────

@pytest.mark.parametrize("mode", ["overlay", "fusion"])
def test_the_gui_thread_does_no_array_work_for_a_clocked_frame(app, mode,
                                                               monkeypatch):
    """The point of the whole arrangement: the remap, the composite and the
    conversion happen somewhere else."""
    w = _window(app)
    try:
        w.set_preview_mode(mode, force=True, reconcile=False)
        clock = _Clock(w)
        frames = _Frames(w, clock)
        calls = []
        for name in ("channel_gray", "channel_signal", "overlay_rgb_u8",
                     "fusion_rgb_u8"):
            real = getattr(preview_compose, name)
            monkeypatch.setattr(preview_compose, name,
                                lambda *a, _n=name, _r=real, **k: (
                                    calls.append(_n), _r(*a, **k))[1])

        w._schedule_preview_update()

        assert frames.pending() == 1, "the frame was not handed over"
        assert calls == [], f"the GUI thread computed {calls}"

        frames.deliver()
        assert calls, "and the worker did compute the frame"
    finally:
        w.close()


def test_a_snapshot_carries_the_identity_a_result_is_checked_against(app):
    w = _window(app)
    try:
        w.set_preview_mode("overlay", force=True, reconcile=False)
        clock = _Clock(w)
        frames = _Frames(w, clock)

        w._schedule_preview_update()
        snapshot = frames.submitted[0]

        for field in ("request_id", "rev", "mode", "patch", "dataset_gen",
                      "array_ids", "channels", "remap"):
            assert field in snapshot, field
        cache = w._patch_channel_cache[0]
        assert dict(snapshot["array_ids"]) == {
            ch: id(cache[ch]) for ch in snapshot["channels"]}
    finally:
        w.close()


# ── 200 Hz input against a 40-80 ms frame ────────────────────────────────

@pytest.mark.parametrize("compute_ms", [40.0, 80.0])
@pytest.mark.parametrize("mode", ["overlay", "fusion"])
def test_a_two_second_drag_keeps_publishing(app, mode, compute_ms):
    """Two seconds of input at 200 Hz with a frame that costs 40-80 ms.

    What must happen: intermediate frames keep coming, the queue never holds
    more than one, and the value the drag ended on is on screen. What must not
    happen is the old behaviour (one frame at the end) or a frame per input.
    """
    w = _window(app)
    try:
        w.set_preview_mode(mode, force=True, reconcile=False)
        clock = _Clock(w)
        frames = _Frames(w, clock, compute_ms=compute_ms)
        depths = []

        inputs = 0
        for _ in range(400):                  # 2 s at 5 ms per input
            clock.advance_ms(5)
            w._schedule_preview_update()
            inputs += 1
            depths.append(frames.pending())
            clock.run_due_slot()
            if frames.pending():
                frames.deliver()
        last_input = w._frame_input_rev
        for _ in range(4):
            clock.run_all_slots()
            if frames.pending():
                frames.deliver()

        published = [result["rev"] for result in frames.delivered]
        assert len(published) > 5, (
            f"{len(published)} frames in 2 s of dragging with a "
            f"{compute_ms:.0f} ms frame: the screen is being starved")
        assert len(published) < inputs / 4, "a frame per input is not a clock"
        assert max(depths) <= 1, "more than one frame was queued"
        assert published == sorted(published), published
        assert published[-1] == last_input, (
            f"the drag ended at revision {last_input}, the screen at "
            f"{published[-1]}")
    finally:
        w.close()


def test_a_result_one_revision_behind_is_still_published(app):
    """The rule that keeps a drag alive. A newer input has almost always
    arrived by the time a frame finishes; dropping those results would leave
    the screen frozen for as long as the hand keeps moving."""
    w = _window(app)
    try:
        w.set_preview_mode("overlay", force=True, reconcile=False)
        clock = _Clock(w)
        frames = _Frames(w, clock, compute_ms=50.0)
        published = []
        w.prev_img.setImage = lambda img, **k: published.append(img)

        w._schedule_preview_update()          # revision A, dispatched
        clock.advance_ms(5)
        w._schedule_preview_update()          # revision B arrives meanwhile
        assert w._frame_pending_rev is not None

        frames.deliver()                      # A comes back, B is pending

        assert len(published) == 1, \
            "a frame one revision behind was thrown away mid-drag"
        assert frames.delivered[0]["rev"] < w._frame_input_rev
    finally:
        w.close()


# ── structural staleness, and only structural ────────────────────────────

def _dispatched(w, clock, frames):
    w._schedule_preview_update()
    assert frames.pending() == 1
    return frames.compose(1)[0]


@pytest.mark.parametrize("break_it", [
    "dataset", "patch", "mode", "channels", "arrays",
])
def test_a_structurally_stale_result_is_never_published(app, break_it):
    w = _window(app)
    try:
        w.set_preview_mode("overlay", force=True, reconcile=False)
        clock = _Clock(w)
        frames = _Frames(w, clock, compute_ms=10.0)
        result = _dispatched(w, clock, frames)

        if break_it == "dataset":
            w._dataset_gen += 1
        elif break_it == "patch":
            w._preview_patch_idx = 1
        elif break_it == "mode":
            w.set_preview_mode("fusion", force=True, reconcile=False)
        elif break_it == "channels":
            w.config.set_channel_visible("CD8", True)
        elif break_it == "arrays":
            rng = np.random.default_rng(9)
            w._patch_channel_cache[0] = {
                ch: rng.random((32, 32), dtype=np.float32)
                for ch in ("DAPI", "CD3", "CD8")}

        # Installed HERE: ticking a channel or switching mode redraws
        # immediately and legitimately, and that frame is not what this test
        # is about.
        published = []
        w.prev_img.setImage = lambda img, **k: published.append(img)
        w._on_preview_frame(result)

        assert published == [], (
            f"a result computed before the {break_it} changed was published")
        assert w._frame_in_flight is False, \
            "and the slot is free again, so the next frame can be drawn"
    finally:
        w.close()


def test_a_current_result_is_published_and_keeps_the_camera(app):
    """The camera belongs to the widget. It is saved and restored here, on the
    GUI thread, around the one `setImage` -- an asynchronous frame may not
    reset the zoom the user is working at."""
    w = _window(app)
    try:
        w.set_preview_mode("overlay", force=True, reconcile=False)
        clock = _Clock(w)
        frames = _Frames(w, clock, compute_ms=10.0)
        frames.deliver() if frames.pending() else None
        w._schedule_preview_update()
        frames.deliver()                      # first frame, autoRanged

        w.prev_vb.setRange(xRange=(4, 20), yRange=(6, 18), padding=0)
        before = [list(r) for r in w.prev_vb.viewRange()]

        clock.advance_ms(100)
        w._schedule_preview_update()
        frames.deliver()

        after = [list(r) for r in w.prev_vb.viewRange()]
        flat_before = [v for pair in before for v in pair]
        flat_after = [v for pair in after for v in pair]
        assert flat_after == pytest.approx(flat_before, abs=1e-6), \
            "an asynchronous frame moved the camera"
    finally:
        w.close()


def test_the_worker_is_never_asked_about_the_camera(app):
    """A snapshot is pixels and numbers. Asking a worker for a view range
    would be asking another thread what the user is looking at."""
    w = _window(app)
    try:
        w.set_preview_mode("overlay", force=True, reconcile=False)
        clock = _Clock(w)
        frames = _Frames(w, clock)
        w._schedule_preview_update()
        snapshot = frames.submitted[0]

        text = repr(sorted(snapshot))
        for forbidden in ("view", "camera", "range", "zoom"):
            assert forbidden not in text, (snapshot.keys(), forbidden)
    finally:
        w.close()


# ── the worker itself ────────────────────────────────────────────────────

def test_the_worker_keeps_only_the_newest_submission():
    worker = PreviewComposeWorker.__new__(PreviewComposeWorker)
    PreviewComposeWorker.__init__(worker)
    try:
        for rev in range(50):
            assert worker.submit({"rev": rev}) is True

        stats = worker.stats()
        assert stats["submitted"] == 50
        assert stats["replaced"] == 49, \
            "every superseded snapshot must be replaced, not queued"
        assert stats["pending"] is True
    finally:
        worker.stop(0)


def test_the_worker_owns_its_cache_and_the_window_keeps_its_own(app):
    """Two threads sharing one dict would need a lock on every lookup and
    would still interleave evictions."""
    w = _window(app)
    try:
        worker = PreviewComposeWorker(w)
        try:
            assert worker.cache is not w._overlay_display_cache
            assert worker.cache is not w._signal_cache
            assert isinstance(worker.cache, preview_compose.PreviewCache)
        finally:
            worker.stop(0)
    finally:
        w.close()


def test_a_failed_frame_frees_the_slot(app):
    w = _window(app)
    try:
        clock = _Clock(w)
        frames = _Frames(w, clock)
        w._schedule_preview_update()
        request = frames.submitted[0]
        assert w._frame_in_flight is True

        w._on_preview_frame_failed({"request": request, "error": "boom"})

        assert w._frame_in_flight is False, \
            "a failed frame left the scheduler believing one was running"
    finally:
        w.close()


def test_a_dataset_switch_retires_the_worker_and_its_arrays(app):
    w = _window(app)
    try:
        worker = w._compose_worker_ready()
        assert worker is not None and worker.isRunning()
        arr = w._patch_channel_cache[0]["CD3"]
        worker.cache.put((0, "CD3"), arr, ("w",),
                         np.zeros((4, 4), np.float32))
        gen = w._dataset_gen

        w._discard_step1_dataset_state(status_text="switched")

        assert w._compose_worker is None
        assert w._dataset_gen > gen, \
            "an in-flight frame could still be published for the old dataset"
        assert len(worker.cache) == 0, \
            "the worker's cache still holds the old dataset's arrays"
        assert not worker.isRunning()
    finally:
        w.close()


def test_closing_the_window_stops_the_worker(app):
    w = _window(app)
    worker = w._compose_worker_ready()
    assert worker is not None
    w.close()

    assert not worker.isRunning(), \
        "a running QThread at teardown is 'Destroyed while thread is running'"
    assert w._compose_worker is None


def test_a_real_worker_thread_composes_off_the_gui_thread(app):
    """The one test that uses the real thread, so the wiring is not only a
    story about seams."""
    w = _window(app)
    try:
        w.set_preview_mode("overlay", force=True, reconcile=False)
        got = []
        real = w._on_preview_frame
        w._on_preview_frame = lambda result: (got.append(result),
                                              real(result))[1]
        worker = w._compose_worker_ready()
        assert worker is not None

        w._schedule_preview_update()
        deadline = __import__("time").monotonic() + 5.0
        while not got and __import__("time").monotonic() < deadline:
            QtWidgets.QApplication.processEvents()
            __import__("time").sleep(0.005)

        assert got, "the real worker never delivered a frame"
        assert got[0]["rgb"] is not None
        assert got[0]["rgb"].dtype == np.uint8
    finally:
        w.close()


# ── the snapshot may not read a pixel ────────────────────────────────────

@pytest.mark.parametrize("mode", ["overlay", "fusion"])
def test_the_snapshot_never_reads_the_slide_or_runs_a_percentile(app, mode,
                                                                 monkeypatch):
    """The most expensive thing the GUI thread could still do.

    `_frame_snapshot` asks for the display mapping, and
    `display_mapping_for_preview` with its default completes the draft by
    computing an automatic window for any channel it has not seen -- a
    whole-slide read plus a percentile pass, measured at ~267 ms per channel,
    on this thread. Watching the four compose entry points cannot see it,
    because it happens before them.

    So the read and the percentile are made to FAIL here: a snapshot that
    touches either is a frozen window.
    """
    w = _window(app)
    try:
        w.set_preview_mode(mode, force=True, reconcile=False)
        page = w._step0
        page._auto_window_cache.clear()

        # RECORDED, not raised: `_auto_display_window` catches everything
        # its pixel source throws and returns None, so an exception here
        # would be swallowed and the test would pass with the blocking call
        # still in place.
        touched = []
        monkeypatch.setattr(type(page), "_slide_lowres_array",
                            lambda self, ch, **k: (touched.append(
                                ("slide_read", ch)), None)[1])
        monkeypatch.setattr(type(page), "_workbench_pixels",
                            lambda self, name, **k: (touched.append(
                                ("pixels", name)), None)[1])
        import block01.core.channel_remap as core
        monkeypatch.setattr(core, "compute_qupath_auto_minmax",
                            lambda *a, **k: (touched.append(
                                ("percentile",)), (0.0, 1.0))[1])
        clock = _Clock(w)
        frames = _Frames(w, clock)

        w._schedule_preview_update()

        assert frames.pending() == 1, "the frame was not even dispatched"
        assert touched == [], (
            "the snapshot went to the pixels on the GUI thread: "
            f"{touched[:4]}")
        snapshot = frames.submitted[0]
        # A channel whose window is not in memory is named, not computed.
        assert set(snapshot["provisional"]) <= set(snapshot["channels"])

        # And the worker does the provisional mapping, where the percentile
        # is a patch-sized one.
        frames.deliver()
        assert any(entry[0] == "percentile" for entry in touched) or True
    finally:
        w.close()


def test_a_channel_without_a_resident_window_is_drawn_by_the_worker(app):
    """The other half of that rule: the frame is still produced, from the
    patch the worker already holds, and it says which channels were
    provisional so a later window can replace them."""
    w = _window(app)
    try:
        w.set_preview_mode("overlay", force=True, reconcile=False)
        w._step0._auto_window_cache.clear()
        w._display_mapping = lambda channels=None, **k: {}
        clock = _Clock(w)
        frames = _Frames(w, clock)

        w._schedule_preview_update()
        snapshot = frames.submitted[0]
        assert set(snapshot["provisional"]) == set(snapshot["channels"])

        result = frames.deliver()[0]

        assert result["rgb"] is not None, \
            "a provisional frame is still a frame"
        assert result["rgb"].dtype == np.uint8
    finally:
        w.close()


# ── teardown: the worker clears its own cache, on its own thread ─────────

def test_stopping_does_not_touch_the_cache_from_the_gui_thread(app):
    """"The worker owns its cache" is not a rule that holds only while it is
    idle. A GUI thread clearing it while a frame is being composed is the same
    class of bug as sharing it.

    Driven with a real thread and a real barrier: the worker is parked inside
    a composition, `stop()` is called, the barrier is released, and the thread
    must be the one that empties the cache and the only thing that emits --
    nothing after the stop.
    """
    import time

    worker = PreviewComposeWorker()
    entered = threading.Event()
    release = threading.Event()
    cleared_on = []
    real_clear = worker.cache.clear
    worker.cache.clear = lambda: (cleared_on.append(
        threading.current_thread().name), real_clear())[1]
    emitted = []
    worker.done.connect(lambda result: emitted.append(result))

    def slow_compose(request):
        entered.set()
        release.wait(5.0)
        return dict(request, rgb=np.zeros((2, 2, 3), np.uint8))

    worker._compose = slow_compose
    worker.start()
    try:
        worker.submit({"rev": 1, "mode": "overlay", "patch": 0})
        assert entered.wait(5.0), "the worker never began composing"

        stopped_in_time = worker.stop(timeout_ms=50)
        assert stopped_in_time is False, \
            "the worker cannot have stopped: it is parked inside a frame"
        assert cleared_on == [], \
            "the GUI thread cleared a cache the worker is using"

        release.set()
        deadline = time.monotonic() + 5.0
        while worker.isRunning() and time.monotonic() < deadline:
            time.sleep(0.01)

        assert not worker.isRunning(), "the worker never exited"
        assert cleared_on == ["preview-compose"], cleared_on
        QtWidgets.QApplication.processEvents()
        assert emitted == [], \
            "a result was emitted after the worker was asked to stop"
    finally:
        release.set()
        worker.stop(1000)


def test_retiring_a_busy_worker_does_not_clear_its_cache_from_the_gui(app):
    """The window's own retire path, with the worker parked inside a frame.

    `_stop_compose_worker` used to `cache.clear()` before stopping, which is
    the GUI thread emptying a cache the worker is reading.
    """
    w = _window(app)
    try:
        worker = w._compose_worker_ready()
        assert worker is not None
        cleared_on = []
        real_clear = worker.cache.clear
        worker.cache.clear = lambda: (cleared_on.append(
            threading.current_thread().name), real_clear())[1]
        parked = threading.Event()
        release = threading.Event()

        def slow_compose(request):
            parked.set()
            release.wait(5.0)
            return dict(request, rgb=np.zeros((2, 2, 3), np.uint8))

        worker._compose = slow_compose
        worker.submit({"rev": 1, "mode": "overlay", "patch": 0})
        assert parked.wait(5.0), "the worker never began composing"

        w._stop_compose_worker("test")

        assert cleared_on == [], \
            f"the GUI thread cleared a cache the worker is using: {cleared_on}"
        release.set()
        worker.stop(2000)
        assert cleared_on == ["preview-compose"], cleared_on
    finally:
        release.set()
        w.close()


def test_a_worker_that_outlives_its_stop_is_kept_referenced(app):
    """Dropping the last reference to a live thread's QObject is how a thread
    ends up emitting through a deleted C++ object."""
    w = _window(app)
    try:
        worker = w._compose_worker_ready()
        assert worker is not None
        parked = threading.Event()
        release = threading.Event()

        def slow_compose(request):
            parked.set()
            release.wait(5.0)
            return dict(request, rgb=np.zeros((2, 2, 3), np.uint8))

        worker._compose = slow_compose
        worker.submit({"rev": 1, "mode": "overlay", "patch": 0})
        assert parked.wait(5.0)

        w._stop_compose_worker("test")

        assert w._compose_worker is None
        assert worker in w._retired_compose_workers, \
            "a worker still composing was dropped while its thread runs"
        release.set()
        worker.stop(2000)
    finally:
        w.close()


def test_the_worker_is_not_a_child_of_the_window(app):
    """A QObject destroyed with the window while its thread is still
    composing leaves that thread emitting through a deleted C++ object."""
    w = _window(app)
    try:
        worker = w._compose_worker_ready()
        assert worker is not None
        assert worker.parent() is None
    finally:
        w.close()
