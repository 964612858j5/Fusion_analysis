"""v16_5 R1 (plan §2.7): what the GitHub build runs against the INSTALLED
wheel on Linux and Windows -- no GL, no server data, no other module of
this repository (only numpy, tifffile, imagecodecs, numcodecs).

* Raw TIFF blocks: tiled synthetic files (zstd / LZW / deflate / none x
  predictor 1/2 x u8/u16/f32 x byte order) through `RawReader`, the
  manifest built here from tifffile's page tables, against tifffile.
* Blosc-LZ4 frames as numcodecs writes them, against numcodecs.
* A zarr v2 array written here (`.zarray` + Blosc-LZ4 chunk files, one
  chunk missing) through `ZarrArray`, against the numpy array.
* The redraw notice: one coalesced byte on a socket pair, then close.
"""

import socket

import numpy as np
import pytest

tifffile = pytest.importorskip("tifffile")
pytest.importorskip("imagecodecs")
numcodecs = pytest.importorskip("numcodecs")
core = pytest.importorskip("fusion_viewer_core")

SHAPE = (150, 113)


def _manifest(path):
    """`build_raw_manifest`'s shape from tifffile alone (tiled pages)."""
    import os
    with tifffile.TiffFile(path) as tf:
        st = os.stat(path)
        levels = []
        for level in tf.series[0].levels:
            pages = [p.aspage() if hasattr(p, "aspage") else p for p in level.pages]
            first = pages[0]
            dtype = np.dtype(first.dtype)
            levels.append({
                "height": int(first.shape[-2]), "width": int(first.shape[-1]),
                "block_rows": int(first.tilelength), "block_cols": int(first.tilewidth),
                "stripped": False, "compression": int(first.compression),
                "predictor": int(first.predictor), "sample_bytes": dtype.itemsize,
                "kind": dtype.kind, "big_endian": tf.byteorder == ">", "channels": len(pages),
                "offsets": np.concatenate([np.asarray(p.dataoffsets) for p in pages]).astype("<u8").tobytes(),
                "counts": np.concatenate([np.asarray(p.databytecounts) for p in pages]).astype("<u8").tobytes(),
            })
        return {"path": os.path.abspath(path), "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                "file_number": st.st_ino, "dtype": str(tf.series[0].dtype), "levels": levels}


def _write(path, data, compression, predictor, byteorder):
    kw = dict(compression=compression, tile=(32, 32))
    if predictor:
        kw["predictor"] = predictor
    with tifffile.TiffWriter(str(path), byteorder=byteorder) as tw:
        tw.write(data, metadata={"axes": "CYX"}, **kw)
    return str(path)


@pytest.mark.parametrize("dtype", ["uint8", "uint16", "float32"])
@pytest.mark.parametrize("compression", ["zstd", "lzw", "zlib", None])
@pytest.mark.parametrize("predictor", [None, 2])
@pytest.mark.parametrize("byteorder", ["<", ">"])
def test_a_raw_blocks_match_tifffile(tmp_path, dtype, compression, predictor, byteorder):
    if predictor and (compression is None or dtype == "float32"):
        pytest.skip("tifffile writes no such file")
    rng = np.random.default_rng(1)
    if dtype == "float32":
        data = rng.normal(0, 100, size=(2,) + SHAPE).astype(dtype)
    else:
        data = rng.integers(0, np.iinfo(dtype).max, size=(2,) + SHAPE, dtype=dtype)
    path = _write(tmp_path / "t.tif", data, compression, predictor, byteorder)
    reader = core.RawReader(_manifest(path))
    want = tifffile.imread(path)
    for c in range(2):
        for y0, y1, x0, x1 in ((0, 150, 0, 113), (5, 41, 7, 70), (141, 150, 100, 113)):
            got = reader.read_region(c, 0, y0, y1, x0, x1)
            assert got == np.ascontiguousarray(want[c, y0:y1, x0:x1]).astype(want.dtype.newbyteorder("=")).tobytes()


@pytest.mark.parametrize("dtype", ["u1", "<u2", "<f4"])
def test_b_blosc_frames_match_numcodecs(dtype):
    from numcodecs import Blosc
    rng = np.random.default_rng(2)
    for n in (7, 4097, 70001):
        for data in (rng.integers(0, 255, n * np.dtype(dtype).itemsize, dtype=np.uint8).view(dtype),
                     (np.sin(np.arange(n) / 30) * 50).astype(dtype)):
            for shuffle in (Blosc.NOSHUFFLE, Blosc.SHUFFLE):
                for blocksize in (0, 1001):
                    frame = Blosc(cname="lz4", clevel=5, shuffle=shuffle, blocksize=blocksize).encode(data)
                    assert core.blosc_decode_for_test(frame) == data.tobytes()


def test_b2_a_zarr_array_reads_back(tmp_path):
    import json
    import os
    from numcodecs import Blosc
    data = np.random.default_rng(3).normal(0, 9, size=(300, 250)).astype("<f4")
    folder = tmp_path / "a"
    folder.mkdir()
    meta = {"zarr_format": 2, "shape": [300, 250], "chunks": [128, 100], "dtype": "<f4",
            "order": "C", "fill_value": 2.5, "filters": None,
            "compressor": {"id": "blosc", "cname": "lz4", "clevel": 5, "shuffle": 1, "blocksize": 0}}
    (folder / ".zarray").write_text(json.dumps(meta))
    codec = Blosc(cname="lz4", clevel=5, shuffle=Blosc.SHUFFLE)
    for cy in range(3):
        for cx in range(3):
            if (cy, cx) == (1, 2):
                continue                                   # missing: reads as fill_value
            chunk = np.zeros((128, 100), "<f4")
            part = data[cy * 128:(cy + 1) * 128, cx * 100:(cx + 1) * 100]
            chunk[:part.shape[0], :part.shape[1]] = part
            (folder / f"{cy}.{cx}").write_bytes(codec.encode(chunk))
    want = data.copy()
    want[128:256, 200:250] = 2.5
    st = os.stat(folder / ".zarray")
    spec = {"dir": str(folder), "shape": [300, 250], "chunks": [128, 100], "dtype": "<f4",
            "order": "C", "compressor": "blosc", "cname": "lz4", "filters_none": True,
            "fill": 2.5, "separator": ".", "zarray": (st.st_size, st.st_mtime_ns, st.st_ino),
            "zattrs": None}
    reader = core.ZarrArray(spec)
    for y0, y1, x0, x1 in ((0, 300, 0, 250), (120, 140, 90, 210), (299, 300, 249, 250)):
        assert reader.read_rect(y0, y1, x0, x1) == np.ascontiguousarray(want[y0:y1, x0:x1]).tobytes()


def test_c_the_redraw_notice_coalesces_and_stops():
    r, w = socket.socketpair()
    r.setblocking(False)
    notifier = core.Notifier(w.fileno())
    try:
        for _ in range(50):
            notifier.notify()
        assert r.recv(4096) == b"\x01"            # fifty notices, one byte
        notifier.ack()
        notifier.notify()
        assert len(r.recv(4096)) == 1
        notifier.ack()
        notifier.close()
        notifier.notify()                      # after close: nothing is written
        with pytest.raises((BlockingIOError, OSError)):
            r.recv(4096)
    finally:
        r.close()
        w.close()
