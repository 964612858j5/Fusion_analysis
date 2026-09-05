"""The Tissue Preview's camera belongs to the USER the moment there are pixels.

The complaint this module pins down: the thumbnail is on screen -- the
status line still says "Loading overview" -- the user zooms in or drags the
map somewhere, and a moment later the view snaps back to the whole slide.
Repeat after drawing a patch and the gestures look dead, because every one
of them is undone before the eye can see it.

What was actually happening is measured, not guessed. `OverviewPanel` fits
the view ONCE per overview geometry, and it decides "per geometry" by
comparing `_thumb_fitted` against `(ov_w, ov_h)`. Before any overview has
landed those two numbers are an ESTIMATE -- `_thumb_rect` derives them as
`round(full / ds)` so that a host-pushed channel image has a rectangle to
be stretched onto -- while the real overview is `read_region(..., ds)`,
i.e. `arr[::ds, ::ds]`, whose shape is `ceil(full / ds)`. When the slide's
height or width is not an exact multiple of the downsample the two differ
by one pixel, `_thumb_fitted` therefore "changes", and the late overview
re-fits the whole slide over whatever the user had done in the meantime.

One pixel of arithmetic, the user's zoom gone. `test_the_overview_landing_
with_a_corrected_shape_keeps_a_users_zoom` is that case exactly.

The fix is not to special-case the arithmetic, because the arithmetic is
not the contract. The contract is ownership:

    A new dataset  -> the camera is the panel's; the first visible image
                      fits, once.
    A wheel zoom or a middle-drag that MOVES something -> the camera is the
                      user's, and stays the user's.
    Everything that arrives afterwards -- the real overview, a new channel
                      picture, a patch drawn, moved, selected or deleted,
                      the page/popup model sync -- swaps pixels and updates
                      graphics. It does not move the camera.
    A double-click  -> the user handing the camera back: fit now, and the
                      panel owns it again.

`_thumbnail_camera_touched` is that one bit, per panel, so the page's own
thumbnail and the Tissue Navigator popup's each answer for themselves.

Own module, like the other page-heavy Step0 suites: combined runs segfault
in offscreen pyqtgraph.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

import numpy as np                                          # noqa: E402
from PyQt5 import QtCore, QtGui, QtTest, QtWidgets          # noqa: E402
from PyQt5.QtCore import Qt                                 # noqa: E402

from block01.ui.step0 import overview_panel as ovp          # noqa: E402


@pytest.fixture(scope="module")
def app():
    a = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return a


# The slide's width is deliberately NOT a multiple of the downsample: that
# is the one-pixel disagreement between the estimate (`round`) and the real
# overview (`ceil`, from `arr[::ds, ::ds]`) that used to re-fit the view.
DS = 10
SLIDE_H, SLIDE_W = 8000, 6003
OV_H, OV_W = 800, 601                  # what read_region(..., ds=10) returns
EST_W = 600                            # what `_thumb_rect` estimates

P1 = (1000, 2000, 1000, 2000)          # level-0; overview 100..200 both axes


class _Loader:
    """Just enough of the loader for the panel and its overview thread."""

    def __init__(self, h=SLIDE_H, w=SLIDE_W):
        self.shape = (h, w)
        self.ch_map = {"DAPI": 0}

    def channel_names(self):
        return ["DAPI"]

    def read_region(self, _ch, y0, y1, x0, x1, downsample=1, **_kw):
        ds = max(1, int(downsample))
        return np.zeros((len(range(y0, y1, ds)), len(range(x0, x1, ds))),
                        "float32")


def _panel(loader=None):
    """A shown, laid-out panel that has NOT loaded anything yet."""
    panel = ovp.OverviewPanel(loader or _Loader(), "DAPI", lazy=True)
    panel.ds = DS
    panel.full_h, panel.full_w = panel.loader.shape
    panel.resize(500, 500)
    panel.show()
    QtTest.QTest.qWaitForWindowExposed(panel)
    return panel


def _rgb(h=OV_H, w=OV_W, value=0):
    return np.full((h, w, 3), value, "uint8")


def _overview():
    return np.zeros((OV_H, OV_W), "float32")


def _range(panel):
    (x0, x1), (y0, y1) = panel.vb.viewRange()
    return (x0, x1, y0, y1)


def _same(a, b):
    return all(u == pytest.approx(v, rel=1e-9, abs=1e-9) for u, v in zip(a, b))


def _land_overview(panel, arr=None):
    """The overview thread's result arriving, through the real slot."""
    panel._t0 = 0.0
    panel._on_overview_loaded(_overview() if arr is None else arr)


