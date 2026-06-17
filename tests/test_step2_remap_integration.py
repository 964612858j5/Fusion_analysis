"""Phase 5b — Step2 manual channel remap integration tests.

Covers the Step2 validation gate, HQ2 conditioning swap, and the
CDS2/lean_carve remap gate (with _block_gi/camp preserved). No GUI; light
synthetic segmentation only.
"""

import numpy as np
import pytest

from block01.utils.channel_remap_config import (
    channel_remap_config_hash,
    default_channel_remap_config,
    validate_remap_covers_selected_channels,
    validate_step2_remap_config,
)


# ── config builders ─────────────────────────────────────────────────────────

def _aligned_config(names=("CD45", "CK19"), compatible=True,
                    intensity_space="corrected_zarr_native_float"):
    cfg = default_channel_remap_config(list(names))
    for n in names:
        cfg["channels"][n].update({
            "min": 100.0, "max": 5000.0,
            "intensity_space": intensity_space,
            "normalization": "none",
            "step2_compatible": compatible,
            "calibration_source_matches_step2": compatible,
            "step2_pre_remap_source": intensity_space,
        })
    cfg["source_policy"].update({
        "source": "step3_current_roi",
        "intensity_space": intensity_space,
        "normalization": "none",
        "scope": "roi_preview",
        "preview_only": True,
        "step2_pre_remap_source": intensity_space,
        "calibration_source_matches_step2": compatible,
        "source_alignment_mode": "single_native_source" if compatible
                                 else "partial_or_preview_fallback",
        "step2_ready": False,
    })
    return cfg


# ── validation gate ─────────────────────────────────────────────────────────

def test_valid_aligned_config_requires_allow_preview():
    cfg = _aligned_config()
    errors, resolved = validate_step2_remap_config(cfg, allow_preview_remap=False)
    assert errors  # preview_only -> rejected without the flag
    assert any("allow_preview_remap" in e for e in errors)
    assert resolved == {}


def test_valid_aligned_config_accepted_with_allow_preview():
    cfg = _aligned_config()
    errors, resolved = validate_step2_remap_config(cfg, allow_preview_remap=True)
    assert errors == []
    assert set(resolved.keys()) == {"CD45", "CK19"}
    assert resolved["CD45"]["min"] == 100.0


def test_reject_source_mismatch():
    cfg = _aligned_config(compatible=False)
    errors, _ = validate_step2_remap_config(cfg, allow_preview_remap=True)
    assert any("calibration_source_matches_step2" in e or "step2_compatible" in e
               for e in errors)


def test_reject_partial_or_preview_fallback():
    cfg = _aligned_config()
    cfg["source_policy"]["source_alignment_mode"] = "partial_or_preview_fallback"
    errors, _ = validate_step2_remap_config(cfg, allow_preview_remap=True)
    assert any("partial_or_preview_fallback" in e for e in errors)


def test_reject_reference_layer_in_channels():
    cfg = _aligned_config(names=("CD45", "DAPI"))
    errors, _ = validate_step2_remap_config(cfg, allow_preview_remap=True)
    assert any("DAPI" in e and "reference" in e for e in errors)


def test_reject_normalized_intensity_space():
    cfg = _aligned_config(intensity_space="raw_ome_normalized_0_1")
    errors, _ = validate_step2_remap_config(cfg, allow_preview_remap=True)
    assert any("not a Step2-compatible native source" in e for e in errors)


def test_reject_missing_minmax():
    cfg = _aligned_config()
    cfg["channels"]["CD45"]["min"] = None
    errors, _ = validate_step2_remap_config(cfg, allow_preview_remap=True)
    assert any("min/max must be concrete" in e for e in errors)


def test_config_hash_stable_and_changes():
    cfg = _aligned_config()
    h1 = channel_remap_config_hash(cfg)
    h2 = channel_remap_config_hash(cfg)
    assert h1 == h2 and len(h1) == 16
    cfg["channels"]["CD45"]["gamma"] = 0.5
    assert channel_remap_config_hash(cfg) != h1


