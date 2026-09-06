"""Global correction defaults, local overrides, and Save artifact identity.

The global Method Parameters are defaults, not a second unrelated pair of
numbers.  Each Per-Channel Decision field inherits its matching global value
until that field is edited locally.  Save compares the exact same effective
value that the production worker stamps onto ``corrected_channels.zarr``;
display/Intensity changes are outside that identity.
"""

import gc
import json
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtCore, QtWidgets  # noqa: E402

from block01.core.bg_correction import (  # noqa: E402
    BG_CORRECTION_ALGO_VERSION,
    CHANNEL_PARAM_OVERRIDES_SCHEMA,
)
from block01.ui.step0 import step0_page as sp  # noqa: E402
from block01.ui.step0.search_ctrl import (  # noqa: E402
    WsiCorrectionWorker,
    read_corrected_zarr_state,
)


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


_LIVE_PAGES = []


@pytest.fixture(autouse=True)
def _tear_pages_down_before_the_global_event_flush():
    yield
    pages, _LIVE_PAGES[:] = list(_LIVE_PAGES), []
    for page in pages:
        try:
            page.teardown()
            page.close()
            page.deleteLater()
        except RuntimeError:
            pass
    gc.collect()


class _Loader:
    shape = (24, 28)
    filepath = "/synthetic.ome.tif"
    ch_map = {"DAPI": 0, "CD3": 1, "CD20": 2}

    def channel_names(self):
        return list(self.ch_map)

    def _read_roi_zarr(self, index, y0, y1, x0, x1):
        yy, xx = np.mgrid[y0:y1, x0:x1]
        return (yy * 3 + xx * 2 + index * 11).astype(np.float32)

    def set_correction_config(self, config):
        self.correction_config = config

    def set_corrected_zarr_store(self, path, decisions):
        self.corrected_path = path
        self.corrected_decisions = dict(decisions or {})


def _page(app):
    page = sp.Step0Page()
    _LIVE_PAGES.append(page)
    page.loader = _Loader()
    page.ome_path = page.loader.filepath
    page.nucleus_channel = "DAPI"
    page.patches = [(0, 12, 0, 14)]
    page.current_patch_idx = 0
    page._rebuild_channel_list()
    page.current_channel = "CD3"
    page._update_decision_ui()
    # Parameter inheritance tests exercise state, not tiled I/O.
    page._sync_compare_params = lambda method=None: None
    page._sync_full_image_param = lambda method=None: None
    return page


def _set_global(page, radius, sigma):
    page._tophat_slider.setValue(radius)
    page._cucim_slider.setValue(sigma)


def test_an_untuned_channel_and_its_visible_boxes_follow_the_global_defaults(app):
    page = _page(app)
    _set_global(page, 35, 61)

    assert page._channel_params == {}
    assert page._resolve_channel_params("CD3") == (35, 61)
    assert (page._dec_radius.value(), page._dec_sigma.value()) == (35, 61)
    info = page.preview_source_provider.describe("CD3")["correction"]
    assert info["effective_tophat_radius"] == 35
    assert info["effective_cucim_sigma"] == 61


def test_each_local_field_overrides_only_itself_and_never_changes_the_globals(app):
    page = _page(app)
    _set_global(page, 35, 61)

    page._dec_radius.setValue(19)
    assert page._channel_params["CD3"] == {"tophat_radius": 19}
    assert (page._tophat_slider.value(), page._cucim_slider.value()) == (35, 61)

    # The local radius stays pinned; the still-inherited sigma follows global.
    _set_global(page, 44, 72)
    assert page._resolve_channel_params("CD3") == (19, 72)
    assert (page._dec_radius.value(), page._dec_sigma.value()) == (19, 72)
    # Another untouched channel continues to inherit both values.
    assert page._resolve_channel_params("CD20") == (44, 72)


def test_returning_a_local_value_to_the_global_default_restores_inheritance(app):
    page = _page(app)
    _set_global(page, 35, 61)
    page._dec_radius.setValue(19)
    page._dec_radius.setValue(35)

    assert "CD3" not in page._channel_params
    page._tophat_slider.setValue(47)
    assert page._dec_radius.value() == 47
    assert page._resolve_channel_params("CD3")[0] == 47


def test_build_config_serializes_only_real_per_parameter_overrides(app):
    page = _page(app)
    _set_global(page, 35, 61)
    page._channel_decisions = {"CD3": "tophat", "CD20": "cucim"}
    page._channel_params = {
        "CD3": {"tophat_radius": 19},
        "CD20": {"tophat_radius": 35, "cucim_sigma": 72},
    }

    config = page._build_config()

    assert config["method_params"] == {
        "tophat_radius": 35,
        "cucim_sigma": 61,
    }
    assert config["channel_params"] == {
        "CD3": {"tophat_radius": 19},
        "CD20": {"cucim_sigma": 72},
    }


