"""Full-image brightness sliders, and why they exist.

The compare panels stretch each PATCH by its own 1st..99.5th percentiles;
the full image uses one fixed range from the whole slide. On the test slide
the user's patch of TOX spans 1..9 in the compare panels against 0..17 in
the full image, so the same tissue reads about half as bright there. No
code compresses anything; the ranges differ by construction, and a
slide-wide view needs a slide-wide range. The toolbar therefore gets a
brightness slider per layer, applied as paint-time `levels` to every marker
layer (overview, floor, raw and precise pools) and to the DAPI overlay --
nothing is re-read or re-quantised.

Own module: page-heavy Step0 suites crash pyqtgraph offscreen when combined
with the background-correction module in one process.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtTest  # noqa: E402

from block01.ui.step0 import step0_page as sp  # noqa: E402

from test_step0_background_correction_tab import app  # noqa: E402,F401


class _Overlay:
    def __init__(self):
        self.contrast = []

    def set_contrast(self, s):
        self.contrast.append(s)


class _Ctl:
    def __init__(self):
        self.contrast = []
        self.channel, self.method, self.params = "CD3", None, ()

    def set_marker_contrast(self, s):
        self.contrast.append(s)

    def set_marker_visible(self, v):
        pass

    def set_tint(self, rgb):
        pass


class _Stack:
    def __init__(self):
        self.controller = _Ctl()
        self.overlay = _Overlay()
        self.provider = self
        self.view = None

    def level_shape(self, _l):
        return (1000, 1000)


class _Tab:
    def __init__(self, stack):
        self.stack = stack

    def show_source(self, *a, **k):
        return True

    def set_dataset(self, _p):
        pass

    def teardown(self, **_k):
        pass


def _page(app, stack):
    page = sp.Step0Page()
    page.current_channel = "CD3"
    page.nucleus_channel = "DAPI"
    page._explore_tab = _Tab(stack)
    return page


def test_the_sliders_drive_the_live_layers(app):
    stack = _Stack()
    page = _page(app, stack)

    page._full_marker_contrast.setValue(50)
    page._full_nucleus_contrast.setValue(200)

    assert stack.controller.contrast == [0.5]
    assert stack.overlay.contrast == [2.0]
    assert page._full_marker_contrast_lbl.text() == "50%"
    assert page._full_nucleus_contrast_lbl.text() == "200%"


def test_a_new_stack_is_told_the_slider_positions(app):
    stack = _Stack()
    page = _page(app, None)
    page._explore_tab.stack = None
    page._full_marker_contrast.setValue(60)      # no stack: remembered only
    page._full_nucleus_contrast.setValue(150)

    page._explore_tab.stack = stack
    page._apply_full_image_contrast(stack)

    assert stack.controller.contrast == [0.6]
    assert stack.overlay.contrast == [1.5]


def test_the_sliders_are_harmless_with_no_stack(app):
    page = _page(app, None)
    page._explore_tab.stack = None
    page._full_marker_contrast.setValue(40)
    page._full_nucleus_contrast.setValue(40)
    assert page._full_marker_contrast_lbl.text() == "40%"
