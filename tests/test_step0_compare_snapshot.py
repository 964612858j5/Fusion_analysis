"""Phase A2: a right-click on the full image is a compare SNAPSHOT.

The three compare panels stopped being a small viewer of a patch and became
a static frame cut out of the full image at the point the user pointed at.
What that has to mean, and what this module pins down:

* the three panels are filled from ONE crop centred on the clicked slide
  point, at the pyramid level the full image is on and at its screen scale,
  so a panel is the same picture as the region it came from -- only
  corrected differently;
* the panels cannot be zoomed or panned. There is no second magnification
  to get out of step with the header, and the range is the snapshot's own
  level-0 rectangle in every panel;
* a second right-click retakes it, wholesale;
* it computes nothing in the page's sense: no `BatchProcessWorker`, no
  `_preview_cache` entry, no channel state, no signature. The metrics do
  show its numbers -- that is the question the page exists to answer -- and
  they say "preview";
* the strip auto-expands on the first right-click and the toolbar button
  still collapses it;
* a coarse level says "downsampled xN", measured from the provider's level
  shapes;
* "Save as patch" appends the snapshot's LEVEL-0 rectangle to the patch
  list, and does nothing else;
* the DAPI overlay follows the same checkbox the full image follows.

Own module, like the other page-heavy Step0 suites: combined runs segfault
in offscreen pyqtgraph (not a regression, see test_step0_full_image.py).

No slide: a fake provider serving a ramp, and the real correction compute
object is replaced by one that records what it was asked for.
"""

import os
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtCore, QtGui, QtTest, QtWidgets  # noqa: E402

from block01.ui.step0 import step0_page as sp  # noqa: E402
from block01.viewer.tile_types import effective_param  # noqa: E402

from test_step0_background_correction_tab import (  # noqa: E402
    _GpuPathLoader,
    app,            # noqa: F401  (pytest fixture)
)


SLIDE_H, SLIDE_W = 4096, 4096


# ── stand-ins ────────────────────────────────────────────────────────────

class _Provider:
    """A 2x pyramid over a square slide, serving a per-pixel ramp.

    The value at a level-L pixel is `y * 10000 + x`, so a crop identifies
    exactly where it was read from and an x/y swap cannot go unnoticed.
    """

    num_levels = 4

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


class _Compute:
    """`CorrectionCompute` reduced to the one method the snapshot uses."""

    def __init__(self):
        self.calls = []

    def correct_array(self, arr, method, param):
        self.calls.append((method, int(param), arr.shape))
        # A method-distinguishable, position-preserving transform, so a
        # panel showing the wrong method or the wrong crop is visible.
        return arr * (2.0 if method == "tophat" else 3.0)


class _Controller:
    def __init__(self, level=0, channel="CD3"):
        self.level = level
        self.channel = channel
        self.method = None
        self.params = ()
        self.compute = _Compute()
        self.grid = type("G", (), {"tile_size": 512})()
        self.view_rects = []
        self.view_box = None

    def set_marker_visible(self, _v):
        pass

    def set_display_mapping(self, *_a, **_k):
        pass

    def set_view_rect_l0(self, x0, y0, w, h):
        self.view_rects.append((x0, y0, w, h))
        # The camera really MOVES, as the real controller's does: the
        # round-trip contract (enter compare mode, come back, and land on
        # the rectangle you left) is only checkable against a view box that
        # remembers what it was told.
        if self.view_box is not None:
            self.view_box.set_rect(x0, y0, w, h)


