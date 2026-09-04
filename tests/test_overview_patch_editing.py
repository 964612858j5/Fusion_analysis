"""Patches are rectangles you pick up on purpose, then move, resize or delete.

Two complaints, one answer. There were never more than four patches per ROI
-- an arbitrary cap on a question ("how many places on this slide are worth
measuring?") that only the person looking at the tissue can answer -- and a
patch, once drawn, was frozen: a rectangle two hundred pixels too far left
had to be deleted and drawn again, by hand, on a thumbnail where one pixel
is thirty-two slide pixels.

So the cap is gone, and the Tissue Preview edits patches directly. But it
does it through an EXPLICIT state, because the first attempt -- "a click on
a patch selects it" -- lost every argument it had with the tools: the
drawing-mode buttons stay pressed all day, so a click on a patch drew a new
rectangle or added a vertex, and even with no tool out the click was as
likely to be read as "take me over there".

The state is entered deliberately, by clicking a patch's BORDER or its
LABEL -- its own furniture -- in ANY mode, or by clicking its name in the
page's patch list. The patch's INTERIOR is still tissue: a click there draws
or navigates exactly as the mode says. While the state is on, the thumbnail
is that rectangle's editor and nothing else: drag to move, grips to resize,
Delete to remove, and drawing and navigation are suspended whatever the mode
buttons say. One click anywhere else ends it and does NOTHING else -- the
mode gets its say back on the next click.

Geometry still commits on release through the SAME `patches_changed` path a
drawn patch uses -- once per gesture, never per mouse-move -- so the patch
list, the ROI bookkeeping and Step 1's hand-off cannot tell an edited patch
from a drawn one. Rectangles stay whole level-0 pixels, inside the slide,
and never smaller than PATCH_MIN_FULL_PX.

Own module: page-heavy Step0 suites segfault offscreen when combined.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtCore, QtGui, QtTest, QtWidgets  # noqa: E402
from PyQt5.QtCore import Qt  # noqa: E402

from block01.ui.step0 import overview_panel as ovp  # noqa: E402
from block01.ui.step0 import step0_page as sp  # noqa: E402

from test_step0_background_correction_tab import (  # noqa: E402
    _GpuPathLoader,
    app,            # noqa: F401  (pytest fixture)
)


SLIDE_H, SLIDE_W = 8000, 6000
DS = 10

# The patches every test starts from, and where they land on the thumbnail:
# full-image (1000, 2000, 1000, 2000) is overview x=100 y=100 w=100 h=100.
P1 = (1000, 2000, 1000, 2000)
P2 = (4000, 5000, 3000, 4000)


class _Loader:
    shape = (SLIDE_H, SLIDE_W)
    ch_map = {"DAPI": 0}

    def channel_names(self):
        return ["DAPI"]


# ── driving the panel with real Qt events ────────────────────────────────

def _panel(mode=None):
    """A panel over a fake slide, laid out and fitted so that viewport
    pixels really do map to overview pixels."""
    panel = ovp.OverviewPanel(_Loader(), "DAPI", lazy=True)
    panel.ds = DS
    panel.ov_h, panel.ov_w = SLIDE_H // DS, SLIDE_W // DS
    panel.full_h, panel.full_w = SLIDE_H, SLIDE_W
    panel.resize(500, 500)
    panel.show()
    QtTest.QTest.qWaitForWindowExposed(panel)
    panel.vb.setRange(
        QtCore.QRectF(0, 0, panel.ov_w, panel.ov_h), padding=0)
    QtTest.QTest.qWait(20)
    panel._set_mode(mode)
    return panel


def _viewport_pos(panel, r, c):
    """Overview (row, col) -> a point on the panel's viewport."""
    scene = panel.vb.mapViewToScene(QtCore.QPointF(float(c), float(r)))
    p = panel.gview.mapFromScene(scene)
    return QtCore.QPoint(int(round(p.x())), int(round(p.y())))


def _ov_at(panel, pos):
    """Where a viewport point actually lands, in overview pixels -- the
    integer viewport grid is coarser than the overview, so the expected
    result of a gesture is read back from the points it was made with."""
    return panel._ov_pos_f(panel.gview.mapToScene(pos))


def _send(panel, kind, pos, button=Qt.LeftButton, buttons=None, mods=Qt.NoModifier):
    ev = QtGui.QMouseEvent(
        kind, QtCore.QPointF(pos),
        button,
        buttons if buttons is not None else button,
        mods)
    QtWidgets.QApplication.instance().sendEvent(panel.gview.viewport(), ev)


