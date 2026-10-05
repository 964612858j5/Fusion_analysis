"""Block PA-3 (G1): Generate cuts its work into chunk-aligned units, and the
fused store is the same pixels whatever the cut -- the polygon too, which is
now applied one unit at a time from the region's mask packed to one bit per
pixel (application v1.2 §3)."""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")
zarr = pytest.importorskip("zarr")

from block01.ui.step0.overview_panel import FullFusionWorker  # noqa: E402

H, W = 53, 71


class _Loader:
    shape = (H, W)
    filepath = "/x/raw.ome.tif"

    def __init__(self):
        self.ch_map = {"DAPI": 0, "CD3": 1, "CD8": 2}
        rng = np.random.default_rng(11)
        self._px = {c: (rng.random((H, W)) * 250).astype(np.float32)
                    for c in self.ch_map}
        self._px["CD8"][::7, :] = np.nan                  # non-finite pixels too

    def channel_names(self):
        return list(self.ch_map)

    def read_region(self, ch, y0, y1, x0, x1, downsample=1, normalize=True):
        return self._px[ch][y0:y1, x0:x1].copy()


POLY = [(3.6, 2.2), (60.9, 9.5), (66.1, 47.7), (20.4, 50.8), (-4.3, 30.1)]


def _run(tmp_path, name, chunk, wide, rois=None):
    cfg = {"ome_tiff": "/x/raw.ome.tif", "output_dir": str(tmp_path / name),
           "nucleus": {"channel": "DAPI", "weight": 0.8},
           "groups": {"a": {"group_weight": 1.0, "channels": {"CD3": 0.7, "CD8": 0.4}},
                      "b": {"group_weight": 0.6, "channels": {"CD8": 1.0}}},
           "channel_remap_params": {
               "DAPI": {"min": 5.0, "max": 200.0, "gamma": 0.7},
               "CD3": {"min": 10.0, "max": 180.0, "gamma": 1.3,
                       "brightness": 0.1, "contrast": 1.2},
               "CD8": {"min": 0.0, "max": 250.0, "gamma": 1.0}},
           "artifact_kind": "step1_fused_zarr", "config_hash": "h"}
    w = FullFusionWorker(_Loader(), cfg, 1, 1, rois=rois)
    w.zarr_chunk, w.UNIT_CHUNKS_WIDE = chunk, wide
    errors = []
    w.error.connect(errors.append)
    w.run()
    assert not errors, errors
    store = "fused.zarr" if rois is None else f"fused_{rois[0]['name']}.zarr"
    return np.array(zarr.open(str(tmp_path / name / store), mode="r")[:])


@pytest.mark.parametrize("chunk,wide", [(8, 1), (16, 3), (5, 2)])
def test_the_cut_changes_no_pixel(tmp_path, chunk, wide):
    whole = _run(tmp_path, "whole", 128, 1)               # one unit: the old 1 x 1
    assert np.array_equal(_run(tmp_path, "cut", chunk, wide), whole)


@pytest.mark.parametrize("chunk,wide", [(8, 1), (16, 3), (7, 2)])
def test_a_polygon_applied_per_unit_is_the_whole_region_mask(tmp_path, chunk, wide):
    rois = [{"name": "R", "bbox_fullres": [2, 50, 1, 69], "polygon_fullres": POLY}]
    whole = _run(tmp_path, "whole", 128, 1, rois=rois)
    cut = _run(tmp_path, "cut", chunk, wide, rois=rois)
    assert np.array_equal(cut, whole)
    # and the whole-region mask, as the old second pass applied it
    mask = FullFusionWorker._poly_mask(POLY, 2, 1, 48, 68)
    assert not cut[~mask].any() and cut[mask].any()


def test_a_packed_mask_unpacks_to_the_whole_mask():
    rng = np.random.default_rng(5)
    for _ in range(50):
        poly = [(float(x), float(y)) for x, y in rng.uniform(-30, 330, (6, 2))]
        ry0, rx0 = int(rng.integers(0, 20)), int(rng.integers(0, 20))
        whole = FullFusionWorker._poly_mask(poly, ry0, rx0, 260, 290)
        packed = FullFusionWorker._poly_mask_packed(poly, ry0, rx0, 260, 290)
        assert packed.nbytes <= 260 * 37
        for oy, ox, h, w in ((0, 0, 260, 290), (17, 40, 64, 100), (200, 250, 60, 40)):
            part = FullFusionWorker._unpack_part(packed, oy, ox, h, w)
            assert np.array_equal(part, whole[oy:oy + h, ox:ox + w])


def test_the_units_tile_the_region_on_its_chunk_grid():
    w = FullFusionWorker(_Loader(), {"groups": {}}, 3, 3)
    w.zarr_chunk, w.UNIT_CHUNKS_WIDE = 16, 2
    units = w._units(5, 58, 3, 74)
    cover = np.zeros((53, 71), int)
    for y0, y1, x0, x1 in units:
        assert (y0 - 5) % 16 == 0 and (x0 - 3) % 32 == 0      # region-relative grid
        cover[y0 - 5:y1 - 5, x0 - 3:x1 - 3] += 1
    assert (cover == 1).all()
