"""Clicking a channel row in Step 0 switches the page's channel.

Reported from manual testing: after a multi-channel Process only the first
channel could be viewed. Measured: a mouse click moved the list's current
row, but the page's `current_channel` did not follow. The click goes
`GlobalChannelRow.mousePressEvent` -> the dock's row-click rule ->
`ChannelDisplayState.set_selected_channel`, which sets the list's current item with
signals BLOCKED, so the page's `currentRowChanged` slot never ran. The page
now listens to the model's `selection_changed`, which both a click and a
programmatic `setCurrentRow` reach.

Own module: page-heavy Step0 suites crash pyqtgraph offscreen when combined
with the background-correction module in one process.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtCore, QtTest, QtWidgets  # noqa: E402

from block01.ui.step0 import step0_page as sp  # noqa: E402

from test_step0_background_correction_tab import _GpuPathLoader, app  # noqa: E402,F401


def _page(app):
    page = sp.Step0Page()
    page.loader = _GpuPathLoader()
    page.patches = [(0, 32, 0, 32)]
    page.current_patch_idx = 0
    page.nucleus_channel = "DAPI"
    page._rebuild_channel_list()
    page.resize(1600, 1000)
    page.show()
    QtTest.QTest.qWait(50)
    return page


def _click_row(page, ch):
    """A real mouse click on the channel's NAME, as a user does."""
    row = page._channel_rows[ch]["row_widget"]
    target = next((c for c in row.findChildren(QtWidgets.QLabel)
                   if c.text().startswith(ch)), row)
    QtTest.QTest.mouseClick(target, QtCore.Qt.LeftButton, pos=target.rect().center())
    QtTest.QTest.qWait(30)


def _finish_run(page, channels):
    disp = np.zeros((32, 32), np.float32)
    m = {"snr": 1.0, "bg_cv": 0.1}
    for ch in channels:
        page._set_channel_computing(ch)
        page._on_batch_patch_done(ch, 0, {
            "original_disp": disp, "tophat_disp": disp, "cucim_disp": disp,
            "original_metrics": m, "tophat_metrics": m, "cucim_metrics": m,
            "nucleus_disp": None})
        page._set_channel_done(ch)
    page._on_batch_all_done()


def test_a_mouse_click_on_a_row_switches_the_channel(app):
    page = _page(app)
    assert page.current_channel == "DAPI"           # the landing channel

    _click_row(page, "CD3")
    assert page.current_channel == "CD3"

    _click_row(page, "CD20")

    assert page.current_channel == "CD20"
    assert page._channel_list.currentRow() == page._channel_order.index("CD20")


def test_rows_stay_clickable_after_a_multi_channel_run(app):
    """The reported case: several channels corrected, then only the first
    one viewable."""
    page = _page(app)
    _finish_run(page, ["CD3", "CD20"])
    _click_row(page, "CD3")
    assert page.current_channel == "CD3"

    _click_row(page, "CD20")
    assert page.current_channel == "CD20"
    assert "CD20" in page._preview_status.text()
    assert page._last_payload is page._preview_cache[("CD20", 0)]

    _click_row(page, "CD3")
    assert page.current_channel == "CD3"
    assert page._last_payload is page._preview_cache[("CD3", 0)]


def test_a_programmatic_selection_still_reaches_the_page_exactly_once(app):
    page = _page(app)
    calls = []
    real = page._on_channel_row_changed
    page._on_channel_row_changed = lambda row: (calls.append(row), real(row))[1]
    # Route through the same connection the page made at construction.
    # Route through the same connection the page made at construction: the
    # SHARED state's selection, which is what the public dock projects.
    state = page.display.state
    state.selection_changed.disconnect(page._on_channel_selected_by_id)
    state.selection_changed.connect(page._on_channel_selected_by_id)

    page._channel_list.setCurrentRow(page._channel_order.index("CD20"))
    QtTest.QTest.qWait(30)

    assert page.current_channel == "CD20"
    assert calls == [page._channel_order.index("CD20")]


def test_clicking_the_current_row_again_does_not_rerun_the_handler(app):
    """The model de-duplicates: a second click on the selected channel must
    not, for instance, start a second on-demand computation."""
    page = _page(app)
    _click_row(page, "CD20")
    calls = []
    real = page._on_channel_row_changed
    page._on_channel_row_changed = lambda row: (calls.append(row), real(row))[1]
    # Route through the same connection the page made at construction: the
    # SHARED state's selection, which is what the public dock projects.
    state = page.display.state
    state.selection_changed.disconnect(page._on_channel_selected_by_id)
    state.selection_changed.connect(page._on_channel_selected_by_id)

    _click_row(page, "CD20")

    assert calls == []
    assert page.current_channel == "CD20"


