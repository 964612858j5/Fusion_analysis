"""Unit tests for the v15 viewer-foundation prototype (viewer/ package).

Pure-Python: no Qt, no GPU, no real OME-TIFF. `bg_correction` kernels are
monkeypatched with fast deterministic CPU lambdas so tests run in well
under a second per case.
"""

import threading
import time

import numpy as np
import pytest

pytest.importorskip("tifffile")

from block01.core import bg_correction  # noqa: E402
from block01.viewer.caches import LRUByteCache  # noqa: E402
from block01.viewer.correction_compute import CorrectionCompute  # noqa: E402
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
)


def make_source(path="/x/dataset.ome.tif", fp="1:1", stage="raw"):
    return SourceIdentity(dataset_path=path, dataset_fingerprint=fp, stage=stage)


def make_tile(tile_size=512, level=0, tx=0, ty=0):
    grid = TileGridSpec(tile_size=tile_size, source_chunk_shape=(), grid_version="v1")
    return TileAddress(grid=grid, level=level, tx=tx, ty=ty)


# ── FakeProvider for scheduler/compute tests ─────────────────────────────────

class FakeProvider:
    """Deterministic stand-in for RawTileProvider.

    read_region returns a "ramp" array (value == absolute row*1000+col at
    the un-clamped request coordinates) so halo-crop correctness can be
    verified positionally. Bounds are clamped to a configurable image size.
    """

    def __init__(self, image_h=2000, image_w=2000):
        self.image_h = image_h
        self.image_w = image_w
        self.read_region_calls = 0

    def read_tile(self, channel, tile: TileAddress):
        ts = tile.grid.tile_size
        y0, x0 = tile.ty * ts, tile.tx * ts
        y1, x1 = min(y0 + ts, self.image_h), min(x0 + ts, self.image_w)
        arr, _ = self.read_region(channel, tile.level, y0, y1, x0, x1)
        return arr, 1.23

    def read_region(self, channel, level, y0, y1, x0, x1):
        self.read_region_calls += 1
        cy0, cy1 = max(0, y0), min(y1, self.image_h)
        cx0, cx1 = max(0, x0), min(x1, self.image_w)
        rows = np.arange(cy0, cy1).reshape(-1, 1)
        cols = np.arange(cx0, cx1).reshape(1, -1)
        arr = (rows * 1000 + cols).astype(np.float32)
        return arr, (cy0, cx0)


# ── 1. key hashability / equality ────────────────────────────────────────────

def test_keys_hashable_and_equal():
    src = make_source()
    tile = make_tile()
    k1 = RawKey(source=src, channel="DAPI", tile=tile)
    k2 = RawKey(source=src, channel="DAPI", tile=tile)
    assert k1 == k2
    assert hash(k1) == hash(k2)

    c1 = CorrectionKey(source=src, channel="DAPI", tile=tile, method="tophat",
                        params=(25,), algorithm_version="v1")
    c2 = CorrectionKey(source=src, channel="DAPI", tile=tile, method="tophat",
                        params=(25,), algorithm_version="v1")
    c3 = CorrectionKey(source=src, channel="DAPI", tile=tile, method="tophat",
                        params=(50,), algorithm_version="v1")
    assert c1 == c2 and hash(c1) == hash(c2)
    assert c1 != c3
    d = {c1: "a"}
    d[c3] = "b"
    assert len(d) == 2


# ── 2. provider-side edge clamp ──────────────────────────────────────────────

def test_fake_provider_edge_clamp():
    provider = FakeProvider(image_h=1000, image_w=1000)
    tile = make_tile(tile_size=512, tx=1, ty=1)  # covers [512:1024, 512:1024], clamped to 1000
    arr, io_ms = provider.read_tile("DAPI", tile)
    assert arr.shape == (488, 488)
    assert io_ms == 1.23


# ── 3. LRU cache ──────────────────────────────────────────────────────────────

