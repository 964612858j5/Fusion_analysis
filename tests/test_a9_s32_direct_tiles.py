"""Block A9 §32 (Odon: decode the file's own tile): `RawTileProvider` reads
a simple tiled level by `pread` + imagecodecs instead of tifffile's zarr
store. The pixels must be EXACTLY the zarr path's, for every region shape
(aligned, unaligned, edge, empty), every supported compression / predictor
/ dtype, and an unsupported layout must keep the zarr path."""

import numpy as np
import pytest

tifffile = pytest.importorskip("tifffile")
pytest.importorskip("imagecodecs")

import test_v16_qpi_source as q  # noqa: E402  (QPTIFF / OME writers)
from block01.viewer.raw_tile_provider import RawTileProvider  # noqa: E402

SHAPE = (150, 113)                    # not a multiple of the 32-px tiles


def _ome(path, data, *, compression, predictor=None, tile=(32, 32), levels=3):
    kw = dict(compression=compression)
    if predictor is not None:
        kw["predictor"] = predictor
    with tifffile.TiffWriter(str(path), ome=True) as tw:
        tw.write(data, subifds=levels - 1, tile=tile,
                 metadata={"axes": "CYX", "Channel": {"Name": ["a", "b", "c"]}}, **kw)
        cur = data
        for _ in range(levels - 1):
            h, w = cur.shape[-2:]
            cur = np.ascontiguousarray(cur[:, :h // 2 * 2:2, :w // 2 * 2:2])
            tw.write(cur, subfiletype=1, tile=tile, **kw)
    return str(path)


def _regions(h, w):
    yield 0, h, 0, w                                  # everything
    yield 0, 32, 0, 32                                # one aligned tile
    yield 32, 64, 64, 96
    yield 5, 41, 7, 70                                # unaligned, across tiles
    yield h - 9, h, w - 13, w                         # the ragged corner
    yield 10, 10, 3, 20                               # empty
    yield 10, 5, 0, 4                                 # inverted: empty too
    yield 3, 9, 20, 2
    rng = np.random.default_rng(3)
    for _ in range(12):
        y0, x0 = int(rng.integers(0, h)), int(rng.integers(0, w))
        yield y0, int(rng.integers(y0, h + 1)), x0, int(rng.integers(x0, w + 1))


def _both(path, monkeypatch):
    direct = RawTileProvider(path)
    monkeypatch.setenv("BLOCK01_DIRECT_TILES", "0")
    viazarr = RawTileProvider(path)
    monkeypatch.delenv("BLOCK01_DIRECT_TILES")
    return direct, viazarr


def _assert_same(direct, viazarr, *, expect_direct=True):
    try:
        for level in range(len(direct._level_shapes)):
            h, w = direct.level_shape(level)
            for c in range(direct._num_channels):
                for y0, y1, x0, x1 in _regions(h, w):
                    got, off = direct.read_region(c, level, y0, y1, x0, x1)
                    ref, ref_off = viazarr.read_region(c, level, y0, y1, x0, x1)
                    assert off == ref_off
                    assert got.dtype == ref.dtype and got.shape == ref.shape
                    assert np.array_equal(got, ref), (level, c, (y0, y1, x0, x1))
            assert bool(direct._chunk_plans.get(level)) is expect_direct, level
            assert not viazarr._chunk_plans.get(level)
    finally:
        direct.close()
        viazarr.close()


@pytest.mark.parametrize("dtype", [np.uint8, np.uint16])
@pytest.mark.parametrize("compression,predictor", [
    ("lzw", None), ("lzw", 2), ("zlib", None), ("zlib", 2), ("zstd", None), (None, None)])
def test_a_direct_read_is_exactly_the_zarr_read(tmp_path, monkeypatch, dtype, compression, predictor):
    rng = np.random.default_rng(1)
    data = rng.integers(0, np.iinfo(dtype).max, size=(3,) + SHAPE, dtype=dtype)
    path = _ome(tmp_path / "s.ome.tif", data, compression=compression, predictor=predictor)
    _assert_same(*_both(path, monkeypatch))


def test_a_qptiff_reads_directly_and_its_strip_level_through_zarr(tmp_path, monkeypatch):
    data = q._data()
    path = q.write_qpi(tmp_path / "slide.qptiff", data)    # last level in strips
    direct, viazarr = _both(path, monkeypatch)
    last = len(direct._level_shapes) - 1
    try:
        for level in range(last + 1):
            h, w = direct.level_shape(level)
            for c in range(direct._num_channels):
                for y0, y1, x0, x1 in _regions(h, w):
                    got, _ = direct.read_region(c, level, y0, y1, x0, x1)
                    ref, _ = viazarr.read_region(c, level, y0, y1, x0, x1)
                    assert np.array_equal(got, ref), (level, c)
        assert all(direct._chunk_plans[level] for level in range(last))
        assert direct._chunk_plans[last] is False, "a strip level keeps the zarr path"
    finally:
        direct.close()
        viazarr.close()


def test_an_unsupported_compression_keeps_the_zarr_path(tmp_path, monkeypatch):
    rng = np.random.default_rng(2)
    data = rng.integers(0, 255, size=(3,) + SHAPE, dtype=np.uint8)
    path = _ome(tmp_path / "s.ome.tif", data, compression="packbits")
    _assert_same(*_both(path, monkeypatch), expect_direct=False)


def test_a_closed_provider_releases_its_descriptor(tmp_path):
    rng = np.random.default_rng(4)
    data = rng.integers(0, 255, size=(3,) + SHAPE, dtype=np.uint8)
    provider = RawTileProvider(_ome(tmp_path / "s.ome.tif", data, compression="lzw"))
    provider.read_region(0, 0, 0, 32, 0, 32)
    assert provider._chunk_fd is not None
    provider.close()
    assert provider._chunk_fd is None
    with pytest.raises(RuntimeError):
        provider.read_region(0, 0, 0, 32, 0, 32)


def test_packed_twelve_bit_samples_keep_the_zarr_path(tmp_path, monkeypatch):
    imagecodecs = pytest.importorskip("imagecodecs")
    rng = np.random.default_rng(5)
    data = rng.integers(0, 4095, size=(3,) + SHAPE, dtype=np.uint16)
    path = tmp_path / "s12.ome.tif"
    try:
        with tifffile.TiffWriter(str(path), ome=True) as tw:
            tw.write(data, tile=(32, 32), bitspersample=12, compression="lzw",
                     metadata={"axes": "CYX"})
    except Exception as exc:                                  # noqa: BLE001
        pytest.skip(f"tifffile cannot write 12-bit samples here: {exc}")
    direct, viazarr = _both(str(path), monkeypatch)
    try:
        got, _ = direct.read_region(0, 0, 0, 40, 0, 40)
        ref, _ = viazarr.read_region(0, 0, 0, 40, 0, 40)
        assert np.array_equal(got, ref)
        assert not direct._chunk_plans.get(0), "packed samples are not read directly"
    finally:
        direct.close()
        viazarr.close()


def test_threads_reading_at_once_get_the_zarr_pixels(tmp_path, monkeypatch):
    import concurrent.futures
    rng = np.random.default_rng(6)
    data = rng.integers(0, 255, size=(3,) + SHAPE, dtype=np.uint8)
    path = _ome(tmp_path / "s.ome.tif", data, compression="lzw")
    direct, viazarr = _both(path, monkeypatch)
    jobs = [(c, level, y, x) for c in range(3) for level in range(3)
            for y in range(0, 64, 16) for x in range(0, 64, 16)]
    try:
        def read(job):
            c, level, y, x = job
            return direct.read_region(c, level, y, y + 40, x, x + 40)[0]
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            got = list(pool.map(read, jobs))
        for job, tile in zip(jobs, got):
            c, level, y, x = job
            assert np.array_equal(tile, viazarr.read_region(c, level, y, y + 40, x, x + 40)[0])
    finally:
        direct.close()
        viazarr.close()
