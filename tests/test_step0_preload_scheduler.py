"""Reading patch pixels: on demand, prioritised, bounded, cancelled by identity.

MEASURED before this: drawing a patch restarted a sweep of every patch x every
conditioning channel after emptying the cache, so a second patch meant 2N reads
of which N were of pixels already in memory. This module pins the replacement's
rules with real reader threads and a read that can be held open, because every
one of them is about WHEN a read happens relative to another.

Own module: no Qt widgets here beyond the QObject the scheduler is, so it also
runs in a fraction of a page-heavy suite's time.
"""

import os
import threading

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets  # noqa: E402

from block01.workers.preload_scheduler import (  # noqa: E402
    BACKGROUND, FOREGROUND, PreloadScheduler,
)


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Reads:
    """A read that records, and can be held open on demand."""

    def __init__(self, size=4, hold=None):
        self.calls = []
        self.lock = threading.Lock()
        self.size = size
        self.hold = hold                  # channel name -> threading.Event
        self.started = threading.Event()

    def __call__(self, channel, y0, y1, x0, x1, normalize=False,
                 downsample=1):
        with self.lock:
            self.calls.append((channel, (y0, y1, x0, x1)))
        self.started.set()
        gate = (self.hold or {}).get(channel)
        if gate is not None:
            assert gate.wait(10.0), f"the {channel} read was never released"
        return np.full((self.size, self.size), float(len(channel)),
                       dtype=np.float32)

    def channels(self):
        with self.lock:
            return [c for c, _bbox in self.calls]


def _scheduler(app, reads, **kw):
    sched = PreloadScheduler(read=reads, **kw)
    sched.set_source(object(), 1)
    return sched


P1 = (0, 16, 0, 16)
P2 = (16, 32, 16, 32)


def test_the_same_pixels_are_read_once_and_then_served(app):
    reads = _Reads()
    sched = _scheduler(app, reads)
    try:
        assert sched.request(P1, "CD3") == "queued"
        assert sched.drain()
        assert sched.request(P1, "CD3") == "hit"
        assert reads.channels() == ["CD3"]
        assert sched.resident(P1, "CD3") is not None
    finally:
        sched.stop()


def test_a_second_patch_does_not_invalidate_the_first(app):
    """The whole point of keying on the bbox.

    P1's arrays are the same pixels from the same region whatever P2 is, and
    whatever INDEX P1 now has -- so drawing P2 reads P2 and nothing else.
    """
    reads = _Reads()
    sched = _scheduler(app, reads)
    try:
        sched.request_many([P1], ["DAPI", "CD3"], priority=BACKGROUND)
        assert sched.drain()
        assert sorted(reads.channels()) == ["CD3", "DAPI"]

        sched.request_many([P1, P2], ["DAPI", "CD3"], priority=BACKGROUND)
        assert sched.drain()

        # Exactly two more reads, both of the NEW patch.
        new = reads.calls[2:]
        assert len(new) == 2, reads.calls
        assert {bbox for _ch, bbox in new} == {P2}
        assert sched.resident(P1, "DAPI") is not None
    finally:
        sched.stop()


def test_the_foreground_request_overtakes_a_queued_background_one(app):
    """A speculative read may not be in front of the patch on screen.

    One reader, held inside a read that was already running: while it is
    stuck, eight background tiles queue up and then the current patch's
    channel is asked for. When the reader is released, THAT is what it takes
    next -- not the background work that was queued before it.
    """
    gate = threading.Event()
    reads = _Reads(hold={"HELD": gate})
    sched = _scheduler(app, reads, max_readers=1)
    try:
        sched.request(P1, "HELD", priority=BACKGROUND)
        assert reads.started.wait(5.0), "the reader never started"

        for i in range(8):
            sched.request((i * 100, i * 100 + 8, 0, 8), "BG%d" % i,
                          priority=BACKGROUND)
        assert sched.request(P2, "URGENT", priority=FOREGROUND) == "queued"

        gate.set()
        assert sched.drain(15_000)

        order = reads.channels()
        assert order[0] == "HELD"
        assert order[1] == "URGENT", (
            f"a background read went in front of the current patch: {order}")
    finally:
        gate.set()
        sched.stop()


def test_a_background_request_for_pixels_already_wanted_is_not_queued_twice(app):
    gate = threading.Event()
    reads = _Reads(hold={"HELD": gate})
    sched = _scheduler(app, reads, max_readers=1)
    try:
        sched.request(P1, "HELD", priority=BACKGROUND)
        assert reads.started.wait(5.0)
        assert sched.request(P2, "CD3", priority=BACKGROUND) == "queued"
        assert sched.request(P2, "CD3", priority=BACKGROUND) == "coalesced"
        # Wanted sooner: the same request moves to the foreground queue
        # rather than becoming a second read of the same pixels.
        assert sched.request(P2, "CD3", priority=FOREGROUND) == "coalesced"
        stats = sched.stats()
        assert stats["foreground_pending"] == 1, stats
        assert stats["background_pending"] == 0, stats
        gate.set()
        assert sched.drain(15_000)
        assert reads.channels().count("CD3") == 1
    finally:
        gate.set()
        sched.stop()


