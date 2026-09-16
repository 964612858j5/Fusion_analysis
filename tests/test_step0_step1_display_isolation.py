"""Step0 and Step1 keep their own ticks and their own current channel.

Reported from the real machine, 2026-09-16: ticking a channel in Step1 ticked
it in Step0 as well, and selecting a row in one step moved the other's
selection. Reusing ONE dock across the steps was never meant to mean one set
of display answers -- a step shows what it is working on.

What is separated, and what deliberately is not:

    separate   the display tick, the current channel
    shared     colour, Min/Max/Gamma, channel order and names, and the dock
               instance itself with its rows, its search text and its scroll
    Step1 only fusion participation and weights (the scientific draft)

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Loader:
    filepath = "/tmp/isolation.ome.tiff"
    shape = (256, 256)

    def __init__(self, names=("DAPI", "CD3", "CD8", "CD20")):
        self._names = list(names)
        self.ch_map = {c: i for i, c in enumerate(self._names)}

    def channel_names(self):
        return list(self._names)

    def read_region(self, channel, y0, y1, x0, x1, downsample=1,
                    normalize=True):
        return np.zeros(((y1 - y0) or 1, (x1 - x0) or 1), np.float32)


def _window(app):
    from block01.ui.main_window import MainWindow
    w = MainWindow()
    w._schedule_step1_session_save = lambda: None
    w._save_step1_session = lambda *a, **k: None
    loader = _Loader()
    w.loader = loader
    w._step0.loader = loader
    w._step0.nucleus_channel = "DAPI"
    w._step0._rebuild_channel_list()
    w.config.set_channels(loader.channel_names())
    w.config.load_panel({"markers": {"CD3": 0.0, "CD8": 0.0, "CD20": 0.0}},
                        "DAPI")
    w.config.set_nucleus("DAPI", 1.0)
    return w


def _close(w):
    w._display.shutdown("test")
    w.deleteLater()


def _in(w, step):
    w._set_step_active(step)
    QtWidgets.QApplication.processEvents()


# ── 1. ticks ──────────────────────────────────────────────────────────

def test_each_step_keeps_its_own_ticks(app):
    w = _window(app)
    try:
        state = w._display.state
        _in(w, 0)
        state.set_display_visible("CD3", True, origin="step0-test")
        assert state.display_visible("CD3") is True

        _in(w, 1)
        assert state.display_visible("CD3") is False, \
            "Step1 inherited Step0's tick"
        w._channel_dock.use_channel("CD8", True, origin="step1-test")
        assert state.display_visible("CD8") is True

        _in(w, 0)
        assert state.display_visible("CD3") is True    # Step0's own, back
        assert state.display_visible("CD8") is False, \
            "a Step1 tick reached Step0"
    finally:
        _close(w)


def test_a_step_opens_on_the_dataset_s_baseline_not_on_another_step_s_edits(app):
    """A scope is materialised from the dataset's shared pair.

    Step0's edits do not travel into a Step1 that has not been opened yet,
    and Step1's do not travel back.
    """
    w = _window(app)
    try:
        state = w._display.state
        _in(w, 0)
        state.set_display_visible("CD20", True, origin="step0-test")

        _in(w, 1)
        assert state.display_visible("CD20") is False, \
            "Step1 opened on Step0's edit instead of the dataset baseline"

        w._channel_dock.use_channel("CD20", True, origin="step1-test")
        _in(w, 0)
        assert state.display_visible("CD20") is True   # Step0's own, unchanged
    finally:
        _close(w)


def test_a_step1_session_restores_from_step0_without_touching_step0(app):
    """The session names its scope; the page on screen is irrelevant."""
    w = _window(app)
    try:
        state = w._display.state
        _in(w, 1)
        assert state.display_visible("CD8") is False
        _in(w, 0)
        state.set_display_visible("CD3", True, origin="step0-test")
        state.set_selected_channel("CD3", origin="step0-test")
        step0_before = dict(state.display_visibility())

        # Restored while standing in STEP0.
        w._apply_step1_display_state({
            "preview_mode": "overlay",
            "channel_visibility": {"DAPI": True, "CD8": True, "CD3": False},
            "current_channel": "CD8",
        })
        QtWidgets.QApplication.processEvents()

        assert dict(state.display_visibility()) == step0_before
        assert state.selected_channel() == "CD3"

        _in(w, 1)
        assert state.display_visible("CD8") is True
        assert state.display_visible("CD3") is False
    finally:
        _close(w)


# ── 2. current channel ────────────────────────────────────────────────

def test_each_step_keeps_its_own_current_channel(app):
    w = _window(app)
    try:
        state = w._display.state
        _in(w, 0)
        state.set_selected_channel("CD3", origin="step0-test")
        _in(w, 1)
        state.set_selected_channel("CD8", origin="step1-test")
        assert state.selected_channel() == "CD8"

        _in(w, 0)
        assert state.selected_channel() == "CD3", "the selection crossed"
        _in(w, 1)
        assert state.selected_channel() == "CD8"
    finally:
        _close(w)


# ── 3. Step1's science is Step1's ─────────────────────────────────────

def test_a_step1_weight_edit_leaves_step0_alone(app):
    w = _window(app)
    try:
        state, model = w._display.state, w._display.fusion
        _in(w, 0)
        step0_before = dict(state.display_visibility())

        _in(w, 1)
        w._channel_dock.row("CD8").spin.setValue(0.4)
        QtWidgets.QApplication.processEvents()
        assert model.fusion_enabled("CD8") is True
        assert state.display_visible("CD8") is True

        _in(w, 0)
        assert dict(state.display_visibility()) == step0_before
    finally:
        _close(w)


def test_unticking_in_step1_leaves_step0_alone(app):
    w = _window(app)
    try:
        state = w._display.state
        _in(w, 1)
        w._channel_dock.use_channel("CD3", True, origin="step1-test")
        _in(w, 0)
        state.set_display_visible("CD3", True, origin="step0-test")

        _in(w, 1)
        w._channel_dock.use_channel("CD3", False, origin="step1-test")
        assert w._display.fusion.fusion_enabled("CD3") is False

        _in(w, 0)
        assert state.display_visible("CD3") is True
    finally:
        _close(w)


def test_step0_display_work_leaves_step1_s_draft_alone(app):
    w = _window(app)
    try:
        state, model = w._display.state, w._display.fusion
        _in(w, 1)
        w._channel_dock.use_channel("CD3", True, origin="step1-test")
        draft = model.draft_snapshot()
        revision = model.draft_revision()

        _in(w, 0)
        state.set_display_visible("CD8", True, origin="step0-test")
        state.set_display_visible("CD3", False, origin="step0-test")
        state.set_selected_channel("CD8", origin="step0-test")

        assert model.draft_snapshot() == draft
        assert model.draft_revision() == revision
        _in(w, 1)
        assert model.fusion_enabled("CD3") is True
        assert state.display_visible("CD3") is True
    finally:
        _close(w)


def test_a_step_change_makes_no_command_of_its_own(app):
    """Switching steps writes nothing: no tick, no weight, no save."""
    w = _window(app)
    try:
        state, model = w._display.state, w._display.fusion
        saves, visibility, weights = [], [], []
        w._schedule_step1_session_save = lambda: saves.append(1)
        state.visibility_changed.connect(
            lambda ch, on: visibility.append((ch, on)))
        model.weight_changed.connect(lambda ch: weights.append(ch))

        _in(w, 1)
        w._channel_dock.use_channel("CD3", True, origin="step1-test")
        saves.clear(); visibility.clear(); weights.clear()
        revision = model.draft_revision()

        for step in (0, 1, 0, 1):
            _in(w, step)

        assert visibility == []
        assert weights == []
        assert saves == []
        assert model.draft_revision() == revision
    finally:
        _close(w)


def test_the_rows_on_screen_show_the_step_that_is_on(app):
    """The separation has to be VISIBLE: a row may not keep the other step's
    tick until something else happens to redraw it."""
    w = _window(app)
    try:
        state = w._display.state
        _in(w, 0)
        state.set_display_visible("CD3", True, origin="step0-test")
        assert w._channel_dock.row("CD3").checkbox.isChecked() is True

        _in(w, 1)
        assert w._channel_dock.row("CD3").checkbox.isChecked() is False, \
            "the row still shows Step0's tick"

        w._channel_dock.use_channel("CD20", True, origin="step1-test")
        assert w._channel_dock.row("CD20").checkbox.isChecked() is True

        _in(w, 0)
        assert w._channel_dock.row("CD3").checkbox.isChecked() is True
        assert w._channel_dock.row("CD20").checkbox.isChecked() is False
    finally:
        _close(w)


# ── 3b. the PAGES, not just the store ─────────────────────────────────

def test_a_step1_selection_leaves_step0_s_page_where_it_was(app):
    """Step0's current channel, its row and its viewer's channel stay put."""
    w = _window(app)
    try:
        state = w._display.state
        _in(w, 0)
        w._step0._on_channel_selected_by_id("CD3")
        QtWidgets.QApplication.processEvents()
        step0_current = w._step0.current_channel

        _in(w, 1)
        state.set_selected_channel("CD8", origin="step1-test")
        w._channel_dock.use_channel("CD8", True, origin="step1-test")
        QtWidgets.QApplication.processEvents()

        assert w._step0.current_channel == step0_current, \
            "Step1's selection moved Step0's current channel"
        # The ROWS are the one dock's, shared by design, so while Step1 is on
        # screen they show Step1's answers -- that is the point of one dock.
        # What must not have moved is Step0's own state, which is what the
        # page draws from when it comes back (see the catch-up test).
        assert w._display.state.scope() == "step1"
    finally:
        _close(w)


def test_step0_work_does_not_make_step1_load_or_redraw(app):
    """No loader demand, no patch redraw and no session save from Step0."""
    w = _window(app)
    try:
        state = w._display.state
        _in(w, 1)
        loads, draws, saves = [], [], []
        w._start_loader_for = lambda idx, needed=None: loads.append(
            list(needed or []))
        real_refresh = w._refresh_patch_preview
        w._refresh_patch_preview = lambda reset_view=False: draws.append(
            reset_view)
        w._schedule_step1_session_save = lambda: saves.append(1)

        _in(w, 0)
        loads.clear(); draws.clear(); saves.clear()
        state.set_display_visible("CD8", True, origin="step0-test")
        state.set_selected_channel("CD8", origin="step0-test")
        QtWidgets.QApplication.processEvents()

        assert loads == [], loads
        assert draws == [], draws
        assert saves == [], saves
        w._refresh_patch_preview = real_refresh
    finally:
        _close(w)


def test_entering_a_step_redraws_it_from_its_own_answers(app):
    """The catch-up: a page ignored the other step's notices, so it replays."""
    w = _window(app)
    try:
        state = w._display.state
        _in(w, 0)
        w._step0._on_channel_selected_by_id("CD3")
        state.set_display_visible("CD20", True, origin="step0-test")
        QtWidgets.QApplication.processEvents()

        _in(w, 1)
        state.set_selected_channel("CD8", origin="step1-test")
        w._channel_dock.use_channel("CD8", True, origin="step1-test")
        QtWidgets.QApplication.processEvents()

        # Back to Step0: its rows and its current channel are its own again.
        _in(w, 0)
        QtWidgets.QApplication.processEvents()
        assert w._step0.current_channel == "CD3"
        rows = w._step0._channel_rows or {}
        for ch, expected in (("CD20", True), ("CD8", False)):
            row = rows.get(ch)
            if row is not None and row.get("checkbox") is not None:
                assert row["checkbox"].isChecked() is expected, ch
    finally:
        _close(w)


