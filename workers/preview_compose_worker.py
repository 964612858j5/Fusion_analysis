"""The thread that turns a Step1 preview snapshot into pixels.

WHY. Measured on the real desk, a Step1 frame cost 42-85 ms and ran on the
GUI thread, so every published frame was a GUI stall of that length -- which
is why a 33 ms frame clock alone cannot make a drag feel attached to the hand:
it would only stall more often. The arrays have to be computed somewhere else.

WHAT IT IS NOT. Not a queue and not a pool. One computation at a time, and a
single pending SNAPSHOT that the newest submission replaces: a drag at 200
inputs a second must never mean 200 frames of history, and two workers on the
same arrays would only take turns holding the GIL and the memory bus.

WHAT CROSSES THE BOUNDARY. Going in: a snapshot the GUI thread built, holding
references to raw arrays it will not mutate (a new read replaces the array
object rather than writing into it) plus plain numbers and strings. Coming
back: one uint8 RGB array and the identity it was computed from. No widget, no
camera, no cache of the window's -- this worker owns its own `PreviewCache`,
because a dict mutated from two threads is a different bug every time.
"""

import threading

from PyQt5.QtCore import QObject, pyqtSignal

from ..core import preview_compose
from ..utils import perf_trace


class PreviewComposeWorker(QObject):
    """Compose Step1 preview frames off the GUI thread, latest-only.

    A plain daemon thread rather than a QThread, deliberately. A QThread that
    is still running when its parent widget is deleted aborts the process --
    "QThread: Destroyed while thread is still running" -- and a preview thread
    spends most of its life waiting for the next snapshot, so that is the
    normal case, not an edge one. A daemon thread waiting on a condition can
    be abandoned safely at teardown; Qt signals may be emitted from it because
    the connection to a GUI-thread receiver is queued.
    """

    done = pyqtSignal(object)          # the result dict
    failed = pyqtSignal(object)        # {"request": ..., "error": str}

    def __init__(self, parent=None, cache=None):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._pending = None
        self._stopping = False
        self._busy = False
        self._submitted = 0
        self._replaced = 0
        self._computed = 0
        self._thread = None
        # The worker's OWN cache. The window keeps its own for the frames it
        # still draws immediately (a patch switch, a restore); sharing one
        # dict across two threads would need a lock on every lookup and would
        # still interleave evictions.
        self.cache = cache if cache is not None else preview_compose.PreviewCache()

    # ── producer side (GUI thread) ──────────────────────────────────
    def submit(self, request):
        """Take the newest snapshot, discard any older pending one.

        Returns True when this snapshot became the pending one. Never blocks
        on the computation: the point of the whole arrangement is that the
        GUI thread hands over and returns.
        """
        with self._wake:
            if self._stopping:
                return False
            if self._pending is not None:
                self._replaced += 1
            self._pending = request
            self._submitted += 1
            self._wake.notify()
        return True

    def stats(self):
        with self._lock:
            return {"submitted": self._submitted, "replaced": self._replaced,
                    "computed": self._computed, "busy": self._busy,
                    "pending": self._pending is not None}

    def is_busy(self):
        with self._lock:
            return self._busy or self._pending is not None

    def start(self):
        """Start the composing thread, once."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            thread = threading.Thread(target=self.run,
                                      name="preview-compose", daemon=True)
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
        """Ask the thread to finish. Nothing of the worker's is touched here.

        The caller may NOT clear the cache: this thread could be in the
        middle of reading it, and "the worker owns its cache" is not a rule
        that holds only while it is idle. So the request is a flag, the
        thread empties its own cache on the way out, and a stop that times
        out leaves everything alone -- the object stays referenced and the
        daemon thread finishes the array it is on. It emits nothing after
        being asked to stop, so a late result cannot reach a window that is
        closing.
        """
        with self._wake:
            self._stopping = True
            self._pending = None
            self._wake.notify_all()
        return self.wait(timeout_ms)

    # ── consumer side (this thread) ─────────────────────────────────
    def run(self):
        """The composing loop. Returns when `stop()` is called.

        The cache is emptied HERE, on the way out, because it belongs to this
        thread: a GUI thread clearing it while a frame is being composed is
        the same class of bug as sharing it in the first place.
        """
        try:
            while True:
                with self._wake:
                    while self._pending is None and not self._stopping:
                        self._wake.wait(0.05)
                    if self._stopping:
                        return
                    request = self._pending
                    self._pending = None
                    self._busy = True
                try:
                    result = self._compose(request)
                except Exception as exc:                    # noqa: BLE001
                    with self._lock:
                        self._busy = False
                        stopping = self._stopping
                    if not stopping:
                        self.failed.emit({"request": request,
                                          "error": str(exc)})
                    continue
                with self._lock:
                    self._busy = False
                    self._computed += 1
                    stopping = self._stopping
                if result is not None and not stopping:
                    self.done.emit(result)
        finally:
            try:
                self.cache.clear()
            except Exception:                               # noqa: BLE001
                pass

    def _compose(self, request):
        mode = request.get("mode")
        patch = request.get("patch")
        with perf_trace.span("step1.worker_frame", mode=mode, patch=patch,
                             rev=request.get("rev"),
                             channels=len(request.get("arrays") or {})):
            if mode == "overlay":
                rgb = preview_compose.overlay_rgb_u8(
                    patch, request["arrays"], request["remap"],
                    request["colors"], request["weights"], self.cache,
                    span=perf_trace.span)
                fused_to_nothing = False
            else:
                rgb, fused_to_nothing = preview_compose.fusion_rgb_u8(
                    patch, request["arrays"], request["remap"],
                    request["groups"], request["group_weights"],
                    request["nucleus"], self.cache, request["fallback_norm"],
                    request["to_rgb"], span=perf_trace.span)
        result = dict(request)
        result.pop("arrays", None)          # the pixels go back as one array
        result.pop("to_rgb", None)
        result.pop("fallback_norm", None)
        result["rgb"] = rgb
        result["fused_to_nothing"] = fused_to_nothing
        return result
