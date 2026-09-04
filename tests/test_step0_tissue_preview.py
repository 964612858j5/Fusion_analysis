"""The Tissue Preview shows the channel the page is working on.

The thumbnail was DAPI, always, whatever channel the user had selected --
a picture of where the tissue is, in a workspace whose whole question is
what ONE channel looks like there. It now shows that channel, through the
SAME display mapping and the SAME colour the full image and the compare
panels draw it with, so the three views of a channel cannot disagree about
its brightness or its colour, and "is this the channel I think it is?" is
answered by looking rather than by reading a label.

What this module pins:

* the picture is the current channel's whole-slide low-resolution array --
  the one the page already holds for the display seed and the Intensity
  histogram -- and no second read is made for it;
* its colour is the channel's colour, from the one colour store, and it
  follows a colour change;
* it follows the selected row, and the DAPI row (a reference selection)
  leaves it on the marker;
* DAPI composites additively into it exactly while its layer switch is on;
* a Min/Max edit reaches it, but only after the slider stream settles;
* and everything drawn ON the thumbnail still works: the ROI/patch artists
  stay above it, and a click still navigates to the right slide pixel even
  though the pushed image is a different shape from the overview it
  replaces.

Own module, like the other page-heavy Step0 suites: combined runs segfault
in offscreen pyqtgraph (not a regression, see test_step0_full_image.py).
"""

import os
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtCore, QtTest  # noqa: E402

from block01.ui.step0 import overview_panel as ovp  # noqa: E402
from block01.ui.step0 import step0_page as sp  # noqa: E402

from test_step0_background_correction_tab import (  # noqa: E402
    _GpuPathLoader,
    app,            # noqa: F401  (pytest fixture)
)


SLIDE_H, SLIDE_W = 2048, 1024
LOW_H, LOW_W = 64, 32


# ── stand-ins ────────────────────────────────────────────────────────────

class _LowresLoader(_GpuPathLoader):
    """Serves a whole-slide low-resolution read, one distinct ramp per
    channel, and counts the reads so a redraw cannot be a re-read."""

    shape = (SLIDE_H, SLIDE_W)

    def __init__(self):
        super().__init__()
        self.lowres_reads = []

    def overview_downsample(self):
        return 32

    def read_region_lowres(self, ch, y0, y1, x0, x1, ds, normalize=False):
        self.lowres_reads.append(ch)
        base = {"DAPI": 0.0, "CD3": 1.0, "CD20": 2.0}.get(ch, 3.0)
        ramp = np.linspace(0.0, 1000.0, LOW_H * LOW_W, dtype=np.float32)
        return (ramp + base * 1000.0).reshape(LOW_H, LOW_W)


class _Tab:
    def __init__(self):
        self.stack = None

    def show_source(self, *_a, **_k):
        return True

    def set_dataset(self, _p):
        pass

    def teardown(self, **_k):
        pass


def _page(app, channel="CD3"):
    """`channel=None` leaves the channel the load itself chose -- the
    landing state, which is DAPI."""
    page = sp.Step0Page()
    page.loader = _LowresLoader()
    page.ome_path = "/fake/slide.ome.tif"
    page.patches = []
    page.nucleus_channel = "DAPI"
    page._rebuild_channel_list()
    if channel is not None:
        page.current_channel = channel
    page._explore_tab = _Tab()
    # The Tissue Preview is drawn on the page's own overview panel; give it
    # the slide it is a preview OF.
    page.overview.loader = page.loader
    page.overview.full_h, page.overview.full_w = SLIDE_H, SLIDE_W
    page._sync_step0_to_workbench()
    return page


def _thumb(page):
    """What the Tissue Preview is currently drawing."""
    return page.overview.img_item.image


def _mean_rgb(img):
    return tuple(float(v) for v in np.asarray(img).reshape(-1, 3).mean(axis=0))


# ── 1. the picture is the channel, in the channel's colour ───────────────

