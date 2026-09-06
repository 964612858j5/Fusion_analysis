"""ROI/patch edit rights follow the step, on the ONE shared navigator.

Step0 may create, reshape and delete ROIs. Step1 borrows the same window and
may delete an ROI and edit patches freely, but may not create or reshape one:
a new analysis region needs its own corrected output, which only a Step0 Save
can produce. The gate sits on the edit actions themselves, so a forbidden edit
never reaches the model and never has to be silently undone.

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


def _roi(bbox=(0, 32, 0, 32), name="ROI_1"):
    y0, y1, x0, x1 = bbox
    return {"name": name, "bbox_fullres": [y0, y1, x0, x1],
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
    for panel in (page.overview,):
        panel.loader = page.loader
        panel.full_h, panel.full_w = 64, 64
        panel.full_wsi_mode = False
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
    w._rois = [_roi()]
    w._active_roi = w._rois[0]
    w._on_patches([(0, 16, 0, 16)])
    w._step0.show_tissue_navigator()   # opened from Step0, as a user would
    return w, str(step0_dir)


def _enter_step1(w):
    w._stack.setCurrentIndex(1)
    w._set_step_active(1)


def _enter_step0(w):
    w._go_to_step0()


def _published(step0_dir, name):
    with open(os.path.join(step0_dir, name), "r", encoding="utf-8") as f:
        return json.load(f)


def _draw_roi(overview, pts=((40, 40), (60, 40), (60, 60))):
    """The exact sequence a user's clicks produce: vertices, then close."""
    overview._set_mode("roi")
    for x, y in pts:
        if overview._mode == "roi":
            overview._cur_pts.append((x, y))
    overview._finish_roi()


def test_step0_may_create_and_delete_rois_and_edit_patches(app, tmp_path):
    w, step0_dir = _window(app, tmp_path)
    try:
        ov = w._step0._tissue_navigator_popup.overview
        assert ov.edit_policy() == {"roi_create": True, "roi_delete": True,
                                    "patch_edit": True}
        before = len(ov._rois)
        _draw_roi(ov)
        assert len(ov._rois) == before + 1
        ov._delete_last_roi()
        assert len(ov._rois) == before
    finally:
        w.close()


def test_step1_cannot_create_an_roi(app, tmp_path):
    w, step0_dir = _window(app, tmp_path)
    try:
        ov = w._step0._tissue_navigator_popup.overview
        _enter_step1(w)
        assert ov.edit_policy()["roi_create"] is False

        model_before = list(w._step0._roi_model.rois)
        drawn_before = len(ov._rois)
        disk_before = _published(step0_dir, "roi_config.json")

        _draw_roi(ov)

        assert ov._mode != "roi"                      # the tool never armed
        assert ov._cur_pts == []                      # no vertex was taken
        assert len(ov._rois) == drawn_before          # the picture is unchanged
        assert list(w._step0._roi_model.rois) == model_before
        assert _published(step0_dir, "roi_config.json") == disk_before
    finally:
        w.close()


def test_step1_cannot_reshape_an_roi(app, tmp_path):
    w, step0_dir = _window(app, tmp_path)
    try:
        ov = w._step0._tissue_navigator_popup.overview
        _enter_step1(w)
        # The panel offers no ROI move/resize gesture at all: ROI geometry can
        # only change by drawing a new one or deleting one, and creation is
        # already refused above. Pin that, so a future reshape gesture cannot
        # be added without deciding its policy.
        assert not hasattr(ov, "_roi_drag")
        assert not hasattr(ov, "_begin_roi_drag")
        assert ov.edit_policy()["roi_create"] is False
    finally:
        w.close()


def test_step1_may_delete_an_roi(app, tmp_path):
    w, step0_dir = _window(app, tmp_path)
    try:
        ov = w._step0._tissue_navigator_popup.overview
        _enter_step1(w)
        assert ov.edit_policy()["roi_delete"] is True
        assert len(ov._rois) == 1

        ov._delete_last_roi()
        assert ov._rois == []
        assert list(w._step0._roi_model.rois) == []
    finally:
        w.close()


def test_step1_may_add_move_and_delete_patches(app, tmp_path):
    w, step0_dir = _window(app, tmp_path)
    try:
        ov = w._step0._tissue_navigator_popup.overview
        _enter_step1(w)
        assert ov.edit_policy()["patch_edit"] is True

        ov._patches = [{"roi_idx": 0, "coords": (0, 16, 0, 16)},
                       {"roi_idx": 0, "coords": (16, 32, 16, 32)}]
        ov.patches_changed.emit(ov._patch_coords())
        assert [p["bbox_fullres"] for p in _published(step0_dir, "patch_config.json")] \
            == [[0, 16, 0, 16], [16, 32, 16, 32]]

        assert ov._commit_patch_geometry(1, (18, 30, 18, 30)) is True
        assert [p["bbox_fullres"] for p in _published(step0_dir, "patch_config.json")] \
            == [[0, 16, 0, 16], [18, 30, 18, 30]]

        ov._remove_patch(1)
        assert [p["bbox_fullres"] for p in _published(step0_dir, "patch_config.json")] \
            == [[0, 16, 0, 16]]
        assert w.step0_done is True          # patch edits never lock Step1
        assert w._step1_context_ready is True
    finally:
        w.close()