def test_a_page_catches_up_on_answers_written_while_it_was_away(app):
    """The catch-up has work to do when the page's OWN answers moved.

    A restore writes Step0's partition while Step1 is on screen; Step0's page
    ignored nothing (it was told nothing) and must read them on entry.
    """
    w = _window(app)
    try:
        state = w._display.state
        _in(w, 0)
        w._step0._on_channel_selected_by_id("CD3")
        QtWidgets.QApplication.processEvents()
        assert w._step0.current_channel == "CD3"

        _in(w, 1)
        # Step0's partition moves with NO NOTICE REACHING ANYONE -- which is
        # what a bulk install does for a page that is not on screen, and the
        # case the catch-up exists for. Blocking the signals here is how the
        # test tells the catch-up apart from the per-field handlers.
        state.blockSignals(True)
        try:
            with state.using_scope("step0"):
                state.set_selected_channel("CD20", origin="step0-restore")
                state.set_display_visible("CD20", True, origin="step0-restore")
        finally:
            state.blockSignals(False)
        QtWidgets.QApplication.processEvents()
        assert w._step0.current_channel == "CD3", "Step0 acted while away"

        _in(w, 0)
        QtWidgets.QApplication.processEvents()
        assert w._step0.current_channel == "CD20", \
            "Step0 did not catch up with its own answers on entry"
    finally:
        _close(w)


