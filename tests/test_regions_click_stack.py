"""Regions: an exclusion inside a larger core can be clicked (the user's
item 8, 2026-11-12; docs/v17_acceptance_notes_plan.md B24): in the Cores
tab a Select click picks the smallest shape under the pointer, cores and
exclusions alike, and an exclusion opens the Exclusions tab; a click again
at the same spot picks the next shape under it. The Exclusions tab keeps
its rule (exclusions first, then the core masks it converts).
"""

from test_tma_f3_page import _centre, _drag, _page, app, env  # noqa: F401
from test_regions_r3 import _open_exclusions, _square


def _real_click(page, y, x, ctrl=False):
    """A still left click as the overlay sends it: press; when claimed,
    drag(finish) then click; release."""
    if page.ctl.press(y, x, ctrl):
        page.ctl.drag(y, x, finish=True)
    page.ctl.click(y, x, ctrl)
    page.ctl.released()


def _core_with_an_exclusion(page):
    """Core (1, 1) with a small exclusion at its centre; back in the Cores tab."""
    cy, cx = _centre(1, 1)
    _open_exclusions(page)
    _square(page, cy - 20, cx - 20, 40)
    page._key("enter")
    (uid,) = page.holder.d["exclusions"]
    page.frame.left_tabs.setCurrentWidget(page.cores)
    assert not page.exclusions_tab_open()
    pid = page.ctl.piece_at(cy, cx)
    assert pid is not None
    page._set_tool("select")
    return (cy, cx), uid, pid


