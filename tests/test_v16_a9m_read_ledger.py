"""Block A9-M: pixel reads are counted once, at the place they happen."""

import numpy as np
import pytest

tifffile = pytest.importorskip("tifffile")
zarr = pytest.importorskip("zarr")

from block01.viewer import read_ledger  # noqa: E402
from block01.viewer.raw_tile_provider import RawTileProvider  # noqa: E402
from block01.viewer.source_tile_provider import open_viewer_source  # noqa: E402
from block01.viewer.step1_source import CoarsePlane, CorrectedRegion  # noqa: E402
from block01.viewer.tile_types import TileAddress, TileGridSpec  # noqa: E402
from test_v16_pixel_source import _data, _write  # noqa: E402


@pytest.fixture
def slide(tmp_path):
    path = tmp_path / "s.ome.tif"
    _write(path, _data("uint8"), 2, (64, 64))
    return str(path)


def test_raw_reads_count_once_whichever_way_in(slide):
    for p in (RawTileProvider(slide), open_viewer_source(slide)):
        read_ledger.reset()
        p.read_region(0, 0, 0, 10, 0, 10)
        grid = TileGridSpec(tile_size=16, source_chunk_shape=(), grid_version="v1")
        p.read_tile(0, TileAddress(level=1, tx=0, ty=0, grid=grid))
        snap = read_ledger.snapshot()
        assert snap.get("raw") == 2 and snap["total"] == 2, snap
        assert read_ledger.by_level() == {("raw", 0): 1, ("raw", 1): 1}
        before = read_ledger.snapshot()
        p.read_region(0, 0, 5, 5, 0, 10)                # empty: nothing read
        assert read_ledger.delta(before)["total"] in (0, 1)
        p.close()


def test_corrected_and_coarse_reads_are_counted_apart(tmp_path):
    z = zarr.open(str(tmp_path / "c.zarr"), mode="w", shape=(40, 40), dtype=np.float32)
    read_ledger.reset()
    CorrectedRegion(z, (0, 40, 0, 40)).read(0, 10, 0, 10)
    CoarsePlane(z, 4, (0, 0)).tile((0, 8, 0, 8))
    snap = read_ledger.snapshot()
    assert snap.get("corrected_saved") == 1 and snap.get("coarse_plane") == 1
    assert snap["total"] == 2