def test_loading_an_old_full_pair_migrates_copied_defaults_back_to_inheritance(
        app, tmp_path):
    page = _page(app)
    page.output_dir = str(tmp_path)
    with open(tmp_path / "correction_config.json", "w", encoding="utf-8") as f:
        json.dump({
            "method_params": {"tophat_radius": 35, "cucim_sigma": 61},
            "channel_decisions": {"CD3": "tophat", "CD20": "cucim"},
            "channel_params": {
                # Both are old auto-materialised defaults, not proven edits.
                "CD3": {"tophat_radius": 15, "cucim_sigma": 50},
                # Non-default legacy values retain the useful evidence.
                "CD20": {"tophat_radius": 19, "cucim_sigma": 72},
            },
        }, f)

    page._load_existing_config()
    page._update_decision_ui()

    assert (page._tophat_slider.value(), page._cucim_slider.value()) == (35, 61)
    assert page._channel_params == {
        "CD20": {"tophat_radius": 19, "cucim_sigma": 72},
    }
    assert page._resolve_channel_params("CD3") == (35, 61)
    assert page._resolve_channel_params("CD20") == (19, 72)


def test_a_new_schema_preserves_an_intentional_override_equal_to_old_defaults(
        app, tmp_path):
    page = _page(app)
    page.output_dir = str(tmp_path)
    with open(tmp_path / "correction_config.json", "w", encoding="utf-8") as f:
        json.dump({
            "method_params": {"tophat_radius": 35, "cucim_sigma": 61},
            "channel_decisions": {"CD3": "tophat"},
            "channel_params": {"CD3": {"tophat_radius": 15}},
            "channel_param_overrides_schema": CHANNEL_PARAM_OVERRIDES_SCHEMA,
        }, f)

    page._load_existing_config()
    page._update_decision_ui()

    assert page._channel_params == {"CD3": {"tophat_radius": 15}}
    assert page._resolve_channel_params("CD3") == (15, 61)
    assert page._dec_radius.value() == 15


def _write_real_corrected_zarr(step0_dir, loader, config, rois,
                               process_channels=("CD3",)):
    result = {}
    worker = WsiCorrectionWorker(
        loader, str(step0_dir), config, rois=rois,
        process_channels=set(process_channels), incremental=False)
    worker.finished.connect(
        lambda path, decisions: result.update(path=path, decisions=decisions))
    worker.error.connect(lambda message: result.update(error=message))
    worker.run()
    assert "error" not in result
    assert result.get("path")
    return result["path"]


@pytest.mark.parametrize("local_radius", [None, 19])
def test_intensity_only_save_reuses_the_real_corrected_artifact(
        app, tmp_path, monkeypatch, local_radius):
    page = _page(app)
    _set_global(page, 35, 61)
    page.output_dir = str(tmp_path / "project")
    page._channel_order = ["DAPI", "CD3"]
    page._channel_decisions = {"CD3": "tophat"}
    page._channel_params = (
        {} if local_radius is None
        else {"CD3": {"tophat_radius": local_radius}})

    rois = [page._full_wsi_roi()]
    step0_dir = tmp_path / "stable_roi" / "step0"
    step0_dir.mkdir(parents=True)
    page._roi_context = {
        "roi_id": "stable_roi",
        "step_dirs": {"step0": str(step0_dir)},
    }
    page._roi_context_sig = page._roi_context_signature(rois)
    config = page._build_config()
    zarr_path = _write_real_corrected_zarr(
        step0_dir, page.loader, config, rois)

    expected_radius = local_radius if local_radius is not None else 35
    assert read_corrected_zarr_state(zarr_path) == (
        {"CD3": ("tophat", expected_radius,
                  BG_CORRECTION_ALGO_VERSION)},
        [(0, 24, 0, 28)],
    )

    # Intensity is a display mapping only. It must still be persisted by the
    # handoff, but it is not part of background-correction artifact identity.
    page._cond_workbench._params["CD3"] = {
        "min": 7.0, "max": 123.0, "gamma": 1.7,
    }
    completed = []
    page._confirm_raw_channels = lambda: True
    page._apply_corrected_store = lambda path, decisions: None
    page._emit_complete = (
        lambda cfg, path, decisions:
        (completed.append((cfg, path, decisions)) or True))

    class _MustNotRun:
        def __init__(self, *args, **kwargs):
            raise AssertionError(
                "unchanged background correction constructed a WSI worker")

    monkeypatch.setattr(sp, "WsiCorrectionWorker", _MustNotRun)
    page._save_and_continue()

    assert len(completed) == 1
    assert completed[0][1] == zarr_path
    assert completed[0][2] == {"CD3": "tophat"}
    assert read_corrected_zarr_state(zarr_path)[0]["CD3"] == (
        "tophat", expected_radius, BG_CORRECTION_ALGO_VERSION)


