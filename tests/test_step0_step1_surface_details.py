"""Step0 / Step1 surface details (user ruling, 2026-09-23).

1. Step0: no `Ready.` status line under the channel list -- no run status
   line on screen at all -- and the list moves up into its place.
2. Step0: no `Quantitative Metrics` panel.
4. Step1: `Intensity…` looks exactly like Step0's and sits where Step0's
   does inside the same Channels frame; no `Nucleus: DAPI (weight 1.00)`.
5. Step1: Step0's `Show all` tick, same place and look, meaning what ticking
   each row means; Step0 no longer shows its own (the control is kept).
6. Step1: the session/handoff buttons sit on the Patch row; the two status
   rows under it are gone from the screen, their words go to the terminal,
   and their height goes to the viewer.

Each assertion is written against what the user sees -- visibility,
geometry, style -- plus, for `Show all`, the scientific state it leaves.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtCore, QtWidgets  # noqa: E402

from test_step1_channel_panel import _window, app  # noqa: E402,F401


def _pump(n=5):
    for _ in range(n):
        QtWidgets.QApplication.processEvents()


def _show_step(w, step):
    w._stack.setCurrentIndex(step)
    w._set_step_active(step)
    _pump()


@pytest.fixture
def win(app):
    w = _window(app)
    w.resize(1500, 950)
    w.show()
    _pump()
    yield w
    w.close()
    _pump()


def _on_screen(widget, root):
    return widget.isVisibleTo(root) and widget.isVisible()


def _geo(widget, root):
    top_left = widget.mapTo(root, QtCore.QPoint(0, 0))
    return QtCore.QRect(top_left, widget.size())


# ── 1 / 2 / 5: Step0 ──────────────────────────────────────────────────────

def test_step0_shows_no_status_line_and_no_metrics_panel(win):
    _show_step(win, 0)
    page = win._step0
    assert page.isVisible()
    shown = [lbl.text() for lbl in page.findChildren(QtWidgets.QLabel)
             if _on_screen(lbl, page)]
    assert "Ready." not in shown
    assert not _on_screen(page._proc_status, page)
    boxes = {b.title() for b in page.findChildren(QtWidgets.QGroupBox)
             if _on_screen(b, page)}
    assert "Quantitative Metrics" not in boxes
    assert {"Preview Patch", "Per-Channel Decision"} <= boxes
    # A run still writes its words somewhere nothing breaks: the objects live.
    page._proc_status.setText("running")
    page._metrics_original.setText("Original  → SNR: 1")
    assert not _on_screen(page._proc_status, page)


def test_step0_list_moves_up_into_the_status_line(win):
    _show_step(win, 0)
    page = win._step0
    box = page._channels_box
    dock = win._channel_dock
    assert dock.parentWidget() is not None and _on_screen(dock, page)
    # Nothing on screen between the header rule and the bottom of the frame
    # except the dock (and the cuCIM warning, only where cuCIM is missing).
    others = [wd for wd in box.findChildren(QtWidgets.QLabel,
                                            options=QtCore.Qt.FindDirectChildrenOnly)
              if _on_screen(wd, page) and wd is not page._cucim_warn]
    assert others == []


def test_step0_show_all_is_kept_but_not_shown(win):
    _show_step(win, 0)
    page = win._step0
    assert not _on_screen(page._cb_all, page)
    # Kept, only withdrawn from the screen: still wired to its handler.
    assert page._cb_all.receivers(page._cb_all.stateChanged) >= 1


# ── 4: Step1 Intensity and the nucleus line ───────────────────────────────

def test_step1_intensity_matches_step0_in_look_and_place(win):
    _show_step(win, 0)
    s0 = win._step0
    s0_btn, s0_box = s0._btn_intensity_window, s0._channels_box
    s0_rel = (_geo(s0_box, win).right() - _geo(s0_btn, win).right(),
              _geo(s0_btn, win).top() - _geo(s0_box, win).top(),
              s0_btn.size())
    _show_step(win, 1)
    btn, box = win._btn_step1_intensity, win._step1_channels_box
    assert _on_screen(btn, win)
    assert btn.styleSheet() == s0_btn.styleSheet()
    assert btn.parentWidget() is box
    rel = (_geo(box, win).right() - _geo(btn, win).right(),
           _geo(btn, win).top() - _geo(box, win).top(), btn.size())
    assert rel == s0_rel


def test_step0_method_leads_the_row_where_step1_has_show_all(win):
    """Second round (user ruling, 2026-09-23): Step0's `Method ▾` moves to
    the left end of the header row, into the room the withdrawn `Show all`
    left, so it lines up with Step1's `Show all`; both Intensity buttons
    close their rows at the right edge."""
    _show_step(win, 0)
    s0 = win._step0
    s0_box = _geo(s0._channels_box, win)
    method = _geo(s0._method_all, win)
    s0_int = _geo(s0._btn_intensity_window, win)
    assert method.right() < s0_int.left()
    s0_rel = (method.left() - s0_box.left(), method.top() - s0_box.top(),
              s0_box.right() - s0_int.right(), s0_int.top() - s0_box.top())
    _show_step(win, 1)
    box = _geo(win._step1_channels_box, win)
    show_all = _geo(win._step1_cb_all, win)
    s1_int = _geo(win._btn_step1_intensity, win)
    rel = (show_all.left() - box.left(), show_all.top() - box.top(),
           box.right() - s1_int.right(), s1_int.top() - box.top())
    assert rel == s0_rel


def test_step1_has_no_nucleus_line(win):
    _show_step(win, 1)
    page = win._step1_page_widget
    shown = [lbl.text() for lbl in page.findChildren(QtWidgets.QLabel)
             if _on_screen(lbl, page)]
    assert not any(t.startswith("Nucleus:") or "(weight" in t for t in shown), shown
    # Still written, just not shown.
    assert "DAPI" in win.config._nuc_value.text()


# ── 5: Step1 Show all ─────────────────────────────────────────────────────

def test_step1_show_all_sits_where_step0_had_it(win):
    _show_step(win, 1)
    cb = win._step1_cb_all
    assert _on_screen(cb, win)
    assert cb.text() == "Show all"
    assert cb.styleSheet() == win._step0._cb_all.styleSheet()
    box = _geo(win._step1_channels_box, win)
    # Left end of the header row, level with Intensity.
    assert _geo(cb, win).left() < _geo(win._btn_step1_intensity, win).left()
    assert abs(_geo(cb, win).center().y()
               - _geo(win._btn_step1_intensity, win).center().y()) <= 1
    assert _geo(cb, win).top() - box.top() < 40


def _science(w):
    fusion, state = w._display.fusion, w._display.state
    return {ch: (state.display_visible(ch), fusion.fusion_enabled(ch),
                 fusion.channel_weight(ch))
            for ch in ("CD3", "CD8")}, fusion.nucleus()


def test_step1_show_all_means_ticking_every_row(app):
    by_rows = _window(app)
    by_sweep = _window(app)
    try:
        for w in (by_rows, by_sweep):
            w.resize(1500, 950)
            w.show()
            _show_step(w, 1)
        for ch in ("CD3", "CD8"):
            by_rows._channel_dock.row(ch).checkbox.setChecked(True)
        by_sweep._step1_cb_all.setChecked(True)
        _pump()
        ticked, nucleus = _science(by_sweep)
        assert ticked == _science(by_rows)[0]
        assert all(v[0] and v[1] for v in ticked.values()), ticked
        assert nucleus == ("DAPI", 1.0)

        for ch in ("CD3", "CD8"):
            by_rows._channel_dock.row(ch).checkbox.setChecked(False)
        by_sweep._step1_cb_all.setChecked(False)
        _pump()
        unticked, nucleus = _science(by_sweep)
        assert unticked == _science(by_rows)[0]
        assert not any(v[0] for v in unticked.values()), unticked
        assert nucleus == ("DAPI", 1.0)
    finally:
        by_rows.close()
        by_sweep.close()
        _pump()


# ── 6: Step1 Patch row, status rows and the viewer ────────────────────────

def _step1_buttons(w):
    return {b.text(): b for b in w.viewer_tab.findChildren(QtWidgets.QPushButton)
            if b.text() in ("Load Step0 ROI Result", "Load Previous Step1 Session",
                            "Save Session", "⟳ Update")}


def test_step1_session_buttons_share_the_patch_row(win):
    _show_step(win, 1)
    buttons = _step1_buttons(win)
    assert len(buttons) == 4
    patch = _geo(win._patch_menu_btn, win)
    for name, b in buttons.items():
        assert _on_screen(b, win), name
        assert abs(_geo(b, win).center().y() - patch.center().y()) <= 1, name
        assert _geo(b, win).left() > patch.right(), name


def test_step1_status_rows_are_off_screen_and_go_to_the_terminal(win, capsys):
    _show_step(win, 1)
    for label in (win.prev_status, win.patch_cache_status):
        assert not _on_screen(label, win)
    capsys.readouterr()
    win.prev_status.setText("⚠ P3 is outside active ROI")
    win.prev_status.setText("⚠ P3 is outside active ROI")      # printed once
    win.patch_cache_status.setText("P2 load error: boom")
    win.patch_cache_status.setText(" ")                          # blank: silent
    out = capsys.readouterr().out
    assert out.count("⚠ P3 is outside active ROI") == 1
    assert "P2 load error: boom" in out
    # Readers of the text are unchanged.
    assert win.prev_status.text() == "⚠ P3 is outside active ROI"


def test_step1_viewer_starts_right_under_the_patch_row(win):
    _show_step(win, 1)
    assert _on_screen(win.prev_gv, win)
    # From the Patch selector itself: before this change the session buttons
    # sat on the status row below it, so measuring from them hid two rows.
    patch_row_bottom = _geo(win._patch_menu_btn, win).bottom()
    viewer_top = _geo(win.prev_gv, win).top()
    # Only the layout's spacing between them (plus the row's own centring
    # slack): no status row in between.
    assert viewer_top - patch_row_bottom <= 12, (viewer_top, patch_row_bottom)


# ── second round: navigator, title bar and tabs, mode buttons ─────────────

def test_both_steps_call_it_tissue_navigator_in_one_look(win):
    s0_btn = win._step0._btn_tissue_nav
    s1_btn = win._btn_step1_tissue_nav
    assert s0_btn.text() == s1_btn.text() == "Tissue Navigator"
    assert s1_btn.styleSheet() == s0_btn.styleSheet()
    _show_step(win, 0)
    s0_geo = _geo(s0_btn, win)
    _show_step(win, 1)
    assert _geo(s1_btn, win) == s0_geo


def test_the_tabs_start_on_the_same_line_in_both_steps(win):
    _show_step(win, 0)
    s0 = win._step0
    bar0 = _geo(s0._file_bar, win)
    tabs0 = _geo(s0._step0_tabs.tabBar(), win)
    _show_step(win, 1)
    bar1 = _geo(win._step1_title_bar, win)
    assert (bar1.top(), bar1.height()) == (bar0.top(), bar0.height())
    for tabs in (win._step1_left_tabs, win._step1_right_tabs):
        tab_bar = _geo(tabs.tabBar(), win)
        assert (tab_bar.top(), tab_bar.height()) == (tabs0.top(), tabs0.height())


def test_the_mode_buttons_sit_on_the_patch_row(win):
    _show_step(win, 1)
    patch = _geo(win._patch_menu_btn, win)
    buttons = _step1_buttons(win)
    first_session = min(_geo(b, win).left() for b in buttons.values())
    for btn in (win._btn_mode_overlay, win._btn_mode_fusion):
        g = _geo(btn, win)
        assert _on_screen(btn, win)
        assert abs(g.center().y() - patch.center().y()) <= 1
        assert patch.right() < g.left() < first_session
