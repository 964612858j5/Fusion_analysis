"""Scheduler activity snapshot + idle notification (viewer/scheduler.py).

The primitive Step0 needs: once its producers stop, learn ASYNCHRONOUSLY
when this scheduler's physical work is over. No owners and no parallel
ledger -- `_pending` is the single source of truth (see
`TileScheduler.activity_snapshot`).

Kept in its own module rather than appended to test_viewer_prototype.py
because that suite is already 1800 lines and these cases bring their own
fakes and thread choreography (gated warm-up, blocked provider reads,
callbacks on worker threads).

UNRESOLVED, recorded so nobody re-derives it: while these tests lived
inside test_viewer_prototype.py, a whole-session pytest run segfaulted
inside pyqtgraph's paint path in test_explore_controller. The current
layout has not reproduced it in six runs across both suite orderings, but
pytest still runs everything in ONE process, so the move is NOT a proven
cause or a proven fix -- it is INCONCLUSIVE. Nothing here touches Qt.
"""

import threading
import time

import numpy as np
import pytest

pytest.importorskip("tifffile")

from block01.viewer.caches import LRUByteCache  # noqa: E402
from block01.viewer.scheduler import TileScheduler  # noqa: E402
from block01.viewer.tile_types import (  # noqa: E402
    CorrectionKey,
    PixelBuffer,
    QualityLevel,
    RawKey,
    SourceIdentity,
    TileAddress,
    TileGridSpec,
    TileRequest,
    TileResult,
)


GRID = TileGridSpec(tile_size=512, source_chunk_shape=(), grid_version="v1")


def make_source(path="/x/dataset.ome.tif", fp="1:1", stage="raw"):
    return SourceIdentity(dataset_path=path, dataset_fingerprint=fp, stage=stage)


def make_tile(level=0, tx=0, ty=0, grid=GRID):
    return TileAddress(grid=grid, level=level, tx=tx, ty=ty)


def make_ckey(params=(25,), tx=0, ty=0, quality=QualityLevel.INTERACTIVE):
    return CorrectionKey(source=make_source(), channel="DAPI",
                         tile=make_tile(tx=tx, ty=ty), method="tophat",
                         params=params, algorithm_version="v1", quality=quality)


class FakeProvider:
    """Only what the scheduler touches: a tile read and a warm-up hook."""

    def __init__(self):
        self.read_tile_calls = 0

    def warm_thread_handle(self, levels=(0,)):
        return True

    def read_tile(self, channel, tile):
        self.read_tile_calls += 1
        ts = tile.grid.tile_size
        return np.zeros((ts, ts), dtype=np.float32), 0.0

    def close(self):
        pass


class FakeCompute:
    def __init__(self, delay_event=None, started_event=None):
        self.calls = 0
        self.calls_lock = threading.Lock()
        self.delay_event = delay_event
        self.started_event = started_event

    def raw_keys_for(self, key):
        return []

    def compute(self, key):
        with self.calls_lock:
            self.calls += 1
        if self.started_event is not None:
            self.started_event.set()
        if self.delay_event is not None:
            self.delay_event.wait(timeout=5)
        arr = np.full((8, 8), float(key.params[0]), dtype=np.float32)
        req = TileRequest(key=key, generation=0, priority=0)
        pixels = PixelBuffer(residency="cpu", dtype="float32",
                             shape=arr.shape, handle=arr)
        return TileResult(request=req, pixels=pixels, quality=key.quality,
                          provisional=False, timing={}, error=None)


def new_scheduler(compute_workers=1, delay_event=None, started_event=None,
                  io_workers=1):
    provider = FakeProvider()
    compute = FakeCompute(delay_event=delay_event, started_event=started_event)
    raw_cache = LRUByteCache(10_000_000)
    corr_cache = LRUByteCache(10_000_000)
    sched = TileScheduler(provider, compute, raw_cache, corr_cache,
                          io_workers=io_workers, compute_workers=compute_workers)
    return sched, compute, raw_cache, corr_cache


def _shutdown_flag(sched):
    """Read `_shutdown` under the scheduler's own lock, so the test observes
    the same state the staging path will."""
    with sched._lock:
        return sched._shutdown


