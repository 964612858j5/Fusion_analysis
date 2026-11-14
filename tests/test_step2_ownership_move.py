"""Step2's two tile loops -- `_segment_one_zarr` and the full-image loop in
`run()` -- merge an engine method's tiles through `core.seam_merge`: each
loop's mask and total equal the pure functions' reference, over
boundary-crossing cells and empty tiles. (The legacy inline paste these loops
once also ran, for HQ / HQ2, was removed with those methods, Windows plan W2.)

The model is replaced by a fixed labelling, as in `test_label_ownership.py`.
The shadow compare is switched off: never called, and neither the log nor
the run's engine metadata claims it is on.
"""

import logging
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

def _labels(seed, h=97, w=113, n=80):
    rng = np.random.default_rng(seed)
    lab = np.zeros((h, w), np.uint16)
    for k in range(1, n + 1):
        y, x = rng.integers(0, h - 6), rng.integers(0, w - 6)
        dy, dx = rng.integers(2, 12, 2)
        lab[y:y + dy, x:x + dx] = k
    if seed == 3:
        lab[:, :60] = 0                                    # whole tiles empty
    return lab


def _fake_result(method, tile_data):
    """The model's stand-in: labels = distinct values of channel 0."""
    t = np.asarray(tile_data)[..., 0]
    u, inv = np.unique(t, return_inverse=True)
    m = inv.reshape(t.shape).astype(np.uint32)
    if u[0] != 0:
        m += 1
    return m


def _seam_reference(data, rows, cols, overlap):
    import sys
    from block01.core import seam_merge as sm
    from block01.utils.tile_scheduler import TileScheduler
    sys.path.insert(0, os.path.dirname(__file__))
    from test_seam_merge import reference_merge
    h, w = data.shape[:2]
    tiles, locals_ = [], []
    for k, tile in enumerate(TileScheduler(h, w, rows, cols, overlap).tiles):
        ry0, ry1, rx0, rx1 = tile.read_bbox
        tiles.append(sm.Tile(k, tuple(tile.read_bbox), tuple(tile.own_bbox)))
        locals_.append(_fake_result("cellpose_wholecell_fusion", data[ry0:ry1, rx0:rx1]))
    return reference_merge(tiles, locals_, (h, w))


def _worker(tmp_path, monkeypatch, method, lab, rows, cols, overlap):
    import zarr
    from block01.workers.segment_merge_worker import SegmentMergeWorker as W

    zp = str(tmp_path / "fused.zarr")
    z = zarr.open(zp, mode="w", shape=lab.shape + (2,), chunks=(32, 32, 2), dtype=np.uint16)
    z[..., 0] = lab
    z[..., 1] = lab
    monkeypatch.setattr(W, "_segment_tile",
                        lambda self, tile_data, *a, **k: _fake_result(self.method, tile_data))
    monkeypatch.setattr(W, "_init_segmentation_backend", lambda self, *a, **k: None)
    monkeypatch.setattr(W, "_uses_torch_backend", lambda self: False)

    def no_shadow(*a, **k):
        raise AssertionError("the shadow compare must not run")

    monkeypatch.setattr(W, "_merge_policy_shadow_compare", no_shadow)
    worker = W(zp, seg_config={"method": method},
               n_rows=rows, n_cols=cols, overlap_px=overlap,
               output_dir=str(tmp_path / "proj"))
    return worker, zp


CASES = [(0, 2, 3, 9), (1, 3, 2, 14), (2, 1, 1, 0), (3, 4, 4, 5)]
METHODS = ["cellpose_wholecell_fusion"]


@pytest.mark.parametrize("loop", ["segment_one_zarr", "run"])
@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("seed,rows,cols,overlap", CASES)
def test_both_loops_equal_the_seam_reference(tmp_path, monkeypatch, caplog, loop,
                                                      method, seed, rows, cols, overlap):
    import zarr

    lab = _labels(seed)
    worker, zp = _worker(tmp_path, monkeypatch, method, lab, rows, cols, overlap)
    caplog.set_level(logging.INFO)
    if loop == "segment_one_zarr":
        total = worker._segment_one_zarr(zp, "t", model=None, use_gpu=False,
                                         log=logging.getLogger("test"))
        sfx = "_t"
    else:
        got = {}
        worker.job_finished.connect(lambda d, n: got.setdefault("n", n))
        worker.run()
        total = got.get("n")
        sfx = ""

    def read(name):
        return np.asarray(zarr.open(os.path.join(worker.output_dir, f"{name}{sfx}.zarr"),
                                    mode="r"))

    # Block N3b: an engine method is merged by `core.seam_merge` -- equal to
    # the pure functions' reference, ids included
    ref = _seam_reference(np.stack([lab, lab], -1), rows, cols, overlap)
    got = read("global_mask")
    assert total == int(ref.max()) == int(got.max())
    np.testing.assert_array_equal(got, ref)


def test_the_shadow_compare_is_off_in_the_log_and_the_metadata(tmp_path, monkeypatch, caplog):
    lab = _labels(0)
    worker, _ = _worker(tmp_path, monkeypatch, "cellpose_wholecell_fusion", lab, 2, 2, 8)
    got = {}
    worker.job_finished.connect(lambda d, n: got.setdefault("n", n))
    worker.run()
    assert got.get("n", 0) > 0
    log_text = "".join(open(os.path.join(root, f), encoding="utf-8", errors="replace").read()
                       for root, _, files in os.walk(worker.output_dir)
                       for f in files if f.endswith(".log"))
    assert log_text and "shadow_compare=enabled" not in log_text
    assert worker._step2_engine_meta()["merge_policy"]["shadow_compare_enabled"] is False
    assert worker._merge_shadow_compare_count == 0