def test_step1_s_panel_ignores_a_selection_made_in_step0(app):
    """The panel that carries Step1's current channel, and the Intensity
    window behind it, follow Step1's selection only."""
    w = _window(app)
    try:
        state = w._display.state
        _in(w, 1)
        state.set_selected_channel("CD8", origin="step1-test")
        QtWidgets.QApplication.processEvents()
        assert w.config.current_channel() == "CD8"

        _in(w, 0)
        state.set_selected_channel("CD3", origin="step0-test")
        QtWidgets.QApplication.processEvents()
        assert w.config.current_channel() == "CD8", \
            "Step0's selection moved Step1's panel"

        _in(w, 1)
        QtWidgets.QApplication.processEvents()
        assert w.config.current_channel() == "CD8"
    finally:
        _close(w)


def test_step3_does_not_follow_step0_or_step1_ticks(app):
    """Step3 draws the shared pair and was not part of this ruling."""
    w = _window(app)
    try:
        state = w._display.state
        renders = []
        w._step3._render_roi = lambda reset_view=False: renders.append(
            reset_view)
        w._step3._marker_channels = lambda: ["CD3", "CD8", "CD20"]

        _in(w, 1)
        state.set_display_visible("CD3", True, origin="step1-test")
        _in(w, 0)
        state.set_display_visible("CD8", True, origin="step0-test")
        QtWidgets.QApplication.processEvents()

        assert renders == [], renders
    finally:
        _close(w)


