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


def test_a_sustained_loss_of_the_button_ends_the_gesture():
    """Moves that stop carrying the middle button mean the release was
    never seen. Ending there is what stops a pan sticking to the cursor for
    the rest of the session -- but it takes a RUN of them, and only after
    the platform has proved in this same gesture that it does report the
    button. One blank move is an anomaly, not a release; see
    `test_a_first_move_reporting_no_button_does_not_kill_the_gesture`."""
    panel = _panel()
    p0 = QtCore.QPoint(250, 250)
    _send(panel, QtCore.QEvent.MouseButtonPress, p0, button=Qt.MiddleButton)
    # The platform reports the button once: from here a blank move means
    # something.
    _send(panel, QtCore.QEvent.MouseMove, QtCore.QPoint(290, 275),
          button=Qt.NoButton, buttons=Qt.MiddleButton)
    for i in range(ovp.MID_PAN_BLANK_MOVE_TOLERANCE + 1):
        _send(panel, QtCore.QEvent.MouseMove, QtCore.QPoint(300 + i, 280),
              button=Qt.NoButton, buttons=Qt.NoButton)
    assert panel._mid_pan_last is None


def test_a_move_reporting_another_button_ends_the_gesture():
    """A platform that is reporting buttons, and is not reporting ours, is
    a positive contradiction rather than an absence of evidence -- and it
    ends the gesture at once, with no tolerance at all."""
    panel = _panel()
    _send(panel, QtCore.QEvent.MouseButtonPress, QtCore.QPoint(250, 250),
          button=Qt.MiddleButton)
    _send(panel, QtCore.QEvent.MouseMove, QtCore.QPoint(300, 280),
          button=Qt.NoButton, buttons=Qt.LeftButton)
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
    """Moves that stop reporting the middle button mean the release was
    never seen. A grab left standing there is not a stuck map, it is an
    application that answers no further input at all."""
    panel = _panel()
    _send(panel, QtCore.QEvent.MouseButtonPress, QtCore.QPoint(250, 250),
          button=Qt.MiddleButton)
    assert _grabber() is panel.gview.viewport()
    _send(panel, QtCore.QEvent.MouseMove, QtCore.QPoint(290, 275),
          button=Qt.NoButton, buttons=Qt.MiddleButton)
    for i in range(ovp.MID_PAN_BLANK_MOVE_TOLERANCE + 1):
        _send(panel, QtCore.QEvent.MouseMove, QtCore.QPoint(300 + i, 280),
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


def test_a_transferred_grab_does_not_throw_away_the_drag(app):
    """THE REGRESSION TEST for the reported bug.

    `QWidget.mouseGrabber()` is a process-wide singleton this panel neither
    owns nor can keep: any other widget calling `grabMouse()` replaces it
    (Qt releases the previous grabber first, which is what this test does),
    and a platform grab can be refused or transferred at any moment. The
    gesture used to require that singleton to still name this viewport
    before it would honour a move -- so from the first move after any of
    that, middle moves this viewport had legitimately RECEIVED, with the
    middle button still down, were thrown away and the thumbnail did not
    move. That is the "the middle button only works after I left-click
    somewhere first" report.

    Restore the gate -- put `QWidget.mouseGrabber() is self._mid_pan_grab`
    back into `_middle_pan_holding` -- and this test fails.
    """
    panel = _panel()
    _send(panel, QtCore.QEvent.MouseButtonPress, QtCore.QPoint(250, 250),
          button=Qt.MiddleButton)
    thief = QtWidgets.QWidget()
    thief.resize(10, 10)
    thief.show()
    thief.grabMouse()
    assert _grabber() is thief, "the fixture did not take the grab"
    before = _range(panel)

    _send(panel, QtCore.QEvent.MouseMove, QtCore.QPoint(320, 300),
          button=Qt.NoButton, buttons=Qt.MiddleButton)

    assert _range(panel) != before, \
        "a legitimate middle move was dropped because of a global grab"

    # ...and the pointer someone else holds is still theirs: ending our
    # gesture must not release a grab we are not the owner of.
    _send(panel, QtCore.QEvent.MouseButtonRelease, QtCore.QPoint(320, 300),
          button=Qt.MiddleButton, buttons=Qt.NoButton)
    assert _grabber() is thief, "released somebody else's grab"
    assert panel._mid_pan_last is None
    assert panel._mid_pan_grab is None
    thief.releaseMouse()
    thief.hide()


def test_a_press_whose_grab_is_refused_still_pans(app):
    """The grab is an enhancement -- it keeps the drag alive once the cursor
    leaves the thumbnail -- and a platform that refuses it (the offscreen
    one says so out loud; a real X server can refuse or transfer one at any
    moment) must cost that and nothing else."""
    panel = _panel()
    vp = panel.gview.viewport()
    orig = vp.grabMouse

    def refuse():
        raise RuntimeError("the platform refused the grab")

    vp.grabMouse = refuse
    try:
        before = _range(panel)
        _middle_drag(panel, -60, -40)
    finally:
        vp.grabMouse = orig
    assert _range(panel) != before, "a refused grab killed the whole gesture"
    assert panel._mid_pan_grab is None
    assert panel._mid_pan_last is None


def test_a_focus_change_does_not_end_a_live_drag(app):
    """Which widget holds the KEYBOARD is not a fact about the pointer. A
    drag that is cancelled by a focus change is cancelled for a reason that
    has nothing to do with the gesture the hand is making."""
    panel = _panel()
    _send(panel, QtCore.QEvent.MouseButtonPress, QtCore.QPoint(250, 250),
          button=Qt.MiddleButton)
    before = _range(panel)

    QtWidgets.QApplication.instance().sendEvent(
        panel.gview.viewport(),
        QtGui.QFocusEvent(QtCore.QEvent.FocusOut, Qt.OtherFocusReason))

    _send(panel, QtCore.QEvent.MouseMove, QtCore.QPoint(320, 300),
          button=Qt.NoButton, buttons=Qt.MiddleButton)
    assert _range(panel) != before, "a focus change killed a live drag"
    panel._middle_pan_cancel()


def test_deactivating_the_window_ends_the_gesture(app):
    """A window that is no longer active does not have the pointer, and a
    grab left standing on one is an application that answers nothing."""
    panel = _panel()
    _send(panel, QtCore.QEvent.MouseButtonPress, QtCore.QPoint(250, 250),
          button=Qt.MiddleButton)
    assert _grabber() is panel.gview.viewport()

    QtWidgets.QApplication.instance().sendEvent(
        panel.gview.viewport(),
        QtCore.QEvent(QtCore.QEvent.WindowDeactivate))

    assert panel._mid_pan_last is None
    assert panel._mid_pan_grab is None
    assert _grabber() is None
    settled = _range(panel)
    _send(panel, QtCore.QEvent.MouseMove, QtCore.QPoint(320, 300),
          button=Qt.NoButton, buttons=Qt.MiddleButton)
    assert _range(panel) == settled


# ── the two states the desk reported it failing in ───────────────────────


@pytest.mark.parametrize("mode", [None, "patch", "roi"])
def test_a_middle_drag_pans_while_the_status_still_says_loading(app, mode):
    """"Loading" is a LABEL. Once there is a thumbnail on screen it is a map,
    and the wheel already treated it as one -- zoom worked in this state and
    the middle button did not, which is what said the picture was never the
    problem.

    Nothing has been left-clicked in this panel: the gesture may not need an
    activating click, because the left button is not free to give one (it
    draws, it navigates, it edits)."""
    panel = _panel(mode)
    panel.status.setText("Loading overview, please wait...")
    before = _range(panel)

    _middle_drag(panel, -60, -40)

    assert "Loading" in panel.status.text()
    assert _range(panel) != before, "the thumbnail did not move while loading"
    assert after_is_a_translation(before, _range(panel))


def after_is_a_translation(before, after):
    return ((after[1] - after[0]) == pytest.approx(before[1] - before[0])
            and (after[3] - after[2]) == pytest.approx(before[3] - before[2]))


def test_a_middle_drag_pans_immediately_after_a_patch_is_drawn(app):
    """Draw a rectangle with the left button and reach straight for the
    middle one. No intervening click of any kind."""
    panel = _panel("patch")
    a, b = QtCore.QPoint(120, 120), QtCore.QPoint(220, 220)
    _send(panel, QtCore.QEvent.MouseButtonPress, a, button=Qt.LeftButton)
    for i in range(1, 4):
        _send(panel, QtCore.QEvent.MouseMove,
              QtCore.QPoint(a.x() + (b.x() - a.x()) * i // 3,
                            a.y() + (b.y() - a.y()) * i // 3),
              button=Qt.NoButton, buttons=Qt.LeftButton)
    _send(panel, QtCore.QEvent.MouseButtonRelease, b,
          button=Qt.LeftButton, buttons=Qt.NoButton)
    assert len(panel._patches) == 1, "the fixture did not draw a patch"
    rect_before = tuple(panel._patches[0]["coords"])
    before = _range(panel)

    _middle_drag(panel, -60, -40, start=(300, 300))

    assert _range(panel) != before, "the thumbnail did not move after a draw"
    assert len(panel._patches) == 1
    assert tuple(panel._patches[0]["coords"]) == rect_before


def test_a_middle_drag_changes_no_patches_and_no_rois(app):
    """The gesture is a camera move. Nothing downstream may hear about it."""
    panel = _panel("patch")
    panel.add_patch_rect(*P1)
    seen = []
    panel.patches_changed.connect(lambda *_a: seen.append("patches"))
    panel.rois_changed.connect(lambda *_a: seen.append("rois"))
    panel.navigate_requested.connect(lambda *_a: seen.append("navigate"))

    _middle_drag(panel, -60, -40)

    assert seen == []


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


# ── press, then move AT ONCE ─────────────────────────────────────────────
#
# The report this section answers: on the real machine, pressing the middle
# button and moving immediately does nothing at all, while pressing, waiting
# about a second and then moving pans normally. There is no timer anywhere
# in this code, so nothing is being enabled by the wait -- what the wait
# gives is time for the platform's button state to settle. The rule that
# used to end the gesture,
#
#     if not (event.buttons() & Qt.MiddleButton): cancel()
#
# takes that one bit as decisive, so a first move that arrives carrying
# NoButton killed the whole drag on its first event, permanently, which is
# precisely the symptom.
#
# Offscreen CANNOT reproduce a platform's button-state race: every event
# here is one we constructed, and `QApplication.mouseButtons()` is NoButton
# throughout because no real button is down. What offscreen CAN do -- and
# what these tests do -- is construct the exact sequence the desk observed,
# first move NoButton and later moves MiddleButton, and pin that the
# gesture survives it and pans from the very first move. Which cause
# actually produces that sequence on the affected machine is a question for
# the diagnostic switch (`BLOCK01_MIDPAN_DEBUG=1`), not for this file.


def _press(panel, at=(250, 250)):
    _send(panel, QtCore.QEvent.MouseButtonPress, QtCore.QPoint(*at),
          button=Qt.MiddleButton)


def _move(panel, at, buttons=Qt.MiddleButton):
    _send(panel, QtCore.QEvent.MouseMove, QtCore.QPoint(*at),
          button=Qt.NoButton, buttons=buttons)


def _release(panel, at=(240, 244)):
    _send(panel, QtCore.QEvent.MouseButtonRelease, QtCore.QPoint(*at),
          button=Qt.MiddleButton, buttons=Qt.NoButton)


def _observed_drag(panel, start=(250, 250), dx=-40, dy=-24, steps=4):
    """The sequence the desk actually saw: press, then a first move that
    reports NO buttons, then moves that report the middle button."""
    x0, y0 = start
    _press(panel, start)
    _move(panel, (x0 + dx // steps, y0 + dy // steps), buttons=Qt.NoButton)
    for i in range(2, steps + 1):
        _move(panel, (x0 + dx * i // steps, y0 + dy * i // steps))
    _release(panel, (x0 + dx, y0 + dy))


# 1. ── the first move pans, with nothing in between ─────────────────────

@pytest.mark.parametrize("mode", [None, "patch", "roi"])
def test_the_very_first_move_after_the_press_pans(app, mode):
    """No wait, no second move, no left click: press, move once, moved."""
    panel = _panel(mode)
    before = _range(panel)

    _press(panel)
    _move(panel, (240, 244))

    assert _range(panel) != before, "the first move after the press did nothing"
    panel._middle_pan_cancel("test")


# 2. ── the exact observed sequence ──────────────────────────────────────

@pytest.mark.parametrize("mode", [None, "patch", "roi"])
def test_a_first_move_reporting_no_button_does_not_kill_the_gesture(app, mode):
    """THE REGRESSION TEST for "I have to hold it for a second".

    First move: NoButton -- the state the platform is suspected of
    reporting before it has settled. It must still pan, and above all it
    must not end the gesture, because the moves that follow it DO carry the
    middle button and they are the rest of the user's drag.

    Restore `if not (event.buttons() & Qt.MiddleButton): cancel()` in
    `_middle_pan_move` and this test fails: the range stops at whatever the
    first move did (nothing), and `_mid_pan_last` is already None.
    """
    panel = _panel(mode)
    before = _range(panel)

    _press(panel)
    _move(panel, (240, 244), buttons=Qt.NoButton)
    after_first = _range(panel)
    assert after_first != before, "the blank first move did not pan"
    assert panel._mid_pan_last is not None, \
        "one blank move ended a press we had seen ourselves"

    _move(panel, (230, 238))
    assert _range(panel) != after_first, "the gesture was lost after move one"

    _release(panel, (230, 238))
    assert panel._mid_pan_last is None


def test_the_whole_observed_sequence_pans_the_full_distance(app):
    """Blank first move and all, the drag comes out 1:1 with the cursor --
    the tolerated move is panned, not merely survived."""
    panel = _panel()
    before = _range(panel)

    _observed_drag(panel, dx=-40, dy=-24)

    after = _range(panel)
    per_px = (before[1] - before[0]) / panel.gview.viewport().width()
    assert (after[0] - before[0]) == pytest.approx(40 * per_px, rel=0.05)


# 3./4. ── and it still ends ─────────────────────────────────────────────

def test_the_release_ends_a_gesture_that_began_with_a_blank_move(app):
    panel = _panel()
    _press(panel)
    _move(panel, (240, 244), buttons=Qt.NoButton)
    _release(panel, (240, 244))

    settled = _range(panel)
    assert panel._mid_pan_last is None
    assert panel._mid_pan_watch is None
    _move(panel, (200, 200))
    assert _range(panel) == settled


def test_a_release_delivered_to_another_widget_still_ends_the_gesture(app):
    """The gesture no longer ends itself on the first blank move, so what
    guarantees it ends at all is that the RELEASE is caught wherever it
    lands. A grab that was refused, transferred or broken by a popup sends
    it to whatever is under the cursor instead -- so the panel watches the
    application for it, for the drag's lifetime and not one event longer."""
    panel = _panel()
    stranger = QtWidgets.QWidget()
    stranger.resize(10, 10)
    stranger.show()
    try:
        _press(panel)
        _move(panel, (240, 244))
        assert panel._mid_pan_watch is not None, "no application watcher"

        ev = QtGui.QMouseEvent(
            QtCore.QEvent.MouseButtonRelease, QtCore.QPointF(1, 1),
            Qt.MiddleButton, Qt.NoButton, Qt.NoModifier)
        QtWidgets.QApplication.instance().sendEvent(stranger, ev)

        assert panel._mid_pan_last is None, "a release elsewhere was missed"
        assert panel._mid_pan_grab is None
        assert panel._mid_pan_watch is None
        settled = _range(panel)
        _move(panel, (100, 100))
        assert _range(panel) == settled
    finally:
        stranger.hide()
        panel._middle_pan_cancel("test")


def test_no_application_filter_is_left_installed_between_gestures(app):
    """Outside a drag the panel filters nothing application-wide at all."""
    panel = _panel()
    assert panel._mid_pan_watch is None
    _press(panel)
    assert panel._mid_pan_watch is not None
    _release(panel, (250, 250))
    assert panel._mid_pan_watch is None


# 5.-8. ── the four states the desk reported it failing in ───────────────

@pytest.mark.parametrize("mode", [None, "patch", "roi"])
def test_the_first_move_pans_while_the_status_still_says_loading(app, mode):
    panel = _panel(mode)
    panel.status.setText("Loading overview, please wait...")
    before = _range(panel)

    _press(panel)
    _move(panel, (240, 244), buttons=Qt.NoButton)

    assert "Loading" in panel.status.text()
    assert _range(panel) != before, "no pan while the label said Loading"
    panel._middle_pan_cancel("test")


def test_the_first_move_pans_immediately_after_a_patch_is_drawn(app):
    """Draw a rectangle with the left button and reach straight for the
    middle one -- no intervening click, and the first middle move blank."""
    panel = _panel("patch")
    a, b = QtCore.QPoint(120, 120), QtCore.QPoint(220, 220)
    _send(panel, QtCore.QEvent.MouseButtonPress, a, button=Qt.LeftButton)
    for i in range(1, 4):
        _send(panel, QtCore.QEvent.MouseMove,
              QtCore.QPoint(a.x() + (b.x() - a.x()) * i // 3,
                            a.y() + (b.y() - a.y()) * i // 3),
              button=Qt.NoButton, buttons=Qt.LeftButton)
    _send(panel, QtCore.QEvent.MouseButtonRelease, b,
          button=Qt.LeftButton, buttons=Qt.NoButton)
    assert len(panel._patches) == 1
    rect_before = tuple(panel._patches[0]["coords"])
    before = _range(panel)

    _press(panel, (300, 300))
    _move(panel, (290, 294), buttons=Qt.NoButton)

    assert _range(panel) != before, "no pan straight after drawing a patch"
    assert tuple(panel._patches[0]["coords"]) == rect_before
    panel._middle_pan_cancel("test")


def test_the_first_move_pans_with_a_patch_selected(app):
    panel = _panel("patch")
    panel.add_patch_rect(*P1)
    panel._select_patch_artist(0)
    assert panel.is_adjusting_patch() is True
    before = _range(panel)

    _press(panel)
    _move(panel, (240, 244), buttons=Qt.NoButton)

    assert _range(panel) != before, "no pan with a patch selected"
    assert panel.is_adjusting_patch() is True
    panel._middle_pan_cancel("test")


def test_the_first_move_pans_straight_after_an_adjustment(app):
    """Drag the selected rectangle by its border, let go, and reach for the
    middle button with no click in between: `_adjust_swallow` and the patch
    drag both belong to the LEFT button and neither may touch this."""
    panel = _panel("patch")
    panel.add_patch_rect(*P1)
    panel._select_patch_artist(0)
    edge = _viewport_pos(panel, 100, 100)
    _send(panel, QtCore.QEvent.MouseButtonPress, edge, button=Qt.LeftButton)
    for i in range(1, 4):
        _send(panel, QtCore.QEvent.MouseMove,
              QtCore.QPoint(edge.x() + 5 * i, edge.y() + 5 * i),
              button=Qt.NoButton, buttons=Qt.LeftButton)
    _send(panel, QtCore.QEvent.MouseButtonRelease,
          QtCore.QPoint(edge.x() + 15, edge.y() + 15),
          button=Qt.LeftButton, buttons=Qt.NoButton)
    assert panel._patch_drag is None
    before = _range(panel)
    coords_before = tuple(panel._patches[0]["coords"])

    _press(panel, (300, 300))
    _move(panel, (290, 294), buttons=Qt.NoButton)

    assert _range(panel) != before, "no pan straight after an adjustment"
    assert tuple(panel._patches[0]["coords"]) == coords_before
    panel._middle_pan_cancel("test")


# 10. ── both thumbnails, through a real window ───────────────────────────

def _window_observed_drag(panel, dx, dy, steps=4):
    """`_window_drag`, but with the first move reporting NoButton."""
    host = panel.window()
    handle = host.windowHandle()
    assert handle is not None
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
                Qt.NoButton, Qt.NoButton if i == 1 else Qt.MiddleButton)
    deliver(QtCore.QEvent.MouseButtonRelease, p0 + QtCore.QPoint(dx, dy),
            Qt.MiddleButton, Qt.NoButton)


def test_both_thumbnails_survive_the_observed_sequence(app):
    """The page's own Tissue Preview and the Tissue Navigator popup's, each
    driven through its own real window."""
    page = _loaded_page()
    popup = page._ensure_tissue_navigator()
    _give_slide(popup.overview, page.loader)
    popup.show()
    QtTest.QTest.qWaitForWindowExposed(popup)
    try:
        for panel, what in ((page.overview, "page"), (popup.overview, "popup")):
            _zoom_in(panel)
            before = _range(panel)
            _window_observed_drag(panel, -60, -40)
            assert _range(panel) != before, \
                f"{what}: the blank first move lost the gesture"
            assert panel._mid_pan_last is None
            assert panel._mid_pan_watch is None
    finally:
        popup.hide()
        page.hide()


# 11.-12. ── the gate, and what it may not touch ─────────────────────────

def test_twenty_immediate_gestures_all_pan(app):
    """Twenty presses, each followed by ONE move at once -- the first of
    them blank, as the desk saw it -- and twenty pans. Counted one by one:
    "the range ended up different" was true of the broken version too."""
    panel = _panel()
    panned = 0
    for i in range(20):
        before = _range(panel)
        _press(panel, (250, 250))
        _move(panel, (240, 244), buttons=Qt.NoButton)
        if _range(panel) != before:
            panned += 1
        _release(panel, (240, 244))
        assert panel._mid_pan_last is None, f"drag {i}: gesture left open"
        assert panel._mid_pan_watch is None, f"drag {i}: filter left installed"
    assert panned == 20


def test_the_observed_sequence_changes_no_patches_rois_or_navigation(app):
    panel = _panel("patch")
    panel.add_patch_rect(*P1)
    seen = []
    panel.patches_changed.connect(lambda *_a: seen.append("patches"))
    panel.rois_changed.connect(lambda *_a: seen.append("rois"))
    panel.navigate_requested.connect(lambda *_a: seen.append("navigate"))

    _observed_drag(panel)

    assert seen == []
    assert len(panel._patches) == 1
    assert panel.is_adjusting_patch() is False


# 13. ── nothing is ever left held ────────────────────────────────────────

@pytest.mark.parametrize("how", ["hide", "close", "deactivate", "teardown"])
def test_nothing_is_left_holding_the_pointer(app, how):
    """Grab and application filter both, on every way out."""
    panel = _panel()
    _press(panel)
    _move(panel, (240, 244), buttons=Qt.NoButton)
    assert _grabber() is panel.gview.viewport()
    assert panel._mid_pan_watch is not None
    watch = panel._mid_pan_watch

    if how == "hide":
        panel.hide()
    elif how == "close":
        panel.close()
    elif how == "deactivate":
        QtWidgets.QApplication.instance().sendEvent(
            panel.gview.viewport(),
            QtCore.QEvent(QtCore.QEvent.WindowDeactivate))
    else:
        # Teardown: the watcher is the panel's CHILD, so it dies with it and
        # Qt drops a destroyed filter itself -- there is no teardown hook to
        # forget to call.
        assert watch.parent() is panel
        panel.hide()
        panel.deleteLater()
        QtWidgets.QApplication.instance().processEvents()
        assert _grabber() is None
        return

    QtWidgets.QApplication.instance().processEvents()
    assert _grabber() is None
    assert panel._mid_pan_grab is None
    assert panel._mid_pan_last is None
    assert panel._mid_pan_watch is None


def test_a_swapped_viewport_does_not_leave_a_gesture_open(app):
    """A graphics view can replace its viewport underneath a drag, and a
    gesture anchored to a widget the panel no longer owns can never be
    completed by anything the panel will see again."""
    panel = _panel()
    _press(panel)
    assert panel._mid_pan_last is not None
    orphan = panel._mid_pan_grab

    panel.gview.setViewport(QtWidgets.QWidget())
    QtWidgets.QApplication.instance().processEvents()
    # Any mouse event at all now tells the watcher the world has moved on.
    _move(panel, (240, 244))

    assert panel._mid_pan_last is None
    assert panel._mid_pan_grab is None
    assert panel._mid_pan_watch is None
    assert QtWidgets.QWidget.mouseGrabber() is not orphan


# ── the diagnostic switch ────────────────────────────────────────────────

def test_the_diagnostic_is_off_by_default_and_speaks_when_asked(
        app, capsys, monkeypatch):
    """One switch, default off. The machine that has the bug is the
    instrument: offscreen can construct the suspected sequence but only a
    real X server can say which cause produces it."""
    monkeypatch.delenv(ovp.MID_PAN_DEBUG_ENV, raising=False)
    panel = _panel()
    _press(panel)
    _move(panel, (240, 244), buttons=Qt.NoButton)
    _release(panel, (240, 244))
    assert "MIDPAN" not in capsys.readouterr().err

    monkeypatch.setenv(ovp.MID_PAN_DEBUG_ENV, "1")
    _press(panel)
    _move(panel, (240, 244), buttons=Qt.NoButton)
    _release(panel, (240, 244))
    err = capsys.readouterr().err

    lines = [ln for ln in err.splitlines() if ln.startswith("MIDPAN")]
    assert len(lines) >= 3, err
    whats = [ln.split(" what=")[1].split(" ")[0] for ln in lines]
    assert "press" in whats and "move-blank" in whats
    for field in ("app_buttons=", "grabber=", "scene_grab=", "focus=",
                  "active=", "sel_patch=", "patch_drag=", "drag_start=",
                  "swallow=", "since_press=", "buttons=", "button=", "type="):
        assert field in lines[0], f"{field} missing from {lines[0]}"
    assert any("closed_by=" in ln for ln in lines), \
        "the log never says which line closed the gesture"


# ── what the diagnostic can now measure ──────────────────────────────────
#
# The previous readout answered "did the code run": events in, state
# machine, which line closed the gesture. It could not answer the two
# questions a sluggish drag actually raises -- did the camera step reach
# the SCREEN, and was the GUI thread even available when the event
# arrived -- nor the one that a dead FIRST drag raises: did the press
# reach this viewport at all. These pin the fields that answer them, and
# pin equally that none of it runs, or costs anything, with the switch
# off. They do not claim the real machine's cause; they make the
# instrument able to name it.

def _midpan_lines(err):
    return [ln for ln in err.splitlines() if ln.startswith("MIDPAN")]


def _field(line, name):
    for token in line.split():
        if token.startswith(name + "="):
            return token[len(name) + 1:]
    return None


def test_a_camera_step_is_timed_and_says_whether_the_range_moved(
        app, capsys, monkeypatch):
    monkeypatch.setenv(ovp.MID_PAN_DEBUG_ENV, "1")
    panel = _panel()
    _middle_drag(panel, -40, -24)
    lines = _midpan_lines(capsys.readouterr().err)

    applied = [ln for ln in lines if " what=move-applied " in ln]
    assert len(applied) == 4, \
        f"four moves translated, {len(applied)} were measured"
    for ln in applied:
        assert _field(ln, "moved") == "True", ln
        assert float(_field(ln, "took_ms")) >= 0.0, ln
        assert _field(ln, "range_x") and _field(ln, "range_y"), ln
        assert _field(ln, "dx_view") and _field(ln, "dy_view"), ln


def test_a_move_that_translated_nothing_is_not_reported_as_a_step(
        app, capsys, monkeypatch):
    """`move-applied` is the CAMERA's line, not the event's: a zero-delta
    move has nothing to time and nothing to have reached the screen."""
    monkeypatch.setenv(ovp.MID_PAN_DEBUG_ENV, "1")
    panel = _panel()
    _press(panel, at=(250, 250))
    _move(panel, (250, 250))
    _release(panel, (250, 250))
    lines = _midpan_lines(capsys.readouterr().err)

    assert [ln for ln in lines if " what=move " in ln], \
        "the move itself is still logged"
    assert not [ln for ln in lines if " what=move-applied " in ln]


def test_the_repaint_after_a_step_is_dated_from_that_step(
        app, capsys, monkeypatch):
    """C, the drawing case: the camera moved on every move and the picture
    followed hundreds of ms later. Unmeasurable until the repaint carries
    its own delay from the step that asked for it."""
    monkeypatch.setenv(ovp.MID_PAN_DEBUG_ENV, "1")
    panel = _panel()
    _press(panel)
    _move(panel, (240, 244))
    QtWidgets.QApplication.instance().sendEvent(
        panel.gview.viewport(),
        QtGui.QPaintEvent(panel.gview.viewport().rect()))
    _release(panel, (240, 244))
    lines = _midpan_lines(capsys.readouterr().err)

    paints = [ln for ln in lines if " what=paint " in ln]
    assert len(paints) == 1, \
        f"one repaint after one step, got {len(paints)}"
    assert float(_field(paints[0], "after_step_ms")) >= 0.0, paints[0]
    assert _field(paints[0], "gesture_open") == "True", paints[0]


def test_only_the_first_repaint_after_a_step_is_logged(app, capsys,
                                                       monkeypatch):
    """The eye waits for the first one. The rest of a scene's repaints say
    nothing about whether the picture followed the hand, and a line per
    repaint would drown the timeline they are in."""
    monkeypatch.setenv(ovp.MID_PAN_DEBUG_ENV, "1")
    panel = _panel()
    _press(panel)
    _move(panel, (240, 244))
    vp = panel.gview.viewport()
    for _ in range(5):
        QtWidgets.QApplication.instance().sendEvent(
            vp, QtGui.QPaintEvent(vp.rect()))
    _release(panel, (240, 244))
    lines = _midpan_lines(capsys.readouterr().err)

    assert len([ln for ln in lines if " what=paint " in ln]) == 1


def test_repaints_outside_a_gesture_are_not_logged(app, capsys, monkeypatch):
    """Bounded on purpose: with the switch on, ordinary browsing must not
    pay for this."""
    monkeypatch.setenv(ovp.MID_PAN_DEBUG_ENV, "1")
    panel = _panel()
    vp = panel.gview.viewport()
    # A step HAS happened -- the repaint is pending and would be dated if
    # the window were open. What closes it is the window, and nothing else.
    _press(panel)
    _move(panel, (240, 244))
    _release(panel, (240, 244))
    assert panel._mid_pan_step_t is not None
    assert not panel._mid_pan_step_painted
    capsys.readouterr()

    monkeypatch.setattr(ovp, "MID_PAN_PAINT_WINDOW_MS", 0.0)
    for _ in range(3):
        QtWidgets.QApplication.instance().sendEvent(
            vp, QtGui.QPaintEvent(vp.rect()))
    lines = _midpan_lines(capsys.readouterr().err)

    assert not [ln for ln in lines if " what=paint " in ln]


def test_a_middle_move_with_no_open_gesture_reports_the_lost_press(
        app, capsys, monkeypatch):
    """A, the ownership case: the button IS down and this viewport never
    saw the press. Previously indistinguishable from no gesture at all."""
    monkeypatch.setenv(ovp.MID_PAN_DEBUG_ENV, "1")
    panel = _panel()
    _move(panel, (240, 244), buttons=Qt.MiddleButton)
    lines = _midpan_lines(capsys.readouterr().err)

    strays = [ln for ln in lines if " what=stray-move " in ln]
    assert len(strays) == 1, \
        "a middle-button move with no press taken here is the signature of " \
        "a press that went somewhere else; the log has to name it"
    assert _field(strays[0], "last") == "None", strays[0]


def test_a_move_with_no_button_and_no_gesture_is_not_a_lost_press(
        app, capsys, monkeypatch):
    """Every idle mouse move is not evidence. Only a move carrying the
    middle button says a press was lost."""
    monkeypatch.setenv(ovp.MID_PAN_DEBUG_ENV, "1")
    panel = _panel()
    _move(panel, (240, 244), buttons=Qt.NoButton)
    lines = _midpan_lines(capsys.readouterr().err)

    assert not [ln for ln in lines if " what=stray-move " in ln]


def test_the_gui_thread_heartbeat_lives_exactly_as_long_as_a_gesture(
        app, monkeypatch):
    """D, the blocked-thread case: a 5 ms timer is the only instrument that
    can measure the event loop's own availability from inside it. It must
    not outlive the drag -- a permanent 5 ms timer in a slide viewer is
    the overhead this diagnostic is forbidden to leave behind."""
    monkeypatch.setenv(ovp.MID_PAN_DEBUG_ENV, "1")
    panel = _panel()
    assert panel._mid_pan_gap_timer is None

    _press(panel)
    assert panel._mid_pan_gap_timer is not None
    assert panel._mid_pan_gap_timer.isActive()

    _release(panel, (250, 250))
    assert panel._mid_pan_gap_timer is None


def test_the_heartbeat_does_not_exist_with_the_switch_off(app, monkeypatch):
    monkeypatch.delenv(ovp.MID_PAN_DEBUG_ENV, raising=False)
    panel = _panel()
    _press(panel)
    assert panel._mid_pan_gap_timer is None
    _release(panel, (250, 250))


def test_a_stalled_event_loop_is_reported_as_a_gap(app, capsys, monkeypatch):
    monkeypatch.setenv(ovp.MID_PAN_DEBUG_ENV, "1")
    monkeypatch.setattr(ovp, "MID_PAN_LOOP_GAP_MS", 0.0)
    panel = _panel()
    _press(panel)
    panel._mid_pan_gap_tick()
    _release(panel, (250, 250))
    lines = _midpan_lines(capsys.readouterr().err)

    gaps = [ln for ln in lines if " what=loop-gap " in ln]
    assert gaps, "a tick later than its interval is the thread being busy"
    assert float(_field(gaps[0], "gap_ms")) >= 0.0, gaps[0]


def test_none_of_the_new_lines_appear_with_the_switch_off(app, capsys,
                                                          monkeypatch):
    monkeypatch.delenv(ovp.MID_PAN_DEBUG_ENV, raising=False)
    panel = _panel()
    vp = panel.gview.viewport()
    _middle_drag(panel, -40, -24)
    _move(panel, (240, 244), buttons=Qt.MiddleButton)
    QtWidgets.QApplication.instance().sendEvent(
        vp, QtGui.QPaintEvent(vp.rect()))

    assert "MIDPAN" not in capsys.readouterr().err


def test_the_log_can_be_collected_from_a_file(app, capsys, monkeypatch,
                                              tmp_path):
    """The run is driven by whoever has the mouse; it is read by whoever
    has the question. stderr on a real desktop run is a terminal nobody is
    watching mid-gesture, so the same lines go to a file when one is
    named."""
    path = tmp_path / "midpan.log"
    monkeypatch.setenv(ovp.MID_PAN_DEBUG_ENV, "1")
    monkeypatch.setenv(ovp.MID_PAN_LOG_ENV, str(path))
    monkeypatch.setattr(ovp, "_MID_PAN_LOG_FILE", None)
    panel = _panel()
    _middle_drag(panel, -40, -24)

    on_stderr = _midpan_lines(capsys.readouterr().err)
    in_file = _midpan_lines(path.read_text(encoding="utf-8"))
    assert in_file == on_stderr, "the file sink is the same readout, not a subset"
    assert any(" what=move-applied " in ln for ln in in_file)


def test_an_unwritable_log_file_does_not_break_the_drag(app, capsys,
                                                        monkeypatch,
                                                        tmp_path):
    """A diagnostic that can break the gesture it is diagnosing is worse
    than no diagnostic."""
    monkeypatch.setenv(ovp.MID_PAN_DEBUG_ENV, "1")
    monkeypatch.setenv(ovp.MID_PAN_LOG_ENV, str(tmp_path / "no" / "such.log"))
    monkeypatch.setattr(ovp, "_MID_PAN_LOG_FILE", None)
    panel = _panel()
    before = _range(panel)
    _middle_drag(panel, -40, -24)

    assert _range(panel) != before, "the pan still happened"
    err = capsys.readouterr().err
    assert "log-file-failed" in err
    assert _midpan_lines(err), "and the lines still reached stderr"


# ── the position a pointer grab replays ──────────────────────────────────
#
# MEASURED, on the real desk, with BLOCK01_MIDPAN_DEBUG=1 over 106
# gestures on three panels and 816 moves: 26 gestures contained a camera
# step that the NEXT move undid exactly -- dx and dy negated to the last
# bit of a double -- every undoing move reported buttons=NoButton while
# QApplication.mouseButtons() still said Middle, all landed 4-17 ms after
# the press, and in ALL 31 such pairs the undone step was the gesture's
# FIRST step and never a later one. That last count is what names the
# signature: the position those moves carried was the PRESS position,
# replayed by the platform when `grabMouse()` established the pointer
# grab.
#
# The rule these pin is exactly that shape and no wider: no button
# reported, position bit-identical to this gesture's press, and not
# already there. A platform that fails to report buttons while giving a
# REAL new position keeps the camera and the anchor -- refusing that
# whole class would rebuild the same complaint from the other side, so
# the tests below hold both halves.

def test_a_blank_move_at_the_press_position_does_not_move_the_camera(app):
    """The measured failure: the step is undone by the position the grab
    replays, and a quarter of all drags lose their first fraction."""
    panel = _panel()
    start = _range(panel)
    _press(panel, at=(250, 250))
    _move(panel, (210, 226))                    # real, middle down
    moved = _range(panel)
    assert moved != start, "the real move panned"

    _move(panel, (250, 250), buttons=Qt.NoButton)   # the grab's stale position

    assert _range(panel) == moved, (
        "a move that reports no buttons carried the position the gesture "
        "started from; panning on it undoes the step just taken")


def test_a_blank_move_at_a_new_position_still_pans_and_re_anchors(app):
    """The other half, and the risk of fixing this too widely: a move
    that reports no buttons but carries a REAL new position is a hand
    that moved on a platform that failed to say which button is down.
    Refusing it would stop the picture, freeze the anchor, and end the
    gesture after a run of them -- the reported complaint, rebuilt from
    the other side. Nothing in the measurement licenses that."""
    panel = _panel()
    start = _range(panel)
    _press(panel, at=(250, 250))
    _move(panel, (240, 244))                    # real, middle down
    after_first = _range(panel)

    _move(panel, (230, 238), buttons=Qt.NoButton)   # blank, but a new place

    after_blank = _range(panel)
    assert after_blank != after_first, \
        "a blank move carrying a new position must still pan"
    step1 = [a - b for a, b in zip(after_first, start)]
    step2 = [a - b for a, b in zip(after_blank, after_first)]
    assert step2 == pytest.approx(step1, rel=1e-9), \
        "and it must be worth exactly what the same cursor step is worth"

    _move(panel, (220, 232))                    # the drag continues, real
    step3 = [a - b for a, b in zip(_range(panel), after_blank)]
    assert step3 == pytest.approx(step1, rel=1e-9), \
        "the anchor followed the blank move, so the next real one is one step"


def test_only_this_gesture_s_press_position_is_refused(app):
    """Measured: the undone step was ALWAYS the gesture's first, so the
    replayed coordinate is the press. A blank move landing on some
    EARLIER anchor is not that signature and is not refused."""
    panel = _panel()
    _press(panel, at=(250, 250))
    _move(panel, (240, 244))                    # anchor A
    _move(panel, (230, 238))                    # anchor B
    after_b = _range(panel)

    _move(panel, (240, 244), buttons=Qt.NoButton)   # back to A, no buttons

    assert _range(panel) != after_b, \
        "only the press position is the grab's replay; this is a hand"


def test_a_blank_move_at_the_press_position_before_any_step_is_harmless(app):
    """The press position with no step yet behind it is a zero-delta move
    either way. It must not end the gesture or claim the camera."""
    panel = _panel()
    before = _range(panel)
    _press(panel, at=(250, 250))
    panel._thumbnail_camera_touched = False
    _move(panel, (250, 250), buttons=Qt.NoButton)

    assert _range(panel) == before
    assert panel._mid_pan_last is not None, "the gesture is still live"
    assert panel._thumbnail_camera_touched is False


def test_the_press_position_is_forgotten_with_the_gesture(app):
    panel = _panel()
    _press(panel, at=(250, 250))
    assert panel._mid_pan_press_scene is not None
    _release(panel, (250, 250))
    assert panel._mid_pan_press_scene is None


def test_the_replayed_move_is_named_in_the_log(app, capsys, monkeypatch):
    monkeypatch.setenv(ovp.MID_PAN_DEBUG_ENV, "1")
    panel = _panel()
    _press(panel, at=(250, 250))
    _move(panel, (240, 244))
    _move(panel, (250, 250), buttons=Qt.NoButton)
    lines = _midpan_lines(capsys.readouterr().err)

    named = [ln for ln in lines if " what=stale-grab-move " in ln]
    assert len(named) == 1, \
        "a refused move must say so, or the next reader cannot tell it " \
        "from a move that never arrived"


def test_a_monotone_drag_never_moves_the_picture_backwards(app):
    """The user-visible invariant, and the one the measurement caught
    being broken: while the cursor goes one way, the picture goes one way.

    Not an anchor test. This handler pans from ABSOLUTE positions, so a
    stale event that pans back and re-anchors is self-consistent -- the
    error cancels on the next real move and the drag ends in the right
    place. What it leaves behind is a picture that went out and jumped
    straight back, which is what a hand feels as the drag doing nothing at
    first, and it is invisible to any test that only looks at the end.
    """
    panel = _panel()
    xs = [_range(panel)[0]]
    _press(panel, at=(250, 250))
    for i in range(1, 5):
        _move(panel, (250 - 10 * i, 250 - 6 * i))
        xs.append(_range(panel)[0])
        # the grab's stale move, arriving between real ones
        _move(panel, (250, 250), buttons=Qt.NoButton)
        xs.append(_range(panel)[0])
    _release(panel, (210, 226))

    assert xs == sorted(xs), (
        "the picture went backwards inside a one-way drag: "
        f"{[round(x, 2) for x in xs]}")
    assert xs[-1] > xs[0], "and it did travel"


def test_a_blank_first_move_still_pans(app):
    """Not a retreat to cancel-on-first-blank-move. Until the platform has
    reported the middle button down ONCE in this gesture, a blank move is
    all there is, and a platform that never reports buttons must still be
    able to drag."""
    panel = _panel()
    before = _range(panel)
    _press(panel, at=(250, 250))
    _move(panel, (210, 226), buttons=Qt.NoButton)

    assert _range(panel) != before
    assert panel._mid_pan_last is not None


def test_a_blank_move_still_ends_a_gesture_whose_release_was_lost(app):
    """It keeps its say over the button. Only the camera and the anchor
    are taken away from it."""
    panel = _panel()
    _press(panel, at=(250, 250))
    _move(panel, (240, 244))                    # confirms the button
    for i in range(ovp.MID_PAN_BLANK_MOVE_TOLERANCE + 2):
        _move(panel, (240 - i, 244), buttons=Qt.NoButton)

    assert panel._mid_pan_last is None
    assert panel._mid_pan_grab is None


def test_a_blank_move_does_not_claim_the_camera_for_the_panel(app):
    """A step that did not happen must not cost the slide its one
    automatic fit either."""
    panel = _panel()
    panel._thumbnail_camera_touched = False
    _press(panel, at=(250, 250))
    _move(panel, (250, 250), buttons=Qt.NoButton)
    _move(panel, (250, 250), buttons=Qt.NoButton)

    assert panel._thumbnail_camera_touched is False


def test_the_real_desk_sequence_travels_its_full_distance(app):
    """The whole measured sequence: press, a real move, the grab's stale
    move, then the rest of the drag. The picture ends where the cursor
    did, 1:1, with nothing lost at the start."""
    x0, y0 = 250, 250
    panel = _panel()
    start = _range(panel)

    _press(panel, at=(x0, y0))
    _move(panel, (x0 - 10, y0 - 6))
    step = [a - b for a, b in zip(_range(panel), start)]
    _move(panel, (x0, y0), buttons=Qt.NoButton)     # stale, at the press
    for i in range(2, 5):
        _move(panel, (x0 - 10 * i, y0 - 6 * i))
    _release(panel, (x0 - 40, y0 - 24))

    travelled = [a - b for a, b in zip(_range(panel), start)]
    assert travelled == pytest.approx([4 * s for s in step], rel=1e-9), (
        "four equal moves of the cursor must move the picture by four equal "
        "steps; the stale event in the middle must cost nothing and add "
        f"nothing (one step {step}, four moves travelled {travelled})")


def test_no_signal_is_emitted_per_move_that_nothing_listens_to(app):
    """`sigRangeChangedManually` had no subscriber: nothing in this
    application connects it, and in pyqtgraph only PlotItem forwards it --
    this plot is a bare ViewBox. The camera work is _resetTarget() +
    translateBy(), which is what moves the view and disables the
    auto-range that would fight it."""
    panel = _panel()
    seen = []
    panel.vb.sigRangeChangedManually.connect(lambda *a: seen.append(a))
    before = _range(panel)
    _middle_drag(panel, -40, -24)

    assert _range(panel) != before, "the camera still moves"
    assert seen == [], \
        "the pan announced itself once per move to nobody at all"


# ── separating a late event from a hole that opened before it ────────────
#
# MEASURED on the real desk: the intervals between one middle move and
# the next were p50 8 ms but p90 46 ms, p99 148 ms and 308 ms at worst,
# while the handler's own work was under 2 ms and the first repaint under
# 10 ms, and the 5 ms heartbeat recorded no matching silence. So the
# moves were not late because this code was busy. What the log could not
# then say is whether the event waited after being stamped -- which this
# application could be responsible for -- or whether the hole opened
# before the stamp, upstream of Qt entirely. Those need opposite fixes,
# and only the first is ours.

def test_a_move_carries_how_late_it_was(app, capsys, monkeypatch):
    monkeypatch.setenv(ovp.MID_PAN_DEBUG_ENV, "1")
    panel = _panel()
    _middle_drag(panel, -40, -24)
    lines = [ln for ln in _midpan_lines(capsys.readouterr().err)
             if " what=move " in ln]

    assert lines
    assert _field(lines[0], "qt_ts") is not None
    later = [ln for ln in lines[1:] if _field(ln, "arr_gap") is not None]
    assert later, "every move after the first is dated from the one before"
    for ln in later:
        assert _field(ln, "ts_gap") is not None, ln
        assert _field(ln, "late_ms") is not None, ln


def test_the_first_move_of_a_gesture_has_nothing_to_be_late_against(
        app, capsys, monkeypatch):
    """Gaps are measured within one gesture. Carried across, the time
    between two drags would be reported as a stall."""
    monkeypatch.setenv(ovp.MID_PAN_DEBUG_ENV, "1")
    panel = _panel()
    _middle_drag(panel, -40, -24)
    _middle_drag(panel, -40, -24)
    lines = [ln for ln in _midpan_lines(capsys.readouterr().err)
             if " what=press " in ln or " what=move " in ln]
    firsts = [ln for ln in lines if _field(ln, "arr_gap") is None]

    assert len(firsts) == 2 and all(" what=press " in ln for ln in firsts), (
        "each gesture starts its own timeline at its press, and the first "
        "move is dated from that press -- so exactly one line per gesture "
        "has no predecessor, and it is the press")
    assert all(_field(ln, "arr_gap") is not None
               for ln in lines if " what=move " in ln)


def test_a_hole_before_the_stamp_is_not_reported_as_a_late_event(
        app, capsys, monkeypatch):
    """The whole point of the pair: when the platform's own stamps show
    the same gap the arrivals do, nothing waited inside this application.
    What opened the hole upstream is a separate question this does not
    answer."""
    monkeypatch.setenv(ovp.MID_PAN_DEBUG_ENV, "1")
    panel = _panel()
    _press(panel)
    # Two moves whose platform stamps are 200 ms apart, delivered 200 ms
    # apart: a hand that stopped for 200 ms.
    _move(panel, (240, 244))
    QtTest.QTest.qWait(200)
    _move(panel, (230, 238))
    lines = [ln for ln in _midpan_lines(capsys.readouterr().err)
             if " what=move " in ln and _field(ln, "late_ms") is not None]

    assert lines, "the second move carries the comparison"
    # Constructed events share a stamp, so this asserts the ARITHMETIC:
    # late_ms is arr_gap minus ts_gap, whatever those turn out to be.
    for ln in lines:
        arr = float(_field(ln, "arr_gap"))
        ts = float(_field(ln, "ts_gap"))
        assert float(_field(ln, "late_ms")) == pytest.approx(arr - ts,
                                                             abs=0.2), ln


def test_lateness_is_not_measured_with_the_switch_off(app, monkeypatch):
    monkeypatch.delenv(ovp.MID_PAN_DEBUG_ENV, raising=False)
    panel = _panel()
    _middle_drag(panel, -40, -24)

    assert panel._mid_pan_prev_arr is None
    assert panel._mid_pan_prev_ts is None
