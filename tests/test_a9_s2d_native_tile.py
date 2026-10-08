"""Block A9 S2d (application §28, contract v2 §28.5): Step1's native tile
interface. No consumer uses it yet -- every existing read is unchanged.

A `step1-native` key read through `Step1TileProvider.read_tile_key` gives a
`NativeTile`: a raw uint8/uint16 channel keeps its integers (zero, and
INVALID, outside the ROI rectangle); everything else is `read_tile`'s float
tile, valid where finite. Synthetic pyramids only.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")

import test_step1_viewer_host as h  # noqa: E402
from block01.ui.step1_viewer_host import Step1TileProvider  # noqa: E402
from block01.viewer import native_tile as nt  # noqa: E402
from block01.viewer.tile_types import CorrectionKey, RawKey, TileRequest  # noqa: E402


class _IntPyramid(h._RawPyramid):
    """The same synthetic slide, stored as integers of `dtype`."""

    def __init__(self, dtype):
        super().__init__()
        self.dtype = np.dtype(dtype)

    def pixels(self, channel, y0, y1, x0, x1):
        top = np.iinfo(self.dtype).max
        yy, xx = np.mgrid[y0:y1, x0:x1]
        base = (yy * 7 + xx * 3 + self.CHANNELS.index(channel) * 11) % (top + 1)
        base[0, 0] = top                                  # an extreme value
        return base.astype(self.dtype)


def _provider(dtype=np.uint8, decisions=None, products=None, roi_bbox=h.ROI):
    raw = _IntPyramid(dtype) if dtype is not None else h._RawPyramid()
    return Step1TileProvider(raw, h._table(decisions, products, roi_bbox=roi_bbox)), raw


def _key(provider, channel, tile, native=True):
    source = provider.native_source_identity() if native else provider.source_identity()
    return RawKey(source=source, channel=channel, tile=tile)


@pytest.mark.parametrize("dtype", [np.uint8, np.uint16])
# inside the ROI (512..1536), and a level-1 tile the ROI cuts
@pytest.mark.parametrize("level,ty,tx", [(0, 1, 1), (0, 2, 2), (1, 0, 0)])
def test_native_integers_equal_the_float_tile_where_valid(dtype, level, ty, tx):
    provider, _raw = _provider(dtype)
    tile = h._tile(level, ty, tx)
    native, _io = provider.read_tile_key(_key(provider, "CD3", tile))
    float_tile, _io = provider.read_tile("CD3", tile)
    assert native.kind == nt.KIND_RAW and native.dtype == np.dtype(dtype)
    assert native.shape == float_tile.shape
    valid = native.valid_mask()
    assert np.array_equal(valid, np.isfinite(float_tile)), "validity moved"
    # bit for bit where valid, absent (NaN) everywhere else
    assert np.array_equal(native.to_float()[valid], float_tile[valid])
    assert np.isnan(native.to_float()[~valid]).all()
    assert not native.values.flags.writeable


def test_a_tile_outside_the_roi_is_wholly_invalid_and_reads_nothing():
    provider, raw = _provider(np.uint8, roi_bbox=(0, 4, 0, 4))
    raw.reads.clear()
    far = h._tile(0, 3, 3)
    native, _io = provider.read_tile_key(_key(provider, "CD3", far))
    assert native.valid_rect is None and not native.valid_mask().any()
    assert raw.reads == []


def test_a_float_stored_raw_channel_keeps_read_tile_numbers():
    provider, _raw = _provider(dtype=None)                 # float32 pyramid
    tile = h._tile(1, 0, 0)
    native, _io = provider.read_tile_key(_key(provider, "CD3", tile))
    float_tile, _io = provider.read_tile("CD3", tile)
    assert native.kind == nt.KIND_RAW_FLOAT and native.valid_rect == nt.FINITE
    assert np.array_equal(native.values, float_tile, equal_nan=True)


def test_the_roi_cut_level1_tile_is_its_rectangle_and_zero_outside():
    provider, _raw = _provider(np.uint8)
    native, _io = provider.read_tile_key(_key(provider, "CD3", h._tile(1, 0, 0)))
    # ROI 512..1536 at downsample 4 -> 128..384 of the 512-px level-1 tile
    assert native.valid_rect == (128, 384, 128, 384)
    outside = ~native.valid_mask()
    assert outside.any() and not native.values[outside].any()


def test_a_corrected_channel_is_read_tile_unchanged_and_never_the_pyramid():
    provider, raw = _provider(np.uint8, decisions={"CD3": "tophat"},
                              products={"CD3": h._Corrected("CD3")})
    raw.reads.clear()
    tile = h._tile(0, 2, 2)
    native, _io = provider.read_tile_key(_key(provider, "CD3", tile))
    float_tile, _io = provider.read_tile("CD3", tile)
    assert native.kind == nt.KIND_CORRECTED and native.valid_rect == nt.FINITE
    assert np.array_equal(native.values, float_tile, equal_nan=True)
    assert raw.reads == []


class _WeirdFloatPyramid(h._RawPyramid):
    """A float-stored raw channel holding NaN and +-Inf inside the ROI."""

    def pixels(self, channel, y0, y1, x0, x1):
        out = super().pixels(channel, y0, y1, x0, x1)
        out[0, 0], out[0, 1], out[1, 0] = np.nan, np.inf, -np.inf
        return out


def test_raw_nan_and_inf_stay_invalid_in_the_float_fallback():
    provider = Step1TileProvider(_WeirdFloatPyramid(), h._table())
    tile = h._tile(0, 1, 1)
    native, _io = provider.read_tile_key(_key(provider, "CD3", tile))
    float_tile, _io = provider.read_tile("CD3", tile)
    assert native.kind == nt.KIND_RAW_FLOAT
    assert np.array_equal(native.values, float_tile, equal_nan=True)
    mask = native.valid_mask()
    assert not mask[0, 0] and not mask[0, 1] and not mask[1, 0] and mask[2, 2]


class _OddPyramid(_IntPyramid):
    """Edges that are not tile multiples, and a non-integer downsample."""

    def __init__(self):
        super().__init__(np.uint16)
        self._shapes = {0: (2000, 1900), 1: (667, 634)}

    def level_downsample(self, level):
        return 1.0 if level == 0 else 3.0


@pytest.mark.parametrize("roi", [(0, 2000, 0, 1900), (101, 1003, 53, 951)])
@pytest.mark.parametrize("level,ty,tx", [(0, 3, 3), (0, 1, 1), (1, 1, 1), (1, 0, 0)])
def test_truncated_edges_and_odd_rois_match_read_tile(roi, level, ty, tx):
    provider = Step1TileProvider(_OddPyramid(), h._table(roi_bbox=roi))
    tile = h._tile(level, ty, tx)
    native, _io = provider.read_tile_key(_key(provider, "CD3", tile))
    float_tile, _io = provider.read_tile("CD3", tile)
    assert native.shape == float_tile.shape
    assert np.array_equal(native.valid_mask(), np.isfinite(float_tile))
    assert np.array_equal(native.to_float(), float_tile, equal_nan=True)


def test_a_channel_without_its_product_is_absent_and_never_raw():
    provider, raw = _provider(np.uint8, decisions={"CD8": "tophat"})   # no product
    raw.reads.clear()
    native, _io = provider.read_tile_key(_key(provider, "CD8", h._tile(0, 1, 1)))
    assert native.kind == nt.KIND_MISSING and native.valid_rect is None
    assert not native.valid_mask().any()
    assert raw.reads == [], "a missing product was replaced by raw pixels"


def test_an_ordinary_key_is_read_exactly_as_before():
    provider, _raw = _provider(np.uint8)
    tile = h._tile(0, 1, 1)
    via_key, _io = provider.read_tile_key(_key(provider, "CD3", tile, native=False))
    direct, _io = provider.read_tile("CD3", tile)
    assert isinstance(via_key, np.ndarray) and via_key.dtype == np.float32
    assert np.array_equal(via_key, direct, equal_nan=True)


def test_the_two_namespaces_never_share_a_key_and_retire_together():
    provider, _raw = _provider(np.uint8)
    native, ordinary = provider.native_source_identity(), provider.source_identity()
    assert native != ordinary and native.stage == nt.NATIVE_STAGE
    assert native.corrected_artifact == ordinary.corrected_artifact
    moved, _raw2 = _provider(np.uint8, decisions={"CD3": "tophat"})
    assert moved.native_source_identity() != native
    assert moved.source_identity() != ordinary


def test_the_scheduler_delivers_native_tiles_cold_and_from_cache():
    from block01.viewer.caches import LRUByteCache
    from block01.viewer.correction_compute import CorrectionCompute
    from block01.viewer.scheduler import TileScheduler

    provider, raw = _provider(np.uint16)
    raw_cache = LRUByteCache(8 << 20)
    scheduler = TileScheduler(provider, CorrectionCompute(provider, raw_cache),
                              raw_cache, LRUByteCache(8 << 20))
    try:
        key = _key(provider, "CD3", h._tile(0, 1, 1))
        got = []
        import threading
        done = threading.Event()

        def cb(result):
            got.append(result)
            done.set()
        scheduler.request(TileRequest(key=key, priority=0, generation="g"), cb)
        assert done.wait(10)
        cold = got[-1]
        assert cold.error is None and isinstance(cold.pixels.handle, nt.NativeTile)
        assert cold.pixels.dtype == "uint16"
        assert raw_cache.get(key) is cold.pixels.handle
        reads = len(raw.reads)
        scheduler.request(TileRequest(key=key, priority=0, generation="g"), cb)
        hit = got[-1]
        assert hit.timing.get("cache") == "hit" and hit.pixels.handle is cold.pixels.handle
        assert len(raw.reads) == reads, "a cache hit read again"
        # the float tile of the same place is its own entry, side by side
        ordinary = _key(provider, "CD3", h._tile(0, 1, 1), native=False)
        assert raw_cache.get(ordinary) is None
        done.clear()
        scheduler.request(TileRequest(key=ordinary, priority=0, generation="g"), cb)
        assert done.wait(10)
        both = (raw_cache.get(key), raw_cache.get(ordinary))
        assert isinstance(both[0], nt.NativeTile) and both[1].dtype == np.float32
        assert raw_cache.stats()["bytes"] == both[0].nbytes + both[1].nbytes
        assert both[0].nbytes * 2 == both[1].nbytes          # uint16 vs float32
    finally:
        scheduler.shutdown()


def test_a_native_identity_cannot_be_corrected():
    from block01.viewer.caches import LRUByteCache
    from block01.viewer.correction_compute import CorrectionCompute
    from block01.viewer.scheduler import TileScheduler

    provider, _raw = _provider(np.uint8)
    raw_cache = LRUByteCache(1 << 20)
    scheduler = TileScheduler(provider, CorrectionCompute(provider, raw_cache),
                              raw_cache, LRUByteCache(1 << 20))
    try:
        key = CorrectionKey(source=provider.native_source_identity(), channel="CD3",
                            tile=h._tile(0, 0, 0), method="tophat", params=(5,),
                            algorithm_version="v")
        with pytest.raises(ValueError):
            scheduler.request(TileRequest(key=key, priority=0, generation="g"), lambda r: None)
    finally:
        scheduler.shutdown()


def test_the_tile_is_a_valid_byte_bounded_cache_value():
    tile = nt.NativeTile(np.zeros((4, 6), np.uint8), (0, 2, 0, 3), nt.KIND_RAW)
    assert tile.nbytes == 24 and tile.shape == (4, 6) and tile.dtype == np.uint8
    with pytest.raises(ValueError):
        nt.NativeTile(np.zeros((4, 4), np.float64), nt.FINITE, nt.KIND_CORRECTED)
    with pytest.raises(ValueError):
        nt.NativeTile(np.zeros((4, 4), np.uint8), nt.FINITE, nt.KIND_RAW)