def test_a_step1_handoff_does_not_untick_step0(app):
    """The bug a real Save produced: everything off but DAPI.

    Saving in Step0 publishes a handoff, Step1's panel loads it, and its
    `load_panel` writes "show the nucleus and nothing else" -- which landed
    in whatever scope was current. With the user standing in Step0, that was
    Step0's own column (diagnosed from
    `channel.visibility ... origin=step1-load-panel` in a real run).
    """
    w = _window(app)
    try:
        state = w._display.state
        _in(w, 0)
        state.set_display_visible("CD3", True, origin="step0-test")
        state.set_display_visible("CD8", True, origin="step0-test")
        before = dict(state.display_visibility())

        # Step1's panel takes a handoff while Step0 is on screen.
        w.config.load_panel({"markers": {"CD3": 0.0, "CD8": 0.0,
                                         "CD20": 0.0}}, "DAPI")
        QtWidgets.QApplication.processEvents()

        assert dict(state.display_visibility()) == before, \
            "a Step1 handoff rewrote Step0's ticks"
        _in(w, 1)
        assert state.display_visible("CD3") is False
        assert state.display_visible("DAPI") is True
    finally:
        _close(w)


# ── 4. what stays shared ──────────────────────────────────────────────

def test_colour_and_intensity_are_still_shared(app):
    w = _window(app)
    try:
        state = w._display.state
        _in(w, 0)
        state.set_color("CD3", "#123456", origin="step0-test")
        state.set_mapping("CD3", 10.0, 200.0, 1.2, origin="step0-test")

        _in(w, 1)
        assert state.color("CD3").lower() == "#123456"
        assert state.mapping("CD3") == (10.0, 200.0, 1.2)

        state.set_color("CD3", "#abcdef", origin="step1-test")
        _in(w, 0)
        assert state.color("CD3").lower() == "#abcdef"
    finally:
        _close(w)


def test_the_dock_its_rows_and_its_search_are_the_same_objects(app):
    w = _window(app)
    try:
        _in(w, 0)
        dock = w._channel_dock
        row = dock.row("CD3")
        dock.search.setText("CD")
        bar = dock.list_widget.verticalScrollBar()
        bar.setValue(bar.maximum())
        keep = bar.value()

        _in(w, 1)
        assert w._channel_dock is dock
        assert dock.row("CD3") is row
        assert dock.search.text() == "CD"
        assert dock.list_widget.verticalScrollBar().value() == keep
    finally:
        _close(w)


def test_channel_order_and_names_are_still_shared(app):
    w = _window(app)
    try:
        state = w._display.state
        _in(w, 0)
        order = state.channel_order()
        _in(w, 1)
        assert state.channel_order() == order
    finally:
        _close(w)


# ── 5. a restore writes one step's partition ──────────────────────────

def test_a_step1_restore_writes_only_step1(app):
    w = _window(app)
    try:
        state = w._display.state
        _in(w, 0)
        state.set_display_visible("CD3", True, origin="step0-test")
        state.set_display_visible("CD8", False, origin="step0-test")
        step0_before = dict(state.display_visibility())

        _in(w, 1)
        w._apply_step1_display_state({
            "preview_mode": "overlay",
            "channel_visibility": {"DAPI": True, "CD3": False, "CD8": True},
            "current_channel": "CD8",
        })
        QtWidgets.QApplication.processEvents()
        assert state.display_visible("CD8") is True
        assert state.display_visible("CD3") is False

        _in(w, 0)
        assert dict(state.display_visibility()) == step0_before
    finally:
        _close(w)
