"""Compare mode is a VIRTUAL PATCH: one region, computed once, shown three
ways.

A right-click on the full image at slide point P defines the region R -- the
full image's own visible rectangle, re-centred on P and clamped to the slide
-- and a worker produces the three arrays for it at the finest pyramid level
whose crop of R is within 2048 px. The panels then hold those arrays and
nothing else. Nothing beyond R is ever fetched, no level is ever switched,
and no frame is ever replaced as a whole.

The module pins the four things that has to be true of:

* the RULE -- what R is, what L is, and that neither depends on the panels;
* the FEED -- one worker, three arrays, a placeholder until they land, and
  the region fitted exactly once;
* the RECOMPUTE -- a channel row change or a parameter edit recomputes the
  same R and keeps the camera;
* the TOGGLE -- entering and leaving must not move the full image by a
  pixel, which is checked against PIXELS: two `grab()`s of the viewer,
  compared byte for byte.

Own module: the page-heavy Step0 suites crash pyqtgraph offscreen when
combined with the background-correction module in one process.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtCore, QtTest  # noqa: E402
from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtGui import QKeyEvent, QMouseEvent  # noqa: E402

from block01.ui.step0 import step0_page as sp  # noqa: E402

from test_step0_background_correction_tab import _GpuPathLoader, app  # noqa: E402,F401


SLIDE_H, SLIDE_W = 29000, 31000


# ── a slide behind the page, without a slide ─────────────────────────────

class _Provider:
    """The three questions the region rule and the worker ask a provider."""

    num_levels = 5

    def __init__(self):
        self.reads = []

    def level_downsample(self, level):
        return float(1 << int(level))

    def level_shape(self, level):
        ds = 1 << int(level)
        return (SLIDE_H // ds, SLIDE_W // ds)

    def read_region(self, channel, level, y0, y1, x0, x1):
        """Clamped to the level, exactly as the real provider does, and it
        reports the ORIGIN it actually read from."""
        lh, lw = self.level_shape(level)
        cy0, cy1 = max(0, int(y0)), min(lh, int(y1))
        cx0, cx1 = max(0, int(x0)), min(lw, int(x1))
        self.reads.append((channel, int(level), cy0, cy1, cx0, cx1))
        h, w = max(1, cy1 - cy0), max(1, cx1 - cx0)
        arr = np.full((h, w), 100.0 if channel == "CD3" else 40.0, np.float32)
        return arr, (cy0, cx0)


class _Compute:
    def __init__(self):
        self.calls = []

    def correct_array(self, arr, method, param):
        self.calls.append((method, int(param), arr.shape))
        return arr * (0.5 if method == "tophat" else 0.8)


class _Controller:
    """The one thing the page asks the full image's controller to do to its
    camera.

    `set_view_rect_l0` here is `ExploreController.set_view_rect_l0`'s camera
    move verbatim -- both axes set independently, padding 0, nothing
    re-fitted (`test_the_real_controller_moves_the_camera_the_same_way`
    holds the two together).
    """

    def __init__(self, view, provider, compute):
        self.view = view
        self.provider = provider
        self.compute = compute
        self.channel, self.method, self.params = "CD3", None, ()
        self.suspended = False
        self._current_bbox = None
        self.rects = []

    def set_view_rect_l0(self, x0, y0, w, h):
        self.rects.append((x0, y0, w, h))
        self.view.view_box.setRange(xRange=(x0, x0 + w), yRange=(y0, y0 + h),
                                    padding=0)

    def set_selection(self, **_kw):
        pass

    def set_tint(self, *_a, **_kw):
        pass

    def set_marker_visible(self, *_a, **_kw):
        pass


class _Stack:
    def __init__(self, view, provider, compute):
        self.view = view
        self.provider = provider
        self.compute = compute
        self.controller = _Controller(view, provider, compute)
        self.scheduler = None
        self.caches = ()
        self.overlay = None
        self.torn_down = False

    def teardown(self, *, wait_for_floor=False):
        self.torn_down = True


class _Tab:
    def __init__(self, stack):
        self.stack = stack

    def teardown(self, *_a, **_kw):
        pass

    def resume_from_production(self):
        pass


def _page(app, *, view_rect=(10000.0, 8000.0, 4000.0, 2000.0)):
    """A page whose full image is a REAL `ExploreView` parked on `view_rect`.

    Real, because the toggle test compares grabbed pixels: a stand-in widget
    would prove nothing about the aspect-locked ViewBox that is the actual
    risk.
    """
    from block01.viewer.explore_view import ExploreView

    page = sp.Step0Page()
    page.loader = _GpuPathLoader()
    page.patches = []
    page.current_patch_idx = 0
    page.nucleus_channel = "DAPI"
    page._rebuild_channel_list()
    page.current_channel = "CD3"

    view = ExploreView()
    provider, compute = _Provider(), _Compute()
    stack = _Stack(view, provider, compute)
    page._explore_tab = _Tab(stack)
    # The viewer widget goes where the real one goes: into the full-image
    # page of the viewing-area stack, so both modes are pages of one stack
    # and the toolbar sits above both.
    full_page = page._view_area.widget(page._VIEW_FULL)
    full_page.layout().addWidget(view)

    # Something to LOOK at, so a grab of an empty scene cannot pass the
    # pixel-identity test by being uniformly blank.
    rng = np.random.default_rng(11)
    view.overview_item.setImage(
        rng.integers(0, 255, size=(290, 310), dtype=np.uint8))
    view.overview_item.setRect(QtCore.QRectF(0, 0, SLIDE_W, SLIDE_H))
    view.overview_item.setVisible(True)

    page.resize(1800, 1100)
    page.show()
    QtTest.QTest.qWait(60)
    x, y, w, h = view_rect
    view.view_box.setRange(xRange=(x, x + w), yRange=(y, y + h), padding=0)
    QtTest.QTest.qWait(30)
    return page


def _payload(h=64, w=64, value=500.0):
    m = np.full((h, w), value, np.float32)
    metrics = {"snr": 4.0, "bg_cv": 0.25}
    return {"original_raw": m, "tophat_raw": m * 0.5, "cucim_raw": m * 0.8,
            "original_metrics": metrics, "tophat_metrics": metrics,
            "cucim_metrics": metrics, "nucleus_raw": None}


def _enter(page, px, py, payload=None):
    """Right-click at P and hand the panels their arrays, without a thread."""
    page._on_full_image_right_click(px, py)
    QtTest.QTest.qWait(10)
    page._stop_compare_worker()          # whatever it started, drop it
    page._compare_req_id += 0
    page._apply_compare_payload(payload if payload is not None else _payload())
    QtTest.QTest.qWait(20)
    return page._compare_region


def _ranges(page):
    return [vb.viewRange() for vb in page._preview_vbs]


# ── 1. the rule: what R is, and what L is ────────────────────────────────

def test_the_region_is_the_full_images_rectangle_recentred_on_the_click(app):
    page = _page(app, view_rect=(10000.0, 8000.0, 4000.0, 2000.0))
    rect = page._full_image_view_rect_l0()

    region = page._compare_region_for(20000.0, 15000.0)

    x, y, w, h = region
    # Same SIZE as what the user is already looking at -- "compare this
    # spot" means this neighbourhood, not a third of it.
    assert (w, h) == pytest.approx((rect[2], rect[3]))
    assert (x + w / 2.0, y + h / 2.0) == pytest.approx((20000.0, 15000.0))


def test_the_region_is_clamped_by_sliding_not_by_shrinking(app):
    """A click near an edge still gets a full-size region: it is pushed back
    inside the slide, not cut down."""
    page = _page(app, view_rect=(0.0, 0.0, 4000.0, 2000.0))
    rect = page._full_image_view_rect_l0()

    x, y, w, h = page._compare_region_for(10.0, 10.0)

    assert (x, y) == pytest.approx((0.0, 0.0))
    assert (w, h) == pytest.approx((rect[2], rect[3]))

    x, y, w, h = page._compare_region_for(SLIDE_W - 5.0, SLIDE_H - 5.0)
    assert x + w == pytest.approx(SLIDE_W)
    assert y + h == pytest.approx(SLIDE_H)


def test_a_region_bigger_than_the_slide_is_cut_down_to_it(app):
    page = _page(app, view_rect=(-5000.0, -5000.0, 60000.0, 40000.0))

    x, y, w, h = page._compare_region_for(SLIDE_W / 2, SLIDE_H / 2)

    assert (x, y) == pytest.approx((0.0, 0.0))
    assert (w, h) == pytest.approx((SLIDE_W, SLIDE_H))


def test_the_level_is_the_finest_one_the_region_fits_at(app):
    page = _page(app)

    # A level-0 view: R already fits, so L is 0 and the panels get level-0
    # pixels.
    assert page._compare_level_for((0.0, 0.0, 2048.0, 1000.0)) == 0
    assert page._compare_level_for((0.0, 0.0, 2049.0, 1000.0)) == 1
    assert page._compare_level_for((0.0, 0.0, 4096.0, 1000.0)) == 1
    assert page._compare_level_for((0.0, 0.0, 4097.0, 1000.0)) == 2
    # The whole slide: 31000 / 2**4 = 1937 <= 2048.
    assert page._compare_level_for((0.0, 0.0, SLIDE_W, SLIDE_H)) == 4
    # The longer side decides, whichever it is.
    assert page._compare_level_for((0.0, 0.0, 100.0, 5000.0)) == 2


def test_the_crop_is_the_region_in_the_levels_own_pixels(app):
    page = _page(app)

    y0, x0, h, w = page._compare_crop_for((4096.0, 2048.0, 8192.0, 4096.0), 2)

    assert (y0, x0) == (512, 1024)          # 2048/4, 4096/4
    assert (h, w) == (1024, 2048)           # 4096/4, 8192/4


def test_the_rule_never_consults_the_panels(app):
    """The region is solved from the FULL IMAGE alone, which is why a
    right-click works on the very first entry, before the compare page has
    ever been laid out."""
    page = _page(app)
    # Never shown: the compare page is the hidden half of the stack, so its
    # ViewBoxes have whatever geometry a widget that was never laid out has.
    assert page._compare_mode() is False
    cold = page._compare_region_for(20000.0, 15000.0)
    cold_level = page._compare_level_for(cold)
    cold_sizes = [(vb.width(), vb.height()) for vb in page._preview_vbs]

    # Now show them, and at a completely different size.
    page._set_compare_mode(True)
    QtTest.QTest.qWait(40)
    page.resize(700, 1400)
    QtTest.QTest.qWait(60)
    assert [(vb.width(), vb.height())
            for vb in page._preview_vbs] != cold_sizes, "the panels did not move"

    assert page._compare_region_for(20000.0, 15000.0) == pytest.approx(cold)
    assert page._compare_level_for(cold) == cold_level


# ── 2. the feed: one worker, three arrays ────────────────────────────────

def test_the_worker_produces_the_three_arrays_at_the_level(app):
    """Original is the raw crop; TopHat and cuCIM are that crop corrected
    with the LEVEL-SCALED parameter and the method's own halo, cropped back
    -- the same path the full image's own preview takes."""
    from block01.viewer.tile_types import effective_param
    from block01.viewer.correction_compute import halo_for

    provider, compute = _Provider(), _Compute()
    got = {}
    worker = sp.CompareRegionWorker(
        7, provider, compute, "CD3", "DAPI", 2, 512, 1024, 300, 400, 25, 50)
    worker.done.connect(lambda rid, p: got.update({"id": rid, "p": p}))
    worker.run()                       # on this thread: no signal marshalling

    payload = got["p"]
    assert got["id"] == 7
    for key in ("original_raw", "tophat_raw", "cucim_raw", "nucleus_raw"):
        assert payload[key].shape == (300, 400), key
    for key in ("original_metrics", "tophat_metrics", "cucim_metrics"):
        assert set(payload[key]) >= {"snr", "bg_cv"}
    # TopHat is half the original here, cuCIM 0.8 of it: the panels show the
    # results as computed, never renormalised.
    assert payload["tophat_raw"][0, 0] == pytest.approx(50.0)
    assert payload["cucim_raw"][0, 0] == pytest.approx(80.0)

    # The parameter really was level-scaled, and the read really was haloed.
    ds = provider.level_downsample(2)
    assert compute.calls[0][:2] == ("tophat", effective_param(25, 2, ds))
    assert compute.calls[1][:2] == ("cucim", effective_param(50, 2, ds))
    halo = halo_for("tophat", effective_param(25, 2, ds))
    assert any(r[2] == 512 - halo and r[4] == 1024 - halo
               for r in provider.reads), provider.reads


