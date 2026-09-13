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
        # ...AS A USER WOULD: clicking a marker row is what selects it and,
        # since B4-A, what shows it. A fresh slide lands on DAPI with its
        # markers hidden, so a test that only assigned `current_channel`
        # would be looking at a channel nobody asked to see.
        page._on_channel_row_clicked(channel)
    page._explore_tab = _Tab()
    # The Tissue Preview is drawn on the page's own overview panel; give it
    # the slide it is a preview OF.
    page.overview.loader = page.loader
    page.overview.full_h, page.overview.full_w = SLIDE_H, SLIDE_W
    page._sync_step0_to_workbench()
    return page


def _settle(page, ms=1500):
    """Let Block01's frame clock reach its slot and its worker come back.

    The thumbnail is no longer drawn inside the call that changed the state:
    it is an input on a ~30 FPS clock whose frames are composed on the
    `tissue-compose` thread. So a test that wants to look at the picture has
    to give the clock its slot and the thread its result, which is what this
    does -- and no more than that, because waiting a fixed time would hide
    exactly the starvation this module is here to catch.
    """
    co = page.display.coordinator
    deadline = time.monotonic() + ms / 1000.0
    quiet = 0
    while time.monotonic() < deadline:
        QtTest.QTest.qWait(5)
        busy = any(wk is not None and wk.is_busy() for wk in
                   (page.display._seed_worker, page.display._read_worker))
        stats = co.frame_stats()
        if busy or stats["in_flight"] or stats["pending"]:
            quiet = 0
            continue
        # TWO quiet rounds, not one. A first display window is computed off
        # thread, and the frame it then asks for is a second round trip: a
        # single quiet check lands in the gap between the two and calls it
        # settled while the picture is still the previous channel's.
        quiet += 1
        if quiet >= 3:
            return
    raise AssertionError(f"the frame clock never settled: {co.frame_stats()}")


def _thumb(page):
    """What the Tissue Preview is currently drawing."""
    return page.overview.img_item.image


def _mean_rgb(img):
    return tuple(float(v) for v in np.asarray(img).reshape(-1, 3).mean(axis=0))


# ── 1. the picture is the channel, in the channel's colour ───────────────

def test_the_thumbnail_is_the_current_channel(app):
    page = _page(app)

    page._update_tissue_preview()
    # A first display window is computed on the seed thread now: the
    # synchronous draw starts it, and the picture follows when it lands.
    _settle(page)

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
    # DAPI is ON by default now and is composited into the thumbnail, so
    # its blue would land in the mean this test reads. The subject here is
    # the MARKER's colour, so the reference layer is turned off to leave it
    # alone; `test_dapi_is_composited_in_while_its_layer_is_on` and
    # `test_the_thumbnail_shows_dapi_from_the_first_frame` own the other
    # half.
    page._on_nucleus_visibility_toggled(False)

    page._apply_channel_color("CD3", (1.0, 0.0, 0.0))
    _settle(page)

    r, g, b = _mean_rgb(_thumb(page))
    assert r > 40.0 and g == 0.0 and b == 0.0, (r, g, b)

    page._apply_channel_color("CD3", (0.0, 1.0, 0.0))
    _settle(page)

    r, g, b = _mean_rgb(_thumb(page))
    assert g > 40.0 and r == 0.0 and b == 0.0, (r, g, b)


def test_a_colour_picked_anywhere_reaches_it(app):
    """All three colour writers funnel through `_apply_channel_color`; this
    drives the channel model's, the one furthest from the thumbnail."""
    page = _page(app)
    # DAPI is ON by default now and is composited into the thumbnail, so
    # its blue would land in the mean this test reads. The subject here is
    # the MARKER's colour, so the reference layer is turned off to leave it
    # alone; `test_dapi_is_composited_in_while_its_layer_is_on` and
    # `test_the_thumbnail_shows_dapi_from_the_first_frame` own the other
    # half.
    page._on_nucleus_visibility_toggled(False)
    page._apply_channel_color("CD3", (1.0, 0.0, 0.0))

    page._on_model_color_changed("CD3", "#0000ff")
    _settle(page)

    r, g, b = _mean_rgb(_thumb(page))
    assert b > 40.0 and r == 0.0 and g == 0.0, (r, g, b)


# ── 2. it follows the selected row ───────────────────────────────────────

