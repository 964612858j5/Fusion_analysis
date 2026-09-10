"""One monotonic timeline for the interactive paths, default off.

Why a module rather than prints. Three complaints -- Intensity lagging the
mouse, drawing two patches freezing the window, the first seconds of Step1
stuttering -- all look the same from the outside: the GUI stops answering.
What separates them is WHICH callback was on the thread and for how long, and
that cannot be reasoned out. It has to be timed, on the machine that has the
problem, with one clock so the spans can be laid beside each other.

Cost when off: `enabled()` reads one environment variable and returns. `span`
returns a shared do-nothing object, `mark` returns immediately, and no timer
exists. The switch is read per call rather than captured at import so a single
run can be traced without a code change -- the same choice
`BLOCK01_MIDPAN_DEBUG` made, and for the same reason.

    BLOCK01_PERF=1                  trace to stderr
    BLOCK01_PERF_LOG=<path>          and append the same lines to a file

Line shape, one event per line:

    PERF t=1234.567890 ev=step1.overlay dur_ms=41.83 channels=3 patch=0

`t` is `time.monotonic()`, so every line in a run -- and every MIDPAN line
from the middle-drag diagnostic, which uses the same clock -- is comparable.
"""

import os
import sys
import time

PERF_ENV = "BLOCK01_PERF"
PERF_LOG_ENV = "BLOCK01_PERF_LOG"

# A heartbeat silence longer than this is the GUI thread being unavailable:
# the timer should tick 200 times a second, so a gap IS the thread busy
# elsewhere, measured from inside the event loop.
HEARTBEAT_INTERVAL_MS = 5
HEARTBEAT_GAP_MS = 25.0

_LOG_FILE = None            # (path, handle) for the current run


def enabled():
    return os.environ.get(PERF_ENV, "") not in ("", "0", "false", "no")


def _sink(line):
    """stderr always, plus the file the environment named if it named one.

    A file that cannot be opened costs the traced code nothing: the line still
    reaches stderr and the failure is reported once, in place of the file.
    """
    global _LOG_FILE
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:                                       # noqa: BLE001
        pass
    path = os.environ.get(PERF_LOG_ENV, "") or None
    if path is None:
        return
    try:
        if _LOG_FILE is None or _LOG_FILE[0] != path:
            if _LOG_FILE is not None:
                try:
                    _LOG_FILE[1].close()
                except Exception:                           # noqa: BLE001
                    pass
            _LOG_FILE = (path, open(path, "a", encoding="utf-8", buffering=1))
        _LOG_FILE[1].write(line + "\n")
        _LOG_FILE[1].flush()
    except Exception as exc:                                # noqa: BLE001
        _LOG_FILE = None
        try:
            print(f"PERF log-file-failed path={path!r} err={exc!r}",
                  file=sys.stderr, flush=True)
        except Exception:                                   # noqa: BLE001
            pass


def _fields(items):
    out = []
    for key, value in items:
        if value is None:
            continue
        if isinstance(value, float):
            value = f"{value:.4g}"
        out.append(f"{key}={value}")
    return out


def mark(event, **fields):
    """One instant, no duration: an input arriving, a decision taken."""
    if not enabled():
        return
    try:
        line = " ".join(["PERF", f"t={time.monotonic():.6f}", f"ev={event}"]
                        + _fields(fields.items()))
        _sink(line)
    except Exception:                                       # noqa: BLE001
        pass


class _Nothing:
    """What `span` returns with the switch off: no clock, no allocation of
    consequence, no line."""

    __slots__ = ()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def add(self, **_fields):
        return self


_NOTHING = _Nothing()


class _Span:
    """A timed region. Fields may be added while it runs -- the count of
    channels composited, the patch it was for -- and are written with the
    duration when it ends, so one line carries what the work was and what it
    cost."""

    __slots__ = ("_event", "_fields", "_t0")

    def __init__(self, event, fields):
        self._event = event
        self._fields = dict(fields)
        self._t0 = time.monotonic()

    def add(self, **fields):
        self._fields.update(fields)
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, _tb):
        try:
            dur = (time.monotonic() - self._t0) * 1000.0
            if exc_type is not None:
                self._fields["raised"] = exc_type.__name__
            line = " ".join(
                ["PERF", f"t={time.monotonic():.6f}", f"ev={self._event}",
                 f"dur_ms={dur:.2f}"] + _fields(self._fields.items()))
            _sink(line)
        except Exception:                                   # noqa: BLE001
            pass
        return False


def span(event, **fields):
    if not enabled():
        return _NOTHING
    try:
        return _Span(event, fields)
    except Exception:                                       # noqa: BLE001
        return _NOTHING


class Revisions:
    """Numbers for the inputs, so a published frame can be compared with the
    input it was drawn from.

    "The picture lags the mouse" is a statement about two clocks: when the
    slider moved and when the screen showed that move. Counting the input
    events gives the second number a name -- a frame that publishes revision
    17 while revision 31 has already arrived is 14 events behind, and that is
    a fact rather than an impression.
    """

    def __init__(self):
        self._counters = {}

    def bump(self, kind):
        value = self._counters.get(kind, 0) + 1
        self._counters[kind] = value
        return value

    def latest(self, kind):
        return self._counters.get(kind, 0)


REVISIONS = Revisions()


class Heartbeat:
    """Ask the GUI thread every few milliseconds whether it is still there.

    A callback that runs for 400 ms and an input that was never delivered are
    the same hole in a log of callbacks. This closes that: the timer should
    tick at a known rate, so a silence is the thread being unavailable, and
    the gap is reported with what the loop was last seen doing.

    Created only when the switch is on, and owned by its host, which stops it
    on teardown. Never started with the switch off, so a program that is not
    being traced has no 5 ms timer in it.
    """

    def __init__(self, parent=None, interval_ms=HEARTBEAT_INTERVAL_MS,
                 gap_ms=HEARTBEAT_GAP_MS, label=""):
        from PyQt5 import QtCore

        self._gap_ms = float(gap_ms)
        self._label = str(label or "")
        self._last = time.monotonic()
        self._timer = QtCore.QTimer(parent)
        self._timer.setInterval(int(interval_ms))
        self._timer.timeout.connect(self._tick)

    def start(self):
        self._last = time.monotonic()
        self._timer.start()
        return self

    def stop(self):
        try:
            self._timer.stop()
        except Exception:                                   # noqa: BLE001
            pass

    def is_active(self):
        try:
            return bool(self._timer.isActive())
        except Exception:                                   # noqa: BLE001
            return False

    def _tick(self):
        now = time.monotonic()
        last, self._last = self._last, now
        gap = (now - last) * 1000.0
        if gap > self._gap_ms:
            mark("gui.gap", gap_ms=gap,
                 expected_ms=self._timer.interval(),
                 where=self._label or None)


def start_heartbeat(parent=None, label=""):
    """A heartbeat if the switch is on, else None -- the caller keeps whatever
    it gets and stops it if it is not None."""
    if not enabled():
        return None
    try:
        return Heartbeat(parent=parent, label=label).start()
    except Exception:                                       # noqa: BLE001
        return None
