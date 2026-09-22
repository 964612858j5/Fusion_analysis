"""The Full Image <-> Compare round trip must not walk the view.

Manual acceptance: "right-clicking to swap between the full image and the
compare panels still shifts up and to the left, and doing it over and over
walks the window's centre."

AN EARLIER REVISION OF THIS MODULE ARGUED THAT WAS NOT A BUG. That argument
went: a right-click means COMPARE HERE, leaving adopts the panels' camera,
therefore a mouse parked at a fixed screen pixel re-centres each round on
the point the previous round moved under it -- a consequence of the
contract rather than a defect. **The user has ruled otherwise (2026-09-21)
and that product judgement is withdrawn.** The measurement it was built on
was right and is kept; only the conclusion was wrong.

The contract now is:

    1. "Compare here" STAYS: right-click at P and the panels open on P;
    2. leave WITHOUT having moved the panels, and the full image goes back
       to the camera it had before -- a temporary look, not a navigation;
    3. leave AFTER moving them -- a pan, a zoom, a Patch or a Tissue
       landing -- and the full image adopts the panels, exactly as before;
    4. no new button. The removed "Compare current center" stays removed.

Measured on this rig before the fix, ten rounds at one fixed viewport
pixel: a constant `(-334, -115)` screen pixels per round, which is exactly
the pointer's offset from the centre of the viewport -- while every other
seam (entry landing on the click point, the panels between entry and exit,
the exit's application, the layout settle) measured 0.000000. After the
fix the same ten rounds walk 0.0 px and the panned case still lands on Q.

Type A sends real `QMouseEvent`s at a fixed viewport pixel -- the mistake
the older tests made was handing the SAME level-0 point in every round; a
user's hand hands in the same SCREEN pixel, which was a different world
point every time. Type B drives the two page calls the removed button used
to make, which is still the page's own symmetry and still worth pinning.

Own module, like the other page-heavy Step0 suites: combined runs segfault
in offscreen pyqtgraph.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtCore, QtGui, QtTest, QtWidgets  # noqa: E402

from block01.ui.step0 import step0_page as sp  # noqa: E402
from block01.viewer.explore_view import ExploreView  # noqa: E402

from test_step0_compare_tiles import (  # noqa: E402
    SLIDE_H,
    SLIDE_W,
    _FullController,
    _Provider,
    _Store,
    _LowresLoader,
    _fake_compare_factory,
    app,            # noqa: F401  (pytest fixture)
)


# ── a page whose FULL IMAGE is a real ExploreView ────────────────────────
#
# Type A needs real mouse events to reach a real `RightClickViewBox`
# through a real `GraphicsScene`, and it needs to read the level-0 point
# through the SAME mapping production uses. Nothing short of the real
# widget can be trusted to answer that: the whole question is whether
# screen -> scene -> view agrees with where the page then puts the camera.


class _RealFullController(_FullController):
    """The full image's controller, writing its camera to a REAL ViewBox."""

    def _write(self, x0, y0, w, h):
        self.view_box.setRange(xRange=(x0, x0 + w), yRange=(y0, y0 + h),
                               padding=0)

    def jump_to(self, y0, x0, w, h):
        self.jumps.append((y0, x0, w, h))
        self._current_bbox = (y0, x0, y0 + h, x0 + w)
        self._write(x0, y0, w, h)

    def set_view_rect_l0(self, x0, y0, w, h):
        self.view_rects.append((x0, y0, w, h))
        self._current_bbox = (y0, x0, y0 + h, x0 + w)
        self._write(x0, y0, w, h)


class _RealStack:
    def __init__(self, parent):
        self.view = ExploreView(parent)
        self.controller = _RealFullController()
        self.controller.view_box = self.view.view_box
        self.provider = _Provider()
        self.scheduler = type("S", (), {"raw_cache": None,
                                        "corrected_cache": None})()
        self.overlay = None
        self.controller._owns_overview_store = True
        self.controller._overview_store = _Store()

    @property
    def overview_store(self):
        return getattr(self.controller, "_overview_store", None)


