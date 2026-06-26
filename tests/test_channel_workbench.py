"""Headless smoke tests for the v13.1 ChannelWorkbench data bridge.

Runs under an offscreen Qt platform. Skips cleanly if PyQt5 or a Qt platform
plugin is unavailable (e.g. minimal CI), so it never breaks the suite.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets  # noqa: E402

from block01.utils.channel_remap_config import (  # noqa: E402
    load_channel_remap_config,
    save_channel_remap_config,
    validate_channel_remap_config,
)


@pytest.fixture(scope="module")
def app():
    try:
        application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    except Exception as exc:  # no usable Qt platform -> skip the module
        pytest.skip(f"Qt unavailable: {exc}")
    return application


@pytest.fixture()
def workbench(app):
    from block01.ui.widgets.channel_workbench import ChannelWorkbench
    return ChannelWorkbench()


def _real_like_channels():
    rng = np.random.default_rng(0)
    return {
        "DAPI": (rng.random((48, 48)) * 5000).astype(np.float32),
        "CD45": (rng.random((48, 48)) * 8000).astype(np.float32),
        "PanCK": (rng.random((48, 48)) * 2000).astype(np.float32),
    }


def test_empty_input_is_graceful(workbench):
    workbench.set_channel_images({}, source="step3")
    assert workbench.has_channel_data() is False
    assert "No Step3 channel data" in workbench._status_lbl.text()


def test_set_real_channels_populates_list_and_active(workbench):
    workbench.set_channel_images(_real_like_channels(),
                                 context={"roi": "ROI_2"}, source="step3")
    assert workbench.has_channel_data()
    assert workbench._names == ["DAPI", "CD45", "PanCK"]
    assert workbench._active == "DAPI"
    assert "Step3 current ROI" in workbench._status_lbl.text()
    assert "ROI_2" in workbench._status_lbl.text()


def test_auto_sets_window_on_active_channel(workbench):
    workbench.set_channel_images(_real_like_channels(), source="step3")
    workbench._on_auto()
    p = workbench._params[workbench._active]
    assert p["auto"] is True
    assert p["max"] > p["min"]


def test_param_edits_update_params(workbench):
    workbench.set_channel_images(_real_like_channels(), source="step3")
    workbench._sp_min.setValue(100.0)
    workbench._sp_max.setValue(4000.0)
    workbench._sl_gamma.setValue(70)  # -> 0.70
    p = workbench._params[workbench._active]
    assert p["min"] == 100.0
    assert p["max"] == 4000.0
    assert p["gamma"] == pytest.approx(0.70)


def test_build_config_uses_real_channel_names_and_segmentation_only(workbench):
    workbench.set_channel_images(_real_like_channels(), source="step3")
    cfg = workbench.build_config()
    assert set(cfg["channels"].keys()) == {"DAPI", "CD45", "PanCK"}
    assert cfg["used_for"] == "segmentation_only"
    assert validate_channel_remap_config(cfg) == []


def test_save_and_reload_config_roundtrip(workbench, tmp_path):
    workbench.set_channel_images(_real_like_channels(), source="step3")
    path = os.path.join(str(tmp_path), "channel_remap_config.json")
    save_channel_remap_config(workbench.build_config(), path)
    loaded = load_channel_remap_config(path)
    assert set(loaded["channels"].keys()) == {"DAPI", "CD45", "PanCK"}
    assert loaded["used_for"] == "segmentation_only"


def test_non_2d_input_is_skipped(workbench):
    workbench.set_channel_images({"bad": np.zeros((4, 4, 5))}, source="step3")
    assert workbench.has_channel_data() is False


def test_clear_channel_images(workbench):
    workbench.set_channel_images(_real_like_channels(), source="step3")
    workbench.clear_channel_images()
    assert workbench.has_channel_data() is False
    assert workbench._active is None


def test_step3_page_instantiates_with_workbench(app):
    from block01.ui.step3_page import Step3Page
    page = Step3Page()
    assert hasattr(page, "_channel_workbench")
    # adapter with no patch loaded returns an empty dict (no crash)
    assert page._get_current_channel_images_for_conditioning() == {}
    page._sync_step3_to_workbench()
    assert page._channel_workbench.has_channel_data() is False


# ── source provenance guard (Phase 2.1b) ────────────────────────────────────

def test_build_config_includes_source_policy_from_context(workbench):
    policy = {
        "source": "step3_current_roi",
        "intensity_space": "corrected_zarr_native_float",
        "normalization": "none",
        "scope": "roi_preview",
        "preview_only": True,
    }
    workbench.set_channel_images(_real_like_channels(), source="step3",
                                source_policy=policy)
    cfg = workbench.build_config()
    sp = cfg["source_policy"]
    assert sp["source"] == "step3_current_roi"
    assert sp["intensity_space"] == "corrected_zarr_native_float"
    assert sp["preview_only"] is True
    assert validate_channel_remap_config(cfg) == []


def test_build_config_records_per_channel_metadata(workbench):
    meta = {
        "CD45": {"source": "corrected_zarr roi_local",
                 "intensity_space": "corrected_zarr_native_float",
                 "normalization": "none"},
    }
    workbench.set_channel_images(_real_like_channels(), source="step3",
                                channel_metadata=meta)
    cfg = workbench.build_config()
    cd45 = cfg["channels"]["CD45"]
    assert cd45["intensity_space"] == "corrected_zarr_native_float"
    assert "value_min_observed" in cd45 and "value_max_observed" in cd45
    # channels without supplied metadata still get a self-describing space
    assert "intensity_space" in cfg["channels"]["DAPI"]


def test_unknown_source_policy_defaults_preview_only(workbench):
    workbench.set_channel_images(_real_like_channels(), source="step3")
    cfg = workbench.build_config()
    sp = cfg["source_policy"]
    assert sp["preview_only"] is True
    assert sp["intensity_space"] == "unknown"
    # status surfaces the preview-only / unknown warning
    assert "Preview" in workbench._status_lbl.text() or \
           "PREVIEW" in workbench._status_lbl.text()


def test_status_shows_intensity_space_when_known(workbench):
    policy = {"source": "step3_current_roi",
              "intensity_space": "corrected_zarr_native_float",
              "normalization": "none", "scope": "roi_preview",
              "preview_only": True}
    workbench.set_channel_images(_real_like_channels(), source="step3",
                                source_policy=policy)
    txt = workbench._status_lbl.text()
    assert "corrected_zarr_native_float" in txt
    assert "roi_preview" in txt


def test_step3_sync_passes_source_metadata(app):
    from block01.ui.step3_page import Step3Page
    page = Step3Page()
    # Map a couple of source strings to intensity spaces (no guessing).
    assert page._intensity_space_for_source("corrected_zarr roi_local") == (
        "corrected_zarr_native_float", "none")
    assert page._intensity_space_for_source("raw_ome global_bbox=[]") == (
        "raw_ome_normalized_0_1", "minmax_per_read")
    assert page._intensity_space_for_source("canonical_step3_dapi") == (
        "step3_dapi_normalized_0_1", "display_minmax")
    assert page._intensity_space_for_source("weird") == ("unknown", "unknown")
    # markers without step2_compatible -> preview fallback mode at top level
    meta = {
        "CD45": {"intensity_space": "corrected_zarr_native_float",
                 "normalization": "none"},
        "CK19": {"intensity_space": "raw_ome_normalized_0_1",
                 "normalization": "minmax_per_read"},
    }
    sp = page._build_conditioning_source_policy(meta)
    assert sp["intensity_space"] == "mixed_or_preview"
    assert sp["source_alignment_mode"] == "partial_or_preview_fallback"
    assert sp["preview_only"] is True
    assert sp["scope"] == "roi_preview"


# ── reference layer overlays (Phase 2.2) ────────────────────────────────────

def _mask_labels():
    m = np.zeros((48, 48), dtype=np.uint32)
    m[5:15, 5:15] = 1
    m[20:30, 22:38] = 2
    return m


def test_set_reference_layers_accepts_all(workbench):
    workbench.set_channel_images(_real_like_channels(), source="step3")
    dapi = np.random.default_rng(1).random((48, 48)).astype(np.float32)
    fusion = (np.random.default_rng(2).random((48, 48, 3)) * 255).astype(np.uint8)
    workbench.set_reference_layers(dapi=dapi, mask=_mask_labels(), fusion=fusion)
    avail = workbench.reference_layer_availability()
    assert avail == {"dapi": True, "mask": True, "fusion": True}


def test_missing_reference_layers_do_not_crash(workbench):
    workbench.set_channel_images(_real_like_channels(), source="step3")
    workbench.set_reference_layers(dapi=None, mask=None, fusion=None)
    assert workbench.reference_layer_availability() == {
        "dapi": False, "mask": False, "fusion": False}


def test_partial_reference_layers(workbench):
    workbench.set_channel_images(_real_like_channels(), source="step3")
    dapi = np.random.default_rng(3).random((48, 48)).astype(np.float32)
    workbench.set_reference_layers(dapi=dapi)  # mask/fusion absent
    avail = workbench.reference_layer_availability()
    assert avail["dapi"] is True
    assert avail["mask"] is False and avail["fusion"] is False
    # the DAPI control becomes enabled, mask/fusion stay disabled
    assert workbench._ref_chk["dapi"].isEnabled() is True
    assert workbench._ref_chk["mask"].isEnabled() is False


def test_reference_layers_not_saved_as_marker_channels(workbench, tmp_path):
    # Markers here are real markers only (no DAPI); reference layers are
    # supplied separately and must not appear in the saved channel list.
    rng = np.random.default_rng(7)
    markers = {
        "CD45": (rng.random((48, 48)) * 8000).astype(np.float32),
        "CK19": (rng.random((48, 48)) * 5000).astype(np.float32),
        "CD68": (rng.random((48, 48)) * 3000).astype(np.float32),
    }
    workbench.set_channel_images(markers, source="step3")
    workbench.set_reference_layers(
        dapi=np.ones((48, 48), np.float32), mask=_mask_labels(),
        fusion=np.ones((48, 48, 3), np.float32))
    cfg = workbench.build_config()
    channels = {c.lower() for c in cfg["channels"]}
    assert "dapi" not in channels
    assert "mask" not in channels
    assert "fusion" not in channels
    assert set(cfg["channels"].keys()) == {"CD45", "CK19", "CD68"}  # markers only
    # still saves cleanly with markers only
    path = os.path.join(str(tmp_path), "channel_remap_config.json")
    save_channel_remap_config(cfg, path)
    loaded = load_channel_remap_config(path)
    assert set(loaded["channels"].keys()) == set(cfg["channels"].keys())


def test_reference_visibility_toggle_no_crash(workbench):
    workbench.set_channel_images(_real_like_channels(), source="step3")
    workbench.set_reference_layers(
        dapi=np.ones((48, 48), np.float32), mask=_mask_labels())
    workbench._ref_chk["dapi"].setChecked(True)
    workbench._ref_chk["mask"].setChecked(True)
    workbench._ref_op["dapi"].setValue(30)
    txt = workbench._status_lbl.text()
    assert "DAPI ✓" in txt and "Mask ✓" in txt and "Fusion —" in txt


def test_clear_reference_layers(workbench):
    workbench.set_channel_images(_real_like_channels(), source="step3")
    workbench.set_reference_layers(dapi=np.ones((48, 48), np.float32))
    workbench.clear_reference_layers()
    assert workbench.reference_layer_availability() == {
        "dapi": False, "mask": False, "fusion": False}


def test_step3_sync_reference_layers_no_crash_when_missing(app):
    from block01.ui.step3_page import Step3Page
    page = Step3Page()
    # no patch loaded -> all reference layers None, sync must not crash
    refs = page._get_current_reference_layers_for_conditioning()
    assert set(refs.keys()) == {"dapi", "mask", "fusion"}
    page._sync_step3_to_workbench()
    assert page._channel_workbench.reference_layer_availability() == {
        "dapi": False, "mask": False, "fusion": False}


# ── viewer stability + reference display normalization (Phase 2.2b) ──────────

def test_display_normalize_handles_all_dtypes(app):
    from block01.ui.widgets.channel_viewer_canvas import display_normalize
    cases = [
        np.linspace(0, 1, 100).reshape(10, 10).astype(np.float32),       # 0–1
        (np.random.default_rng(1).random((10, 10)) * 255).astype(np.uint8),    # 0–255
        (np.random.default_rng(2).random((10, 10)) * 65535).astype(np.uint16),  # 0–65535
        np.full((6, 6), 7.0, np.float32),                                # constant
        np.array([[np.nan, np.inf], [1.0, 2.0]], np.float32),            # NaN/inf
        np.zeros((4, 4), np.float32),                                     # all-zero
    ]
    for arr in cases:
        out = display_normalize(arr)
        assert out.dtype == np.float32
        assert out.shape == arr.shape
        assert np.all(np.isfinite(out))
        assert float(out.min()) >= 0.0 and float(out.max()) <= 1.0


def test_canvas_preserves_view_on_param_change(app):
    from block01.ui.widgets.channel_viewer_canvas import ChannelViewerCanvas
    c = ChannelViewerCanvas()
    img = np.random.default_rng(0).random((32, 32)).astype(np.float32)
    c.set_images(remapped=img)          # first load -> fits, need_fit cleared
    assert c._need_fit is False
    assert c._prev_shape == (32, 32)
    # display-parameter change (same shape) must not request a refit
    c.set_images(remapped=img * 0.5)
    assert c._need_fit is False
    assert c._prev_shape == (32, 32)


def test_canvas_refits_on_shape_change(app):
    from block01.ui.widgets.channel_viewer_canvas import ChannelViewerCanvas
    c = ChannelViewerCanvas()
    c.set_images(remapped=np.zeros((16, 16), np.float32))
    assert c._prev_shape == (16, 16)
    c.set_images(remapped=np.zeros((24, 40), np.float32))
    assert c._prev_shape == (24, 40)
    assert c._need_fit is False  # fit consumed by the shape change


def test_canvas_request_fit_sets_flag(app):
    from block01.ui.widgets.channel_viewer_canvas import ChannelViewerCanvas
    c = ChannelViewerCanvas()
    c.set_images(remapped=np.zeros((16, 16), np.float32))
    assert c._need_fit is False
    c.request_fit()
    assert c._need_fit is True
    c.set_images(remapped=np.zeros((16, 16), np.float32))  # same shape, but forced
    assert c._need_fit is False  # consumed


def _aligned_policy():
    return {
        "source": "step3_current_roi",
        "intensity_space": "corrected_zarr_native_float",
        "normalization": "none",
        "scope": "roi_preview",
        "preview_only": True,
        "step2_pre_remap_source": "corrected_zarr_native_float",
        "calibration_source_matches_step2": True,
        "step2_ready": False,
    }


def test_status_shows_step2_source_match(workbench):
    markers = {"CD45": (np.random.default_rng(8).random((40, 40)) * 8000).astype(np.float32)}
    workbench.set_channel_images(markers, source="step3",
                                source_policy=_aligned_policy())
    txt = workbench._status_lbl.text()
    assert "corrected_zarr_native_float" in txt
    assert "Step2 match: yes" in txt
    assert "PREVIEW-ONLY" in txt


def test_status_shows_fallback_when_not_aligned(workbench):
    policy = dict(_aligned_policy())
    policy.update({"intensity_space": "raw_ome_normalized_0_1",
                   "calibration_source_matches_step2": False})
    markers = {"CD45": np.random.default_rng(9).random((40, 40)).astype(np.float32)}
    workbench.set_channel_images(markers, source="step3", source_policy=policy)
    txt = workbench._status_lbl.text()
    assert "Step2 match: no" in txt
    assert "fallback" in txt


def test_build_config_keeps_step2_ready_false(workbench):
    workbench.set_channel_images(_real_like_channels(), source="step3",
                                source_policy=_aligned_policy())
    sp = workbench.build_config()["source_policy"]
    assert sp["step2_ready"] is False
    assert sp["preview_only"] is True
    assert sp["calibration_source_matches_step2"] is True


def test_step3_build_policy_corrected_matches_but_not_ready(app):
    from block01.ui.step3_page import Step3Page
    page = Step3Page()
    meta = {"CD45": {"intensity_space": "corrected_zarr_native_float",
                     "step2_compatible": True},
            "CK19": {"intensity_space": "corrected_zarr_native_float",
                     "step2_compatible": True}}
    sp = page._build_conditioning_source_policy(meta)
    assert sp["calibration_source_matches_step2"] is True
    assert sp["step2_ready"] is False
    assert sp["preview_only"] is True


def test_step3_build_policy_mixed_sources_not_matching(app):
    from block01.ui.step3_page import Step3Page
    page = Step3Page()
    meta = {"CD45": {"intensity_space": "corrected_zarr_native_float",
                     "step2_compatible": True},
            "CK19": {"intensity_space": "raw_ome_normalized_0_1",
                     "step2_compatible": False}}
    sp = page._build_conditioning_source_policy(meta)
    assert sp["calibration_source_matches_step2"] is False
    assert sp["step2_ready"] is False


def test_step3_build_policy_raw_normalized_is_fallback(app):
    from block01.ui.step3_page import Step3Page
    page = Step3Page()
    meta = {"CD45": {"intensity_space": "raw_ome_normalized_0_1",
                     "step2_compatible": False}}
    sp = page._build_conditioning_source_policy(meta)
    assert sp["calibration_source_matches_step2"] is False
    assert sp["preview_only"] is True


def test_reference_layers_do_not_affect_marker_source_policy(app):
    # DAPI reference must not enter the marker source policy computation.
    from block01.ui.step3_page import Step3Page
    page = Step3Page()
    # only markers are passed to the policy builder; a DAPI reference is never
    # part of channel_metadata
    marker_meta = {"CD45": {"intensity_space": "corrected_zarr_native_float",
                            "step2_compatible": True}}
    sp = page._build_conditioning_source_policy(marker_meta)
    assert sp["intensity_space"] == "corrected_zarr_native_float"
    assert sp["calibration_source_matches_step2"] is True


# ── per-channel source alignment hardening (Phase 2.1d) ─────────────────────

def test_alignment_mode_single_native_source(app):
    from block01.ui.step3_page import Step3Page
    page = Step3Page()
    meta = {
        "CD45": {"intensity_space": "corrected_zarr_native_float",
                 "step2_compatible": True},
        "CK19": {"intensity_space": "corrected_zarr_native_float",
                 "step2_compatible": True},
    }
    sp = page._build_conditioning_source_policy(meta)
    assert sp["source_alignment_mode"] == "single_native_source"
    assert sp["intensity_space"] == "corrected_zarr_native_float"
    assert sp["calibration_source_matches_step2"] is True
    assert sp["step2_ready"] is False
    assert sp["preview_only"] is True


def test_alignment_mode_per_channel_native(app):
    from block01.ui.step3_page import Step3Page
    page = Step3Page()
    meta = {
        "CD45": {"intensity_space": "corrected_zarr_native_float",
                 "step2_compatible": True},
        "CK19": {"intensity_space": "raw_ome_native_float",
                 "step2_compatible": True},
    }
    sp = page._build_conditioning_source_policy(meta)
    assert sp["source_alignment_mode"] == "per_channel_native"
    assert sp["intensity_space"] == "mixed_native"
    assert sp["step2_pre_remap_source"] == "per_channel_native"
    assert sp["calibration_source_matches_step2"] is True
    assert sp["step2_ready"] is False
    assert sp["preview_only"] is True


def test_alignment_mode_partial_preview_fallback(app):
    from block01.ui.step3_page import Step3Page
    page = Step3Page()
    meta = {
        "CD45": {"intensity_space": "corrected_zarr_native_float",
                 "step2_compatible": True},
        "CK19": {"intensity_space": "raw_ome_normalized_0_1",
                 "step2_compatible": False},
    }
    sp = page._build_conditioning_source_policy(meta)
    assert sp["source_alignment_mode"] == "partial_or_preview_fallback"
    assert sp["intensity_space"] == "mixed_or_preview"
    assert sp["calibration_source_matches_step2"] is False
    assert sp["step2_ready"] is False
    assert sp["preview_only"] is True


def test_alignment_match_requires_all_channels_compatible(app):
    from block01.ui.step3_page import Step3Page
    page = Step3Page()
    # one incompatible channel flips the top-level match to False
    meta = {
        "A": {"intensity_space": "corrected_zarr_native_float",
              "step2_compatible": True},
        "B": {"intensity_space": "corrected_zarr_native_float",
              "step2_compatible": True},
        "C": {"intensity_space": "unknown", "step2_compatible": False},
    }
    sp = page._build_conditioning_source_policy(meta)
    assert sp["calibration_source_matches_step2"] is False
    assert sp["source_alignment_mode"] == "partial_or_preview_fallback"


def test_alignment_mode_none_when_no_markers(app):
    from block01.ui.step3_page import Step3Page
    page = Step3Page()
    sp = page._build_conditioning_source_policy({})
    assert sp["source_alignment_mode"] == "none"
    assert sp["calibration_source_matches_step2"] is False
    assert sp["step2_ready"] is False


def test_status_shows_per_channel_native_mix(workbench):
    policy = {
        "source": "step3_current_roi", "intensity_space": "mixed_native",
        "normalization": "none", "scope": "roi_preview", "preview_only": True,
        "step2_pre_remap_source": "per_channel_native",
        "calibration_source_matches_step2": True,
        "source_alignment_mode": "per_channel_native", "step2_ready": False,
    }
    workbench.set_channel_images(
        {"CD45": np.ones((16, 16), np.float32)}, source="step3",
        source_policy=policy)
    txt = workbench._status_lbl.text()
    assert "per-channel native mix" in txt
    assert "Step2 match: yes" in txt
    assert "PREVIEW-ONLY" in txt


def test_status_shows_preview_fallback(workbench):
    policy = {
        "source": "step3_current_roi", "intensity_space": "mixed_or_preview",
        "normalization": "mixed", "scope": "roi_preview", "preview_only": True,
        "step2_pre_remap_source": "per_channel_or_unknown",
        "calibration_source_matches_step2": False,
        "source_alignment_mode": "partial_or_preview_fallback",
        "step2_ready": False,
    }
    workbench.set_channel_images(
        {"CD45": np.ones((16, 16), np.float32)}, source="step3",
        source_policy=policy)
    txt = workbench._status_lbl.text()
    assert "preview fallback present" in txt
    assert "Step2 match: no" in txt
    assert "fallback" in txt


def test_saved_config_preserves_per_channel_source_metadata(workbench, tmp_path):
    markers = {"CD45": (np.random.default_rng(11).random((24, 24)) * 9000).astype(np.float32),
               "CK19": (np.random.default_rng(12).random((24, 24)) * 7000).astype(np.float32)}
    channel_meta = {
        "CD45": {"source": "corrected_zarr",
                 "intensity_space": "corrected_zarr_native_float",
                 "normalization": "none", "step2_compatible": True,
                 "step2_pre_remap_source": "corrected_zarr_native_float",
                 "calibration_source_matches_step2": True},
        "CK19": {"source": "raw_ome_native",
                 "intensity_space": "raw_ome_native_float",
                 "normalization": "none", "step2_compatible": True,
                 "step2_pre_remap_source": "raw_ome_native_float",
                 "calibration_source_matches_step2": True,
                 "fallback_reason": "channel_not_found_in_corrected_zarr"},
    }
    workbench.set_channel_images(markers, source="step3",
                                channel_metadata=channel_meta)
    path = os.path.join(str(tmp_path), "channel_remap_config.json")
    save_channel_remap_config(workbench.build_config(), path)
    loaded = load_channel_remap_config(path)
    cd45 = loaded["channels"]["CD45"]
    ck19 = loaded["channels"]["CK19"]
    assert cd45["step2_pre_remap_source"] == "corrected_zarr_native_float"
    assert cd45["step2_compatible"] is True
    assert ck19["intensity_space"] == "raw_ome_native_float"
    assert ck19["fallback_reason"] == "channel_not_found_in_corrected_zarr"


def test_reference_layers_do_not_change_alignment_mode(app):
    from block01.ui.step3_page import Step3Page
    page = Step3Page()
    # only marker metadata reaches the policy builder; references never do
    marker_meta = {
        "CD45": {"intensity_space": "corrected_zarr_native_float",
                 "step2_compatible": True},
    }
    sp = page._build_conditioning_source_policy(marker_meta)
    assert sp["source_alignment_mode"] == "single_native_source"
    assert sp["calibration_source_matches_step2"] is True


def test_reference_display_norm_does_not_touch_config(workbench, tmp_path):
    # A native-scale DAPI reference must not influence saved marker Min/Max.
    markers = {"CD45": (np.random.default_rng(5).random((40, 40)) * 8000).astype(np.float32)}
    workbench.set_channel_images(markers, source="step3")
    cfg_before = workbench.build_config()
    cd45_before = dict(cfg_before["channels"]["CD45"])
    workbench.set_reference_layers(
        dapi=(np.random.default_rng(6).random((40, 40)) * 65535).astype(np.uint16))
    cfg_after = workbench.build_config()
    assert cfg_after["channels"]["CD45"]["min"] == cd45_before["min"]
    assert cfg_after["channels"]["CD45"]["max"] == cd45_before["max"]
    assert set(c.lower() for c in cfg_after["channels"]) == {"cd45"}


# ── Phase 5c.1: DAPI reference default-on + Select all / Clear all (B1/B2/B4) ──

def test_dapi_reference_visible_by_default(workbench):
    # B1: when a DAPI reference layer is supplied it is shown by default so the
    # conditioning viewer has nuclei context (mask/fusion stay off).
    workbench.set_channel_images(_real_like_channels(), source="step3")
    workbench.set_reference_layers(
        dapi=np.ones((48, 48), np.float32), mask=_mask_labels(),
        fusion=np.ones((48, 48, 3), np.float32))
    assert workbench._ref_chk["dapi"].isChecked() is True
    assert workbench._ref_chk["mask"].isChecked() is False
    assert workbench._ref_chk["fusion"].isChecked() is False


def test_dapi_reference_not_a_marker_channel(workbench):
    # B1/B6: DAPI default-on as a reference must NOT add DAPI to config channels.
    markers = {
        "CD45": (np.random.default_rng(1).random((48, 48)) * 8000).astype(np.float32),
        "CK19": (np.random.default_rng(2).random((48, 48)) * 5000).astype(np.float32),
    }
    workbench.set_channel_images(markers, source="step3")
    workbench.set_reference_layers(dapi=np.ones((48, 48), np.float32))
    cfg = workbench.build_config()
    assert "dapi" not in {c.lower() for c in cfg["channels"]}
    assert set(cfg["channels"].keys()) == {"CD45", "CK19"}


# (#2 declutter) the Select all / Clear all marker buttons + handler were removed.
def test_select_clear_all_markers_buttons_removed(workbench):
    assert not hasattr(workbench, "_btn_select_all")
    assert not hasattr(workbench, "_btn_clear_all")
    assert not hasattr(workbench, "_set_all_markers_enabled")


def test_individual_channel_toggle_still_works(workbench):
    # channels remain individually toggleable via the per-channel Enabled control.
    workbench.set_channel_images(_real_like_channels(), source="step3")
    active = workbench._active
    workbench._chk_enabled.setChecked(False)
    assert workbench.build_config()["channels"][active]["enabled"] is False
    workbench._chk_enabled.setChecked(True)
    assert workbench.build_config()["channels"][active]["enabled"] is True


def test_channel_order_preserved_not_alphabetical(workbench):
    # B3: the workbench preserves the supplied (panel/OME) order, not sorted().
    ordered = {}
    rng = np.random.default_rng(9)
    for name in ("CK19", "CD68", "CD3D", "CD163", "AFP"):
        ordered[name] = (rng.random((32, 32)) * 4000).astype(np.float32)
    workbench.set_channel_images(ordered, source="step3")
    assert workbench._names == ["CK19", "CD68", "CD3D", "CD163", "AFP"]
    cfg = workbench.build_config()
    assert list(cfg["channels"].keys()) == ["CK19", "CD68", "CD3D", "CD163", "AFP"]


# ── Phase 5f-a: Channel Conditioning moved to Step1.5 ────────────────────────

class _FakeLoader:
    """Minimal OMETIFFLoader stand-in for Step1.5 conditioning tests."""

    def __init__(self, names, filepath=None, shape=None):
        self._names = list(names)
        if filepath is not None:
            self.filepath = filepath
        if shape is not None:
            self.shape = shape

    def channel_names(self):
        return list(self._names)

    def read_region(self, ch, y0, y1, x0, x1, downsample=1,
                    correction_config=None, normalize=True):
        rng = np.random.default_rng(abs(hash(ch)) % 10000)
        a = rng.random((y1 - y0, x1 - x0)).astype(np.float32)
        return a if normalize else (a * 1000.0).astype(np.float32)


def _make_step15(app, tmp_path, names=("DAPI", "CD45", "CK19", "CD68"),
                 filepath=None, shape=None):
    from block01.ui.step1_5_bg_page import Step15BackgroundCorrectionPage
    page = Step15BackgroundCorrectionPage()
    loader = _FakeLoader(names, filepath=filepath, shape=shape)
    patches = [(0, 32, 0, 32), (0, 32, 32, 64)]
    page.set_context(loader, str(tmp_path), patches, "DAPI")
    return page


def test_step15_exposes_channel_conditioning_tab(app, tmp_path):
    page = _make_step15(app, tmp_path)
    assert hasattr(page, "_cond_workbench")
    titles = [page._s15_tabs.tabText(i) for i in range(page._s15_tabs.count())]
    assert "Background Correction" in titles
    assert any("Channel Conditioning" in t for t in titles)


def test_step15_background_correction_still_present(app, tmp_path):
    # Part F: existing background-correction widgets/methods are not removed.
    page = _make_step15(app, tmp_path)
    assert hasattr(page, "_tophat_slider") and hasattr(page, "_cucim_slider")
    assert hasattr(page, "_show_current_channel")
    assert hasattr(page, "_apply_current_channel_decision")


def test_step15_workbench_mirrors_channel_order_excluding_dapi(app, tmp_path):
    page = _make_step15(app, tmp_path, names=("DAPI", "CD45", "CK19", "CD68"))
    page._sync_step15_to_workbench()
    # markers follow Step1.5 channel order, DAPI excluded (reference only)
    assert page._cond_workbench._names == ["CD45", "CK19", "CD68"]
    avail = page._cond_workbench.reference_layer_availability()
    assert avail["dapi"] is True and avail["mask"] is False and avail["fusion"] is False


def test_step15_dapi_not_a_marker_in_saved_config(app, tmp_path):
    page = _make_step15(app, tmp_path)
    page._sync_step15_to_workbench()
    cfg = page._cond_workbench.build_config()
    assert "dapi" not in {c.lower() for c in cfg["channels"]}
    assert set(cfg["channels"].keys()) == {"CD45", "CK19", "CD68"}


def test_step15_saves_preview_config(app, tmp_path, monkeypatch):
    from block01.utils.channel_remap_config import load_channel_remap_config
    page = _make_step15(app, tmp_path)
    page._sync_step15_to_workbench()
    out = os.path.join(str(tmp_path), "channel_remap_configs", "cfg.json")
    monkeypatch.setattr(
        "block01.ui.step1_5_bg_page.QFileDialog.getSaveFileName",
        lambda *a, **k: (out, "JSON (*.json)"))
    monkeypatch.setattr(
        "block01.ui.step1_5_bg_page.QMessageBox.information",
        lambda *a, **k: None)
    page._save_step15_remap_config()
    assert os.path.isfile(out)
    loaded = load_channel_remap_config(out)
    assert loaded["created_from_step"] == "step1_5_channel_conditioning"
    assert loaded["used_for"] == "segmentation_only"
    sp = loaded["source_policy"]
    assert sp["preview_only"] is True
    assert sp["step2_ready"] is False
    assert sp["scope"] == "step1_5_pre_segmentation"
    assert "dapi" not in {c.lower() for c in loaded["channels"]}


def test_step15_config_saved_under_channel_remap_configs_dir(app, tmp_path, monkeypatch):
    page = _make_step15(app, tmp_path)
    page._sync_step15_to_workbench()
    captured = {}

    def _fake_dialog(self, title, default, flt):
        captured["default"] = default
        return ("", "")  # user cancels -> nothing saved

    monkeypatch.setattr(
        "block01.ui.step1_5_bg_page.QFileDialog.getSaveFileName", _fake_dialog)
    page._save_step15_remap_config()
    assert os.path.join("channel_remap_configs") in captured["default"]
    assert captured["default"].endswith(".json")


def test_step15_workbench_follows_patch_switch(app, tmp_path):
    page = _make_step15(app, tmp_path)
    page._sync_step15_to_workbench()
    assert page._cond_workbench._context.get("patch") == 1
    page._select_patch(1)  # switch to second patch
    assert page._cond_workbench._context.get("patch") == 2


def test_step15_no_loader_clears_workbench(app, tmp_path):
    from block01.ui.step1_5_bg_page import Step15BackgroundCorrectionPage
    page = Step15BackgroundCorrectionPage()
    page._sync_step15_to_workbench()  # no loader/patches -> graceful empty
    assert page._cond_workbench.has_channel_data() is False


def test_step3_workbench_tab_reframed_as_review(app):
    from block01.ui.step3_page import Step3Page
    page = Step3Page()
    # Step3 keeps the workbench but it is now a review/QC surface.
    assert hasattr(page, "_channel_workbench")
    idx = getattr(page, "_cond_tab_index", -1)
    assert idx >= 0


# ── Phase 5f-a.1: honest Step1.5 metadata + host-action cleanup ────────────

def _step15_saved_config(app, tmp_path, monkeypatch):
    """Save a Step1.5 conditioning config and return the loaded dict."""
    from block01.utils.channel_remap_config import load_channel_remap_config
    page = _make_step15(app, tmp_path)
    page._sync_step15_to_workbench()
    out = os.path.join(str(tmp_path), "channel_remap_configs", "cfg.json")
    monkeypatch.setattr(
        "block01.ui.step1_5_bg_page.QFileDialog.getSaveFileName",
        lambda *a, **k: (out, "JSON (*.json)"))
    monkeypatch.setattr(
        "block01.ui.step1_5_bg_page.QMessageBox.information", lambda *a, **k: None)
    page._save_step15_remap_config()
    return load_channel_remap_config(out)


def test_step15_source_policy_is_honest_unverified(app, tmp_path, monkeypatch):
    cfg = _step15_saved_config(app, tmp_path, monkeypatch)
    sp = cfg["source_policy"]
    assert cfg["created_from_step"] == "step1_5_channel_conditioning"
    assert sp["preview_only"] is True
    assert sp["step2_ready"] is False
    assert sp["calibration_source_matches_step2"] is False
    assert sp["source_alignment_mode"] == "partial_or_preview_fallback"
    assert sp["step2_pre_remap_source"] == "unknown"


def test_step15_per_channel_metadata_is_honest(app, tmp_path, monkeypatch):
    cfg = _step15_saved_config(app, tmp_path, monkeypatch)
    assert cfg["channels"]
    for name, params in cfg["channels"].items():
        assert params["step2_compatible"] is False, name
        assert params["calibration_source_matches_step2"] is False, name
        assert params["step2_pre_remap_source"] == "unknown", name


def test_step15_config_passes_basic_but_rejected_by_step2(app, tmp_path, monkeypatch):
    from block01.utils.channel_remap_config import (
        validate_channel_remap_config, validate_step2_remap_config)
    cfg = _step15_saved_config(app, tmp_path, monkeypatch)
    # Well-formed: the basic validator accepts it (not malformed JSON).
    assert validate_channel_remap_config(cfg) == []
    # Step2 runtime validation rejects it even with allow_preview_remap=True,
    # and the rejection is about unverified source alignment, not structure.
    errors, resolved = validate_step2_remap_config(cfg, allow_preview_remap=True)
    assert errors and resolved == {}
    blob = " ".join(errors).lower()
    assert ("calibration_source_matches_step2" in blob
            or "step2_pre_remap_source" in blob
            or "source_alignment_mode" in blob)
    assert "step2_compatible is false" in blob


def test_step15_workbench_has_no_step3_button_text(app, tmp_path):
    from PyQt5 import QtWidgets
    page = _make_step15(app, tmp_path)
    wb = page._cond_workbench
    labels = [b.text() for b in wb.findChildren(QtWidgets.QPushButton)]
    assert not any("Step3" in t for t in labels), labels
    assert "Load current patch channels" in labels


def test_step15_hides_generic_internal_save(app, tmp_path):
    page = _make_step15(app, tmp_path)
    wb = page._cond_workbench
    # generic internal save hidden; Step1.5's own save button is the official path
    assert wb._btn_save_internal.isHidden() is True


def test_step3_workbench_keeps_default_host_actions(app):
    from block01.ui.step3_page import Step3Page
    from PyQt5 import QtWidgets
    page = Step3Page()
    wb = page._channel_workbench
    labels = [b.text() for b in wb.findChildren(QtWidgets.QPushButton)]
    assert "Load current Step3 ROI" in labels
    assert "Save remap config…" in labels
    assert wb._btn_save_internal.isHidden() is False


# ── Phase 2.1c-b Part A: Step1.5 records calibration source identity ─────────

def _step15_saved_config_loader(app, tmp_path, monkeypatch, filepath, shape):
    """Save a Step1.5 config from a loader with a known filepath/shape."""
    from block01.utils.channel_remap_config import load_channel_remap_config
    page = _make_step15(app, tmp_path, filepath=filepath, shape=shape)
    page._sync_step15_to_workbench()
    out = os.path.join(str(tmp_path), "channel_remap_configs", "cfg.json")
    monkeypatch.setattr(
        "block01.ui.step1_5_bg_page.QFileDialog.getSaveFileName",
        lambda *a, **k: (out, "JSON (*.json)"))
    monkeypatch.setattr(
        "block01.ui.step1_5_bg_page.QMessageBox.information", lambda *a, **k: None)
    page._save_step15_remap_config()
    return load_channel_remap_config(out)


def test_step15_records_calibration_identity(app, tmp_path, monkeypatch):
    src = str(tmp_path / "raw.ome.tiff")
    cfg = _step15_saved_config_loader(app, tmp_path, monkeypatch, src, (4148, 2786))
    sp = cfg["source_policy"]
    assert sp["calibration_source_path"] == os.path.abspath(src)
    assert sp["calibration_source_kind"] == "raw_ome"
    assert sp["calibration_source_shape"] == [4148, 2786]  # FULL source [H, W], not patch
    assert sp["calibration_intensity_space"] == "raw_ome_native_float"
    assert sp["calibration_patch_bbox"] == [0, 32, 0, 32]
    assert sp["calibration_patch_index"] == 0


def test_step15_calibration_does_not_flip_preview_or_ready(app, tmp_path, monkeypatch):
    src = str(tmp_path / "raw.ome.tiff")
    cfg = _step15_saved_config_loader(app, tmp_path, monkeypatch, src, (4148, 2786))
    sp = cfg["source_policy"]
    assert sp["preview_only"] is True
    assert sp["step2_ready"] is False
    assert sp["calibration_source_matches_step2"] is False


def test_step15_calibration_shape_null_when_loader_cannot_provide(app, tmp_path, monkeypatch):
    # loader without filepath/shape -> identity recorded as null, stays preview-only
    cfg = _step15_saved_config(app, tmp_path, monkeypatch)
    sp = cfg["source_policy"]
    assert sp["calibration_source_shape"] is None
    assert sp["calibration_source_path"] is None
    assert sp["preview_only"] is True
    assert sp["step2_ready"] is False


# ── step0-fix-min-nonneg: intensity Min can never be negative (structural) ───
def test_min_widget_is_structurally_non_negative(workbench):
    # 1. the Min input is bounded at 0 (cannot represent a negative).
    assert workbench._sp_min.minimum() == 0.0
    # Max bound is unchanged (Max may still be whatever it is).
    assert workbench._sp_max.minimum() == -1e9


def test_min_setvalue_below_zero_is_clamped_not_stored_negative(workbench):
    workbench.set_channel_images(_real_like_channels(), source="step3")
    # 2. setting Min below 0 (widget API) is clamped at the INPUT, not post-hoc.
    workbench._sp_min.setValue(-50.0)
    assert workbench._sp_min.value() == 0.0
    assert workbench._params[workbench._active]["min"] >= 0.0
    # a normal positive value is unaffected
    workbench._sp_min.setValue(123.4)
    assert workbench._sp_min.value() == 123.4


def test_auto_minmax_never_sets_negative_min(workbench):
    workbench.set_channel_images(_real_like_channels(), source="step3")
    # 2 (programmatic setter): auto-minmax respects >= 0.
    workbench._on_auto()
    assert workbench._sp_min.value() >= 0.0


def test_saved_config_has_no_negative_channel_min(workbench, tmp_path):
    workbench.set_channel_images(_real_like_channels(), source="step3")
    workbench._sp_min.setValue(-9999.0)   # try to force a negative
    cfg = workbench.build_config()
    for name, params in cfg["channels"].items():
        assert params.get("min") is None or params["min"] >= 0.0
    # round-trips through the on-disk schema without a negative Min
    path = str(tmp_path / "cfg.json")
    save_channel_remap_config(cfg, path)


def test_max_and_gamma_unchanged(workbench):
    workbench.set_channel_images(_real_like_channels(), source="step3")
    # Max still accepts its full range (including negative / large).
    workbench._sp_max.setValue(4000.0)
    assert workbench._sp_max.value() == 4000.0
    workbench._sp_max.setValue(-7.0)
    assert workbench._sp_max.value() == -7.0
    # Gamma slider range unchanged.
    assert workbench._sl_gamma.minimum() == 10
    assert workbench._sl_gamma.maximum() == 300


# ── step0-fix-histogram-min-nonneg: histogram Min handle cannot drag below 0 ──
def test_histogram_min_handle_lower_bound_is_zero(workbench):
    # 2. the Min handle (pyqtgraph InfiniteLine) has a structural lower bound of 0;
    #    the Max handle is NOT lower-bounded.
    assert workbench._histogram._min_line.maxRange[0] == 0
    assert workbench._histogram._max_line.maxRange[0] is None


def test_histogram_min_handle_stops_at_zero_on_negative_drag(workbench):
    h = workbench._histogram
    # 1. simulate dragging the Min handle toward negative x: the HANDLE itself
    #    stops at 0 (position clamped), not just the value snapping back.
    h._min_line.setValue(-50.0)
    assert h._min_line.value() == 0.0          # handle position bounded at 0
    assert h.window()[0] == 0.0                # reported Min is 0
    # 3. a normal positive drag still works.
    h._min_line.setValue(123.4)
    assert h._min_line.value() == 123.4


def test_histogram_min_drag_negative_yields_nonneg_saved_config(workbench):
    workbench.set_channel_images(_real_like_channels(), source="step3")
    h = workbench._histogram
    h._min_line.setValue(-99.0)                # drag handle into negative -> 0
    workbench._on_histogram_window(*h.window())  # push through the workbench path
    cfg = workbench.build_config()
    for name, params in cfg["channels"].items():
        assert params.get("min") is None or params["min"] >= 0.0


def test_histogram_max_handle_unchanged_accepts_negative(workbench):
    h = workbench._histogram
    # 5. Max handle behavior unchanged: still lower-unbounded.
    h._max_line.setValue(-7.0)
    assert h._max_line.value() == -7.0
    h._max_line.setValue(5000.0)
    assert h._max_line.value() == 5000.0


# ── step0-conditioning-declutter #5: Show-remapped / Split toggles removed ───
def test_show_remapped_and_split_toggles_removed(workbench):
    assert not hasattr(workbench, "_chk_remapped")
    assert not hasattr(workbench, "_chk_split")


def test_viewer_shows_conditioned_channel_not_blank(workbench):
    # fixed default after removing "Show remapped": always show the conditioned
    # (remapped) channel — the viewer is never blank once a channel is active.
    workbench.set_channel_images(_real_like_channels(), source="step3")
    assert workbench._active is not None
    assert workbench._canvas._img_item.image is not None


def test_is_split_view_kept_and_false_after_toggle_removed(workbench):
    # is_split_view() stays for v14.2c viewport sync; no UI path enters split now.
    assert hasattr(workbench, "is_split_view")
    assert workbench.is_split_view() is False


# ── step0-channel-list-cleanup: host-optional reference bar + Enabled checkbox ─
def test_workbench_defaults_keep_reference_and_enabled(app):
    """Default construction (Step1.5 / Step3) keeps both surfaces."""
    from block01.ui.widgets.channel_workbench import ChannelWorkbench
    wb = ChannelWorkbench()
    assert hasattr(wb, "_chk_enabled")
    assert set(wb._ref_chk.keys()) == {"dapi", "mask", "fusion"}


def test_workbench_flags_off_remove_reference_and_enabled(app):
    """Step0 construction turns both off: no Enabled checkbox, no reference UI."""
    from block01.ui.widgets.channel_workbench import ChannelWorkbench
    wb = ChannelWorkbench(show_reference_bar=False, show_enabled_checkbox=False)
    assert not hasattr(wb, "_chk_enabled")
    assert wb._ref_chk == {} and wb._ref_op == {}
    # set_reference_layers is a harmless no-op (no overlay path)
    wb.set_reference_layers(dapi=np.ones((16, 16), np.float32))
    assert not any(wb.reference_layer_availability().values())


# ── step0-qupath-multichannel-overlay: workbench interaction ─────────────────
def _mc_workbench(app, names=("DAPI", "CD68", "CK19"), visible=("DAPI",)):
    """A multichannel-overlay workbench preloaded with small channel images."""
    from block01.ui.widgets.channel_workbench import ChannelWorkbench
    wb = ChannelWorkbench(show_reference_bar=False, show_enabled_checkbox=False,
                          multichannel_overlay=True)
    rng = np.random.default_rng(0)
    imgs = {n: rng.random((16, 16)).astype(np.float32) for n in names}
    colors = {"DAPI": "#3366ff"}
    wb.set_channel_images(imgs, colors=colors, active="DAPI",
                          visible=list(visible))
    return wb


def test_overlay_default_state_dapi_only(app):
    wb = _mc_workbench(app)
    assert wb.visible_channels() == ["DAPI"]      # only DAPI checked
    assert wb.active_channel() == "DAPI"
    assert wb._colors["DAPI"] == "#3366ff"        # blue
    comp = wb._canvas._composite
    assert comp is not None and comp.shape == (16, 16, 3)
    # blue-dominant (DAPI tinted blue)
    assert comp[..., 2].mean() >= comp[..., 0].mean()
    assert comp[..., 2].mean() >= comp[..., 1].mean()


def test_overlay_click_row_checks_and_activates(app):
    wb = _mc_workbench(app)
    before = wb._canvas._composite.copy()
    wb._on_active_changed("CD68")                 # click an unchecked row
    assert "CD68" in wb.visible_channels()        # auto-checked
    assert wb.active_channel() == "CD68"          # and active
    assert not np.allclose(before, wb._canvas._composite)  # overlay changed


def test_overlay_checkbox_toggle_on_activates(app):
    wb = _mc_workbench(app)
    wb._on_visibility_changed("CK19", True)
    assert "CK19" in wb.visible_channels()
    assert wb.active_channel() == "CK19"


def test_overlay_uncheck_migrates_active(app):
    wb = _mc_workbench(app)
    wb._on_visibility_changed("CD68", True)       # DAPI + CD68 visible, active CD68
    wb._on_visibility_changed("CK19", True)       # + CK19, active CK19
    wb._on_visibility_changed("CK19", False)      # uncheck active CK19
    assert "CK19" not in wb.visible_channels()
    assert wb.active_channel() in ("DAPI", "CD68")  # migrated to a checked one


def test_overlay_uncheck_last_clears_view(app):
    wb = _mc_workbench(app)                        # only DAPI visible/active
    wb._on_visibility_changed("DAPI", False)
    assert wb.visible_channels() == []
    assert wb.active_channel() is None
    assert wb._canvas._composite is None          # viewer empty/black


def test_overlay_multichannel_composite_changes(app):
    wb = _mc_workbench(app)
    wb._on_visibility_changed("CD68", True)
    wb._on_visibility_changed("CK19", True)
    three = wb._canvas._composite.copy()
    assert three.shape == (16, 16, 3)
    assert float(three.min()) >= 0.0 and float(three.max()) <= 1.0
    wb._on_visibility_changed("CK19", False)      # remove one -> composite differs
    assert not np.allclose(three, wb._canvas._composite)


def test_overlay_color_change_updates_composite(app, monkeypatch):
    from PyQt5.QtGui import QColor
    from PyQt5 import QtWidgets
    wb = _mc_workbench(app)
    before = wb._canvas._composite.copy()
    monkeypatch.setattr(QtWidgets.QColorDialog, "getColor",
                        staticmethod(lambda *a, **k: QColor("#ff0000")))
    wb._on_color_clicked("DAPI")                  # swatch click -> dialog
    assert wb._colors["DAPI"] == "#ff0000"
    assert not np.allclose(before, wb._canvas._composite)


def test_overlay_minmax_change_updates_composite(app):
    wb = _mc_workbench(app)                        # active DAPI
    before = wb._canvas._composite.copy()
    wb._sp_min.setValue(0.1)
    wb._sp_max.setValue(0.4)
    wb._on_minmax_changed()
    assert not np.allclose(before, wb._canvas._composite)


def test_overlay_lazy_load_on_check(app):
    from block01.ui.widgets.channel_workbench import ChannelWorkbench
    wb = ChannelWorkbench(show_reference_bar=False, show_enabled_checkbox=False,
                          multichannel_overlay=True)
    calls = []
    pool = {n: np.random.rand(8, 8).astype(np.float32)
            for n in ("DAPI", "CD68")}

    def provider(name):
        calls.append(name)
        return pool[name]

    wb.set_pixel_provider(provider)
    # DAPI eager (real array), CD68 lazy (None placeholder)
    wb.set_channel_images({"DAPI": pool["DAPI"], "CD68": None},
                          colors={"DAPI": "#3366ff"}, active="DAPI",
                          visible=["DAPI"])
    calls.clear()
    wb._on_visibility_changed("CD68", True)       # check unloaded channel
    assert "CD68" in calls                        # lazy load fired
    assert wb._raw.get("CD68") is not None


def test_overlay_swatch_color_clicked_signal(app, monkeypatch):
    """The color swatch is clickable: the row emits color_clicked(name)."""
    from PyQt5 import QtWidgets
    from PyQt5.QtGui import QColor
    # the workbench is already wired to open a real QColorDialog on this signal;
    # stub it (invalid color = "cancelled") so the click stays non-modal.
    monkeypatch.setattr(QtWidgets.QColorDialog, "getColor",
                        staticmethod(lambda *a, **k: QColor()))
    wb = _mc_workbench(app)
    got = []
    wb._layer_list.color_clicked.connect(got.append)
    wb._layer_list._rows["DAPI"]._on_swatch_clicked(None)
    assert got == ["DAPI"]


def test_overlay_build_config_unchanged(app):
    wb = _mc_workbench(app)
    cfg = wb.build_config()
    # all channels present with full params regardless of visibility/overlay
    assert set(cfg["channels"]) == {"DAPI", "CD68", "CK19"}
    for c in cfg["channels"].values():
        for key in ("min", "max", "gamma", "brightness", "contrast", "enabled"):
            assert key in c


def test_single_channel_hosts_unaffected_by_overlay(app):
    """Default (Step3/Step1.5) workbench keeps single-channel set_images path."""
    from block01.ui.widgets.channel_workbench import ChannelWorkbench
    wb = ChannelWorkbench()
    assert wb._multichannel_overlay is False
    rng = np.random.default_rng(1)
    wb.set_channel_images({"A": rng.random((8, 8)).astype(np.float32),
                           "B": rng.random((8, 8)).astype(np.float32)})
    # single-channel path: composite stays None, uses raw/remapped
    assert wb._canvas._composite is None


# ── step0-conditioning-cleanup-and-all-toggle (#2 load btns / #3 All) ────────
def test_load_buttons_hidden_when_show_load_buttons_false(app):
    from block01.ui.widgets.channel_workbench import ChannelWorkbench
    wb = ChannelWorkbench(multichannel_overlay=True)
    wb.configure_host_actions(show_load_buttons=False)
    assert wb._btn_host_refresh.isHidden()        # "Load current ... channels"
    assert wb._btn_demo.isHidden()                # "Load demo patch"
    assert wb._btn_file.isHidden()                # "Load preview image…"


def test_load_buttons_present_by_default(app):
    from block01.ui.widgets.channel_workbench import ChannelWorkbench
    wb = ChannelWorkbench()                        # Step3 / Step1.5 default host
    assert not wb._btn_host_refresh.isHidden()
    assert not wb._btn_demo.isHidden()
    assert not wb._btn_file.isHidden()


def test_all_toggle_exists_only_in_multichannel(app):
    from block01.ui.widgets.channel_workbench import ChannelWorkbench
    assert hasattr(ChannelWorkbench(multichannel_overlay=True), "_chk_all")
    assert not hasattr(ChannelWorkbench(), "_chk_all")


def test_all_toggle_default_partial_with_dapi_only(app):
    from PyQt5.QtCore import Qt
    wb = _mc_workbench(app)                         # only DAPI visible
    assert wb._chk_all.checkState() == Qt.PartiallyChecked


def test_all_toggle_check_shows_all(app):
    from PyQt5.QtCore import Qt
    wb = _mc_workbench(app, names=("DAPI", "CD68", "CK19"))
    wb._on_all_toggled(True)
    assert wb.visible_channels() == ["DAPI", "CD68", "CK19"]
    assert wb._chk_all.checkState() == Qt.Checked
    assert wb._canvas._composite is not None       # overlay shows everything


def test_all_toggle_uncheck_clears(app):
    from PyQt5.QtCore import Qt
    wb = _mc_workbench(app)
    wb._on_all_toggled(True)
    wb._on_all_toggled(False)
    assert wb.visible_channels() == []
    assert wb._chk_all.checkState() == Qt.Unchecked
    assert wb._canvas._composite is None           # empty/black


def test_all_toggle_reflects_individual_toggles(app):
    from PyQt5.QtCore import Qt
    wb = _mc_workbench(app, names=("DAPI", "CD68", "CK19"))
    for n in ("DAPI", "CD68", "CK19"):
        wb._on_visibility_changed(n, True)
    assert wb._chk_all.checkState() == Qt.Checked   # all checked
    wb._on_visibility_changed("CD68", False)
    assert wb._chk_all.checkState() == Qt.PartiallyChecked  # mixed
    for n in ("DAPI", "CK19"):
        wb._on_visibility_changed(n, False)
    assert wb._chk_all.checkState() == Qt.Unchecked  # none


def test_all_toggle_does_not_change_build_config(app):
    wb = _mc_workbench(app, names=("DAPI", "CD68", "CK19"))
    before = wb.build_config()
    wb._on_all_toggled(True)
    wb._on_all_toggled(False)
    after = wb.build_config()
    assert set(before["channels"]) == set(after["channels"]) == {"DAPI", "CD68", "CK19"}


# ── step0-all-toggle-perf: progressive overlay loading ───────────────────────
def _mc_lazy_workbench(app, n=28):
    """Multichannel workbench with a counting provider; only DAPI pre-loaded,
    the rest are lazy placeholders (None)."""
    from block01.ui.widgets.channel_workbench import ChannelWorkbench
    wb = ChannelWorkbench(show_reference_bar=False, show_enabled_checkbox=False,
                          multichannel_overlay=True)
    names = ["DAPI"] + [f"M{i}" for i in range(n - 1)]
    pool = {nm: np.random.rand(12, 12).astype(np.float32) for nm in names}
    calls = []

    def provider(name):
        calls.append(name)
        return pool[name]

    wb.set_pixel_provider(provider)
    imgs = {nm: (pool[nm] if nm == "DAPI" else None) for nm in names}
    wb.set_channel_images(imgs, colors={"DAPI": "#3366ff"}, active="DAPI",
                          visible=["DAPI"])
    calls.clear()
    return wb, names, calls


def _loaded_count(wb):
    return sum(1 for n in wb._names if wb._raw.get(n) is not None)


def test_all_toggle_does_not_block_load_all(app):
    wb, names, calls = _mc_lazy_workbench(app, n=28)
    wb._on_all_toggled(True)
    # immediately after: NO synchronous mass-load — only the already-loaded DAPI
    assert calls == []                              # no read_region in the call stack
    assert _loaded_count(wb) < 28                   # not all 28 loaded synchronously
    assert wb._progressive_timer is not None        # progressive loader scheduled
    assert wb._canvas._composite is not None        # shows what's loaded now


def test_progressive_ticks_load_all(app):
    wb, names, calls = _mc_lazy_workbench(app, n=28)
    wb._on_all_toggled(True)
    for _ in range(60):                             # advance the timer manually
        if wb._progressive_timer is None:
            break
        wb._on_progressive_tick()
    assert _loaded_count(wb) == 28                  # every visible channel loaded
    assert wb._progressive_timer is None            # timer stopped when done
    # each tick reads exactly one channel -> 27 lazy reads total
    assert len(calls) == 27


def test_uncheck_all_stops_progressive_timer(app):
    wb, names, calls = _mc_lazy_workbench(app, n=28)
    wb._on_all_toggled(True)
    assert wb._progressive_timer is not None
    wb._on_progressive_tick()                       # load one
    wb._on_all_toggled(False)                       # user unchecks mid-load
    assert wb._progressive_timer is None            # timer stopped
    loaded_after = _loaded_count(wb)
    # ticking again does nothing (no visible channels pending)
    wb._on_progressive_tick()
    assert _loaded_count(wb) == loaded_after


def test_single_channel_click_is_instant(app):
    wb, names, calls = _mc_lazy_workbench(app, n=28)
    # click one lazy channel: exactly one read, no progressive timer
    wb._on_active_changed("M5")
    assert calls == ["M5"]
    assert wb._progressive_timer is None


def test_progressive_load_build_config_unchanged(app):
    wb, names, calls = _mc_lazy_workbench(app, n=6)
    before = set(wb.build_config()["channels"])
    wb._on_all_toggled(True)
    for _ in range(20):
        if wb._progressive_timer is None:
            break
        wb._on_progressive_tick()
    assert set(wb.build_config()["channels"]) == before == set(names)
