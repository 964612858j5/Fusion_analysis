"""v14.4: corrected_channels.zarr output validity, provenance, and explicit-write.

Case A (audited): WsiCorrectionWorker writes real per-channel float32 arrays
(root[<roi>][<channel>]) when channels are assigned tophat/cuCIM; an empty group
(zero channel arrays) is produced only when no channel is assigned. These tests
verify the validity REPORT distinguishes the two, drive the real worker to a
non-empty zarr, and check provenance — never treating "directory exists" as
success.

Qt tests need an offscreen platform (env: QT_QPA_PLATFORM=offscreen).
"""

import os

import numpy as np
import pytest

pytest.importorskip("PyQt5")

zarr = pytest.importorskip("zarr")


@pytest.fixture(scope="module")
def app():
    from PyQt5 import QtWidgets
    a = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return a


# ── validity report: empty group is NOT a valid corrected output ─────────────
def test_report_empty_group_is_not_valid(tmp_path):
    from block01.core.bg_correction import corrected_zarr_report
    path = str(tmp_path / "empty.zarr")
    root = zarr.open_group(path, mode="w")
    root.create_group("ROI_1")          # group only, ZERO channel arrays
    rep = corrected_zarr_report(path)
    assert rep["exists"] is True
    assert rep["non_empty"] is False
    assert rep["n_channel_arrays"] == 0


def test_report_missing_path_is_not_valid():
    from block01.core.bg_correction import corrected_zarr_report
    rep = corrected_zarr_report("/no/such/path.zarr")
    assert rep["exists"] is False and rep["non_empty"] is False


def test_report_real_arrays_is_valid(tmp_path):
    from block01.core.bg_correction import corrected_zarr_report
    path = str(tmp_path / "real.zarr")
    root = zarr.open_group(path, mode="w")
    g = root.create_group("ROI_1")
    g.create_dataset("CD68", data=np.ones((30, 40), dtype=np.float32))
    rep = corrected_zarr_report(path)
    assert rep["non_empty"] is True
    assert rep["n_channel_arrays"] == 1
    assert rep["channel_arrays"] == ["ROI_1/CD68"]
    assert rep["shapes"]["ROI_1/CD68"] == [30, 40]


# ── Case A: the real worker writes a valid non-empty zarr with real arrays ───
class _FakeLoader:
    def __init__(self):
        self.ch_map = {"CD68": 0, "CK19": 1}
        self.shape = (200, 200)
        self.filepath = "/tmp/fake.ome.tif"

    def _read_roi_zarr(self, idx, y0, y1, x0, x1):
        return (np.random.rand(y1 - y0, x1 - x0) * 1000).astype(np.float32)


def test_explicit_worker_writes_nonempty_corrected_zarr(app, tmp_path):
    from block01.ui.step0.search_ctrl import WsiCorrectionWorker
    from block01.core.bg_correction import corrected_zarr_report
    cfg = {"channel_decisions": {"CD68": "tophat", "CK19": "original"},
           "method_params": {"tophat_radius": 15, "cucim_sigma": 50}}
    rois = [{"name": "ROI_1", "bbox_fullres": [10, 90, 20, 120]}]
    w = WsiCorrectionWorker(_FakeLoader(), str(tmp_path), cfg, rois=rois)
    out = {}
    w.finished.connect(lambda p, dec: out.update(path=p, dec=dec))
    w.error.connect(lambda m: out.update(err=m))
    w.run()                              # run synchronously (no QThread)
    assert out.get("err") is None
    path = out["path"]
    assert path.endswith("corrected_channels.zarr")
    rep = corrected_zarr_report(path)
    # at least one channel array, non-zero shape — and 'original' channel excluded
    assert rep["non_empty"] is True
    assert rep["n_channel_arrays"] == 1
    assert rep["channel_arrays"] == ["ROI_1/CD68"]
    assert rep["shapes"]["ROI_1/CD68"] == [80, 100]
    # NOT a directory-only / empty group
    assert os.path.isdir(path) and rep["non_empty"]


def test_worker_no_channels_makes_no_zarr(app, tmp_path):
    # all 'original' -> worker emits empty path, writes no zarr group itself
    from block01.ui.step0.search_ctrl import WsiCorrectionWorker
    cfg = {"channel_decisions": {"CD68": "original"},
           "method_params": {"tophat_radius": 15, "cucim_sigma": 50}}
    rois = [{"name": "ROI_1", "bbox_fullres": [0, 50, 0, 50]}]
    w = WsiCorrectionWorker(_FakeLoader(), str(tmp_path), cfg, rois=rois)
    out = {}
    w.finished.connect(lambda p, dec: out.update(path=p, dec=dec))
    w.run()
    assert out.get("path") == ""         # no corrected output claimed


# ── provenance + no step2_ready ──────────────────────────────────────────────
def test_provenance_stamp_no_step2_ready(tmp_path):
    from block01.core.bg_correction import (
        stamp_corrected_zarr_provenance, CREATED_FROM_STEP0_BACKGROUND_CORRECTION,
        CORRECTED_ZARR_OUTPUT_KIND, CORRECTED_ZARR_USED_FOR)
    path = str(tmp_path / "p.zarr")
    root = zarr.open_group(path, mode="w")
    root.create_group("ROI_1").create_dataset(
        "CD68", data=np.ones((4, 4), dtype=np.float32))
    stamp_corrected_zarr_provenance(root)
    attrs = dict(zarr.open_group(path, mode="r").attrs)
    assert attrs["created_from_step"] == CREATED_FROM_STEP0_BACKGROUND_CORRECTION
    assert attrs["created_from_step"] == "step0_background_correction"
    assert attrs["output_kind"] == CORRECTED_ZARR_OUTPUT_KIND == "corrected_channels_zarr"
    assert attrs["used_for"] == CORRECTED_ZARR_USED_FOR
    assert "step2_ready" not in attrs   # never marked Step2-ready


# ── HQ2/CSD raw_ome_only source policy unchanged (not touched by v14.4) ──────
def test_hq2_csd_source_policy_unchanged():
    from block01.workers.hq_source_resolver import (
        SOURCE_MODE_RAW_ONLY, SOURCE_MODE_CORRECTED_THEN_RAW)
    assert SOURCE_MODE_RAW_ONLY == "raw_ome_only"
    assert SOURCE_MODE_CORRECTED_THEN_RAW == "corrected_then_raw_fallback"


# ── dependency wall: BG layer adds no resolver/promotion/runtime imports ─────
def test_no_forbidden_imports_in_bg_layer():
    # No resolver/promotion/runtime imports (check import lines; docstring prose
    # may legitimately mention step2_ready to say it is NEVER written).
    import inspect
    from block01.core import bg_correction as mod
    src = inspect.getsource(mod)
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            for forbidden in ("hq_source_resolver", "remap_promotion",
                              "promote_remap_config", "segment_merge_worker"):
                assert forbidden not in stripped, stripped
    # step2_ready is never written as a zarr attr / assignment in this layer
    assert 'attrs["step2_ready"]' not in src
    assert "step2_ready =" not in src