def test_the_thumbnail_is_the_current_channel(app):
    page = _page(app)

    page._update_tissue_preview()

    img = _thumb(page)
    assert img is not None, "nothing was drawn"
    assert img.shape == (LOW_H, LOW_W, 3), "not the slide's low-res array"
    assert img.dtype == np.uint8


def test_it_reads_nothing_of_its_own(app):
    """The array is the one the display seed and the Intensity histogram
    already share. A thumbnail that re-read the slide would be the same
    pixels decoded twice."""
    page = _page(app)
    page._slide_lowres_array("CD3")          # warm the one cache
    reads = list(page.loader.lowres_reads)

    for _ in range(3):
        page._update_tissue_preview()

    assert page.loader.lowres_reads == reads


def test_the_thumbnail_takes_the_channels_colour(app):
    page = _page(app)

    page._apply_channel_color("CD3", (1.0, 0.0, 0.0))

    r, g, b = _mean_rgb(_thumb(page))
    assert r > 40.0 and g == 0.0 and b == 0.0, (r, g, b)

    page._apply_channel_color("CD3", (0.0, 1.0, 0.0))

    r, g, b = _mean_rgb(_thumb(page))
    assert g > 40.0 and r == 0.0 and b == 0.0, (r, g, b)


def test_a_colour_picked_anywhere_reaches_it(app):
    """All three colour writers funnel through `_apply_channel_color`; this
    drives the channel model's, the one furthest from the thumbnail."""
    page = _page(app)
    page._apply_channel_color("CD3", (1.0, 0.0, 0.0))

    page._on_model_color_changed("CD3", "#0000ff")

    r, g, b = _mean_rgb(_thumb(page))
    assert b > 40.0 and r == 0.0 and g == 0.0, (r, g, b)


# ── 2. it follows the selected row ───────────────────────────────────────

def test_switching_channel_switches_the_picture(app):
    page = _page(app)
    page._apply_channel_color("CD3", (1.0, 0.0, 0.0))
    page._apply_channel_color("CD20", (0.0, 1.0, 0.0))
    page._update_tissue_preview()
    before = np.array(_thumb(page), copy=True)

    page._on_channel_selected_by_id("CD20")

    after = _thumb(page)
    assert not np.array_equal(before, after), "the thumbnail kept CD3"
    r, g, b = _mean_rgb(after)
    assert g > 40.0 and r == 0.0 and b == 0.0, (r, g, b)


def test_the_dapi_row_leaves_the_marker_on_screen(app):
    """DAPI is a reference channel: its row re-points the Intensity window
    and nothing else."""
    page = _page(app)
    page._apply_channel_color("CD3", (1.0, 0.0, 0.0))
    page._update_tissue_preview()
    before = np.array(_thumb(page), copy=True)

    page._on_channel_selected_by_id("DAPI")

    assert page.current_channel == "CD3"
    assert np.array_equal(before, _thumb(page))


# ── 3. the DAPI composite follows the DAPI switch ────────────────────────

def test_dapi_is_composited_in_while_its_layer_is_on(app):
    page = _page(app)
    page._apply_channel_color("CD3", (1.0, 0.0, 0.0))
    page._apply_nucleus_color((0.0, 0.0, 1.0))
    page._on_nucleus_visibility_toggled(False)
    marker_only = np.array(_thumb(page), copy=True)
    assert _mean_rgb(marker_only)[2] == 0.0, "blue before the layer is on"

    page._on_nucleus_visibility_toggled(True)

    both = _thumb(page)
    r, _g, b = _mean_rgb(both)
    assert b > 40.0, "the nucleus is not in the picture"
    # ADDITIVE, in each channel's own colour: the marker's red is untouched.
    assert r == pytest.approx(_mean_rgb(marker_only)[0])

    page._on_nucleus_visibility_toggled(False)

    assert np.array_equal(_thumb(page), marker_only)


def test_the_nucleus_colour_reaches_the_composite(app):
    page = _page(app)
    page._apply_channel_color("CD3", (1.0, 0.0, 0.0))
    page._on_nucleus_visibility_toggled(True)

    page._apply_nucleus_color((0.0, 1.0, 0.0))

    _r, g, b = _mean_rgb(_thumb(page))
    assert g > 40.0 and b == 0.0, (g, b)


