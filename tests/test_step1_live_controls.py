"""The controls the user actually touches change the pixels actually shown.

These tests drive the REAL widgets — the slider and spin box inside the channel
row, the Intensity window's own parameter change — and read the REAL ImageItem
afterwards, letting the REAL coalescing timer fire. Asserting that a fusion
helper returns different numbers proves nothing about a screen that never got
them: the failure being pinned here was exactly that, a live control whose
value moved while the picture did not.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import os
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets  # noqa: E402

import block01.ui.main_window as mw  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Loader:
    filepath = "/tmp/dataset.ome.tiff"
    shape = (128, 128)

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
        rng = np.random.default_rng(abs(hash(channel)) % 997)
        return rng.random(((y1 - y0) or 1, (x1 - x0) or 1), dtype=np.float32)


def _window(app):
    from block01.ui.main_window import MainWindow
    w = MainWindow()
    w.loader = _Loader()
    w.config.set_channels(w.loader.channel_names())
    w.config.load_panel({"markers": {"CD3": 0.0, "CD8": 0.0}}, "DAPI")
    w.config.set_nucleus("DAPI", 1.0)
    w._all_patches = [(0, 32, 0, 32)]
    w._preview_patch_idx = 0
    rng = np.random.default_rng(5)
    w._patch_channel_cache[0] = {
        ch: rng.random((32, 32), dtype=np.float32) * 0.8 + 0.1
        for ch in ("DAPI", "CD3", "CD8")}
    w._patch_load_ready.add(0)
    w.config.set_channel_visible("CD3", True)          # in the configuration
    return w


def _settle(w, timeout=2.0):
    """Let the coalesced publication happen, as the event loop would."""
    deadline = time.monotonic() + timeout
    while w._prev_timer.isActive() and time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.005)
    QtWidgets.QApplication.processEvents()


def _screen(w):
    """What is on the ImageItem right now."""
    return None if w.prev_img.image is None else np.asarray(w.prev_img.image).copy()


class _FakeClock:
    """Two seconds of input without waiting two seconds.

    Replaces the window's two scheduler seams: the clock it measures slots
    against, and the timer it arms. `run_due_slot` is the event loop's part
    -- deliver the timeout when the wait has elapsed -- so a test can drive
    hundreds of inputs and know exactly which frames the scheduler chose to
    publish.
    """

    def __init__(self, w, start=0.0, idle_ms=10_000):
        self._w = w
        self.now = float(start)
        self.armed_at = None
        self.armed_wait = None
        self.arms = []
        w._frame_now = lambda: self.now
        w._arm_frame_timer = self._arm
        w._prev_timer.stop()
        w._prev_timer.isActive = lambda: self.armed_at is not None
        # Stopping the timer disarms the slot, here as in Qt -- otherwise a
        # reset leaves this double claiming a frame is still due.
        w._prev_timer.stop = self._disarm
        # The window's scheduler state is on the REAL clock until now; move
        # it onto this one, `idle_ms` in the past, so the first input is
        # treated as arriving after an idle period rather than after a frame
        # published in the year the monotonic clock started.
        w._frame_last_publish = self.now - float(idle_ms) / 1000.0
        w._frame_input_at = w._frame_last_publish
        w._frame_pending_rev = None
        w._frame_coalesced = 0
        w._frame_in_flight = False

    def _disarm(self):
        self.armed_at = self.armed_wait = None

    def _arm(self, wait_ms):
        self.armed_at = self.now
        self.armed_wait = float(wait_ms)
        self.arms.append(float(wait_ms))

    def advance_ms(self, ms):
        self.now += float(ms) / 1000.0

    def due(self):
        return (self.armed_at is not None
                and (self.now - self.armed_at) * 1000.0 >= self.armed_wait)

    def run_due_slot(self):
        """Fire the armed slot if its wait has elapsed, as Qt would."""
        if not self.due():
            return False
        self.armed_at = self.armed_wait = None
        self._w._apply_pending_preview_update()
        return True

    def run_all_slots(self, limit=10000):
        fired = 0
        while self.armed_at is not None and fired < limit:
            self.advance_ms(self.armed_wait)
            if not self.run_due_slot():
                break
            fired += 1
        return fired


def _drag(w, channel, values):
    """Move the REAL slider through `values` (0..1), as a drag does."""
    row = w.config._rows[channel]
    for value in values:
        row.slider.setValue(int(round(float(value) * 100)))


@pytest.mark.parametrize("mode", ["overlay", "fusion"])
def test_the_weight_slider_changes_the_picture_on_screen(app, mode):
    w = _window(app)
    try:
        w.set_preview_mode(mode, force=True, reconcile=False)
        _drag(w, "CD3", [1.0])
        _settle(w)
        full = _screen(w)
        assert full is not None

        _drag(w, "CD3", [0.25])
        _settle(w)
        quarter = _screen(w)

        _drag(w, "CD3", [0.0])
        _settle(w)
        none_at_all = _screen(w)

        assert not np.array_equal(full, quarter)
        assert not np.array_equal(quarter, none_at_all)
        # And the tick is where the user left it, at every step.
        assert "CD3" in w.config.visible_channels()
    finally:
        w.close()


@pytest.mark.parametrize("mode", ["overlay", "fusion"])
def test_a_drag_publishes_on_the_clock_and_ends_on_its_final_value(app, mode):
    """REWRITTEN, because the contract changed and the old one is what the
    complaint was about.

    It used to assert "nothing drawn mid-drag, one frame after the hand
    stops", and the implementation delivered exactly that: a 60 ms
    single-shot timer restarted by every input, so a 2-3 second drag
    published 25 frames in 11.4 s (2.2 FPS, measured) -- only where an input
    gap happened to exceed 60 ms. The picture was not lagging the mouse; it
    was waiting for the mouse to stop.

    Now: the drag publishes intermediate frames on a fixed clock, far fewer
    frames than there were inputs, and the last frame is the value the drag
    ended on. Still no channel re-read -- a weight or window change is
    arithmetic on pixels already in hand.
    """
    w = _window(app)
    try:
        w.set_preview_mode(mode, force=True, reconcile=False)
        _drag(w, "CD3", [1.0])
        _settle(w)

        published = []
        real = w.prev_img.setImage
        w.prev_img.setImage = lambda img, **k: (
            published.append(np.asarray(img).copy()) or real(img, **k))
        reads = len(w.loader.reads)

        clock = _FakeClock(w, start=w._frame_last_publish)
        values = [0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.15, 0.1]
        for value in values:
            clock.advance_ms(20)               # 50 inputs a second
            _drag(w, "CD3", [value])
            clock.run_due_slot()

        assert 1 < len(published) < len(values), (
            f"{len(published)} frames for {len(values)} inputs: a drag should "
            "publish on the clock, neither per input nor only at the end")

        clock.advance_ms(mw.PREVIEW_FRAME_MS + 1)
        clock.run_due_slot()
        assert w.config.channel_weight("CD3") == pytest.approx(0.1)
        assert len(w.loader.reads) == reads    # and no channel was re-read

        # The last frame published is the state the drag ended on.
        w._refresh_patch_preview(reset_view=False)
        assert np.array_equal(published[-1], _screen(w))
    finally:
        w.close()


@pytest.mark.parametrize("mode", ["overlay", "fusion"])
def test_the_intensity_window_changes_the_picture_on_screen(app, mode, monkeypatch):
    """Driven through Step0's own signal, the one the workbench emits."""
    w = _window(app)
    try:
        window = {"CD3": {"min": 0.0, "max": 1.0, "gamma": 1.0},
                  "DAPI": {"min": 0.0, "max": 1.0, "gamma": 1.0}}
        monkeypatch.setattr(type(w), "_display_mapping",
                            lambda self, *a, **k: window)
        w.set_preview_mode(mode, force=True, reconcile=False)
        _drag(w, "CD3", [1.0])
        _settle(w)
        wide = _screen(w)
        assert wide is not None

        window["CD3"] = {"min": 0.0, "max": 0.3, "gamma": 1.0}
        w._step0.display_mapping_changed.emit("CD3")
        _settle(w)

        assert not np.array_equal(wide, _screen(w))
    finally:
        w.close()


