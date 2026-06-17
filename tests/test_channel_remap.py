import json
import os

import numpy as np
import pytest

from block01.core.channel_remap import (
    apply_channel_remap,
    apply_channel_remap_config,
    compute_qupath_auto_minmax,
    fuse_channels,
)
from block01.utils.channel_remap_config import (
    default_channel_remap_config,
    default_channel_remap_params,
    default_source_policy,
    load_channel_remap_config,
    normalize_channel_remap_config,
    normalize_channel_remap_params,
    normalize_source_policy,
    save_channel_remap_config,
    validate_channel_remap_config,
)


# ── apply_channel_remap: output range / dtype ───────────────────────────────

def test_output_range_and_dtype():
    img = np.array([[0, 1000, 5000], [8000, 12000, 60000]], dtype=np.uint16)
    out = apply_channel_remap(img, {"min": 300.0, "max": 8000.0})
    assert out.dtype == np.float32
    assert out.shape == img.shape
    assert float(out.min()) >= 0.0
    assert float(out.max()) <= 1.0


def test_identity_params_window_to_full_range():
    # min/max spanning the data, gamma=contrast=1, brightness=0 -> linear 0..1
    img = np.linspace(0, 100, 11, dtype=np.float32)
    out = apply_channel_remap(img, {"min": 0.0, "max": 100.0})
    expected = np.linspace(0, 1, 11, dtype=np.float32)
    np.testing.assert_allclose(out, expected, atol=1e-6)


# ── min/max clipping ────────────────────────────────────────────────────────

def test_min_max_clipping():
    img = np.array([100, 300, 4150, 8000, 9000], dtype=np.float32)
    out = apply_channel_remap(img, {"min": 300.0, "max": 8000.0})
    # <= min -> 0, >= max -> 1, midpoint ~ 0.5
    assert out[0] == pytest.approx(0.0)
    assert out[1] == pytest.approx(0.0)
    assert out[3] == pytest.approx(1.0)
    assert out[4] == pytest.approx(1.0)
    assert out[2] == pytest.approx(0.5, abs=1e-3)


# ── brightness / contrast ───────────────────────────────────────────────────

def test_brightness_shifts_up():
    img = np.array([0.0, 50.0, 100.0], dtype=np.float32)
    base = apply_channel_remap(img, {"min": 0.0, "max": 100.0})
    bright = apply_channel_remap(img, {"min": 0.0, "max": 100.0, "brightness": 0.2})
    # interior value should increase (and stay within [0,1])
    assert bright[1] > base[1]
    assert float(bright.max()) <= 1.0


def test_contrast_steepens_around_midpoint():
    img = np.array([0.0, 25.0, 50.0, 75.0, 100.0], dtype=np.float32)
    high_c = apply_channel_remap(img, {"min": 0.0, "max": 100.0, "contrast": 2.0})
    # contrast pivots around 0.5: low quartile pushed down, high quartile up
    assert high_c[1] < 0.25
    assert high_c[3] > 0.75
    assert high_c[2] == pytest.approx(0.5, abs=1e-3)


# ── gamma ───────────────────────────────────────────────────────────────────

def test_gamma_lifts_or_crushes():
    img = np.array([0.0, 25.0, 50.0, 75.0, 100.0], dtype=np.float32)
    g_low = apply_channel_remap(img, {"min": 0.0, "max": 100.0, "gamma": 0.5})
    g_high = apply_channel_remap(img, {"min": 0.0, "max": 100.0, "gamma": 2.0})
    # gamma<1 lifts midtones above linear; gamma>1 crushes them below
    linear = apply_channel_remap(img, {"min": 0.0, "max": 100.0})
    assert g_low[2] > linear[2]
    assert g_high[2] < linear[2]
    # 0 and 1 endpoints are fixed under gamma
    assert g_low[0] == pytest.approx(0.0)
    assert g_high[4] == pytest.approx(1.0)


def test_gamma_nonpositive_is_handled_safely():
    img = np.array([0.0, 50.0, 100.0], dtype=np.float32)
    out = apply_channel_remap(img, {"min": 0.0, "max": 100.0, "gamma": 0.0})
    assert np.all(np.isfinite(out))
    assert float(out.min()) >= 0.0 and float(out.max()) <= 1.0


# ── auto min/max (QuPath percentile) ────────────────────────────────────────

def test_auto_minmax_percentiles():
    # 0..1000 inclusive: percentile 0.1 ~ 1.0, percentile 99.9 ~ 999.0
    img = np.arange(0, 1001, dtype=np.float32)
    lo, hi = compute_qupath_auto_minmax(img, saturation=0.1)
    assert lo == pytest.approx(np.percentile(img, 0.1), abs=1e-3)
    assert hi == pytest.approx(np.percentile(img, 99.9), abs=1e-3)
    assert lo < hi


