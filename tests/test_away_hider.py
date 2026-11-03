"""Always-on-top windows step aside while the user is in another program
(`ui/widgets/away_hider.py`, user ruling 2026-10-31).

  * the application inactive for a moment -> visible windows hide; active
    again -> they come back, without taking the focus;
  * a minimized or closed window is left alone and does not come back;
  * not while a system file dialog is up; a message box keeps only its
    owner;
  * `allowed()` false on return -> it stays hidden (Step3's window off Step3);
  * `forget_hidden` -> someone else's hide wins.

Offscreen Qt: the state change is handed to the hider directly.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtWidgets  # noqa: E402
from PyQt5.QtCore import Qt  # noqa: E402

from block01.ui.widgets import away_hider as ah  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def rig(app, monkeypatch):
    made = []

    def window(allowed=None):
        win = QtWidgets.QWidget(None, Qt.Window | Qt.WindowStaysOnTopHint)
        hider = ah.hide_when_away(win, allowed)
        win.show()
        made.append(win)
        return win, hider
    state = {"active": True}
    monkeypatch.setattr(ah._AwayHider, "_app_active", lambda self: state["active"])
    yield window, state
    for win in made:
        win.close()
        win.deleteLater()


def _away(hider, state):
    state["active"] = False
    hider._on_state(Qt.ApplicationInactive)
    assert hider._timer.isActive()
    hider._timer.stop()
    hider._step_aside()


def _back(hider, state):
    state["active"] = True
    hider._on_state(Qt.ApplicationActive)


def test_hides_away_and_comes_back_without_the_focus(rig):
    window, state = rig
    win, hider = window()
    other, _ = window()
    other.close()                                   # closed: left alone
    _away(hider, state)
    assert not win.isVisible() and hider.hidden_by_us(win)
    _back(hider, state)
    assert win.isVisible() and not hider.hidden_by_us(win)
    assert not win.testAttribute(Qt.WA_ShowWithoutActivating), "the attribute was left on"
    assert not other.isVisible()


def test_focus_passing_between_own_windows_does_not_hide(rig):
    window, state = rig
    win, hider = window()
    hider._on_state(Qt.ApplicationInactive)          # a blip ...
    hider._on_state(Qt.ApplicationActive)            # ... back before the settle time
    assert not hider._timer.isActive() and win.isVisible()


def test_minimized_left_alone(rig):
    window, state = rig
    win, hider = window()
    win.showMinimized()
    _away(hider, state)
    assert not hider.hidden_by_us(win)


def test_not_while_a_native_file_dialog_is_up(rig, monkeypatch):
    window, state = rig
    win, hider = window()
    dialog = QtWidgets.QFileDialog()
    monkeypatch.setattr(QtWidgets.QApplication, "activeModalWidget", staticmethod(lambda: dialog))
    _away(hider, state)
    assert win.isVisible() and not hider.hidden_by_us(win)
    dialog.setOption(QtWidgets.QFileDialog.DontUseNativeDialog, True)   # a Qt one: hides
    _away(hider, state)
    assert not win.isVisible()


def test_a_message_box_keeps_only_its_owner(rig, monkeypatch):
    window, state = rig
    owner, hider = window()
    other, _ = window()
    box = QtWidgets.QMessageBox(owner)
    monkeypatch.setattr(QtWidgets.QApplication, "activeModalWidget", staticmethod(lambda: box))
    _away(hider, state)
    assert owner.isVisible() and not other.isVisible()


def test_allowed_and_forget(rig):
    window, state = rig
    gate = {"ok": True}
    win, hider = window(allowed=lambda: gate["ok"])
    _away(hider, state)
    gate["ok"] = False                               # e.g. no longer on Step3
    _back(hider, state)
    assert not win.isVisible()
    gate["ok"] = True
    win.show()
    _away(hider, state)
    hider.forget_hidden(win)
    _back(hider, state)
    assert not win.isVisible()


def test_a_window_being_dragged_is_not_hidden(rig):
    """Acceptance 2026-11-03: dragging a window by its title bar makes the
    application inactive for the drag; hiding it then and showing it again
    afterwards put it back where the drag started. A window the window
    manager moved within `MOVING_MS` stays, and the check asks again."""
    import time
    window, state = rig
    win, hider = window()
    hider._last_move = time.monotonic()              # the window manager is moving it
    _away(hider, state)
    assert win.isVisible() and not hider.hidden_by_us(win)
    assert hider._timer.isActive()                   # asked again
    hider._timer.stop()
    hider._last_move = time.monotonic() - 10.0       # the drag is long over
    hider._step_aside()
    assert not win.isVisible() and hider.hidden_by_us(win)
    _back(hider, state)
