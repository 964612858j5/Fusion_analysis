"""One visual template for every channel row, in the ONE public dock.

Block01 drew channel rows in more than one place, and before B1 it BUILT them
in more than one place: the shared dock's `ChannelRowBase` and Step1's private
`ConfigPanel.ChannelRow` each made their own checkbox, swatch and name, with
their own stylesheets, and Step1 carried a second list stylesheet and a second
colour palette. Measured on a real window, the same channel had a 22x18 themed
checkbox in Step0 and a 14x15 platform one in Step1, its swatch at x=50
against x=25, its name at x=68 against x=43, and a row 25 px tall against 22.

B5 removed the second list; what this module pins is the same thing against
what is left: the COMMON columns come from `channel_dock.template` and measure
the same in every STEP -- the row is the same object, and only its accessory
changes -- while the list keeps its own UI state across a rebuild. It asserts
real widget geometry, not source text.

Own module, like the other page-heavy Step0/Step1 suites: combined runs
segfault in offscreen pyqtgraph.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets  # noqa: E402

from block01.core.fusion_domain import FusionDomainModel  # noqa: E402

from block01.ui.widgets.channel_dock import template  # noqa: E402
from block01.ui.widgets.channel_dock.global_dock import (  # noqa: E402
    GlobalChannelDock, STEP0, STEP1, STEP2, STEP3,
)


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _dock(app, channels=("DAPI", "CD3", "CD8"), fusion=None):
    """The real public dock, shown, over `channels`."""
    fusion = fusion if fusion is not None else FusionDomainModel()
    dock = GlobalChannelDock(None, fusion, title="")
    dock.set_channels(list(channels))
    dock.resize(460, 400)
    dock.show()
    app.processEvents()
    for row in dock.rows().values():
        row.layout().activate()
    return dock


def _geo(w):
    r = w.geometry()
    return (r.x(), r.y(), r.width(), r.height())


def _core(row):
    return (_geo(row.checkbox), _geo(row.state_slot), _geo(row.swatch),
            row.name_label.x(), row.name_label.width(), row.styleSheet())


# ── 1. the common columns measure the same in every step ─────────────────

def test_the_common_row_columns_are_identical_in_every_step(app):
    dock = _dock(app)
    try:
        seen = {}
        for step in (STEP0, STEP1, STEP2, STEP3):
            dock.set_step(step)
            app.processEvents()
            row = dock.row("CD3")
            row.layout().activate()
            seen[step] = _core(row)
            assert row.height() == template.ROW_CONTENT_HEIGHT or \
                row.height() <= template.ROW_HEIGHT
            assert dock.item("CD3").sizeHint().height() == template.ROW_HEIGHT
            assert row.layout().getContentsMargins() == template.ROW_MARGINS
            assert row.layout().spacing() == template.ROW_SPACING
        assert len(set(seen.values())) == 1, seen
    finally:
        dock.close()


def test_the_checkbox_indicator_is_the_template(app):
    dock = _dock(app)
    try:
        for row in dock.rows().values():
            assert row.checkbox.size().width() == template.CHECKBOX_SIZE[0]
            assert row.checkbox.size().height() == template.CHECKBOX_SIZE[1]
            assert template.CHECKBOX_INDICATOR_QSS in row.styleSheet()
    finally:
        dock.close()


def test_the_state_slot_keeps_its_width_when_it_is_empty(app):
    """Step1, Step2 and Step3 draw nothing in it. If it collapsed, the swatch
    and the name would sit at a different x than in Step0 -- which is the
    difference this template removes."""
    dock = _dock(app)
    try:
        for step in (STEP1, STEP2, STEP3):
            dock.set_step(step)
            app.processEvents()
            slot = dock.row("CD3").state_slot
            assert slot.text() == ""
            assert slot.toolTip() == ""
            assert slot.width() == template.STATE_SLOT_WIDTH
        dock.set_step(STEP0)
        app.processEvents()
        assert dock.row("CD3").state_slot.width() == template.STATE_SLOT_WIDTH
    finally:
        dock.close()


def test_the_list_uses_the_template_stylesheet_and_scroll_policy(app):
    from PyQt5.QtCore import Qt

    dock = _dock(app)
    try:
        assert dock.list_widget.styleSheet() == template.LIST_QSS
        assert dock.list_widget.horizontalScrollBarPolicy() == \
            Qt.ScrollBarAlwaysOff
    finally:
        dock.close()


def test_a_selected_row_under_the_cursor_stays_selected_coloured(app):
    """Step1's old list stylesheet had no `item:selected:hover` rule, so a
    selected row under the mouse turned the green hover colour while Step0's
    stayed blue."""
    dock = _dock(app)
    try:
        sheet = dock.list_widget.styleSheet()
        assert "item:selected:hover" in sheet
        rule = sheet.split("item:selected:hover")[1]
        assert template.COLOR_SELECTED in rule.split("}")[0]
        assert template.COLOR_HOVER not in rule.split("}")[0]
    finally:
        dock.close()


# ── 2. the name column ───────────────────────────────────────────────────

def test_the_name_column_is_the_longest_name_in_the_list(app):
    dock = _dock(app, channels=("DAPI", "CD3", "A_VERY_LONG_NAME"))
    try:
        widths = {r.name_label.width() for r in dock.rows().values()}
        assert len(widths) == 1, f"name column is not uniform: {widths}"
    finally:
        dock.close()


def test_a_marker_on_one_name_does_not_shift_another_rows_columns(app):
    """Step0 appends a nucleus star and Step1 an ambiguity asterisk. Either
    widens the ONE name column for the whole list; neither moves one row's
    swatch relative to another's."""
    dock = _dock(app)
    try:
        before = {cid: r.swatch.x() for cid, r in dock.rows().items()}
        dock.row("CD3").set_name("CD3", mixed_values=(0.2, 0.7))
        template.uniform_name_width(list(dock.rows().values()))
        app.processEvents()
        after = {cid: r.swatch.x() for cid, r in dock.rows().items()}
        assert before == after, "a name marker moved the swatch column"
        widths = {r.name_label.width() for r in dock.rows().values()}
        assert len(widths) == 1
    finally:
        dock.close()


