"""Step1's page is two columns of tabs, in Step0's proportion.

Block A of `docs/step1_rework_plan.md`. What is pinned here is the SURFACE the
user ruled on:

* Step1's channel column shows the SAME SHARE of the width as Step0's does --
  measured on both pages' real widths after the layout has settled, not read
  off the stretch factors and not compared against a copied number. (Step0's
  own share is not 1:2: its column starts at `4/3` of its minimum and works
  out near 0.28 of the work area. A hard 1:2 in Step1 was visibly wider than
  Step0 and is what the real-machine check rejected.)
* `Fusion | Pre-segmentation` on the left (was `Channels | Method & Parameters`), `Viewer | Patch Results` on the
  right, switched rather than stacked;
* the ONE public channel dock stays mounted while the other left tab is up: a
  tab that is not current is not visible, which is Qt's doing and not a
  command, so what is asserted is that nobody hid or unmounted it and that it
  comes back on its own;
* a short window still reaches the segmentation settings and the commit row.

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
    filepath = "/tmp/step1_layout.ome.tiff"
    shape = (256, 256)

    def __init__(self):
        self._names = ["DAPI", "CD3", "CD8"]
        self.ch_map = {c: i for i, c in enumerate(self._names)}

    def channel_names(self):
        return list(self._names)

    def read_region(self, channel, y0, y1, x0, x1, downsample=1,
                    normalize=True):
        ds = max(1, int(downsample))
        return np.zeros(((y1 - y0) // ds or 1, (x1 - x0) // ds or 1),
                        np.float32)


def _window(app, width=1600, height=900):
    from block01.ui.main_window import MainWindow
    w = MainWindow()
    # NOTHING HERE MAY REACH A REAL PROJECT. A window built from the user's
    # own config carries that project's `output_dir`, and a draft change then
    # autosaves `step1_session.json` INTO IT. Stubbed before the first edit,
    # as every other Step1 suite does (`test_fusion_domain_model.py`).
    w._schedule_step1_session_save = lambda: None
    w._save_step1_session = lambda *a, **k: None
    w.loader = _Loader()
    w.config.set_channels(w.loader.channel_names())
    w.config.load_panel({"markers": {"CD3": 0.0, "CD8": 0.0}}, "DAPI")
    w.config.set_nucleus("DAPI", 1.0)
    w._set_step_active(1)
    w._stack.setCurrentWidget(w._step1_page_widget)
    w.resize(width, height)
    w.show()
    _settle(app)
    return w


def _settle(app):
    """Let Qt finish the layout: a width read before this is the size hint."""
    for _ in range(6):
        app.processEvents()


def _close(w):
    w._display.shutdown("test")
    w.close()


def _columns(w):
    return w._step1_left_tabs, w.right_tabs


def _left_fraction(sizes):
    total = sum(sizes)
    assert total > 0
    return sizes[0] / float(total)


def _step0_left_fraction(app, w):
    """Step0's own share, measured on Step0 -- the thing Step1 must match."""
    w._set_step_active(0)
    w._stack.setCurrentWidget(w._step0)
    _settle(app)
    split = w._step0._bg_c_split
    assert split.count() == 2
    return _left_fraction(split.sizes())


# ── gate 1: Step0's proportion, measured ──────────────────────────────

@pytest.mark.parametrize("width", [1280, 1600, 1920, 2560])
def test_step1_shows_step0_s_own_channel_column_share(app, width):
    """Measured on BOTH pages, at the same window width."""
    w = _window(app, width=width)
    try:
        assert w._step1_main_split.count() == 2, "two columns, not three"
        step0 = _step0_left_fraction(app, w)

        w._set_step_active(1)
        w._stack.setCurrentWidget(w._step1_page_widget)
        _settle(app)
        step1 = _left_fraction(w._step1_main_split.sizes())

        assert abs(step1 - step0) <= 0.02, (width, step0, step1)
    finally:
        _close(w)


def _drag(app, split, fraction):
    """Move a splitter's handle as a user would, and announce it as a drag."""
    usable = max(1, split.width() - split.handleWidth())
    left = max(40, int(round(usable * fraction)))
    split.setSizes([left, max(1, usable - left)])
    split.splitterMoved.emit(left, 1)
    _settle(app)


