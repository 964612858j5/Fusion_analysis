"""Step1 follows the published geometry instead of keeping its own copy.

A patch edited in the shared navigator while Step1 is showing must reach Step0's
single ROI model, be published by Step0's writer, and then be adopted by Step1 —
dropping every preview, fusion and segmentation result that described the old
rectangle. The P1/P2/P3 buttons stay pure preview selectors.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")
pytest.importorskip("zarr")
import numpy as np  # noqa: E402

from PyQt5 import QtWidgets  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _roi(bbox=(0, 32, 0, 32)):
    y0, y1, x0, x1 = bbox
    return {"name": "ROI_1", "bbox_fullres": [y0, y1, x0, x1],
            "polygon_fullres": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}


def _window(app, tmp_path):
    """MainWindow bound to a Step0 page with one published handoff."""
    from block01.ui.main_window import MainWindow

    raw = tmp_path / "raw.ome.tif"
    raw.write_bytes(b"x" * 16)

    class _Loader:
        shape = (64, 64)
        ch_map = {"DAPI": 0, "CD3": 1}
        filepath = str(raw)

        def channel_names(self):
            return ["DAPI", "CD3"]

        def read_region(self, channel, y0, y1, x0, x1, downsample=1):
            ds = max(1, int(downsample))
            return np.zeros(((y1 - y0) // ds or 1, (x1 - x0) // ds or 1), np.float32)

    w = MainWindow()
    page = w._step0
    page.loader = _Loader()
    page.ome_path = str(raw)
    page.output_dir = str(tmp_path)
    page.panel_csv_path = ""
    page.panel_groups = {}
    page.nucleus_channel = "DAPI"
    page.overview.loader = page.loader
    page.overview.full_h, page.overview.full_w = 64, 64
    page.overview.full_wsi_mode = False
    page.overview._rois = [_roi()]
    page.overview._patches = [{"roi_idx": 0, "coords": (0, 16, 0, 16)}]

    step0_dir = tmp_path / "roi1" / "step0"
    step0_dir.mkdir(parents=True, exist_ok=True)
    page._roi_context = {
        "roi_id": "roi1",
        "roi_dir": str(tmp_path / "roi1"),
        "project_dir": str(tmp_path),
        "step_dirs": {"step0": str(step0_dir),
                      "step1": str(tmp_path / "roi1" / "step1"),
                      "step2": str(tmp_path / "roi1" / "step2")},
    }
    page._roi_context_sig = page._roi_context_signature(page._standard_rois())
    # The single ROI model is what feeds the navigator; keep it in step with the
    # overview state this fixture set up directly.
    page._roi_model.adopt(rois=[_roi()], patches=[(0, 16, 0, 16)],
                          full_wsi_mode=False, loader=page.loader,
                          nucleus_channel="DAPI")
    page._write_step0_handoff(
        {"method_params": {"tophat_radius": 25, "cucim_sigma": 30},
         "channel_decisions": {}},
        str(step0_dir / "corrected_channels.zarr"))

    w.loader = _Loader()
    w.step0_output = {
        "step0_manifest_path": str(step0_dir / "step0_roi_result.json"),
        "step0_dir": str(step0_dir),
        "step1_dir": str(tmp_path / "roi1" / "step1"),
        "output_dir": str(step0_dir),
    }
    w.step0_done = True
    w._step1_context_ready = True
    w._current_step = 1
    w._rois = [_roi()]
    w._active_roi = w._rois[0]
    w._on_patches([(0, 16, 0, 16)])
    return w, str(step0_dir)


def _published_patches(step0_dir):
    with open(os.path.join(step0_dir, "patch_config.json"), "r", encoding="utf-8") as f:
        return [p["bbox_fullres"] for p in json.load(f)]


def test_an_edit_in_the_shared_navigator_reaches_step0_disk_and_step1(app, tmp_path):
    w, step0_dir = _window(app, tmp_path)
    try:
        w._show_tissue_navigator()
        popup = w._step0._tissue_navigator_popup

        popup.overview._patches = [{"roi_idx": 0, "coords": (0, 16, 0, 16)},
                                   {"roi_idx": 0, "coords": (16, 32, 16, 32)}]
        popup.overview.patches_changed.emit(popup.overview._patch_coords())

        # one model -> the Step0 page overview, the published files and Step1
        assert [p["coords"] for p in w._step0.overview._patches] == [
            (0, 16, 0, 16), (16, 32, 16, 32)]
        assert _published_patches(step0_dir) == [[0, 16, 0, 16], [16, 32, 16, 32]]
        assert w._all_patches == [(0, 16, 0, 16), (16, 32, 16, 32)]
        assert len(w._patch_sel_btns) == 2
    finally:
        w.close()


def test_an_edit_on_the_step0_page_reaches_the_navigator_and_step1(app, tmp_path):
    w, step0_dir = _window(app, tmp_path)
    try:
        w._show_tissue_navigator()
        popup = w._step0._tissue_navigator_popup

        w._step0.overview._patches = [{"roi_idx": 0, "coords": (4, 20, 4, 20)}]
        w._step0.overview.patches_changed.emit(w._step0.overview._patch_coords())

        assert [p["coords"] for p in popup.overview._patches] == [(4, 20, 4, 20)]
        assert _published_patches(step0_dir) == [[4, 20, 4, 20]]
        assert w._all_patches == [(4, 20, 4, 20)]
    finally:
        w.close()


def test_a_moved_patch_drops_the_results_that_described_the_old_rectangle(app, tmp_path):
    w, step0_dir = _window(app, tmp_path)
    try:
        w._patch_channel_cache[0] = {"DAPI": np.zeros((4, 4), np.float32)}
        w._patch_load_ready.add(0)
        w._patch_seg_results[0] = {"cells": 11}
        w._seg_preview_history["P1"] = {"cellpose": {"history": [{"cells": 11}]}}
        w._active_preview_patch = "P1"

        w._step0.overview._patches = [{"roi_idx": 0, "coords": (8, 24, 8, 24)}]
        w._step0.overview.patches_changed.emit(w._step0.overview._patch_coords())

        assert w._all_patches == [(8, 24, 8, 24)]
        assert 0 not in w._patch_channel_cache
        assert 0 not in w._patch_load_ready
        assert 0 not in w._patch_seg_results
        assert "P1" not in w._seg_preview_history
        assert w._active_preview_patch == ""
    finally:
        w.close()


def test_a_geometry_commit_for_another_handoff_is_ignored(app, tmp_path):
    w, step0_dir = _window(app, tmp_path)
    try:
        w._step0.geometry_committed.emit({
            "step0_manifest_path": str(tmp_path / "other" / "step0_roi_result.json"),
            "rois": [_roi()],
            "patches": [{"name": "P1", "bbox_fullres": [40, 56, 40, 56]}],
        })
        assert w._all_patches == [(0, 16, 0, 16)]
    finally:
        w.close()


def test_the_patch_buttons_only_switch_the_preview(app, tmp_path):
    w, step0_dir = _window(app, tmp_path)
    try:
        w._step0.overview._patches = [{"roi_idx": 0, "coords": (0, 16, 0, 16)},
                                      {"roi_idx": 0, "coords": (16, 32, 16, 32)}]
        w._step0.overview.patches_changed.emit(w._step0.overview._patch_coords())
        published_before = _published_patches(step0_dir)
        geometry_before = list(w._all_patches)

        w._select_preview_patch(1)
        assert w._preview_patch_idx == 1
        w._select_preview_patch(0)
        assert w._preview_patch_idx == 0

        assert w._all_patches == geometry_before
        assert _published_patches(step0_dir) == published_before
    finally:
        w.close()