def test_lru_cache_hit_miss_eviction():
    cache = LRUByteCache(max_bytes=50 * 4)  # room for exactly 2 5x5 float32 tiles
    a = np.zeros((5, 5), dtype=np.float32)  # 100 bytes
    b = np.zeros((5, 5), dtype=np.float32)
    c = np.zeros((5, 5), dtype=np.float32)

    assert cache.get("a") is None
    cache.put("a", a)
    cache.put("b", b)
    stats = cache.stats()
    assert stats["items"] == 2
    assert cache.get("a") is not None  # "a" now most-recently-used
    cache.put("c", c)  # forces eviction of least-recently-used ("b")
    stats = cache.stats()
    assert stats["evictions"] == 1
    assert cache.get("b") is None
    assert cache.get("a") is not None
    assert cache.get("c") is not None

    final = cache.stats()
    assert final["hits"] >= 3
    assert final["misses"] >= 2
    cache.clear()
    assert cache.stats()["items"] == 0


# ── 4. halo crop correctness ──────────────────────────────────────────────────

def test_correction_compute_crops_halo_exactly(monkeypatch):
    monkeypatch.setattr(bg_correction, "_apply_tophat_gpu_or_cpu", lambda arr, radius: arr.copy())

    provider = FakeProvider(image_h=4000, image_w=4000)
    compute = CorrectionCompute(provider)
    src = make_source()
    tile = make_tile(tile_size=512, tx=2, ty=3)  # y0=1536, x0=1024, well inside bounds
    key = CorrectionKey(source=src, channel="DAPI", tile=tile, method="tophat",
                         params=(10,), algorithm_version="v1")

    result = compute.compute(key)
    assert result.error is None
    assert result.pixels.shape == (512, 512)

    y0, x0 = tile.ty * 512, tile.tx * 512
    rows = np.arange(y0, y0 + 512).reshape(-1, 1)
    cols = np.arange(x0, x0 + 512).reshape(1, -1)
    expected = (rows * 1000 + cols).astype(np.float32)
    np.testing.assert_array_equal(result.pixels.handle, expected)
    assert result.timing["kernel_includes_transfers"] is True


def test_correction_compute_crops_halo_at_edge(monkeypatch):
    monkeypatch.setattr(bg_correction, "_apply_cucim_or_cpu",
                         lambda arr, sigma, prefer_gpu=True: arr.copy())
    provider = FakeProvider(image_h=1000, image_w=1000)
    compute = CorrectionCompute(provider)
    src = make_source()
    tile = make_tile(tile_size=512, tx=1, ty=1)  # core clamped to 488x488
    key = CorrectionKey(source=src, channel="DAPI", tile=tile, method="cucim",
                         params=(10,), algorithm_version="v1")
    result = compute.compute(key)
    assert result.pixels.shape == (488, 488)


# ── Fake compute for scheduler tests (avoids real bg_correction/GPU) ────────

class FakeCompute:
    def __init__(self, delay_event=None, started_event=None):
        self.calls = 0
        self.calls_lock = threading.Lock()
        self.delay_event = delay_event
        self.started_event = started_event

    def compute(self, key: CorrectionKey):
        with self.calls_lock:
            self.calls += 1
        if self.started_event is not None:
            self.started_event.set()
        if self.delay_event is not None:
            self.delay_event.wait(timeout=5)
        arr = np.full((8, 8), float(key.params[0]), dtype=np.float32)
        req = TileRequest(key=key, generation=0, priority=0)
        pixels = PixelBuffer(residency="cpu", dtype="float32", shape=arr.shape, handle=arr)
        return __import__("block01.viewer.tile_types", fromlist=["TileResult"]).TileResult(
            request=req, pixels=pixels, quality=key.quality, provisional=False,
            timing={"io_ms": 0.0, "kernel_ms": 0.0, "total_ms": 0.0}, error=None,
        )