def test_a_right_click_starts_one_worker_for_the_region(app):
    page = _page(app)
    page._on_full_image_right_click(20000.0, 15000.0)

    assert page._compare_mode() is True
    assert page._compare_region is not None
    assert page._compare_worker is not None
    assert page._compare_pending is True
    # ...and the panels say so rather than showing the last region's pixels.
    assert all(item.image is None for item in page._preview_imgs)
    assert "Computing" in page._preview_status.text()
    assert all("computing" in lbl.text()
               for lbl in (page._metrics_original, page._metrics_tophat,
                           page._metrics_cucim))
    page._stop_compare_worker()


def test_the_arrays_land_and_the_region_is_fitted_once(app):
    page = _page(app)
    _enter(page, 20000.0, 15000.0, _payload(h=100, w=200))

    assert page._compare_pending is False
    assert page._compare_fitted is True
    assert page._preview_imgs[0].image.shape == (100, 200)
    # Fitted: the whole picture is inside the view, on all three.
    for vb in page._preview_vbs:
        (x0, x1), (y0, y1) = vb.viewRange()
        assert x0 <= 0 and x1 >= 200 and y0 <= 0 and y1 >= 100
    assert page._metrics_original.text().startswith("Original")
    assert "(preview)" in page._metrics_original.text()


