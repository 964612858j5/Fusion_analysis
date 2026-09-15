"""The Method popup's Save reaches the REAL compare panels.

The tests that first pinned this replaced `_sync_compare_params` /
`_sync_full_image_param` with recorders, so all they proved was that a
refresh was ATTEMPTED. The review was right that this is not the same claim:
the real synchroniser filters by method, the real strip filters again by
panel, and the branch that pushes them was reached only for channels with no
per-channel parameter override.

So nothing here is stubbed. The page is real, the compare strip is real, and
what is asserted is the `set_selection` each panel controller actually
received -- the call that re-issues that panel's tiles.

Own module: page-heavy Step0 suites crash pyqtgraph offscreen when combined.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtTest  # noqa: E402

from block01.ui.step0 import compare_strip as cs  # noqa: E402
from block01.ui.step0 import step0_page as sp  # noqa: E402

from test_step0_compare_tiles import (  # noqa: E402
    _LowresLoader, _Stack, _Tab, _fake_compare_factory,
    app,            # noqa: F401  (pytest fixture)
)


def _page(app, channel="CD3"):
    """The same real page the compare-tile suite drives: a real strip built
    by a factory that makes real `ExploreView`s."""
    page = sp.Step0Page()
    page.loader = _LowresLoader()
    page.ome_path = "/fake/slide.ome.tif"
    page.patches = []
    page.nucleus_channel = "DAPI"
    page._rebuild_channel_list()
    page.current_channel = channel
    page._explore_tab = _Tab(_Stack())
    page._compare_builds = []
    page._compare_strip_widget._stack_factory = _fake_compare_factory(
        page._compare_builds)
    page.resize(1200, 800)
    page.show()
    QtTest.QTest.qWait(30)
    return page


def _enter_compare(page):
    """Right-click the full image, as a user does, and let the relayout run."""
    cx, cy, _s = page._full_image_camera()
    page._on_full_image_right_click(float(cx), float(cy))
    QtTest.QTest.qWait(30)
    QtTest.QTest.qWait(10)
    strip = page._compare_strip_widget
    assert strip.built, "the compare strip did not build"
    assert page._compare_mode() is True
    return strip


def _panels(strip):
    """`{method: controller}` for the two corrected panels."""
    out = {}
    for source, controller in zip(cs.COMPARE_SOURCES, strip.controllers):
        method = cs.COMPARE_METHODS.get(source)
        if method is not None:
            out[method] = controller
    return out


def _set_method(page, method, **params):
    """Drive the REAL Method popup: toggles, optional numbers, then Save."""
    page._method_tophat_btn.setChecked(method in ("both", "tophat"))
    page._method_cucim_btn.setChecked(method in ("both", "cucim"))
    if "radius" in params:
        page._method_tophat_param.setValue(int(params["radius"]))
    if "sigma" in params:
        page._method_cucim_param.setValue(int(params["sigma"]))
    page._on_method_menu_saved()
    for _ in range(3):
        QtTest.QTest.qWait(20)


def test_choosing_a_method_redraws_the_real_panel_with_no_number_touched(app):
    """The reported bug, against the real strip: switching the method has to
    re-issue that panel's tiles even though no parameter changed."""
    page = _page(app)
    try:
        _set_method(page, "tophat")
        strip = _enter_compare(page)
        panels = _panels(strip)
        before = {m: len(c.selections) for m, c in panels.items()}

        _set_method(page, "cucim")          # no number touched

        assert len(panels["cucim"].selections) > before["cucim"], \
            "the cuCIM panel was never re-selected"
        assert panels["cucim"].selections[-1][1] == "cucim"
        # ...and the method nobody is previewing was left alone
        assert len(panels["tophat"].selections) == before["tophat"]
    finally:
        page.close()


def test_a_channel_with_its_own_parameter_is_redrawn_too(app):
    """A local override is the user's number, not a reason to stop drawing.

    The redraw used to live inside the branch that INHERITS the global value
    into the Per-Channel Decision boxes, which a channel with an override
    never enters -- so its panels kept the old picture when the method moved.
    """
    page = _page(app)
    try:
        page._channel_params["CD3"] = {"tophat_radius": 21, "cucim_sigma": 33}
        _set_method(page, "tophat")
        strip = _enter_compare(page)
        panels = _panels(strip)
        before = {m: len(c.selections) for m, c in panels.items()}
        # the override really is in force
        assert page._resolve_channel_params("CD3") == (21, 33)

        _set_method(page, "cucim")

        assert len(panels["cucim"].selections) > before["cucim"], \
            "a channel with a local parameter was never redrawn"
        channel, method, params = panels["cucim"].selections[-1]
        assert method == "cucim"
        # drawn with ITS OWN number, not the global one
        assert 33 in tuple(params), params
        # ...and the override is untouched by the Save
        assert page._channel_params["CD3"] == {"tophat_radius": 21,
                                               "cucim_sigma": 33}
    finally:
        page.close()


def test_a_number_for_the_method_in_use_reaches_the_real_panel(app):
    page = _page(app)
    try:
        _set_method(page, "cucim")
        strip = _enter_compare(page)
        panels = _panels(strip)
        before = len(panels["cucim"].selections)

        _set_method(page, "cucim", sigma=7)

        assert len(panels["cucim"].selections) > before
        assert 7 in tuple(panels["cucim"].selections[-1][2])
    finally:
        page.close()


def test_a_number_for_an_unlit_method_reaches_no_panel(app):
    """Saved, but not computed -- measured at the panel, not at a recorder."""
    page = _page(app)
    try:
        _set_method(page, "cucim")
        strip = _enter_compare(page)
        panels = _panels(strip)
        before = {m: len(c.selections) for m, c in panels.items()}

        _set_method(page, "cucim", radius=44)      # TopHat is dark

        assert page._tophat_slider.value() == 44, "the number was not saved"
        assert len(panels["tophat"].selections) == before["tophat"], \
            "an unlit method's panel was recomputed"
    finally:
        page.close()


def test_both_lit_redraws_both_real_panels(app):
    page = _page(app)
    try:
        _set_method(page, "original")
        strip = _enter_compare(page)
        panels = _panels(strip)
        before = {m: len(c.selections) for m, c in panels.items()}

        _set_method(page, "both", radius=19, sigma=61)

        for method in ("tophat", "cucim"):
            assert len(panels[method].selections) > before[method], method
        assert 19 in tuple(panels["tophat"].selections[-1][2])
        assert 61 in tuple(panels["cucim"].selections[-1][2])
    finally:
        page.close()
