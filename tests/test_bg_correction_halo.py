"""Production tiled-correction halo contract (BG_CORRECTION_ALGO_VERSION 2).

The tiled production path must match a whole-image single call within float
tolerance for BOTH methods — i.e. tile borders leave no seams. The gaussian
halo was widened from 2*sigma to ceil(4*sigma) (full filter support); this
test is the regression pin. CPU paths only (deterministic); GPU seam parity
is covered separately (viewer golden seam tests / optional GPU test).
"""

import numpy as np
import pytest


@pytest.fixture()
def image():
    rng = np.random.default_rng(7)
    yy, xx = np.mgrid[0:600, 0:600]
    img = (0.2 * xx + 0.1 * yy).astype(np.float32)          # smooth background
    for _ in range(60):                                     # bright blobs
        y, x = rng.integers(20, 580, 2)
        img[y - 3:y + 3, x - 3:x + 3] += rng.uniform(50, 200)
    return img


def test_gaussian_tiled_matches_whole_image(image, monkeypatch):
    from block01.core import bg_correction as bg
    monkeypatch.setattr(bg, "GPU_MORPH_AVAILABLE", False)
    sigma = 40
    whole = bg._apply_cucim_or_cpu(image, sigma, prefer_gpu=False)
    tiled = bg._apply_background_method_tiled(
        image, "cucim", sigma=sigma, tile_size=256, prefer_gpu=False)
    assert np.allclose(tiled, whole, atol=1e-3), (
        f"gaussian seam error max={np.abs(tiled - whole).max()}")


def test_tophat_tiled_matches_whole_image(image, monkeypatch):
    from block01.core import bg_correction as bg
    monkeypatch.setattr(bg, "GPU_MORPH_AVAILABLE", False)
    radius = 12
    whole = bg._apply_tophat_cpu(image, radius)
    tiled = bg._apply_background_method_tiled(
        image, "tophat", radius=radius, tile_size=256, prefer_gpu=False)
    assert np.allclose(tiled, whole, atol=1e-3), (
        f"tophat seam error max={np.abs(tiled - whole).max()}")


def test_algo_version_bumped():
    from block01.core import bg_correction as bg
    assert bg.BG_CORRECTION_ALGO_VERSION == "2"


def test_method_overlap_single_source_of_truth():
    from block01.core import bg_correction as bg
    assert bg.method_overlap("tophat", 25) == 50
    assert bg.method_overlap("cucim", 50) == 200          # ceil(4*sigma)
    # the worker's tiling must use the same helper (no duplicated formula)
    import inspect
    from block01.ui.step0 import search_ctrl
    src = inspect.getsource(search_ctrl.WsiCorrectionWorker.run)
    assert "method_overlap(method, param)" in src
    assert "2 * param" not in src


# ── incremental-save identity includes the algorithm version ─────────────────

def _make_corrected_zarr(tmp_path, ch_attrs):
    zarr = pytest.importorskip("zarr")
    path = str(tmp_path / "corrected_channels.zarr")
    root = zarr.open_group(path, mode="w")
    grp = root.create_group("ROI_1")
    grp.attrs["bbox_fullres"] = [0, 64, 0, 64]
    ds = grp.create_dataset("CD3", shape=(64, 64), dtype="f4")
    ds.attrs.update(ch_attrs)
    return path


def test_legacy_zarr_without_version_is_reprocessed(tmp_path):
    """v1 zarr (no version stamp) must NEVER match a v2 signature."""
    from block01.ui.step0.search_ctrl import read_corrected_zarr_state
    from block01.core.bg_correction import BG_CORRECTION_ALGO_VERSION
    path = _make_corrected_zarr(tmp_path, {
        "correction_method": "cucim", "correction_param_value": 50})
    from block01.core.bg_correction import current_compute_signature
    sigs, bboxes = read_corrected_zarr_state(path)
    # legacy -> version "1"; no backend recorded (block S0P) -> never equal
    assert sigs["CD3"] == ("cucim", 50, "1", None, "<unrecorded>", None)
    current = ("cucim", 50, BG_CORRECTION_ALGO_VERSION) + current_compute_signature("cucim")
    assert sigs["CD3"] != current                          # -> reprocess


def test_current_version_zarr_is_skipped(tmp_path):
    """Same method+param+current version -> incremental save may skip."""
    from block01.ui.step0.search_ctrl import read_corrected_zarr_state
    from block01.core.bg_correction import BG_CORRECTION_ALGO_VERSION
    from block01.core.bg_correction import current_compute_signature
    here = current_compute_signature("cucim")          # block S0P: this machine's backend
    path = _make_corrected_zarr(tmp_path, {
        "correction_method": "cucim", "correction_param_value": 50,
        "bg_correction_algo_version": BG_CORRECTION_ALGO_VERSION,
        "bg_compute_path": here[0], "tophat_footprint": here[1],
        "bg_compute_impl": here[2]})                   # block CG: the implementation
    sigs, _ = read_corrected_zarr_state(path)
    assert sigs["CD3"] == ("cucim", 50, BG_CORRECTION_ALGO_VERSION) + here


def test_a_gaussian_made_before_the_implementation_field_reads_as_scipy(tmp_path):
    """Block CG: no `bg_compute_impl` -> the scipy (CPU) / cupyx (GPU)
    Gaussian it was made with, so it is still reused where that is what
    runs, and recomputed where OpenCV runs now."""
    from block01.ui.step0.search_ctrl import read_corrected_zarr_state
    from block01.core.bg_correction import BG_CORRECTION_ALGO_VERSION
    for where, impl in (("cpu", "scipy-gaussian"), ("gpu", "cupyx-gaussian")):
        path = _make_corrected_zarr(tmp_path / where, {
            "correction_method": "cucim", "correction_param_value": 50,
            "bg_correction_algo_version": BG_CORRECTION_ALGO_VERSION,
            "bg_compute_path": where, "tophat_footprint": None})
        sigs, _ = read_corrected_zarr_state(path)
        assert sigs["CD3"][5] == impl


def test_stamp_writes_algo_version(tmp_path):
    zarr = pytest.importorskip("zarr")
    from block01.core import bg_correction as bg
    root = zarr.open_group(str(tmp_path / "z.zarr"), mode="w")
    ds = root.create_dataset("CD3", shape=(8, 8), dtype="f4")
    bg.stamp_corrected_channel_identity(
        ds, channel_name="CD3", correction_method="tophat",
        correction_param_name="tophat_radius", correction_param_value=25)
    assert ds.attrs["bg_correction_algo_version"] == bg.BG_CORRECTION_ALGO_VERSION
