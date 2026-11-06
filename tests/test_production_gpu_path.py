"""The production paths (patch batch, Save) really run the runtime kernels.

Block RT follow-up (docs/v17_step0_runtime_kernels.md §6.7). The unit tests
around this one check *dispatch*; this one checks the two production
consumers of `core.bg_correction.correct_tile` end to end on synthetic data:

* `BatchProcessWorker` — the patch batch Enter starts (the payload's
  `tophat_raw` must equal the cupyx reference for the patch it read);
* `WsiCorrectionWorker` — the whole-slide Save, whose corrected zarr must
  contain exactly the reference pixels for the ROI, and must record
  `bg_compute_path == "gpu"` / `tophat_footprint == "square"`.

Both assert that the runtime kernels were the ones used: the backend is
`runtime-kernels` and `legacy_calls` does not move across the run. Without a
GPU the module skips (the CPU path is a different contract, covered by the
halo/CPU suites).
"""

import numpy as np
import pytest

bg = pytest.importorskip("block01.core.bg_correction")
gk = pytest.importorskip("block01.core.gpu_kernels")
pytest.importorskip("block01.qt")
zarr = pytest.importorskip("zarr")


def _require_gpu():
    if not bg.GPU_MORPH_AVAILABLE:
        pytest.skip("no GPU in this environment")
    assert bg.gpu_filter_backend() == "runtime-kernels", bg.RUNTIME_KERNEL_ERROR


def _settle(worker):
    from block01.core import product_swap
    product_swap.apply_plan(worker.output_dir, worker.swap_plan)
    product_swap.commit(worker.output_dir)


@pytest.fixture(scope="module")
def app():
    from block01.qt import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _PatchLoader:
    """Deterministic patch pixels; `read_region` is all the batch needs."""

    ch_map = {"CD68": 0, "DAPI": 1}
    shape = (200, 200)
    filepath = "/tmp/s0-gpu-patch.ome.tif"

    def read_region(self, channel, y0, y1, x0, x1, downsample=1,
                    correction_config=None, normalize=False):
        rng = np.random.default_rng(abs(hash((channel, y0, x0))) % (2 ** 32))
        yy, xx = np.mgrid[y0:y1, x0:x1]
        return (0.4 * xx + 0.2 * yy + rng.random((y1 - y0, x1 - x0)) * 40
                ).astype(np.float32)


class _SaveLoader(_PatchLoader):
    """`_read_roi_zarr` is what `WsiCorrectionWorker` reads its tiles with."""

    def _read_roi_zarr(self, idx, y0, y1, x0, x1):
        yy, xx = np.mgrid[y0:y1, x0:x1]
        return (0.4 * xx + 0.2 * yy).astype(np.float32) + np.float32(idx * 10)


# ── patch batch (the Enter path with patch bookmarks) ────────────────────

def test_patch_batch_uses_the_runtime_kernels_and_matches_the_reference(app):
    _require_gpu()
    from block01.ui.step0.search_ctrl import BatchProcessWorker
    patch = (0, 64, 0, 80)          # (y0, y1, x0, x1), the repo's patch order
    loader = _PatchLoader()
    raw = loader.read_region("CD68", *patch)
    before = gk.stats()["legacy_calls"]
    payloads = []
    worker = BatchProcessWorker(
        loader, [patch], {"CD68": "tophat"}, None, 15, 50,
        channel_params={"CD68": {"tophat_radius": 15, "cucim_sigma": 50}},
        max_gpu_workers=1)
    worker.channel_patch_done.connect(lambda ch, i, p: payloads.append((ch, i, p)))
    errors = []
    worker.error_signal.connect(lambda ch, i, m: errors.append((ch, i, m)))
    worker.run()
    assert not errors, errors
    assert len(payloads) == 1
    ch, idx, payload = payloads[0]
    assert ch == "CD68" and idx == 0
    assert gk.stats()["legacy_calls"] == before, "patch batch fell back to cupyx"
    expected = gk.tophat(raw, 15)
    got = payload["tophat_raw"]
    assert got is not None
    assert float(np.max(np.abs(got - expected))) == 0.0


# ── whole-slide Save (WsiCorrectionWorker) ───────────────────────────────

def test_save_product_uses_the_runtime_kernels_and_matches_the_reference(app, tmp_path):
    _require_gpu()
    from block01.ui.step0.search_ctrl import WsiCorrectionWorker
    loader = _SaveLoader()
    cfg = {"channel_decisions": {"CD68": "tophat"},
           "method_params": {"tophat_radius": 15, "cucim_sigma": 50}}
    roi = {"name": "ROI_1", "bbox_fullres": [10, 90, 20, 120]}
    before = gk.stats()["legacy_calls"]
    w = WsiCorrectionWorker(loader, str(tmp_path), cfg, rois=[roi])
    out = {}
    w.job_finished.connect(lambda p, dec: out.update(path=p))
    w.error.connect(lambda m: out.update(err=m))
    w.run()
    _settle(w)
    assert out.get("err") is None, out.get("err")
    assert gk.stats()["legacy_calls"] == before, "Save fell back to cupyx"

    group = zarr.open_group(out["path"], mode="r")
    ds = group["ROI_1"]["CD68"]
    attrs = dict(ds.attrs)
    assert attrs["bg_compute_path"] == "gpu"
    assert attrs["tophat_footprint"] == "square"

    # The reference. The worker tiles WITHIN the ROI (its halo is clamped to
    # the ROI's own bounds, `_tile_slices(roi_h, roi_w, ...)`), and this ROI
    # is smaller than one tile, so the product is the ROI corrected as one
    # block through the production tile entry.
    y0, y1, x0, x1 = roi["bbox_fullres"]
    ref = bg.correct_tile(loader._read_roi_zarr(0, y0, y1, x0, x1),
                          "tophat", 15, "gpu")
    saved = np.asarray(ds[...], dtype=np.float32)
    assert saved.shape == ref.shape
    d = float(np.max(np.abs(saved - ref)))
    assert d == 0.0, f"Save product differs from the reference by {d}"