def test_a_late_reply_from_a_superseded_region_is_dropped(app):
    page = _page(app)
    _enter(page, 20000.0, 15000.0)
    stale_id = page._compare_req_id
    current = page._compare_payload

    page._on_compare_region_done(stale_id - 1, _payload(value=999.0))

    assert page._compare_payload is current


def test_the_same_region_and_numbers_are_served_from_the_cache(app):
    page = _page(app)
    _enter(page, 20000.0, 15000.0)
    page._compare_cache[page._compare_key] = page._compare_payload
    req_before = page._compare_req_id

    page._request_compare_region()

    assert page._compare_req_id == req_before, "a cached region started a worker"
    assert page._compare_worker is None or not page._compare_worker.isRunning()


# ── 3. the recompute keeps the camera ────────────────────────────────────

def _zoom_in(page):
    page._preview_vbs[0].setRange(xRange=(10, 20), yRange=(10, 20), padding=0)
    page._sync_zoom(0)
    return _ranges(page)


def test_a_parameter_edit_recomputes_the_same_region_and_keeps_the_camera(app):
    page = _page(app)
    _enter(page, 20000.0, 15000.0)
    region, level = page._compare_region, page._compare_level
    zoomed = _zoom_in(page)

    page._tophat_slider.setValue(page._tophat_slider.value() + 3)
    page._sync_compare_params()
    page._stop_compare_worker()
    page._apply_compare_payload(_payload(value=222.0))

    assert page._compare_region == region and page._compare_level == level
    assert np.allclose(np.asarray(_ranges(page), float),
                       np.asarray(zoomed, float), rtol=0, atol=1e-6)
    assert page._preview_imgs[0].image[0, 0] == np.float32(222.0)


