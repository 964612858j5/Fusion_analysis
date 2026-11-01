"""One global exception hook (docs/v17_error_guard_plan.md §1).

Under PyQt5 5.15 an exception that escapes a slot, a timer or a paint while
Python's default `sys.excepthook` is in place ends the whole process. With
this hook it is logged and the process keeps running:

  * `sys.excepthook` and `threading.excepthook`: the traceback to stderr as
    before, and appended to `~/.block01/logs/errors.log` (rotated past 5 MB
    to `errors.log.1`); stderr only when the home folder cannot be written;
  * one non-modal notice per distinct error (type + innermost file and line)
    per session, never two at once. Both hooks only EMIT a queued signal of
    one GUI-owned object -- `sys.excepthook` also runs on worker threads (an
    exception escaping `QThread.run()`) -- and its slot, on the GUI thread,
    builds the box;
  * `KeyboardInterrupt` / `SystemExit`: Python's default.

Installed once by `main.py`'s `main()`; tests and scripts keep the default.
"""

import os
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

from PyQt5 import QtCore, QtWidgets
from PyQt5.QtCore import Qt

#: past this size the log is rotated (one old file kept)
LOG_LIMIT_BYTES = 5 * 1024 * 1024

_state = {"installed": False, "notifier": None}


def log_path():
    """Where errors are logged: `~/.block01/logs/errors.log`."""
    return Path.home() / ".block01" / "logs" / "errors.log"


class ErrorLog:
    """Appends tracebacks to one file, serialised, opened per entry."""

    def __init__(self, path, limit=LOG_LIMIT_BYTES):
        self.path = Path(path)
        self.limit = int(limit)
        self._lock = threading.Lock()

    def write(self, text):
        """True when `text` reached the file."""
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size >= self.limit:
                    os.replace(self.path, self.path.with_name(self.path.name + ".1"))
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(text)
                return True
            except OSError:
                return False


def signature(exc_type, tb):
    """(type name, innermost file, line): what makes two errors the same."""
    frames = traceback.extract_tb(tb) if tb is not None else []
    last = frames[-1] if frames else None
    return (getattr(exc_type, "__name__", str(exc_type)),
            last.filename if last else "", last.lineno if last else 0)


class Notifier(QtCore.QObject):
    """Lives on the GUI thread; shows the notices the hooks ask for."""

    requested = QtCore.pyqtSignal(object, str, bool)    # signature, text, logged

    def __init__(self, parent=None):
        super().__init__(parent)
        self._seen = set()
        self._box = None
        self._guard = threading.Lock()
        self.requested.connect(self._show, Qt.QueuedConnection)

    def ask(self, sig, text, logged):
        """Any thread: one notice per signature, never two at once."""
        with self._guard:
            if sig in self._seen or self._box is not None:
                return False
            self._seen.add(sig)
            self._box = "pending"
        self.requested.emit(sig, text, logged)
        return True

    def _show(self, sig, text, logged):
        try:
            where = (f"Details: {log_path()}" if logged
                     else "Details were printed in the terminal.")
            box = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Warning, "Block01",
                                        f"Something went wrong in Block01:\n\n{text}\n\n"
                                        "Block01 kept running. If something looks wrong, "
                                        f"save your work and restart.\n\n{where}")
            box.setWindowModality(Qt.NonModal)
            # owned by Python alone (no WA_DeleteOnClose: Qt deleting it too
            # would delete it twice); released after its `finished` returns
            box.finished.connect(self._closed)
            with self._guard:
                self._box = box
            box.show()
        except Exception:                                   # noqa: BLE001 -- never re-raised
            with self._guard:
                self._box = None
            traceback.print_exc()

    def _closed(self, *_):
        with self._guard:
            closing, self._box = self._box, None
        # not inside the box's own signal: dropped on the next turn
        self._closing = closing
        QtCore.QTimer.singleShot(0, self._release)

    def _release(self):
        self._closing = None


def handle(exc_type, exc, tb, *, log=None, notifier=None, thread_name=None):
    """Log one unhandled exception and ask for its notice. Never raises."""
    try:
        if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            sys.__excepthook__(exc_type, exc, tb)
            return
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        sys.stderr.write(text)
        where = f" in thread {thread_name}" if thread_name else ""
        stamp = datetime.now().isoformat(timespec="seconds")
        logged = (log or ErrorLog(log_path())).write(f"--- {stamp}{where}\n{text}\n")
        notifier = notifier if notifier is not None else _state["notifier"]
        if notifier is not None:
            notifier.ask(signature(exc_type, tb), f"{exc_type.__name__}: {exc}", logged)
    except Exception:                                       # noqa: BLE001 -- the hook never raises
        try:
            sys.__excepthook__(exc_type, exc, tb)
        except Exception:                                   # noqa: BLE001
            pass


def install(app):
    """Install both hooks once; the notifier is made here, on the GUI thread."""
    if _state["installed"]:
        return _state["notifier"]
    notifier = Notifier(app)
    log = ErrorLog(log_path())
    _state.update(installed=True, notifier=notifier, log=log)
    sys.excepthook = lambda t, e, tb: handle(t, e, tb, log=log, notifier=notifier)

    def thread_hook(args):
        handle(args.exc_type, args.exc_value, args.exc_traceback, log=log,
               notifier=notifier, thread_name=getattr(args.thread, "name", None))
    threading.excepthook = thread_hook
    return notifier
