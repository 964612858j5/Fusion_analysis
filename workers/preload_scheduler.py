"""Reading patch pixels for the conditioning views: on demand, and bounded.

WHAT IT REPLACES. `PreloadWorker` read EVERY patch x EVERY conditioning
channel, and `_start_preload` restarted it -- after emptying the cache -- on
every geometry change. Drawing a second patch therefore threw away every
array already read for the first one and started 2 x N reads, of which the
user was waiting for exactly one: the current patch's current channel. That is
the other half of "drawing two patches freezes the window"; the first half was
the synchronous handoff write (see `GeometryPersistWorker`).

THE RULES THIS KEEPS.

* Cache keyed by what the pixels ARE -- dataset generation, patch BBOX,
  channel, normalize, downsample -- not by patch INDEX. An added patch changes
  no other patch's bbox, so nothing already read is invalidated by it, and the
  indices shifting underneath cannot make one patch serve another's pixels.
* The current patch's needed channels are read FIRST. Everything else is
  background work that only starts when no foreground request is waiting, so a
  speculative read can never be in front of the one the user is looking at.
* A bounded number of readers (two by default), and a bounded queue. A storm
  of geometry edits must not become a storm of threads or an unbounded
  backlog: the oldest background request is dropped, and counted.
* Cancellation is a GENERATION, never a join. The GUI thread does not wait for
  a reader -- it changes what counts as current, and a read already inside the
  loader finishes and is discarded. `wait(3000)` on the GUI thread is how the
  window froze at teardown.
* The store has a byte ceiling, because these are whole patches of float32:
  four 1024x1024 channels are 16 MiB.
"""

import collections
import threading

import numpy as np

from PyQt5.QtCore import QObject, pyqtSignal

from ..utils import perf_trace

FOREGROUND = "foreground"
BACKGROUND = "background"

DEFAULT_MAX_READERS = 2
DEFAULT_MAX_PENDING = 256
DEFAULT_MAX_BYTES = 512 * 1024 * 1024


