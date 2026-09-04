"""The compare panels paint raw intensity through the channel's display
mapping -- the same numbers the full image uses -- and never normalise.

Each panel holds the raw (or background-corrected) array with
`levels=(min, max)` and a lookup table carrying gamma and colour; the
nucleus is a second item ADDED on top (CompositionMode_Plus) under its own
channel's mapping. Per pixel that is
clip(marker_colour*clip((m-min)/(max-min))**gamma + nucleus_colour*...),
proven here against `_make_colored_rgb` computed on the mapped values.

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
    # The compare strip is collapsed until a snapshot asks for it; these
    # tests are about what it PAINTS, so it is expanded by hand.
    page._set_compare_strip_visible(True)
    page.resize(1500, 900)
    page.show()
    QtTest.QTest.qWait(50)
    return page


def _payload(marker=500.0, nucleus=300.0, h=64, w=64, with_nucleus=True,
             tophat_scale=0.5, cucim=True):
    """Raw-intensity payload the way the worker builds it."""
    m = np.full((h, w), marker, np.float32)
    metrics = {"snr": 1.0, "bg_cv": 0.1}
    return {"original_raw": m, "tophat_raw": m * tophat_scale,
            "cucim_raw": (m * 0.8) if cucim else None,
            "original_metrics": metrics, "tophat_metrics": metrics,
            "cucim_metrics": metrics,
            "nucleus_raw": np.full((h, w), nucleus, np.float32) if with_nucleus else None}


def _centre_pixel(page, idx):
    vb = page._preview_vbs[idx]
    vb.autoRange()
    QtTest.QTest.qWait(30)
    img = page._preview_gv.grab().toImage()
    rect = vb.sceneBoundingRect()
    p = page._preview_gv.mapFromScene(rect.center())
    c = img.pixelColor(int(p.x()), int(p.y()))
    return np.array([c.red(), c.green(), c.blue()]) / 255.0


def _mapped(v, lo, hi, gamma):
    return float(np.clip((v - lo) / (hi - lo), 0, 1) ** gamma)


def _expected(page, marker, nucleus, m_map, n_map, marker_on=True, nucleus_on=True):
    ch = page.current_channel
    marker_rgb = page._channel_colors.get(ch, getattr(page, "_marker_color", (0.0, 1.0, 0.3)))
    m = np.array([[_mapped(marker, *m_map) if marker_on else 0.0]], np.float32)
    n = (np.array([[_mapped(nucleus, *n_map)]], np.float32)
         if nucleus_on and nucleus is not None else None)
    return page._make_colored_rgb(m, n, marker_rgb=marker_rgb, nucleus_rgb=page._nuc_color)[0, 0]


def _show(page, payload):
    page._on_batch_patch_done("CD3", 0, payload)
    QtTest.QTest.qWait(30)


def test_the_painted_pixel_is_the_mapping_of_the_raw_value(app):
    page = _page(app)
    page._channel_colors["CD3"] = (0.0, 1.0, 0.0)
    page._nuc_color = (0.0, 0.5, 1.0)
    page._btn_show_nucleus.setChecked(True)   # (v15) DAPI starts off
    page.set_display_mapping("CD3", 100.0, 900.0, 1.0)
    page.set_display_mapping("DAPI", 0.0, 1000.0, 1.0)
    _show(page, _payload(marker=500.0, nucleus=400.0))

    got = _centre_pixel(page, 0)
    want = _expected(page, 500.0, 400.0, (100.0, 900.0, 1.0), (0.0, 1000.0, 1.0))

    assert np.allclose(got, want, atol=2 / 255), (got, want)
    assert got[1] > 0.45 and got[2] > 0.3        # added, not painted over


def test_the_three_panels_share_one_mapping_so_a_darker_result_is_darker(app):
    """The worker's TopHat result here is half the original: under one
    shared mapping it must paint at half the brightness, not be re-stretched
    to look as bright."""
    page = _page(app)
    page._channel_colors["CD3"] = (1.0, 1.0, 1.0)
    page.set_display_mapping("CD3", 0.0, 1000.0, 1.0)
    page._btn_show_nucleus.setChecked(False)
    _show(page, _payload(marker=600.0, tophat_scale=0.5))

    orig = _centre_pixel(page, 0)
    tophat = _centre_pixel(page, 1)

    assert np.allclose(orig, [0.6] * 3, atol=2 / 255)
    assert np.allclose(tophat, [0.3] * 3, atol=2 / 255)


def test_gamma_and_range_changes_are_table_and_level_swaps(app):
    page = _page(app)
    page._channel_colors["CD3"] = (1.0, 0.0, 0.0)
    page._btn_show_nucleus.setChecked(True)   # (v15) DAPI starts off
    page.set_display_mapping("CD3", 0.0, 1000.0, 1.0)
    page.set_display_mapping("DAPI", 0.0, 1000.0, 1.0)
    _show(page, _payload(marker=400.0, nucleus=200.0))
    before = _centre_pixel(page, 0)
    image_before = page._preview_imgs[0].image

    page.set_display_mapping("CD3", 0.0, 500.0, 0.5)         # brighter, gamma
    page.set_display_mapping("DAPI", 0.0, 400.0, 1.0)
    QtTest.QTest.qWait(30)
    after = _centre_pixel(page, 0)

    assert not np.allclose(before, after)
    assert np.allclose(after, _expected(page, 400.0, 200.0, (0.0, 500.0, 0.5), (0.0, 400.0, 1.0)),
                       atol=2 / 255)
    # No per-pixel work: the item still holds the same raw buffer.
    assert np.shares_memory(page._preview_imgs[0].image, image_before), "the pixels were rebuilt"
    assert page._preview_imgs[0].image.dtype == np.float32
    assert list(page._preview_imgs[0].levels) == pytest.approx([0.0, 500.0])


def test_the_controls_show_and_edit_the_current_channels_mapping(app):
    """The min/max/gamma controls are the Channel Remap inspector itself,
    hosted in the floating Intensity window; they show and edit the same
    mapping the compare panels paint through."""
    page = _page(app)
    page._sync_step0_to_workbench()
    wb = page._cond_workbench
    page.set_display_mapping("CD3", 10.0, 800.0, 1.5)
    page.set_display_mapping("DAPI", 5.0, 900.0, 0.9)
    _show(page, _payload())
    page.show_intensity_window()
    wb.set_active_channel("CD3")

    assert page._display_mapping_for("CD3") == pytest.approx((10.0, 800.0, 1.5))
    assert page._display_mapping_for("DAPI")[1] == pytest.approx(900.0)
    assert wb._sp_min.value() == pytest.approx(10.0)
    assert wb._sp_max.value() == pytest.approx(800.0)

    wb._sp_max.setValue(600.0)
    assert page._display_mapping_for("CD3") == pytest.approx((10.0, 600.0, 1.5))
    assert list(page._preview_imgs[0].levels) == pytest.approx([10.0, 600.0])


def test_the_seed_comes_from_the_payload_when_the_slide_cannot_be_read(app):
    """A loader without a pyramid (the fakes) cannot seed from the slide,
    so the first result's own raw pixels seed the mapping -- QuPath-style
    0.1/99.9 over non-zero pixels."""
    page = _page(app)
    rng = np.random.default_rng(3)
    p = _payload()
    p["original_raw"] = rng.uniform(100, 2000, size=(64, 64)).astype(np.float32)
    _show(page, p)
    lo, hi, gamma = page._display_mapping_for("CD3")
    assert gamma == 1.0
    assert 100 <= lo < 200 and 1900 < hi <= 2000


def test_the_switches_hide_and_show_each_layer(app):
    page = _page(app)
    page._channel_colors["CD3"] = (0.0, 1.0, 0.0)
    page.set_display_mapping("CD3", 0.0, 1000.0, 1.0)
    page.set_display_mapping("DAPI", 0.0, 1000.0, 1.0)
    _show(page, _payload(marker=600.0, nucleus=400.0))
    maps = ((0.0, 1000.0, 1.0), (0.0, 1000.0, 1.0))

    page._btn_show_nucleus.setChecked(False)
    QtTest.QTest.qWait(30)
    assert np.allclose(_centre_pixel(page, 0), _expected(page, 600.0, 400.0, *maps, nucleus_on=False), atol=2 / 255)

    page._btn_show_nucleus.setChecked(True)
    page._btn_show_marker.setChecked(False)
    QtTest.QTest.qWait(30)
    assert np.allclose(_centre_pixel(page, 0), _expected(page, 600.0, 400.0, *maps, marker_on=False), atol=2 / 255)

    page._btn_show_marker.setChecked(False)
    page._btn_show_nucleus.setChecked(False)
    QtTest.QTest.qWait(30)
    assert np.allclose(_centre_pixel(page, 0), (0, 0, 0), atol=2 / 255)


def test_a_result_that_was_not_computed_is_black_with_nothing_added(app):
    page = _page(app)
    _show(page, _payload(cucim=False))
    assert np.allclose(_centre_pixel(page, 2), (0, 0, 0), atol=2 / 255)
    nuc = page._preview_nuc_imgs[2]
    assert nuc is None or nuc.isVisible() is False
    assert page._preview_imgs[2].image.shape == (64, 64)


def test_legacy_normalised_payloads_still_display(app):
    """Older payloads and fixtures carry only `*_disp` arrays in 0..1."""
    page = _page(app)
    disp = np.full((32, 32), 0.5, np.float32)
    m = {"snr": 1.0, "bg_cv": 0.1}
    page._on_batch_patch_done("CD3", 0, {"original_disp": disp, "tophat_disp": disp, "cucim_disp": disp,
                                        "original_metrics": m, "tophat_metrics": m, "cucim_metrics": m,
                                        "nucleus_disp": None})
    assert page._preview_imgs[0].image is not None
    lo, hi, _g = page._display_mapping_for("CD3")
    assert lo <= 0.5 <= hi


def test_a_switch_between_cached_channels_is_cheap(app):
    page = _page(app)
    big = 1024
    a = _payload(500.0, 300.0, big, big)
    b = _payload(700.0, 200.0, big, big)
    page._on_batch_patch_done("CD3", 0, a)
    page._preview_cache[("CD20", 0)] = b
    page._computed_channels = {"CD3", "CD20"}
    page._show_channel_from_cache("CD20")
    page._show_channel_from_cache("CD3")

    t = time.perf_counter()
    for ch in ("CD20", "CD3", "CD20", "CD3"):
        page._show_channel_from_cache(ch)
    per_switch_ms = (time.perf_counter() - t) * 1000 / 4

    assert per_switch_ms < 80, f"{per_switch_ms:.0f} ms per switch"
    assert page._last_payload is a


def test_nucleus_items_are_created_on_first_use_additive_and_row_major(app):
    page = _page(app)
    page._btn_show_nucleus.setChecked(True)   # (v15) DAPI starts off
    assert page._preview_nuc_imgs == [None, None, None]
    _show(page, _payload(cucim=False))
    assert page._preview_nuc_imgs[0] is not None and page._preview_nuc_imgs[1] is not None
    assert page._preview_nuc_imgs[2] is None
    for item in page._preview_nuc_imgs[:2]:
        assert item.axisOrder == "row-major"
        assert item.paintMode == QtGui.QPainter.CompositionMode_Plus