# ── real viewport events ─────────────────────────────────────────────────

def _send(panel, kind, pos, button=Qt.LeftButton, buttons=None,
          mods=Qt.NoModifier):
    ev = QtGui.QMouseEvent(
        kind, QtCore.QPointF(pos), button,
        buttons if buttons is not None else button, mods)
    QtWidgets.QApplication.instance().sendEvent(panel.gview.viewport(), ev)


def _wheel(panel, pos=(250, 250), delta=120):
    ev = QtGui.QWheelEvent(
        QtCore.QPointF(*pos), QtCore.QPointF(*pos), QtCore.QPoint(0, 0),
        QtCore.QPoint(0, delta), Qt.NoButton, Qt.NoModifier,
        Qt.NoScrollPhase, False)
    QtWidgets.QApplication.instance().sendEvent(panel.gview.viewport(), ev)


def _middle_drag(panel, dx, dy, steps=4, start=(250, 250)):
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


def _double_click(panel, pos=(250, 250)):
    """A double-click as the SCENE delivers it -- `_on_overview_click` is
    connected to `scene().sigMouseClicked`, so that is the real path."""
    sp = panel.gview.mapToScene(QtCore.QPoint(*pos))
    _send(panel, QtCore.QEvent.MouseButtonPress, QtCore.QPoint(*pos))
    _send(panel, QtCore.QEvent.MouseButtonRelease, QtCore.QPoint(*pos),
          button=Qt.LeftButton, buttons=Qt.NoButton)
    _send(panel, QtCore.QEvent.MouseButtonDblClick, QtCore.QPoint(*pos))
    ev = _DoubleClick(sp)
    panel._on_overview_click(ev)


class _DoubleClick:
    """pyqtgraph's `MouseClickEvent` as `_on_overview_click` reads it."""

    def __init__(self, scene_pos):
        self._sp = scene_pos

    def double(self):
        return True

    def scenePos(self):
        return self._sp


# ══ 1. the overview landing must not take a zoom away ════════════════════

def test_the_overview_landing_with_a_corrected_shape_keeps_a_users_zoom(app):
    """The reported case, end to end.

    The host pushes the channel picture first, so the thumbnail IS on
    screen while the status still says "Loading". The user wheels. Then the
    real overview lands, one pixel wider than the estimate. The view is
    exactly where the user left it.
    """
    panel = _panel()
    panel.set_channel_image(_rgb())
    assert panel.ov_w == EST_W, "fixture must exercise the estimate"

    _wheel(panel)
    zoomed = _range(panel)
    assert not _same(zoomed, _range(_panel())), "the wheel must have moved it"
    assert panel._thumbnail_camera_touched is True

    _land_overview(panel)

    assert panel.ov_w == OV_W, "the real overview shape must be adopted"
    assert _same(_range(panel), zoomed), "the overview stole the user's zoom"


def test_the_overview_landing_keeps_a_users_middle_drag(app):
    """The same path, panned instead of zoomed."""
    panel = _panel()
    panel.set_channel_image(_rgb())
    _middle_drag(panel, -60, -40)
    panned = _range(panel)
    assert panel._thumbnail_camera_touched is True

    _land_overview(panel)

    assert _same(_range(panel), panned), "the overview stole the user's pan"


