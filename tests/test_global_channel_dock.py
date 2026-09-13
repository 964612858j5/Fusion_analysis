"""ONE public channel dock, through every step.

B4-B. Step0, Step1, Step2 and Step3 used to hold a public channel list each --
Step0's dock in the Background Correction tab, Step1's private `ConfigPanel`
rows, Step3's hand-built overlay rows -- so the same channel could be ticked
in one and clear in another, and walking between steps destroyed and rebuilt
the list under the user: search text, scroll position and the row being worked
on were gone every time.

What is pinned here is that there is now exactly ONE public dock, that it is
the same object (and the same rows) across a full step walk, that a step
change switches only the accessory, and that each public control still writes
exactly one owner: display visibility and colour to `ChannelDisplayState`,
participation and weight to `FusionDomainModel`, the correction decision to
Step0's own domain.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets  # noqa: E402
from PyQt5.QtCore import Qt  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Loader:
    filepath = "/tmp/dataset_a.ome.tiff"
    shape = (128, 128)

    def __init__(self, names=("DAPI", "CD3", "CD8"), path=None):
        self._names = list(names)
        if path:
            self.filepath = path
        self.ch_map = {c: i for i, c in enumerate(self._names)}

    def channel_names(self):
        return list(self._names)

    def read_region(self, channel, y0, y1, x0, x1, downsample=1,
                    normalize=True):
        rng = np.random.default_rng(abs(hash(channel)) % 997)
        return rng.random(((y1 - y0) or 1, (x1 - x0) or 1), dtype=np.float32)


def _window(app, names=("DAPI", "CD3", "CD8")):
    from block01.ui.main_window import MainWindow
    w = MainWindow()
    loader = _Loader(names)
    w.loader = loader
    w.config.set_channels(list(names))
    w.config.load_panel({"markers": {c: 0.0 for c in names[1:]}}, names[0])
    w.config.set_nucleus(names[0], 1.0)
    w._step0.loader = loader
    w._step0.nucleus_channel = names[0]
    w._step0._rebuild_channel_list()
    w._set_step_active(0)
    return w


def _close(w):
    w._display.shutdown("test")
    w.deleteLater()


STEP_WALK = (0, 1, 2, 3, 2, 1, 0)


# ── A. one instance, stable identity through a full step walk ───────────────

def test_one_dock_and_stable_row_identity_through_the_walk(app):
    w = _window(app)
    try:
        dock = w._channel_dock
        ids = {"dock": [], "list": [], "search": [], "rows": []}
        for step in STEP_WALK:
            w._set_step_active(step)
            ids["dock"].append(id(w._channel_dock))
            ids["list"].append(id(w._channel_dock.list_widget))
            ids["search"].append(id(w._channel_dock.search))
            ids["rows"].append(tuple(id(dock.row(c))
                                     for c in dock.channel_order()))
        for key, seen in ids.items():
            assert len(set(seen)) == 1, f"{key} was rebuilt: {seen}"
        assert dock.channel_order() == ["DAPI", "CD3", "CD8"]
    finally:
        _close(w)


def test_the_dock_lives_outside_the_stacked_pages(app):
    w = _window(app)
    try:
        dock = w._channel_dock
        assert w._display.channel_dock() is dock
        # not inside any page of the stack
        for i in range(w._stack.count()):
            page = w._stack.widget(i)
            assert dock not in page.findChildren(type(dock))
            assert not _is_descendant(dock, page)
    finally:
        _close(w)


def _is_descendant(widget, ancestor):
    node = widget.parent()
    while node is not None:
        if node is ancestor:
            return True
        node = node.parent()
    return False


# ── B. the old public lists are not constructed ─────────────────────────────

def test_exactly_one_public_channel_list_in_the_real_window(app):
    from block01.ui.widgets.channel_dock.global_dock import (
        GlobalChannelDock, GlobalChannelRow)
    from block01.ui.widgets.channel_dock import ChannelDock
    from block01.ui.step0.config_panel import ChannelRow as Step1PrivateRow

    w = _window(app)
    try:
        docks = w.findChildren(GlobalChannelDock) + [w._channel_dock]
        assert len({id(d) for d in docks}) == 1
        # Step0's own ChannelDock and Step1's private rows are not built
        assert w.findChildren(ChannelDock) == []
        assert w.findChildren(Step1PrivateRow) == []
        assert w._step0._dock_adapter.dock is w._channel_dock
        assert w._step0._channel_list is w._channel_dock.list_widget
        # ...and one set of public rows, the dock's
        rows = w._channel_dock.findChildren(GlobalChannelRow)
        assert len(rows) == len(w._channel_dock.channel_order())
        assert w.config._rows == w._channel_dock.rows()
        assert w.config._private_rows == {}
    finally:
        _close(w)


def test_step3_marker_rows_carry_no_public_controls(app):
    w = _window(app)
    try:
        step3 = w._step3
        step3._available_channels = ["CD3", "CD8"]
        step3._channel_sources = {"CD3": "raw", "CD8": "raw"}
        for ch in step3._available_channels:
            step3._channel_settings.setdefault(
                ch, step3._default_channel_settings(ch))
        step3._rebuild_channel_panel()
        for ch in ("CD3", "CD8"):
            row = step3._channel_rows[ch]
            # display visibility and colour are public and edited in the dock
            assert "checkbox" not in row, ch
            assert "color_btn" not in row, ch
            # ...while the page keeps its own overlay control
            assert "opacity" in row, ch
        # the page's own layers are not channels and keep their controls
        assert "checkbox" in step3._channel_rows["__layer_dapi__"]
        assert "checkbox" in step3._channel_rows["__layer_fusion__"]
    finally:
        _close(w)


def test_step3_reads_the_shared_visibility_and_colour(app):
    w = _window(app)
    try:
        step3 = w._step3
        step3._available_channels = ["CD3", "CD8"]
        step3._channel_sources = {"CD3": "raw", "CD8": "raw"}
        for ch in step3._available_channels:
            step3._channel_settings.setdefault(
                ch, step3._default_channel_settings(ch))
        state = w._display.state
        state.set_display_visible("CD3", True, origin="test")
        state.set_color("CD3", "#123456", origin="test")
        assert step3._marker_visible("CD3") is True
        assert step3._marker_color("CD3").lower() == "#123456"
        state.set_display_visible("CD3", False, origin="test")
        assert step3._marker_visible("CD3") is False
    finally:
        _close(w)


# ── C. the user's place in the list survives the walk ───────────────────────

def test_search_scroll_selection_and_focus_survive_the_walk(app):
    w = _window(app, names=("DAPI", "CD3", "CD8", "CD20", "CD68"))
    try:
        dock = w._channel_dock
        dock.resize(380, 160)
        dock.show()
        QtWidgets.QApplication.processEvents()
        dock.search.setText("CD")
        w._display.state.set_selected_channel("CD8", origin="test")
        bar = dock.list_widget.verticalScrollBar()
        bar.setValue(bar.maximum())
        dock.search.setFocus(Qt.OtherFocusReason)
        before = (dock.search.text(), bar.value(),
                  w._display.state.selected_channel(),
                  dock.channel_order(),
                  QtWidgets.QApplication.focusWidget())
        row_ids = {c: id(dock.row(c)) for c in dock.channel_order()}

        for step in STEP_WALK:
            w._set_step_active(step)
            QtWidgets.QApplication.processEvents()

        after = (dock.search.text(), bar.value(),
                 w._display.state.selected_channel(),
                 dock.channel_order(),
                 QtWidgets.QApplication.focusWidget())
        assert before == after
        assert row_ids == {c: id(dock.row(c)) for c in dock.channel_order()}
    finally:
        dock.hide()
        _close(w)


def test_the_step_change_switches_only_the_accessory(app):
    from block01.ui.widgets.channel_dock import global_dock as gd

    w = _window(app)
    try:
        dock = w._channel_dock
        row = dock.row("CD3")
        w._set_step_active(0)
        assert row.method_cb.isVisibleTo(row)
        assert not row.fusion_box.isVisibleTo(row)
        w._set_step_active(1)
        assert row.fusion_box.isVisibleTo(row)
        assert not row.method_cb.isVisibleTo(row)
        for step in (2, 3):
            w._set_step_active(step)
            # Step2 and Step3 consume the public answers; they have no
            # accessory here and no invented second panel.
            assert dock.row("CD3").accessory(step) == ()
            assert not row.method_cb.isVisibleTo(row)
            assert not row.fusion_box.isVisibleTo(row)
        # the weight editor is CORE: present in every step
        for step in STEP_WALK:
            w._set_step_active(step)
            assert row.spin.isVisibleTo(row)
            assert row.checkbox.isVisibleTo(row)
        assert gd.STEP0 == 0 and gd.STEP1 == 1
    finally:
        _close(w)


def test_a_step_change_issues_no_command(app):
    w = _window(app)
    try:
        state, fusion = w._display.state, w._display.fusion
        seen = []
        state.visibility_changed.connect(
            lambda c, v: seen.append(("vis", c, v)))
        state.color_changed.connect(lambda c, v: seen.append(("col", c)))
        fusion.weight_changed.connect(lambda c: seen.append(("weight", c)))
        fusion.participation_changed.connect(
            lambda c, e: seen.append(("fusion", c, e)))
        fusion.draft_changed.connect(lambda: seen.append(("draft",)))
        before_rev = fusion.draft_revision()
        for step in STEP_WALK:
            w._set_step_active(step)
        assert seen == []
        assert fusion.draft_revision() == before_rev
    finally:
        _close(w)


# ── D. the B1 visual template, on the public row ────────────────────────────

def test_the_public_row_keeps_the_shared_template_geometry(app):
    from block01.ui.widgets.channel_dock import template

    w = _window(app)
    try:
        dock = w._channel_dock
        dock.resize(420, 300)
        dock.show()
        QtWidgets.QApplication.processEvents()
        rows = [dock.row(c) for c in dock.channel_order()]
        for r in rows:
            r.resize(400, template.ROW_HEIGHT)
            r.layout().activate()
            assert template.is_template_row(r)
            assert r.checkbox.size().width() == template.CHECKBOX_SIZE[0]
            assert r.checkbox.size().height() == template.CHECKBOX_SIZE[1]
            assert r.state_slot.width() == template.STATE_SLOT_WIDTH
            assert r.swatch.size().width() == template.SWATCH_SIZE
        assert len({r.swatch.x() for r in rows}) == 1
        assert len({r.name_label.x() for r in rows}) == 1
        assert len({r.name_label.width() for r in rows}) == 1
        assert dock.list_widget.horizontalScrollBarPolicy() == \
            Qt.ScrollBarAlwaysOff
        assert "item:selected:hover" in template.LIST_QSS
        # an accessory does not restyle, re-space or re-measure the core:
        # the name column is the longest name plus the template's padding,
        # and it is the SAME column in every step
        from block01.ui.widgets.channel_dock import template as _t
        expected = max(lbl.fontMetrics().boundingRect(lbl.text()).width()
                       for lbl in (r.name_label for r in rows)) \
            + _t.NAME_WIDTH_PADDING
        core_per_step = {}
        for step in STEP_WALK:
            w._set_step_active(step)
            QtWidgets.QApplication.processEvents()
            for r in rows:
                r.layout().activate()
            assert len({r.name_label.x() for r in rows}) == 1
            core_per_step[step] = [(r.checkbox.x(), r.state_slot.x(),
                                    r.swatch.x(), r.name_label.x(),
                                    r.name_label.width(),
                                    r.styleSheet()) for r in rows]
        assert len({tuple(v) for v in core_per_step.values()}) == 1, \
            "the accessory moved the public core between steps"
        assert {r.name_label.width() for r in rows} == {expected}
    finally:
        dock.hide()
        _close(w)


# ── E. Step0's correction accessory ─────────────────────────────────────────

def test_the_correction_accessory_only_changes_the_correction(app):
    from block01.ui.widgets.channel_dock.global_dock import CORRECTION_METHODS

    w = _window(app)
    try:
        w._set_step_active(0)
        page, state, fusion = w._step0, w._display.state, w._display.fusion
        row = w._channel_dock.row("CD3")
        state.set_display_visible("CD3", True, origin="test")
        fusion.set_fusion_enabled("CD3", True, origin="test")
        before = (state.display_visible("CD3"), fusion.fusion_enabled("CD3"),
                  fusion.channel_weight("CD3"))

        row.method_cb.setCurrentText("TopHat")

        assert page._channel_decisions["CD3"] == "tophat"
        assert page._channel_row_method("CD3") == "tophat"
        assert (state.display_visible("CD3"), fusion.fusion_enabled("CD3"),
                fusion.channel_weight("CD3")) == before
        # `Both` is a computation, never a final answer
        assert "both" not in [m.lower() for m in CORRECTION_METHODS]
        assert [row.method_cb.itemText(i)
                for i in range(row.method_cb.count())] == CORRECTION_METHODS
        # the nucleus cannot be corrected, and can still be shown
        nuc = w._channel_dock.row("DAPI")
        assert not nuc.method_cb.isEnabled()
        assert nuc.checkbox.isEnabled()
    finally:
        _close(w)


def test_no_state_tooltip_names_a_button_that_is_gone(app):
    from block01.ui.widgets.channel_dock.global_dock import STATE_GLYPHS

    w = _window(app)
    try:
        row = w._channel_dock.row("CD3")
        for key in STATE_GLYPHS:
            row.set_state(key)
            assert "process" not in row.state_slot.toolTip().lower(), key
        joined = " ".join(t for _g, _s, t in STATE_GLYPHS.values()).lower()
        assert "process" not in joined
        assert "press enter" in joined
    finally:
        _close(w)


# ── F. Step1's three commands are three things ──────────────────────────────

def test_the_public_checkbox_is_display_and_only_display(app):
    w = _window(app)
    try:
        w._set_step_active(1)
        state, fusion = w._display.state, w._display.fusion
        row = w._channel_dock.row("CD3")
        fusion.set_fusion_enabled("CD3", True, origin="test")
        rev = fusion.draft_revision()
        enabled = fusion.fusion_enabled("CD3")
        weight = fusion.channel_weight("CD3")

        row.checkbox.setChecked(True)
        assert state.display_visible("CD3") is True
        row.checkbox.setChecked(False)
        assert state.display_visible("CD3") is False
        # nothing scientific moved
        assert fusion.draft_revision() == rev
        assert fusion.fusion_enabled("CD3") == enabled
        assert fusion.channel_weight("CD3") == weight
        assert w._step0._channel_decisions.get("CD3") in (None, "original")
    finally:
        _close(w)


def test_participation_and_weight_are_separate_commands(app):
    w = _window(app)
    try:
        w._set_step_active(1)
        state, fusion = w._display.state, w._display.fusion
        row = w._channel_dock.row("CD3")
        state.set_display_visible("CD3", False, origin="test")

        # a first enable of a channel nobody has weighted answers 1.0, once
        row.fusion_box.setChecked(True)
        assert fusion.fusion_enabled("CD3") is True
        assert fusion.channel_weight("CD3") == pytest.approx(1.0)
        assert state.display_visible("CD3") is False

        # an explicit 0.0 is an answer and survives a disable/re-enable
        row.spin.setValue(0.0)
        assert fusion.channel_weight("CD3") == pytest.approx(0.0)
        row.fusion_box.setChecked(False)
        row.fusion_box.setChecked(True)
        assert fusion.channel_weight("CD3") == pytest.approx(0.0)
        assert state.display_visible("CD3") is False
    finally:
        _close(w)


def test_a_mixed_channel_is_named_not_flattened(app):
    w = _window(app)
    try:
        fusion = w._display.fusion
        fusion.add_group("g2", {"CD3": 0.7})
        fusion.add_group("markers", {"CD3": 0.2, "CD8": 0.0})
        dock = w._channel_dock
        dock.refresh()
        row = dock.row("CD3")
        rep = fusion.representative_weight("CD3")
        assert rep.mixed and set(rep.values) == {0.2, 0.7}
        assert row.name_label.text().endswith("*")
        assert row.weight() == pytest.approx(0.7)

        # repainting and walking the steps must not unify the groups
        for step in STEP_WALK:
            w._set_step_active(step)
        dock.refresh()
        assert set(fusion.representative_weight("CD3").values) == {0.2, 0.7}

        # only an explicit edit unifies them
        row.spin.setValue(0.5)
        assert set(fusion.representative_weight("CD3").values) == {0.5}
        assert fusion.groups()["markers"]["CD3"] == pytest.approx(0.5)
        assert fusion.groups()["g2"]["CD3"] == pytest.approx(0.5)
    finally:
        _close(w)


def test_one_user_action_is_one_domain_command(app):
    w = _window(app)
    try:
        w._set_step_active(1)
        state, fusion = w._display.state, w._display.fusion
        row = w._channel_dock.row("CD8")
        vis, col, weight, part = [], [], [], []
        state.visibility_changed.connect(lambda c, v: vis.append((c, v)))
        state.color_changed.connect(lambda c, v: col.append((c, v)))
        fusion.weight_changed.connect(weight.append)
        fusion.participation_changed.connect(lambda c, e: part.append((c, e)))

        row.checkbox.setChecked(not row.checkbox.isChecked())
        assert len(vis) == 1 and not col and not weight and not part

        row.spin.setValue(0.42)
        assert len(weight) == 1 and len(vis) == 1

        state.set_color("CD8", "#00ff88", origin="test")
        assert len(col) == 1

        row.fusion_box.setChecked(not row.fusion_box.isChecked())
        assert len(part) == 1
    finally:
        _close(w)


def test_a_tick_reaches_the_window_once(app):
    """One tick, one refresh. Two lists meant two: the panel announced it and
    the shared state announced it, so the patch preview was recomposed twice
    for one click."""
    w = _window(app)
    try:
        w._set_step_active(1)
        # Counted where the WORK is, through the production connections: a
        # second announcement of the same tick shows up as a second compose,
        # which is what two channel lists used to cost.
        calls = []
        w._refresh_patch_preview = lambda *a, **k: calls.append(a)

        row = w._channel_dock.row("CD3")
        row.checkbox.setChecked(not row.checkbox.isChecked())

        assert len(calls) == 1, calls
    finally:
        _close(w)


# ── G. an edit made while Step1 is not the step on screen ───────────────────

def test_a_weight_edited_from_step3_reaches_the_model_and_step1(app):
    from block01.ui.step0.config_panel import ChannelRow as Step1PrivateRow

    w = _window(app)
    try:
        w._set_step_active(3)
        fusion = w._display.fusion
        row = w._channel_dock.row("CD8")
        row.spin.setValue(0.35)
        assert fusion.channel_weight("CD8") == pytest.approx(0.35)
        assert fusion.weight_provenance("CD8") == "explicit"
        assert w.config.channel_weight("CD8") == pytest.approx(0.35)
        assert w.findChildren(Step1PrivateRow) == []

        w._set_step_active(1)
        assert w._channel_dock.row("CD8").weight() == pytest.approx(0.35)
        assert fusion.channel_weight("CD8") == pytest.approx(0.35)
    finally:
        _close(w)


# ── H. the display consumers follow the one answer ──────────────────────────

def test_a_colour_written_in_the_dock_reaches_every_reader(app):
    w = _window(app)
    try:
        state = w._display.state
        w._display.state.set_color("CD3", "#20b2aa", origin="dock-swatch")
        assert state.color("CD3").lower() == "#20b2aa"
        assert w._channel_dock.row("CD3").color().lower() == "#20b2aa"
        assert w.config.channel_color("CD3").lower() == "#20b2aa"
        assert w._step0._channel_swatch_hex("CD3").lower() == "#20b2aa"
    finally:
        _close(w)


# ── I. dataset switches, and what a rebuild may not do ──────────────────────

def test_rows_are_rebuilt_for_a_new_universe_and_not_for_a_step(app):
    w = _window(app)
    try:
        dock = w._channel_dock
        first = {c: id(dock.row(c)) for c in dock.channel_order()}
        dock.set_channels(["DAPI", "CD3", "CD8"])          # same universe
        assert {c: id(dock.row(c)) for c in dock.channel_order()} == first
        rebuilt = dock.set_channels(["DAPI", "CD3", "CD8", "CD20"])
        assert rebuilt is True
        assert dock.channel_order() == ["DAPI", "CD3", "CD8", "CD20"]
    finally:
        _close(w)


def test_a_programmatic_selection_shows_nothing(app):
    w = _window(app)
    try:
        state = w._display.state
        state.set_display_visible("CD8", False, origin="test")
        state.set_selected_channel("CD8", origin="restore")
        assert state.display_visible("CD8") is False
        # a REAL click on the same row selects AND shows it
        w._channel_dock.row("CD8").row_clicked.emit("CD8")
        assert state.display_visible("CD8") is True
        assert state.selected_channel() == "CD8"
    finally:
        _close(w)


# ── J. close, resume, finalize ──────────────────────────────────────────────

def test_a_refused_close_keeps_the_same_dock_and_finalize_retires_it_once(app):
    w = _window(app)
    dock = w._channel_dock
    display = w._display
    display.begin_close("test")
    display.resume()
    assert display.channel_dock() is dock
    display.begin_close("test")
    display.finalize_close("test")
    assert display.channel_dock() is None
    display.finalize_close("test")          # idempotent
    assert display.is_finalized()
    # a finalized session builds no new dock
    assert display.ensure_channel_dock() is None
    w.deleteLater()


# ── K. Step0's page is not what holds the public answers ────────────────────

def test_the_dock_outlives_the_step0_page(app):
    w = _window(app)
    try:
        state, fusion = w._display.state, w._display.fusion
        state.set_color("CD3", "#ff8800", origin="test")
        state.set_display_visible("CD3", True, origin="test")
        fusion.edit_channel_weight("CD3", 0.4, origin="test")

        page = w._step0
        page.setParent(None)
        page.deleteLater()
        QtWidgets.QApplication.processEvents()

        dock = w._display.channel_dock()
        assert dock is not None
        assert state.color("CD3").lower() == "#ff8800"
        assert state.display_visible("CD3") is True
        assert fusion.channel_weight("CD3") == pytest.approx(0.4)
        # ...including a full repaint, which asks Step0 for its correction
        # answers and must not reach through the destroyed page
        dock.refresh()
        # the public row still edits the owners
        dock.row("CD3").spin.setValue(0.6)
        assert fusion.channel_weight("CD3") == pytest.approx(0.6)
    finally:
        w._display.shutdown("test")
        w.deleteLater()


def test_step2_display_edits_do_not_touch_segmentation(app):
    w = _window(app)
    try:
        w._set_step_active(2)
        before = w._step2._seg_params_edit.text()
        state = w._display.state
        row = w._channel_dock.row("CD3")
        row.checkbox.setChecked(not row.checkbox.isChecked())
        w._display.state.set_color("CD3", "#445566", origin="test")
        assert w._step2._seg_params_edit.text() == before
        assert state.color("CD3").lower() == "#445566"
    finally:
        _close(w)


def test_the_correction_accessory_fails_closed_after_step0_is_gone(app):
    w = _window(app)
    try:
        dock = w._display.channel_dock()
        page = w._step0
        page.setParent(None)
        page.deleteLater()
        QtWidgets.QApplication.processEvents()
        # No crash, and no decision written through a dangling page.
        dock.row("CD3").method_cb.setCurrentText("cucim")
        dock.correction_method_changed.emit("CD3", "TopHat")
    finally:
        w._display.shutdown("test")
        w.deleteLater()


def test_a_late_owner_signal_after_finalize_touches_no_dead_row(app):
    w = _window(app)
    state, fusion = w._display.state, w._display.fusion
    w._display.begin_close("test")
    w._display.finalize_close("test")
    QtWidgets.QApplication.processEvents()
    # the owners outlive the dock; a late answer must not reach a dead row
    state.set_display_visible("CD3", True, origin="late")
    state.set_color("CD3", "#010203", origin="late")
    fusion.edit_channel_weight("CD3", 0.9, origin="late")
    QtWidgets.QApplication.processEvents()
    assert w._display.channel_dock() is None
    w.deleteLater()
