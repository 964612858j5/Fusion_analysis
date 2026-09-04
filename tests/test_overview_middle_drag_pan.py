"""The middle button pans the Tissue Preview, in every mode.

The thumbnail is a map you have to be able to move under a zoom, and the
left button is spoken for everywhere: it draws rectangles in patch mode,
adds vertices in ROI mode, navigates with no tool out, and edits the
selected rectangle in the adjust state. The middle button is the one gesture
that means the same thing in all four, so it is the one that pans.

Pinned here because it is a cross-cutting gesture: every branch of the event
filter has to let it through, and none of them may let it also draw, also
navigate or also edit.

The gesture belongs to the ViewBox (`_PanViewBox.mouseDragEvent`), so these
drags are delivered the way the platform delivers them and walk pyqtgraph's
whole stack -- viewport, graphics view, scene, the items under the cursor --
before anything pans. That is the point: the panel used to catch the middle
press in its viewport filter and consume it, which worked only on the one
path that reaches that filter, and the last section here drives the gesture
through a real assembled page and a real window to say so.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtCore, QtGui, QtTest, QtWidgets  # noqa: E402
from PyQt5.QtCore import Qt  # noqa: E402

from block01.ui.step0 import overview_panel as ovp  # noqa: E402

from test_step0_background_correction_tab import app  # noqa: E402,F401


SLIDE_H, SLIDE_W = 8000, 6000
DS = 10
P1 = (1000, 2000, 1000, 2000)      # overview x=100 y=100 w=100 h=100


class _Loader:
    shape = (SLIDE_H, SLIDE_W)
    ch_map = {"DAPI": 0}

    def channel_names(self):
        return ["DAPI"]

    def read_channel(self, *_a, **_kw):
        import numpy as np
        return np.zeros((SLIDE_H // DS, SLIDE_W // DS), "float32")


def _panel(mode=None, *, zoomed=True):
    panel = ovp.OverviewPanel(_Loader(), "DAPI", lazy=True)
    panel.ds = DS
    panel.ov_h, panel.ov_w = SLIDE_H // DS, SLIDE_W // DS
    panel.full_h, panel.full_w = SLIDE_H, SLIDE_W
    panel.resize(500, 500)
    panel.show()
    QtTest.QTest.qWaitForWindowExposed(panel)
    if zoomed:
        # Zoomed IN, which is the only state in which panning means
        # anything -- and the state the complaint was made from.
        panel.vb.setRange(QtCore.QRectF(100, 100, 200, 200), padding=0)
    else:
        panel.vb.setRange(
            QtCore.QRectF(0, 0, panel.ov_w, panel.ov_h), padding=0)
    QtTest.QTest.qWait(20)
    panel._set_mode(mode)
    return panel


def _send(panel, kind, pos, button=Qt.LeftButton, buttons=None,
          mods=Qt.NoModifier):
    ev = QtGui.QMouseEvent(
        kind, QtCore.QPointF(pos), button,
        buttons if buttons is not None else button, mods)
    QtWidgets.QApplication.instance().sendEvent(panel.gview.viewport(), ev)


def _middle_drag(panel, dx, dy, steps=4, start=(250, 250)):
    """Press the middle button and move by `(dx, dy)` viewport pixels."""
    p0 = QtCore.QPoint(*start)
    _send(panel, QtCore.QEvent.MouseButtonPress, p0, button=Qt.MiddleButton)
    for i in range(1, steps + 1):
        _send(panel, QtCore.QEvent.MouseMove,
              QtCore.QPoint(p0.x() + dx * i // steps,
                            p0.y() + dy * i // steps),
              button=Qt.NoButton, buttons=Qt.MiddleButton)
    _send(panel, QtCore.QEvent.MouseButtonRelease,
          QtCore.QPoint(p0.x() + dx, p0.y() + dy),
          button=Qt.MiddleButton, buttons=Qt.NoButton)


def _range(panel):
    (x0, x1), (y0, y1) = panel.vb.viewRange()
    return (x0, x1, y0, y1)


def _viewport_pos(panel, r, c):
    scene = panel.vb.mapViewToScene(QtCore.QPointF(float(c), float(r)))
    p = panel.gview.mapFromScene(scene)
    return QtCore.QPoint(int(round(p.x())), int(round(p.y())))


# ── it pans, in every mode ───────────────────────────────────────────────

@pytest.mark.parametrize("mode", [None, "patch", "roi"])
def test_a_middle_drag_pans_the_thumbnail(app, mode):
    panel = _panel(mode)
    before = _range(panel)

    _middle_drag(panel, -60, -40)

    after = _range(panel)
    assert after != before, f"mode {mode!r}: the thumbnail did not move"
    # Dragging the mouse LEFT moves the content left, i.e. the view moves
    # RIGHT: the thumbnail follows the hand, as a map does.
    assert after[0] > before[0] and after[2] > before[2]
    # A pan is a translation: the magnification is untouched.
    assert (after[1] - after[0]) == pytest.approx(before[1] - before[0])
    assert (after[3] - after[2]) == pytest.approx(before[3] - before[2])


@pytest.mark.parametrize("mode", [None, "patch", "roi"])
def test_a_middle_drag_pans_while_a_patch_is_being_adjusted(app, mode):
    """The adjust state owns the mouse -- a press on it edits the rectangle
    and a press off it ends the state -- but panning is not an edit, so the
    middle button still moves the map and the patch stays selected."""
    panel = _panel(mode)
    panel.add_patch_rect(*P1)
    panel._select_patch_artist(0)
    assert panel.is_adjusting_patch() is True
    before = _range(panel)
    rect_before = tuple(panel._patches[0]["coords"])

    _middle_drag(panel, -50, -30)

    assert _range(panel) != before
    assert panel.is_adjusting_patch() is True, "panning ended the adjustment"
    assert tuple(panel._patches[0]["coords"]) == rect_before


# ── ...and it never does anything else ───────────────────────────────────

def test_a_middle_drag_draws_no_patch(app):
    panel = _panel("patch")
    _middle_drag(panel, -60, -40)
    assert panel._patches == []
    assert not panel._temp.isVisible()


def test_a_middle_drag_adds_no_roi_vertex(app):
    panel = _panel("roi")
    _middle_drag(panel, -60, -40)
    assert panel._cur_pts == []


def test_a_middle_drag_does_not_navigate(app):
    """With no tool out a left CLICK navigates. A middle drag must not."""
    panel = _panel(None)
    seen = []
    panel.navigate_requested.connect(lambda y, x: seen.append((y, x)))

    _middle_drag(panel, -60, -40)

    assert seen == []


def test_a_middle_drag_does_not_select_a_patch(app):
    """The border and the label are the patch's own furniture, so a LEFT
    click there enters the adjust state. A middle drag over the same pixels
    must not."""
    panel = _panel(None)
    panel.add_patch_rect(*P1)
    start = _viewport_pos(panel, 100, 100)      # P1's top-left corner

    _middle_drag(panel, 40, 40, start=(start.x(), start.y()))

    assert panel.is_adjusting_patch() is False


# ── the gesture ends cleanly ─────────────────────────────────────────────

def test_the_pan_stops_at_the_release(app):
    panel = _panel(None)
    _middle_drag(panel, -40, -40)

    after_release = _range(panel)
    _send(panel, QtCore.QEvent.MouseMove, QtCore.QPoint(400, 400),
          button=Qt.NoButton, buttons=Qt.NoButton)
    assert _range(panel) == after_release


def test_a_move_with_no_middle_press_pans_nothing(app):
    """A stray move with the middle button held but no press seen (the
    press went to another widget) must not jump the map."""
    panel = _panel(None)
    before = _range(panel)

    _send(panel, QtCore.QEvent.MouseMove, QtCore.QPoint(300, 300),
          button=Qt.NoButton, buttons=Qt.MiddleButton)

    assert _range(panel) == before


# ── the assembled page, through a real window ────────────────────────────
#
# Everything above sends the gesture straight at the thumbnail's viewport.
# That is the shape of the bug this section exists for: the pan used to be
# an event filter on that one widget, the filter passed, and the running
# application still did not move. So these drive the two OverviewPanels a
# loaded Step0 page actually owns -- its own and the Tissue Navigator
# popup's -- and hand the events to the WINDOW, which is what the platform
# does: Qt picks the child under the point, and every layer of pyqtgraph
# below it gets its chance at the press before anything pans.
#
# `QTest.mouseMove` cannot be used for a drag. It sets the cursor position
# and lets the platform generate the move, and the offscreen platform has
# no cursor: measured here as a press and a release with no move between
# them, which is a pan of nothing whatever the panel does.

from block01.ui.step0 import step0_page as sp            # noqa: E402
from test_step0_tissue_preview import _LowresLoader, _Tab  # noqa: E402


def _give_slide(panel, loader):
    """The geometry a loaded panel has: the slide, the downsample, and the
    thumbnail's own size in overview pixels."""
    panel.loader = loader
    panel.full_h, panel.full_w = loader.shape
    panel.ds = DS
    panel.ov_h = panel.full_h // DS
    panel.ov_w = panel.full_w // DS


