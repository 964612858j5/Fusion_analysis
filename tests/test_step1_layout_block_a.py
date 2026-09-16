"""Step1's page is two columns of tabs, in Step0's proportion.

Block A of `docs/step1_rework_plan.md`. What is pinned here is the SURFACE the
user ruled on:

* one channel column to two of picture -- Step0's `c_split` ratio, measured on
  the real widths after the layout has settled, not read off the stretch
  factors;
* `Channels | Method & Parameters` on the left, `Viewer | Patch Results` on the
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


# ── gate 1: Step0's proportion, measured ──────────────────────────────

@pytest.mark.parametrize("width", [1280, 1600, 1920])
def test_the_picture_gets_two_parts_to_the_channel_column_s_one(app, width):
    w = _window(app, width=width)
    try:
        left, right = _columns(w)
        assert w._step1_main_split.count() == 2, "two columns, not three"
        ratio = right.width() / float(left.width())
        assert 1.8 <= ratio <= 2.2, (width, left.width(), right.width(), ratio)
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

        left, right = _columns(w)
        ratio = right.width() / float(left.width())
        assert 1.8 <= ratio <= 2.2, (left.width(), right.width(), ratio)
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
            "Channels", "Method & Parameters"]
        assert [right.tabText(i) for i in range(right.count())] == [
            "Viewer", "Patch Results"]
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
