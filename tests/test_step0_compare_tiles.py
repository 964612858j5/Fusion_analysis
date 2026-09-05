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
        # The production hand-off the page performs on the full image's
        # side. Recorded rather than acted on: what the compare tests care
        # about is what the page does to the STRIP around it.
        self.released = False
        self.releases = []
        self.resumes = 0

    def show_source(self, channel, method, params=(), **_kw):
        self.calls.append((channel, method, tuple(params)))
        return True

    def set_dataset(self, _p):
        pass

    def release_for_production(self, reason):
        self.released = True
        self.releases.append(reason)

    def resume_from_production(self):
        self.released = False
        self.resumes += 1

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
        # Tile reads are kept APART from `reads`, which the tests above use
        # to count whole-LEVEL reads. On this 4096-square fake slide the
        # coarsest level is exactly one 512 tile, so a tile read of (0, 0)
        # there is indistinguishable from a whole-level read by its
        # coordinates alone -- and counting one as the other made "the
        # overview was read once" fail for a strip that had read it once.
        self.tile_reads = []

    def close(self):
        self.closed += 1

    def source_identity(self):
        return ("fake", "slide")

    def level_downsample_yx(self, level):
        return (float(1 << int(level)), float(1 << int(level)))

    def read_tile(self, channel, tile):
        """One tile, in the source's own dtype -- the entry point the
        CORRECTED path uses (`Assembler.assemble`).

        Without it every corrected tile in this fixture failed with an
        AttributeError on the compute worker, which the scheduler reports
        as an error rather than raising: the strip built, drew its raw
        layers and looked healthy while the corrected cache stayed empty
        for the whole suite. Anything that asks what the corrected cache
        holds needs this to exist.
        """
        h, w = self.level_shape(tile.level)
        ts = tile.grid.tile_size
        cy0, cx0 = tile.ty * ts, tile.tx * ts
        cy1, cx1 = min(cy0 + ts, h), min(cx0 + ts, w)
        self.tile_reads.append((channel, tile.level, cy0, cy1, cx0, cx1))
        ys = np.arange(cy0, cy1, dtype=np.float32)[:, None]
        xs = np.arange(cx0, cx1, dtype=np.float32)[None, :]
        base = 1000.0 if channel == "DAPI" else 0.0
        return (ys * 10000.0 + xs + base), 0.0


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


def _cold(strip, channel):
    """Make `channel` genuinely cold again, and keep it that way.

    The strip prepares the channels NEXT to the one on screen in the
    background (`CompareStrip.start_hot`), and this fixture's slide has
    three channels of which one is the nucleus -- so "the neighbour" is
    exactly the channel the cold-switch tests below switch to. Left alone,
    those tests would still pass while testing nothing: the record they
    expect to see read on the switch would already be resident.

    The cold path has not gone anywhere -- any channel HOT has not reached
    yet, and every channel at all before HOT has settled, still takes it --
    and it is what these tests are for. So they turn the preparation off
    and drop what it warmed, rather than being weakened to accept either
    outcome.
    """
    strip.stop_hot()
    store = strip.controllers[0]._overview_store
    for key in [k for k in store.cache if k[1] == channel]:
        del store.cache[key]


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
    than three -- measured here as ONE record per channel, not three.

    Per channel, and not "CD3 is the only channel in the store": the strip
    also prepares its neighbours in the background, so another channel
    being resident is the prefetch working. What would be the bug is the
    same channel appearing three times, once per panel.
    """
    _page_, strip, _provider = real_strip
    _drain()
    store = strip.controllers[0]._overview_store
    per_channel = {}
    for key in store.cache.keys():
        per_channel[key[1]] = per_channel.get(key[1], 0) + 1
    assert per_channel.get("CD3") == 1, store.cache.keys()
    assert all(n == 1 for n in per_channel.values()), store.cache.keys()


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

    The switch is now STAGED (`CompareStrip.set_channel`), so a cold target
    reaches the panels after its preparation rather than during it -- which
    is why this drains until the publication instead of a fixed 800 ms. The
    thing under test is unchanged: all three end up holding the new
    channel's overview, and none of them is left behind.
    """
    page, strip, _provider = real_strip
    _drain()
    _cold(strip, "CD20")
    store = strip.controllers[0]._overview_store
    assert not any(k[1] == "CD20" for k in store.cache), "CD20 was not cold"

    page.current_channel = "CD20"
    page._sync_compare_to_channel()
    for _ in range(40):
        if strip.displayed_channel == "CD20":
            break
        _drain(100)
    _drain(200)

    for i, controller in enumerate(strip.controllers):
        assert controller.channel == "CD20"
        assert _overview_ready(controller, "CD20"), (
            f"panel {i} never received the shared overview record")


def test_a_cold_switch_reads_the_new_channel_once_for_all_three(real_strip):
    """Woken from ONE read, not three. Counted at the provider, which is
    where a duplicate read would actually cost something."""
    page, strip, provider = real_strip
    _drain()
    _cold(strip, "CD20")
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
    _cold(strip, "CD20")
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


# ── 22. the metrics are coalesced behind a quiet period ──────────────────
#
# A row click used to measure the three panels synchronously, inside the
# handler. Measured on the real 29-channel slide with every tile already
# resident: `_update_compare_metrics` cost 108-290 ms per click, and ten
# consecutive marker clicks spent ~1.4 s inside the handlers with the event
# loop never entered -- the panels had switched channel and the window was
# frozen. Nine of those ten measurements were painted and replaced before
# anybody could read them.
#
# So the click switches the image and PLANS the measurement; one page-level
# single-shot timer, restarted by every change, runs the last one. What this
# section pins is the product statement, driven through the real entry
# points: the image switch is accepted immediately, and after the quiet
# period exactly one measurement has run, for the channel the user stopped
# on.


class _TenChannelLoader(_LowresLoader):
    """DAPI plus ten markers -- enough to click down a real marker list."""

    _CHANNELS = ["DAPI"] + [f"M{i}" for i in range(1, 11)]


def _page10(app):
    page = sp.Step0Page()
    page.loader = _TenChannelLoader()
    page.ome_path = "/fake/slide.ome.tif"
    page.patches = []
    page.nucleus_channel = "DAPI"
    page._rebuild_channel_list()
    page.current_channel = "M1"
    page._explore_tab = _Tab(_Stack())
    page._compare_builds = []
    page._compare_strip_widget._stack_factory = _fake_compare_factory(
        page._compare_builds)
    page.resize(1200, 800)
    page.show()
    QtTest.QTest.qWait(30)
    return page