# ── 3. the core cannot be replaced ───────────────────────────────────────

def test_a_row_that_did_not_come_from_the_template_is_refused(app):
    """The template stamps what it builds, and the guard refuses anything
    else. `row_factory` used to be an escape hatch: a host could return any
    widget, so "one shell" did not mean one row."""
    impostor = QtWidgets.QWidget()
    QtWidgets.QHBoxLayout(impostor).addWidget(QtWidgets.QCheckBox())

    assert template.is_template_row(impostor) is False
    with pytest.raises(TypeError):
        template.require_template_row(impostor)

    from block01.ui.widgets.channel_dock.global_dock import GlobalChannelRow
    real = GlobalChannelRow("CD3")
    assert template.is_template_row(real) is True
    assert template.require_template_row(real) is real
    impostor.deleteLater()
    real.deleteLater()


def test_the_accessory_cannot_move_the_common_columns(app):
    """The step accessory is appended after the name; whatever it is, the
    checkbox, state slot, swatch and name stay where the template put them --
    which is what `set_step` must not disturb."""
    dock = _dock(app)
    try:
        rows = list(dock.rows().values())
        columns = []
        for step in (STEP0, STEP1, STEP2, STEP3):
            dock.set_step(step)
            app.processEvents()
            for r in rows:
                r.layout().activate()
            columns.append([(r.checkbox.x(), r.state_slot.x(), r.swatch.x(),
                             r.name_label.x()) for r in rows])
        assert len({tuple(c) for c in columns}) == 1, columns
    finally:
        dock.close()


# ── 4. one palette ───────────────────────────────────────────────────────

def test_a_standalone_step1_panel_uses_the_shared_palette(app):
    """No shared display state registered: the panel still answers with the
    process palette, not a private one of its own."""
    from block01.ui.step0.config_panel import ConfigPanel

    panel = ConfigPanel(["DAPI", "CD3", "CD8"], fusion=FusionDomainModel())
    try:
        assert panel._display_state is None, "the test needs a standalone panel"
        for i, ch in enumerate(["DAPI", "CD3", "CD8"]):
            assert panel.channel_color(ch).lower() == \
                   template.palette_color(i).lower(), ch
    finally:
        panel.deleteLater()


