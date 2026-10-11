"""v16_5 R0: the Rust core reads raw TIFF pixels EXACTLY as Python does.

`fusion_viewer_core.RawReader`, opened from `build_raw_manifest`, must give
the same bytes as `RawTileProvider` read through tifffile's own decoding
(the zarr path) for every region shape, every compression / predictor /
sample size / byte order the core takes, tiled and stripped pages. A file
that changed after the manifest was built is refused. The two real slides
are sampled when present (the full sweep is scripts/r0_decode_probe.py).
"""

import os

import numpy as np
import pytest

tifffile = pytest.importorskip("tifffile")
pytest.importorskip("imagecodecs")
core = pytest.importorskip("fusion_viewer_core")

from block01.ui.step1_core_adapter import build_raw_manifest  # noqa: E402
from block01.viewer.raw_tile_provider import RawTileProvider  # noqa: E402

SHAPE = (150, 113)                    # not a multiple of the 32-px blocks
REAL = ("/sdb1/cop/Kevin/TMA3/FINALRUN_updated_TMA3_Scan1_bgcorrected.ome.tiff",
        "/sdb1/cop/Kevin/FINALRUN_updated_TMA4_Scan1.qptiff")


def _ome(path, data, *, compression, predictor=None, tile=(32, 32), levels=3,
         byteorder="<", rowsperstrip=None):
    kw = dict(compression=compression)
    if predictor is not None:
        kw["predictor"] = predictor
    if tile is not None:
        kw["tile"] = tile
    else:
        kw["rowsperstrip"] = rowsperstrip
    with tifffile.TiffWriter(str(path), ome=True, byteorder=byteorder) as tw:
        tw.write(data, subifds=levels - 1,
                 metadata={"axes": "CYX", "Channel": {"Name": ["a", "b", "c"]}}, **kw)
        cur = data
        for _ in range(levels - 1):
            h, w = cur.shape[-2:]
            cur = np.ascontiguousarray(cur[:, :h // 2 * 2:2, :w // 2 * 2:2])
            tw.write(cur, subfiletype=1, **kw)
    return str(path)


def _regions(h, w):
    yield 0, h, 0, w                                  # everything
    yield 0, 32, 0, 32                                # one aligned block
    yield 5, 41, 7, 70                                # unaligned, across blocks
    yield h - 9, h, w - 13, w                         # the ragged corner
    yield 10, 10, 3, 20                               # empty
    yield 10, 5, 0, 4                                 # inverted: empty too
    rng = np.random.default_rng(3)
    for _ in range(10):
        y0, x0 = int(rng.integers(0, h)), int(rng.integers(0, w))
        yield y0, int(rng.integers(y0, h + 1)), x0, int(rng.integers(x0, w + 1))


def _zarr_reference(path, monkeypatch):
    monkeypatch.setenv("BLOCK01_DIRECT_TILES", "0")
    ref = RawTileProvider(path)
    monkeypatch.delenv("BLOCK01_DIRECT_TILES")
    return ref


def _assert_rust_matches(path, monkeypatch):
    provider = RawTileProvider(path)
    ref = _zarr_reference(path, monkeypatch)
    try:
        reader = core.RawReader(build_raw_manifest(provider))
        for level in range(provider.num_levels):
            h, w = provider.level_shape(level)
            for c in range(provider.num_channels):
                for y0, y1, x0, x1 in _regions(h, w):
                    want, _ = ref.read_region(c, level, y0, y1, x0, x1)
                    # the Rust reader takes regions already clamped, as
                    # `read_region` clamps them before reading
                    cy0, cy1 = (max(0, min(v, h)) for v in (y0, y1))
                    cx0, cx1 = (max(0, min(v, w)) for v in (x0, x1))
                    got = reader.read_region(c, level, cy0, cy1, cx0, cx1)
                    assert got == np.ascontiguousarray(want).tobytes(), (level, c, (y0, y1, x0, x1))
    finally:
        provider.close()
        ref.close()


@pytest.mark.parametrize("dtype", [np.uint8, np.uint16])
@pytest.mark.parametrize("compression,predictor", [
    ("lzw", None), ("lzw", 2), ("zlib", None), ("zlib", 2), (None, None)])
def test_a_tiled_pages_read_the_same_bytes(tmp_path, monkeypatch, dtype, compression, predictor):
    rng = np.random.default_rng(1)
    data = rng.integers(0, np.iinfo(dtype).max, size=(3,) + SHAPE, dtype=dtype)
    _assert_rust_matches(_ome(tmp_path / "t.ome.tif", data, compression=compression,
                              predictor=predictor), monkeypatch)


@pytest.mark.parametrize("predictor", [None, 2])
def test_b_big_endian_samples_come_back_native(tmp_path, monkeypatch, predictor):
    rng = np.random.default_rng(2)
    data = rng.integers(0, 65535, size=(3,) + SHAPE, dtype=np.uint16)
    _assert_rust_matches(_ome(tmp_path / "be.ome.tif", data, compression="zlib",
                              predictor=predictor, byteorder=">"), monkeypatch)


@pytest.mark.parametrize("compression", ["lzw", "zlib", None])
def test_c_stripped_pages_read_the_same_bytes(tmp_path, monkeypatch, compression):
    """Kevin's qptiff stores its coarsest level in strips; the last strip
    of a page holds fewer rows (150 = 21 x 7 + 3)."""
    rng = np.random.default_rng(4)
    data = rng.integers(0, 255, size=(3,) + SHAPE, dtype=np.uint8)
    _assert_rust_matches(_ome(tmp_path / "s.ome.tif", data, compression=compression,
                              tile=None, rowsperstrip=7), monkeypatch)


def test_d_a_file_changed_after_the_manifest_is_refused(tmp_path):
    data = np.zeros((3,) + SHAPE, np.uint8)
    path = _ome(tmp_path / "m.ome.tif", data, compression="zlib")
    provider = RawTileProvider(path)
    manifest = build_raw_manifest(provider)
    provider.close()
    core.RawReader(dict(manifest))                    # unchanged: opens
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    with pytest.raises(ValueError, match="changed since it was parsed"):
        core.RawReader(dict(manifest))


def test_d2_a_file_replaced_under_an_open_provider_is_refused(tmp_path):
    """The provider parsed its block tables from the first file; a new file
    moved onto the path afterwards must not be paired with those tables."""
    rng = np.random.default_rng(6)
    path = _ome(tmp_path / "r.ome.tif", np.zeros((3,) + SHAPE, np.uint8), compression="zlib")
    provider = RawTileProvider(path)
    provider.warm_thread_handle()                     # tables built, descriptor open
    other = _ome(tmp_path / "other.ome.tif",
                 rng.integers(0, 255, size=(3,) + SHAPE, dtype=np.uint8), compression="lzw")
    os.replace(other, path)
    try:
        with pytest.raises(ValueError, match="changed after this provider opened it"):
            build_raw_manifest(provider)
    finally:
        provider.close()


def test_d3_an_all_stripped_file_replaced_under_its_provider_is_refused(tmp_path):
    """No tiled level means no direct-read descriptor; the provider's own
    identity (its cache keys) still decides."""
    rng = np.random.default_rng(8)
    path = _ome(tmp_path / "rs.ome.tif", np.zeros((3,) + SHAPE, np.uint8), compression="zlib",
                tile=None, rowsperstrip=7)
    provider = RawTileProvider(path)
    provider.source_identity()                        # what its cache keys say
    assert provider._direct_plan(0) is None
    other = _ome(tmp_path / "os.ome.tif",
                 rng.integers(0, 255, size=(3,) + SHAPE, dtype=np.uint8), compression="zlib",
                 tile=None, rowsperstrip=7)
    os.replace(other, path)
    try:
        with pytest.raises(ValueError, match="changed after this provider opened it"):
            build_raw_manifest(provider)
    finally:
        provider.close()


def test_e_a_request_outside_the_manifest_is_an_error_not_a_crash(tmp_path):
    data = np.zeros((3,) + SHAPE, np.uint8)
    path = _ome(tmp_path / "b.ome.tif", data, compression="zlib")
    provider = RawTileProvider(path)
    reader = core.RawReader(build_raw_manifest(provider))
    provider.close()
    with pytest.raises(ValueError, match="bounds"):
        reader.read_region(3, 0, 0, 8, 0, 8)          # no channel 3
    with pytest.raises(ValueError, match="bounds"):
        reader.read_region(0, 0, 0, SHAPE[0] + 1, 0, 8)
    with pytest.raises(ValueError, match="bounds"):
        reader.read_region(0, 9, 0, 8, 0, 8)          # no level 9


@pytest.mark.parametrize("path", REAL)
def test_f_real_slides_sampled(path):
    if not os.path.exists(path):
        pytest.skip(f"{path} is not on this machine")
    provider = RawTileProvider(path)
    try:
        reader = core.RawReader(build_raw_manifest(provider))
        rng = np.random.default_rng(9)
        for level in range(provider.num_levels):
            h, w = provider.level_shape(level)
            for _ in range(20):
                c = int(rng.integers(0, provider.num_channels))
                ty, tx = int(rng.integers(0, -(-h // 512))), int(rng.integers(0, -(-w // 512)))
                y0, x0 = ty * 512, tx * 512
                y1, x1 = min(h, y0 + 512), min(w, x0 + 512)
                want, _ = provider.read_region(c, level, y0, y1, x0, x1)
                assert reader.read_region(c, level, y0, y1, x0, x1) == want.tobytes(), \
                    (level, c, ty, tx)
    finally:
        provider.close()
