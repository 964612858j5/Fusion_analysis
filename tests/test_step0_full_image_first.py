"""Phase A1: the Background Correction page is FULL-IMAGE-first.

What changed, and what these tests pin down:

* a loaded dataset LANDS on the whole-slide full image -- raw, the DAPI
  (nucleus) channel, no patch drawn, no Process run. The three compare
  panels are a strip beside it that starts collapsed (A2 turned the
  collapsible page into a splitter pane and made a right-click fill it);
* the full image's own header carries an exclusive Original / TopHat /
  cuCIM switch. It is a PREVIEW: on-the-fly viewport correction with the
  row's current parameters, available for every marker channel whatever its
  checkbox or Method combo says, never for the DAPI reference, and it writes
  nothing and marks nothing computed;
* the ONLY things that start a correction run are the checkbox and the
  Process button. The old "click an unticked row after Process and it
  computes on demand" behaviour is gone;
* every marker row shows what its result is worth -- not computed /
  computed / stale -- derived from the same signature bookkeeping the
  incremental Process consults;
* Save says, once, which channels it is about to write raw.

Own module, like the other page-heavy Step0 suites: combined runs segfault
in offscreen pyqtgraph (not a regression, see the note in
test_step0_full_image.py).
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtCore, QtTest  # noqa: E402

from block01.ui.step0 import step0_page as sp  # noqa: E402
from block01.ui.step0.step0_page import (  # noqa: E402
    FULL_IMAGE_COARSE_LEVEL,
)

from test_step0_background_correction_tab import (  # noqa: E402
    _GpuPathLoader,
    app,            # noqa: F401  (pytest fixture)
)


# ── stand-ins ────────────────────────────────────────────────────────────

class _RecordingExploreTab:
    """Records what the page asks the viewer to show. No slide involved."""

    def __init__(self, stack=None):
        self.calls = []          # [(channel, method, params)]
        self.viewports = []
        self.stack = stack
        self.released = []

    def show_source(self, channel, method, params=(), *, viewport_l0=None,
                    tint=None, nucleus=None):
        self.calls.append((channel, method, tuple(params)))
        self.viewports.append(viewport_l0)
        return True

    def set_dataset(self, _path):
        pass

    def release_for_production(self, reason):
        self.released.append(reason)

    def teardown(self, **_kw):
        pass


class _FakeController:
    def __init__(self, level=0):
        self.level = level
        self.channel = None
        self.method = None
        self.params = ()

    def set_marker_visible(self, _v):
        pass

    def set_display_mapping(self, *args, **kwargs):
        self.mapping = tuple(args) + tuple(kwargs.values())


class _FakeProvider:
    """Level shapes of a 2x pyramid over a 4096-row slide."""

    def level_shape(self, level):
        return (4096 >> int(level), 4096 >> int(level))


class _FakeStack:
    def __init__(self, level=0):
        self.controller = _FakeController(level)
        self.provider = _FakeProvider()
        self.overlay = None


def _page(app, *, patches=True, channel="CD3"):
    """A loaded page. `channel=None` leaves the channel the load itself
    chose -- the LANDING channel, which is DAPI; every other test here is
    about a marker the user has since clicked, so they say which one."""
    page = sp.Step0Page()
    page.loader = _GpuPathLoader()
    page.ome_path = "/fake/slide.ome.tif"
    page.patches = [(0, 32, 0, 32)] if patches else []
    page.current_patch_idx = 0
    page.nucleus_channel = "DAPI"
    page._rebuild_channel_list()
    page._preload_cache = {0: {ch: np.zeros((32, 32), np.float32)
                               for ch in ("DAPI", "CD3", "CD20")}}
    if channel is not None:
        page.current_channel = channel
    page._update_full_method_buttons()
    return page


def _no_workers(monkeypatch):
    """Fail loudly if ANY correction worker is constructed."""
    started = []

    def _boom(*a, **k):
        started.append(a)
        raise AssertionError("a correction worker was constructed")

    monkeypatch.setattr(sp, "BatchProcessWorker", _boom)
    monkeypatch.setattr(sp, "WsiCorrectionWorker", _boom)
    return started


def _finish_run(page, channels, method="both"):
    """Deliver a run's results for `channels`, as the worker signals do."""
    disp = np.zeros((32, 32), np.float32)
    m = {"snr": 1.0, "bg_cv": 0.1}
    for ch in channels:
        page._pending_signatures[ch] = page._channel_signature(ch, method)
        page._set_channel_computing(ch)
        page._on_batch_patch_done(ch, 0, {
            "original_disp": disp, "tophat_disp": disp, "cucim_disp": disp,
            "original_metrics": m, "tophat_metrics": m, "cucim_metrics": m,
            "nucleus_disp": None})
        page._on_batch_channel_done(ch)
    page._on_batch_all_done()


