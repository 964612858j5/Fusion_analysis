"""Tests for scripts/qc_step2_remap_run.py — automated Step2 remap QC.

Uses tiny numpy/zarr arrays; no full WSI, no GUI. Skips cleanly if zarr is
unavailable for the end-to-end run.
"""

import json
import os

import numpy as np
import pytest

from block01.scripts.qc_step2_remap_run import (
    summarize_masks,
    check_masks,
    qc_provenance,
    qc_runtime,
    run_qc,
    _test_folder,
)


def _matching_masks(shape=(20, 20)):
    mask = np.zeros(shape, dtype=np.uint32)
    nuc = np.zeros(shape, dtype=np.uint32)
    mask[2:8, 2:8] = 1
    nuc[3:6, 3:6] = 1
    mask[12:18, 12:18] = 2
    nuc[13:16, 13:16] = 2
    return mask, nuc


def test_matching_masks_pass():
    mask, nuc = _matching_masks()
    info = summarize_masks(mask, nuc, scan_block_rows=8)
    assert info["shape_match"] and info["max_label_match"]
    assert info["mask_max_label"] == 2
    assert info["fraction_nucleus_pixels_inside_cell"] == 1.0
    assert info["median_cell_to_nucleus_area_ratio"] > 1.0
    failures, _ = check_masks(info)
    assert failures == []


def test_shape_mismatch_fails():
    mask, _ = _matching_masks((20, 20))
    nuc = np.zeros((20, 21), dtype=np.uint32)
    info = summarize_masks(mask, nuc, scan_block_rows=8)
    assert info["shape_match"] is False
    failures, _ = check_masks(info)
    assert any("shape mismatch" in f for f in failures)


def test_empty_mask_fails():
    mask = np.zeros((20, 20), dtype=np.uint32)
    nuc = np.zeros((20, 20), dtype=np.uint32)
    info = summarize_masks(mask, nuc, scan_block_rows=8)
    failures, _ = check_masks(info)
    assert any("max_label == 0" in f for f in failures)
    assert any("nonzero_fraction == 0" in f for f in failures)


def test_max_label_mismatch_fails():
    mask, nuc = _matching_masks()
    nuc[18:20, 0:2] = 3  # extra nucleus label with no matching cell
    info = summarize_masks(mask, nuc, scan_block_rows=8)
    assert info["max_label_match"] is False
    failures, _ = check_masks(info)
    assert any("max_label != nuclei max_label" in f for f in failures)


def test_nuclei_without_cell_fail():
    mask, nuc = _matching_masks()
    # add 2 orphan nuclei labels (no cells) -> >30% nuclei without cell
    nuc[0:1, 0:1] = 3
    nuc[0:1, 5:6] = 4
    info = summarize_masks(mask, nuc, scan_block_rows=8)
    failures, _ = check_masks(info)
    assert any("no matching cell label" in f for f in failures)


def test_provenance_missing_fails(tmp_path):
    run_dir = str(tmp_path)
    out = qc_provenance(run_dir)
    assert any("manual_remap_enabled is not true" in f for f in out["failures"])
    assert any("allow_preview_remap is not true" in f for f in out["failures"])


def test_provenance_cross_folder_and_preview_warns(tmp_path):
    run_dir = tmp_path / "test9" / "run"
    run_dir.mkdir(parents=True)
    prov = {
        "manual_remap_enabled": True,
        "allow_preview_remap": True,
        "gate_mode": "remap_and_gi",
        "used_for": "segmentation_only",
        "channel_remap_config_hash": "abc",
        "channel_remap_config_path": "/data/test3/remap_configs/x.json",
        "source_policy": {"intensity_space": "raw_ome_native_float",
                          "preview_only": True,
                          "step2_pre_remap_source": "raw_ome_native_float"},
    }
    (run_dir / "channel_remap_provenance.json").write_text(json.dumps(prov))
    out = qc_provenance(str(run_dir))
    assert out["failures"] == []
    assert any("different test folder" in w for w in out["warnings"])
    assert any("preview_only is true" in w for w in out["warnings"])


def test_provenance_bad_gate_and_used_for_fail(tmp_path):
    prov = {"manual_remap_enabled": True, "allow_preview_remap": True,
            "gate_mode": "gi", "used_for": "expression"}
    (tmp_path / "channel_remap_provenance.json").write_text(json.dumps(prov))
    out = qc_provenance(str(tmp_path))
    assert any("gate mode is 'gi'" in f for f in out["failures"])
    assert any("used_for is 'expression'" in f for f in out["failures"])


def test_runtime_status_failed_flags(tmp_path):
    (tmp_path / "runtime_partial.json").write_text(json.dumps(
        {"status": "failed", "method": "cellpose_nuclei_csd", "elapsed": "1s"}))
    out = qc_runtime(str(tmp_path))
    assert any("status is 'failed'" in f for f in out["failures"])


def test_test_folder_extraction():
    assert _test_folder("/a/b/test4/run") == "test4"
    assert _test_folder("/a/b/nope") is None


@pytest.mark.skipif("zarr" not in globals() and __import__("importlib").util.find_spec("zarr") is None,
                    reason="zarr unavailable")
def test_run_qc_end_to_end_pass(tmp_path):
    import zarr
    run_dir = tmp_path / "test4" / "run"
    run_dir.mkdir(parents=True)
    mask, nuc = _matching_masks((40, 40))
    zarr.save_array(str(run_dir / "global_mask_Full WSI.zarr"), mask)
    zarr.save_array(str(run_dir / "global_nuclei_mask_Full WSI.zarr"), nuc)
    prov = {"manual_remap_enabled": True, "allow_preview_remap": True,
            "gate_mode": "remap_and_gi", "used_for": "segmentation_only",
            "channel_remap_config_path": "/x/test4/c.json",
            "source_policy": {"preview_only": False, "intensity_space": "raw_ome_native_float"}}
    (run_dir / "channel_remap_provenance.json").write_text(json.dumps(prov))
    (run_dir / "runtime_partial.json").write_text(json.dumps(
        {"status": "finished", "method": "cellpose_nuclei_csd", "elapsed": "1s"}))
    report = run_qc(str(run_dir))
    assert report["overall_status"] in ("pass", "warning")
    assert report["sections"]["masks"]["failures"] == []
    assert report["metrics"]["masks.max_label_match"] is True


@pytest.mark.skipif(__import__("importlib").util.find_spec("zarr") is None,
                    reason="zarr unavailable")
def test_run_qc_end_to_end_empty_fails(tmp_path):
    import zarr
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    empty = np.zeros((30, 30), dtype=np.uint32)
    zarr.save_array(str(run_dir / "global_mask_Full WSI.zarr"), empty)
    zarr.save_array(str(run_dir / "global_nuclei_mask_Full WSI.zarr"), empty)
    report = run_qc(str(run_dir))
    assert report["overall_status"] == "fail"
    assert any("empty mask" in f or "nonzero_fraction == 0" in f
               for f in report["failures"])
