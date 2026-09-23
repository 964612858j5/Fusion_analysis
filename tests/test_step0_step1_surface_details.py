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
    # Third round: Preview Patch moved into the viewer's toolbar.
    assert "Preview Patch" not in boxes
    assert "Per-Channel Decision" in boxes
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
    _show_step(win, 1)
    btn, box = win._btn_step1_intensity, win._step1_channels_box
    assert _on_screen(btn, win)
    assert btn.styleSheet() == s0_btn.styleSheet()
    assert btn.parentWidget() is box
    # Where in the frame: `test_step0_method_leads_the_row_where_step1_has_show_all`.
    assert btn.size() == s0_btn.size()


def test_step0_method_leads_the_row_where_step1_has_show_all(win):
    """`Method ▾` (Step0) and `Show all` (Step1) lead their rows at the same
    place and width; `Intensity…` follows each right away (third round,
    user ruling 2026-09-23: compact), at the same place in both."""
    _show_step(win, 0)
    s0 = win._step0
    s0_box = _geo(s0._channels_box, win)
    method = _geo(s0._method_all, win)
    s0_int = _geo(s0._btn_intensity_window, win)
    spacing = s0._channels_host.itemAt(0).layout().spacing()
    assert s0_int.left() - method.right() - 1 == spacing
    s0_rel = (method.left() - s0_box.left(), method.width(),
              s0_int.left() - s0_box.left(), s0_int.top() - s0_box.top())
    _show_step(win, 1)
    box = _geo(win._step1_channels_box, win)
    show_all = _geo(win._step1_cb_all, win)
    s1_int = _geo(win._btn_step1_intensity, win)
    rel = (show_all.left() - box.left(), show_all.width(),
           s1_int.left() - box.left(), s1_int.top() - box.top())
    assert rel == s0_rel


def test_step1_show_all_follows_step0_method_width(win):
    s0 = win._step0
    _show_step(win, 0)
    s0._method_all.setText("Method: Original ▾")
    _pump()
    width = s0._method_all.width()
    s0_int = _geo(s0._btn_intensity_window, win).left() - _geo(s0._channels_box, win).left()
    _show_step(win, 1)
    assert win._step1_cb_all.width() == width
    s1_int = (_geo(win._btn_step1_intensity, win).left()
              - _geo(win._step1_channels_box, win).left())
    assert s1_int == s0_int


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


# ── third round ───────────────────────────────────────────────────────────

def test_step1_frame_is_called_channels(win):
    assert win._step1_channels_box.title() == win._step0._channels_box.title() == "Channels"


def test_step1_has_no_fusion_or_roi_line_under_the_panel(win):
    _show_step(win, 1)
    page = win._step1_page_widget
    shown = [lbl.text() for lbl in page.findChildren(QtWidgets.QLabel)
             if _on_screen(lbl, page)]
    for text in shown:
        assert "fusion changes" not in text and "Fusion settings saved" not in text, text
        assert "patches=" not in text and "No ROI loaded" not in text, text
    # The Channels frame reaches down to the bottom bar: the gap to Save
    # Fusion Settings is exactly Step0's gap from its Channels frame to the
    # Per-Channel Decision border (fifth round).
    from block01.ui.main_window import groupbox_frame_rect
    box = _geo(win._step1_channels_box, win)
    save = _geo(win._btn_save_fusion_settings, win)
    _show_step(win, 0)
    s0_box = _geo(win._step0._channels_box, win)
    decision = win._step0._decision_box
    border_top = decision.mapTo(win, QtCore.QPoint(
        0, groupbox_frame_rect(decision).y())).y()
    assert save.top() - box.bottom() == border_top - s0_box.bottom()


def test_step0_has_no_correction_status_line(win):
    _show_step(win, 0)
    page = win._step0
    page._bg_corrected_status.setText(
        "No background correction applied (no channels assigned). "
        "Use Intensity for display mapping, or continue to Step1.")
    shown = [lbl.text() for lbl in page.findChildren(QtWidgets.QLabel)
             if _on_screen(lbl, page)]
    assert not any("No background correction applied" in t for t in shown)
    assert not _on_screen(page._bg_corrected_status, page)


