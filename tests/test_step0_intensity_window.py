"""The Background Correction tab's display controls ARE the Channel Remap
tab's "Intensity" inspector, in a floating window.

The previous attempt built a second, smaller set of spin boxes. This one
does not re-implement anything: the workbench hands its inspector panel over
(`detach_inspector`) and keeps driving it, so histogram, Min/Max sliders +
spin boxes, Gamma, Auto and Reset behave here exactly as they do in Channel
Remap -- on the same per-channel params, which are also the page's display
mapping. Around it:

- the Patch Preview header keeps only lock-zoom and reset-all;
- every channel row carries its own colour swatch;
- the nucleus row's checkbox is the DAPI layer's show/hide switch and never
  enters Process / Apply / on-demand / Save.

Own module: page-heavy Step0 suites crash pyqtgraph offscreen when combined
with the background-correction module in one process.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtCore, QtWidgets  # noqa: E402
from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtGui import QColor  # noqa: E402

from block01.ui.step0 import step0_page as sp  # noqa: E402

from test_step0_background_correction_tab import _GpuPathLoader, app  # noqa: E402,F401


# ── fakes ────────────────────────────────────────────────────────────────

class _Overlay:
    def __init__(self):
        self.mappings = []
        self.tints = []
        self.enabled = []

    def set_display_mapping(self, lo, hi, gamma=None):
        self.mappings.append((lo, hi, gamma))

    def set_tint(self, rgb):
        self.tints.append(rgb)

    def set_enabled(self, on, *_a, **_k):
        self.enabled.append(bool(on))


class _Ctl:
    def __init__(self):
        self.mappings = []
        self.tints = []
        self.channel, self.method, self.params = "CD3", None, ()

    def set_display_mapping(self, lo, hi, gamma=None, *, channel=None):
        self.mappings.append((lo, hi, gamma, channel))

    def set_marker_visible(self, v):
        pass

    def set_tint(self, rgb):
        self.tints.append(rgb)


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


class _Signal:
    def connect(self, *_a, **_k):
        pass


class _FakeBatchWorker(QtCore.QThread):
    """Records the `channels` dict it was constructed with; never runs."""

    created = []

    def __init__(self, loader, patches, channels, nucleus_channel,
                 tophat_radius, cucim_sigma, channel_params=None,
                 max_gpu_workers=4, parent=None):
        super().__init__()
        self.channels = dict(channels)
        self.nucleus_channel = nucleus_channel
        _FakeBatchWorker.created.append(self)

    def __getattr__(self, name):
        return _Signal()

    def isRunning(self):
        return False

    def start(self):
        pass

    def stop(self):
        pass


class _SeedLoader(_GpuPathLoader):
    """A loader that CAN be read at overview level, so the slide-wide display
    seed is a real window instead of the (0, 1) fallback."""

    def overview_downsample(self):
        return 32

    def read_region_lowres(self, ch, y0, y1, x0, x1, ds, normalize=False):
        rng = np.random.default_rng(abs(hash(ch)) % 2**31)
        return rng.uniform(300.0, 4000.0, size=(32, 32)).astype(np.float32)


def _bare_page(app, stack=None, loader=None):
    """A page whose Channel Remap tab was NEVER shown -- the real-app state
    the Intensity window has to cope with."""
    page = sp.Step0Page()
    page.loader = loader or _GpuPathLoader()
    page.patches = [(0, 32, 0, 32)]
    page.current_patch_idx = 0
    page.nucleus_channel = "DAPI"
    page._rebuild_channel_list()
    page.current_channel = "CD3"
    page._explore_tab = _Tab(stack)
    return page


def _page(app, stack=None, loader=None):
    page = _bare_page(app, stack, loader)
    page._sync_step0_to_workbench()
    return page


def _hist_hex(wb):
    """The colour the inspector's density curve is drawn/filled with."""
    return wb._histogram._curve.opts["pen"].color().name().lower()


def _payload(marker=500.0, nucleus=300.0, h=32, w=32):
    m = np.full((h, w), marker, np.float32)
    metrics = {"snr": 1.0, "bg_cv": 0.1}
    return {"original_raw": m, "tophat_raw": m * 0.5, "cucim_raw": m * 0.8,
            "original_metrics": metrics, "tophat_metrics": metrics,
            "cucim_metrics": metrics,
            "nucleus_raw": np.full((h, w), nucleus, np.float32)}


# ── 1. the window ────────────────────────────────────────────────────────

def test_the_window_is_lazy_and_built_once(app):
    page = _page(app)
    assert page._intensity_window is None, "built eagerly"

    win = page.show_intensity_window()

    assert win is not None and win.isVisible()
    assert page.show_intensity_window() is win, "a second open rebuilt it"
    assert page.intensity_panel() is not None


def test_it_is_a_separate_top_level_window_not_the_navigator(app):
    page = _page(app)
    win = page.show_intensity_window()

    assert win.isWindow(), "not a top-level window"
    assert win.parent() is page, "orphaned: it must be owned by the page"
    flags = win.windowFlags()
    for hint in (Qt.WindowMinimizeButtonHint, Qt.WindowMaximizeButtonHint,
                 Qt.WindowCloseButtonHint, Qt.WindowStaysOnTopHint):
        assert flags & hint, hint
    # It is NOT inside the Tissue Navigator (which stays navigation-only).
    nav = page._ensure_tissue_navigator()
    assert win.parentWidget() is not nav
    assert page.intensity_panel() not in nav.findChildren(QtWidgets.QWidget)


