"""The compare panels paint what `_make_colored_rgb` computes -- without
computing it.

Channel switching in compare mode took ~850 ms per switch on a 2720x2336
patch: two float32 RGB composites per result plus a third in the refresh,
all on the GUI thread, for three panels. The panels now hold a uint8 marker
image with a colour lookup table and the contrast as `levels`, and a second
image item for the nucleus that is ADDED on top (CompositionMode_Plus). The
sum the painter computes must be the sum the numpy code computed, so the
proof here is a pixel comparison against `_make_colored_rgb` itself.

Own module: page-heavy Step0 suites crash pyqtgraph offscreen when combined
with the background-correction module in one process.
"""

import os
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtGui, QtTest  # noqa: E402

from block01.ui.step0 import step0_page as sp  # noqa: E402

from test_step0_background_correction_tab import _GpuPathLoader, app  # noqa: E402,F401


def _page(app):
    page = sp.Step0Page()
    page.loader = _GpuPathLoader()
    page.patches = [(0, 64, 0, 64)]
    page.current_patch_idx = 0
    page.nucleus_channel = "DAPI"
    page._rebuild_channel_list()
    page.current_channel = "CD3"
    page.resize(1500, 900)
    page.show()
    QtTest.QTest.qWait(50)
    return page


def _payload(marker=0.5, nucleus=0.3, h=64, w=64, with_nucleus=True):
    m = np.full((h, w), marker, np.float32)
    metrics = {"snr": 1.0, "bg_cv": 0.1}
    return {"original_disp": m, "tophat_disp": m * 0.5, "cucim_disp": None,
            "original_metrics": metrics, "tophat_metrics": metrics,
            "cucim_metrics": metrics,
            "nucleus_disp": np.full((h, w), nucleus, np.float32) if with_nucleus else None}


def _centre_pixel(page, idx):
    """The painted colour at the centre of panel `idx`, as floats 0..1."""
    vb = page._preview_vbs[idx]
    vb.autoRange()
    QtTest.QTest.qWait(30)
    img = page._preview_gv.grab().toImage()
    rect = vb.sceneBoundingRect()
    p = page._preview_gv.mapFromScene(rect.center())
    c = img.pixelColor(int(p.x()), int(p.y()))
    return np.array([c.red(), c.green(), c.blue()]) / 255.0


def _expected(page, marker, nucleus, marker_scale=1.0, nucleus_scale=1.0,
              marker_on=True, nucleus_on=True):
    ch = page.current_channel
    marker_rgb = page._channel_colors.get(ch, getattr(page, "_marker_color", (0.0, 1.0, 0.3)))
    m = np.array([[min(1.0, marker / marker_scale) if marker_on else 0.0]], np.float32)
    n = (np.array([[min(1.0, nucleus / nucleus_scale)]], np.float32)
         if nucleus_on and nucleus is not None else None)
    return page._make_colored_rgb(m, n, marker_rgb=marker_rgb,
                                  nucleus_rgb=page._nuc_color)[0, 0]


def _show(page, payload):
    page._on_batch_patch_done("CD3", 0, payload)
    QtTest.QTest.qWait(30)


def test_the_painted_pixel_is_the_numpy_sum(app):
    page = _page(app)
    page._channel_colors["CD3"] = (0.0, 1.0, 0.0)
    page._nuc_color = (0.0, 0.5, 1.0)
    _show(page, _payload(marker=0.6, nucleus=0.4))

    got = _centre_pixel(page, 0)
    want = _expected(page, 0.6, 0.4)

    assert np.allclose(got, want, atol=2 / 255), (got, want)
    # And the nucleus really is added, not painted over: green from the
    # marker survives under the blue nucleus.
    assert got[1] > 0.5 and got[2] > 0.3


def test_contrast_sliders_change_the_pixel_without_recomputing(app):
    page = _page(app)
    page._channel_colors["CD3"] = (1.0, 0.0, 0.0)
    _show(page, _payload(marker=0.4, nucleus=0.2))
    before = _centre_pixel(page, 0)
    quantised = page._last_payload["_u8"]

    page._marker_contrast_slider.setValue(50)        # scale 0.5 -> marker doubles
    page._nuc_contrast_slider.setValue(50)
    QtTest.QTest.qWait(30)
    after = _centre_pixel(page, 0)

    assert not np.allclose(before, after)
    assert np.allclose(after, _expected(page, 0.4, 0.2, 0.5, 0.5), atol=2 / 255)
    assert page._last_payload["_u8"] is quantised, "the slider re-quantised the pixels"


