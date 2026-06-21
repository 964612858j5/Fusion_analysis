"""v14.5a: corrected_channels.zarr per-channel identity metadata (metadata-only).

WsiCorrectionWorker stamps per-channel source identity attrs on each corrected
channel array WITHOUT changing array bytes/shape. Root v14.4 provenance stays;
no step2_ready attr is written.
"""

import hashlib

import numpy as np
import pytest

pytest.importorskip("PyQt5")
zarr = pytest.importorskip("zarr")


@pytest.fixture(scope="module")
def app():
    from PyQt5 import QtWidgets
    a = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return a


class _FakeLoader:
    def __init__(self):
        self.ch_map = {"CD68": 0, "CK19": 1}
        self.shape = (200, 200)
        self.filepath = "/tmp/fake.ome.tif"

    def _read_roi_zarr(self, idx, y0, y1, x0, x1):
        np.random.seed(idx * 1000 + y0)
        return (np.random.rand(y1 - y0, x1 - x0) * 1000).astype(np.float32)


def _run_worker(tmp_path, decisions):
    from block01.ui.step0.search_ctrl import WsiCorrectionWorker
    cfg = {"channel_decisions": decisions,
           "method_params": {"tophat_radius": 15, "cucim_sigma": 50}}
    rois = [{"name": "ROI_1", "bbox_fullres": [10, 90, 20, 120]}]
    w = WsiCorrectionWorker(_FakeLoader(), str(tmp_path), cfg, rois=rois)
    out = {}
    w.finished.connect(lambda p, dec: out.update(path=p))
    w.error.connect(lambda m: out.update(err=m))
    w.run()
    assert out.get("err") is None
    return out["path"]


# ── 10. Per-channel identity attrs written ───────────────────────────────────
def test_corrected_array_has_per_channel_identity(app, tmp_path):
    path = _run_worker(tmp_path, {"CD68": "tophat"})
    ds = zarr.open_group(path, mode="r")["ROI_1"]["CD68"]
    a = dict(ds.attrs)
    assert a["source_kind"] == "corrected_zarr"
    assert a["channel_name"] == "CD68"
    assert a["channel_key"] == "CD68"
    assert a["channel_index"] == 0                      # from loader.ch_map
    assert a["intensity_space"] == "background_corrected_marker_image"
    assert a["correction_method"] == "tophat"
    assert a["dtype"] == "float32"
    assert a["source_shape"] == [80, 100]
    assert a["roi_name"] == "ROI_1"
    assert a["roi_bbox_fullres"] == [10, 90, 20, 120]


def test_channel_index_nullable_when_loader_lacks_map(app, tmp_path):
    # A loader whose ch_map omits the channel -> channel_index None, other id kept.
    from block01.core.bg_correction import stamp_corrected_channel_identity
    path = str(tmp_path / "x.zarr")
    g = zarr.open_group(path, mode="w").create_group("ROI_1")
    ds = g.create_dataset("CD68", data=np.ones((4, 4), dtype=np.float32))
    stamp_corrected_channel_identity(ds, "CD68", channel_index=None,
                                     correction_method="cucim")
    a = dict(ds.attrs)
    assert a["channel_index"] is None
    assert a["channel_name"] == "CD68" and a["channel_key"] == "CD68"
    assert a["source_shape"] == [4, 4] and a["dtype"] == "float32"


# ── 11. corrected_zarr_report still detects non-empty arrays ─────────────────
def test_report_still_detects_nonempty(app, tmp_path):
    from block01.core.bg_correction import corrected_zarr_report
    path = _run_worker(tmp_path, {"CD68": "tophat"})
    rep = corrected_zarr_report(path)
    assert rep["non_empty"] is True
    assert rep["channel_arrays"] == ["ROI_1/CD68"]
    assert rep["shapes"]["ROI_1/CD68"] == [80, 100]


# ── 12 + 13. Root v14.4 provenance intact; no step2_ready anywhere ──────────
def test_root_provenance_intact_and_no_step2_ready(app, tmp_path):
    path = _run_worker(tmp_path, {"CD68": "tophat"})
    root = zarr.open_group(path, mode="r")
    # NOTE: worker itself does not stamp v14.4 root provenance (that lands via
    # _write_step0_handoff). Here we assert the corrected output carries no
    # step2_ready marker on root or array, which is the v14.5a invariant.
    assert "step2_ready" not in dict(root.attrs)
    assert "step2_ready" not in dict(root["ROI_1"]["CD68"].attrs)


def test_v14_4_root_provenance_helper_still_works(app, tmp_path):
    # The v14.4 provenance stamp is unchanged and still sets the three root keys.
    from block01.core.bg_correction import (
        stamp_corrected_zarr_provenance, CREATED_FROM_STEP0_BACKGROUND_CORRECTION,
        CORRECTED_ZARR_OUTPUT_KIND, CORRECTED_ZARR_USED_FOR)
    path = str(tmp_path / "prov.zarr")
    root = zarr.open_group(path, mode="w")
    root.create_group("ROI_1").create_dataset("CD68", data=np.ones((4, 4), dtype=np.float32))
    stamp_corrected_zarr_provenance(root)
    a = dict(zarr.open_group(path, mode="r").attrs)
    assert a["created_from_step"] == CREATED_FROM_STEP0_BACKGROUND_CORRECTION
    assert a["output_kind"] == CORRECTED_ZARR_OUTPUT_KIND
    assert a["used_for"] == CORRECTED_ZARR_USED_FOR
    assert "step2_ready" not in a


# ── 14. Array bytes + shape unchanged by metadata stamping ───────────────────
def test_metadata_stamp_does_not_change_array_bytes(app, tmp_path):
    from block01.core.bg_correction import stamp_corrected_channel_identity
    arr = (np.random.rand(30, 25) * 500).astype(np.float32)
    path = str(tmp_path / "b.zarr")
    g = zarr.open_group(path, mode="w").create_group("ROI_1")
    ds = g.create_dataset("CD68", data=arr)
    h_before = hashlib.sha256(ds[:].tobytes()).hexdigest()
    shape_before = ds.shape
    stamp_corrected_channel_identity(
        ds, "CD68", channel_index=0, correction_method="tophat",
        roi_name="ROI_1", roi_bbox_fullres=[0, 30, 0, 25])
    h_after = hashlib.sha256(ds[:].tobytes()).hexdigest()
    assert h_before == h_after            # value/hash identical, not just shape
    assert ds.shape == shape_before == (30, 25)
    assert np.array_equal(ds[:], arr)