class _ViewBox:
    """Reports a viewport and a widget size, which together fix the full
    image's scale in screen pixels per level-0 pixel.

    `set_rect` is the real ViewBox's `setRange` reduced to what
    `set_view_rect_l0` asks of it: the rectangle handed in is solved for
    this widget's own aspect, so an aspect-locked box would honour it
    exactly and this one simply stores it.
    """

    def __init__(self, x0=0.0, x1=None, width=1024.0, height=768.0):
        self._range = ((float(x0), float(SLIDE_W if x1 is None else x1)),
                       (0.0, float(SLIDE_H)))
        self._width = float(width)
        self._height = float(height)

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
        self.controller = _Controller(level)
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
    """A `_GpuPathLoader` that can also read the WHOLE SLIDE at 1/64.

    That read is the compare panels' underlay -- the floor a zoom-out
    uncovers -- so a fixture without it is a page with no floor at all and
    proves nothing about the layering. Same ramp as the provider serves, so
    an underlay array says which slide pixels it holds; DAPI is offset by
    1000, so its overlay cannot be confused with a second copy of the
    marker.
    """

    shape = (SLIDE_H, SLIDE_W)
    OVERVIEW_DS = 64

    def __init__(self):
        super().__init__()
        self.lowres_reads = []

    def overview_downsample(self):
        return self.OVERVIEW_DS

    def read_region_lowres(self, ch, y0, y1, x0, x1, ds, normalize=False):
        ds = int(ds)
        self.lowres_reads.append((ch, ds))
        h = max(1, (int(y1) - int(y0)) // ds)
        w = max(1, (int(x1) - int(x0)) // ds)
        ys = np.arange(h, dtype=np.float32)[:, None] * ds
        xs = np.arange(w, dtype=np.float32)[None, :] * ds
        return ys * 10000.0 + xs + (1000.0 if ch == "DAPI" else 0.0)


LOWRES_SHAPE = (SLIDE_H // _LowresLoader.OVERVIEW_DS,
                SLIDE_W // _LowresLoader.OVERVIEW_DS)


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
    # Shown, because the crop's SIZE is the panel's size: a page that was
    # never laid out has none, and a fallback constant would be a second
    # geometry for the tests to keep in step with. `_panel_px` reads the
    # real one back.
    page.resize(1200, 800)
    page.show()
    QtTest.QTest.qWait(30)
    return page


def _panel_px(page):
    """One panel's size in screen pixels, as the page measures it."""
    page._set_compare_mode(True)
    QtTest.QTest.qWait(30)
    return page._compare_panel_px()


def _snapshot(page, x, y, timeout=5000):
    """Right-click at level-0 `(x, y)` and wait for the panels to fill.

    The previous payload is dropped first so that waiting for "a snapshot
    is on screen" cannot be satisfied by the one before it.
    """
    page._last_payload = None
    page._on_full_image_right_click(float(x), float(y))
    worker = page._compare_snapshot_worker
    if worker is not None:
        assert worker.wait(timeout), "the snapshot worker never finished"
    deadline = QtCore.QElapsedTimer()
    deadline.start()
    while (page._last_payload or {}).get("snapshot") is not True:
        QtTest.QTest.qWait(10)
        assert deadline.elapsed() < timeout, "the snapshot never arrived"
    return page._last_payload


def _right_click(widget):
    """A real right-button press on `widget`, as Qt would deliver it."""
    QtWidgets.QApplication.sendEvent(
        widget,
        QtGui.QMouseEvent(QtCore.QEvent.MouseButtonPress,
                          QtCore.QPointF(5.0, 5.0), QtCore.Qt.RightButton,
                          QtCore.Qt.RightButton, QtCore.Qt.NoModifier))


def _crop_calls(page):
    """`(method, param)` for the corrections run on a CROP, not on the
    whole-slide underlay."""
    return [(m, p) for m, p, shape
            in page._explore_tab.stack.controller.compute.calls
            if shape != LOWRES_SHAPE]


def _underlay_calls(page):
    """`(method, param)` for the corrections run on the whole-slide
    underlay."""
    return [(m, p) for m, p, shape
            in page._explore_tab.stack.controller.compute.calls
            if shape == LOWRES_SHAPE]


def _no_workers(monkeypatch):
    started = []

    def _boom(*a, **k):
        started.append(a)
        raise AssertionError("a BatchProcessWorker was constructed")

    monkeypatch.setattr(sp, "BatchProcessWorker", _boom)
    return started


# ── 1. the three panels are one frame centred on the click ───────────────

def test_a_right_click_fills_all_three_panels(app):
    page = _page(app)

    payload = _snapshot(page, 2000, 1500)

    pw, ph = page._compare_panel_px()
    for key in ("original_raw", "tophat_raw", "cucim_raw"):
        assert payload[key] is not None, key
        # Level pixels, so the panel's screen size over the full image's
        # scale (a quarter screen pixel per slide pixel here).
        assert payload[key].shape == (round(ph / 0.25), round(pw / 0.25))
    assert [img.image is not None for img in page._preview_imgs] == [True] * 3


def test_the_snapshot_is_centred_on_the_full_images_view(app):
    """The right-click is a TRIGGER, not a centre.

    Recentring on the clicked point is what made the two modes drift: going
    back adopted the recentred view, so the next click at the same screen
    pixel named a different slide point and the view walked. The panels take
    the view that is already on screen.
    """
    page = _page(app)
    (vx0, vx1), (vy0, vy1) = page._explore_tab.stack.view.view_box.viewRange()

    payload = _snapshot(page, 2000, 1500)

    x0, y0, w, h = payload["snapshot_rect_l0"]
    # Within the rounding of the crop to whole level pixels: an odd width
    # cannot have the centre exactly in the middle of a pixel.
    assert (x0 + w / 2.0, y0 + h / 2.0) == pytest.approx(
        ((vx0 + vx1) / 2.0, (vy0 + vy1) / 2.0), abs=1.0)


def test_the_clicked_point_moves_nothing(app):
    """Two right-clicks at opposite corners of the slide, same view: the
    same frame both times."""
    page = _page(app)

    first = dict(_snapshot(page, 10, 10))
    second = _snapshot(page, SLIDE_W - 10, SLIDE_H - 10)

    assert second["snapshot_rect_l0"] == first["snapshot_rect_l0"]


def test_the_panels_are_at_the_full_images_own_scale(app):
    """Screen pixels per slide pixel in a panel must equal the number the
    full image is drawn at: the crop covers `panel_px / scale` slide pixels
    and the panel is pinned to exactly that rectangle."""
    # 1024 widget px over 4096 slide px -> a quarter screen px per slide px.
    page = _page(app, scale_width=1024.0, view_w=SLIDE_W)
    pw, ph = _panel_px(page)

    payload = _snapshot(page, 2000, 1500)

    _x0, _y0, w, h = payload["snapshot_rect_l0"]
    assert (w, h) == (round(pw / 0.25), round(ph / 0.25))
    for vb in page._preview_vbs:
        (_vx0, _vx1), (vy0, vy1) = vb.viewRange()
        # The HEIGHT is the exact one. An aspect-locked ViewBox handed a
        # rectangle keeps the axis it does not have to grow and widens the
        # other to the widget's aspect, and the three panels are columns of
        # one layout whose widths differ by a pixel or two -- so the x
        # extent carries that difference and the scale does not.
        assert ph / (vy1 - vy0) == pytest.approx(0.25, abs=0.002)


def test_a_zoomed_in_full_image_gives_a_smaller_crop(app):
    """Same panel, finer scale: fewer slide pixels, same screen size."""
    page = _page(app, scale_width=1024.0, view_w=1024)   # 1 screen px / px
    pw, ph = _panel_px(page)

    payload = _snapshot(page, 2000, 1500)

    _x0, _y0, w, h = payload["snapshot_rect_l0"]
    assert (w, h) == (round(pw), round(ph))


def test_every_panel_is_registered_to_the_same_place_and_scale(app):
    """One crop, three panels, one scale.

    The three ViewBoxes are columns of one pyqtgraph layout and their
    widths can differ by a pixel or two, so an aspect-locked panel handed
    the crop's rectangle keeps the HEIGHT it was given and widens the x
    extent to fit -- a hair more neighbouring slide on the wider column,
    at the same magnification. Height and centre are therefore what "the
    same frame" means here, and they are exact.
    """
    page = _page(app)

    payload = _snapshot(page, 2000, 1500)

    x0, y0, w, h = payload["snapshot_rect_l0"]
    centres, heights = [], []
    for vb in page._preview_vbs:
        (vx0, vx1), (vy0, vy1) = vb.viewRange()
        centres.append((round((vx0 + vx1) / 2.0, 3),
                        round((vy0 + vy1) / 2.0, 3)))
        heights.append(round(vy1 - vy0, 3))
    assert len(set(centres)) == 1, centres
    assert len(set(heights)) == 1, heights
    assert centres[0] == pytest.approx((x0 + w / 2.0, y0 + h / 2.0), abs=1.0)
    assert heights[0] == pytest.approx(h, abs=1.0)


def test_the_crop_is_read_where_it_says_it_is(app):
    """The ramp the fake provider serves makes the array itself say which
    slide pixels it holds."""
    page = _page(app, scale_width=1024.0, view_w=1024)   # 1:1, level 0

    payload = _snapshot(page, 2000, 1500)

    x0, y0, _w, _h = payload["snapshot_rect_l0"]
    assert payload["original_raw"][0, 0] == y0 * 10000.0 + x0


def test_near_the_edge_the_frame_slides_in_rather_than_shrinking(app):
    """Shrinking the crop would change the scale, which is the one thing a
    snapshot must not do."""
    page = _page(app, scale_width=1024.0, view_w=1024)
    pw, ph = _panel_px(page)
    # The full image hanging over the slide's top-left corner at 1:1, so
    # its centre is a few pixels in and a panel-sized frame around that
    # centre runs off the slide.
    page._explore_tab.stack.view.view_box.set_rect(-500.0, -400.0,
                                                   1024.0, 768.0)

    payload = _snapshot(page, 5, 5)

    x0, y0, w, h = payload["snapshot_rect_l0"]
    assert (w, h) == (round(pw), round(ph))
    assert (x0, y0) == (0.0, 0.0)


# ── 2. the panels have one camera, and it drives the crop ────────────────

def test_the_panels_take_mouse_input(app):
    page = _page(app)
    for vb in page._preview_vbs:
        assert vb.state["mouseEnabled"] == [True, True]


def test_the_view_controls_are_gone(app):
    page = _page(app)
    for gone in ("_btn_lock_zoom", "_reset_all_views", "_reset_single_view",
                 "_sync_zoom", "_full_image_buttons", "_enter_full_image",
                 "_match_full_image_to_panel", "_full_view_range_for_panel",
                 "_compare_panel_camera", "_vb_screen_geometry",
                 "_compare_viewport_l0", "_preview_stack"):
        assert not hasattr(page, gone), gone


class _WheelEvent:
    """The little of a pyqtgraph wheel event ViewBox.wheelEvent reads."""

    def delta(self):
        return 120

    def scenePos(self):
        return QtCore.QPointF(0.0, 0.0)

    def pos(self):
        return QtCore.QPointF(0.0, 0.0)

    def accept(self):
        pass

    def ignore(self):
        pass


def _settle(page, ms=260, timeout=5000):
    """Let the panels' refill debounce fire and its worker land."""
    QtTest.QTest.qWait(ms)
    worker = page._compare_snapshot_worker
    if worker is not None:
        assert worker.wait(timeout)
    QtTest.QTest.qWait(80)


def _view(page):
    (x0, x1), (y0, y1) = page._preview_vbs[0].viewRange()
    return (x0, y0, x1 - x0, y1 - y0)


def _pan(page, dx, dy=0.0):
    """Move the shared camera by `(dx, dy)` level-0 pixels, as a drag would."""
    (x0, x1), (y0, y1) = page._preview_vbs[0].viewRange()
    page._preview_vbs[0].setRange(xRange=(x0 + dx, x1 + dx),
                                  yRange=(y0 + dy, y1 + dy), padding=0)


def test_a_wheel_event_zooms_a_panel(app):
    """The panels are the whole viewing area now. Not being able to look
    closer at the thing they were opened to look at cost more than the
    second magnification it avoided."""
    page = _page(app)
    _snapshot(page, 2000, 1500)
    _x0, _y0, before_w, _h = _view(page)

    page._preview_vbs[0].wheelEvent(_WheelEvent())

    assert _view(page)[2] < before_w, "the wheel did not zoom in"


def test_zooming_one_panel_moves_the_other_two(app):
    """One camera, three panels: three columns at three magnifications of
    three places would not be a comparison."""
    page = _page(app)
    _snapshot(page, 2000, 1500)

    page._preview_vbs[0].wheelEvent(_WheelEvent())

    ranges = [vb.viewRange() for vb in page._preview_vbs]
    for xr, yr in ranges[1:]:
        # The HEIGHT is the exact shared number; the aspect-locked columns
        # differ by a pixel or two in width, so the x extents carry that
        # difference and their CENTRES are what "the same place" means --
        # the same reading the geometry tests above take.
        assert list(yr) == pytest.approx(list(ranges[0][1]), rel=1e-6)
        assert (xr[0] + xr[1]) / 2 == pytest.approx(
            (ranges[0][0][0] + ranges[0][0][1]) / 2, abs=2.0)


def test_panning_inside_the_margin_asks_for_nothing(app):
    """A view-driven crop carries 25% of the view on each side, so a nudge
    inside it costs no read at all. (The crop a right-click cuts has no
    margin -- it is exactly the frame that was pointed at -- so the first
    gesture after entering compare mode does refetch.)"""
    page = _page(app)
    _snapshot(page, 2000, 1500)
    _pan(page, 40.0)
    _settle(page)
    req = page._compare_snapshot_req
    view_w = _view(page)[2]

    _pan(page, view_w * 0.1)             # well inside a 25% margin
    _settle(page)

    assert page._compare_snapshot_req == req


def test_panning_beyond_the_margin_recuts_the_crop(app):
    page = _page(app)
    _snapshot(page, 2000, 1500)
    _pan(page, 40.0)
    _settle(page)
    req = page._compare_snapshot_req
    crop_before = page._compare_snapshot["rect_l0"]
    view_w = _view(page)[2]

    _pan(page, view_w * 0.8)             # past the margin
    _settle(page)

    assert page._compare_snapshot_req == req + 1, "exactly one refill"
    view = _view(page)
    crop = page._compare_snapshot["rect_l0"]
    assert crop[0] > crop_before[0], "the crop did not follow the camera"
    # The crop covers the camera, with the margin around it.
    assert crop[0] <= view[0] + 1.0
    assert crop[0] + crop[2] >= view[0] + view[2] - 1.0
    # A margin of a quarter of the view on the near side. (The far side is
    # clamped at the edge of the slide here, so the width is not the full
    # 1.5x -- there is no more slide to ask for.)
    assert crop[0] == pytest.approx(view[0] - view[2] * 0.25, abs=4.0)
    # And the pixels arriving for a camera do not move it.
    assert _view(page)[0] == pytest.approx(view[0], abs=1e-6)


def test_a_refill_is_read_for_the_new_rectangle(app):
    page = _page(app)
    _snapshot(page, 2000, 1500)
    reads_before = len(page._explore_tab.stack.provider.reads)

    _pan(page, 600.0)
    _settle(page)

    _ch, level, ry0, ry1, rx0, rx1 = (
        page._explore_tab.stack.provider.reads[reads_before])
    y0, x0, h, w = page._compare_snapshot["region"]
    assert (level, ry0, rx0) == (page._compare_snapshot["level"], y0, x0)
    assert (ry1 - ry0, rx1 - rx0) == (h, w)


def test_zooming_past_a_level_boundary_recuts_at_the_new_level(app):
    """The level rule is the full image's own: the coarsest level whose
    downsample still does not exceed one slide pixel per screen pixel."""
    page = _page(app, level=2, scale_width=1024.0, view_w=SLIDE_W * 4)
    _snapshot(page, 2000, 1500)
    assert page._compare_snapshot["level"] == 2
    assert "downsampled ×4" in page._compare_level_lbl.text()

    for _ in range(12):                  # well past two level boundaries
        page._preview_vbs[0].wheelEvent(_WheelEvent())
    _settle(page)

    assert page._compare_snapshot["level"] == 0
    # The "downsampled xN" warning follows the level it describes.
    assert page._compare_level_lbl.isHidden() or (
        page._compare_level_lbl.text() == "")


def test_the_old_pixels_stay_up_while_the_new_crop_is_read(app, monkeypatch):
    """No blanking: someone judging a background must have something to
    look at for the whole of the read."""
    page = _page(app)
    _snapshot(page, 2000, 1500)
    before = [img.image for img in page._preview_imgs]
    compute = page._explore_tab.stack.controller.compute
    slow = compute.correct_array
    monkeypatch.setattr(compute, "correct_array",
                        lambda *a, **k: (time.sleep(0.4), slow(*a, **k))[1])

    _pan(page, 900.0)
    QtTest.QTest.qWait(300)              # debounce fired, read still running

    assert page._compare_snapshot_worker is not None
    assert page._compare_snapshot_worker.isRunning()
    assert [img.image is not None for img in page._preview_imgs] == [True] * 3
    assert all(a is b for a, b in
               zip(before, (img.image for img in page._preview_imgs)))
    page._compare_snapshot_worker.wait(5000)


def test_a_second_right_click_retakes_the_snapshot(app):
    """A right-click after the full image has been moved cuts the new
    view."""
    page = _page(app)
    first = dict(_snapshot(page, 2000, 1500))
    page._exit_compare_mode()
    page._explore_tab.stack.view.view_box.set_rect(3000.0, 2500.0,
                                                   1024.0, 768.0)

    second = _snapshot(page, 2000, 1500)

    assert second["snapshot_rect_l0"] != first["snapshot_rect_l0"]
    x0, y0, w, h = second["snapshot_rect_l0"]
    assert (x0 + w / 2.0, y0 + h / 2.0) == pytest.approx(
        (3000.0 + 512.0, 2500.0 + 384.0), abs=1.0)


# ── 3. a snapshot is not a computed result ───────────────────────────────

def test_a_snapshot_starts_no_correction_run(app, monkeypatch):
    started = _no_workers(monkeypatch)
    page = _page(app)

    _snapshot(page, 2000, 1500)

    assert started == []
    assert page._batch_worker is None


def test_a_snapshot_marks_nothing_computed(app, monkeypatch):
    _no_workers(monkeypatch)
    page = _page(app)
    before = page._channel_compute_state("CD3")

    _snapshot(page, 2000, 1500)

    assert page._preview_cache == {}
    assert page._computed_channels == set()
    assert page._computed_signatures == {}
    assert page._channel_compute_state("CD3") == before


def test_the_metrics_say_they_are_a_preview(app):
    page = _page(app)

    _snapshot(page, 2000, 1500)

    for label in (page._metrics_original, page._metrics_tophat,
                  page._metrics_cucim):
        assert "preview" in label.text(), label.text()
        assert "SNR" in label.text()


def test_a_production_run_refuses_the_snapshot(app, monkeypatch):
    page = _page(app)
    monkeypatch.setattr(page, "production_correction_busy",
                        lambda: "whole-slide correction (Save)")

    assert page._on_full_image_right_click(2000.0, 1500.0) is None
    assert page._compare_snapshot_worker is None
    assert page._last_payload is None


# ── 4. the correction is the viewport's own ──────────────────────────────

def test_the_two_methods_run_with_the_rows_current_parameters(app):
    page = _page(app)
    page._channel_params["CD3"] = {"tophat_radius": 21, "cucim_sigma": 9}

    _snapshot(page, 2000, 1500)

    # The CROP's calls. The whole-slide underlay runs through the same
    # compute object, with the same parameters scaled for the overview's
    # downsample instead, and is told apart by the array it ran on.
    assert _crop_calls(page) == [("tophat", 21), ("cucim", 9)]


def test_a_coarse_level_scales_the_parameter_the_way_a_tile_does(app):
    """`effective_param` is the controller's own scaling; a snapshot that
    used the level-0 number would remove a different background from the
    one the full image is showing."""
    page = _page(app, level=2)
    page._channel_params["CD3"] = {"tophat_radius": 20, "cucim_sigma": 8}

    _snapshot(page, 2000, 1500)

    ds = page._explore_tab.stack.provider.level_downsample(2)
    assert _crop_calls(page) == [
        ("tophat", effective_param(20, 2, ds)),
        ("cucim", effective_param(8, 2, ds))]


def test_the_correction_is_read_with_the_methods_own_halo(app):
    """Same halo as `CorrectionCompute.compute` uses per tile -- 2*radius
    for top-hat -- so the interior of the crop is the tile's own pixels."""
    from block01.viewer.correction_compute import halo_for

    page = _page(app, scale_width=1024.0, view_w=1024)
    page._channel_params["CD3"] = {"tophat_radius": 21, "cucim_sigma": 9}

    _snapshot(page, 2000, 1500)

    rect_h = page._compare_snapshot["region"][2]
    heights = [r[3] - r[2] for r in page._explore_tab.stack.provider.reads]
    assert rect_h in heights, "the raw crop was never read at its own size"
    assert rect_h + 2 * halo_for("tophat", 21) in heights, (
        f"no read grown by the top-hat halo: {heights}")


def test_the_original_panel_is_the_raw_crop(app):
    page = _page(app, scale_width=1024.0, view_w=1024)

    payload = _snapshot(page, 2000, 1500)

    # The fake compute multiplies; Original must not have been through it.
    assert np.allclose(payload["tophat_raw"],
                       payload["original_raw"] * 2.0)
    assert np.allclose(payload["cucim_raw"],
                       payload["original_raw"] * 3.0)


# ── 5. the two exclusive modes ───────────────────────────────────────────

def test_the_right_click_replaces_the_image_with_the_panels(app):
    page = _page(app)
    full_page = page._view_area.widget(page._VIEW_FULL)
    assert page._compare_mode() is False
    assert full_page.isVisible() and not page._compare_strip.isVisible()

    _snapshot(page, 2000, 1500)

    assert page._compare_mode() is True
    assert page._compare_strip.isVisible(), "the panels are not on screen"
    assert not full_page.isVisible(), "both views are sharing the area"


def test_a_right_click_in_compare_mode_goes_back_to_the_image(app):
    page = _page(app)
    _snapshot(page, 2000, 1500)

    _right_click(page._preview_gv.viewport())

    assert page._compare_mode() is False
    assert page._last_payload is not None, "the snapshot is kept"


def test_the_way_back_puts_the_panels_camera_on_the_full_image(app):
    """Someone who zoomed to a cell in the panels comes back to that cell,
    not to the landmark they went in at."""
    page = _page(app)
    _snapshot(page, 2000, 1500)
    for _ in range(4):
        page._preview_vbs[0].wheelEvent(_WheelEvent())
    x0, y0, w, h = _view(page)

    page._on_compare_escape()

    rects = page._explore_tab.stack.controller.view_rects
    assert rects, "the full image was not moved"
    # The CENTRE and the SCALE come back, not the rectangle: the full image
    # is a different shape from a panel, so it covers more x at the same
    # magnification. The panel's own axis is the y one (`_compare_camera`
    # reads the scale off it), and the widths are the two widgets'.
    fx0, fy0, fw, fh = rects[-1]
    vb = page._explore_tab.stack.view.view_box
    ph_px = page._preview_vbs[0].rect().height()
    assert (fx0 + fw / 2.0, fy0 + fh / 2.0) == pytest.approx(
        (x0 + w / 2.0, y0 + h / 2.0), rel=1e-9)
    assert vb.width() / fw == pytest.approx(ph_px / h, rel=1e-9)
    assert vb.height() / fh == pytest.approx(ph_px / h, rel=1e-9)


def test_toggling_the_modes_in_place_moves_nothing(app):
    """The bug this pins: with the mouse held still, every in-and-out cycle
    used to move the view a little further in one direction.

    Entering recentred the panels on the clicked point, and coming back
    adopted that recentred rectangle -- so the same screen pixel named a new
    slide point each cycle and the drift compounded. Centre and scale are
    carried instead, and the rectangle after N cycles is the rectangle
    before them, to float noise.
    """
    page = _page(app)
    vb = page._explore_tab.stack.view.view_box
    vb.set_rect(19000.0, 12000.0, 1024.0, 768.0)
    before = vb.viewRange()
    panels_before = None

    for _ in range(5):
        _snapshot(page, 2000, 1500)
        camera = page._compare_camera()
        if panels_before is None:
            panels_before = camera
        assert camera == pytest.approx(panels_before, rel=1e-9)
        page._exit_compare_mode()

    (bx0, bx1), (by0, by1) = before
    (ax0, ax1), (ay0, ay1) = vb.viewRange()
    assert (ax0, ax1, ay0, ay1) == pytest.approx((bx0, bx1, by0, by1),
                                                 rel=1e-6)


def test_the_panels_open_on_the_full_images_centre_and_scale(app):
    """Less width -- a panel is a third of the area -- at the same
    magnification and around the same point."""
    page = _page(app, scale_width=1024.0, view_w=1024)   # 1 screen px / px
    vb = page._explore_tab.stack.view.view_box
    vb.set_rect(2000.0, 1500.0, 1024.0, 768.0)

    _snapshot(page, 0, 0)

    cx, cy, scale = page._compare_camera()
    assert (cx, cy) == pytest.approx((2512.0, 1884.0), rel=1e-9)
    assert scale == pytest.approx(1.0, rel=1e-9)
    (px0, px1), _yr = page._preview_vbs[0].viewRange()
    assert px1 - px0 < 1024.0, "a panel is not narrower than the image"


def test_flipping_modes_without_a_snapshot_moves_no_camera(app):
    page = _page(app)

    page._set_compare_mode(True)
    page._exit_compare_mode()

    assert page._explore_tab.stack.controller.view_rects == []


def test_escape_goes_back_to_the_image_too(app):
    """A real key event, through the page's own handlers: there is no
    QShortcut object, because a shortcut connected to a bound method of the
    page closes a reference cycle the between-test gc walks into."""
    page = _page(app)
    _snapshot(page, 2000, 1500)
    assert not hasattr(page, "_compare_escape_shortcut")

    QtWidgets.QApplication.sendEvent(
        page._preview_gv.viewport(),
        QtGui.QKeyEvent(QtCore.QEvent.KeyPress, QtCore.Qt.Key_Escape,
                        QtCore.Qt.NoModifier))

    assert page._compare_mode() is False

    _snapshot(page, 2000, 1500)
    page.keyPressEvent(QtGui.QKeyEvent(QtCore.QEvent.KeyPress,
                                       QtCore.Qt.Key_Escape,
                                       QtCore.Qt.NoModifier))

    assert page._compare_mode() is False


def test_the_way_back_asks_the_viewer_for_nothing(app):
    """Returning re-SHOWS a hidden page; it does not rebuild, re-select or
    reload anything."""
    page = _page(app)
    _snapshot(page, 2000, 1500)
    tab = page._explore_tab
    before = list(tab.calls)

    page._on_compare_escape()

    assert tab.calls == before


def test_the_toggle_button_and_the_splitter_are_gone(app):
    page = _page(app)
    for gone in ("_btn_show_compare", "_preview_split",
                 "_compare_strip_visible", "_set_compare_strip_visible",
                 "_on_compare_strip_toggled"):
        assert not hasattr(page, gone), gone


def test_the_panels_get_the_whole_area(app):
    """The crop is cut for the panel size AFTER the switch has been laid
    out: the panels are the viewing area now, not a strip under it."""
    page = _page(app)
    area_h = page._view_area.height()

    _snapshot(page, 2000, 1500)

    assert page._compare_strip.height() == area_h
    assert page._compare_panel_px()[1] > area_h / 2


def test_the_downsampled_label_and_save_as_patch_stay_in_compare_mode(app):
    page = _page(app, level=2)

    _snapshot(page, 2000, 1500)

    assert page._compare_level_lbl.isVisible()
    assert page._btn_snapshot_patch.isVisible()
    assert page._btn_snapshot_patch.isEnabled()


def test_a_fine_level_says_nothing_about_downsampling(app):
    page = _page(app, level=0)

    _snapshot(page, 2000, 1500)

    assert page._compare_level_lbl.isHidden() or (
        page._compare_level_lbl.text() == "")


def test_a_coarse_level_says_how_far_it_is_downsampled(app):
    """The factor comes from the provider's level shapes, not 2**level."""
    page = _page(app, level=3)

    _snapshot(page, 2000, 1500)

    assert "×8" in page._compare_level_lbl.text()
    assert "downsampled" in page._compare_level_lbl.text()


# ── 6. save as patch ─────────────────────────────────────────────────────

def test_save_as_patch_appends_the_level_zero_rectangle(app):
    page = _page(app)
    payload = _snapshot(page, 2000, 1500)
    x0, y0, w, h = (int(v) for v in payload["snapshot_rect_l0"])

    coords = page._save_snapshot_as_patch()

    assert coords == (y0, y0 + h, x0, x0 + w)
    assert page.patches[-1] == coords


def test_save_as_patch_is_off_until_there_is_a_snapshot(app):
    page = _page(app)
    assert page._btn_snapshot_patch.isEnabled() is False
    assert page._save_snapshot_as_patch() is None
    assert page.patches == []

    _snapshot(page, 2000, 1500)

    assert page._btn_snapshot_patch.isEnabled() is True


def test_save_as_patch_computes_nothing(app, monkeypatch):
    started = _no_workers(monkeypatch)
    page = _page(app)
    _snapshot(page, 2000, 1500)

    page._save_snapshot_as_patch()

    assert started == []
    assert page._preview_cache == {}
    assert page._computed_channels == set()


# ── 7. the DAPI overlay follows its checkbox ─────────────────────────────

def test_the_dapi_layer_is_off_until_its_checkbox_is_on(app):
    page = _page(app)

    payload = _snapshot(page, 2000, 1500)

    assert payload.get("nucleus_raw") is None
    assert all(item is None or not item.isVisible()
               for item in page._preview_nuc_imgs)


def test_turning_dapi_on_afterwards_retakes_the_snapshot(app):
    """A snapshot cut with the layer off does not carry the nucleus
    channel, so the switch has to fetch it -- at the same point."""
    page = _page(app)
    first = _snapshot(page, 2000, 1500)
    assert first.get("nucleus_raw") is None

    page._btn_show_nucleus.setChecked(True)
    worker = page._compare_snapshot_worker
    assert worker is not None, "the switch did not retake the snapshot"
    assert worker.wait(5000)
    QtTest.QTest.qWait(50)

    payload = page._last_payload
    assert payload["nucleus_raw"] is not None
    assert payload["snapshot_rect_l0"] == first["snapshot_rect_l0"]


def test_the_dapi_checkbox_puts_the_overlay_in_the_panels(app):
    page = _page(app)
    page._btn_show_nucleus.setChecked(True)

    payload = _snapshot(page, 2000, 1500)

    assert payload["nucleus_raw"] is not None
    # The fake provider offsets DAPI by 1000, so the overlay is that
    # channel's pixels and not a second copy of the marker's.
    assert np.allclose(payload["nucleus_raw"],
                       payload["original_raw"] + 1000.0)
    assert all(item is not None and item.isVisible()
               for item in page._preview_nuc_imgs)


# ── 8. the panels follow the row the page is on ──────────────────────────
#
# A snapshot is a snapshot OF something, and while the panels are the view
# that something can change under them: another marker row is clicked, or the
# row's radius/sigma is edited. Both used to leave the three images showing
# the previous channel's pixels while the header, the metrics, the Intensity
# window and the hidden full image had all moved on.

def _wait_for_retake(page, timeout=5000):
    """Wait out the worker a retake started and return the new payload."""
    worker = page._compare_snapshot_worker
    assert worker is not None, "no snapshot was retaken"
    assert worker.wait(timeout), "the snapshot worker never finished"
    deadline = QtCore.QElapsedTimer()
    deadline.start()
    while page._last_payload is None or not page._last_payload.get("snapshot"):
        QtTest.QTest.qWait(10)
        assert deadline.elapsed() < timeout, "the snapshot never arrived"
    QtTest.QTest.qWait(30)
    return page._last_payload


def test_a_row_change_in_compare_mode_recuts_the_same_frame(app):
    page = _page(app)
    first = dict(_snapshot(page, 2000, 1500))
    assert page._compare_snapshot["channel"] == "CD3"
    region = page._compare_snapshot["region"]
    reads_before = len(page._explore_tab.stack.provider.reads)

    page._last_payload = None
    page._on_channel_selected_by_id("CD20")
    payload = _wait_for_retake(page)

    assert page.current_channel == "CD20"
    assert page._compare_snapshot["channel"] == "CD20", (
        "the panels kept the old channel")
    # SAME place, SAME level, SAME scale: only the subject changed.
    assert payload["snapshot_rect_l0"] == first["snapshot_rect_l0"]
    assert page._compare_snapshot["region"] == region
    assert page._compare_snapshot["level"] == first["snapshot_level"]
    # It really re-read, for the new channel.
    new_reads = page._explore_tab.stack.provider.reads[reads_before:]
    assert new_reads, "nothing was read for the new channel"
    assert {r[0] for r in new_reads} == {"CD20"}


def test_the_dapi_row_does_not_retake_the_snapshot(app):
    """DAPI is a reference channel: selecting its row re-points the
    Intensity window and nothing else. The overlay's own checkbox is what
    changes what the panels carry, and it still does."""
    page = _page(app)
    _snapshot(page, 2000, 1500)
    req_before = page._compare_snapshot_req

    page._on_channel_selected_by_id("DAPI")

    assert page._compare_snapshot_req == req_before, "DAPI retook the snapshot"
    assert page._compare_snapshot["channel"] == "CD3"
    assert page.current_channel == "CD3"


def test_a_row_change_outside_compare_mode_takes_no_snapshot(app):
    """The full image is the view; there are no panels on screen to keep in
    step, and dragging the user into compare mode would be worse than
    stale pixels."""
    page = _page(app)
    req_before = page._compare_snapshot_req

    page._on_channel_selected_by_id("CD20")

    assert page._compare_mode() is False
    assert page._compare_snapshot_req == req_before


def test_a_parameter_edit_recuts_the_panels(app):
    """The TopHat and cuCIM panels PREVIEW the row's current numbers."""
    page = _page(app)
    _snapshot(page, 2000, 1500)
    calls_before = len(page._explore_tab.stack.controller.compute.calls)

    page._last_payload = None
    page._dec_sigma.setValue(page._dec_sigma.value() + 7)
    _wait_for_retake(page)

    calls = page._explore_tab.stack.controller.compute.calls[calls_before:]
    sigmas = [param for method, param, _shape in calls if method == "cucim"]
    assert sigmas == [page._dec_sigma.value()]


# ── 9. the panels never show background ──────────────────────────────────
#
# A panel used to hold exactly one image, so a fast zoom-out shrank the crop
# into the widget's black background and then, when the refill landed,
# snapped a full-size image back in: a flash of nothing followed by a jump,
# once per zoom-out. The full image never does that -- not because it is
# faster, but because it keeps coarser layers underneath. The panels now
# keep the same two: the whole slide at overview resolution under
# everything, and the crop being replaced under the one being read.


def _underlay_ready(page, timeout=5000):
    """Wait for the corrected underlays as well as the crop."""
    deadline = QtCore.QElapsedTimer()
    deadline.start()
    while len(page._compare_underlay_cache) < 3:
        worker = page._compare_underlay_worker
        if worker is not None:
            worker.wait(timeout)
        QtTest.QTest.qWait(10)
        assert deadline.elapsed() < timeout, "the underlay never arrived"
    QtTest.QTest.qWait(30)
    return page._compare_underlay_cache


def _item_rects(page, idx):
    """Every visible image item's level-0 rectangle in panel `idx`."""
    out = []
    for slots in (page._compare_under_imgs, page._compare_under_nuc_imgs,
                  page._preview_prev_imgs, page._preview_prev_nuc_imgs,
                  page._preview_imgs, page._preview_nuc_imgs):
        item = slots[idx]
        rect = getattr(item, "_l0_rect", None)
        if item is None or rect is None or not item.isVisible():
            continue
        if getattr(item, "image", None) is None:
            continue
        out.append((rect.x(), rect.y(), rect.width(), rect.height()))
    return out


def _covered(page, idx):
    """True when SOME image item covers the whole of panel `idx`'s view.

    The panels only ever hold axis-aligned rectangles that share a corner
    with the slide or with each other, so "no gap" is "one of them contains
    the view" -- which is the shape of the guarantee anyway: the underlay
    covers the entire slide.
    """
    (vx0, vx1), (vy0, vy1) = page._preview_vbs[idx].viewRange()
    for x, y, w, h in _item_rects(page, idx):
        if (x <= vx0 + 1e-6 and y <= vy0 + 1e-6
                and x + w >= vx1 - 1e-6 and y + h >= vy1 - 1e-6):
            return True
    return False


def test_every_panel_gets_the_whole_slide_underneath(app):
    page = _page(app)

    _snapshot(page, 2000, 1500)
    _underlay_ready(page)

    for idx in range(3):
        item = page._compare_under_imgs[idx]
        assert item is not None, f"panel {idx} has no underlay"
        assert item.isVisible()
        assert item.image is not None
        assert item.image.shape == LOWRES_SHAPE
        rect = item._l0_rect
        assert (rect.x(), rect.y(), rect.width(), rect.height()) == (
            0.0, 0.0, float(SLIDE_W), float(SLIDE_H)), (
            "the underlay is not placed on the whole slide")


def test_the_underlay_is_below_the_crop(app):
    page = _page(app)

    _snapshot(page, 2000, 1500)

    for idx in range(3):
        assert (page._compare_under_imgs[idx].zValue()
                < page._preview_imgs[idx].zValue())


def test_the_corrected_underlays_are_the_low_res_array_corrected(app):
    """The same correction function the crop is corrected with, on the same
    array the Tissue Preview is drawn from, with the parameter scaled for
    the overview's downsample."""
    page = _page(app)

    _snapshot(page, 2000, 1500)
    cache = _underlay_ready(page)

    radius, sigma = page._effective_correction_params("CD3")
    ds = float(_LowresLoader.OVERVIEW_DS)
    eff = {"tophat": effective_param(radius, 1, ds),
           "cucim": effective_param(sigma, 1, ds)}
    calls = page._explore_tab.stack.controller.compute.calls
    for method in ("tophat", "cucim"):
        assert ("CD3", method, eff[method]) in cache
        assert (method, eff[method], LOWRES_SHAPE) in calls, (
            f"{method} was not run over the whole-slide array")
    # And it really is the correction of that array: the fake compute
    # doubles for tophat and triples for cucim.
    base = cache[("CD3", "original", 0)]
    assert np.allclose(cache[("CD3", "tophat", eff["tophat"])], base * 2.0)
    assert np.allclose(cache[("CD3", "cucim", eff["cucim"])], base * 3.0)


def test_the_underlay_costs_one_read_and_it_is_the_page_s_own(app):
    """No new IO: the array is the one `_slide_lowres_array` already
    holds."""
    page = _page(app)

    _snapshot(page, 2000, 1500)
    _underlay_ready(page)

    assert ([r for r in page.loader.lowres_reads if r[0] == "CD3"]
            == [("CD3", _LowresLoader.OVERVIEW_DS)])
    assert (page._compare_underlay_cache[("CD3", "original", 0)]
            is page._slide_lowres_array("CD3"))


def test_a_zoom_out_never_uncovers_the_background(app):
    """The measurement the bug was reported as: shrink the crop hard, and
    look at every frame BEFORE the refill lands."""
    # Opened deep in: 4 screen pixels per slide pixel, in the middle of the
    # slide, so a dozen 1.4x steps out still ask for slide rather than for
    # the void beyond its edge -- which no layer can cover and which is not
    # what the bug was about.
    page = _page(app, scale_width=1024.0, view_w=256)
    page._explore_tab.stack.view.view_box.set_rect(1920.0, 1952.0,
                                                   256.0, 192.0)
    _snapshot(page, 2000, 1500)
    _underlay_ready(page)

    for step in range(11):
        (x0, x1), (y0, y1) = page._preview_vbs[0].viewRange()
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        w, h = (x1 - x0) * 1.4, (y1 - y0) * 1.4
        page._preview_vbs[0].setRange(xRange=(cx - w / 2, cx + w / 2),
                                      yRange=(cy - h / 2, cy + h / 2),
                                      padding=0)
        for idx in range(3):
            assert _covered(page, idx), (
                f"panel {idx} showed background at zoom-out step {step}")


def test_the_previous_crop_stays_up_until_the_new_one_lands(app, monkeypatch):
    """The viewer's "switch levels without clearing", in three panels."""
    page = _page(app)
    _snapshot(page, 2000, 1500)
    before = [img.image for img in page._preview_imgs]

    real_run = sp.CompareSnapshotWorker.run
    monkeypatch.setattr(sp.CompareSnapshotWorker, "run",
                        lambda self: (time.sleep(0.6), real_run(self)))
    _pan(page, 600.0)
    QtTest.QTest.qWait(200)

    for idx in range(3):
        prev = page._preview_prev_imgs[idx]
        assert prev is not None and prev.isVisible(), (
            f"panel {idx} dropped the crop it was showing")
        assert np.array_equal(prev.image, before[idx])
        assert prev.zValue() < page._preview_imgs[idx].zValue()
        assert prev.zValue() > page._compare_under_imgs[idx].zValue()

    _settle(page)

    assert all(not item.isVisible() for item in page._preview_prev_imgs)
    assert all(img.image.shape != b.shape or not np.array_equal(img.image, b)
               for img, b in zip(page._preview_imgs, before))


def test_a_parameter_change_recomputes_the_corrected_underlay_once(app):
    page = _page(app)
    page._channel_params["CD3"] = {"tophat_radius": 21, "cucim_sigma": 9}
    _snapshot(page, 2000, 1500)
    _underlay_ready(page)
    before = _underlay_calls(page)
    assert len(before) == 2, before

    page._channel_params["CD3"]["cucim_sigma"] = 200
    page._retake_compare_snapshot()
    _wait_for_retake(page)
    _underlay_ready(page)

    added = _underlay_calls(page)[len(before):]
    assert len(added) == 1, f"the underlay was recomputed {len(added)} times"
    ds = float(_LowresLoader.OVERVIEW_DS)
    assert added == [("cucim", effective_param(200, 1, ds))]
    # And nothing at all for a retake that changes no parameter: the floor
    # is a picture of the channel and its numbers, not of the camera.
    page._retake_compare_snapshot()
    _wait_for_retake(page)
    _underlay_ready(page)
    assert _underlay_calls(page)[len(before):] == added


def test_a_channel_change_gives_the_new_channel_its_own_floor(app):
    page = _page(app)
    _snapshot(page, 2000, 1500)
    _underlay_ready(page)

    page._last_payload = None
    page._on_channel_selected_by_id("CD20")
    _wait_for_retake(page)
    _underlay_ready(page)

    assert page._compare_underlay_keys["original"][0] == "CD20"
    assert np.allclose(page._compare_under_imgs[0].image,
                       page._slide_lowres_array("CD20"))


def test_zooming_out_across_a_level_refills_without_waiting(app):
    """The settle is for the expensive direction. A coarser level is fewer
    pixels for more slide, and it is what the user asked to see."""
    page = _page(app, level=0, scale_width=1024.0, view_w=1024)
    _snapshot(page, 2000, 1500)
    req = page._compare_snapshot_req
    assert page._compare_snapshot["level"] == 0

    (x0, x1), (y0, y1) = page._preview_vbs[0].viewRange()
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    w, h = (x1 - x0) * 8.0, (y1 - y0) * 8.0
    page._preview_vbs[0].setRange(xRange=(cx - w / 2, cx + w / 2),
                                  yRange=(cy - h / 2, cy + h / 2), padding=0)

    # No wait at all: the request is in flight already.
    assert page._compare_snapshot_req == req + 1
    assert page._compare_snapshot["level"] > 0


def test_zooming_in_still_waits_out_the_settle(app):
    page = _page(app, level=2, scale_width=1024.0, view_w=SLIDE_W * 4)
    _snapshot(page, 2000, 1500)
    req = page._compare_snapshot_req

    (x0, x1), (y0, y1) = page._preview_vbs[0].viewRange()
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    w, h = (x1 - x0) / 8.0, (y1 - y0) / 8.0
    page._preview_vbs[0].setRange(xRange=(cx - w / 2, cx + w / 2),
                                  yRange=(cy - h / 2, cy + h / 2), padding=0)

    assert page._compare_snapshot_req == req, "the expensive read did not wait"
    _settle(page)
    assert page._compare_snapshot_req == req + 1


def test_the_settle_is_a_tenth_of_a_second(app):
    assert sp.Step0Page._COMPARE_SETTLE_MS == 100


def test_the_dapi_underlay_follows_the_switch(app):
    """One switch, one picture -- on the floor as well as on the crop."""
    page = _page(app)
    page._btn_show_nucleus.setChecked(True)
    _snapshot(page, 2000, 1500)
    _underlay_ready(page)

    for idx in range(3):
        nuc = page._compare_under_nuc_imgs[idx]
        assert nuc is not None and nuc.isVisible(), f"panel {idx}"
        assert np.allclose(nuc.image, page._slide_lowres_array("DAPI"))
        assert nuc.paintMode == QtGui.QPainter.CompositionMode_Plus
        assert nuc.zValue() < page._preview_imgs[idx].zValue()

    page._btn_show_nucleus.setChecked(False)
    _wait_for_retake(page)

    assert all(item is None or not item.isVisible()
               for item in page._compare_under_nuc_imgs)