def _count_metrics(page):
    """Record every `_update_compare_metrics` that actually runs, with the
    channel it ran for."""
    runs = []
    real = page._update_compare_metrics

    def counted():
        strip = page._compare_strip_widget
        runs.append((page.current_channel,
                     tuple(c.channel for c in strip.controllers)))
        return real()

    page._update_compare_metrics = counted
    return runs


def _click(page, channel):
    page._on_channel_row_changed(page._channel_order.index(channel))


def _quiet(ms=None):
    """Let the quiet timer fire."""
    QtTest.QTest.qWait(sp.Step0Page._COMPARE_METRICS_QUIET_MS * 2 + 60
                       if ms is None else ms)


MARKERS = [f"M{i}" for i in range(1, 11)]


def test_ten_quick_channel_clicks_measure_once_for_the_last_one(app):
    """THE product statement. Ten clicks: ten image switches, ONE
    measurement, and it belongs to the tenth channel."""
    page = _page10(app)
    strip = _enter(page)
    runs = _count_metrics(page)
    before = len(strip.controllers[0].selections)

    for channel in MARKERS:
        _click(page, channel)

    # the image side is finished already, before any waiting at all
    assert page.current_channel == "M10"
    assert [c.channel for c in strip.controllers] == ["M10"] * 3
    assert len(strip.controllers[0].selections) - before == len(MARKERS)
    # ...and nothing has been measured yet
    assert runs == []

    _quiet()
    assert len(runs) == 1, runs
    assert runs[0] == ("M10", ("M10", "M10", "M10"))


def test_a_channel_click_does_not_measure_inside_the_handler(app):
    """The handler itself must not reach the measurement -- not once, not
    for the first click."""
    page = _page10(app)
    _enter(page)
    runs = _count_metrics(page)
    _click(page, "M2")
    assert runs == []


def test_the_metrics_go_to_a_dash_the_moment_the_channel_changes(app):
    """The numbers on screen belong to the previous channel from the
    instant the panels leave it, so they come down with the switch rather
    than standing until the new ones are ready."""
    page = _page10(app)
    _enter(page)
    page._metrics_tophat.setText("TopHat     → SNR: 9.99  BG-CV: 0.01")
    _click(page, "M2")
    assert page._metrics_tophat.text() == "TopHat → —"
    assert page._compare_metrics_pending() is True


def test_a_single_click_left_alone_still_ends_up_measured(app):
    """Coalescing is not dropping: click once, wait, and the numbers are
    there for that channel."""
    page = _page10(app)
    _enter(page)
    runs = _count_metrics(page)
    _click(page, "M4")
    _quiet()
    assert len(runs) == 1
    assert runs[0][0] == "M4"
    assert page._compare_metrics_pending() is False


def test_another_click_inside_the_quiet_period_voids_the_first_plan(app):
    """LATEST-WINS. The plan made by the first click is not merely
    superseded in time -- it must not be able to write M2's numbers under
    M5's labels."""
    page = _page10(app)
    _enter(page)
    runs = _count_metrics(page)
    _click(page, "M2")
    first = page._compare_metrics_plan
    QtTest.QTest.qWait(20)
    _click(page, "M5")
    assert page._compare_metrics_plan != first
    _quiet()
    assert len(runs) == 1, runs
    assert runs[0] == ("M5", ("M5", "M5", "M5"))


def test_the_quiet_period_starts_again_with_every_click(app):
    """A restart, not a first-one-wins deadline.

    Clicks spaced a little under the quiet period are the ordinary case: a
    user reading down a marker list clicks faster than they think. A timer
    that only started when idle would fire in the MIDDLE of that stream --
    measuring a channel the user has already left, and paying the 100-290 ms
    for it inside the run they are still making. Restarting means the one
    measurement happens once the clicking has actually stopped.
    """
    page = _page10(app)
    _enter(page)
    runs = _count_metrics(page)
    gap = int(sp.Step0Page._COMPARE_METRICS_QUIET_MS * 0.7)
    for channel in MARKERS[:5]:
        _click(page, channel)
        QtTest.QTest.qWait(gap)
    assert runs == [], "measured in the middle of the click stream"
    _quiet()
    assert len(runs) == 1, runs
    assert runs[0][0] == "M5"


def test_a_stale_plan_is_dropped_rather_than_measured(app):
    """The guard itself, driven directly: a plan whose channel no longer
    matches the panels is thrown away when the timer fires."""
    page = _page10(app)
    strip = _enter(page)
    runs = _count_metrics(page)
    _click(page, "M2")
    # the panels move on without going through the scheduling entry --
    # whatever moved them, the pending plan is now about something else
    strip.set_channel("M7", params_for=page._compare_params_for)
    page._on_compare_metrics_quiet()
    assert runs == []


def test_a_dataset_switch_inside_the_quiet_period_lands_nothing(app):
    """The dataset generation is in the plan. A measurement planned for the
    previous dataset must not be written into the new one's labels."""
    page = _page10(app)
    _enter(page)
    runs = _count_metrics(page)
    _click(page, "M3")
    assert page._compare_metrics_pending() is True
    page._dataset_gen += 1          # what a COMMITTED dataset switch does
    _quiet()
    assert runs == []


def test_leaving_compare_inside_the_quiet_period_measures_nothing(app):
    """The panels are not on screen any more, so there is nothing to
    measure and the hidden page's labels are left alone."""
    page = _page10(app)
    _enter(page)
    runs = _count_metrics(page)
    _click(page, "M3")
    page._exit_compare_mode()
    assert page._compare_mode() is False
    _quiet()
    assert runs == []


def test_a_run_of_parameter_edits_measures_the_last_one_once(app):
    """The parameter path shares the entry, so a dragged spinner coalesces
    exactly as a run of clicks does -- and there is no second timer that
    could be holding a different answer."""
    page = _page10(app)
    strip = _enter(page)
    runs = _count_metrics(page)
    for radius in (11, 13, 15, 17, 19):
        page._channel_params.setdefault(page.current_channel, {})
        page._channel_params[page.current_channel]["tophat_radius"] = radius
        page._sync_compare_params()
    assert runs == []
    _quiet()
    assert len(runs) == 1, runs
    assert strip.controllers[1].params == (19,)