def test_a_global_change_dirties_only_channels_that_still_inherit_it(
        app, tmp_path, monkeypatch):
    page = _page(app)
    _set_global(page, 35, 61)
    page.output_dir = str(tmp_path / "project")
    page._channel_order = ["DAPI", "CD3", "CD20"]
    page._channel_decisions = {"CD3": "tophat", "CD20": "tophat"}
    page._channel_params = {"CD20": {"tophat_radius": 19}}
    rois = [page._full_wsi_roi()]
    step0_dir = tmp_path / "stable_roi" / "step0"
    step0_dir.mkdir(parents=True)
    page._roi_context = {
        "roi_id": "stable_roi",
        "step_dirs": {"step0": str(step0_dir)},
    }
    page._roi_context_sig = page._roi_context_signature(rois)
    _write_real_corrected_zarr(
        step0_dir, page.loader, page._build_config(), rois,
        process_channels=("CD3", "CD20"))

    # CD3 inherits and must move 35 -> 44. CD20 is locally pinned to 19.
    page._tophat_slider.setValue(44)
    captured = {}

    class _Worker(QtCore.QThread):
        progress = QtCore.pyqtSignal(int, int, int, int, str, str, int)
        finished = QtCore.pyqtSignal(str, dict)
        canceled = QtCore.pyqtSignal(str)
        error = QtCore.pyqtSignal(str)

        def __init__(self, *args, process_channels=None, incremental=False,
                     **kwargs):
            super().__init__()
            captured["channels"] = set(process_channels or ())
            captured["incremental"] = incremental

        def start(self):
            captured["started"] = True

        def stop_after_current_channel(self):
            pass

    class _Dialog(QtCore.QObject):
        cancel_requested = QtCore.pyqtSignal()

        def show(self):
            pass

        def set_progress(self, *args):
            pass

    monkeypatch.setattr(sp, "WsiCorrectionWorker", _Worker)
    monkeypatch.setattr(sp, "_WsiCorrectionProgressDialog", _Dialog)
    page._confirm_raw_channels = lambda: True
    page._release_explore_for_production = lambda reason: None
    page._watch_production_worker = lambda worker: None

    page._save_and_continue()

    assert captured == {
        "channels": {"CD3"},
        "incremental": True,
        "started": True,
    }


def _no_patch_full_wsi_page_with_saved_correction(app, tmp_path):
    """Build a page whose full-WSI artifact is real but has no preview patch."""
    page = _page(app)
    page.patches = []
    page.current_patch_idx = 0
    page.output_dir = str(tmp_path / "project")
    page._channel_order = ["DAPI", "CD3"]
    page._channel_decisions = {"CD3": "tophat"}
    page._channel_params = {}
    _set_global(page, 35, 61)

    rois = [page._full_wsi_roi()]
    step0_dir = tmp_path / "stable_roi" / "step0"
    step0_dir.mkdir(parents=True)
    page._roi_context = {
        "roi_id": "stable_roi",
        "step_dirs": {"step0": str(step0_dir)},
    }
    page._roi_context_sig = page._roi_context_signature(rois)
    config = page._build_config()
    zarr_path = _write_real_corrected_zarr(
        step0_dir, page.loader, config, rois)
    return page, zarr_path


def test_save_without_preview_patches_reuses_a_valid_full_wsi_artifact(
        app, tmp_path, monkeypatch):
    """A patch is a locator, not a prerequisite for an unchanged Save."""
    page, zarr_path = _no_patch_full_wsi_page_with_saved_correction(
        app, tmp_path)
    completed = []
    page._confirm_raw_channels = lambda: True
    page._apply_corrected_store = lambda path, decisions: None
    page._emit_complete = (
        lambda cfg, path, decisions:
        (completed.append((cfg, path, decisions)) or True))

    class _MustNotRun:
        def __init__(self, *args, **kwargs):
            raise AssertionError(
                "unchanged no-patch Save constructed a WSI worker")

    monkeypatch.setattr(sp, "WsiCorrectionWorker", _MustNotRun)
    page._save_and_continue()

    assert completed and completed[0][1] == zarr_path
    assert completed[0][2] == {"CD3": "tophat"}
    assert read_corrected_zarr_state(zarr_path)[0]["CD3"][1] == 35