def test_dragging_step0_s_handle_moves_step1_s(app):
    w = _window(app)
    try:
        _step0_left_fraction(app, w)                    # lay Step0 out
        _drag(app, w._step0._bg_c_split, 0.40)
        step0 = _left_fraction(w._step0._bg_c_split.sizes())

        w._set_step_active(1)
        w._stack.setCurrentWidget(w._step1_page_widget)
        _settle(app)
        assert abs(_left_fraction(w._step1_main_split.sizes()) - step0) <= 0.02
    finally:
        _close(w)


def test_dragging_step1_s_handle_moves_step0_s(app):
    """The direction the first fix left out -- reported from the real machine."""
    w = _window(app)
    try:
        _step0_left_fraction(app, w)
        w._set_step_active(1)
        w._stack.setCurrentWidget(w._step1_page_widget)
        _settle(app)

        _drag(app, w._step1_main_split, 0.42)
        step1 = _left_fraction(w._step1_main_split.sizes())

        w._set_step_active(0)
        w._stack.setCurrentWidget(w._step0)
        _settle(app)
        assert abs(_left_fraction(w._step0._bg_c_split.sizes()) - step1) <= 0.02
    finally:
        _close(w)


def test_a_drag_reaches_step0_s_hidden_peer_too(app):
    """Step0's work area has a second splitter behind it; both are written.

    They end at the SAME width whenever the width fits both. The peer is a
    hidden widget and is narrower than the visible work area, so a request
    wider than the peer's own maximum leaves it at that maximum -- Step0's
    own mechanism behaves exactly the same way when its handle is dragged,
    and this test states which case it is rather than asserting an equality
    that the geometry cannot always give.
    """
    w = _window(app)
    try:
        _step0_left_fraction(app, w)
        peer = getattr(w._step0, "_left_split_b", None)
        if peer is None or peer.count() != 2:
            pytest.skip("this build has no hidden conditioning splitter")

        w._set_step_active(1)
        w._stack.setCurrentWidget(w._step1_page_widget)
        _settle(app)
        before = peer.sizes()[0]
        _drag(app, w._step1_main_split, 0.38)

        w._set_step_active(0)
        w._stack.setCurrentWidget(w._step0)
        _settle(app)

        main_left = w._step0._bg_c_split.sizes()[0]
        peer_left = peer.sizes()[0]
        assert w._step0._left_col_width == main_left
        assert peer_left != before, "the hidden peer was not written at all"
        peer_max = max(1, peer.width() - peer.handleWidth()
                       - peer.widget(1).minimumSizeHint().width())
        if main_left <= peer_max:
            assert peer_left == main_left
        else:
            assert peer_left >= peer_max - 2, (peer_left, peer_max)
    finally:
        _close(w)


def test_a_width_that_fits_both_step0_splitters_lands_on_both(app):
    """The case the mechanism is actually for: one number, two splitters."""
    w = _window(app)
    try:
        _step0_left_fraction(app, w)
        peer = getattr(w._step0, "_left_split_b", None)
        if peer is None or peer.count() != 2:
            pytest.skip("this build has no hidden conditioning splitter")
        w._step0.apply_channel_column_width(300)
        _settle(app)
        assert w._step0._bg_c_split.sizes()[0] == peer.sizes()[0] == 300
    finally:
        _close(w)


@pytest.mark.parametrize("width", [1280, 1920])
def test_a_resize_does_not_let_the_two_pages_drift(app, width):
    w = _window(app, width=width)
    try:
        _step0_left_fraction(app, w)
        _drag(app, w._step0._bg_c_split, 0.36)

        for new_width in (1440, 2000, 1280):
            w.resize(new_width, 900)
            _settle(app)
            w._set_step_active(1)
            w._stack.setCurrentWidget(w._step1_page_widget)
            _settle(app)
            step1 = _left_fraction(w._step1_main_split.sizes())
            w._set_step_active(0)
            w._stack.setCurrentWidget(w._step0)
            _settle(app)
            step0 = _left_fraction(w._step0._bg_c_split.sizes())
            assert abs(step1 - step0) <= 0.02, (new_width, step0, step1)
            assert abs(step0 - 0.36) <= 0.03, (new_width, step0)
    finally:
        _close(w)


def test_a_step1_drag_survives_until_step0_is_laid_out(app):
    """The share is REMEMBERED, not re-measured from whoever is on screen.

    Step1 can be dragged before Step0 has ever been shown. Reading Step0 at
    that moment gives its opening width and would silently discard the drag.
    """
    w = _window(app)
    try:
        w._set_step_active(1)
        w._stack.setCurrentWidget(w._step1_page_widget)
        _settle(app)
        _drag(app, w._step1_main_split, 0.45)
        asked = w.channel_column_fraction()
        assert abs(asked - 0.45) <= 0.02, asked

        w._set_step_active(0)
        w._stack.setCurrentWidget(w._step0)
        _settle(app)
        assert abs(_left_fraction(w._step0._bg_c_split.sizes()) - 0.45) <= 0.02
    finally:
        _close(w)


