"""Phase 5b — Step2 manual channel remap integration tests.

Covers the Step2 validation gate, HQ2 conditioning swap, and the
CDS2/lean_carve remap gate (with _block_gi/camp preserved). No GUI; light
synthetic segmentation only.
"""

import os

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


# ── Phase 5c — selected_channels string parsing robustness ──────────────────

def test_validate_remap_covers_selected_channels_accepts_semicolon_string():
    resolved = {"CD45": {}, "CK19": {}}
    assert validate_remap_covers_selected_channels(resolved, "CD45;CK19") == []
    miss = validate_remap_covers_selected_channels(resolved, "CD45;CK19;CD68")
    assert any("CD68" in e for e in miss)


def test_validate_remap_covers_selected_channels_accepts_comma_string():
    resolved = {"CD45": {}, "CK19": {}}
    assert validate_remap_covers_selected_channels(resolved, "CD45, CK19") == []


def test_validate_remap_covers_selected_channels_does_not_iterate_characters():
    # "CD45" must be ONE channel, not characters C,D,4,5.
    resolved = {"CD45": {}}
    assert validate_remap_covers_selected_channels(resolved, "CD45") == []
    # a single missing channel string reports the whole token, not a char
    errors = validate_remap_covers_selected_channels({"CK19": {}}, "CD45")
    assert any("CD45" in e for e in errors)
    assert not any(e.endswith(": C") for e in errors)


def test_validate_remap_covers_selected_channels_accepts_tuple_and_set():
    resolved = {"CD45": {}, "CK19": {}}
    assert validate_remap_covers_selected_channels(resolved, ("CD45", "CK19")) == []
    assert validate_remap_covers_selected_channels(resolved, {"CD45", "CK19"}) == []


# ── Phase 5c — smoke-test helpers ───────────────────────────────────────────

def test_build_mode_seg_config_baseline_has_no_remap_keys():
    from block01.scripts.smoke_test_step2_remap_small_roi import build_mode_seg_config
    cfg = build_mode_seg_config({"method": "cds2"}, "baseline")
    assert "channel_remap_config_path" not in cfg
    assert cfg["method"] == "cds2"


def test_build_mode_seg_config_remap_modes():
    from block01.scripts.smoke_test_step2_remap_small_roi import build_mode_seg_config
    for mode, gate in (("gi", "gi"), ("remap", "remap"),
                       ("remap_and_gi", "remap_and_gi")):
        cfg = build_mode_seg_config({"method": "cds2"}, mode,
                                    remap_config_path="/tmp/x.json",
                                    allow_preview_remap=True)
        assert cfg["remap_gate_mode"] == gate
        assert cfg["allow_preview_remap"] is True
        assert cfg["channel_remap_config_path"].endswith("x.json")


def test_build_mode_seg_config_requires_config_for_remap():
    from block01.scripts.smoke_test_step2_remap_small_roi import build_mode_seg_config
    with pytest.raises(ValueError):
        build_mode_seg_config({}, "remap", remap_config_path=None)


def test_build_mode_seg_config_strips_stale_remap_keys_for_baseline():
    from block01.scripts.smoke_test_step2_remap_small_roi import build_mode_seg_config
    base = {"method": "cds2", "channel_remap_config_path": "/old.json",
            "remap_gate_mode": "remap"}
    cfg = build_mode_seg_config(base, "baseline")
    assert "channel_remap_config_path" not in cfg
    assert "remap_gate_mode" not in cfg


def test_check_remap_provenance_missing(tmp_path):
    from block01.scripts.smoke_test_step2_remap_small_roi import check_remap_provenance
    info = check_remap_provenance(str(tmp_path), expect_remap=True)
    assert info["used_json_exists"] is False
    assert info["provenance_json_exists"] is False
    assert info["ok"] is False