def test_the_switches_hide_and_show_each_layer(app):
    page = _page(app)
    page._channel_colors["CD3"] = (0.0, 1.0, 0.0)
    _show(page, _payload(marker=0.6, nucleus=0.4))

    page._btn_show_nucleus.setChecked(False)
    QtTest.QTest.qWait(30)
    assert np.allclose(_centre_pixel(page, 0), _expected(page, 0.6, 0.4, nucleus_on=False), atol=2 / 255)

    page._btn_show_nucleus.setChecked(True)
    page._btn_show_marker.setChecked(False)
    QtTest.QTest.qWait(30)
    assert np.allclose(_centre_pixel(page, 0), _expected(page, 0.6, 0.4, marker_on=False), atol=2 / 255)

    page._btn_show_marker.setChecked(False)
    page._btn_show_nucleus.setChecked(False)
    QtTest.QTest.qWait(30)
    assert np.allclose(_centre_pixel(page, 0), (0, 0, 0), atol=2 / 255)


def test_a_result_that_was_not_computed_is_black_with_nothing_added(app):
    page = _page(app)
    _show(page, _payload(marker=0.6, nucleus=0.4))     # cucim_disp is None

    assert np.allclose(_centre_pixel(page, 2), (0, 0, 0), atol=2 / 255)
    nuc = page._preview_nuc_imgs[2]
    assert nuc is None or nuc.isVisible() is False
    assert page._preview_imgs[2].image.shape == (64, 64), "geometry kept for the zoom"


def test_the_colour_change_is_a_table_swap(app):
    page = _page(app)
    page._channel_colors["CD3"] = (0.0, 1.0, 0.0)
    _show(page, _payload(marker=0.6, nucleus=0.0))
    quantised = page._last_payload["_u8"]

    page._channel_colors["CD3"] = (1.0, 0.0, 0.0)
    page._rebuild_payload_rgb("CD3")
    page._refresh_preview_display(keep_zoom=True)
    QtTest.QTest.qWait(30)

    got = _centre_pixel(page, 0)
    assert got[0] > 0.5 and got[1] < 0.05, got
    assert page._last_payload["_u8"] is quantised
    assert "original_rgb" not in page._last_payload, "no composite is kept any more"


def test_a_switch_between_cached_channels_is_cheap(app):
    """The reason for the change. Both channels quantised once; the second
    and later switches do no per-pixel work at all."""
    page = _page(app)
    big = 1024
    a = _payload(0.5, 0.3, big, big)
    b = _payload(0.7, 0.2, big, big)
    page._on_batch_patch_done("CD3", 0, a)
    page._preview_cache[("CD20", 0)] = b
    page._computed_channels = {"CD3", "CD20"}
    page._show_channel_from_cache("CD20")            # first show: quantise
    page._show_channel_from_cache("CD3")

    t = time.perf_counter()
    for ch in ("CD20", "CD3", "CD20", "CD3"):
        page._show_channel_from_cache(ch)
    per_switch_ms = (time.perf_counter() - t) * 1000 / 4

    assert per_switch_ms < 60, f"{per_switch_ms:.0f} ms per switch"
    assert page._last_payload is a


def test_nucleus_items_are_created_on_first_use_additive_and_row_major(app):
    """Deferred, like the full-image viewer: three more scene items per page
    made the Step0 suite's known offscreen crash deterministic."""
    page = _page(app)
    assert page._preview_nuc_imgs == [None, None, None]

    _show(page, _payload(marker=0.6, nucleus=0.4))      # cucim result absent

    assert page._preview_nuc_imgs[0] is not None and page._preview_nuc_imgs[1] is not None
    assert page._preview_nuc_imgs[2] is None, "no nucleus item for a panel with no result"
    for item in page._preview_nuc_imgs[:2]:
        assert item.axisOrder == "row-major"
        assert item.paintMode == QtGui.QPainter.CompositionMode_Plus
        assert item.zValue() > page._preview_imgs[0].zValue()
    assert page._nuc_item(0) is page._preview_nuc_imgs[0]
