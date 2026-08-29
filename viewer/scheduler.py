"""Priority/dedup/cancellation-aware tile scheduler.

Contract (docs/v15_viewer_foundation_interfaces.md §4):

- Dedup is STRICTLY by identity key (RawKey/CorrectionKey), never by
  request generation: a tile already in flight collects every waiter that
  asks for it, and all waiters receive the same TileResult object.
- `generation` on a TileRequest is a delivery token only. It never affects
  whether work runs or whether a result enters a cache.
- RawKey requests go straight to an I/O ThreadPoolExecutor. CorrectionKey
  requests go onto a priority ready-queue (min-heap on `priority`, FIFO
  within a priority tier) served by exactly `compute_workers` worker
  thread(s).
- `cancel_generation(gen)` marks `gen` stale. A queued-but-not-started
  compute entry wanted only by stale generations is dropped without
  running its callbacks receive error="cancelled". Work already running
  always finishes and its result is always written to the cache; delivery
  to any waiter whose generation is stale at completion time is simply
  skipped (no callback for that waiter) -- this is the "lands in the cache,
  not necessarily delivered" rule from the design doc.
"""

import dataclasses
import heapq
import itertools
import threading
from concurrent.futures import ThreadPoolExecutor

from .tile_types import CorrectionKey, PixelBuffer, QualityLevel, RawKey, TileResult


class _Entry:
    """Bookkeeping for one in-flight (deduped) key."""

    __slots__ = ("waiters", "started")

    def __init__(self, req, callback):
        self.waiters = [(req, callback)]
        self.started = False


class TileScheduler:
    """Dispatches raw/correction tile requests with dedup and cancellation."""

    def __init__(self, provider, compute, raw_cache, corrected_cache,
                 io_workers: int = 4, compute_workers: int = 1):
        self.provider = provider
        self.compute = compute
        self.raw_cache = raw_cache
        self.corrected_cache = corrected_cache

        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._pending = {}  # key -> _Entry
        self._heap = []  # (priority, seq, key) for CorrectionKey work
        self._seq = itertools.count()
        self._stale_gens = set()
        self._shutdown = False

        self._io_executor = ThreadPoolExecutor(max_workers=io_workers, thread_name_prefix="tile-io")
        self._compute_threads = [
            threading.Thread(target=self._compute_worker, daemon=True, name=f"tile-compute-{i}")
            for i in range(compute_workers)
        ]
        for t in self._compute_threads:
            t.start()

    # ── public API ───────────────────────────────────────────────────────

    def request(self, req, callback):
        """Ask for `req.key`; `callback(TileResult)` fires once (sync on hit)."""
        key = req.key
        cache = self._cache_for(key)
        cached = cache.get(key)
        if cached is not None:
            callback(self._wrap_cache_hit(req, cached))
            return

        with self._lock:
            entry = self._pending.get(key)
            if entry is not None:
                entry.waiters.append((req, callback))
                return
            entry = _Entry(req, callback)
            self._pending[key] = entry
            if isinstance(key, RawKey):
                entry.started = True
                submit_raw = True
            else:
                heapq.heappush(self._heap, (req.priority, next(self._seq), key))
                submit_raw = False
                self._cv.notify_all()

        if submit_raw:
            self._io_executor.submit(self._run_raw, key)

    def cancel_generation(self, gen: int):
        """Mark `gen` stale; drop queued compute work wanted only by it."""
        with self._lock:
            self._stale_gens.add(gen)
            self._cv.notify_all()

    def shutdown(self):
        """Stop worker threads and the I/O pool, draining in-flight work."""
        with self._lock:
            self._shutdown = True
            self._cv.notify_all()
        for t in self._compute_threads:
            t.join()
        self._io_executor.shutdown(wait=True)

    # ── internals ────────────────────────────────────────────────────────

    def _cache_for(self, key):
        return self.raw_cache if isinstance(key, RawKey) else self.corrected_cache

    def _wrap_cache_hit(self, req, arr) -> TileResult:
        quality = req.key.quality if isinstance(req.key, CorrectionKey) else QualityLevel.NATIVE
        pixels = PixelBuffer(residency="cpu", dtype=str(arr.dtype), shape=tuple(arr.shape), handle=arr)
        return TileResult(
            request=req, pixels=pixels, quality=quality,
            provisional=False, timing={"cache": "hit"}, error=None,
        )

    def _is_stale(self, req) -> bool:
        with self._lock:
            return req.generation in self._stale_gens

    def _deliver(self, waiters, build_result):
        """Call each waiter's callback with its own TileResult, skipping stale ones."""
        for req, cb in waiters:
            if self._is_stale(req):
                continue
            cb(build_result(req))

    # -- raw path --

    def _run_raw(self, key: RawKey):
        try:
            arr, io_ms = self.provider.read_tile(key.channel, key.tile)
            error = None
        except Exception as exc:  # pragma: no cover - defensive
            arr, io_ms, error = None, None, str(exc)

        if error is None:
            self.raw_cache.put(key, arr)

        with self._lock:
            entry = self._pending.pop(key, None)
        waiters = entry.waiters if entry is not None else []

        if error is not None:
            self._deliver(waiters, lambda req: TileResult(
                request=req, pixels=None, quality=QualityLevel.NATIVE,
                provisional=False, timing={}, error=error,
            ))
            return

        pixels = PixelBuffer(residency="cpu", dtype=str(arr.dtype), shape=tuple(arr.shape), handle=arr)
        timing = {"io_ms": io_ms, "total_ms": io_ms}
        self._deliver(waiters, lambda req: TileResult(
            request=req, pixels=pixels, quality=QualityLevel.NATIVE,
            provisional=False, timing=timing, error=None,
        ))

    # -- compute path --

    def _compute_worker(self):
        while True:
            with self._cv:
                while not self._heap and not self._shutdown:
                    self._cv.wait()
                if self._shutdown and not self._heap:
                    return
                _priority, _seq, key = heapq.heappop(self._heap)
                entry = self._pending.get(key)
                if entry is None:
                    continue  # already resolved/removed concurrently

                waiters_snapshot = list(entry.waiters)
                live = [w for w in waiters_snapshot if w[0].generation not in self._stale_gens]
                if not live:
                    # Nobody still wants this: drop without running.
                    del self._pending[key]
                    to_cancel = waiters_snapshot
                else:
                    entry.started = True
                    to_cancel = None

            if to_cancel is not None:
                for req, cb in to_cancel:
                    cb(TileResult(
                        request=req, pixels=None, quality=req.key.quality,
                        provisional=False, timing={}, error="cancelled",
                    ))
                continue

            self._run_compute(key, entry)

    def _run_compute(self, key: CorrectionKey, entry: _Entry):
        try:
            result = self.compute.compute(key)
            error = None
        except Exception as exc:  # pragma: no cover - defensive
            result, error = None, str(exc)

        if error is None:
            self.corrected_cache.put(key, result.pixels.handle)

        with self._lock:
            waiters = list(entry.waiters)
            self._pending.pop(key, None)

        if error is not None:
            self._deliver(waiters, lambda req: TileResult(
                request=req, pixels=None, quality=key.quality,
                provisional=False, timing={}, error=error,
            ))
            return

        self._deliver(waiters, lambda req: dataclasses.replace(result, request=req))