def test_an_untouched_camera_still_fits_the_corrected_overview(app):
    """The other half of the rule: nobody has touched the camera, so the
    panel still owns it and the corrected geometry is fitted properly."""
    panel = _panel()
    panel.set_channel_image(_rgb())
    assert panel._thumbnail_camera_touched is False
    estimated = _range(panel)

    _land_overview(panel)

    assert panel.ov_w == OV_W
    assert not _same(_range(panel), estimated), "the correction was ignored"
    x0, x1, y0, y1 = _range(panel)
    assert x0 < 0 < OV_W < x1 and y0 < 0 < OV_H < y1, "not the whole slide"


# ══ 2. nothing that arrives later re-fits ════════════════════════════════

def test_a_new_channel_picture_does_not_reset_the_camera(app):
    """Switching the channel the user is working on swaps pixels only."""
    panel = _panel()
    panel.set_channel_image(_rgb())
    _land_overview(panel)
    _wheel(panel)
    before = _range(panel)

    panel.set_channel_image(_rgb(value=7))
    assert _same(_range(panel), before)
    panel.set_channel_image(_rgb(h=400, w=300, value=9))    # another pyramid level
    assert _same(_range(panel), before)
    panel.set_channel_image(None)                            # back to DAPI
    assert _same(_range(panel), before)


def test_drawing_moving_selecting_and_deleting_patches_keeps_the_camera(app):
    """Every patch path, one after another, on a camera the user owns."""
    panel = _panel()
    panel.set_channel_image(_rgb())
    _land_overview(panel)
    _middle_drag(panel, -40, -25)
    before = _range(panel)

    panel.add_patch_rect(*P1)
    assert _same(_range(panel), before), "creating a patch moved the camera"
    panel._select_patch_artist(0)
    assert _same(_range(panel), before), "selecting a patch moved the camera"
    panel._commit_patch_geometry(0, (1200, 2200, 1200, 2200))
    assert _same(_range(panel), before), "moving a patch moved the camera"
    panel.add_patch_rect(3000, 3500, 3000, 3500)
    panel._remove_patch(0)
    assert _same(_range(panel), before), "deleting a patch moved the camera"

    # ...and the page/popup model sync, which re-renders every artist.
    panel.set_rois_and_patches([], [(2000, 2600, 2000, 2600)], True)
    assert _same(_range(panel), before), "the model sync moved the camera"


def test_a_late_overview_after_a_patch_still_keeps_the_camera(app):
    """The reported ORDER: draw a patch while the overview is still in
    flight, then let it land."""
    panel = _panel()
    panel.set_channel_image(_rgb())
    _wheel(panel)
    panel.add_patch_rect(*P1)
    panel._select_patch_artist(0)
    before = _range(panel)

    _land_overview(panel)

    assert _same(_range(panel), before)
    assert panel._patches, "the patch must survive the overview"


# ══ 3. the gestures work in the adjust state ═════════════════════════════

@pytest.mark.parametrize("mode", [None, "patch", "roi"])
def test_the_wheel_still_zooms_while_a_patch_is_being_adjusted(app, mode):
    panel = _panel()
    panel.set_channel_image(_rgb())
    _land_overview(panel)
    panel._set_mode(mode)
    panel.add_patch_rect(*P1)
    panel._select_patch_artist(0)
    assert panel.is_adjusting_patch() is True
    before = _range(panel)

    _wheel(panel)

    after = _range(panel)
    assert not _same(after, before), f"mode {mode!r}: the wheel did nothing"
    assert (after[1] - after[0]) < (before[1] - before[0]), "not a zoom IN"
    assert panel._thumbnail_camera_touched is True
    assert panel.is_adjusting_patch() is True, "the wheel ended the adjustment"