def test_a_parameter_edit_then_a_channel_click_share_one_pending_state(app):
    """One timer, one plan. A parameter edit followed by a row click inside
    the quiet period leaves ONE measurement, of the click."""
    page = _page10(app)
    _enter(page)
    runs = _count_metrics(page)
    page._channel_params.setdefault("M1", {})["tophat_radius"] = 21
    page._sync_compare_params()
    QtTest.QTest.qWait(20)
    _click(page, "M6")
    _quiet()
    assert len(runs) == 1, runs
    assert runs[0][0] == "M6"


def test_a_panel_with_nothing_cached_still_shows_only_a_dash(app):
    """The quiet period does not license a number computed from a hole: an
    incomplete cache is still a dash after the timer has run."""
    page = _page10(app)
    _enter(page)
    page._snapshot_from_caches = lambda *_a, **_k: {}
    _click(page, "M2")
    _quiet()
    assert page._metrics_tophat.text() == "TopHat → —"
    assert page._metrics_original.text() == "Original → —"
    assert page._metrics_cucim.text() == "cucim → —"


def test_the_coalescing_changes_nothing_the_panels_show(app):
    """Ten clicks still move the panels the same way: same camera, same
    three methods, same colour per click, same order of selections. The
    measurement is the only thing that was taken out of the handler."""
    page = _page10(app)
    strip = _enter(page)
    camera_before = [strip.camera(i) for i in range(3)]
    tints_before = [len(c.tints) for c in strip.controllers]
    _count_metrics(page)

    for channel in MARKERS:
        _click(page, channel)

    assert [strip.camera(i) for i in range(3)] == camera_before
    assert [c.method for c in strip.controllers] == [None, "tophat", "cucim"]
    for controller, before in zip(strip.controllers, tints_before):
        assert len(controller.tints) > before
        assert controller.tints[-1] == page._full_image_tint()
        assert [s[0] for s in controller.selections[-len(MARKERS):]] == MARKERS
    _quiet()
    assert [strip.camera(i) for i in range(3)] == camera_before


def test_the_quiet_timer_is_a_child_of_the_page(app):
    """Owned by Qt, not by the garbage collector, and single-shot -- which
    is what makes `start()` a restart rather than a second measurement."""
    page = _page10(app)
    timer = page._compare_metrics_timer
    assert timer.parent() is page
    assert timer.isSingleShot() is True



# ── 20. neighbour preparation (HOT) over the real backend ────────────────
#
# The user's complaint: "switching channels in Compare, you plainly watch
# TopHat and cuCIM load, one after the other, every time -- including
# switching back to a channel you were just on, at the same place and with
# the same parameters." The design answer had existed for a while and was
# connected to nothing: `MultiChannelPrefetchController`, the HOT
# neighbourhood (i-1, i+1, i-2, i+2), was a viewer-level component with no
# caller in Step0 at all.
#
# It is mounted here, ONCE, on the strip: one coordinator over the strip's
# own scheduler, corrected cache and overview store, with the TopHat panel
# as the host that reports the camera and the selection. What it produces
# is CACHE -- the corrected tiles for both methods and the overview record
# a switch needs. Nothing about how a panel draws changes.
#
# Measured on the real 59040x35520 slide at level 0 over a 9-tile viewport
# (the table is in the commit message): an unprepared neighbour cost 36
# corrected computes, ~550 ms to its first corrected pixel and ~810 ms to a
# complete precise viewport. A prepared one is a cache read.


def _hot_settle(strip, ms=700):
    """Let HOT confirm its settle and spend its queue."""
    _drain(ms)
    return strip.hot


def _corrected_keys(strip):
    cache = strip.stacks.scheduler.corrected_cache
    with cache._lock:
        return list(cache._store.keys())


def test_the_strip_mounts_exactly_one_hot_coordinator(real_strip):
    """One per strip, not one per panel: the three panels are one camera
    and one channel, so "prepare the neighbours" is one question with one
    answer, and three coordinators would ask it three times and spend three
    times the budget racing each other for the same scheduler."""
    _page_, strip, _provider = real_strip
    from block01.viewer.multichannel_prefetch import (
        MultiChannelPrefetchController)

    hot = strip.hot
    assert isinstance(hot, MultiChannelPrefetchController)
    # ...on the strip's OWN backend, and hosted by the TopHat panel.
    assert hot.scheduler is strip.stacks.scheduler
    assert hot.grid is strip.stacks.grid
    assert hot.controller is strip.controllers[1]
    assert strip.controllers[1].method == "tophat"


def test_hot_is_not_handed_the_full_images_scheduler(real_strip):
    """The ownership boundary `build_compare_stacks` documents is not
    quietly crossed by the prefetch: a shared scheduler would make
    `suspend_for_production`'s wait-until-idle mean "wait for the other
    mode too", which is a GUI-thread wait on somebody else's work."""
    page, strip, _provider = real_strip
    full_stack = page._explore_tab.stack
    assert strip.hot.scheduler is not getattr(full_stack, "scheduler", None)


def test_the_neighbourhood_is_the_switchable_channels_in_row_order(
        real_strip):
    """Row order, because that is what "the channel above this one" means,
    and without the nucleus row -- selecting it never moves the panels, so
    a slot spent preparing it is a slot the user can never collect."""
    page, strip, _provider = real_strip
    assert page.nucleus_channel == "DAPI"
    assert [s.channel for s in strip.hot.specs] == ["CD3", "CD20"]


def test_the_specs_carry_the_providers_effective_parameters(real_strip):
    """From the preview source provider's `effective_*`, which is the same
    single answer the panels themselves are rendered with -- never the
    page's own last-known fields, which would be free to drift."""
    page, strip, _provider = real_strip
    provider = page.preview_source_provider
    assert provider is not None
    for spec in strip.hot.specs:
        correction = provider.describe(spec.channel)["correction"]
        assert spec.tophat_radius == correction["effective_tophat_radius"]
        assert spec.cucim_sigma == correction["effective_cucim_sigma"]