def test_check_remap_provenance_ok(tmp_path):
    import json as _json
    from block01.scripts.smoke_test_step2_remap_small_roi import check_remap_provenance
    d = str(tmp_path)
    with open(os.path.join(d, "channel_remap_config.used.json"), "w") as f:
        _json.dump({"used_for": "segmentation_only"}, f)
    with open(os.path.join(d, "channel_remap_provenance.json"), "w") as f:
        _json.dump({"manual_remap_enabled": True, "allow_preview_remap": True,
                    "gate_mode": "remap", "channel_remap_config_hash": "abc",
                    "used_for": "segmentation_only",
                    "source_policy": {"calibration_source_matches_step2": True}}, f)
    info = check_remap_provenance(d, expect_remap=True)
    assert info["ok"] is True
    assert info["remap_gate_mode"] == "remap"


def test_check_remap_provenance_baseline_ok_when_absent(tmp_path):
    from block01.scripts.smoke_test_step2_remap_small_roi import check_remap_provenance
    info = check_remap_provenance(str(tmp_path), expect_remap=False)
    assert info["ok"] is True


def test_format_smoke_summary_table():
    from block01.scripts.smoke_test_step2_remap_small_roi import format_smoke_summary_table
    rows = [
        {"roi": "clean", "mode": "remap", "status": "ok", "runtime": "3s",
         "cells": 120, "mask_area": 4000, "mean_area": 33, "provenance": "yes"},
        {"roi": "AF_heavy", "mode": "gi", "status": "ok"},
    ]
    table = format_smoke_summary_table(rows)
    assert "roi" in table and "mode" in table and "provenance" in table
    assert "clean" in table and "AF_heavy" in table
    assert "remap" in table
    # missing fields render as '-'
    assert "-" in table.splitlines()[-1]


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


# ── Phase 5c.1: Step2 GUI exposes remap controls into seg_config ─────────────

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def _qapp():
    pytest.importorskip("PyQt5")
    from PyQt5 import QtWidgets
    try:
        return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    except Exception as exc:  # no usable Qt platform -> skip
        pytest.skip(f"Qt unavailable: {exc}")


@pytest.fixture()
def _step2(_qapp):
    from block01.ui.step2_page import Step2Page
    return Step2Page()


def test_step2_gui_no_remap_path_leaves_keys_absent(_step2):
    cfg = _step2.get_seg_config()
    assert "channel_remap_config_path" not in cfg
    assert "allow_preview_remap" not in cfg
    assert "remap_gate_mode" not in cfg


# The manual remap box was removed: the Step0 saved remap now auto-applies —
# whole-cell/DAPI via the Step1 fused input, marker methods (HQ/HQ2/CSD) via the
# ROI's step0_channel_remap.json in get_seg_config.

def _set_method(page, method):
    idx = page._method_combo.findData(method)
    assert idx >= 0, method
    page._method_combo.setCurrentIndex(idx)


def _roi_with_remap(tmp_path):
    from block01.utils.channel_remap_config import save_channel_remap_config
    step0 = tmp_path / "rois" / "r1" / "step0"
    step0.mkdir(parents=True)
    save_channel_remap_config(
        {"channels": {"PanCK": {"min": 0.0, "max": 200.0, "gamma": 1.0}},
         "source_policy": {"preview_only": True, "step2_ready": False},
         "used_for": "segmentation_only"},
        str(step0 / "step0_channel_remap.json"))
    return str(tmp_path / "rois" / "r1")


def test_step2_no_box_widgets(_step2):
    # the manual-remap box is gone
    from PyQt5 import QtWidgets
    assert not hasattr(_step2, "_remap_config_edit")
    titles = [b.title() for b in _step2.findChildren(QtWidgets.QGroupBox)]
    assert not any("Manual channel remap" in t for t in titles)


# get_seg_config must NEVER attach the Step0 preview config: it is Step2-
# incompatible by design and the worker's validator rejects it (a crash). The
# Step0 remap reaches Step2 ONLY via source-alignment promotion at launch
# (_promote_step0_remap), which self-validates and attaches a step2_ready config
# or nothing.

