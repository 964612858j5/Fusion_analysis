"""The middle button pans the Tissue Preview, in every mode.

The thumbnail is a map you have to be able to move under a zoom, and the
left button is spoken for everywhere: it draws rectangles in patch mode,
adds vertices in ROI mode, navigates with no tool out, and edits the
selected rectangle in the adjust state. The middle button is the one gesture
that means the same thing in all four, so it is the one that pans.

Pinned here because it is a cross-cutting gesture: every branch of the event
filter has to let it through, and none of them may let it also draw, also
navigate or also edit.
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
    assert panel._pan_last is None

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