def test_hot_prepares_the_neighbour_for_both_methods_and_its_overview(
        real_strip):
    """THE product statement, measured at the cache. After settling on CD3
    the neighbour CD20 is switch-ready: its overview record is resident and
    its visible corrected tiles are cached for TopHat AND cuCIM.

    MUTATION this catches: never mounting HOT, or mounting it and leaving
    it stopped, leaves CD20 with neither.
    """
    _page_, strip, _provider = real_strip
    hot = _hot_settle(strip)
    snapshot = strip.controllers[1].snapshot()

    assert hot.is_channel_ready("CD20", snapshot) is True
    assert hot.stats["hot_tiles_completed"] > 0
    assert strip.controllers[0].has_overview_record("CD20") is True

    methods = {k.method for k in _corrected_keys(strip)
               if k.channel == "CD20"}
    assert methods == {"tophat", "cucim"}


def test_hot_never_paints_the_neighbour_into_a_panel(real_strip):
    """Cache-only: preparing CD20 must not make any panel show CD20, and
    must not disturb the overview the three are displaying."""
    _page_, strip, _provider = real_strip
    _hot_settle(strip)
    assert [c.channel for c in strip.controllers] == ["CD3"] * 3
    for controller in strip.controllers:
        assert controller._overview_identity[1] == "CD3"


def test_switching_to_a_prepared_neighbour_finds_every_tile_cached(
        real_strip):
    """The point of the preparation, counted where a miss would cost
    something: every level-0 visible tile of the two corrected panels was
    already in the cache before the switch was made."""
    page, strip, _provider = real_strip
    _hot_settle(strip)
    prepared = set(_corrected_keys(strip))

    page.current_channel = "CD20"
    page._sync_compare_to_channel()
    _drain(400)

    assert [c.channel for c in strip.controllers] == ["CD20"] * 3
    checked = 0
    for controller in strip.controllers[1:]:
        for tx, ty in controller._visible_tiles:
            key = controller._make_correction_key(tx, ty)
            assert key in prepared, key
            checked += 1
    assert checked > 0


def test_the_second_visit_to_a_channel_recomputes_no_corrected_tile(
        real_strip):
    """CD3 -> CD20 -> CD3 at one viewport and one set of parameters. The
    second CD3 must not compute a corrected tile that is already cached --
    that round trip is exactly what the user reported feeling recomputed."""
    page, strip, _provider = real_strip
    _hot_settle(strip)
    computes = []
    real_compute = strip.stacks.compute.compute

    def counting(key):
        computes.append(key)
        return real_compute(key)

    strip.stacks.compute.compute = counting
    try:
        page.current_channel = "CD20"
        page._sync_compare_to_channel()
        _drain(500)
        page.current_channel = "CD3"
        page._sync_compare_to_channel()
        _drain(500)
    finally:
        strip.stacks.compute.compute = real_compute

    assert [c.channel for c in strip.controllers] == ["CD3"] * 3
    level = strip.controllers[1].level
    back = [k for k in computes
            if k.channel == "CD3" and k.tile.level == level]
    assert back == [], back


def test_a_parameter_change_re_plans_hot_under_the_new_numbers(real_strip):
    """A retune moves no camera, so nothing else would ever tell HOT its
    plan had gone stale. The new numbers must reach it, and the results
    cached under the OLD parameter must never be reported as ready.

    MUTATION this catches: freezing the specs at construction (the state
    before this change) leaves `is_channel_ready` answering about the old
    parameters for the rest of the session.
    """
    page, strip, _provider = real_strip
    _hot_settle(strip)
    host = strip.controllers[1]
    assert strip.hot.is_channel_ready("CD20", host.snapshot()) is True
    old = strip.hot._spec_by_channel["CD20"]

    page._channel_params["CD20"] = {
        "tophat_radius": int(old.tophat_radius) + 7,
        "cucim_sigma": int(old.cucim_sigma)}
    page._sync_compare_params()

    assert (strip.hot._spec_by_channel["CD20"].tophat_radius
            == old.tophat_radius + 7)
    # The parameter is part of the CorrectionKey, so the old results are
    # simply never asked for again -- they are not served as new ones.
    assert strip.hot.is_channel_ready("CD20", host.snapshot()) is False

    _hot_settle(strip, 900)
    assert strip.hot.is_channel_ready("CD20", host.snapshot()) is True


def test_an_unchanged_parameter_edit_does_not_churn_hots_generation(
        real_strip):
    """`refresh_hot` runs on every selection change; it has to be free when
    nothing HOT cares about actually moved."""
    _page_, strip, _provider = real_strip
    _hot_settle(strip)
    generation = strip.hot._hot_generation
    strip.refresh_hot()
    assert strip.hot._hot_generation == generation


def test_leaving_compare_stops_hot(real_strip):
    """Off screen, the prefetch goes with it: nothing may be issued for a
    mode nobody is looking at, and the strip's suspend waits the scheduler
    idle -- which a producer still refilling would keep from ever
    happening.

    MUTATION this catches: not stopping HOT on the way out leaves the
    coordinator connected to the host, with a live queue.
    """
    page, strip, _provider = real_strip
    _hot_settle(strip)
    hot = strip.hot
    requested = hot.stats["hot_tiles_requested"]

    page._exit_compare_mode()
    _drain(400)

    assert strip.hot is None
    assert hot._stopped is True
    assert len(hot._tile_queue) == 0
    assert hot.stats["hot_tiles_requested"] == requested


def test_coming_back_to_compare_plans_again(real_strip):
    """Stopped is not disabled. Re-entering plans from the channel, the
    viewport and the parameters as they are THEN."""
    page, strip, _provider = real_strip
    _hot_settle(strip)
    page._exit_compare_mode()
    _drain(200)
    assert strip.hot is None

    _enter(page)
    _hot_settle(strip)

    assert strip.hot is not None
    assert strip.hot.is_channel_ready(
        "CD20", strip.controllers[1].snapshot()) is True


def test_a_production_run_takes_hot_off_the_device_and_gives_it_back(
        real_strip):
    """Process/Save own the GPU. HOT is background work on the same device
    and stops for the run -- without taking the panels away, because a
    production run does not take the view."""
    page, strip, _provider = real_strip
    _hot_settle(strip)
    assert strip.hot is not None

    page._release_explore_for_production("whole-slide correction (Save)")
    assert strip.hot is None
    assert strip.built is True

    page._on_production_worker_finished()
    _drain(300)
    assert strip.hot is not None


def test_a_dataset_switch_leaves_no_hot_behind(real_strip):
    """No leaked background HOT, and no way for the previous dataset to be
    prepared into the next one."""
    _page_, strip, _provider = real_strip
    _hot_settle(strip)
    hot = strip.hot

    strip.set_dataset("/fake/other.ome.tif")

    assert strip.hot is None
    assert hot._stopped is True
    assert strip.built is False


