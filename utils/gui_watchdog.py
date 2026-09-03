"""Report where the GUI thread is when the event loop stops responding.

Manual testing reported the main window freezing on the FIRST Save after
start-up -- long enough for the desktop to offer to kill the application --
and recovering by itself. Nothing was logged, and the freeze could not be
reproduced offscreen, where every phase of the Save path measures in
milliseconds. A freeze that cannot be reproduced has to be caught in the
act: this module does that and nothing else.

Mechanism: a `QTimer` on the GUI thread writes a heartbeat timestamp every
`beat_ms`. A daemon thread checks it every `check_ms`; when the heartbeat is
older than `stall_seconds` it reports ONCE per stall, with the GUI thread's
Python stack at that moment (`sys._current_frames()`), and reports again
when the heartbeat returns, with the stall's total length. The stack is the
point: it names the function the GUI thread was stuck in, which is what the
report from the desk could not tell us.

Cost when nothing is wrong: one timer tick per `beat_ms` and one wake-up per
`check_ms` on a thread that compares two floats. Disabled by setting the
environment variable `BLOCK01_GUI_WATCHDOG=0`.
"""

import os
import sys
import threading
import time
import traceback

from PyQt5 import QtCore

ENV_DISABLE = "BLOCK01_GUI_WATCHDOG"


class GuiWatchdog:
    """See the module docstring. `report` receives one string per event;
    the default prints to stderr with a `[gui-watchdog]` prefix."""

    def __init__(self, *, stall_seconds=2.0, beat_ms=200, check_ms=250,
                 report=None, clock=time.monotonic):
        self.stall_seconds = float(stall_seconds)
        self.beat_ms = int(beat_ms)
        self.check_ms = int(check_ms)
        self._report = report or self._print
        self._clock = clock
        self._last_beat = clock()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._stalled_since = None
        self._gui_thread_ident = threading.get_ident()
        self.stalls = []          # (duration_s, stack_text) for every stall seen
        self._timer = QtCore.QTimer()
        self._timer.setInterval(self.beat_ms)
        self._timer.timeout.connect(self._beat)
        self._thread = threading.Thread(target=self._watch, name="gui-watchdog",
                                        daemon=True)

    @staticmethod
    def _print(text):
        print(f"[gui-watchdog] {text}", file=sys.stderr, flush=True)

    def start(self):
        self._last_beat = self._clock()
        self._timer.start()
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._timer.stop()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _beat(self):
        with self._lock:
            self._last_beat = self._clock()

    def gui_stack(self):
        """The GUI thread's current Python stack, formatted. Best effort:
        the frame can move between the lookup and the formatting."""
        frame = sys._current_frames().get(self._gui_thread_ident)
        if frame is None:
            return "<no frame for the GUI thread>"
        return "".join(traceback.format_stack(frame))

    def _watch(self):
        while not self._stop.wait(self.check_ms / 1000.0):
            now = self._clock()
            with self._lock:
                age = now - self._last_beat
            if age >= self.stall_seconds and self._stalled_since is None:
                self._stalled_since = now - age
                stack = self.gui_stack()
                self._report(
                    f"GUI event loop has not run for {age:.1f} s; "
                    f"the GUI thread is here:\n{stack}")
                self.stalls.append([None, stack])
            elif age < self.stall_seconds and self._stalled_since is not None:
                total = now - self._stalled_since
                self._stalled_since = None
                if self.stalls and self.stalls[-1][0] is None:
                    self.stalls[-1][0] = total
                self._report(f"GUI event loop responsive again after {total:.1f} s")


def start_gui_watchdog(**kwargs):
    """Start a watchdog for the running QApplication unless disabled by
    `BLOCK01_GUI_WATCHDOG=0`. Returns it, or None when disabled. Call on
    the GUI thread, after the QApplication exists."""
    if os.environ.get(ENV_DISABLE, "1").strip().lower() in {"0", "false", "no", "off"}:
        return None
    return GuiWatchdog(**kwargs).start()