def test_auto_excludes_nonfinite_and_optionally_zero():
    img = np.array([np.nan, np.inf, 0, 0, 10, 20, 30, 40], dtype=np.float32)
    lo, hi = compute_qupath_auto_minmax(img, saturation=0.0, exclude_zero=True)
    # zeros and non-finite excluded -> window over {10,20,30,40}
    assert lo == pytest.approx(10.0)
    assert hi == pytest.approx(40.0)


def test_auto_empty_returns_safe_default():
    img = np.array([np.nan, np.inf], dtype=np.float32)
    lo, hi = compute_qupath_auto_minmax(img)
    assert (lo, hi) == (0.0, 1.0)


# ── edge cases ──────────────────────────────────────────────────────────────

def test_degenerate_window_returns_zeros():
    img = np.array([5.0, 5.0, 5.0], dtype=np.float32)
    out = apply_channel_remap(img, {"min": 5.0, "max": 5.0})
    np.testing.assert_array_equal(out, np.zeros_like(img))
    # max < min also degenerate
    out2 = apply_channel_remap(img, {"min": 10.0, "max": 1.0})
    np.testing.assert_array_equal(out2, np.zeros_like(img))


def test_all_zero_image_with_default_window():
    img = np.zeros((4, 4), dtype=np.float32)
    out = apply_channel_remap(img, None)  # defaults derive min==max==0 -> zeros
    assert out.dtype == np.float32
    np.testing.assert_array_equal(out, np.zeros_like(img))


def test_empty_image():
    img = np.array([], dtype=np.float32)
    out = apply_channel_remap(img, {"min": 0.0, "max": 1.0})
    assert out.dtype == np.float32
    assert out.size == 0


def test_nan_inf_pixels_map_to_zero():
    img = np.array([np.nan, np.inf, -np.inf, 50.0, 100.0], dtype=np.float32)
    out = apply_channel_remap(img, {"min": 0.0, "max": 100.0})
    assert out[0] == 0.0 and out[1] == 0.0 and out[2] == 0.0
    assert out[4] == pytest.approx(1.0)
    assert np.all(np.isfinite(out))


def test_integer_image_supported():
    img = np.array([[0, 128, 255]], dtype=np.uint8)
    out = apply_channel_remap(img, {"min": 0.0, "max": 255.0})
    assert out.dtype == np.float32
    assert out[0, 0] == pytest.approx(0.0)
    assert out[0, 2] == pytest.approx(1.0)


# ── fusion ──────────────────────────────────────────────────────────────────

def test_fuse_channels_weighted_average_in_range():
    a = np.full((3, 3), 0.2, dtype=np.float32)
    b = np.full((3, 3), 0.8, dtype=np.float32)
    fused = fuse_channels([a, b], [1.0, 1.0])
    assert float(fused.min()) >= 0.0 and float(fused.max()) <= 1.0
    assert fused[0, 0] == pytest.approx(0.5)


def test_fuse_channels_zero_total_weight_returns_zeros():
    a = np.full((2, 2), 0.5, dtype=np.float32)
    fused = fuse_channels([a], [0.0])
    np.testing.assert_array_equal(fused, np.zeros_like(a))


def test_apply_config_skips_disabled_and_missing():
    imgs = {
        "A": np.full((2, 2), 100.0, dtype=np.float32),
        "B": np.full((2, 2), 100.0, dtype=np.float32),
    }
    cfg = {
        "channels": {
            "A": {"enabled": True, "min": 0.0, "max": 100.0, "weight": 1.0},
            "B": {"enabled": False, "min": 0.0, "max": 100.0, "weight": 1.0},
            "C": {"enabled": True, "min": 0.0, "max": 100.0},  # missing image
        }
    }
    res = apply_channel_remap_config(imgs, cfg)
    assert set(res["remapped"].keys()) == {"A"}
    assert res["fused"] is not None
    assert res["fused"].dtype == np.float32


# ── config utilities ────────────────────────────────────────────────────────

def test_default_params_shape():
    p = default_channel_remap_params()
    for key in ("enabled", "min", "max", "brightness", "contrast", "gamma",
                "opacity", "weight", "auto"):
        assert key in p


def test_normalize_params_coerces_types():
    p = normalize_channel_remap_params({"min": "300", "max": "8000",
                                        "enabled": 1, "gamma": "2.0"})
    assert p["min"] == 300.0 and isinstance(p["min"], float)
    assert p["max"] == 8000.0
    assert p["enabled"] is True
    assert p["gamma"] == 2.0


