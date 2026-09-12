"""The thread that works out a channel's FIRST display window.

WHY. A channel nobody has set Min/Max/Gamma for needs an automatic one, and
the rule is QuPath's: percentiles over the slide's non-zero pixels
(`core.display_mapping.seed_display_range`). That is a full pass over a
whole-slide low-resolution array -- ~923x555 on the real slide -- and it was
being run inside the GUI-thread callback that builds a Tissue Preview
snapshot. A snapshot's whole job is to hand values over and return; a
percentile in there is the stall this work exists to remove, and it lands
exactly when a new channel first appears, which is when the user is looking.

WHAT IT IS. One computation at a time, a QUEUE of distinct requests rather
than a single latest-only slot -- unlike the compose worker, because these
are not frames: every channel's seed is wanted, none supersedes another, and
each is wanted exactly once. Requests are deduplicated by
`(dataset, channel, role)`, so three views asking for the same channel cost
one pass.

WHAT CROSSES THE BOUNDARY. Going in: the dataset token the answer is ABOUT,
the channel, and a reference to an array the GUI thread will not mutate (a
new read replaces the array object rather than filling the old one). Coming
back: two floats and the identity they were computed under -- so a seed that
started on slide A and finishes after slide B has loaded is written into A's
namespace rather than shown as B's.
"""

import collections
import threading

from PyQt5.QtCore import QObject, pyqtSignal

from ..core.display_mapping import seed_display_range
from ..utils import perf_trace


class DisplaySeedWorker(QObject):
    """Compute automatic display windows off the GUI thread."""

    done = pyqtSignal(object)          # {"token", "channel", "nucleus", ...}
    failed = pyqtSignal(object)

    THREAD_NAME = "display-seed"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._queue = collections.deque()
        self._queued_keys = set()
        self._stopping = False
        self._busy = False
        self._computed = 0
        self._deduped = 0
        self._thread = None

    # ── producer side (GUI thread) ──────────────────────────────────
    def submit(self, binding, channel, array, nucleus=False):
        """Ask for `channel`'s window. True when this request was taken.

        False when the same `(binding, channel, role)` is already queued or
        being computed: three views wanting the same channel is one pass, and
        a re-request while it is in flight must not become a second one.

        `binding` is opaque here -- a `DisplayBinding`, carried out and back
        so the caller can check WHICH SLIDE and WHICH BINDING the answer is
        about. This thread only has to keep it with the request.
        """
        key = (binding, channel, bool(nucleus))
        with self._wake:
            if self._stopping:
                return False
            if key in self._queued_keys:
                self._deduped += 1
                return False
            self._queued_keys.add(key)
            self._queue.append((key, array))
            self._wake.notify()
        return True

    def pending(self, binding=None, channel=None, nucleus=False):
        with self._lock:
            if channel is None:
                return len(self._queued_keys)
            return (binding, channel, bool(nucleus)) in self._queued_keys

    def stats(self):
        with self._lock:
            return {"queued": len(self._queue), "computed": self._computed,
                    "deduped": self._deduped, "busy": self._busy}

    def is_busy(self):
        with self._lock:
            return self._busy or bool(self._queue)

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            thread = threading.Thread(target=self.run,
                                      name=self.THREAD_NAME, daemon=True)
            self._thread = thread
        thread.start()

    def isRunning(self):
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def wait(self, timeout_ms=2000):
        thread = self._thread
        if thread is None:
            return True
        thread.join(max(0.0, float(timeout_ms) / 1000.0))
        return not thread.is_alive()

    def stop(self, timeout_ms=2000):
        """Ask the thread to finish. It emits nothing after this, so a late
        seed cannot reach a window that is closing."""
        with self._wake:
            self._stopping = True
            self._queue.clear()
            self._queued_keys.clear()
            self._wake.notify_all()
        return self.wait(timeout_ms)

    # ── consumer side (this thread) ─────────────────────────────────
    def run(self):
        while True:
            with self._wake:
                while not self._queue and not self._stopping:
                    self._wake.wait(0.05)
                if self._stopping:
                    return
                key, array = self._queue.popleft()
                self._busy = True
            binding, channel, nucleus = key
            try:
                with perf_trace.span("tissue.seed", channel=channel,
                                     nucleus=nucleus):
                    lo, hi = seed_display_range(array)
                result = {"binding": binding, "channel": channel,
                          "nucleus": nucleus, "min": float(lo),
                          "max": float(hi), "gamma": 1.0}
            except Exception as exc:                        # noqa: BLE001
                with self._wake:
                    self._busy = False
                    self._queued_keys.discard(key)
                    stopping = self._stopping
                if not stopping:
                    self.failed.emit({"channel": channel, "error": str(exc)})
                continue
            with self._wake:
                self._busy = False
                self._computed += 1
                self._queued_keys.discard(key)
                stopping = self._stopping
            if not stopping:
                self.done.emit(result)


