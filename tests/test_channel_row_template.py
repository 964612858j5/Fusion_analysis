"""One visual template for every channel list.

Block01 draws channel rows in more than one place, and before B1 it BUILT them
in more than one place: the shared dock's `ChannelRowBase` and Step1's private
`ConfigPanel.ChannelRow` each made their own checkbox, swatch and name, with
their own stylesheets, and Step1 carried a second list stylesheet and a second
colour palette. Measured on a real window, the same channel had a 22x18 themed
checkbox in Step0 and a 14x15 platform one in Step1, its swatch at x=50
against x=25, its name at x=68 against x=43, and a row 25 px tall against 22.

What this module pins is that the COMMON columns now come from
`channel_dock.template` and measure the same in both steps, while each step
keeps its own accessory -- Step0's correction method, Step1's weight controls.
It asserts real widget geometry, not source text.

Own module, like the other page-heavy Step0/Step1 suites: combined runs
segfault in offscreen pyqtgraph.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets  # noqa: E402

from block01.core.fusion_domain import (  # noqa: E402
    FusionDomainModel,
)

from block01.ui.widgets.channel_dock import template  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


# ── the two real lists, side by side ─────────────────────────────────────

def _both(app, channels=("DAPI", "CD3", "CD8")):
    """A real Step0 dock and a real Step1 panel over the same channels."""
    from block01.ui.widgets.channel_dock import (
        ChannelDock, ChannelSetModel, ChannelState, Step0ChannelRow)
    from block01.ui.step0.config_panel import ConfigPanel

    model = ChannelSetModel()
    model.set_channels([ChannelState(channel_id=c, name=c) for c in channels])
    dock = ChannelDock(model, row_factory=Step0ChannelRow, title="")
    panel = ConfigPanel(list(channels), fusion=FusionDomainModel(),
                        private_list=True)
    host = QtWidgets.QWidget()
    lay = QtWidgets.QHBoxLayout(host)
    lay.addWidget(dock)
    lay.addWidget(panel)
    host.resize(760, 400)
    host.show()
    app.processEvents()
    return host, dock, panel


def _geo(w):
    r = w.geometry()
    return (r.x(), r.y(), r.width(), r.height())


# ── 1. the common columns measure the same ───────────────────────────────

def test_the_common_row_columns_are_identical_in_both_lists(app):
    host, dock, panel = _both(app)
    try:
        a, b = dock.row("CD3"), panel._rows["CD3"]

        assert a.height() == b.height(), "row height"
        assert dock.item("CD3").sizeHint().height() == \
               panel._items["CD3"].sizeHint().height(), "item height"
        assert a.layout().getContentsMargins() == b.layout().getContentsMargins()
        assert a.layout().spacing() == b.layout().spacing()
        assert _geo(a.checkbox) == _geo(b.checkbox), "checkbox"
        assert _geo(a.swatch) == _geo(b.swatch), "swatch"
        assert a.name_label.x() == b.name_label.x(), "name x"
        assert a.name_label.font().pixelSize() == b.name_label.font().pixelSize()
        assert a.styleSheet() == b.styleSheet(), "row stylesheet"
    finally:
        host.close()


def test_the_checkbox_indicator_is_the_template_in_both(app):
    host, dock, panel = _both(app)
    try:
        for row in (dock.row("CD3"), panel._rows["CD3"]):
            assert row.checkbox.size().width() == template.CHECKBOX_SIZE[0]
            assert row.checkbox.size().height() == template.CHECKBOX_SIZE[1]
            assert template.CHECKBOX_INDICATOR_QSS in row.styleSheet()
    finally:
        host.close()


def test_the_state_slot_keeps_its_width_when_it_is_empty(app):
    """Step1 draws nothing in it. If it collapsed, the swatch and the name
    would sit at a different x than in Step0 -- which is the difference this
    template removes."""
    host, dock, panel = _both(app)
    try:
        step1_slot = panel._rows["CD3"].state_slot
        assert step1_slot.text() == ""
        assert step1_slot.width() == template.STATE_SLOT_WIDTH
        assert dock.row("CD3").state_slot.width() == template.STATE_SLOT_WIDTH
    finally:
        host.close()


def test_the_lists_share_one_stylesheet_and_scroll_policy(app):
    host, dock, panel = _both(app)
    try:
        assert dock.list_widget.styleSheet() == panel._list.styleSheet()
        assert dock.list_widget.styleSheet() == template.LIST_QSS
        assert dock.list_widget.horizontalScrollBarPolicy() == \
               panel._list.horizontalScrollBarPolicy()
    finally:
        host.close()


def test_a_selected_row_under_the_cursor_stays_selected_coloured(app):
    """Step1's old list stylesheet had no `item:selected:hover` rule, so a
    selected row under the mouse turned the green hover colour while Step0's
    stayed blue."""
    host, dock, panel = _both(app)
    try:
        for sheet in (dock.list_widget.styleSheet(), panel._list.styleSheet()):
            assert "item:selected:hover" in sheet
            rule = sheet.split("item:selected:hover")[1]
            assert template.COLOR_SELECTED in rule.split("}")[0]
            assert template.COLOR_HOVER not in rule.split("}")[0]
    finally:
        host.close()


# ── 2. the name column ───────────────────────────────────────────────────

def test_the_name_column_is_the_longest_name_in_the_list(app):
    host, dock, panel = _both(app, channels=("DAPI", "CD3", "A_VERY_LONG_NAME"))
    try:
        for rows in (dock.rows().values(), panel._rows.values()):
            widths = {r.name_label.width() for r in rows}
            assert len(widths) == 1, f"name column is not uniform: {widths}"
    finally:
        host.close()


def test_a_marker_on_one_name_does_not_shift_another_rows_columns(app):
    """Step0 appends a nucleus star and Step1 an ambiguity asterisk. Either
    widens the ONE name column for the whole list; neither moves one row's
    swatch relative to another's."""
    host, dock, panel = _both(app)
    try:
        before = {cid: r.swatch.x() for cid, r in panel._rows.items()}
        panel._rows["CD3"].name_label.setText("CD3 *")
        template.uniform_name_width(list(panel._rows.values()))
        app.processEvents()
        after = {cid: r.swatch.x() for cid, r in panel._rows.items()}
        assert before == after, "a name marker moved the swatch column"
        widths = {r.name_label.width() for r in panel._rows.values()}
        assert len(widths) == 1
    finally:
        host.close()


