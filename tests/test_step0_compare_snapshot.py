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

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtCore, QtTest  # noqa: E402

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

    def set_marker_visible(self, _v):
        pass

    def set_display_mapping(self, *_a, **_k):
        pass


class _ViewBox:
    """Reports a fixed viewport and widget width, which together fix the
    full image's scale in screen pixels per level-0 pixel."""

    def __init__(self, x0=0.0, x1=None, width=1024.0):
        self._range = ((float(x0), float(SLIDE_W if x1 is None else x1)),
                       (0.0, float(SLIDE_H)))
        self._width = float(width)

    def viewRange(self):
        return self._range

    def width(self):
        return self._width


class _Stack:
    def __init__(self, level=0, scale_width=1024.0, view_w=SLIDE_W):
        self.controller = _Controller(level)
        self.provider = _Provider()
        self.scheduler = type("S", (), {"raw_cache": None,
                                        "corrected_cache": None})()
        self.overlay = None
        self.view = type("V", (), {"view_box": _ViewBox(0.0, view_w,
                                                        scale_width)})()


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


def _page(app, *, level=0, scale_width=1024.0, view_w=SLIDE_W):
    page = sp.Step0Page()
    page.loader = _GpuPathLoader()
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
    page._set_compare_strip_visible(True)
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


def test_the_snapshot_is_centred_on_the_clicked_point(app):
    page = _page(app)

    payload = _snapshot(page, 2000, 1500)

    x0, y0, w, h = payload["snapshot_rect_l0"]
    # Within the rounding of the crop to whole level pixels: an odd width
    # cannot have the click exactly in the middle of a pixel.
    assert (x0 + w / 2.0, y0 + h / 2.0) == pytest.approx((2000.0, 1500.0),
                                                         abs=1.0)


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

    payload = _snapshot(page, 5, 5)

    x0, y0, w, h = payload["snapshot_rect_l0"]
    assert (w, h) == (round(pw), round(ph))
    assert (x0, y0) == (0.0, 0.0)


# ── 2. the panels are not a viewer ───────────────────────────────────────

def test_the_panels_take_no_mouse_input(app):
    page = _page(app)
    for vb in page._preview_vbs:
        assert vb.state["mouseEnabled"] == [False, False]


def test_the_view_controls_are_gone(app):
    page = _page(app)
    for gone in ("_btn_lock_zoom", "_reset_all_views", "_reset_single_view",
                 "_sync_zoom", "_full_image_buttons", "_enter_full_image",
                 "_match_full_image_to_panel", "_full_view_range_for_panel",
                 "_compare_panel_camera", "_vb_screen_geometry",
                 "_compare_viewport_l0", "_preview_stack"):
        assert not hasattr(page, gone), gone


def test_a_wheel_event_does_not_move_a_panel(app):
    page = _page(app)
    _snapshot(page, 2000, 1500)
    before = [vb.viewRange() for vb in page._preview_vbs]

    for vb in page._preview_vbs:
        vb.wheelEvent(_WheelEvent())

    assert [vb.viewRange() for vb in page._preview_vbs] == before


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


def test_a_second_right_click_retakes_the_snapshot(app):
    page = _page(app)
    first = dict(_snapshot(page, 2000, 1500))

    second = _snapshot(page, 3000, 2500)

    assert second["snapshot_rect_l0"] != first["snapshot_rect_l0"]
    x0, y0, w, h = second["snapshot_rect_l0"]
    assert (x0 + w / 2.0, y0 + h / 2.0) == pytest.approx((3000.0, 2500.0),
                                                         abs=1.0)


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

    calls = page._explore_tab.stack.controller.compute.calls
    assert [(m, p) for m, p, _shape in calls] == [("tophat", 21),
                                                  ("cucim", 9)]


def test_a_coarse_level_scales_the_parameter_the_way_a_tile_does(app):
    """`effective_param` is the controller's own scaling; a snapshot that
    used the level-0 number would remove a different background from the
    one the full image is showing."""
    page = _page(app, level=2)
    page._channel_params["CD3"] = {"tophat_radius": 20, "cucim_sigma": 8}

    _snapshot(page, 2000, 1500)

    ds = page._explore_tab.stack.provider.level_downsample(2)
    calls = page._explore_tab.stack.controller.compute.calls
    assert [(m, p) for m, p, _s in calls] == [
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


# ── 5. the strip ─────────────────────────────────────────────────────────

def test_the_strip_expands_on_the_first_right_click(app):
    page = _page(app)
    assert page._compare_strip_visible() is False

    _snapshot(page, 2000, 1500)

    assert page._compare_strip_visible() is True
    assert page._full_image_visible(), "the image must stay on screen"


def test_the_toolbar_button_still_collapses_it(app):
    page = _page(app)
    _snapshot(page, 2000, 1500)

    page._btn_show_compare.click()

    assert page._compare_strip_visible() is False
    assert page._last_payload is not None, "the snapshot is kept"


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