def test_step2_get_seg_config_never_attaches_preview_remap(_step2, tmp_path):
    from block01.utils.segmentation_config import (
        CELLPOSE_NUCLEI_HQ, CELLPOSE_NUCLEI_HQ2, CELLPOSE_NUCLEI_CSD,
        MESMER_WHOLE_CELL, CELLPOSE_WHOLECELL_FUSION)
    _step2.set_roi_context(roi_id="r1", roi_dir=_roi_with_remap(tmp_path))
    for m in (CELLPOSE_NUCLEI_HQ, CELLPOSE_NUCLEI_HQ2, CELLPOSE_NUCLEI_CSD,
              MESMER_WHOLE_CELL, CELLPOSE_WHOLECELL_FUSION):
        _set_method(_step2, m)
        cfg = _step2.get_seg_config()
        assert "channel_remap_config_path" not in cfg, m


def test_promote_step0_remap_non_hq2csd_is_noop(_step2, tmp_path):
    # only HQ2/CSD read raw_ome markers that match Step0's raw calibration
    from block01.utils.segmentation_config import MESMER_WHOLE_CELL
    _step2.set_roi_context(roi_id="r1", roi_dir=_roi_with_remap(tmp_path))
    _step2._rois = []
    _step2._full_h, _step2._full_w = 512, 512
    seg = {"method": MESMER_WHOLE_CELL}
    _step2._promote_step0_remap(seg)
    assert "channel_remap_config_path" not in seg


def test_promote_step0_remap_no_preview_config_is_noop(_step2, tmp_path):
    from block01.utils.segmentation_config import CELLPOSE_NUCLEI_HQ2
    _step2.set_roi_context(roi_id="r1", roi_dir=str(tmp_path))  # no step0 remap file
    _step2._rois = []
    _step2._full_h, _step2._full_w = 512, 512
    seg = {"method": CELLPOSE_NUCLEI_HQ2, "hq_channels": ["PanCK"]}
    _step2._promote_step0_remap(seg)
    assert "channel_remap_config_path" not in seg


def test_promote_step0_remap_roi_run_skipped(_step2, tmp_path):
    # an ROI crop is a different grid than the full raw_ome source; skip (no crash)
    from block01.utils.segmentation_config import CELLPOSE_NUCLEI_HQ2
    _step2.set_roi_context(roi_id="r1", roi_dir=_roi_with_remap(tmp_path))
    _step2._rois = [{"name": "r1", "bbox_fullres": [0, 100, 0, 100]}]
    _step2._full_h, _step2._full_w = 512, 512
    seg = {"method": CELLPOSE_NUCLEI_HQ2, "hq_channels": ["PanCK"]}
    _step2._promote_step0_remap(seg)
    assert "channel_remap_config_path" not in seg


def test_promote_step0_remap_attaches_promoted_on_success(_step2, tmp_path, monkeypatch):
    from block01.utils.segmentation_config import CELLPOSE_NUCLEI_HQ2
    _step2.set_roi_context(roi_id="r1", roi_dir=_roi_with_remap(tmp_path),
                           step2_dir=str(tmp_path / "step2"))
    _step2._rois = []
    _step2._full_h, _step2._full_w = 512, 512
    promoted = {"channels": {"PanCK": {"min": 0.0, "max": 200.0}},
                "source_policy": {"step2_ready": True, "preview_only": False},
                "used_for": "segmentation_only"}
    monkeypatch.setattr("block01.scripts.promote_remap_config.build_resolved_source",
                        lambda cfg: object())
    monkeypatch.setattr(
        "block01.utils.remap_promotion.promote_step1_5_config_for_step2",
        lambda *a, **k: (promoted, {"promoted": True, "source_kind": "raw_ome",
                                    "source_path": "/x/raw.ome.tiff"}))
    seg = {"method": CELLPOSE_NUCLEI_HQ2, "hq_channels": ["PanCK"]}
    _step2._promote_step0_remap(seg)
    assert seg["channel_remap_config_path"].endswith("channel_remap_config.step2ready.json")
    assert seg["allow_preview_remap"] is False
    assert seg["remap_gate_mode"] == "remap_and_gi"
    assert os.path.exists(seg["channel_remap_config_path"])