# ── 3. the core cannot be replaced ───────────────────────────────────────

def test_a_row_that_did_not_come_from_the_template_is_refused(app):
    """`row_factory` used to be an escape hatch: a host could return any
    widget, so "one shell" did not mean one row."""
    from block01.ui.widgets.channel_dock import ChannelDock, ChannelSetModel
    from block01.ui.widgets.channel_dock import ChannelState

    model = ChannelSetModel()
    model.set_channels([ChannelState(channel_id="CD3")])

    class _Impostor(QtWidgets.QWidget):
        def __init__(self, _model, _cid, parent=None):
            super().__init__(parent)
            QtWidgets.QHBoxLayout(self).addWidget(QtWidgets.QCheckBox())

    with pytest.raises(TypeError):
        ChannelDock(model, row_factory=_Impostor, title="")


def test_the_accessory_cannot_move_the_common_columns(app):
    """A host's accessory is appended after the name; whatever it is, the
    checkbox, state slot, swatch and name stay where the template put them."""
    from block01.ui.widgets.channel_dock import (
        ChannelDock, ChannelSetModel, ChannelState, ChannelRowBase)

    class _FatAccessory(ChannelRowBase):
        def __init__(self, model, cid, parent=None):
            super().__init__(model, cid, parent)
            big = QtWidgets.QPushButton("wide accessory")
            big.setMinimumWidth(300)
            self._extras_layout.addWidget(big)

    model = ChannelSetModel()
    model.set_channels([ChannelState(channel_id="CD3", name="CD3")])
    plain = ChannelDock(model, row_factory=ChannelRowBase, title="")
    fat = ChannelDock(model, row_factory=_FatAccessory, title="")
    for d in (plain, fat):
        d.resize(420, 200)
        d.show()
    app.processEvents()
    try:
        a, b = plain.row("CD3"), fat.row("CD3")
        assert a.checkbox.x() == b.checkbox.x()
        assert a.state_slot.x() == b.state_slot.x()
        assert a.swatch.x() == b.swatch.x()
        assert a.name_label.x() == b.name_label.x()
    finally:
        plain.close()
        fat.close()


# ── 4. one palette ───────────────────────────────────────────────────────