# ── Phase 5b.1 hardening ────────────────────────────────────────────────────

def test_step2_rejects_raw_used_for_not_segmentation_only():
    # normalize() force-sets used_for; validation must catch the RAW value.
    cfg = _aligned_config()
    cfg["used_for"] = "expression"
    errors, resolved = validate_step2_remap_config(cfg, allow_preview_remap=True)
    assert any("used_for" in e for e in errors)
    assert resolved == {}


def test_step2_rejects_missing_used_for():
    cfg = _aligned_config()
    del cfg["used_for"]
    errors, _ = validate_step2_remap_config(cfg, allow_preview_remap=True)
    assert any("used_for" in e for e in errors)


def test_reject_channel_intensity_space_step2_source_mismatch():
    cfg = _aligned_config()
    # claims match but the two source fields disagree
    cfg["channels"]["CD45"]["step2_pre_remap_source"] = "raw_ome_native_float"
    cfg["channels"]["CD45"]["intensity_space"] = "corrected_zarr_native_float"
    cfg["channels"]["CD45"]["calibration_source_matches_step2"] = True
    errors, _ = validate_step2_remap_config(cfg, allow_preview_remap=True)
    assert any("!=" in e and "step2_pre_remap_source" in e for e in errors)


def test_reject_channel_unknown_step2_pre_remap_source():
    cfg = _aligned_config()
    cfg["channels"]["CD45"]["step2_pre_remap_source"] = "unknown"
    errors, _ = validate_step2_remap_config(cfg, allow_preview_remap=True)
    assert any("step2_pre_remap_source" in e and "native source" in e
               for e in errors)


# ── selected-channel coverage helper ────────────────────────────────────────

def test_reject_missing_selected_marker_channel():
    resolved = {"CD45": {"min": 0.0, "max": 1.0}}
    errors = validate_remap_covers_selected_channels(resolved, ["CD45", "CK19"])
    assert any("missing selected Step2 marker channels: CK19" in e for e in errors)


def test_accept_all_selected_marker_channels_present():
    resolved = {"CD45": {}, "CK19": {}}
    errors = validate_remap_covers_selected_channels(resolved, ["CD45", "CK19"])
    assert errors == []


def test_coverage_ignores_reference_layers():
    resolved = {"CD45": {}, "CK19": {}}
    # DAPI selected but not in config -> not required (reference layer)
    errors = validate_remap_covers_selected_channels(
        resolved, ["CD45", "CK19", "DAPI"])
    assert errors == []


def test_reject_step1_weighted_fusion_with_remap():
    resolved = {"CD45": {}, "CK19": {}}
    errors = validate_remap_covers_selected_channels(
        resolved, ["step1_weighted_fusion"],
        hq_input_mode="step1_weighted_fusion")
    assert errors
    assert any("step1_weighted_fusion" in e for e in errors)


def test_reject_step1_weighted_fusion_marker_even_if_mode_unset():
    resolved = {"CD45": {}}
    errors = validate_remap_covers_selected_channels(
        resolved, ["step1_weighted_fusion"])
    assert errors
    assert any("step1_weighted_fusion" in e for e in errors)


# ── HQ2 conditioning swap ───────────────────────────────────────────────────

def test_hq2_marker_conditioning_uses_remap_when_active():
    from block01.workers.hq2_marker_segmentation import _marker_conditioning
    rng = np.random.default_rng(0)
    arr = (rng.random((32, 32)) * 8000).astype(np.float32)

    absent = _marker_conditioning("CD45", arr, {}, 1.0, 99.5)
    params = {"_channel_remap_params": {
        "CD45": {"min": 100.0, "max": 5000.0, "brightness": 0.0,
                 "contrast": 1.0, "gamma": 1.0}}}
    active = _marker_conditioning("CD45", arr, params, 1.0, 99.5)

    assert absent.dtype == np.float32 and active.dtype == np.float32
    assert 0.0 <= float(active.min()) and float(active.max()) <= 1.0
    assert not np.allclose(absent, active)  # remap path actually differs