def test_switching_channel_switches_the_picture(app):
    page = _page(app)
    # DAPI is ON by default now and is composited into the thumbnail, so
    # its blue would land in the mean this test reads. The subject here is
    # the MARKER's colour, so the reference layer is turned off to leave it
    # alone; `test_dapi_is_composited_in_while_its_layer_is_on` and
    # `test_the_thumbnail_shows_dapi_from_the_first_frame` own the other
    # half.
    page._on_nucleus_visibility_toggled(False)
    page._apply_channel_color("CD3", (1.0, 0.0, 0.0))
    page._apply_channel_color("CD20", (0.0, 1.0, 0.0))
    page._update_tissue_preview()
    _settle(page)
    before = np.array(_thumb(page), copy=True)

    # A ROW CLICK, which is both halves: the model's selection and the
    # "show me this one" a click means. Selection alone no longer shows a
    # hidden channel -- that is the B4-A rule, and the DAPI test below is
    # the other side of it.
    page._on_channel_selected_by_id("CD20")
    page._on_channel_row_clicked("CD20")
    _settle(page)

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
    _settle(page)
    marker_only = np.array(_thumb(page), copy=True)
    assert _mean_rgb(marker_only)[2] == 0.0, "blue before the layer is on"

    page._on_nucleus_visibility_toggled(True)
    _settle(page)

    both = _thumb(page)
    r, _g, b = _mean_rgb(both)
    assert b > 40.0, "the nucleus is not in the picture"
    # ADDITIVE, in each channel's own colour: the marker's red is untouched.
    assert r == pytest.approx(_mean_rgb(marker_only)[0])

    page._on_nucleus_visibility_toggled(False)
    _settle(page)

    assert np.array_equal(_thumb(page), marker_only)


def test_the_nucleus_colour_reaches_the_composite(app):
    page = _page(app)
    page._apply_channel_color("CD3", (1.0, 0.0, 0.0))
    page._on_nucleus_visibility_toggled(True)

    page._apply_nucleus_color((0.0, 1.0, 0.0))
    _settle(page)

    _r, g, b = _mean_rgb(_thumb(page))
    assert g > 40.0 and b == 0.0, (g, b)


# ── 4. the mapping, DURING the drag ──────────────────────────────────────
#
# This section used to pin the opposite rule: "the thumbnail waits out the
# stream". It waited because it was composed on the GUI thread, and the wait
# was a single-shot 100 ms timer RESTARTED by every slider step -- so a
# continuous drag pushed the deadline forward faster than it could arrive and
# the picture did not move until the hand stopped. That is what the user
# reported. The pixels are now composed on Block01's `tissue-compose` thread
# and published on a ~30 FPS clock, so what is pinned here is that
# intermediate frames appear WHILE the input is still coming.

def test_a_mapping_edit_reaches_the_thumbnail(app):
    page = _page(app)
    page._apply_channel_color("CD3", (1.0, 0.0, 0.0))
    page._update_tissue_preview()
    _settle(page)
    before = np.array(_thumb(page), copy=True)
    lo, hi, _gamma = page._display_mapping_for("CD3")

    page.set_display_mapping("CD3", lo, hi / 4.0)
    _settle(page)

    after = _thumb(page)
    assert not np.array_equal(before, after)
    # A narrower window is a brighter picture of the same pixels.
    assert _mean_rgb(after)[0] > _mean_rgb(before)[0]


def test_a_drag_publishes_while_the_hand_is_still_moving(app):
    """THE reported bug, as a test.

    Two seconds of Min/Max at 200 Hz. The old debounce drew exactly one
    frame, after the last event; the frame clock must draw many DURING the
    stream -- and they must be different pictures, not the same one pushed
    repeatedly.
    """
    page = _page(app)
    page._on_nucleus_visibility_toggled(False)
    page._apply_channel_color("CD3", (1.0, 0.0, 0.0))
    _settle(page)
    frames = []
    page.overview.set_channel_image = (
        lambda rgb, token=None: frames.append(np.array(rgb, copy=True)))

    lo, hi, _g = page._display_mapping_for("CD3")
    steps = 400                                  # 2 s at 200 Hz
    for i in range(1, steps + 1):
        page.set_display_mapping("CD3", lo, hi - (hi - lo) * 0.5 * i / steps)
        QtTest.QTest.qWait(1)
        assert page.display.coordinator.frame_stats()["pending"] <= 1
    mid = len(frames)
    assert mid >= 3, f"only {mid} frame(s) during a 2 s drag"
    distinct = {f.tobytes() for f in frames}
    assert len(distinct) >= 3, "the same picture was pushed over and over"

    _settle(page)

    # The last frame is the last VALUE, not the value the drag happened to be
    # at when the final slot fired.
    final = page._tissue_preview_rgb()[0]
    assert np.array_equal(frames[-1], final)