def new_scheduler(compute_workers=1, delay_event=None, started_event=None):
    provider = FakeProvider()
    compute = FakeCompute(delay_event=delay_event, started_event=started_event)
    raw_cache = LRUByteCache(10_000_000)
    corr_cache = LRUByteCache(10_000_000)
    sched = TileScheduler(provider, compute, raw_cache, corr_cache,
                           io_workers=2, compute_workers=compute_workers)
    return sched, compute, raw_cache, corr_cache


def make_ckey(params=(25,), tx=0, ty=0, quality=QualityLevel.INTERACTIVE):
    src = make_source()
    tile = make_tile(tx=tx, ty=ty)
    return CorrectionKey(source=src, channel="DAPI", tile=tile, method="tophat",
                          params=params, algorithm_version="v1", quality=quality)


# ── 5. cache hit is synchronous ──────────────────────────────────────────────

def test_scheduler_cache_hit_synchronous():
    sched, compute, raw_cache, corr_cache = new_scheduler()
    key = make_ckey()
    corr_cache.put(key, np.zeros((8, 8), dtype=np.float32))

    results = []
    req = TileRequest(key=key, generation=0, priority=0)
    sched.request(req, results.append)

    assert len(results) == 1
    assert results[0].timing.get("cache") == "hit"
    assert compute.calls == 0
    sched.shutdown()


# ── 6. dedup: concurrent same-generation requests ────────────────────────────

def test_scheduler_dedup_same_key_concurrent():
    started = threading.Event()
    delay = threading.Event()
    sched, compute, raw_cache, corr_cache = new_scheduler(delay_event=delay, started_event=started)
    key = make_ckey()

    results = []
    lock = threading.Lock()

    def cb(r):
        with lock:
            results.append(r)

    req1 = TileRequest(key=key, generation=1, priority=0)
    req2 = TileRequest(key=key, generation=1, priority=0)
    sched.request(req1, cb)
    sched.request(req2, cb)

    assert started.wait(timeout=5)
    delay.set()

    deadline = time.time() + 5
    while len(results) < 2 and time.time() < deadline:
        time.sleep(0.01)

    assert compute.calls == 1
    assert len(results) == 2
    assert results[0].pixels.handle is results[1].pixels.handle
    sched.shutdown()


# ── 7. dedup across generations ──────────────────────────────────────────────

def test_scheduler_dedup_across_generations():
    started = threading.Event()
    delay = threading.Event()
    sched, compute, raw_cache, corr_cache = new_scheduler(delay_event=delay, started_event=started)
    key = make_ckey()
    results = []

    sched.request(TileRequest(key=key, generation=1, priority=0), results.append)
    assert started.wait(timeout=5)
    # second request arrives for a NEW generation while the first is in flight
    sched.request(TileRequest(key=key, generation=2, priority=0), results.append)
    delay.set()

    deadline = time.time() + 5
    while len(results) < 2 and time.time() < deadline:
        time.sleep(0.01)

    assert compute.calls == 1
    assert len(results) == 2
    sched.shutdown()


# ── 8. cancellation ──────────────────────────────────────────────────────────

def test_scheduler_cancel_generation_drops_queued_not_running():
    started = threading.Event()
    delay = threading.Event()
    sched, compute, raw_cache, corr_cache = new_scheduler(
        compute_workers=1, delay_event=delay, started_event=started
    )

    key_running = make_ckey(tx=0, ty=0)
    key_queued = make_ckey(tx=1, ty=0)

    running_results = []
    queued_results = []

    sched.request(TileRequest(key=key_running, generation=1, priority=0), running_results.append)
    assert started.wait(timeout=5)  # worker is now blocked inside compute() for key_running

    # This one is only wanted by generation 2, which we will cancel before it runs.
    sched.request(TileRequest(key=key_queued, generation=2, priority=0), queued_results.append)

    sched.cancel_generation(2)
    delay.set()  # let the running task finish; worker then looks at the queue

    deadline = time.time() + 5
    while (not running_results or not queued_results) and time.time() < deadline:
        time.sleep(0.01)

    assert len(running_results) == 1
    assert running_results[0].error is None

    assert len(queued_results) == 1
    assert queued_results[0].error == "cancelled"
    assert compute.calls == 1  # queued one never ran
    sched.shutdown()