# ── 1. the full image is the landing view ────────────────────────────────

def test_the_landing_view_is_the_full_image_with_no_patch_and_no_process(
        app, monkeypatch):
    """The whole point of A1: a loaded slide opens on the whole slide."""
    started = _no_workers(monkeypatch)
    page = _page(app, patches=False)
    tab = _RecordingExploreTab(stack=_FakeStack())
    page._explore_tab = tab

    page._enter_full_image_landing()

    assert page._full_image_visible()
    assert page.patches == []                      # no patch was drawn
    assert page._computed_channels == set()        # no Process ran
    assert started == []
    # Raw, the channel the user last had, no viewport hand-off from a
    # compare panel.
    assert tab.calls == [("CD3", None, ())]
    assert tab.viewports == [None]


def test_the_landing_view_does_not_need_the_reopen_placeholder(app,
                                                               monkeypatch):
    """Landing opens the viewer itself; "Reopen full image" is a recovery
    path, not the way in."""
    _no_workers(monkeypatch)
    page = _page(app)
    tab = _RecordingExploreTab(stack=_FakeStack())
    page._explore_tab = tab

    page._enter_full_image_landing()

    assert tab.calls, "landing did not open the full image"
    assert page._full_image_visible()


def test_the_landing_view_is_the_full_image_and_only_the_full_image(
        app, monkeypatch):
    """ONE viewing area, two exclusive modes: a loaded slide lands on the
    full image with the compare panels not on screen at all, and there is
    no button to put them beside it."""
    _no_workers(monkeypatch)
    page = _page(app)
    page._explore_tab = _RecordingExploreTab(stack=_FakeStack())
    page._enter_full_image_landing()

    assert page._compare_mode() is False
    assert page._view_area.currentIndex() == page._VIEW_FULL
    assert page._view_area.currentWidget() is not page._compare_strip
    for gone in ("_btn_show_compare", "_preview_split",
                 "_compare_strip_visible", "_set_compare_strip_visible",
                 "_on_compare_strip_toggled"):
        assert not hasattr(page, gone), gone


def test_the_two_modes_are_exclusive(app, monkeypatch):
    _no_workers(monkeypatch)
    page = _page(app)
    page._explore_tab = _RecordingExploreTab(stack=_FakeStack())
    page._enter_full_image_landing()
    page.resize(900, 700)
    page.show()
    QtTest.QTest.qWait(30)
    full_page = page._view_area.widget(page._VIEW_FULL)

    page._set_compare_mode(True)
    QtTest.QTest.qWait(30)

    assert page._compare_strip.isVisible() and not full_page.isVisible()
    # The panels have the WHOLE area now, which is the point of the change.
    assert page._compare_strip.height() == page._view_area.height()

    page._set_compare_mode(False)
    QtTest.QTest.qWait(30)

    assert full_page.isVisible() and not page._compare_strip.isVisible()


def test_the_method_switch_keeps_the_image_on_screen(app, monkeypatch):
    """The switch used to have to bring the full-image PAGE forward. There
    is no page to bring forward now."""
    _no_workers(monkeypatch)
    page = _page(app)
    tab = _RecordingExploreTab(stack=_FakeStack())
    page._explore_tab = tab

    page._on_full_method_clicked("tophat")

    assert page._full_image_visible()
    assert page._full_image_source == "tophat"
    assert tab.calls[-1][0] == "CD3" and tab.calls[-1][1] == "tophat"