def test_a_burst_is_coalesced_to_one_frame_in_flight(app):
    """Latest-only, depth one. 200 inputs must not become 200 frames."""
    page = _page(app)
    page._update_tissue_preview()
    rendered = []
    page.overview.set_channel_image = (
        lambda rgb, token=None: rendered.append(rgb))

    lo, hi, _g = page._display_mapping_for("CD3")
    for i in range(1, 201):
        page.set_display_mapping("CD3", lo, hi - i * 0.5)
        assert page.display.coordinator.frame_stats()["pending"] <= 1
    _settle(page)

    stats = page.display.coordinator.frame_stats()
    assert stats["max_pending_depth"] <= 1, stats
    assert len(rendered) < 200 // 4, f"{len(rendered)} frames for 200 inputs"
    assert rendered, "nothing was drawn at all"


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
    page._on_channel_row_clicked("CD3")     # as a user selects a marker
    page._explore_tab = _Tab()
    page.overview.loader = page.loader
    page.overview.full_h, page.overview.full_w = SLIDE_H, SLIDE_W
    assert page._workbench_params("CD3") is None, "the workbench is engaged"
    page._apply_channel_color("CD3", (1.0, 0.0, 0.0))
    _settle(page)
    before = np.array(_thumb(page), copy=True)
    lo, hi, _g = page._display_mapping_for("CD3")

    page.set_display_mapping("CD3", lo, hi / 4.0)
    _settle(page)

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
    _settle(page)

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
    _settle(page)
    off = _thumb(page).copy()

    page._on_nucleus_visibility_toggled(True)
    _settle(page)

    assert np.array_equal(_thumb(page), off)


# ── 6. the marker's DISPLAY answer and its SCIENTIFIC weight ─────────────
#
# Two different facts, and B4-A is where Step0 started obeying both. A hidden
# channel contributes nothing because nobody is looking at it; a channel
# weighted `0.0` contributes nothing because somebody said it counts for
# nothing. The single-channel MAIN viewer deliberately applies neither the
# weight nor anything else -- it is the channel as it is, which is what makes
# it the reference the correction decisions are made against.

def _weights(page):
    return page.display.fusion


def test_a_hidden_marker_contributes_nothing_to_the_thumbnail(app):
    page = _page(app)
    page._on_nucleus_visibility_toggled(False)      # the marker alone
    page._apply_channel_color("CD3", (1.0, 0.0, 0.0))
    page._update_tissue_preview()
    _settle(page)
    assert _mean_rgb(_thumb(page))[0] > 40.0

    page.display.state.set_display_visible("CD3", False, origin="test")
    _settle(page)

    assert _mean_rgb(_thumb(page)) == (0.0, 0.0, 0.0), "the marker was drawn"
    # ...and hiding it says nothing about the science.
    assert _weights(page).representative_weight("CD3").absent is True


def test_a_hidden_dapi_contributes_nothing_to_the_thumbnail(app):
    page = _page(app)
    page._apply_channel_color("CD3", (1.0, 0.0, 0.0))
    page._apply_channel_color("DAPI", (0.0, 0.0, 1.0))
    page._update_tissue_preview()
    _settle(page)
    assert _mean_rgb(_thumb(page))[2] > 0.0, "DAPI was not composited in"

    page.display.state.set_display_visible("DAPI", False, origin="test")
    _settle(page)

    r, g, b = _mean_rgb(_thumb(page))
    assert b == 0.0, (r, g, b)
    assert r > 40.0, (r, g, b)


def test_the_thumbnail_applies_the_markers_representative_weight(app):
    """1.0 -> a middle value -> 0.0, and the picture follows each of them."""
    page = _page(app)
    page._on_nucleus_visibility_toggled(False)
    page._apply_channel_color("CD3", (1.0, 0.0, 0.0))
    model = _weights(page)
    model.edit_channel_weight("CD3", 1.0, origin="test")
    page._update_tissue_preview()
    _settle(page)
    full = _mean_rgb(_thumb(page))[0]
    assert full > 40.0

    model.edit_channel_weight("CD3", 0.4, origin="test")
    page._queue_tissue_preview(kind="weight")
    _settle(page)
    middle = _mean_rgb(_thumb(page))[0]

    model.edit_channel_weight("CD3", 0.0, origin="test")
    page._queue_tissue_preview(kind="weight")
    _settle(page)
    zero = _mean_rgb(_thumb(page))[0]

    assert 0.0 < middle < full, (zero, middle, full)
    assert zero == 0.0, (zero, middle, full)
    # An EXPLICIT zero is an answer, not an absence: the provisional
    # full-strength default may not bring the channel back.
    assert model.weight_provenance("CD3") == "explicit"
    # ...and nothing was written back into the model by drawing it.
    assert model.representative_weight("CD3").value == 0.0


