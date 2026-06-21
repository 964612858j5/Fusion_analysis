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


# ── v14.5a.1 hardening: central registry + recursive scan + registry tests ───

def _place_marker(cfg, marker, value, location):
    """Insert a source-aware marker at the given config location."""
    if location == "top_level":
        cfg[marker] = value
    elif location == "source_policy":
        cfg["source_policy"][marker] = value
    elif location == "channel_immediate":
        cfg["channels"]["CD68"][marker] = value
    elif location == "channel_nested":
        cfg["channels"]["CD68"]["metadata"] = {marker: value}
    elif location == "channel_deep_nested":
        cfg["channels"]["CD68"]["a"] = {"b": {marker: value}}
    else:
        raise AssertionError(location)
    return cfg


_LOCATIONS = ["top_level", "source_policy", "channel_immediate",
              "channel_nested", "channel_deep_nested"]


# 1. Registry contains exactly the current source-aware markers.
def test_registry_contains_all_current_markers():
    assert si.SOURCE_AWARE_FIELD_REGISTRY == {
        "source_request", "calibration_source_identity",
        "actual_source_kind", "actual_source_path",
        "source_mixture_mode", "camp_source_policy",
    }


# 2-5. Detection at every location (parameterized over registry x location).
@pytest.mark.parametrize("marker", sorted(si.SOURCE_AWARE_FIELD_REGISTRY))
@pytest.mark.parametrize("location", _LOCATIONS)
def test_config_is_source_aware_detects_marker_at_location(marker, location):
    cfg = _ready_config()
    _place_marker(cfg, marker, si.source_aware_trigger_value(marker), location)
    assert si.config_is_source_aware(cfg) is True


# 6-7. Every registered marker at every location HARD-rejects step2_ready=true.
@pytest.mark.parametrize("marker", sorted(si.SOURCE_AWARE_FIELD_REGISTRY))
@pytest.mark.parametrize("location", _LOCATIONS)
def test_every_marker_hard_rejects_step2_ready(marker, location):
    cfg = _ready_config()                         # step2_ready=true baseline
    _place_marker(cfg, marker, si.source_aware_trigger_value(marker), location)
    errs, resolved = validate_step2_remap_config(cfg, allow_preview_remap=False)
    assert any("source-aware" in e for e in errs), (marker, location)
    assert resolved == {}                         # not runnable


# 9. Old promoted / non-source-aware config fields are NOT source-aware.
def test_old_promoted_fields_not_source_aware():
    cfg = _ready_config()
    # mimic promotion output: source_kind/source_path (NOT actual_source_*),
    # a calibration_source audit block, and native per-channel provenance.
    cfg["source_policy"].update(source_kind="raw_ome", source_path="/x",
                                source_shape=[16, 16])
    cfg["calibration_source"] = {
        "source_kind": "raw_ome", "source_path": "/x", "source_shape": [16, 16],
        "intensity_space": "raw_ome_native_float"}
    assert si.config_is_source_aware(cfg) is False
    errs, resolved = validate_step2_remap_config(cfg, allow_preview_remap=False)
    assert not any("source-aware" in e for e in errs)
    assert "CD68" in resolved


# 10. Registration-enforcement meta-test (Approach B): every SOURCE_AWARE_KEY_*
#     constant must appear in the central registry, so a developer cannot define
#     a new source-aware marker constant without registering it.
def test_every_source_aware_key_constant_is_registered():
    key_consts = {n: getattr(si, n) for n in dir(si)
                  if n.startswith("SOURCE_AWARE_KEY_")}
    assert key_consts, "expected SOURCE_AWARE_KEY_* marker constants"
    for name, value in key_consts.items():
        assert value in si.SOURCE_AWARE_FIELD_REGISTRY, (
            f"{name}={value!r} is defined but NOT in SOURCE_AWARE_FIELD_REGISTRY")
    # the registry has no extra unmapped names either
    assert set(key_consts.values()) == set(si.SOURCE_AWARE_FIELD_REGISTRY)