# ── 1b. the toolbar keeps one place ──────────────────────────────────────

def _toolbar_y(page):
    """The pinned bar's top, in page coordinates."""
    return page._view_toolbar.mapTo(page, QtCore.QPoint(0, 0)).y()


def test_the_toolbar_sits_in_the_same_place_in_every_state(app, monkeypatch):
    """It used to live inside the full-image page, alone in that page's
    layout until the viewer widget was created -- so with no dataset Qt
    centred it and the bar sat halfway down the workspace, then jumped to
    the top when a slide loaded."""
    _no_workers(monkeypatch)
    page = sp.Step0Page()
    page.resize(900, 700)
    page.show()
    QtTest.QTest.qWait(30)
    before_load = _toolbar_y(page)

    page.loader = _GpuPathLoader()
    page.ome_path = "/fake/slide.ome.tif"
    page.patches = []
    page.nucleus_channel = "DAPI"
    page._rebuild_channel_list()
    page.current_channel = "CD3"
    page._explore_tab = _RecordingExploreTab(stack=_FakeStack())
    page._enter_full_image_landing()
    QtTest.QTest.qWait(30)
    after_load = _toolbar_y(page)

    page._set_compare_mode(True)
    QtTest.QTest.qWait(30)
    in_compare = _toolbar_y(page)
    assert page._view_toolbar.isVisible(), "the bar went away in compare mode"

    page._set_compare_mode(False)
    QtTest.QTest.qWait(30)
    back = _toolbar_y(page)

    assert before_load == after_load == in_compare == back, (
        before_load, after_load, in_compare, back)


def test_the_method_switch_is_disabled_while_the_panels_are_up(app,
                                                               monkeypatch):
    """Disabled, not hidden: the bar's geometry is identical in both modes,
    and the buttons still say which source the image is on."""
    _no_workers(monkeypatch)
    page = _page(app)
    page._explore_tab = _RecordingExploreTab(stack=_FakeStack())
    page._enter_full_image_landing()
    assert page._full_method_buttons["tophat"].isEnabled()

    page._set_compare_mode(True)

    assert not any(b.isEnabled() for b in page._full_method_buttons.values())
    assert page._full_method_buttons["original"].isChecked()

    page._set_compare_mode(False)

    assert page._full_method_buttons["tophat"].isEnabled()


# ── 2. the method buttons on top of the full image ───────────────────────

def test_the_header_carries_three_exclusive_method_buttons(app):
    page = _page(app)
    buttons = page._full_method_buttons

    assert [b.text() for b in
            (buttons["original"], buttons["tophat"], buttons["cucim"])] == [
        "Original", "TopHat", "cuCIM"]
    assert page._full_method_group.exclusive()
    assert buttons["original"].isChecked()
    assert not buttons["tophat"].isChecked()


@pytest.mark.parametrize(
    ("source", "method", "key"),
    [("tophat", "tophat", "effective_tophat_radius"),
     ("cucim", "cucim", "effective_cucim_sigma")])
def test_a_method_button_previews_with_the_rows_current_parameters(
        app, monkeypatch, source, method, key):
    _no_workers(monkeypatch)
    page = _page(app)
    tab = _RecordingExploreTab()
    page._explore_tab = tab
    page._channel_params["CD3"] = {"tophat_radius": 41, "cucim_sigma": 57}
    expected = page.preview_source_provider.describe("CD3")["correction"][key]

    page._full_method_buttons[source].click()

    assert page._full_image_source == source
    assert tab.calls[-1] == ("CD3", method, (int(expected),))
    assert page._full_method_buttons[source].isChecked()
    assert not page._full_method_buttons["original"].isChecked()


def test_switching_method_keeps_the_camera(app, monkeypatch):
    """No viewport is handed down, so the controller leaves the view alone."""
    _no_workers(monkeypatch)
    page = _page(app)
    tab = _RecordingExploreTab()
    page._explore_tab = tab

    page._full_method_buttons["tophat"].click()
    page._full_method_buttons["cucim"].click()
    page._full_method_buttons["original"].click()

    assert tab.viewports == [None, None, None]
    assert [c[1] for c in tab.calls] == ["tophat", "cucim", None]


