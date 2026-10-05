"""Block PA-4: Step2's OME-TIFF exports are written tile by tile, and the
file is byte for byte the one the whole-array write made (application
v1.2 §4)."""

import filecmp
import re

import numpy as np
import pytest
import tifffile

from block01.workers.segment_merge_worker import write_tiled_ome_tiff

_UUID = re.compile(rb'UUID="urn:uuid:[0-9a-f-]{36}"')


def _old(path, arr):
    """What the exports did before: the whole array in memory at once."""
    with tifffile.TiffWriter(path, bigtiff=True) as tif:
        tif.write(np.asarray(arr), tile=(512, 512), compression='lzw',
                  photometric='minisblack', metadata=None)


@pytest.mark.parametrize("shape", [(1500, 1300), (512, 512), (37, 2049), (1024, 1)])
def test_a_mask_written_tile_by_tile_is_the_same_file(tmp_path, shape):
    rng = np.random.default_rng(sum(shape))
    mask = rng.integers(0, 70000, shape, dtype=np.uint32)
    mask[rng.random(shape) < 0.6] = 0
    mm = np.memmap(tmp_path / "m.dat", dtype=np.uint32, mode="w+", shape=shape)
    mm[:] = mask
    mm.flush()
    ro = np.memmap(tmp_path / "m.dat", dtype=np.uint32, mode="r", shape=shape)
    _old(tmp_path / "old.tif", ro.astype(np.float32))
    write_tiled_ome_tiff(str(tmp_path / "new.tif"), ro, np.float32)
    assert filecmp.cmp(tmp_path / "old.tif", tmp_path / "new.tif", shallow=False)
    back = tifffile.imread(tmp_path / "new.tif")
    assert back.dtype == np.float32 and np.array_equal(back, mask.astype(np.float32))


@pytest.mark.parametrize("dtype", [np.uint8, np.uint16, np.float32])
def test_dapi_keeps_its_own_dtype_and_bytes(tmp_path, dtype):
    rng = np.random.default_rng(7)
    shape = (900, 1100)
    dapi = (rng.random(shape) * 200).astype(dtype)
    _old(tmp_path / "old.tif", dapi)
    write_tiled_ome_tiff(str(tmp_path / "new.tif"), dapi, None)
    assert filecmp.cmp(tmp_path / "old.tif", tmp_path / "new.tif", shallow=False)
    assert tifffile.imread(tmp_path / "new.tif").dtype == np.dtype(dtype)


def test_an_ome_named_file_differs_only_by_its_random_uuid(tmp_path):
    """The exports are named `*.ome.tiff`, for which tifffile writes OME-XML
    with a NEW uuid every time -- the old whole-array write was never byte
    stable there either. Everything else is the same (verified on the A5
    synthetic run: 13 bytes, all inside the uuid)."""
    rng = np.random.default_rng(3)
    mask = rng.integers(0, 900, (700, 1300), dtype=np.uint32)
    _old(tmp_path / "old.ome.tiff", mask.astype(np.float32))
    write_tiled_ome_tiff(str(tmp_path / "new.ome.tiff"), mask, np.float32)
    a = (tmp_path / "old.ome.tiff").read_bytes()
    b = (tmp_path / "new.ome.tiff").read_bytes()
    assert len(_UUID.findall(a)) == 1 == len(_UUID.findall(b))
    assert _UUID.sub(b"U", a) == _UUID.sub(b"U", b)
