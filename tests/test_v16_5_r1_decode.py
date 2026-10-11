"""v16_5 R1 (plan §3, §7 gate 1): the Rust core reads every raw TIFF the
Python direct path reads, byte for byte -- now zstd, and 16/32-bit integer
and float samples, predictor 2 included.

Two oracles per file: the Python direct path (`RawTileProvider` as Step1
reads it) and tifffile's own decoding (the zarr path). A float sample under
predictor 2 is the same-width integer's bit-pattern difference (imagecodecs
`delta_decode`), and the fixture checks the file really carries predictor 2.
8-byte samples (uint64 here) under predictor 2 are refused (they stay with Python).
"""

import numpy as np
import pytest

tifffile = pytest.importorskip("tifffile")
pytest.importorskip("imagecodecs")
core = pytest.importorskip("fusion_viewer_core")

from block01.ui.step1_core_adapter import build_raw_manifest  # noqa: E402
from block01.viewer.raw_tile_provider import RawTileProvider  # noqa: E402

SHAPE = (150, 113)                    # not a multiple of the blocks
DTYPES = [np.uint8, np.uint16, np.int16, np.uint32, np.int32, np.float32]


def _data(dtype, seed):
    rng = np.random.default_rng(seed)
    if np.dtype(dtype).kind == "f":
        values = rng.normal(0, 1000, size=(3,) + SHAPE).astype(dtype)
        values[0, :3, :3] = [[np.nan, np.inf, -np.inf], [0.0, -0.0, 1e-38], [3e38, -3e38, 1.0]]
        return values
    info = np.iinfo(dtype)
    return rng.integers(info.min, info.max, size=(3,) + SHAPE, dtype=dtype, endpoint=True)


def _ome(path, data, *, compression, predictor=None, tile=(32, 32), byteorder="<",
         rowsperstrip=None, levels=3):
    float_predictor = data.dtype.kind == "f" and predictor == 2
    if float_predictor:
        # tifffile refuses predictor 2 on floats, but such files exist and
        # imagecodecs decodes them as bit-pattern differences: write the
        # same bits as the same-width SIGNED integer (whose wrapping
        # differences are the same bits, and for which tifffile writes a
        # SampleFormat tag, 2), then mark the samples float (3)
        width = data.dtype.itemsize
        data = data.view(np.dtype(f"i{width}"))
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
    if float_predictor:
        _mark_float(str(path), width)
    with tifffile.TiffFile(str(path)) as tf:           # the fixture is what it says
        page = tf.series[0].levels[0].pages[0]
        assert int(page.predictor) == int(predictor or 1)
        if float_predictor:
            assert np.dtype(page.dtype).kind == "f" and tf.series[0].dtype.kind == "f"
    return str(path)


def _mark_float(path, width):
    """Every page's SampleFormat 2 -> 3 (IEEE float), and the OME type."""
    with tifffile.TiffFile(path) as tf:
        spots = []
        for level in tf.series[0].levels:
            for page in level.pages:
                page = page.aspage() if hasattr(page, "aspage") else page
                tag = page.tags["SampleFormat"]
                assert tag.count == 1 and tag.value == 2
                spots.append(tag.valueoffset)
        order = tf.byteorder
    old, new = f'Type="int{width * 8}"'.encode(), b'Type="float"'
    assert len(old) == len(new)
    with open(path, "r+b") as fh:
        for spot in spots:
            fh.seek(spot)
            fh.write((3).to_bytes(2, "little" if order == "<" else "big"))
        fh.seek(0)
        blob = fh.read()
        at = blob.find(old)
        assert at > 0
        fh.seek(at)
        fh.write(new)


def _regions(h, w):
    yield 0, h, 0, w
    yield 0, 32, 0, 32
    yield 5, 41, 7, 70
    yield h - 9, h, w - 13, w
    yield 10, 10, 3, 20
    rng = np.random.default_rng(3)
    for _ in range(8):
        y0, x0 = int(rng.integers(0, h)), int(rng.integers(0, w))
        yield y0, int(rng.integers(y0, h + 1)), x0, int(rng.integers(x0, w + 1))


def _assert_rust_matches(path, monkeypatch, *, direct_expected=True):
    provider = RawTileProvider(path)
    monkeypatch.setenv("BLOCK01_DIRECT_TILES", "0")
    ref = RawTileProvider(path)
    monkeypatch.delenv("BLOCK01_DIRECT_TILES")
    try:
        if direct_expected:
            assert all(provider._direct_plan(l) is not None for l in range(provider.num_levels))
        reader = core.RawReader(build_raw_manifest(provider))
        for level in range(provider.num_levels):
            h, w = provider.level_shape(level)
            for c in range(provider.num_channels):
                for y0, y1, x0, x1 in _regions(h, w):
                    direct, _ = provider.read_region(c, level, y0, y1, x0, x1)
                    zarr, _ = ref.read_region(c, level, y0, y1, x0, x1)
                    # the core takes regions already clamped, as
                    # `read_region` clamps before it reads
                    cy0, cy1 = (max(0, min(v, h)) for v in (y0, y1))
                    cx0, cx1 = (max(0, min(v, w)) for v in (x0, x1))
                    got = reader.read_region(c, level, cy0, cy1, cx0, cx1)
                    want = np.ascontiguousarray(direct).tobytes()
                    assert want == np.ascontiguousarray(zarr).tobytes()
                    assert got == want, (level, c, (y0, y1, x0, x1))
    finally:
        provider.close()
        ref.close()


@pytest.mark.parametrize("layout", ["tiles", "strips"])
@pytest.mark.parametrize("byteorder", ["<", ">"])
@pytest.mark.parametrize("dtype", DTYPES)
@pytest.mark.parametrize("predictor", [None, 2])
@pytest.mark.parametrize("compression", ["zstd", "lzw", "zlib", None])
def test_a_the_full_matrix(tmp_path, monkeypatch, compression, predictor, dtype, byteorder, layout):
    """Plan §7 gate 1: every codec x predictor x sample type x byte order x
    tiled / stripped layout (strips are read by Python through zarr, by the
    core from the strip tables)."""
    if compression is None and predictor == 2:
        pytest.skip("a predictor needs compression (TIFF; tifffile refuses it)")
    data = _data(dtype, 1)
    tiled = layout == "tiles"
    path = _ome(tmp_path / "m.ome.tif", data, compression=compression, predictor=predictor,
                byteorder=byteorder, tile=(32, 32) if tiled else None,
                rowsperstrip=None if tiled else 7)
    _assert_rust_matches(path, monkeypatch, direct_expected=tiled)


def test_e_256_pixel_tiles(tmp_path, monkeypatch):
    rng = np.random.default_rng(5)
    data = rng.integers(0, 255, size=(3, 700, 530), dtype=np.uint8)
    _assert_rust_matches(_ome(tmp_path / "t256.ome.tif", data, compression="lzw",
                              tile=(256, 256), levels=2), monkeypatch)


def test_f_predictor_on_8_byte_samples_is_refused(tmp_path):
    """No 8-byte OME fixture (tifffile's OME writer has no uint64): the
    manifest of a real file, its sample width changed to 8."""
    data = np.zeros((3, 64, 64), np.uint16)
    path = _ome(tmp_path / "w8.ome.tif", data, compression="zlib", predictor=2, levels=1)
    provider = RawTileProvider(path)
    try:
        manifest = build_raw_manifest(provider)
        manifest["levels"][0]["sample_bytes"] = 8
        with pytest.raises(ValueError, match="predictor 2 on 8-byte"):
            core.RawReader(manifest)
    finally:
        provider.close()
