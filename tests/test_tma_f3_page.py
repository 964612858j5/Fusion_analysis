"""The Regions page (docs/region_qc_module_plan.md §4, R2; F3's TMA page
tests moved onto it), offscreen: choose the map sheet, find cores, number
by line / clicks / one core, merge, undo, lock, reopen read-only, the draft
back after a reopen, a late result of another context dropped, closing
while a job runs; the overlay as the one input path (a drag the tool does
not take pans the view).

The page is built on its own; a pyqtgraph view stands in for the Step1
viewer (the overlay attaches to any graphics view + ViewBox).
"""

import importlib.util
import json
import os
import sys

import numpy as np
import pytest

pytest.importorskip("openpyxl")
tifffile = pytest.importorskip("tifffile")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pyqtgraph as pg  # noqa: E402
from PyQt5 import QtCore  # noqa: E402
from PyQt5.QtWidgets import QApplication, QMessageBox  # noqa: E402

from block01.core.regions import document as D  # noqa: E402
from block01.core.regions import lock as K  # noqa: E402

_F1 = os.path.join(os.path.dirname(__file__), "test_tma_f1.py")
_spec = importlib.util.spec_from_file_location("_tma_f1_helpers", _F1)
f1 = importlib.util.module_from_spec(_spec)
sys.modules["_tma_f1_helpers"] = f1
_spec.loader.exec_module(f1)

ROWS, COLS = f1.ROWS, f1.COLS          # 3 x 2, position 6 empty
DIA = 120
H, W = 900, 700


def _centre(x, y):
    return 200 + (y - 1) * 250, 220 + (x - 1) * 260


def _slide(path):
    rng = np.random.default_rng(5)
    yy, xx = np.mgrid[0:H, 0:W]
    img = np.zeros((H, W), np.float32)
    for x in range(1, COLS + 1):
        for y in range(1, ROWS + 1):
            if (x - 1) * ROWS + y == 6:
                continue
            cy, cx = _centre(x, y)
            disc = (yy - cy) ** 2 + (xx - cx) ** 2 <= (DIA / 2) ** 2
            img[disc] = rng.uniform(20, 200, disc.sum())
    base = np.stack([img.astype(np.uint8), np.zeros((H, W), np.uint8)])
    with tifffile.TiffWriter(str(path), ome=True) as tw:
        tw.write(base, subifds=1, tile=(128, 128),
                 metadata={"axes": "CYX", "Channel": {"Name": ["DAPI", "CD8"]},
                           "PhysicalSizeX": 0.5, "PhysicalSizeY": 0.5})
        tw.write(base[:, ::2, ::2], subfiletype=1, tile=(128, 128))
    return str(path)


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def env(tmp_path, monkeypatch):
    from block01.core import provenance as prov
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "project_manifest.json").write_text(json.dumps({"version": 1, "project_schema_version": 1}))
    slide = _slide(tmp_path / "tma.ome.tif")
    xlsx = str(tmp_path / "map.xlsx")
    f1._workbook(xlsx)
    from block01.core.tma import map_sheet as ms
    real_parse = ms.parse

    def parse(p):
        s = real_parse(p)
        s.diameter_mm = DIA * 0.5 / 1000.0
        return s
    monkeypatch.setattr(ms, "parse", parse)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    ctx = {"project_dir": str(proj), "slide_path": slide, "slide_id": prov.ensure_raw_slide(str(proj), slide),
           "shape": (H, W), "pixel_um": (0.5, 0.5), "workspace_id": "full", "handoff_path": "/h.json",
           "handoff_sha256": "ab" * 32, "full_wsi": True, "channel_names": ["DAPI", "CD8"]}
    yield ctx, xlsx
    from block01.ui.tma import workers
    workers.shutdown(30000)
    a = QApplication.instance()
    for _ in range(20):
        a.processEvents()


def _wait(app, cond, ms=20000):
    import time
    t = time.time()
    while not cond():
        app.processEvents()
        if time.time() - t > ms / 1000:
            raise AssertionError("timed out")
        time.sleep(0.01)