def test_the_backlog_is_bounded_and_the_drops_are_counted(app):
    gate = threading.Event()
    reads = _Reads(hold={"HELD": gate})
    sched = _scheduler(app, reads, max_readers=1, max_pending=4)
    try:
        sched.request(P1, "HELD", priority=BACKGROUND)
        assert reads.started.wait(5.0)
        outcomes = [sched.request((i, i + 8, 0, 8), "BG", priority=BACKGROUND)
                    for i in range(10)]
        assert outcomes.count("dropped") == 6, outcomes
        stats = sched.stats()
        assert stats["background_pending"] == 4
        assert stats["dropped"] == 6

        # A foreground request still gets in -- by displacing the OLDEST
        # speculative one, not by growing the queue.
        assert sched.request(P2, "URGENT", priority=FOREGROUND) == "queued"
        stats = sched.stats()
        assert stats["foreground_pending"] == 1
        assert stats["background_pending"] == 3
    finally:
        gate.set()
        sched.stop()


def test_no_more_readers_than_the_limit(app):
    """Two readers is two reads, whatever is asked for.

    Proved with FOREGROUND work: speculative reads can never reach the cap on
    their own, because one reader is reserved and none of them starts while
    the current patch is waiting (both below).
    """
    started = threading.Semaphore(0)
    release = threading.Event()
    live = {"now": 0, "max": 0}
    lock = threading.Lock()

    def read(channel, *_a, **_k):
        with lock:
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
        started.release()
        release.wait(10.0)
        with lock:
            live["now"] -= 1
        return np.zeros((2, 2), np.float32)

    sched = _scheduler(app, read, max_readers=2)
    try:
        for i in range(6):
            sched.request((i, i + 4, 0, 4), "CH%d" % i, priority=FOREGROUND)
        assert started.acquire(timeout=5.0)
        assert started.acquire(timeout=5.0)
        release.set()
        assert sched.drain(15_000)
        assert live["max"] == 2, live
        assert sched.stats()["max_readers"] == 2
    finally:
        release.set()
        sched.stop()


def test_one_reader_is_always_left_for_the_current_patch(app):
    """The freeze this rule removes: both readers inside speculative reads
    when the user finishes a new patch. A read cannot be cancelled once it is
    in the loader, so the next patch would wait for one to finish."""
    gate = threading.Event()
    reads = _Reads(hold={"BG0": gate, "BG1": gate, "URGENT": None})
    sched = _scheduler(app, reads, max_readers=2)
    try:
        sched.request((0, 8, 0, 8), "BG0", priority=BACKGROUND)
        sched.request((8, 16, 0, 8), "BG1", priority=BACKGROUND)
        assert reads.started.wait(5.0)
        import time
        time.sleep(0.05)                  # give a second reader its chance
        stats = sched.stats()
        assert stats["background_in_flight"] == 1, stats

        # The current patch arrives and is read AT ONCE, by the reserved
        # reader, while the speculative one is still stuck.
        sched.request(P2, "URGENT", priority=FOREGROUND)
        deadline = time.monotonic() + 5.0
        while (sched.resident(P2, "URGENT") is None
               and time.monotonic() < deadline):
            time.sleep(0.005)
        assert sched.resident(P2, "URGENT") is not None, (
            "the current patch waited for a speculative read: "
            f"{sched.stats()}")
        assert reads.channels().count("BG1") == 0
    finally:
        gate.set()
        sched.stop()


def test_nothing_speculative_starts_while_the_current_patch_is_waiting(app):
    """"Foreground first" has to mean first in TIME. A background read that
    starts a microsecond before the foreground request is taken is a
    foreground request waiting on an uncancellable read."""
    gate = threading.Event()
    reads = _Reads(hold={"HELD": gate})
    sched = _scheduler(app, reads, max_readers=2)
    try:
        sched.request(P1, "HELD", priority=FOREGROUND)
        assert reads.started.wait(5.0)
        for i in range(4):
            sched.request((i, i + 8, 0, 8), "BG%d" % i, priority=BACKGROUND)
        import time
        time.sleep(0.10)
        assert reads.channels() == ["HELD"], (
            f"a speculative read started while the patch was waiting: "
            f"{reads.channels()}")
        stats = sched.stats()
        assert stats["background_in_flight"] == 0, stats
        gate.set()
        assert sched.drain(15_000)
        assert len(reads.channels()) == 5
    finally:
        gate.set()
        sched.stop()