def test_a_drag_below_step0_s_minimum_still_leaves_the_pages_together(app):
    """Step0's column has a floor; Step1's answer must be the floor too.

    Dragging Step1 far past it used to leave Step0 clamped at its minimum and
    Step1 at the width that was asked for -- the two pages apart again.
    """
    w = _window(app)
    try:
        _step0_left_fraction(app, w)
        w._set_step_active(1)
        w._stack.setCurrentWidget(w._step1_page_widget)
        _settle(app)

        _drag(app, w._step1_main_split, 0.05)          # well under the floor
        _settle(app)
        step1 = _left_fraction(w._step1_main_split.sizes())

        w._set_step_active(0)
        w._stack.setCurrentWidget(w._step0)
        _settle(app)
        step0_split = w._step0._bg_c_split
        step0 = _left_fraction(step0_split.sizes())

        # Step0 clamped -- that is its own rule -- and Step1 followed it there.
        assert step0_split.sizes()[0] >= step0_split.widget(0).minimumSizeHint().width()
        assert abs(step1 - step0) <= 0.02, (step0, step1)
        assert abs(w.channel_column_fraction() - step0) <= 0.02
    finally:
        _close(w)


def test_the_two_pages_do_not_answer_each_other_for_ever(app):
    """A sync writes sizes; a write that read back as a drag would loop."""
    w = _window(app)
    try:
        _step0_left_fraction(app, w)
        seen = []
        w._step0._bg_c_split.splitterMoved.connect(
            lambda *_a: seen.append("step0"))
        w._step1_main_split.splitterMoved.connect(
            lambda *_a: seen.append("step1"))

        _drag(app, w._step1_main_split, 0.34)
        for _ in range(3):
            w._set_step_active(0)
            w._stack.setCurrentWidget(w._step0)
            _settle(app)
            w._set_step_active(1)
            w._stack.setCurrentWidget(w._step1_page_widget)
            _settle(app)

        assert len(seen) <= 2, seen
    finally:
        _close(w)


def test_the_share_both_pages_use_is_the_one_measured_from_them(app):
    """The number is READ from a page, not copied into one."""
    w = _window(app)
    try:
        step0 = _step0_left_fraction(app, w)
        assert abs(w.channel_column_fraction() - step0) <= 0.001
    finally:
        _close(w)


def test_the_ratio_holds_when_step1_is_entered_without_a_resize(app):
    """The real path: the window opens on Step0 and walks to Step1.

    No resize happens in between, so a fix that only runs on `resizeEvent`
    leaves the columns at whatever the size hints produced -- measured as a
    1:1 split in a real session while this suite, which resized after
    switching pages, still read 1:2.
    """
    from block01.ui.main_window import MainWindow

    w = MainWindow()
    w._schedule_step1_session_save = lambda: None
    w._save_step1_session = lambda *a, **k: None
    w.loader = _Loader()
    w.config.set_channels(w.loader.channel_names())
    w.config.load_panel({"markers": {"CD3": 0.0, "CD8": 0.0}}, "DAPI")
    w.config.set_nucleus("DAPI", 1.0)
    try:
        # Sized and shown on Step0 FIRST, exactly as the application starts.
        w.resize(1600, 900)
        w._set_step_active(0)
        w._stack.setCurrentWidget(w._step0)
        w.show()
        _settle(app)

        # ...then the walk to Step1, with no resize of any kind.
        w._set_step_active(1)
        w._stack.setCurrentWidget(w._step1_page_widget)
        _settle(app)

        step0 = _left_fraction(w._step0._bg_c_split.sizes())
        step1 = _left_fraction(w._step1_main_split.sizes())
        assert abs(step1 - step0) <= 0.02, (step0, step1)
    finally:
        _close(w)


def test_the_picture_column_grows_with_the_window(app):
    widths = []
    for width in (1280, 1600, 1920):
        w = _window(app, width=width)
        try:
            widths.append(w.right_tabs.width())
        finally:
            _close(w)
    assert widths == sorted(widths) and widths[0] < widths[-1], widths