def test_step0_patch_selector_is_step1s_on_the_viewer_toolbar(win):
    from block01.ui import main_window as mw
    from block01.ui.step0 import step0_page as sp

    assert (sp.PATCH_INLINE_BUTTONS, sp.PATCH_BTN_W, sp.PATCH_BTN_H,
            sp.PATCH_BTN_SPACING) == (
        mw.STEP1_INLINE_PATCH_BUTTONS, mw.STEP1_PATCH_BTN_W,
        mw.STEP1_PATCH_BTN_H, mw.STEP1_PATCH_BTN_SPACING)
    _show_step(win, 0)
    page = win._step0
    assert page._patch_menu_btn.styleSheet() == win._patch_menu_btn.styleSheet()
    page.patches = [(0, 64, 0, 64)] * 9
    page._rebuild_patch_buttons()
    _pump()
    inline = [page._patch_buttons_row.itemAt(i).widget()
              for i in range(page._patch_buttons_row.count())]
    assert [b.text() for b in inline] == [f"P{i}" for i in range(1, 8)]
    assert all(b.size() == QtCore.QSize(sp.PATCH_BTN_W, sp.PATCH_BTN_H) for b in inline)
    assert [a.text() for a in page._patch_menu_actions] == [f"P{i}" for i in range(1, 10)]
    # Left of the Original / TopHat / cuCIM switch, on its line.
    menu = _geo(page._patch_menu_btn, win)
    original = _geo(page._full_method_buttons["original"], win)
    assert _geo(inline[-1], win).right() < original.left()
    assert abs(menu.center().y() - original.center().y()) <= 2
    assert _on_screen(page._patch_menu_btn, page)
    assert not _on_screen(page._patch_box, page)


def test_step0_decision_sits_left_of_save_across_one_row(win):
    _show_step(win, 0)
    page = win._step0
    dec = _geo(page._decision_box, win)
    save = _geo(page._btn_continue, win)
    assert _on_screen(page._decision_box, page)
    assert isinstance(page._decision_box.layout(), QtWidgets.QHBoxLayout)
    assert dec.right() < save.left()
    assert dec.top() <= save.center().y() <= dec.bottom()
    # Every control of the panel on one line.
    ys = {(_geo(w_, win).center().y()) for w_ in (
        page._dec_radius, page._dec_sigma, page._dec_top, page._dec_cu,
        page._dec_orig, page._apply_btn)}
    assert max(ys) - min(ys) <= 3, ys


def test_step0_viewer_takes_the_column_down_to_the_bottom_row(win):
    _show_step(win, 0)
    page = win._step0
    view = _geo(page._view_area, win)
    dec = _geo(page._decision_box, win)
    # Nothing between the picture and the bottom row but layout spacing.
    assert 0 <= dec.top() - view.bottom() <= 12, (view.bottom(), dec.top())


# ── fourth round ──────────────────────────────────────────────────────────

_MANY = ["DAPI"] + [f"CH{i:02d}" for i in range(39)]


@pytest.fixture
def many(app):
    """A window with enough channels for the list to scroll."""
    import test_step1_channel_panel as panel_tests
    from block01.ui.main_window import MainWindow

    loader = panel_tests._Loader()
    loader._names = list(_MANY)
    loader.ch_map = {c: i for i, c in enumerate(_MANY)}
    w = MainWindow()
    w.loader = loader
    w.config.set_channels(list(_MANY))
    w.config.load_panel({"markers": {c: 0.5 for c in _MANY[1:]}}, "DAPI")
    w.config.set_nucleus("DAPI", 1.0)
    w.resize(1500, 950)
    w.show()
    _pump()
    yield w
    w.close()
    _pump()


def _fully_shown(widget, panel):
    """Every pixel of `widget` lies inside what `panel` really shows."""
    shown = panel.visibleRegion().boundingRect()
    rect = QtCore.QRect(widget.mapTo(panel, QtCore.QPoint(0, 0)), widget.size())
    return shown.contains(rect)


def test_step1_channel_column_is_never_covered(many):
    w = many
    _show_step(w, 1)
    w._step1_main_split.setSizes([40, 1440])        # drag the handle far left
    _pump(10)
    panel = w._step1_left_panel
    lst = w._channel_dock.list_widget
    assert lst.verticalScrollBar().isVisible()
    for widget in (w._step1_channels_box, lst.verticalScrollBar()):
        assert _fully_shown(widget, panel), widget
    # Save Fusion Settings lives in the page's bottom bar since the fifth
    # round; it must be whole there, whatever the handle does.
    assert _fully_shown(w._btn_save_fusion_settings, w._step1_page_widget)
    for cid in ("DAPI", "CH00", "CH38"):
        row = w._channel_dock.row(cid)
        spin = QtCore.QRect(row.spin.mapTo(lst.viewport(), QtCore.QPoint(0, 0)),
                            row.spin.size())
        assert spin.right() < lst.viewport().width(), cid


def test_step1_scroll_bar_is_step0s(many):
    w = many
    _show_step(w, 0)
    bar = w._channel_dock.list_widget.verticalScrollBar()
    s0 = (bar.style().metaObject().className(), bar.width(), bar.isVisible())
    s0_shot = bar.grab().toImage()
    _show_step(w, 1)
    s1 = (bar.style().metaObject().className(), bar.width(), bar.isVisible())
    assert s1 == s0
    # Same pixels too, where the two bars have the same length.
    s1_shot = bar.grab().toImage()
    h = min(s0_shot.height(), s1_shot.height())
    assert s0_shot.copy(0, 0, s0_shot.width(), 16) == s1_shot.copy(0, 0, s1_shot.width(), 16)
    assert h > 16


def test_save_fusion_settings_has_no_icon(win):
    assert win._btn_save_fusion_settings.text() == "Save Fusion Settings"