def test_a_channel_in_two_groups_shows_its_representative_weight(app):
    """0.2 in one group and 0.7 in another: the preview draws the
    representative value and does not flatten the project by drawing it."""
    page = _page(app)
    page._on_nucleus_visibility_toggled(False)
    page._apply_channel_color("CD3", (1.0, 0.0, 0.0))
    model = _weights(page)
    model.install_draft({
        "groups": {"A": {"group_weight": 1.0, "channels": {"CD3": 0.2}},
                   "B": {"group_weight": 1.0, "channels": {"CD3": 0.7}}},
        "nucleus": {"channel": "DAPI", "weight": 1.0},
        "enabled": ["CD3", "DAPI"],
        "provenance": {"CD3": "authoritative", "DAPI": "authoritative"},
    })
    page._update_tissue_preview()
    _settle(page)

    rep = model.representative_weight("CD3")
    assert rep.mixed is True and rep.value == pytest.approx(0.7)
    drawn = _mean_rgb(_thumb(page))[0]
    assert 0.0 < drawn
    # The groups still disagree afterwards -- looking at the preview did not
    # unify them.
    groups = model.groups()
    assert groups["A"]["CD3"] == pytest.approx(0.2)
    assert groups["B"]["CD3"] == pytest.approx(0.7)


def test_a_channel_nobody_has_weighted_is_drawn_at_full_strength(app):
    """`absent` draws provisionally, and stays absent in the model."""
    page = _page(app)
    page._on_nucleus_visibility_toggled(False)
    page._apply_channel_color("CD3", (1.0, 0.0, 0.0))
    model = _weights(page)
    assert model.representative_weight("CD3").absent is True

    page._update_tissue_preview()
    _settle(page)

    assert _mean_rgb(_thumb(page))[0] > 40.0
    assert model.representative_weight("CD3").absent is True, \
        "the preview's provisional default was written into the model"
    assert model.weight_provenance("CD3") == "absent"


def test_the_single_channel_main_viewer_does_not_apply_the_weight(app):
    """THE EXCEPTION, deliberately: Step0's main viewer shows the channel as
    it is. The compare panels are the reference a correction decision is made
    against, so a scientific weight must not dim them; the Tissue Preview is
    the shared picture and does apply it."""
    page = _page(app)
    model = _weights(page)
    model.edit_channel_weight("CD3", 0.25, origin="test")

    snapshot = page.tissue_render_snapshot(computed_only=False)
    assert snapshot["marker_weight"] == pytest.approx(0.25)

    # The panels are given the channel's own colour and its display window
    # -- never a weight, and never a colour dimmed by one.
    page._apply_channel_color("CD3", (1.0, 0.0, 0.0))
    strip = _ReceivingStrip()
    page._compare_strip_widget = strip
    page._refresh_preview_display()
    assert strip.marker_visible is True
    assert strip.tint == (page._channel_color("CD3"), "CD3"), strip.tint
    assert strip.tint[0] == page._channel_color("CD3")
    assert "weight" not in strip.__dict__


class _ReceivingStrip:
    """What the compare panels are told, recorded."""

    built = True

    def __init__(self):
        self.marker_visible = None
        self.tint = None
        self.mapping = None

    def set_tint(self, color, channel=None):
        self.tint = (color, channel)

    def set_display_mapping(self, lo, hi, gamma, channel=None):
        self.mapping = (lo, hi, gamma, channel)

    def set_marker_visible(self, visible):
        self.marker_visible = bool(visible)

    def set_nucleus_enabled(self, enabled):
        self.nucleus_enabled = bool(enabled)

    def set_nucleus_suppressed(self, suppressed):
        self.nucleus_suppressed = bool(suppressed)

    def set_nucleus_display_mapping(self, lo, hi, gamma):
        self.nucleus_mapping = (lo, hi, gamma)

    def set_nucleus_tint(self, color):
        self.nucleus_tint = color

    def __getattr__(self, name):
        # Anything else the page tells the panels is recorded by name, so a
        # WEIGHT reaching them would show up as an attribute rather than as
        # an AttributeError this test had to keep chasing.
        if name.startswith("set_"):
            return lambda *a, **k: self.__dict__.setdefault(
                name[4:], (a, k))
        raise AttributeError(name)