def test_a_channel_row_change_recomputes_the_same_region_and_keeps_it(app):
    page = _page(app)
    _enter(page, 20000.0, 15000.0)
    region = page._compare_region
    zoomed = _zoom_in(page)

    page.current_channel = "CD20"
    page._sync_compare_to_channel()
    page._stop_compare_worker()
    page._apply_compare_payload(_payload(value=333.0))

    assert page._compare_region == region
    assert np.allclose(np.asarray(_ranges(page), float),
                       np.asarray(zoomed, float), rtol=0, atol=1e-6)
    assert page._compare_key[0] == "CD20"


def test_nothing_recomputes_while_the_full_image_is_the_view(app):
    """A slider dragged over the full image must not compute three arrays of
    a region nobody is looking at."""
    page = _page(app)
    assert page._compare_mode() is False

    page._sync_compare_params()
    page._sync_compare_to_channel()

    assert page._compare_worker is None
    assert page._compare_payload is None


# ── 4. the toggle moves nothing, proved with pixels ──────────────────────

def _full_view_widget(page):
    return page._explore_tab.stack.view


def _grab(page):
    QtTest.QTest.qWait(60)
    return _full_view_widget(page).grab().toImage()


def _geometry(page):
    w = _full_view_widget(page)
    return (w.mapTo(page, QtCore.QPoint(0, 0)), w.size())


def _max_abs_diff(a, b):
    def _arr(img):
        img = img.convertToFormat(img.Format_RGBA8888)
        ptr = img.bits()
        ptr.setsize(img.sizeInBytes())
        return np.frombuffer(ptr, np.uint8).reshape(
            img.height(), img.bytesPerLine())[:, :img.width() * 4]

    x, y = _arr(a).astype(int), _arr(b).astype(int)
    if x.shape != y.shape:
        return None
    return int(np.abs(x - y).max())


def test_five_toggles_leave_the_full_image_pixel_identical(app):
    """The gesture is a right-click with the mouse held still: press, look,
    press again. The slide must not have moved a pixel -- so the check is
    two grabs of the viewer compared byte for byte, not two rectangles
    compared to a tolerance."""
    page = _page(app)
    rect_before = page._full_image_view_rect_l0()
    geom_before = _geometry(page)
    image_before = _grab(page)
    assert image_before.width() > 100 and image_before.height() > 100

    for i in range(5):
        _enter(page, 20000.0, 15000.0)
        assert page._compare_mode() is True
        page._exit_compare_mode()
        assert page._compare_mode() is False

        assert page._full_image_view_rect_l0() == pytest.approx(rect_before), i
        assert _geometry(page) == geom_before, i
        assert _max_abs_diff(_grab(page), image_before) == 0, (
            f"the full image moved on cycle {i}")


