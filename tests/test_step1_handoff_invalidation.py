"""When the published handoff stops matching the geometry, Step1 fails closed.

An ROI change moves the analysis region: the roi_context, the ROI directories
and the corrected zarr computed for the old region all stop applying, and there
is no safe cross-ROI reuse rule. A patch commit that cannot be written leaves
the geometry in memory ahead of what is published. In both cases the user's
edit is kept as Step0's staged geometry and every Step1 fact derived from the
old geometry is dropped, rather than the other way round.

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


def _window(app, tmp_path, publish=True):
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
    page._roi_model.adopt(rois=[_roi()], patches=[(0, 16, 0, 16)],
                          full_wsi_mode=False, loader=page.loader,
                          nucleus_channel="DAPI")

    step0_dir = tmp_path / "roi1" / "step0"
    step0_dir.mkdir(parents=True, exist_ok=True)
    if not publish:
        return w, str(step0_dir)

    page._roi_context = {
        "roi_id": "roi1",
        "roi_dir": str(tmp_path / "roi1"),
        "project_dir": str(tmp_path),
        "step_dirs": {"step0": str(step0_dir),
                      "step1": str(tmp_path / "roi1" / "step1"),
                      "step2": str(tmp_path / "roi1" / "step2")},
    }
    page._roi_context_sig = page._roi_context_signature(page._standard_rois())
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
    w._step0.show_tissue_navigator()
    return w, str(step0_dir)


def _published(step0_dir, name):
    with open(os.path.join(step0_dir, name), "r", encoding="utf-8") as f:
        return json.load(f)


def _draw_roi(overview, pts=((40, 40), (60, 40), (60, 60))):
    overview._set_mode("roi")
    for x, y in pts:
        if overview._mode == "roi":
            overview._cur_pts.append((x, y))
    overview._finish_roi()


def test_a_new_roi_in_step0_locks_step1_and_keeps_the_new_geometry(app, tmp_path):
    w, step0_dir = _window(app, tmp_path)
    try:
        ov = w._step0._tissue_navigator_popup.overview
        _draw_roi(ov)

        assert len(w._step0._roi_model.rois) == 2      # the edit is kept
        assert w.step0_done is False
        assert w._step1_context_ready is False
        assert w.step1_done is False
        assert "no longer valid" in w.prev_status.text()
        w._go_to_step1()
        assert w._stack.currentIndex() != 1
    finally:
        w.close()


def test_deleting_an_roi_in_step1_returns_to_step0_and_locks_it(app, tmp_path):
    w, step0_dir = _window(app, tmp_path)
    try:
        ov = w._step0._tissue_navigator_popup.overview
        w._stack.setCurrentIndex(1)
        w._set_step_active(1)

        ov._delete_last_roi()

        assert w._step0._roi_model.rois == []          # the deletion is kept
        assert w._stack.currentIndex() == 0            # sent back to Step0
        assert w.step0_done is False
        assert w._step1_context_ready is False
        assert w._all_patches == []
        assert w.loader is None
    finally:
        w.close()


def test_drawing_before_the_first_save_invalidates_nothing(app, tmp_path):
    w, step0_dir = _window(app, tmp_path, publish=False)
    seen = []
    try:
        w._step0.handoff_invalidated.connect(seen.append)
        w._step0.show_tissue_navigator()
        ov = w._step0._tissue_navigator_popup.overview
        _draw_roi(ov)
        ov._patches.append({"roi_idx": 0, "coords": (40, 56, 40, 56)})
        ov.patches_changed.emit(ov._patch_coords())

        assert seen == []
        assert len(w._step0._roi_model.rois) == 2
    finally:
        w.close()


def test_a_successful_patch_commit_keeps_step1_ready(app, tmp_path):
    w, step0_dir = _window(app, tmp_path)
    seen = []
    try:
        w._step0.handoff_invalidated.connect(seen.append)
        ov = w._step0._tissue_navigator_popup.overview
        w._patch_channel_cache[0] = {"DAPI": np.zeros((4, 4), np.float32)}
        w._patch_load_ready.add(0)

        ov._patches.append({"roi_idx": 0, "coords": (16, 32, 16, 32)})
        ov.patches_changed.emit(ov._patch_coords())

        assert seen == []
        assert w.step0_done is True
        assert w._step1_context_ready is True
        assert w._all_patches == [(0, 16, 0, 16), (16, 32, 16, 32)]
        assert [p["bbox_fullres"] for p in _published(step0_dir, "patch_config.json")] \
            == [[0, 16, 0, 16], [16, 32, 16, 32]]
    finally:
        w.close()


def test_a_failed_patch_write_stages_the_edit_and_locks_step1(app, tmp_path, monkeypatch):
    w, step0_dir = _window(app, tmp_path)
    try:
        manifest_path = os.path.join(step0_dir, "step0_roi_result.json")
        before_manifest = _published(step0_dir, "step0_roi_result.json")
        before_mtime = os.stat(manifest_path).st_mtime_ns

        def _fail(self, *_a, **_k):
            raise RuntimeError("disk is full")

        monkeypatch.setattr(type(w._step0), "_write_step0_handoff", _fail)
        ov = w._step0._tissue_navigator_popup.overview
        ov._patches.append({"roi_idx": 0, "coords": (16, 32, 16, 32)})
        ov.patches_changed.emit(ov._patch_coords())

        # staged in memory, absent from disk
        assert len(w._step0._roi_model.patches) == 2
        assert _published(step0_dir, "step0_roi_result.json") == before_manifest
        assert os.stat(manifest_path).st_mtime_ns == before_mtime
        # and Step1 is closed
        assert w.step0_done is False
        assert w._step1_context_ready is False
        assert "was not published" in w.prev_status.text()
    finally:
        w.close()


def test_a_missing_corrected_zarr_locks_step1_too(app, tmp_path):
    import shutil

    w, step0_dir = _window(app, tmp_path)
    try:
        shutil.rmtree(os.path.join(step0_dir, "corrected_channels.zarr"))
        ov = w._step0._tissue_navigator_popup.overview
        ov._patches.append({"roi_idx": 0, "coords": (16, 32, 16, 32)})
        ov.patches_changed.emit(ov._patch_coords())

        assert len(w._step0._roi_model.patches) == 2
        assert w.step0_done is False
        assert w._step1_context_ready is False
    finally:
        w.close()


def test_an_unchanged_geometry_or_a_patch_selection_invalidates_nothing(app, tmp_path):
    w, step0_dir = _window(app, tmp_path)
    seen = []
    try:
        w._step0.handoff_invalidated.connect(seen.append)
        ov = w._step0._tissue_navigator_popup.overview

        ov.patches_changed.emit(ov._patch_coords())     # same geometry
        w._select_preview_patch(0)
        w._show_tissue_navigator()
        w._step0._tissue_navigator_popup.hide()
        w._step0.show_tissue_navigator()

        assert seen == []
        assert w.step0_done is True
        assert w._step1_context_ready is True
    finally:
        w.close()


def test_an_invalidation_for_another_handoff_is_ignored(app, tmp_path):
    w, step0_dir = _window(app, tmp_path)
    try:
        w._step0.handoff_invalidated.emit({
            "step0_manifest_path": str(tmp_path / "other" / "step0_roi_result.json"),
            "geometry_revision": 99,
            "reason": "roi_changed",
            "message": "not ours",
        })
        assert w.step0_done is True
        assert w._step1_context_ready is True
        assert w._all_patches == [(0, 16, 0, 16)]
    finally:
        w.close()