def test_the_widget_inside_is_the_workbenchs_own_inspector(app):
    """1:1, not a replica: the same object the Channel Remap tab used to
    show, and the Channel Remap tab no longer contains it."""
    page = _page(app)
    wb = page._cond_workbench
    before = wb._inspector
    assert before in wb.findChildren(QtWidgets.QWidget)

    win = page.show_intensity_window()

    panel = page.intensity_panel()
    assert panel is before, "a different widget was built"
    assert panel in win.findChildren(QtWidgets.QWidget)
    assert panel not in wb.findChildren(QtWidgets.QWidget), \
        "the Channel Remap tab still shows the inspector"
    assert wb.inspector_is_detached()
    # The workbench still drives it: its controls are that panel's children.
    for ctrl in (wb._histogram, wb._sp_min, wb._sp_max, wb._sp_gamma,
                 wb._btn_auto, wb._btn_reset):
        assert ctrl in panel.findChildren(QtWidgets.QWidget), ctrl


def test_the_button_restores_a_minimised_window(app):
    """Reported from manual testing: minimise the Intensity window and the
    button that opened it stops working.

    A minimised window is still `isVisible()` -- Qt counts it as shown, just
    shown as an icon -- so `show()` was a no-op and `raise_()` raised
    something nobody could see. The minimised bit has to be cleared.
    """
    page = _page(app)
    win = page.show_intensity_window()
    panel = page.intensity_panel()
    win.showMinimized()
    assert win.isMinimized()

    assert page.show_intensity_window() is win

    assert not win.isMinimized(), "the window is still minimised"
    assert win.isVisible()
    # The detach state is untouched: it is still the workbench's inspector.
    assert page.intensity_panel() is panel
    assert page._cond_workbench.inspector_is_detached()


def test_the_toggle_restores_a_minimised_window_rather_than_hiding_it(app):
    """Toggling a minimised window into hidden would take away the only
    thing left to click on."""
    page = _page(app)
    win = page.show_intensity_window()
    win.showMinimized()

    page.toggle_intensity_window()

    assert not win.isMinimized()
    assert win.isVisible()


def test_the_button_reopens_a_closed_window(app):
    page = _page(app)
    win = page.show_intensity_window()
    win.close()
    assert not win.isVisible()

    assert page.show_intensity_window() is win

    assert win.isVisible()


def test_toggle_hides_and_shows_the_same_window(app):
    page = _page(app)
    win = page.toggle_intensity_window()
    assert win.isVisible()
    assert page.toggle_intensity_window() is win and not win.isVisible()
    assert page.toggle_intensity_window() is win and win.isVisible()


def test_the_button_lives_next_to_the_channel_list(app):
    page = _page(app)
    btn = page._btn_intensity_window
    assert btn.text() == "Intensity…"
    # NOT in the Patch Preview header.
    row = page._preview_ctrl_row
    assert btn not in [row.itemAt(i).widget() for i in range(row.count())]


# ── 2. the controls edit the one mapping every view reads ────────────────

def test_moving_min_max_gamma_moves_every_view(app):
    stack = _Stack()
    page = _page(app, stack)
    wb = page._cond_workbench
    page.show_intensity_window()
    page._on_batch_patch_done("CD3", 0, _payload())
    wb.set_active_channel("CD3")

    wb._sp_min.setValue(120.0)
    wb._sp_max.setValue(880.0)
    wb._sp_gamma.setValue(1.75)

    assert page._display_mapping_for("CD3") == pytest.approx((120.0, 880.0, 1.75))
    # the compare panels
    assert list(page._preview_imgs[0].levels) == pytest.approx([120.0, 880.0])
    assert page._preview_imgs[0].lut is not None
    # the full image
    assert stack.controller.mappings[-1] == pytest.approx((120.0, 880.0, 1.75, "CD3"))


def test_the_dapi_channels_mapping_reaches_the_overlay(app):
    stack = _Stack()
    page = _page(app, stack)
    wb = page._cond_workbench
    wb.set_active_channel("DAPI")

    wb._sp_max.setValue(444.0)

    assert page._display_mapping_for("DAPI")[1] == 444.0
    assert stack.overlay.mappings[-1][1] == 444.0


def test_auto_stays_the_workbenchs_own_button(app):
    """The window is a 1:1 replica, so its Auto is the workbench's QuPath
    auto-contrast over the workbench's OWN pixels -- untouched by this page,
    whatever those pixels are."""
    page = _page(app)
    wb = page._cond_workbench
    rng = np.random.default_rng(5)
    wb._raw["CD3"] = rng.uniform(600.0, 900.0, size=(32, 32)).astype(np.float32)
    wb.set_active_channel("CD3")

    wb._btn_auto.click()

    lo, hi, _ = page._display_mapping_for("CD3")
    assert 600 <= lo < 700 and 850 < hi <= 900
    assert wb._params["CD3"]["auto"] is True


def test_the_slide_seed_lands_once_and_a_user_value_survives(app):
    page = _page(app, loader=_SeedLoader())
    wb = page._cond_workbench

    lo, hi, gamma = page._display_mapping_for("CD3")

    assert "CD3" in page._display_seeded
    assert 300 <= lo < 400 and 3900 < hi <= 4000, (lo, hi)
    assert gamma == 1.0
    assert wb._params["CD3"]["min"] == lo and wb._params["CD3"]["max"] == hi
    assert wb._params["CD3"]["brightness"] == 0.0
    assert wb._params["CD3"]["contrast"] == 1.0

    wb.set_active_channel("CD3")
    wb._sp_min.setValue(50.0)
    for _ in range(3):
        assert page._display_mapping_for("CD3")[0] == 50.0, "re-seeded over a user value"


# ── 3. the inspector follows the Background Correction selection ─────────