@pytest.mark.parametrize("mode", [None, "patch", "roi"])
def test_the_middle_drag_still_pans_while_a_patch_is_being_adjusted(app, mode):
    panel = _panel()
    panel.set_channel_image(_rgb())
    _land_overview(panel)
    panel.vb.setRange(QtCore.QRectF(100, 100, 200, 200), padding=0)
    panel._thumbnail_camera_touched = False
    panel._set_mode(mode)
    panel.add_patch_rect(*P1)
    panel._select_patch_artist(0)
    rect_before = tuple(panel._patches[0]["coords"])
    before = _range(panel)

    _middle_drag(panel, -50, -30)

    after = _range(panel)
    assert not _same(after, before), f"mode {mode!r}: the drag did nothing"
    # A pan is a translation: the magnification is untouched.
    assert (after[1] - after[0]) == pytest.approx(before[1] - before[0])
    assert panel._thumbnail_camera_touched is True
    assert panel.is_adjusting_patch() is True
    assert tuple(panel._patches[0]["coords"]) == rect_before


# ══ 4. the two panels own their cameras separately ═══════════════════════

def test_the_page_and_the_popup_own_their_cameras_separately(app):
    """One class, two instances -- the page's thumbnail and the Tissue
    Navigator popup's -- and the bit is per instance. Moving one must not
    hand the other's camera away, or take it back."""
    from block01.ui.widgets.tissue_navigator_popup import TissueNavigatorPopup

    page_panel = _panel()
    popup = TissueNavigatorPopup()
    popup.set_overview_context(loader=_Loader(), nuc_ch="DAPI")
    popup.show()
    QtTest.QTest.qWaitForWindowExposed(popup)
    popup_panel = popup.overview
    popup_panel.ds = DS
    for panel in (page_panel, popup_panel):
        panel.set_channel_image(_rgb())

    _wheel(page_panel)
    assert page_panel._thumbnail_camera_touched is True
    assert popup_panel._thumbnail_camera_touched is False, \
        "the popup inherited the page's camera ownership"

    page_range = _range(page_panel)
    popup_before = _range(popup_panel)
    _land_overview(page_panel)
    _land_overview(popup_panel)

    assert _same(_range(page_panel), page_range), "the page's zoom was lost"
    assert not _same(_range(popup_panel), popup_before), \
        "the popup, untouched, should have fitted the corrected overview"

    popup.close()
    popup.deleteLater()


# ══ 5. a new dataset takes the camera back ═══════════════════════════════

def test_a_new_dataset_clears_the_camera_ownership_and_fits_again(app):
    """Loading another slide is the panel's camera again: the previous
    slide's zoom must not survive into it, and the new picture must fit."""
    panel = _panel()
    panel.set_channel_image(_rgb())
    _wheel(panel)
    _middle_drag(panel, -30, -20)
    assert panel._thumbnail_camera_touched is True

    # A new slide, half the size, through the real load path.
    panel.loader = _Loader(4000, 3001)
    panel.full_h, panel.full_w = panel.loader.shape
    panel._load_overview()
    assert panel._thumbnail_camera_touched is False, \
        "the new slide inherited the old slide's camera ownership"
    assert panel._thumb_fitted is None

    for _ in range(200):
        QtTest.QTest.qWait(10)
        if panel._overview_arr is not None:
            break
    assert panel._overview_arr is not None, "the overview never landed"
    assert (panel.ov_h, panel.ov_w) == panel._overview_arr.shape

    x0, x1, y0, y1 = _range(panel)
    assert x0 < 0 < panel.ov_w < x1 and y0 < 0 < panel.ov_h < y1, \
        "the new slide's first image did not fit"


# ══ 6. the double-click hands the camera back ════════════════════════════