# ── 4. the mapping, off the slider hot path ──────────────────────────────

def test_a_mapping_edit_reaches_the_thumbnail_after_the_stream_settles(app):
    """Dragging Min/Max must not re-render the whole slide once per step,
    so the thumbnail waits out the stream -- unlike the panels and the full
    image, which only swap a levels pair."""
    page = _page(app)
    page._apply_channel_color("CD3", (1.0, 0.0, 0.0))
    page._update_tissue_preview()
    before = np.array(_thumb(page), copy=True)
    lo, hi, _gamma = page._display_mapping_for("CD3")

    page.set_display_mapping("CD3", lo, hi / 4.0)

    assert np.array_equal(_thumb(page), before), "re-rendered on the hot path"
    assert page._tissue_preview_timer.isActive()

    QtTest.QTest.qWait(page._TISSUE_PREVIEW_DEBOUNCE_MS + 120)

    after = _thumb(page)
    assert not np.array_equal(before, after)
    # A narrower window is a brighter picture of the same pixels.
    assert _mean_rgb(after)[0] > _mean_rgb(before)[0]


def test_a_burst_of_slider_steps_renders_once(app):
    page = _page(app)
    page._update_tissue_preview()
    rendered = []
    page.overview.set_channel_image = lambda rgb: rendered.append(rgb)

    lo, hi, _g = page._display_mapping_for("CD3")
    for i in range(1, 11):
        page.set_display_mapping("CD3", lo, hi - i * 10.0)
    QtTest.QTest.qWait(page._TISSUE_PREVIEW_DEBOUNCE_MS + 120)

    assert len(rendered) == 1, rendered


def test_the_mapping_reaches_it_before_the_workbench_is_engaged_too(app):
    """The real landing state: the Channel Remap workbench has never been
    shown, so a mapping edit writes the page's own fallback entry and emits
    no `params_changed` at all -- it calls `_on_display_mapping_changed`
    directly. The thumbnail is hooked there, so both paths reach it.

    Measured on the real slide before this: the window moved from
    (1.0, 37.0) to (1.0, 9.25) and the thumbnail did not change.
    """
    page = sp.Step0Page()
    page.loader = _LowresLoader()
    page.patches = []
    page.nucleus_channel = "DAPI"
    page._rebuild_channel_list()
    page.current_channel = "CD3"
    page._explore_tab = _Tab()
    page.overview.loader = page.loader
    page.overview.full_h, page.overview.full_w = SLIDE_H, SLIDE_W
    assert page._workbench_params("CD3") is None, "the workbench is engaged"
    page._apply_channel_color("CD3", (1.0, 0.0, 0.0))
    before = np.array(_thumb(page), copy=True)
    lo, hi, _g = page._display_mapping_for("CD3")

    page.set_display_mapping("CD3", lo, hi / 4.0)
    QtTest.QTest.qWait(page._TISSUE_PREVIEW_DEBOUNCE_MS + 120)

    assert page._display_mapping_for("CD3")[1] == pytest.approx(hi / 4.0)
    assert not np.array_equal(before, _thumb(page))
    assert _mean_rgb(_thumb(page))[0] > _mean_rgb(before)[0]

# ── 5. everything drawn on the thumbnail still works ─────────────────────

def _panel():
    """A bare overview panel over the same slide, with no async load."""
    panel = ovp.OverviewPanel(_LowresLoader(), "DAPI", lazy=True)
    panel.full_h, panel.full_w = SLIDE_H, SLIDE_W
    return panel


