"""The display mapping controls live in a floating "Display" window.

The compare view's header used to carry two rows of min/max/gamma spin boxes
plus two Marker/Nucleus toggle buttons — four rows of chrome for numbers a
user touches once per channel. They now live in `DisplayMappingPopup`, a
second floating window (the Tissue Navigator stays navigation-only), opened
from the header's "Display…" button.

The popup owns nothing: the page owns one display mapping per channel and the
popup is a view over it. These tests pin that direction of ownership, the
"Use as segmentation remap" hand-off, and the fact that nothing in the
Display window can start a computation — including for DAPI, which is never
background-corrected.

Own module: page-heavy Step0 suites crash pyqtgraph offscreen when combined
with the background-correction module in one process.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtTest, QtWidgets  # noqa: E402
from PyQt5.QtCore import Qt  # noqa: E402

from block01.ui.step0 import step0_page as sp  # noqa: E402
from block01.ui.widgets.display_mapping_popup import DisplayMappingPopup  # noqa: E402

from test_step0_background_correction_tab import _GpuPathLoader, app  # noqa: E402,F401


def _page(app, show=False):
    page = sp.Step0Page()
    page.loader = _GpuPathLoader()
    page.patches = [(0, 64, 0, 64)]
    page.current_patch_idx = 0
    page.nucleus_channel = "DAPI"
    page._rebuild_channel_list()
    page.current_channel = "CD3"
    if show:
        page.resize(1500, 900)
        page.show()
        QtTest.QTest.qWait(50)
    return page


def _payload(marker=500.0, nucleus=300.0, h=64, w=64):
    m = np.full((h, w), marker, np.float32)
    metrics = {"snr": 1.0, "bg_cv": 0.1}
    return {"original_raw": m, "tophat_raw": m * 0.5, "cucim_raw": m * 0.8,
            "original_metrics": metrics, "tophat_metrics": metrics,
            "cucim_metrics": metrics,
            "nucleus_raw": np.full((h, w), nucleus, np.float32)}


def _show(page, payload=None):
    page._on_batch_patch_done("CD3", 0, payload or _payload())
    QtTest.QTest.qWait(30)


def _centre_pixel(page, idx):
    vb = page._preview_vbs[idx]
    vb.autoRange()
    QtTest.QTest.qWait(30)
    img = page._preview_gv.grab().toImage()
    p = page._preview_gv.mapFromScene(vb.sceneBoundingRect().center())
    c = img.pixelColor(int(p.x()), int(p.y()))
    return np.array([c.red(), c.green(), c.blue()]) / 255.0


# ── 1. one lazily created, separate top-level window ─────────────────────

def test_the_popup_is_created_once_lazily_and_is_its_own_window(app):
    page = _page(app)
    assert page._display_popup is None, "built eagerly"

    popup = page.show_display_popup()

    assert isinstance(popup, DisplayMappingPopup)
    assert page.show_display_popup() is popup, "a second open rebuilt it"
    assert popup.windowFlags() & Qt.Window
    assert popup.isWindow() and popup.windowTitle() == "Display"
    # Its own window, NOT a child of the navigation-only Tissue Navigator.
    nav = page._ensure_tissue_navigator()
    assert popup.parent() is page
    for child in nav.findChildren(QtWidgets.QWidget):
        assert not isinstance(child, DisplayMappingPopup)


def test_the_header_lost_the_inline_rows_and_kept_a_button(app):
    page = _page(app)
    for gone in ("_marker_display_min", "_marker_display_max", "_nuc_display_gamma",
                 "_on_display_controls_edited"):
        assert not hasattr(page, gone), gone
    assert page._btn_display_popup.text() == "Display…"


def test_toggle_hides_and_shows_the_same_window(app):
    page = _page(app)
    popup = page.toggle_display_popup()
    assert popup.isVisible()
    assert page.toggle_display_popup() is popup and not popup.isVisible()
    assert page.toggle_display_popup() is popup and popup.isVisible()


# ── 2. editing a row writes the page's mapping ───────────────────────────

def test_editing_the_marker_row_sets_the_current_channels_mapping(app):
    page = _page(app, show=True)
    page.set_display_mapping("CD3", 10.0, 800.0, 1.5)
    _show(page)
    popup = page.show_display_popup()

    popup._widgets["marker"]["max"].setValue(600.0)
    popup._widgets["marker"]["gamma"].setValue(0.5)

    assert page._display_mapping_for("CD3") == (10.0, 600.0, 0.5)
    state = page._dock_adapter.model.get("CD3")
    assert (state.display_min, state.display_max, state.display_gamma) == (10.0, 600.0, 0.5)
    assert list(page._preview_imgs[0].levels) == pytest.approx([10.0, 600.0])


def test_editing_the_dapi_row_sets_the_nucleus_mapping(app):
    page = _page(app, show=True)
    page.set_display_mapping("DAPI", 5.0, 900.0, 1.0)
    _show(page)
    popup = page.show_display_popup()

    popup._widgets["nucleus"]["min"].setValue(20.0)

    assert page._display_mapping_for("DAPI") == (20.0, 900.0, 1.0)
    assert page._display_mapping_for("CD3")[0] != 20.0, "the marker was touched"
    assert list(page._preview_nuc_imgs[0].levels) == pytest.approx([20.0, 900.0])


def test_the_dapi_row_is_editable_even_while_dapi_is_the_selected_channel(app):
    """DAPI is excluded from CORRECTION, not from display."""
    page = _page(app, show=True)
    _show(page)
    popup = page.show_display_popup()
    page._on_channel_row_changed(page._channel_order.index("DAPI"))

    popup._widgets["nucleus"]["max"].setValue(777.0)

    assert page._display_mapping_for("DAPI")[1] == 777.0
    assert "excluded from correction" in page._preview_status.text()


def test_auto_reseeds_the_row_from_the_pixels(app):
    page = _page(app)
    rng = np.random.default_rng(1)
    raw = rng.uniform(200, 4000, size=(32, 32)).astype(np.float32)
    p = _payload()
    p["original_raw"] = raw
    _show(page, p)
    page.set_display_mapping("CD3", 0.0, 1.0, 2.0)
    popup = page.show_display_popup()

    popup._widgets["marker"]["auto"].click()

    lo, hi, gamma = page._display_mapping_for("CD3")
    assert 200 <= lo < 300 and 3900 < hi <= 4000 and gamma == 1.0
    # the spin boxes show one decimal, so compare at their resolution
    assert popup.mapping("marker") == pytest.approx((lo, hi, 1.0), abs=0.05)


# ── 3. the Show checkboxes drive the hidden holders and the panels ───────

def test_show_checkboxes_drive_the_hidden_state_holders(app):
    page = _page(app)
    popup = page.show_display_popup()
    assert page._btn_show_marker.isChecked() and page._btn_show_nucleus.isChecked()
    # The holders are state only: never in a layout, never on screen.
    assert page._btn_show_marker.parent() is page
    assert not page._btn_show_marker.isVisible()

    popup._widgets["marker"]["show"].setChecked(False)
    assert page._btn_show_marker.isChecked() is False
    popup._widgets["nucleus"]["show"].setChecked(False)
    assert page._btn_show_nucleus.isChecked() is False

    # …and the page syncs back into the popup without re-emitting.
    page._btn_show_marker.setChecked(True)
    page._sync_display_controls()
    assert popup.is_shown("marker") is True
    assert page._btn_show_marker.isChecked() is True


def test_marker_off_paints_a_black_table(app):
    page = _page(app, show=True)
    page._channel_colors["CD3"] = (0.0, 1.0, 0.0)
    page.set_display_mapping("CD3", 0.0, 1000.0, 1.0)
    page.set_display_mapping("DAPI", 0.0, 1000.0, 1.0)
    _show(page, _payload(marker=600.0, nucleus=400.0))
    popup = page.show_display_popup()

    popup._widgets["marker"]["show"].setChecked(False)
    popup._widgets["nucleus"]["show"].setChecked(False)
    QtTest.QTest.qWait(30)

    assert np.allclose(_centre_pixel(page, 0), (0, 0, 0), atol=2 / 255)
    # black TABLE, not a hidden item: the panel keeps its geometry
    assert page._preview_imgs[0].image.shape == (64, 64)


# ── 4. a channel change re-syncs values and names ────────────────────────

def test_a_channel_change_resyncs_the_popup(app):
    page = _page(app)
    page.set_display_mapping("CD3", 10.0, 800.0, 1.5)
    page.set_display_mapping("CD20", 1.0, 20.0, 0.5)
    page.set_display_mapping("DAPI", 5.0, 900.0, 0.9)
    popup = page.show_display_popup()
    assert popup.mapping("marker") == pytest.approx((10.0, 800.0, 1.5))
    assert popup._widgets["marker"]["label"].text() == "Marker (CD3)"

    page._on_channel_row_changed(page._channel_order.index("CD20"))

    assert popup.mapping("marker") == pytest.approx((1.0, 20.0, 0.5))
    assert popup.mapping("nucleus") == pytest.approx((5.0, 900.0, 0.9))
    assert popup._widgets["marker"]["label"].text() == "Marker (CD20)"
    assert popup._widgets["nucleus"]["label"].text() == "DAPI (DAPI)"
    # A sync must not write anything back.
    assert page._display_mapping_for("CD3") == (10.0, 800.0, 1.5)


# ── 5. "Use as segmentation remap" ───────────────────────────────────────

def test_use_as_remap_copies_the_mapping_with_neutral_brightness_contrast(app):
    page = _page(app)
    wb = page._cond_workbench
    wb._params["CD3"] = {"min": 0.0, "max": 1.0, "brightness": 0.4,
                         "contrast": 2.0, "gamma": 3.0, "auto": True,
                         "enabled": True}
    page.set_display_mapping("CD3", 12.0, 640.0, 1.25)

    page._display_popup = None
    popup = page.show_display_popup()
    popup.btn_use_as_remap.click()

    p = wb._params["CD3"]
    assert (p["min"], p["max"], p["gamma"]) == (12.0, 640.0, 1.25)
    assert p["brightness"] == 0.0 and p["contrast"] == 1.0
    assert p["auto"] is False
    assert wb._user_adjusted["CD3"] is True


def test_use_as_remap_emits_params_changed_for_that_channel(app):
    page = _page(app)
    seen = []
    page._cond_workbench.params_changed.connect(seen.append)
    page.set_display_mapping("CD3", 3.0, 30.0, 1.0)

    page._use_display_as_segmentation_remap()

    assert seen == ["CD3"]


def test_use_as_remap_never_writes_for_the_nucleus_channel(app):
    page = _page(app)
    wb = page._cond_workbench
    before = dict(wb._params.get("DAPI") or {})
    page.current_channel = "DAPI"

    page._use_display_as_segmentation_remap()

    assert dict(wb._params.get("DAPI") or {}) == before
    assert "DAPI" not in wb._user_adjusted or wb._user_adjusted["DAPI"] is not True


def test_use_as_remap_without_a_workbench_is_a_no_op(app, capsys):
    page = _page(app)
    page._cond_workbench = None
    page._use_display_as_segmentation_remap()          # must not raise
    assert "[step0]" in capsys.readouterr().out


# ── 6. nothing here computes; DAPI is never corrected ────────────────────

def test_on_demand_never_starts_for_the_nucleus_channel(app, monkeypatch):
    page = _page(app)
    started = []

    class _Boom:
        def __init__(self, *a, **k):
            started.append(a)
            raise AssertionError("a worker was built for the nucleus channel")

    monkeypatch.setattr(sp, "BatchProcessWorker", _Boom)
    page._process_completed = True

    page._start_ondemand("DAPI")

    assert started == []


def test_opening_and_editing_the_popup_starts_no_worker(app, monkeypatch):
    page = _page(app)
    monkeypatch.setattr(sp, "BatchProcessWorker",
                        lambda *a, **k: pytest.fail("the Display window computed"))
    page._process_completed = True
    popup = page.show_display_popup()
    popup._widgets["marker"]["max"].setValue(123.0)
    popup._widgets["nucleus"]["max"].setValue(456.0)
    popup._widgets["marker"]["auto"].click()
    popup._widgets["marker"]["show"].setChecked(False)
    popup.btn_use_as_remap.click()


def test_the_saved_config_never_lists_the_nucleus_channel(app):
    """Regression: DAPI is excluded from correction everywhere, including
    the config the page writes."""
    page = _page(app)
    for ch in page._channel_order:
        page._channel_decisions[ch] = "tophat"

    cfg = page._build_config()

    assert "DAPI" not in cfg["channel_decisions"]
    assert "DAPI" not in cfg["channel_params"]
    assert set(cfg["channel_decisions"]) == {"CD3", "CD20"}
