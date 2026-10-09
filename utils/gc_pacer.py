"""Python's FULL garbage collections, moved off the user's gestures.

Block A9 §32. CPython runs a full (generation 2) collection on its own
schedule, wherever the interpreter happens to be: on Kevin's 69-channel
slide one took 82-248 ms (measured, 5 in one session), which is a viewer
that stops for 5-15 frames in the middle of a wheel gesture. Odon is Rust:
it has no collector, so its frame loop never pauses for one. This is the
nearest equivalent:

  * the automatic full collection is switched off (its threshold set out
    of reach); generations 0 and 1 keep their automatic collections, which
    are short (<= ~3 ms measured);
  * a full collection runs instead when the user has been IDLE -- no mouse,
    wheel or key input anywhere in the application -- for `IDLE_S`, at most
    once per `MIN_GAP_S`;
  * a session that never goes idle still collects: after `MAX_GAP_S`
    without one, the next timer tick collects regardless.

A full collection walks every tracked object -- ~180 ms on Kevin even when
it finds nothing -- so after one, the survivors are FROZEN (`gc.freeze`):
the next idle collections walk only what was allocated since, a few ms.
Garbage among frozen objects is reclaimed by reference counting as usual;
only a CYCLE formed among them waits, until the next THOROUGH collection
(`gc.unfreeze` + full + freeze), which runs after `THOROUGH_IDLE_S` of idle
input at most once per `THOROUGH_GAP_S`. `BLOCK01_GC_PACER=0` keeps
CPython's default.
"""

import gc
import os
import time

from PyQt5 import QtCore

from . import perf_trace

IDLE_S = 2.0
MIN_GAP_S = 10.0
MAX_GAP_S = 120.0
THOROUGH_IDLE_S = 5.0
THOROUGH_GAP_S = 600.0
TICK_MS = 500
#: generation-2 threshold that the automatic schedule never reaches
OFF = 1_000_000_000

_INPUT = frozenset({
    QtCore.QEvent.MouseButtonPress, QtCore.QEvent.MouseButtonRelease,
    QtCore.QEvent.MouseMove, QtCore.QEvent.Wheel, QtCore.QEvent.KeyPress,
    QtCore.QEvent.KeyRelease, QtCore.QEvent.TouchBegin, QtCore.QEvent.TouchUpdate,
    QtCore.QEvent.Gesture})


class GcPacer(QtCore.QObject):
    def __init__(self, app, *, clock=time.monotonic):
        super().__init__(app)
        self._clock = clock
        now = clock()
        self.last_input = now
        self.last_full = now
        self.full_collections = 0
        self.last_thorough = None
        self._born = now
        self._previous = gc.get_threshold()
        gen0, gen1, _gen2 = self._previous
        gc.set_threshold(gen0, gen1, OFF)
        app.installEventFilter(self)
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.tick)
        self._timer.start(TICK_MS)

    def eventFilter(self, _watched, event):  # noqa: N802
        if event.type() in _INPUT:
            self.last_input = self._clock()
        return False

    def tick(self):
        now = self._clock()
        since_full = now - self.last_full
        idle = now - self.last_input >= IDLE_S
        thorough_due = (self.last_thorough is None
                        or now - self.last_thorough >= THOROUGH_GAP_S)
        since_thorough = now - (self.last_thorough if self.last_thorough is not None
                                else self._born)
        overdue_thorough = since_thorough >= 2 * THOROUGH_GAP_S
        if thorough_due and (now - self.last_input >= THOROUGH_IDLE_S or overdue_thorough):
            # a session busy without a pause for 2 x THOROUGH_GAP_S still
            # gets its frozen cycles back (codex): one longer pause at most
            # every 20 minutes of uninterrupted input
            self.collect(reason="thorough" if not overdue_thorough else "thorough-overdue",
                         thorough=True)
        elif (idle and since_full >= MIN_GAP_S) or since_full >= MAX_GAP_S:
            self.collect(reason="idle" if idle else "overdue")

    def collect(self, reason="idle", thorough=False):
        started = time.perf_counter()
        if thorough:
            gc.unfreeze()
        found = gc.collect(2)
        gc.freeze()
        self.last_full = self._clock()
        if thorough:
            self.last_thorough = self.last_full
        self.full_collections += 1
        perf_trace.mark("gc.full", reason=reason, collected=found,
                        frozen=gc.get_freeze_count(),
                        ms=round((time.perf_counter() - started) * 1000.0, 2))
        return found

    def stop(self):
        self._timer.stop()
        gc.unfreeze()
        gc.set_threshold(*self._previous)


def start(app):
    """Install the pacer on `app`; None when `BLOCK01_GC_PACER=0`."""
    if os.environ.get("BLOCK01_GC_PACER", "1") == "0":
        return None
    return GcPacer(app)