def test_the_preview_ignores_the_checkbox_and_the_method_combo(app,
                                                               monkeypatch):
    """Every marker channel can be previewed corrected -- that is what makes
    the switch a way to DECIDE whether to correct it."""
    _no_workers(monkeypatch)
    page = _page(app)
    tab = _RecordingExploreTab()
    page._explore_tab = tab
    row = page._channel_rows["CD3"]
    row["checkbox"].setChecked(False)
    row["method_cb"].setCurrentText("Original")
    assert page._channel_decisions["CD3"] == "original"

    page._full_method_buttons["tophat"].click()

    assert tab.calls[-1][1] == "tophat"
    # ...and the preview changed no decision of its own.
    assert page._channel_decisions["CD3"] == "original"
    assert not row["checkbox"].isChecked()


def test_a_preview_marks_nothing_computed(app, monkeypatch):
    _no_workers(monkeypatch)
    page = _page(app)
    page._explore_tab = _RecordingExploreTab()

    page._full_method_buttons["cucim"].click()

    assert page._computed_channels == set()
    assert page._computed_signatures == {}
    assert page._channel_compute_state("CD3") == "not-computed"


def test_the_reference_channel_is_never_previewed_corrected(app, monkeypatch):
    """DAPI is the overlay, not a subject: the corrected buttons are shut."""
    _no_workers(monkeypatch)
    page = _page(app)
    tab = _RecordingExploreTab()
    page._explore_tab = tab
    page.current_channel = "DAPI"

    page._update_full_method_buttons()

    assert page._full_method_buttons["original"].isEnabled()
    assert not page._full_method_buttons["tophat"].isEnabled()
    assert not page._full_method_buttons["cucim"].isEnabled()
    assert page._full_method_buttons["original"].isChecked()

    before = list(tab.calls)
    page._on_full_method_clicked("tophat")
    assert tab.calls == before, "a corrected preview of DAPI was requested"
    assert page._full_image_source == "original"


def test_selecting_the_dapi_row_only_moves_the_intensity_window(app,
                                                                monkeypatch):
    _no_workers(monkeypatch)
    page = _page(app)
    page._explore_tab = _RecordingExploreTab()

    page._on_channel_selected_by_id("DAPI")

    assert page.current_channel == "CD3"        # display unchanged
    assert page._inspector_channel == "DAPI"


# ── the coarse-level hint ────────────────────────────────────────────────

@pytest.mark.parametrize("level", [0, 1])
def test_no_coarse_hint_at_a_fine_level(app, monkeypatch, level):
    _no_workers(monkeypatch)
    page = _page(app)
    page._explore_tab = _RecordingExploreTab(stack=_FakeStack(level))
    page._full_image_source = "tophat"

    page._update_full_level_hint()

    # `isHidden`, not `isVisible`: the hint is inside the full-image page of
    # a stack that is not on screen in a headless test, so `isVisible` is
    # False for every label here and would prove nothing.
    assert page._full_level_hint.isHidden()


def test_a_coarse_level_says_the_preview_is_downsampled(app, monkeypatch):
    _no_workers(monkeypatch)
    page = _page(app)
    page._explore_tab = _RecordingExploreTab(stack=_FakeStack(3))
    page._full_image_source = "cucim"

    page._update_full_level_hint()

    text = page._full_level_hint.text()
    assert "×8" in text, text                       # 4096 -> 512 at level 3
    assert "zoom in" in text
    assert not page._full_level_hint.isHidden()


def test_the_hint_is_about_the_correction_not_the_zoom(app, monkeypatch):
    """Original at a coarse level is just a smaller picture of the same
    pixels; there is nothing to warn about."""
    _no_workers(monkeypatch)
    page = _page(app)
    page._explore_tab = _RecordingExploreTab(stack=_FakeStack(4))
    page._full_image_source = "original"

    page._update_full_level_hint()

    assert page._full_level_hint.isHidden()


