"""The full image continues the compare panel it was opened from.

`test_step0_full_image_viewport.py` pins the REGION the drill-down asks for
-- which part of the slide. This module pins the CAMERA: the pixels the user
was looking at in the panel must stay at the same size and at the same place
on the screen when the bigger full-image widget takes over, which is not the
same statement. Opening on the region alone cannot do it: the full widget has
a different size and a different aspect, and an aspect-locked ViewBox handed
a rect FITS it, so the picture grows.

Three claims, tested separately:

  1. the MATH -- a pure function turns (panel scale, the world point at the
     panel's top-left, where that corner is on the desktop, the full view's
     own screen rect) into the world range the full view must hold, including
     the case where the two widgets are offset from each other on screen;
  2. the READING -- the page takes that camera off the panel that was
     CLICKED, in level-0 units, and refuses when the panel cannot define one;
  3. the DELIVERY -- entering the full image applies it through the
     controller, on the cold path and again on a re-entry, and falls back to
     the region-based open when there is no camera or no controller support.

Its own module, like the other page-heavy Step0 suites (see that module's
docstring). No slide and no wall-clock: synthetic payloads, real Qt widgets
for the geometry, and a recording controller.
"""

import math
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

import pyqtgraph as pg  # noqa: E402
from PyQt5 import QtWidgets  # noqa: E402

from block01.ui.step0 import step0_page as sp  # noqa: E402

from test_step0_background_correction_tab import app  # noqa: E402,F401

PATCH = (1000, 1030, 4000, 4070)       # (y0, y1, x0, x1), half-open, level-0
SLIDE_H, SLIDE_W = 29000, 31000


# ── 1. the math ──────────────────────────────────────────────────────────
#
# Hand-computed throughout. `world = world_tl + (global - global_tl) / scale`
# is the whole rule; the cases below vary one term at a time.

def test_the_same_screen_rect_gives_back_the_panel_itself():
    """Degenerate on purpose: a full view sitting exactly where the panel is
    and exactly its size must reproduce the panel's own range."""
    got = sp.Step0Page._full_view_range_for_panel(
        2.0, (1000.0, 500.0), (100.0, 200.0), (100.0, 200.0, 400.0, 300.0))

    # 400 px / 2 px-per-world = 200 world units wide, 150 tall.
    assert got == (1000.0, 1200.0, 500.0, 650.0)


def test_a_bigger_widget_adds_surroundings_at_the_same_scale():
    """The full view is bigger but starts at the same screen corner: the
    world top-left is UNCHANGED and only the extent grows."""
    got = sp.Step0Page._full_view_range_for_panel(
        2.0, (1000.0, 500.0), (100.0, 200.0), (100.0, 200.0, 900.0, 700.0))

    x0, x1, y0, y1 = got
    assert (x0, y0) == (1000.0, 500.0)
    assert (x1 - x0, y1 - y0) == (450.0, 350.0)
    # Same scale, which is the promise: px / world is still 2.
    assert 900.0 / (x1 - x0) == 2.0


def test_a_full_view_offset_on_screen_shifts_the_world_the_other_way():
    """The reviewer's case. The full view's top-left is 40 px LEFT and 60 px
    BELOW the panel's, so the world point it shows there is 40/2 = 20 units
    left and 60/2 = 30 units down from the panel's."""
    got = sp.Step0Page._full_view_range_for_panel(
        2.0, (1000.0, 500.0), (100.0, 200.0), (60.0, 260.0, 800.0, 600.0))

    assert got == (980.0, 1380.0, 530.0, 830.0)