def test_teardown_stops_hot_before_the_scheduler_goes(real_strip):
    """Order, not merely eventual cleanup: HOT must stop issuing before the
    scheduler joins its workers and the provider is closed, or a request
    can be handed to a scheduler that is already shutting down."""
    _page_, strip, _provider = real_strip
    _hot_settle(strip)
    hot = strip.hot
    order = []
    real_stop = hot.stop
    real_shutdown = strip.stacks.scheduler.shutdown

    hot.stop = lambda: (order.append("hot"), real_stop())[1]
    strip.stacks.scheduler.shutdown = lambda: (
        order.append("scheduler"), real_shutdown())[1]

    strip.teardown(wait_for_floor=False)

    assert order[:2] == ["hot", "scheduler"], order


def test_ten_rapid_channel_clicks_leave_hot_planning_only_the_last(
        real_strip):
    """Latest-wins. Ten switches must not leave ten plans' worth of
    background work behind, and the plan that survives belongs to the
    tenth channel."""
    page, strip, _provider = real_strip
    _hot_settle(strip)

    for channel in ("CD20", "CD3") * 5:
        page.current_channel = channel
        page._sync_compare_to_channel()

    _hot_settle(strip, 900)

    assert [c.channel for c in strip.controllers] == ["CD3"] * 3
    hot = strip.hot
    assert hot._hot_snapshot is not None
    assert hot._hot_snapshot.channel == "CD3"
    assert all(item[0] == hot._hot_generation for item in hot._tile_queue)


def test_hot_requests_never_outrank_the_foreground(real_strip):
    """Strict priority: whatever the user is looking at is asked for in a
    band the background can never enter."""
    from block01.viewer.multichannel_prefetch import HOT_PRIORITY_BASE

    _page_, strip, _provider = real_strip
    scheduler = strip.stacks.scheduler
    seen = []
    real_request = scheduler.request

    def watching(req, callback):
        seen.append(req.priority)
        return real_request(req, callback)

    scheduler.request = watching
    try:
        # A camera nudge, so the window carries FOREGROUND traffic as well
        # as HOT's -- otherwise "HOT is above the band" would be a claim
        # about a band nothing else was in.
        cx, cy, scale = strip.camera(0)
        strip.set_camera(cx + 40.0, cy + 40.0, scale)
        _hot_settle(strip)
    finally:
        scheduler.request = real_request

    assert any(p >= HOT_PRIORITY_BASE for p in seen), "no HOT request at all"
    assert any(p < HOT_PRIORITY_BASE for p in seen), "no foreground request"


# ── 15. the staged publication: three final images, one moment ───────────
#
# Compare exists so three results of the SAME pixels can be judged against
# each other. A channel switch that reaches the three panels one at a time
# breaks exactly that: "TopHat came up first" is read as "TopHat is
# different", and the user reported it in those words.
#
# Measured on the real 59040x35520 slide (29 channels, CD22 -> TIM3) with a
# paint-level probe and a 16 ms sampler, on the code this section replaced:
#
#     level 0, everything HOT-prepared    401 / 401 / 473 ms   spread  71 ms
#     level 1, everything HOT-prepared    445 / 523 / 618 ms   spread 173 ms
#     level 0, cuCIM evicted              413 / 518 / 700 ms   spread 287 ms
#     level 1, cuCIM+TopHat evicted       376 / 577 / 577 ms   spread 200 ms
#
# The first two rows had correction compute 0 and every CorrectionKey a
# cache hit: nothing was computed and nothing was slow. The serialisation
# was in DELIVERY -- three `set_selection` calls, three queued cache-hit
# callbacks, three GUI turns, three frames. `_try_atomic_cached_channel_swap`
# removed it for TopHat and cuCIM at level 0 only, and never for Original,
# which has no method and whose visible image is its raw layer.
#
# So: a read-only preflight decides whether the target is completely ready;
# if it is, all three move in ONE GUI turn; if it is not, the three panels
# keep the OLD channel's three complete images and say so, the missing
# tiles are fetched at foreground priority into the shared caches only, and
# the same one-turn publication runs when the last of them lands.


class _StagedLoader(_LowresLoader):
    _CHANNELS = ["DAPI", "CD22", "TIM3", "PD1"]


class _StagedProvider(_RealishProvider):
    channel_names = ["DAPI", "CD22", "TIM3", "PD1"]


@pytest.fixture
def staged(app, monkeypatch):
    """A real three-controller strip over four channels, HOT stopped.

    HOT is off because it is not what this section is about and because it
    would keep re-warming the very caches these tests empty to create a
    cold target. The preparation under test is the strip's OWN, foreground
    one.
    """
    from block01.viewer import raw_tile_provider as rtp

    provider = _StagedProvider()
    monkeypatch.setattr(rtp, "RawTileProvider", lambda _path: provider)

    page = sp.Step0Page()
    page.loader = _StagedLoader()
    page.ome_path = "/fake/slide.ome.tif"
    page.patches = []
    page.nucleus_channel = "DAPI"
    page._rebuild_channel_list()
    page.current_channel = "CD22"
    page._explore_tab = _Tab(_Stack())
    page.resize(1200, 800)
    page.show()
    QtTest.QTest.qWait(30)
    page._compare_strip_widget._stack_factory = cs.build_compare_stacks
    strip = _enter(page)
    QtTest.QTest.qWait(60)
    strip.stop_hot()
    _drain(200)
    try:
        yield page, strip, provider
    finally:
        try:
            strip.teardown(wait_for_floor=False)
        except Exception:                                   # noqa: BLE001
            pass


def _channels(strip):
    return [c.channel for c in strip.controllers]


def _params_for(page):
    return page._compare_params_for


