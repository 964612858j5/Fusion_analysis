"""The patch selector strip, shared by Step1 and Step3 (plan step 3).

A `Patch` drop-down that lists EVERY patch, then at most
`STEP1_INLINE_PATCH_BUTTONS` inline buttons, each in its patch's own colour
(Step0's button look). Built from the window's one patch list on every
rebuild -- never a second patch model -- and it knows nothing of viewers:
a click is `chosen(index)`, and the window decides what that means.

Moved out of `ui/main_window.py` unchanged (sizes, widths, style sheets,
the menu's minimum width rule); the window keeps its old attribute names as
aliases of this object's widgets and lists.
"""

from PyQt5 import QtCore
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QMenu, QPushButton, QToolButton, QWidget

# The inline strip is capped: measured before the cap, twenty patches made
# the strip 916 px wide and put the last button's right edge at 1309 px, i.e.
# outside a 1000 px window entirely. Seven is what the row showed before, so
# nothing that fits today moves; patch eight onwards lives in the "Patch"
# menu, which is always there and always lists ALL of them.
STEP1_INLINE_PATCH_BUTTONS = 7

# The inline patch button's fixed size and the strip's spacing. The
# dropdown's width is derived from them, so they have to be one definition --
# a menu 250 px wide (Qt's default for twenty "P12" items, measured) next to a
# 318 px strip reads as a different control, not as the rest of the same row.
STEP1_PATCH_BTN_W = 42
STEP1_PATCH_BTN_H = 22
STEP1_PATCH_BTN_SPACING = 4

MENU_BUTTON_QSS = (
    "QToolButton{color:#9bd0ff;background:#182230;"
    "border:1px solid #354a63;border-radius:3px;"
    "font-size:10px;font-weight:bold;padding:2px 8px;}"
    "QToolButton::menu-indicator{subcontrol-position:right center;"
    "subcontrol-origin:padding;left:-4px;}")


def step1_patch_menu_width():
    """As wide as a full inline strip: seven buttons and their gaps."""
    n = STEP1_INLINE_PATCH_BUTTONS
    return n * STEP1_PATCH_BTN_W + (n - 1) * STEP1_PATCH_BTN_SPACING


class PatchStrip(QtCore.QObject):
    """The `Patch` menu button, the inline buttons and the menu's actions.

    `buttons` and `menu_actions` are the SAME list objects for the strip's
    whole life (they are refilled, never replaced), so an alias taken once
    stays true."""

    chosen = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.menu_button = QToolButton()
        self.menu_button.setText("Patch")
        self.menu_button.setPopupMode(QToolButton.InstantPopup)
        self.menu_button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.menu_button.setFixedHeight(STEP1_PATCH_BTN_H)
        self.menu_button.setStyleSheet(MENU_BUTTON_QSS)
        self.menu = QMenu(self.menu_button)
        # The dropdown is as wide as a full inline strip, so the list reads
        # as the continuation of the row it drops out of. A MINIMUM, not a
        # fixed width: a list taller than the screen is laid out by Qt in
        # columns, and a hard width then clips every column but the first.
        self.menu.setMinimumWidth(step1_patch_menu_width())
        self.menu_button.setMenu(self.menu)
        self.menu_actions = []
        self.buttons = []
        self.container = QHBoxLayout()
        self.container.setSpacing(STEP1_PATCH_BTN_SPACING)
        self._holder = None

    def holder(self):
        """The menu button and the inline buttons in one widget, for a row
        that takes widgets (Step3). Step1 lays the two parts out itself."""
        if self._holder is None:
            holder = QWidget()
            lay = QHBoxLayout(holder)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(STEP1_PATCH_BTN_SPACING)
            lay.addWidget(self.menu_button)
            lay.addLayout(self.container)
            self._holder = holder
        return self._holder

    def rebuild(self, count, label_of):
        """Inline buttons for the first patches, a menu action for every one."""
        for btn in self.buttons:
            self.container.removeWidget(btn)
            btn.deleteLater()
        self.buttons.clear()
        for i in range(min(count, STEP1_INLINE_PATCH_BUTTONS)):
            btn = QPushButton(label_of(i))
            btn.setCheckable(True)
            btn.setFixedSize(STEP1_PATCH_BTN_W, STEP1_PATCH_BTN_H)
            btn.clicked.connect(lambda _=False, idx=i: self.chosen.emit(idx))
            self.container.addWidget(btn)
            self.buttons.append(btn)
        self.rebuild_menu(count, label_of)

    def rebuild_menu(self, count, label_of):
        """One checkable menu action per patch, in patch order."""
        self.menu.clear()
        # `clear()` drops the actions, not the width -- restate it so the
        # dropdown cannot shrink back to whatever its longest label asks for.
        self.menu.setMinimumWidth(step1_patch_menu_width())
        self.menu_actions.clear()
        if count <= 0:
            self.menu_button.setEnabled(False)
            self.menu_button.setToolTip("No patches yet")
            return
        self.menu_button.setEnabled(True)
        self.menu_button.setToolTip(f"{count} patch{'es' if count != 1 else ''} — "
                                    "click to choose")
        for i in range(count):
            act = self.menu.addAction(label_of(i))
            act.setCheckable(True)
            # THE SAME ENTRY POINT the inline buttons use.
            act.triggered.connect(lambda _=False, idx=i: self.chosen.emit(idx))
            self.menu_actions.append(act)

    def set_selected(self, idx):
        """Show `idx` as the selection in the strip and in the menu."""
        for i, btn in enumerate(self.buttons):
            btn.setChecked(i == idx)
        for i, act in enumerate(self.menu_actions):
            act.setChecked(i == idx)

    def set_entry(self, idx, label, qss=None):
        """One patch's label (menu and inline) and, inline, its style."""
        if idx < len(self.menu_actions):
            self.menu_actions[idx].setText(label)
        if idx < len(self.buttons):
            self.buttons[idx].setText(label)
            if qss is not None:
                self.buttons[idx].setStyleSheet(qss)