def _wait_until(predicate, timeout=5.0):
    """Poll a predicate with a deadline instead of sleeping a guessed
    interval. Returns True if it became true in time."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return predicate()


class _WarmProvider(FakeProvider):
    """FakeProvider whose per-thread warm-up is observable and can be made
    to block or to fail."""

    def __init__(self, gate=None, fail=False, **kwargs):
        super().__init__(**kwargs)
        self._gate = gate
        self._fail = fail
        self.warm_calls = 0
        self._warm_lock = threading.Lock()

    def warm_thread_handle(self, levels=(0,)):
        with self._warm_lock:
            self.warm_calls += 1
        if self._gate is not None:
            self._gate.wait(timeout=5)
        if self._fail:
            raise RuntimeError("warm exploded")
        return True

    def close(self):
        pass


def _scheduler_with_provider(provider, io_workers=2, compute_workers=1):
    compute = FakeCompute()
    raw_cache = LRUByteCache(10_000_000)
    corr_cache = LRUByteCache(10_000_000)
    sched = TileScheduler(provider, compute, raw_cache, corr_cache,
                          io_workers=io_workers, compute_workers=compute_workers)
    return sched, compute, raw_cache, corr_cache


def test_activity_warming_blocks_idle_until_warm_up_finishes():
    gate = threading.Event()
    provider = _WarmProvider(gate=gate)
    sched, _c, _rc, _cc = _scheduler_with_provider(provider, io_workers=2)
    try:
        assert _wait_until(lambda: provider.warm_calls == 2)
        snap = sched.activity_snapshot()
        assert snap["warming"] == 2 and snap["total"] == 0
        assert sched.idle() is False, "warming workers are activity"

        gate.set()
        assert _wait_until(lambda: sched.activity_snapshot()["warming"] == 0)
        assert sched.idle() is True
        assert sched.warmed_workers == 2, "success counter unchanged in meaning"
    finally:
        gate.set()
        sched.shutdown()


def test_activity_warming_reaches_zero_even_when_warm_up_raises():
    """A warm-up that throws must not leave the scheduler permanently
    non-idle -- which is why the decrement lives in a `finally`."""
    provider = _WarmProvider(fail=True)
    sched, _c, _rc, _cc = _scheduler_with_provider(provider, io_workers=2)
    try:
        assert _wait_until(lambda: sched.activity_snapshot()["warming"] == 0)
        assert sched.idle() is True
        assert sched.warmed_workers == 0, "no warm-up succeeded"
    finally:
        sched.shutdown()


def test_notify_when_idle_fires_immediately_and_once_when_already_idle():
    sched, _c, _rc, _cc = new_scheduler(io_workers=1)
    try:
        assert _wait_until(sched.idle)
        calls = []
        sched.notify_when_idle(lambda: calls.append("a"))
        assert calls == ["a"], "an already-idle scheduler calls back inline"
        assert _wait_until(lambda: len(calls) == 1, timeout=0.2)
    finally:
        sched.shutdown()


def test_notify_when_idle_waits_for_physical_completion():
    started, delay = threading.Event(), threading.Event()
    sched, _c, _rc, corr_cache = new_scheduler(delay_event=delay,
                                               started_event=started,
                                               io_workers=1)
    try:
        assert _wait_until(sched.idle)
        fired = threading.Event()
        sched.request(TileRequest(key=make_ckey(), generation=0, priority=0),
                      lambda r: None)
        assert started.wait(timeout=5)

        sched.notify_when_idle(fired.set)
        assert sched.activity_snapshot()["running"] == 1
        assert not fired.is_set(), "work is still physically running"

        delay.set()
        assert fired.wait(timeout=5)
        assert sched.idle() is True
    finally:
        delay.set()
        sched.shutdown()


def test_activity_counts_entries_not_waiters_under_dedup():
    started, delay = threading.Event(), threading.Event()
    sched, compute, _rc, _cc = new_scheduler(delay_event=delay,
                                             started_event=started,
                                             io_workers=1)
    try:
        key = make_ckey()
        for gen in range(4):
            sched.request(TileRequest(key=key, generation=gen, priority=0),
                          lambda r: None)
        assert started.wait(timeout=5)
        snap = sched.activity_snapshot()
        assert snap["total"] == 1, f"dedup collapses to one entry: {snap}"
        assert snap["running"] == 1 and snap["queued"] == 0
    finally:
        delay.set()
        sched.shutdown()
        assert compute.calls == 1


def test_cache_hit_never_enters_pending_or_activity():
    sched, compute, _rc, corr_cache = new_scheduler(io_workers=1)
    try:
        key = make_ckey()
        corr_cache.put(key, np.zeros((8, 8), dtype=np.float32))
        assert _wait_until(sched.idle)

        results = []
        sched.request(TileRequest(key=key, generation=0, priority=0),
                      results.append)

        assert len(results) == 1 and results[0].timing.get("cache") == "hit"
        assert sched.activity_snapshot()["total"] == 0
        assert sched.idle() is True and compute.calls == 0
    finally:
        sched.shutdown()


def test_internal_raw_staging_is_visible_in_the_activity_snapshot():
    """Staging enters `_pending` like any other work, so it counts -- there
    is deliberately no separate staging ledger to keep in step."""
    gate = threading.Event()

    class _StagingCompute(FakeCompute):
        def raw_keys_for(self, key):
            return [RawKey(source=key.source, channel=key.channel,
                           tile=key.tile)]

    class _BlockingProvider(FakeProvider):
        def read_tile(self, channel, tile):
            gate.wait(timeout=5)
            return super().read_tile(channel, tile)

    provider = _BlockingProvider()
    compute = _StagingCompute()
    sched = TileScheduler(provider, compute, LRUByteCache(10_000_000),
                          LRUByteCache(10_000_000),
                          io_workers=1, compute_workers=1)
    try:
        assert _wait_until(sched.idle)
        fired = threading.Event()
        sched.request(TileRequest(key=make_ckey(), generation=0, priority=0),
                      lambda r: None)

        # The corrected entry runs; its staged raw tile is a second entry.
        assert _wait_until(lambda: sched.activity_snapshot()["total"] >= 2)
        sched.notify_when_idle(fired.set)
        assert not fired.is_set()

        gate.set()
        assert fired.wait(timeout=10)
        assert sched.activity_snapshot()["total"] == 0
    finally:
        gate.set()
        sched.shutdown()


def test_stale_but_started_work_delays_idle_until_it_physically_ends():
    """`cancel_generation` must not make the scheduler look idle: started
    work always runs to completion."""
    started, delay = threading.Event(), threading.Event()
    sched, _c, _rc, _cc = new_scheduler(delay_event=delay,
                                        started_event=started, io_workers=1)
    try:
        sched.request(TileRequest(key=make_ckey(), generation=7, priority=0),
                      lambda r: None)
        assert started.wait(timeout=5)

        sched.cancel_generation(7)
        fired = threading.Event()
        sched.notify_when_idle(fired.set)

        assert sched.activity_snapshot()["running"] == 1
        assert not fired.wait(timeout=0.2), (
            "cancelling a generation must not fake an idle scheduler")

        delay.set()
        assert fired.wait(timeout=5)
    finally:
        delay.set()
        sched.shutdown()


def test_queued_stale_entry_dropped_reaches_idle():
    """The other stale path: an entry cancelled BEFORE a worker started it
    is dropped without running, and that too can reach idle."""
    started, delay = threading.Event(), threading.Event()
    sched, _c, _rc, _cc = new_scheduler(delay_event=delay,
                                        started_event=started, io_workers=1)
    try:
        blocker = make_ckey(params=(1,))
        victim = make_ckey(params=(2,))
        sched.request(TileRequest(key=blocker, generation=1, priority=0),
                      lambda r: None)
        assert started.wait(timeout=5)

        results = []
        sched.request(TileRequest(key=victim, generation=2, priority=1),
                      results.append)
        assert _wait_until(lambda: sched.activity_snapshot()["queued"] == 1)

        sched.cancel_generation(2)
        fired = threading.Event()
        sched.notify_when_idle(fired.set)
        delay.set()

        assert fired.wait(timeout=5)
        assert sched.activity_snapshot()["total"] == 0
        assert [r.error for r in results] == ["cancelled"]
    finally:
        delay.set()
        sched.shutdown()


def test_every_registered_idle_callback_fires_once_and_failures_are_contained():
    """A raising callback must not kill the worker thread that runs it, nor
    stop the callbacks after it."""
    started, delay = threading.Event(), threading.Event()
    sched, _c, _rc, _cc = new_scheduler(delay_event=delay,
                                        started_event=started, io_workers=1)
    try:
        sched.request(TileRequest(key=make_ckey(), generation=0, priority=0),
                      lambda r: None)
        assert started.wait(timeout=5)

        calls = []
        lock = threading.Lock()

        def record(name):
            def cb():
                with lock:
                    calls.append(name)
            return cb

        def boom():
            with lock:
                calls.append("boom")
            raise RuntimeError("callback exploded")

        sched.notify_when_idle(record("first"))
        sched.notify_when_idle(boom)
        sched.notify_when_idle(record("last"))

        delay.set()
        assert _wait_until(lambda: len(calls) == 3)
        assert calls == ["first", "boom", "last"]

        # The worker survived: it still serves requests.
        done = threading.Event()
        sched.request(TileRequest(key=make_ckey(params=(9,)), generation=0,
                                  priority=0),
                      lambda r: done.set())
        assert done.wait(timeout=5)

        # And each callback was one-shot.
        assert _wait_until(lambda: len(calls) == 3, timeout=0.2)
    finally:
        delay.set()
        sched.shutdown()


def test_idle_callback_runs_with_the_lock_released():
    """The callback must not hold `_lock`.

    Re-entering `request()` from the callback's OWN thread would prove
    nothing -- `_lock` is an RLock, so the same thread can re-acquire it.
    The check therefore hands the work to a SECOND thread and waits for it:
    that thread can only get in if the lock is genuinely free.
    """
    started, delay = threading.Event(), threading.Event()
    sched, _c, _rc, _cc = new_scheduler(delay_event=delay,
                                        started_event=started, io_workers=1)
    try:
        sched.request(TileRequest(key=make_ckey(), generation=0, priority=0),
                      lambda r: None)
        assert started.wait(timeout=5)

        other_thread_got_in = threading.Event()
        callback_returned = threading.Event()

        def on_idle():
            def from_another_thread():
                # Any call that takes `_lock` will do; a snapshot is the
                # cheapest and has no side effects.
                sched.activity_snapshot()
                sched.request(
                    TileRequest(key=make_ckey(params=(3,)), generation=0,
                                priority=0),
                    lambda r: None)
                other_thread_got_in.set()

            t = threading.Thread(target=from_another_thread)
            t.start()
            # Block the callback until the other thread has acquired the
            # lock: if the callback still held it, this would deadlock and
            # the assertions below would time out.
            entered = other_thread_got_in.wait(timeout=5)
            t.join(timeout=5)
            if entered:
                callback_returned.set()

        sched.notify_when_idle(on_idle)
        delay.set()
        assert other_thread_got_in.wait(timeout=5), (
            "another thread could not take the scheduler lock while an idle "
            "callback was running -- the callback holds it")
        assert callback_returned.wait(timeout=5)
    finally:
        delay.set()
        sched.shutdown()


def test_shutdown_discards_idle_callbacks_without_calling_them():
    started, delay = threading.Event(), threading.Event()
    sched, _c, _rc, _cc = new_scheduler(delay_event=delay,
                                        started_event=started, io_workers=1)
    fired = threading.Event()
    try:
        sched.request(TileRequest(key=make_ckey(), generation=0, priority=0),
                      lambda r: None)
        assert started.wait(timeout=5)
        sched.notify_when_idle(fired.set)
    finally:
        delay.set()
        sched.shutdown()

    assert not fired.is_set(), (
        "teardown is not a drain -- a shutdown must not start follow-up work")
    # And a callback registered after shutdown is neither stored nor called.
    late = threading.Event()
    sched.notify_when_idle(late.set)
    assert not late.is_set()


def test_request_after_shutdown_is_refused_synchronously():
    sched, compute, _rc, corr_cache = new_scheduler(io_workers=1)
    key = make_ckey()
    corr_cache.put(key, np.zeros((8, 8), dtype=np.float32))
    sched.shutdown()

    results = []
    sched.request(TileRequest(key=key, generation=0, priority=0),
                  results.append)

    assert len(results) == 1, "the callback must still fire exactly once"
    assert results[0].error == "shutdown"
    assert results[0].pixels is None, "not even a cache hit is served"
    assert sched.activity_snapshot()["total"] == 0, "nothing was queued"


def test_request_racing_shutdown_never_leaves_an_orphan_entry():
    """Deterministic reproduction of the request/shutdown race.

    The cache lookup is paused, `shutdown()` is allowed to complete (its
    workers exit), and only then is `request()` allowed to make its
    decision. Checking `_shutdown` before the lookup and enqueuing after it
    would queue work nobody can ever run: no callback, and `_pending` never
    empties, so `idle()` stays false for good.
    """
    at_cache_lookup = threading.Event()
    shutdown_done = threading.Event()
    sched, _c, _rc, corr_cache = new_scheduler()
    key = make_ckey()

    real_get = corr_cache.get

    def slow_get(k):
        value = real_get(k)
        if k == key:
            at_cache_lookup.set()
            # Hold the request here until shutdown has fully completed.
            shutdown_done.wait(timeout=5)
        return value

    corr_cache.get = slow_get
    results = []
    requester = threading.Thread(
        target=lambda: sched.request(
            TileRequest(key=key, generation=0, priority=0), results.append))
    requester.start()
    try:
        assert at_cache_lookup.wait(timeout=5)
        sched.shutdown()                 # workers exit while request waits
        shutdown_done.set()
        requester.join(timeout=5)
        assert not requester.is_alive()
    finally:
        corr_cache.get = real_get
        shutdown_done.set()

    assert len(results) == 1, "the caller must always get exactly one callback"
    assert results[0].error == "shutdown"
    assert sched.activity_snapshot()["total"] == 0, (
        "a request that lost the race to shutdown left an orphan entry that "
        "no worker can ever serve")


def test_internal_staging_racing_shutdown_leaves_no_orphan_entry():
    """The same race on the INTERNAL path: a compute task already running
    can reach raw staging after the raw workers have exited."""
    at_staging = threading.Event()
    shutdown_done = threading.Event()
    raw_key = RawKey(source=make_source(), channel="DAPI", tile=make_tile())

    class _StagingCompute(FakeCompute):
        def raw_keys_for(self, key):
            # Runs on the compute worker, just before staging: hold here
            # until shutdown has completed.
            at_staging.set()
            shutdown_done.wait(timeout=5)
            return [raw_key]

    provider = FakeProvider()
    compute = _StagingCompute()
    sched = TileScheduler(provider, compute, LRUByteCache(10_000_000),
                          LRUByteCache(10_000_000),
                          io_workers=1, compute_workers=1)
    results = []
    sched.request(TileRequest(key=make_ckey(), generation=0, priority=0),
                  results.append)
    try:
        assert at_staging.wait(timeout=5)
    finally:
        shutdown_thread = threading.Thread(target=sched.shutdown)
        shutdown_thread.start()
        # `shutdown()` joins the compute worker, which is parked in
        # `raw_keys_for`. Release it only once `_shutdown` is OBSERVED set,
        # so the staging attempt provably happens after the flag -- a sleep
        # here would only be guessing that it had.
        flag_set = _wait_until(lambda: _shutdown_flag(sched), timeout=5)
        shutdown_done.set()
        shutdown_thread.join(timeout=10)

    assert flag_set, "shutdown() never set the flag the staging path checks"
    assert not shutdown_thread.is_alive(), "shutdown did not complete"
    assert sched.activity_snapshot()["total"] == 0, (
        "staging queued an entry after the raw workers had exited")
    assert len(results) == 1, "the external waiter still got its callback"
