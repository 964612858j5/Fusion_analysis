"""v16_5 R1 (plan §3, §7 gate 2): corrected products decode byte for byte.

* Blosc-LZ4 frames as numcodecs writes them: every type width, shuffle on
  and off, automatic and fixed block sizes (split and unsplit blocks, a
  short last block, the bytes past the last whole element), memcpyed
  frames, compressible and incompressible data -- each against
  numcodecs. Bit shuffle and other codecs are refused.
* zarr v2 arrays read through `ZarrArray.read_rect` against zarr itself:
  edge chunks, missing chunks (fill_value 0, a non-zero value, NaN); the
  '/' dimension separator is refused (plan §2.2 takes '.' only).
* The real product on the server, every chunk, when present.
"""

import os

import numpy as np
import pytest

numcodecs = pytest.importorskip("numcodecs")
zarr = pytest.importorskip("zarr")
core = pytest.importorskip("fusion_viewer_core")

from numcodecs import Blosc  # noqa: E402

from block01.ui.step1_core_adapter import _array_spec  # noqa: E402

REAL = "/sda1/Fusion/tmp/rois/full_wsi_20260810_051003_7a0d/step0/corrected_channels.zarr/Full_WSI"


def _datas(dtype, n, rng):
    yield rng.integers(0, 255, n * np.dtype(dtype).itemsize, dtype=np.uint8).view(dtype)
    yield np.zeros(n, dtype)
    yield (np.sin(np.arange(n) / 50) * 100).astype(dtype)
    yield np.repeat(rng.integers(0, 3, n // 7 + 1), 7)[:n].astype(dtype)


@pytest.mark.parametrize("dtype", ["u1", "<u2", "<f4", "<f8"])
def test_a_blosc_frames_decode_like_numcodecs(dtype):
    rng = np.random.default_rng(0)
    seen = set()
    for n in (1, 7, 1000, 4097, 262157):
        for data in _datas(dtype, n, rng):
            for shuffle in (Blosc.NOSHUFFLE, Blosc.SHUFFLE):
                for blocksize in (0, 128, 1001, 65536):
                    for clevel in (0, 1, 9):
                        frame = Blosc(cname="lz4", clevel=clevel, shuffle=shuffle,
                                      blocksize=blocksize).encode(data)
                        seen.add(frame[2])
                        assert core.blosc_decode_for_test(frame) == data.tobytes(), \
                            (n, shuffle, blocksize, clevel)
    # flags: shuffle (0x01), memcpyed (0x02), don't-split (0x10) all occurred
    assert {f & 0x01 for f in seen} == {0, 1} or dtype == "u1"
    assert any(f & 0x02 for f in seen) and any(f & 0x10 for f in seen) and any(not f & 0x10 for f in seen)


def test_b_what_is_not_taken_is_refused():
    data = np.arange(5000, dtype="<f4")
    for codec in (Blosc(cname="lz4", shuffle=Blosc.BITSHUFFLE), Blosc(cname="zstd"),
                  Blosc(cname="zlib"), Blosc(cname="blosclz")):
        with pytest.raises(ValueError):
            core.blosc_decode_for_test(codec.encode(data))
    frame = Blosc(cname="lz4").encode(data)
    for cut in (0, 10, 16, len(frame) // 2):
        with pytest.raises(ValueError):
            core.blosc_decode_for_test(frame[:cut])


def _array(tmp_path, *, fill, separator, missing):
    store = zarr.DirectoryStore(str(tmp_path / "p.zarr"), dimension_separator=separator)
    root = zarr.group(store=store, overwrite=True)
    arr = root.create_dataset("ROI_1/x", shape=(700, 530), chunks=(256, 200), dtype="<f4",
                              fill_value=fill, compressor=Blosc(cname="lz4", clevel=5, shuffle=Blosc.SHUFFLE))
    rng = np.random.default_rng(1)
    arr[:, :] = rng.normal(0, 10, size=(700, 530)).astype("<f4")
    folder = os.path.join(str(tmp_path / "p.zarr"), "ROI_1", "x")
    for cy, cx in missing:
        os.remove(os.path.join(folder, f"{cy}{separator}{cx}"))
    return zarr.open_array(store=zarr.DirectoryStore(str(tmp_path / "p.zarr"), dimension_separator=separator),
                           path="ROI_1/x", mode="r")


@pytest.mark.parametrize("fill", [0.0, 7.5, float("nan")])
@pytest.mark.parametrize("separator", ["."])
def test_c_zarr_arrays_read_like_zarr(tmp_path, fill, separator):
    arr = _array(tmp_path, fill=fill, separator=separator, missing=[(0, 1), (2, 2)])
    spec = _array_spec(arr)
    reader = core.ZarrArray(spec)
    rng = np.random.default_rng(2)
    rects = [(0, 700, 0, 530), (0, 256, 0, 200), (250, 520, 190, 410), (690, 700, 520, 530),
             (5, 5, 0, 10)]
    rects += [(y0, int(rng.integers(y0, 701)), x0, int(rng.integers(x0, 531)))
              for y0, x0 in zip(rng.integers(0, 700, 20), rng.integers(0, 530, 20))]
    for y0, y1, x0, x1 in rects:
        want = np.ascontiguousarray(arr[y0:y1, x0:x1], "<f4").tobytes()
        assert reader.read_rect(y0, y1, x0, x1) == want, (y0, y1, x0, x1)


def test_c2_the_slash_separator_is_refused(tmp_path):
    arr = _array(tmp_path, fill=0.0, separator="/", missing=[])
    with pytest.raises(ValueError, match="separator"):
        core.ZarrArray(_array_spec(arr))


def test_c3_a_memcpyed_frame_must_add_up():
    """flags memcpyed, 3 bytes claimed in a 16+16-byte frame: numcodecs
    refuses it, so does the core."""
    frame = bytes([2, 1, 0x22, 1]) + (3).to_bytes(4, "little") + (3).to_bytes(4, "little") \
        + (16).to_bytes(4, "little") + b"abc"
    with pytest.raises(Exception):
        Blosc().decode(frame)
    with pytest.raises(ValueError):
        core.blosc_decode_for_test(frame)


def test_d_a_missing_fill_value_is_refused(tmp_path):
    arr = _array(tmp_path, fill=0.0, separator=".", missing=[])
    folder = os.path.join(str(tmp_path / "p.zarr"), "ROI_1", "x")
    import json
    with open(os.path.join(folder, ".zarray")) as fh:
        meta = json.load(fh)
    meta["fill_value"] = None
    with open(os.path.join(folder, ".zarray"), "w") as fh:
        json.dump(meta, fh)
    with pytest.raises(ValueError, match="fill_value"):
        _array_spec(zarr.open_array(str(folder), mode="r"))


@pytest.mark.skipif(not os.path.isdir(REAL), reason="the server's real product is not here")
def test_e_every_chunk_of_the_real_product():
    count = 0
    for channel in sorted(os.listdir(REAL)):
        folder = os.path.join(REAL, channel)
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            if name.startswith("."):
                continue
            with open(os.path.join(folder, name), "rb") as fh:
                frame = fh.read()
            assert core.blosc_decode_for_test(frame) == bytes(Blosc().decode(frame)), (channel, name)
            count += 1
    assert count == 1320