def test_the_world_point_under_a_given_pixel_is_preserved():
    """Stated as the user-visible invariant rather than as a rect: pick a
    global pixel inside BOTH viewports and the two cameras must name the
    same world point for it."""
    scale, world_tl, global_tl = 3.0, (7000.0, 2500.0), (300.0, 400.0)
    full = (250.0, 380.0, 1000.0, 800.0)

    x0, x1, y0, y1 = sp.Step0Page._full_view_range_for_panel(
        scale, world_tl, global_tl, full)

    gx, gy = 640.0, 700.0                       # a pixel inside both
    by_panel = (world_tl[0] + (gx - global_tl[0]) / scale,
                world_tl[1] + (gy - global_tl[1]) / scale)
    by_full = (x0 + (gx - full[0]) / scale, y0 + (gy - full[1]) / scale)
    assert by_full == pytest.approx(by_panel)
    # And the range really is the widget's size at that scale.
    assert (x1 - x0, y1 - y0) == pytest.approx((1000.0 / 3.0, 800.0 / 3.0))


def test_a_fractional_scale_zooms_out_not_in():
    """Zoomed OUT in the panel (half a screen pixel per slide pixel): the
    same widget then covers twice as many world units."""
    got = sp.Step0Page._full_view_range_for_panel(
        0.5, (0.0, 0.0), (0.0, 0.0), (0.0, 0.0, 400.0, 200.0))

    assert got == (0.0, 800.0, 0.0, 400.0)


def test_nothing_is_clipped_to_the_slide():
    """Negative world coordinates are the honest answer near the slide's
    corner: that is background, and it is what the widget's extra area
    actually looks onto."""
    got = sp.Step0Page._full_view_range_for_panel(
        1.0, (10.0, 5.0), (500.0, 500.0), (0.0, 0.0, 800.0, 600.0))

    assert got == (-490.0, 310.0, -495.0, 105.0)


@pytest.mark.parametrize(
    ("why", "args"),
    [("no scale", (0.0, (1.0, 1.0), (0.0, 0.0), (0.0, 0.0, 10.0, 10.0))),
     ("negative scale", (-2.0, (1.0, 1.0), (0.0, 0.0), (0.0, 0.0, 10.0, 10.0))),
     ("no width", (2.0, (1.0, 1.0), (0.0, 0.0), (0.0, 0.0, 0.0, 10.0))),
     ("no height", (2.0, (1.0, 1.0), (0.0, 0.0), (0.0, 0.0, 10.0, 0.0))),
     ("non-finite scale",
      (float("inf"), (1.0, 1.0), (0.0, 0.0), (0.0, 0.0, 10.0, 10.0))),
     ("non-finite world",
      (2.0, (float("nan"), 1.0), (0.0, 0.0), (0.0, 0.0, 10.0, 10.0))),
     ("nothing at all", (None, (1.0, 1.0), (0.0, 0.0), (0.0, 0.0, 10.0, 10.0)))],
)
def test_an_impossible_camera_is_no_camera(why, args):
    assert sp.Step0Page._full_view_range_for_panel(*args) is None


# ── the page, with a recording viewer ────────────────────────────────────

class _Controller:
    """Only what the page touches on a stack's controller."""

    def __init__(self):
        self.channel, self.method, self.params = "CD3", None, ()
        self.rects = []
        self.jumps = []
        self._current_bbox = None

    def set_view_rect_l0(self, x0, y0, w, h):
        self.rects.append((float(x0), float(y0), float(w), float(h)))

    def jump_to(self, y0, x0, w, h):
        self.jumps.append((y0, x0, w, h))

    def set_marker_visible(self, _visible):
        pass


class _NoRectController(_Controller):
    """An older controller: no exact-range entry point at all."""
    set_view_rect_l0 = None