def test_one_colour_reaches_the_row_in_every_step(app):
    from block01.ui.block01_display import Block01DisplayServices
    from block01.core import display_identity

    services = Block01DisplayServices()
    services.state.bind(
        display_identity.DatasetIdentity("/tmp/template.ome.tiff", "1:1"))
    dock = GlobalChannelDock(services.state, services.fusion, title="")
    dock.set_channels(["DAPI", "CD3", "CD8"])
    dock.resize(460, 300)
    dock.show()
    app.processEvents()
    try:
        services.state.set_color("CD3", "#123456", origin="test")
        expected = template.swatch_qss("#123456")
        for step in (STEP0, STEP1, STEP2, STEP3):
            dock.set_step(step)
            app.processEvents()
            assert dock.row("CD3").swatch.styleSheet() == expected, step
    finally:
        dock.close()
        services.shutdown("test")


# ── 5. the outer frame ───────────────────────────────────────────────────

def test_the_channels_frame_comes_from_one_token(app):
    from block01.ui.step0 import step0_page as sp
    assert sp.Step0Page._box_style(template.COLOR_FRAME) == \
           template.frame_qss(template.COLOR_FRAME)


# ── 6. a rebuild keeps the list's own UI state ───────────────────────────

def test_a_rebuild_keeps_the_scroll_position(app):
    """Rebuilding is the same channels drawn again. Landing the user back at
    the top of a long list is not part of that."""
    names = [f"CH{i:02d}" for i in range(40)]
    dock = _dock(app, channels=tuple(names))
    try:
        dock.list_widget.verticalScrollBar().setValue(80)
        app.processEvents()

        dock.set_channels(names, force=True)
        app.processEvents()

        assert dock.list_widget.verticalScrollBar().value() == 80
    finally:
        dock.close()


def test_a_rebuild_keeps_the_search_text_and_its_filter(app):
    dock = _dock(app, channels=("DAPI", "CD3", "CD8"))
    try:
        dock.search.setText("cd3")
        app.processEvents()
        assert dock.visible_row_ids() == ["CD3"]

        dock.set_channels(["DAPI", "CD3", "CD8"], force=True)
        app.processEvents()

        assert dock.search.text() == "cd3"
        assert dock.visible_row_ids() == ["CD3"]
    finally:
        dock.close()


def test_a_rebuild_does_not_announce_a_user_action(app):
    """Drawing the same channels again is not clicking: no visibility,
    weight, participation or colour command may come out of a repaint."""
    from block01.ui.block01_display import Block01DisplayServices
    from block01.core import display_identity

    services = Block01DisplayServices()
    state, fusion = services.state, services.fusion
    state.bind(display_identity.DatasetIdentity("/tmp/rebuild.ome.tiff", "1:1"))
    fusion.add_group("markers", {"CD3": 0.4})
    dock = GlobalChannelDock(state, fusion, title="")
    dock.set_channels(["DAPI", "CD3", "CD8"])
    dock.set_step(STEP1)
    state.set_display_visible("CD3", True, origin="test")
    try:
        seen = []
        state.visibility_changed.connect(lambda c, v: seen.append(("vis", c, v)))
        state.color_changed.connect(lambda c, h: seen.append(("col", c)))
        state.selection_changed.connect(lambda c: seen.append(("sel", c)))
        fusion.weight_changed.connect(lambda c: seen.append(("weight", c)))
        fusion.participation_changed.connect(
            lambda c, e: seen.append(("fusion", c, e)))
        revision = fusion.draft_revision()

        dock.set_channels(["DAPI", "CD3", "CD8"], force=True)
        dock.refresh()
        app.processEvents()

        assert seen == [], seen
        assert fusion.draft_revision() == revision
        assert fusion.channel_weight("CD3") == pytest.approx(0.4)
        assert state.display_visible("CD3") is True
    finally:
        dock.deleteLater()
        services.shutdown("test")


# ── 7. an empty list ─────────────────────────────────────────────────────

def test_an_empty_list_is_not_an_error(app):
    from block01.ui.step0.config_panel import ConfigPanel

    dock = GlobalChannelDock(None, FusionDomainModel(), title="")
    dock.set_channels([])
    panel = ConfigPanel([], fusion=FusionDomainModel())
    try:
        assert dock.rows() == {}
        assert panel._rows == {}
        assert template.uniform_name_width([]) == 0
    finally:
        dock.deleteLater()
        panel.deleteLater()