class LowresReadWorker(QObject):
    """The thread that reads a channel's whole-slide low-resolution array.

    WHY A SECOND ONE. The viewers' own overview store already reads these in
    the background and is the right place when a viewer exists. It does not
    always: the landing before the full image is built, a page whose compare
    strip has never been opened, a session driven by scripts. The fallback
    used to be a synchronous `read_region_lowres` on the GUI thread, taken
    "only once per channel per slide" -- but once is 170-230 ms, measured,
    and it lands exactly when a new channel first appears, which is when the
    user is looking.

    Same shape as `DisplaySeedWorker`: a queue of distinct requests,
    deduplicated by `(dataset, channel)`, one at a time. The array comes back
    with the dataset token it was read under, so one that outlives a slide
    switch is refused rather than installed as the new slide's.
    """

    done = pyqtSignal(object)          # {"token", "channel", "array"}
    failed = pyqtSignal(object)

    THREAD_NAME = "lowres-read"

    def __init__(self, reader, parent=None):
        super().__init__(parent)
        self._reader = reader
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._queue = collections.deque()
        self._queued_keys = set()
        self._stopping = False
        self._busy = False
        self._read = 0
        self._deduped = 0
        self._thread = None

    def submit(self, token, channel):
        key = (token, channel)
        with self._wake:
            if self._stopping:
                return False
            if key in self._queued_keys:
                self._deduped += 1
                return False
            self._queued_keys.add(key)
            self._queue.append(key)
            self._wake.notify()
        return True

    def pending(self, token=None, channel=None):
        with self._lock:
            if channel is None:
                return len(self._queued_keys)
            return (token, channel) in self._queued_keys

    def is_busy(self):
        with self._lock:
            return self._busy or bool(self._queue)

    def stats(self):
        with self._lock:
            return {"queued": len(self._queue), "read": self._read,
                    "deduped": self._deduped, "busy": self._busy}

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            thread = threading.Thread(target=self.run,
                                      name=self.THREAD_NAME, daemon=True)
            self._thread = thread
        thread.start()

    def isRunning(self):
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def wait(self, timeout_ms=5000):
        thread = self._thread
        if thread is None:
            return True
        thread.join(max(0.0, float(timeout_ms) / 1000.0))
        return not thread.is_alive()

    def stop(self, timeout_ms=5000):
        with self._wake:
            self._stopping = True
            self._queue.clear()
            self._queued_keys.clear()
            self._wake.notify_all()
        return self.wait(timeout_ms)

    def run(self):
        while True:
            with self._wake:
                while not self._queue and not self._stopping:
                    self._wake.wait(0.05)
                if self._stopping:
                    return
                key = self._queue.popleft()
                self._busy = True
            token, channel = key
            try:
                with perf_trace.span("tissue.lowres_read", channel=channel):
                    array = self._reader(channel)
            except Exception as exc:                        # noqa: BLE001
                with self._wake:
                    self._busy = False
                    self._queued_keys.discard(key)
                    stopping = self._stopping
                if not stopping:
                    self.failed.emit({"channel": channel, "error": str(exc)})
                continue
            with self._wake:
                self._busy = False
                self._read += 1
                self._queued_keys.discard(key)
                stopping = self._stopping
            if not stopping and array is not None:
                self.done.emit({"token": token, "channel": channel,
                                "array": array})
