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
from block01.viewer.assembler import RawTileAssembler  # noqa: E402
from block01.viewer.caches import LRUByteCache  # noqa: E402
from block01.viewer.correction_compute import CorrectionCompute, halo_for  # noqa: E402
from block01.viewer.raw_tile_provider import RawTileProvider  # noqa: E402
from block01.viewer.scheduler import (  # noqa: E402
    DEFAULT_COMPUTE_WORKERS,
    DEFAULT_IO_WORKERS,
    TileScheduler,
)
from block01.viewer.tile_types import (  # noqa: E402
    CorrectionKey,
    PixelBuffer,
    QualityLevel,
    RawKey,
    SourceIdentity,
    TileAddress,
    TileGridSpec,
    TileRequest,
    effective_param,
    tiles_covering,
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

    def __init__(self, image_h=2000, image_w=2000, dtype=np.float32, level_shapes=None):
        self.image_h = image_h
        self.image_w = image_w
        self.dtype = dtype
        self.read_region_calls = 0
        self.read_tile_calls = 0
        # level_shapes: optional list of (h, w) for level_downsample support.
        self._level_shapes = level_shapes or [(image_h, image_w)]

    def level_shape(self, level: int):
        return self._level_shapes[level]

    def level_downsample(self, level: int) -> float:
        h0, _w0 = self._level_shapes[0]
        hn, _wn = self._level_shapes[level]
        if hn <= 0:
            return 1.0
        return round(h0 / hn)

    def read_tile(self, channel, tile: TileAddress):
        self.read_tile_calls += 1
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
        arr = (rows * 1000 + cols).astype(self.dtype)
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
    raw_cache = LRUByteCache(50_000_000)
    compute = CorrectionCompute(provider, raw_cache)
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
    raw_cache = LRUByteCache(50_000_000)
    compute = CorrectionCompute(provider, raw_cache)
    src = make_source()
    tile = make_tile(tile_size=512, tx=1, ty=1)  # core clamped to 488x488
    key = CorrectionKey(source=src, channel="DAPI", tile=tile, method="cucim",
                         params=(10,), algorithm_version="v1")
    result = compute.compute(key)
    assert result.pixels.shape == (488, 488)


# ── halo sizing contract ──────────────────────────────────────────────────

def test_halo_for_tophat_and_cucim():
    assert halo_for("tophat", 6) == 12
    assert halo_for("tophat", 25) == 50
    # cucim/gaussian: ceil(4 * sigma), matching truncate=4 defaults.
    assert halo_for("cucim", 8) == 32
    assert halo_for("cucim", 7) == 28
    with pytest.raises(ValueError):
        halo_for("bogus", 1)


# ── effective_param scaling ──────────────────────────────────────────────

def test_effective_param_scaling():
    assert effective_param(24, level=0, downsample=1) == 24
    assert effective_param(24, level=1, downsample=2) == 12
    assert effective_param(24, level=2, downsample=4) == 6
    # rounds, never below 1
    assert effective_param(3, level=1, downsample=8) == 1
    assert effective_param(1, level=3, downsample=100) == 1
    # level 0 is never scaled regardless of a stray downsample value
    assert effective_param(24, level=0, downsample=4) == 24


# ── tiles_covering: bbox -> tile-set coverage (pure math) ──────────────────

def test_tiles_covering_basic_aligned():
    # A bbox exactly covering 2x2 512-tiles.
    bbox = (0, 0, 1024, 1024)
    assert tiles_covering(bbox, 512) == {(0, 0), (1, 0), (0, 1), (1, 1)}


def test_tiles_covering_unaligned_shift_crosses_boundary():
    """An unaligned bbox, shifted by one quarter-tile across a tile
    boundary, must yield a tile set with >= 1 tile not in the previous set
    (this is the coverage-function regression pin for the pan-test fix)."""
    tile_size = 512
    q = tile_size // 4
    # Unaligned start (mimics a center-anchored viewport), like the fill
    # phase's tiles_for_viewport: y0/x0 not multiples of tile_size.
    y0, x0 = 100, 700
    y1, x1 = y0 + 2048, x0 + 2048
    bbox0 = (y0, x0, y1, x1)
    tiles0 = tiles_covering(bbox0, tile_size)

    # Shift right by enough quarter-tiles to guarantee crossing a boundary:
    # right edge x1=700+2048=2748 -> tile (2748-1)//512 = 5; shifting by
    # 4 quarter-tiles (= 1 full tile) is guaranteed to add a new column.
    shift = q * 4
    bbox1 = (y0, x0 + shift, y1, x1 + shift)
    tiles1 = tiles_covering(bbox1, tile_size)

    new_tiles = tiles1 - tiles0
    assert len(new_tiles) >= 1


def test_tiles_covering_empty_for_degenerate_bbox():
    assert tiles_covering((10, 10, 10, 10), 512) == set()


# ── RawTileAssembler: stitching correctness + cache-hit accounting ────────

def test_assembler_stitches_and_reuses_cache():
    provider = FakeProvider(image_h=4000, image_w=4000)
    raw_cache = LRUByteCache(50_000_000)
    assembler = RawTileAssembler(provider, raw_cache)
    src = make_source()
    grid = TileGridSpec(tile_size=512, source_chunk_shape=(), grid_version="v1")

    # A window spanning a 2x2 block of canonical 512 tiles.
    y0, y1, x0, x1 = 400, 900, 400, 900
    arr, (ry0, rx0), stats = assembler.assemble(src, grid, "DAPI", 0, y0, y1, x0, x1)

    assert (ry0, rx0) == (y0, x0)
    assert arr.shape == (y1 - y0, x1 - x0)
    rows = np.arange(y0, y1).reshape(-1, 1)
    cols = np.arange(x0, x1).reshape(1, -1)
    expected = (rows * 1000 + cols).astype(np.float32)
    np.testing.assert_array_equal(arr, expected)

    # 2x2 tiles covered, all cold on the first call.
    assert stats["tiles_total"] == 4
    assert stats["tiles_hit"] == 0
    assert stats["io_ms"] >= 0.0
    first_call_reads = provider.read_tile_calls
    assert first_call_reads == 4

    # Second call over the same window: everything should now be a cache hit
    # and no new provider.read_tile calls should happen.
    arr2, _, stats2 = assembler.assemble(src, grid, "DAPI", 0, y0, y1, x0, x1)
    np.testing.assert_array_equal(arr2, expected)
    assert stats2["tiles_total"] == 4
    assert stats2["tiles_hit"] == 4
    assert provider.read_tile_calls == first_call_reads


# ── native dtype raw cache ─────────────────────────────────────────────────

def test_raw_cache_and_pixelbuffer_are_native_dtype():
    provider = FakeProvider(image_h=2000, image_w=2000, dtype=np.uint8)
    raw_cache = LRUByteCache(50_000_000)
    corr_cache = LRUByteCache(50_000_000)
    compute = CorrectionCompute(provider, raw_cache)
    sched = TileScheduler(provider, compute, raw_cache, corr_cache, io_workers=1, compute_workers=1)

    src = make_source()
    tile = make_tile(tile_size=512, tx=0, ty=0)
    key = RawKey(source=src, channel="DAPI", tile=tile)
    results = []
    sched.request(TileRequest(key=key, generation=0, priority=0), results.append)

    deadline = time.time() + 5
    while not results and time.time() < deadline:
        time.sleep(0.01)

    assert results[0].error is None
    assert results[0].pixels.dtype == "uint8"
    assert results[0].pixels.handle.dtype == np.uint8

    cached = raw_cache.get(key)
    assert cached is not None
    assert cached.dtype == np.uint8
    sched.shutdown()


# ── golden seam test: stitched tiled correction == whole-image reference ──

def _synthetic_image(size=1024, seed=0):
    rng = np.random.RandomState(seed)
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    gradient = 50.0 + 30.0 * (xx / size) + 20.0 * (yy / size)
    blobs = np.zeros((size, size), dtype=np.float32)
    for _ in range(15):
        cy, cx = rng.randint(0, size, size=2)
        r = rng.randint(10, 40)
        yyi, xxi = np.ogrid[:size, :size]
        mask = (yyi - cy) ** 2 + (xxi - cx) ** 2 <= r * r
        blobs[mask] += rng.uniform(80, 200)
    noise = rng.normal(0, 2.0, size=(size, size)).astype(np.float32)
    return np.clip(gradient + blobs + noise, 0, None).astype(np.float32)


class _SyntheticProvider:
    """Real-shaped provider over a single in-memory float32 image."""

    def __init__(self, image: np.ndarray):
        self.image = image
        h, w = image.shape
        self._shape = (h, w)

    def level_shape(self, level):
        assert level == 0
        return self._shape

    def level_downsample(self, level):
        return 1.0

    def read_tile(self, channel, tile: TileAddress):
        ts = tile.grid.tile_size
        y0, x0 = tile.ty * ts, tile.tx * ts
        h, w = self._shape
        y1, x1 = min(y0 + ts, h), min(x0 + ts, w)
        arr, offset = self.read_region(channel, tile.level, y0, y1, x0, x1)
        return arr, 0.0

    def read_region(self, channel, level, y0, y1, x0, x1):
        h, w = self._shape
        cy0, cy1 = max(0, min(y0, h)), max(0, min(y1, h))
        cx0, cx1 = max(0, min(x0, w)), max(0, min(x1, w))
        return self.image[cy0:cy1, cx0:cx1].copy(), (cy0, cx0)


@pytest.mark.parametrize("method,param", [("cucim", 8), ("tophat", 6)])
def test_golden_seam_stitched_matches_whole_image_reference(monkeypatch, method, param):
    monkeypatch.setattr(bg_correction, "GPU_MORPH_AVAILABLE", False)

    image = _synthetic_image(size=1024, seed=0)
    provider = _SyntheticProvider(image)
    raw_cache = LRUByteCache(200_000_000)
    compute = CorrectionCompute(provider, raw_cache)

    src = make_source()
    grid = TileGridSpec(tile_size=512, source_chunk_shape=(), grid_version="v1")

    if method == "cucim":
        reference = bg_correction._apply_cucim_or_cpu(image, param, prefer_gpu=False)
    else:
        reference = bg_correction._apply_tophat_gpu_or_cpu(image, param)

    stitched = np.zeros_like(image)
    for ty in range(2):
        for tx in range(2):
            addr = TileAddress(grid=grid, level=0, tx=tx, ty=ty)
            key = CorrectionKey(
                source=src, channel="DAPI", tile=addr, method=method,
                params=(param,), algorithm_version="v1",
            )
            result = compute.compute(key)
            assert result.error is None
            y0, x0 = ty * 512, tx * 512
            h, w = result.pixels.shape
            stitched[y0:y0 + h, x0:x0 + w] = result.pixels.handle

    np.testing.assert_allclose(stitched, reference, atol=1e-4)


@pytest.mark.skipif(not bg_correction.GPU_MORPH_AVAILABLE, reason="GPU unavailable")
@pytest.mark.parametrize("method,param", [("cucim", 8), ("tophat", 6)])
def test_golden_seam_gpu_stitched_matches_whole_image(method, param):
    """GPU-path repeat of the seam check, prefer_gpu=True.

    cucim (gaussian): asserted against the CPU disk... no — asserted for
    GPU-tiled vs GPU-whole-image self-consistency at atol=1e-2 (GPU float
    tolerance), same as tophat, for symmetry; a CPU/GPU cross-check for
    gaussian is not expected to have the same structuring-element caveat as
    tophat, but self-consistency is the property this test actually needs
    to pin (seam correctness), so we check that for both methods.

    tophat: GPU cucim/cupyx morphology uses a SQUARE structuring element,
    while the CPU disk-based tophat (skimage) uses a circular/disk one --
    a PRE-EXISTING design question (not something this benchmark task is
    meant to resolve), so GPU tophat is intentionally NOT asserted for
    parity against the CPU disk result. Instead we assert GPU-tiled ==
    GPU-whole-image (self-consistency: tile borders leave no seam), which
    is the property this test is actually meant to guard.
    """
    image = _synthetic_image(size=1024, seed=1)
    provider = _SyntheticProvider(image)
    raw_cache = LRUByteCache(200_000_000)
    compute = CorrectionCompute(provider, raw_cache)

    src = make_source()
    grid = TileGridSpec(tile_size=512, source_chunk_shape=(), grid_version="v1")

    if method == "cucim":
        reference = bg_correction._apply_cucim_or_cpu(image, param, prefer_gpu=True)
    else:
        reference = bg_correction._apply_tophat_gpu_or_cpu(image, param)

    stitched = np.zeros_like(image)
    for ty in range(2):
        for tx in range(2):
            addr = TileAddress(grid=grid, level=0, tx=tx, ty=ty)
            key = CorrectionKey(
                source=src, channel="DAPI", tile=addr, method=method,
                params=(param,), algorithm_version=bg_correction.BG_CORRECTION_ALGO_VERSION,
            )
            result = compute.compute(key)
            assert result.error is None
            y0, x0 = ty * 512, tx * 512
            h, w = result.pixels.shape
            stitched[y0:y0 + h, x0:x0 + w] = result.pixels.handle

    # GPU-tiled must equal GPU-whole-image (self-consistency at GPU float
    # tolerance) for BOTH methods -- this is the seam-correctness property.
    np.testing.assert_allclose(stitched, reference, atol=1e-2)


# ── Fake compute for scheduler tests (avoids real bg_correction/GPU) ────────

class FakeCompute:
    def __init__(self, delay_event=None, started_event=None):
        self.calls = 0
        self.calls_lock = threading.Lock()
        self.delay_event = delay_event
        self.started_event = started_event

    def raw_keys_for(self, key: CorrectionKey):
        # Existing scheduler tests don't exercise raw staging; no raw deps.
        return []

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


# ── raw I/O staging (docs/v15_viewer_foundation_interfaces.md §4) ──────────

def test_raw_keys_for_matches_assembler_coverage(monkeypatch):
    """`raw_keys_for(key)` must name exactly the raw tiles `compute()`
    actually assembles -- no more, no less."""
    monkeypatch.setattr(bg_correction, "_apply_tophat_gpu_or_cpu", lambda arr, radius: arr.copy())
    provider = FakeProvider(image_h=4000, image_w=4000)
    raw_cache = LRUByteCache(50_000_000)
    compute = CorrectionCompute(provider, raw_cache)
    src = make_source()
    tile = make_tile(tile_size=512, tx=2, ty=3)
    key = CorrectionKey(source=src, channel="DAPI", tile=tile, method="tophat",
                         params=(10,), algorithm_version="v1")

    raw_keys = compute.raw_keys_for(key)
    assert raw_keys  # non-trivial halo -> at least the core tile

    result = compute.compute(key)
    assert result.error is None

    # Every tile named by raw_keys_for is now in the raw cache (assembled),
    # and nothing else was fetched.
    for rk in raw_keys:
        assert raw_cache.get(rk) is not None
    assert provider.read_tile_calls == len(raw_keys)


def test_staging_single_flight_across_correction_keys(monkeypatch):
    """Two CorrectionKeys whose halos share a raw tile, staged concurrently
    by two compute workers, must read that shared tile exactly once."""
    monkeypatch.setattr(bg_correction, "_apply_tophat_gpu_or_cpu", lambda arr, radius: arr.copy())
    provider = FakeProvider(image_h=8000, image_w=8000)
    orig_read_tile = provider.read_tile
    call_counts = {}
    counts_lock = threading.Lock()

    def slow_read_tile(channel, tile):
        addr = (tile.tx, tile.ty)
        with counts_lock:
            call_counts[addr] = call_counts.get(addr, 0) + 1
        time.sleep(0.05)  # widen the window so both workers' staging overlaps
        return orig_read_tile(channel, tile)

    provider.read_tile = slow_read_tile

    raw_cache = LRUByteCache(50_000_000)
    corr_cache = LRUByteCache(50_000_000)
    compute = CorrectionCompute(provider, raw_cache)
    sched = TileScheduler(provider, compute, raw_cache, corr_cache, io_workers=4, compute_workers=2)

    src = make_source()
    # Large halo (2*300=600) guarantees tile (tx=4,ty=4) and (tx=5,ty=4) share
    # raw tiles in their halo-padded windows.
    tile_a = make_tile(tile_size=512, tx=4, ty=4)
    tile_b = make_tile(tile_size=512, tx=5, ty=4)
    key_a = CorrectionKey(source=src, channel="DAPI", tile=tile_a, method="tophat",
                           params=(300,), algorithm_version="v1")
    key_b = CorrectionKey(source=src, channel="DAPI", tile=tile_b, method="tophat",
                           params=(300,), algorithm_version="v1")
    assert set(compute.raw_keys_for(key_a)) & set(compute.raw_keys_for(key_b))

    results = []
    lock = threading.Lock()

    def cb(r):
        with lock:
            results.append(r)

    sched.request(TileRequest(key=key_a, generation=0, priority=0), cb)
    sched.request(TileRequest(key=key_b, generation=0, priority=0), cb)

    deadline = time.time() + 15
    while len(results) < 2 and time.time() < deadline:
        time.sleep(0.01)

    assert len(results) == 2
    assert all(r.error is None for r in results)
    assert all(v == 1 for v in call_counts.values())  # single-flight: read each tile once
    sched.shutdown()


def test_staging_shares_with_external_raw_request(monkeypatch):
    """Staging for a CorrectionKey must dedup against an external RawKey
    request already in flight for the same raw tile -- no duplicate read."""
    monkeypatch.setattr(bg_correction, "_apply_tophat_gpu_or_cpu", lambda arr, radius: arr.copy())
    provider = FakeProvider(image_h=4000, image_w=4000)
    orig_read_tile = provider.read_tile
    call_counts = {}
    counts_lock = threading.Lock()
    gate = threading.Event()

    def gated_read_tile(channel, tile):
        addr = (tile.tx, tile.ty)
        with counts_lock:
            call_counts[addr] = call_counts.get(addr, 0) + 1
        gate.wait(timeout=5)
        return orig_read_tile(channel, tile)

    provider.read_tile = gated_read_tile

    raw_cache = LRUByteCache(50_000_000)
    corr_cache = LRUByteCache(50_000_000)
    compute = CorrectionCompute(provider, raw_cache)
    sched = TileScheduler(provider, compute, raw_cache, corr_cache, io_workers=4, compute_workers=1)

    src = make_source()
    tile = make_tile(tile_size=512, tx=2, ty=2)
    key = CorrectionKey(source=src, channel="DAPI", tile=tile, method="tophat",
                         params=(10,), algorithm_version="v1")
    shared_raw_key = compute.raw_keys_for(key)[0]

    raw_results = []
    corr_results = []
    sched.request(TileRequest(key=shared_raw_key, generation=0, priority=0), raw_results.append)
    time.sleep(0.1)  # let the external raw fetch start and block on the gate
    sched.request(TileRequest(key=key, generation=0, priority=0), corr_results.append)
    time.sleep(0.1)  # let the compute worker's staging attach as a waiter
    gate.set()

    deadline = time.time() + 15
    while (not raw_results or not corr_results) and time.time() < deadline:
        time.sleep(0.01)

    assert raw_results and raw_results[0].error is None
    assert corr_results and corr_results[0].error is None
    assert call_counts.get((shared_raw_key.tile.tx, shared_raw_key.tile.ty)) == 1
    sched.shutdown()


def test_staging_populates_cache_for_full_hits(monkeypatch):
    """Staged tiles land in raw_cache before compute() assembles, so the
    assembler reports 100% hits and the TileResult carries staging timing."""
    monkeypatch.setattr(bg_correction, "_apply_tophat_gpu_or_cpu", lambda arr, radius: arr.copy())
    provider = FakeProvider(image_h=4000, image_w=4000)
    raw_cache = LRUByteCache(50_000_000)
    corr_cache = LRUByteCache(50_000_000)
    compute = CorrectionCompute(provider, raw_cache)
    sched = TileScheduler(provider, compute, raw_cache, corr_cache, io_workers=4, compute_workers=1)

    src = make_source()
    tile = make_tile(tile_size=512, tx=2, ty=2)
    key = CorrectionKey(source=src, channel="DAPI", tile=tile, method="tophat",
                         params=(10,), algorithm_version="v1")
    expected_staged = len(compute.raw_keys_for(key))

    results = []
    sched.request(TileRequest(key=key, generation=0, priority=0), results.append)
    deadline = time.time() + 10
    while not results and time.time() < deadline:
        time.sleep(0.01)

    assert results[0].error is None
    timing = results[0].timing
    assert timing["staged_tiles"] == expected_staged
    assert timing["staging_wall_ms"] >= 0.0
    assert timing["raw_tiles_hit"] == timing["raw_tiles_total"]  # 100% hits post-staging
    sched.shutdown()


def test_staged_read_failure_surfaces_as_deterministic_error(monkeypatch):
    """If a staged raw tile's read fails, staging still completes (the
    failing tile is simply left uncached) and the assembler's own direct
    read then re-raises -> compute() fails deterministically."""
    monkeypatch.setattr(bg_correction, "_apply_tophat_gpu_or_cpu", lambda arr, radius: arr.copy())
    provider = FakeProvider(image_h=4000, image_w=4000)
    orig_read_tile = provider.read_tile

    def failing_read_tile(channel, tile):
        if (tile.tx, tile.ty) == (2, 2):
            raise RuntimeError("boom")
        return orig_read_tile(channel, tile)

    provider.read_tile = failing_read_tile

    raw_cache = LRUByteCache(50_000_000)
    corr_cache = LRUByteCache(50_000_000)
    compute = CorrectionCompute(provider, raw_cache)
    sched = TileScheduler(provider, compute, raw_cache, corr_cache, io_workers=4, compute_workers=1)

    src = make_source()
    tile = make_tile(tile_size=512, tx=2, ty=2)  # core tile IS the failing address
    key = CorrectionKey(source=src, channel="DAPI", tile=tile, method="tophat",
                         params=(10,), algorithm_version="v1")

    results = []
    sched.request(TileRequest(key=key, generation=0, priority=0), results.append)
    deadline = time.time() + 10
    while not results and time.time() < deadline:
        time.sleep(0.01)

    assert results[0].error is not None
    assert "boom" in results[0].error
    sched.shutdown()


def test_staging_runs_with_io_parallelism(monkeypatch):
    """With io_workers=4 and >=4 missing raw tiles, staging must actually
    overlap reads across threads (proves parallel submission, not a serial
    loop inside compute)."""
    monkeypatch.setattr(bg_correction, "_apply_tophat_gpu_or_cpu", lambda arr, radius: arr.copy())
    provider = FakeProvider(image_h=8000, image_w=8000)
    orig_read_tile = provider.read_tile
    concurrent = [0]
    max_concurrent = [0]
    reached_four = threading.Event()
    lock = threading.Lock()

    def tracking_read_tile(channel, tile):
        with lock:
            concurrent[0] += 1
            max_concurrent[0] = max(max_concurrent[0], concurrent[0])
            if concurrent[0] >= 4:
                reached_four.set()
        time.sleep(0.05)
        with lock:
            concurrent[0] -= 1
        return orig_read_tile(channel, tile)

    provider.read_tile = tracking_read_tile

    raw_cache = LRUByteCache(50_000_000)
    corr_cache = LRUByteCache(50_000_000)
    compute = CorrectionCompute(provider, raw_cache)
    sched = TileScheduler(provider, compute, raw_cache, corr_cache, io_workers=4, compute_workers=1)

    src = make_source()
    tile = make_tile(tile_size=512, tx=4, ty=4)
    # Large halo -> the padded window spans a 3x3 (or larger) block of raw
    # tiles, well past the 4 concurrent readers this test needs to observe.
    key = CorrectionKey(source=src, channel="DAPI", tile=tile, method="tophat",
                         params=(200,), algorithm_version="v1")
    assert len(compute.raw_keys_for(key)) >= 4

    results = []
    sched.request(TileRequest(key=key, generation=0, priority=0), results.append)
    deadline = time.time() + 15
    while not results and time.time() < deadline:
        time.sleep(0.01)

    assert results[0].error is None
    assert reached_four.is_set()
    assert max_concurrent[0] >= 4
    sched.shutdown()


# ── raw ready-queue: cancellation / staging / priority (interactive-bug ───
# fix round: raw requests are now queued through a cancellable priority
# ready-queue exactly like the compute path, instead of going straight to
# an I/O ThreadPoolExecutor -- see viewer/scheduler.py module docstring).

def test_raw_queued_request_dropped_when_only_waiter_generation_cancelled():
    """A queued RawKey request whose only external waiter's generation was
    cancelled before it started must be dropped WITHOUT ever calling
    provider.read_tile."""
    started = threading.Event()
    delay = threading.Event()
    provider = FakeProvider()
    orig_read_tile = provider.read_tile

    def blocking_first_read(channel, tile):
        started.set()
        delay.wait(timeout=5)
        return orig_read_tile(channel, tile)

    provider.read_tile = blocking_first_read
    compute = FakeCompute()
    raw_cache = LRUByteCache(10_000_000)
    corr_cache = LRUByteCache(10_000_000)
    sched = TileScheduler(provider, compute, raw_cache, corr_cache, io_workers=1, compute_workers=1)

    src = make_source()
    tile_blocking = make_tile(tx=0, ty=0)
    tile_cancelled = make_tile(tx=1, ty=0)
    key_blocking = RawKey(source=src, channel="DAPI", tile=tile_blocking)
    key_cancelled = RawKey(source=src, channel="DAPI", tile=tile_cancelled)

    blocking_results = []
    cancelled_results = []
    sched.request(TileRequest(key=key_blocking, generation=1, priority=0), blocking_results.append)
    assert started.wait(timeout=5)  # the single raw worker is now blocked

    sched.request(TileRequest(key=key_cancelled, generation=2, priority=0), cancelled_results.append)
    sched.cancel_generation(2)
    delay.set()  # release the blocking read; worker moves on to the queue

    deadline = time.time() + 5
    while (not blocking_results or not cancelled_results) and time.time() < deadline:
        time.sleep(0.01)

    assert blocking_results and blocking_results[0].error is None
    assert cancelled_results and cancelled_results[0].error == "cancelled"
    # The read for the cancelled tile must never have happened.
    assert provider.read_tile_calls == 1
    sched.shutdown()


def test_raw_staged_work_still_executes_after_cancel_generation():
    """Internal staging waiters (from _stage_raw_tile) are never stale:
    staged raw work must still execute (and populate the cache) even when
    every generation that would otherwise want it has been cancelled."""
    monkeypatch_targets = []
    provider = FakeProvider(image_h=4000, image_w=4000)
    raw_cache = LRUByteCache(50_000_000)
    corr_cache = LRUByteCache(50_000_000)
    compute = CorrectionCompute(provider, raw_cache)
    sched = TileScheduler(provider, compute, raw_cache, corr_cache, io_workers=2, compute_workers=1)

    import block01.core.bg_correction as bg_correction_mod
    orig = bg_correction_mod._apply_tophat_gpu_or_cpu
    bg_correction_mod._apply_tophat_gpu_or_cpu = lambda arr, radius: arr.copy()
    monkeypatch_targets.append((bg_correction_mod, "_apply_tophat_gpu_or_cpu", orig))
    try:
        src = make_source()
        tile = make_tile(tile_size=512, tx=2, ty=3)
        key = CorrectionKey(source=src, channel="DAPI", tile=tile, method="tophat",
                             params=(10,), algorithm_version="v1")
        raw_keys = compute.raw_keys_for(key)
        assert raw_keys

        # Cancel the generation BEFORE issuing the compute request: the
        # correction request itself is dropped as stale-queued compute work,
        # but staging (triggered separately here, directly) must still run.
        sched.cancel_generation(999)

        done = threading.Event()
        for rk in raw_keys:
            sched._stage_raw_tile(rk, lambda: None)
        # Give the raw workers time to drain the staged reads.
        deadline = time.time() + 5
        while time.time() < deadline and any(
            raw_cache.get(rk) is None for rk in raw_keys
        ):
            time.sleep(0.01)

        for rk in raw_keys:
            assert raw_cache.get(rk) is not None
    finally:
        for mod, name, orig_fn in monkeypatch_targets:
            setattr(mod, name, orig_fn)
        sched.shutdown()


def test_raw_priority_respected_single_worker():
    started = threading.Event()
    delay = threading.Event()
    provider = FakeProvider()
    orig_read_tile = provider.read_tile

    def blocking_first_read(channel, tile):
        started.set()
        delay.wait(timeout=5)
        return orig_read_tile(channel, tile)

    provider.read_tile = blocking_first_read
    compute = FakeCompute()
    raw_cache = LRUByteCache(10_000_000)
    corr_cache = LRUByteCache(10_000_000)
    sched = TileScheduler(provider, compute, raw_cache, corr_cache, io_workers=1, compute_workers=1)

    src = make_source()
    key_first = RawKey(source=src, channel="DAPI", tile=make_tile(tx=0, ty=0))
    key_low_prio = RawKey(source=src, channel="DAPI", tile=make_tile(tx=1, ty=0))
    key_high_prio = RawKey(source=src, channel="DAPI", tile=make_tile(tx=2, ty=0))

    order = []
    order_lock = threading.Lock()

    def make_cb(name):
        def cb(r):
            with order_lock:
                order.append(name)
        return cb

    sched.request(TileRequest(key=key_first, generation=1, priority=0), make_cb("first"))
    assert started.wait(timeout=5)

    sched.request(TileRequest(key=key_low_prio, generation=1, priority=5), make_cb("low"))
    sched.request(TileRequest(key=key_high_prio, generation=1, priority=1), make_cb("high"))

    delay.set()

    deadline = time.time() + 5
    while len(order) < 3 and time.time() < deadline:
        time.sleep(0.01)

    assert order[0] == "first"
    assert order[1:] == ["high", "low"]
    sched.shutdown()


# ── RawTileProvider handle_mode ────────────────────────────────────────────

def _write_small_ome_tiff(path):
    """2-channel 256x256 uint16 OME-TIFF, one pixel value scheme per
    channel so a read at (c, y, x) is verifiable positionally."""
    tifffile = pytest.importorskip("tifffile")
    h = w = 256
    data = np.zeros((2, h, w), dtype=np.uint16)
    rows = np.arange(h).reshape(-1, 1)
    cols = np.arange(w).reshape(1, -1)
    data[0] = (rows * 1000 + cols).astype(np.uint16)
    data[1] = (rows * 1000 + cols + 1).astype(np.uint16)
    tifffile.imwrite(path, data, ome=True, metadata={"Channel": {"Name": ["ch0", "ch1"]}})
    return data


@pytest.fixture
def small_ome_tiff(tmp_path):
    path = str(tmp_path / "small.ome.tif")
    data = _write_small_ome_tiff(path)
    return path, data


def test_provider_per_thread_mode_concurrent_reads_correct(small_ome_tiff):
    path, data = small_ome_tiff
    provider = RawTileProvider(path, handle_mode="per_thread")
    assert provider.open_count == 1  # metadata open at construction

    n_threads = 2
    reads_per_thread = 5
    results = {}
    results_lock = threading.Lock()
    barrier = threading.Barrier(n_threads)

    def worker(idx):
        barrier.wait(timeout=5)
        local = []
        for _ in range(reads_per_thread):
            arr, _off = provider.read_region(idx % 2, 0, 0, 256, 0, 256)
            local.append(arr)
        with results_lock:
            results[idx] = local

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    for idx, arrs in results.items():
        expected = data[idx % 2]
        for arr in arrs:
            np.testing.assert_array_equal(arr, expected)

    # open_count = 1 (metadata) + at most one TiffFile per distinct thread,
    # NOT one per call (reads_per_thread * n_threads calls happened).
    assert provider.open_count <= 1 + n_threads
    provider.close()


def test_provider_shared_lock_mode_serializes_correctly(small_ome_tiff):
    path, data = small_ome_tiff
    provider = RawTileProvider(path, handle_mode="shared_lock")
    assert provider.open_count == 1

    n_threads = 4
    results = {}
    results_lock = threading.Lock()
    barrier = threading.Barrier(n_threads)

    def worker(idx):
        barrier.wait(timeout=5)
        arr, _off = provider.read_region(idx % 2, 0, 0, 256, 0, 256)
        with results_lock:
            results[idx] = arr

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    for idx, arr in results.items():
        np.testing.assert_array_equal(arr, data[idx % 2])

    # Exactly two TiffFile opens total: one at construction (metadata) and
    # one lazy shared-handle open on first read -- concurrent reads from
    # n_threads threads must NOT open additional handles.
    assert provider.open_count == 2
    provider.close()


def test_provider_close_closes_tracked_handles_without_error(small_ome_tiff):
    path, _data = small_ome_tiff

    provider = RawTileProvider(path, handle_mode="per_thread")
    provider.read_region(0, 0, 0, 256, 0, 256)
    provider.close()  # must not raise

    provider2 = RawTileProvider(path, handle_mode="shared_lock")
    provider2.read_region(0, 0, 0, 256, 0, 256)
    provider2.close()  # must not raise

    # per_call mode: close() tracks no handles, but the lifecycle contract is
    # uniform — after close(), reads MUST fail loudly in every mode.
    provider3 = RawTileProvider(path, handle_mode="per_call")
    provider3.read_region(0, 0, 0, 256, 0, 256)
    provider3.close()
    import pytest as _pytest
    with _pytest.raises(RuntimeError, match="closed"):
        provider3.read_region(0, 0, 0, 256, 0, 256)


def test_provider_read_after_close_raises_every_mode(small_ome_tiff):
    """Lifecycle contract: stop scheduler -> join workers -> close();
    any read after close() is a hard error, never an undefined re-open of a
    stale thread-local handle."""
    import pytest as _pytest
    path, _data = small_ome_tiff
    for mode in ("per_thread", "shared_lock", "per_call"):
        provider = RawTileProvider(path, handle_mode=mode)
        provider.read_region(0, 0, 0, 64, 0, 64)
        provider.close()
        with _pytest.raises(RuntimeError, match="closed"):
            provider.read_region(0, 0, 0, 64, 0, 64)
        with _pytest.raises(RuntimeError, match="closed"):
            provider.read_tile(0, TileAddress(
                grid=TileGridSpec(tile_size=64), level=0, tx=0, ty=0))


# ── per-worker handle warming ────────────────────────────────────────────

def test_default_worker_counts_are_shared_constants():
    """TileScheduler.__init__'s io_workers/compute_workers defaults are the
    shared DEFAULT_IO_WORKERS/DEFAULT_COMPUTE_WORKERS constants, and every
    script call site references the constants rather than its own literal
    (source grep -- the point of this half of the test)."""
    import inspect
    import pathlib

    sig = inspect.signature(TileScheduler.__init__)
    assert sig.parameters["io_workers"].default == DEFAULT_IO_WORKERS
    assert sig.parameters["compute_workers"].default == DEFAULT_COMPUTE_WORKERS
    # Pinned deliberately: this value is an evidence-backed decision, not a
    # taste. 8 beats 4 on a level-crossing zoom (62.7% vs 47.5% in-motion
    # coverage) and on cold start (20 tiles in 259ms vs 366ms), and ties on
    # a drag (96.5% vs 97.0%, inside run-to-run spread). An earlier round
    # lowered it to 4 on the strength of the DRAG sweep alone, which is
    # saturated at 2 and therefore says nothing about the zoom burst. If you
    # change this, measure a level-crossing zoom, not just a drag.
    assert DEFAULT_IO_WORKERS == 8
    assert DEFAULT_COMPUTE_WORKERS == 4

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    for rel in (
        "scripts/explore_demo.py",
        "scripts/g1_render_probe.py",
        "scripts/benchmark_multichannel_prefetch.py",
    ):
        src = (repo_root / rel).read_text(encoding="utf-8")
        assert "DEFAULT_IO_WORKERS" in src, f"{rel} does not reference DEFAULT_IO_WORKERS"
        assert "DEFAULT_COMPUTE_WORKERS" in src, f"{rel} does not reference DEFAULT_COMPUTE_WORKERS"


def test_every_io_worker_warms_its_own_handle(small_ome_tiff):
    path, _data = small_ome_tiff
    provider = RawTileProvider(path, handle_mode="per_thread")
    raw_cache = LRUByteCache(1024 * 1024)
    corr_cache = LRUByteCache(1024 * 1024)
    compute = CorrectionCompute(provider, raw_cache)
    n_io = 3
    sched = TileScheduler(provider, compute, raw_cache, corr_cache,
                           io_workers=n_io, compute_workers=1)
    try:
        deadline = time.time() + 5.0
        while sched.warmed_workers < n_io and time.time() < deadline:
            time.sleep(0.01)
        assert sched.warmed_workers == n_io
        # One open for the constructing thread (metadata) + exactly one per
        # warmed I/O worker thread -- no more, no fewer.
        assert provider.open_count == 1 + n_io
    finally:
        sched.shutdown()
        provider.close()


def test_warm_failure_does_not_block_the_queue(small_ome_tiff, monkeypatch):
    """If warm_thread_handle's internal path raises for exactly one worker
    thread's first call, that worker still serves requests normally --
    warming failure is non-fatal and never propagates."""
    path, _data = small_ome_tiff
    provider = RawTileProvider(path, handle_mode="per_thread")
    raw_cache = LRUByteCache(1024 * 1024)
    corr_cache = LRUByteCache(1024 * 1024)
    compute = CorrectionCompute(provider, raw_cache)

    orig_per_thread_state = RawTileProvider._per_thread_state
    calls = {"n": 0}
    calls_lock = threading.Lock()

    def failing_once(self):
        with calls_lock:
            calls["n"] += 1
            first = calls["n"] == 1
        if first:
            raise RuntimeError("boom (simulated warm failure)")
        return orig_per_thread_state(self)

    monkeypatch.setattr(RawTileProvider, "_per_thread_state", failing_once)

    n_io = 3
    sched = TileScheduler(provider, compute, raw_cache, corr_cache,
                           io_workers=n_io, compute_workers=1)
    try:
        # Wait for every worker's warm attempt to have been counted (calls
        # reaching n_io) AND for the resulting warmed_workers count to
        # settle (its increment happens strictly after the call is
        # recorded, so waiting on `calls` alone can race ahead of it).
        deadline = time.time() + 5.0
        while (calls["n"] < n_io or sched.warmed_workers < n_io - 1) and time.time() < deadline:
            time.sleep(0.01)
        # Exactly one warm attempt failed; the rest succeeded.
        assert calls["n"] == n_io
        assert sched.warmed_workers == n_io - 1

        # The queue must still serve requests correctly despite the failure.
        grid = TileGridSpec(tile_size=64, source_chunk_shape=(), grid_version="v1")
        tile = TileAddress(grid=grid, level=0, tx=0, ty=0)
        src = provider.source_identity()
        key = RawKey(source=src, channel=0, tile=tile)
        results = []
        done = threading.Event()

        def cb(tr):
            results.append(tr)
            done.set()

        sched.request(TileRequest(key=key, generation=1, priority=0), cb)
        assert done.wait(timeout=5.0)
        assert results[0].error is None
        np.testing.assert_array_equal(results[0].pixels.handle, _data[0][:64, :64])
    finally:
        sched.shutdown()
        provider.close()


def test_warm_on_closed_provider_returns_false(small_ome_tiff):
    path, _data = small_ome_tiff
    provider = RawTileProvider(path, handle_mode="per_thread")
    provider.close()
    assert provider.warm_thread_handle() is False


def test_shutdown_during_warm_terminates(small_ome_tiff):
    """A scheduler whose per-worker warming is slow must still shut down
    promptly (warming checks the shutdown flag right after it finishes,
    before ever taking the queue's condition variable) and must leave no
    provider handle open."""
    path, _data = small_ome_tiff
    provider = RawTileProvider(path, handle_mode="per_thread")

    real_warm = provider.warm_thread_handle
    warm_delay_s = 0.3

    def slow_warm(levels=(0,)):
        time.sleep(warm_delay_s)
        return real_warm(levels)

    provider.warm_thread_handle = slow_warm

    raw_cache = LRUByteCache(1024 * 1024)
    corr_cache = LRUByteCache(1024 * 1024)
    compute = CorrectionCompute(provider, raw_cache)
    n_io = 3
    sched = TileScheduler(provider, compute, raw_cache, corr_cache,
                           io_workers=n_io, compute_workers=1)

    t0 = time.perf_counter()
    sched.shutdown()
    elapsed_s = time.perf_counter() - t0
    # Generous bound: worst case is roughly one warm_delay_s (workers warm
    # concurrently), plus scheduling slack -- nowhere near n_io * delay.
    assert elapsed_s < warm_delay_s + 3.0

    provider.close()
    with provider._registry_lock:
        assert provider._thread_registry == []


def test_stale_waiter_is_not_called_back_by_default():
    """The historical contract, kept as the DEFAULT: a waiter whose
    generation has gone stale gets no callback at all. A foreground consumer
    has nothing to do with a result it can no longer display.

    Only a consumer that meters its own PHYSICAL concurrency needs the
    opposite, and it must ask for it explicitly via
    `TileRequest.notify_on_stale_completion` (covered separately). This test
    exists so that opt-in can never quietly become the default.
    """
    from block01.viewer.tile_types import TileRequest

    default_req = TileRequest(key=object(), generation=("g", 0), priority=0)
    assert default_req.notify_on_stale_completion is False

    delivered = []

    class _Sched(TileScheduler):
        def __init__(self):
            # No worker threads: this exercises _deliver directly.
            self._lock = threading.RLock()
            self._stale_gens = {("g", 0)}

    sched = _Sched()
    stale_default = TileRequest(key=object(), generation=("g", 0), priority=0)
    stale_optin = TileRequest(key=object(), generation=("g", 0), priority=0,
                              notify_on_stale_completion=True)
    sched._deliver([(stale_default, lambda r: delivered.append("default")),
                    (stale_optin, lambda r: delivered.append(r.error))],
                   lambda req: None)

    assert delivered == ["stale"], (
        f"expected only the opt-in waiter to be called back, got {delivered}")