class _RealTab(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.stack = _RealStack(self)
        layout.addWidget(self.stack.view)
        self.calls = []

    def show_source(self, channel, method, params=(), **_kw):
        self.calls.append((channel, method, tuple(params)))
        return True

    def set_dataset(self, _p):
        pass

    def teardown(self, **_kw):
        pass


def _real_page(app):
    page = sp.Step0Page()
    page.loader = _LowresLoader()
    page.ome_path = "/fake/slide.ome.tif"
    page.patches = []
    page.nucleus_channel = "DAPI"
    page._rebuild_channel_list()
    page.current_channel = "CD3"
    tab = _RealTab()
    page._explore_tab = tab
    page._full_image_host.addWidget(tab, stretch=1)
    page._compare_builds = []
    page._compare_strip_widget._stack_factory = _fake_compare_factory(
        page._compare_builds)
    page.resize(1200, 800)
    page.show()
    QtTest.QTest.qWait(50)
    # The full image starts on the whole slide, at its own aspect.
    page._apply_full_image_view_rect((0.0, 0.0, float(SLIDE_W),
                                      float(SLIDE_H)))
    QtTest.QTest.qWait(20)
    # The gesture under test, wired the way production wires it.
    page._connect_full_image_right_click(tab.stack)
    return page


def _settle(page):
    """Let the compare entry's one `singleShot(0)` relayout turn run."""
    for _ in range(4):
        QtTest.QTest.qWait(20)


def _viewport(page):
    return page._explore_tab.stack.view.graphics.viewport()


def _send_right_click(page, pos):
    """A real right-button press and release at viewport pixel `pos`."""
    vp = _viewport(page)
    for kind in (QtCore.QEvent.MouseButtonPress,
                 QtCore.QEvent.MouseButtonRelease):
        QtWidgets.QApplication.sendEvent(
            vp, QtGui.QMouseEvent(kind, QtCore.QPointF(pos), QtCore.Qt.RightButton,
                                  QtCore.Qt.RightButton, QtCore.Qt.NoModifier))
    QtTest.QTest.qWait(10)


def _level0_under(page, pos):
    """The level-0 point at viewport pixel `pos`, through the SAME mapping
    the production right-click handler uses: the graphics view's scene
    mapping, then the ViewBox's `mapToView` (see `RightClickViewBox`)."""
    view = page._explore_tab.stack.view
    scene_pt = view.graphics.mapToScene(QtCore.QPoint(int(pos.x()),
                                                      int(pos.y())))
    world = view.view_box.mapSceneToView(scene_pt)
    return (float(world.x()), float(world.y()))


# ── A. the real-right-click semantics ────────────────────────────────────

FIXED_PIXEL = QtCore.QPointF(180.0, 120.0)      # off-centre, on purpose


def test_a_right_click_at_a_fixed_pixel_centres_that_rounds_world_point(app):
    """Four rounds, mouse never moved.

    "Compare here" is unchanged: each entry centres on the point under the
    cursor at that moment, at the full image's own magnification. What HAS
    changed is the consequence -- because leaving an unmoved comparison now
    puts the full image back, the same screen pixel names the SAME world
    point every round instead of walking.
    """
    page = _real_page(app)
    rounds = []
    for _ in range(4):
        full_before = page._full_image_camera()
        want = _level0_under(page, FIXED_PIXEL)
        _send_right_click(page, FIXED_PIXEL)
        _settle(page)
        assert page._compare_mode() is True
        compare_after = page._compare_camera()
        rounds.append({"full_before": full_before,
                       "screen_pos": (FIXED_PIXEL.x(), FIXED_PIXEL.y()),
                       "level0": want,
                       "compare_after_entry": compare_after})
        # The claim: the panels centre on THIS round's world point.
        assert compare_after[0] == pytest.approx(want[0], abs=1.0)
        assert compare_after[1] == pytest.approx(want[1], abs=1.0)
        # ...and at the full image's magnification, unchanged.
        assert compare_after[2] == pytest.approx(full_before[2], rel=1e-6)
        page._exit_compare_mode()
        _settle(page)

    # The contract's consequence: the view comes back, so a fixed screen
    # pixel IS a fixed world point round after round.
    first = rounds[0]["level0"]
    for row in rounds[1:]:
        assert row["level0"] == pytest.approx(first, abs=1.0), (
            "the same screen pixel named a different world point -- the "
            "view did not come back")
        assert row["full_before"] == pytest.approx(rounds[0]["full_before"],
                                                   rel=1e-9, abs=1e-6)


def test_the_exit_adopts_the_panels_camera_not_the_entry_camera(app):
    """Move the panels to Q, leave, and the full image is centred on Q."""
    page = _real_page(app)
    _send_right_click(page, FIXED_PIXEL)
    _settle(page)
    strip = page._compare_strip_widget
    q = (1234.0, 2345.0)
    strip.set_camera(q[0], q[1], page._compare_camera()[2])
    QtTest.QTest.qWait(20)
    before_exit = page._compare_camera()
    page._exit_compare_mode()
    _settle(page)
    after = page._full_image_camera()
    assert after[0] == pytest.approx(q[0], abs=1.0)
    assert after[1] == pytest.approx(q[1], abs=1.0)
    assert after[2] == pytest.approx(before_exit[2], rel=1e-6)


def test_the_residual_right_click_drift_is_pointer_QUANTISATION(app):
    """Where the last of the walk comes from, measured.

    Park the pointer on the pixel NEAREST the centre of the view and the
    walk does not stop -- it becomes small and perfectly constant. That is
    the whole remaining story, and it is not a bug in any of this page's
    camera code:

    * a pointer names an INTEGER pixel, and the mapping resolves that
      pixel's CORNER;
    * the ViewBox here is 1091 screen pixels wide, so its centre falls on
      a pixel BOUNDARY at x = 554.5 -- no integer pixel can name it;
    * click 554 and the world point is half a screen pixel to the LEFT of
      the centre; click 555 and it is half a screen pixel to the RIGHT.

    Compare-here then centres there. What used to happen next was that
    leaving adopted it and the next round started from the moved view -- a
    constant sub-screen-pixel step, in one direction, for ever.

    THE ENTRY SEAM IS STILL THAT, and is still measured here as a bound:
    the pointer's quantisation is real and nothing downstream should
    pretend otherwise. What no longer happens is the ACCUMULATION, because
    leaving an unmoved comparison puts the full image back -- asserted
    below, and again over ten rounds in
    `test_ten_fixed_pixel_round_trips_do_not_walk`. No compensation
    constant is added anywhere: the step is allowed to exist and is simply
    not carried out of compare mode.

    On this rig the viewport is 1028x470, whose centre (514.0, 235.0) an
    integer pixel names exactly, so this particular gesture measures a step
    of zero; `test_a_half_screen_pixel_click_does_not_accumulate` applies
    the sub-pixel offset directly, where the step is real.
    """
    page = _real_page(app)
    vp = _viewport(page)
    px = QtCore.QPointF(float(int(vp.width() / 2)), float(int(vp.height() / 2)))
    started = page._full_image_camera()
    steps = []
    for _ in range(10):
        before = page._full_image_camera()
        _send_right_click(page, px)
        _settle(page)
        assert page._compare_mode() is True
        entry = page._compare_camera()
        steps.append(((entry[0] - before[0]) * entry[2],
                      (entry[1] - before[1]) * entry[2]))
        page._exit_compare_mode()
        _settle(page)
    # Every round's ENTRY moves by the same amount -- a constant, not an
    # accumulating error -- and that amount is at most half a screen pixel.
    for dx, dy in steps:
        assert abs(dx) <= 0.5 + 1e-9
        assert abs(dy) <= 0.5 + 1e-9
        assert (dx, dy) == pytest.approx(steps[0], abs=1e-6)
    # ...and none of it is carried out: the full image is where it started.
    after = page._full_image_camera()
    assert after[0] == pytest.approx(started[0], abs=1e-6)
    assert after[1] == pytest.approx(started[1], abs=1e-6)
    assert after[2] == pytest.approx(started[2], rel=1e-9)


def test_every_stage_after_the_pointer_is_exact(app):
    """The quantisation above is the ONLY loss. Feed the level-0 point in
    directly -- no pointer, no pixel -- and the round trip is exact to
    floating point at every seam: set_camera, the page switch, the layout,
    and `set_view_rect_l0`."""
    page = _real_page(app)
    first = page._full_image_camera()
    for _ in range(10):
        cx, cy, _s = page._full_image_camera()
        page._on_full_image_right_click(cx, cy)
        _settle(page)
        page._exit_compare_mode()
        _settle(page)
    after = page._full_image_camera()
    assert after[0] == pytest.approx(first[0], abs=1e-6)
    assert after[1] == pytest.approx(first[1], abs=1e-6)
    assert after[2] == pytest.approx(first[2], rel=1e-9)


def test_the_numeric_diagnosis_of_one_round_trip(app):
    """Every stage of one entry+exit, measured, with the >1 px budget
    applied to each seam separately -- so a future regression names the
    stage it broke instead of a total. No compensation constants: the
    numbers are asserted equal, not corrected."""
    page = _real_page(app)
    full_before = page._full_image_camera()
    right_click_level0 = _level0_under(page, FIXED_PIXEL)
    _send_right_click(page, FIXED_PIXEL)
    _settle(page)
    compare_after_entry = page._compare_camera()
    compare_before_exit = page._compare_camera()
    page._exit_compare_mode()
    full_immediate_after_exit = page._full_image_camera()
    _settle(page)
    full_after_layout_settle = page._full_image_camera()

    # screen -> scene -> level-0 -> the panels' centre.
    assert compare_after_entry[0] == pytest.approx(right_click_level0[0], abs=1.0)
    assert compare_after_entry[1] == pytest.approx(right_click_level0[1], abs=1.0)
    # the full image's magnification survives the trip in.
    assert compare_after_entry[2] == pytest.approx(full_before[2], rel=1e-6)
    # the panels do not move between entry and exit.
    assert compare_before_exit == pytest.approx(compare_after_entry, rel=1e-9,
                                                abs=1e-6)
    # ...so nothing was navigated, and the exit puts the full image back
    # where it was rather than adopting the "compare here" point.
    assert full_immediate_after_exit[0] == pytest.approx(full_before[0],
                                                         abs=1e-6)
    assert full_immediate_after_exit[1] == pytest.approx(full_before[1],
                                                         abs=1e-6)
    assert full_immediate_after_exit[2] == pytest.approx(full_before[2],
                                                         rel=1e-9)
    # and the stacked-page relayout does not shift it afterwards.
    assert full_after_layout_settle == pytest.approx(
        full_immediate_after_exit, rel=1e-9, abs=1e-6)



def _toggle(page):
    """What the removed toolbar button did: swap pages on the CURRENT camera.

    The `Compare current center` button is gone (user ruling, 2026-09-15) --
    a right-click is enough -- but the symmetry it was built to prove is the
    page's, not the widget's: entering on the view's own centre and leaving
    on the panels' own centre must be the identity. That is what these tests
    still pin, through the same two calls the button made.
    """
    if page._compare_mode():
        page._exit_compare_mode()
    else:
        page._enter_compare_mode()


# ── B. the toolbar button, which reads no mouse ──────────────────────────

def test_the_compare_toggle_button_is_gone(app):
    """The button was removed: a right-click already opens the comparison,
    and Esc / a right-click already leave it."""
    page = _real_page(app)
    assert not hasattr(page, "_btn_compare_toggle")
    assert not hasattr(page, "_on_compare_toggle_clicked")
    assert not hasattr(page, "_COMPARE_TOGGLE_TO_COMPARE")
    # ...and the two moves it made are still the page's own
    assert callable(page._enter_compare_mode)
    assert callable(page._exit_compare_mode)


def test_ten_button_round_trips_drift_nowhere(app):
    """The whole point of the button. Ten there-and-backs with nothing else
    touched: no centre drift, no scale drift, and no per-round creep."""
    page = _real_page(app)
    first_full = page._full_image_camera()
    first_compare = None
    for _ in range(10):
        _toggle(page)
        _settle(page)
        assert page._compare_mode() is True
        compare = page._compare_camera()
        if first_compare is None:
            first_compare = compare
        assert compare[0] == pytest.approx(first_compare[0], abs=1.0)
        assert compare[1] == pytest.approx(first_compare[1], abs=1.0)
        assert compare[2] == pytest.approx(first_compare[2], rel=1e-6)
        _toggle(page)
        _settle(page)
        assert page._compare_mode() is False
        full = page._full_image_camera()
        assert full[0] == pytest.approx(first_full[0], abs=1.0)
        assert full[1] == pytest.approx(first_full[1], abs=1.0)
        assert full[2] == pytest.approx(first_full[2], rel=1e-6)
    # The full image's centre is also what the panels were centred on.
    assert first_compare[0] == pytest.approx(first_full[0], abs=1.0)
    assert first_compare[1] == pytest.approx(first_full[1], abs=1.0)


def test_ten_button_round_trips_build_the_backend_once(app):
    page = _real_page(app)
    controllers = None
    for _ in range(10):
        _toggle(page)
        _settle(page)
        if controllers is None:
            controllers = list(page._compare_strip_widget.controllers)
        _toggle(page)
        _settle(page)
    assert len(page._compare_builds) == 1
    assert list(page._compare_strip_widget.controllers) == controllers


def test_ten_button_round_trips_make_no_extra_channel_selections(app):
    page = _real_page(app)
    _toggle(page)
    _settle(page)
    baseline = [len(c.selections)
                for c in page._compare_strip_widget.controllers]
    _toggle(page)
    _settle(page)
    for _ in range(9):
        _toggle(page)
        _settle(page)
        _toggle(page)
        _settle(page)
    assert [len(c.selections)
            for c in page._compare_strip_widget.controllers] == baseline


def test_the_button_never_reads_the_mouse(app):
    """Park the pointer far off-centre and press the button: it still
    compares the CENTRE. This is the difference from a right-click, and it
    is the assertion that a "use the last mouse position" implementation
    would fail."""
    page = _real_page(app)
    # Establish that the off-centre pixel is a different world point.
    off_centre = _level0_under(page, FIXED_PIXEL)
    centre = page._full_image_camera()[:2]
    assert abs(off_centre[0] - centre[0]) > 100.0
    _toggle(page)
    _settle(page)
    compare = page._compare_camera()
    assert compare[0] == pytest.approx(centre[0], abs=1.0)
    assert compare[1] == pytest.approx(centre[1], abs=1.0)


def test_move_to_q_then_back_then_compare_again_stays_on_q(app):
    """The spec's round trip: compare, move the panels to Q, Back -- the
    full image is centred on Q -- then Compare current center, and the
    panels are on Q again."""
    page = _real_page(app)
    _toggle(page)
    _settle(page)
    scale = page._compare_camera()[2]
    q = (2100.0, 900.0)
    page._compare_strip_widget.set_camera(q[0], q[1], scale)
    QtTest.QTest.qWait(20)

    _toggle(page)                     # Back to full image
    _settle(page)
    full = page._full_image_camera()
    assert full[0] == pytest.approx(q[0], abs=1.0)
    assert full[1] == pytest.approx(q[1], abs=1.0)
    assert full[2] == pytest.approx(scale, rel=1e-6)

    _toggle(page)                     # Compare current center
    _settle(page)
    compare = page._compare_camera()
    assert compare[0] == pytest.approx(q[0], abs=1.0)
    assert compare[1] == pytest.approx(q[1], abs=1.0)
    assert compare[2] == pytest.approx(scale, rel=1e-6)


def test_the_button_shares_the_right_clicks_way_back(app):
    """Not a second state machine: the two ways in and the two ways out are
    the same two page calls, and they agree about what leaving means.

    Entering with a right-click at P and leaving WITHOUT having moved the
    panels goes back to the camera the full image had -- the same answer
    Esc gives, and the same answer the page gives when it was entered with
    no point at all.
    """
    page = _real_page(app)
    _toggle(page)
    _settle(page)
    page._on_compare_escape()
    _settle(page)
    assert page._compare_mode() is False

    before = page._full_image_camera()
    _send_right_click(page, FIXED_PIXEL)
    _settle(page)
    compare = page._compare_camera()
    # the panels really did go somewhere else -- otherwise the assertion
    # below would be true for the wrong reason.
    assert abs(compare[0] - before[0]) * compare[2] > 10.0
    _toggle(page)
    _settle(page)
    assert page._compare_mode() is False
    full = page._full_image_camera()
    assert full[0] == pytest.approx(before[0], abs=1e-6)
    assert full[1] == pytest.approx(before[1], abs=1e-6)
    assert full[2] == pytest.approx(before[2], rel=1e-9)


def test_a_cold_first_press_still_shows_the_preparing_badge(app):
    """The button is an entry, not a bypass: a cold build still puts the
    badge over the full image for the turn the build takes."""
    page = _real_page(app)
    seen = []
    real = page._show_preparing_compare

    def spy():
        seen.append(True)
        return real()

    page._show_preparing_compare = spy
    _toggle(page)
    _settle(page)
    assert seen == [True]
    assert page._compare_mode() is True
    assert page._compare_strip_widget.built is True


# ═══════════════════════════════════════════════════════════════════════
# C. The 2026-09-21 ruling, gated.
#
# "Compare here" stays; a comparison the user never moved does not move the
# full image; a comparison they DID move is adopted exactly as before. No
# new button, no compensation constant, no direction-specific correction.
# ═══════════════════════════════════════════════════════════════════════

def _round_trip(page, pixel, exit_with="right_click"):
    """One round trip at `pixel`, panels untouched.

    IN through a real `QMouseEvent` on the full image -- that gesture is the
    whole subject. OUT through the production handler the panels' own
    right-click is connected to (`_on_compare_right_click`, see
    `_connect_compare_right_click`) or through Esc. A second mouse event on
    the full image's viewport would reach nothing: the full image is hidden
    while comparing, and the way back is wired to the PANELS' views.
    """
    _send_right_click(page, pixel)
    _settle(page)
    assert page._compare_mode() is True
    if exit_with == "escape":
        page._on_compare_escape()
    else:
        page._on_compare_right_click()
    _settle(page)
    assert page._compare_mode() is False


def _screen_delta(before, after):
    """`(dx, dy)` between two cameras, in SCREEN pixels."""
    scale = float(after[2])
    return (abs(float(after[0]) - float(before[0])) * scale,
            abs(float(after[1]) - float(before[1])) * scale)


# ── Gate A: ten rounds at a fixed OFF-CENTRE pixel ────────────────────

def test_ten_fixed_pixel_round_trips_do_not_walk(app):
    """The user's own gesture, ten times, with a real mouse.

    Measured before the fix on this rig: a constant `(-334, -115)` screen
    pixels per round -- exactly the pointer's offset from the centre of the
    viewport -- for a total walk of `(-3340, -1150)`. The budget here is one
    screen pixel PER ROUND and for the whole run, and the scale must not
    creep either.
    """
    page = _real_page(app)
    first = page._full_image_camera()
    previous = first
    for index in range(10):
        _round_trip(page, FIXED_PIXEL)
        now = page._full_image_camera()
        dx, dy = _screen_delta(previous, now)
        assert dx <= 1.0 and dy <= 1.0, (
            f"round {index} moved the full image by ({dx:.3f}, {dy:.3f}) "
            "screen px")
        assert now[2] == pytest.approx(previous[2], rel=1e-9), (
            f"round {index} changed the magnification")
        previous = now
    total_x, total_y = _screen_delta(first, previous)
    assert total_x <= 1.0 and total_y <= 1.0, (
        f"ten round trips walked ({total_x:.3f}, {total_y:.3f}) screen px")
    assert previous[2] == pytest.approx(first[2], rel=1e-9)


# ── Gate B: a genuine SUB-PIXEL offset must not accumulate ────────────

def test_a_half_screen_pixel_click_does_not_accumulate(app):
    """The older suite's half-pixel story, applied where it is real.

    That suite described a 1091-wide viewport whose centre falls on a pixel
    BOUNDARY, so no integer pixel can name it. This rig's offscreen layout
    pins the viewport at 1028x470 whatever the page is resized to (measured:
    1200..1301 all give 1028) and 514 names its centre exactly -- so that
    geometry cannot be reproduced here and the nearest-integer gesture
    measures a step of zero. The sub-pixel offset is therefore applied
    directly, which is the phenomenon rather than the geometry.

    Measured before the fix: exactly 1 screen pixel per round, 10 px over
    ten rounds. No compensation constant is used to remove it -- the step
    is simply not carried out of compare mode.
    """
    page = _real_page(app)
    vp = _viewport(page)
    half = QtCore.QPointF(vp.width() / 2.0 + 0.5, vp.height() / 2.0 + 0.5)
    first = page._full_image_camera()
    for _ in range(10):
        _round_trip(page, half)
    last = page._full_image_camera()
    dx, dy = _screen_delta(first, last)
    assert dx <= 1.0 and dy <= 1.0, (
        f"a half-pixel click accumulated ({dx:.3f}, {dy:.3f}) screen px "
        "over ten rounds")
    assert last[2] == pytest.approx(first[2], rel=1e-9)


# ── Gate C: "Compare here" still means here ───────────────────────────

def test_compare_here_still_opens_on_the_clicked_point(app):
    """The capability the ruling explicitly keeps."""
    page = _real_page(app)
    before = page._full_image_camera()
    want = _level0_under(page, FIXED_PIXEL)
    _send_right_click(page, FIXED_PIXEL)
    _settle(page)
    entry = page._compare_camera()
    assert entry[0] == pytest.approx(want[0], abs=1.0)
    assert entry[1] == pytest.approx(want[1], abs=1.0)
    # ...at the magnification the full image was at.
    assert entry[2] == pytest.approx(before[2], rel=1e-6)
    # ...and it really is somewhere else, so this is not vacuous.
    assert abs(entry[0] - before[0]) * entry[2] > 10.0


# ── Gate D: every stage of an unmoved round trip ──────────────────────

def test_an_unmoved_comparison_returns_the_entry_camera_at_every_stage(app):
    page = _real_page(app)
    full_before = page._full_image_camera()
    want = _level0_under(page, FIXED_PIXEL)
    _send_right_click(page, FIXED_PIXEL)
    _settle(page)
    entry = page._compare_camera()
    assert entry[0] == pytest.approx(want[0], abs=1.0)
    before_exit = page._compare_camera()
    assert before_exit == pytest.approx(entry, rel=1e-9, abs=1e-6)
    page._exit_compare_mode()
    immediate = page._full_image_camera()
    assert immediate == pytest.approx(full_before, rel=1e-9, abs=1e-6)
    _settle(page)
    settled = page._full_image_camera()
    assert settled == pytest.approx(full_before, rel=1e-9, abs=1e-6)


# ── Gate E / F: a comparison the user DID move is adopted ─────────────

def test_a_panned_comparison_is_still_adopted(app):
    page = _real_page(app)
    full_before = page._full_image_camera()
    _send_right_click(page, FIXED_PIXEL)
    _settle(page)
    scale = page._compare_camera()[2]
    q = (3300.0, 1700.0)
    page._compare_strip_widget.set_camera(q[0], q[1], scale)
    QtTest.QTest.qWait(20)
    page._exit_compare_mode()
    _settle(page)
    full = page._full_image_camera()
    assert full[0] == pytest.approx(q[0], abs=1.0)
    assert full[1] == pytest.approx(q[1], abs=1.0)
    assert full[2] == pytest.approx(scale, rel=1e-6)
    assert abs(full[0] - full_before[0]) * full[2] > 10.0, (
        "the pan was thrown away and the entry camera restored")


def test_a_zoomed_comparison_is_still_adopted_even_with_the_same_centre(app):
    """Gate F: a zoom about an unchanged centre moves no centre at all.

    Comparing centres alone would call this "unmoved" and throw the user's
    magnification away, so the scale is checked first.
    """
    page = _real_page(app)
    _send_right_click(page, FIXED_PIXEL)
    _settle(page)
    cx, cy, scale = page._compare_camera()
    page._compare_strip_widget.set_camera(cx, cy, scale * 2.0)
    QtTest.QTest.qWait(20)
    zoomed = page._compare_camera()
    assert zoomed[2] == pytest.approx(scale * 2.0, rel=1e-3)
    assert zoomed[0] == pytest.approx(cx, abs=1e-6)
    page._exit_compare_mode()
    _settle(page)
    full = page._full_image_camera()
    assert full[2] == pytest.approx(zoomed[2], rel=1e-6), (
        "the zoom was discarded because the centre had not moved")
    assert full[0] == pytest.approx(cx, abs=1.0)


# ── Gate G: Patch / Tissue landings inside compare ────────────────────

def test_a_landing_inside_compare_is_carried_back(app):
    """A Patch or Tissue jump moves the panels through the page's own
    entry, so leaving must land there and not restore the old position."""
    page = _real_page(app)
    _send_right_click(page, FIXED_PIXEL)
    _settle(page)
    scale = page._compare_camera()[2]
    landing = (1200.0, 3400.0)
    assert page._apply_compare_camera(landing[0], landing[1], scale) is True
    QtTest.QTest.qWait(20)
    page._exit_compare_mode()
    _settle(page)
    full = page._full_image_camera()
    assert full[0] == pytest.approx(landing[0], abs=1.0)
    assert full[1] == pytest.approx(landing[1], abs=1.0)


# ── Gate H: changing only the channel is not a camera move ────────────

def test_changing_only_the_channel_still_returns_the_entry_camera(app):
    page = _real_page(app)
    full_before = page._full_image_camera()
    _send_right_click(page, FIXED_PIXEL)
    _settle(page)
    entry_panels = page._compare_camera()
    page.current_channel = "CD20"
    page._refresh_preview_display(keep_zoom=True)
    _settle(page)
    # the channel change moved no camera...
    assert page._compare_camera() == pytest.approx(entry_panels, rel=1e-9,
                                                   abs=1e-6)
    page._exit_compare_mode()
    _settle(page)
    assert page._full_image_camera() == pytest.approx(full_before, rel=1e-9,
                                                      abs=1e-6)


# ── Gate I: the entry reading belongs to one session ──────────────────

def test_the_entry_reading_is_cleared_on_the_way_out(app):
    page = _real_page(app)
    assert page._compare_entry_panel_camera is None
    _send_right_click(page, FIXED_PIXEL)
    _settle(page)
    assert page._compare_entry_panel_camera is not None
    page._exit_compare_mode()
    _settle(page)
    assert page._compare_entry_panel_camera is None, (
        "the entry reading outlived its compare session")


def test_a_dataset_change_drops_the_entry_reading(app):
    page = _real_page(app)
    _send_right_click(page, FIXED_PIXEL)
    _settle(page)
    assert page._compare_entry_panel_camera is not None
    page._exit_compare_mode()
    _settle(page)
    page._compare_entry_panel_camera = (1.0, 2.0, 3.0)      # as if stale
    page._reset_dataset_view_state()
    assert page._compare_entry_panel_camera is None, (
        "a new dataset inherited the previous one's entry reading")
    assert page._compare_entry_full_camera is None


def test_with_no_entry_reading_the_panels_are_adopted_as_before(app):
    """The pre-existing behaviour is what a page with no reading falls back
    to -- so a build that never recorded one cannot restore a stale place."""
    page = _real_page(app)
    _send_right_click(page, FIXED_PIXEL)
    _settle(page)
    panels = page._compare_camera()
    page._compare_entry_panel_camera = None
    assert page._returning_full_camera() == pytest.approx(panels, rel=1e-9,
                                                          abs=1e-6)


# ── Gate J: the shared camera port reads what is on screen ────────────

def test_the_shared_camera_port_follows_the_round_trip(app):
    """Step0's snapshot is the view that is on screen, and an unmoved
    temporary comparison must not permanently rewrite where Step0 is."""
    page = _real_page(app)
    before = page.current_camera_snapshot()
    _send_right_click(page, FIXED_PIXEL)
    _settle(page)
    # while comparing, the panels ARE the view
    assert page.current_camera_snapshot() == pytest.approx(
        page._compare_camera(), rel=1e-9, abs=1e-6)
    page._exit_compare_mode()
    _settle(page)
    after = page.current_camera_snapshot()
    assert after == pytest.approx(before, rel=1e-9, abs=1e-6), (
        "a temporary Compare here permanently moved Step0's shared camera")


# ── the ruling's fourth clause: no new button ─────────────────────────

def test_no_compare_button_was_reintroduced(app):
    """The removed "Compare current center" stays removed, and this fix
    added no entry of its own."""
    import inspect
    source = inspect.getsource(sp)
    for banned in ("Compare current center", "Compare current centre"):
        assert banned not in source, f"{banned!r} came back"
    page = _real_page(app)
    labels = [w.text() for w in page.findChildren(QtWidgets.QAbstractButton)]
    for text in labels:
        assert "compare current" not in (text or "").lower()


def test_the_right_click_and_escape_ways_out_agree(app):
    """Contract: the two exits are the same exit."""
    page = _real_page(app)
    before = page._full_image_camera()
    _round_trip(page, FIXED_PIXEL, exit_with="right_click")
    by_click = page._full_image_camera()
    _round_trip(page, FIXED_PIXEL, exit_with="escape")
    by_escape = page._full_image_camera()
    assert by_click == pytest.approx(before, rel=1e-9, abs=1e-6)
    assert by_escape == pytest.approx(by_click, rel=1e-9, abs=1e-6)