def test_selecting_a_row_switches_the_inspector(app):
    page = _page(app)
    wb = page._cond_workbench
    page.show_intensity_window()
    page.set_display_mapping("CD3", 10.0, 100.0, 1.0)
    page.set_display_mapping("CD20", 20.0, 200.0, 1.2)

    page._on_channel_selected_by_id("CD3")
    assert wb.active_channel() == "CD3"
    assert wb._sp_min.value() == pytest.approx(10.0)
    assert wb._sp_max.value() == pytest.approx(100.0)

    page._on_channel_selected_by_id("CD20")
    assert page.current_channel == "CD20"
    assert wb.active_channel() == "CD20"
    assert wb._sp_min.value() == pytest.approx(20.0)
    assert wb._sp_max.value() == pytest.approx(200.0)
    assert wb._sp_gamma.value() == pytest.approx(1.2)


def test_the_dapi_row_only_moves_the_inspector(app):
    """DAPI is a REFERENCE channel: selecting its row means "the Intensity
    window now edits DAPI's mapping", nothing else. The compare panels, the
    full image and the method combo keep the marker they were on."""
    stack = _Stack()
    page = _page(app, stack)
    wb = page._cond_workbench
    page.show_intensity_window()
    page._on_channel_selected_by_id("CD20")
    stack.controller.channel = "CD20"

    page._on_channel_selected_by_id("DAPI")

    assert page.current_channel == "CD20", "the displayed channel was dropped"
    assert stack.controller.channel == "CD20", "the full image switched to DAPI"
    assert wb.active_channel() == "DAPI"
    assert _hist_hex(wb) == page._channel_swatch_hex("DAPI").lower()
    assert page._channel_rows["CD20"]["method_cb"].isEnabled()


def test_the_dapi_row_starts_no_correction_and_rebuilds_no_viewer(app,
                                                                  monkeypatch):
    stack = _Stack()
    page = _page(app, stack)
    page._on_channel_selected_by_id("CD20")
    page._process_completed = True
    # On-demand computing is gone; the guard is now that NO worker is
    # constructed at all, whichever row is clicked.
    started = []
    import block01.ui.step0.step0_page as _sp
    monkeypatch.setattr(_sp, "BatchProcessWorker",
                        lambda *a, **k: started.append(a))
    shown = []
    monkeypatch.setattr(page, "_show_full_image",
                        lambda *a, **k: shown.append(a))

    page._on_channel_selected_by_id("DAPI")

    assert started == [], "selecting DAPI started a correction"
    assert shown == [], "selecting DAPI rebuilt the full image"


def test_moving_the_dapi_sliders_reaches_the_overlay(app):
    stack = _Stack()
    page = _page(app, stack)
    wb = page._cond_workbench
    page.show_intensity_window()
    page._on_channel_selected_by_id("CD20")
    page._on_channel_selected_by_id("DAPI")

    wb._sp_max.setValue(777.0)

    assert page._display_mapping_for("DAPI", nucleus=True)[1] == 777.0
    assert stack.overlay.mappings[-1][1] == 777.0
    assert page.current_channel == "CD20"


def test_a_marker_row_takes_the_inspector_back(app):
    page = _page(app)
    wb = page._cond_workbench
    page.show_intensity_window()
    page._on_channel_selected_by_id("CD20")
    page._on_channel_selected_by_id("DAPI")

    page._on_channel_selected_by_id("CD3")

    assert page.current_channel == "CD3"
    assert page._inspector_channel is None
    assert wb.active_channel() == "CD3"
    assert _hist_hex(wb) == page._channel_swatch_hex("CD3").lower()


# ── 3b. the landing view has NO patch, and the window still works ────────
#
# The real state a slide loads into: the full image is up, no patch has
# been drawn, and nothing has ever been computed. The workbench used to be
# fed from the CURRENT PATCH, so on this page there was no channel data at
# all -- the Intensity window opened with an empty histogram, disabled
# sliders and an active channel of None, and every number it showed came
# from the page-level fallback instead of the single source of truth.


class _OutlierSeedLoader(_GpuPathLoader):
    """A whole-slide low-resolution read with a few very bright pixels, so
    the seeded window (p0.1/99.9 over tissue) is visibly NOT the array's
    min/max."""

    shape = (2048, 2048)

    def overview_downsample(self):
        return 32

    def read_region_lowres(self, ch, y0, y1, x0, x1, ds, normalize=False):
        arr = np.full((64, 64), 700.0, np.float32)
        arr[0, :4] = 60000.0            # hot pixels, above the 99.9th pct
        arr[1, :4] = 0.0                # background zeros, excluded
        return arr


def _landing_page(app, stack=None, loader=None):
    """A freshly loaded slide on its landing view: dataset bound, full
    image showing, NO patch drawn, workbench never engaged."""
    page = _bare_page(app, stack, loader or _SeedLoader())
    page.patches = []
    page.current_patch_idx = 0
    return page


def test_with_no_patch_the_window_opens_on_the_current_channel(app):
    page = _landing_page(app)
    wb = page._cond_workbench
    assert not page.patches and not wb.has_channel_data()

    page.show_intensity_window()

    assert wb.has_channel_data(), "no channel data with no patch drawn"
    assert wb.active_channel() == "CD3"
    assert wb._raw["CD3"] is not None, "the histogram has no pixels"
    assert wb._sp_max.isEnabled() and wb._sp_min.isEnabled()


