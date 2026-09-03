"""A recompute of the patch on screen keeps the compare panels' zoom.

Reported from manual testing: with the three panels zoomed in, changing a
Per-Channel Decision parameter and pressing Enter recomputed the patch and
threw the zoom away -- the panels auto-ranged back to the whole patch. The
result replaces the SAME pixel grid, so the camera has no reason to move.
A result for a different-sized patch, or the very first result, still
auto-ranges: the previous rectangle means nothing on a new grid.

Own module: the page-heavy Step0 suites crash pyqtgraph offscreen when
combined with the background-correction module in one process.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from block01.ui.step0 import step0_page as sp  # noqa: E402

from test_step0_background_correction_tab import (  # noqa: E402,F401
    _GpuPathLoader,
    app,
)


def _payload(h, w, value=0.5):
    disp = np.full((h, w), value, np.float32)
    metrics = {"snr": 4.0, "bg_cv": 0.25}
    return {
        "original_disp": disp, "tophat_disp": disp, "cucim_disp": disp,
        "original_metrics": metrics, "tophat_metrics": metrics,
        "cucim_metrics": metrics, "nucleus_disp": None,
    }


def _page(app):
    page = sp.Step0Page()
    page.loader = _GpuPathLoader()
    page.patches = [(0, 64, 0, 64)]
    page.current_patch_idx = 0
    page.nucleus_channel = "DAPI"
    page._rebuild_channel_list()
    page.current_channel = "CD3"
    page.resize(1400, 900)
    page.show()
    return page


def _zoom_in(page):
    """Zoom every panel to a small corner and return what they report.
    The zoom lock is left ON, as a user has it: the ranges must agree."""
    page._btn_lock_zoom.setChecked(True)
    page._preview_vbs[0].setRange(xRange=(10, 20), yRange=(30, 40), padding=0)
    return [vb.viewRange() for vb in page._preview_vbs]


def _ranges(page):
    return [vb.viewRange() for vb in page._preview_vbs]


def _same(a, b):
    """Range lists equal up to float noise: `setRange` on an aspect-locked
    ViewBox recomputes the other axis, which can move by 1e-14."""
    return np.allclose(np.asarray(a, dtype=float), np.asarray(b, dtype=float),
                       rtol=0, atol=1e-6)


def test_recomputing_the_same_patch_keeps_the_zoom(app):
    page = _page(app)
    page._on_batch_patch_done("CD3", 0, _payload(64, 64))
    zoomed = _zoom_in(page)
    assert not _same(zoomed, _ranges_after_autorange(page)), "zoom did not take"

    page._on_batch_patch_done("CD3", 0, _payload(64, 64, value=0.9))

    assert _same(_ranges(page), zoomed)
    assert page._last_payload["original_disp"][0, 0] == np.float32(0.9), (
        "the new result must still replace the old pixels")


def test_the_preview_worker_path_keeps_the_zoom_too(app):
    """`_on_preview_ready` is the other place a finished computation lands
    and used to auto-range as well."""
    page = _page(app)
    page._on_batch_patch_done("CD3", 0, _payload(64, 64))
    zoomed = _zoom_in(page)

    page._preview_req_id = 7
    page._on_preview_ready(7, _payload(64, 64, value=0.2))

    assert _same(_ranges(page), zoomed)


def test_the_first_result_still_auto_ranges(app):
    """Nothing is on screen yet, so there is no zoom to keep."""
    page = _page(app)
    before = _ranges(page)

    page._on_batch_patch_done("CD3", 0, _payload(64, 64))

    assert not _same(_ranges(page), before)


def test_a_result_of_a_different_size_auto_ranges(app):
    """A different patch is a different pixel grid: the old rectangle would
    show an arbitrary corner of it."""
    page = _page(app)
    page._on_batch_patch_done("CD3", 0, _payload(64, 64))
    zoomed = _zoom_in(page)

    page._on_batch_patch_done("CD3", 0, _payload(96, 48))

    assert not _same(_ranges(page), zoomed)


def test_the_decision_is_taken_on_the_pixel_grid_only(app):
    page = _page(app)
    assert page._recompute_keeps_zoom(None, _payload(8, 8)) is False
    assert page._recompute_keeps_zoom(_payload(8, 8), _payload(8, 8)) is True
    assert page._recompute_keeps_zoom(_payload(8, 8), _payload(8, 9)) is False
    assert page._recompute_keeps_zoom({}, _payload(8, 8)) is False
    # A payload carrying only one method still has a grid.
    only_cucim = {"cucim_disp": np.zeros((8, 8), np.float32)}
    assert page._recompute_keeps_zoom(only_cucim, _payload(8, 8)) is True


def _ranges_after_autorange(page):
    """What the panels would show if auto-ranged -- computed on a scratch
    copy of the ranges so the page under test is not touched."""
    saved = _ranges(page)
    page._zoom_lock_active = True
    try:
        for vb in page._preview_vbs:
            vb.autoRange()
        auto = _ranges(page)
        for vb, (xr, yr) in zip(page._preview_vbs, saved):
            vb.setRange(xRange=xr, yRange=yr, padding=0)
    finally:
        page._zoom_lock_active = False
    return auto