def test_promote_step0_remap_refusal_attaches_nothing(_step2, tmp_path, monkeypatch):
    from block01.utils.segmentation_config import CELLPOSE_NUCLEI_HQ2
    _step2.set_roi_context(roi_id="r1", roi_dir=_roi_with_remap(tmp_path),
                           step2_dir=str(tmp_path / "step2"))
    _step2._rois = []
    _step2._full_h, _step2._full_w = 512, 512
    monkeypatch.setattr("block01.scripts.promote_remap_config.build_resolved_source",
                        lambda cfg: object())
    monkeypatch.setattr(
        "block01.utils.remap_promotion.promote_step1_5_config_for_step2",
        lambda *a, **k: (None, {"promoted": False, "failures": ["source mismatch"]}))
    seg = {"method": CELLPOSE_NUCLEI_HQ2, "hq_channels": ["PanCK"]}
    _step2._promote_step0_remap(seg)
    assert "channel_remap_config_path" not in seg


def test_promote_step0_remap_uncovered_channel_attaches_nothing(_step2, tmp_path, monkeypatch):
    # promotion succeeded but the promoted config does not cover a selected marker
    # -> refuse (no silent mixed remap/legacy gate)
    from block01.utils.segmentation_config import CELLPOSE_NUCLEI_HQ2
    _step2.set_roi_context(roi_id="r1", roi_dir=_roi_with_remap(tmp_path),
                           step2_dir=str(tmp_path / "step2"))
    _step2._rois = []
    _step2._full_h, _step2._full_w = 512, 512
    promoted = {"channels": {"PanCK": {"min": 0.0, "max": 200.0}},
                "source_policy": {"step2_ready": True, "preview_only": False},
                "used_for": "segmentation_only"}
    monkeypatch.setattr("block01.scripts.promote_remap_config.build_resolved_source",
                        lambda cfg: object())
    monkeypatch.setattr(
        "block01.utils.remap_promotion.promote_step1_5_config_for_step2",
        lambda *a, **k: (promoted, {"promoted": True}))
    seg = {"method": CELLPOSE_NUCLEI_HQ2, "hq_channels": ["PanCK", "CD45"]}  # CD45 uncovered
    _step2._promote_step0_remap(seg)
    assert "channel_remap_config_path" not in seg


# ── Phase 5d.1: remap gate shape alignment hardening ─────────────────────────

def _remap_params_for(name="PanCK"):
    return {name: {"min": 300.0, "max": 8000.0, "brightness": 0.0,
                   "contrast": 1.0, "gamma": 1.0}}


def test_align_helper_same_shape_unchanged():
    from block01.workers.lean_carve_segmentation import _align_remap_gate_to_reference
    cond = np.arange(48 * 50, dtype=np.float32).reshape(48, 50)
    ref = np.zeros((48, 50), dtype=bool)
    out = _align_remap_gate_to_reference(cond, ref)
    assert out is cond  # untouched, same object


def test_align_helper_center_crops_larger_cond():
    from block01.workers.lean_carve_segmentation import _align_remap_gate_to_reference
    cond = np.arange(8 * 8, dtype=np.float32).reshape(8, 8)
    ref = np.zeros((4, 6), dtype=bool)
    out = _align_remap_gate_to_reference(cond, ref, channel_name="CD68")
    assert out.shape == (4, 6)
    # center crop: rows 2..6, cols 1..7 of the 8x8 source
    np.testing.assert_array_equal(out, cond[2:6, 1:7])
    # values are not altered, only cropped
    assert out.dtype == cond.dtype