def test_five_mapping_changes_publish_one_picture(app, monkeypatch):
    w = _window(app)
    try:
        window = {"CD3": {"min": 0.0, "max": 1.0, "gamma": 1.0}}
        monkeypatch.setattr(type(w), "_display_mapping",
                            lambda self, *a, **k: window)
        w.set_preview_mode("fusion", force=True, reconcile=False)
        _drag(w, "CD3", [1.0])
        _settle(w)

        published = []
        real = w.prev_img.setImage
        w.prev_img.setImage = lambda img, **k: (
            published.append(1) or real(img, **k))

        for top in (0.9, 0.7, 0.5, 0.4, 0.3):
            window["CD3"] = {"min": 0.0, "max": top, "gamma": 1.0}
            w._step0.display_mapping_changed.emit("CD3")
        assert published == []
        _settle(w)

        assert len(published) == 1
    finally:
        w.close()


def test_editing_a_channel_that_is_not_in_the_picture_redraws_nothing(app, monkeypatch):
    """CD8 is neither ticked nor current: its window is nobody's business
    here, and redrawing for it would be work with nothing to show."""
    w = _window(app)
    try:
        window = {"CD8": {"min": 0.0, "max": 1.0, "gamma": 1.0}}
        monkeypatch.setattr(type(w), "_display_mapping",
                            lambda self, *a, **k: window)
        w.set_preview_mode("overlay", force=True, reconcile=False)
        _drag(w, "CD3", [1.0])
        _settle(w)

        published = []
        real = w.prev_img.setImage
        w.prev_img.setImage = lambda img, **k: (
            published.append(1) or real(img, **k))

        window["CD8"] = {"min": 0.0, "max": 0.2, "gamma": 1.0}
        w._step0.display_mapping_changed.emit("CD8")
        _settle(w)

        assert published == []
    finally:
        w.close()