def test_validate_rejects_max_le_min():
    cfg = default_channel_remap_config()
    cfg["channels"]["X"] = {"min": 8000.0, "max": 300.0}
    errors = validate_channel_remap_config(cfg)
    assert any("max" in e for e in errors)


def test_validate_forces_segmentation_only():
    cfg = default_channel_remap_config(["A"])
    cfg["used_for"] = "expression"
    errors = validate_channel_remap_config(cfg)
    assert any("used_for" in e for e in errors)


def test_validate_clean_config_passes():
    cfg = default_channel_remap_config()
    cfg["channels"]["CD45"] = {
        "enabled": True, "min": 300.0, "max": 8000.0,
        "brightness": 0.0, "contrast": 1.0, "gamma": 1.0,
        "opacity": 1.0, "weight": 1.0, "auto": False,
    }
    assert validate_channel_remap_config(cfg) == []


def test_save_load_roundtrip(tmp_path):
    cfg = default_channel_remap_config()
    cfg["channels"]["CD45"] = {
        "enabled": True, "min": 300.0, "max": 8000.0,
        "brightness": 0.1, "contrast": 1.2, "gamma": 0.8,
        "opacity": 1.0, "weight": 2.0, "auto": False,
    }
    path = os.path.join(str(tmp_path), "channel_remap_config.json")
    save_channel_remap_config(cfg, path)
    assert os.path.isfile(path)

    loaded = load_channel_remap_config(path)
    assert loaded["used_for"] == "segmentation_only"
    assert loaded["channels"]["CD45"]["min"] == 300.0
    assert loaded["channels"]["CD45"]["weight"] == 2.0
    # saved file is valid JSON with the forced provenance fields
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    assert raw["version"] == "v13.1"
    assert raw["mode"] == "manual_remap"


def test_save_refuses_invalid_config(tmp_path):
    cfg = default_channel_remap_config()
    cfg["channels"]["X"] = {"min": 100.0, "max": 50.0}  # degenerate
    path = os.path.join(str(tmp_path), "bad.json")
    with pytest.raises(ValueError):
        save_channel_remap_config(cfg, path)
    assert not os.path.isfile(path)


# ── source_policy / provenance guard (Phase 2.1b) ───────────────────────────

def test_default_config_includes_source_policy_preview_only():
    cfg = default_channel_remap_config()
    assert "source_policy" in cfg
    sp = cfg["source_policy"]
    assert sp["preview_only"] is True
    assert sp["intensity_space"] == "unknown"
    assert sp["normalization"] == "unknown"


def test_validate_requires_source_policy():
    cfg = default_channel_remap_config(["A"])
    del cfg["source_policy"]
    errors = validate_channel_remap_config(cfg)
    assert any("source_policy" in e for e in errors)


def test_validate_requires_preview_only_field():
    cfg = default_channel_remap_config(["A"])
    cfg["source_policy"] = {"source": "x"}  # missing preview_only
    errors = validate_channel_remap_config(cfg)
    assert any("preview_only" in e for e in errors)


def test_normalize_fills_source_policy_defaults():
    cfg = normalize_channel_remap_config({"channels": {}})
    assert isinstance(cfg["source_policy"], dict)
    assert cfg["source_policy"]["preview_only"] is True


def test_normalize_source_policy_coerces():
    sp = normalize_source_policy({"intensity_space": "corrected_zarr_native_float",
                                  "preview_only": 0})
    assert sp["intensity_space"] == "corrected_zarr_native_float"
    assert sp["preview_only"] is False
    assert sp["source"] == "unknown"  # filled from defaults


def test_save_load_preserves_source_policy(tmp_path):
    cfg = default_channel_remap_config(["CD45"])
    cfg["channels"]["CD45"]["min"] = 350.0
    cfg["channels"]["CD45"]["max"] = 12000.0
    cfg["source_policy"] = {
        "source": "step3_current_roi",
        "intensity_space": "corrected_zarr_native_float",
        "normalization": "none",
        "scope": "roi_preview",
        "preview_only": True,
    }
    path = os.path.join(str(tmp_path), "channel_remap_config.json")
    save_channel_remap_config(cfg, path)
    loaded = load_channel_remap_config(path)
    sp = loaded["source_policy"]
    assert sp["intensity_space"] == "corrected_zarr_native_float"
    assert sp["normalization"] == "none"
    assert sp["scope"] == "roi_preview"
    assert sp["preview_only"] is True


def test_default_source_policy_is_independent_copy():
    a = default_source_policy()
    a["preview_only"] = False
    assert default_source_policy()["preview_only"] is True
