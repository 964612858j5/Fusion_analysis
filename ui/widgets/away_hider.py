"""Block01's always-on-top windows step aside while the user is in another
program (user ruling 2026-10-31).

Tissue Preview, Intensity and Step3's results window stay on top of
everything (X11 gives a plain Qt.Window no transient-for, so "on top of the
main window" means on top of all windows). Without this, Alt-Tab to a browser
left them covering it. `hide_when_away(win)` hides such a window when the
application stops being the active one and shows it again, without taking
the focus, when the user comes back -- only the windows it hid itself, and
only if `allowed()` still says so (Step3's window is hidden off Step3).

Not while a system (native) file dialog is up: it can make Qt report the
application inactive while the user is still choosing. A Qt message box does
not stop the others hiding on a real switch; only the window that owns it
stays, so the box keeps its parent. The check
waits a moment so focus passing between Block01's own windows (briefly
"inactive" on X11) does not flicker them.

Not while one of these windows is being MOVED: dragging a window by its
title bar makes the window manager take the pointer, and Qt reports the
application inactive for the length of the drag. Hiding then and showing it
again when the drag ended put the window back where the drag started
(acceptance 2026-11-03: the Tissue Preview could not be moved). A window that
moved within the last `MOVING_MS` is being dragged; the check waits again.
"""

import time

from PyQt5 import QtCore, QtWidgets
from PyQt5.QtCore import Qt

#: how long the application must stay inactive before the windows hide
SETTLE_MS = 250
#: a window that moved this recently is being dragged, not left behind
MOVING_MS = 400


class _AwayHider(QtCore.QObject):
    def __init__(self, app):
        super().__init__(app)
        self._app = app
        self._windows = {}           # id(win) -> [win, allowed, hidden by us]
        self._last_move = 0.0        # monotonic time a managed window last moved
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(SETTLE_MS)
        self._timer.timeout.connect(self._step_aside)
        app.applicationStateChanged.connect(self._on_state)

    def add(self, win, allowed=None):
        key = id(win)
        if key in self._windows:
            if allowed is not None:
                self._windows[key][1] = allowed
            return
        self._windows[key] = [win, allowed, False]
        win.installEventFilter(self)
        win.destroyed.connect(lambda _o=None, k=key: self._windows.pop(k, None))

    def eventFilter(self, obj, event):                      # noqa: N802
        # spontaneous: the window manager moved it (a drag), not the program
        if (event.type() == QtCore.QEvent.Move and event.spontaneous()
                and id(obj) in self._windows):
            self._last_move = time.monotonic()
        return False

    def _on_state(self, state):
        if state == Qt.ApplicationActive:
            self._timer.stop()
            self._come_back()
        else:
            self._timer.start()

    def _app_active(self):
        return self._app.applicationState() == Qt.ApplicationActive

    def _step_aside(self):
        if self._app_active():
            return
        if (time.monotonic() - self._last_move) * 1000.0 < MOVING_MS:
            self._timer.start()      # a window is being dragged: ask again
            return
        modal = QtWidgets.QApplication.activeModalWidget()
        if _native_file_dialog(modal):
            return                   # the state is unreliable under it
        for entry in self._windows.values():
            win = entry[0]
            if _owns(win, modal):
                continue             # a box of its own is up: keep its owner
            if win.isVisible() and not win.isMinimized():
                entry[2] = True
                win.hide()

    def _come_back(self):
        for entry in self._windows.values():
            win, allowed, hidden = entry
            if not hidden:
                continue
            entry[2] = False
            if allowed is not None and not allowed():
                continue
            quiet = win.testAttribute(Qt.WA_ShowWithoutActivating)
            win.setAttribute(Qt.WA_ShowWithoutActivating, True)
            win.show()
            win.setAttribute(Qt.WA_ShowWithoutActivating, quiet)

    def hidden_by_us(self, win):
        entry = self._windows.get(id(win))
        return bool(entry and entry[2])

    def forget_hidden(self, win):
        """Someone else decided `win` stays hidden: do not bring it back."""
        entry = self._windows.get(id(win))
        if entry:
            entry[2] = False


def _native_file_dialog(modal):
    return (isinstance(modal, QtWidgets.QFileDialog)
            and not modal.testOption(QtWidgets.QFileDialog.DontUseNativeDialog))


def _owns(win, modal):
    """`modal` is `win` or sits under it, across window boundaries."""
    while modal is not None:
        if modal is win:
            return True
        modal = modal.parentWidget()
    return False


def _hider():
    app = QtWidgets.QApplication.instance()
    if app is None:
        return None
    made = app.findChild(_AwayHider)
    return made if made is not None else _AwayHider(app)


def hide_when_away(win, allowed=None):
    """`win` hides while the user is in another program; see the module."""
    hider = _hider()
    if hider is not None:
        hider.add(win, allowed)
    return hider