def test_the_hint_threshold_is_the_documented_one(app, monkeypatch):
    _no_workers(monkeypatch)
    page = _page(app)
    page._explore_tab = _RecordingExploreTab(
        stack=_FakeStack(FULL_IMAGE_COARSE_LEVEL))
    page._full_image_source = "tophat"

    page._update_full_level_hint()

    assert not page._full_level_hint.isHidden()


# ── 3. only the checkbox + Process compute ───────────────────────────────

def test_clicking_an_unticked_row_starts_nothing(app, monkeypatch):
    """The removed behaviour, pinned: after a completed Process, selecting a
    channel that was never ticked used to launch a worker for it."""
    started = _no_workers(monkeypatch)
    page = _page(app)
    page._explore_tab = _RecordingExploreTab()
    _finish_run(page, ["CD3"])
    assert page._process_completed is True
    assert not page._channel_rows["CD20"]["checkbox"].isChecked()

    page._on_channel_selected_by_id("CD20")

    assert page.current_channel == "CD20"          # it still DISPLAYS
    assert started == []
    assert "CD20" not in page._computed_channels


def test_no_helper_is_left_that_could_start_one(app):
    page = _page(app)
    assert not hasattr(page, "_start_ondemand")


@pytest.mark.parametrize(
    ("what", "drive"),
    [("a patch change", lambda p: p._select_patch(0)),
     ("a channel click", lambda p: p._on_channel_selected_by_id("CD20")),
     ("a method combo change",
      lambda p: p._channel_rows["CD20"]["method_cb"].setCurrentText("cucim")),
     ("a sigma change", lambda p: p._dec_sigma.setValue(37)),
     ("a method preview", lambda p: p._full_method_buttons["tophat"].click())])
def test_nothing_but_process_reaches_a_worker(app, monkeypatch, what, drive):
    """None of these is a request to compute. Changing a number, moving to
    another channel, previewing a method: they change what is DISPLAYED and
    what a later Process would do, and none of them puts the GPU to work.

    Pressing Enter in a param box is not on this list, and used to be. It
    is the one deliberate "this channel, now" the page has -- the
    per-channel Process button was removed in favour of the single one, so
    without it there is no way to say it at all -- and it is pinned as
    starting exactly one run, from the real keystroke on the real widget,
    in `test_step0_decision_enter_recompute.py`. Note the case just above:
    changing the sigma WITHOUT Enter is still nothing.
    """
    started = _no_workers(monkeypatch)
    page = _page(app)
    page._explore_tab = _RecordingExploreTab()
    _finish_run(page, ["CD3"])

    drive(page)

    assert started == [], f"{what} started a correction run"


def test_process_computes_exactly_the_ticked_channels(app, monkeypatch):
    page = _page(app)
    page._explore_tab = _RecordingExploreTab()
    asked = {}

    class _Worker(QtCore.QThread):
        channel_patch_done = QtCore.pyqtSignal(str, int, dict)
        channel_done = QtCore.pyqtSignal(str)
        all_done = QtCore.pyqtSignal()
        progress = QtCore.pyqtSignal(int, int, str)
        error_signal = QtCore.pyqtSignal(str, int, str)
        canceled = QtCore.pyqtSignal()

        def __init__(self, _loader, _patches, channels, *_a, **_k):
            super().__init__()
            asked["channels"] = dict(channels)

        def run(self):
            return

    monkeypatch.setattr(sp, "BatchProcessWorker", _Worker)
    for ch in ("CD3", "CD20"):
        page._channel_rows[ch]["method_cb"].setCurrentText("TopHat")
        page._channel_rows[ch]["checkbox"].setChecked(True)

    page._on_process_clicked()

    assert asked["channels"] == {"CD3": "tophat", "CD20": "tophat"}


