"""Block 2b: Step3 shares Step1's ticks, weights, fusion draft and navigator.

User ruling, 2026-09-26: a user who looks at the mask in Step3 and builds a
better fusion there must find the same settings back in Step1, ready to run
again. So Step3 IS Step1's display scope, its channel rows are Step1's rows
(the tick is the fusion command, the weight is editable), and its Tissue
Preview composes Step1's live context. What stays put: the committed
snapshot (only an explicit save in Step1 moves it) and Step2 (own ticks,
seeded from the commit).

  * the full public walk: in Step3 tick a channel Step1 had left out and set
    its weight -> Step1 shows the same -> the session is asked to remember
    the draft; the committed snapshot and Step2's ticks are unchanged;
  * Step3's rows are Step1's rows, Step2's keep tick / swatch / name;
  * entering Step3 is a redraw, never a command: no tick, no weight, no
    draft revision and no session save comes out of it;
  * scope coming back to Step1 through Step3 re-syncs Step1's consumers once;
  * the Tissue Preview in Step3 is Step1's context and follows a tick and a
    weight moved there, in the mode Step1 was in.

The old Step3 page is not asserted on (block 2b transition limit, accepted).
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets  # noqa: E402

import test_block01_tissue_preview_contract as tp  # noqa: E402
import test_downstream_display_seed as ds  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    for name in ("information", "critical", "warning"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name,
                            staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.Yes))


def _window(app, tmp_path):
    w = ds._window(app, tmp_path)
    saves = []
    w._schedule_step1_session_save = lambda: saves.append(1)
    return w, saves


def test_a_fusion_built_in_step3_is_step1_s_and_nothing_is_committed(app, tmp_path):
    w, saves = _window(app, tmp_path)
    try:
        fusion = w._display.fusion
        ds._enable(w, "CD3", True, weight=1.0)
        ds._commit(w)
        committed = fusion.committed_snapshot()
        committed_hash = fusion.committed_hash()
        step2_before = ds._ticks(w, 2)
        assert fusion.fusion_enabled("CD8") is False

        ds._in(w, 3)
        saves.clear()
        row = w._channel_dock.row("CD8")
        row.checkbox.setChecked(True)            # the public tick, in Step3
        row.spin.setValue(0.6)                   # the public weight, in Step3
        QtWidgets.QApplication.processEvents()

        assert fusion.fusion_enabled("CD8") is True
        assert fusion.channel_weight("CD8") == pytest.approx(0.6)
        assert saves, "the session was not asked to remember the draft"

        ds._in(w, 1)
        row1 = w._channel_dock.row("CD8")
        assert row1.checkbox.isChecked() and row1.spin.value() == pytest.approx(0.6)
        assert w._display.state.display_visible("CD8") is True
        assert fusion.committed_snapshot() == committed
        assert fusion.committed_hash() == committed_hash
        assert ds._ticks(w, 2) == step2_before and "CD8" not in step2_before
    finally:
        ds._close(w)


def test_step3_rows_are_step1_rows_and_step2_rows_are_not(app, tmp_path):
    w, _saves = _window(app, tmp_path)
    try:
        row = w._channel_dock.row("CD3")
        ds._in(w, 3)
        for widget in (row.slider, row.spin):
            assert not widget.isHidden() and widget.isEnabled()
        ds._in(w, 2)
        for widget in (row.slider, row.spin):
            assert widget.isHidden() and not widget.isEnabled()
    finally:
        ds._close(w)


@pytest.mark.parametrize("start", [0, 1, 2])
def test_entering_step3_is_never_a_command(app, tmp_path, start):
    w, saves = _window(app, tmp_path)
    try:
        ds._enable(w, "CD3", True, weight=0.8)
        ds._commit(w)
        ds._in(w, start)
        fusion = w._display.fusion
        revision = fusion.draft_revision()
        draft = fusion.draft_snapshot()
        with w._display.state.using_scope("step1"):
            ticks = dict(w._display.state.display_visibility())
        saves.clear()

        ds._in(w, 3)

        assert fusion.draft_revision() == revision and fusion.draft_snapshot() == draft
        assert dict(w._display.state.display_visibility()) == ticks
        assert saves == []
    finally:
        ds._close(w)


def test_step1_consumers_catch_up_when_step3_brings_step1_s_scope_back(app, tmp_path,
                                                                        monkeypatch):
    w, _saves = _window(app, tmp_path)
    try:
        calls = []
        real = w._resync_step1_display_from_state
        monkeypatch.setattr(w, "_resync_step1_display_from_state",
                            lambda *a, **k: (calls.append(1), real(*a, **k))[1])
        ds._in(w, 0)
        ds._in(w, 3)
        assert calls == [1], "entering Step3 from Step0 did not re-sync Step1"
        ds._in(w, 1)
        assert calls == [1], "Step3 -> Step1 is the same scope: nothing to replay"
        ds._in(w, 2)
        ds._in(w, 3)
        assert calls == [1, 1]
    finally:
        ds._close(w)


# ── the Tissue Preview ────────────────────────────────────────────────

def test_step3_s_tissue_preview_follows_a_tick_and_a_weight_moved_there(app):
    from block01.ui import block01_display as bd
    w = tp._window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        w._step0._apply_channel_color("CD3", (1.0, 0.0, 0.0))
        w._step0._apply_channel_color("CD8", (0.0, 1.0, 0.0))
        tp._goto(w, 1)
        tp._goto(w, 3)
        co = w._display.coordinator
        assert co.active_context_id() == bd.STEP1
        before = np.array(tp._thumb(w), copy=True)
        green_before = tp._mean_rgb(before)[1]

        row = w._channel_dock.row("CD8")
        row.checkbox.setChecked(True)            # a tick in Step3
        row.spin.setValue(1.0)
        tp._pump(w, 300)
        after_tick = np.array(tp._thumb(w), copy=True)
        assert co.last_published()["owner"] == bd.STEP1
        assert tp._mean_rgb(after_tick)[1] > green_before, "the tick did not reach the preview"

        row.spin.setValue(0.2)                   # a weight in Step3
        tp._pump(w, 300)
        after_weight = np.array(tp._thumb(w), copy=True)
        assert tp._mean_rgb(after_weight)[1] < tp._mean_rgb(after_tick)[1]
    finally:
        w.close()


def test_step3_s_tissue_preview_keeps_step1_s_mode(app):
    from block01.ui import block01_display as bd
    from block01.core import tissue_compose
    w = tp._window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        tp._goto(w, 1)
        w.set_preview_mode("fusion")
        tp._pump(w, 200)
        tp._goto(w, 3)
        tp._pump(w, 300)
        published = w._display.coordinator.last_published()
        assert published["owner"] == bd.STEP1
        assert published["mode"] == tissue_compose.MODE_FUSION
    finally:
        w.close()
