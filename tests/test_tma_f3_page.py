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


def _drag(page, a, b, steps=4, ctrl=False):
    """A left drag on the viewer, as the overlay feeds it to the tool."""
    assert page.ctl.press(*a, ctrl) is True
    for k in range(1, steps + 1):
        f = k / steps
        page.ctl.drag(a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, finish=k == steps)
    page.ctl.released()                                               # the button comes up (the overlay says so)


def _merge(page, mode="Group"):
    """Merge ▾ -> an item (the button only opens its menu)."""
    act = next(a for a in page.btn_merge.menu().actions() if a.text().startswith(mode))
    act.trigger()


def _line(page, a, b):
    page._set_tool("line")
    _drag(page, _centre(*a), _centre(*b))


def test_number_by_line_and_clicks_merge_undo_and_lock(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    assert all(len(p["polygon"]) >= 3 for p in page.holder.d["pieces"].values())      # outlines, not circles
    assert page.ctl.tool is None and not any(b.isChecked() for b in page.family_buttons.values())
    _set_target(page, "X1-Y01", "X1-Y03", "Along column")
    _line(page, (1, 1), (1, 3))                                       # a drag along the column
    assert page.cores.numbering.btn_confirm.isEnabled()
    page.cores.numbering.btn_confirm.click()
    assert sorted(filter(None, (_label_of(page, u) for u in page.holder.d["cores"]))) == ["X1-Y01", "X1-Y02", "X1-Y03"]
    page._set_tool("click")
    assert page.family_buttons["Annotate"].text() == "Annotate: Clicks ▾"
    _set_target(page, "X2-Y01", "X2-Y02", "Along column")
    for y in (1, 2):
        page.ctl.click(*_centre(2, y))
    page._key("enter")                                                # ends; Confirm still commits
    page.ctl.click(*_centre(1, 1))
    assert len(page.ctl.sequence) == 2
    page.cores.numbering.btn_confirm.click()
    assert sum(1 for u in page.holder.d["cores"] if _label_of(page, u)) == 5
    # a circle drawn by a drag, merged into X1-Y01 with the Merge button, then undone
    page._set_tool("circle")
    cy, cx = _centre(1, 1)
    _drag(page, (cy - 10, cx + 90), (cy + 10, cx + 110))              # one corner, then the opposite
    frag = page.selected
    assert page.holder.d["pieces"][frag]["source"] == "drawn"
    drawn = page.holder.d["pieces"][frag]["owner"]                    # a core of its own: yellow
    assert drawn and page.holder.d["cores"][drawn]["mapping"] == "unmatched"
    assert next(it for it in page.viewer.overlay.items if it["id"] == frag)["color"] == "#fdd835"
    page._set_tool("select")
    page.ctl.click(cy, cx)
    core = page.selected_core()
    assert not page.btn_merge.isEnabled()                             # one piece: nothing to merge
    page.ctl.click(cy, cx + 100, ctrl=True)                           # Ctrl+click adds the fragment
    assert page.selection[-1] == frag and page.selected is None and page.btn_merge.isEnabled()
    _merge(page)
    assert page.holder.d["pieces"][frag]["owner"] == core
    assert page.holder.d["cores"][core]["mapping"] == "review"
    page.undo()
    own = page.holder.d["pieces"][frag]["owner"]                      # back to its own core (drawn = a core)
    assert own != core and page.holder.d["cores"][own]["pieces"] == [frag]
    page.select(frag)
    page._delete_selected()                                           # Delete: the piece is gone
    assert frag not in page.holder.d["pieces"]
    page.btn_save.click()                                             # Save: any time, editing goes on
    rec = K.load_saved(page.ctx["project_dir"])
    assert rec["save_count"] == 1 and not page.holder.read_only
    assert page.lbl_saved.text().startswith("Saved ")
    key = next(iter(page.holder.d["positions"]))
    page.edit(D.set_run, key, not page.holder.d["positions"][key]["run"])
    assert "unsaved changes" in page.lbl_saved.text()
    page.btn_save.click()
    assert K.load_saved(page.ctx["project_dir"])["save_count"] == 2 and "unsaved" not in page.lbl_saved.text()
    page.shutdown()


def test_select_drag_moves_the_selected_piece_as_a_preview_until_enter(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    page._set_tool("select")
    assert page.ctl.press(5.0, 5.0) is False                          # glass: the view pans
    assert page.ctl.press(*_centre(1, 1)) is True and page.ctl._drag is None   # not selected yet: a click
    page.ctl.released()
    page.ctl.click(*_centre(1, 1))
    pid = page.selected
    before = [list(q) for q in page.holder.d["pieces"][pid]["polygon"]]
    cy, cx = _centre(1, 1)
    _drag(page, (cy, cx), (cy + 10, cx + 20))
    _drag(page, (cy + 10, cx + 20), (cy + 20, cx + 40))               # a second drag, same transform
    assert page.holder.d["pieces"][pid]["polygon"] == before          # the document waits for Enter
    shown = page.viewer.overlay.live
    assert shown["id"] == pid and shown["dash"] and shown["polygon"] != before   # a dashed live preview
    assert pid not in [it["id"] for it in page.viewer.overlay.items]  # not in the cached layer
    page.undo()                                                       # Ctrl+Z: back one drag
    assert page.ctl.transform is not None and page.ctl.transform.history
    page._key("enter")
    after = page.holder.d["pieces"][pid]["polygon"]
    assert all(a[0] - b[0] == 20 and a[1] - b[1] == 10 for a, b in zip(after, before))
    page.undo()
    assert page.holder.d["pieces"][pid]["polygon"] == before          # the whole transform = one step
    _drag(page, (cy, cx), (cy + 30, cx + 30))
    page._escape()                                                    # Esc: dropped, the tool stays
    assert page.ctl.transform is None and page.ctl.tool == "select"
    assert page.holder.d["pieces"][pid]["polygon"] == before
    page.shutdown()


def test_the_overlay_routes_a_left_drag_to_the_tool_or_to_the_view(app, env, monkeypatch):
    from PyQt5.QtGui import QMouseEvent
    page = _page(app, env, monkeypatch)
    ov = page.viewer.overlay
    vp = page.viewer.graphics.viewport()
    for _ in range(20):
        app.processEvents()

    def ev(kind, y, x, button=QtCore.Qt.LeftButton, buttons=None):
        pos = ov.to_widget(x, y)
        return QMouseEvent(kind, QtCore.QPointF(pos), button, button if buttons is None else buttons,
                           QtCore.Qt.NoModifier)
    assert ov.eventFilter(vp, ev(QtCore.QEvent.MouseButtonPress, 5, 5)) is False          # no tool: pans
    ov.eventFilter(vp, ev(QtCore.QEvent.MouseButtonRelease, 5, 5))
    page._set_tool("select")
    assert ov.eventFilter(vp, ev(QtCore.QEvent.MouseButtonPress, 5, 5)) is False          # blank ground: pans
    assert ov.eventFilter(vp, ev(QtCore.QEvent.MouseButtonRelease, 5, 5)) is False
    page._set_tool("circle")
    cy, cx = _centre(1, 1)
    n = len(page.holder.d["pieces"])
    assert ov.eventFilter(vp, ev(QtCore.QEvent.MouseButtonPress, cy, cx + 100)) is True    # Circle takes it
    ov.eventFilter(vp, ev(QtCore.QEvent.MouseMove, cy + 12, cx + 112, QtCore.Qt.NoButton, QtCore.Qt.LeftButton))
    assert page.viewer.overlay.circle is not None                                         # the preview follows
    assert ov.eventFilter(vp, ev(QtCore.QEvent.MouseButtonRelease, cy + 15, cx + 115)) is True
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
    assert again.holder is not None and not again.holder.read_only
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
    assert roi.ctl.press(*_centre(1, 1)) is True and roi.ctl._drag is None      # no line here
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


def test_row_clicks_jump_the_shared_camera(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    assert not hasattr(page, "btn_next") and not hasattr(page, "btn_outlines")        # removed (user 2026-10-18)
    _set_target(page, "X1-Y01", "X1-Y03", "Along column")
    _line(page, (1, 1), (1, 3))
    page.cores.numbering.btn_confirm.click()
    key = next(c["position_key"] for c in page.holder.d["cores"].values() if c["position_key"])
    page.cores.row_clicked("position", key, False)
    assert len(page.jumps) == 1 and len(page.jumps[0]) == 4 and page.selected is not None
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
    roi.ctl.click(*_centre(1, 2), ctrl=True)
    assert len(roi.selection) == 2 and not roi.btn_merge.isEnabled()
    before = json.dumps(roi.holder.d, sort_keys=True)
    roi.select(pid)
    assert roi.ctl.press(*_centre(1, 1)) is True and roi.ctl._drag is None   # a click, no move / scale
    roi._set_tool("circle")
    roi.ctl.press(*_centre(1, 1))
    roi.ctl.drag(*_centre(1, 2), finish=True)                         # no new piece
    roi.ctl.released()
    roi._delete_selected()
    roi.merge_selected()
    assert json.dumps(roi.holder.d, sort_keys=True) == before
    page.shutdown()


def test_a_click_on_a_core_adjusts_the_line_instead_of_starting_a_new_one(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    _set_target(page, "X1-Y01", "X1-Y03", "Along column")
    _line(page, (1, 1), (1, 3))
    assert len(page.ctl.sequence) == 3
    assert page.ctl.press(*_centre(1, 2)) is True
    page.ctl.drag(*_centre(1, 2), finish=True)
    page.ctl.released()
    page.ctl.click(*_centre(1, 2))                                    # on a core of the finished line
    assert len(page.ctl.sequence) == 2 and page.ctl.line is not None
    page.shutdown()


def test_save_works_before_find_cores_and_lists_it_as_open(app, env, monkeypatch):
    page = _page(app, env, monkeypatch, find=False)
    page.cores.btn_map.click()
    page.btn_save.click()                                             # never refused
    rec = K.load_saved(page.ctx["project_dir"])
    assert any("cores are not found yet" in p for p in rec["open_issues"]) and not rec["complete"]
    assert "open" in page.lbl_saved.text() and "cores are not found yet" in page.lbl_saved.toolTip()
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
    pd = page.ctx["project_dir"]
    for name in ("regions.json", "regions_v7.json"):                  # another slide's files
        with open(_os.path.join(pd, "tma", name), "w") as f:
            json.dump({"schema_version": 2, "kind": "regions_layout", "slide_id": "slide_ffffffffffffffff",
                       "document": {}}, f)
    page.flush_draft()
    K.discard_draft(pd)
    from block01.ui.regions.page import RegionsPage
    again = RegionsPage()
    again.set_context(page.ctx)                                       # refused with a message, no crash
    assert again.holder is not None and again.holder.d["slide_id"] == page.ctx["slide_id"]
    assert "cannot be read" in again.cores.lbl_info.text()
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


def test_the_transform_box_works_as_photoshops_and_autosave_waits_for_enter(app, env, monkeypatch):
    import numpy as np
    from block01.core.regions import geometry as G
    from block01.core.regions import lock as KK
    from block01.core.regions import transform as T
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
    q = T.box_quad(poly)
    (x0, y0), (x1, y1) = q[0], q[2]
    assert page.viewer.overlay.handles_for is not None
    # the bottom-right corner, freely: the top-left corner stays put
    _drag(page, (y1, x1), (y1 + 30, x1 + 10))
    p2 = np.array(page.ctl.transform.preview())
    assert abs(p2[:, 0].min() - x0) < 1 and abs(p2[:, 1].min() - y0) < 1
    assert abs(p2[:, 0].max() - (x1 + 10)) < 1 and abs(p2[:, 1].max() - (y1 + 30)) < 1
    page.undo()
    # Shift keeps the ratio, Alt scales about the centre
    from PyQt5.QtCore import Qt
    assert page.ctl.press(y1, x1)
    page.ctl.drag(y1 + 40, x1 + 40, finish=True, mods=int(Qt.ShiftModifier | Qt.AltModifier))
    page.ctl.released()
    p3 = np.array(page.ctl.transform.preview())
    w0, h0 = x1 - x0, y1 - y0
    w3, h3 = np.ptp(p3[:, 0]), np.ptp(p3[:, 1])
    assert abs(w3 / w0 - h3 / h0) < 0.02 and w3 > w0
    assert abs(p3[:, 0].mean() - np.array(poly)[:, 0].mean()) < 2
    page.undo()
    # just outside a corner: a rotation about the centre (Shift: 15 degrees)
    tol = 8 * page.view.screen_px()
    assert page.ctl.box_hit(y0 - 2 * tol, x0 - 2 * tol) == ("rotate", 0)
    assert page.ctl.press(y0 - 2 * tol, x0 - 2 * tol)
    page.ctl.drag(y0 - 2 * tol, x1 + 4 * tol, finish=True, mods=int(Qt.ShiftModifier))
    page.ctl.released()
    th = page.ctl.transform.theta
    assert abs(th) > 0.1 and abs(th / np.radians(15) - round(th / np.radians(15))) < 1e-9
    # rotated, an edge handle still scales along the box's own side
    quad = page.ctl.transform.quad
    right = T.handle_points(quad)[5]
    assert page.ctl.press(right[1], right[0])
    page.ctl.drag(right[1] + 20 * np.sin(th), right[0] + 20 * np.cos(th), finish=True)
    page.ctl.released()
    q2 = page.ctl.transform.quad
    assert np.allclose(q2[0], quad[0]) and np.allclose(q2[3], quad[3])  # the left side stays
    # Ctrl on a corner: only that corner moves (distort)
    c = q2[2]
    assert page.ctl.press(c[1], c[0], True)
    page.ctl.drag(c[1] + 15, c[0] + 5, finish=True)
    page.ctl.released()
    q3 = page.ctl.transform.quad
    assert np.allclose(q3[:2], q2[:2]) and np.allclose(q3[3], q2[3]) and not np.allclose(q3[2], q2[2])
    a0 = G.area(poly)
    assert writes == []
    page._key("enter")
    assert page.ctl.transform is None and G.area(page.holder.d["pieces"][pid]["polygon"]) != a0
    _wait(app, lambda: writes, ms=3000)
    assert len(writes) == 1
    page.shutdown()


def test_a_pending_transform_is_applied_before_anything_else(app, env, monkeypatch):
    from block01.core.regions import document as D
    page = _page(app, env, monkeypatch)
    page._set_tool("select")
    cy, cx = _centre(1, 1)
    page.ctl.click(cy, cx)
    pid = page.selected
    before = [list(q) for q in page.holder.d["pieces"][pid]["polygon"]]
    _drag(page, (cy, cx), (cy + 10, cx + 10))
    page.ctl.click(*_centre(1, 2))                                    # another selection
    assert page.holder.d["pieces"][pid]["polygon"] != before and page.ctl.transform is None
    page.select(pid)
    _drag(page, (cy + 10, cx + 10), (cy + 20, cx + 20))
    key = next(iter(page.holder.d["positions"]))
    page.edit(D.set_run, key, True)                                   # any edit
    assert page.ctl.transform is None
    page.undo()                                                       # the Run tick
    page.undo()                                                       # the second move
    assert page.holder.d["pieces"][pid]["polygon"][0][0] == before[0][0] + 10
    _drag(page, (cy + 10, cx + 10), (cy + 30, cx + 30))
    page.frame.left_tabs.setCurrentWidget(page.exclusions)           # a tab switch
    assert page.holder.d["pieces"][pid]["polygon"][0][0] == before[0][0] + 30
    page.frame.left_tabs.setCurrentWidget(page.cores)
    page.select(pid)
    _drag(page, (cy + 30, cx + 30), (cy + 40, cx + 40))
    page.set_context(dict(page.ctx, full_wsi=False, workspace_id="roi_a"))   # another workspace
    assert page.holder.d["pieces"][pid]["polygon"][0][0] == before[0][0] + 40
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


def test_line_and_circle_drags_hold_the_view_and_the_wheel_zooms_between_them(app, env, monkeypatch):
    """User 2026-10-18: Line and Circle are drags; while one is lit a left
    drag never pans, the wheel zooms unless a drag is under way (a Line
    drag's wheel sets the band width); Select / no tool pans again."""
    page = _page(app, env, monkeypatch)
    _set_target(page, "X1-Y01", "X1-Y03", "Along column")
    page._set_tool("line")
    assert page.ctl.wheel(120, dragging=False) is False               # zooms
    assert page.ctl.press(*_centre(1, 1)) is True                     # the drag is the line's
    page.ctl.drag(*_centre(1, 2))
    assert page.ctl.line is not None and len(page.ctl.sequence) == 2
    band = page.ctl.band
    assert page.ctl.wheel(120, dragging=True) is True and page.ctl.band > band
    page.ctl.drag(*_centre(1, 3), finish=True)
    page.ctl.released()
    assert len(page.ctl.sequence) == 3
    assert page.ctl.press(5.0, 5.0) is True                           # still no pan with Line lit
    page.ctl.drag(5.0, 5.0, finish=True)
    page.ctl.released()
    page._set_tool("circle")
    assert page.ctl.press(400.0, 600.0) is True
    page.ctl.drag(440.0, 640.0)
    assert page.ctl.wheel(120, dragging=True) is True                 # swallowed mid-drag
    page._escape()                                                    # Esc: the circle goes, the tool off
    assert page.ctl.preview_circle() is None and page.ctl.tool is None
    page.ctl.drag(400.0, 650.0, finish=True)                          # the late release commits nothing
    page.ctl.released()
    n = len(page.holder.d["pieces"])
    assert n == 5 and page.ctl.press(5.0, 5.0) is False               # no tool: pans
    page.shutdown()


def test_esc_cancels_a_line_and_its_preview_and_ctrl_z_steps_back_one_level(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    _set_target(page, "X1-Y01", "X1-Y03", "Along column")
    _line(page, (1, 1), (1, 3))
    assert page.ctl.sequence
    page._escape()
    assert page.ctl.line is None and page.ctl.sequence == [] and page.ctl.tool is None
    assert not page.cores.numbering.btn_confirm.isEnabled()
    # Ctrl+Z: the half-done drag, then the unconfirmed preview, then the last edit; the tool stays lit
    key = next(iter(page.holder.d["positions"]))
    page.edit(D.set_run, key, True)
    _line(page, (1, 1), (1, 3))
    assert page.ctl.press(*_centre(2, 1)) is True
    page.ctl.drag(*_centre(2, 2))
    assert page.ctl.busy()
    page.undo()
    assert not page.ctl.busy() and page.ctl.tool == "line"
    page.ctl.drag(*_centre(2, 2), finish=True)                        # the release after it: nothing
    page.ctl.released()
    _line(page, (1, 1), (1, 3))
    assert page.ctl.has_preview()
    page.undo()
    assert not page.ctl.has_preview() and page.holder.d["positions"][key]["run"] and page.ctl.tool == "line"
    page.undo()
    assert not page.holder.d["positions"][key]["run"] and page.ctl.tool == "line"
    page.frame.left_tabs.setCurrentWidget(page.exclusions)
    assert page.ctl.tool is None                                      # numbering is the Cores tab's
    page._set_tool("select")
    page.ctl.click(*_centre(1, 1))                                    # a core, in the Exclusions tab
    assert "Cores tab" in page.exclusions.lbl_info.text()
    page.shutdown()


