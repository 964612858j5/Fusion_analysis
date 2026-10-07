"""Block A9 (first Step1 entry): which Qt event the GUI thread was busy in.

The GUI watchdog showed the unexplained part of the first Step1 entry with
no Python frame on the stack -- the thread was inside `app.exec_()`, in Qt
or the driver. Python spans cannot split that. This times event DELIVERY
instead: an application whose `notify` records every dispatch that took
longer than a threshold, with its receiver and event type, as a
`qt.dispatch` mark (dur_ms, t_begin, depth). Nested dispatches are recorded
with their depth, so a slow paint inside a slow show is attributed to both.

Measurement only. Used only when BOTH `BLOCK01_PERF=1` and
`BLOCK01_PERF_DISPATCH=1` are set -- a Python `notify` runs for every event
in the program, so it is a separate switch from ordinary tracing and an
A9 run that compares numbers leaves it off.

    BLOCK01_PERF_DISPATCH=1           turn it on
    BLOCK01_PERF_DISPATCH_MS=<ms>     threshold, default 15
"""

import os
import threading
import time

from PyQt5.QtCore import QEvent
from PyQt5.QtWidgets import QApplication

from . import perf_trace

DISPATCH_ENV = "BLOCK01_PERF_DISPATCH"
DISPATCH_MS_ENV = "BLOCK01_PERF_DISPATCH_MS"
DEFAULT_THRESHOLD_MS = 15.0


def wanted():
    return (perf_trace.enabled()
            and os.environ.get(DISPATCH_ENV, "") not in ("", "0", "false", "no"))


def _threshold_ms():
    try:
        return float(os.environ.get(DISPATCH_MS_ENV, "") or DEFAULT_THRESHOLD_MS)
    except ValueError:
        return DEFAULT_THRESHOLD_MS


_EVENT_NAMES = {}
for _name in dir(QEvent):
    _value = getattr(QEvent, _name)
    if isinstance(_value, QEvent.Type):
        _EVENT_NAMES.setdefault(int(_value), _name)


def _describe(receiver):
    cls = type(receiver).__name__
    try:            # the receiver may have been deleted by its own event
        name = receiver.objectName()
    except Exception:                                   # noqa: BLE001
        name = ""
    return f"{cls}:{name}" if name else cls


class TimedApplication(QApplication):
    """A QApplication whose `notify` reports slow event deliveries."""

    def __init__(self, argv):
        super().__init__(argv)
        self._dispatch_threshold_s = _threshold_ms() / 1000.0
        # per thread: worker threads with event loops are dispatched here too
        self._dispatch_local = threading.local()
        # Teardown: the two diagnostic runs of 2026-10-07 crashed (SIGSEGV)
        # after their scenarios finished, with this override on and only
        # then. Stop timing once the loop is quitting, and leave the C++
        # objects to the process exit instead of destroying them one by one
        # through their Python wrappers while `notify` is still Python.
        self._dispatch_on = True
        self.aboutToQuit.connect(self._dispatch_stop)
        try:
            from PyQt5 import sip
            sip.setdestroyonexit(False)
        except Exception:                               # noqa: BLE001
            pass

    def _dispatch_stop(self):
        self._dispatch_on = False

    def notify(self, receiver, event):  # noqa: N802
        if not self._dispatch_on:
            return super().notify(receiver, event)
        local = self._dispatch_local
        depth = getattr(local, "depth", 0)
        local.depth = depth + 1
        etype = int(event.type())
        t0 = time.monotonic()
        try:
            return super().notify(receiver, event)
        finally:
            local.depth = depth
            dur = time.monotonic() - t0
            if dur >= self._dispatch_threshold_s:
                perf_trace.mark("qt.dispatch", dur_ms=dur * 1000.0, t_begin=t0,
                                depth=depth, etype=_EVENT_NAMES.get(etype, etype),
                                recv=_describe(receiver),
                                thread=threading.current_thread().name)


def make_application(argv):
    """`TimedApplication` when the switch is on, else a plain QApplication."""
    return TimedApplication(argv) if wanted() else QApplication(argv)
