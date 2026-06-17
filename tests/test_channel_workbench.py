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
    # mixed channel spaces collapse to "mixed" at the top level
    meta = {
        "CD45": {"intensity_space": "corrected_zarr_native_float",
                 "normalization": "none"},
        "DAPI": {"intensity_space": "step3_dapi_normalized_0_1",
                 "normalization": "display_minmax"},
    }
    sp = page._build_conditioning_source_policy(meta)
    assert sp["intensity_space"] == "mixed"
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