def test_a_pushed_image_keeps_the_overviews_own_geometry(app):
    """The pushed array is a different shape from the overview it replaces
    (a different pyramid level), so it is STRETCHED onto the overview's
    rectangle -- every coordinate on this panel is defined by `ds`."""
    panel = _panel()

    panel.set_channel_image(np.zeros((LOW_H, LOW_W, 3), np.uint8))

    assert (panel.ov_h, panel.ov_w) == (SLIDE_H // 32, SLIDE_W // 32)
    rect = panel.img_item.boundingRect()
    mapped = panel.img_item.mapRectToView(rect)
    assert (mapped.x(), mapped.y()) == (0.0, 0.0)
    assert (mapped.width(), mapped.height()) == (panel.ov_w, panel.ov_h)


def test_click_to_navigate_still_lands_on_the_right_pixel(app):
    panel = _panel()
    panel.set_channel_image(np.zeros((LOW_H, LOW_W, 3), np.uint8))
    panel.vb.setRange(QtCore.QRectF(0, 0, panel.ov_w, panel.ov_h), padding=0)
    QtTest.QTest.qWait(20)

    scene_pos = panel.vb.mapViewToScene(
        QtCore.QPointF(panel.ov_w / 2.0, panel.ov_h / 2.0))
    r, c = panel._ov_pos(scene_pos)

    assert (r, c) == pytest.approx((panel.ov_h / 2.0, panel.ov_w / 2.0), abs=2)
    assert panel._to_fullres(r, c) == pytest.approx(
        (SLIDE_H / 2.0, SLIDE_W / 2.0), abs=64)


def test_the_patch_artists_stay_above_the_thumbnail(app):
    panel = _panel()
    panel.set_channel_image(np.full((LOW_H, LOW_W, 3), 255, np.uint8))

    panel.add_patch_rect(100, 300, 100, 300)

    assert panel._patch_artists, "no patch was drawn"
    for artist in panel._patch_artists[0]:
        assert artist.zValue() > panel.img_item.zValue()
    assert panel.img_item.image is not None, "the patch replaced the picture"


def test_the_viewport_rectangle_stays_above_it_too(app):
    panel = _panel()
    panel.set_channel_image(np.full((LOW_H, LOW_W, 3), 255, np.uint8))

    panel.set_current_view_rect((100, 300, 100, 300))

    assert panel._current_view_item is not None
    assert panel._current_view_item.zValue() > panel.img_item.zValue()


def test_the_dapi_overview_is_kept_and_comes_back(app):
    """A host that stops pushing gets the panel's own overview back, and an
    overview landing after a push does not replace the picture the page put
    there -- the load is asynchronous and the two arrive in either order."""
    panel = _panel()
    overview = np.arange(
        (SLIDE_H // 32) * (SLIDE_W // 32), dtype=np.float32).reshape(
            SLIDE_H // 32, SLIDE_W // 32)
    pushed = np.full((LOW_H, LOW_W, 3), 200, np.uint8)

    panel.set_channel_image(pushed)
    panel._t0 = time.time()               # normally set by `_load_overview`
    panel._on_overview_loaded(overview)

    assert panel.img_item.image.shape == pushed.shape, "the load clobbered it"

    panel.set_channel_image(None)

    assert panel.img_item.image.shape == overview.shape


# ── the landing state: the thumbnail is DAPI, drawn once ─────────────────

def test_on_the_landing_the_thumbnail_is_dapi(app):
    """A freshly loaded slide shows DAPI everywhere it shows anything, and
    the thumbnail is not an exception -- it draws `current_channel`, which
    is DAPI until a marker row is clicked."""
    page = _page(app, channel=None)
    assert page.current_channel == "DAPI"

    page._update_tissue_preview()

    img = _thumb(page)
    assert img is not None
    dapi_alone = page._lowres_tinted(page._slide_lowres_array("DAPI"), "DAPI")
    assert np.array_equal(img, dapi_alone)


def test_the_landing_thumbnail_never_composites_dapi_onto_itself(app):
    """The DAPI layer switch adds DAPI on top of a MARKER. With DAPI itself
    on screen there is nothing to add it to, so turning the layer on must
    not brighten the picture by drawing the channel twice."""
    page = _page(app, channel=None)
    page._update_tissue_preview()
    off = _thumb(page).copy()

    page._on_nucleus_visibility_toggled(True)

    assert np.array_equal(_thumb(page), off)
