"""The shared ownership / renumbering (plan block C, step 1: a pure move).

The gate (plan 7.11.3): on the same label image and the same own region,
`core.label_ownership` keeps the same labels and gives the same new ids as
Step2's authoritative inline code. Proved against Step2 ITSELF: its real
`_segment_one_zarr` merge runs on a synthetic fused zarr with the model
replaced by a fixed labelling, and its global mask must equal the one built
from this module tile by tile.
"""

import logging
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from block01.core import label_ownership as lo  # noqa: E402


def _random_labels(seed, h=90, w=110, n=70):
    rng = np.random.default_rng(seed)
    lab = np.zeros((h, w), np.uint16)
    for k in range(1, n + 1):
        y, x = rng.integers(0, h - 6), rng.integers(0, w - 6)
        dy, dx = rng.integers(2, 8, 2)
        lab[y:y + dy, x:x + dx] = k
    return lab


def _fixed_labelling(self, tile_data, backend, *a, **kw):
    """Stands in for the model: labels = the distinct values of channel 0,
    numbered 1.. in value order (0 stays background). Deterministic per
    window, like a model, and different windows number the same cell
    differently -- which is what the merge must cope with."""
    t = np.asarray(tile_data)[..., 0]
    u, inv = np.unique(t, return_inverse=True)
    out = inv.reshape(t.shape).astype(np.uint32)
    if u[0] != 0:
        out += 1
    return out


@pytest.mark.parametrize("seed,rows,cols,overlap", [(0, 2, 3, 7), (1, 3, 2, 12), (2, 1, 1, 0),
                                                    (3, 4, 4, 5)])
def test_step2s_real_merge_equals_this_module_tile_by_tile(tmp_path, monkeypatch, seed, rows,
                                                           cols, overlap):
    import zarr
    from block01.utils.tile_scheduler import TileScheduler
    from block01.workers.segment_merge_worker import SegmentMergeWorker

    lab = _random_labels(seed)
    h, w = lab.shape
    zp = str(tmp_path / "fused.zarr")
    z = zarr.open(zp, mode="w", shape=(h, w, 2), chunks=(32, 32, 2), dtype=np.uint16)
    z[..., 0] = lab
    z[..., 1] = lab
    monkeypatch.setattr(SegmentMergeWorker, "_segment_tile", _fixed_labelling)
    worker = SegmentMergeWorker(zp, seg_config={"method": "cellpose_wholecell_fusion"},
                                n_rows=rows, n_cols=cols, overlap_px=overlap,
                                output_dir=str(tmp_path))
    total = worker._segment_one_zarr(zp, "t", model=None, use_gpu=False,
                                     log=logging.getLogger("test"))
    step2 = np.asarray(zarr.open(os.path.join(worker.output_dir, "global_mask_t.zarr"),
                                 mode="r"))

    ours = np.zeros((h, w), np.uint32)
    offset = 0
    data = np.stack([lab, lab], -1)
    for tile in TileScheduler(h, w, rows, cols, overlap).tiles:
        ry0, ry1, rx0, rx1 = tile.read_bbox
        oy0, oy1, ox0, ox1 = tile.own_bbox
        local = _fixed_labelling(None, data[ry0:ry1, rx0:rx1], None)
        out, _, n = lo.apply_ownership(local, (oy0 - ry0, oy1 - ry0, ox0 - rx0, ox1 - rx0),
                                       offset)
        dst = ours[ry0:ry1, rx0:rx1]
        np.copyto(dst, out, where=(out > 0))          # Step2's paste, unchanged
        offset += n
    assert offset == total
    np.testing.assert_array_equal(step2, ours)


def test_centroids_are_step2s_centroids():
    from block01.workers.segment_merge_worker import SegmentMergeWorker
    for seed in range(5):
        lab = _random_labels(seed).astype(np.uint32)
        lab[lab == 7] = 0                                    # a missing label
        a, b = lo.centroids(lab), SegmentMergeWorker._centroids_vectorised(lab)
        np.testing.assert_array_equal(a[0], b[0])
        np.testing.assert_array_equal(a[1], b[1])


def test_the_own_region_is_half_open_and_ids_follow_the_original_order():
    m = np.zeros((10, 10), np.uint32)
    m[0:2, 0:2] = 5          # centroid (0.5, 0.5): inside
    m[4:6, 4:6] = 2          # centroid (4.5, 4.5): outside [0, 4)
    m[3:4, 3:4] = 9          # centroid (3, 3): inside, on the low edge of nothing
    m[4, 0] = 3              # centroid (4, 0): y == oy1 -> outside
    out, _, n = lo.apply_ownership(m, (0, 4, 0, 4), offset=10)
    assert n == 2
    assert set(np.unique(out)) == {0, 11, 12}
    assert (out[0:2, 0:2] == 11).all() and (out[3, 3] == 12)   # 5 -> 11, 9 -> 12
    assert (out[4:6, 4:6] == 0).all() and out[4, 0] == 0


def test_a_shared_mask_goes_through_the_same_lut():
    cell = np.zeros((8, 8), np.uint32)
    cell[0:4, 0:4] = 1
    cell[4:8, 4:8] = 2                                        # owned elsewhere
    nuc = np.zeros_like(cell)
    nuc[1:3, 1:3] = 1
    nuc[5:7, 5:7] = 2
    nuc[0, 7] = 99                                            # above the cells' max
    out, nout, n = lo.apply_ownership(cell, (0, 4, 0, 4), shared=nuc)
    assert n == 1
    assert (nout[1:3, 1:3] == 1).all() and nout[5:7, 5:7].max() == 0 and nout[0, 7] == 0


def test_nothing_owned_gives_zeros_and_count_is_not_the_largest_id():
    m = np.zeros((6, 6), np.uint32)
    m[5, 5] = 40
    out, nout, n = lo.apply_ownership(m, (0, 3, 0, 3), shared=np.zeros_like(m))
    assert n == 0 and out.max() == 0 and nout.max() == 0
    empty, _, n0 = lo.apply_ownership(np.zeros((4, 4), np.uint32), (0, 4, 0, 4))
    assert n0 == 0 and empty.shape == (4, 4)
    m[0, 0] = 40
    _, _, n1 = lo.apply_ownership(m, (0, 3, 0, 3))
    assert n1 == 1