def test_a_weight_drag_starts_no_worker(app):
    w = _window(app)
    try:
        started = []
        w._start_loader_for = lambda idx, needed=None: started.append(needed)
        w.set_preview_mode("fusion", force=True, reconcile=False)

        _drag(w, "CD3", [0.9, 0.5, 0.2])
        _settle(w)

        assert started == []            # everything it needs is already cached
        assert w._fusion_worker is None
    finally:
        w.close()


# ── the frame clock ──────────────────────────────────────────────────────
#
# MEASURED before this slice, over 653 `intensity.in` events on the real
# desk: 25 frames in 11.435 s of overlay dragging (2.2 FPS) and 28 in 9.677 s
# of fusion dragging (2.9 FPS), with the last input reaching the screen 131 ms
# / 101 ms later on average. Not because a frame was slow -- the whole frame
# was 42-85 ms -- but because a 60 ms single-shot timer was RESTARTED by
# every input, so a publish happened only where the hand paused longer than
# 60 ms. The rules below are what replaces that.

def test_the_first_input_draws_at_once(app):
    """A leading edge. It used to wait 60 ms for a single slider step, and
    for the first step of every drag."""
    w = _window(app)
    try:
        w.set_preview_mode("overlay", force=True, reconcile=False)
        clock = _FakeClock(w)              # a slot is due after the idle
        published = []
        w.prev_img.setImage = lambda img, **k: published.append(1)

        w._schedule_preview_update()

        assert published == [1], "the first input after an idle wait was held"
        assert clock.arms == [], "and it did not need a timer at all"
    finally:
        w.close()


def test_continuous_input_publishes_on_fixed_slots(app):
    """Two seconds of dragging at 200 inputs a second, on a virtual clock.

    What must NOT happen is the old behaviour -- one frame, at the end --
    and what must not happen either is a frame per input.
    """
    w = _window(app)
    try:
        w.set_preview_mode("overlay", force=True, reconcile=False)
        clock = _FakeClock(w, start=0.0)
        clock.advance_ms(1000)
        published = []
        w.prev_img.setImage = lambda img, **k: published.append(1)

        inputs = 0
        for _ in range(400):               # 2 s at 5 ms per input
            clock.advance_ms(5)
            w._schedule_preview_update()
            inputs += 1
            clock.run_due_slot()

        slots = 2000.0 / mw.PREVIEW_FRAME_MS
        assert 0.5 * slots <= len(published) <= 1.2 * slots, (
            f"{len(published)} frames in 2 s of input; a {mw.PREVIEW_FRAME_MS} "
            f"ms clock should give about {slots:.0f}")
        assert len(published) < inputs / 4, \
            "a frame per input is not a frame clock either"
    finally:
        w.close()


def test_the_pending_queue_is_never_deeper_than_one(app):
    """653 inputs may not become 653 frames of history. What is pending is a
    revision, not a queue."""
    w = _window(app)
    try:
        w.set_preview_mode("fusion", force=True, reconcile=False)
        clock = _FakeClock(w, start=0.0)
        depths = []
        for i in range(653):
            clock.advance_ms(2)
            w._schedule_preview_update()
            depths.append(0 if w._frame_pending_rev is None else 1)
            if i % 17 == 0:
                clock.advance_ms(mw.PREVIEW_FRAME_MS)
                clock.run_due_slot()

        assert max(depths) == 1
        assert w._frame_input_rev >= 653, \
            "every input was counted, however few frames were drawn"
    finally:
        w.close()