def test_scheduler_stale_waiter_not_delivered_but_cache_populated():
    started = threading.Event()
    delay = threading.Event()
    sched, compute, raw_cache, corr_cache = new_scheduler(delay_event=delay, started_event=started)
    key = make_ckey()

    results = []
    sched.request(TileRequest(key=key, generation=1, priority=0), results.append)
    assert started.wait(timeout=5)
    sched.cancel_generation(1)  # the only waiter for this in-flight work is now stale
    delay.set()

    time.sleep(0.3)  # let the running compute finish and try to deliver
    assert results == []  # stale waiter must not receive a callback
    assert corr_cache.get(key) is not None  # but the cache is still populated
    sched.shutdown()


# ── 9. cache populated after completion -> next request is a hit ───────────

def test_scheduler_result_enters_cache_for_next_request():
    delay = threading.Event()
    delay.set()  # don't block; let it run immediately
    sched, compute, raw_cache, corr_cache = new_scheduler(delay_event=delay)
    key = make_ckey()
    results = []
    sched.request(TileRequest(key=key, generation=1, priority=0), results.append)

    deadline = time.time() + 5
    while not results and time.time() < deadline:
        time.sleep(0.01)
    assert len(results) == 1
    assert compute.calls == 1

    results2 = []
    sched.request(TileRequest(key=key, generation=2, priority=0), results2.append)
    assert len(results2) == 1
    assert results2[0].timing.get("cache") == "hit"
    assert compute.calls == 1  # no recompute
    sched.shutdown()


# ── 10. priority ordering ────────────────────────────────────────────────────

def test_scheduler_priority_ordering_single_worker():
    started = threading.Event()
    delay = threading.Event()
    sched, compute, raw_cache, corr_cache = new_scheduler(
        compute_workers=1, delay_event=delay, started_event=started
    )

    key_first = make_ckey(tx=0, ty=0)
    key_low_prio = make_ckey(tx=1, ty=0)   # priority 5, queued first
    key_high_prio = make_ckey(tx=2, ty=0)  # priority 1, queued second

    order = []
    order_lock = threading.Lock()

    def make_cb(name):
        def cb(r):
            with order_lock:
                order.append(name)
        return cb

    # Occupy the single worker so the next two both sit in the ready-queue.
    sched.request(TileRequest(key=key_first, generation=1, priority=0), make_cb("first"))
    assert started.wait(timeout=5)

    sched.request(TileRequest(key=key_low_prio, generation=1, priority=5), make_cb("low"))
    sched.request(TileRequest(key=key_high_prio, generation=1, priority=1), make_cb("high"))

    delay.set()  # release "first"; worker will then drain the ready-queue by priority

    deadline = time.time() + 5
    while len(order) < 3 and time.time() < deadline:
        time.sleep(0.01)

    assert order[0] == "first"
    assert order[1:] == ["high", "low"]
    sched.shutdown()


# ── raw path smoke test ──────────────────────────────────────────────────────

def test_scheduler_raw_key_path():
    sched, compute, raw_cache, corr_cache = new_scheduler()
    src = make_source()
    tile = make_tile()
    key = RawKey(source=src, channel="DAPI", tile=tile)
    results = []
    sched.request(TileRequest(key=key, generation=0, priority=0), results.append)

    deadline = time.time() + 5
    while not results and time.time() < deadline:
        time.sleep(0.01)
    assert len(results) == 1
    assert results[0].error is None
    assert results[0].quality == QualityLevel.NATIVE
    assert raw_cache.get(key) is not None
    sched.shutdown()
