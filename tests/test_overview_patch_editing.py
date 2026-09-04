"""Patches are rectangles you can pick up, move, resize and delete.

Two complaints, one answer. There were never more than four patches per ROI
-- an arbitrary cap on a question ("how many places on this slide are worth
measuring?") that only the person looking at the tissue can answer -- and a
patch, once drawn, was frozen: a rectangle two hundred pixels too far left
had to be deleted and drawn again, by hand, on a thumbnail where one pixel
is thirty-two slide pixels.

So: the cap is gone, and the Tissue Preview edits patches directly. Click a
patch to select it (thicker outline, eight grips); drag its body to move it,
drag a grip to resize it; Delete removes it. The gesture commits on release
through the SAME `patches_changed` path a drawn patch uses -- once per
gesture, never per mouse-move -- so the patch list, the ROI bookkeeping and
Step 1's hand-off cannot tell an edited patch from a drawn one. Rectangles
stay whole level-0 pixels, inside the slide, and never smaller than one
overview pixel.

Editing lives in navigate mode only. While a drawing tool is active, drawing
a new shape wins -- a press in patch mode still starts a new rectangle even
on top of an existing one. In navigate mode a click on bare tissue still
means "take me there", and drops the selection on its way.

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


# ── 2. clicking selects ──────────────────────────────────────────────────

def test_clicking_a_patch_selects_and_highlights_it(app):
    panel = _panel()
    panel.add_patch_rect(1000, 2000, 1000, 2000)
    panel.add_patch_rect(4000, 5000, 3000, 4000)

    _click(panel, 450, 350)                     # inside the second patch

    assert panel._selected_patch_idx == 1
    assert _pen_width(panel, 1) > _pen_width(panel, 0)
    assert len(_grips(panel, 1)) == len(ovp.PATCH_HANDLE_DIRS)
    assert _grips(panel, 0) == [], "an unselected patch carries no handles"


def test_selecting_a_patch_changes_no_patch(app):
    panel = _panel()
    panel.add_patch_rect(1000, 2000, 1000, 2000)
    seen = []
    panel.patches_changed.connect(seen.append)

    _click(panel, 150, 150)

    assert panel._selected_patch_idx == 0
    assert seen == [], "selection is a view state, not an edit"


def test_a_click_on_bare_tissue_navigates_and_deselects(app):
    panel = _panel()
    panel.add_patch_rect(1000, 2000, 1000, 2000)
    panel._select_patch_artist(0)
    seen = []
    panel.navigate_requested.connect(lambda y, x: seen.append((y, x)))
    edits = []
    panel.patches_changed.connect(edits.append)

    _click(panel, 600, 500)                     # far from the patch

    assert panel._selected_patch_idx == -1
    assert len(seen) == 1
    y, x = seen[0]
    assert (y, x) == pytest.approx((6000, 5000), abs=3 * DS)
    assert edits == []


# ── 3. dragging moves the selected patch ─────────────────────────────────

def test_dragging_a_patch_moves_it_by_the_dragged_distance(app):
    panel = _panel()
    panel.add_patch_rect(1000, 2000, 1000, 2000)
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


def test_a_move_is_clamped_to_the_slide(app):
    panel = _panel()
    panel.add_patch_rect(1000, 2000, 1000, 2000)

    _drag(panel, 150, 150, 20, 20)              # aimed off the top-left

    y0, y1, x0, x1 = _rect(panel, 0)
    assert (y0, x0) == (0, 0)
    assert (y1, x1) == (1000, 1000), "the rectangle keeps its size"


def test_the_first_press_selects_before_it_drags(app):
    """A patch nobody had selected can be picked up in one gesture."""
    panel = _panel()
    panel.add_patch_rect(1000, 2000, 1000, 2000)
    panel.add_patch_rect(4000, 5000, 3000, 4000)
    assert panel._selected_patch_idx == -1

    _drag(panel, 450, 350, 470, 370)

    assert panel._selected_patch_idx == 1
    assert _rect(panel, 0) == (1000, 2000, 1000, 2000)
    assert _rect(panel, 1) != (4000, 5000, 3000, 4000)


# ── 4. dragging a grip resizes it ────────────────────────────────────────

def test_a_corner_handle_resizes_the_patch(app):
    panel = _panel()
    panel.add_patch_rect(1000, 2000, 1000, 2000)
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
    panel.add_patch_rect(1000, 2000, 1000, 2000)
    panel._select_patch_artist(0)

    start, end = _drag(panel, 100, 150, 60, 150)    # top edge, upwards

    dy = int(round((end[0] - start[0]) * DS))
    assert _rect(panel, 0) == (1000 + dy, 2000, 1000, 2000)


def test_a_resize_cannot_shrink_a_patch_to_nothing(app):
    panel = _panel()
    panel.add_patch_rect(1000, 2000, 1000, 2000)
    panel._select_patch_artist(0)

    _drag(panel, 100, 150, 400, 150)      # drag the top edge past the bottom

    y0, y1, x0, x1 = _rect(panel, 0)
    assert y1 - y0 == ovp.PATCH_MIN_FULL_PX
    assert y1 == 2000, "the edge nobody dragged stayed put"


def test_a_resize_is_clamped_to_the_slide(app):
    panel = _panel()
    panel.add_patch_rect(1000, 2000, 1000, 2000)
    panel._select_patch_artist(0)

    _drag(panel, 100, 150, -120, 150)     # top edge, off the slide

    assert _rect(panel, 0)[0] == 0


# ── 5. Delete removes the selected patch ─────────────────────────────────

def test_delete_removes_the_selected_patch(app):
    panel = _panel()
    panel.add_patch_rect(1000, 2000, 1000, 2000)
    panel.add_patch_rect(4000, 5000, 3000, 4000)
    _click(panel, 450, 350)
    seen = []
    panel.patches_changed.connect(seen.append)

    QtTest.QTest.keyClick(panel.gview.viewport(), Qt.Key_Delete)

    assert [p["coords"] for p in panel._patches] == [(1000, 2000, 1000, 2000)]
    assert seen == [[(1000, 2000, 1000, 2000)]]


def test_delete_with_nothing_selected_removes_nothing(app):
    panel = _panel()
    panel.add_patch_rect(1000, 2000, 1000, 2000)
    seen = []
    panel.patches_changed.connect(seen.append)

    QtTest.QTest.keyClick(panel.gview.viewport(), Qt.Key_Delete)

    assert len(panel._patches) == 1
    assert seen == []


# ── 6. the drawing tools are untouched ───────────────────────────────────

def test_in_patch_mode_a_drag_over_a_patch_draws_a_new_one(app):
    panel = _panel("patch")
    panel.add_patch_rect(1000, 2000, 1000, 2000)
    panel._select_patch_artist(0)

    _drag(panel, 120, 120, 180, 180)

    assert len(panel._patches) == 2, "drawing wins while the tool is out"
    assert _rect(panel, 0) == (1000, 2000, 1000, 2000)


def test_in_roi_mode_a_click_over_a_patch_still_adds_a_vertex(app):
    panel = _panel("roi")
    panel.add_patch_rect(1000, 2000, 1000, 2000)
    panel._select_patch_artist(0)

    _click(panel, 150, 150)

    assert len(panel._cur_pts) == 1
    assert _rect(panel, 0) == (1000, 2000, 1000, 2000)


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


def test_selecting_on_the_thumbnail_moves_the_patch_list(app):
    page = _page()
    page.overview.add_patch_rect(1000, 2000, 1000, 2000)
    page.overview.add_patch_rect(4000, 5000, 3000, 4000)

    page.overview._select_patch_artist(1)

    assert page._patch_list.currentRow() == 1
    assert page._patch_selected_idx == 1


def test_selecting_in_the_patch_list_highlights_the_thumbnail(app):
    page = _page()
    page.overview.add_patch_rect(1000, 2000, 1000, 2000)
    page.overview.add_patch_rect(4000, 5000, 3000, 4000)

    page._patch_list.setCurrentRow(1)

    assert page.overview._selected_patch_idx == 1


def test_an_edit_on_the_thumbnail_reaches_the_patch_list(app):
    page = _page()
    page.overview.add_patch_rect(1000, 2000, 1000, 2000)

    page.overview._commit_patch_geometry(0, (1500, 2500, 1200, 2200))

    assert page.patches == [(1500, 2500, 1200, 2200)]
    assert "1000x1000px" in page._patch_list.item(0).text()
