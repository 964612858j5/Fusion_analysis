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

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtCore, QtGui, QtTest, QtWidgets  # noqa: E402

from block01.ui.step0 import compare_strip as cs  # noqa: E402
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

    def width(self):
        return self._width

    def height(self):
        return self._height

    def set_rect(self, x0, y0, w, h):
        self._range = ((float(x0), float(x0) + float(w)),
                       (float(y0), float(y0) + float(h)))


class _Stack:
    def __init__(self, level=0, scale_width=1024.0, view_w=SLIDE_W):
        self.controller = _FullController(level)
        self.provider = _Provider()
        self.scheduler = type("S", (), {"raw_cache": None,
                                        "corrected_cache": None})()
        self.overlay = None
        self.view = type("V", (), {"view_box": _ViewBox(0.0, view_w,
                                                        scale_width)})()
        self.controller.view_box = self.view.view_box


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


class _Store:
    def __init__(self):
        self.cache = {}
        self.shutdowns = 0

    def shutdown(self):
        self.shutdowns += 1


def _fake_compare_factory(record):
    """A `build_compare_stacks` stand-in that makes real `ExploreView`s."""

    def factory(path, channel, parent_widget=None, *, sources=cs.COMPARE_SOURCES,
                params_for=None, tint=None, nucleus_channel=None,
                nucleus_tint=None, nucleus_enabled=False, viewport_l0=None):
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
        stacks = cs.CompareStacks(provider, object(), object(), object(),
                                  (), _Store(), controllers, views, overlays)
        record.append({"path": path, "channel": channel, "tint": tint,
                       "viewport_l0": viewport_l0,
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


def test_the_entry_camera_and_the_point_are_remembered(app):
    page = _page(app)
    cam = page._full_image_camera()
    _enter(page, cam[0] + 250, cam[1] + 125)
    assert page._compare_entry_full_camera == pytest.approx(cam, rel=1e-12)
    assert page._compare_entry_point == pytest.approx(
        (cam[0] + 250, cam[1] + 125))
    assert page._compare_entry_scale == pytest.approx(cam[2])


# ── 4. leaving: the saved camera plus the delta ──────────────────────────

def _full_rect(page):
    (x0, x1), (y0, y1) = page._explore_tab.stack.view.view_box.viewRange()
    return (x0, y0, x1 - x0, y1 - y0)


def test_five_toggles_with_the_mouse_still_move_nothing(app):
    """THE contract. Entering centres on P, which on its own would walk the
    view by `P - centre` every cycle; the delta rule cancels it exactly."""
    page = _page(app)
    cam = page._full_image_camera()
    P = (cam[0] + 700.0, cam[1] - 400.0)
    before = _full_rect(page)
    for _ in range(5):
        _enter(page, *P)
        page._exit_compare_mode()
        QtTest.QTest.qWait(10)
    after = _full_rect(page)
    for a, b in zip(before, after):
        assert b == pytest.approx(a, rel=1e-9, abs=1e-6)


def test_a_pan_in_compare_mode_shifts_the_full_image_by_the_same_delta(app):
    page = _page(app)
    cam = page._full_image_camera()
    P = (cam[0] + 300.0, cam[1] + 200.0)
    strip = _enter(page, *P)
    now = strip.camera(0)
    page._apply_compare_camera(now[0] + 900.0, now[1] - 500.0, now[2])
    QtTest.QTest.qWait(10)
    page._exit_compare_mode()
    QtTest.QTest.qWait(10)
    back = page._full_image_camera()
    assert back[0] == pytest.approx(cam[0] + 900.0, abs=1.0)
    assert back[1] == pytest.approx(cam[1] - 500.0, abs=1.0)
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


def test_flipping_modes_without_ever_entering_moves_no_camera(app):
    page = _page(app)
    before = _full_rect(page)
    page._set_compare_mode(True)
    page._exit_compare_mode()
    QtTest.QTest.qWait(10)
    assert _full_rect(page) == pytest.approx(before)


def test_the_returning_camera_is_the_saved_one_when_nothing_moved(app):
    page = _page(app)
    cam = page._full_image_camera()
    _enter(page, cam[0] + 900.0, cam[1])
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

def test_the_three_controllers_share_one_provider_and_scheduler(app):
    """Real factory, no page: three views of one slide must not open three
    TIFF handles, three caches and three schedulers."""
    from block01.ui.step0.compare_strip import build_compare_stacks
    import inspect
    src = inspect.getsource(build_compare_stacks)
    assert src.count("RawTileProvider(") == 1
    assert src.count("TileScheduler(") == 1
    assert src.count("LRUByteCache(") == 2      # one raw, one corrected
    assert src.count("SharedOverviewStore()") == 1


def test_each_panel_issues_under_its_own_generation_namespace(app):
    """One scheduler between three controllers: `cancel_generation` matches
    by equality, so without a namespace all three reach `("raw", 5)` and one
    panel's cancel drops another's queued tiles."""
    from block01.viewer.explore_view import ExploreController
    import inspect
    sig = inspect.signature(ExploreController.__init__)
    assert "gen_ns" in sig.parameters
    assert "overview_store" in sig.parameters
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
    assert store.shutdowns == 1


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

def test_the_dapi_layer_is_off_until_its_checkbox_is_on(app):
    page = _page(app)
    strip = _enter(page)
    assert [o.effective_enabled for o in strip.overlays] == [False] * 3


def test_the_dapi_checkbox_puts_the_overlay_in_every_panel(app):
    page = _page(app)
    strip = _enter(page)
    page._btn_show_nucleus.setChecked(True)
    QtTest.QTest.qWait(10)
    assert [o.effective_enabled for o in strip.overlays] == [True] * 3
    page._btn_show_nucleus.setChecked(False)
    QtTest.QTest.qWait(10)
    assert [o.effective_enabled for o in strip.overlays] == [False] * 3


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