def test_a_frame_slower_than_the_input_keeps_one_in_flight_and_one_pending(app):
    """When computing is slower than the hand, the answer is to skip, not to
    queue: one frame being drawn, one revision waiting, and no history."""
    w = _window(app)
    try:
        w.set_preview_mode("overlay", force=True, reconcile=False)
        clock = _FakeClock(w, start=0.0)
        clock.advance_ms(1000)
        frames = []          # (revision being drawn, pending when it began)

        def slow_frame(*_a, **_k):
            # Recorded BEFORE the nested input, so the pair says what this
            # frame is drawing and what was waiting when it started.
            frames.append((w._frame_drawing_rev, w._frame_pending_rev))
            clock.advance_ms(80)                  # a frame costs 80 ms
            # input arriving DURING the frame must not start a second one
            w._schedule_preview_update()
            assert w._frame_in_flight is True

        w._refresh_patch_preview = slow_frame

        for _ in range(20):
            clock.advance_ms(10)
            w._schedule_preview_update()
            clock.run_due_slot()

        assert len(frames) > 1, f"only {len(frames)} frames ran in 200 ms of input"
        for drawing, pending in frames:
            assert drawing is not None, frames
            assert pending is None, (
                "a frame started while another revision was still pending: "
                f"drawing {drawing}, pending {pending}")
        drawn = [drawing for drawing, _ in frames]
        assert drawn == sorted(drawn) and len(set(drawn)) == len(drawn), \
            f"frames were drawn out of order or twice: {drawn}"
        assert max(drawn) < w._frame_input_rev, \
            "every input was drawn: this test is not exercising a slow frame"
        assert w._frame_pending_rev is not None, \
            "the newest input is still waiting for its slot"
        assert w._frame_in_flight is False
    finally:
        w.close()


@pytest.mark.parametrize("mode", ["overlay", "fusion"])
def test_the_final_value_is_always_published(app, mode):
    """The hand stops. Whatever the clock was doing, the last revision has to
    reach the screen."""
    w = _window(app)
    try:
        w.set_preview_mode(mode, force=True, reconcile=False)
        clock = _FakeClock(w, start=0.0)
        clock.advance_ms(1000)
        revs = []
        real = w._refresh_patch_preview
        w._refresh_patch_preview = lambda *a, **k: (
            revs.append(w._frame_input_rev), real(*a, **k))[1]

        for _ in range(12):
            clock.advance_ms(4)               # faster than the clock
            w._schedule_preview_update()
            clock.run_due_slot()
        last_input = w._frame_input_rev
        clock.run_all_slots()

        assert revs, "nothing was published"
        assert revs[-1] == last_input, (
            f"the drag ended at revision {last_input} and the screen stopped "
            f"at {revs[-1]}")
        assert w._frame_pending_rev is None
    finally:
        w.close()


def test_an_explicit_delay_is_a_floor_not_a_reset(app):
    """A dataset switch asking for 60 ms may not push back a slot that is
    already due -- that is the trailing-debounce bug in miniature."""
    w = _window(app)
    try:
        clock = _FakeClock(w, start=0.0)
        clock.advance_ms(10_000)
        published = []
        w.prev_img.setImage = lambda img, **k: published.append(1)

        w._schedule_preview_update(delay_ms=mw.PREVIEW_COALESCE_MS)
        assert published == [], "an explicit delay was ignored"
        assert clock.arms == [float(mw.PREVIEW_COALESCE_MS)], clock.arms

        clock.advance_ms(mw.PREVIEW_COALESCE_MS)
        clock.run_due_slot()
        assert published == [1]
    finally:
        w.close()


def test_input_during_a_frame_rides_the_next_slot(app):
    w = _window(app)
    try:
        clock = _FakeClock(w, start=0.0)
        clock.advance_ms(1000)
        arms_before = list(clock.arms)
        real = w._refresh_patch_preview

        def reenter(*a, **k):
            w._schedule_preview_update()      # Qt can deliver this here
            return real(*a, **k)

        w._refresh_patch_preview = reenter
        w._schedule_preview_update()

        assert w._frame_pending_rev is not None
        assert clock.arms != arms_before, \
            "the input that arrived during the frame armed no slot"
        assert clock.arms[-1] == float(mw.PREVIEW_FRAME_MS)
    finally:
        w.close()


