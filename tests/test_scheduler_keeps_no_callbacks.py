"""The shared tile scheduler keeps no consumer alive between jobs.

A worker thread waiting for its next job holds nothing from the last one:
a delivered read, a queued read dropped because its generation was
cancelled (raw and compute workers), and the idle callbacks run after the
warm-up. A consumer (a retired Step1 Python GPU binding in the product;
here a plain object whose bound method is the callback) is collected as
soon as nothing else holds it (viewer release plan §6).
"""

import gc
import threading
import weakref

import pytest

pytest.importorskip("tifffile")

from block01.viewer.caches import LRUByteCache  # noqa: E402
from block01.viewer.scheduler import TileScheduler  # noqa: E402
from block01.viewer.tile_types import RawKey, TileRequest  # noqa: E402

from test_scheduler_idle import (  # noqa: E402
    FakeCompute, FakeProvider, _wait_until, make_ckey, make_source, make_tile, new_scheduler)


class _Owner:
    def __init__(self):
        self.results = []

    def callback(self, result):
        self.results.append(result.error)


def _collected(ref):
    return _wait_until(lambda: (gc.collect(), ref() is None)[1], timeout=5.0)


def test_a_delivered_read_keeps_no_consumer():
    sched, _c, _rc, _cc = new_scheduler(io_workers=2)
    try:
        owner = _Owner()
        ref = weakref.ref(owner)
        raw = RawKey(source=make_source(), channel="DAPI", tile=make_tile())
        sched.request(TileRequest(key=raw, generation=0, priority=0), owner.callback)
        assert _wait_until(lambda: owner.results == [None])
        assert _wait_until(sched.idle)
        del owner
        assert _collected(ref)
    finally:
        sched.shutdown()


def test_a_cancelled_queued_compute_keeps_no_consumer():
    started, delay = threading.Event(), threading.Event()
    sched, _c, _rc, _cc = new_scheduler(delay_event=delay, started_event=started)
    try:
        sched.request(TileRequest(key=make_ckey(params=(1,)), generation=1, priority=0),
                      lambda r: None)
        assert started.wait(timeout=5)
        owner = _Owner()
        ref = weakref.ref(owner)
        sched.request(TileRequest(key=make_ckey(params=(2,)), generation=2, priority=1),
                      owner.callback)
        sched.cancel_generation(2)
        delay.set()
        assert _wait_until(lambda: owner.results == ["cancelled"])
        assert _wait_until(sched.idle)
        del owner
        assert _collected(ref)
    finally:
        delay.set()
        sched.shutdown()


def test_a_cancelled_queued_raw_read_keeps_no_consumer():
    gate, reading = threading.Event(), threading.Event()

    class _Blocking(FakeProvider):
        def read_tile(self, channel, tile):
            if tile.tx == 0:
                reading.set()
                gate.wait(timeout=5)
            return super().read_tile(channel, tile)

    sched = TileScheduler(_Blocking(), FakeCompute(), LRUByteCache(10_000_000),
                          LRUByteCache(10_000_000), io_workers=1, compute_workers=1)
    try:
        first = RawKey(source=make_source(), channel="DAPI", tile=make_tile(tx=0))
        sched.request(TileRequest(key=first, generation=1, priority=0), lambda r: None)
        assert reading.wait(timeout=5)
        owner = _Owner()
        ref = weakref.ref(owner)
        victim = RawKey(source=make_source(), channel="DAPI", tile=make_tile(tx=1))
        sched.request(TileRequest(key=victim, generation=2, priority=1), owner.callback)
        sched.cancel_generation(2)
        gate.set()
        assert _wait_until(lambda: owner.results == ["cancelled"])
        assert _wait_until(sched.idle)
        del owner
        assert _collected(ref)
    finally:
        gate.set()
        sched.shutdown()


def test_idle_callbacks_after_the_warm_up_are_not_kept():
    warming = threading.Event()

    class _SlowWarm(FakeProvider):
        def warm_thread_handle(self, levels=(0,)):
            warming.wait(timeout=5)          # the callback is registered meanwhile
            return True

    sched = TileScheduler(_SlowWarm(), FakeCompute(), LRUByteCache(10_000_000),
                          LRUByteCache(10_000_000), io_workers=1, compute_workers=1)
    try:
        owner = _Owner()
        ref = weakref.ref(owner)
        sched.notify_when_idle(lambda o=owner: o.results.append("idle"))   # holds it
        assert owner.results == []                    # still warming
        warming.set()                                 # the warm-up's end runs it
        assert _wait_until(lambda: owner.results == ["idle"])
        del owner
        assert _collected(ref)
    finally:
        warming.set()
        sched.shutdown()
