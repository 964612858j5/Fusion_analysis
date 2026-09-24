"""Patches keep their names (plan block P, user ruling 2026-09-24).

Deleting P2 of P1..P3 leaves P3 as P3; a new patch takes the next id after
the highest ever used and never reuses one; a renamed patch shows its new
name in every view -- the navigator, Step0's list and buttons, Step1's patch
selector and menu, Step1.5 -- and on disk. Before this, every "P<n>" was the
patch's list position, re-counted on every edit.

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

from block01.ui.step0.roi_context_model import (  # noqa: E402
    Patch, RoiContextModel, with_patch_ids)


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _roi(bbox=(0, 64, 0, 64)):
    y0, y1, x0, x1 = bbox
    return {"name": "ROI_1", "bbox_fullres": [y0, y1, x0, x1],
            "polygon_fullres": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}


def _window(app, tmp_path):
    """MainWindow bound to a Step0 page with one published handoff (one patch)."""
    from block01.ui.main_window import MainWindow

    raw = tmp_path / "raw.ome.tif"
    raw.write_bytes(b"x" * 16)

    class _Loader:
        shape = (128, 128)
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
    page.overview.full_h, page.overview.full_w = 128, 128
    page.overview.full_wsi_mode = False
    # Through the panel's public setter, as the page's own model feed does,
    # then the page's list is built from it.
    page.overview.set_rois_and_patches([_roi()], [Patch((0, 16, 0, 16), 1)])
    page._on_patches_changed(page.overview._patch_coords())

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
    page._roi_model.adopt(rois=[_roi()], patches=[Patch((0, 16, 0, 16), 1)],
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
    w._on_patches([Patch((0, 16, 0, 16), 1)])
    return w, str(step0_dir)


def _settle(w, timeout=20.0):
    import time
    deadline = time.monotonic() + timeout
    worker = getattr(w._step0, "_geometry_persist_worker", None)
    while time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        if worker is None or not worker.is_busy():
            break
        time.sleep(0.005)
    QtWidgets.QApplication.processEvents()
    return w._step0.geometry_persist_state()


def _published(step0_dir):
    with open(os.path.join(step0_dir, "step0_roi_result.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    with open(manifest["patch_config_path"], encoding="utf-8") as f:
        return [(p.get("id"), p.get("name")) for p in json.load(f)], manifest


def _names(panel):
    return [panel.patch_name(i) for i in range(len(panel._patches))]


def _label_texts(panel):
    return [lbl.toPlainText() for _rect, lbl in panel._patch_artists]


def _step1_labels(w):
    return [a.text() for a in w._patch_menu_actions]


# ── the model ───────────────────────────────────────────────────────────────
def test_an_id_is_never_given_twice():
    m = RoiContextModel()
    m.adopt(patches=[(0, 1, 0, 1), (2, 3, 2, 3), (4, 5, 4, 5)])
    assert [p.name for p in m.patches] == ["P1", "P2", "P3"]
    m.adopt(patches=[m.patches[0], m.patches[2]])            # delete P2
    assert [p.name for p in m.patches] == ["P1", "P3"]
    assert m.allocate_patch_id() == 4                         # not 2
    m.adopt(patches=[m.patches[0]])                           # delete the highest
    assert m.allocate_patch_id() == 5                         # and 4 is not reused


def test_a_list_from_before_stable_ids_keeps_the_numbers_it_was_shown_with():
    patches, nxt = with_patch_ids([(0, 1, 0, 1), (2, 3, 2, 3)])
    assert [(p.id, p.name) for p in patches] == [(1, "P1"), (2, "P2")] and nxt == 3


# ── delete / add through the navigator ──────────────────────────────────────
def test_deleting_p2_leaves_p3_and_a_new_patch_is_p4_everywhere(app, tmp_path):
    w, step0_dir = _window(app, tmp_path)
    try:
        w._show_tissue_navigator()
        nav = w._step0._tissue_navigator_popup.overview
        nav.add_patch_rect(16, 32, 16, 32, roi_idx=0)
        assert _settle(w) == "published"
        nav.add_patch_rect(32, 48, 32, 48, roi_idx=0)
        assert _settle(w) == "published"
        assert _names(nav) == ["P1", "P2", "P3"]

        nav._remove_patch(1)                                   # delete P2
        assert _settle(w) == "published"
        for panel in (nav, w._step0.overview):
            assert _names(panel) == ["P1", "P3"]
            assert _label_texts(panel) == ["P1", "P3"]
        assert [w._step0._patch_list.item(i).text().split()[0]
                for i in range(w._step0._patch_list.count())] == ["P1", "P3"]
        assert _step1_labels(w) == ["P1", "P3"]

        nav.add_patch_rect(48, 64, 48, 64, roi_idx=0)
        assert _settle(w) == "published"
        assert _names(nav) == ["P1", "P3", "P4"]
        assert _step1_labels(w) == ["P1", "P3", "P4"]
        records, manifest = _published(step0_dir)
        assert records == [(1, "P1"), (3, "P3"), (4, "P4")]

        nav._remove_patch(2)                                   # delete the highest
        assert _settle(w) == "published"
        nav.add_patch_rect(48, 64, 48, 64, roi_idx=0)
        assert _settle(w) == "published"
        assert _names(nav) == ["P1", "P3", "P5"]               # 4 is not reused
        records, manifest = _published(step0_dir)
        assert manifest["next_patch_id"] == 6
    finally:
        w.close()


def test_a_patch_keeps_its_colour_in_every_view_when_another_is_deleted(app, tmp_path):
    # User ruling 2026-09-24: colour follows the permanent id, not the
    # position -- deleting P2 must not hand P3 the colour P2 had.
    from block01.config import PATCH_COLORS
    w, _ = _window(app, tmp_path)
    try:
        w._show_tissue_navigator()
        nav = w._step0._tissue_navigator_popup.overview
        nav.add_patch_rect(16, 32, 16, 32, roi_idx=0)
        _settle(w)
        nav.add_patch_rect(32, 48, 32, 48, roi_idx=0)
        _settle(w)
        p3 = PATCH_COLORS[2]
        nav._remove_patch(1)                                   # delete P2
        assert _settle(w) == "published"

        def canvas_colour(panel, i):
            return panel._patch_artists[i][0].pen().color().name()

        for panel in (nav, w._step0.overview):
            assert _names(panel) == ["P1", "P3"]
            assert canvas_colour(panel, 1) == p3.lower()
        s0_btn = [b for row in w._step0._all_patch_rows()
                  for b in (row.itemAt(k).widget() for k in range(row.count()))
                  if isinstance(b, QtWidgets.QPushButton) and b.property("patch_index") == 1][0]
        assert f"color:{p3}" in s0_btn.styleSheet()
        assert f"color:{p3}" in w._patch_sel_btns[1].styleSheet()
        assert w._preseg_patches.tiles()[1]._color == p3
        page15 = w._step1_5
        page15.set_context(w.loader, str(tmp_path / "s15"), list(w._all_patches), "DAPI")
        b15 = [page15._patch_buttons_row.itemAt(k).widget()
               for k in range(page15._patch_buttons_row.count())
               if isinstance(page15._patch_buttons_row.itemAt(k).widget(), QtWidgets.QPushButton)]
        assert f"color:{p3}" in b15[1].styleSheet()
    finally:
        w.close()


# ── rename ──────────────────────────────────────────────────────────────────
def test_a_double_click_rename_reaches_every_view_and_is_published(app, tmp_path, monkeypatch):
    from block01.ui.step0 import step0_page as sp
    w, step0_dir = _window(app, tmp_path)
    try:
        w._show_tissue_navigator()
        nav = w._step0._tissue_navigator_popup.overview
        monkeypatch.setattr(sp.QInputDialog, "getText",
                            staticmethod(lambda *a, **k: ("tumour edge", True)))
        lst = w._step0._patch_list
        lst.itemDoubleClicked.emit(lst.item(0))
        # A rename moves no rectangle and still has to be published.
        assert _settle(w) == "published"

        for panel in (nav, w._step0.overview):
            assert _names(panel) == ["tumour edge"]
            assert _label_texts(panel) == ["tumour edge"]
        assert lst.item(0).text().startswith("tumour edge")
        assert _step1_labels(w)[0].startswith("tumour edge")
        assert w._patch_sel_btns[0].text().startswith("tumour edge")
        records, _ = _published(step0_dir)
        assert records == [(1, "tumour edge")]                 # same id
    finally:
        w.close()


def test_an_empty_or_duplicate_name_is_refused(app, tmp_path):
    w, _ = _window(app, tmp_path)
    try:
        panel = w._step0.overview
        panel.add_patch_rect(16, 32, 16, 32, roi_idx=0)
        _settle(w)
        assert panel.rename_patch(1, "  ") is False
        assert panel.rename_patch(1, "P1") is False
        assert _names(panel) == ["P1", "P2"]
        assert panel.rename_patch(1, "edge") is True
        assert _names(panel) == ["P1", "edge"]
    finally:
        w.close()


# ── Step1 ───────────────────────────────────────────────────────────────────
def test_step1_keeps_the_ids_through_the_roi_filter(app, tmp_path):
    w, step0_dir = _window(app, tmp_path)
    try:
        w._rois = [_roi((0, 32, 0, 32))]
        w._active_roi = w._rois[0]
        w._step0.geometry_committed.emit({
            "step0_manifest_path": w.step0_output["step0_manifest_path"],
            "rois": [_roi((0, 32, 0, 32))],
            "patches": [
                {"id": 1, "name": "P1", "bbox_fullres": [0, 16, 0, 16]},
                {"id": 2, "name": "P2", "bbox_fullres": [40, 56, 40, 56]},   # outside
                {"id": 3, "name": "P3", "bbox_fullres": [16, 32, 16, 32]},
            ]})
        assert [p.id for p in w._all_patches] == [1, 3]
        assert _step1_labels(w) == ["P1", "P3"]                 # not P1, P2
    finally:
        w.close()


def test_a_delete_keeps_the_history_of_the_patches_that_remain(app, tmp_path):
    w, _ = _window(app, tmp_path)
    try:
        w._on_patches([Patch((0, 16, 0, 16), 1), Patch((16, 32, 16, 32), 3)])
        w._seg_preview_history["P1"] = {"cellpose": {"history": [1]}}
        w._seg_preview_history["P3"] = {"cellpose": {"history": [3]}}
        w._on_patches([Patch((16, 32, 16, 32), 3)])             # delete P1
        assert "P3" in w._seg_preview_history
        assert "P1" not in w._seg_preview_history
        w._on_patches([Patch((20, 36, 20, 36), 3)])             # move P3
        assert "P3" not in w._seg_preview_history
    finally:
        w.close()


def test_the_session_remembers_the_selected_patch_by_id(app, tmp_path):
    w, _ = _window(app, tmp_path)
    try:
        w._on_patches([Patch((0, 16, 0, 16), 1), Patch((16, 32, 16, 32), 3, "edge")])
        w._select_preview_patch(1)
        assert w._patch_key(1) == "P3"
        assert w._patch_index_for_key("P3") == 1
        assert w._patch_label(1) == "edge"
    finally:
        w.close()


def test_a_patch_file_from_before_stable_ids_reads_as_p1_to_pn(app, tmp_path):
    from block01.ui.main_window import MainWindow
    patches = MainWindow._patches_from_records([
        {"name": "P1", "bbox_fullres": [0, 16, 0, 16]},
        {"name": "P2", "bbox_fullres": [16, 32, 16, 32]}])
    assert [(p.id, p.name) for p in patches] == [(1, "P1"), (2, "P2")]


def test_a_page_bound_to_an_old_project_numbers_after_its_patches(app, tmp_path):
    w, _ = _window(app, tmp_path)
    try:
        model = w._step0._roi_model
        model.reset_patch_ids()
        w._step0.adopt_published_geometry_revision({"n_patches": 5})   # no next_patch_id
        assert model.allocate_patch_id() == 6
        w._step0.adopt_published_geometry_revision({"next_patch_id": 9, "n_patches": 2})
        assert model.allocate_patch_id() == 9
    finally:
        w.close()


def test_step1_5_shows_the_patch_names(app, tmp_path):
    w, _ = _window(app, tmp_path)
    try:
        page = w._step1_5
        page.set_context(w.loader, str(tmp_path / "s15"),
                         [Patch((0, 16, 0, 16), 1), Patch((16, 32, 16, 32), 3, "edge")], "DAPI")
        texts = [page._patch_buttons_row.itemAt(i).widget().text()
                 for i in range(page._patch_buttons_row.count())
                 if isinstance(page._patch_buttons_row.itemAt(i).widget(), QtWidgets.QPushButton)]
        assert texts == ["P1", "edge"]
        page._select_patch(1)
        checked = [page._patch_buttons_row.itemAt(i).widget().isChecked()
                   for i in range(len(texts))]
        assert checked == [False, True]
    finally:
        w.close()