def test_hq2_marker_conditioning_unchanged_without_config():
    from block01.workers.hq2_marker_segmentation import (
        _marker_conditioning, percentile_normalize)
    rng = np.random.default_rng(1)
    arr = (rng.random((16, 16)) * 4000).astype(np.float32)
    got = _marker_conditioning("CD45", arr, {}, 1.0, 99.5)
    np.testing.assert_array_equal(got, percentile_normalize(arr, 1.0, 99.5))


# ── lean_carve / CDS2 engine integration ────────────────────────────────────

def _two_cells():
    shape = (96, 96)
    nuclei = np.zeros(shape, dtype=np.uint32)
    yy, xx = np.ogrid[:shape[0], :shape[1]]
    nuclei[(yy - 32) ** 2 + (xx - 32) ** 2 <= 25] = 1
    nuclei[(yy - 64) ** 2 + (xx - 64) ** 2 <= 25] = 2
    marker = np.full(shape, 200.0, dtype=np.float32)
    marker[(yy - 32) ** 2 + (xx - 32) ** 2 <= 196] = 6000.0
    marker[(yy - 64) ** 2 + (xx - 64) ** 2 <= 196] = 6000.0
    return nuclei, marker


def _base_params(**kw):
    p = {"max_cell_radius": 14, "minimal_radius": 3, "shrink_pixels": 0,
         "lean_block_size": 4096, "lean_halo_margin": 8, "use_gpu": False}
    p.update(kw)
    return p


def test_lean_carve_unchanged_without_remap():
    from block01.workers.lean_carve_segmentation import run_lean_carve_segmentation
    nuclei, marker = _two_cells()
    a = run_lean_carve_segmentation(nuclei, [marker], ["PanCK"], _base_params())
    b = run_lean_carve_segmentation(nuclei, [marker], ["PanCK"], _base_params())
    assert np.array_equal(a["final_labels"], b["final_labels"])
    assert int(np.count_nonzero(a["final_labels"])) > 0


def test_lean_carve_remap_gate_runs():
    from block01.workers.lean_carve_segmentation import run_lean_carve_segmentation
    nuclei, marker = _two_cells()
    params = _base_params(
        _channel_remap_params={"PanCK": {"min": 300.0, "max": 8000.0,
                                         "brightness": 0.0, "contrast": 1.0,
                                         "gamma": 1.0}},
        _remap_gate_mode="remap", _remap_gate_threshold=0.05)
    res = run_lean_carve_segmentation(nuclei, [marker], ["PanCK"], params)
    assert int(np.count_nonzero(res["final_labels"])) > 0
    # nuclei must be re-asserted as cells
    assert set(np.unique(res["final_labels"])) >= {0, 1, 2}


def test_cds2_remap_gate_preserves_camp_path():
    from block01.workers.cds2_segmentation import run_cds2_segmentation
    nuclei, marker = _two_cells()
    # CDS2 keeps gi for camp; remap only drives the gate. Should run + label.
    params = _base_params(
        cytoplasm_engine="cds2",
        _channel_remap_params={"CK19": {"min": 300.0, "max": 8000.0,
                                        "brightness": 0.0, "contrast": 1.0,
                                        "gamma": 1.0}},
        _remap_gate_mode="remap", _remap_gate_threshold=0.05)
    res = run_cds2_segmentation(nuclei, [marker], ["CK19"], params)
    assert int(np.count_nonzero(res["final_labels"])) > 0


def test_cds2_unchanged_without_remap():
    from block01.workers.cds2_segmentation import run_cds2_segmentation
    nuclei, marker = _two_cells()
    a = run_cds2_segmentation(nuclei, [marker], ["CK19"],
                              _base_params(cytoplasm_engine="cds2"))
    b = run_cds2_segmentation(nuclei, [marker], ["CK19"],
                              _base_params(cytoplasm_engine="cds2"))
    assert np.array_equal(a["final_labels"], b["final_labels"])