def test_a_standalone_step1_panel_uses_the_shared_palette(app):
    """No shared display state registered: the panel still deals the process
    palette, not a private one of its own."""
    from block01.ui.step0.config_panel import ConfigPanel

    panel = ConfigPanel(["DAPI", "CD3", "CD8"], fusion=FusionDomainModel(),
                        private_list=True)
    try:
        assert panel._display_state is None, "the test needs a standalone panel"
        for i, ch in enumerate(["DAPI", "CD3", "CD8"]):
            assert panel.channel_color(ch).lower() == \
                   template.palette_color(i).lower(), ch
    finally:
        panel.deleteLater()


def test_the_two_lists_show_one_colour_for_one_channel(app):
    host, dock, panel = _both(app)
    try:
        dock._model.set_color("CD3", "#123456")
        panel.set_channel_color("CD3", "#123456")
        app.processEvents()
        assert dock.row("CD3").swatch.styleSheet() == \
               panel._rows["CD3"].swatch.styleSheet()
    finally:
        host.close()


# ── 5. the outer frame ───────────────────────────────────────────────────

def test_both_channels_frames_come_from_one_token(app):
    from block01.ui.step0 import step0_page as sp
    assert sp.Step0Page._box_style(template.COLOR_FRAME) == \
           template.frame_qss(template.COLOR_FRAME)


# ── 6. a rebuild keeps the list's own UI state ───────────────────────────

def test_a_rebuild_keeps_the_scroll_position(app):
    """Rebuilding is the same channels drawn again. Landing the user back at
    the top of a long list is not part of that."""
    names = [f"CH{i:02d}" for i in range(40)]
    host, dock, panel = _both(app, channels=tuple(names))
    try:
        dock.list_widget.verticalScrollBar().setValue(80)
        panel._list.verticalScrollBar().setValue(80)
        app.processEvents()

        dock.rebuild()
        panel._rebuild_rows()

        assert dock.list_widget.verticalScrollBar().value() == 80
        assert panel._list.verticalScrollBar().value() == 80
    finally:
        host.close()


def test_a_rebuild_keeps_the_selection(app):
    names = [f"CH{i:02d}" for i in range(40)]
    host, dock, panel = _both(app, channels=tuple(names))
    try:
        dock._model.select("CH20")
        panel.set_current_channel("CH20", auto_show=False)
        app.processEvents()

        dock.rebuild()
        panel._rebuild_rows()
        app.processEvents()

        assert dock._model.selected() == "CH20"
        assert dock.list_widget.currentItem() is dock.item("CH20")
        assert panel.current_channel() == "CH20"
    finally:
        host.close()


def test_a_rebuild_keeps_the_search_text_and_its_filter(app):
    host, dock, panel = _both(app, channels=("DAPI", "CD3", "CD8"))
    try:
        dock.search.setText("cd3")
        app.processEvents()
        assert dock.visible_row_ids() == ["CD3"]

        dock.rebuild()
        app.processEvents()

        assert dock.search.text() == "cd3"
        assert dock.visible_row_ids() == ["CD3"]
    finally:
        host.close()


def test_a_rebuild_does_not_announce_a_user_action(app):
    """Restoring a selection is not clicking: no visibility, weight or
    config-changed signal may come out of a repaint."""
    host, dock, panel = _both(app)
    try:
        panel.set_channel_visible("CD3", True)
        seen = []
        panel.visibility_changed.connect(
            lambda *a: seen.append(("visibility",) + a))
        panel.config_changed.connect(lambda: seen.append(("config",)))
        panel.current_channel_changed.connect(
            lambda c: seen.append(("current", c)))
        weight_before = panel.channel_weight("CD3")

        panel._rebuild_rows()
        app.processEvents()

        assert seen == [], seen
        assert panel.channel_weight("CD3") == weight_before
        assert panel.visible_channels() == ["CD3"]
    finally:
        host.close()


# ── 7. an empty list ─────────────────────────────────────────────────────

def test_an_empty_list_is_not_an_error(app):
    from block01.ui.widgets.channel_dock import ChannelDock, ChannelSetModel
    from block01.ui.step0.config_panel import ConfigPanel

    model = ChannelSetModel()
    dock = ChannelDock(model, row_factory=None, title="")
    panel = ConfigPanel([], fusion=FusionDomainModel(), private_list=True)
    try:
        assert dock.rows() == {}
        assert panel._rows == {}
        assert template.uniform_name_width([]) == 0
    finally:
        dock.deleteLater()
        panel.deleteLater()
