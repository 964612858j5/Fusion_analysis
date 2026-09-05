"""Two DIFFERENT gestures for the same two pages, and why one of them moves.

Manual acceptance: "right-clicking to swap between the full image and the
compare panels still shifts up and to the left, and doing it over and over
walks the window's centre." That report is correct, and the first thing
this module does is state why it is not a bug:

    1. a right-click on the full image means COMPARE HERE -- the level-0
       point under the cursor becomes the panels' centre;
    2. leaving adopts the panels' CURRENT centre and magnification;
    3. therefore, with the mouse parked at a fixed screen pixel that is not
       the centre of the view, each right-click re-centres on a point that
       the PREVIOUS right-click moved under that pixel.

Three cannot hold at once with (1) and (2), and (1) and (2) are the product
contract. The existing tests missed it because they handed the SAME level-0
point P in on every round; a user's hand hands in the same SCREEN pixel,
which is a different world point every time. Type A below sends real
`QMouseEvent`s at a fixed viewport pixel and records what each round's
world point actually is -- documenting the semantics rather than pretending
the drift is not there.

So there is a second way in that has no "here" in it: one toolbar button,
"Compare current center" on the full image and "Back to full image" on the
panels. It reads no mouse position. It is not a second camera state machine
-- it calls the same `_enter_compare_mode` / `_exit_compare_mode` that the
right-click and Esc call, with no point, and entering with no point already
means the middle of the view. Type B presses it ten times and measures the
drift, which is zero.

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
    """Four rounds, mouse never moved. Each entry centres on the point that
    was under the cursor AT THAT MOMENT -- which is a different level-0
    point each round, because the previous round moved the slide under it.

    That is what "Compare here" MEANS. The rounds are recorded rather than
    asserted equal to one another: a test that fed the same P in every time
    is exactly the test that could not see the walk the user reported.
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

    # The documented consequence: a fixed screen pixel is NOT a fixed world
    # point once the first entry has moved the view.
    firsts = rounds[0]["level0"]
    assert rounds[1]["level0"] != pytest.approx(firsts, abs=1.0)


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

    Compare-here then centres there, leaving adopts it, and the next round
    starts from the moved view: a constant half-screen-pixel step, in one
    direction, for ever. On the whole slide fitted to the window that half
    pixel is tens of level-0 pixels, which is the "keeps sliding up and to
    the left" of the manual report.

    Asserted as a BOUND on the offending stage, not corrected. A constant
    added anywhere downstream would only move the bias to a different
    zoom level; the fix, if the product ever wants one, is the button
    below, which names no pixel at all.
    """
    page = _real_page(app)
    vp = _viewport(page)
    px = QtCore.QPointF(float(int(vp.width() / 2)), float(int(vp.height() / 2)))
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
    # Every round moves by the SAME amount -- a constant, not an
    # accumulating error -- and that amount is at most half a screen pixel.
    for dx, dy in steps:
        assert abs(dx) <= 0.5 + 1e-9
        assert abs(dy) <= 0.5 + 1e-9
        assert (dx, dy) == pytest.approx(steps[0], abs=1e-6)


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
    # exit adopts them, and the stacked-page relayout does not shift it.
    assert full_immediate_after_exit[0] == pytest.approx(
        compare_before_exit[0], abs=1.0)
    assert full_immediate_after_exit[1] == pytest.approx(
        compare_before_exit[1], abs=1.0)
    assert full_after_layout_settle == pytest.approx(
        full_immediate_after_exit, rel=1e-9, abs=1e-6)


# ── B. the toolbar button, which reads no mouse ──────────────────────────

def test_the_button_is_named_after_where_it_goes(app):
    page = _real_page(app)
    btn = page._btn_compare_toggle
    assert btn.text() == page._COMPARE_TOGGLE_TO_COMPARE
    btn.click()
    _settle(page)
    assert page._compare_mode() is True
    assert btn.text() == page._COMPARE_TOGGLE_TO_FULL
    btn.click()
    _settle(page)
    assert page._compare_mode() is False
    assert btn.text() == page._COMPARE_TOGGLE_TO_COMPARE


def test_ten_button_round_trips_drift_nowhere(app):
    """The whole point of the button. Ten there-and-backs with nothing else
    touched: no centre drift, no scale drift, and no per-round creep."""
    page = _real_page(app)
    btn = page._btn_compare_toggle
    first_full = page._full_image_camera()
    first_compare = None
    for _ in range(10):
        btn.click()
        _settle(page)
        assert page._compare_mode() is True
        compare = page._compare_camera()
        if first_compare is None:
            first_compare = compare
        assert compare[0] == pytest.approx(first_compare[0], abs=1.0)
        assert compare[1] == pytest.approx(first_compare[1], abs=1.0)
        assert compare[2] == pytest.approx(first_compare[2], rel=1e-6)
        btn.click()
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
    btn = page._btn_compare_toggle
    controllers = None
    for _ in range(10):
        btn.click()
        _settle(page)
        if controllers is None:
            controllers = list(page._compare_strip_widget.controllers)
        btn.click()
        _settle(page)
    assert len(page._compare_builds) == 1
    assert list(page._compare_strip_widget.controllers) == controllers


def test_ten_button_round_trips_make_no_extra_channel_selections(app):
    page = _real_page(app)
    btn = page._btn_compare_toggle
    btn.click()
    _settle(page)
    baseline = [len(c.selections)
                for c in page._compare_strip_widget.controllers]
    btn.click()
    _settle(page)
    for _ in range(9):
        btn.click()
        _settle(page)
        btn.click()
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
    page._btn_compare_toggle.click()
    _settle(page)
    compare = page._compare_camera()
    assert compare[0] == pytest.approx(centre[0], abs=1.0)
    assert compare[1] == pytest.approx(centre[1], abs=1.0)


def test_move_to_q_then_back_then_compare_again_stays_on_q(app):
    """The spec's round trip: compare, move the panels to Q, Back -- the
    full image is centred on Q -- then Compare current center, and the
    panels are on Q again."""
    page = _real_page(app)
    btn = page._btn_compare_toggle
    btn.click()
    _settle(page)
    scale = page._compare_camera()[2]
    q = (2100.0, 900.0)
    page._compare_strip_widget.set_camera(q[0], q[1], scale)
    QtTest.QTest.qWait(20)

    btn.click()                     # Back to full image
    _settle(page)
    full = page._full_image_camera()
    assert full[0] == pytest.approx(q[0], abs=1.0)
    assert full[1] == pytest.approx(q[1], abs=1.0)
    assert full[2] == pytest.approx(scale, rel=1e-6)

    btn.click()                     # Compare current center
    _settle(page)
    compare = page._compare_camera()
    assert compare[0] == pytest.approx(q[0], abs=1.0)
    assert compare[1] == pytest.approx(q[1], abs=1.0)
    assert compare[2] == pytest.approx(scale, rel=1e-6)


def test_the_button_shares_the_right_clicks_way_back(app):
    """Not a second state machine: entering with the button and leaving
    with Esc, or entering with a right-click and leaving with the button,
    both work and both adopt the panels' camera."""
    page = _real_page(app)
    page._btn_compare_toggle.click()
    _settle(page)
    page._on_compare_escape()
    _settle(page)
    assert page._compare_mode() is False

    _send_right_click(page, FIXED_PIXEL)
    _settle(page)
    compare = page._compare_camera()
    page._btn_compare_toggle.click()
    _settle(page)
    assert page._compare_mode() is False
    full = page._full_image_camera()
    assert full[0] == pytest.approx(compare[0], abs=1.0)
    assert full[1] == pytest.approx(compare[1], abs=1.0)


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
    page._btn_compare_toggle.click()
    _settle(page)
    assert seen == [True]
    assert page._compare_mode() is True
    assert page._compare_strip_widget.built is True
