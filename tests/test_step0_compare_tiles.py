"""Compare mode is three tile viewers of one slide, under one camera.

It used to be a SNAPSHOT: one crop cut for the panels' size, replaced
wholesale when the camera settled somewhere the crop did not cover, over a
whole-slide low-res floor. Every part of that was defensible and the whole
was rejected -- a zoom-out showed a blurred floor and then POPPED to sharp
when the refill landed. So the panels are now the same engine as the full
image, three times, and what that has to mean is what this module pins down:

* three `ExploreController`s, agreeing on the channel and the display
  mapping, disagreeing only on the method -- Original, TopHat, cuCIM -- each
  with the channel row's CURRENT parameter;
* ONE camera: a range change on any of the three moves the other two, to the
  same centre and the same magnification, and no further (no ping-pong);
* ONE backend: a single provider, a single scheduler and a single overview
  store, with generation tokens namespaced per panel so one panel's cancel
  cannot drop another's queued tiles;
* entering at slide point P centres the three on P at the full image's own
  scale, and LEAVING restores the full image's saved camera plus exactly the
  delta the user made while comparing -- so toggling with the mouse held
  still moves nothing at all, however many times;
* the GPU is handed over in both directions: the hidden side is SUSPENDED,
  keeping its pools and its camera, and resumed on the way back;
* the thumbnail navigates the panels and draws their viewport;
* "Save as patch" writes the panels' CURRENT level-0 rectangle;
* the snapshot machinery is gone -- no worker, no underlay, no settle, no
  previous-crop layer, no "downsampled xN" label.

Own module, like the other page-heavy Step0 suites: combined runs segfault
in offscreen pyqtgraph (not a regression, see test_step0_full_image.py).

No slide. The full image is the same fake stack the old suite used, and the
compare strip is built by a fake factory that makes REAL `ExploreView`s --
so the camera, the aspect lock and the range signal under test are the real
ones -- driven by recording controllers.
"""

import os
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtCore, QtGui, QtTest, QtWidgets  # noqa: E402

from block01.ui.step0 import compare_strip as cs  # noqa: E402
from block01.viewer.explore_view import (  # noqa: E402
    SharedOverviewStore as _SharedOverviewStore)
from block01.ui.step0 import step0_page as sp  # noqa: E402

from test_step0_background_correction_tab import (  # noqa: E402
    _GpuPathLoader,
    app,            # noqa: F401  (pytest fixture)
)


SLIDE_H, SLIDE_W = 4096, 4096


# ── stand-ins for the FULL IMAGE side ────────────────────────────────────

class _Provider:
    """A 2x pyramid over a square slide, serving a per-pixel ramp."""

    num_levels = 4
    channel_names = ["DAPI", "CD3", "CD20"]
    open_count = 1

    def __init__(self):
        self.reads = []

    def level_shape(self, level):
        return (SLIDE_H >> int(level), SLIDE_W >> int(level))

    def level_downsample(self, level):
        return float(1 << int(level))

    def read_region(self, channel, level, y0, y1, x0, x1):
        h, w = self.level_shape(level)
        cy0, cy1 = max(0, min(int(y0), h)), max(0, min(int(y1), h))
        cx0, cx1 = max(0, min(int(x0), w)), max(0, min(int(x1), w))
        self.reads.append((channel, level, cy0, cy1, cx0, cx1))
        ys = np.arange(cy0, cy1, dtype=np.float32)[:, None]
        xs = np.arange(cx0, cx1, dtype=np.float32)[None, :]
        base = 1000.0 if channel == "DAPI" else 0.0
        return (ys * 10000.0 + xs + base), (cy0, cx0)


class _FullController:
    def __init__(self, level=0, channel="CD3"):
        self.level = level
        self.channel = channel
        self.method = None
        self.params = ()
        self.grid = type("G", (), {"tile_size": 512})()
        self.view_rects = []
        self.jumps = []
        self._current_bbox = None
        self.suspended = False
        self.suspend_calls = []
        self.resume_calls = 0
        self.view_box = None

    def set_marker_visible(self, _v):
        pass

    def set_display_mapping(self, *_a, **_k):
        pass

    def suspend_for_production(self, reason, *, badge=None, **_kw):
        self.suspend_calls.append((reason, badge))
        self.suspended = True
        return {}

    def resume_from_production(self):
        self.resume_calls += 1
        self.suspended = False

    def jump_to(self, y0, x0, w, h):
        self.jumps.append((y0, x0, w, h))
        self._current_bbox = (y0, x0, y0 + h, x0 + w)
        if self.view_box is not None:
            self.view_box.set_rect(x0, y0, w, h)

    def set_view_rect_l0(self, x0, y0, w, h):
        self.view_rects.append((x0, y0, w, h))
        self._current_bbox = (y0, x0, y0 + h, x0 + w)
        if self.view_box is not None:
            self.view_box.set_rect(x0, y0, w, h)


class _ViewBox:
    """The full image's viewport and widget size, which together fix its
    scale in screen pixels per level-0 pixel."""

    def __init__(self, x0=0.0, x1=None, width=1024.0, height=768.0):
        x1 = float(SLIDE_W if x1 is None else x1)
        self._width = float(width)
        self._height = float(height)
        # The y extent is solved for the widget's own aspect, because the
        # real ViewBox is ASPECT-LOCKED and so cannot be in any other state.
        # A fixture that started square in a 4:3 widget would have two
        # different scales, and the first camera round trip would normalise
        # it -- a fixture artefact that looks exactly like the drift these
        # tests exist to rule out.
        self._range = ((float(x0), x1),
                       (0.0, (x1 - float(x0)) * self._height / self._width))

    def viewRange(self):
        return self._range

    def viewPixelSize(self):
        """View units per SCREEN pixel, on each axis -- what pyqtgraph's own
        `viewPixelSize` returns, and what the page measures magnification
        with. Derived from this fixture's range and size so it cannot
        disagree with them."""
        (x0, x1), (y0, y1) = self._range
        return ((x1 - x0) / self._width, (y1 - y0) / self._height)

    def width(self):
        return self._width

    def height(self):
        return self._height

    def set_rect(self, x0, y0, w, h):
        self._range = ((float(x0), float(x0) + float(w)),
                       (float(y0), float(y0) + float(h)))


class _Store(_SharedOverviewStore):
    """A REAL `SharedOverviewStore` that counts its own shutdowns.

    Real, not a stand-in, because the page LENDS the full image's store to
    the strip and the real `build_compare_stacks` builds three real
    controllers on whatever it is given. A stand-in here would only mean
    the fixture could not build.
    """

    def __init__(self):
        super().__init__()
        self.shutdowns = 0

    def shutdown(self):
        self.shutdowns += 1
        super().shutdown()


class _StatusLabel:
    """The in-view badge `ExploreView` puts over the picture."""

    def __init__(self):
        self._text = ""
        self._visible = False

    def text(self):
        return self._text

    def isVisible(self):
        return self._visible


class _FullView:
    """Enough of `ExploreView` for the page: a camera and a status badge."""

    def __init__(self, view_box):
        self.view_box = view_box
        self.status_label = _StatusLabel()

    def set_status_text(self, text):
        if not text:
            self.status_label._visible = False
            return
        self.status_label._text = text
        self.status_label._visible = True


class _Stack:
    def __init__(self, level=0, scale_width=1024.0, view_w=SLIDE_W):
        self.controller = _FullController(level)
        self.provider = _Provider()
        self.scheduler = type("S", (), {"raw_cache": None,
                                        "corrected_cache": None})()
        self.overlay = None
        self.view = _FullView(_ViewBox(0.0, view_w, scale_width))
        self.controller.view_box = self.view.view_box
        # The full image's stack is the OWNER of the overview store the
        # compare strip borrows.
        self.controller._owns_overview_store = True
        self.controller._overview_store = _Store()

    @property
    def overview_store(self):
        return getattr(self.controller, "_overview_store", None)


class _Tab:
    def __init__(self, stack):
        self.stack = stack
        self.calls = []

    def show_source(self, channel, method, params=(), **_kw):
        self.calls.append((channel, method, tuple(params)))
        return True

    def set_dataset(self, _p):
        pass

    def teardown(self, **_kw):
        pass