def test_with_no_patch_moving_max_reaches_the_full_image(app):
    stack = _Stack()
    page = _landing_page(app, stack)
    wb = page._cond_workbench
    page.show_intensity_window()

    wb._sp_max.setValue(6.0)

    assert page._display_mapping_for("CD3")[1] == pytest.approx(6.0)
    assert stack.controller.mappings[-1][:3] == pytest.approx((
        page._display_mapping_for("CD3")[0], 6.0,
        page._display_mapping_for("CD3")[2]))
    assert stack.controller.mappings[-1][3] == "CD3"


def test_with_no_patch_selecting_a_row_moves_the_inspector(app):
    page = _landing_page(app)
    wb = page._cond_workbench
    page.show_intensity_window()

    page._on_channel_selected_by_id("CD20")

    assert page.current_channel == "CD20"
    assert wb.active_channel() == "CD20"
    assert wb._raw["CD20"] is not None, "the switched-to channel is empty"


def test_with_no_patch_the_dapi_row_edits_the_overlay(app):
    stack = _Stack()
    page = _landing_page(app, stack)
    wb = page._cond_workbench
    page.show_intensity_window()

    page._on_channel_selected_by_id("DAPI")
    wb._sp_max.setValue(444.0)

    assert wb.active_channel() == "DAPI"
    assert page.current_channel == "CD3", "the displayed channel was dropped"
    assert page._display_mapping_for("DAPI")[1] == 444.0
    assert stack.overlay.mappings[-1][1] == 444.0


def test_the_workbench_pixels_are_the_slide_not_the_patch(app):
    """One array per channel, read once: the same whole-slide low-resolution
    read the display seed uses -- so the two cannot disagree, and there is
    no second read."""
    loader = _OutlierSeedLoader()
    page = _landing_page(app, loader=loader)
    wb = page._cond_workbench
    page.show_intensity_window()

    slide = page._slide_lowres_array("CD3")
    assert wb._raw["CD3"] is slide, "the workbench read its own pixels"
    lo, hi = sp.seed_display_range(slide)
    assert wb._params["CD3"]["min"] == pytest.approx(lo)
    assert wb._params["CD3"]["max"] == pytest.approx(hi)
    assert hi < 60000.0, "the sliders opened on the array max, not the seed"
    assert page._display_mapping_for("CD3")[:2] == pytest.approx((lo, hi))


def test_a_drawn_patch_does_not_change_the_workbench_pixels(app):
    """One rule on this page: the whole slide, patch or no patch."""
    page = _landing_page(app, loader=_OutlierSeedLoader())
    page.show_intensity_window()
    before = page._cond_workbench._params["CD3"]["max"]

    page.patches = [(0, 32, 0, 32)]
    page.current_patch_idx = 0
    page._sync_step0_to_workbench()

    wb = page._cond_workbench
    assert wb._raw["CD3"] is page._slide_lowres_array("CD3")
    assert wb._params["CD3"]["max"] == pytest.approx(before)



# ── 4. the compare header carries the snapshot's own controls ────────────

def test_the_compare_header_describes_the_snapshot_and_nothing_else(app):
    """The panels stopped being a viewer, so the view controls went with
    them: what is left says WHERE the snapshot is, whether it is
    downsampled, and offers to keep it as a patch."""
    page = _page(app)
    row = page._preview_ctrl_row
    widgets = [row.itemAt(i).widget() for i in range(row.count())]
    widgets = [w for w in widgets if w is not None]

    assert widgets == [page._compare_where_lbl, page._compare_level_lbl,
                       page._btn_snapshot_patch], (
        [w.__class__.__name__ for w in widgets])
    for gone in ("_btn_lock_zoom", "_reset_all_views", "_sync_zoom",
                 "_reset_single_view", "_full_image_buttons",
                 "_dec_process_btn", "_preview_stack",
                 "_nuc_color_btn", "_marker_color_btn", "_btn_display_popup",
                 "_display_popup", "show_display_popup", "toggle_display_popup",
                 "_ensure_display_popup", "_use_display_as_segmentation_remap"):
        assert not hasattr(page, gone), gone


def test_the_hidden_layer_holders_stay(app):
    page = _page(app)
    for btn in (page._btn_show_marker, page._btn_show_nucleus):
        assert btn.isCheckable()
        assert not btn.isVisible()
        assert btn.parentWidget() is page
    # The marker layer is the page's subject and starts on; the DAPI layer
    # is an aid the user opts into (v15 default-off).
    assert page._btn_show_marker.isChecked() is True
    assert page._btn_show_nucleus.isChecked() is False


# ── 5. the DAPI checkbox is a layer switch, never a processing one ───────

def test_the_nucleus_row_is_checkable_and_its_combo_is_not(app):
    page = _page(app)
    row = page._channel_rows["DAPI"]
    assert row["checkbox"].isEnabled(), "the DAPI show/hide switch is disabled"
    assert not row["method_cb"].isEnabled(), "DAPI must not get a method"
    assert row["checkbox"].isChecked() is False, "DAPI starts hidden (v15)"


def test_the_dapi_layer_starts_off_in_both_views(app):
    """(v15) DAPI is opt-in: on page creation and on every dataset (re)load
    the nucleus row is UNCHECKED and no nucleus pixels are drawn -- not in
    the compare panels, not in the full image -- until the user checks it."""
    stack = _Stack()
    page = _page(app, stack=stack)
    cb = page._channel_rows["DAPI"]["checkbox"]

    assert cb.isChecked() is False
    assert page._btn_show_nucleus.isChecked() is False
    assert page._btn_full_nucleus.isChecked() is False
    assert page._btn_full_nucleus.text().startswith("○")
    # Marker channels are untouched by the change.
    assert page._btn_show_marker.isChecked() is True

    # compare panels: the nucleus item is not drawn
    page._last_payload = _payload()
    page._refresh_preview_display()
    assert all(item is None or not item.isVisible()
               for item in page._preview_nuc_imgs)

    # full image: the overlay is asked to stay off
    page._show_full_image()
    assert stack.overlay.enabled and stack.overlay.enabled[-1] is False

    # ...and checking the row turns the layer on in BOTH views.
    cb.setChecked(True)
    assert page._btn_show_nucleus.isChecked() is True
    assert page._btn_full_nucleus.isChecked() is True
    page._refresh_preview_display()
    assert any(item is not None and item.isVisible()
               for item in page._preview_nuc_imgs)
    page._show_full_image()
    assert stack.overlay.enabled[-1] is True