class PreloadScheduler(QObject):
    """On-demand, priority-ordered patch reads with a bounded cache."""

    loaded = pyqtSignal(object)    # {"dataset_gen", "bbox", "channel", ...}

    def __init__(self, read=None, max_readers=DEFAULT_MAX_READERS,
                 max_pending=DEFAULT_MAX_PENDING,
                 max_bytes=DEFAULT_MAX_BYTES, parent=None):
        super().__init__(parent)
        # Injected for the tests (a read that blocks on a barrier is how
        # "the callback returned while a read was in flight" is proved);
        # the default is the loader's own `read_region`.
        self._read = read
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._fg = collections.deque()
        self._bg = collections.deque()
        self._queued = set()
        self._in_flight = set()
        self._store = collections.OrderedDict()
        self._loader = None
        self._gen = 0
        self._stopping = False
        self._threads = []
        self.max_readers = int(max_readers)
        self.max_pending = int(max_pending)
        self.max_bytes = int(max_bytes)
        self._stats = {"hits": 0, "misses": 0, "reads": 0, "dropped": 0,
                       "stale": 0, "failed": 0, "evicted": 0,
                       "coalesced": 0}

    # ── identity ────────────────────────────────────────────────────
    def set_source(self, loader, dataset_gen):
        """Bind to a dataset. Everything read for another one is dropped."""
        with self._wake:
            self._loader = loader
            if int(dataset_gen) != self._gen:
                self._gen = int(dataset_gen)
                self._store.clear()
                self._fg.clear()
                self._bg.clear()
                self._queued.clear()
            self._wake.notify_all()

    def invalidate(self, dataset_gen=None):
        """A dataset switch. Queued work is dropped; a read in flight finishes
        and its result is discarded by generation rather than waited for."""
        with self._wake:
            if dataset_gen is not None:
                self._gen = int(dataset_gen)
            self._store.clear()
            dropped = len(self._fg) + len(self._bg)
            self._fg.clear()
            self._bg.clear()
            self._queued.clear()
            self._stats["dropped"] += dropped
            self._wake.notify_all()

    def dataset_gen(self):
        with self._lock:
            return self._gen

    # ── the store ───────────────────────────────────────────────────
    @staticmethod
    def key(dataset_gen, bbox, channel, normalize=False, downsample=1):
        return (int(dataset_gen), tuple(int(v) for v in bbox), str(channel),
                bool(normalize), int(downsample))

    def resident(self, bbox, channel, normalize=False, downsample=1):
        """The pixels, if they are already here. Never reads."""
        with self._lock:
            key = self.key(self._gen, bbox, channel, normalize, downsample)
            arr = self._store.get(key)
            if arr is None:
                self._stats["misses"] += 1
                return None
            self._store.move_to_end(key)
            self._stats["hits"] += 1
            return arr

    def put(self, bbox, channel, arr, normalize=False, downsample=1):
        """Adopt pixels read elsewhere (a synchronous re-read after a
        correction decision changed what a channel means)."""
        with self._lock:
            self._put_locked(self.key(self._gen, bbox, channel, normalize,
                                      downsample), arr)

    def _put_locked(self, key, arr):
        self._store.pop(key, None)
        self._store[key] = arr
        total = sum(int(getattr(a, "nbytes", 0) or 0)
                    for a in self._store.values())
        while total > self.max_bytes and len(self._store) > 1:
            oldest, old_arr = next(iter(self._store.items()))
            self._store.pop(oldest, None)
            total -= int(getattr(old_arr, "nbytes", 0) or 0)
            self._stats["evicted"] += 1

    def drop_channel(self, channel):
        with self._lock:
            for key in [k for k in self._store if k[2] == str(channel)]:
                self._store.pop(key, None)

    def clear(self):
        with self._lock:
            self._store.clear()

    def nbytes(self):
        with self._lock:
            return sum(int(getattr(a, "nbytes", 0) or 0)
                       for a in self._store.values())

    # ── requests (GUI thread) ───────────────────────────────────────
    def request(self, bbox, channel, priority=FOREGROUND, normalize=False,
                downsample=1):
        """Ask for one patch channel. Returns what happened, and never blocks.

        "hit" it is already here, "in_flight" a reader has it, "queued" it was
        accepted, "coalesced" it was already waiting (promoted if this request
        is the more urgent one), "dropped" the queue is full.
        """
        with self._wake:
            key = self.key(self._gen, bbox, channel, normalize, downsample)
            if key in self._store:
                self._store.move_to_end(key)
                self._stats["hits"] += 1
                return "hit"
            if key in self._in_flight:
                return "in_flight"
            if key in self._queued:
                if priority == FOREGROUND:
                    # The same pixels, wanted sooner: move the request across
                    # rather than queue it twice.
                    try:
                        self._bg.remove(key)
                    except ValueError:
                        pass
                    else:
                        self._fg.append(key)
                self._stats["coalesced"] += 1
                return "coalesced"
            if len(self._fg) + len(self._bg) >= self.max_pending:
                # Bounded backlog. A foreground request displaces the oldest
                # speculative one; a background request that does not fit is
                # simply not made -- the pixels are read when they are needed.
                if priority == FOREGROUND and self._bg:
                    victim = self._bg.popleft()
                    self._queued.discard(victim)
                    self._stats["dropped"] += 1
                else:
                    self._stats["dropped"] += 1
                    return "dropped"
            self._queued.add(key)
            (self._fg if priority == FOREGROUND else self._bg).append(key)
            self._stats["misses"] += 1
            self._wake.notify()
        self.start()
        return "queued"

    def request_many(self, bboxes, channels, priority=BACKGROUND,
                     normalize=False, downsample=1):
        out = collections.Counter()
        for bbox in bboxes:
            for channel in channels:
                out[self.request(bbox, channel, priority=priority,
                                 normalize=normalize,
                                 downsample=downsample)] += 1
        return dict(out)

    def stats(self):
        with self._lock:
            out = dict(self._stats)
            out.update({"foreground_pending": len(self._fg),
                        "background_pending": len(self._bg),
                        "in_flight": len(self._in_flight),
                        "resident": len(self._store),
                        "max_readers": self.max_readers,
                        "dataset_gen": self._gen})
            return out

    def drain(self, timeout_ms=5000):
        """Wait until nothing is queued or in flight. For TESTS and teardown
        reporting -- never call it from a callback the user is waiting on."""
        import time
        deadline = time.monotonic() + max(0.0, float(timeout_ms) / 1000.0)
        self.start()
        while time.monotonic() < deadline:
            with self._lock:
                idle = not (self._fg or self._bg or self._in_flight)
            if idle:
                return True
            time.sleep(0.002)
        with self._lock:
            return not (self._fg or self._bg or self._in_flight)

    # ── readers ─────────────────────────────────────────────────────
    def start(self):
        with self._lock:
            if self._stopping:
                return
            self._threads = [t for t in self._threads if t.is_alive()]
            missing = self.max_readers - len(self._threads)
            new = []
            for i in range(max(0, missing)):
                thread = threading.Thread(
                    target=self._run, name=f"preload-reader-{i}", daemon=True)
                self._threads.append(thread)
                new.append(thread)
        for thread in new:
            thread.start()

    def isRunning(self):
        return any(t.is_alive() for t in self._threads)

    def resume(self):
        """Accept work again after a `stop()`.

        `stop()` is how a cancelled preload is expressed, and the next patch
        edit has to be able to ask for pixels again -- the readers are
        restarted by the next `request`.
        """
        with self._wake:
            self._stopping = False

    def stop(self, timeout_ms=0):
        """Request a stop. Does NOT wait by default -- a reader inside a read
        cannot be interrupted, and the GUI thread waiting for it is the freeze
        this class exists to remove."""
        with self._wake:
            self._stopping = True
            self._fg.clear()
            self._bg.clear()
            self._queued.clear()
            self._wake.notify_all()
        if timeout_ms:
            for thread in list(self._threads):
                thread.join(max(0.0, float(timeout_ms) / 1000.0))
        return not self.isRunning()

    def _next_locked(self):
        # Foreground first, always: a speculative read may not go in front of
        # the patch the user is looking at, not even one that was queued
        # earlier.
        for queue in (self._fg, self._bg):
            while queue:
                key = queue.popleft()
                self._queued.discard(key)
                if key[0] != self._gen:
                    self._stats["stale"] += 1
                    continue
                if key in self._store or key in self._in_flight:
                    continue
                self._in_flight.add(key)
                return key
        return None

    def _run(self):
        while True:
            with self._wake:
                key = None
                while True:
                    if self._stopping:
                        return
                    key = self._next_locked()
                    if key is not None:
                        break
                    self._wake.wait(0.05)
                loader = self._loader
                gen = self._gen
            arr = None
            error = ""
            try:
                arr = self._read_one(loader, key)
                if arr is not None:
                    # The conditioning views expect a 2D float32 patch; the
                    # old `PreloadWorker` did this on its own thread too, and
                    # a cast on the GUI thread is a copy of a whole patch.
                    arr = np.asarray(arr, dtype=np.float32)
                    if arr.ndim == 3 and arr.shape[2] == 1:
                        arr = arr[:, :, 0]
            except Exception as exc:                        # noqa: BLE001
                error = str(exc)
            with self._lock:
                self._in_flight.discard(key)
                if error:
                    self._stats["failed"] += 1
                elif key[0] != self._gen:
                    # The dataset moved while this read was in the loader.
                    # Finishing was unavoidable; keeping it is not.
                    self._stats["stale"] += 1
                    arr = None
                elif arr is not None:
                    self._stats["reads"] += 1
                    self._put_locked(key, arr)
                stopping = self._stopping
            if arr is not None and not stopping and key[0] == gen:
                self.loaded.emit({"dataset_gen": key[0], "bbox": key[1],
                                  "channel": key[2], "normalize": key[3],
                                  "downsample": key[4], "array": arr})

    def _read_one(self, loader, key):
        _gen, bbox, channel, normalize, downsample = key
        y0, y1, x0, x1 = bbox
        with perf_trace.job("step0.preload_read", channel=channel,
                            bbox=f"{y0}:{y1},{x0}:{x1}",
                            downsample=downsample) as _job:
            with _job.read(channel=channel, bbox=f"{y0}:{y1},{x0}:{x1}"):
                if self._read is not None:
                    return self._read(channel, y0, y1, x0, x1,
                                      normalize=normalize,
                                      downsample=downsample)
                if loader is None:
                    return None
                return loader.read_region(channel, y0, y1, x0, x1,
                                          normalize=normalize,
                                          downsample=downsample)