def test_intensity_only_save_without_preview_patches_does_not_reprocess(
        app, tmp_path, monkeypatch):
    """Changing only display intensity must reuse correction without a patch."""
    page, zarr_path = _no_patch_full_wsi_page_with_saved_correction(
        app, tmp_path)
    page._cond_workbench._params["CD3"] = {
        "min": 7.0, "max": 123.0, "gamma": 1.7,
    }
    completed = []
    page._confirm_raw_channels = lambda: True
    page._apply_corrected_store = lambda path, decisions: None
    page._emit_complete = (
        lambda cfg, path, decisions:
        (completed.append((cfg, path, decisions)) or True))

    class _MustNotRun:
        def __init__(self, *args, **kwargs):
            raise AssertionError(
                "intensity-only no-patch Save constructed a WSI worker")

    monkeypatch.setattr(sp, "WsiCorrectionWorker", _MustNotRun)
    page._save_and_continue()

    assert completed and completed[0][1] == zarr_path
    assert read_corrected_zarr_state(zarr_path)[0]["CD3"][1] == 35


def test_no_patch_background_change_processes_the_full_wsi_region(
        app, tmp_path, monkeypatch):
    """With no patch, a changed correction is saved over the full WSI ROI."""
    page, zarr_path = _no_patch_full_wsi_page_with_saved_correction(
        app, tmp_path)
    page._tophat_slider.setValue(44)
    captured = {}

    class _Worker(QtCore.QThread):
        progress = QtCore.pyqtSignal(int, int, int, int, str, str, int)
        finished = QtCore.pyqtSignal(str, dict)
        canceled = QtCore.pyqtSignal(str)
        error = QtCore.pyqtSignal(str)

        def __init__(self, loader, out, cfg, rois=None, parent=None,
                     process_channels=None, incremental=False):
            super().__init__(parent)
            captured["channels"] = set(process_channels or ())
            captured["incremental"] = bool(incremental)
            captured["rois"] = list(rois or [])

        def start(self):
            captured["started"] = True

        def stop_after_current_channel(self):
            pass

    class _Dialog(QtCore.QObject):
        cancel_requested = QtCore.pyqtSignal()

        def __init__(self, parent=None):
            super().__init__(parent)

        def show(self):
            pass

        def set_progress(self, *args):
            pass

    monkeypatch.setattr(sp, "WsiCorrectionWorker", _Worker)
    monkeypatch.setattr(sp, "_WsiCorrectionProgressDialog", _Dialog)
    monkeypatch.setattr(sp, "read_corrected_zarr_state",
                        lambda zp: ({"CD3": ("tophat", 35,
                                              BG_CORRECTION_ALGO_VERSION)},
                                    [(0, 24, 0, 28)]))
    page._confirm_raw_channels = lambda: True
    page._apply_corrected_store = lambda path, decisions: None
    page._emit_complete = lambda *args, **kwargs: True
    page._release_explore_for_production = lambda reason: None
    page._watch_production_worker = lambda worker: None

    page._save_and_continue()

    assert captured["started"] is True
    assert captured["channels"] == {"CD3"}
    assert captured["incremental"] is True
    assert captured["rois"] == [page._full_wsi_roi()]
    assert str(zarr_path).endswith("corrected_channels.zarr")


def test_no_patch_without_correction_gives_a_correction_message_not_patch_error(
        app, tmp_path, monkeypatch):
    """No decisions is reported as no correction, not as missing a locator."""
    page = _page(app)
    page.patches = []
    page.output_dir = str(tmp_path / "project")
    page._channel_order = ["DAPI", "CD3"]
    page._channel_decisions = {}
    page._confirm_raw_channels = lambda: True
    page._ensure_empty_corrected_zarr = lambda *args, **kwargs: None
    page._apply_corrected_store = lambda *args, **kwargs: None
    page._emit_complete = lambda *args, **kwargs: True
    messages = []

    class _Msg:
        @staticmethod
        def information(_parent, title, text):
            messages.append((title, text))

        @staticmethod
        def warning(*args, **kwargs):
            raise AssertionError(f"unexpected validation warning: {args}")

    monkeypatch.setattr(sp, "QMessageBox", _Msg)
    page._save_and_continue()

    assert messages
    assert messages[-1][0] == "No background correction"
    assert "No channel is assigned TopHat or cuCIM" in messages[-1][1]
    assert "preview patch" not in messages[-1][1].lower()