def test_a_double_click_resets_the_view_and_can_be_repeated(app):
    """The explicit "give it back": fit the whole thumbnail, and return to
    a state with no contradiction in it -- untouched, and fitted to the
    geometry that is actually on screen."""
    panel = _panel()
    panel.set_channel_image(_rgb())
    _land_overview(panel)
    fitted = _range(panel)

    for _ in range(3):
        _wheel(panel)
        _middle_drag(panel, -70, -50)
        assert not _same(_range(panel), fitted)
        assert panel._thumbnail_camera_touched is True

        _double_click(panel)

        assert _same(_range(panel), fitted), "the reset did not restore the view"
        assert panel._thumbnail_camera_touched is False
        assert panel._thumb_fitted == (panel.ov_w, panel.ov_h), \
            "reset left _thumb_fitted disagreeing with the camera state"


def test_after_a_reset_a_corrected_overview_shape_may_fit_once_more(app):
    """The state after a reset is defined, not merely tidy: the panel owns
    the camera again, so a LATER shape correction gets its one exact fit --
    the same thing that would have happened had the user never touched it."""
    panel = _panel()
    panel.set_channel_image(_rgb())
    _wheel(panel)
    _double_click(panel)
    reset_to = _range(panel)

    _land_overview(panel)

    assert panel.ov_w == OV_W
    assert not _same(_range(panel), reset_to), \
        "the corrected geometry never got its fit"
    x0, x1, y0, y1 = _range(panel)
    assert x0 < 0 < OV_W < x1 and y0 < 0 < OV_H < y1


# ══ 7. the bit means what it says ════════════════════════════════════════

def test_a_gesture_that_moves_nothing_does_not_claim_the_camera(app):
    """"Touched" is "the user CHANGED the camera", not "the user pressed a
    button". A middle press with no movement, and a zero-delta wheel, leave
    the panel owning the camera -- otherwise a stray click on a half-loaded
    thumbnail would cancel its first fit for good."""
    panel = _panel()
    panel.set_channel_image(_rgb())
    before = _range(panel)

    _send(panel, QtCore.QEvent.MouseButtonPress, QtCore.QPoint(250, 250),
          button=Qt.MiddleButton)
    _send(panel, QtCore.QEvent.MouseMove, QtCore.QPoint(250, 250),
          button=Qt.NoButton, buttons=Qt.MiddleButton)
    _send(panel, QtCore.QEvent.MouseButtonRelease, QtCore.QPoint(250, 250),
          button=Qt.MiddleButton, buttons=Qt.NoButton)
    assert _same(_range(panel), before)
    assert panel._thumbnail_camera_touched is False

    _wheel(panel, delta=0)
    assert _same(_range(panel), before)
    assert panel._thumbnail_camera_touched is False

    # ...so the overview still gets its fit.
    _land_overview(panel)
    assert not _same(_range(panel), before)


# ══ 8. the diagnostic the report is written from ═════════════════════════

@pytest.mark.parametrize("mode", [None, "patch", "roi"])
@pytest.mark.parametrize(
    "state", ["clean", "just_created", "selected", "just_dragged"])
