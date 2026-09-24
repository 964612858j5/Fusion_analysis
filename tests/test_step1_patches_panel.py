"""The Patches strip at the top of Step1's Pre-segmentation tab (block A1).

All patches ticked by default; ticks remembered by patch id through adds,
deletes and moves; `Select all` / `Select none`; a hint when there is no
patch; no hover text; a `×` on ticked tiles that deletes the patch through
Step0; double-click to rename, also through Step0; tiles that wrap and a
strip that stops growing at three rows and never widens the channel column.

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
from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402

from block01.ui.step0.roi_context_model import Patch  # noqa: E402
from block01.ui.step1_presegmentation import patches_panel as pp  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _patches(*ids):
    return [Patch((i * 16, i * 16 + 16, 0, 16), i) for i in ids]


def _panel(app, patches=None, width=260):
    panel = pp.PatchesPanel()
    panel.resize(width, 200)
    panel.set_patches(patches if patches is not None else _patches(1, 2, 3))
    panel.show()
    QtWidgets.QApplication.processEvents()
    return panel


def _names(panel):
    return [t.name() for t in panel.tiles()]


def _double_click(widget):
    """The event sequence a real double-click produces in Qt 5: press,
    release, DOUBLE-CLICK, release -- one press only. (QTest.mouseDClick
    sends a second press, which would hide a tick the first press flipped.)"""
    from PyQt5.QtCore import QEvent, QPointF
    from PyQt5.QtGui import QMouseEvent
    pos = QPointF(widget.width() / 3.0, widget.height() / 2.0)
    for kind, buttons in ((QEvent.MouseButtonPress, Qt.LeftButton),
                          (QEvent.MouseButtonRelease, Qt.NoButton),
                          (QEvent.MouseButtonDblClick, Qt.LeftButton),
                          (QEvent.MouseButtonRelease, Qt.NoButton)):
        QtWidgets.QApplication.sendEvent(
            widget, QMouseEvent(kind, pos, Qt.LeftButton, buttons, Qt.NoModifier))
    QtWidgets.QApplication.processEvents()


# ── the strip on its own ────────────────────────────────────────────────────
def test_every_patch_starts_ticked_and_select_all_none_work(app):
    panel = _panel(app)
    try:
        assert panel.selected_ids() == [1, 2, 3]
        assert panel._count.text() == "3/3 selected"
        QTest.mouseClick(panel._btn_none, Qt.LeftButton)
        assert panel.selected_ids() == []
        assert panel._count.text() == "0/3 selected"
        QTest.mouseClick(panel._btn_all, Qt.LeftButton)
        assert panel.selected_ids() == [1, 2, 3]
        QTest.mouseClick(panel.tiles()[1], Qt.LeftButton)     # untick P2
        assert panel.selected_ids() == [1, 3]
        assert panel._count.text() == "2/3 selected"
    finally:
        panel.close()


def test_ticks_follow_the_patch_id_through_delete_add_and_move(app):
    panel = _panel(app)
    try:
        QTest.mouseClick(panel.tiles()[2], Qt.LeftButton)     # untick P3
        panel.set_patches(_patches(1, 3))                      # P2 deleted
        assert _names(panel) == ["P1", "P3"]
        assert panel.selected_ids() == [1]                     # P3 stays unticked
        panel.set_patches(_patches(1, 3) + _patches(4))        # a new patch
        assert panel.selected_ids() == [1, 4]                  # comes in ticked
        moved = [Patch((40, 56, 40, 56), 3)] + _patches(1, 4)  # P3 moved / reordered
        panel.set_patches(moved)
        assert panel.selected_ids() == [1, 4]
    finally:
        panel.close()


def test_no_patch_shows_the_hint(app):
    panel = _panel(app, patches=[])
    try:
        assert panel._empty.isVisible() and panel._empty.text() == pp.EMPTY_HINT
        assert not panel._scroll.isVisible()
        assert not panel._btn_all.isEnabled() and not panel._btn_none.isEnabled()
        panel.set_patches(_patches(1))
        QtWidgets.QApplication.processEvents()
        assert not panel._empty.isVisible() and panel._scroll.isVisible()
    finally:
        panel.close()


def test_only_a_ticked_tile_has_the_x_and_it_asks_for_a_delete(app):
    panel = _panel(app)
    asked = []
    panel.delete_requested.connect(asked.append)
    try:
        tile = panel.tiles()[1]
        assert tile.close_button().isVisible()
        QTest.mouseClick(tile, Qt.LeftButton)                  # untick
        assert not tile.close_button().isVisible()
        QTest.mouseClick(tile, Qt.LeftButton)                  # tick again
        QTest.mouseClick(tile.close_button(), Qt.LeftButton)
        assert asked == [2]
        assert panel.selected_ids() == [1, 2, 3]               # the × is not a tick
    finally:
        panel.close()


def test_a_double_click_asks_for_a_rename_and_leaves_the_tick_alone(app, monkeypatch):
    panel = _panel(app)
    asked = []
    panel.rename_requested.connect(lambda pid, name: asked.append((pid, name)))
    try:
        monkeypatch.setattr(pp.QtWidgets.QInputDialog, "getText",
                            staticmethod(lambda *a, **k: ("edge", True)))
        _double_click(panel.tiles()[0])
        assert asked == [(1, "edge")]
        assert panel.selected_ids() == [1, 2, 3]
        # a name another patch has is refused before anything is asked of Step0
        warned = []
        monkeypatch.setattr(pp.QtWidgets.QMessageBox, "warning",
                            staticmethod(lambda *a, **k: warned.append(a)))
        monkeypatch.setattr(pp.QtWidgets.QInputDialog, "getText",
                            staticmethod(lambda *a, **k: ("P3", True)))
        _double_click(panel.tiles()[0])
        assert asked == [(1, "edge")] and len(warned) == 1
    finally:
        panel.close()


def test_delete_asks_once_and_names_only_the_ticked_patches(app, monkeypatch):
    panel = _panel(app)
    asked = []
    panel.delete_selected_requested.connect(asked.append)
    try:
        QTest.mouseClick(panel._btn_none, Qt.LeftButton)
        assert not panel._btn_delete.isEnabled()                 # nothing ticked
        QTest.mouseClick(panel.tiles()[0], Qt.LeftButton)
        QTest.mouseClick(panel.tiles()[2], Qt.LeftButton)
        assert panel._btn_delete.isEnabled()
        questions = []
        monkeypatch.setattr(pp.QtWidgets.QMessageBox, "question",
                            staticmethod(lambda *a, **k: (questions.append(a[2]),
                                                          pp.QtWidgets.QMessageBox.No)[1]))
        QTest.mouseClick(panel._btn_delete, Qt.LeftButton)
        assert asked == [] and questions == ["Delete 2 selected patches?"]
        monkeypatch.setattr(pp.QtWidgets.QMessageBox, "question",
                            staticmethod(lambda *a, **k: pp.QtWidgets.QMessageBox.Yes))
        QTest.mouseClick(panel._btn_delete, Qt.LeftButton)
        assert asked == [[1, 3]]
    finally:
        panel.close()


def test_there_is_no_hover_text(app):
    panel = _panel(app)
    try:
        assert all(t.toolTip() == "" and t.close_button().toolTip() == ""
                   for t in panel.tiles())
    finally:
        panel.close()


def test_tiles_wrap_and_the_strip_stops_growing_at_three_rows(app):
    cap = pp.MAX_ROWS * pp.TILE_H + (pp.MAX_ROWS - 1) * pp.TILE_SPACING + 2
    one = pp.TILE_H + 2
    panel = _panel(app, patches=_patches(1), width=220)
    try:
        assert panel._scroll.height() == one
        panel.set_patches(_patches(*range(1, 41)))
        QtWidgets.QApplication.processEvents()
        tops = {t.geometry().top() for t in panel.tiles()}
        assert len(tops) > pp.MAX_ROWS                         # it did wrap
        assert panel._scroll.height() == cap                   # and stopped growing
    finally:
        panel.close()


def test_forty_patches_do_not_make_the_strip_wider(app):
    # The tiles wrap instead of asking for a row's width: the tile area's
    # minimum is one tile, whatever the count, and the strip's own minimum
    # stays that of an empty strip (buttons and count, not the tiles).
    few = _panel(app, patches=_patches(1))
    many = _panel(app, patches=_patches(*range(1, 41)))
    try:
        assert many._tile_host.minimumSizeHint().width() <= pp.TILE_MIN_W
        # ... plus, at most, the vertical scroll bar the fourth row brings.
        bar = many.style().pixelMetric(QtWidgets.QStyle.PM_ScrollBarExtent)
        assert many.minimumSizeHint().width() <= few.minimumSizeHint().width() + bar
    finally:
        few.close()
        many.close()


# ── in the application ──────────────────────────────────────────────────────
def _roi(bbox=(0, 64, 0, 64)):
    y0, y1, x0, x1 = bbox
    return {"name": "ROI_1", "bbox_fullres": [y0, y1, x0, x1],
            "polygon_fullres": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}


def _window(app, tmp_path):
    """MainWindow bound to a Step0 page with a published handoff, P1..P3."""
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

    patches = [Patch((0, 16, 0, 16), 1), Patch((16, 32, 16, 32), 2),
               Patch((32, 48, 32, 48), 3)]
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
    page.overview.set_rois_and_patches([_roi()], patches)
    page._on_patches_changed(page.overview._patch_coords())
    step0_dir = tmp_path / "roi1" / "step0"
    step0_dir.mkdir(parents=True, exist_ok=True)
    page._roi_context = {
        "roi_id": "roi1", "roi_dir": str(tmp_path / "roi1"), "project_dir": str(tmp_path),
        "step_dirs": {"step0": str(step0_dir), "step1": str(tmp_path / "roi1" / "step1"),
                      "step2": str(tmp_path / "roi1" / "step2")},
    }
    page._roi_context_sig = page._roi_context_signature(page._standard_rois())
    page._roi_model.adopt(rois=[_roi()], patches=patches, full_wsi_mode=False,
                          loader=page.loader, nucleus_channel="DAPI")
    page._write_step0_handoff(
        {"method_params": {"tophat_radius": 25, "cucim_sigma": 30}, "channel_decisions": {}},
        str(step0_dir / "corrected_channels.zarr"))
    w.loader = _Loader()
    w.step0_output = {
        "step0_manifest_path": str(step0_dir / "step0_roi_result.json"),
        "step0_dir": str(step0_dir), "step1_dir": str(tmp_path / "roi1" / "step1"),
        "output_dir": str(step0_dir),
    }
    w.step0_done = True
    w._step1_context_ready = True
    w._current_step = 1
    w._rois = [_roi()]
    w._active_roi = w._rois[0]
    w._on_patches(list(patches))
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
        return [(p.get("id"), p.get("name")) for p in json.load(f)]


def test_the_strip_heads_the_method_and_parameters_tab(app, tmp_path):
    w, _ = _window(app, tmp_path)
    try:
        lay = w.method_params_tab.layout()
        assert lay.itemAt(0).widget() is w._preseg_patches
        # Block B put the Methods part between the strip and the old controls.
        assert lay.itemAt(1).widget() is w._preseg_methods
        assert lay.itemAt(2).widget() is w._preseg_results
        assert lay.itemAt(3).widget() is w._step1_method_params_scroll
        assert _names(w._preseg_patches) == ["P1", "P2", "P3"]
        assert w._preseg_patches.selected_ids() == [1, 2, 3]
        # It must not widen the channel column: the left tabs' minimum is the
        # same with the strip shown as without it -- with many patches, which
        # is when a strip that did not wrap would ask for the most.
        w._on_patches([Patch((i * 4, i * 4 + 4, 0, 4), i) for i in range(1, 30)])
        tabs = w.method_params_tab.parentWidget().parentWidget()
        QtWidgets.QApplication.processEvents()
        with_strip = tabs.minimumSizeHint().width()
        w._preseg_patches.hide()
        QtWidgets.QApplication.processEvents()
        assert with_strip == tabs.minimumSizeHint().width()
    finally:
        w.close()


def test_the_x_deletes_through_step0_and_every_view_follows(app, tmp_path):
    w, step0_dir = _window(app, tmp_path)
    try:
        w._show_tissue_navigator()
        nav = w._step0._tissue_navigator_popup.overview
        w._show_step1_method_params_tab("test")
        QtWidgets.QApplication.processEvents()
        tile = w._preseg_patches.tiles()[1]
        QTest.mouseClick(tile.close_button(), Qt.LeftButton)     # delete P2
        assert _settle(w) == "published"
        assert _names(w._preseg_patches) == ["P1", "P3"]
        assert [nav.patch_name(i) for i in range(len(nav._patches))] == ["P1", "P3"]
        assert [a.text() for a in w._patch_menu_actions] == ["P1", "P3"]
        assert _published(step0_dir) == [(1, "P1"), (3, "P3")]
    finally:
        w.close()


def test_a_double_click_renames_through_step0_and_every_view_follows(app, tmp_path, monkeypatch):
    w, step0_dir = _window(app, tmp_path)
    try:
        w._show_step1_method_params_tab("test")
        QtWidgets.QApplication.processEvents()
        monkeypatch.setattr(pp.QtWidgets.QInputDialog, "getText",
                            staticmethod(lambda *a, **k: ("edge", True)))
        _double_click(w._preseg_patches.tiles()[2])
        assert _settle(w) == "published"
        assert _names(w._preseg_patches) == ["P1", "P2", "edge"]
        assert w._step0.overview.patch_name(2) == "edge"
        assert w._patch_menu_actions[2].text().startswith("edge")
        assert w._step0._patch_list.item(2).text() == "edge"     # the name alone
        assert _published(step0_dir)[2] == (3, "edge")
    finally:
        w.close()


def test_on_screen_the_strip_is_no_taller_than_it_needs_and_shows_its_count(app, tmp_path):
    # Measured with the window shown (offscreen), where the tab's layout has
    # really been applied: a strip that stretched would take the method
    # controls' height, and a count squeezed to nothing would be invisible.
    w, _ = _window(app, tmp_path)
    try:
        w.resize(1500, 950)
        w.show()
        w._stack.setCurrentIndex(1)
        w._show_step1_method_params_tab("test")
        QtWidgets.QApplication.processEvents()
        strip = w._preseg_patches
        assert strip.height() <= strip.sizeHint().height()
        assert strip._count.isVisible() and strip._count.width() > 0
        assert strip._count.text() == "3/3 selected"
    finally:
        w.close()


def test_delete_removes_the_ticked_patches_in_one_edit(app, tmp_path, monkeypatch):
    w, step0_dir = _window(app, tmp_path)
    try:
        w._show_tissue_navigator()
        nav = w._step0._tissue_navigator_popup.overview
        monkeypatch.setattr(pp.QtWidgets.QMessageBox, "question",
                            staticmethod(lambda *a, **k: pp.QtWidgets.QMessageBox.Yes))
        strip = w._preseg_patches
        emitted = []
        w._step0.overview.patches_changed.connect(lambda *_: emitted.append(1))
        QTest.mouseClick(strip.tiles()[1], Qt.LeftButton)          # untick P2
        QTest.mouseClick(strip._btn_delete, Qt.LeftButton)         # delete P1, P3
        assert _settle(w) == "published"
        assert len(emitted) == 1                                    # one edit, not two
        assert _names(strip) == ["P2"]
        assert [nav.patch_name(i) for i in range(len(nav._patches))] == ["P2"]
        assert _published(step0_dir) == [(2, "P2")]
        QTest.mouseClick(strip._btn_all, Qt.LeftButton)
        QTest.mouseClick(strip._btn_delete, Qt.LeftButton)         # select all + delete
        assert _settle(w) == "published"
        assert strip.tiles() == [] and not strip._empty.isHidden()   # the hint is back
        assert _published(step0_dir) == []
    finally:
        w.close()


def test_the_tab_is_called_pre_segmentation(app, tmp_path):
    w, _ = _window(app, tmp_path)
    try:
        tabs = w.method_params_tab.parentWidget().parentWidget()
        assert tabs.tabText(tabs.indexOf(w.method_params_tab)) == "Pre-segmentation"
    finally:
        w.close()


def test_the_navigator_patch_list_shows_the_name_alone(app, tmp_path):
    w, _ = _window(app, tmp_path)
    try:
        lst = w._step0._patch_list
        assert [lst.item(i).text() for i in range(lst.count())] == ["P1", "P2", "P3"]
    finally:
        w.close()