def test_a_dataset_reload_puts_the_dapi_layer_back_to_off(app):
    page = _page(app)
    page._channel_rows["DAPI"]["checkbox"].setChecked(True)
    assert page._btn_show_nucleus.isChecked() is True

    page._reset_dataset_view_state()

    assert page._btn_show_nucleus.isChecked() is False
    assert page._btn_full_nucleus.isChecked() is False


def test_the_dapi_checkbox_drives_both_views(app):
    page = _page(app)
    cb = page._channel_rows["DAPI"]["checkbox"]

    cb.setChecked(False)
    assert page._btn_show_nucleus.isChecked() is False
    assert page._btn_full_nucleus.isChecked() is False

    cb.setChecked(True)
    assert page._btn_show_nucleus.isChecked() is True
    assert page._btn_full_nucleus.isChecked() is True

    # ...and the full image's own toggle moves the row checkbox back.
    page._btn_full_nucleus.setChecked(False)
    assert cb.isChecked() is False
    assert page._btn_show_nucleus.isChecked() is False


def test_the_dapi_checkbox_never_enters_processing(app, monkeypatch):
    _FakeBatchWorker.created = []
    monkeypatch.setattr(sp, "BatchProcessWorker", _FakeBatchWorker)
    page = _page(app)
    page._channel_rows["DAPI"]["checkbox"].setChecked(True)
    page._channel_rows["CD3"]["checkbox"].setChecked(True)

    assert "DAPI" not in page._channel_methods
    assert page._channel_decisions.get("DAPI") in (None, "original")

    page._on_process_clicked()

    assert _FakeBatchWorker.created, "Process did not start"
    for w in _FakeBatchWorker.created:
        assert "DAPI" not in w.channels, w.channels

    cfg = page._build_config()
    assert "CD3" in cfg["channel_decisions"], "the fake config went empty"
    assert "DAPI" not in cfg["channel_decisions"]
    assert "DAPI" not in cfg["channel_params"]


# ── 6. a colour swatch per channel ───────────────────────────────────────

def test_the_row_order_is_checkbox_swatch_name_combo(app):
    page = _page(app)
    for ch in ("CD3", "DAPI"):
        row = page._channel_rows[ch]["row_widget"]
        lay = row.layout()
        widgets = [lay.itemAt(i).widget() for i in range(lay.count())]
        widgets = [w for w in widgets if w is not None]
        # The compute-state glyph sits between the checkbox and the swatch:
        # it is a claim about the channel the checkbox selects.
        assert widgets[:5] == [row.checkbox, row.state_lbl, row.swatch,
                               row.name_label, row.method_cb], (ch, widgets)
        assert row.swatch.isVisibleTo(row), f"{ch}: the swatch is hidden"


def test_the_swatch_starts_at_the_pages_colour(app):
    page = _page(app)
    page._channel_colors["CD3"] = (1.0, 0.0, 0.0)
    page._nuc_color = (0.0, 0.0, 1.0)
    page._rebuild_channel_list()

    assert page._dock_adapter.model.get("CD3").color.lower() == "#ff0000"
    assert page._dock_adapter.model.get("DAPI").color.lower() == "#0000ff"


def test_the_default_swatches_are_the_channel_remap_palette(app):
    """One colour store: the default a channel wears in the Background
    Correction list is the Channel Remap palette entry for its index, so the
    same channel is the same colour in both lists (and in the full image)."""
    page = _page(app)
    wb = page._cond_workbench
    from block01.ui.widgets import channel_workbench as cw

    order = [c for c in page._channel_order if c != "DAPI"]
    assert len(set(page._channel_swatch_hex(c) for c in order)) == len(order), \
        "the marker swatches are not distinct"
    for ch in page._channel_order:
        assert page._channel_swatch_hex(ch).lower() == wb._colors[ch].lower(), ch
        assert page._dock_adapter.model.get(ch).color.lower() == \
            wb._colors[ch].lower(), ch
    # ...and that is the palette rule, by channel index.
    palette_order = page._palette_channel_order()
    for i, ch in enumerate(palette_order):
        if ch == "DAPI":
            continue                       # DAPI keeps its own blue
        assert page._channel_swatch_hex(ch).lower() == \
            cw._PALETTE[i % len(cw._PALETTE)].lower(), ch
    # The full image of channel i paints in palette colour i.
    assert page._full_image_tint("CD3") == pytest.approx(
        cw._hex_to_rgb01(page._channel_swatch_hex("CD3")))


def test_a_swatch_pick_moves_the_channel_remap_list_too(app, monkeypatch):
    page = _page(app)
    wb = page._cond_workbench
    monkeypatch.setattr(QtWidgets.QColorDialog, "getColor",
                        staticmethod(lambda *a, **k: QColor("#123456")))

    page._on_channel_swatch_clicked("CD20")

    assert wb._colors["CD20"].lower() == "#123456"
    assert page._channel_swatch_hex("CD20").lower() == "#123456"