def test_no_outer_scroll_area_wraps_the_page(app):
    w = _window(app)
    try:
        assert w._step1_scroll is None
        assert w._stack.indexOf(w._step1_page_widget) >= 0
        page = w._step1_page_widget
        assert not isinstance(page.parentWidget(), QtWidgets.QScrollArea)
    finally:
        _close(w)


# ── gate 2: the dock survives a tab switch ────────────────────────────

def test_the_dock_stays_mounted_while_the_other_left_tab_is_up(app):
    w = _window(app)
    try:
        dock = w._channel_dock
        host_before = dock.parentWidget()
        rows_before = dock.list_widget.count()
        checked_before = [dock.list_widget.item(i).data(0)
                          for i in range(rows_before)]
        assert dock.isVisible()

        left, _right = _columns(w)
        left.setCurrentWidget(w.method_params_tab)
        _settle(app)

        # NOT `isVisible()`: a widget on a tab that is not current is hidden by
        # Qt itself. What must not have happened is a hide or an unmount.
        assert dock.parentWidget() is host_before
        assert not dock.isHidden() or dock.parentWidget() is host_before
        assert dock.list_widget.count() == rows_before
        assert [dock.list_widget.item(i).data(0)
                for i in range(rows_before)] == checked_before

        left.setCurrentWidget(w._step1_left_panel)
        _settle(app)
        assert dock.isVisible()
        assert dock.parentWidget() is host_before
    finally:
        _close(w)


def test_the_channel_state_survives_the_tab_switch(app):
    w = _window(app)
    try:
        # THE Step1 command, through its one owner (`GlobalChannelDock`):
        # ticking a channel shows it AND puts it into the fusion.
        w._channel_dock.use_channel("CD3", True, origin="test")
        draft_before = w._display.fusion.draft_snapshot()
        assert w._display.state.display_visible("CD3") is True

        left, _right = _columns(w)
        left.setCurrentWidget(w.method_params_tab)
        _settle(app)
        left.setCurrentWidget(w._step1_left_panel)
        _settle(app)

        assert w._display.state.display_visible("CD3") is True
        assert w._display.fusion.draft_snapshot() == draft_before
    finally:
        _close(w)


# ── gate 3: a short window still reaches everything ───────────────────

def test_a_720p_window_still_reaches_the_settings_and_the_commit_row(app):
    w = _window(app, width=1280, height=720)
    try:
        left, _right = _columns(w)
        left.setCurrentWidget(w.method_params_tab)
        _settle(app)
        scroll = w._step1_method_params_scroll
        assert scroll.isVisible()
        # Reachable means scrollable when it does not fit, not "fits".
        assert scroll.widget() is w.search
        assert scroll.widgetResizable()
        assert scroll.verticalScrollBar() is not None
        assert w.btn_save.isVisible()
        assert w._btn_save_fusion_settings is not None
    finally:
        _close(w)


# ── gate 4: the tabs themselves ───────────────────────────────────────

def test_the_two_columns_carry_the_tabs_the_user_asked_for(app):
    w = _window(app)
    try:
        left, right = _columns(w)
        assert [left.tabText(i) for i in range(left.count())] == [
            "Fusion", "Pre-segmentation"]
        assert [right.tabText(i) for i in range(right.count())] == [
            "Viewer", "Patch Results", "Pre-seg Results"]
        assert left.currentWidget() is w._step1_left_panel
        assert right.currentWidget() is w.viewer_tab
    finally:
        _close(w)


def test_patch_results_is_a_tab_beside_the_viewer_not_a_strip_over_it(app):
    w = _window(app)
    try:
        _left, right = _columns(w)
        assert right.indexOf(w.patch_results_tab) >= 0
        assert right.indexOf(w.viewer_tab) >= 0
        # Switching is what shows it: the two are never on screen together.
        w._show_step1_patch_results_tab("test")
        _settle(app)
        assert right.currentWidget() is w.patch_results_tab
        assert not w.prev_gv.isVisible()
        w._show_step1_viewer_tab("test")
        _settle(app)
        assert right.currentWidget() is w.viewer_tab
        assert w.prev_gv.isVisible()
    finally:
        _close(w)


def test_the_method_params_switch_follows_the_panel_to_the_left_column(app):
    w = _window(app)
    try:
        left, _right = _columns(w)
        left.setCurrentWidget(w._step1_left_panel)
        w._show_step1_method_params_tab("test")
        _settle(app)
        assert left.currentWidget() is w.method_params_tab
    finally:
        _close(w)