def _click(panel, r, c):
    pos = _viewport_pos(panel, r, c)
    _send(panel, QtCore.QEvent.MouseButtonPress, pos)
    _send(panel, QtCore.QEvent.MouseButtonRelease, pos, buttons=Qt.NoButton)
    return pos


def _drag(panel, r0, c0, r1, c1, steps=3):
    """Press at one overview point, move to another, release. Returns the
    (row, col) the press and the release actually landed on."""
    p0 = _viewport_pos(panel, r0, c0)
    p1 = _viewport_pos(panel, r1, c1)
    _send(panel, QtCore.QEvent.MouseButtonPress, p0)
    for i in range(1, steps + 1):
        mid = QtCore.QPoint(
            p0.x() + (p1.x() - p0.x()) * i // steps,
            p0.y() + (p1.y() - p0.y()) * i // steps)
        _send(panel, QtCore.QEvent.MouseMove, mid,
              button=Qt.NoButton, buttons=Qt.LeftButton)
    _send(panel, QtCore.QEvent.MouseButtonRelease, p1, buttons=Qt.NoButton)
    return _ov_at(panel, p0), _ov_at(panel, p1)


def _rect(panel, idx):
    return tuple(panel._patches[idx]["coords"])


def _pen_width(panel, idx):
    return panel._patch_artists[idx][0].pen().width()


def _grips(panel, idx):
    return panel._patch_artists[idx][0].childItems()


# ── where a patch's furniture is, in overview coordinates ────────────────

def _top_border(panel, idx):
    """The middle of the patch's top edge -- the click that adjusts it."""
    x, y, w, h = panel._patch_display_rect(idx)
    return (y, x + w / 2.0)


def _interior(panel, idx):
    """The middle of the patch -- which is still just tissue."""
    x, y, w, h = panel._patch_display_rect(idx)
    return (y + h / 2.0, x + w / 2.0)


def _label(panel, idx):
    """The middle of the "P1" tag, which hangs above the top-left corner."""
    x0, y0, x1, y1 = panel._patch_label_rect(idx)
    return ((y0 + y1) / 2.0, (x0 + x1) / 2.0)


BARE = (600, 500)          # tissue far from any patch in these tests


# ── 1. there is no cap ───────────────────────────────────────────────────

def test_a_roi_takes_as_many_patches_as_the_user_draws(app):
    panel = _panel("patch")

    for i in range(6):
        y0 = 1000 + i * 300
        panel._add_patch(y0, y0 + 200, 500, 700,
                         y0 // DS, (y0 + 200) // DS, 50, 70, None)

    assert len(panel._patches) == 6
    assert len(panel.get_patches()) == 6
    assert "Maximum" not in panel.status.text()


def test_the_cap_is_gone_from_the_panel_altogether(app):
    """No dormant limit anyone can switch back on by calling it."""
    assert not hasattr(ovp.OverviewPanel, "_max_patches_for_roi")


def test_the_page_keeps_every_drawn_patch(app):
    page = _page()

    for i in range(6):
        y0 = 1000 + i * 300
        page.overview.add_patch_rect(y0, y0 + 200, 500, 700)

    assert len(page.patches) == 6
    assert page._patch_list.count() == 6
    assert not page._patch_warning.isVisible()


# ── 2. entering the adjust state: the border and the label ───────────────

def test_clicking_a_patch_border_adjusts_it(app):
    panel = _panel()
    panel.add_patch_rect(*P1)
    panel.add_patch_rect(*P2)

    _click(panel, *_top_border(panel, 1))

    assert panel.is_adjusting_patch()
    assert panel._selected_patch_idx == 1
    assert _pen_width(panel, 1) > _pen_width(panel, 0)
    assert len(_grips(panel, 1)) == len(ovp.PATCH_HANDLE_DIRS)
    assert _grips(panel, 0) == [], "an unselected patch carries no handles"


def test_clicking_a_patch_label_adjusts_it(app):
    panel = _panel()
    panel.add_patch_rect(*P1)
    r, c = _label(panel, 0)
    x, y, w, h = panel._patch_display_rect(0)
    assert y - r > panel._patch_border_tol(), \
        "the label must sit clear of the border band, or it proves nothing"

    _click(panel, r, c)

    assert panel._selected_patch_idx == 0


def test_a_border_click_adjusts_while_the_roi_tool_is_out(app):
    """The mode buttons stay pressed all day; adjusting must not need them off."""
    panel = _panel("roi")
    panel.add_patch_rect(*P1)

    _click(panel, *_top_border(panel, 0))

    assert panel._selected_patch_idx == 0
    assert panel._cur_pts == [], "no vertex was added"


def test_a_border_click_adjusts_while_the_patch_tool_is_out(app):
    panel = _panel("patch")
    panel.add_patch_rect(*P1)

    _click(panel, *_top_border(panel, 0))

    assert panel._selected_patch_idx == 0
    assert len(panel._patches) == 1, "no rectangle was drawn"


def test_the_interior_of_a_patch_is_still_tissue_in_patch_mode(app):
    """Drawing over an existing patch has to keep working: patches overlap."""
    panel = _panel("patch")
    panel.add_patch_rect(*P1)

    r, c = _interior(panel, 0)
    _drag(panel, r - 20, c - 20, r + 20, c + 20)

    assert len(panel._patches) == 2, "the interior drew a new patch"
    assert panel._selected_patch_idx == -1, "and adjusted nothing"
    assert _rect(panel, 0) == P1


def test_the_interior_of_a_patch_is_still_tissue_in_navigate_mode(app):
    panel = _panel()
    panel.add_patch_rect(*P1)
    seen = []
    panel.navigate_requested.connect(lambda y, x: seen.append((y, x)))

    _click(panel, *_interior(panel, 0))

    assert len(seen) == 1, "the interior navigated"
    assert panel._selected_patch_idx == -1


def test_the_interior_of_a_patch_is_still_tissue_in_roi_mode(app):
    panel = _panel("roi")
    panel.add_patch_rect(*P1)

    _click(panel, *_interior(panel, 0))

    assert len(panel._cur_pts) == 1
    assert panel._selected_patch_idx == -1
    assert _rect(panel, 0) == P1


def test_entering_the_adjust_state_changes_no_patch(app):
    panel = _panel()
    panel.add_patch_rect(*P1)
    seen = []
    panel.patches_changed.connect(seen.append)

    _click(panel, *_top_border(panel, 0))

    assert panel._selected_patch_idx == 0
    assert seen == [], "selection is a view state, not an edit"


def test_the_hint_says_which_patch_is_being_adjusted(app):
    panel = _panel()
    panel.add_patch_rect(*P1)
    mode_hint = panel.hint.text()

    panel._select_patch_artist(0)
    assert "Adjusting P1" in panel.hint.text()

    panel._select_patch_artist(-1)
    assert panel.hint.text() == mode_hint


# ── 3. leaving the adjust state ──────────────────────────────────────────

def test_a_click_off_the_patch_ends_the_adjustment_and_does_nothing_else(app):
    panel = _panel()
    panel.add_patch_rect(*P1)
    panel._select_patch_artist(0)
    nav, edits = [], []
    panel.navigate_requested.connect(lambda y, x: nav.append((y, x)))
    panel.patches_changed.connect(edits.append)

    _click(panel, *BARE)

    assert not panel.is_adjusting_patch()
    assert nav == [], "the click that finishes an adjustment does not navigate"
    assert edits == []


def test_the_next_click_navigates_again(app):
    panel = _panel()
    panel.add_patch_rect(*P1)
    panel._select_patch_artist(0)
    nav = []
    panel.navigate_requested.connect(lambda y, x: nav.append((y, x)))

    _click(panel, *BARE)                    # finishes the adjustment
    _click(panel, *BARE)                    # and this one is a click again

    assert len(nav) == 1
    y, x = nav[0]
    assert (y, x) == pytest.approx((6000, 5000), abs=3 * DS)


def test_leaving_the_adjustment_draws_no_patch(app):
    panel = _panel("patch")
    panel.add_patch_rect(*P1)
    panel._select_patch_artist(0)

    _drag(panel, 600, 500, 640, 540)        # a drag on bare tissue

    assert not panel.is_adjusting_patch()
    assert len(panel._patches) == 1, "the finishing gesture drew nothing"
    assert _rect(panel, 0) == P1


def test_leaving_the_adjustment_adds_no_roi_vertex(app):
    panel = _panel("roi")
    panel.add_patch_rect(*P1)
    panel._select_patch_artist(0)

    _click(panel, *BARE)

    assert not panel.is_adjusting_patch()
    assert panel._cur_pts == []


def test_after_leaving_the_adjustment_the_tool_draws_again(app):
    panel = _panel("patch")
    panel.add_patch_rect(*P1)
    panel._select_patch_artist(0)

    _click(panel, *BARE)                    # finishes the adjustment
    _drag(panel, 600, 500, 640, 540)        # and now the tool is back

    assert len(panel._patches) == 2


# ── 4. dragging moves the patch under adjustment ─────────────────────────

def test_dragging_a_patch_moves_it_by_the_dragged_distance(app):
    panel = _panel()
    panel.add_patch_rect(*P1)
    panel._select_patch_artist(0)
    seen = []
    panel.patches_changed.connect(seen.append)

    start, end = _drag(panel, 150, 150, 250, 220)

    dy = int(round((end[0] - start[0]) * DS))
    dx = int(round((end[1] - start[1]) * DS))
    assert _rect(panel, 0) == (1000 + dy, 2000 + dy, 1000 + dx, 2000 + dx)
    # ... and that is roughly the 100 x 70 overview pixels asked for
    assert (dy, dx) == pytest.approx((1000, 700), abs=2 * DS)
    assert len(seen) == 1, "one gesture is one edit"
    assert seen[0] == [_rect(panel, 0)]
    assert all(isinstance(v, int) for v in _rect(panel, 0))


def test_a_patch_can_be_moved_while_the_patch_tool_is_out(app):
    """The whole point of the explicit state: the tool is suspended, not off."""
    panel = _panel("patch")
    panel.add_patch_rect(*P1)
    panel._select_patch_artist(0)
    seen = []
    panel.patches_changed.connect(seen.append)

    _drag(panel, 150, 150, 250, 220)

    assert len(panel._patches) == 1, "no new rectangle was drawn"
    assert _rect(panel, 0) != P1
    assert len(seen) == 1


def test_a_move_is_clamped_to_the_slide(app):
    panel = _panel()
    panel.add_patch_rect(*P1)
    panel._select_patch_artist(0)

    _drag(panel, 150, 150, 20, 20)              # aimed off the top-left

    y0, y1, x0, x1 = _rect(panel, 0)
    assert (y0, x0) == (0, 0)
    assert (y1, x1) == (1000, 1000), "the rectangle keeps its size"


def test_the_border_press_adjusts_before_it_drags(app):
    """A patch nobody had selected can be picked up in one gesture."""
    panel = _panel()
    panel.add_patch_rect(*P1)
    panel.add_patch_rect(*P2)
    assert panel._selected_patch_idx == -1
    r, c = _top_border(panel, 1)

    _drag(panel, r, c, r + 20, c + 20)

    assert panel._selected_patch_idx == 1
    assert _rect(panel, 0) == P1
    assert _rect(panel, 1) != P2


# ── 5. dragging a grip resizes it ────────────────────────────────────────

def test_a_corner_handle_resizes_the_patch(app):
    panel = _panel()
    panel.add_patch_rect(*P1)
    panel._select_patch_artist(0)
    seen = []
    panel.patches_changed.connect(seen.append)

    start, end = _drag(panel, 200, 200, 260, 250)   # bottom-right grip

    dy = int(round((end[0] - start[0]) * DS))
    dx = int(round((end[1] - start[1]) * DS))
    assert _rect(panel, 0) == (1000, 2000 + dy, 1000, 2000 + dx)
    assert len(seen) == 1


def test_an_edge_handle_moves_only_that_edge(app):
    panel = _panel()
    panel.add_patch_rect(*P1)
    panel._select_patch_artist(0)

    start, end = _drag(panel, 100, 150, 60, 150)    # top edge, upwards

    dy = int(round((end[0] - start[0]) * DS))
    assert _rect(panel, 0) == (1000 + dy, 2000, 1000, 2000)


def test_a_resize_cannot_shrink_a_patch_to_nothing(app):
    panel = _panel()
    panel.add_patch_rect(*P1)
    panel._select_patch_artist(0)

    _drag(panel, 100, 150, 400, 150)      # drag the top edge past the bottom

    y0, y1, x0, x1 = _rect(panel, 0)
    assert y1 - y0 == ovp.PATCH_MIN_FULL_PX
    assert y1 == 2000, "the edge nobody dragged stayed put"


def test_a_resize_is_clamped_to_the_slide(app):
    panel = _panel()
    panel.add_patch_rect(*P1)
    panel._select_patch_artist(0)

    _drag(panel, 100, 150, -120, 150)     # top edge, off the slide

    assert _rect(panel, 0)[0] == 0


# ── 6. Delete removes the patch under adjustment ─────────────────────────

def test_delete_removes_the_adjusted_patch(app):
    panel = _panel()
    panel.add_patch_rect(*P1)
    panel.add_patch_rect(*P2)
    _click(panel, *_top_border(panel, 1))
    seen = []
    panel.patches_changed.connect(seen.append)

    QtTest.QTest.keyClick(panel.gview.viewport(), Qt.Key_Delete)

    assert [p["coords"] for p in panel._patches] == [P1]
    assert seen == [[P1]]
    assert not panel.is_adjusting_patch(), \
        "there is nothing left to adjust, so the state is over"


def test_delete_removes_the_adjusted_patch_in_roi_mode(app):
    panel = _panel("roi")
    panel.add_patch_rect(*P1)
    _click(panel, *_top_border(panel, 0))

    QtTest.QTest.keyClick(panel.gview.viewport(), Qt.Key_Backspace)

    assert panel._patches == []


def test_delete_with_nothing_adjusted_removes_nothing(app):
    panel = _panel()
    panel.add_patch_rect(*P1)
    seen = []
    panel.patches_changed.connect(seen.append)

    QtTest.QTest.keyClick(panel.gview.viewport(), Qt.Key_Delete)

    assert len(panel._patches) == 1
    assert seen == []


# ── 7. the patch list and the thumbnail agree ────────────────────────────

def _page():
    page = sp.Step0Page()
    page.loader = _GpuPathLoader()
    page.nucleus_channel = "DAPI"
    page.overview.loader = page.loader
    page.overview.ds = DS
    page.overview.ov_h, page.overview.ov_w = SLIDE_H // DS, SLIDE_W // DS
    page.overview.full_h, page.overview.full_w = SLIDE_H, SLIDE_W
    return page


def test_adjusting_on_the_thumbnail_moves_the_patch_list(app):
    page = _page()
    page.overview.add_patch_rect(*P1)
    page.overview.add_patch_rect(*P2)

    page.overview._select_patch_artist(1)

    assert page._patch_list.currentRow() == 1
    assert page._patch_selected_idx == 1


def _click_patch_name(page, row):
    """A real click on a row of the page's patch list."""
    lst = page._patch_list
    lst.setParent(None)                     # the page itself is never shown
    lst.resize(240, 120)
    lst.show()
    QtTest.QTest.qWaitForWindowExposed(lst)
    QtTest.QTest.mouseClick(
        lst.viewport(), Qt.LeftButton, Qt.NoModifier,
        lst.visualItemRect(lst.item(row)).center())


def test_clicking_a_name_in_the_patch_list_adjusts_that_patch(app):
    page = _page()
    page.overview.add_patch_rect(*P1)
    page.overview.add_patch_rect(*P2)

    _click_patch_name(page, 1)

    assert page.overview._selected_patch_idx == 1
    assert page.overview.is_adjusting_patch()


def test_the_list_adjusts_even_the_row_it_already_calls_current(app):
    """A rebuilt list always has a current row; clicking it still means it."""
    page = _page()
    page.overview.add_patch_rect(*P1)
    assert page._patch_list.currentRow() == 0
    assert not page.overview.is_adjusting_patch()

    _click_patch_name(page, 0)

    assert page.overview._selected_patch_idx == 0


def test_selecting_a_name_in_the_patch_list_adjusts_that_patch(app):
    page = _page()
    page.overview.add_patch_rect(*P1)
    page.overview.add_patch_rect(*P2)

    page._patch_list.setCurrentRow(1)

    assert page.overview._selected_patch_idx == 1


def test_clearing_the_patch_list_selection_ends_the_adjustment(app):
    page = _page()
    page.overview.add_patch_rect(*P1)
    page.overview.add_patch_rect(*P2)
    page._patch_list.setCurrentRow(1)
    assert page.overview.is_adjusting_patch()

    page._patch_list.clearSelection()

    assert not page.overview.is_adjusting_patch()


def test_drawing_a_patch_does_not_adjust_anything(app):
    """Rebuilding the list must not arm the state nobody asked for."""
    page = _page()

    page.overview.add_patch_rect(*P1)

    assert not page.overview.is_adjusting_patch()


def test_an_edit_on_the_thumbnail_reaches_the_patch_list(app):
    page = _page()
    page.overview.add_patch_rect(*P1)

    page.overview._commit_patch_geometry(0, (1500, 2500, 1200, 2200))

    assert page.patches == [(1500, 2500, 1200, 2200)]
    assert "1000x1000px" in page._patch_list.item(0).text()


def test_an_edit_leaves_the_patch_still_under_adjustment(app):
    page = _page()
    page.overview.add_patch_rect(*P1)
    page.overview._select_patch_artist(0)

    page.overview._commit_patch_geometry(0, (1500, 2500, 1200, 2200))

    assert page.overview._selected_patch_idx == 0, \
        "the list rebuild must not knock the patch out of the editor"


def test_deleting_on_the_thumbnail_ends_the_adjustment_everywhere(app):
    page = _page()
    page.overview.add_patch_rect(*P1)
    page.overview.add_patch_rect(*P2)
    page.overview._select_patch_artist(1)

    page.overview._remove_patch(1)

    assert not page.overview.is_adjusting_patch()
    assert page.patches == [P1]
