"""Priority/dedup/cancellation-aware tile scheduler.

Contract (docs/v15_viewer_foundation_interfaces.md §4):

- Dedup is STRICTLY by identity key (RawKey/CorrectionKey), never by
  request generation: a tile already in flight collects every waiter that
  asks for it, and all waiters receive the same TileResult object.
- `generation` on a TileRequest is a delivery token only. It never affects
  whether work runs or whether a result enters a cache.
- RawKey requests go onto a priority ready-queue (min-heap on `priority`,
  FIFO within a priority tier) served by exactly `io_workers` persistent
  raw-worker thread(s) -- mirroring the compute path exactly, so a burst of
  stale raw requests (fast zoom/pan) never backs up FIFO behind hundreds of
  queued reads. CorrectionKey requests go onto their own priority
  ready-queue served by exactly `compute_workers` worker thread(s).
- `cancel_generation(gen)` marks `gen` stale. A queued-but-not-started
  compute OR raw entry wanted only by stale generations is dropped without
  running; its callbacks receive error="cancelled". Work already running
  always finishes and its result is always written to the cache; delivery
  to any waiter whose generation is stale at completion time is simply
  skipped (no callback for that waiter) -- this is the "lands in the cache,
  not necessarily delivered" rule from the design doc.
- Before a raw worker thread executes a queued raw entry, it checks the
  entry's waiters: external waiters whose generation is stale are ignored
  for the "should this run at all" decision. If there are no live external
  waiters AND no internal (staging) waiters, the read is skipped entirely
  and the queued external waiters are delivered error="cancelled" (same
  drop semantics as the compute path). Internal staging waiters (added by
  `_stage_raw_tile`) are never stale -- an entry with at least one internal
  waiter always executes and always populates the cache, regardless of
  whether every external waiter for it has gone stale.
- **Raw I/O staging** (docs/v15_viewer_foundation_interfaces.md §4): before a
  compute worker runs a CorrectionKey, it asks
  `compute.raw_keys_for(key)` for the halo-padded tile set and stages any
  cache-missing raw tiles through the SAME single-flight `_pending`
  machinery as external RawKey requests (`_stage_raw_tile`), submitted in
  parallel to the I/O ThreadPoolExecutor (`io_workers` wide). The worker
  blocks on all of them (generous timeout, then proceeds anyway — the
  assembler falls back to direct reads for anything still missing) so raw
  reads for one CorrectionKey are never duplicated across compute workers
  and always run with I/O parallelism instead of serially inside compute.
"""

import dataclasses
import heapq
import itertools
import threading
import time

from .tile_types import CorrectionKey, PixelBuffer, QualityLevel, RawKey, TileResult


class _Entry:
    """Bookkeeping for one in-flight (deduped) key.

    `waiters` are external (req, callback) pairs delivered via `_deliver`
    (subject to generation staleness). `internal_waiters` are plain
    zero-arg callables used by raw-tile staging: they always fire on
    completion (success or error), regardless of any generation, because
    staging has no generation of its own -- it just wants the cache filled.
    """

    __slots__ = ("waiters", "internal_waiters", "started")

    def __init__(self):
        self.waiters = []
        self.internal_waiters = []
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
        self._raw_heap = []  # (priority, seq, key) for RawKey work
        self._seq = itertools.count()
        self._stale_gens = set()
        self._shutdown = False

        self._compute_threads = [
            threading.Thread(target=self._compute_worker, daemon=True, name=f"tile-compute-{i}")
            for i in range(compute_workers)
        ]
        for t in self._compute_threads:
            t.start()

        self._raw_threads = [
            threading.Thread(target=self._raw_worker, daemon=True, name=f"tile-io-{i}")
            for i in range(io_workers)
        ]
        for t in self._raw_threads:
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
            entry = _Entry()
            entry.waiters.append((req, callback))
            self._pending[key] = entry
            if isinstance(key, RawKey):
                heapq.heappush(self._raw_heap, (req.priority, next(self._seq), key))
            else:
                heapq.heappush(self._heap, (req.priority, next(self._seq), key))
            self._cv.notify_all()

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
        for t in self._raw_threads:
            t.join()

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

    def _raw_worker(self):
        while True:
            with self._cv:
                while not self._raw_heap and not self._shutdown:
                    self._cv.wait()
                if self._shutdown and not self._raw_heap:
                    return
                _priority, _seq, key = heapq.heappop(self._raw_heap)
                entry = self._pending.get(key)
                if entry is None:
                    continue  # already resolved/removed concurrently

                waiters_snapshot = list(entry.waiters)
                has_internal = bool(entry.internal_waiters)
                live_external = [w for w in waiters_snapshot if w[0].generation not in self._stale_gens]
                if not live_external and not has_internal:
                    # Nobody still wants this (and no internal staging waiter
                    # forces it): drop without reading.
                    del self._pending[key]
                    to_cancel = waiters_snapshot
                else:
                    entry.started = True
                    to_cancel = None

            if to_cancel is not None:
                for req, cb in to_cancel:
                    cb(TileResult(
                        request=req, pixels=None, quality=QualityLevel.NATIVE,
                        provisional=False, timing={}, error="cancelled",
                    ))
                continue

            self._run_raw(key)

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
        internal_waiters = entry.internal_waiters if entry is not None else []
        for iw in internal_waiters:
            iw()

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

    def _stage_raw_tile(self, raw_key: RawKey, on_done):
        """Ensure `raw_key` is staged into `raw_cache`, deduped via the same
        single-flight `_pending` machinery as external RawKey requests.
        `on_done()` fires exactly once, unconditionally, when the tile is
        either already cached or the (possibly shared) fetch completes."""
        if self.raw_cache.get(raw_key) is not None:
            on_done()
            return

        with self._lock:
            entry = self._pending.get(raw_key)
            if entry is not None:
                entry.internal_waiters.append(on_done)
            else:
                entry = _Entry()
                entry.internal_waiters.append(on_done)
                self._pending[raw_key] = entry
                # Staging has no per-request priority of its own; queue it
                # at the highest priority tier (0) so it is never starved
                # behind a backlog of lower-priority external raw requests.
                heapq.heappush(self._raw_heap, (0, next(self._seq), raw_key))
            self._cv.notify_all()

    def _stage_raw_for(self, key: CorrectionKey):
        """Stage every raw tile `key`'s halo needs (parallel, single-flight),
        blocking the calling compute worker until all are resolved (or a
        generous timeout elapses). Returns (staged_tiles, staging_wall_ms)."""
        missing = [rk for rk in self.compute.raw_keys_for(key) if self.raw_cache.get(rk) is None]
        if not missing:
            return 0, 0.0

        t0 = time.perf_counter()
        event = threading.Event()
        remaining = [len(missing)]
        remaining_lock = threading.Lock()

        def on_done():
            with remaining_lock:
                remaining[0] -= 1
                done = remaining[0] <= 0
            if done:
                event.set()

        for rk in missing:
            self._stage_raw_tile(rk, on_done)

        event.wait(timeout=120.0)  # on timeout, proceed anyway -- assembler falls back
        return len(missing), (time.perf_counter() - t0) * 1000.0

    def _run_compute(self, key: CorrectionKey, entry: _Entry):
        staged_tiles, staging_wall_ms = self._stage_raw_for(key)
        try:
            result = self.compute.compute(key)
            error = None
        except Exception as exc:  # pragma: no cover - defensive
            result, error = None, str(exc)

        if error is None:
            result.timing["staged_tiles"] = staged_tiles
            result.timing["staging_wall_ms"] = staging_wall_ms
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
