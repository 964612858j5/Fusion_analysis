"""Block CG: the CPU Gaussian (cuCIM path) with OpenCV -- the same estimator
as scipy to float32 rounding, recorded as its own implementation, and never
mixed with scipy inside one Save channel."""
import numpy as np
import pytest
from skimage.filters import gaussian as sk_gaussian

from block01.core import bg_correction as bg

pytestmark = pytest.mark.skipif(bg._cv2 is None, reason="OpenCV not installed")


def _scipy(a, s):
    a = np.asarray(a, np.float32)
    return np.clip(a - sk_gaussian(a, sigma=s, preserve_range=True,
                                   mode="reflect").astype(np.float32), 0, None)


@pytest.mark.parametrize("sigma", [1, 2, 5, 10, 25, 50, 100])
@pytest.mark.parametrize("shape", [(1, 1), (3, 7), (40, 41), (257, 300)])
def test_opencv_agrees_with_scipy_to_float_rounding(sigma, shape):
    rng = np.random.default_rng(sigma + sum(shape))
    a = (rng.random(shape) * 255).astype(np.uint8).astype(np.float32)
    got = bg._cucim_cpu(a, sigma, bg.GAUSSIAN_IMPL_OPENCV)
    assert got.dtype == np.float32 and got.shape == shape
    assert np.abs(got - _scipy(a, sigma)).max() <= 3e-4      # 0-255 data


def test_the_scipy_implementation_is_the_old_one_bitwise():
    rng = np.random.default_rng(3)
    a = (rng.random((200, 150)) * 255).astype(np.float32)
    assert np.array_equal(bg._cucim_cpu(a, 12, bg.GAUSSIAN_IMPL_SCIPY), _scipy(a, 12))


def test_display_paths_choose_per_call_and_save_paths_refuse_to_mix():
    a = np.full((64, 64), 10.0, np.float32)
    a[5, 5] = np.nan
    out = bg._cucim_cpu(a, 4)                                  # display: scipy here
    assert np.array_equal(out, _scipy(a, 4), equal_nan=True)
    with pytest.raises(bg.CpuImplUnavailable):
        bg._cucim_cpu(a, 4, bg.GAUSSIAN_IMPL_OPENCV)           # a frozen channel


def test_the_signature_names_the_implementation():
    path, foot, impl = bg.current_compute_signature("cucim")
    if path == "cpu":
        assert impl == bg.GAUSSIAN_IMPL_OPENCV and foot is None
    assert bg.current_compute_signature("tophat")[2] is None
    assert bg.normalize_save_signature(["cucim", 50, "2", "cpu", None]) == \
        ("cucim", 50, "2", "cpu", None, bg.GAUSSIAN_IMPL_SCIPY)
    assert bg.normalize_save_signature(["tophat", 5, "2", "cpu", "disk"])[5] is None


# ── a Save channel: one implementation from its first tile to its last ────

zarr = pytest.importorskip("zarr")
from test_step0_bg_parallel import _Loader, _save  # noqa: E402


class _NanLoader(_Loader):
    """The fake 3-tile slide with a NaN in its LAST tile only."""

    def __init__(self):
        super().__init__()
        for page in self._pages.values():
            page[8800, 10] = np.nan


def test_a_tile_opencv_cannot_take_recomputes_the_channel_with_scipy(tmp_path, monkeypatch):
    _w, got, arr = _save(tmp_path / "nan", loader=_NanLoader(), method="cucim")
    assert got["finished"]
    assert arr.attrs["bg_compute_impl"] == bg.GAUSSIAN_IMPL_SCIPY
    monkeypatch.setattr(bg, "_cv2", None)                      # an all-scipy channel
    _w, _g, ref = _save(tmp_path / "ref", loader=_NanLoader(), method="cucim")
    assert ref.attrs["bg_compute_impl"] == bg.GAUSSIAN_IMPL_SCIPY
    assert np.array_equal(np.asarray(arr[...]), np.asarray(ref[...]), equal_nan=True)


def test_a_clean_channel_is_all_opencv(tmp_path, monkeypatch):
    _w, got, arr = _save(tmp_path / "cv", method="cucim")
    assert arr.attrs["bg_compute_impl"] == bg.GAUSSIAN_IMPL_OPENCV
    monkeypatch.setattr(bg, "_cv2", None)
    _w, _g, ref = _save(tmp_path / "sp", method="cucim")
    diff = np.abs(np.asarray(arr[...]) - np.asarray(ref[...]))
    assert 0 < diff.max() <= 3e-4


def test_a_published_run_records_the_implementation_its_pixels_were_made_with(tmp_path):
    """codex CG: the run's params.json takes the channel's signature from the
    product, not from the prediction made before the Save."""
    import os
    import types
    from block01.ui.step0.step0_page import Step0Page
    from block01.utils import run_store as rs
    proj = tmp_path / "proj"
    ws = proj / "rois" / "ws1"
    ws.mkdir(parents=True)
    (proj / "project_manifest.json").write_text("{}")
    (ws / "roi_manifest.json").write_text("{}")
    run = rs.new_run(str(ws), "correct")
    grp = zarr.open_group(os.path.join(run, "corrected_channels.zarr"), mode="w") \
        .create_group("R")
    grp.attrs["bbox_fullres"] = [0, 8, 0, 8]
    ds = grp.create_dataset("CD3", shape=(8, 8), dtype="f4")
    ds.attrs.update({"correction_method": "cucim", "correction_param_value": 50,
                     "bg_correction_algo_version": bg.BG_CORRECTION_ALGO_VERSION,
                     "bg_compute_path": "cpu", "tophat_footprint": None,
                     "bg_compute_impl": bg.GAUSSIAN_IMPL_SCIPY})       # recomputed with scipy
    predicted = ("cucim", 50, bg.BG_CORRECTION_ALGO_VERSION, "cpu", None,
                 bg.GAUSSIAN_IMPL_OPENCV)
    fake = types.SimpleNamespace(_roi_context={}, _rm_pending_run=run,
                                 _rm_save_sigs={"CD3": predicted})
    fake._rm_sigs = Step0Page._rm_sigs
    spec = {"config": {"channel_decisions": {"CD3": "cucim"}},
            "analysis_region_type": "full_wsi", "rois": [], "patches": [],
            "raw_path": "/s.ome.tif"}
    Step0Page._rm_publish_correct_run(fake, run, spec)
    assert rs.read_params(run)["corrected"]["CD3"][5] == bg.GAUSSIAN_IMPL_SCIPY