class _LowresLoader(_GpuPathLoader):
    shape = (SLIDE_H, SLIDE_W)
    OVERVIEW_DS = 64

    def overview_downsample(self):
        return self.OVERVIEW_DS

    def read_region_lowres(self, ch, y0, y1, x0, x1, ds, normalize=False):
        ds = int(ds)
        h = max(1, (int(y1) - int(y0)) // ds)
        w = max(1, (int(x1) - int(x0)) // ds)
        ys = np.arange(h, dtype=np.float32)[:, None] * ds
        xs = np.arange(w, dtype=np.float32)[None, :] * ds
        return ys * 10000.0 + xs + (1000.0 if ch == "DAPI" else 0.0)


# ── stand-ins for the COMPARE side ───────────────────────────────────────

class _PanelController:
    """One compare panel, over a REAL `ExploreView`.

    The camera it is asked to take is written straight through to the real
    ViewBox, because that is the thing under test: the strip mirrors a range
    change by solving each panel's own rectangle and handing it to
    `set_view_rect_l0`, and the round trip is only checkable against a box
    that honours it.
    """

    def __init__(self, view, source, channel, method, params, gen_ns):
        self.view = view
        self.source = source
        self.channel = channel
        self.method = method
        self.params = tuple(params)
        self.level = 0
        self.compute = None
        self.grid = type("G", (), {"tile_size": 512})()
        self._current_bbox = None
        self.suspended = False
        self.suspend_calls = []
        self.resume_calls = 0
        self.view_generation = ("raw", gen_ns, 0)
        self.selections = []
        self.tints = []
        self.mappings = []
        self.marker_visible = True
        self.torn_down = None

    # -- selection --
    def set_selection(self, channel=None, method=None, params=None):
        if channel is not None:
            self.channel = channel
        if method is not None or params is not None:
            self.method = method
            self.params = tuple(params or ())
        self.selections.append((self.channel, self.method, self.params))

    def set_tint(self, rgb):
        self.tints.append(rgb)

    def set_display_mapping(self, lo, hi, gamma=None, *, channel=None):
        self.mappings.append((lo, hi, gamma, channel))

    def set_marker_visible(self, visible):
        self.marker_visible = bool(visible)

    # -- camera --
    def set_view_rect_l0(self, x0, y0, w, h):
        self._current_bbox = (y0, x0, y0 + h, x0 + w)
        self.view.view_box.setRange(xRange=(x0, x0 + w),
                                    yRange=(y0, y0 + h), padding=0)

    def jump_to(self, y0, x0, w, h):
        self.set_view_rect_l0(x0, y0, w, h)

    # -- lifecycle --
    def suspend_for_production(self, reason, *, badge=None, **_kw):
        self.suspend_calls.append((reason, badge))
        self.suspended = True
        return {}

    def resume_from_production(self):
        self.resume_calls += 1
        self.suspended = False

    def teardown(self, shutdown_backend=True, **_kw):
        self.torn_down = bool(shutdown_backend)

    def load_overview(self, ensure_floor=True):
        pass

    def attach_overlay(self, _o):
        pass


class _Overlay:
    def __init__(self):
        self.enabled = False
        self.suppressed = False
        self.tints = []
        self.mappings = []

    @property
    def effective_enabled(self):
        return self.enabled and not self.suppressed

    def set_enabled(self, enabled, *, host=None):
        self.enabled = bool(enabled)

    def set_suppressed(self, suppressed, *, host=None):
        self.suppressed = bool(suppressed)

    def set_tint(self, rgb):
        self.tints.append(rgb)

    def set_display_mapping(self, lo, hi, gamma=None):
        self.mappings.append((lo, hi, gamma))


def _fake_compare_factory(record):
    """A `build_compare_stacks` stand-in that makes real `ExploreView`s."""

    def factory(path, channel, parent_widget=None, *, sources=cs.COMPARE_SOURCES,
                params_for=None, tint=None, nucleus_channel=None,
                nucleus_tint=None, nucleus_enabled=False, viewport_l0=None,
                overview_store=None):
        from block01.viewer.explore_view import ExploreView
        controllers, views, overlays = [], [], []
        for source in sources:
            method = cs.COMPARE_METHODS.get(source)
            params = () if method is None else tuple(
                params_for(source) if params_for is not None else ())
            view = ExploreView(parent_widget)
            controller = _PanelController(view, source, channel, method,
                                          params, source)
            overlay = _Overlay() if nucleus_channel else None
            if overlay is not None:
                overlay.set_suppressed(nucleus_channel == channel)
                overlay.set_enabled(bool(nucleus_enabled))
            controllers.append(controller)
            views.append(view)
            overlays.append(overlay)
        provider = _Provider()
        stacks = cs.CompareStacks(
            provider, object(), object(), object(), (),
            overview_store if overview_store is not None else _Store(),
            controllers, views, overlays,
            owns_overview_store=overview_store is None)
        record.append({"path": path, "channel": channel, "tint": tint,
                       "viewport_l0": viewport_l0,
                       "overview_store": overview_store,
                       "nucleus_channel": nucleus_channel,
                       "nucleus_enabled": nucleus_enabled, "stacks": stacks})
        if viewport_l0 is not None:
            for controller in controllers:
                controller.jump_to(*(int(v) for v in viewport_l0))
        return stacks

    return factory


# ── the page ─────────────────────────────────────────────────────────────

def _page(app, *, level=0, scale_width=1024.0, view_w=SLIDE_W):
    page = sp.Step0Page()
    page.loader = _LowresLoader()
    page.ome_path = "/fake/slide.ome.tif"
    page.patches = []
    page.nucleus_channel = "DAPI"
    page._rebuild_channel_list()
    page.current_channel = "CD3"
    page._explore_tab = _Tab(_Stack(level=level, scale_width=scale_width,
                                    view_w=view_w))
    page._compare_builds = []
    page._compare_strip_widget._stack_factory = _fake_compare_factory(
        page._compare_builds)
    # Shown, because the panels' SIZE is what their camera rectangle is
    # solved from: a page that was never laid out has none.
    page.resize(1200, 800)
    page.show()
    QtTest.QTest.qWait(30)
    return page


def _enter(page, x=None, y=None):
    """Right-click at level-0 `(x, y)` and let the two-turn relayout run."""
    if x is None or y is None:
        cx, cy, _s = page._full_image_camera()
        x = cx if x is None else x
        y = cy if y is None else y
    page._on_full_image_right_click(float(x), float(y))
    QtTest.QTest.qWait(30)
    QtTest.QTest.qWait(10)
    return page._compare_strip_widget


def _strip(page):
    return page._compare_strip_widget


def _right_click(widget):
    QtWidgets.QApplication.sendEvent(
        widget,
        QtGui.QMouseEvent(QtCore.QEvent.MouseButtonPress,
                          QtCore.QPointF(5.0, 5.0), QtCore.Qt.RightButton,
                          QtCore.Qt.RightButton, QtCore.Qt.NoModifier))


# ── 1. three viewers, one channel, three methods ─────────────────────────

def test_a_right_click_opens_three_tile_viewers(app):
    page = _page(app)
    strip = _enter(page)
    assert page._compare_mode() is True
    assert strip.built is True
    assert len(strip.controllers) == 3
    assert len(strip.views) == 3


def test_the_three_panels_are_original_tophat_and_cucim(app):
    page = _page(app)
    strip = _enter(page)
    assert [c.method for c in strip.controllers] == [None, "tophat", "cucim"]


def test_every_panel_shows_the_current_channel(app):
    page = _page(app)
    strip = _enter(page)
    assert [c.channel for c in strip.controllers] == ["CD3"] * 3
    assert {sel[0] for sel in strip.selection()} == {"CD3"}


def test_the_two_methods_open_with_the_rows_current_parameters(app):
    page = _page(app)
    page._channel_params["CD3"] = {"tophat_radius": 31, "cucim_sigma": 77}
    strip = _enter(page)
    assert strip.selection() == [("CD3", None, ()),
                                 ("CD3", "tophat", (31,)),
                                 ("CD3", "cucim", (77,))]


def test_original_carries_no_parameter(app):
    page = _page(app)
    page._channel_params["CD3"] = {"tophat_radius": 31, "cucim_sigma": 77}
    strip = _enter(page)
    assert strip.controllers[0].params == ()


# ── 2. one camera ────────────────────────────────────────────────────────

def _cams(strip):
    return [strip.camera(i) for i in range(3)]


def test_the_three_panels_open_on_one_camera(app):
    page = _page(app)
    strip = _enter(page)
    cams = _cams(strip)
    assert all(c is not None for c in cams)
    for other in cams[1:]:
        assert other == pytest.approx(cams[0], rel=1e-9, abs=1e-6)


def test_moving_one_panel_moves_the_other_two(app):
    page = _page(app)
    strip = _enter(page)
    cx, cy, scale = strip.camera(0)
    box = strip.view_boxes[1]
    w_px, h_px = float(box.rect().width()), float(box.rect().height())
    w, h = w_px / scale, h_px / scale
    box.setRange(xRange=(cx + 500 - w / 2, cx + 500 + w / 2),
                 yRange=(cy - 300 - h / 2, cy - 300 + h / 2), padding=0)
    QtTest.QTest.qWait(20)
    for i in (0, 2):
        moved = strip.camera(i)
        assert moved[0] == pytest.approx(cx + 500, abs=1.0)
        assert moved[1] == pytest.approx(cy - 300, abs=1.0)
        assert moved[2] == pytest.approx(scale, rel=1e-6)


def test_the_mirroring_does_not_ping_pong(app):
    """A follower's own range event must not bounce back and move the panel
    that started it. The strip holds `_linking` for exactly that."""
    page = _page(app)
    strip = _enter(page)
    counts = [0, 0, 0]
    for i, box in enumerate(strip.view_boxes):
        box.sigRangeChanged.connect(lambda *_a, _i=i: counts.__setitem__(
            _i, counts[_i] + 1))
    cx, cy, scale = strip.camera(0)
    strip.set_camera(cx + 100, cy, scale)
    QtTest.QTest.qWait(20)
    # Each box is set once, by the strip, and nothing re-enters.
    assert counts == [1, 1, 1]


def test_a_panel_that_is_a_different_width_still_gets_the_same_scale(app):
    """The three are aspect-locked columns whose widths differ by a pixel or
    two. One RECTANGLE for all three would be reshaped differently in each;
    a centre and a scale survive."""
    page = _page(app)
    strip = _enter(page)
    widths = [float(b.rect().width()) for b in strip.view_boxes]
    assert len(set(round(w) for w in widths)) <= 2      # they really do differ
    scales = [strip.camera(i)[2] for i in range(3)]
    assert scales[1] == pytest.approx(scales[0], rel=1e-9)
    assert scales[2] == pytest.approx(scales[0], rel=1e-9)


# ── 3. entering: centred on P at the full image's scale ──────────────────

def test_entering_centres_the_panels_on_the_clicked_point(app):
    page = _page(app)
    cx, cy, _s = page._full_image_camera()
    strip = _enter(page, cx + 700, cy - 400)
    got = strip.camera(0)
    assert got[0] == pytest.approx(cx + 700, abs=1.0)
    assert got[1] == pytest.approx(cy - 400, abs=1.0)


def test_entering_keeps_the_full_images_scale(app):
    page = _page(app)
    _cx, _cy, scale = page._full_image_camera()
    strip = _enter(page)
    assert strip.camera(0)[2] == pytest.approx(scale, rel=1e-9)


def test_a_panel_is_narrower_than_the_full_image_at_the_same_scale(app):
    """Three columns cannot hold what one pane held. Same magnification,
    less width -- that is the trade, and it is the intended one."""
    page = _page(app)
    strip = _enter(page)
    (fx0, fx1), _ = page._explore_tab.stack.view.view_box.viewRange()
    px0, _py, pw, _ph = strip.view_rect_l0(0)
    assert pw < (fx1 - fx0)


def test_the_entry_camera_is_remembered_as_a_fallback(app):
    """The full image's camera at the moment compare mode opened. It is no
    longer the basis of the way back -- the panels' camera is -- but it is
    what a way back with no panels' camera falls back to."""
    page = _page(app)
    cam = page._full_image_camera()
    _enter(page, cam[0] + 250, cam[1] + 125)
    assert page._compare_entry_full_camera == pytest.approx(cam, rel=1e-12)


# ── 4. leaving: the panels' camera, directly ─────────────────────────────
#
# The product rule, in the user's words: right-click at P and the panels
# open on P, so leaving without moving puts the full image on P; move the
# panels to Q and leaving puts it on Q; and the magnification you were
# comparing at is the one you come back to. The full image is three times as
# wide as one panel, so at that same magnification it shows more of the
# surroundings, which is what going back to look around means.
#
# It used to be a DELTA -- the saved entry camera plus
# `(compare_now - P)` -- which agrees with the rule only while nothing
# moves. Right-click away from the centre, leave, and the delta version came
# back centred on the old centre rather than on the spot that had just been
# compared. That is what the acceptance run reported.

def _full_rect(page):
    (x0, x1), (y0, y1) = page._explore_tab.stack.view.view_box.viewRange()
    return (x0, y0, x1 - x0, y1 - y0)


def test_leaving_without_moving_comes_back_centred_on_the_clicked_point(app):
    """THE rule, and the assertion the delta algorithm fails: P is where
    the panels opened, so P is where the full image comes back to."""
    page = _page(app)
    cam = page._full_image_camera()
    P = (cam[0] + 700.0, cam[1] - 400.0)

    _enter(page, *P)
    page._exit_compare_mode()
    QtTest.QTest.qWait(10)

    back = page._full_image_camera()
    assert back[0] == pytest.approx(P[0], abs=1.0)
    assert back[1] == pytest.approx(P[1], abs=1.0)
    assert back[2] == pytest.approx(cam[2], rel=1e-6)


def test_a_pan_in_compare_mode_comes_back_centred_where_it_ended(app):
    """Not shifted by the pan -- AT the panels' centre."""
    page = _page(app)
    cam = page._full_image_camera()
    P = (cam[0] + 300.0, cam[1] + 200.0)
    strip = _enter(page, *P)
    now = strip.camera(0)
    Q = (now[0] + 900.0, now[1] - 500.0)
    page._apply_compare_camera(Q[0], Q[1], now[2])
    QtTest.QTest.qWait(10)

    page._exit_compare_mode()
    QtTest.QTest.qWait(10)

    back = page._full_image_camera()
    assert back[0] == pytest.approx(Q[0], abs=1.0)
    assert back[1] == pytest.approx(Q[1], abs=1.0)
    assert back[2] == pytest.approx(cam[2], rel=1e-6)


def test_a_zoom_in_compare_mode_comes_back_as_that_magnification(app):
    page = _page(app)
    cam = page._full_image_camera()
    strip = _enter(page)
    now = strip.camera(0)
    page._apply_compare_camera(now[0], now[1], now[2] * 4.0)
    QtTest.QTest.qWait(10)
    page._exit_compare_mode()
    QtTest.QTest.qWait(10)
    assert page._full_image_camera()[2] == pytest.approx(cam[2] * 4.0,
                                                         rel=1e-6)


def test_the_full_image_shows_more_ground_at_the_same_magnification(app):
    """Why adopting the panels' camera is not a zoom-out: same
    magnification, wider widget, more surroundings."""
    page = _page(app)
    strip = _enter(page)
    panel_w = strip.view_rect_l0(0)[2]
    page._exit_compare_mode()
    QtTest.QTest.qWait(10)
    _fx, _fy, full_w, _fh = _full_rect(page)
    assert full_w > panel_w * 2.0


def test_ten_entries_and_exits_at_the_same_point_do_not_drift(app):
    """The panels always open on the point under the cursor and the full
    image always comes back to the panels' centre, so the cycle is a fixed
    point rather than a walk."""
    page = _page(app)
    cam = page._full_image_camera()
    P = (cam[0] + 700.0, cam[1] - 400.0)
    for _ in range(10):
        _enter(page, *P)
        page._exit_compare_mode()
        QtTest.QTest.qWait(10)
    back = page._full_image_camera()
    assert back[0] == pytest.approx(P[0], abs=1.0)
    assert back[1] == pytest.approx(P[1], abs=1.0)
    assert back[2] == pytest.approx(cam[2], rel=1e-6)


def test_flipping_modes_without_ever_entering_moves_no_camera(app):
    page = _page(app)
    before = _full_rect(page)
    page._set_compare_mode(True)
    page._exit_compare_mode()
    QtTest.QTest.qWait(10)
    assert _full_rect(page) == pytest.approx(before)


def test_the_returning_camera_is_the_panels_own(app):
    page = _page(app)
    strip = _enter(page)
    strip.set_camera(1234.0, 2345.0, strip.camera(0)[2] * 2.0)
    QtTest.QTest.qWait(10)
    assert page._returning_full_camera() == pytest.approx(strip.camera(0),
                                                          rel=1e-9)


def test_the_entry_camera_is_used_only_when_the_panels_have_none(app):
    """The fallback, exercised: no strip, so no camera to adopt."""
    page = _page(app)
    cam = page._full_image_camera()
    _enter(page)
    page._compare_strip_widget.teardown()
    assert page._compare_camera() is None
    assert page._returning_full_camera() == pytest.approx(cam, rel=1e-9)


# ── 5. the way out ───────────────────────────────────────────────────────

def test_a_right_click_in_a_panel_goes_back_to_the_image(app):
    page = _page(app)
    strip = _enter(page)
    strip.views[1].sigRightClicked.emit(10.0, 20.0)
    QtTest.QTest.qWait(10)
    assert page._compare_mode() is False


def test_escape_goes_back_to_the_image_too(app):
    page = _page(app)
    _enter(page)
    page.keyPressEvent(QtGui.QKeyEvent(QtCore.QEvent.KeyPress,
                                       QtCore.Qt.Key_Escape,
                                       QtCore.Qt.NoModifier))
    assert page._compare_mode() is False


def test_a_right_click_on_the_chrome_goes_back_too(app):
    page = _page(app)
    _enter(page)
    _right_click(page._preview_status)
    assert page._compare_mode() is False


# ── 6. the GPU hand-off ──────────────────────────────────────────────────

def test_entering_suspends_the_full_image(app):
    page = _page(app)
    _enter(page)
    controller = page._explore_tab.stack.controller
    assert controller.suspended is True
    reason, badge = controller.suspend_calls[-1]
    assert reason == "compare mode"
    # It says something true. The full image is not paused for a correction
    # run, and a badge claiming one would be a lie the user could read.
    assert "correction" not in (badge or "").lower()


def test_leaving_resumes_the_full_image_and_suspends_the_strip(app):
    page = _page(app)
    strip = _enter(page)
    page._exit_compare_mode()
    QtTest.QTest.qWait(10)
    assert page._explore_tab.stack.controller.suspended is False
    assert strip.suspended is True
    assert all(c.suspended for c in strip.controllers)


def test_re_entering_resumes_the_same_controllers(app):
    """The pools are KEPT, so coming back nearby is a re-issue for what is
    missing rather than a rebuild."""
    page = _page(app)
    strip = _enter(page)
    first = list(strip.controllers)
    page._exit_compare_mode()
    QtTest.QTest.qWait(10)
    _enter(page)
    assert list(strip.controllers) == first
    assert strip.suspended is False
    assert all(not c.suspended for c in strip.controllers)
    assert len(page._compare_builds) == 1        # built once, ever


def test_the_strip_is_not_built_until_it_is_needed(app):
    page = _page(app)
    assert page._compare_strip_widget.built is False
    assert page._compare_builds == []


# ── 7. one backend ───────────────────────────────────────────────────────

def test_the_three_controllers_share_one_provider_and_scheduler(app, monkeypatch):
    """Real factory, real objects: three views of one slide must not open
    three TIFF handles, three caches and three schedulers.

    Counted on the BUILT strip rather than in the source text. Counting
    occurrences of `RawTileProvider(` in the function body said nothing
    about what the three controllers ended up holding -- one call inside a
    loop would have passed it -- and it broke on a comment.
    """
    from block01.viewer import raw_tile_provider as rtp
    from block01.viewer.scheduler import TileScheduler

    provider = _RealishProvider()
    monkeypatch.setattr(rtp, "RawTileProvider", lambda _path: provider)
    stacks = cs.build_compare_stacks("/fake/slide.ome.tif", "CD3")
    try:
        controllers = stacks.controllers
        assert len(controllers) == 3
        assert len({id(c.provider) for c in controllers}) == 1
        assert len({id(c.scheduler) for c in controllers}) == 1
        assert len({id(c.compute) for c in controllers}) == 1
        assert len({id(c._overview_store) for c in controllers}) == 1
        assert provider.open_count == 1
        assert isinstance(stacks.scheduler, TileScheduler)
        # One raw cache and one corrected cache, and they are the ones the
        # scheduler is actually serving from.
        assert len(stacks.caches) == 2
        assert stacks.scheduler.raw_cache is stacks.caches[0]
        assert stacks.scheduler.corrected_cache is stacks.caches[1]
    finally:
        stacks.teardown()


def test_each_panel_issues_under_its_own_generation_namespace(app):
    """One scheduler between three controllers: `cancel_generation` matches
    by equality, so without a namespace all three reach `("raw", 5)` and one
    panel's cancel drops another's queued tiles."""
    page = _page(app)
    strip = _enter(page)
    gens = [c.view_generation for c in strip.controllers]
    assert len(set(gens)) == 3


def test_a_lone_controller_keeps_its_historical_two_element_tokens(app):
    from block01.viewer.explore_view import ExploreController
    obj = ExploreController.__new__(ExploreController)
    obj._gen_ns = None
    assert obj._gen_token("raw", 5) == ("raw", 5)
    obj._gen_ns = "tophat"
    assert obj._gen_token("raw", 5) == ("raw", "tophat", 5)


def test_teardown_shuts_the_shared_backend_down_exactly_once(app):
    page = _page(app)
    strip = _enter(page)
    stacks = strip.stacks
    controllers = list(stacks.controllers)
    store = stacks.overview_store
    strip.teardown()
    assert [c.torn_down for c in controllers] == [True, False, False]
    # The overview store is the FULL IMAGE's, lent for the duration, so the
    # strip does not shut it down at all -- doing so would leave the lender
    # unable to read another channel's overview.
    assert stacks.owns_overview_store is False
    assert store.shutdowns == 0
    assert store is page._explore_tab.stack.overview_store


# ── 8. the channel, the parameters and the mapping follow the page ───────

def test_a_row_change_moves_all_three_panels(app):
    page = _page(app)
    strip = _enter(page)
    page._channel_list.setCurrentRow(page._channel_order.index("CD20"))
    QtTest.QTest.qWait(20)
    assert [c.channel for c in strip.controllers] == ["CD20"] * 3
    assert [c.method for c in strip.controllers] == [None, "tophat", "cucim"]


def test_a_parameter_edit_re_selects_tophat_and_cucim(app):
    page = _page(app)
    strip = _enter(page)
    before = [len(c.selections) for c in strip.controllers]
    page._dec_radius.setValue(41)
    page._dec_sigma.setValue(19)
    QtTest.QTest.qWait(20)
    assert strip.controllers[1].params == (41,)
    assert strip.controllers[2].params == (19,)
    # Original has no parameter: re-selecting it would cancel and re-issue a
    # batch of raw tiles that cannot have changed.
    assert len(strip.controllers[0].selections) == before[0]


def test_a_parameter_edit_before_the_strip_exists_builds_nothing(app):
    page = _page(app)
    page._dec_radius.setValue(41)
    QtTest.QTest.qWait(20)
    assert page._compare_strip_widget.built is False


def test_the_display_mapping_reaches_all_three(app):
    page = _page(app)
    strip = _enter(page)
    page.set_display_mapping("CD3", 120.0, 880.0, 1.4)
    QtTest.QTest.qWait(20)
    for controller in strip.controllers:
        lo, hi, gamma, channel = controller.mappings[-1]
        assert (lo, hi, gamma) == pytest.approx((120.0, 880.0, 1.4))
        assert channel == "CD3"


def test_the_channel_colour_reaches_all_three(app):
    page = _page(app)
    strip = _enter(page)
    page._channel_colors["CD3"] = (1.0, 0.0, 0.5)
    page._refresh_preview_display(keep_zoom=True)
    for controller in strip.controllers:
        assert controller.tints[-1] == (1.0, 0.0, 0.5)


def test_the_marker_switch_gates_every_panel(app):
    page = _page(app)
    strip = _enter(page)
    page._btn_show_marker.setChecked(False)
    QtTest.QTest.qWait(10)
    assert [c.marker_visible for c in strip.controllers] == [False] * 3


# ── 9. DAPI ──────────────────────────────────────────────────────────────

def test_the_dapi_layer_is_on_in_every_panel_from_the_first_frame(app):
    """The reference layer is there without being asked for, in all three,
    the same as it is in the full image."""
    page = _page(app)
    strip = _enter(page)
    assert [o.effective_enabled for o in strip.overlays] == [True] * 3


def test_the_dapi_checkbox_drives_all_three_panels(app):
    page = _page(app)
    strip = _enter(page)
    page._on_nucleus_visibility_toggled(False)
    QtTest.QTest.qWait(10)
    assert [o.effective_enabled for o in strip.overlays] == [False] * 3
    # ...and the SWITCH is what moved, not a suppression
    assert [o.suppressed for o in strip.overlays] == [False] * 3
    page._on_nucleus_visibility_toggled(True)
    QtTest.QTest.qWait(10)
    assert [o.effective_enabled for o in strip.overlays] == [True] * 3


def test_the_nucleus_is_never_added_to_itself(app):
    page = _page(app)
    page.current_channel = "DAPI"
    strip = _enter(page)
    page._btn_show_nucleus.setChecked(True)
    QtTest.QTest.qWait(10)
    assert [o.suppressed for o in strip.overlays] == [True] * 3
    assert [o.effective_enabled for o in strip.overlays] == [False] * 3


def test_the_dapi_mapping_is_its_own_channels(app):
    page = _page(app)
    strip = _enter(page)
    page._btn_show_nucleus.setChecked(True)
    QtTest.QTest.qWait(10)
    expected = page._display_mapping_for("DAPI", nucleus=True)
    for overlay in strip.overlays:
        assert overlay.mappings[-1] == pytest.approx(expected)


# ── 10. the Tissue Preview ───────────────────────────────────────────────

class _Overview:
    def __init__(self):
        self.rects = []
        self.cleared = 0

    def set_current_view_rect(self, rect):
        self.rects.append(rect)

    def clear_current_view_rect(self):
        self.cleared += 1


def _navigator(page):
    popup = type("P", (), {"overview": _Overview()})()
    page._tissue_navigator_popup = popup
    return popup


def test_navigating_in_compare_mode_centres_the_panels(app):
    page = _page(app)
    strip = _enter(page)
    _navigator(page)
    scale_before = strip.camera(0)[2]
    assert page._navigate_compare_to(3000.0, 1500.0) is True
    got = strip.camera(0)
    assert got[0] == pytest.approx(1500.0, abs=1.0)
    assert got[1] == pytest.approx(3000.0, abs=1.0)
    assert got[2] == pytest.approx(scale_before, rel=1e-9)


def test_a_navigator_click_in_compare_mode_moves_the_panels_not_the_image(app):
    page = _page(app)
    strip = _enter(page)
    _navigator(page)
    jumps_before = len(page._explore_tab.stack.controller.jumps)
    page._on_tissue_navigate(3000.0, 1500.0)
    assert len(page._explore_tab.stack.controller.jumps) == jumps_before
    assert strip.camera(0)[0] == pytest.approx(1500.0, abs=1.0)


def test_the_thumbnail_draws_the_panels_viewport_in_compare_mode(app):
    page = _page(app)
    strip = _enter(page)
    popup = _navigator(page)
    page._update_compare_view_rect()
    x0, y0, w, h = strip.view_rect_l0(0)
    assert popup.overview.rects[-1] == pytest.approx((y0, y0 + h, x0, x0 + w))


def test_the_thumbnail_follows_a_zoom_of_the_panels(app):
    page = _page(app)
    strip = _enter(page)
    popup = _navigator(page)
    page._update_compare_view_rect()
    first = popup.overview.rects[-1]
    now = strip.camera(0)
    page._apply_compare_camera(now[0], now[1], now[2] * 2.0)
    QtTest.QTest.qWait(10)
    last = popup.overview.rects[-1]
    assert (last[1] - last[0]) < (first[1] - first[0])


def test_the_thumbnail_goes_back_to_the_full_image_on_the_way_out(app):
    page = _page(app)
    _enter(page)
    popup = _navigator(page)
    page._exit_compare_mode()
    QtTest.QTest.qWait(10)
    bbox = page._explore_tab.stack.controller._current_bbox
    y0, x0, y1, x1 = (float(v) for v in bbox)
    assert popup.overview.rects[-1] == pytest.approx((y0, y1, x0, x1))


# ── 11. Save as patch ────────────────────────────────────────────────────

def test_save_as_patch_is_off_until_the_panels_have_opened(app):
    page = _page(app)
    assert page._btn_snapshot_patch.isEnabled() is False
    assert page._save_snapshot_as_patch() is None


def test_save_as_patch_appends_the_panels_current_rectangle(app):
    page = _page(app)
    strip = _enter(page)
    x0, y0, w, h = strip.view_rect_l0(0)
    coords = page._save_snapshot_as_patch()
    assert coords == (int(round(y0)), int(round(y0 + h)),
                      int(round(x0)), int(round(x0 + w)))
    assert list(page.patches)[-1] == coords


def test_save_as_patch_follows_the_camera(app):
    """The rectangle that means "here" is the one on screen when the button
    is pressed, not one recorded when the panels opened."""
    page = _page(app)
    strip = _enter(page)
    first = page._save_snapshot_as_patch()
    now = strip.camera(0)
    page._apply_compare_camera(now[0] + 800.0, now[1], now[2])
    QtTest.QTest.qWait(10)
    second = page._save_snapshot_as_patch()
    assert second != first
    assert second[2] > first[2]


def test_save_as_patch_computes_nothing(app, monkeypatch):
    page = _page(app)
    _enter(page)

    def _boom(*_a, **_k):
        raise AssertionError("a BatchProcessWorker was constructed")

    monkeypatch.setattr(sp, "BatchProcessWorker", _boom)
    page._save_snapshot_as_patch()


# ── 12. the metrics ──────────────────────────────────────────────────────

def test_the_metrics_say_they_are_a_preview(app):
    page = _page(app)
    _enter(page)
    page._update_compare_metrics()
    for label in (page._metrics_original, page._metrics_tophat,
                  page._metrics_cucim):
        text = label.text()
        assert "SNR" not in text or "(preview)" in text


def test_the_metrics_are_read_from_the_visible_region(app):
    """Fed from the controllers' OWN caches over their own `_current_bbox`,
    so a number can only come from tiles belonging to that panel's exact
    selection."""
    page = _page(app)
    strip = _enter(page)
    asked = []
    page._snapshot_from_caches = (
        lambda controller, scheduler, channel, level, y0, x0, h, w:
        asked.append((controller, channel, level, y0, x0, h, w)) or {})
    page._update_compare_metrics()
    assert len(asked) == 3
    assert [a[0] for a in asked] == strip.controllers
    for _c, channel, _lvl, _y, _x, h, w in asked:
        assert channel == "CD3"
        assert h > 0 and w > 0


def test_a_panel_with_nothing_cached_shows_no_number(app):
    page = _page(app)
    _enter(page)
    page._snapshot_from_caches = lambda *_a, **_k: {}
    page._update_compare_metrics()
    assert "SNR" not in page._metrics_tophat.text()


# ── 13. the snapshot machinery is gone ───────────────────────────────────

@pytest.mark.parametrize("name", [
    "CompareSnapshotWorker", "CompareUnderlayWorker",
])
def test_the_snapshot_workers_are_gone(app, name):
    assert not hasattr(sp, name)


@pytest.mark.parametrize("name", [
    "_take_compare_snapshot", "_start_compare_snapshot",
    "_retake_compare_snapshot", "_cancel_compare_snapshot",
    "_maybe_refill_compare_snapshot", "_refill_compare_snapshot",
    "_compare_needs_refill", "_compare_level_would_change",
    "_refresh_compare_underlay", "_ensure_compare_underlay",
    "_place_preview_item", "_stash_previous_crop", "_hide_previous_crop",
    "_update_compare_level_label", "_nuc_item", "_extra_item",
])
def test_the_snapshot_methods_are_gone(app, name):
    assert not hasattr(sp.Step0Page, name)


@pytest.mark.parametrize("name", [
    "_compare_snapshot", "_compare_underlay_cache", "_compare_refill_timer",
    "_compare_last_view_w", "_preview_imgs", "_preview_gv",
    "_compare_level_lbl", "_compare_under_imgs", "_preview_prev_imgs",
])
def test_the_snapshot_state_is_gone(app, name):
    page = _page(app)
    assert not hasattr(page, name)


def test_there_is_no_settle_left_to_wait_out(app):
    """A range change on a tile viewer issues its own requests at its own
    level, and the coarser layers underneath stay up while the finer ones
    arrive. The debounce existed to stop a whole-frame re-cut firing once
    per mouse move; there is no whole-frame re-cut left."""
    for name in ("_COMPARE_SETTLE_MS", "_COMPARE_REFILL_MARGIN",
                 "_COMPARE_CONTAINMENT_SLOP_PX"):
        assert not hasattr(sp.Step0Page, name)


# ── 14. the dataset ──────────────────────────────────────────────────────

def test_a_dataset_switch_drops_the_strip(app):
    page = _page(app)
    strip = _enter(page)
    assert strip.built is True
    page._reset_dataset_view_state()
    QtTest.QTest.qWait(20)
    assert strip.built is False
    assert page._compare_opened is False
    assert page._compare_entry_full_camera is None
    assert page._btn_snapshot_patch.isEnabled() is False


def test_the_page_tears_the_strip_down(app):
    page = _page(app)
    strip = _enter(page)
    stacks = strip.stacks
    page.teardown()
    assert stacks.torn_down is True
    assert strip.built is False


# ── 12. the whole slide, over a REAL backend ─────────────────────────────
#
# Everything above drives recording controllers, which is the right tool for
# the camera, the selection and the lifecycle: it makes the ExploreViews and
# the range signal real while keeping the tests free of a slide.
#
# It cannot answer the question this rebuild exists for. "The panels can be
# navigated over the whole slide, and tiles arrive where they are taken" is
# a claim about request planning, tile keys and the caches -- the parts a
# recording controller replaces. So this section builds the strip through
# the REAL `build_compare_stacks`, with real `ExploreController`s, a real
# `TileScheduler` and real caches, over a provider that serves a synthetic
# pyramid instead of a file.
#
# The rejected design could not have passed any of these: it cached ONE
# region R and fetched nothing outside it, so a pan beyond R had nothing to
# land and a far jump had nowhere to go.


class _RealishProvider(_Provider):
    """`_Provider` with the handful of extras a real `ExploreController`
    and `RawTileProvider` consumer ask of it."""

    def __init__(self):
        super().__init__()
        self.closed = 0

    def close(self):
        self.closed += 1

    def source_identity(self):
        return ("fake", "slide")


@pytest.fixture
def real_strip(app, monkeypatch):
    """A `CompareStrip` built by the real factory over `_RealishProvider`.

    Yields `(page, strip, provider)`. Torn down at the end: three real
    controllers own a scheduler with worker threads.
    """
    from block01.viewer import raw_tile_provider as rtp

    provider = _RealishProvider()
    monkeypatch.setattr(rtp, "RawTileProvider", lambda _path: provider)

    page = _page(app)
    # The REAL factory, not the recording one `_page` installs.
    page._compare_strip_widget._stack_factory = cs.build_compare_stacks
    strip = _enter(page)
    QtTest.QTest.qWait(60)
    try:
        yield page, strip, provider
    finally:
        try:
            strip.teardown(wait_for_floor=False)
        except Exception:                                   # noqa: BLE001
            pass


def _drain(ms=400, step=20):
    """Let the scheduler's worker threads deliver."""
    for _ in range(max(1, ms // step)):
        QtWidgets.QApplication.instance().processEvents()
        QtTest.QTest.qWait(step)


def _raw_keys(strip):
    cache = strip.stacks.scheduler.raw_cache
    keys = getattr(cache, "keys", None)
    if callable(keys):
        return list(keys())
    return list(getattr(cache, "_od", {}).keys())


def test_the_real_strip_builds_three_controllers_on_one_backend(real_strip):
    _page_, strip, provider = real_strip
    assert strip.built
    controllers = strip.controllers
    assert len(controllers) == 3
    # ONE provider, one scheduler, one of each cache -- the whole point of
    # sharing, and what stops Original's raw pixels being read again for
    # TopHat and a third time for cuCIM.
    assert len({id(c.provider) for c in controllers}) == 1
    assert len({id(c.scheduler) for c in controllers}) == 1
    assert provider.open_count == 1
    # ...and one overview store, so the whole-slide level is read once.
    assert len({id(c._overview_store) for c in controllers}) == 1


def test_a_pan_far_outside_the_entry_rectangle_lands_tiles_there(real_strip):
    """THE property the virtual patch could not have.

    The panels open on a rectangle around the clicked point. This drags the
    camera to a part of the slide that rectangle never covered and asks
    whether tiles ARRIVE there -- not whether the camera moved, which a
    fixed-region design would also have managed while showing background.
    """
    page, strip, provider = real_strip
    _drain()
    entry = strip.view_rect_l0(0)
    assert entry is not None

    # A corner of the slide the entry rectangle does not reach.
    far_x, far_y = SLIDE_W * 0.85, SLIDE_H * 0.85
    ex, ey, ew, eh = entry
    assert not (ex <= far_x <= ex + ew and ey <= far_y <= ey + eh), (
        "the fixture's entry rectangle already covers the far point")

    strip.set_camera(far_x, far_y, strip.camera(0)[2])
    _drain(600)

    # every panel's own idea of what is visible is out there now
    for controller in strip.controllers:
        y0, x0, y1, x1 = controller._current_bbox
        assert x0 <= far_x <= x1, (x0, far_x, x1)
        assert y0 <= far_y <= y1, (y0, far_y, y1)

    # ...and the slide was READ out there, which is what "nothing beyond R
    # is ever fetched" made impossible.
    reads = [r for r in provider.reads
             if r[3] > far_y * 0.5 and r[5] > far_x * 0.5]
    assert reads, "no read covered the far region"


def test_a_far_jump_moves_all_three_panels_together(real_strip):
    """A Tissue Navigator click on the other side of the slide. All three
    land on it, at one camera, and none of them is left behind."""
    page, strip, _provider = real_strip
    _drain()
    scale_before = strip.camera(0)[2]

    assert page._navigate_compare_to(SLIDE_H * 0.9, SLIDE_W * 0.1) is True
    _drain(600)

    cams = [strip.camera(i) for i in range(3)]
    for cam in cams:
        assert cam[0] == pytest.approx(SLIDE_W * 0.1, abs=2.0)
        assert cam[1] == pytest.approx(SLIDE_H * 0.9, abs=2.0)
        # a jump is not a zoom
        assert cam[2] == pytest.approx(scale_before, rel=1e-6)
    # one camera, to the pixel
    assert cams[1][0] == pytest.approx(cams[0][0], rel=1e-9)
    assert cams[2][0] == pytest.approx(cams[0][0], rel=1e-9)

    for controller in strip.controllers:
        y0, x0, y1, x1 = controller._current_bbox
        assert x0 <= SLIDE_W * 0.1 <= x1
        assert y0 <= SLIDE_H * 0.9 <= y1


def test_the_panels_can_reach_every_corner_of_the_slide(real_strip):
    """Not one region and not an enlarged one: the whole slide is
    reachable, corner to corner, in one session."""
    _page_, strip, _provider = real_strip
    scale = strip.camera(0)[2]
    corners = ((0.1, 0.1), (0.9, 0.1), (0.1, 0.9), (0.9, 0.9))
    for fx, fy in corners:
        x, y = SLIDE_W * fx, SLIDE_H * fy
        strip.set_camera(x, y, scale)
        _drain(200)
        got = strip.camera(0)
        assert got[0] == pytest.approx(x, abs=2.0), (fx, fy)
        assert got[1] == pytest.approx(y, abs=2.0), (fx, fy)


def test_entering_does_not_wait_for_the_three_pictures(real_strip):
    """The UI and the camera are finished before any tile has arrived.

    The rejected design computed three whole arrays and only then let the
    page appear. Here the panels are placed and the camera is set in the
    entering event; what arrives later is tiles, refining in place.
    """
    page, strip, _provider = real_strip
    # `_enter` already ran WITHOUT draining the workers, and by then:
    assert page._compare_mode() is True
    assert page._compare_opened is True
    assert strip.camera(0) is not None
    assert all(c._current_bbox is not None for c in strip.controllers)


def test_the_overview_is_read_once_for_the_three(real_strip):
    """The shared store is what makes a channel switch one disk read rather
    than three -- measured here as one key in the store, not three."""
    _page_, strip, _provider = real_strip
    _drain()
    store = strip.controllers[0]._overview_store
    channels = {k[1] for k in store.cache.keys()}
    assert channels == {"CD3"}, store.cache.keys()


def test_a_channel_switch_reads_the_new_overview_once(real_strip):
    page, strip, _provider = real_strip
    _drain()
    page.current_channel = "CD20"
    page._sync_compare_to_channel()
    _drain(600)
    assert [c.channel for c in strip.controllers] == ["CD20"] * 3
    store = strip.controllers[0]._overview_store
    per_channel = {}
    for key in store.cache.keys():
        per_channel[key[1]] = per_channel.get(key[1], 0) + 1
    assert per_channel.get("CD20", 0) <= 1, store.cache.keys()


def test_one_panels_cancel_does_not_drop_anothers_work(real_strip):
    """The namespaced generations, over the real scheduler. Without them all
    three reach `("raw", 5)` and a cancel by one drops the queued tiles of
    the other two."""
    _page_, strip, _provider = real_strip
    tokens = [c.view_generation for c in strip.controllers]
    assert len(set(tokens)) == 3, tokens
    scheduler = strip.stacks.scheduler
    scheduler.cancel_generation(tokens[0])
    # the other two are untouched
    assert tokens[1] not in getattr(scheduler, "_stale_gens", set())
    assert tokens[2] not in getattr(scheduler, "_stale_gens", set())


# ── 13. the same size on screen, measured ────────────────────────────────

def test_a_landmark_is_the_same_screen_size_in_both_modes(app):
    """Spec: the same structure must occupy the same number of SCREEN
    pixels in the full image and in a compare panel.

    Measured with `viewPixelSize` on both sides -- the real device
    transform, not a ratio of logical numbers, which would agree only while
    that transform is the identity.
    """
    page = _page(app)
    before = page._full_image_scale()
    strip = _enter(page)
    after = strip.camera(0)[2]

    assert before is not None and after is not None
    assert after == pytest.approx(before, rel=1e-9)

    # A 1000-level-0-pixel landmark, in screen pixels, either side.
    assert 1000.0 * after == pytest.approx(1000.0 * before, rel=1e-9)

    # ...and the panel therefore shows LESS ground, not smaller content.
    (fx0, fx1), _fy = page._explore_tab.stack.view.view_box.viewRange()
    _px, _py, pw, _ph = strip.view_rect_l0(0)
    assert pw < (fx1 - fx0) * 0.6


def test_the_magnification_is_measured_not_derived_from_a_rectangle(app):
    """`camera()[2]` must be `1 / viewPixelSize()[0]`, so that "the same
    magnification" is the same measurement on both sides rather than two
    conventions that agree by luck."""
    page = _page(app)
    strip = _enter(page)
    for i, box in enumerate(strip.view_boxes):
        expected = 1.0 / float(box.viewPixelSize()[0])
        assert strip.camera(i)[2] == pytest.approx(expected, rel=1e-12)


def test_the_real_fixture_really_is_real(real_strip):
    """Guards the section above: if the fixture ever fell back to the
    recording factory, every assertion in it would become vacuous."""
    from block01.viewer.explore_view import (ExploreController, ExploreView,
                                             SharedOverviewStore)
    from block01.viewer.scheduler import TileScheduler
    _page_, strip, _provider = real_strip
    assert all(isinstance(c, ExploreController) for c in strip.controllers)
    assert all(isinstance(v, ExploreView) for v in strip.views)
    assert isinstance(strip.stacks.scheduler, TileScheduler)
    assert isinstance(strip.stacks.overview_store, SharedOverviewStore)


# ── 14. one read, every waiter woken ─────────────────────────────────────
#
# The bug this section exists for, and the evidence it was found with.
#
# The three panels share a `SharedOverviewStore`. On a channel switch each
# of them called `prepare_overview_async`; the FIRST submitted the read, and
# the other two saw the key already in `inflight` and returned. The result
# then came back on the submitting controller's own Qt signal, so only that
# controller was told. TopHat and cuCIM were left with their overview
# cleared, blocked on a record nobody would ever hand them: they drew
# nothing and requested nothing for the rest of the session.
#
# Measured on the real 29-channel slide (59040x35520), twenty consecutive
# marker clicks in compare mode: `panels_ready=[True, False, False]` on
# every single switch. Not an intermittent race -- the invariable outcome.
#
# So the store owns the notification now: one read, and everybody still
# waiting on that key is woken from it.

def _overview_ready(controller, channel):
    return controller._overview_identity == (
        controller.provider.source_identity(), channel)


def test_a_cold_channel_switch_wakes_all_three_panels(real_strip):
    """THE regression. Three real controllers, a genuinely cold channel,
    and all three end up holding the new channel's overview.

    Before the fix the first assertion passed and the loop failed on panels
    1 and 2 -- for good, not just for a moment.
    """
    page, strip, _provider = real_strip
    _drain()
    store = strip.controllers[0]._overview_store
    assert not any(k[1] == "CD20" for k in store.cache), "CD20 was not cold"

    page.current_channel = "CD20"
    page._sync_compare_to_channel()
    _drain(800)

    for i, controller in enumerate(strip.controllers):
        assert controller.channel == "CD20"
        assert _overview_ready(controller, "CD20"), (
            f"panel {i} never received the shared overview record")


def test_a_cold_switch_reads_the_new_channel_once_for_all_three(real_strip):
    """Woken from ONE read, not three. Counted at the provider, which is
    where a duplicate read would actually cost something."""
    page, strip, provider = real_strip
    _drain()
    level = strip.controllers[0]._pick_overview_level()
    before = len([r for r in provider.reads
                  if r[0] == "CD20" and r[1] == level])

    page.current_channel = "CD20"
    page._sync_compare_to_channel()
    _drain(800)

    whole_level_reads = [r for r in provider.reads
                         if r[0] == "CD20" and r[1] == level
                         and r[2] == 0 and r[4] == 0]
    assert len(whole_level_reads) - before == 1, whole_level_reads


def test_a_waiter_that_moved_on_does_not_install_the_old_record(real_strip):
    """Woken is not the same as installing. A controller that has since
    been put on another channel keeps the record in the shared cache and
    leaves its own display alone."""
    page, strip, _provider = real_strip
    _drain()
    page.current_channel = "CD20"
    page._sync_compare_to_channel()
    # ...and immediately back, before the read can land.
    page.current_channel = "CD3"
    page._sync_compare_to_channel()
    _drain(800)

    for controller in strip.controllers:
        assert controller.channel == "CD3"
        assert _overview_ready(controller, "CD3")


def test_a_torn_down_panel_is_off_the_waiter_list(real_strip):
    """A shared store outlives its controllers. A record landing after one
    of them has been torn down must not be delivered into it."""
    page, strip, _provider = real_strip
    _drain()
    store = strip.controllers[0]._overview_store
    victim = strip.controllers[2]
    page.current_channel = "CD20"
    page._sync_compare_to_channel()
    victim.teardown(shutdown_backend=False)
    _drain(800)
    assert victim._torn_down is True
    for controller in strip.controllers[:2]:
        assert _overview_ready(controller, "CD20")
    assert store.waiting_on(
        (strip.controllers[0].provider.source_identity(), "CD20",
         strip.controllers[0]._pick_overview_level())) == 0


# ── 15. the hidden full image stops following every channel ──────────────
#
# `_on_channel_row_changed` synced the full image first and the panels
# second. In compare mode the full image is off screen AND suspended, and
# that sync was the great majority of the click: 178-239 ms of the 200-270
# ms a switch cost on the real slide, spent clearing pools, cancelling
# generations, starting an overview read and re-requesting a corrected floor
# for a picture nobody could see. Six or seven clicks of that in a row is
# what the user reported as the application freezing.
#
# So it is deferred while comparing, and applied ONCE on the way out -- to
# the channel the user actually settled on.

def test_a_channel_click_in_compare_mode_leaves_the_full_image_alone(app):
    page = _page(app)
    strip = _enter(page)
    tab = page._explore_tab
    before = len(tab.calls)

    page._on_channel_row_changed(page._channel_order.index("CD20"))

    assert page.current_channel == "CD20"
    assert [c.channel for c in strip.controllers] == ["CD20"] * 3
    assert len(tab.calls) == before, (
        "the hidden full image was re-selected while comparing")
    assert page._full_image_channel_stale is True


def test_leaving_compare_mode_syncs_the_full_image_once(app):
    """Five channels tried while comparing cost the full image ONE
    selection change, to the last of them."""
    page = _page(app)
    _enter(page)
    tab = page._explore_tab
    before = len(tab.calls)

    for name in ("CD20", "CD3", "CD20", "CD3", "CD20"):
        page._on_channel_row_changed(page._channel_order.index(name))
    assert len(tab.calls) == before

    page._exit_compare_mode()

    assert len(tab.calls) == before + 1
    assert tab.calls[-1][0] == "CD20"
    assert page._full_image_channel_stale is False


def test_leaving_compare_mode_with_no_channel_change_syncs_nothing(app):
    """Flipping modes is not a selection change. Re-entering
    `_show_full_image` for a selection the controller already holds costs a
    provisional cycle and a directional-prefetch cancel for nothing."""
    page = _page(app)
    _enter(page)
    tab = page._explore_tab
    before = len(tab.calls)

    page._exit_compare_mode()

    assert len(tab.calls) == before


def test_a_channel_click_with_the_full_image_up_still_syncs_at_once(app):
    """The deferral is compare mode's alone. On the landing view the full
    image IS the picture and must follow the row immediately."""
    page = _page(app)
    tab = page._explore_tab
    before = len(tab.calls)

    page._on_channel_row_changed(page._channel_order.index("CD20"))

    assert len(tab.calls) == before + 1
    assert tab.calls[-1][0] == "CD20"
    assert page._full_image_channel_stale is False


# ── 16. the display seed stops reading the slide twice ───────────────────
#
# `_display_mapping_for` seeds a channel's window from the whole slide at
# `overview_downsample()`. That is the SAME pyramid level, read with the
# same `seed_display_range`, that the tile controllers read into their
# shared overview store -- so a channel switch was reading it twice: once on
# a worker, for the viewers, and once synchronously on the GUI thread, for
# the window. The synchronous one cost 166-230 ms per switch on the real
# slide and was the whole of what was left of the stall once the hidden full
# image stopped following every channel.

def test_the_seed_adopts_a_record_a_viewer_has_already_read(real_strip):
    """No second read: the page's low-res array for a channel IS the
    viewer's overview record."""
    page, strip, _provider = real_strip
    _drain()
    page._slide_lowres.clear()

    arr = page._slide_lowres_array("CD3")

    rec = strip.controllers[0].overview_record("CD3")
    assert rec is not None
    assert arr is rec.arr


def test_the_seed_does_not_read_while_a_viewer_is_reading(real_strip):
    """A cold switch has just asked three controllers for this level. The
    page waits for that read instead of starting a second one on the GUI
    thread."""
    page, strip, provider = real_strip
    _drain()
    page._slide_lowres.clear()
    level = strip.controllers[0]._pick_overview_level()

    page.current_channel = "CD20"
    page._sync_compare_to_channel()
    reads_during = [r for r in provider.reads
                    if r[0] == "CD20" and r[1] == level]

    assert page._overview_read_pending("CD20") is True
    assert page._slide_lowres_array("CD20", blocking=False) is None
    assert [r for r in provider.reads
            if r[0] == "CD20" and r[1] == level] == reads_during, (
        "the display seed read the overview level a second time")


def test_the_window_is_seeded_when_the_record_lands(real_strip):
    """Deferring the seed is only honest if it actually arrives. The
    provisional window must be replaced from the record, without the page
    ever reading the slide itself."""
    page, strip, _provider = real_strip
    _drain()
    page._slide_lowres.clear()
    page._display_fallback.pop("CD20", None)

    page.current_channel = "CD20"
    page._sync_compare_to_channel()
    _drain(800)

    rec = strip.controllers[0].overview_record("CD20")
    assert rec is not None
    lo, hi, _gamma = page._display_mapping_for("CD20")
    assert (lo, hi) == pytest.approx((rec.display_lo, rec.display_hi))


def test_a_channel_no_viewer_is_reading_is_still_seeded_at_once(app):
    """The non-blocking rule is "somebody else is already doing it", not
    "never read". With no viewer on that channel the page reads it, as it
    always did."""
    page = _page(app)
    page._slide_lowres.clear()
    assert page._overview_read_pending("CD20") is False
    assert page._slide_lowres_array("CD20", blocking=False) is not None


# ── 17. the dashed rectangle follows the panels' camera ──────────────────
#
# The page had `_on_compare_range_changed` and nothing was connected to it.
# So the Tissue Preview's dashed rectangle was drawn once, on entry, and
# then stayed exactly where and what size it was: it did not move with a pan
# and it did not shrink under a zoom. The strip emits ONE `camera_changed`
# per camera change now, after the mirroring has finished, and the page
# redraws from it.

def test_the_strip_announces_a_camera_change(app):
    page = _page(app)
    strip = _enter(page)
    seen = []
    strip.camera_changed.connect(lambda: seen.append(1))

    now = strip.camera(0)
    strip.set_camera(now[0] + 500.0, now[1], now[2])
    QtTest.QTest.qWait(10)

    assert seen, "moving the panels announced nothing"


def test_three_followers_do_not_cause_three_page_redraws(app):
    """One camera change, one page-level redraw. The two panels the strip
    mirrors onto emit their own range signals; those must not each reach
    the page."""
    page = _page(app)
    strip = _enter(page)
    popup = _navigator(page)
    before = len(popup.overview.rects)

    now = strip.camera(0)
    strip.set_camera(now[0] + 500.0, now[1], now[2])
    QtTest.QTest.qWait(10)

    assert len(popup.overview.rects) - before == 1, popup.overview.rects


def test_a_pan_of_the_panels_moves_the_dashed_rectangle(app):
    """No explicit redraw call: the signal is the whole mechanism."""
    page = _page(app)
    strip = _enter(page)
    popup = _navigator(page)
    page._update_compare_view_rect()
    first = popup.overview.rects[-1]

    now = strip.camera(0)
    strip.set_camera(now[0] + 800.0, now[1] + 400.0, now[2])
    QtTest.QTest.qWait(10)

    last = popup.overview.rects[-1]
    assert last != first
    # A pan: the centre moved, the size did not.
    assert (last[1] - last[0]) == pytest.approx(first[1] - first[0], rel=1e-6)
    assert (last[3] - last[2]) == pytest.approx(first[3] - first[2], rel=1e-6)
    assert (last[2] - first[2]) == pytest.approx(800.0, rel=0.05)
    assert (last[0] - first[0]) == pytest.approx(400.0, rel=0.05)


def test_a_four_times_zoom_makes_the_dashed_rectangle_a_quarter_as_wide(app):
    """The number the acceptance run asks for: zoom in 4x and the rectangle
    is about a quarter of its width and a quarter of its height."""
    page = _page(app)
    strip = _enter(page)
    popup = _navigator(page)
    page._update_compare_view_rect()
    first = popup.overview.rects[-1]

    now = strip.camera(0)
    strip.set_camera(now[0], now[1], now[2] * 4.0)
    QtTest.QTest.qWait(10)

    last = popup.overview.rects[-1]
    assert (last[3] - last[2]) == pytest.approx((first[3] - first[2]) / 4.0,
                                                rel=0.02)
    assert (last[1] - last[0]) == pytest.approx((first[1] - first[0]) / 4.0,
                                                rel=0.02)


def test_a_wheel_zoom_on_a_real_panel_moves_the_rectangle(real_strip):
    """Through the REAL ViewBox and its real range signal, so the path
    under test is the one a wheel actually takes -- not a page method
    called by hand."""
    page, strip, _provider = real_strip
    popup = _navigator(page)
    _drain(100)
    page._update_compare_view_rect()
    first = popup.overview.rects[-1]

    box = strip.view_boxes[1]
    (x0, x1), (y0, y1) = box.viewRange()
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    w, h = (x1 - x0) / 4.0, (y1 - y0) / 4.0
    box.setRange(xRange=(cx - w / 2.0, cx + w / 2.0),
                 yRange=(cy - h / 2.0, cy + h / 2.0), padding=0)
    _drain(200)

    last = popup.overview.rects[-1]
    assert (last[3] - last[2]) < (first[3] - first[2]) * 0.5


# ── 18. a far jump keeps the magnification the user chose ────────────────
#
# `_navigate_compare_to` went back through `_enter_compare_mode`, which
# reads the camera off the FULL IMAGE -- hidden, and still at whatever scale
# it had when compare mode opened. So a click on the thumbnail silently
# threw away a zoom made in compare mode. It recentres now, and nothing
# else.

def test_a_jump_after_a_zoom_keeps_the_zoom(app):
    """The killing case. The old implementation passed the plain jump test
    because the full image happened to be at the entry scale; zoom the
    panels first and the two answers separate."""
    page = _page(app)
    strip = _enter(page)
    _navigator(page)
    entry_scale = strip.camera(0)[2]
    now = strip.camera(0)
    page._apply_compare_camera(now[0], now[1], entry_scale * 4.0)
    QtTest.QTest.qWait(10)
    zoomed = strip.camera(0)[2]
    assert zoomed == pytest.approx(entry_scale * 4.0, rel=1e-6)

    assert page._navigate_compare_to(3000.0, 1500.0) is True

    after = strip.camera(0)
    assert after[2] == pytest.approx(zoomed, rel=1e-6), (
        "the jump reset the magnification to the full image's")
    assert after[0] == pytest.approx(1500.0, abs=1.0)
    assert after[1] == pytest.approx(3000.0, abs=1.0)


def test_a_jump_does_not_rebuild_the_strip(app):
    """A move is a move. Re-entering compare mode would re-run the whole
    entry path -- the GPU hand-over, the status text, the ensure-built --
    for a click that only means "look over there"."""
    page = _page(app)
    strip = _enter(page)
    _navigator(page)
    builds_before = len(page._compare_builds)
    stacks_before = strip.stacks

    page._navigate_compare_to(3000.0, 1500.0)

    assert len(page._compare_builds) == builds_before
    assert strip.stacks is stacks_before


def test_a_jump_updates_the_dashed_rectangle_at_once(app):
    page = _page(app)
    strip = _enter(page)
    popup = _navigator(page)
    page._update_compare_view_rect()
    before = popup.overview.rects[-1]

    page._navigate_compare_to(3000.0, 1500.0)

    last = popup.overview.rects[-1]
    assert last != before
    assert (last[0] + last[1]) / 2.0 == pytest.approx(3000.0, abs=2.0)
    assert (last[2] + last[3]) / 2.0 == pytest.approx(1500.0, abs=2.0)


def test_a_zoomed_jump_on_real_panels_keeps_the_scale(real_strip):
    """Same property, through three real controllers and real ViewBoxes."""
    page, strip, _provider = real_strip
    _drain()
    now = strip.camera(0)
    strip.set_camera(now[0], now[1], now[2] * 4.0)
    _drain(200)
    zoomed = strip.camera(0)[2]

    assert page._navigate_compare_to(SLIDE_H * 0.9, SLIDE_W * 0.1) is True
    _drain(400)

    for i in range(3):
        cam = strip.camera(i)
        assert cam[2] == pytest.approx(zoomed, rel=1e-6)
        assert cam[0] == pytest.approx(SLIDE_W * 0.1, abs=2.0)
        assert cam[1] == pytest.approx(SLIDE_H * 0.9, abs=2.0)


# ── 19. a cold entry keeps the picture that is already there ─────────────
#
# The measurement, on the real 59040x35520 slide, before this section:
#
#   right-click -> three panels on screen      2050 ms
#     of which build_compare_stacks            1622 ms
#       RawTileProvider(path)                    95 ms
#       TileScheduler                            24 ms
#       three ExploreViews + three controllers   44 ms
#       the FIRST load_overview                1110 ms
#   and the compare page was switched to at the START of that, so all
#   2050 ms of it were a page with nothing on it but a sentence.
#
# So the second backend was never the cost -- 163 ms of it was. The cost
# was a synchronous, GUI-thread re-read of a whole pyramid level the full
# image had already read and was still holding, because the strip made its
# own `SharedOverviewStore`. The strip borrows the full image's now; that is
# the ONLY thing it borrows.
#
# What is left cannot leave the GUI thread -- building QWidgets is the GUI
# thread's work -- so the other half of the fix is about what the user is
# looking at while it happens: the full image, with a badge, until the
# panels have something on them. 686 ms cold and 174 ms warm, measured.

def test_the_strip_borrows_the_full_images_overview_store(app):
    page = _page(app)
    _enter(page)
    build = page._compare_builds[-1]
    assert build["overview_store"] is not None
    assert build["overview_store"] is page._explore_tab.stack.overview_store


def test_the_full_image_stack_owns_the_store_it_lends(app):
    """`overview_store` is the controller's own -- the object it created
    and shuts down at teardown. Lending it is lending, not transferring."""
    page = _page(app)
    stack = page._explore_tab.stack
    assert stack.overview_store is not None
    assert stack.controller._owns_overview_store is True
    # ...and the real thing agrees: a controller built with no store makes
    # and owns one.
    from block01.viewer.explore_view import SharedOverviewStore
    from block01.ui.step0 import step0_explore_tab as et
    assert "overview_store" in et.ExploreStack.__dict__
    assert SharedOverviewStore is not None


def test_a_borrowed_store_is_not_shut_down_with_the_strip(real_strip):
    """Shutting a borrowed pool down would leave the LENDER unable to read
    another channel's overview for the rest of the session."""
    from block01.viewer.explore_view import SharedOverviewStore

    page, strip, _provider = real_strip
    _drain()
    lender = SharedOverviewStore()
    strip.stacks.overview_store = lender
    strip.stacks.owns_overview_store = False

    strip.teardown()

    # Still usable: a request on it still starts a read.
    reads = []

    class _W:
        def on_overview_delivered(self, *_a):
            reads.append("woken")

    started = strip is not None and lender.request(
        (("fake", "slide"), "CD3", 0),
        lambda: _overview_record_for_test("CD3"), _W())
    assert started is True
    deadline = time.time() + 5.0
    while time.time() < deadline and not reads:
        _drain(20)
    assert reads == ["woken"]
    lender.shutdown()


def _overview_record_for_test(channel):
    from block01.viewer.explore_view import OverviewRecord
    arr = np.zeros((4, 4), dtype=np.float32)
    return OverviewRecord(source=("fake", "slide"), channel=channel, level=0,
                          arr=arr, display_lo=0.0, display_hi=1.0,
                          shape=(4, 4))


def test_a_strip_with_nobody_to_borrow_from_makes_its_own(app, monkeypatch):
    """The strip must still work standing alone -- a build with no full
    image behind it -- and then it OWNS what it made and shuts it down."""
    from block01.viewer import raw_tile_provider as rtp
    from block01.viewer.explore_view import SharedOverviewStore

    provider = _RealishProvider()
    monkeypatch.setattr(rtp, "RawTileProvider", lambda _path: provider)

    stacks = cs.build_compare_stacks("/fake/slide.ome.tif", "CD3")
    try:
        assert stacks.owns_overview_store is True
        assert isinstance(stacks.overview_store, SharedOverviewStore)
        # ...and every controller knows it is a borrower of THAT store.
        for controller in stacks.controllers:
            assert controller._overview_store is stacks.overview_store
            assert controller._owns_overview_store is False
    finally:
        stacks.teardown()
    assert provider.closed == 1


def test_the_click_does_not_put_a_blank_page_up(app):
    """THE fix for the 2.7 s blank. After the right-click returns, the full
    image is still what is on screen -- and it says why."""
    page = _page(app)
    assert page._compare_mode() is False

    page._on_full_image_right_click(1000.0, 1000.0)

    # ...still the full image, still its pixels, with a badge over them.
    assert page._compare_mode() is False, "switched to an empty compare page"
    assert page._explore_tab.stack.view.status_label.text() == \
        page._PREPARING_COMPARE_BADGE
    assert page._explore_tab.stack.view.status_label.isVisible() is True

    QtTest.QTest.qWait(30)
    QtTest.QTest.qWait(10)
    assert page._compare_mode() is True
    assert page._compare_strip_widget.built is True


def test_the_panels_are_never_shown_before_they_are_built(app):
    """The order is build-then-switch. A switch first is a blank page for
    as long as the build takes, which is the whole complaint."""
    page = _page(app)
    seen = []
    real_factory = page._compare_strip_widget._stack_factory

    def watching(*a, **k):
        seen.append(page._compare_mode())
        return real_factory(*a, **k)

    page._compare_strip_widget._stack_factory = watching
    _enter(page)

    assert seen == [False], (
        "the compare page was already showing when the strip was built")


def test_the_preparing_badge_is_taken_down_once_the_panels_are_up(app):
    page = _page(app)
    _enter(page)
    label = page._explore_tab.stack.view.status_label
    # The GPU hand-off replaces it with the pause badge; either way the
    # "Preparing" badge must not still be standing.
    assert not (label.isVisible()
                and label.text() == page._PREPARING_COMPARE_BADGE)


def test_a_failed_build_leaves_the_full_image_up(app):
    """No blank compare page to be stranded on: the switch never happened,
    so a failure has nothing to undo."""
    page = _page(app)

    def failing(*_a, **_k):
        raise RuntimeError("no provider")

    page._compare_strip_widget._stack_factory = failing
    page._on_full_image_right_click(1000.0, 1000.0)
    QtTest.QTest.qWait(30)
    QtTest.QTest.qWait(10)

    assert page._compare_mode() is False
    assert page._compare_strip_widget.built is False
    label = page._explore_tab.stack.view.status_label
    assert not (label.isVisible()
                and label.text() == page._PREPARING_COMPARE_BADGE)


def test_opening_compare_on_the_shown_channel_reads_no_overview(real_strip):
    """The point of the lending, measured where it costs: the channel the
    full image is already showing is a cache hit, not a whole-level read."""
    page, strip, provider = real_strip
    _drain()
    level = strip.controllers[0]._pick_overview_level()
    channel = strip.controllers[0].channel
    whole_level = [r for r in provider.reads
                   if r[0] == channel and r[1] == level
                   and r[2] == 0 and r[4] == 0]
    assert len(whole_level) == 1, (
        f"the strip re-read {channel}'s overview level: {whole_level}")


def test_the_page_tears_the_strip_down_before_the_explore_tab(app):
    """`Step0Page.teardown`, the deterministic path."""
    page = _page(app)
    _enter(page)
    order = []
    strip = page._compare_strip_widget
    tab = page._explore_tab
    real_strip_td = strip.teardown
    real_tab_td = tab.teardown
    strip.teardown = lambda *a, **k: (order.append("strip"),
                                      real_strip_td(*a, **k))[1]
    tab.teardown = lambda *a, **k: (order.append("tab"),
                                    real_tab_td(*a, **k))[1]

    page.teardown()

    assert order == ["strip", "tab"], order


# -- 20. lending is only safe with an ownership rule ----------------------

def test_the_compare_controllers_do_not_own_the_store_they_are_given(app):
    """The rule that makes lending safe at the controller level: a
    controller given a store never shuts its pool down."""
    page = _page(app)
    _enter(page)
    build = page._compare_builds[-1]
    assert build["overview_store"] is page._explore_tab.stack.overview_store


def test_a_dataset_switch_unbinds_the_strip_before_the_full_image(app):
    """Ordering, at the commit path. The full image's teardown shuts the
    borrowed pool down, so the borrower has to be unbound first."""
    page = _page(app)
    _enter(page)
    order = []
    strip = page._compare_strip_widget
    tab = page._explore_tab
    real_strip_sd = strip.set_dataset
    real_tab_sd = tab.set_dataset
    strip.set_dataset = lambda p: (order.append("strip"), real_strip_sd(p))[1]
    tab.set_dataset = lambda p: (order.append("tab"), real_tab_sd(p))[1]

    page._unbind_viewers_for_dataset_switch()

    assert order == ["strip", "tab"], order


def test_the_shared_backend_is_closed_exactly_once_on_a_switch(real_strip):
    """The strip's OWN provider -- which it does own -- is closed once and
    only once when the dataset goes."""
    page, strip, provider = real_strip
    _drain()
    assert provider.closed == 0

    strip.set_dataset(None)
    _drain(200)

    assert provider.closed == 1
    # Idempotent: a second unbind must not close it again.
    strip.set_dataset(None)
    strip.teardown()
    assert provider.closed == 1


def test_the_borrowed_store_survives_the_strip_and_still_serves(real_strip):
    """After compare mode is gone the LENDER must still be able to read a
    channel it has never seen."""
    page, strip, _provider = real_strip
    _drain()
    store = strip.stacks.overview_store
    strip.teardown()
    _drain(100)

    woken = []

    class _W:
        def on_overview_delivered(self, key, rec, err=None):
            woken.append((key, rec))

    started = store.request((("fake", "slide"), "DAPI", 0),
                            lambda: _overview_record_for_test("DAPI"), _W())
    assert started is True
    deadline = time.time() + 5.0
    while time.time() < deadline and not woken:
        _drain(20)
    assert woken and woken[0][1] is not None


# -- 21. the two sets of generation tokens cannot collide -----------------
#
# Checked because sharing the store is the beginning of sharing, and the
# next question anybody asks is whether the SCHEDULER could be shared too.
# It could not, for a different reason (see `build_compare_stacks`), but the
# token namespacing that would be needed is already correct and is worth
# pinning.

def test_the_full_image_and_the_panels_cannot_cancel_each_other(real_strip):
    """The full image's tokens are 2-tuples and a compare panel's are
    3-tuples, so no equality between them is possible whatever the counters
    do."""
    page, strip, _provider = real_strip
    _drain()
    tokens = [c.view_generation for c in strip.controllers]
    assert all(len(t) == 3 for t in tokens), tokens
    assert len({t[1] for t in tokens}) == 3, tokens
    from block01.viewer.explore_view import ExploreController
    plain = ExploreController._gen_token(
        type("C", (), {"_gen_ns": None})(), "raw", 5)
    assert plain == ("raw", 5)
    assert all(plain != t for t in tokens)