def test_a_channel_remap_pick_moves_the_background_correction_swatch(app,
                                                                     monkeypatch):
    page = _page(app)
    wb = page._cond_workbench
    monkeypatch.setattr(QtWidgets.QColorDialog, "getColor",
                        staticmethod(lambda *a, **k: QColor("#abcdef")))

    wb._on_color_clicked("CD3")

    assert page._channel_colors["CD3"] == pytest.approx(
        (0xab / 255, 0xcd / 255, 0xef / 255), abs=1 / 255)
    assert page._channel_swatch_hex("CD3").lower() == "#abcdef"
    assert page._dock_adapter.model.get("CD3").color.lower() == "#abcdef"


def test_clicking_a_marker_swatch_recolours_every_view(app, monkeypatch):
    stack = _Stack()
    page = _page(app, stack)
    page._on_batch_patch_done("CD3", 0, _payload())
    monkeypatch.setattr(QtWidgets.QColorDialog, "getColor",
                        staticmethod(lambda *a, **k: QColor("#ff8800")))

    page._on_channel_swatch_clicked("CD3")

    assert page._channel_colors["CD3"] == pytest.approx(
        (1.0, 0x88 / 255.0, 0.0), abs=1 / 255)
    assert page._dock_adapter.model.get("CD3").color.lower() == "#ff8800"
    assert page._full_image_tint("CD3") == page._channel_colors["CD3"]
    assert stack.controller.tints[-1] == page._channel_colors["CD3"]


def test_clicking_the_nucleus_swatch_recolours_the_dapi_overlay(app, monkeypatch):
    stack = _Stack()
    page = _page(app, stack)
    monkeypatch.setattr(QtWidgets.QColorDialog, "getColor",
                        staticmethod(lambda *a, **k: QColor("#00ccff")))

    page._on_channel_swatch_clicked("DAPI")

    assert page._nuc_color == pytest.approx(
        (0.0, 0xcc / 255.0, 1.0), abs=1 / 255)
    assert page._dock_adapter.model.get("DAPI").color.lower() == "#00ccff"
    assert stack.overlay.tints[-1] == page._nuc_color


def test_a_cancelled_dialog_changes_nothing(app, monkeypatch):
    page = _page(app)
    before = dict(page._channel_colors)
    monkeypatch.setattr(QtWidgets.QColorDialog, "getColor",
                        staticmethod(lambda *a, **k: QColor()))

    page._on_channel_swatch_clicked("CD3")

    assert page._channel_colors == before


def test_the_histogram_is_filled_with_the_active_channels_colour(app):
    """The Intensity inspector's density curve is the channel's colour, on
    activation and live while it is active."""
    page = _page(app)
    wb = page._cond_workbench
    page.show_intensity_window()
    model = page._dock_adapter.model

    wb.set_active_channel("CD3")
    assert _hist_hex(wb) == page._channel_swatch_hex("CD3").lower()

    # live: the swatch colour changes while CD3 is the active channel
    model.set_color("CD3", "#00ffcc")
    assert _hist_hex(wb) == "#00ffcc"
    assert page._channel_swatch_hex("CD3").lower() == "#00ffcc"

    # a channel switch brings the OTHER channel's colour with it
    wb.set_active_channel("CD20")
    assert _hist_hex(wb) == page._channel_swatch_hex("CD20").lower()
    assert _hist_hex(wb) != "#00ffcc"


def test_the_histogram_follows_the_dapi_colour_too(app):
    page = _page(app)
    wb = page._cond_workbench
    page.show_intensity_window()
    wb.set_active_channel("DAPI")

    page._dock_adapter.model.set_color("DAPI", "#8844ff")

    assert _hist_hex(wb) == "#8844ff"
    assert page._nuc_color == pytest.approx(
        (0x88 / 255, 0x44 / 255, 1.0), abs=1 / 255)


def test_a_colour_change_on_an_inactive_channel_leaves_the_curve_alone(app):
    page = _page(app)
    wb = page._cond_workbench
    page.show_intensity_window()
    wb.set_active_channel("CD3")
    before = _hist_hex(wb)

    page._dock_adapter.model.set_color("CD20", "#ff0000")

    assert _hist_hex(wb) == before


# ── 7. the workbench is engaged by the window, not by its tab ────────────

def test_opening_the_window_engages_a_never_shown_remap_tab(app):
    """The gap: the workbench used to be fed only when the Channel Remap tab
    was entered, so on a real slide the Intensity window opened empty --
    no active channel, no params, no histogram pixels."""
    page = _bare_page(app)
    wb = page._cond_workbench
    assert not wb.has_channel_data(), "the fixture pre-engaged the workbench"
    assert wb.active_channel() is None
    assert "CD3" not in wb._params

    page.show_intensity_window()

    assert wb.has_channel_data()
    assert wb.active_channel() == page.current_channel == "CD3"
    assert "CD3" in wb._params
    assert wb._raw.get("CD3") is not None, "no pixels: the histogram stays empty"


def test_the_engaged_window_is_the_source_of_truth_not_the_fallback(app):
    page = _bare_page(app, loader=_SeedLoader())
    wb = page._cond_workbench
    # Asked BEFORE the window engaged the workbench: the page-level fallback
    # answers, exactly as it does in the real app when the compare panels
    # paint first. That copy must not survive as a second source.
    page._display_mapping_for("CD3")
    assert "CD3" in page._display_fallback

    page.show_intensity_window()
    lo, hi, gamma = page._display_mapping_for("CD3")

    assert "CD3" in wb._params
    p = wb._params["CD3"]
    assert (p["min"], p["max"], p["gamma"]) == (lo, hi, gamma)
    assert (p["brightness"], p["contrast"]) == (0.0, 1.0)
    assert 300 <= lo < 400 and 3900 < hi <= 4000, (lo, hi)   # the slide seed
    assert "CD3" not in page._display_fallback, "read from the page fallback"


