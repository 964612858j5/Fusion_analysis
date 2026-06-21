"""v14.5a: source-aware schema primitives (schema-only, no runtime consumption).

Covers SourceRequest / CalibrationSourceIdentity / source_mixture_mode /
camp_source_policy validators, and the validate_step2_remap_config HARD guard
that rejects a source-aware step2_ready config until v14.5c/v14.5d runtime exists.
"""

import pytest

from block01.utils import source_identity as si
from block01.utils.channel_remap_config import (
    validate_step2_remap_config, default_channel_remap_config)


def _ready_config(channels=("CD68",)):
    """A NON-source-aware, fully step2-ready-shaped config (baseline)."""
    cfg = default_channel_remap_config(list(channels))
    cfg["source_policy"].update(
        preview_only=False, step2_ready=True,
        calibration_source_matches_step2=True,
        source_alignment_mode="single_native_source",
        intensity_space="raw_ome_native_float",
        step2_pre_remap_source="raw_ome_native_float")
    for ch in channels:
        cfg["channels"][ch].update(
            min=0.0, max=1.0, step2_compatible=True,
            calibration_source_matches_step2=True,
            intensity_space="raw_ome_native_float",
            step2_pre_remap_source="raw_ome_native_float")
    return cfg


# ── 1 + 2. SourceRequest ─────────────────────────────────────────────────────
def test_source_request_accepts_known_sources():
    assert si.validate_source_request(
        {"requested_source": "raw_ome", "channel_name": "CD68"}) == []
    assert si.validate_source_request(
        {"requested_source": "corrected_zarr", "channel_name": "CK19"}) == []


def test_source_request_rejects_unknown():
    errs = si.validate_source_request(
        {"requested_source": "magic", "channel_name": "CD68"})
    assert errs and any("requested_source" in e for e in errs)
    # empty channel name rejected too
    assert si.validate_source_request(
        {"requested_source": "raw_ome", "channel_name": ""}) != []


# ── 3. CalibrationSourceIdentity required fields ─────────────────────────────
def test_calibration_identity_requires_core_fields():
    full = si.make_calibration_source_identity(
        actual_source_kind="corrected_zarr", channel_name="CD68",
        source_shape=[80, 100], dtype="float32",
        intensity_space="background_corrected_marker_image",
        channel_index=0)
    assert si.validate_calibration_source_identity(full) == []
    for missing in ("actual_source_kind", "channel_name", "source_shape",
                    "dtype", "intensity_space"):
        bad = dict(full)
        bad.pop(missing)
        errs = si.validate_calibration_source_identity(bad)
        assert errs and any(missing in e for e in errs), missing
    # bad source kind / bad shape rejected
    assert si.validate_calibration_source_identity({**full, "actual_source_kind": "x"}) != []
    assert si.validate_calibration_source_identity({**full, "source_shape": [80]}) != []


# ── 4. source_mixture_mode ───────────────────────────────────────────────────
def test_source_mixture_mode_allowed_values():
    for ok in ("homogeneous_raw", "homogeneous_corrected", "mixed_raw_corrected"):
        assert si.validate_source_mixture_mode(ok) == []
    assert si.validate_source_mixture_mode("partial") != []
    assert si.SOURCE_MIXTURE_MODES == {
        "homogeneous_raw", "homogeneous_corrected", "mixed_raw_corrected"}


# ── 5 + 6. camp_source_policy ────────────────────────────────────────────────
def test_camp_source_policy_default_and_validation():
    assert si.DEFAULT_CAMP_SOURCE_POLICY == "raw_gi_only"
    assert si.camp_source_policy_of({}) == "raw_gi_only"
    assert si.camp_source_policy_of({"camp_source_policy": "selected_source"}) == "selected_source"
    assert si.validate_camp_source_policy("raw_gi_only") == []
    assert si.validate_camp_source_policy("selected_source") == []
    assert si.validate_camp_source_policy("everything") != []


# ── 7. Preview config may carry source-aware fields, stays not-ready ─────────
def test_preview_config_may_carry_source_aware_fields():
    cfg = default_channel_remap_config(["CD68"])
    cfg["source_mixture_mode"] = "mixed_raw_corrected"
    cfg["channels"]["CD68"]["source_request"] = si.make_source_request(
        "CD68", "corrected_zarr")
    assert si.config_is_source_aware(cfg) is True
    # preview_only / step2_ready=false by default -> not runtime-authoritative
    assert cfg["source_policy"]["preview_only"] is True
    assert cfg["source_policy"]["step2_ready"] is False


# ── 8. HARD reject: source-aware + step2_ready ───────────────────────────────
def test_source_aware_step2_ready_hard_rejected():
    cfg = _ready_config()
    cfg["channels"]["CD68"]["source_request"] = si.make_source_request(
        "CD68", "corrected_zarr")
    errs, resolved = validate_step2_remap_config(cfg, allow_preview_remap=False)
    assert any("source-aware" in e for e in errs)   # hard error, not a warning
    assert resolved == {}                            # not runnable


def test_source_aware_via_mixture_mode_also_rejected_when_ready():
    cfg = _ready_config()
    cfg["source_mixture_mode"] = "mixed_raw_corrected"
    errs, resolved = validate_step2_remap_config(cfg, allow_preview_remap=False)
    assert any("source-aware" in e for e in errs)
    assert resolved == {}


def test_non_default_camp_policy_makes_config_source_aware():
    cfg = _ready_config()
    cfg["camp_source_policy"] = "selected_source"
    errs, _ = validate_step2_remap_config(cfg, allow_preview_remap=False)
    assert any("source-aware" in e for e in errs)
    # default raw_gi_only is NOT source-aware
    cfg2 = _ready_config()
    cfg2["camp_source_policy"] = "raw_gi_only"
    assert si.config_is_source_aware(cfg2) is False


# ── 9. Existing non-source-aware step2_ready behavior unchanged ──────────────
def test_plain_step2_ready_still_passes():
    cfg = _ready_config()
    assert si.config_is_source_aware(cfg) is False
    errs, resolved = validate_step2_remap_config(cfg, allow_preview_remap=False)
    assert errs == []          # baseline still valid
    assert "CD68" in resolved