def test_an_array_bigger_than_the_whole_budget_is_not_cached(app):
    """The eviction loop keeps at least one entry, so a single oversized
    array would sit above the declared ceiling for good."""
    reads = _Reads(size=256)              # 256x256 float32 = 256 KiB
    sched = _scheduler(app, reads, max_bytes=64 * 1024)
    try:
        sched.request(P1, "HUGE", priority=FOREGROUND)
        assert sched.drain()
        assert sched.resident(P1, "HUGE") is None
        assert sched.nbytes() <= 64 * 1024
        assert sched.stats()["oversized"] == 1
    finally:
        sched.stop()


def test_a_read_in_flight_when_the_dataset_changes_is_not_kept(app):
    """Cancellation is an identity, never a join.

    The GUI thread does not wait for this read -- `invalidate` returns at
    once, the read finishes on its own, and its array is dropped because the
    generation it belongs to is no longer current.
    """
    gate = threading.Event()
    reads = _Reads(hold={"SLOW": gate})
    sched = _scheduler(app, reads, max_readers=1)
    try:
        sched.request(P1, "SLOW", priority=FOREGROUND)
        assert reads.started.wait(5.0)

        import time
        t0 = time.monotonic()
        sched.invalidate(2)
        assert time.monotonic() - t0 < 0.2, "invalidate waited for the reader"

        gate.set()
        assert sched.drain(15_000)
        assert sched.resident(P1, "SLOW") is None
        assert sched.stats()["stale"] >= 1
        assert sched.dataset_gen() == 2
    finally:
        gate.set()
        sched.stop()


def test_queued_work_for_the_previous_dataset_is_never_read(app):
    gate = threading.Event()
    reads = _Reads(hold={"HELD": gate})
    sched = _scheduler(app, reads, max_readers=1)
    try:
        sched.request(P1, "HELD", priority=BACKGROUND)
        assert reads.started.wait(5.0)
        sched.request(P2, "OLD", priority=BACKGROUND)
        sched.invalidate(7)
        gate.set()
        assert sched.drain(15_000)
        assert "OLD" not in reads.channels()
    finally:
        gate.set()
        sched.stop()


def test_the_store_has_a_byte_ceiling(app):
    reads = _Reads(size=64)               # 64x64 float32 = 16 KiB
    sched = _scheduler(app, reads, max_bytes=40 * 1024)
    try:
        for i in range(5):
            sched.request((i, i + 64, 0, 64), "CH%d" % i, priority=BACKGROUND)
        assert sched.drain()
        assert sched.nbytes() <= 40 * 1024
        assert sched.stats()["evicted"] >= 1
        # The most recently used survives; the first one asked for is gone.
        assert sched.resident((4, 68, 0, 64), "CH4") is not None
        assert sched.resident((0, 64, 0, 64), "CH0") is None
    finally:
        sched.stop()


def test_a_hit_is_the_most_recently_used(app):
    reads = _Reads(size=64)
    sched = _scheduler(app, reads, max_bytes=40 * 1024)
    try:
        for i in range(2):
            sched.request((i, i + 64, 0, 64), "CH%d" % i, priority=BACKGROUND)
        assert sched.drain()
        assert sched.resident((0, 64, 0, 64), "CH0") is not None    # a hit
        sched.request((9, 73, 0, 64), "CH9", priority=BACKGROUND)
        assert sched.drain()
        # CH0 was used last, so CH1 is what the ceiling evicts.
        assert sched.resident((0, 64, 0, 64), "CH0") is not None
        assert sched.resident((1, 65, 0, 64), "CH1") is None
    finally:
        sched.stop()


def test_one_channel_can_be_dropped_without_dropping_the_others(app):
    reads = _Reads()
    sched = _scheduler(app, reads)
    try:
        sched.request_many([P1, P2], ["DAPI", "CD3"], priority=BACKGROUND)
        assert sched.drain()
        sched.drop_channel("CD3")
        assert sched.resident(P1, "CD3") is None
        assert sched.resident(P2, "CD3") is None
        assert sched.resident(P1, "DAPI") is not None
    finally:
        sched.stop()


def test_normalize_and_downsample_are_part_of_the_identity(app):
    reads = _Reads()
    sched = _scheduler(app, reads)
    try:
        sched.request(P1, "CD3", priority=BACKGROUND)
        assert sched.drain()
        assert sched.resident(P1, "CD3", normalize=True) is None
        assert sched.resident(P1, "CD3", downsample=4) is None
        assert sched.resident(P1, "CD3") is not None
    finally:
        sched.stop()


def test_stopping_does_not_wait_for_the_reader(app):
    gate = threading.Event()
    reads = _Reads(hold={"SLOW": gate})
    sched = _scheduler(app, reads, max_readers=1)
    try:
        sched.request(P1, "SLOW", priority=FOREGROUND)
        assert reads.started.wait(5.0)
        import time
        t0 = time.monotonic()
        sched.stop()
        assert time.monotonic() - t0 < 0.2, (
            "stop() joined a reader; that is the GUI freeze this replaces")
    finally:
        gate.set()
        sched.stop(timeout_ms=5000)