def test_selection_still_switches_after_the_window_engaged_it(app):
    page = _bare_page(app)
    wb = page._cond_workbench
    page.show_intensity_window()

    page._on_channel_selected_by_id("CD20")

    assert page.current_channel == "CD20"
    assert wb.active_channel() == "CD20"
    assert "CD20" in wb._params
    assert wb._raw.get("CD20") is not None


def test_engaging_starts_no_correction(app, monkeypatch):
    _FakeBatchWorker.created = []
    monkeypatch.setattr(sp, "BatchProcessWorker", _FakeBatchWorker)
    page = _bare_page(app)

    page.show_intensity_window()
    page._on_channel_selected_by_id("CD20")

    assert _FakeBatchWorker.created == []


# ── 7. the detached inspector must not drive the workbench's own preview ──
#
# While the inspector lives in Step0's floating window the Channel Remap tab
# is not on screen, but every Min/Max move and every channel switch still
# recomposited the workbench's OWN patch overlay: measured on a real slide,
# 360 ms of a 404 ms slider step and 943 ms of a 1.48 s channel switch, for
# pixels nobody can see. Step0's own views are raw arrays + a lookup table,
# so nothing here depends on that composite.

def _count_composites(monkeypatch):
    """Count `compose_multichannel_overlay` calls from the workbench."""
    import block01.ui.widgets.channel_workbench as cwb
    calls = []
    real = cwb.compose_multichannel_overlay

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(cwb, "compose_multichannel_overlay", counting)
    return calls


def test_a_param_change_while_detached_recomposites_nothing(app, monkeypatch):
    page = _page(app)
    page.show_intensity_window()
    wb = page._cond_workbench
    assert wb.inspector_is_detached() and not wb.isVisible()

    calls = _count_composites(monkeypatch)
    wb._sp_max.setValue(float(wb._sp_max.value()) + 50.0)

    assert calls == [], f"{len(calls)} composite(s) for an off-screen canvas"
    assert wb._preview_dirty, "the deferred refresh was not recorded"


def test_the_inspector_still_drives_the_page_while_detached(app, monkeypatch):
    """Skipping the workbench's own composite must not skip anything Step0
    shows: the compare panels and the full image still follow live."""
    stack = _Stack()
    page = _page(app, stack=stack)
    page._last_payload = _payload()
    page.show_intensity_window()
    wb = page._cond_workbench
    page._show_full_image()
    before = len(stack.controller.mappings)

    _count_composites(monkeypatch)
    wb._sp_max.setValue(4321.0)

    assert page._display_mapping_for("CD3")[1] == 4321.0
    assert len(stack.controller.mappings) > before
    assert stack.controller.mappings[-1][1] == 4321.0


def test_an_active_change_while_detached_recomposites_once_after_reattach(app, monkeypatch):
    page = _page(app)
    page.show_intensity_window()
    wb = page._cond_workbench

    calls = _count_composites(monkeypatch)
    page._on_channel_selected_by_id("CD20")
    assert calls == [], "the hidden canvas was recomposited on a channel switch"
    assert wb.active_channel() == "CD20"

    wb.reattach_inspector()

    assert len(calls) == 1, f"{len(calls)} composites on reattach, want 1"
    assert not wb._preview_dirty
    assert not wb.inspector_is_detached()
    # And it is paid back only once: a second reattach has nothing to replay.
    wb.reattach_inspector()
    assert len(calls) == 1


def test_showing_the_channel_remap_tab_pays_the_deferred_preview_back(app, monkeypatch):
    page = _page(app)
    page.show_intensity_window()
    wb = page._cond_workbench
    calls = _count_composites(monkeypatch)
    wb._sp_max.setValue(float(wb._sp_max.value()) + 50.0)
    assert calls == []

    # Entering the Channel Remap tab: Qt delivers a show event to the
    # workbench, whose handler pays the deferred composite back.
    page.show()
    page._step0_tabs.setCurrentIndex(page._cond_tab_index)
    try:
        assert wb.isVisible()
        assert len(calls) == 1, f"{len(calls)} composites on show, want 1"
        assert not wb._preview_dirty
        # ...and only once: leaving and re-entering with nothing changed in
        # between must not recomposite again.
        page._step0_tabs.setCurrentIndex(0)
        page._step0_tabs.setCurrentIndex(page._cond_tab_index)
        assert len(calls) == 1, "a second visit recomposited again"
    finally:
        page.hide()


def test_a_channel_switch_rebuilds_the_histogram_exactly_once(app):
    """`set_active_channel` used to run the whole activation twice: the list's
    own `active_changed` signal AND an explicit second call."""
    page = _page(app)
    page.show_intensity_window()
    wb = page._cond_workbench
    hist, loads = [], []
    real_hist = wb._histogram.set_data
    real_load = wb._load_params_into_controls
    wb._histogram.set_data = lambda *a, **k: (hist.append(1), real_hist(*a, **k))[1]
    wb._load_params_into_controls = lambda *a, **k: (loads.append(1), real_load(*a, **k))[1]

    page._on_channel_selected_by_id("CD20")

    assert len(hist) == 1, f"{len(hist)} histogram rebuilds for one switch"
    # The slide-wide seed lands after the activation and refreshes the numbers
    # once more -- but without a second whole-patch histogram rebuild.
    assert len(loads) <= 2, f"{len(loads)} control reloads for one switch"