def test_clicking_the_nucleus_row_keeps_the_displayed_channel(app):
    """DAPI is a reference channel: its row hands the Intensity window DAPI's
    mapping and changes nothing else -- the page keeps showing the marker."""
    page = _page(app)
    _click_row(page, "CD20")

    _click_row(page, "DAPI")

    assert page.display.state.selected_channel() == "DAPI"
    assert page.current_channel == "CD20"
    assert page._inspector_channel == "DAPI"


def test_the_page_no_longer_depends_on_current_row_changed(app):
    """If the list's own signal were still the page's source, blocking it
    (as the dock does on a click) would lose the selection."""
    page = _page(app)
    page._channel_list.blockSignals(True)
    try:
        page.display.state.set_selected_channel("CD20", origin="test")
    finally:
        page._channel_list.blockSignals(False)
    assert page.current_channel == "CD20"


# ── B4-A: a click shows, a programmatic selection does not ───────────────

def test_a_click_on_a_hidden_row_shows_that_channel(app):
    """Selecting something you cannot see is a dead end, so a CLICK shows it.

    The checkbox is display visibility since B4-A, and a fresh slide lands on
    DAPI with its markers hidden -- so the gesture that chooses a marker is
    also the gesture that puts it on screen.
    """
    page = _page(app)
    state = page.display.state
    # CD20 is not the marker a fresh slide shows, so it starts hidden.
    assert state.display_visible("CD20") is False

    _click_row(page, "CD20")

    assert page.current_channel == "CD20"
    assert state.display_visible("CD20") is True
    assert page._channel_rows["CD20"]["checkbox"].isChecked() is True
    # A click is not a correction decision.
    assert page._channel_decisions.get("CD20") in (None, "")


def test_a_programmatic_selection_does_not_show_a_hidden_channel(app):
    """A restore, a dataset switch or the landing rule selects without
    showing: a channel the user deliberately hid must not come back because
    something moved the cursor onto it."""
    page = _page(app)
    state = page.display.state
    state.set_display_visible("CD3", False, origin="test")

    page._on_channel_selected_by_id("CD3")

    assert page.current_channel == "CD3"
    assert state.display_visible("CD3") is False
    assert page._channel_rows["CD3"]["checkbox"].isChecked() is False


def test_a_click_on_a_visible_row_changes_nothing_but_the_selection(app):
    page = _page(app)
    state = page.display.state
    state.set_display_visible("CD3", True, origin="test")
    seen = []
    state.visibility_changed.connect(lambda *a: seen.append(a))

    _click_row(page, "CD3")

    assert page.current_channel == "CD3"
    assert seen == [], f"a click on a shown row wrote visibility: {seen}"


def test_the_row_checkbox_only_moves_display_visibility(app):
    """Ticking a row used to assign a background-correction method and
    unticking it wrote `original` over the user's decision."""
    page = _page(app)
    page._channel_rows["CD3"]["method_cb"].setCurrentText("TopHat")
    decisions = dict(page._channel_decisions)
    methods = dict(page._channel_methods)
    fusion_before = page.display.fusion.draft_snapshot()

    cb = page._channel_rows["CD3"]["checkbox"]
    cb.setChecked(True)
    cb.setChecked(False)

    assert page._channel_decisions == decisions
    assert page._channel_methods == methods
    assert page.display.fusion.draft_snapshot() == fusion_before
    assert page.display.state.display_visible("CD3") is False


def test_the_dapi_row_is_a_visibility_row_and_stays_uncorrectable(app):
    page = _page(app)
    caps = page.display.state.capabilities("DAPI")
    assert caps.is_nucleus is True
    assert caps.display_toggleable is True
    assert caps.correction_eligible is False
    assert caps.bulk_toggleable is False

    cb = page._channel_rows["DAPI"]["checkbox"]
    cb.setChecked(False)

    assert page.display.state.display_visible("DAPI") is False
    assert page._nucleus_layer_visible() is False
    assert "DAPI" not in page._channel_decisions
    assert "DAPI" not in page._channel_methods
    assert page._channel_rows["DAPI"]["method_cb"].isEnabled() is False