def test_a_parameter_change_only_marks_the_channel_stale(app, monkeypatch):
    started = _no_workers(monkeypatch)
    page = _page(app)
    page._explore_tab = _RecordingExploreTab()
    _finish_run(page, ["CD3"])
    assert page._channel_compute_state("CD3") == "computed"

    page.current_channel = "CD3"
    page._dec_sigma.setValue(51)
    page._on_dec_param_changed()

    assert page._channel_compute_state("CD3") == "stale"
    assert started == []
    # The result is still there -- stale is not "gone".
    assert ("CD3", 0) in page._preview_cache


# ── 4. the per-row state glyph ───────────────────────────────────────────

def test_a_fresh_row_reads_not_computed(app):
    page = _page(app)
    row = page._channel_rows["CD3"]["row_widget"]

    assert page._channel_compute_state("CD3") == "not-computed"
    assert row.state_lbl.text() == "○"
    assert "not computed" in row.state_lbl.toolTip()


def test_the_glyph_sits_next_to_the_checkbox(app):
    page = _page(app)
    row = page._channel_rows["CD3"]["row_widget"]
    layout = row.layout()
    order = [layout.itemAt(i).widget() for i in range(layout.count())]

    assert order.index(row.state_lbl) == order.index(row.checkbox) + 1


def test_the_glyph_follows_a_run_and_then_a_parameter_change(app, monkeypatch):
    _no_workers(monkeypatch)
    page = _page(app)
    page._explore_tab = _RecordingExploreTab()
    row = page._channel_rows["CD3"]["row_widget"]

    _finish_run(page, ["CD3"])
    assert page._channel_compute_state("CD3") == "computed"
    assert row.state_lbl.text() == "✓"

    page.current_channel = "CD3"
    page._dec_radius.setValue(37)
    page._on_dec_param_changed()

    assert row.state_lbl.text() == "!"
    assert "stale" in row.state_lbl.toolTip()


def test_a_method_change_makes_a_computed_channel_stale(app, monkeypatch):
    _no_workers(monkeypatch)
    page = _page(app)
    page._explore_tab = _RecordingExploreTab()
    page._channel_rows["CD3"]["method_cb"].setCurrentText("TopHat")
    _finish_run(page, ["CD3"], method="tophat")
    assert page._channel_compute_state("CD3") == "computed"

    page._channel_rows["CD3"]["method_cb"].setCurrentText("cucim")

    assert page._channel_compute_state("CD3") == "stale"


def test_the_reference_row_is_marked_as_one(app):
    page = _page(app)
    row = page._channel_rows["DAPI"]["row_widget"]

    assert page._channel_compute_state("DAPI") == "nucleus"
    assert row.state_lbl.text() == "★"


def test_a_stopped_run_does_not_leave_a_row_spinning(app):
    page = _page(app)
    page._pending_signatures["CD3"] = page._channel_signature("CD3", "both")
    page._set_channel_computing("CD3")
    assert page._channel_compute_state("CD3") == "computing"

    page._on_batch_canceled()

    assert page._channel_compute_state("CD3") == "not-computed"


def test_a_dataset_switch_resets_every_glyph(app, monkeypatch):
    _no_workers(monkeypatch)
    page = _page(app)
    page._explore_tab = _RecordingExploreTab()
    _finish_run(page, ["CD3"])
    assert page._channel_compute_state("CD3") == "computed"

    page._reset_dataset_view_state()

    assert page._channel_compute_state("CD3") == "not-computed"
    assert page._channel_rows["CD3"]["row_widget"].state_lbl.text() == "○"


def test_a_computed_channel_can_still_be_unticked(app, monkeypatch):
    """It has to be: unticking is how a computed channel is saved raw."""
    _no_workers(monkeypatch)
    page = _page(app)
    page._explore_tab = _RecordingExploreTab()
    _finish_run(page, ["CD3"])
    cb = page._channel_rows["CD3"]["checkbox"]

    assert cb.isEnabled()
    cb.setChecked(False)

    assert page._channel_decisions["CD3"] == "original"


# ── 5. Save names the channels it writes raw ─────────────────────────────

