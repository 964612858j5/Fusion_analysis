"""v14.5d B3b (construction stage): the worker validates + stashes a source-aware
runtime remap DESCRIPTOR, and HARD-REJECTS it when the feature flag is off.

Tested via the unbound method on a stub — no full SegmentMergeWorker construction
(which needs a run dir / rois). The construction stage must NOT resolve sources; it
only flag-gates, validates the descriptor shape, and stashes it. Source re-resolve +
cross-check are a later stage (B3b-2)."""

import types

import pytest

from block01.workers.segment_merge_worker import SegmentMergeWorker
from block01.utils.segmentation_config import (
    STEP2_SOURCE_AWARE_RUNTIME_ENV, CELLPOSE_NUCLEI_HQ2, MESMER_WHOLE_CELL)


def _runtime_cfg(runtime_supported=True, channels=("CK19", "CD68")):
    ch = {"min": 0.0, "max": 1.0, "gamma": 1.0, "step2_compatible": True,
          "calibration_source_matches_step2": True,
          "resolved_source_kind": "corrected_zarr",
          "resolved_source_path": "/data/corrected.zarr",
          "resolved_group_name": "ROI_1",
          "resolved_source_shape": [200, 200]}
    return {
        "channels": {c: dict(ch) for c in channels},
        "source_policy": {"step2_ready": True,
                          "source_alignment_mode": "per_channel_native",
                          "preview_only": False,
                          "calibration_source_matches_step2": True},
        "source_mixture_mode": "homogeneous_corrected",
        "created_by_source_aware_promotion": True,
        "source_aware_promotion_ready": True,
        "runtime_supported": runtime_supported,
        "used_for": "segmentation_only",
    }


def _stub(method=CELLPOSE_NUCLEI_HQ2, hq_input_mode="selected_channels_from_source",
          hq_channels=("CK19", "CD68")):
    return types.SimpleNamespace(
        _pending_source_aware_runtime=None, _manual_remap_enabled=False,
        seg_config={"method": method, "hq_input_mode": hq_input_mode,
                    "hq_channels": list(hq_channels)})


def _accept(stub, cfg):
    return SegmentMergeWorker._accept_source_aware_runtime_descriptor(stub, cfg)


def test_descriptor_hard_rejected_when_flag_off(monkeypatch):
    monkeypatch.delenv(STEP2_SOURCE_AWARE_RUNTIME_ENV, raising=False)
    s = _stub()
    with pytest.raises(ValueError, match="is off"):
        _accept(s, _runtime_cfg())
    assert s._pending_source_aware_runtime is None      # not stashed -> not run


def test_descriptor_accepted_and_stashed_when_flag_on(monkeypatch):
    monkeypatch.setenv(STEP2_SOURCE_AWARE_RUNTIME_ENV, "1")
    s = _stub()
    cfg = _runtime_cfg()
    _accept(s, cfg)
    assert s._pending_source_aware_runtime is cfg
    assert s._manual_remap_enabled is True


def test_invalid_descriptor_rejected_even_with_flag_on(monkeypatch):
    monkeypatch.setenv(STEP2_SOURCE_AWARE_RUNTIME_ENV, "1")
    s = _stub()
    with pytest.raises(ValueError, match="invalid"):
        _accept(s, _runtime_cfg(runtime_supported=False))
    assert s._pending_source_aware_runtime is None


# ── method + input-mode gate ─────────────────────────────────────────────────

def test_rejected_for_non_hq2csd_method(monkeypatch):
    monkeypatch.setenv(STEP2_SOURCE_AWARE_RUNTIME_ENV, "1")
    s = _stub(method=MESMER_WHOLE_CELL)
    with pytest.raises(ValueError, match="HQ2/CSD only"):
        _accept(s, _runtime_cfg())
    assert s._pending_source_aware_runtime is None


def test_rejected_for_weighted_fusion_mode(monkeypatch):
    monkeypatch.setenv(STEP2_SOURCE_AWARE_RUNTIME_ENV, "1")
    s = _stub(hq_input_mode="step1_weighted_fusion")
    with pytest.raises(ValueError, match="step1_weighted_fusion"):
        _accept(s, _runtime_cfg())
    assert s._pending_source_aware_runtime is None


def test_rejected_for_non_selected_channels_mode(monkeypatch):
    monkeypatch.setenv(STEP2_SOURCE_AWARE_RUNTIME_ENV, "1")
    s = _stub(hq_input_mode="hybrid")
    with pytest.raises(ValueError, match="selected_channels_from_source"):
        _accept(s, _runtime_cfg())
    assert s._pending_source_aware_runtime is None


# ── selected-marker coverage ─────────────────────────────────────────────────

def test_rejected_when_selected_marker_not_covered(monkeypatch):
    monkeypatch.setenv(STEP2_SOURCE_AWARE_RUNTIME_ENV, "1")
    # config covers CK19/CD68 but the run selected CD45 too
    s = _stub(hq_channels=("CK19", "CD68", "CD45"))
    with pytest.raises(ValueError, match="does not cover"):
        _accept(s, _runtime_cfg(channels=("CK19", "CD68")))
    assert s._pending_source_aware_runtime is None