def _publish_and_wait(strip, page, channel, ms=4000):
    strip.set_channel(channel, params_for=_params_for(page))
    for _ in range(max(1, ms // 100)):
        if not strip.pending:
            break
        _drain(100)
    _drain(120)
    return strip.displayed_channel == channel


def _warm(strip, page, channel, ms=4000):
    """Drive a switch to `channel` through to publication, then come back.

    Leaves `channel` fully prepared in the shared caches and the panels
    where they started, which is the state a "HOT has already prepared it"
    test needs and the only honest way to reach it: the same requests, the
    same keys, the same publication.
    """
    was = strip.displayed_channel
    assert _publish_and_wait(strip, page, channel, ms), (
        f"warming {channel} never published")
    if was is not None and was != channel:
        assert _publish_and_wait(strip, page, was, ms)


class _Sampler(QtCore.QObject):
    """Every 16 ms, what channel is each of the three panels showing?"""

    def __init__(self, strip, parent=None):
        super().__init__(parent)
        self._strip = strip
        self.frames = []
        self._timer = QtCore.QTimer(self)
        self._timer.setTimerType(QtCore.Qt.PreciseTimer)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)

    def start(self):
        self._tick()
        self._timer.start()

    def stop(self):
        self._timer.stop()
        self._tick()

    def _tick(self):
        self.frames.append(tuple(_channels(self._strip)))

    @property
    def mixed(self):
        return [f for f in self.frames if len(set(f)) != 1]


def _drop_corrected(strip, channel, methods=("tophat", "cucim")):
    """Evict `channel`'s corrected tiles for `methods`. Returns how many."""
    cache = strip.stacks.scheduler.corrected_cache
    dropped = 0
    with cache._lock:
        for key in [k for k in list(cache._store)
                    if getattr(k, "channel", None) == channel
                    and getattr(k, "method", None) in methods]:
            arr = cache._store.pop(key)
            cache._bytes -= arr.nbytes
            dropped += 1
    return dropped


def _drop_raw(strip, channel):
    cache = strip.stacks.scheduler.raw_cache
    dropped = 0
    with cache._lock:
        for key in [k for k in list(cache._store)
                    if getattr(k, "channel", None) == channel]:
            arr = cache._store.pop(key)
            cache._bytes -= arr.nbytes
            dropped += 1
    return dropped


# -- 15.1  a prepared target: one GUI turn, no mixed frame ----------------

def test_a_prepared_switch_moves_all_three_inside_one_call(staged):
    """The fast path IS the atomicity: the three `set_selection` calls
    happen inside `set_channel`, with nothing between them."""
    page, strip, _p = staged
    _warm(strip, page, "TIM3")
    assert not strip.pending

    turns = []
    for controller in strip.controllers:
        real = controller.set_selection

        def watched(*a, _r=real, **k):
            turns.append(len(turns))
            return _r(*a, **k)

        controller.set_selection = watched

    strip.set_channel("TIM3", params_for=_params_for(page))
    assert not strip.pending, "a prepared target went through the cold path"
    assert len(turns) == 3, "not all three panels were moved"
    assert _channels(strip) == ["TIM3"] * 3


def test_no_16ms_sample_ever_catches_two_channels_in_the_panels(staged):
    """The product statement, sampled the way the user's eye would."""
    page, strip, _p = staged
    _warm(strip, page, "TIM3")
    sampler = _Sampler(strip, parent=strip)
    sampler.start()
    _drain(120)
    strip.set_channel("TIM3", params_for=_params_for(page))
    _drain(400)
    sampler.stop()
    assert sampler.frames, "the sampler never ran"
    assert not sampler.mixed, f"mixed frames: {sampler.mixed[:5]}"
    assert sampler.frames[-1] == ("TIM3",) * 3


def test_a_prepared_switch_computes_nothing(staged):
    """Cache hit means cache hit: no CorrectionKey is recomputed, and the
    provider is not asked for a tile it has already given."""
    page, strip, provider = staged
    _warm(strip, page, "TIM3")
    before = len(provider.tile_reads)
    keys_before = set(_corrected_keys(strip))

    strip.set_channel("TIM3", params_for=_params_for(page))
    _drain(150)

    assert not strip.pending
    assert _channels(strip) == ["TIM3"] * 3
    new = set(_corrected_keys(strip)) - keys_before
    assert not [k for k in new if k.channel == "TIM3"], (
        "a prepared channel was corrected again")
    assert len(provider.tile_reads) == before, (
        "a prepared channel was read again")


# -- 15.2/15.3/15.4  partial readiness never publishes early --------------

@pytest.mark.parametrize("missing", ["cucim", "tophat", "both", "raw"])
def test_a_partly_ready_target_keeps_the_old_three_images(staged, missing):
    """Original ready and TopHat/cuCIM not, TopHat ready and cuCIM not, and
    the raw-only case the Original panel owns: none of them may put one or
    two panels on the new channel."""
    page, strip, _p = staged
    _warm(strip, page, "TIM3")
    if missing == "both":
        assert _drop_corrected(strip, "TIM3")
    elif missing == "raw":
        assert _drop_raw(strip, "TIM3")
    else:
        assert _drop_corrected(strip, "TIM3", methods=(missing,))

    sampler = _Sampler(strip, parent=strip)
    sampler.start()
    strip.set_channel("TIM3", params_for=_params_for(page))
    assert strip.pending, "a target with a hole in it published anyway"
    assert _channels(strip) == ["CD22"] * 3
    assert strip.displayed_channel == "CD22"
    assert strip.pending_text() == "Preparing TIM3 — still showing CD22"
    _drain(150)
    sampler.stop()
    assert not sampler.mixed, f"mixed frames: {sampler.mixed[:5]}"


def test_the_publication_waits_for_the_last_of_the_three(staged):
    """Scrambled completion order: whatever lands first, nothing is
    published until nothing is missing."""
    page, strip, _p = staged
    _warm(strip, page, "TIM3")
    _drop_corrected(strip, "TIM3")
    _drop_raw(strip, "TIM3")

    seen = []
    real = strip._settle_pending

    def watched():
        if strip.pending:
            missing = strip._missing_for("TIM3", _params_for(page))
            seen.append((len(missing or ()), tuple(_channels(strip))))
        return real()

    strip._settle_pending = watched
    strip.set_channel("TIM3", params_for=_params_for(page))
    for _ in range(40):
        if not strip.pending:
            break
        _drain(100)
    _drain(150)

    assert _channels(strip) == ["TIM3"] * 3
    assert seen, "nothing was ever delivered"
    assert all(panels == ("CD22",) * 3 for _n, panels in seen), (
        "a panel moved to TIM3 while the plan was still short")


# -- 15.5  latest wins ----------------------------------------------------

def test_a_third_click_wins_and_the_second_never_publishes(staged):
    """CD22 -> TIM3 -> PD1: TIM3 is dropped, only PD1 is published."""
    page, strip, _p = staged
    _drop_corrected(strip, "TIM3")
    _drop_raw(strip, "TIM3")
    published = []
    strip.publication_changed.connect(
        lambda: published.append(strip.displayed_channel))

    strip.set_channel("TIM3", params_for=_params_for(page))
    assert strip.pending and strip.pending_channel == "TIM3"
    strip.set_channel("PD1", params_for=_params_for(page))
    assert strip.pending_channel == "PD1"
    for _ in range(40):
        if not strip.pending:
            break
        _drain(100)
    _drain(200)

    assert _channels(strip) == ["PD1"] * 3
    assert "TIM3" not in published, (
        f"a superseded target was published: {published}")


def test_a_superseded_targets_failure_does_not_sink_the_current_one(staged):
    """The serial is not decoration. A delivery belongs to the plan that
    asked for it, and a FAILING one from a plan the user has moved off
    would otherwise take down the plan they are actually waiting for --
    the panels would stop preparing PD1 and say a preparation had failed,
    because a TIM3 tile the scheduler was already running came back with
    an error."""
    page, strip, _p = staged
    _drop_corrected(strip, "TIM3")
    _drop_raw(strip, "TIM3")
    strip.set_channel("TIM3", params_for=_params_for(page))
    assert strip.pending
    stale_serial = strip._pending["serial"]

    _drop_corrected(strip, "PD1")
    _drop_raw(strip, "PD1")
    strip.set_channel("PD1", params_for=_params_for(page))
    assert strip.pending and strip.pending_channel == "PD1"

    class _Failed:
        error = "the reader gave up"

    strip._on_pending_tile(stale_serial, _Failed())

    assert strip.pending, "a superseded plan's failure cancelled the live one"
    assert strip.pending_channel == "PD1"
    assert strip.pending_text() == "Preparing PD1 — still showing CD22"


# -- 15.6/15.7  a pending plan is only good for where it was made ---------

def test_a_pan_during_a_pending_switch_replans_for_the_new_viewport(staged):
    """Publishing from a stale viewport would put the tiles of where the
    panels WERE on a camera that has moved."""
    page, strip, _p = staged
    _drop_corrected(strip, "TIM3")
    _drop_raw(strip, "TIM3")
    strip.set_channel("TIM3", params_for=_params_for(page))
    assert strip.pending
    before = strip._pending["token"]
    generation = strip._pending["generation"]

    cx, cy, scale = strip.camera(0)
    strip.set_camera(cx + 900.0, cy + 700.0, scale)
    QtTest.QTest.qWait(20)

    if strip.pending:
        assert strip._pending["token"] != before, "the plan was not re-made"
        assert strip._pending["generation"] != generation
    assert _channels(strip) in (["CD22"] * 3, ["TIM3"] * 3)
    if strip.displayed_channel == "TIM3":
        # If it did publish, it published for the viewport it is on NOW.
        assert not strip._missing_for("TIM3", _params_for(page))


def test_a_parameter_edit_during_a_pending_switch_voids_the_old_numbers(staged):
    """The parameters are in the CorrectionKey, so an edit makes every key
    the plan prepared a key nobody will ask for again."""
    page, strip, _p = staged
    _drop_corrected(strip, "TIM3")
    strip.set_channel("TIM3", params_for=_params_for(page))
    assert strip.pending
    before = strip._pending["token"]

    def other_params(source):
        base = page._compare_params_for(source)
        return tuple(int(v) + 7 for v in base)

    strip.set_params(other_params)
    assert strip.pending, "a parameter edit published a half-prepared target"
    assert strip._pending["token"] != before
    assert _channels(strip) == ["CD22"] * 3
    # ...and the plan the strip is now working to is the NEW numbers'.
    assert strip._missing_for("TIM3", other_params) is not None


# -- 15.8  every way compare stops being the view -------------------------

@pytest.mark.parametrize("stop", ["suspend", "leave", "teardown", "dataset"])
def test_a_pending_switch_is_cancelled_when_compare_stops(staged, stop):
    """Leaving, a Process/Save hand-off, a dataset switch and teardown all
    have to leave nothing behind that could publish later."""
    page, strip, _p = staged
    _drop_corrected(strip, "TIM3")
    _drop_raw(strip, "TIM3")
    strip.set_channel("TIM3", params_for=_params_for(page))
    assert strip.pending

    if stop == "suspend":
        strip.suspend("a run", badge="Paused")
    elif stop == "leave":
        page._exit_compare_mode()
    elif stop == "teardown":
        strip.teardown(wait_for_floor=False)
    else:
        strip.set_dataset("/fake/other.ome.tif")

    assert not strip.pending
    assert strip.pending_text() is None
    _drain(400)
    assert not strip.pending
    if strip.built:
        assert _channels(strip) == ["CD22"] * 3


# -- 15.9/15.10  every level, not just level 0 ----------------------------

@pytest.mark.parametrize("scale", [1.0, 0.5, 0.2])
def test_a_prepared_switch_is_atomic_at_every_level(staged, scale):
    """The old atomic swap was LEVEL 0 ONLY, which is why level 1 measured
    a 173 ms spread on the real slide with nothing left to compute."""
    page, strip, _p = staged
    cx, cy, _s = strip.camera(0)
    strip.set_camera(cx, cy, scale)
    _drain(300)
    levels = {c.level for c in strip.controllers}
    assert len(levels) == 1
    _warm(strip, page, "TIM3")

    sampler = _Sampler(strip, parent=strip)
    sampler.start()
    strip.set_channel("TIM3", params_for=_params_for(page))
    published_at_once = not strip.pending
    _drain(200)
    sampler.stop()

    assert published_at_once, f"level {levels} took the cold path when ready"
    assert not sampler.mixed
    assert _channels(strip) == ["TIM3"] * 3
    swaps = [c.stats.get("atomic_channel_swaps", 0) for c in strip.controllers]
    assert all(s >= 1 for s in swaps), (
        f"level {levels}: some panel did not swap synchronously ({swaps})")


# -- 15.11  the nucleus overlay is not part of this -----------------------

def test_the_dapi_overlay_is_untouched_by_a_pending_switch(staged):
    page, strip, _p = staged
    before = [(o.effective_enabled, o.channel) for o in strip.overlays]
    _drop_corrected(strip, "TIM3")
    _drop_raw(strip, "TIM3")
    strip.set_channel("TIM3", params_for=_params_for(page))
    assert strip.pending
    assert [(o.effective_enabled, o.channel) for o in strip.overlays] == before
    for _ in range(40):
        if not strip.pending:
            break
        _drain(100)
    assert [(o.effective_enabled, o.channel) for o in strip.overlays] == before


# -- 15.12  one measurement, and only after the publication ---------------

def test_the_metrics_wait_for_the_publication_and_then_run_once(staged):
    page, strip, _p = staged
    runs = _count_metrics(page)
    _drop_corrected(strip, "TIM3")
    _drop_raw(strip, "TIM3")

    page.current_channel = "TIM3"
    page._sync_compare_to_channel()
    assert strip.pending
    assert page._compare_metrics_plan_now() is None, (
        "a pending switch planned a measurement of the old channel")
    assert not page._compare_metrics_pending()
    assert runs == [], "the old channel was measured under the new label"

    for _ in range(40):
        if not strip.pending:
            break
        _drain(100)
    assert _channels(strip) == ["TIM3"] * 3
    _quiet()
    assert len(runs) == 1, f"expected exactly one measurement, got {runs}"
    assert runs[0] == ("TIM3", ("TIM3", "TIM3", "TIM3"))


def test_a_pending_switch_puts_the_numbers_back_to_a_dash(staged):
    page, strip, _p = staged
    _warm(strip, page, "TIM3")
    page.current_channel = "TIM3"
    page._sync_compare_to_channel()
    _quiet()
    _drop_corrected(strip, "PD1")
    _drop_raw(strip, "PD1")
    page.current_channel = "PD1"
    page._sync_compare_to_channel()
    assert strip.pending
    for label, _source, name in page._compare_metric_labels():
        assert label.text() == f"{name} → —"


# -- 15.13  the words, and the invariant behind them ----------------------

def test_the_panels_never_disagree_with_the_strips_own_heading(staged):
    """Either the three panels are the displayed channel, or the strip says
    in words that they are not. There is no third state."""
    page, strip, _p = staged
    _drop_corrected(strip, "TIM3")
    _drop_raw(strip, "TIM3")
    strip.set_channel("TIM3", params_for=_params_for(page))
    seen = []
    for _ in range(40):
        seen.append((tuple(_channels(strip)), strip.displayed_channel,
                     strip.pending_text()))
        if not strip.pending:
            break
        _drain(100)
    _drain(150)
    seen.append((tuple(_channels(strip)), strip.displayed_channel,
                 strip.pending_text()))
    for panels, displayed, words in seen:
        assert len(set(panels)) == 1, f"panels disagreed: {panels}"
        assert panels[0] == displayed, (
            f"panels on {panels[0]} while the strip says {displayed}")
        if displayed != "TIM3":
            assert words == "Preparing TIM3 — still showing CD22"


def test_a_pending_target_never_writes_into_a_visible_pool(staged):
    """The preparation is CACHES ONLY: nothing it fetches may be pooled,
    because a pooled tile is a painted tile."""
    page, strip, _p = staged
    _drop_corrected(strip, "TIM3")
    _drop_raw(strip, "TIM3")
    strip.set_channel("TIM3", params_for=_params_for(page))
    assert strip.pending
    for _ in range(6):
        _drain(60)
        if not strip.pending:
            break
        for controller in strip.controllers:
            for pool in (controller._raw_pool, controller._precise_pool):
                for entry in pool.entries.values():
                    key = entry.key
                    if key is not None:
                        assert getattr(key, "channel", None) != "TIM3", (
                            "a pending target's tile was pooled")


# -- 15.14  the preflight itself ------------------------------------------

def test_the_preflight_names_the_overview_as_well_as_the_tiles(staged):
    """Cached tiles are not enough: without the target's overview record
    its display range is unknown and nothing may be drawn."""
    page, strip, _p = staged
    _warm(strip, page, "TIM3")
    assert not strip._missing_for("TIM3", _params_for(page))

    store = strip.controllers[0]._overview_store
    for key in [k for k in store.cache if k[1] == "TIM3"]:
        del store.cache[key]

    missing = strip._missing_for("TIM3", _params_for(page))
    assert missing, "an evicted overview record left the target 'ready'"
    assert ("overview", "TIM3") in missing


def test_the_preflight_reads_nothing_and_requests_nothing(staged):
    """It is a cache probe. If it were not, running it would be a cost the
    fast path could not afford."""
    page, strip, provider = staged
    _drop_corrected(strip, "TIM3")
    reads = len(provider.reads) + len(provider.tile_reads)
    asked = []
    real_request = strip.stacks.scheduler.request
    strip.stacks.scheduler.request = lambda r, cb: asked.append(r)
    try:
        strip._missing_for("TIM3", _params_for(page))
        strip.pending_token("TIM3", _params_for(page))
    finally:
        strip.stacks.scheduler.request = real_request
    assert asked == []
    assert len(provider.reads) + len(provider.tile_reads) == reads


def test_a_pending_target_is_asked_for_above_the_neighbour_band(staged):
    """Foreground: the user is waiting on this channel, so nothing HOT
    queues may be served before it."""
    from block01.viewer.multichannel_prefetch import HOT_PRIORITY_BASE

    page, strip, _p = staged
    _drop_corrected(strip, "TIM3")
    _drop_raw(strip, "TIM3")
    seen = []
    real_request = strip.stacks.scheduler.request

    def watching(req, callback):
        seen.append(req.priority)
        return real_request(req, callback)

    strip.stacks.scheduler.request = watching
    try:
        strip.set_channel("TIM3", params_for=_params_for(page))
    finally:
        strip.stacks.scheduler.request = real_request

    assert seen, "a cold target asked for nothing"
    assert max(seen) < HOT_PRIORITY_BASE, (
        f"a pending target was queued in the neighbour band: {seen}")


def test_a_failed_preparation_keeps_the_old_three_images(staged):
    """A target that cannot be prepared must not take the panels down with
    it, and must not leave two channels mixed across the three."""
    page, strip, _p = staged
    _drop_corrected(strip, "TIM3")
    _drop_raw(strip, "TIM3")
    strip.set_channel("TIM3", params_for=_params_for(page))
    assert strip.pending
    strip._fail_pending("the reader said no")

    assert not strip.pending
    assert _channels(strip) == ["CD22"] * 3
    assert "still showing CD22" in strip.pending_text()
    _drain(300)
    assert _channels(strip) == ["CD22"] * 3
