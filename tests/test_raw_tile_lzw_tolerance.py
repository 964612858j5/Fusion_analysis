"""The raw-slide direct reader decodes LZW tiles into their own size, as
tifffile and the Rust reader do: a stored tile with stray bytes after its
data decodes whole (45 such tiles in the PhenoCycler QPTIFF of Slice1 made
Generate fused.zarr fail, 2026-10-19)."""

import os

import imagecodecs
import numpy as np
import pytest

from block01.viewer import raw_tile_provider as rtp

SLICE1 = "/sdb1/Fusion/Fusion_Final_20260824_Slice1/Scan1/Fusion_Final_20260824_Slice1_Scan1.qptiff"


def test_lzw_is_decoded_into_the_tiles_own_size(monkeypatch):
    seen = {}

    def fake(raw, out=None):
        seen["out"] = out
        return bytes(out)
    monkeypatch.setattr(imagecodecs, "lzw_decode", fake)
    plan = rtp._ChunkPlan(tile=(4, 8), dtype=np.dtype("<u2"), decode=fake)
    assert len(rtp._decode(plan, b"x")) == 64 and seen["out"] == 4 * 8 * 2
    other = rtp._ChunkPlan(tile=(4, 8), dtype=np.dtype("u1"), decode=lambda raw: raw)
    assert rtp._decode(other, b"abc") == b"abc"                      # other codecs as before


def test_a_real_lzw_round_trip_is_unchanged():
    tile = (np.arange(64 * 64) % 251).astype(np.uint8).reshape(64, 64)
    plan = rtp._ChunkPlan(tile=(64, 64), dtype=np.dtype("u1"), decode=imagecodecs.lzw_decode)
    out = np.frombuffer(rtp._decode(plan, imagecodecs.lzw_encode(tile.tobytes())), np.uint8)
    np.testing.assert_array_equal(out.reshape(64, 64), tile)


@pytest.mark.skipif(not os.path.exists(SLICE1), reason="the Slice1 slide is not on this machine")
def test_the_slice1_tiles_with_stray_bytes_read_whole_and_equal_tifffile():
    import tifffile
    with tifffile.TiffFile(SLICE1) as tf:
        page = tf.series[0].pages[43]
        page = page.aspage() if hasattr(page, "aspage") else page
        off, cnt = page.dataoffsets[1474], page.databytecounts[1474]
        th, tw = page.tilelength, page.tilewidth
        fh = tf.filehandle
        fh.seek(off)
        raw = fh.read(cnt)
        want, _idx, _shape = page.decode(raw, 1474)              # tifffile's own decode of that tile
    with pytest.raises(imagecodecs.LzwError):
        imagecodecs.lzw_decode(raw)                              # the strict decoder refuses
    plan = rtp._ChunkPlan(tile=(th, tw), dtype=np.dtype("u1"), decode=imagecodecs.lzw_decode)
    got = np.frombuffer(rtp._decode(plan, raw), np.uint8, count=th * tw).reshape(th, tw)
    np.testing.assert_array_equal(got, np.asarray(want).reshape(th, tw))
