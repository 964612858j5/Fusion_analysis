"""Plan step 3: the shared patch strip (`ui/patch_strip.py`).

  * the look Step1 had before the move, letter for letter: the menu button's
    style sheet, the 42 x 22 buttons, the spacing, the menu's minimum width;
  * at most seven inline buttons, a menu action for every patch;
  * a click on a button or a menu action is `chosen(index)`;
  * the selection marks the button and the action; labels and styles per
    patch; no patches -> the menu button is disabled, "No patches yet";
  * `buttons` / `menu_actions` stay the SAME list objects across rebuilds
    (the window's old names are aliases of them).
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtWidgets  # noqa: E402

from block01.ui import patch_strip as ps  # noqa: E402

#: The menu button's style sheet as Step1 wrote it before the move.
OLD_MENU_QSS = ("QToolButton{color:#9bd0ff;background:#182230;"
                "border:1px solid #354a63;border-radius:3px;"
                "font-size:10px;font-weight:bold;padding:2px 8px;}"
                "QToolButton::menu-indicator{subcontrol-position:right center;"
                "subcontrol-origin:padding;left:-4px;}")


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def strip(app):
    made = ps.PatchStrip()
    made.seen = []
    made.chosen.connect(made.seen.append)
    return made


def test_the_look_is_step1s(strip):
    assert strip.menu_button.styleSheet() == OLD_MENU_QSS
    assert strip.menu_button.text() == "Patch" and strip.menu_button.height() == 22
    assert ps.STEP1_INLINE_PATCH_BUTTONS == 7
    assert (ps.STEP1_PATCH_BTN_W, ps.STEP1_PATCH_BTN_H, ps.STEP1_PATCH_BTN_SPACING) == (42, 22, 4)
    assert ps.step1_patch_menu_width() == 7 * 42 + 6 * 4
    assert strip.menu.minimumWidth() == ps.step1_patch_menu_width()
    assert strip.container.spacing() == 4
    strip.rebuild(3, lambda i: f"P{i + 1}")
    assert all(b.width() == 42 and b.height() == 22 and b.isCheckable() for b in strip.buttons)


def test_seven_inline_and_every_patch_in_the_menu(strip):
    buttons, actions = strip.buttons, strip.menu_actions
    strip.rebuild(20, lambda i: f"P{i + 1}")
    assert len(strip.buttons) == 7 and len(strip.menu_actions) == 20
    assert [a.text() for a in strip.menu_actions][-1] == "P20"
    assert strip.menu.minimumWidth() == ps.step1_patch_menu_width()
    strip.rebuild(2, lambda i: f"P{i + 1}")
    assert len(strip.buttons) == 2 and len(strip.menu_actions) == 2
    assert strip.buttons is buttons and strip.menu_actions is actions      # the same lists


def test_clicks_and_menu_actions_are_chosen(strip):
    strip.rebuild(10, lambda i: f"P{i + 1}")
    strip.buttons[3].click()
    strip.menu_actions[8].trigger()
    assert strip.seen == [3, 8]


def test_the_selection_labels_and_styles(strip):
    strip.rebuild(9, lambda i: f"P{i + 1}")
    strip.set_selected(8)
    assert [a.isChecked() for a in strip.menu_actions] == [False] * 8 + [True]
    assert not any(b.isChecked() for b in strip.buttons)
    strip.set_selected(2)
    assert [b.isChecked() for b in strip.buttons] == [False, False, True] + [False] * 4
    strip.set_entry(2, "Mark A ✓", "QPushButton{background:#123456;}")
    assert strip.buttons[2].text() == "Mark A ✓" and strip.menu_actions[2].text() == "Mark A ✓"
    assert strip.buttons[2].styleSheet() == "QPushButton{background:#123456;}"
    strip.set_entry(8, "P9 ⟳", "ignored for a menu-only patch")
    assert strip.menu_actions[8].text() == "P9 ⟳"


def test_no_patches(strip):
    strip.rebuild(0, lambda i: "")
    assert not strip.menu_button.isEnabled() and strip.menu_button.toolTip() == "No patches yet"
    strip.rebuild(1, lambda i: "P1")
    assert strip.menu_button.isEnabled() and strip.menu_button.toolTip() == "1 patch — click to choose"


def test_the_holder_puts_both_parts_in_one_widget(strip):
    holder = strip.holder()
    assert strip.holder() is holder
    assert strip.menu_button.parent() is holder
