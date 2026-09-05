"""The middle button pans the Tissue Preview, in every mode.

The thumbnail is a map you have to be able to move under a zoom, and the
left button is spoken for everywhere: it draws rectangles in patch mode,
adds vertices in ROI mode, navigates with no tool out, and edits the
selected rectangle in the adjust state. The middle button is the one gesture
that means the same thing in all four, so it is the one that pans.

Pinned here because it is a cross-cutting gesture: every branch of the event
filter has to let it through, and none of them may let it also draw, also
navigate or also edit.

The gesture belongs to the VIEWPORT -- `OverviewPanel.eventFilter` on
`gview.viewport()` -- and the choice is measured rather than assumed. A
four-move middle drag delivered to the window in the real assembled
hierarchy reaches the viewport as press=1, move=4, release=1, while every
other object on the path (the window, the graphics view, the panel, the
scene) sees at most the press. pyqtgraph's `GraphicsScene` built a drag
event for only two of those four moves, which is the stutter; on the user's
machine, for the Tissue Navigator popup, it built none and the middle button
did nothing at all.

So the tests below pin the entry point itself, not just the outcome: that
the viewport receives the whole sequence, that each move pans exactly once,
and that the picture follows the cursor 1:1 rather than at some multiple of
it. `OverviewPanel` is one class with two instances -- the page's own
thumbnail and the popup's -- so there is one implementation to test, and the
last section drives both of them through a real window to say so.
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


# ── the entry point, and what each move is worth ─────────────────────────
#
# The three properties the gesture was rebuilt for. They are about the
# mechanism rather than the outcome, because "the range changed" was true of
# the broken version too -- it changed by the wrong amount, on half the
# moves, and on only one of the two thumbnails.


def _spy_on(obj):
    """Record the middle-button mouse events `obj` receives.

    Installed AFTER the panel's own filter, so it runs FIRST and sees the
    event whether or not the panel goes on to consume it.
    """
    seen = []

    class _Spy(QtCore.QObject):
        def eventFilter(self, _o, ev):
            t = ev.type()
            if t in (QtCore.QEvent.MouseButtonPress,
                     QtCore.QEvent.MouseButtonRelease,
                     QtCore.QEvent.MouseMove):
                try:
                    mid = (ev.button() == Qt.MiddleButton
                           or bool(ev.buttons() & Qt.MiddleButton))
                except RuntimeError:
                    mid = False
                if mid:
                    seen.append(t)
            return False

    spy = _Spy()
    obj.installEventFilter(spy)
    return seen, spy


def test_the_viewport_receives_the_whole_gesture():
    """One press, every move, one release -- all at the object that owns
    the pan. This is the measurement the design rests on."""
    panel = _panel()
    seen, spy = _spy_on(panel.gview.viewport())
    try:
        _middle_drag(panel, 60, 40, steps=4)
    finally:
        panel.gview.viewport().removeEventFilter(spy)
    assert seen.count(QtCore.QEvent.MouseButtonPress) == 1
    assert seen.count(QtCore.QEvent.MouseMove) == 4
    assert seen.count(QtCore.QEvent.MouseButtonRelease) == 1


def test_each_move_pans_exactly_once():
    """Four moves, four translations.

    The version this replaced went through pyqtgraph's scene, which built a
    drag event for two of the four and then double-counted each one. Both
    halves of that are wrong and only this assertion catches the first.
    """
    panel = _panel()
    calls = []
    orig = panel.vb.translateBy

    def counting(*a, **kw):
        calls.append((a, kw))
        return orig(*a, **kw)

    panel.vb.translateBy = counting
    try:
        _middle_drag(panel, 80, 40, steps=4)
    finally:
        panel.vb.translateBy = orig
    assert len(calls) == 4


def test_a_move_that_goes_nowhere_pans_nothing():
    """A move event at the same point is not a pan. Qt sends these, and a
    handler that translated by zero would still reset the target and emit a
    range change for a gesture that did not happen."""
    panel = _panel()
    p0 = QtCore.QPoint(250, 250)
    _send(panel, QtCore.QEvent.MouseButtonPress, p0, button=Qt.MiddleButton)
    calls = []
    orig = panel.vb.translateBy
    panel.vb.translateBy = lambda *a, **kw: (calls.append(1),
                                             orig(*a, **kw))[1]
    try:
        for _ in range(3):
            _send(panel, QtCore.QEvent.MouseMove, p0,
                  button=Qt.NoButton, buttons=Qt.MiddleButton)
    finally:
        panel.vb.translateBy = orig
    _send(panel, QtCore.QEvent.MouseButtonRelease, p0,
          button=Qt.MiddleButton, buttons=Qt.NoButton)
    assert calls == []


def test_the_picture_follows_the_cursor_one_to_one():
    """The slide point under the cursor at the press is under it at the
    release.

    This is what "drag the map" means, and it is the property the previous
    version got wrong: going through the scene it panned twice as far as the
    cursor moved, which is why the gesture felt like it was running away.
    """
    panel = _panel()
    start = QtCore.QPoint(250, 250)
    dx, dy = 60, 40
    grabbed = panel.vb.mapSceneToView(panel.gview.mapToScene(start))
    _middle_drag(panel, dx, dy, steps=4, start=(start.x(), start.y()))
    end = QtCore.QPoint(start.x() + dx, start.y() + dy)
    under_cursor = panel.vb.mapSceneToView(panel.gview.mapToScene(end))
    assert under_cursor.x() == pytest.approx(grabbed.x(), abs=0.5)
    assert under_cursor.y() == pytest.approx(grabbed.y(), abs=0.5)


def test_a_release_outside_the_thumbnail_still_ends_the_gesture():
    """Qt's implicit grab delivers the release here wherever the cursor is,
    and the gesture must not survive it: a later move with no button down
    would otherwise keep panning."""
    panel = _panel()
    p0 = QtCore.QPoint(250, 250)
    _send(panel, QtCore.QEvent.MouseButtonPress, p0, button=Qt.MiddleButton)
    _send(panel, QtCore.QEvent.MouseMove, QtCore.QPoint(300, 280),
          button=Qt.NoButton, buttons=Qt.MiddleButton)
    # Well outside the 500x500 widget.
    _send(panel, QtCore.QEvent.MouseButtonRelease, QtCore.QPoint(9000, 9000),
          button=Qt.MiddleButton, buttons=Qt.NoButton)
    assert panel._mid_pan_last is None
    settled = _range(panel)
    _send(panel, QtCore.QEvent.MouseMove, QtCore.QPoint(400, 400),
          button=Qt.NoButton, buttons=Qt.NoButton)
    assert _range(panel) == settled


def test_a_move_with_the_button_lost_ends_the_gesture():
    """A move whose buttons no longer carry the middle one means the
    release was never seen. Ending there is what stops a pan sticking to
    the cursor for the rest of the session."""
    panel = _panel()
    p0 = QtCore.QPoint(250, 250)
    _send(panel, QtCore.QEvent.MouseButtonPress, p0, button=Qt.MiddleButton)
    _send(panel, QtCore.QEvent.MouseMove, QtCore.QPoint(300, 280),
          button=Qt.NoButton, buttons=Qt.NoButton)
    assert panel._mid_pan_last is None


def test_both_thumbnails_run_the_same_implementation():
    """The page's own and the popup's are two instances of ONE class, which
    is what makes "one gesture, not two copies" checkable rather than a
    claim about how the code looks."""
    page = _loaded_page()
    page._ensure_tissue_navigator()
    popup = page._tissue_navigator_popup
    assert popup is not None
    assert type(popup.overview) is type(page.overview)
    assert (type(popup.overview)._middle_pan_move
            is type(page.overview)._middle_pan_move)
    assert popup.overview is not page.overview


def test_the_viewbox_no_longer_takes_the_gesture():
    """The pan is owned in one place. If the ViewBox grew its own middle
    handler again there would be two, and they would both fire."""
    assert "mouseDragEvent" not in vars(ovp._PanViewBox)


# ── the pointer is OWNED, not assumed ────────────────────────────────────
#
# The gesture used to rely on Qt's implicit grab: consume the press in the
# filter and trust that the viewport is bound to the pointer for the rest of
# the drag. The desk disproved it -- six consecutive middle drags produced
# two pans -- and it was always the wrong thing to rely on: the implicit
# grab is given to the widget that ACCEPTS the press in its own
# `mousePressEvent`, and a press consumed by an event filter never reaches
# that handler.
#
# So the panel takes the grab itself, and these tests ask Qt who holds it
# rather than asking whether the range happened to change. `mouseGrabber()`
# is a static of `QWidget` in PyQt5 and it is Qt's own bookkeeping, so it is
# truthful offscreen even though the offscreen platform warns that it cannot
# take a platform-level grab.
#
# The one thing these cannot do is the acceptance gate itself: twenty
# consecutive drags on the real machine, with a real pointer. Offscreen
# there is no cursor and every event is synthetic. What is checkable here is
# the mechanism that failed -- who holds the pointer, and that nothing can
# leave it held.

def _grabber():
    return QtWidgets.QWidget.mouseGrabber()


def test_the_press_takes_the_mouse_grab(app):
    """After the press, the application's mouse grabber IS this thumbnail's
    viewport -- named, not inferred from a side effect."""
    panel = _panel()
    assert _grabber() is None
    _send(panel, QtCore.QEvent.MouseButtonPress, QtCore.QPoint(250, 250),
          button=Qt.MiddleButton)
    try:
        assert _grabber() is panel.gview.viewport()
        assert panel._mid_pan_grab is panel.gview.viewport()
    finally:
        panel._middle_pan_cancel()


def test_the_release_gives_the_mouse_grab_back(app):
    panel = _panel()
    _middle_drag(panel, -50, -30)
    assert _grabber() is None
    assert panel._mid_pan_grab is None
    assert panel._mid_pan_last is None


def test_a_lost_button_gives_the_mouse_grab_back(app):
    """A move with the middle button no longer down means the release was
    never seen. A grab left standing there is not a stuck map, it is an
    application that answers no further input at all."""
    panel = _panel()
    _send(panel, QtCore.QEvent.MouseButtonPress, QtCore.QPoint(250, 250),
          button=Qt.MiddleButton)
    assert _grabber() is panel.gview.viewport()
    _send(panel, QtCore.QEvent.MouseMove, QtCore.QPoint(300, 280),
          button=Qt.NoButton, buttons=Qt.NoButton)
    assert _grabber() is None
    assert panel._mid_pan_grab is None


def test_hiding_the_thumbnail_gives_the_mouse_grab_back(app):
    """The Tissue Navigator is a popup: closing it while a drag is held is
    an ordinary thing for a user to do."""
    panel = _panel()
    _send(panel, QtCore.QEvent.MouseButtonPress, QtCore.QPoint(250, 250),
          button=Qt.MiddleButton)
    assert _grabber() is panel.gview.viewport()
    panel.hide()
    QtWidgets.QApplication.instance().processEvents()
    assert _grabber() is None
    assert panel._mid_pan_grab is None


def test_closing_the_thumbnail_gives_the_mouse_grab_back(app):
    panel = _panel()
    _send(panel, QtCore.QEvent.MouseButtonPress, QtCore.QPoint(250, 250),
          button=Qt.MiddleButton)
    panel.close()
    QtWidgets.QApplication.instance().processEvents()
    assert _grabber() is None
    assert panel._mid_pan_grab is None


def test_a_grab_taken_away_mid_gesture_stops_the_pan(app):
    """Somebody else grabbed the pointer -- a modal dialog, a menu. The
    moves that follow are not ours, and panning on them would drag the map
    under a cursor that is somewhere else."""
    panel = _panel()
    _send(panel, QtCore.QEvent.MouseButtonPress, QtCore.QPoint(250, 250),
          button=Qt.MiddleButton)
    thief = QtWidgets.QWidget()
    thief.resize(10, 10)
    thief.show()
    thief.grabMouse()
    assert _grabber() is thief
    before = _range(panel)

    _send(panel, QtCore.QEvent.MouseMove, QtCore.QPoint(320, 300),
          button=Qt.NoButton, buttons=Qt.MiddleButton)

    assert _range(panel) == before, "panned on somebody else's pointer"
    assert panel._mid_pan_grab is None
    thief.releaseMouse()
    thief.hide()


def test_a_second_gesture_starts_from_its_own_press(app):
    """Two drags in a row: the second must measure from where IT started.

    An inherited anchor would make the second drag's first move jump by the
    distance between the two presses -- which, at a zoom, is the whole
    thumbnail.
    """
    panel = _panel()
    _middle_drag(panel, -40, -20, start=(250, 250))
    after_first = _range(panel)

    # The second gesture starts a long way from where the first ended.
    _send(panel, QtCore.QEvent.MouseButtonPress, QtCore.QPoint(100, 100),
          button=Qt.MiddleButton)
    assert panel._mid_pan_last is not None
    _send(panel, QtCore.QEvent.MouseMove, QtCore.QPoint(110, 100),
          button=Qt.NoButton, buttons=Qt.MiddleButton)
    moved = _range(panel)
    _send(panel, QtCore.QEvent.MouseButtonRelease, QtCore.QPoint(110, 100),
          button=Qt.MiddleButton, buttons=Qt.NoButton)

    # 10 viewport pixels, at this panel's scale, and nothing like the 150
    # an inherited anchor would have produced.
    per_px = (after_first[1] - after_first[0]) / panel.gview.viewport().width()
    assert (after_first[0] - moved[0]) == pytest.approx(10 * per_px, rel=0.2)


def test_twenty_consecutive_gestures_all_pan(app):
    """The acceptance gate, in the only form offscreen can express it:
    twenty drags in a row, each one taking the grab, panning by its own
    displacement, and giving the grab back.

    Not "the range ended up different": every single one is counted. The
    version this replaced passed a test that only looked at the final
    range, because two working drags out of six still move the map.
    """
    panel = _panel()
    panned = 0
    for i in range(20):
        before = _range(panel)
        _send(panel, QtCore.QEvent.MouseButtonPress, QtCore.QPoint(250, 250),
              button=Qt.MiddleButton)
        assert _grabber() is panel.gview.viewport(), f"drag {i}: no grab"
        _send(panel, QtCore.QEvent.MouseMove, QtCore.QPoint(240, 244),
              button=Qt.NoButton, buttons=Qt.MiddleButton)
        _send(panel, QtCore.QEvent.MouseButtonRelease,
              QtCore.QPoint(240, 244),
              button=Qt.MiddleButton, buttons=Qt.NoButton)
        assert _grabber() is None, f"drag {i}: grab not returned"
        if _range(panel) != before:
            panned += 1
    assert panned == 20


def test_the_popups_thumbnail_owns_its_own_grab(app):
    """Both instances, separately: the popup's viewport is the grabber for
    a gesture on the popup, and the page's for one on the page."""
    page = _loaded_page()
    popup = page._ensure_tissue_navigator()
    _give_slide(popup.overview, page.loader)
    popup.show()
    QtTest.QTest.qWaitForWindowExposed(popup)
    try:
        for panel in (page.overview, popup.overview):
            _send(panel, QtCore.QEvent.MouseButtonPress,
                  QtCore.QPoint(50, 50), button=Qt.MiddleButton)
            assert _grabber() is panel.gview.viewport()
            _send(panel, QtCore.QEvent.MouseButtonRelease,
                  QtCore.QPoint(50, 50),
                  button=Qt.MiddleButton, buttons=Qt.NoButton)
            assert _grabber() is None
    finally:
        popup.hide()
        page.hide()


def test_the_grab_is_released_on_the_widget_it_was_taken_on(app):
    """The grabbed widget is remembered, not re-derived. Re-deriving it
    would release the WRONG widget when a viewport is swapped underneath,
    and the real grab would stand forever."""
    panel = _panel()
    _send(panel, QtCore.QEvent.MouseButtonPress, QtCore.QPoint(250, 250),
          button=Qt.MiddleButton)
    held = panel._mid_pan_grab
    assert held is panel.gview.viewport()
    released = []
    held.releaseMouse = lambda *_a: (released.append(1),
                                     QtWidgets.QWidget.releaseMouse(held))[1]

    panel._middle_pan_cancel()

    assert released == [1]
    assert _grabber() is None
