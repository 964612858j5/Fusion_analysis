"""The product's visible surface, pinned.

`UI_SURFACE_RULES.md` is the ruling; this module is what enforces it. Making a
component global means reusing an instance, its state, its style and its
lifetime -- never adding a button, a window or a checkbox nobody asked for.

Three such additions had to be rolled back: a per-row `f` participation box
(which split "use this channel" into two controls and left a slide fused to
DAPI alone), `Intensity…` and `Weights…` buttons in the top step bar, and a
`Channel Weights` window duplicating the weight editor Step1's rows carry.

What is pinned here is the SURFACE, not the internals: `FusionDomainModel`
keeps participation, groups, per-group weights and provenance -- it simply has
no control of its own.
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
    filepath = "/tmp/surface.ome.tiff"
    shape = (256, 256)

    def __init__(self, names=("DAPI", "CD3", "CD8")):
        self._names = list(names)
        self.ch_map = {c: i for i, c in enumerate(self._names)}

    def channel_names(self):
        return list(self._names)

    def read_region(self, channel, y0, y1, x0, x1, downsample=1,
                    normalize=True):
        rng = np.random.default_rng(abs(hash(channel)) % 997)
        return rng.random(((y1 - y0) or 1, (x1 - x0) or 1), dtype=np.float32)


def _window(app):
    from block01.ui.main_window import MainWindow
    w = MainWindow()
    # A window built here inherits the machine's real project config, and a
    # draft change would autosave `step1_session.json` INTO that project.
    # Stubbed before anything edits state.
    w._schedule_step1_session_save = lambda: None
    w._save_step1_session = lambda *a, **k: None
    loader = _Loader()
    w.loader = loader
    w.config.set_channels(loader.channel_names())
    w.config.load_panel({"markers": {"CD3": 0.0, "CD8": 0.0}}, "DAPI")
    w.config.set_nucleus("DAPI", 1.0)
    w._step0.loader = loader
    w._step0.nucleus_channel = "DAPI"
    w._step0._rebuild_channel_list()
    w._set_step_active(1)
    return w


def _close(w):
    w._display.shutdown("test")
    w.deleteLater()


def test_no_row_carries_a_participation_control(app):
    """The `f` box: not built, not laid out, not hidden somewhere."""
    from block01.ui.widgets.channel_dock.global_dock import GlobalChannelRow

    w = _window(app)
    try:
        dock = w._channel_dock
        assert not hasattr(GlobalChannelRow, "set_fusion_enabled")
        assert not hasattr(GlobalChannelRow, "is_fusion_enabled")
        assert not hasattr(GlobalChannelRow, "fusion_toggled")
        for cid in dock.channel_order():
            row = dock.row(cid)
            assert not hasattr(row, "fusion_box"), cid
            texts = [c.text() for c in row.findChildren(QtWidgets.QCheckBox)]
            assert "ƒ" not in texts, (cid, texts)
            # one tick box per row, and it is the display/use one
            assert len(row.findChildren(QtWidgets.QCheckBox)) == 1, cid
    finally:
        _close(w)


def test_step1_rows_have_no_leftover_gap_where_it_stood(app):
    """Removing a control must not leave a hole in the row's geometry."""
    from block01.ui.widgets.channel_dock import template

    w = _window(app)
    w.resize(1500, 950)
    w.show()
    try:
        w._set_step_active(1)
        QtWidgets.QApplication.processEvents()
        dock = w._channel_dock
        rows = [dock.row(c) for c in dock.channel_order()]
        for row in rows:
            assert abs(row.height() - template.ROW_HEIGHT) <= 1
            # THE LAYOUT ITSELF: the weight spin is the last thing in the
            # row, and nothing holds the place the `f` box used to take.
            lay = row._lay
            widgets = [lay.itemAt(i).widget() for i in range(lay.count())
                       if lay.itemAt(i).widget() is not None]
            # Step0's method combo is in the row too, retired in Step1. Of
            # what Step1 SHOWS, the weight spin is the last thing, and there
            # is no second tick box behind it.
            shown = [x for x in widgets if x.isVisibleTo(row)]
            assert shown[-1] is row.spin, [type(x).__name__ for x in shown]
            assert len([x for x in shown
                        if isinstance(x, QtWidgets.QCheckBox)]) == 1
        # every row's spin ends at the same x: no ragged right edge
        assert len({r.spin.x() + r.spin.width() for r in rows}) == 1
    finally:
        w.hide()
        _close(w)


def test_the_step_bar_carries_no_buttons_at_all(app):
    """`Intensity…`, `Weights…` and a SECOND `Tissue Preview` were added to
    the step bar; none was asked for.

    Step0 already has a Tissue Preview button next to Load, and that is the
    one the user keeps: two buttons for one window is one too many.
    """
    w = _window(app)
    try:
        assert not hasattr(w, "_btn_global_intensity")
        assert not hasattr(w, "_btn_global_weights")
        assert not hasattr(w, "_open_global_intensity")
        assert not hasattr(w, "_open_global_weights")
        # THE STEP BAR ITSELF, not the whole window: Step0's own page has an
        # `Intensity…` button of its own and always did -- that is an
        # existing, authorised entry and it stays.
        bar = None
        for layout in w.findChildren(QtWidgets.QHBoxLayout):
            if layout.objectName() == "block01_step_bar":
                bar = layout
                break
        assert bar is not None, "the step bar was not found"
        labels = []
        for i in range(bar.count()):
            widget = bar.itemAt(i).widget()
            if isinstance(widget, QtWidgets.QPushButton):
                labels.append(widget.text())
        assert labels == [], labels
        assert not hasattr(w, "_btn_global_tissue")
        # ...and Step0's own one, beside Load, is still there
        assert w._step0._btn_tissue_nav.text().endswith("Tissue Navigator")
    finally:
        _close(w)


