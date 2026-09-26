"""Block 2c-1: the new Step3 page -- a simplified Step1.

User ruling, 2026-09-26: Step1's layout and look, the channel panel and the
viewer only; Step3 shares Step1's ticks, weights, draft and mode (block 2b).
Until block 2c-2 the viewer slot holds a notice (functional gap, accepted).

  * the page: Step1's `Channels` frame with `Show all` / `Intensity…`,
    `Reset weights` / `Load weights` and the one dock (Step1's rows) in a
    host of its own; the Overlay / Fusion pair over the viewer notice; the
    controls take Step1's style sources;
  * `Show all` is Step1's sweep: markers shown and fused, the nucleus left
    alone; `Reset weights` zeroes the markers; `Load weights` reads the chosen
    session without writing it (a historical file, and the current session
    file) -- the committed snapshot never moves;
  * the Overlay / Fusion pairs of the two pages are one mode, and the Tissue
    Preview follows a switch made in Step3;
  * one channel-column share over four pages;
  * entering: ready -> opens, no session save; a bound unread handoff is read
    first; not ready -> stays where it was, the reason in a dialog on screen.
"""

import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets  # noqa: E402

import test_block01_tissue_preview_contract as tp  # noqa: E402
import test_downstream_display_seed as ds  # noqa: E402
import test_fusion_domain_model as fdm  # noqa: E402
import test_step1_load_weights as lw  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    for name in ("information", "critical", "warning"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name,
                            staticmethod(lambda *a, **k: None))


def _window(app, tmp_path):
    w = ds._window(app, tmp_path)
    saves = []
    w._schedule_step1_session_save = lambda: saves.append(1)
    return w, saves


# ── the page ──────────────────────────────────────────────────────────

def test_the_page_is_step1_s_channels_frame_and_a_viewer_slot(app, tmp_path):
    from block01.ui.step1_button_styles import MODE_BUTTON_QSS
    from block01.ui.step3_page import VIEWER_PENDING_TEXT
    from block01.ui.widgets.channel_dock import template as channel_template
    w, _saves = _window(app, tmp_path)
    try:
        page = w._step3
        boxes = [b for b in page.findChildren(QtWidgets.QGroupBox) if b.title() == "Channels"]
        assert len(boxes) == 1 and boxes[0].styleSheet() == channel_template.frame_qss()
        texts = {b.text() for b in page.findChildren(QtWidgets.QPushButton)}
        assert {"Intensity…", "Reset weights", "Load weights", "Overlay", "Fusion",
                "Tissue Navigator", "← Back to Step 2"} <= texts
        assert w._step3_cb_all.text() == "Show all"
        # Step1's style sources
        assert w._step3_cb_all.styleSheet() == w._step0._cb_all.styleSheet()
        assert w._btn_step3_mode_fusion.styleSheet() == MODE_BUTTON_QSS
        assert page.viewer_notice().text() == VIEWER_PENDING_TEXT
        # nothing of the old page
        assert not page.findChildren(QtWidgets.QTabWidget, "") or all(
            t.tabText(i) in ("Fusion", "Viewer")
            for t in page.findChildren(QtWidgets.QTabWidget) for i in range(t.count()))
        # its own host, and the one dock with Step1's rows mounted there
        assert w._channels_host_for(3) is not w._channels_host_for(1)
        ds._in(w, 3)
        dock = w._channel_dock
        assert dock.parentWidget() is page.channels_host().parentWidget()
        row = dock.row("CD3")
        assert not row.spin.isHidden() and row.spin.isEnabled()
    finally:
        ds._close(w)


def test_show_all_is_step1_s_sweep(app, tmp_path):
    w, _saves = _window(app, tmp_path)
    try:
        fusion, state = w._display.fusion, w._display.state
        ds._in(w, 3)
        nucleus_before = state.display_visible("DAPI")
        w._step3_cb_all.setChecked(True)
        QtWidgets.QApplication.processEvents()
        for ch in ("CD3", "CD8", "CD20"):
            assert state.display_visible(ch) is True and fusion.fusion_enabled(ch) is True, ch
        assert state.display_visible("DAPI") == nucleus_before
        w._step3_cb_all.setChecked(False)
        QtWidgets.QApplication.processEvents()
        for ch in ("CD3", "CD8", "CD20"):
            assert state.display_visible(ch) is False and fusion.fusion_enabled(ch) is False, ch
        assert state.display_visible("DAPI") == nucleus_before
    finally:
        ds._close(w)


def test_reset_weights_zeroes_the_markers_and_leaves_the_commit(app, tmp_path):
    w, _saves = _window(app, tmp_path)
    try:
        fusion = w._display.fusion
        ds._enable(w, "CD3", True, weight=0.8)
        ds._commit(w)
        committed = fusion.committed_snapshot()
        ds._in(w, 3)
        reset = [b for b in w._step3.findChildren(QtWidgets.QPushButton)
                 if b.text() == "Reset weights"][0]
        reset.click()
        QtWidgets.QApplication.processEvents()
        assert fusion.channel_weight("CD3") == pytest.approx(0.0)
        ds._in(w, 1)
        assert w._channel_dock.row("CD3").spin.value() == pytest.approx(0.0)
        assert fusion.committed_snapshot() == committed
    finally:
        ds._close(w)