def test_the_published_frame_records_what_it_merged(app):
    """The evidence the report needs: which revision was drawn, how many
    inputs were merged into it, and how late the newest input was."""
    from block01.utils import perf_trace

    w = _window(app)
    try:
        clock = _FakeClock(w, start=0.0)
        clock.advance_ms(1000)
        lines = []
        os.environ["BLOCK01_PERF"] = "1"
        try:
            perf_trace.WRITER.submit = lambda rec: lines.append(rec) or True
            # The first input publishes at once (leading edge); the four that
            # follow are merged into the slot after it.
            w._schedule_preview_update()
            lines.clear()
            for _ in range(4):
                clock.advance_ms(4)
                w._schedule_preview_update()
            clock.advance_ms(mw.PREVIEW_FRAME_MS)
            clock.run_due_slot()
        finally:
            os.environ.pop("BLOCK01_PERF", None)
            del perf_trace.WRITER.submit

        publishes = [rec for rec in lines if rec[2] == "step1.publish"]
        assert len(publishes) == 1, [rec[2] for rec in lines]
        fields = publishes[0][4]
        assert fields["coalesced"] == 3, fields
        assert fields["rev"] == fields["input_rev"], fields
        assert "latency_ms" in fields
        schedules = [rec for rec in lines if rec[2] == "step1.schedule"]
        assert len(schedules) == 4, "every input is recorded, drawn or not"
    finally:
        w.close()


def test_a_frame_cannot_start_inside_a_frame(app):
    """Drawing runs Qt code, and Qt can deliver a queued timeout inside it --
    `processEvents`, a dialog, a nested event loop. A second frame starting
    there would composite the same arrays twice and, worse, publish out of
    order."""
    w = _window(app)
    try:
        clock = _FakeClock(w)
        depth = []
        real = w._refresh_patch_preview

        def reenter(*a, **k):
            depth.append(len(depth) + 1)
            # exactly what a delivered timeout would do
            w._schedule_preview_update()
            w._apply_pending_preview_update()
            return real(*a, **k)

        w._refresh_patch_preview = reenter
        w._schedule_preview_update()

        assert depth == [1], f"{len(depth)} frames ran inside one another"
        assert w._frame_in_flight is False
        assert w._frame_pending_rev is not None, \
            "the input that arrived during the frame is still pending"
    finally:
        w.close()


def test_a_dataset_switch_forgets_the_frame_clock(app):
    """Stopping the timer is not enough. A pending revision, a merge count or
    an `in_flight` left standing from the old dataset makes the new one's
    first input believe a frame is already due -- or already running, which
    drops it silently -- and a time base from the old slide's last frame turns
    the first input into a mid-drag slot instead of a leading edge."""
    w = _window(app)
    try:
        clock = _FakeClock(w)
        w._schedule_preview_update()          # leading edge, publishes
        clock.advance_ms(4)
        w._schedule_preview_update()          # now pending, slot armed
        w._frame_in_flight = True             # as if a frame were running
        assert w._frame_pending_rev is not None

        w._reset_frame_clock()

        assert w._frame_pending_rev is None
        assert w._frame_drawing_rev is None
        assert w._frame_coalesced == 0
        assert w._frame_in_flight is False
        assert w._frame_last_publish == 0.0
        assert w._frame_input_at == 0.0
        assert w._preview_update_pending is False
        assert not w._prev_timer.isActive()
    finally:
        w.close()


def test_the_discard_path_resets_the_clock(app):
    """The real caller: `_discard_step1_dataset_state`."""
    w = _window(app)
    try:
        clock = _FakeClock(w)
        w._schedule_preview_update()
        clock.advance_ms(4)
        w._schedule_preview_update()
        assert w._frame_pending_rev is not None

        w._discard_step1_dataset_state(status_text="switched")

        assert w._frame_pending_rev is None
        assert w._frame_in_flight is False
        assert w._frame_last_publish == 0.0
    finally:
        w.close()


def test_a_slot_is_never_asked_for_early(app):
    """`int()` on a 32.7 ms wait asks for 32 and fires before the budget has
    elapsed; over a long drag that drifts the clock faster than the frame cost
    it was chosen against."""
    w = _window(app)
    try:
        asked = []
        w._arm_frame_timer = lambda ms: asked.append(ms)
        clock = _FakeClock(w)
        w._arm_frame_timer = lambda ms: asked.append(ms)
        w._frame_last_publish = clock.now - 0.0003      # 0.3 ms ago

        w._schedule_preview_update()

        assert asked, "no slot was asked for"
        assert asked[0] >= mw.PREVIEW_FRAME_MS - 0.3
        real = mw.MainWindow._arm_frame_timer
        waits = []
        w._prev_timer.start = lambda ms: waits.append(ms)
        real(w, 32.7)
        assert waits == [33], waits
    finally:
        w.close()
