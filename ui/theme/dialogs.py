"""Dialogs in the theme (docs/v17_desktop_ui_plan.md §10, ruling 5).

Every QMessageBox already takes the theme from the application sheet: the
default button is the primary one, the others outlined, and the platform
decides the order. This helper is for the few choices that DESTROY
something: the destructive button is red (`ds=danger`) and is NEVER the
default -- Enter or Esc keep things as they are.
"""

from ...qt import QtWidgets


def confirm_danger(parent, title, text, action="Delete", cancel="Cancel"):
    """Ask before a destructive action; True only when `action` is clicked."""
    box = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Warning, title, text,
                                QtWidgets.QMessageBox.NoButton, parent)
    go = box.addButton(action, QtWidgets.QMessageBox.DestructiveRole)
    keep = box.addButton(cancel, QtWidgets.QMessageBox.RejectRole)
    go.setProperty("ds", "danger")
    box.setDefaultButton(keep)
    box.setEscapeButton(keep)
    box.exec()
    return box.clickedButton() is go