@pytest.mark.parametrize("which", ["historical", "current"])
def test_load_weights_reads_the_chosen_session(app, tmp_path, monkeypatch, which):
    src = fdm._window(app)
    try:
        path = lw._session_from(src, tmp_path, enabled=("CD8",), weights=(("CD8", 0.6),))
    finally:
        src.close()
    before = open(path, "rb").read()
    w = fdm._window(app)
    try:
        saved = []
        w._schedule_step1_session_save = lambda: saved.append(1)
        if which == "current":
            # the chosen file IS the current session's file
            monkeypatch.setattr(w, "_step1_session_path", lambda: path)
        committed = w._display.fusion.committed_snapshot()
        w._set_step_active(3)
        asked = []
        monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName",
                            staticmethod(lambda parent, *a, **k: (asked.append(parent), (path, ""))[1]))
        load = [b for b in w._step3.findChildren(QtWidgets.QPushButton)
                if b.text() == "Load weights"][0]
        load.click()
        fdm._pump()
        assert asked == [w._step3], "the dialog was not asked from Step3"
        assert w._display.fusion.fusion_enabled("CD8") is True
        assert w._display.fusion.channel_weight("CD8") == pytest.approx(0.6)
        assert open(path, "rb").read() == before, "loading wrote the chosen file"
        assert w._display.fusion.committed_snapshot() == committed
        # the current session is saved as any edit is (the save itself is
        # stubbed here; for the current file that save is what rewrites it)
        assert saved
        w._set_step_active(1)
        assert w._channel_dock.row("CD8").spin.value() == pytest.approx(0.6)
    finally:
        w.close()


# ── one mode ──────────────────────────────────────────────────────────

def test_the_two_pairs_are_one_mode_and_the_preview_follows_step3(app):
    from block01.core import tissue_compose
    from block01.ui import block01_display as bd
    w = tp._window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w.config._rows["CD3"].spin.setValue(1.0)
        tp._goto(w, 1)
        tp._goto(w, 3)
        w._btn_step3_mode_fusion.click()
        tp._pump(w, 300)
        assert w._step1_preview_mode == "fusion"
        assert w._btn_mode_fusion.isChecked() and not w._btn_mode_overlay.isChecked()
        assert w._btn_step3_mode_fusion.isChecked() and not w._btn_step3_mode_overlay.isChecked()
        published = w._display.coordinator.last_published()
        assert published["owner"] == bd.STEP1 and published["mode"] == tissue_compose.MODE_FUSION
        # clicking the checked button keeps it checked (it is re-set every time)
        w._btn_step3_mode_fusion.click()
        assert w._btn_step3_mode_fusion.isChecked()
        tp._goto(w, 1)
        w._btn_mode_overlay.click()
        tp._pump(w, 100)
        assert w._btn_step3_mode_overlay.isChecked() and not w._btn_step3_mode_fusion.isChecked()
    finally:
        w.close()


# ── one column share ──────────────────────────────────────────────────

def test_four_pages_share_one_channel_column(app):
    from block01.ui.main_window import MainWindow
    w = MainWindow()
    w.resize(1920, 1080)
    w.show()
    try:
        split3 = w._step3.channel_column_splitter()
        assert split3 in w._channel_column_splitters()

        def go(step, page):
            w._set_step_active(step)
            w._stack.setCurrentWidget(page)
            for _ in range(5):
                QtWidgets.QApplication.processEvents()

        go(3, w._step3)
        split3.setSizes([320, split3.width() - 320 - split3.handleWidth()])
        w._on_channel_column_dragged(split3)
        go(0, w._step0)
        s0 = w._step0._bg_c_split.sizes()[0]
        go(1, w._stack.widget(1))
        s1 = w._step1_main_split.sizes()[0]
        go(2, w._step2)
        s2 = w._step2.channel_column_splitter().sizes()[0]
        go(3, w._step3)
        assert all(abs(v - 320) <= 2 for v in (s0, s1, s2, split3.sizes()[0])), (s0, s1, s2)
    finally:
        w.hide()
        w.close()


# ── entering ──────────────────────────────────────────────────────────

def test_a_ready_context_opens_step3_without_a_save(app, tmp_path):
    w, saves = _window(app, tmp_path)
    try:
        w._step1_context_ready = True
        ds._in(w, 1)
        saves.clear()
        w._go_to_step3()
        QtWidgets.QApplication.processEvents()
        assert w._stack.currentIndex() == 3 and w._current_step == 3
        assert saves == []
    finally:
        ds._close(w)


def test_a_bound_unread_handoff_is_read_before_step3_opens(app, tmp_path, monkeypatch):
    w, _saves = _window(app, tmp_path)
    try:
        w._step1_context_ready = False
        w.step0_output.update({"step0_dir": str(tmp_path), "step1_dir": str(tmp_path)})
        reads = []
        monkeypatch.setattr(w, "_load_step0_roi_result",
                            lambda auto=False: (reads.append(auto), True)[1])
        w._go_to_step3()
        assert reads == [True] and w._stack.currentIndex() == 3
        assert w._step1_context_ready is True
    finally:
        ds._close(w)


def test_not_ready_stays_put_and_says_why_on_screen(app, tmp_path):
    w, _saves = _window(app, tmp_path)
    try:
        w._step1_context_ready = False
        w.step0_output = {}
        ds._in(w, 2)
        w._stack.setCurrentIndex(2)
        w._go_to_step3()
        QtWidgets.QApplication.processEvents()
        assert w._stack.currentIndex() == 2 and w._current_step == 2
        box = w._step3_refusal_box
        assert box.isVisible() and not box.isModal()
        assert "Step3 is not ready" in box.text()
        w._go_to_step3()                               # a second refusal reuses the box
        assert w._step3_refusal_box is box
    finally:
        ds._close(w)
