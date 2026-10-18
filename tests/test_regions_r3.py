"""Regions R3 (docs/region_qc_module_plan.md §3, §4, §5 R3): the Polygon and
Vertex tools and blur / fold exclusion annotation on the Regions page --
drawn on the viewer while the Exclusions tab is open, edited with Select /
Vertex, reasons / channels / note in the tab (one undo step each), deleted
with Delete; editable in an ROI workspace (where the cores are read-only);
kept when the cores are replaced; locked with the link to the core they lie
in. Offscreen, with the page tests' synthetic project and stand-in viewer.
"""

import json

import pytest

from test_tma_f3_page import _centre, _drag, _line, _merge, _page, _set_target, app, env  # noqa: F401

from block01.core.regions import geometry as G  # noqa: E402
from block01.core.regions import lock as K  # noqa: E402


def _open_exclusions(page):
    page.frame.left_tabs.setCurrentWidget(page.exclusions)
    assert page.exclusions_tab_open()


def _square(page, y0, x0, s):
    page._set_tool("polygon")
    for y, x in ((y0, x0), (y0, x0 + s), (y0 + s, x0 + s), (y0 + s, x0)):
        page.ctl.click(y, x)


def test_a_polygon_drawn_in_the_exclusions_tab_is_an_exclusion_with_the_defaults(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    _open_exclusions(page)
    page.exclusions.cb["fold"].setChecked(True)
    page.exclusions.cb["blur"].setChecked(False)
    n_pieces = len(page.holder.d["pieces"])
    _square(page, 100, 100, 80)
    assert page.viewer.overlay.draft and len(page.viewer.overlay.draft) == 4
    page._key("backspace")
    assert len(page.ctl.poly_pts) == 3
    page.ctl.click(180, 100)
    page._key("enter")                                                # closes it
    ex = page.holder.d["exclusions"]
    assert len(ex) == 1 and len(page.holder.d["pieces"]) == n_pieces
    e = next(iter(ex.values()))
    assert e["reasons"] == ["fold"] and e["channels"] == "all" and len(e["polygon"]) == 4
    assert page.selected in ex and page.exclusions.table.rowCount() == 1
    items = {it["id"]: it for it in page.viewer.overlay.items}
    assert items[page.selected]["hatch"]
    page._set_tool("polygon")
    page.ctl.click(10, 10)
    page.ctl.click(10, 40)
    page.undo()                                                       # Ctrl+Z: the polygon in progress goes
    assert page.ctl.poly_pts == [] and len(ex) == 1 and page.ctl.tool == "polygon"
    page.ctl.click(10, 10)
    page._escape()                                                    # Esc: too, and the tool goes off
    assert page.ctl.poly_pts == [] and len(ex) == 1 and page.ctl.tool is None
    page.shutdown()


def test_clicking_the_first_vertex_closes_and_a_circle_works_too(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    _open_exclusions(page)
    _square(page, 300, 300, 60)
    page.ctl.click(301, 301)                                          # on the first vertex
    assert len(page.holder.d["exclusions"]) == 1
    page._set_tool("circle")
    assert page.ctl.press(600, 500)
    page.ctl.drag(620, 520)
    page.ctl.drag(640, 540, finish=True)
    page.ctl.released()
    assert len(page.holder.d["exclusions"]) == 2
    page.shutdown()


def test_select_vertex_reasons_channels_and_delete_each_one_undo_step(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    _open_exclusions(page)
    _square(page, 100, 100, 80)
    page._key("enter")
    uid = page.selected
    poly0 = [list(q) for q in page.holder.d["exclusions"][uid]["polygon"]]
    page._set_tool("select")
    page.ctl.click(140, 140)
    assert page.ctl.press(140, 140)
    page.ctl.drag(150, 160, finish=True)                              # moved by (10, 20)
    page.ctl.released()
    page._key("enter")                                                # the transform applied
    poly1 = page.holder.d["exclusions"][uid]["polygon"]
    assert all(b[0] - a[0] == 20 and b[1] - a[1] == 10 for a, b in zip(poly0, poly1))
    page._set_tool("vertex")
    page.ctl.click(150, 160)
    vx, vy = poly1[0]
    monkeypatch.setattr(page.ctl, "vertex_at", lambda y, x: 0)
    assert page.ctl.press(vy, vx)
    page.ctl.drag(vy - 30, vx - 30, finish=True)
    page.ctl.released()
    assert page.holder.d["exclusions"][uid]["polygon"][0] == [vx - 30, vy - 30]
    page.exclusions.cb["other"].setChecked(True)                      # an edit of the selected one
    assert page.holder.d["exclusions"][uid]["reasons"] == ["blur", "other"]
    page.exclusions.set_channel_choice(["CD8"])
    assert page.holder.d["exclusions"][uid]["channels"] == ["CD8"]
    page.exclusions.cb["blur"].setChecked(False)
    page.exclusions.cb["other"].setChecked(False)                     # the last reason cannot go
    assert page.holder.d["exclusions"][uid]["reasons"] == ["other"]
    assert page.exclusions.cb["other"].isChecked()                    # and the box shows it again
    page.undo()                                                       # blur back
    assert page.holder.d["exclusions"][uid]["reasons"] == ["blur", "other"]
    page.undo()                                                       # channels back
    assert page.holder.d["exclusions"][uid]["channels"] == "all"
    page._delete_selected()
    assert uid not in page.holder.d["exclusions"]
    page.undo()
    assert uid in page.holder.d["exclusions"]
    page.shutdown()


def test_an_roi_workspace_draws_exclusions_but_not_cores(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    page.set_context(dict(page.ctx, full_wsi=False, workspace_id="roi_a"))
    assert not page.cores_editable()
    page.frame.left_tabs.setCurrentWidget(page.cores)
    n = len(page.holder.d["pieces"])
    _square(page, 100, 100, 50)
    page._key("enter")
    assert len(page.holder.d["pieces"]) == n                          # cores tab: nothing drawn
    _open_exclusions(page)
    _square(page, 100, 100, 50)
    page._key("enter")
    e = next(iter(page.holder.d["exclusions"].values()))
    assert e["created_in_workspace"] == "roi_a"
    page.shutdown()


def test_exclusions_stay_when_the_cores_are_replaced_and_save_with_their_core(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    _open_exclusions(page)
    cy, cx = _centre(1, 1)
    _square(page, cy - 20, cx - 20, 40)                               # inside core X1-Y01
    page._key("enter")
    _square(page, 850, 650, 30)                                       # on glass
    page._key("enter")
    page.frame.left_tabs.setCurrentWidget(page.cores)
    page.cores.btn_find.click()                                       # new candidates replace the cores
    from test_tma_f3_page import _wait
    _wait(app, lambda: not page.cores._detecting)
    assert len(page.holder.d["exclusions"]) == 2
    # number everything, save
    _set_target(page, "X1-Y01", "X1-Y03", "Along column")
    _line(page, (1, 1), (1, 3))
    page.cores.numbering.btn_confirm.click()
    _set_target(page, "X2-Y01", "X2-Y02", "Along column")
    _line(page, (2, 1), (2, 2))
    page.cores.numbering.btn_confirm.click()
    page.btn_save.click()
    rec = K.load_saved(page.ctx["project_dir"])
    assert rec["complete"], rec["open_issues"]
    links = sorted(bool(e["core_uid"]) for e in rec["exclusions"].values())
    assert links == [False, True]
    assert all(e["type"] == "blur" and e["region_id"].startswith("reg_") for e in rec["exclusions"].values())
    page.shutdown()


def test_a_row_click_jumps_to_the_exclusion(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    _open_exclusions(page)
    _square(page, 100, 100, 80)
    page._key("enter")
    page.selected = None
    page.exclusions._row_clicked(0, 0)
    assert page.selected in page.holder.d["exclusions"] and page.jumps
    y0, y1, x0, x1 = page.jumps[-1]
    assert y0 < 100 and y1 > 180 and x0 < 100 and x1 > 180
    page.shutdown()


# ── R3 review fixes (codex_regions_r3_1.out) ───────────────────────────

def test_delete_only_touches_the_open_tabs_shape(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    _open_exclusions(page)
    pid = next(iter(page.holder.d["pieces"]))
    page.select(pid)                                                  # a core piece selected (a row click)
    assert pid in page.holder.d["pieces"]
    page._delete_selected()
    assert pid in page.holder.d["pieces"]
    page.shutdown()


def test_a_tool_switch_shows_the_selected_exclusions_handles(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    _open_exclusions(page)
    page._set_tool("polygon")
    page.ctl.click(100, 100)
    page.ctl.click(100, 180)
    page.ctl.click(180, 180)
    page._key("enter")
    uid = page.selected
    page._set_tool("select")
    page.select(uid)
    assert page.viewer.overlay.handles_for is not None and page.viewer.overlay.vertices_for is None
    page._set_tool("vertex")
    assert page.viewer.overlay.handles_for is None and page.viewer.overlay.vertices_for is not None
    page.shutdown()


def test_deselecting_shows_the_defaults_again_and_drags_are_logged(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    _open_exclusions(page)
    page.exclusions.cb["fold"].setChecked(True)                       # defaults: blur + fold
    _square(page, 100, 100, 80)
    page._key("enter")
    uid = page.selected
    page.exclusions.cb["blur"].setChecked(False)                      # the selected one: fold only
    page._set_tool("select")
    page.ctl.click(5, 5)                                              # blank: nothing selected
    assert page.selected is None
    assert page.exclusions.lbl_target.text() == "For the next exclusion:"
    assert page.exclusions.reasons() == ["blur", "fold"]
    assert page.exclusions.table.selectedItems() == []
    page.select(uid)
    n = len(page.holder.d["operations"])
    assert page.ctl.press(140, 140)
    page.ctl.drag(150, 150, finish=True)
    page.ctl.released()
    assert len(page.holder.d["operations"]) == n                      # a preview until Enter
    page._key("enter")
    op = page.holder.d["operations"][-1]
    assert len(page.holder.d["operations"]) == n + 1 and op["kind"] == "reshape" and op["workspace_id"] == "full"
    page.shutdown()


def test_revert_to_saved_drops_later_changes_and_a_reopen_keeps_unsaved_work(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    _open_exclusions(page)
    assert not page.btn_revert.isEnabled() and page.lbl_saved.text() == "Not saved yet"
    _square(page, 700, 500, 60)
    page._key("enter")
    page.btn_save.click()
    _square(page, 300, 300, 60)
    page._key("enter")
    assert len(page.holder.d["exclusions"]) == 2
    page.flush_draft()
    from block01.ui.regions.page import RegionsPage
    again = RegionsPage()                                             # a reopen: the working copy
    again.set_context(page.ctx)
    assert len(again.holder.d["exclusions"]) == 2 and "unsaved changes" in again.lbl_saved.text()
    page._revert_to_saved()
    assert len(page.holder.d["exclusions"]) == 1 and "unsaved" not in page.lbl_saved.text()
    page.shutdown()


def test_an_old_locked_version_is_the_starting_point_when_nothing_was_saved(app, env, monkeypatch):
    import os
    page = _page(app, env, monkeypatch)
    pd = page.ctx["project_dir"]
    _open_exclusions(page)
    _square(page, 700, 500, 60)
    page._key("enter")
    rec = K.save(pd, page.holder.d, page.cores.study)
    old = dict(rec, version=3, based_on=None)                         # as an earlier build's lock
    old.pop("document")
    with open(K.version_path(pd, 3), "w") as f:
        json.dump(old, f)
    os.remove(K.saved_path(pd))
    page.flush_draft()
    K.discard_draft(pd)
    from block01.ui.regions.page import RegionsPage
    again = RegionsPage()
    again.set_context(page.ctx)
    assert len(again.holder.d["exclusions"]) == 1 and not again.holder.read_only
    assert again.lbl_saved.text() == "Not saved yet"
    page.shutdown()


# ── the tool row redesign (docs/regions_tools_redesign.md §9) ───────────

def test_marquees_select_by_centre_and_delete_removes_several_in_one_step(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    pieces = page.holder.d["pieces"]
    centres = {pid: G.centroid(p["polygon"]) for pid, p in pieces.items()}
    a, b = _centre(1, 1), _centre(1, 2)
    page._set_tool("select_rect")
    assert page.family_buttons["Select"].text() == "Select: Rectangle ▾"
    lo = (min(a[0], b[0]) - 5, min(a[1], b[1]) - 5)
    hi = (max(a[0], b[0]) + 5, max(a[1], b[1]) + 5)
    _drag(page, lo, hi)
    want = sorted(pid for pid, (cy, cx) in centres.items() if lo[0] <= cy <= hi[0] and lo[1] <= cx <= hi[1])
    assert sorted(page.selection) == want and len(want) == 2
    page._set_tool("select_ellipse")
    c = _centre(2, 1)
    _drag(page, (c[0] - 20, c[1] - 20), (c[0] + 20, c[1] + 20), ctrl=True)    # Ctrl adds
    assert len(page.selection) == 3
    page._set_tool("select_polygon")
    for y, x in ((lo[0], lo[1]), (lo[0], hi[1]), (hi[0], hi[1]), (hi[0], lo[1])):
        page.ctl.click(y, x)
    page._key("enter")
    assert sorted(page.selection) == want
    n_ops = len(page.holder.d["operations"])
    page._delete_selected()
    assert not any(p in page.holder.d["pieces"] for p in want)
    assert len(page.holder.d["operations"]) == n_ops + 1
    page.undo()
    assert all(p in page.holder.d["pieces"] for p in want)
    page.shutdown()


def test_merge_keeps_the_smallest_number_after_asking_and_ids_are_never_reused(app, env, monkeypatch):
    from PyQt5.QtWidgets import QMessageBox
    from block01.core.regions import document as D
    page = _page(app, env, monkeypatch)
    _set_target(page, "X1-Y01", "X1-Y03", "Along column")
    _line(page, (1, 1), (1, 3))
    page.cores.numbering.btn_confirm.click()
    d = page.holder.d
    by_label = {page.cores.labels_by_key()[c["position_key"]]["label"]: u
                for u, c in d["cores"].items() if c["position_key"]}
    p1, p3 = d["cores"][by_label["X1-Y01"]]["pieces"][0], d["cores"][by_label["X1-Y03"]]["pieces"][0]
    asked = []
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: (asked.append(a[2]), QMessageBox.No)[1]))
    page.select(p3)
    page.toggle_selection(p1)
    _merge(page)
    assert asked and "X1-Y01" in asked[0] and "X1-Y03" in asked[0]
    assert d["pieces"][p1]["owner"] != d["pieces"][p3]["owner"]               # No: nothing changed
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    _merge(page)
    d = page.holder.d
    owner = d["pieces"][p1]["owner"]
    assert d["pieces"][p3]["owner"] == owner
    assert page.cores.labels_by_key()[d["cores"][owner]["position_key"]]["label"] == "X1-Y01"
    assert not any(page.cores.labels_by_key()[c["position_key"]]["label"] == "X1-Y03"
                   for c in d["cores"].values() if c["position_key"])     # released
    top = D.piece_counter(d)
    page.select(p3)
    page._delete_selected()
    page._set_tool("circle")
    _drag(page, (800, 600), (830, 630))
    assert page.selected != p3 and int(page.selected[1:]) > top                # a fresh number
    page.shutdown()


def test_space_held_pans_whatever_the_tool(app, env, monkeypatch):
    from PyQt5.QtCore import QEvent, QPointF, Qt
    from PyQt5.QtGui import QKeyEvent, QMouseEvent
    page = _page(app, env, monkeypatch)
    ov = page.viewer.overlay
    vp, gw = page.viewer.graphics.viewport(), page.viewer.graphics
    page._set_tool("circle")
    ov.eventFilter(gw, QKeyEvent(QEvent.KeyPress, Qt.Key_Space, Qt.NoModifier))
    pos = QPointF(ov.to_widget(600, 400))
    press = QMouseEvent(QEvent.MouseButtonPress, pos, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    assert ov.eventFilter(vp, press) is False                                 # pyqtgraph pans
    ov.eventFilter(vp, QMouseEvent(QEvent.MouseButtonRelease, pos, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    ov.eventFilter(gw, QKeyEvent(QEvent.KeyRelease, Qt.Key_Space, Qt.NoModifier))
    assert ov.eventFilter(vp, press) is True                                  # Circle again
    page.shutdown()


def test_several_selected_exclusions_have_no_shared_fields(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    _open_exclusions(page)
    _square(page, 100, 100, 50)
    page._key("enter")
    _square(page, 300, 300, 50)
    page._key("enter")
    page.select_many(list(page.holder.d["exclusions"]))
    assert page.exclusions.lbl_target.text() == "Several exclusions selected."
    assert not page.exclusions.cb["blur"].isEnabled() and not page.btn_merge.isEnabled()
    page._delete_selected()
    assert page.holder.d["exclusions"] == {}
    page.shutdown()


# ── tool row review fixes (codex_regions_tools_1.out) ───────────────────

def test_an_old_document_without_a_counter_never_reuses_a_deleted_id(app, env, monkeypatch):
    from block01.core.regions import document as D
    page = _page(app, env, monkeypatch)
    d = page.holder.d
    d.pop("piece_counter", None)                                      # as an old draft
    top = max(int(k[1:]) for k in d["pieces"])
    page.select(f"p{top}")
    page._delete_selected()
    assert D.piece_counter(page.holder.d) == top
    page._set_tool("circle")
    _drag(page, (800, 600), (830, 630))
    assert int(page.selected[1:]) == top + 1
    page.shutdown()


def test_last_piece_deletion_keeps_qc_clears_run_and_the_counter_survives_lock(app, env, monkeypatch):
    from block01.core.regions import document as D
    page = _page(app, env, monkeypatch)
    _set_target(page, "X1-Y01", "X1-Y03", "Along column")
    _line(page, (1, 1), (1, 3))
    page.cores.numbering.btn_confirm.click()
    d = page.holder.d
    uid, c = next((u, c) for u, c in d["cores"].items() if c["position_key"])
    key = c["position_key"]
    page.edit(D.set_qc, key, "accepted")
    page.edit(D.set_run, key, True)
    page.select_many(list(page.holder.d["cores"][uid]["pieces"]))
    page._delete_selected()
    pos = page.holder.d["positions"][key]
    assert uid not in page.holder.d["cores"] and pos["qc"] == "accepted" and not pos["run"]
    page.undo()                                                       # one step brings it all back
    assert uid in page.holder.d["cores"] and page.holder.d["positions"][key]["run"]
    counter = D.piece_counter(page.holder.d)
    page._set_tool("circle")
    _drag(page, (800, 600), (830, 630))
    page.select(page.selected)
    page._delete_selected()                                           # the counter now runs past the pieces
    assert D.piece_counter(page.holder.d) == counter + 1
    page.holder.d["map_sha256"] = None                                # an exclusions-only lock is enough here
    page.cores.study = None
    page.btn_save.click()
    assert K.load_saved(page.ctx["project_dir"])["piece_counter"] == counter + 1
    page._revert_to_saved()                                           # reopened from the Save
    assert D.piece_counter(page.holder.d) == counter + 1
    page.shutdown()


def test_a_numbered_merge_leaves_unselected_pieces_and_undoes_in_one_step(app, env, monkeypatch):
    import json as _json
    from block01.core.regions import document as D
    page = _page(app, env, monkeypatch)
    _set_target(page, "X1-Y01", "X1-Y03", "Along column")
    _line(page, (1, 1), (1, 3))
    page.cores.numbering.btn_confirm.click()
    d = page.holder.d
    labels = page.cores.labels_by_key()
    core = {labels[c["position_key"]]["label"]: u for u, c in d["cores"].items() if c["position_key"]}
    a, b = core["X1-Y01"], core["X1-Y02"]
    page._set_tool("circle")
    cy, cx = _centre(1, 2)
    _drag(page, (cy - 5, cx + 90), (cy + 5, cx + 100))                       # an extra piece, merged into X1-Y02
    extra = page.selected
    page.select_many([d["cores"][b]["pieces"][0], extra])
    _merge(page)
    assert page.holder.d["pieces"][extra]["owner"] == b
    before = _json.dumps(page.holder.d, sort_keys=True)
    pa, pb = page.holder.d["cores"][a]["pieces"][0], page.holder.d["cores"][b]["pieces"][0]
    page.select_many([pa, pb])                                        # only one piece of X1-Y02
    _merge(page)
    d = page.holder.d
    assert d["pieces"][pb]["owner"] == a and d["pieces"][extra]["owner"] == b       # left behind
    assert d["cores"][b]["position_key"] is None                      # its number released
    page.undo()
    after = json.loads(_json.dumps(page.holder.d, sort_keys=True))
    want = json.loads(before)
    after.pop("operations"), want.pop("operations")
    assert after == want
    page.shutdown()


def test_ctrl_z_or_a_tab_switch_while_the_button_is_down_drops_that_click(app, env, monkeypatch):
    from PyQt5.QtCore import QEvent, QPointF, Qt
    from PyQt5.QtGui import QMouseEvent
    page = _page(app, env, monkeypatch)
    _open_exclusions(page)
    ov, vp = page.viewer.overlay, page.viewer.graphics.viewport()

    def ev(kind, y, x):
        return QMouseEvent(kind, QPointF(ov.to_widget(x, y)), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    page._set_tool("polygon")
    for y, x in ((100, 100), (100, 180)):
        ov.eventFilter(vp, ev(QEvent.MouseButtonPress, y, x))
        ov.eventFilter(vp, ev(QEvent.MouseButtonRelease, y, x))
    ov.eventFilter(vp, ev(QEvent.MouseButtonPress, 180, 180))
    page.undo()                                                       # Ctrl+Z with the button down
    ov.eventFilter(vp, ev(QEvent.MouseButtonRelease, 180, 180))
    assert page.ctl.poly_pts == []
    ov.eventFilter(vp, ev(QEvent.MouseButtonPress, 300, 300))
    ov.eventFilter(vp, ev(QEvent.MouseButtonRelease, 300, 300))
    assert page.ctl.poly_pts == [(300, 300)] or len(page.ctl.poly_pts) == 1     # the next click counts
    ov.eventFilter(vp, ev(QEvent.MouseButtonPress, 300, 380))
    page.frame.left_tabs.setCurrentWidget(page.cores)                 # a tab switch with the button down
    ov.eventFilter(vp, ev(QEvent.MouseButtonRelease, 300, 380))
    assert page.ctl.poly_pts == []
    page.shutdown()


def test_a_double_click_closes_a_polygon_or_counts_as_a_press(app, env, monkeypatch):
    from PyQt5.QtCore import QEvent, QPointF, Qt
    from PyQt5.QtGui import QKeyEvent, QMouseEvent
    page = _page(app, env, monkeypatch)
    _open_exclusions(page)
    ov, vp, gw = page.viewer.overlay, page.viewer.graphics.viewport(), page.viewer.graphics

    def ev(kind, y, x):
        return QMouseEvent(kind, QPointF(ov.to_widget(x, y)), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    page._set_tool("polygon")
    for y, x in ((100, 100), (100, 180), (180, 180)):
        ov.eventFilter(vp, ev(QEvent.MouseButtonPress, y, x))
        ov.eventFilter(vp, ev(QEvent.MouseButtonRelease, y, x))
    assert ov.eventFilter(vp, ev(QEvent.MouseButtonDblClick, 180, 180)) is True
    ov.eventFilter(vp, ev(QEvent.MouseButtonRelease, 180, 180))
    assert len(page.holder.d["exclusions"]) == 1 and page.ctl.poly_pts == []      # closed, no stray point
    page._set_tool("circle")                                          # a quick second press: still a circle
    assert ov.eventFilter(vp, ev(QEvent.MouseButtonDblClick, 500, 400)) is True
    ov.eventFilter(vp, QMouseEvent(QEvent.MouseMove, QPointF(ov.to_widget(430, 530)), Qt.NoButton,
                                   Qt.LeftButton, Qt.NoModifier))
    ov.eventFilter(vp, ev(QEvent.MouseButtonRelease, 530, 430))
    assert len(page.holder.d["exclusions"]) == 2
    page._set_tool("polygon")                                         # Space held: the tool gets nothing
    for y, x in ((600, 100), (600, 180), (680, 180)):
        page.ctl.click(y, x)
    ov.eventFilter(gw, QKeyEvent(QEvent.KeyPress, Qt.Key_Space, Qt.NoModifier))
    assert ov.eventFilter(vp, ev(QEvent.MouseButtonDblClick, 680, 180)) is False
    assert len(page.holder.d["exclusions"]) == 2 and len(page.ctl.poly_pts) == 3
    ov.eventFilter(vp, ev(QEvent.MouseButtonRelease, 680, 180))
    from PyQt5.QtGui import QFocusEvent
    ov.eventFilter(gw, QFocusEvent(QEvent.FocusOut))                  # Space released elsewhere
    assert ov._space is False
    page.shutdown()


def test_an_old_left_out_piece_can_be_restored_or_deleted(app, env, monkeypatch):
    from PyQt5.QtWidgets import QMenu
    from block01.core.regions import document as D
    page = _page(app, env, monkeypatch)
    pid = next(iter(page.holder.d["pieces"]))
    page.edit(D.drop_piece, pid, "dust")                              # as old data has it
    seen = []
    monkeypatch.setattr(QMenu, "exec_", lambda self, *a: seen.extend(x.text() for x in self.actions()))
    page.cores.piece_menu(pid)
    assert "Restore" in seen and "Delete" in seen and not any("Leave out" in t for t in seen)
    page.shutdown()


def test_ctrl_z_on_a_first_press_cancels_only_that_press(app, env, monkeypatch):
    from PyQt5.QtCore import QEvent, QPointF, Qt
    from PyQt5.QtGui import QMouseEvent
    page = _page(app, env, monkeypatch)
    _open_exclusions(page)
    _square(page, 100, 100, 50)
    page._key("enter")
    ov, vp = page.viewer.overlay, page.viewer.graphics.viewport()
    pos = QPointF(ov.to_widget(400, 400))
    ov.eventFilter(vp, QMouseEvent(QEvent.MouseButtonPress, pos, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    page.undo()
    ov.eventFilter(vp, QMouseEvent(QEvent.MouseButtonRelease, pos, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    assert len(page.holder.d["exclusions"]) == 1 and page.ctl.poly_pts == []
    page.shutdown()


def test_find_cores_on_an_old_draft_keeps_its_highest_piece_id(app, env, monkeypatch):
    from block01.core.regions import document as D
    page = _page(app, env, monkeypatch)
    d = page.holder.d
    d["pieces"]["p40"] = dict(next(iter(d["pieces"].values())), owner=None)   # an old draft's high id
    d.pop("piece_counter", None)
    D.attach_tma(d, page.cores.study, page.cores.candidates)
    assert D.piece_counter(d) == 40 and "p40" not in d["pieces"]
    page.shutdown()


# ── second real-machine round (docs/regions_tools_redesign.md §10) ──────

def test_a_polygon_point_goes_down_with_the_button_even_if_the_mouse_moves(app, env, monkeypatch):
    from PyQt5.QtCore import QEvent, QPointF, Qt
    from PyQt5.QtGui import QMouseEvent
    page = _page(app, env, monkeypatch)
    ov, vp = page.viewer.overlay, page.viewer.graphics.viewport()

    def ev(kind, y, x, buttons=Qt.LeftButton):
        return QMouseEvent(kind, QPointF(ov.to_widget(x, y)), Qt.LeftButton if kind != QEvent.MouseMove
                           else Qt.NoButton, buttons, Qt.NoModifier)
    for tool in ("polygon", "select_polygon"):
        page._set_tool(tool)
        ov.eventFilter(vp, ev(QEvent.MouseButtonPress, 100, 100))
        assert len(page.ctl.poly_pts) == 1                            # at the press already
        ov.eventFilter(vp, ev(QEvent.MouseMove, 100, 160))            # a fast hand: far past the slop
        ov.eventFilter(vp, ev(QEvent.MouseButtonRelease, 100, 160))
        assert page.ctl.poly_pts == [(100.0, 100.0)] or len(page.ctl.poly_pts) == 1
        page._escape()


def test_circle_keeps_the_press_on_its_outline_however_far_the_drag_goes(app, env, monkeypatch):
    import math
    import numpy as np
    from PyQt5.QtCore import Qt
    page = _page(app, env, monkeypatch)
    _open_exclusions(page)
    page._set_tool("circle")

    def near(p, y, x):                                                # the outline passes by (y, x)
        return min(math.hypot(q[1] - y, q[0] - x) for q in p) <= 3.0

    assert page.ctl.press(100, 100)
    for far in (20, 80, 160):                                         # the preview, while dragging
        page.ctl.drag(100 + far / 2, 100 + far)
        y0, x0, y1, x1 = page.ctl.preview_circle()
        cy, cx, ry, rx = (y0 + y1) / 2, (x0 + x1) / 2, (y1 - y0) / 2, (x1 - x0) / 2
        assert abs(((100 - cy) / ry) ** 2 + ((100 - cx) / rx) ** 2 - 1.0) < 1e-9   # the press: on it
    page.ctl.drag(180, 260, finish=True)
    page.ctl.released()
    p = page.holder.d["exclusions"][page.selected]["polygon"]
    assert near(p, 100, 100) and near(p, 180, 260)                    # both points on the outline
    assert page.ctl.press(300, 300)
    page.ctl.drag(340, 360, finish=True, mods=int(Qt.ShiftModifier))
    page.ctl.released()
    p = np.array(page.holder.d["exclusions"][page.selected]["polygon"])
    assert abs(np.ptp(p[:, 0]) - np.ptp(p[:, 1])) <= 2                # a circle
    assert near(p.tolist(), 300, 300) and near(p.tolist(), 340, 360)  # on that diameter
    assert page.ctl.press(600, 400)
    page.ctl.drag(620, 430, finish=True, mods=int(Qt.AltModifier))
    page.ctl.released()
    p = np.array(page.holder.d["exclusions"][page.selected]["polygon"])
    assert abs(p[:, 0].mean() - 400) <= 2 and abs(p[:, 1].mean() - 600) <= 2   # centred on the press
    assert near(p.tolist(), 620, 430)
    page.shutdown()


def test_esc_drops_a_transform_and_a_reopen_never_brings_it_back(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    from block01.core.regions import document as D
    key = next(iter(page.holder.d["positions"]))
    page.edit(D.set_run, key, True)                                   # an edit: the save timer runs
    page._set_tool("select")
    cy, cx = _centre(1, 1)
    page.ctl.click(cy, cx)
    pid = page.selected
    before = [list(q) for q in page.holder.d["pieces"][pid]["polygon"]]
    _drag(page, (cy, cx), (cy + 50, cx + 50))
    from test_tma_f3_page import _wait
    _wait(app, lambda: not page._save_timer.isActive(), ms=3000)      # saved meanwhile
    page._escape()
    page.flush_draft()
    from block01.ui.regions.page import RegionsPage
    again = RegionsPage()
    again.set_context(page.ctx)
    assert again.holder.d["pieces"][pid]["polygon"] == before
    page.shutdown()


def test_save_applies_a_pending_transform_first(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    _open_exclusions(page)
    _square(page, 700, 500, 120)                                      # big enough on screen: the centre moves
    page._key("enter")
    uid = page.selected
    page._set_tool("select")
    _drag(page, (760, 560), (750, 550))
    page.holder.d["map_sha256"] = None
    page.cores.study = None
    page.btn_save.click()
    rec = K.load_saved(page.ctx["project_dir"])
    poly = next(iter(rec["exclusions"].values()))["polygon"]
    assert min(q[1] for q in poly) == 690 and page.ctl.transform is None and uid
    page.shutdown()


def _two_pieces_touching(page):
    """A drawn patch overlapping X1-Y01's piece."""
    d = page.holder.d
    cy, cx = _centre(1, 1)
    base = next(p for p, v in d["pieces"].items() if G.contains(v["polygon"], cy, cx))
    page._set_tool("circle")
    _drag(page, (cy - 30, cx + 40), (cy + 30, cx + 100))             # sticks out of the core's right side
    return base, page.selected


def test_union_fuses_a_patch_into_the_numbered_core_and_undoes_in_one_step(app, env, monkeypatch):
    import json as _json
    page = _page(app, env, monkeypatch)
    _set_target(page, "X1-Y01", "X1-Y03", "Along column")
    _line(page, (1, 1), (1, 3))
    page.cores.numbering.btn_confirm.click()
    base, patch = _two_pieces_touching(page)
    d = page.holder.d
    core = d["pieces"][base]["owner"]
    key = d["cores"][core]["position_key"]
    a_base, a_patch = G.area(d["pieces"][base]["polygon"]), G.area(d["pieces"][patch]["polygon"])
    before = _json.dumps(d, sort_keys=True)
    page.select_many([base, patch])
    _merge(page, "Union")
    d = page.holder.d
    new = page.selected
    assert base not in d["pieces"] and patch not in d["pieces"]
    assert d["pieces"][new]["source"] == "union" and d["pieces"][new]["owner"] == core
    assert d["cores"][core]["position_key"] == key and d["cores"][core]["mapping"] == "review"
    a = G.area(d["pieces"][new]["polygon"])
    assert max(a_base, a_patch) < a < a_base + a_patch
    assert len(d["pieces"][new]["polygon"]) < 400                     # simplified
    page.undo()
    after = json.loads(_json.dumps(page.holder.d, sort_keys=True))
    want = json.loads(before)
    after.pop("operations"), want.pop("operations")
    after.pop("piece_counter"), want.pop("piece_counter")
    assert after == want
    page.shutdown()


def test_union_refuses_pieces_that_do_not_touch_and_works_inside_one_core(app, env, monkeypatch):
    from block01.core.regions import document as D
    page = _page(app, env, monkeypatch)
    d = page.holder.d
    a = next(p for p, v in d["pieces"].items() if G.contains(v["polygon"], *_centre(1, 1)))
    b = next(p for p, v in d["pieces"].items() if G.contains(v["polygon"], *_centre(1, 2)))
    page.select_many([a, b])
    n = len(page.holder.d["operations"])
    _merge(page, "Union")
    assert "do not touch" in page.cores.lbl_info.text() and len(page.holder.d["operations"]) == n
    base, patch = _two_pieces_touching(page)
    page.select_many([base, patch])
    _merge(page, "Group")                                             # first one core, two outlines
    core = page.holder.d["pieces"][base]["owner"]
    assert page.holder.d["pieces"][patch]["owner"] == core
    page.select_many([base, patch])
    _merge(page, "Union")                                             # Group would do nothing here
    assert page.holder.d["cores"][core]["pieces"] == [page.selected]
    page.shutdown()


def test_union_outline_fills_holes_and_refuses_a_diagonal_touch():
    ring = [[[0, 0], [30, 0], [30, 10], [0, 10]], [[0, 20], [30, 20], [30, 30], [0, 30]],
            [[0, 0], [10, 0], [10, 30], [0, 30]], [[20, 0], [30, 0], [30, 30], [20, 30]]]
    out, why = G.union_outline(ring, (100, 100))
    assert why is None and G.area(out) >= 29 * 29                     # the hole is filled
    out, why = G.union_outline([[[0, 0], [9, 0], [9, 9], [0, 9]], [[10, 10], [19, 10], [19, 19], [10, 19]]],
                               (100, 100))
    assert out is None and why == "apart"                             # corners only: not one region
    out, why = G.union_outline([[[0, 0], [5, 0], [5, 5], [0, 5]], [[0, 0], [5, 0], [5, 5], [0, 5]]], (10 ** 6, 10 ** 6))
    assert why is None
    big = [[[0, 0], [5, 0], [5, 5], [0, 5]], [[19990, 19990], [19995, 19990], [19995, 19995], [19990, 19995]]]
    assert G.union_outline(big, (10 ** 6, 10 ** 6)) == (None, "too_far")


def test_transform_geometry_rejects_folded_boxes():
    import numpy as np
    from block01.core.regions import transform as T
    q = T.box_quad([[0, 0], [10, 0], [10, 10], [0, 10]])
    assert T.distort(q, 2, (-5, -5)) is None                          # a crossed box
    assert T.scale(q, 0.0, 2, (0, 0)) is None                         # flattened
    flip = T.scale(q, 0.0, 2, (-10, -10))                             # through the anchor: a flip
    assert flip is not None and np.allclose(sorted(flip[:, 0]), [-10, -10, 0, 0])
    s = T.Session("x", [[0, 0], [10, 0], [10, 10], [0, 10]])
    s.begin("move", -1, (5, 5))
    s.move((5, 5))
    s.end()
    assert s.history == [] and not s.changed                          # a drag that moved nothing: no step


# ── implementation review fixes (codex_regions_impl10_1.out) ────────────

def test_an_edit_during_a_transform_drag_ends_that_drag_cleanly(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    page._set_tool("select")
    cy, cx = _centre(1, 1)
    page.ctl.click(cy, cx)
    pid = page.selected
    assert page.ctl.press(cy, cx)
    page.ctl.drag(cy + 10, cx + 10)
    page._delete_selected()                                           # Delete with the button down
    page.ctl.drag(cy + 20, cx + 20)
    page.ctl.drag(cy + 20, cx + 20, finish=True)                      # no crash, nothing more happens
    page.ctl.released()
    assert pid not in page.holder.d["pieces"] and page.ctl.transform is None
    page.shutdown()


def test_a_one_pixel_move_far_out_on_a_large_slide_counts():
    from block01.core.regions import transform as T
    s = T.Session("x", [[100000, 100000], [100100, 100000], [100100, 100100], [100000, 100100]])
    s.begin("move", -1, (100050, 100050))
    s.move((100051, 100050))
    s.end()
    assert s.changed and s.history and round(s.preview()[0][0]) == 100001


def test_a_click_on_a_handle_without_moving_changes_nothing(app, env, monkeypatch):
    from block01.core.regions import transform as T
    page = _page(app, env, monkeypatch)
    page._set_tool("select")
    cy, cx = _centre(1, 1)
    page.ctl.click(cy, cx)
    q = T.box_quad(page.holder.d["pieces"][page.selected]["polygon"])
    x1, y1 = q[2]
    tol = 8 * page.view.screen_px()
    assert page.ctl.press(y1 + tol / 2, x1 + tol / 2)                 # near, not on, the corner
    page.ctl.drag(y1 + tol / 2, x1 + tol / 2, finish=True)
    page.ctl.released()
    assert page.ctl.transform is None
    page.shutdown()


def test_the_rotate_zone_never_hides_a_neighbours_click(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    page._set_tool("select")
    cy, cx = _centre(1, 1)
    page.ctl.click(cy, cx)
    first = page.selected
    monkeypatch.setattr(type(page.ctl), "box_hit", lambda self, y, x: ("rotate", 2))
    ny, nx = _centre(1, 2)
    assert page.ctl.cursor_at(ny, nx) is None                         # no rotate pointer over it either
    assert page.ctl.press(ny, nx) is True and page.ctl._drag is None  # a plain click on the neighbour
    page.ctl.click(ny, nx)
    page.ctl.released()
    assert page.selected not in (None, first)
    page.shutdown()


def test_esc_during_the_first_transform_drag_keeps_the_tool(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    page._set_tool("select")
    cy, cx = _centre(1, 1)
    page.ctl.click(cy, cx)
    assert page.ctl.press(cy, cx)
    page.ctl.drag(cy + 10, cx + 10)
    page._escape()
    assert page.ctl.tool == "select" and page.ctl.transform is None
    page._escape()
    assert page.ctl.tool is None
    page.shutdown()


def test_undo_and_redo_buttons_follow_a_pending_transform(app, env, monkeypatch):
    from block01.core.regions import document as D
    page = _page(app, env, monkeypatch)
    page.holder._undo.clear()
    key = next(iter(page.holder.d["positions"]))
    page.edit(D.set_run, key, True)
    page.undo()
    assert page.btn_redo.isEnabled() and not page.btn_undo.isEnabled()
    page._set_tool("select")
    cy, cx = _centre(1, 1)
    page.ctl.click(cy, cx)
    _drag(page, (cy, cx), (cy + 10, cx + 10))
    assert page.btn_undo.isEnabled() and not page.btn_redo.isEnabled()
    page.cores.row_clicked("piece", page.selected, False)            # the same shape again from the table
    assert page.ctl.transform is not None and page.btn_undo.isEnabled()
    page.shutdown()


def test_a_pending_transform_is_hit_where_it_is_shown_keeps_its_label_and_a_return_trip_clears(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    _set_target(page, "X1-Y01", "X1-Y03", "Along column")
    _line(page, (1, 1), (1, 3))
    page.cores.numbering.btn_confirm.click()
    page.holder._undo.clear()
    page._set_tool("select")
    cy, cx = _centre(1, 1)
    page.ctl.click(cy, cx)
    pid = page.selected
    _drag(page, (cy, cx), (cy + 100, cx + 30))                       # onto empty glass below
    assert page.ctl.shape_at(cy + 100, cx + 30) == pid                # hit where it is shown
    assert page.ctl.shape_at(cy, cx) is None
    assert page.viewer.overlay.live.get("label")                      # the number still shows
    _drag(page, (cy + 100, cx + 30), (cy, cx))                        # and back again
    page._key("enter")
    ov = page.viewer.overlay
    assert ov.live is None and pid in [it["id"] for it in ov.items]   # back in the cached layer
    assert not page.btn_undo.isEnabled()                              # nothing was changed
    page.shutdown()


def test_the_circle_rule_holds_for_every_modifier_and_direction(app, env, monkeypatch):
    import itertools
    from PyQt5.QtCore import Qt
    page = _page(app, env, monkeypatch)
    _open_exclusions(page)
    page._set_tool("circle")

    def on(c, y, x):
        y0, x0, y1, x1 = c
        cy, cx, ry, rx = (y0 + y1) / 2, (x0 + x1) / 2, (y1 - y0) / 2, (x1 - x0) / 2
        return abs(((y - cy) / ry) ** 2 + ((x - cx) / rx) ** 2 - 1.0) < 1e-9
    for (dy, dx), shift, alt in itertools.product(((40, 70), (-40, 70), (40, -70), (-40, -70)),
                                                  (False, True), (False, True)):
        mods = (int(Qt.ShiftModifier) if shift else 0) | (int(Qt.AltModifier) if alt else 0)
        assert page.ctl.press(450, 350)
        page.ctl.drag(450 + dy, 350 + dx, mods=mods)
        c = page.ctl.preview_circle()
        assert on(c, 450 + dy, 350 + dx)                              # the pointer: always on it
        if alt:
            assert abs((c[0] + c[2]) / 2 - 450) < 1e-9 and abs((c[1] + c[3]) / 2 - 350) < 1e-9
        else:
            assert on(c, 450, 350)                                    # the press: on it
        if shift:
            assert abs((c[2] - c[0]) - (c[3] - c[1])) < 1e-9
        page._escape()
        page._set_tool("circle")
    n = len(page.holder.d["exclusions"])
    assert page.ctl.press(450, 350)
    page.ctl.drag(450, 450, finish=True)                              # flat: no shape without Shift
    page.ctl.released()
    assert len(page.holder.d["exclusions"]) == n
    assert page.ctl.press(450, 350)
    page.ctl.drag(450, 450, finish=True, mods=int(Qt.ShiftModifier))  # Shift: a circle
    page.ctl.released()
    assert len(page.holder.d["exclusions"]) == n + 1
    page.shutdown()


# ── Save review fixes (codex_regions_save_1.out) ────────────────────────

def test_unsaved_changes_follow_the_document_state_exactly(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    _open_exclusions(page)
    _square(page, 700, 500, 60)
    page._key("enter")
    uid = page.selected
    page.exclusions.note.setText("one")
    page.exclusions.note.editingFinished.emit()
    page.btn_save.click()
    assert "unsaved" not in page.lbl_saved.text()
    page.undo()
    page.exclusions.note.setText("two")                               # same second, same kind of edit
    page.exclusions.note.editingFinished.emit()
    assert page.holder.d["exclusions"][uid]["note"] == "two" and "unsaved changes" in page.lbl_saved.text()
    page.undo()
    page.redo()                                                       # an undo / redo trip is still unsaved here
    assert "unsaved changes" in page.lbl_saved.text()
    page.btn_save.click()
    page.undo()
    page.redo()                                                       # back to exactly the saved state
    assert "unsaved" not in page.lbl_saved.text()
    page.set_context(dict(page.ctx, full_wsi=False, workspace_id="roi_a"))
    assert "unsaved changes" in page.lbl_saved.text()                 # a new workspace is a new handoff
    page.shutdown()


def test_revert_drops_a_half_drawn_polygon(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    _open_exclusions(page)
    page.btn_save.click()
    page._set_tool("polygon")
    for y, x in ((100, 100), (100, 200), (200, 200)):
        page.ctl.click(y, x)
    page._revert_to_saved()
    page._key("enter")
    assert page.holder.d["exclusions"] == {} and page.ctl.poly_pts == []
    page.shutdown()


def test_an_unchanged_save_registers_nothing_new_and_an_interrupted_one_is_seen(app, env, monkeypatch):
    from block01.core import provenance as prov
    page = _page(app, env, monkeypatch)
    pd = page.ctx["project_dir"]
    page.btn_save.click()
    page.btn_save.click()                                             # nothing changed
    rec = K.load_saved(pd)
    assert rec["save_count"] == 1
    assert len([e for e in prov.load_entries(pd) if e["kind"] == "regions_layout"]) == 1
    assert K.table_matches(pd, rec)
    with open(K.table_path(pd), "ab") as f:                          # as if the next Save stopped halfway
        f.write(b"x")
    assert not K.table_matches(pd, rec)
    page.flush_draft()
    from block01.ui.regions.page import RegionsPage
    again = RegionsPage()
    again.set_context(page.ctx)
    assert "interrupted" in again.cores.lbl_info.text()
    page.shutdown()


def test_revert_in_another_workspace_shows_its_handoff_as_unsaved(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    page.btn_save.click()
    page.set_context(dict(page.ctx, full_wsi=False, workspace_id="roi_b", handoff_sha256="ee" * 32))
    page._revert_to_saved()
    assert "unsaved changes" in page.lbl_saved.text()                 # roi_b's handoff is not in the Save
    page.btn_save.click()
    for ws in ("full", "roi_b", "full", "roi_b"):                     # holders come and go: still saved
        page.set_context(dict(page.ctx, full_wsi=ws == "full", workspace_id=ws,
                              handoff_sha256=("ab" * 32) if ws == "full" else "ee" * 32))
    assert "unsaved" not in page.lbl_saved.text()
    page.shutdown()