def _loaded_page():
    page = sp.Step0Page()
    page.loader = _LowresLoader()
    page.ome_path = "/fake/slide.ome.tif"
    page.patches = []
    page.nucleus_channel = "DAPI"
    page._rebuild_channel_list()
    page.current_channel = "CD3"
    page._explore_tab = _Tab()
    _give_slide(page.overview, page.loader)
    page._sync_step0_to_workbench()
    # The page's own thumbnail lives in a section the Background Correction
    # layout keeps hidden; a gesture cannot be delivered to a widget that is
    # not on screen, so this test shows it.
    page._roi_patch_section.setVisible(True)
    page.resize(1800, 1100)
    page.show()
    QtTest.QTest.qWaitForWindowExposed(page)
    return page


def _window_drag(panel, dx, dy, steps=4):
    """Deliver a middle drag over `panel`'s thumbnail to its WINDOW.

    The window is where the platform puts a mouse event; Qt then routes it
    to the child widget under the point and to that widget's filters. This
    is the flow the running application has and the direct-to-viewport
    helper above does not.
    """
    host = panel.window()
    handle = host.windowHandle()
    assert handle is not None, "the panel's window was never created"
    vp = panel.gview.viewport()
    p0 = vp.mapTo(host, QtCore.QPoint(vp.width() // 2, vp.height() // 2))
    app_ = QtWidgets.QApplication.instance()

    def deliver(kind, pos, button, buttons):
        ev = QtGui.QMouseEvent(
            kind, QtCore.QPointF(pos),
            QtCore.QPointF(host.mapToGlobal(pos)), button, buttons,
            Qt.NoModifier)
        app_.sendEvent(handle, ev)
        app_.processEvents()

    deliver(QtCore.QEvent.MouseButtonPress, p0, Qt.MiddleButton,
            Qt.MiddleButton)
    for i in range(1, steps + 1):
        deliver(QtCore.QEvent.MouseMove,
                p0 + QtCore.QPoint(dx * i // steps, dy * i // steps),
                Qt.NoButton, Qt.MiddleButton)
    deliver(QtCore.QEvent.MouseButtonRelease, p0 + QtCore.QPoint(dx, dy),
            Qt.MiddleButton, Qt.NoButton)


def _zoom_in(panel):
    """A pan only means something under a zoom -- and the zoom is what the
    user has when they reach for the middle button."""
    panel.vb.setRange(
        QtCore.QRectF(panel.ov_w * 0.25, panel.ov_h * 0.25,
                      panel.ov_w * 0.5, panel.ov_h * 0.5),
        padding=0)
    QtTest.QTest.qWait(20)


def test_the_pages_own_thumbnail_pans_in_the_real_hierarchy(app):
    page = _loaded_page()
    panel = page.overview
    _zoom_in(panel)
    before = _range(panel)

    _window_drag(panel, -60, -40)

    after = _range(panel)
    assert after != before, "the page's Tissue Preview did not move"
    assert after[0] > before[0] and after[2] > before[2]
    assert (after[1] - after[0]) == pytest.approx(before[1] - before[0])

    # ...and the viewport rectangle the full image keeps pushing at it is
    # an overlay, not a camera: it must not put the map back.
    panel.set_current_view_rect((100, 900, 100, 500))
    QtTest.QTest.qWait(20)
    assert _range(panel) == pytest.approx(after)
    page.hide()


def test_the_navigator_popups_thumbnail_pans_too(app):
    """The popup's OverviewPanel is a SECOND instance, in a window of its
    own -- and it is the one the user actually drags."""
    page = _loaded_page()
    popup = page._ensure_tissue_navigator()
    _give_slide(popup.overview, page.loader)
    popup.show()
    QtTest.QTest.qWaitForWindowExposed(popup)
    panel = popup.overview
    assert panel is not page.overview
    _zoom_in(panel)
    before = _range(panel)

    _window_drag(panel, -60, -40)

    after = _range(panel)
    assert after != before, "the Tissue Navigator's preview did not move"
    assert after[0] > before[0] and after[2] > before[2]

    panel.set_current_view_rect((100, 900, 100, 500))
    QtTest.QTest.qWait(20)
    assert _range(panel) == pytest.approx(after)
    popup.hide()
    page.hide()


def test_the_box_would_have_swallowed_the_drag_on_its_own(app):
    """Why the gesture cannot be left to pyqtgraph: `ViewBox.mouseDragEvent`
    accepts every button and then multiplies the translation by
    `mouseEnabled`, which this panel turns off so the left button can draw.
    A stock box therefore takes the middle drag and does nothing with it --
    silently, which is exactly what the running application did."""
    import pyqtgraph as pg

    panel = _panel(None)
    assert panel.vb.state["mouseEnabled"] == [False, False]
    assert isinstance(panel.vb, ovp._PanViewBox)
    # The base class, on the very same state, is the do-nothing.
    before = _range(panel)
    pg.ViewBox.mouseDragEvent(panel.vb, _StubDrag())
    assert _range(panel) == before


class _StubDrag:
    """The minimum of a pyqtgraph MouseDragEvent the base class reads."""

    def accept(self):
        pass

    def button(self):
        return Qt.MiddleButton

    def pos(self):
        from pyqtgraph.Point import Point
        return Point(60, 40)

    def lastPos(self):
        from pyqtgraph.Point import Point
        return Point(0, 0)
