"""The full image draws the channel in the channel's colour.

Two claims:

  1. the COLOUR is the compare panels' colour -- same source
     (`_channel_colors` with `_marker_color` as fallback), so a channel
     looks the same in both places -- and it reaches the viewer before any
     camera move, so no frame is painted in the previous channel's colour;
  2. the marker layer can be turned OFF and back on without rebuilding,
     re-reading or re-quantising anything.

The lookup table itself is checked against the compare panels' own
compositing function rather than against hand-written numbers: the point
is that the two agree, not that a particular byte is 0x7f.

Own module for the reason the other full-image modules are separate: the
Step0 page-heavy suites segfault when the background-correction module runs
first in a process (measured, pre-existing).
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

import pyqtgraph as pg  # noqa: E402

from block01.ui.step0 import step0_page as sp  # noqa: E402
from block01.viewer.explore_view import ExploreController  # noqa: E402

from test_step0_background_correction_tab import app  # noqa: E402,F401


class _RecordingTab:
    def __init__(self):
        self.calls = []
        self.stack = None

    def show_source(self, channel, method, params=(), *, viewport_l0=None,
                    tint=None):
        self.calls.append({"channel": channel, "tint": tint,
                           "viewport": viewport_l0})
        return True

    def set_dataset(self, _path):
        pass

    def teardown(self, **_kw):
        pass


def _page(app):
    page = sp.Step0Page()
    page.current_channel = "CD3"
    page.nucleus_channel = "DAPI"
    page._explore_tab = _RecordingTab()
    return page


# ── 1. the lookup table ──────────────────────────────────────────────────

def test_the_lut_matches_the_compare_panels_own_colouring(app):
    """One answer to 'what colour is this channel'. The table is compared
    against `_make_colored_rgb`, the function the compare panels composite
    with, over the whole 0..255 range."""
    rgb = (0.0, 0.75, 0.25)
    lut = ExploreController.build_tint_lut(rgb)

    assert lut.shape == (256, 3) and lut.dtype == np.uint8
    grey = np.arange(256, dtype=np.float32) / 255.0
    expected = sp.Step0Page._make_colored_rgb(
        grey.reshape(1, 256), None, marker_rgb=rgb, nucleus_rgb=(0, 0, 0))
    expected = np.round(np.clip(expected[0], 0, 1) * 255).astype(np.uint8)
    # Same ramp, within one count of rounding at every entry.
    assert np.max(np.abs(lut.astype(int) - expected.astype(int))) <= 1


def test_the_lut_is_black_at_zero_and_the_colour_at_full(app):
    lut = ExploreController.build_tint_lut((0.0, 1.0, 0.0))

    assert tuple(lut[0]) == (0, 0, 0)
    assert tuple(lut[255]) == (0, 255, 0)
    assert lut[128][1] > lut[64][1] > lut[0][1]      # monotone in the ramp
    assert set(lut[:, 0]) == {0} and set(lut[:, 2]) == {0}   # no red, no blue


def test_a_grey_channel_is_still_possible(app):
    """`None` means greyscale -- the state every layer is in before a host
    sets a colour."""
    page = _page(app)
    page.current_channel = None

    assert page._full_image_tint() is None


# ── 2. the page hands the compare colour over ────────────────────────────

def test_the_page_sends_the_compare_panels_colour(app):
    page = _page(app)
    page._channel_colors["CD3"] = (0.2, 0.4, 0.9)

    assert page._full_image_tint() == (0.2, 0.4, 0.9)
    assert page._full_image_tint("CD3") == page._channel_colors["CD3"]


def test_an_uncoloured_channel_falls_back_to_the_marker_colour(app):
    page = _page(app)
    page._channel_colors.pop("CD3", None)

    assert page._full_image_tint() == getattr(page, "_marker_color",
                                              (0.0, 1.0, 0.3))


def test_opening_the_full_image_passes_the_tint(app):
    page = _page(app)
    page._channel_colors["CD3"] = (1.0, 0.5, 0.0)

    page._enter_full_image("original")

    assert page._explore_tab.calls[-1]["tint"] == (1.0, 0.5, 0.0)


# ── 3. tint reaches the controller before the camera moves ───────────────

class _Ctl:
    def __init__(self):
        self.order = []
        self.channel, self.method, self.params = "CD3", None, ()

    def set_tint(self, rgb):
        self.order.append(("set_tint", rgb))

    def set_selection(self, channel=None, method=None, params=None):
        self.order.append("set_selection")
        self.channel, self.method = channel, method
        self.params = tuple(params or ())

    def jump_to(self, *a):
        self.order.append("jump_to")

    def set_marker_visible(self, v):
        self.order.append(("marker", bool(v)))


def _tab(monkeypatch):
    from PyQt5 import QtWidgets
    from block01.ui.step0 import step0_explore_tab as et

    made = {}

    class _Stack:
        def __init__(self, controller):
            self.controller = controller
            self.provider = self
            self.scheduler = self
            self.view = QtWidgets.QLabel("fake")
            self.caches = ()

        def level_shape(self, _l):
            return (100, 100)

        def shutdown(self):
            pass

        def close(self):
            pass

        def teardown(self, **_kw):
            pass

    def factory(path, channel, parent_widget=None, *, method=None, params=(),
                initial_viewport_l0=None, tint=None):
        ctl = _Ctl()
        ctl.order.append(("built_with_tint", tint))
        made["ctl"] = ctl
        stack = _Stack(ctl)
        stack.view.setParent(parent_widget)
        return stack

    class _Page:
        current_channel = "CD3"

    return et.Step0ExploreTab(_Page(), stack_factory=factory), made


def test_the_warm_path_colours_before_it_moves_the_camera(app, monkeypatch):
    """A channel switch changes the colour too; setting it after the jump
    would paint one frame of the new channel in the old colour."""
    tab, made = _tab(monkeypatch)
    tab.set_dataset("/data/a.ome.tif")
    tab.show_source("CD3", None, ())
    ctl = made["ctl"]
    ctl.order.clear()

    tab.show_source("CD20", "tophat", (15,), viewport_l0=(1, 2, 3, 4),
                    tint=(0.0, 1.0, 0.0))

    assert ("set_tint", (0.0, 1.0, 0.0)) in ctl.order
    assert ctl.order.index("set_selection") < ctl.order.index(
        ("set_tint", (0.0, 1.0, 0.0))) < ctl.order.index("jump_to")
    tab.teardown()


def test_no_tint_leaves_the_colour_alone_on_the_warm_path(app, monkeypatch):
    tab, made = _tab(monkeypatch)
    tab.set_dataset("/data/a.ome.tif")
    tab.show_source("CD3", None, (), tint=(1.0, 0.0, 0.0))
    ctl = made["ctl"]
    ctl.order.clear()

    tab.show_source("CD3", None, (), viewport_l0=(1, 2, 3, 4))

    assert not any(isinstance(e, tuple) and e[0] == "set_tint"
                   for e in ctl.order)
    tab.teardown()


# ── 4. the marker toggle ─────────────────────────────────────────────────

def test_the_toggle_reaches_the_live_stack_without_rebuilding(app):
    page = _page(app)
    seen = []
    page._explore_tab.stack = type("S", (), {
        "controller": type("C", (), {
            "set_marker_visible": lambda _s, v: seen.append(bool(v))})()})()

    page._btn_full_marker.setChecked(False)
    page._btn_full_marker.setChecked(True)

    assert seen == [False, True]
    # Display-only: nothing was asked of the viewer beyond visibility.
    assert page._explore_tab.calls == []


def test_the_toggle_is_harmless_with_no_stack(app):
    page = _page(app)
    page._explore_tab.stack = None

    page._btn_full_marker.setChecked(False)          # must not raise

    assert page._btn_full_marker.isChecked() is False


def test_a_new_stack_is_told_the_toggle_state(app):
    """A build always comes up visible, so a stack built while the marker is
    off would ignore the button."""
    page = _page(app)
    seen = []
    page._explore_tab.stack = type("S", (), {
        "controller": type("C", (), {
            "set_marker_visible": lambda _s, v: seen.append(bool(v))})()})()
    page._btn_full_marker.setChecked(False)
    seen.clear()

    page._enter_full_image("original")

    assert seen == [False]


# ── 5. the controller really recolours every layer ───────────────────────

def test_set_tint_colours_every_layer_the_controller_owns(app):
    """Overview, corrected floor and BOTH pools -- and the pools must keep
    the table for tiles that arrive later, which is the case a caller that
    only walked the existing items would miss."""
    from PyQt5 import QtCore

    class _Item:
        def __init__(self):
            self.lut = "unset"
            self.opacity = 1.0

        def setLookupTable(self, lut):
            self.lut = lut

        def setOpacity(self, a):
            self.opacity = a

    class _Pool:
        def __init__(self):
            self.lut = "unset"
            self.opacity = 1.0

        def set_lookup_table(self, lut):
            self.lut = lut

        def set_layer_opacity(self, a):
            self.opacity = a

    ctl = ExploreController.__new__(ExploreController)
    ctl.view = type("V", (), {})()
    ctl.view.overview_item = _Item()
    ctl.view.corrected_floor_item = _Item()
    ctl._raw_pool = _Pool()
    ctl._precise_pool = _Pool()

    ctl.set_tint((0.0, 1.0, 0.0))

    for layer in (ctl.view.overview_item, ctl.view.corrected_floor_item,
                  ctl._raw_pool, ctl._precise_pool):
        assert isinstance(layer.lut, np.ndarray), layer
        assert tuple(layer.lut[255]) == (0, 255, 0)

    ctl.set_marker_visible(False)
    for layer in (ctl.view.overview_item, ctl.view.corrected_floor_item,
                  ctl._raw_pool, ctl._precise_pool):
        assert layer.opacity == 0.0
    ctl.set_marker_visible(True)
    for layer in (ctl.view.overview_item, ctl.view.corrected_floor_item,
                  ctl._raw_pool, ctl._precise_pool):
        assert layer.opacity == 1.0

    ctl.set_tint(None)
    assert ctl.view.overview_item.lut is None
    assert ctl._raw_pool.lut is None


def test_a_tile_arriving_after_the_tint_is_coloured_too(app):
    """The pool owns the table precisely because items are created as tiles
    land: a caller that walked only the existing entries would leave every
    later tile grey."""
    from block01.viewer.explore_view import TileItemPool
    from PyQt5.QtCore import QRectF

    class _Box:
        def __init__(self):
            self.items = []

        def addItem(self, item):
            self.items.append(item)

        def removeItem(self, item):
            self.items.remove(item)

    box = _Box()
    pool = TileItemPool(box, base_z=100, num_levels=3, budget=8)
    lut = ExploreController.build_tint_lut((0.0, 1.0, 0.0))
    pool.set_lookup_table(lut)
    pool.set_layer_opacity(0.0)

    arr = np.full((4, 4), 200, np.uint8)
    entry = pool.put(0, 1, 1, QRectF(0, 0, 4, 4), arr, key="k")

    assert entry.item.lut is not None
    assert np.array_equal(entry.item.lut, lut)
    assert entry.item.opacity() == 0.0