class _Viewer:
    """A pyqtgraph view standing in for the Step1 viewer."""

    def __init__(self, page):
        from block01.ui.regions.overlay import RegionsOverlay
        self.graphics = pg.GraphicsLayoutWidget()
        self.view_box = pg.ViewBox(lockAspect=True, invertY=True)
        self.graphics.addItem(self.view_box)
        page._viewer_layout.addWidget(self.graphics, 1)
        self.view_box.setRange(QtCore.QRectF(0, 0, W, H), padding=0)
        self.overlay = RegionsOverlay(self.graphics, self.view_box, page.ctl)
        page.set_view(self.overlay)


def _page(app, env, monkeypatch, find=True):
    from block01.ui.regions.page import RegionsPage
    ctx, xlsx = env
    page = RegionsPage()
    page.resize(1300, 850)
    page.jump_box = lambda bbox: page.__dict__.setdefault("jumps", []).append(bbox)
    page.set_context(ctx)
    page.show()
    page.viewer = _Viewer(page)
    from PyQt5.QtWidgets import QFileDialog
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (xlsx, "")))
    if find:
        page.cores.btn_map.click()
        assert page.cores.study is not None
        page.cores.btn_find.click()
        _wait(app, lambda: not page.cores._detecting)
        assert page.holder is not None and len(page.holder.d["pieces"]) == 5
    return page


def _set_target(page, start_label, end_label, order_text):
    nb = page.cores.numbering
    nb.order.setCurrentIndex([nb.order.itemText(i) for i in range(nb.order.count())].index(order_text))
    for box, lab in ((nb.start, start_label), (nb.end, end_label)):
        box.setCurrentIndex(next(i for i in range(box.count()) if box.itemText(i).startswith(lab)))


def _label_of(page, uid):
    key = page.holder.d["cores"][uid]["position_key"]
    return page.cores.labels_by_key()[key]["label"] if key else None