def test_the_toggle_survives_a_pan_and_a_zoom_of_the_panels(app):
    """What the user does to the PANELS is a camera over a picture and says
    nothing about the slide, so it must not follow them out."""
    page = _page(app)
    rect_before = page._full_image_view_rect_l0()
    image_before = _grab(page)

    _enter(page, 20000.0, 15000.0)
    page._preview_vbs[0].setRange(xRange=(5, 25), yRange=(7, 27), padding=0)
    page._sync_zoom(0)
    page._exit_compare_mode()

    assert page._full_image_view_rect_l0() == pytest.approx(rect_before)
    assert _max_abs_diff(_grab(page), image_before) == 0


def test_the_viewing_area_is_the_same_size_in_both_modes(app):
    """The toolbar is pinned above the stack and the panel titles live
    inside the compare page, so neither mode can resize the other."""
    page = _page(app)
    area_geom = page._view_area.geometry()
    view_geom = _geometry(page)

    page._set_compare_mode(True)
    QtTest.QTest.qWait(40)
    assert page._view_area.geometry() == area_geom

    page._set_compare_mode(False)
    QtTest.QTest.qWait(40)
    assert page._view_area.geometry() == area_geom
    assert _geometry(page) == view_geom


def test_flipping_modes_with_no_entry_rectangle_moves_nothing(app):
    """A page that has never compared has nothing to restore, and must not
    invent one."""
    page = _page(app)
    rect = page._full_image_view_rect_l0()

    page._set_compare_mode(True)
    page._exit_compare_mode()

    assert page._explore_tab.stack.controller.rects == []
    assert page._full_image_view_rect_l0() == pytest.approx(rect)


def test_the_real_controller_moves_the_camera_the_same_way(app):
    """`_Controller` above stands in for `ExploreController`. This holds the
    two together: the real `set_view_rect_l0` sets both axes independently
    with padding 0, so nothing is re-fitted and the saved rectangle comes
    back exactly."""
    from block01.viewer.explore_view import ExploreController

    calls = []

    class _VB:
        def setRange(self, **kw):
            calls.append(kw)

    class _Fake:
        """Only `_move_camera` is stubbed -- everything else a real jump
        owes the tile engine is beside the point here. What is under test is
        the rectangle the real method hands the ViewBox."""

        def _move_camera(self, apply_range):
            apply_range(_VB())

    ExploreController.set_view_rect_l0(_Fake(), 1.5, 2.5, 30.0, 40.0)

    assert calls == [{"xRange": (1.5, 31.5), "yRange": (2.5, 42.5),
                      "padding": 0}]


# ── 5. the two ways out, and the one thing left behind ───────────────────

def test_escape_returns_to_the_full_image(app):
    page = _page(app)
    _enter(page, 20000.0, 15000.0)

    page.keyPressEvent(QKeyEvent(QtCore.QEvent.KeyPress, Qt.Key_Escape,
                                 Qt.NoModifier))

    assert page._compare_mode() is False


def test_a_right_click_on_a_panel_returns_to_the_full_image(app):
    """A pyqtgraph ViewBox accepts a right-click for its context menu, so
    the gesture is taken on the graphics view's VIEWPORT, before the scene
    ever sees it."""
    page = _page(app)
    _enter(page, 20000.0, 15000.0)
    viewport = page._preview_gv.viewport()

    ev = QMouseEvent(QtCore.QEvent.MouseButtonPress,
                     QtCore.QPointF(30.0, 30.0), Qt.RightButton,
                     Qt.RightButton, Qt.NoModifier)
    handled = page.eventFilter(viewport, ev)

    assert handled is True
    assert page._compare_mode() is False


def test_save_as_patch_appends_the_region(app):
    """R is what was computed, what the metrics describe and what "compare
    this spot" named -- not the part of it currently framed."""
    page = _page(app)
    region = _enter(page, 20000.0, 15000.0)
    # Zoom right in; the patch must still be the whole region.
    page._preview_vbs[0].setRange(xRange=(1, 3), yRange=(1, 3), padding=0)
    page._sync_zoom(0)

    coords = page._save_snapshot_as_patch()

    x0, y0, w, h = (int(round(v)) for v in region)
    assert coords == (y0, y0 + h, x0, x0 + w)
    assert list(page.patches)[-1] == coords


def test_save_as_patch_does_nothing_before_the_panels_are_opened(app):
    page = _page(app)
    assert page._save_snapshot_as_patch() is None
    assert list(page.patches) == []