def test_align_helper_raises_when_cond_smaller():
    from block01.workers.lean_carve_segmentation import _align_remap_gate_to_reference
    cond = np.zeros((10, 10), dtype=np.float32)
    ref = np.zeros((12, 10), dtype=bool)
    with pytest.raises(ValueError) as exc:
        _align_remap_gate_to_reference(
            cond, ref, channel_name="CD68", gate_mode="remap_and_gi",
            block_coords=(0, 0), extra_shapes={"terr_mask": ref.shape})
    msg = str(exc.value)
    assert "CD68" in msg and "remap_and_gi" in msg
    assert "(10, 10)" in msg and "(12, 10)" in msg  # cond + reference shapes


def test_align_helper_raises_when_mixed_one_dim_smaller():
    from block01.workers.lean_carve_segmentation import _align_remap_gate_to_reference
    # larger in rows, smaller in cols -> cannot crop safely -> raise
    cond = np.zeros((20, 8), dtype=np.float32)
    ref = np.zeros((16, 10), dtype=bool)
    with pytest.raises(ValueError):
        _align_remap_gate_to_reference(cond, ref)


def _oversized_loader(pad=24):
    """Callable marker loader that returns blocks LARGER than requested, mimicking
    a native/corrected source on a different pixel grid than the nuclei labels."""
    nuclei, marker = _two_cells()  # 96x96
    h, w = marker.shape
    big = np.full((h + pad, w + pad), 200.0, dtype=np.float32)
    big[pad // 2:pad // 2 + h, pad // 2:pad // 2 + w] = marker
    def getter(name, y0, y1, x0, x1):
        return big.copy()  # ignore coords -> oversized block every call
    return nuclei, getter


def test_lean_carve_remap_gate_aligns_oversized_block():
    from block01.workers.lean_carve_segmentation import run_lean_carve_segmentation
    nuclei, loader = _oversized_loader()
    params = _base_params(_channel_remap_params=_remap_params_for("PanCK"),
                          _remap_gate_mode="remap", _remap_gate_threshold=0.05)
    # Without alignment this raised: operands could not be broadcast together.
    res = run_lean_carve_segmentation(nuclei, loader, ["PanCK"], params)
    assert int(np.count_nonzero(res["final_labels"])) > 0
    assert set(np.unique(res["final_labels"])) >= {0, 1, 2}


def test_lean_carve_remap_and_gi_aligns_oversized_block():
    from block01.workers.lean_carve_segmentation import run_lean_carve_segmentation
    nuclei, loader = _oversized_loader()
    params = _base_params(_channel_remap_params=_remap_params_for("PanCK"),
                          _remap_gate_mode="remap_and_gi", _remap_gate_threshold=0.05)
    res = run_lean_carve_segmentation(nuclei, loader, ["PanCK"], params)
    assert int(np.count_nonzero(res["final_labels"])) > 0


def test_cds2_remap_gate_aligns_oversized_block():
    from block01.workers.cds2_segmentation import run_cds2_segmentation
    nuclei, loader = _oversized_loader()
    params = _base_params(cytoplasm_engine="cds2",
                          _channel_remap_params=_remap_params_for("PanCK"),
                          _remap_gate_mode="remap", _remap_gate_threshold=0.05)
    res = run_cds2_segmentation(nuclei, loader, ["PanCK"], params)
    assert int(np.count_nonzero(res["final_labels"])) > 0


def test_gi_mode_unchanged_with_remap_params_present():
    # gate_mode="gi" -> remap is inactive; result must equal the no-remap baseline.
    from block01.workers.lean_carve_segmentation import run_lean_carve_segmentation
    nuclei, marker = _two_cells()
    baseline = run_lean_carve_segmentation(nuclei, [marker], ["PanCK"], _base_params())
    gi_mode = run_lean_carve_segmentation(
        nuclei, [marker], ["PanCK"],
        _base_params(_channel_remap_params=_remap_params_for("PanCK"),
                     _remap_gate_mode="gi", _remap_gate_threshold=0.05))
    assert np.array_equal(baseline["final_labels"], gi_mode["final_labels"])
