"""Block A9-P: the viewers read through `PixelSource`, and every pixel is
bitwise the one the old reader returned (the old path is the oracle, plan
v2.4 §0.4 rule 2)."""

import numpy as np
import pytest

tifffile = pytest.importorskip("tifffile")
zarr = pytest.importorskip("zarr")

from block01.viewer.raw_tile_provider import RawTileProvider  # noqa: E402
from block01.viewer.source_tile_provider import (SourceTileProvider,  # noqa: E402
                                                 open_viewer_source)
from block01.viewer.array_pixel_source import ArrayRegionSource  # noqa: E402
from block01.viewer.step1_source import CoarsePlane, CorrectedRegion  # noqa: E402
from block01.viewer.tile_types import TileAddress, TileGridSpec  # noqa: E402
from test_v16_pixel_source import _data, _write  # noqa: E402


@pytest.fixture(scope="module", params=[("uint8", 3, (64, 64)), ("uint16", 3, (32, 48))])
def slide(request, tmp_path_factory):
    dtype, levels, tile = request.param
    data = _data(dtype)
    path = tmp_path_factory.mktemp("a9p") / f"slide_{dtype}.ome.tif"
    _write(path, data, levels, tile)
    return str(path)


def _regions(h, w, seed=3, n=60):
    rng = np.random.default_rng(seed)
    out = [(0, h, 0, w), (0, 1, 0, 1), (h - 1, h, w - 1, w), (-5, 7, -9, 4),
           (h - 3, h + 40, w - 2, w + 50), (h, h + 5, 0, 5), (0, 5, w, w + 5),
           (-10, -1, 0, 5), (3, 3, 0, 9), (10, 5, 0, 4), (0, 4, 9, 2)]   # reversed: empty
    for _ in range(n):
        y0, x0 = int(rng.integers(-20, h)), int(rng.integers(-20, w))
        out.append((y0, y0 + int(rng.integers(0, 80)), x0, x0 + int(rng.integers(0, 80))))
    return out


def test_regions_and_tiles_equal_the_old_reader_on_every_level(slide):
    old = RawTileProvider(slide)
    new = open_viewer_source(slide)
    try:
        assert isinstance(new, SourceTileProvider)
        assert new.num_levels == old.num_levels and new.channel_names == old.channel_names
        assert new.source_identity() == old.source_identity()
        for level in range(old.num_levels):
            h, w = old.level_shape(level)
            assert new.level_shape(level) == (h, w)
            assert new.level_downsample(level) == old.level_downsample(level)
            for channel in (0, old.channel_names[-1], 1):
                for r in _regions(h, w):
                    a, ao = old.read_region(channel, level, *r)
                    b, bo = new.read_region(channel, level, *r)
                    assert ao == bo and a.dtype == b.dtype and a.shape == b.shape, (level, r)
                    assert np.array_equal(a, b), (level, channel, r)
            grid = TileGridSpec(tile_size=16, source_chunk_shape=(), grid_version="v1")
            for ty in range(-(-h // 16)):
                for tx in range(-(-w // 16)):
                    t = TileAddress(level=level, tx=tx, ty=ty, grid=grid)
                    a, _ = old.read_tile(1, t)
                    b, _ = new.read_tile(1, t)
                    assert a.dtype == b.dtype and np.array_equal(a, b)
    finally:
        old.close()
        new.close()


def test_the_new_path_reads_through_the_source(slide, monkeypatch):
    new = open_viewer_source(slide)
    calls = []
    real = new.source.read_region
    monkeypatch.setattr(new.source, "read_region",
                        lambda *a: (calls.append(a), real(*a))[1])
    try:
        new.read_region(0, 0, 0, 10, 0, 10)
        assert calls and calls[-1][1:] == (0, 0, 10, 0, 10)
    finally:
        new.close()


def test_a_bad_channel_and_a_closed_reader_are_refused_as_before(slide):
    new = open_viewer_source(slide)
    old = RawTileProvider(slide)
    for p in (old, new):
        with pytest.raises(Exception):
            p.read_region("no-such-channel", 0, 0, 4, 0, 4)
        with pytest.raises(Exception):
            p.read_region("no-such-channel", 0, 3, 3, 0, 4)      # empty clamp too
    new.close()
    old.close()
    with pytest.raises(RuntimeError):
        new.read_region(0, 0, 0, 4, 0, 4)
    with pytest.raises(RuntimeError):
        new.read_region(0, 0, 3, 3, 0, 4)


def test_a_stand_in_provider_needs_no_tiff(monkeypatch):
    """A test rig's provider (no file behind it) still works through the
    source: nothing reads TIFF metadata it does not need."""
    from block01.viewer import raw_tile_provider as rtp

    class _Stub:
        path = "/nowhere.ome.tif"
        num_levels, num_channels, channel_names = 1, 2, ["A", "B"]

        def level_shape(self, level):
            return (8, 8)

        def read_region(self, channel, level, y0, y1, x0, x1):
            return np.full((y1 - y0, x1 - x0), 7, np.uint8), (y0, x0)

        def close(self):
            pass
    monkeypatch.setattr(rtp, "RawTileProvider", lambda _path: _Stub())
    p = open_viewer_source("/nowhere.ome.tif")
    arr, origin = p.read_region("B", 0, 2, 20, 1, 5)
    assert origin == (2, 1) and arr.shape == (6, 4) and (arr == 7).all()
    p.close()


def test_corrected_regions_and_coarse_planes_read_the_same_pixels(tmp_path):
    rng = np.random.default_rng(9)
    z = zarr.open(str(tmp_path / "c.zarr"), mode="w", shape=(70, 90), chunks=(32, 32),
                  dtype=np.float32)
    z[:] = rng.random((70, 90), dtype=np.float32)
    region = CorrectedRegion(z, (100, 170, 50, 140))
    assert isinstance(region.source, ArrayRegionSource)
    for r in _regions(260, 260, seed=4):
        y0, y1, x0, x1 = (v + 80 for v in r)
        got, placed = region.read(y0, y1, x0, x1)
        oy0, oy1 = max(y0, 100), min(y1, 170)
        ox0, ox1 = max(x0, 50), min(x1, 140)
        if oy1 <= oy0 or ox1 <= ox0:
            assert got is None and placed is None
            continue
        want = np.asarray(z[oy0 - 100:oy1 - 100, ox0 - 50:ox1 - 50], dtype=np.float32)
        assert placed == (oy0, oy1, ox0, ox1) and got.dtype == np.float32
        assert np.array_equal(got, want)
    plane = CoarsePlane(z, 4, (5, 7))
    for r in _regions(120, 120, seed=5):
        values, valid = plane.tile(r)
        y0, y1, x0, x1 = r
        h, w = max(0, y1 - y0), max(0, x1 - x0)
        want = np.zeros((h, w), np.float32)
        ok = np.zeros((h, w), bool)
        sy0, sy1 = max(y0 - 5, 0), min(y1 - 5, 70)
        sx0, sx1 = max(x0 - 7, 0), min(x1 - 7, 90)
        if h and w and sy1 > sy0 and sx1 > sx0:
            oy, ox = sy0 + 5 - y0, sx0 + 7 - x0
            want[oy:oy + sy1 - sy0, ox:ox + sx1 - sx0] = z[sy0:sy1, sx0:sx1]
            ok[oy:oy + sy1 - sy0, ox:ox + sx1 - sx0] = True
        assert np.array_equal(values, want) and np.array_equal(valid, ok), r