def test_the_exclusion_inside_a_core_is_picked_and_its_tab_opens(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    (cy, cx), uid, pid = _core_with_an_exclusion(page)
    _real_click(page, cy, cx)
    assert page.selected == uid and page.exclusions_tab_open()
    _real_click(page, cy, cx)                                  # again, the same spot: the core under it
    assert page.selected == pid
    _real_click(page, cy, cx)                                  # and round
    assert page.selected == uid
    _real_click(page, cy + 1, cx + 1)                          # within the click slop: still deeper
    assert page.selected == pid
    page.shutdown()


def test_a_click_elsewhere_in_the_core_picks_the_core(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    (cy, cx), uid, pid = _core_with_an_exclusion(page)
    _real_click(page, cy + 35, cx)                             # inside the core, outside the exclusion
    assert page.selected == pid and not page.exclusions_tab_open()
    _real_click(page, cy, cx)                                  # another spot: the smallest, no cycling
    assert page.selected == uid
    page.shutdown()


def test_the_exclusions_tab_still_selects_a_core_mask_to_convert(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    (cy, cx), uid, pid = _core_with_an_exclusion(page)
    _open_exclusions(page)
    page._set_tool("select")
    _real_click(page, cy + 35, cx)                             # no exclusion there: the core mask
    assert page.selected == pid and page.exclusions_tab_open()
    _real_click(page, cy, cx)                                  # the exclusion first in this tab
    assert page.selected == uid
    page.shutdown()


# ── B25: Shift+click adds / removes; Ctrl no longer does (Shift only) ────

def test_shift_click_adds_and_removes_and_delete_takes_them(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    page._set_tool("select")
    a, b = _centre(1, 1), _centre(2, 1)
    pa, pb = page.ctl.piece_at(*a), page.ctl.piece_at(*b)
    _real_click(page, *a)
    shift = {"on": True}
    monkeypatch.setattr(type(page), "shift_held", staticmethod(lambda: shift["on"]))
    _real_click(page, *a)                                        # inside the one selected: out
    assert page.selection == []
    _real_click(page, *a)
    _real_click(page, *b)
    assert sorted(page.selection) == sorted([pa, pb])
    _real_click(page, *b)                                        # again: out
    assert page.selection == [pa]
    _real_click(page, *b)
    assert sorted(page.selection) == sorted([pa, pb])
    monkeypatch.setattr("block01.qt.QtWidgets.QMessageBox.question",
                        staticmethod(lambda *a, **k: __import__("block01.qt", fromlist=["QtWidgets"])
                                     .QtWidgets.QMessageBox.Yes))
    page._delete_selected()                                      # a Shift-made selection is deleted whole
    assert pa not in page.holder.d["pieces"] and pb not in page.holder.d["pieces"]
    shift["on"] = False
    page.shutdown()


def test_the_hints_name_shift_only(app):
    from block01.ui.regions.page import FAMILIES
    from block01.ui.tma import numbering_panel
    import inspect
    assert "Shift+click adds" in FAMILIES[0][1][0][2] and "Ctrl+ or" not in FAMILIES[0][1][0][2]
    src = inspect.getsource(numbering_panel)
    assert "Select ▸ Click, Shift+click for more" in src and "Ctrl+click for more" not in src


def test_ctrl_click_and_ctrl_marquee_no_longer_add(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    monkeypatch.setattr(type(page), "shift_held", staticmethod(lambda: False))
    page._set_tool("select")
    a, b = _centre(1, 1), _centre(2, 1)
    pa, pb = page.ctl.piece_at(*a), page.ctl.piece_at(*b)
    _real_click(page, *a)
    page.ctl.press(*b, True)                                     # Ctrl held, as the overlay passes it
    page.ctl.click(*b, True)
    page.ctl.released()
    assert page.selection == [pb]                                # a plain click: the selection is replaced
    _real_click(page, *a)
    assert page.selection == [pa]
    page._set_tool("select_rect")
    _drag(page, (b[0] - 20, b[1] - 20), (b[0] + 20, b[1] + 20), ctrl=True)
    assert pb in page.selection and pa not in page.selection     # the marquee replaced it too
    page.shutdown()


def test_a_drag_inside_the_selected_shape_still_moves_it_and_selects_nothing(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    (cy, cx), uid, pid = _core_with_an_exclusion(page)
    _real_click(page, cy + 35, cx)
    assert page.selected == pid
    assert page.ctl.press(cy + 35, cx) is True                  # inside its box: a move
    page.ctl.drag(cy + 45, cx + 10, finish=True)
    page.ctl.click(cy + 45, cx + 10)                            # the overlay sends none after a move; even so
    page.ctl.released()
    assert page.selected == pid and page.ctl.transform is not None   # moved, pending, not reselected
    page.shutdown()


def test_an_exclusion_in_the_selected_cores_rotate_zone_is_picked(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    page._set_tool("select")
    cy, cx = _centre(1, 1)
    _real_click(page, cy, cx)
    pid = page.selected
    assert pid in page.holder.d["pieces"]
    xs = [q[0] for q in page.holder.d["pieces"][pid]["polygon"]]
    ys = [q[1] for q in page.holder.d["pieces"][pid]["polygon"]]
    spot = next(((y, x) for k in range(1, 40) for y, x in ((min(ys) - k, min(xs) - k),)
                 if (page.ctl.box_hit(y, x) or ("",))[0] == "rotate"), None)
    assert spot is not None                                      # a point of its rotate zone
    _open_exclusions(page)
    _square(page, spot[0] - 3, spot[1] - 3, 6)
    page._key("enter")
    (uid,) = page.holder.d["exclusions"]
    page.frame.left_tabs.setCurrentWidget(page.cores)
    page._set_tool("select")
    _real_click(page, cy, cx)
    assert page.selected == pid
    _real_click(page, *spot)                                     # not a rotation: the exclusion
    assert page.selected == uid and page.exclusions_tab_open()
    page.shutdown()


def test_shift_click_takes_out_a_shape_with_a_pending_move(app, env, monkeypatch):
    """codex B25 review: a still Shift+click inside the selected shape after
    an earlier drag (the move still pending, Enter not pressed) takes it out;
    the move is applied first, as for any other selection change."""
    page = _page(app, env, monkeypatch)
    page._set_tool("select")
    a, b = _centre(1, 1), _centre(2, 1)
    pa, pb = page.ctl.piece_at(*a), page.ctl.piece_at(*b)
    _real_click(page, *a)
    shift = {"on": True}
    monkeypatch.setattr(type(page), "shift_held", staticmethod(lambda: shift["on"]))
    _real_click(page, *b)
    assert sorted(page.selection) == sorted([pa, pb])
    shift["on"] = False
    _real_click(page, *b)                                        # b alone, to move it
    before = [list(q) for q in page.holder.d["pieces"][pb]["polygon"]]
    assert page.ctl.press(*b) is True
    page.ctl.drag(b[0] + 10, b[1] + 10, finish=True)
    page.ctl.released()
    assert page.ctl.transform is not None                        # moved, pending
    shift["on"] = True
    _real_click(page, *a)                                        # a back in
    assert sorted(page.selection) == sorted([pa, pb])
    assert page.holder.d["pieces"][pb]["polygon"] != before      # the move was applied
    page._set_tool("select")
    _real_click(page, *a)                                        # Shift+click inside a: out
    assert page.selection == [pb]
    shift["on"] = False
    page.shutdown()


def test_a_still_shift_click_after_a_pending_move_on_the_same_shape(app, env, monkeypatch):
    page = _page(app, env, monkeypatch)
    page._set_tool("select")
    a, b = _centre(1, 1), _centre(2, 1)
    pa, pb = page.ctl.piece_at(*a), page.ctl.piece_at(*b)
    _real_click(page, *b)
    assert page.ctl.press(*b) is True                            # move b, leave it pending
    page.ctl.drag(b[0] + 10, b[1] + 10, finish=True)
    page.ctl.released()
    assert page.ctl.transform is not None
    shift = {"on": True}
    monkeypatch.setattr(type(page), "shift_held", staticmethod(lambda: shift["on"]))
    _real_click(page, b[0] + 10, b[1] + 10)                     # still, inside b as shown: out
    assert page.selection == []
    shift["on"] = False
    page.shutdown()