def test_the_events_reach_the_viewport_in_every_patch_state(app, mode, state):
    """Every state the complaint mentions, with the events counted where
    they arrive rather than inferred from the picture.

    This is the evidence, not the fix: the press, four moves and the
    release all reach the viewport, each move translates the camera once,
    the panel holds the pointer for the whole gesture, and neither the
    wheel nor the drag is swallowed by the adjust state, by a mode, or by
    a patch that was drawn a moment ago.
    """
    panel = _panel()
    panel.set_channel_image(_rgb())
    _land_overview(panel)
    panel.vb.setRange(QtCore.QRectF(100, 100, 200, 200), padding=0)
    panel._set_mode(mode)

    if state != "clean":
        panel.add_patch_rect(*P1)
    if state in ("selected", "just_dragged"):
        panel._select_patch_artist(0)
    if state == "just_dragged":
        panel._begin_patch_drag(0, None, 150.0, 150.0, QtCore.QPoint(10, 10))
        panel._commit_patch_geometry(0, (1100, 2100, 1100, 2100))
        panel._patch_drag = None

    seen = {"wheel": 0, "press": 0, "move": 0, "release": 0}
    real = panel.eventFilter

    def counting(obj, event):
        if obj is panel.gview.viewport():
            t = event.type()
            if t == QtCore.QEvent.Wheel:
                seen["wheel"] += 1
            elif t == QtCore.QEvent.MouseButtonPress \
                    and event.button() == Qt.MiddleButton:
                seen["press"] += 1
            elif t == QtCore.QEvent.MouseMove \
                    and (event.buttons() & Qt.MiddleButton):
                seen["move"] += 1
            elif t == QtCore.QEvent.MouseButtonRelease \
                    and event.button() == Qt.MiddleButton:
                seen["release"] += 1
        return real(obj, event)

    panel.eventFilter = counting
    try:
        before_wheel = _range(panel)
        _wheel(panel)
        after_wheel = _range(panel)

        steps = []
        p0 = QtCore.QPoint(250, 250)
        _send(panel, QtCore.QEvent.MouseButtonPress, p0,
              button=Qt.MiddleButton)
        held = QtWidgets.QWidget.mouseGrabber()
        for i in range(1, 5):
            r = _range(panel)
            _send(panel, QtCore.QEvent.MouseMove,
                  QtCore.QPoint(p0.x() - 10 * i, p0.y() - 6 * i),
                  button=Qt.NoButton, buttons=Qt.MiddleButton)
            n = _range(panel)
            steps.append((n[0] - r[0], n[2] - r[2]))
        _send(panel, QtCore.QEvent.MouseButtonRelease,
              QtCore.QPoint(p0.x() - 40, p0.y() - 24),
              button=Qt.MiddleButton, buttons=Qt.NoButton)
    finally:
        panel.eventFilter = real

    label = f"mode={mode!r} state={state}"
    assert seen == {"wheel": 1, "press": 1, "move": 4, "release": 1}, label
    assert not _same(after_wheel, before_wheel), f"{label}: wheel dead"
    assert (after_wheel[1] - after_wheel[0]) < \
        (before_wheel[1] - before_wheel[0]), f"{label}: not a zoom in"
    # Every move translated, each in the same direction, none dropped.
    assert len(steps) == 4, label
    for dx, dy in steps:
        assert dx > 0 and dy > 0, f"{label}: a middle move did not pan ({steps})"
    # Offscreen refuses a platform grab but Qt records the widget anyway;
    # either way the panel must not have grabbed somebody ELSE's widget.
    assert held in (None, panel.gview.viewport()), label
    assert QtWidgets.QWidget.mouseGrabber() is None, f"{label}: grab leaked"
    assert panel._mid_pan_last is None, f"{label}: the gesture never ended"


@pytest.mark.parametrize(
    "state", ["clean", "just_created", "selected", "just_dragged"])
def test_a_delayed_overview_after_a_gesture_in_any_patch_state_keeps_it(
        app, state):
    """The other half of "it looks dead": the gesture worked, and then
    something that landed a moment later put the view back."""
    panel = _panel()
    panel.set_channel_image(_rgb())
    if state != "clean":
        panel.add_patch_rect(*P1)
    if state in ("selected", "just_dragged"):
        panel._select_patch_artist(0)
    if state == "just_dragged":
        panel._commit_patch_geometry(0, (1100, 2100, 1100, 2100))

    _wheel(panel)
    _middle_drag(panel, -40, -30)
    after_gesture = _range(panel)

    _land_overview(panel)
    assert _same(_range(panel), after_gesture), f"{state}: the overview undid it"
    panel.set_channel_image(_rgb(value=3))
    assert _same(_range(panel), after_gesture), f"{state}: the channel undid it"
    panel.set_rois_and_patches([], [P1], True)
    assert _same(_range(panel), after_gesture), f"{state}: the sync undid it"