def test_number_by_line_and_clicks_merge_undo_and_lock(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    assert all(len(p["polygon"]) >= 3 for p in page.holder.d["pieces"].values())      # outlines, not circles
    page._set_tool("line")
    _set_target(page, "X1-Y01", "X1-Y03", "Along column")
    assert page.ctl.press(*_centre(1, 1))
    page.ctl.drag(*_centre(1, 3), finish=True)
    assert page.cores.numbering.btn_confirm.isEnabled()
    page.cores.numbering.btn_confirm.click()
    assert sorted(filter(None, (_label_of(page, u) for u in page.holder.d["cores"]))) == ["X1-Y01", "X1-Y02", "X1-Y03"]
    page._set_tool("click")
    _set_target(page, "X2-Y01", "X2-Y02", "Along column")
    for y in (1, 2):
        page.ctl.click(*_centre(2, y))
    page._key("enter")                                                # ends; Confirm still commits
    page.ctl.click(*_centre(1, 1))
    assert len(page.ctl.sequence) == 2
    page.cores.numbering.btn_confirm.click()
    assert sum(1 for u in page.holder.d["cores"] if _label_of(page, u)) == 5
    # a drawn circle merged into X1-Y01, then undone
    page._set_tool("circle")
    cy, cx = _centre(1, 1)
    assert page.ctl.press(cy, cx + 100)
    page.ctl.drag(cy, cx + 110, finish=True)
    frag = page.selected
    assert page.holder.d["pieces"][frag]["source"] == "drawn"
    page._set_tool("select")
    page.ctl.click(cy, cx)
    core = page.selected_core()
    page._set_tool("merge")
    page.ctl.click(cy, cx + 100)
    assert page.holder.d["cores"][core]["mapping"] == "review"
    page.undo()
    assert page.holder.d["pieces"][frag]["owner"] is None
    page.edit(D.drop_piece, frag, "dust")
    monkeypatch.setattr(type(page), "_confirm_coverage", lambda self: True)
    page.btn_lock.click()
    assert page.view_version == ("regions", 1) and page.holder.read_only
    assert K.load_draft(page.ctx["project_dir"]) is None
    assert page.btn_lock.text() == "New draft from this version"
    page.btn_lock.click()
    assert page.view_version is None and page.holder.d["based_on"] == 1
    page.shutdown()


def test_select_drag_moves_only_the_selected_piece_else_pans(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    page._set_tool("select")
    assert page.ctl.press(5.0, 5.0) is False                          # glass: the view pans
    assert page.ctl.press(*_centre(1, 1)) is False                    # not selected yet: pans
    page.ctl.click(*_centre(1, 1))
    pid = page.selected
    before = [list(q) for q in page.holder.d["pieces"][pid]["polygon"]]
    assert page.ctl.press(*_centre(1, 1)) is True
    cy, cx = _centre(1, 1)
    page.ctl.drag(cy + 10, cx + 20, finish=True)
    after = page.holder.d["pieces"][pid]["polygon"]
    assert all(a[0] - b[0] == 20 and a[1] - b[1] == 10 for a, b in zip(after, before))
    page.undo()
    assert page.holder.d["pieces"][pid]["polygon"] == before          # one drag = one step
    page.shutdown()


def test_the_overlay_routes_a_left_drag_to_the_tool_or_to_the_view(app, env, monkeypatch):
    from PyQt5.QtGui import QMouseEvent
    page = _page(app, env, monkeypatch)
    ov = page.viewer.overlay
    vp = page.viewer.graphics.viewport()
    for _ in range(20):
        app.processEvents()

    def ev(kind, y, x, button=QtCore.Qt.LeftButton):
        pos = ov.to_widget(x, y)
        return QMouseEvent(kind, QtCore.QPointF(pos), button, button, QtCore.Qt.NoModifier)
    page._set_tool("select")
    assert ov.eventFilter(vp, ev(QtCore.QEvent.MouseButtonPress, 5, 5)) is False          # goes to pyqtgraph
    assert ov.eventFilter(vp, ev(QtCore.QEvent.MouseButtonRelease, 5, 5)) is False
    page._set_tool("circle")
    cy, cx = _centre(1, 1)
    assert ov.eventFilter(vp, ev(QtCore.QEvent.MouseButtonPress, cy, cx + 100)) is True    # the tool takes it
    assert ov.eventFilter(vp, ev(QtCore.QEvent.MouseMove, cy, cx + 115, QtCore.Qt.NoButton)) is True
    n = len(page.holder.d["pieces"])
    assert ov.eventFilter(vp, ev(QtCore.QEvent.MouseButtonRelease, cy, cx + 115)) is True
    assert len(page.holder.d["pieces"]) == n + 1
    page.shutdown()


def test_single_number_conflict_and_draft_restored_on_reopen(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    page._set_tool("select")
    page.ctl.click(*_centre(1, 1))
    page.cores.numbering.number.setText("X1-Y02")
    page.cores.numbering.btn_bind.click()
    uid = page.selected_core()
    assert _label_of(page, uid) == "X1-Y02"
    monkeypatch.setattr(QMessageBox, "exec_", lambda self: (setattr(self, "_clicked", self.buttons()[0]), 0)[1])
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: getattr(self, "_clicked", None))
    page.ctl.click(*_centre(1, 2))
    page.cores.numbering.number.setText("X1-Y02")
    page.cores.numbering.btn_bind.click()
    assert _label_of(page, page.selected_core()) == "X1-Y02" and _label_of(page, uid) is None
    page.flush_draft()
    from block01.ui.regions.page import RegionsPage
    again = RegionsPage()
    again.set_context(page.ctx)
    assert again.holder is not None and again.view_version is None
    assert again.holder.d["cores"] == page.holder.d["cores"]
    assert again.holder.d["session"]["workspace_id"] == "full"
    page.shutdown()


def test_an_roi_workspace_shows_the_cores_read_only(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    page.flush_draft()
    ctx = dict(page.ctx, full_wsi=False, workspace_id="roi_a")
    from block01.ui.regions.page import RegionsPage
    roi = RegionsPage()
    roi.set_context(ctx)
    assert roi.holder is not None and not roi.cores_editable()
    assert not roi.cores.btn_find.isEnabled() and not roi.cores.numbering.isEnabled()
    assert roi.cores.lbl_roi.isVisibleTo(roi.cores)
    roi._set_tool("line")
    assert roi.ctl.press(*_centre(1, 1)) is False
    page.shutdown()


def test_a_late_result_of_another_context_is_dropped_and_closing_waits(app, env, monkeypatch):
    from block01.ui.tma import workers
    page = _page(app, env, monkeypatch)
    gen = page.cores._gen
    page.set_context({})                                              # the dataset went away
    page.cores._detect_done(gen, page.cores.candidates or {"candidates": [], "version": 9})
    assert page.holder is None
    page.set_context(env[0])
    page.cores.btn_find.click()
    assert workers.LIVE
    assert page.shutdown()


def test_the_table_shows_the_map_sheet_before_find_cores(app, env, monkeypatch):
    page = _page(app, env, monkeypatch, find=False)
    page.cores.btn_map.click()
    assert page.cores.table.table.rowCount() == ROWS * COLS
    page.shutdown()


def test_prev_next_and_row_clicks_jump_the_shared_camera(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    page._set_tool("line")
    _set_target(page, "X1-Y01", "X1-Y03", "Along column")
    page.ctl.press(*_centre(1, 1))
    page.ctl.drag(*_centre(1, 3), finish=True)
    page.cores.numbering.btn_confirm.click()
    page.btn_next.click()
    page.btn_next.click()
    key = next(c["position_key"] for c in page.holder.d["cores"].values() if c["position_key"])
    page.cores.row_clicked("position", key, False)
    assert len(page.jumps) == 3 and all(len(b) == 4 for b in page.jumps)
    page.shutdown()


# ── R2 review fixes (codex_regions_r2_1.out) ───────────────────────────

def test_the_overlay_is_shown_and_stays_above_a_new_picture_layer(app, env, monkeypatch):
    from PyQt5.QtWidgets import QWidget
    page = _page(app, env, monkeypatch)
    ov = page.viewer.overlay
    assert ov.isVisible()
    layer = QWidget(ov.viewport())                                    # a new GL layer appears
    layer.show()
    layer.raise_()
    for _ in range(10):
        app.processEvents()
    kids = [w for w in ov.viewport().children() if isinstance(w, QWidget) and w.isVisible()]
    assert kids[-1] is ov
    page.shutdown()


def test_an_roi_workspace_cannot_edit_shapes(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    page.flush_draft()
    from block01.ui.regions.page import RegionsPage
    roi = RegionsPage()
    roi.set_context(dict(page.ctx, full_wsi=False, workspace_id="roi_a"))
    roi.set_view(page.viewer.overlay)
    roi._set_tool("select")
    roi.ctl.click(*_centre(1, 1))
    pid = roi.selected
    assert pid is not None                                            # selecting is fine
    before = json.dumps(roi.holder.d, sort_keys=True)
    assert roi.ctl.press(*_centre(1, 1)) is False                     # no move / scale
    roi._set_tool("circle")
    assert roi.ctl.press(*_centre(1, 1)) is False                     # no new piece
    roi._delete_selected()
    roi._set_tool("merge")
    roi.ctl.click(*_centre(1, 2))
    assert json.dumps(roi.holder.d, sort_keys=True) == before
    page.shutdown()


def test_a_click_on_a_core_adjusts_the_line_instead_of_starting_a_new_one(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    page._set_tool("line")
    _set_target(page, "X1-Y01", "X1-Y03", "Along column")
    assert page.ctl.press(*_centre(1, 1))
    page.ctl.drag(*_centre(1, 3), finish=True)
    assert len(page.ctl.sequence) == 3
    assert page.ctl.press(*_centre(1, 2)) is False                   # on a core: not a new line
    page.ctl.click(*_centre(1, 2))
    assert len(page.ctl.sequence) == 2 and page.ctl.line is not None
    page.shutdown()


def test_lock_needs_the_cores_once_a_map_sheet_is_chosen(app, env, monkeypatch):
    page = _page(app, env, monkeypatch, find=False)
    page.cores.btn_map.click()
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: shown.append(a[2])))
    page.btn_lock.click()
    assert shown and "Find cores first" in shown[0] and page.view_version is None
    page.shutdown()


def test_undo_keeps_the_live_session_and_unreadable_versions_are_refused(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    key = next(iter(page.holder.d["positions"]))
    page.edit(D.set_run, key, True)
    page.ctx["handoff_sha256"] = "cd" * 32                            # Step0 saved again meanwhile
    page._apply_session()
    page.undo()
    assert page.holder.d["session"]["handoff_sha256"] == "cd" * 32
    assert any(h["sha256"] == "cd" * 32 for h in page.holder.d["handoffs"].values())
    import os as _os
    bad = _os.path.join(page.ctx["project_dir"], "tma", "regions_v7.json")
    with open(bad, "w") as f:
        json.dump({"schema_version": 2, "kind": "regions_layout", "slide_id": "slide_ffffffffffffffff"}, f)
    assert page._open_locked(("regions", 7)) is False                 # another slide: refused, no crash
    page.shutdown()


def test_another_workspace_starts_a_fresh_undo_history(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    page._set_tool("select")
    page.ctl.click(*_centre(1, 1))
    page._delete_selected()                                           # a whole-slide core edit
    assert page.holder.can_undo
    page.set_context(dict(page.ctx, full_wsi=False, workspace_id="roi_a"))
    assert not page.holder.can_undo and not page.btn_undo.isEnabled()
    page.shutdown()


def test_a_handle_drag_scales_about_the_centre_and_autosave_waits_for_a_pause(app, env, monkeypatch):
    from block01.core.regions import geometry as G
    from block01.core.regions import lock as KK
    page = _page(app, env, monkeypatch)
    page.flush_draft()
    writes = []
    real = KK.save_draft
    monkeypatch.setattr(KK, "save_draft", lambda p, d: (writes.append(1), real(p, d))[1])
    page._set_tool("select")
    cy, cx = _centre(1, 1)
    page.ctl.click(cy, cx)
    pid = page.selected
    poly = page.holder.d["pieces"][pid]["polygon"]
    a0, c0 = G.area(poly), G.centroid(poly)
    xs = [q[0] for q in poly]
    ys = [q[1] for q in poly]
    corner = (min(ys), max(xs))
    monkeypatch.setattr(page.ctl, "handle_at", lambda y, x: True)
    assert page.ctl.press(*corner)
    page.ctl.drag(c0[0] + (corner[0] - c0[0]) * 1.2, c0[1] + (corner[1] - c0[1]) * 1.2, finish=True)
    poly2 = page.holder.d["pieces"][pid]["polygon"]
    assert abs(G.area(poly2) / a0 - 1.44) < 0.05
    assert abs(G.centroid(poly2)[0] - c0[0]) < 2 and abs(G.centroid(poly2)[1] - c0[1]) < 2
    assert writes == []
    _wait(app, lambda: writes, ms=3000)
    assert len(writes) == 1
    page.shutdown()


def test_a_late_find_cores_result_does_not_touch_an_roi_workspace(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    gen = page.cores._gen
    rec = page.cores.candidates
    page.set_context(dict(page.ctx, full_wsi=False, workspace_id="roi_a"))
    before = json.dumps(page.holder.d["pieces"], sort_keys=True)
    page.cores._detect_done(gen, rec)                                 # the old workspace's job
    page.cores._detect_done(page.cores._gen, rec)                     # even a current one: ROI is read-only
    assert json.dumps(page.holder.d["pieces"], sort_keys=True) == before
    assert not page.holder.can_undo
    page.shutdown()


def test_the_outlines_can_be_hidden_and_shown(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    ov = page.viewer.overlay
    assert ov.items
    page.btn_outlines.setChecked(False)
    assert ov.items == []
    page._set_tool("line")
    _set_target(page, "X1-Y01", "X1-Y03", "Along column")
    page.ctl.press(*_centre(1, 1))
    page.ctl.drag(*_centre(1, 3), finish=True)
    assert ov.line is not None and ov.items == []                     # the line still shows
    page.btn_outlines.setChecked(True)
    assert ov.items
    page.shutdown()


def test_hidden_outlines_are_neither_picked_nor_edited(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    page._set_tool("select")
    page.ctl.click(*_centre(1, 1))
    assert page.selected is not None
    page.btn_outlines.setChecked(False)
    assert page.selected is None
    page.ctl.click(*_centre(1, 1))
    assert page.selected is None
    before = json.dumps(page.holder.d["pieces"], sort_keys=True)
    assert page.ctl.press(*_centre(1, 1)) is False                    # the view pans
    page._set_tool("circle")
    assert page.ctl.press(*_centre(1, 1)) is False
    assert json.dumps(page.holder.d["pieces"], sort_keys=True) == before
    page.shutdown()