def test_there_is_no_separate_weight_window(app):
    """The window, and every production path that could open one."""
    import block01.ui.main_window as main_window

    w = _window(app)
    try:
        assert not hasattr(main_window, "_GlobalWeightEditor")
        assert not hasattr(w, "weight_editor_widget")
        assert not hasattr(w, "_refresh_weight_editor")
        display = w._display
        for name in ("show_weight_editor", "ensure_weight_editor",
                     "weight_editor", "weight_editor_panel",
                     "set_weight_editor_content"):
            assert not hasattr(display, name), name
        titles = [t.windowTitle()
                  for t in QtWidgets.QApplication.topLevelWidgets()]
        assert "Channel Weights" not in titles, titles
    finally:
        _close(w)


def test_the_weights_are_still_editable_where_they_always_were(app):
    """The rule removes SURFACE, never the ability to do the work."""
    w = _window(app)
    try:
        w._set_step_active(1)
        row = w._channel_dock.row("CD3")
        assert row.slider.isEnabled() and row.spin.isEnabled()
        row.spin.setValue(0.42)
        assert w._display.fusion.channel_weight("CD3") == pytest.approx(0.42)
        # ...and the model still keeps everything it ever kept
        model = w._display.fusion
        for name in ("groups", "group_weights", "weight_provenance",
                     "draft_snapshot", "committed_snapshot",
                     "fusion_enabled"):
            assert hasattr(model, name), name
    finally:
        _close(w)


def test_a_full_step_walk_opens_no_window_of_its_own(app):
    w = _window(app)
    w.resize(1500, 950)
    w.show()
    QtWidgets.QApplication.processEvents()
    try:
        before = {(type(t).__name__, t.windowTitle())
                  for t in QtWidgets.QApplication.topLevelWidgets()
                  if t.isVisible()}
        for step in (0, 1, 2, 3, 1, 0):
            w._set_step_active(step)
            QtWidgets.QApplication.processEvents()
        after = {(type(t).__name__, t.windowTitle())
                 for t in QtWidgets.QApplication.topLevelWidgets()
                 if t.isVisible()}
        assert after - before == set(), after - before
    finally:
        w.hide()
        _close(w)


def test_the_rules_file_is_present_and_names_the_removed_controls(app):
    """The rule outlives any one session: it is in the repository."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    rules = root / "UI_SURFACE_RULES.md"
    assert rules.exists(), "UI_SURFACE_RULES.md is missing"
    text = rules.read_text(encoding="utf-8")
    for phrase in ("stop and ask first", "Channel Weights", "Weights…",
                   "Intensity…", "participation"):
        assert phrase in text, phrase


def test_step1_keeps_its_ruled_layout_and_entries(app):
    """Block A's surface: two columns of tabs, and where the entries live.

    `UI_SURFACE_RULES.md` §4 records this page. What is pinned here is the
    part a refactor could move without anyone noticing: the two tab sets, the
    Tissue Preview entry on the title line, `Intensity…` inside the `Channels`
    frame, and the headings that are gone.
    """
    w = _window(app)
    try:
        left = w._step1_left_tabs
        right = w.right_tabs
        assert [left.tabText(i) for i in range(left.count())] == [
            "Fusion", "Pre-segmentation"]
        assert [right.tabText(i) for i in range(right.count())] == [
            "Viewer", "Patch Results", "Pre-seg Results"]
        assert w._step1_main_split.count() == 2

        # The Tissue Navigator entry is on the title line, not in the column.
        # The title line is its own bar since 2026-09-23 (as tall as Step0's
        # Load bar), and that bar sits directly on the page.
        nav = w._btn_step1_tissue_nav
        assert nav.parentWidget() is w._step1_title_bar
        assert w._step1_title_bar.parentWidget() is w._step1_page_widget
        assert w._step1_left_panel.findChildren(QtWidgets.QPushButton).count(
            nav) == 0

        # `Intensity…` is INSIDE the Channels frame, as in Step0.
        intensity = w._btn_step1_intensity
        assert intensity.parentWidget() is w._step1_channels_box

        # The headings the user removed do not come back.
        texts = {lbl.text() for lbl in
                 w._step1_page_widget.findChildren(QtWidgets.QLabel)}
        assert not any("ROI / Patch Overview" in t for t in texts), texts
        assert not any("Red=cyto" in t for t in texts), texts
        # `③ Search Results` inside ResultGridPanel is Step0's own heading
        # and stays; what went is the heading OVER THE PICTURE.
        assert not any(t.startswith(("③ Preview", "③ Overlay", "③ Fusion"))
                       for t in texts), texts
        assert not hasattr(w, "_preview_title")
        assert any("Preliminary Segmentation" in t for t in texts), texts

        # No outer scroll area: the page fills the stack itself.
        assert w._step1_scroll is None
        assert not isinstance(w._step1_page_widget.parentWidget(),
                              QtWidgets.QScrollArea)
    finally:
        _close(w)
