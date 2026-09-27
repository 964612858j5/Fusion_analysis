"""Block 4c: Step3's mask row (`ui/step3_mask_bar.py`), controls only.

  * defaults: both masks shown, green / cyan, 75 %, width 1, Outline;
  * every control emits ONE change naming only its own field; the preset
    swatches and `Custom…` (the picker answered directly -- no modal box)
    set the colour; the colour swatch on the button follows;
  * the run list: empty -> one disabled item; the current directory is
    selected; choosing a run emits its directory;
  * a mask button can be disabled; the hint is cut to fit and shows its whole
    text on hover.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtGui, QtWidgets  # noqa: E402

from block01.ui import step3_mask_bar as mb  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def bar(app):
    made = mb.Step3MaskBar()
    changes = []
    made.style_changed.connect(lambda kind, change: changes.append((kind, change)))
    made.changes = changes
    return made


def test_the_defaults(bar):
    for kind, colour in (("cell", (0.0, 1.0, 0.0)), ("nucleus", (0.0, 0.8, 1.0))):
        panel = bar.buttons[kind].panel
        assert panel.show_box.isChecked()
        assert panel.colour == colour
        assert panel.opacity.value() == 75 and panel.width.value() == 1
        assert panel.width.minimum() == 1 and panel.width.maximum() == 4
        assert panel.outline.isChecked() and not panel.fill.isChecked()
        assert panel.fill_note.text() == mb.FILL_NOTE
    assert bar.buttons["cell"].text() == "Cell mask ▾"
    assert bar.buttons["nucleus"].text() == "Nucleus mask ▾"


def test_each_control_emits_one_change_of_its_own_field(bar):
    panel = bar.buttons["cell"].panel
    panel.show_box.setChecked(False)
    panel.opacity.setValue(40)
    panel.width.setValue(3)
    panel.fill.setChecked(True)
    panel.outline.setChecked(True)
    assert bar.changes == [("cell", {"visible": False}), ("cell", {"alpha": 0.4}),
                           ("cell", {"width": 3.0}), ("cell", {"mode": "fill"}),
                           ("cell", {"mode": "outline"})]
    assert panel.opacity_value.text() == "40 %"


def test_swatches_and_custom_set_the_colour(bar):
    button = bar.buttons["nucleus"]
    panel = button.panel
    rgb, swatch = panel.swatches[2]                      # yellow
    swatch.click()
    assert bar.changes[-1] == ("nucleus", {"color": rgb}) and panel.colour == rgb
    swatch.click()                                       # the same colour: nothing new
    assert len(bar.changes) == 1
    panel.pick_custom_colour = lambda initial: QtGui.QColor(10, 20, 30)
    panel.custom.click()
    kind, change = bar.changes[-1]
    assert kind == "nucleus" and change["color"] == pytest.approx((10 / 255, 20 / 255, 30 / 255))
    icon = button.icon().pixmap(10, 10).toImage()
    assert QtGui.QColor(icon.pixel(5, 5)).getRgb()[:3] == (10, 20, 30)
    panel.pick_custom_colour = lambda initial: QtGui.QColor()    # cancelled
    panel.custom.click()
    assert len(bar.changes) == 2


def test_the_run_list(bar):
    combo = bar.run_combo
    assert not combo.isEnabled() and combo.count() == 1 and combo.itemText(0) == mb.NO_RUNS_TEXT
    chosen = []
    bar.run_chosen.connect(chosen.append)
    bar.set_runs([("a · 2026-09-26 10:00", "/r/a"), ("b · 2026-09-25 10:00", "/r/b")], "/r/b")
    assert combo.isEnabled() and combo.currentData() == "/r/b"
    assert not chosen                                     # filling the list chooses nothing
    combo.activated.emit(0)
    assert chosen == ["/r/a"]
    bar.set_runs([])
    assert not combo.isEnabled() and bar.current_run_dir() == ""


def test_buttons_disable_and_the_hint_is_cut_with_its_whole_text_on_hover(bar):
    bar.set_mask_available("cell", False)
    assert not bar.buttons["cell"].isEnabled() and bar.buttons["nucleus"].isEnabled()
    long = "Zoomed out, masks cannot be shown: " + "a very long reason " * 10
    bar.hint.resize(120, 20)
    bar.set_hint(long)
    assert bar.hint.toolTip() == long and bar.hint.full_text() == long
    assert bar.hint.text().endswith("…") and len(bar.hint.text()) < len(long)
    bar.set_hint("")
    assert bar.hint.text() == ""