def test_returning_to_step0_restores_full_roi_editing_without_reopening(app, tmp_path):
    w, step0_dir = _window(app, tmp_path)
    try:
        ov = w._step0._tissue_navigator_popup.overview
        _enter_step1(w)
        assert ov.edit_policy()["roi_create"] is False

        _enter_step0(w)                       # the popup is never closed
        assert ov.edit_policy()["roi_create"] is True
        before = len(ov._rois)
        _draw_roi(ov)
        assert len(ov._rois) == before + 1

        _enter_step1(w)
        assert ov.edit_policy()["roi_create"] is False
    finally:
        w.close()


def test_round_trips_keep_one_popup_one_overview_and_the_camera(app, tmp_path):
    from block01.ui.step0.overview_panel import OverviewPanel
    from block01.ui.widgets.tissue_navigator_popup import TissueNavigatorPopup

    w, step0_dir = _window(app, tmp_path)
    try:
        popup = w._step0._tissue_navigator_popup
        popup.overview.vb.setRange(xRange=[1, 9], yRange=[2, 8], padding=0)
        camera = [list(r) for r in popup.overview.vb.viewRange()]

        for _ in range(3):
            _enter_step1(w)
            w._show_tissue_navigator()
            _enter_step0(w)
            w._step0.show_tissue_navigator()

        assert w._step0._tissue_navigator_popup is popup
        assert len(w.findChildren(TissueNavigatorPopup)) == 1
        assert len(popup.findChildren(OverviewPanel)) == 1
        assert w._step0._registered_roi_overviews() == [w._step0.overview,
                                                        popup.overview]
        assert [list(r) for r in popup.overview.vb.viewRange()] == camera
    finally:
        w.close()


def _downstream_steps():
    return [2, 3, 4]


@pytest.mark.parametrize("step", _downstream_steps())
def test_steps_after_step1_get_a_read_only_navigator(app, tmp_path, step):
    w, step0_dir = _window(app, tmp_path)
    try:
        ov = w._step0._tissue_navigator_popup.overview
        w._set_step_active(step)
        assert ov.edit_policy() == {"roi_create": False, "roi_delete": False,
                                    "patch_edit": False}

        rois_before = [dict(r) for r in ov._rois]
        patches_before = [dict(p) for p in ov._patches]
        disk_before = _published(step0_dir, "patch_config.json")

        _draw_roi(ov)
        ov._delete_last_roi()
        ov._add_patch(40, 56, 40, 56, 10, 14, 10, 14, 0)
        ov._remove_patch(0)
        assert ov._commit_patch_geometry(0, (2, 14, 2, 14)) is False

        assert [dict(r) for r in ov._rois] == rois_before
        assert [dict(p) for p in ov._patches] == patches_before
        assert _published(step0_dir, "patch_config.json") == disk_before
    finally:
        w.close()


def test_the_roi_and_patch_lists_cannot_delete_downstream_either(app, tmp_path):
    w, step0_dir = _window(app, tmp_path)
    try:
        ov = w._step0._tissue_navigator_popup.overview
        w._set_step_active(2)
        w._step0._roi_selected_indices = [0]
        w._step0._patch_selected_indices = [0]

        w._step0._delete_selected_rois()
        w._step0._delete_selected_patches()

        assert len(w._step0.overview._rois) == 1
        assert len(w._step0.overview._patches) == 1
    finally:
        w.close()


def test_step1_5_also_gets_a_read_only_navigator(app, tmp_path):
    w, step0_dir = _window(app, tmp_path)
    try:
        ov = w._step0._tissue_navigator_popup.overview
        w._go_to_step1_5()
        assert w._stack.currentWidget() is w._step1_5
        assert ov.edit_policy() == {"roi_create": False, "roi_delete": False,
                                    "patch_edit": False}
    finally:
        w.close()


def test_coming_back_to_step0_from_a_read_only_step_restores_everything(app, tmp_path):
    w, step0_dir = _window(app, tmp_path)
    try:
        ov = w._step0._tissue_navigator_popup.overview
        w._set_step_active(3)
        assert ov.edit_policy()["patch_edit"] is False

        w._go_to_step0()
        assert ov.edit_policy() == {"roi_create": True, "roi_delete": True,
                                    "patch_edit": True}
        before = len(ov._rois)
        _draw_roi(ov)
        assert len(ov._rois) == before + 1
    finally:
        w.close()