def _confirm_recorder(monkeypatch):
    asked = []

    class _Msg:
        Ok = 0x00000400
        Cancel = 0x00400000

        @staticmethod
        def question(*a, **k):
            asked.append(a[2] if len(a) > 2 else "")
            return _Msg.Ok

        @staticmethod
        def information(*a, **k):
            pass

        @staticmethod
        def warning(*a, **k):
            pass

    monkeypatch.setattr(sp, "QMessageBox", _Msg)
    return asked, _Msg


def test_save_lists_the_raw_channels_once(app, monkeypatch):
    page = _page(app)
    page._explore_tab = _RecordingExploreTab()
    _finish_run(page, ["CD3"], method="tophat")
    page._channel_rows["CD3"]["method_cb"].setCurrentText("TopHat")
    page._channel_rows["CD3"]["checkbox"].setChecked(True)
    _finish_run(page, ["CD3"], method="tophat")
    # CD20 is ticked but was never computed -> raw. CD3 is computed -> not.
    page._channel_rows["CD20"]["method_cb"].setCurrentText("cucim")
    page._channel_rows["CD20"]["checkbox"].setChecked(True)
    asked, _ = _confirm_recorder(monkeypatch)

    assert page._raw_save_channels() == ["CD20"]
    assert page._confirm_raw_channels() is True
    assert len(asked) == 1, "the confirmation was shown more than once"
    assert "CD20" in asked[0]
    assert "CD3" not in asked[0].replace("CD20", "")


def test_an_untouched_channel_counts_as_raw(app, monkeypatch):
    page = _page(app)
    _confirm_recorder(monkeypatch)

    assert page._raw_save_channels() == ["CD3", "CD20"]
    # ...and the reference channel is not one of them.
    assert "DAPI" not in page._raw_save_channels()


def test_cancelling_the_confirmation_stops_the_save(app, monkeypatch):
    page = _page(app)
    page.output_dir = "/nonexistent-should-never-be-written"
    page._analysis_region_mode = "full_wsi"
    asked, msg = _confirm_recorder(monkeypatch)
    monkeypatch.setattr(msg, "question",
                        staticmethod(lambda *a, **k: msg.Cancel))
    made = []
    monkeypatch.setattr(sp, "create_full_wsi_context",
                        lambda *a, **k: made.append(a))

    page._save_and_continue()

    assert made == [], "Save went ahead after the confirmation was cancelled"


def test_no_confirmation_when_everything_is_corrected(app, monkeypatch):
    page = _page(app)
    page._explore_tab = _RecordingExploreTab()
    for ch in ("CD3", "CD20"):
        page._channel_rows[ch]["method_cb"].setCurrentText("TopHat")
        page._channel_rows[ch]["checkbox"].setChecked(True)
    _finish_run(page, ["CD3", "CD20"], method="tophat")
    asked, _ = _confirm_recorder(monkeypatch)

    assert page._raw_save_channels() == []
    assert page._confirm_raw_channels() is True
    assert asked == []


# ── 8. the landing channel is DAPI ───────────────────────────────────────
#
# A slide opens on the channel that shows where the tissue IS, not on
# whichever marker happens to be first in the panel file. DAPI is a
# REFERENCE channel everywhere else on this page -- the thing drawn on top
# of a marker, and clicking its row only re-points the Intensity window --
# but at the moment a slide loads there is no marker to be a reference FOR,
# so DAPI is the subject. Choosing a marker row is the gesture that leaves
# this state, and nothing about it changes.

def _landed(app, monkeypatch, stack=None):
    _no_workers(monkeypatch)
    page = _page(app, patches=False, channel=None)
    tab = _RecordingExploreTab(stack=stack or _FakeStack())
    page._explore_tab = tab
    page._enter_full_image_landing()
    return page, tab


def test_a_loaded_slide_lands_on_dapi(app, monkeypatch):
    page, tab = _landed(app, monkeypatch)

    assert page.current_channel == "DAPI"
    assert page._showing_nucleus() is True
    # Raw: no method, no parameters, no correction of any kind.
    assert tab.calls == [("DAPI", None, ())]
    assert tab.viewports == [None]