def test_the_slide_seed_is_computed_once_per_channel(app):
    """`_display_mapping_for` is called several times per switch (marker +
    nucleus, compare panels + full image); only the FIRST may read the
    slide."""
    page = _page(app, loader=_SeedLoader())
    reads = []
    real = page.loader.read_region_lowres
    page.loader.read_region_lowres = lambda *a, **k: (reads.append(a[0]), real(*a, **k))[1]
    page.show_intensity_window()

    page._on_channel_selected_by_id("CD20")
    for _ in range(5):
        page._display_mapping_for("CD20")

    assert reads.count("CD20") == 1, f"the slide-wide seed was recomputed: {reads}"


def test_a_big_patch_histogram_is_subsampled_not_scanned_whole(app):
    """The density curve is display-only; scanning 6.4 M float32 pixels for
    it cost 79 ms on the GUI thread of every channel switch."""
    from block01.ui.widgets.channel_histogram_panel import ChannelHistogramPanel
    panel = ChannelHistogramPanel()
    cap = ChannelHistogramPanel._MAX_HISTOGRAM_SAMPLES
    rng = np.random.default_rng(0)
    big = rng.uniform(0.0, 1000.0, size=(2000, 2000)).astype(np.float32)
    assert big.size > cap

    panel.set_data(big, 100.0, 900.0)

    lo, hi = panel._data_bounds
    assert 0.0 <= lo <= 5.0 and 995.0 <= hi <= 1000.0, (lo, hi)
    assert panel.window() == (100.0, 900.0)
    # A small image is still used whole.
    small = np.linspace(0.0, 10.0, 100, dtype=np.float32).reshape(10, 10)
    panel.set_data(small)
    assert panel._data_bounds == (0.0, 10.0)


# ── 9. a re-sync may never move a display window ─────────────────────────
#
# The severe bug: the workbench's per-channel params ARE the page's display
# mapping, but `_sync_step0_to_workbench` rebuilt them from scratch on every
# patches-changed event. A channel whose pixels are not loaded yet is fed to
# the workbench as a LAZY placeholder, and its provisional params are
# (0.0, 1.0) -- read back as a window, every pixel of an 8/16-bit channel
# saturates. On the real slide, drawing six patches in compare mode turned
# all three compare panels and the Tissue Preview thumbnail solid colour.

def test_repeated_syncs_never_move_a_never_activated_channels_window(app):
    """No Intensity window, no channel switch: re-syncing the workbench must
    leave every channel's mapping exactly where it was."""
    page = _bare_page(app, loader=_SeedLoader())
    before = page._display_mapping_for("CD20")
    assert before[1] > 1.0, "the fixture never produced a real window"

    for i in range(6):                      # six patches drawn == six syncs
        page._sync_step0_to_workbench()
        assert page._display_mapping_for("CD20") == before, f"moved on sync {i}"


def test_a_provisional_placeholder_is_seeded_not_read_as_a_window(app):
    """The params key EXISTING is not enough: a lazy channel carries the
    workbench's provisional 0..1 range until its pixels arrive. Asked for a
    mapping, the page seeds it from the slide and writes that into the single
    source of truth."""
    from block01.core.display_mapping import seed_display_range
    page = _page(app, loader=_SeedLoader())
    wb = page._cond_workbench
    assert wb._raw.get("CD20") is None, "CD20 was not lazy"
    assert (wb._params["CD20"]["min"], wb._params["CD20"]["max"]) == (0.0, 1.0)
    assert not wb.channel_params_seeded("CD20")

    lo, hi, gamma = page._display_mapping_for("CD20")

    seed_lo, seed_hi = seed_display_range(page._slide_lowres_array("CD20"))
    assert (lo, hi, gamma) == (seed_lo, seed_hi, 1.0)
    assert 300 <= lo < 400 and 3900 < hi <= 4000, (lo, hi)
    assert wb.channel_params_seeded("CD20"), "the source of truth stayed provisional"
    assert (wb._params["CD20"]["min"], wb._params["CD20"]["max"]) == (lo, hi)


def test_drawing_patches_does_not_re_feed_the_workbench(app):
    """Patches are not the workbench's pixel source any more (it reads the
    whole slide), so a patch drawn or deleted must not rebuild it."""
    page = _page(app, loader=_SeedLoader())
    page._display_mapping_for("CD20")
    calls = []
    real = page._sync_step0_to_workbench
    page._sync_step0_to_workbench = lambda: calls.append(1) or real()

    for n in range(1, 7):                   # the user's six patches
        page._on_patches_changed([(0, 32, 0, 32)] * n)

    assert calls == [], "a patch change re-fed the workbench"


def test_a_re_sync_preserves_a_user_edited_window(app):
    page = _page(app, loader=_SeedLoader())
    wb = page._cond_workbench
    page.set_display_mapping("CD3", 120.0, 2500.0, 1.0)

    page._sync_step0_to_workbench()

    assert (wb._params["CD3"]["min"], wb._params["CD3"]["max"]) == (120.0, 2500.0)
    assert page._display_mapping_for("CD3") == (120.0, 2500.0, 1.0)


def test_a_dataset_switch_still_reseeds_the_workbench(app):
    """Preserving params across a re-sync must not survive a NEW slide: two
    slides of the same panel share every channel name."""
    page = _page(app, loader=_SeedLoader())
    wb = page._cond_workbench
    page.set_display_mapping("CD3", 120.0, 2500.0, 1.0)
    assert wb.has_channel_data()

    page._reset_dataset_view_state()

    assert not wb.has_channel_data()
    assert wb._params == {} and wb._user_adjusted == {}