class _FakeTab(QtWidgets.QWidget):
    """A stand-in Step0ExploreTab holding a REAL pyqtgraph view.

    Real, because the whole point under test is a screen measurement: the
    ViewBox has to sit in a scene, in a QGraphicsView, in the page, so that
    `mapToGlobal` means something.
    """

    def __init__(self, parent=None, controller=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.graphics = pg.GraphicsLayoutWidget()
        layout.addWidget(self.graphics)
        view_box = self.graphics.addViewBox()
        view_box.setAspectLocked(True)
        view_box.invertY(True)

        class _View:
            pass

        view = _View()
        view.view_box = view_box
        self.stack = type("_Stack", (), {})()
        self.stack.view = view
        self.stack.controller = controller or _Controller()
        self.stack.overlay = None
        self.calls = []
        self.released = False

    # -- the Step0ExploreTab surface the page uses --
    def show_source(self, channel, method, params=(), *, viewport_l0=None,
                    tint=None, nucleus=None):
        self.calls.append({"channel": channel, "method": method,
                           "viewport": viewport_l0})
        if viewport_l0 is not None and self.stack is not None:
            self.stack.controller.jump_to(*viewport_l0)
        return self.stack is not None

    def set_dataset(self, _path):
        pass

    def teardown(self, **_kw):
        pass


class _SlideLoader:
    shape = (SLIDE_H, SLIDE_W)

    def channel_names(self):
        return ["DAPI", "CD3", "CD20"]

    @property
    def ch_map(self):
        return {c: i for i, c in enumerate(self.channel_names())}


def _page(app, controller=None, payload=True):
    page = sp.Step0Page()
    page.loader = _SlideLoader()
    page.patches = [PATCH]
    page.current_patch_idx = 0
    page.current_channel = "CD3"
    page.nucleus_channel = "DAPI"
    if payload:
        h, w = PATCH[1] - PATCH[0], PATCH[3] - PATCH[2]
        d = np.linspace(0, 1, h * w, dtype=np.float32).reshape(h, w)
        m = {"snr": 4.0, "bg_cv": 0.25}
        pl = {"original_disp": d, "tophat_disp": d, "cucim_disp": d,
              "nucleus_disp": None, "original_metrics": m,
              "tophat_metrics": m, "cucim_metrics": m}
        page._last_payload = pl
        page._preview_cache[(page.current_channel, page.current_patch_idx)] = pl
    # A real, laid-out page: every measurement below is a screen measurement.
    page.resize(1400, 900)
    page.show()
    QtWidgets.QApplication.processEvents()
    tab = _FakeTab(page, controller=controller)
    page._explore_tab = tab
    page._full_image_host.addWidget(tab, stretch=1)
    QtWidgets.QApplication.processEvents()
    return page, tab


def _screen_rect(view_box):
    """`(global_x, global_y, w_px, h_px)` -- recomputed here, from Qt, so the
    assertions do not lean on the page's own helper.

    `mapRectToScene(rect())`, which is the box's actual viewport;
    `sceneBoundingRect()` is its PAINTED bounds and includes half the border
    pen on each side (see `test_the_border_pen_is_not_measured`).
    """
    rect = view_box.mapRectToScene(view_box.rect())
    gv = view_box.scene().views()[0]
    top_left = gv.mapToGlobal(gv.mapFromScene(rect.topLeft()))
    return (float(top_left.x()), float(top_left.y()),
            float(rect.width()), float(rect.height()))


def _set_panel(page, idx, xr, yr):
    page._btn_lock_zoom.setChecked(False)
    page._preview_vbs[idx].setRange(xRange=xr, yRange=yr, padding=0)
    QtWidgets.QApplication.processEvents()


def _pump():
    """Let the deferred match passes run (see
    `Step0Page._match_full_image_to_panel`)."""
    for _ in range(6):
        QtWidgets.QApplication.processEvents()


# ── 2. reading the camera off the panel ──────────────────────────────────

def test_the_camera_is_read_in_level_zero_units(app):
    page, _tab = _page(app)
    _set_panel(page, 0, (10, 40), (5, 20))

    scale, world_tl, global_tl = page._compare_panel_camera("original")

    vb = page._preview_vbs[0]
    (vx0, vx1), (vy0, _vy1) = vb.viewRange()
    gx, gy, w_px, _h_px = _screen_rect(vb)
    assert scale == pytest.approx(w_px / (vx1 - vx0))
    # The patch origin is added on the RIGHT axis; x carries the patch's x.
    assert world_tl == pytest.approx((PATCH[2] + vx0, PATCH[0] + vy0))
    assert global_tl == pytest.approx((gx, gy))
    page.close()


def test_each_button_reads_its_own_panel(app):
    """With zoom lock off the three panels hold three cameras; reading panel
    0 for every button would pass with the lock on."""
    page, _tab = _page(app)
    for idx, xr in ((0, (0, 10)), (1, (20, 60)), (2, (50, 120))):
        _set_panel(page, idx, xr, (0, 10))

    cams = [page._compare_panel_camera(s) for s in sp.FULL_IMAGE_SOURCES]

    scales = [c[0] for c in cams]
    assert len(set(scales)) == 3, f"all three read one panel: {scales}"
    # Wider range in the same-sized widget => smaller scale, monotonically.
    assert scales == sorted(scales, reverse=True)
    page.close()


@pytest.mark.parametrize(
    ("why", "mutate"),
    [("no payload on screen", lambda p: setattr(p, "_last_payload", None)),
     ("no patch", lambda p: setattr(p, "patches", [])),
     ("payload belongs to another patch",
      lambda p: p._preview_cache.__setitem__(
          (p.current_channel, p.current_patch_idx), {"other": True})),
     ("non-finite range",
      lambda p: setattr(p._preview_vbs[0], "viewRange",
                        lambda: [[0.0, float("nan")], [0.0, 1.0]])),
     ("collapsed range",
      lambda p: setattr(p._preview_vbs[0], "viewRange",
                        lambda: [[5.0, 5.0], [0.0, 1.0]])),
     ("unknown source", lambda p: None)],
)
def test_there_is_no_camera_when_the_panel_cannot_define_one(app, why, mutate):
    page, _tab = _page(app)
    mutate(page)
    source = "nope" if why == "unknown source" else "original"

    assert page._compare_panel_camera(source) is None
    page.close()


def test_a_view_box_with_no_widget_has_no_screen_geometry(app):
    """The fallback's own gate: a ViewBox that is in no scene (and so on no
    screen) measures nothing, rather than measuring zero."""
    assert sp.Step0Page._vb_screen_geometry(None) is None
    assert sp.Step0Page._vb_screen_geometry(pg.ViewBox()) is None


def test_the_border_pen_is_not_measured(app):
    """`sceneBoundingRect()` is the item's PAINTED bounds and carries half
    the border pen on each side -- measured 1306.5 x 652.5 for a 1306 x 652
    box. That half pixel is what the ViewBox's own aspect lock does NOT
    divide by, so measuring it made the range slightly the wrong shape, the
    lock refit it, and the match came out 0.04% off in scale."""
    page, tab = _page(app)
    vb = tab.stack.view.view_box

    _gx, _gy, w, h = sp.Step0Page._vb_screen_geometry(vb)

    painted = vb.sceneBoundingRect()
    assert (w, h) == pytest.approx((vb.rect().width(), vb.rect().height()))
    assert (painted.width(), painted.height()) != pytest.approx((w, h)), (
        "test setup: this platform draws no border, so the two agree")
    page.close()


def test_a_measured_view_box_reports_where_it_is_on_the_desktop(app):
    page, tab = _page(app)

    got = sp.Step0Page._vb_screen_geometry(tab.stack.view.view_box)

    assert got == pytest.approx(_screen_rect(tab.stack.view.view_box))
    page.close()


# ── 3. delivery: the pixels do not move ──────────────────────────────────

def test_entering_the_full_image_keeps_the_scale_and_the_position(app):
    """The whole feature, as one assertion pair: same pixels-per-slide-pixel,
    same world point under the same desktop pixel."""
    page, tab = _page(app)
    _set_panel(page, 2, (10, 40), (5, 20))
    panel_vb = page._preview_vbs[2]
    (vx0, vx1), (vy0, _vy1) = panel_vb.viewRange()
    pgx, pgy, p_w, _p_h = _screen_rect(panel_vb)
    panel_scale = p_w / (vx1 - vx0)

    page._enter_full_image("cucim")
    _pump()

    assert tab.stack.controller.rects, "no exact range was applied"
    x0, y0, w, h = tab.stack.controller.rects[-1]
    fgx, fgy, f_w, f_h = _screen_rect(tab.stack.view.view_box)
    # (a) SAME SCALE -- not the fit the old path produced.
    assert f_w / w == pytest.approx(panel_scale, rel=1e-9)
    assert f_h / h == pytest.approx(panel_scale, rel=1e-9)
    # (b) SAME PLACE -- the world point at the full view's top-left pixel is
    # the one the panel showed at that same desktop pixel.
    expected = (PATCH[2] + vx0 + (fgx - pgx) / panel_scale,
                PATCH[0] + vy0 + (fgy - pgy) / panel_scale)
    assert (x0, y0) == pytest.approx(expected, rel=0, abs=1e-6)
    # And it really is a bigger window, not the same one: more world fits.
    assert w > (vx1 - vx0)
    page.close()


def test_the_full_view_is_not_enlarged_by_the_fit(app):
    """The bug, pinned: `jump_to` on the panel's REGION fits that region into
    the bigger widget, so the scale changes. The exact range must not."""
    page, tab = _page(app)
    _set_panel(page, 0, (10, 40), (5, 20))
    panel_vb = page._preview_vbs[0]
    (vx0, vx1), _ = panel_vb.viewRange()
    _gx, _gy, p_w, _p_h = _screen_rect(panel_vb)
    panel_scale = p_w / (vx1 - vx0)

    page._enter_full_image("original")
    _pump()

    _x0, _y0, w, _h = tab.stack.controller.rects[-1]
    _fgx, _fgy, f_w, _f_h = _screen_rect(tab.stack.view.view_box)
    fit_scale = f_w / (vx1 - vx0)          # what a region-fit would give
    assert fit_scale > panel_scale * 1.05, (
        "test setup: the full view must be enough bigger for the fit to show")
    assert f_w / w == pytest.approx(panel_scale, rel=1e-9)
    page.close()


def test_the_region_is_still_delivered_underneath(app):
    """The region-based open is not removed: it is what a cold build can do
    before its widget has a size, and the exact camera lands on top of it."""
    page, tab = _page(app)
    _set_panel(page, 1, (10, 40), (5, 20))

    page._enter_full_image("tophat")
    _pump()

    assert tab.calls[-1]["viewport"] == page._compare_viewport_l0("tophat")
    assert tab.stack.controller.jumps, "the region open must still happen"
    assert tab.stack.controller.rects, "the exact camera must follow it"
    page.close()


def test_re_entering_uses_the_panel_as_it_is_now(app):
    """The warm path: the stack is already up, and the second click must
    still carry the panel's CURRENT camera, not the first one's."""
    page, tab = _page(app)
    _set_panel(page, 2, (10, 40), (5, 20))
    page._enter_full_image("cucim")
    _pump()
    first = tab.stack.controller.rects[-1]

    page._return_to_compare()
    _set_panel(page, 2, (0, 12), (0, 6))       # zoomed IN
    page._enter_full_image("cucim")
    _pump()

    second = tab.stack.controller.rects[-1]
    assert second != first
    # Zoomed in => a higher scale => less world in the same widget.
    assert second[2] < first[2]
    page.close()


def test_a_channel_change_does_not_reposition(app):
    """Only the button repositions. A channel change or a post-run rebuild
    must leave the user wherever they panned to inside the full image."""
    page, tab = _page(app)
    _set_panel(page, 0, (10, 40), (5, 20))
    page._enter_full_image("original")
    _pump()
    before = len(tab.stack.controller.rects)

    page._sync_full_image_to_channel()
    page._reopen_full_image()
    _pump()

    assert len(tab.stack.controller.rects) == before
    assert [c["viewport"] for c in tab.calls[-2:]] == [None, None]
    page.close()


def test_a_pass_re_applies_only_when_the_widget_has_moved(app):
    """Why there is more than one pass. Qt hands out the full view's size
    over several event-loop turns, and every resize makes the aspect-locked
    ViewBox rescale -- the very refit this feature removes. So a pass whose
    measurement differs from the one that set the range re-applies it, and a
    pass that finds the widget where it left it stops."""
    page, tab = _page(app)
    _set_panel(page, 0, (10, 40), (5, 20))
    camera = page._compare_panel_camera("original")
    page._preview_stack.setCurrentIndex(sp.PREVIEW_PAGE_FULL_IMAGE)
    QtWidgets.QApplication.processEvents()
    settled = sp.Step0Page._vb_screen_geometry(tab.stack.view.view_box)

    # The widget is where the previous pass measured it: nothing to redo.
    assert page._match_full_image_to_panel(
        camera, passes=1, last_rect=settled) is False
    assert tab.stack.controller.rects == []

    # It has been resized since: the range on screen is now the wrong scale.
    stale = (settled[0], settled[1], settled[2] / 2.0, settled[3] / 2.0)
    assert page._match_full_image_to_panel(
        camera, passes=1, last_rect=stale) is True
    assert len(tab.stack.controller.rects) == 1
    page.close()


def test_a_controller_without_the_entry_point_falls_back(app):
    """An older/foreign controller simply keeps the region-based open,
    rather than raising in the middle of a button press."""
    page, tab = _page(app, controller=_NoRectController())
    _set_panel(page, 0, (10, 40), (5, 20))

    page._enter_full_image("original")
    _pump()

    assert tab.stack.controller.jumps == [page._compare_viewport_l0("original")]
    assert tab.stack.controller.rects == []
    page.close()


def test_no_stack_is_not_an_error(app):
    """A refused or failed build leaves no stack; the click must be a no-op
    beyond the page switch."""
    page, tab = _page(app)
    _set_panel(page, 0, (10, 40), (5, 20))
    tab.stack = None

    page._enter_full_image("original")
    _pump()

    assert page._preview_stack.currentIndex() == sp.PREVIEW_PAGE_FULL_IMAGE
    page.close()


# ── 4. the controller's exact-range entry point ──────────────────────────

def test_set_view_rect_l0_sets_both_axes_and_does_not_fit(app):
    """`jump_to` hands the box a rect (which an aspect-locked box fits);
    this one sets the two axes, so a rect already matching the widget's
    aspect survives untouched."""
    from block01.viewer import explore_view as ev

    calls = []

    class _Box:
        def setRange(self, **kw):
            calls.append(kw)

    class _View:
        view_box = _Box()

    class _Ctl(ev.ExploreController):
        def __init__(self):
            self.view = _View()
            self._jumping = False
            self.settle_ms = 80
            self._motion_timer = type("_T", (), {"stop": lambda s: None})()
            self._settle_timer = type(
                "_T", (), {"start": lambda s, ms: None})()
            self.emitted = []

        def _issue_raw_requests(self):
            calls.append("issue")

        def _emit_interaction(self, kind):
            self.emitted.append(kind)

    ctl = _Ctl()
    ctl.set_view_rect_l0(100.0, 200.0, 400.0, 300.0)

    assert calls[0] == {"xRange": (100.0, 500.0), "yRange": (200.0, 500.0),
                        "padding": 0}
    assert "rect" not in calls[0]
    assert calls[1] == "issue", "a camera move must issue its tiles"
    assert ctl.emitted == ["NAVIGATOR_JUMP"]


def test_jump_to_still_means_fit_this_region(app):
    """Other callers (navigator jump, checkpoints) are untouched: they still
    get the rect form, which fits."""
    from block01.viewer import explore_view as ev

    calls = []

    class _Box:
        def setRange(self, **kw):
            calls.append(kw)

    class _View:
        view_box = _Box()

    class _Ctl(ev.ExploreController):
        def __init__(self):
            self.view = _View()
            self._jumping = False
            self.settle_ms = 80
            self._motion_timer = type("_T", (), {"stop": lambda s: None})()
            self._settle_timer = type(
                "_T", (), {"start": lambda s, ms: None})()
            self.emitted = []

        def _issue_raw_requests(self):
            pass

        def _emit_interaction(self, kind):
            self.emitted.append(kind)

    ctl = _Ctl()
    ctl.jump_to(200, 100, 400, 300)

    assert set(calls[0]) == {"rect", "padding"}
    rect = calls[0]["rect"]
    assert (rect.x(), rect.y(), rect.width(), rect.height()) == (
        100.0, 200.0, 400.0, 300.0)
    assert math.isclose(calls[0]["padding"], 0)