def test_step1_weight_numbers_align_by_editability(win):
    _show_step(win, 1)
    nucleus = win._channel_dock.row("DAPI")
    marker = win._channel_dock.row("CD3")
    assert nucleus.spin.isReadOnly()
    assert nucleus.spin.alignment() & QtCore.Qt.AlignHCenter
    assert marker.spin.alignment() & QtCore.Qt.AlignRight
    # Outside Step1 nothing moved: Step0 rows keep the template's stretch.
    _show_step(win, 0)
    lay = marker.layout()
    assert lay.stretch(lay.count() - 1) == 1


def test_step1_weight_box_sits_at_the_row_edge(win):
    _show_step(win, 1)
    row = win._channel_dock.row("CD3")
    margin = row.layout().contentsMargins().right()
    assert row.width() - (row.spin.x() + row.spin.width()) == margin
    assert row.slider.width() > 63             # it got the band back


def test_step0_decision_groups_are_spaced_and_the_frame_hugs_the_status(win):
    from block01.ui.step0 import step0_page as sp

    _show_step(win, 0)
    page = win._step0
    sigma = _geo(page._dec_sigma, win)
    tophat = _geo(page._dec_top, win)
    original = _geo(page._dec_orig, win)
    apply_ = _geo(page._apply_btn, win)
    status = _geo(page._decision_status, win)
    for left, right in ((sigma, tophat), (original, apply_), (apply_, status)):
        assert right.left() - left.right() - 1 >= sp.DECISION_GROUP_GAP, (left, right)
    box = _geo(page._decision_box, win)
    assert 0 <= box.right() - status.right() <= 10
    assert not _on_screen(page._remap_state_lbl, page)


def test_step0_decision_status_keeps_the_longest_width(win):
    _show_step(win, 0)
    page = win._step0
    page._channel_order = ["DAPI", "CD3", "A-VERY-LONG-CHANNEL-NAME"]
    page._fit_decision_status_width()
    _pump()
    label = page._decision_status
    metrics = label.fontMetrics()
    longest = metrics.horizontalAdvance(
        "Saved: A-VERY-LONG-CHANNEL-NAME tophat  (r=150, σ=200)")
    # ...plus one Background Correction tab (fifth round).
    tab = page._step0_tabs.tabBar().tabRect(0).width()
    assert tab > 0
    assert label.width() >= longest + tab
    box_before = _geo(page._decision_box, win)
    label.setText("Saved: CD3 cucim  (r=5, σ=5)")
    _pump()
    assert _geo(page._decision_box, win) == box_before
    # A message longer than the line is elided on screen, whole in the text.
    long_msg = "TopHat parameter saved. " * 6
    label.setText(long_msg)
    _pump()
    assert label.text() == long_msg and label.toolTip() == long_msg
    assert _geo(page._decision_box, win) == box_before


# ── fifth round ───────────────────────────────────────────────────────────

def test_step0_status_line_has_one_tab_of_room_for_every_message(win):
    _show_step(win, 0)
    page = win._step0
    page._channel_order = ["DAPI", "HLA-DR", "FOXP3", "PD1"]
    page._fit_decision_status_width()
    _pump()
    label = page._decision_status
    for msg in ("Saved: HLA-DR tophat  (r=35, σ=50)",
                "HLA-DR: set radius/sigma, pick a method, press Enter."):
        label.setText(msg)
        _pump()
        assert QtWidgets.QLabel.text(label) == msg     # drawn whole, not elided


def test_step1_has_no_back_button_and_save_fusion_takes_its_place(win):
    from block01.ui.main_window import groupbox_frame_rect

    _show_step(win, 0)
    decision = win._step0._decision_box
    frame = groupbox_frame_rect(decision)
    frame_top = decision.mapTo(win, QtCore.QPoint(0, frame.y())).y()
    _show_step(win, 1)
    assert not _on_screen(win._btn_back_to_step0, win)
    save = win._btn_save_fusion_settings
    assert _on_screen(save, win)
    assert save.parentWidget() is not win._step1_left_panel
    # As tall as the decision frame's BORDER -- not the title above it --
    # and level with it.
    assert frame.height() < decision.height()
    assert save.height() == frame.height()
    assert _geo(save, win).top() == frame_top
    # As wide as the Channels frame and under it, following the handle.
    box = win._step1_channels_box
    assert (_geo(save, win).left(), save.width()) == (_geo(box, win).left(), box.width())
    win._step1_main_split.setSizes([430, 1050])
    _pump(8)
    assert (_geo(save, win).left(), save.width()) == (_geo(box, win).left(), box.width())
    assert box.width() > 400


def test_both_channels_frames_are_the_same_height_on_the_same_line(win):
    _show_step(win, 0)
    s0 = _geo(win._step0._channels_box, win)
    _show_step(win, 1)
    s1 = _geo(win._step1_channels_box, win)
    assert (s1.top(), s1.height()) == (s0.top(), s0.height())