def test_the_dapi_row_is_the_selected_one(app, monkeypatch):
    """The list agrees with the picture. The DAPI row is what the dock has
    to say "nothing chosen yet" WITH -- it is a real row, it is selectable,
    and leaving no row selected would leave the shared dock in a state
    (`selection_changed("")`) whose only meaning here is "no channel"."""
    page, _tab = _landed(app, monkeypatch)

    assert page._channel_list.currentRow() == page._channel_order.index("DAPI")
    assert page._inspector_channel is None


def test_the_landing_shows_dapi_in_dapis_own_colour(app, monkeypatch):
    page, _tab = _landed(app, monkeypatch)

    assert page._full_image_tint() == page._channel_color("DAPI")
    assert page._full_image_tint() != page._channel_color("CD3")


def test_the_landing_gives_the_viewer_dapis_display_mapping(app, monkeypatch):
    stack = _FakeStack()
    page, _tab = _landed(app, monkeypatch, stack=stack)

    page._apply_full_image_display(stack)

    assert stack.controller.mapping == (
        page._display_mapping_for("DAPI") + ("DAPI",))


def test_the_correction_switch_is_shut_while_dapi_is_the_picture(
        app, monkeypatch):
    """DAPI is never background-corrected, so there is nothing for TopHat or
    cuCIM to preview -- and the switch says so rather than offering it."""
    page, tab = _landed(app, monkeypatch)

    assert page._full_method_buttons["original"].isEnabled()
    assert page._full_method_buttons["original"].isChecked()
    assert not page._full_method_buttons["tophat"].isEnabled()
    assert not page._full_method_buttons["cucim"].isEnabled()

    before = list(tab.calls)
    page._on_full_method_clicked("tophat")
    assert tab.calls == before
    assert page._full_image_source == "original"


def test_the_landing_never_draws_dapi_twice(app, monkeypatch):
    """The DAPI checkbox means "add DAPI on top of the MARKER". With DAPI
    itself on screen there is no marker under it, so the overlay would add
    the channel to itself -- so it is not DRAWN.

    Not drawn is suppression, and suppression is the renderer's: the
    builder sets it from the channel (`set_suppressed(nucleus == channel)`)
    and the layer ANDs it with the switch. So `nucleus_enabled` carries the
    user's switch, unchanged, and it is `_full_image_nucleus_enabled` --
    the derived "is it on screen" -- that goes false. Handing the derived
    value to the builder instead is what used to lose the setting.
    """
    page, _tab = _landed(app, monkeypatch)
    page._btn_full_nucleus.setChecked(True)      # the user's standing wish

    assert page._full_image_nucleus_enabled() is False
    # the switch travels intact ...
    assert page._full_image_nucleus_args()["nucleus_enabled"] is True
    assert page._nucleus_layer_visible() is True
    # ... and the toggle is disabled while it could not mean anything
    assert page._btn_full_nucleus.isEnabled() is False
    # ...and the toolbar does not call the channel on screen a hidden layer.
    assert "DAPI" not in page._hidden_full_layers()


def test_choosing_a_marker_hands_the_overlay_back(app, monkeypatch):
    """The checked state is the user's and is never touched: it comes back
    whole the moment there is a marker to overlay."""
    page, _tab = _landed(app, monkeypatch)
    page._btn_full_nucleus.setChecked(True)

    page._on_channel_selected_by_id("CD3")

    assert page.current_channel == "CD3"
    assert page._showing_nucleus() is False
    assert page._btn_full_nucleus.isEnabled() is True
    assert page._full_image_nucleus_enabled() is True
    assert page._full_image_nucleus_args()["nucleus_enabled"] is True


def test_the_dapi_row_keeps_its_reference_meaning_after_a_marker(
        app, monkeypatch):
    """Unchanged: once a marker is on screen, the DAPI row is the reference
    row again -- it moves the Intensity window and leaves the picture."""
    page, _tab = _landed(app, monkeypatch)
    page._on_channel_selected_by_id("CD3")

    page._on_channel_selected_by_id("DAPI")

    assert page.current_channel == "CD3"          # display unchanged
    assert page._inspector_channel == "DAPI"
    assert page._showing_nucleus() is False
    assert page._full_method_buttons["tophat"].isEnabled()