def test_show_all_and_hide_all_move_display_only(app):
    """The bulk control is Show all / Hide all: it sweeps the channels whose
    capabilities allow it, and it is not the bulk CORRECTION control."""
    page = _page(app)
    page._channel_rows["CD3"]["method_cb"].setCurrentText("cucim")
    decisions = dict(page._channel_decisions)
    fusion_before = page.display.fusion.draft_snapshot()
    state = page.display.state

    page._cb_all.setChecked(True)

    for ch in page._channel_order:
        if ch == page.nucleus_channel:
            continue
        assert state.display_visible(ch) is True, ch
    # The nucleus is not swept: its layer is shown and hidden deliberately.
    assert state.display_visible("DAPI") is True
    assert page._channel_decisions == decisions
    assert page.display.fusion.draft_snapshot() == fusion_before

    page._cb_all.setChecked(False)

    for ch in page._channel_order:
        if ch == page.nucleus_channel:
            continue
        assert state.display_visible(ch) is False, ch
    assert state.display_visible("DAPI") is True, "the DAPI layer was swept"
    assert page._channel_decisions == decisions


def test_the_bulk_method_box_moves_the_preview_method_only(app):
    """...and the other bulk control is the mirror image of it.

    Show all / Hide all moves visibility and no method; the Method box moves
    the PREVIEW method and no visibility -- and no final decision either,
    which is the Per-Channel Decision panel's answer.
    """
    page = _page(app)
    state = page.display.state
    page._channel_rows["CD3"]["method_cb"].setCurrentText("cucim")
    state.set_display_visible("CD3", True, origin="test")
    state.set_display_visible("CD8", False, origin="test")
    visible_before = dict(state.display_visibility())
    decisions_before = dict(page._channel_decisions)

    page._method_all.setCurrentText("TopHat")

    assert page._channel_preview_method("CD3") == "tophat"
    assert dict(state.display_visibility()) == visible_before
    assert dict(page._channel_decisions) == decisions_before
    assert "DAPI" not in page._channel_methods
    assert "DAPI" not in page._channel_decisions


# ── B4-A follow-up: the capabilities DRIVE the row's controls ────────────

def test_the_rows_controls_follow_the_capabilities_not_a_locked_flag(app):
    """`locked` used to stand for four decisions at once, so a channel that
    is never background-corrected also could not be shown and was skipped by
    a bulk sweep. Each control asks its own permission now."""
    page = _page(app)
    # The capabilities live on the SHARED state since B4-B; the public row
    # asks them directly, so there is no second copy on a dock model.
    state = page.display.state
    dock = page._dock_adapter.dock

    dapi = state.capabilities("DAPI")
    assert dapi.display_toggleable is True
    assert dapi.correction_eligible is False
    assert dapi.bulk_toggleable is False
    dapi_row = page._channel_rows["DAPI"]["row_widget"]
    assert dapi_row.checkbox.isEnabled() is True      # shown and hidden
    assert dapi_row.method_cb.isEnabled() is False    # never corrected

    marker = state.capabilities("CD3")
    assert marker.display_toggleable is True
    assert marker.correction_eligible is True
    assert marker.bulk_toggleable is True
    marker_row = page._channel_rows["CD3"]["row_widget"]
    assert marker_row.checkbox.isEnabled() is True
    assert marker_row.method_cb.isEnabled() is True

    # ...and a bulk sweep follows `bulk_toggleable` too.
    state.set_display_visible("DAPI", True, origin="test")
    dock.set_all_visible(False)
    assert state.display_visible("DAPI") is True, "the DAPI layer was swept"
    assert state.display_visible("CD3") is False


def test_a_fresh_slide_shows_its_first_marker_and_hides_the_rest(app):
    """The ruled default: a picture to land on, without handing Step1 a
    slide with every channel stacked. DAPI keeps its own product default."""
    page = _page(app)
    visibility = page.display.state.display_visibility()

    markers = [ch for ch in page._channel_order if ch != page.nucleus_channel]
    assert visibility[markers[0]] is True, visibility
    for ch in markers[1:]:
        assert visibility[ch] is False, (ch, visibility)
    assert visibility["DAPI"] is True
    # ...and the whole thing arrived as ONE install: order, capabilities,
    # visibility and selection together.
    assert page.display.state.channel_order() == tuple(page._channel_order)
    assert page.display.state.selected_channel() == page.current_channel


def test_answers_someone_else_recorded_survive_a_rebuild(app):
    """A session or a walk back to this slide keeps every display answer;
    the defaults are only for a channel nobody has answered for."""
    page = _page(app)
    state = page.display.state
    state.set_display_visible("CD3", False, origin="step1")
    state.set_display_visible("CD20", True, origin="step1")
    state.set_display_visible("DAPI", False, origin="step1")

    page._rebuild_channel_list()

    visibility = state.display_visibility()
    assert visibility["CD3"] is False, visibility
    assert visibility["CD20"] is True, visibility
    assert visibility["DAPI"] is False, visibility
