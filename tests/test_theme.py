"""The theme layer (docs/v17_desktop_ui_plan.md phase 1): scoped by role,
dark by default, geometry untouched; and the nucleus channel found by name
now that the panel file is gone."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from block01.qt import QtWidgets  # noqa: E402

from block01.ui import theme  # noqa: E402
from block01.ui.step0.step0_page import _nucleus_channel_of  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_dark_is_the_default_and_light_is_chosen_by_name(app, monkeypatch):
    monkeypatch.delenv("BLOCK01_THEME", raising=False)
    monkeypatch.setattr(theme.QtCore, "QSettings", lambda *a: type("S", (), {"value": lambda *a: ""})())
    assert theme.current_name() == "dark"
    monkeypatch.setenv("BLOCK01_THEME", "light")
    assert theme.current_name() == "light"
    monkeypatch.setenv("BLOCK01_THEME", "nonsense")
    assert theme.current_name() == "dark"


def test_the_sheet_has_no_rule_for_a_bare_widget():
    """Viewer canvases, GL widgets and overlays are never matched."""
    import re
    for t in theme.tokens.THEMES.values():
        selectors = [m.strip() for m in re.findall(r"([^{}]+)\{", theme.stylesheet(t))]
        assert selectors
        for sel in selectors:
            assert "[ds=" in sel or sel.startswith(
                ("QToolTip", "QPushButton", "QToolButton", "QTabBar", "QMessageBox")), sel


def test_menus_and_scroll_bars_keep_their_metrics(app):
    """Codex review: a sheet rule on them changed their sizes."""
    bar = QtWidgets.QScrollBar()
    before = bar.sizeHint()
    old = app.styleSheet()
    theme.apply(app, "dark")
    try:
        assert QtWidgets.QScrollBar().sizeHint() == before
        assert "QMenu" not in app.styleSheet() and "QScrollBar" not in app.styleSheet()
    finally:
        app.setStyleSheet(old)


def test_a_flag_change_is_repolished(app):
    lbl = theme.role(QtWidgets.QLabel("Step"), "step", on="0")
    theme.set_flag(lbl, "on", "1")
    assert lbl.property("on") == "1" and lbl.property("ds") == "step"


@pytest.mark.parametrize("names,want", [
    (["DAPI", "CD3"], "DAPI"),
    (["CD3", "DAPI_1"], "DAPI_1"),
    (["CD3", "dapi-01", "DAPI"], "DAPI"),
    (["CD3", "Hoechst 33342"], "Hoechst 33342"),
    (["CD3", "CD8"], None),
])
def test_the_nucleus_channel_is_found_by_its_name(names, want):
    assert _nucleus_channel_of(names) == want


def test_a_problem_of_the_hidden_load_line_is_said_in_a_box(app):
    """User ruling 2026-11-03: the Load row's status line is not shown; a
    problem it carried (⚠) opens a non-modal box, one at a time."""
    from block01.ui.step0.step0_page import Step0Page
    page = Step0Page()
    try:
        assert not page._load_status.isVisibleTo(page)
        page._set_geometry_status("Patch geometry unchanged.")
        assert page.__dict__.get("_load_row_box") is None
        page._set_geometry_status("⚠ ROI changed. Run Step0 Save.")
        box = page._load_row_box
        assert box.isVisible() and box.text() == "ROI changed. Run Step0 Save."
        page._set_geometry_status("⚠ Another problem.")
        assert page._load_row_box is box and box.text() == "Another problem."
        box.close()
    finally:
        page.close()


def test_a_destructive_choice_is_red_and_never_the_default(app, monkeypatch):
    """Ruling 5: Delete is the danger button; Cancel is the default and the
    escape button, so Enter or Esc never delete."""
    from block01.ui.theme import dialogs
    seen = {}

    def fake_exec(box):
        buttons = {b.text(): b for b in box.buttons()}
        seen["default"] = box.defaultButton().text()
        seen["escape"] = box.escapeButton().text()
        seen["danger"] = buttons["Delete"].property("ds")
        box._clicked = buttons["Delete"]
        return 0
    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", fake_exec)
    monkeypatch.setattr(QtWidgets.QMessageBox, "clickedButton", lambda box: box._clicked)
    assert dialogs.confirm_danger(None, "Delete", "Delete 2 patches?") is True
    assert seen == {"default": "Cancel", "escape": "Cancel", "danger": "danger"}


def test_a_role_beats_the_default_button_look(app):
    """The default look for unstyled buttons comes FIRST in the sheet: a role
    rule of the same specificity (later) wins -- Load and Save stay primary."""
    sheet = theme.stylesheet(theme.tokens.THEMES["dark"])
    assert sheet.index("QPushButton:!flat {") < sheet.index('QPushButton[ds="primary"]')
    assert sheet.index("QToolButton {") < sheet.index('QToolButton[ds="split"]')
