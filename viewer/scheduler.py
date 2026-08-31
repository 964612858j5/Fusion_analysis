"""Priority/dedup/cancellation-aware tile scheduler.

Contract (docs/v15_viewer_foundation_interfaces.md §4):

- Dedup is STRICTLY by identity key (RawKey/CorrectionKey), never by
  request generation: a tile already in flight collects every waiter that
  asks for it, and all waiters receive the same TileResult object.
- `generation` on a TileRequest is a delivery token only. It never affects
  whether work runs or whether a result enters a cache. Generation tokens
  are OPAQUE and hashable -- the scheduler never assumes they are ints; it
  only ever puts them in a set (`cancel_generation`) and tests membership
  (`_is_stale`). Callers may use namespaced tuples (e.g. `("raw", 5)` vs
  `("precise", 5)`) so two independent generation counters never collide.
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


# Measured 2026-08-31 (docs/benchmarks/2026-08-31_57ch_multichannel_prefetch.md,
# "Clean rerun" section), real 57-channel PCF slide, compute_workers=4:
#
#   In-motion coverage during a 25-step drag (the regime that matters --
#   a static-viewport sweep is NOT the answer, see that doc):
#       io_workers = 1 -> 46.0%
#       io_workers = 2 -> 98.2%
#       io_workers = 4 -> 98.4%
#       io_workers = 8 -> 98.4%
#
#   Per-thread TIFF handle construction (tifffile's pure-Python OME-XML and
#   page-table parse -- GIL-bound, so more threads does NOT parse faster,
#   it only serialises longer): a worker thread's first read costs 168.3 ms
#   against 0.9 ms for its second, and eight fresh threads' first reads
#   serialise to 126, 218, 295, 401, 517, 648, 748, 865 ms of wall (883 ms
#   total). Handle warm-up wall time by worker count:
#       1 worker  ->  76.9 ms
#       2 workers -> 176.4 ms
#       4 workers -> 393.5 ms
#       8 workers -> 785.3 ms
#
# Read on its own, that drag sweep argues for a small worker count -- and it
# was used to argue exactly that, for 4 instead of 8. Two further
# measurements overturned it, and both are recorded here so the argument is
# not made again from the drag numbers alone.
#
#   ZOOM is a different regime from a drag and is NOT saturated at 2. A
#   drag asks for a trickle of tiles at its leading edge; a level switch
#   asks for a whole new level at once, and that burst does scale with I/O
#   width. In-motion coverage during a 12-step level-crossing zoom, three
#   repeats each, means:
#       io_workers =  2 -> 25.0%
#       io_workers =  4 -> 47.5%
#       io_workers =  6 -> 60.9%
#       io_workers =  8 -> 62.7%
#       io_workers = 12 -> 66.7%
#
#   The 785 ms warm-up never blocks anything. Each I/O worker warms its own
#   handle at thread start and begins serving the moment IT is ready (see
#   `_raw_worker`), so warming overlaps serving instead of gating it. From a
#   cold provider, time from scheduler construction to N tiles delivered:
#       io=4 -> 1st tile 221.5 ms, 20th 366.2 ms
#       io=8 -> 1st tile 117.5 ms, 20th 258.9 ms
#   More workers means one becomes ready sooner AND more capacity comes
#   online during the window, so 8 starts FASTER despite warming longer.
#
# Net, across every axis measured: drag 96.5% (io=8) against 97.0% (io=4),
# inside run-to-run spread; zoom 62.7% against 47.5%; cold start 259 ms
# against 366 ms. 8 it is. Do not lower this on the strength of the drag
# sweep alone -- measure a level-crossing zoom too.
DEFAULT_IO_WORKERS = 8
DEFAULT_COMPUTE_WORKERS = 4


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
                 io_workers: int = DEFAULT_IO_WORKERS,
                 compute_workers: int = DEFAULT_COMPUTE_WORKERS):
        """`io_workers`/`compute_workers` default to `DEFAULT_IO_WORKERS` /
        `DEFAULT_COMPUTE_WORKERS` (see the measured table above this class).

        Each raw-worker thread warms its own provider handle once, at
        thread start, before it ever looks at the queue (see
        `_raw_worker` / `RawTileProvider.warm_thread_handle`). This does
        NOT make the tifffile OME-XML/page-table parse any faster -- it is
        GIL-bound, so N threads parsing still costs roughly N times one
        thread's parse in wall time -- it only moves that fixed cost off
        the interaction path (a user's first pan/zoom) and onto scheduler
        startup, where it can run before there is anything to serve.
        """
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

        # Incremented once per raw-worker thread that successfully warmed
        # its own handle (see `_raw_worker`). A test can assert this equals
        # `io_workers` once all raw threads have started; a thread whose
        # warm-up failed is NOT counted here but still serves requests
        # normally (see `RawTileProvider.warm_thread_handle`).
        self._warmed_workers_lock = threading.Lock()
        self.warmed_workers = 0

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

    def cancel_generation(self, gen):
        """Mark opaque token `gen` stale; drop queued work wanted only by it.

        `gen` may be any hashable value (plain int or a namespaced tuple
        like `("raw", n)`); it is only ever added to a set and tested for
        membership, so its type never affects semantics."""
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
        """Call each waiter's callback with its own TileResult.

        A stale waiter is skipped -- a consumer that can no longer display a
        result has nothing to do with it -- UNLESS it opted in with
        `TileRequest.notify_on_stale_completion`, in which case it gets one
        TERMINAL callback carrying `error="stale"` when the work physically
        finishes. Cancelling a generation does not stop work that has
        already started; a consumer metering its own physical concurrency
        has no other way to learn that such work is over. See that field's
        docstring for why releasing the slot early is not equivalent.
        """
        for req, cb in waiters:
            if self._is_stale(req):
                if getattr(req, "notify_on_stale_completion", False):
                    cb(TileResult(
                        request=req, pixels=None,
                        quality=getattr(req.key, "quality", QualityLevel.NATIVE),
                        provisional=False, timing={}, error="stale"))
                continue
            cb(build_result(req))

    # -- raw path --

    def _raw_worker(self):
        # Warm THIS thread's provider handle once, before the queue loop
        # begins, so the fixed tifffile OME-XML/page-table parse cost (see
        # the measured table above TileScheduler) lands here instead of on
        # a caller's first read. This is done in the worker thread itself
        # (never by submitting warm-up tasks to a pool): a pool gives no
        # guarantee that N warm-up tasks land on N distinct threads, so
        # some threads could stay cold while another warms twice.
        #
        # This does NOT make the parse faster -- it is GIL-bound -- it only
        # moves the cost off the interaction path. It happens BEFORE this
        # thread ever takes `self._cv`, so warming can never hold the queue
        # lock while parsing, and each worker starts serving as soon as ITS
        # OWN warm-up finishes -- there is no barrier / "all ready" gate, so
        # a warm worker can start loading raw tiles while its siblings are
        # still parsing.
        try:
            warmed = self.provider.warm_thread_handle()
        except Exception:  # pragma: no cover - warm_thread_handle already
            # catches everything internally; this is belt-and-suspenders.
            warmed = False
        if warmed:
            with self._warmed_workers_lock:
                self.warmed_workers += 1

        # Shutdown may have been requested while this thread was warming;
        # check immediately so warming can never delay teardown.
        with self._lock:
            if self._shutdown:
                return

        while True:
            with self._cv:
                while not self._raw_heap and not self._shutdown:
                    self._cv.wait()
                if self._shutdown:
                    # Drop queued raw work on shutdown (mirror of the compute
                    # worker): deliver 'cancelled' to external waiters and
                    # fire internal staging waiters so nothing blocks teardown.
                    while self._raw_heap:
                        _p, _s, k = heapq.heappop(self._raw_heap)
                        entry = self._pending.pop(k, None)
                        if entry is None:
                            continue
                        for req, cb in entry.waiters:
                            cb(TileResult(
                                request=req, pixels=None,
                                quality=QualityLevel.NATIVE,
                                provisional=False, timing={}, error="cancelled"))
                        for icb in entry.internal_waiters:
                            icb()
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
                if self._shutdown:
                    # Shutdown DROPS queued work instead of draining it: each
                    # queued compute entry would stage raw reads whose worker
                    # threads may already have exited, stalling teardown on
                    # the 120s staging timeout per entry. Deliver 'cancelled'.
                    while self._heap:
                        _p, _s, k = heapq.heappop(self._heap)
                        entry = self._pending.pop(k, None)
                        if entry is None:
                            continue
                        for req, cb in entry.waiters:
                            cb(TileResult(
                                request=req, pixels=None, quality=req.key.quality,
                                provisional=False, timing={}, error="cancelled"))
                        for icb in entry.internal_waiters:
                            icb()
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

        # Wake up promptly on shutdown too — a raw worker that exited before
        # our staged entries were queued would otherwise leave this waiting
        # for the full timeout during teardown.
        deadline = time.monotonic() + 120.0
        while not event.wait(timeout=0.25):
            if self._shutdown or time.monotonic() >= deadline:
                break                # assembler falls back to direct reads
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
