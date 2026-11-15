"""User ruling 2026-11-15 (Windows acceptance, choice B): a Step0 Save
question holds the main window, not the floating Tissue Navigator.

On the laptop the navigator could be neither minimized nor closed while
"Analysis region changed" was open: a modal box blocks every child window of
the main window, and the navigator is one. Now the question is not modal:
the main window's content is disabled and its close refused until the answer,
the navigator's ROI/patch edits are off meanwhile (Save publishes the
geometry on screen), and everything comes back after.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("block01.qt")

from block01.qt import QtCore, QtWidgets  # noqa: E402
from block01.qt.QtCore import Qt  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def window(app, monkeypatch):
    for name in ("information", "critical", "warning"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name, staticmethod(lambda *a, **k: None))
    from block01.ui.main_window import MainWindow
    w = MainWindow()
    w.show()
    app.processEvents()
    yield w
    w.hide()
    w.deleteLater()
    app.processEvents()


def _answer_after(app, check, act):
    """While the question is up: run `check(box)`, then `act(box)`."""
    seen = {}

    def poke():
        box = next(b for b in app.topLevelWidgets()
                   if isinstance(b, QtWidgets.QMessageBox) and b.isVisible())
        seen.update(check(box) or {})
        act(box)
    QtCore.QTimer.singleShot(50, poke)
    return seen


def test_the_region_question_leaves_a_child_window_usable(app, window):
    page = window._step0
    nav = QtWidgets.QWidget(window, Qt.Window)          # the navigator's kind of window
    nav.show()
    before = page.navigator_edit_policy()

    def check(box):
        close = QtCore.QEvent(QtCore.QEvent.Close)
        QtWidgets.QApplication.sendEvent(window, close)
        return {"modal": box.isModal() or box.windowModality() != Qt.NonModal,
                "nav": nav.isEnabled(),
                "central": window.centralWidget().isEnabled(),
                "close_refused": not close.isAccepted() and window.isVisible(),
                "policy": page.navigator_edit_policy()}

    seen = _answer_after(app, check, lambda box: next(
        b for b in box.buttons() if b.text() == "Overwrite this workspace").click())
    assert page._ask_region_changed() == "overwrite"
    assert seen["modal"] is False and seen["nav"] is True
    assert seen["central"] is False and seen["close_refused"] is True
    assert seen["policy"] == {"roi_policy": "read_only", "patch_editable": False}
    # everything back after the answer
    assert window.centralWidget().isEnabled() and page.navigator_edit_policy() == before
    nav.deleteLater()


@pytest.mark.parametrize("button, want", [(QtWidgets.QMessageBox.Ok, True),
                                          (QtWidgets.QMessageBox.Cancel, False)])
def test_the_raw_channels_question_answers_the_same_way(app, window, monkeypatch, button, want):
    page = window._step0
    monkeypatch.setattr(page, "_raw_save_channels", lambda: ["CD3", "CD8"])
    _answer_after(app, lambda box: None, lambda box: box.button(button).click())
    assert page._confirm_raw_channels() is want
    assert window.centralWidget().isEnabled()


def test_a_question_closed_unanswered_is_a_cancel(app, window):
    page = window._step0
    _answer_after(app, lambda box: None, lambda box: box.close())
    assert page._ask_region_changed() == "cancel"
    assert window.centralWidget().isEnabled()
