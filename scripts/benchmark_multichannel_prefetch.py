"""Multichannel prefetch/correction benchmark — MEASUREMENT ONLY.

This script does not wire anything into the live viewer. It reuses the
production scheduling/compute primitives (`block01.viewer.*`,
`block01.core.bg_correction`) against the real 57-channel OME-TIFF.

## Why this file looks the way it does (2026-08-31 rerun)

The previous run (docs/benchmarks/2026-08-31_57ch_multichannel_prefetch.md)
created a FRESH `RawTileProvider` per benchmark cell, so every I/O worker
thread paid its own first-read `tifffile` OME-XML/page-table parse cost
*inside* the timed region: 8 fresh threads' first reads measured 126-865 ms
(wall 883 ms) against a steady-state job of ~200 ms. That inverted the
compute_workers sweep (looked flat at ~950 ms; really 111-212 ms and DOES
scale) and the neighbour +-1 result (looked like 921 ms NOT MET; really
p95 324 ms MET).

Every timed region in *this* rerun starts with every I/O worker thread's
(or thread-pool thread's) TIFF handle already built. `warm_handles()` /
`warm_thread_pool_handles()` force this and are ALWAYS reported as their
own separate measurement, never folded into another timing. They use
tile coordinates far outside the image (guaranteed-empty reads) so warm-up
never shares a cache key with any real measurement.

Channel choices for the neighbour and batch-size cells are drawn from a
seeded RNG (`--seed`) rather than a fixed deterministic offset, and each
cell collects at least 10 samples, so a single lucky/unlucky draw can't set
a reported p95 (this is what made the previous run's "+-2 is 2x faster than
+-1" number incredible — it was two samples, one of which paid the
cold-handle tax).

Every reported number is labelled measured / inferred / proposed. Targets
from the review spec are printed next to measured values with a
MET/NOT MET/INCONCLUSIVE verdict computed strictly from this script's own
measurements.

Usage:
    cd /sda1/Fusion/analysis_pipline/block01_v14
    python scripts/benchmark_multichannel_prefetch.py --quick
    python scripts/benchmark_multichannel_prefetch.py --out /tmp/bench.json
"""

import argparse
import gc
import itertools
import json
import os
import random
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError


def _register_block01_alias():
    import importlib.util
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    existing = sys.modules.get("block01")
    if existing is not None:
        path = getattr(existing, "__file__", "") or ""
        existing_root = pathlib.Path(path).resolve().parent if path else None
        if existing_root == root:
            return
        raise RuntimeError(
            f"'block01' already imported from {existing_root!r}, not {root!r}")
    spec = importlib.util.spec_from_file_location(
        "block01", root / "__init__.py", submodule_search_locations=[str(root)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["block01"] = mod
    spec.loader.exec_module(mod)
    sys.path.insert(0, str(root.parent))


_register_block01_alias()

import numpy as np  # noqa: E402

from block01.core import bg_correction  # noqa: E402
from block01.viewer.caches import LRUByteCache  # noqa: E402
from block01.viewer.correction_compute import CorrectionCompute  # noqa: E402
from block01.viewer.explore_view import (  # noqa: E402
    FLOOR_MAX_PIXELS,
    FLOOR_MIN_MAX_DIM,
    _pick_calibration_windows,
)
from block01.viewer.raw_tile_provider import RawTileProvider  # noqa: E402
from block01.viewer.scheduler import TileScheduler  # noqa: E402
from block01.viewer.tile_types import (  # noqa: E402
    CorrectionKey,
    QualityLevel,
    RawKey,
    TileAddress,
    TileGridSpec,
    TileRequest,
    effective_param,
)

try:
    import psutil
    _PSUTIL = True
except Exception:
    _PSUTIL = False

try:
    import cupy as cp
    _CUPY = True
except Exception:
    cp = None
    _CUPY = False

DEFAULT_PATH = (
    "/sda1/Albert/fusion/20260210/"
    "20260210_Ming_Albert_HCC_test_01_Scan1.tiff.ome.tif"
)

TILE_SIZE = 512
BASE_PARAM = 25
DEFAULT_SEED = 20260831

# Tile coordinates used ONLY for handle warm-up: far enough outside any real
# image (max plausible level-0 extent is nowhere near 900000*512 px) that
# read_region clamps them to an empty slice — a valid, cheap, zero-data read
# that still forces the full per-thread TiffFile/zarr handle-open path, and
# whose RawKey can never collide with a real measurement's cache key.
_WARMUP_TILE_BASE = 900_000

# Targets from the review spec (printed next to measurements; verdicts are
# computed only from this script's own numbers).
TARGET_NEIGHBOR1_P95_MS = 500.0
TARGET_NEIGHBOR2_P95_MS = 1000.0
TARGET_FAR_CLICK_P95_MS = 2000.0
TARGET_BG_DEGRADATION_FRAC = 0.10

MEASUREMENTS = []  # list of dicts: id, label, description, value, unit, target, verdict, notes


def record(mid, label, description, value=None, unit=None, target=None, verdict=None, notes=None):
    assert label in ("measured", "inferred", "proposed")
    MEASUREMENTS.append({
        "id": mid, "label": label, "description": description,
        "value": value, "unit": unit, "target": target, "verdict": verdict,
        "notes": notes,
    })


def log(msg):
    print(f"[bench] {time.strftime('%H:%M:%S')} {msg}", flush=True)


def rss_mb():
    if not _PSUTIL:
        return None
    return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)


def gpu_mem_mb():
    if not _CUPY:
        return None
    try:
        pool = cp.get_default_memory_pool()
        return {
            "used_mb": pool.used_bytes() / (1024 * 1024),
            "total_mb": pool.total_bytes() / (1024 * 1024),
        }
    except Exception:
        return None


def run_with_timeout(fn, timeout_s, *args, **kwargs):
    """Run fn in a worker thread; return (result, error, timed_out).

    If the call times out, the underlying thread is abandoned (daemon-ish
    best effort — the ThreadPoolExecutor is not joined), so a stall is
    visible in output instead of hanging the whole benchmark."""
    ex = ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(fn, *args, **kwargs)
    try:
        result = fut.result(timeout=timeout_s)
        ex.shutdown(wait=False)
        return result, None, False
    except FutureTimeoutError:
        ex.shutdown(wait=False)
        return None, f"TIMEOUT after {timeout_s}s", True
    except Exception as exc:
        ex.shutdown(wait=False)
        return None, f"{type(exc).__name__}: {exc}", False


# ── OS page cache control (THIS FILE ONLY — never the global page cache) ──

def evict_os_cache(path):
    """posix_fadvise(..., DONTNEED) for the whole of `path`. This drops the
    OS page cache's copy of THIS file's pages only; it never touches the
    machine-wide cache and never calls drop_caches."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    finally:
        os.close(fd)


def cache_state_note(os_state, raw_state, corrected_state, extra=None):
    """Build the mandatory 'which of the three caches was cold/warm' note."""
    note = (f"cache state: OS page cache={os_state}, raw LRU={raw_state}, "
            f"corrected LRU={corrected_state}")
    if extra:
        note = f"{note}; {extra}"
    return note


# Default note attached to every scheduler-backed cell that does NOT
# deliberately manage OS cache state itself: this script reads the same
# file repeatedly over the course of a run, so by the time most cells run
# the OS page cache is warm for previously-touched regions; each such cell
# always uses freshly-constructed LRUByteCache instances (raw + corrected),
# so those two are cold. The one cell that deliberately varies OS-cache
# state is cell_cache_state_headline, below.
DEFAULT_CACHE_NOTE = cache_state_note(
    "warm (file read repeatedly earlier in this run; not reset for this cell)",
    "cold (fresh LRUByteCache for this cell)",
    "cold (fresh LRUByteCache for this cell)",
)


# ── viewport / tile-grid helpers ─────────────────────────────────────────

def pick_overview_level(provider):
    """Mirror ExploreController._pick_floor_level_and_stride's level choice
    (imported constants, same logic) — coarsest level whose long side is
    >= FLOOR_MIN_MAX_DIM and whose pixel count is <= FLOOR_MAX_PIXELS, else
    the coarsest level meeting FLOOR_MIN_MAX_DIM alone, else the coarsest
    level overall."""
    num_levels = provider.num_levels
    qualifying = []
    for level in range(num_levels):
        h, w = provider.level_shape(level)
        if max(h, w) >= FLOOR_MIN_MAX_DIM and h * w <= FLOOR_MAX_PIXELS:
            qualifying.append(level)
    if qualifying:
        return max(qualifying)
    big_enough = [L for L in range(num_levels)
                  if max(provider.level_shape(L)) >= FLOOR_MIN_MAX_DIM]
    if big_enough:
        return max(big_enough)
    return num_levels - 1


def pick_viewport_tile_origin(provider, channel, cols, rows):
    """Pick a tissue-dense (ty0, tx0) tile-grid origin for a `rows` x `cols`
    (512px) tile viewport, using block-means over a coarse overview level —
    same method as explore_view.py::_pick_calibration_windows. Returns
    (overview_level, ty0, tx0, y0_l0, x0_l0)."""
    overview_level = pick_overview_level(provider)
    h_ov, w_ov = provider.level_shape(overview_level)
    arr, _off = provider.read_region(channel, overview_level, 0, h_ov, 0, w_ov)
    arr = arr.astype(np.float32, copy=False)
    ds_y, ds_x = provider.level_downsample_yx(overview_level)
    window_l0 = TILE_SIZE * max(cols, rows)
    windows = _pick_calibration_windows(arr, ds_y, ds_x, window_l0=window_l0, n_windows=1)
    if not windows:
        y0_l0, x0_l0 = 0, 0
    else:
        y0_l0, x0_l0 = windows[0]

    h0, w0 = provider.level_shape(0)
    ty0 = y0_l0 // TILE_SIZE
    tx0 = x0_l0 // TILE_SIZE
    max_ty0 = max(0, (h0 // TILE_SIZE) - rows)
    max_tx0 = max(0, (w0 // TILE_SIZE) - cols)
    ty0 = min(max(0, ty0), max_ty0)
    tx0 = min(max(0, tx0), max_tx0)
    return overview_level, ty0, tx0, ty0 * TILE_SIZE, tx0 * TILE_SIZE


def make_tiles(grid, ty0, tx0, rows, cols, level=0):
    return [
        TileAddress(grid=grid, level=level, tx=tx0 + c, ty=ty0 + r)
        for r in range(rows) for c in range(cols)
    ]


def make_key(source, channel, tile, method, param, algo_version):
    return CorrectionKey(
        source=source, channel=channel, tile=tile, method=method,
        params=(int(param),), algorithm_version=algo_version,
        quality=QualityLevel.NATIVE,
    )


# ── handle warm-up — the core fix for this rerun ─────────────────────────

def warm_handles(scheduler, provider, io_workers, source, grid, timeout_s=30.0, max_rounds=10):
    """Force every I/O worker thread's per-thread TiffFile/zarr handle to be
    built BEFORE any timed region begins.

    Issues RawKey requests at out-of-image tile coordinates (guaranteed
    empty reads, guaranteed not to collide with any real cache key) — many
    more per round than `io_workers`, so with every raw-worker thread idle
    and waiting on the condition variable, each is overwhelmingly likely to
    pick up at least one. Verified, not just hoped for: `provider.open_count`
    (incremented once per `tifffile.TiffFile(...)` open, including the one
    done by the constructor) must reach `1 + io_workers` — one open per
    raw-worker thread, plus the constructor's. If it hasn't after a round,
    another round is issued (up to `max_rounds`).

    Returns a dict describing the warm-up itself — this is reported as ITS
    OWN measurement, never folded into another cell's timing.
    """
    target_open_count = 1 + io_workers
    t_start = time.perf_counter()
    seq = itertools.count(1)
    rounds = 0
    n_per_round = max(io_workers * 6, 16)
    while provider.open_count < target_open_count and rounds < max_rounds:
        rounds += 1
        n_total = n_per_round
        done = threading.Event()
        remaining = [n_total]
        lock = threading.Lock()

        def cb(_tr):
            with lock:
                remaining[0] -= 1
                if remaining[0] <= 0:
                    done.set()

        for _ in range(n_total):
            n = next(seq)
            tile = TileAddress(grid=grid, level=0,
                                tx=_WARMUP_TILE_BASE + n, ty=_WARMUP_TILE_BASE + n)
            key = RawKey(source=source, channel=0, tile=tile)
            req = TileRequest(key=key, generation=("warm", rounds), priority=-999999)
            scheduler.request(req, cb)
        done.wait(timeout=timeout_s)

    wall_ms = (time.perf_counter() - t_start) * 1000.0
    result = {
        "wall_ms": wall_ms,
        "rounds": rounds,
        "io_workers": io_workers,
        "open_count_final": provider.open_count,
        "target_open_count": target_open_count,
        "all_threads_warmed": provider.open_count >= target_open_count,
    }
    return result


def record_warmup(label, warm_result):
    verdict = None
    notes = (f"rounds={warm_result['rounds']}, open_count={warm_result['open_count_final']}"
             f"/{warm_result['target_open_count']}")
    if not warm_result["all_threads_warmed"]:
        notes += " — NOT all raw-worker threads confirmed warmed within max_rounds"
    record(f"warmup.{label}.wall_ms", "measured",
           f"handle warm-up wall time for {label} (io_workers={warm_result['io_workers']}) "
           "— reported separately, never inside another timing",
           value=warm_result["wall_ms"], unit="ms", notes=notes)
    return warm_result["all_threads_warmed"]


def warm_thread_pool_handles(executor, provider, n_threads, timeout_s=30.0, max_rounds=10):
    """Same idea as `warm_handles`, but for a plain ThreadPoolExecutor (used
    by the CUDA-streams cell) instead of the scheduler's raw-worker threads:
    submits enough out-of-image `provider.read_tile` calls directly to the
    pool that every pool thread very likely executes at least one, looping
    until `provider.open_count` reaches `1 + n_threads` or `max_rounds`."""
    target_open_count = 1 + n_threads
    t_start = time.perf_counter()
    seq = itertools.count(1)
    rounds = 0
    n_per_round = max(n_threads * 6, 16)

    grid = TileGridSpec(tile_size=TILE_SIZE)
    while provider.open_count < target_open_count and rounds < max_rounds:
        rounds += 1
        futs = []
        for _ in range(n_per_round):
            n = next(seq)
            tile = TileAddress(grid=grid, level=0,
                                tx=_WARMUP_TILE_BASE + n, ty=_WARMUP_TILE_BASE + n)
            futs.append(executor.submit(provider.read_tile, 0, tile))
        deadline = time.perf_counter() + timeout_s
        for f in futs:
            remaining = max(0.0, deadline - time.perf_counter())
            try:
                f.result(timeout=remaining)
            except Exception:
                pass

    wall_ms = (time.perf_counter() - t_start) * 1000.0
    return {
        "wall_ms": wall_ms,
        "rounds": rounds,
        "n_threads": n_threads,
        "open_count_final": provider.open_count,
        "target_open_count": target_open_count,
        "all_threads_warmed": provider.open_count >= target_open_count,
    }


# ── sequential (single-threaded) baseline — also the CORRECTNESS baseline ──

def sequential_viewport(provider, raw_cache, method, channel, tiles, source, algo_version):
    """Compute every tile in `tiles` for `channel`/`method`, strictly one at
    a time on the calling thread (no scheduler, no thread pool). Returns
    (results_by_tile, per_tile_timings_ms, first_tile_ms, total_ms)."""
    compute = CorrectionCompute(provider, raw_cache)
    results = {}
    per_tile = []
    t_start = time.perf_counter()
    first_tile_ms = None
    for i, tile in enumerate(tiles):
        key = make_key(source, channel, tile, method, BASE_PARAM, algo_version)
        t0 = time.perf_counter()
        tr = compute.compute(key)
        dt_ms = (time.perf_counter() - t0) * 1000.0
        if i == 0:
            first_tile_ms = dt_ms
        results[tile] = tr.pixels.handle
        per_tile.append({"tile": (tile.tx, tile.ty), "ms": dt_ms, "timing": tr.timing})
    total_ms = (time.perf_counter() - t_start) * 1000.0
    return results, per_tile, first_tile_ms, total_ms


def compare_to_baseline(baseline_results, candidate_results):
    """Return (all_match, max_abs_diff, n_compared, n_missing)."""
    max_diff = 0.0
    n_compared = 0
    n_missing = 0
    for tile, base_arr in baseline_results.items():
        cand_arr = candidate_results.get(tile)
        if cand_arr is None:
            n_missing += 1
            continue
        if base_arr.shape != cand_arr.shape:
            n_missing += 1
            continue
        diff = float(np.max(np.abs(base_arr.astype(np.float64) - cand_arr.astype(np.float64))))
        max_diff = max(max_diff, diff)
        n_compared += 1
    all_match = (max_diff == 0.0) and (n_missing == 0) and (n_compared > 0)
    return all_match, max_diff, n_compared, n_missing


# ── scheduler-backed viewport fetch ──────────────────────────────────────

def scheduler_fetch_channels(provider, source, algo_version, grid, tiles, method,
                              channels, io_workers, compute_workers,
                              raw_cache_bytes=256 * 1024 * 1024,
                              corrected_cache_bytes=512 * 1024 * 1024,
                              priority_fn=None, timeout_s=60.0,
                              raw_cache=None, corrected_cache=None,
                              scheduler=None, compute=None):
    """Request every (channel, tile) in `channels` x `tiles` concurrently.

    If `scheduler` is given, it is reused as-is (its own io_workers/
    compute_workers apply; the `io_workers`/`compute_workers` args here are
    then only used for the returned bundle's bookkeeping) — this is how
    callers avoid paying a fresh warm-up cost for every measurement. If not
    given, a fresh scheduler (+ caches, unless passed in) is built.

    Returns a dict with:
      - total_ms: TRUE WALL TIME for the whole batch (measured once, from
        the first request submitted to the last callback firing).
      - aggregate_service_ms: SUM of each individual tile's own reported
        service time (`tr.timing["total_ms"]`, i.e. compute-side kernel+
        residual-io time for CorrectionKey results, or io_ms for RawKey
        results — NOT including scheduler queue-wait). This is inflated
        relative to total_ms by however much work ran concurrently; the
        ratio between the two numbers is NOT a speedup factor and must not
        be reported as one.
      - first_ms: per-channel first-tile arrival time (wall clock).
    """
    owns_scheduler = scheduler is None
    if owns_scheduler:
        if raw_cache is None:
            raw_cache = LRUByteCache(raw_cache_bytes)
        if corrected_cache is None:
            corrected_cache = LRUByteCache(corrected_cache_bytes)
        compute = CorrectionCompute(provider, raw_cache)
        scheduler = TileScheduler(provider, compute, raw_cache, corrected_cache,
                                   io_workers=io_workers, compute_workers=compute_workers)

    gen = ("bench", id(tiles), time.perf_counter())
    n_total = len(channels) * len(tiles)
    done_event = threading.Event()
    lock = threading.Lock()
    state = {"count": 0, "first_ms": {}, "results": {ch: {} for ch in channels},
             "service_ms": [], "t_start": time.perf_counter(), "errors": []}

    def make_cb(ch, tile):
        def cb(tr):
            with lock:
                state["count"] += 1
                if ch not in state["first_ms"]:
                    state["first_ms"][ch] = (time.perf_counter() - state["t_start"]) * 1000.0
                if tr.error is not None:
                    state["errors"].append((ch, tile, tr.error))
                else:
                    state["results"][ch][tile] = tr.pixels.handle
                    svc = (tr.timing or {}).get("total_ms")
                    if svc is not None:
                        state["service_ms"].append(svc)
                if state["count"] >= n_total:
                    done_event.set()
        return cb

    for ch in channels:
        for i, tile in enumerate(tiles):
            key = make_key(source, ch, tile, method, BASE_PARAM, algo_version)
            prio = priority_fn(ch, i) if priority_fn else i
            req = TileRequest(key=key, generation=gen, priority=prio)
            scheduler.request(req, make_cb(ch, tile))

    finished = done_event.wait(timeout=timeout_s)
    total_ms = (time.perf_counter() - state["t_start"]) * 1000.0
    aggregate_service_ms = sum(state["service_ms"]) if state["service_ms"] else None
    return {
        "finished": finished,
        "total_ms": total_ms,
        "aggregate_service_ms": aggregate_service_ms,
        "first_ms": dict(state["first_ms"]),
        "results": state["results"],
        "errors": state["errors"],
        "scheduler": scheduler,
        "raw_cache": raw_cache,
        "corrected_cache": corrected_cache,
        "compute": compute,
        "owns_scheduler": owns_scheduler,
    }


def teardown(bundle_or_scheduler, provider=None):
    sched = bundle_or_scheduler["scheduler"] if isinstance(bundle_or_scheduler, dict) else bundle_or_scheduler
    if isinstance(bundle_or_scheduler, dict) and not bundle_or_scheduler.get("owns_scheduler", True):
        return  # caller owns this scheduler's lifecycle; do not shut it down here
    try:
        sched.shutdown()
    except Exception:
        pass
    if provider is not None:
        try:
            provider.close()
        except Exception:
            pass


def build_warmed_scheduler(path, source, io_workers, compute_workers, grid,
                            raw_cache_bytes=256 * 1024 * 1024,
                            corrected_cache_bytes=512 * 1024 * 1024,
                            warmup_label=None, warmup_timeout_s=30.0):
    """Open a fresh provider, build its scheduler, and warm every I/O
    worker thread's handle before returning. Returns
    (provider, raw_cache, corrected_cache, compute, scheduler, warm_result)."""
    provider = RawTileProvider(path)
    raw_cache = LRUByteCache(raw_cache_bytes)
    corrected_cache = LRUByteCache(corrected_cache_bytes)
    compute = CorrectionCompute(provider, raw_cache)
    scheduler = TileScheduler(provider, compute, raw_cache, corrected_cache,
                              io_workers=io_workers, compute_workers=compute_workers)
    warm_result = warm_handles(scheduler, provider, io_workers, source, grid,
                                timeout_s=warmup_timeout_s)
    if warmup_label is not None:
        record_warmup(warmup_label, warm_result)
    return provider, raw_cache, corrected_cache, compute, scheduler, warm_result


# ── benchmark cells ───────────────────────────────────────────────────────

def cell_current_viewport(path, channel, tiles, source, algo_version, methods, timeout_s):
    """#1/#2: current viewport (~20 tiles), one channel, first-tile / full-
    coverage wall time, per method — SEQUENTIAL single-threaded, so no
    scheduler I/O-worker threads are involved (the contamination this rerun
    fixes does not apply to this cell)."""
    out = {}
    for method in methods:
        provider = RawTileProvider(path)
        raw_cache = LRUByteCache(256 * 1024 * 1024)

        def _do():
            return sequential_viewport(provider, raw_cache, method, channel, tiles, source, algo_version)

        (res, per_tile, first_ms, total_ms), err, timed_out = run_with_timeout(_do, timeout_s)
        if err:
            record(f"viewport.{method}", "measured",
                   f"current viewport ({len(tiles)} tiles) method={method}: FAILED",
                   value=None, notes=err)
            out[method] = {"failed": True, "error": err}
        else:
            record(f"viewport.{method}.first_tile_ms", "measured",
                   f"first-tile wall time, method={method}", value=first_ms, unit="ms",
                   notes=DEFAULT_CACHE_NOTE)
            record(f"viewport.{method}.full_coverage_ms", "measured",
                   f"full-coverage wall time ({len(tiles)} tiles), method={method}",
                   value=total_ms, unit="ms", notes=DEFAULT_CACHE_NOTE)
            out[method] = {"results": res, "per_tile": per_tile,
                            "first_tile_ms": first_ms, "total_ms": total_ms}
        provider.close()
    return out


def cell_cache_state_headline(path, source, algo_version, grid, tiles, channel,
                               io_workers, compute_workers, method, timeout_s):
    """Item 5: one headline cell run under all three cache-state
    combinations that matter, each labelled plainly:
      1. OS page cache COLD (evicted for this file only), raw LRU cold,
         corrected LRU cold.
      2. OS page cache WARM (from run 1's own reads), raw LRU cold,
         corrected LRU cold.
      3. OS page cache WARM, raw LRU WARM, corrected LRU WARM (caches
         reused, unlike every other cell in this script)."""
    provider, raw_cache, corrected_cache, compute, scheduler, warm_result = build_warmed_scheduler(
        path, source, io_workers, compute_workers, grid, warmup_label="cache_state_headline")

    evict_os_cache(path)
    bundle1, err1, _ = run_with_timeout(
        lambda: scheduler_fetch_channels(provider, source, algo_version, grid, tiles, method,
                                          [channel], io_workers, compute_workers,
                                          raw_cache=raw_cache, corrected_cache=corrected_cache,
                                          scheduler=scheduler, compute=compute, timeout_s=timeout_s),
        timeout_s + 15)
    if err1:
        record("cache_state.os_cold.full_coverage_ms", "measured",
               "OS-cold headline run FAILED", notes=err1)
    else:
        record("cache_state.os_cold.full_coverage_ms", "measured",
               f"headline viewport, io_workers={io_workers} compute_workers={compute_workers}, "
               f"method={method}: full-coverage TRUE WALL TIME",
               value=bundle1["total_ms"], unit="ms",
               notes=cache_state_note("cold (evicted for this file just before this run)",
                                      "cold (fresh)", "cold (fresh)"))
        record("cache_state.os_cold.aggregate_service_ms", "measured",
               "same run: AGGREGATE service time summed across tiles (inflated by parallelism; "
               "do not divide by total_ms without saying so)",
               value=bundle1["aggregate_service_ms"], unit="ms")

    raw_cache.clear()
    corrected_cache.clear()
    bundle2, err2, _ = run_with_timeout(
        lambda: scheduler_fetch_channels(provider, source, algo_version, grid, tiles, method,
                                          [channel], io_workers, compute_workers,
                                          raw_cache=raw_cache, corrected_cache=corrected_cache,
                                          scheduler=scheduler, compute=compute, timeout_s=timeout_s),
        timeout_s + 15)
    if err2:
        record("cache_state.os_warm_app_cold.full_coverage_ms", "measured",
               "OS-warm/app-cold headline run FAILED", notes=err2)
    else:
        record("cache_state.os_warm_app_cold.full_coverage_ms", "measured",
               f"headline viewport, io_workers={io_workers} compute_workers={compute_workers}, "
               f"method={method}: full-coverage TRUE WALL TIME",
               value=bundle2["total_ms"], unit="ms",
               notes=cache_state_note("warm (from run 1's own reads, not re-evicted)",
                                      "cold (cleared just before this run)",
                                      "cold (cleared just before this run)"))

    bundle3, err3, _ = run_with_timeout(
        lambda: scheduler_fetch_channels(provider, source, algo_version, grid, tiles, method,
                                          [channel], io_workers, compute_workers,
                                          raw_cache=raw_cache, corrected_cache=corrected_cache,
                                          scheduler=scheduler, compute=compute, timeout_s=timeout_s),
        timeout_s + 15)
    if err3:
        record("cache_state.all_warm.full_coverage_ms", "measured",
               "all-warm headline run FAILED", notes=err3)
    else:
        record("cache_state.all_warm.full_coverage_ms", "measured",
               f"headline viewport, io_workers={io_workers} compute_workers={compute_workers}, "
               f"method={method}: full-coverage TRUE WALL TIME",
               value=bundle3["total_ms"], unit="ms",
               notes=cache_state_note("warm", "warm (reused from run 2, not cleared)",
                                      "warm (reused from run 2, not cleared)"))

    scheduler.shutdown()
    provider.close()
    return {"os_cold": bundle1, "os_warm_app_cold": bundle2, "all_warm": bundle3}


def cell_io_workers_sweep(path, source, algo_version, grid, tiles, channel,
                           io_worker_values, compute_workers_fixed, method,
                           timeout_s, baseline_results):
    """Item 1: io_workers in io_worker_values, compute_workers FIXED. Each
    config gets its own fresh provider/scheduler and its own reported
    warm-up. Returns the io_workers value with the lowest measured
    full-coverage time among successful runs (an INFERRED pick, used only
    to save time in later sweeps — it is not asserted as globally optimal)."""
    results = {}
    for io_workers in io_worker_values:
        provider, raw_cache, corrected_cache, compute, scheduler, warm_result = build_warmed_scheduler(
            path, source, io_workers, compute_workers_fixed, grid,
            warmup_label=f"io{io_workers}")

        bundle, err, _ = run_with_timeout(
            lambda: scheduler_fetch_channels(provider, source, algo_version, grid, tiles, method,
                                              [channel], io_workers, compute_workers_fixed,
                                              raw_cache=raw_cache, corrected_cache=corrected_cache,
                                              scheduler=scheduler, compute=compute, timeout_s=timeout_s),
            timeout_s + 15)
        scheduler.shutdown()
        provider.close()
        if err:
            record(f"io_sweep.io{io_workers}.full_coverage_ms", "measured",
                   f"io_workers={io_workers} compute_workers={compute_workers_fixed}: FAILED",
                   notes=err)
            results[io_workers] = {"failed": True, "error": err}
            continue

        match = None
        if channel in bundle["results"] and method in baseline_results:
            match, max_diff, n_cmp, n_missing = compare_to_baseline(
                baseline_results[method]["results"], bundle["results"][channel])
        note = DEFAULT_CACHE_NOTE
        if match is False:
            note = "output MISMATCHES sequential baseline — timing suppressed"
        record(f"io_sweep.io{io_workers}.first_tile_ms", "measured",
               f"io_workers={io_workers} compute_workers={compute_workers_fixed}: first-tile "
               "TRUE WALL TIME", value=(bundle["first_ms"].get(channel) if match is not False else None),
               unit="ms", notes=note)
        record(f"io_sweep.io{io_workers}.full_coverage_ms", "measured",
               f"io_workers={io_workers} compute_workers={compute_workers_fixed}: full-coverage "
               f"({len(tiles)} tiles) TRUE WALL TIME",
               value=(bundle["total_ms"] if match is not False else None), unit="ms", notes=note)
        record(f"io_sweep.io{io_workers}.aggregate_service_ms", "measured",
               f"io_workers={io_workers}: AGGREGATE service time (sum across tiles, inflated by "
               "parallelism — not a ratio numerator/denominator with total_ms)",
               value=bundle["aggregate_service_ms"], unit="ms")
        results[io_workers] = {"total_ms": bundle["total_ms"] if match is not False else None,
                                "match": match}

    successful = {k: v for k, v in results.items() if v.get("total_ms") is not None}
    winner = min(successful, key=lambda k: successful[k]["total_ms"]) if successful else io_worker_values[0]
    record("io_sweep.winner", "inferred",
           "io_workers value with the lowest measured full-coverage time in this sweep "
           "— used to save time in later sweeps, NOT asserted as globally optimal",
           value=winner)
    return winner, results


def cell_compute_workers_sweep(path, source, algo_version, grid, tiles, channel,
                                io_workers_fixed, compute_worker_values, method,
                                timeout_s, baseline_results):
    """Item 2: compute_workers in compute_worker_values, io_workers FIXED
    at the winner from item 1 (or a caller-supplied value)."""
    results = {}
    for cw in compute_worker_values:
        provider, raw_cache, corrected_cache, compute, scheduler, warm_result = build_warmed_scheduler(
            path, source, io_workers_fixed, cw, grid, warmup_label=f"compute{cw}")

        bundle, err, _ = run_with_timeout(
            lambda: scheduler_fetch_channels(provider, source, algo_version, grid, tiles, method,
                                              [channel], io_workers_fixed, cw,
                                              raw_cache=raw_cache, corrected_cache=corrected_cache,
                                              scheduler=scheduler, compute=compute, timeout_s=timeout_s),
            timeout_s + 15)
        scheduler.shutdown()
        provider.close()
        if err:
            record(f"compute_sweep.compute{cw}.full_coverage_ms", "measured",
                   f"io_workers={io_workers_fixed} compute_workers={cw}: FAILED", notes=err)
            results[cw] = {"failed": True, "error": err}
            continue

        match = None
        if channel in bundle["results"] and method in baseline_results:
            match, max_diff, n_cmp, n_missing = compare_to_baseline(
                baseline_results[method]["results"], bundle["results"][channel])
        note = DEFAULT_CACHE_NOTE
        if match is False:
            note = "output MISMATCHES sequential baseline — timing suppressed"
        record(f"compute_sweep.compute{cw}.first_tile_ms", "measured",
               f"io_workers={io_workers_fixed} compute_workers={cw}: first-tile TRUE WALL TIME",
               value=(bundle["first_ms"].get(channel) if match is not False else None),
               unit="ms", notes=note)
        record(f"compute_sweep.compute{cw}.full_coverage_ms", "measured",
               f"io_workers={io_workers_fixed} compute_workers={cw}: full-coverage "
               f"({len(tiles)} tiles) TRUE WALL TIME",
               value=(bundle["total_ms"] if match is not False else None), unit="ms", notes=note)
        record(f"compute_sweep.compute{cw}.aggregate_service_ms", "measured",
               f"compute_workers={cw}: AGGREGATE service time (sum across tiles, inflated by "
               "parallelism)", value=bundle["aggregate_service_ms"], unit="ms")
        results[cw] = {"total_ms": bundle["total_ms"] if match is not False else None, "match": match}

    successful = {k: v for k, v in results.items() if v.get("total_ms") is not None}
    winner = min(successful, key=lambda k: successful[k]["total_ms"]) if successful else compute_worker_values[0]
    record("compute_sweep.winner", "inferred",
           "compute_workers value with the lowest measured full-coverage time in this sweep "
           "— used for the batch-size/neighbour cells below, NOT asserted as globally optimal",
           value=winner)
    return winner, results


def _percentiles(samples):
    if not samples:
        return None, None, None
    return (float(np.percentile(samples, 50)), float(np.percentile(samples, 95)), float(max(samples)))


def cell_batch_sizes_v2(path, source, algo_version, grid, tiles, channel0, num_channels,
                         method, batch_sizes, io_workers, compute_workers, timeout_s,
                         baseline_results, rng, reps):
    """Item 3 (rerun) + item 4 (randomised order): one persistent, warmed
    scheduler reused across every batch size and rep (so the reported
    warm-up cost is paid exactly once, honestly, and each rep's measurement
    is not contaminated by it). Channels for every rep are drawn via
    `rng.sample` from the full channel population — NOT a fixed
    (channel0..channel0+batch) window — with >= `reps` samples per batch
    size so a single outlier can't set the reported p95."""
    provider, raw_cache, corrected_cache, compute, scheduler, warm_result = build_warmed_scheduler(
        path, source, io_workers, compute_workers, grid, warmup_label="batch_sweep")

    all_channels = list(range(num_channels))
    out = {}
    for batch in batch_sizes:
        wall_samples = []
        service_samples = []
        n_mismatch = 0
        n_correctness_checked = 0
        for rep in range(reps):
            raw_cache.clear()
            corrected_cache.clear()
            channels = rng.sample(all_channels, min(batch, num_channels))

            def _do(channels=channels):
                return scheduler_fetch_channels(
                    provider, source, algo_version, grid, tiles, method, channels,
                    io_workers, compute_workers, raw_cache=raw_cache,
                    corrected_cache=corrected_cache, scheduler=scheduler, compute=compute,
                    timeout_s=timeout_s)

            bundle, err, _ = run_with_timeout(_do, timeout_s + 15)
            if err:
                record(f"batch.n{batch}.rep{rep}", "measured",
                       f"batch={batch} rep={rep} channels={channels}: FAILED", notes=err)
                continue

            match = None
            if channel0 in channels and channel0 in bundle["results"] and method in baseline_results:
                n_correctness_checked += 1
                match, max_diff, n_cmp, n_missing = compare_to_baseline(
                    baseline_results[method]["results"], bundle["results"][channel0])
                if match is False:
                    n_mismatch += 1
                    continue  # per spec: suppress timing for a mismatching config

            wall_samples.append(bundle["total_ms"])
            if bundle["aggregate_service_ms"] is not None:
                service_samples.append(bundle["aggregate_service_ms"])

        p50_wall, p95_wall, max_wall = _percentiles(wall_samples)
        p50_svc, p95_svc, max_svc = _percentiles(service_samples)
        note = (f"n={len(wall_samples)}/{reps} reps usable, seed-drawn random channels each rep, "
                f"correctness checked on {n_correctness_checked} rep(s) that happened to include "
                f"channel {channel0} (mismatches suppressed: {n_mismatch}); {DEFAULT_CACHE_NOTE}")
        if wall_samples:
            record(f"batch.n{batch}.wall_ms", "measured",
                   f"batch={batch} channels, method={method}: TRUE WALL TIME distribution over "
                   f"{len(wall_samples)} reps (random channel draws)",
                   value={"p50": p50_wall, "p95": p95_wall, "max": max_wall, "samples": wall_samples},
                   unit="ms", notes=note)
        else:
            record(f"batch.n{batch}.wall_ms", "measured",
                   f"batch={batch}: no usable samples", value=None, verdict="INCONCLUSIVE",
                   notes=note)
        if service_samples:
            record(f"batch.n{batch}.aggregate_service_ms", "measured",
                   f"batch={batch} channels, method={method}: AGGREGATE service time distribution "
                   "(summed per-tile, inflated by parallelism; never divide by wall_ms without "
                   "saying so)",
                   value={"p50": p50_svc, "p95": p95_svc, "max": max_svc, "samples": service_samples},
                   unit="ms")
        out[batch] = {"wall_samples": wall_samples, "service_samples": service_samples}

    scheduler.shutdown()
    provider.close()
    return out


def cell_neighbors_v2(path, source, algo_version, grid, tiles, num_channels,
                       io_workers, compute_workers, method, timeout_s, rng, reps):
    """Item 4/6: from a settled state, measure +-1 and +-2 neighbour
    full-coverage prep time — but the "settled" base channel is drawn fresh
    (seeded RNG) for every rep, so channel IDENTITY is decorrelated from
    offset DISTANCE across the sample set. >= 10 samples per label
    (reps * 2 offsets)."""
    provider, raw_cache, corrected_cache, compute, scheduler, warm_result = build_warmed_scheduler(
        path, source, io_workers, compute_workers, grid, warmup_label="neighbor_sweep")

    def fetch_one(ch, gen_id, tag):
        gen = ("settle", gen_id)
        done = threading.Event()
        t0 = time.perf_counter()
        remaining = [len(tiles)]
        rl = threading.Lock()

        def cb(tr):
            with rl:
                remaining[0] -= 1
                if remaining[0] <= 0:
                    done.set()
        for i, tile in enumerate(tiles):
            key = make_key(source, ch, tile, method, BASE_PARAM, algo_version)
            scheduler.request(TileRequest(key=key, generation=gen, priority=i), cb)
        ok = done.wait(timeout=timeout_s)
        return (time.perf_counter() - t0) * 1000.0 if ok else None

    offsets = {"+-1": [1, -1], "+-2": [2, -2]}
    samples_by_label = {"+-1": [], "+-2": []}
    seq = itertools.count()
    for rep in range(reps):
        base = rng.randrange(num_channels)
        raw_cache.clear()
        corrected_cache.clear()
        # Settle the random base channel first (not timed as a neighbour result).
        fetch_one(base, f"settle-{rep}", "settle")
        for label, offs in offsets.items():
            for off in offs:
                ch = (base + off) % num_channels
                ms = fetch_one(ch, f"{label}-{rep}-{off}-{next(seq)}", label)
                if ms is not None:
                    samples_by_label[label].append(ms)

    out = {}
    for label, samples in samples_by_label.items():
        target = TARGET_NEIGHBOR1_P95_MS if label == "+-1" else TARGET_NEIGHBOR2_P95_MS
        if len(samples) >= 2:
            p50 = float(np.percentile(samples, 50))
            p95 = float(np.percentile(samples, 95))
            verdict = "MET" if p95 <= target else "NOT MET"
            record(f"neighbor.{label}.p95_ms", "measured",
                   f"neighbour {label} full-coverage prep time from settled state, base channel "
                   f"drawn fresh per rep from seeded RNG (n={len(samples)} samples, "
                   f"{reps} rep(s) x {len(offsets[label])} offset(s))",
                   value=p95, unit="ms", target=target, verdict=verdict,
                   notes=f"p50_ms={p50}, samples_ms={samples}; {DEFAULT_CACHE_NOTE}")
        else:
            record(f"neighbor.{label}.p95_ms", "measured",
                   f"neighbour {label}: fewer than 2 samples completed within timeout — p95 not "
                   "trustworthy", value=None, target=target, verdict="INCONCLUSIVE",
                   notes=f"samples_ms={samples}")
        out[label] = samples

    scheduler.shutdown()
    provider.close()
    return out


def cell_far_click_under_load(path, source, algo_version, grid, tiles, channel,
                               num_channels, io_workers, compute_workers, method,
                               timeout_s, n_background_channels):
    """#7: HOT + COVERAGE work queued (many background channels at low
    priority), then a FAR channel is requested at high priority (simulating
    a user click) — measure its full-coverage completion time."""
    provider, raw_cache, corrected_cache, compute, scheduler, warm_result = build_warmed_scheduler(
        path, source, io_workers, compute_workers, grid, warmup_label="far_click")

    bg_gen = ("bg", 0)
    hot_gen = ("hot", 0)
    click_gen = ("click", 0)

    # HOT: the currently-displayed channel, queued at priority 0 (as it
    # would already be resident/streaming in the live viewer).
    for i, tile in enumerate(tiles):
        key = make_key(source, channel, tile, method, BASE_PARAM, algo_version)
        scheduler.request(TileRequest(key=key, generation=hot_gen, priority=0), lambda tr: None)

    # COVERAGE: many background channels queued at low priority (prefetch
    # ring), simulating queued-ahead work competing for the same worker pool.
    bg_channels = [(channel + 10 + i) % num_channels for i in range(n_background_channels)]
    for ch in bg_channels:
        for i, tile in enumerate(tiles):
            key = make_key(source, ch, tile, method, BASE_PARAM, algo_version)
            scheduler.request(TileRequest(key=key, generation=bg_gen, priority=1000 + i), lambda tr: None)

    # Give the queue a brief head start so background work is genuinely
    # already queued (not just submitted in the same instant as the click).
    time.sleep(0.05)

    far_channel = (channel + num_channels // 2) % num_channels
    done = threading.Event()
    remaining = [len(tiles)]
    rl = threading.Lock()
    t_click = time.perf_counter()

    def cb(tr):
        with rl:
            remaining[0] -= 1
            if remaining[0] <= 0:
                done.set()

    for i, tile in enumerate(tiles):
        key = make_key(source, far_channel, tile, method, BASE_PARAM, algo_version)
        scheduler.request(TileRequest(key=key, generation=click_gen, priority=-1000 + i), cb)

    ok = done.wait(timeout=timeout_s)
    click_ms = (time.perf_counter() - t_click) * 1000.0 if ok else None
    verdict = None
    if click_ms is not None:
        verdict = "MET" if click_ms <= TARGET_FAR_CLICK_P95_MS else "NOT MET"
    else:
        verdict = "INCONCLUSIVE"
    record("far_click.full_coverage_ms", "measured",
           f"FAR channel promoted to top priority while HOT + {n_background_channels} "
           f"background channels are queued: time to finish its own {len(tiles)}-tile viewport "
           "(TRUE WALL TIME)",
           value=click_ms, unit="ms", target=TARGET_FAR_CLICK_P95_MS, verdict=verdict,
           notes=DEFAULT_CACHE_NOTE)
    scheduler.shutdown()
    provider.close()
    return click_ms


def cell_cancellation_latency(path, source, algo_version, grid, tiles, channel,
                               num_channels, io_workers, compute_workers, method,
                               timeout_s, n_background_channels, quiet_period_s=1.0):
    """#8: queue background work, then cancel_generation() it and measure
    time from the cancel call to the last 'cancelled' delivery for entries
    that had not yet started (i.e. queued-but-not-started work stopping).

    IMPORTANT scheduler contract (scheduler.py docstring): an entry that had
    ALREADY started running before cancel_generation() runs to completion
    and its result lands in the cache, but delivery to a now-stale waiter is
    SKIPPED ENTIRELY — no callback fires at all for it (not even
    error='cancelled'). So this cannot wait for "all N callbacks"; instead
    it waits for a quiet period with no NEW 'cancelled' delivery, then stops
    and reports how many callbacks it saw of each kind vs how many were
    silently dropped (still-running-at-cancel-time, per the documented
    contract, not a bug in this script)."""
    provider, raw_cache, corrected_cache, compute, scheduler, warm_result = build_warmed_scheduler(
        path, source, io_workers, compute_workers, grid, warmup_label="cancellation")

    bg_gen = ("bgcancel", 0)
    bg_channels = [(channel + 20 + i) % num_channels for i in range(n_background_channels)]
    n_total = len(bg_channels) * len(tiles)

    lock = threading.Lock()
    cancelled_times = []
    completed_times = []
    last_event_time = [None]

    def cb(tr):
        now = time.perf_counter()
        with lock:
            if tr.error == "cancelled":
                cancelled_times.append(now)
            else:
                completed_times.append(now)
            last_event_time[0] = now

    for ch in bg_channels:
        for i, tile in enumerate(tiles):
            key = make_key(source, ch, tile, method, BASE_PARAM, algo_version)
            scheduler.request(TileRequest(key=key, generation=bg_gen, priority=i), cb)

    # Let a few requests actually start running before cancelling, so this
    # measures "stop consuming the GPU for QUEUED work", not "cancel before
    # anything ever started".
    time.sleep(0.03)
    t_cancel = time.perf_counter()
    scheduler.cancel_generation(bg_gen)

    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        time.sleep(0.02)
        with lock:
            last = last_event_time[0]
        if last is not None and (time.perf_counter() - last) >= quiet_period_s:
            break

    with lock:
        n_cancelled = len(cancelled_times)
        n_completed_after = len(completed_times)
        latest_cancel_delivery = max(cancelled_times) if cancelled_times else None
    n_never_delivered = n_total - n_cancelled - n_completed_after

    stop_latency_ms = ((latest_cancel_delivery - t_cancel) * 1000.0
                        if latest_cancel_delivery is not None else None)
    record("cancellation.stop_latency_ms", "measured",
           "time from cancel_generation() call to the LAST 'cancelled' delivery "
           "(i.e. how long queued-but-not-started work kept arriving as cancelled "
           "rather than being run), observed after a "
           f"{quiet_period_s}s quiet period with no further deliveries",
           value=stop_latency_ms, unit="ms",
           notes=(f"n_queued_total={n_total}, n_delivered_cancelled={n_cancelled}, "
                  f"n_delivered_completed={n_completed_after} (already-running work "
                  "that finished and whose waiter happened to still be live), "
                  f"n_never_delivered={n_never_delivered} (already-running-at-cancel-time "
                  "work that completed AFTER its waiter went stale — scheduler contract "
                  "skips delivery entirely for these, so no callback and no timing sample "
                  f"exists for them; this is not a script bug); {DEFAULT_CACHE_NOTE}"))
    scheduler.shutdown()
    provider.close()
    return stop_latency_ms


def cell_cache_cold_warm(path, source, algo_version, channel, tiles, method, timeout_s):
    """#9: raw cache cold vs warm, same provider/raw_cache reused across the
    two runs so the second run is warm. Single-threaded (sequential), so
    the io-worker-thread contamination this rerun targets does not apply
    here; OS page cache state is whatever this run of the script has left
    it in (not deliberately controlled by this cell — see
    cell_cache_state_headline for the deliberately-controlled version)."""
    provider = RawTileProvider(path)
    raw_cache = LRUByteCache(256 * 1024 * 1024)

    def _cold():
        return sequential_viewport(provider, raw_cache, method, channel, tiles, source, algo_version)
    (res_cold, _pt, first_cold, total_cold), err, _ = run_with_timeout(_cold, timeout_s)
    if err:
        record("cache.cold.full_coverage_ms", "measured", "cold raw-cache run FAILED", notes=err)
        provider.close()
        return None

    def _warm():
        return sequential_viewport(provider, raw_cache, method, channel, tiles, source, algo_version)
    (res_warm, _pt2, first_warm, total_warm), err2, _ = run_with_timeout(_warm, timeout_s)

    os_note = "OS page cache: not deliberately controlled by this cell"
    record("cache.cold.full_coverage_ms", "measured",
           f"raw cache COLD, method={method}: full-coverage ({len(tiles)} tiles)",
           value=total_cold, unit="ms",
           notes=cache_state_note("warm (likely; not reset here)", "cold (fresh)", "n/a (sequential, no CorrectionKey cache used here)"))
    record("cache.cold.first_tile_ms", "measured",
           f"raw cache COLD, method={method}: first-tile", value=first_cold, unit="ms")
    if not err2:
        record("cache.warm.full_coverage_ms", "measured",
               f"raw cache WARM (same raw tiles reused), method={method}: full-coverage",
               value=total_warm, unit="ms",
               notes="corrected-tile compute still re-runs the kernel; only raw I/O is warm. " + os_note)
        record("cache.warm.first_tile_ms", "measured",
               f"raw cache WARM, method={method}: first-tile", value=first_warm, unit="ms")
        match, max_diff, n_cmp, n_missing = compare_to_baseline(res_cold, res_warm)
        record("cache.cold_vs_warm.max_abs_diff", "measured",
               "correctness: cold-cache vs warm-cache corrected pixels for same tiles",
               value=max_diff, notes=f"compared={n_cmp}, missing={n_missing}")
    stats = raw_cache.stats()
    record("cache.raw_cache.stats_after_cold_and_warm", "measured",
           "LRUByteCache.stats() for the raw cache after cold+warm runs", value=stats)
    provider.close()
    return {"cold": total_cold, "warm": total_warm if not err2 else None, "stats": stats}


def cell_shared_vs_independent_staging(path, source, algo_version, channel, tiles, timeout_s):
    """#11: does computing tophat+cucim from the SAME staged raw input
    reduce I/O vs independent per-method raw caches? Measured via raw-cache
    hit/miss counts, not asserted. Sequential/single-threaded — no
    io-worker-thread contamination applies."""
    # Independent: two fresh raw caches, one per method.
    provider_a = RawTileProvider(path)
    raw_a = LRUByteCache(256 * 1024 * 1024)
    raw_b = LRUByteCache(256 * 1024 * 1024)

    def _indep():
        sequential_viewport(provider_a, raw_a, "tophat", channel, tiles, source, algo_version)
        sequential_viewport(provider_a, raw_b, "cucim", channel, tiles, source, algo_version)
    _res, err, _ = run_with_timeout(_indep, timeout_s)
    indep_hits = raw_a.stats()["hits"] + raw_b.stats()["hits"]
    indep_misses = raw_a.stats()["misses"] + raw_b.stats()["misses"]
    provider_a.close()

    # Shared: one raw cache used for both methods back-to-back.
    provider_b = RawTileProvider(path)
    raw_shared = LRUByteCache(256 * 1024 * 1024)

    def _shared():
        sequential_viewport(provider_b, raw_shared, "tophat", channel, tiles, source, algo_version)
        sequential_viewport(provider_b, raw_shared, "cucim", channel, tiles, source, algo_version)
    _res2, err2, _ = run_with_timeout(_shared, timeout_s)
    shared_hits = raw_shared.stats()["hits"]
    shared_misses = raw_shared.stats()["misses"]
    provider_b.close()

    if err or err2:
        record("shared_staging.hit_miss", "measured", "shared-vs-independent staging test FAILED",
               notes=f"indep_err={err}, shared_err={err2}")
        return None

    record("shared_staging.independent.hits_misses", "measured",
           "independent per-method raw caches: total raw-tile hits/misses across both methods",
           value={"hits": indep_hits, "misses": indep_misses})
    record("shared_staging.shared.hits_misses", "measured",
           "single shared raw cache across both methods: total raw-tile hits/misses",
           value={"hits": shared_hits, "misses": shared_misses})
    reduction = None
    if indep_misses > 0:
        reduction = 1.0 - (shared_misses / indep_misses)
    record("shared_staging.io_reduction_fraction", "measured",
           "fractional reduction in raw-tile MISSES from sharing staged raw input "
           "between tophat and cucim vs computing them independently",
           value=reduction)
    return {"indep": (indep_hits, indep_misses), "shared": (shared_hits, shared_misses)}


def cell_bg_degradation(path, source, algo_version, grid, tiles, channel, num_channels,
                         io_workers, compute_workers, method, timeout_s, n_background_channels):
    """Spec: background work must not degrade visible first-tile/full-
    coverage time by more than 10%. Measure the HOT channel's own
    full-coverage time with vs without concurrent background load. Both
    runs use their own freshly-built, freshly-warmed scheduler."""
    provider1, raw_cache1, corrected_cache1, compute1, scheduler1, warm1 = build_warmed_scheduler(
        path, source, io_workers, compute_workers, grid, warmup_label="bg_degradation.alone")

    def _alone():
        return scheduler_fetch_channels(provider1, source, algo_version, grid, tiles,
                                         method, [channel], io_workers, compute_workers,
                                         raw_cache=raw_cache1, corrected_cache=corrected_cache1,
                                         scheduler=scheduler1, compute=compute1, timeout_s=timeout_s)
    bundle_alone, err, _ = run_with_timeout(_alone, timeout_s + 15)
    scheduler1.shutdown()
    provider1.close()
    if err:
        record("bg_degradation.alone_ms", "measured", "HOT-alone run FAILED", notes=err)
        return None
    ms_alone = bundle_alone["total_ms"]

    # With background: HOT at high priority + many background channels at
    # low priority queued at (roughly) the same time.
    provider2, raw_cache2, corrected_cache2, compute2, scheduler2, warm2 = build_warmed_scheduler(
        path, source, io_workers, compute_workers, grid, warmup_label="bg_degradation.with_bg")

    bg_gen = ("bgdeg", 0)
    hot_gen = ("hotdeg", 0)
    bg_channels = [(channel + 5 + i) % num_channels for i in range(n_background_channels)]
    for ch in bg_channels:
        for i, tile in enumerate(tiles):
            key = make_key(source, ch, tile, method, BASE_PARAM, algo_version)
            scheduler2.request(TileRequest(key=key, generation=bg_gen, priority=1000 + i), lambda tr: None)

    done = threading.Event()
    remaining = [len(tiles)]
    rl = threading.Lock()
    t0 = time.perf_counter()

    def cb(tr):
        with rl:
            remaining[0] -= 1
            if remaining[0] <= 0:
                done.set()
    for i, tile in enumerate(tiles):
        key = make_key(source, channel, tile, method, BASE_PARAM, algo_version)
        scheduler2.request(TileRequest(key=key, generation=hot_gen, priority=0), cb)
    ok = done.wait(timeout=timeout_s)
    ms_with_bg = (time.perf_counter() - t0) * 1000.0 if ok else None
    scheduler2.shutdown()
    provider2.close()

    if ms_with_bg is None:
        record("bg_degradation.with_bg_ms", "measured", "HOT-with-background run did not complete in time",
               verdict="INCONCLUSIVE")
        return None

    frac = (ms_with_bg - ms_alone) / ms_alone if ms_alone else None
    verdict = None
    if frac is not None:
        verdict = "MET" if frac <= TARGET_BG_DEGRADATION_FRAC else "NOT MET"
    record("bg_degradation.alone_ms", "measured",
           "HOT channel full-coverage, NO background load (TRUE WALL TIME)", value=ms_alone, unit="ms",
           notes=DEFAULT_CACHE_NOTE)
    record("bg_degradation.with_bg_ms", "measured",
           f"HOT channel full-coverage, WITH {n_background_channels} background channels queued "
           "(TRUE WALL TIME)", value=ms_with_bg, unit="ms", notes=DEFAULT_CACHE_NOTE)
    record("bg_degradation.fraction", "measured",
           "fractional degradation of HOT full-coverage time due to concurrent background load",
           value=frac, target=TARGET_BG_DEGRADATION_FRAC, verdict=verdict)
    return frac


def cell_streams_sweep(path, source, algo_version, channel, tiles, method, n_parallel, timeout_s):
    """Sweep whether explicit per-thread CUDA streams help. cupy's current
    stream is thread-local and defaults to the null/legacy stream for every
    thread unless a thread explicitly binds its own — so N compute threads
    sharing the implicit default stream may serialize on-device even though
    they run on separate Python threads. Compares that default-stream case
    against each thread explicitly creating and binding its own
    cp.cuda.Stream().

    The pool is created ONCE and reused for every rep (the original version
    of this cell built a fresh `ThreadPoolExecutor` per call, so its pool
    threads — and their per-thread TIFF handles — were never actually
    reused across "warm-up" and "timed" calls; that was itself a
    contamination this rerun fixes)."""
    if not _CUPY:
        record("streams.sweep", "measured", "cupy not available — skipped (not guessing)", value=None)
        return None
    provider = RawTileProvider(path)
    raw_cache = LRUByteCache(256 * 1024 * 1024)
    compute = CorrectionCompute(provider, raw_cache)

    executor = ThreadPoolExecutor(max_workers=n_parallel)
    warm_result = warm_thread_pool_handles(executor, provider, n_parallel, timeout_s=30.0)
    record_warmup_pool_result = {
        "wall_ms": warm_result["wall_ms"], "rounds": warm_result["rounds"],
        "io_workers": warm_result["n_threads"],
        "open_count_final": warm_result["open_count_final"],
        "target_open_count": warm_result["target_open_count"],
        "all_threads_warmed": warm_result["all_threads_warmed"],
    }
    record_warmup("streams_pool", record_warmup_pool_result)

    # Pre-stage the REAL tiles used for timing so the timed section is
    # compute-only (this is on top of, not instead of, the handle warm-up
    # above — handle warm-up alone does not populate the raw cache for the
    # actual tiles the timed section will use).
    for tile in tiles[:n_parallel]:
        key = make_key(source, channel, tile, method, BASE_PARAM, algo_version)
        compute.raw_keys_for(key)
    for tile in tiles[:n_parallel]:
        provider.read_tile(channel, tile)

    def run_default_stream():
        def work(tile):
            key = make_key(source, channel, tile, method, BASE_PARAM, algo_version)
            return compute.compute(key)
        t0 = time.perf_counter()
        list(executor.map(work, tiles[:n_parallel]))
        return (time.perf_counter() - t0) * 1000.0

    def run_explicit_streams():
        def work(tile):
            stream = cp.cuda.Stream(non_blocking=True)
            with stream:
                key = make_key(source, channel, tile, method, BASE_PARAM, algo_version)
                result = compute.compute(key)
                stream.synchronize()
                return result
        t0 = time.perf_counter()
        list(executor.map(work, tiles[:n_parallel]))
        return (time.perf_counter() - t0) * 1000.0

    try:
        # warm-up (context/JIT init) not timed — same pool, so this is a
        # genuine warm-up now, not a fresh-thread-pool no-op.
        run_default_stream()
        default_samples = [run_default_stream() for _ in range(3)]
        stream_samples = [run_explicit_streams() for _ in range(3)]
    except Exception as exc:
        record("streams.sweep", "measured",
               f"CUDA streams sweep FAILED cleanly ({type(exc).__name__}: {exc}) — skipped, not guessing",
               value=None)
        executor.shutdown(wait=False)
        provider.close()
        return None

    executor.shutdown(wait=True)

    default_med = statistics.median(default_samples)
    stream_med = statistics.median(stream_samples)
    spread_default = (max(default_samples) - min(default_samples))
    spread_stream = (max(stream_samples) - min(stream_samples))
    noise_floor = max(spread_default, spread_stream)
    diff = default_med - stream_med
    if abs(diff) < noise_floor:
        verdict_note = ("difference between default-stream and explicit-per-thread-stream medians "
                         "is smaller than the observed run-to-run spread — cannot cleanly attribute "
                         "any difference to streams; INCONCLUSIVE, not guessing which is faster")
    elif diff > 0:
        verdict_note = "explicit per-thread streams measured faster than the shared default stream"
    else:
        verdict_note = "explicit per-thread streams measured SLOWER than the shared default stream"

    record("streams.default_stream_ms", "measured",
           f"n_parallel={n_parallel} compute threads sharing cupy's implicit default stream: "
           "median wall time (3 reps, persistent thread pool, handles pre-warmed)",
           value=default_med, unit="ms", notes=f"samples={default_samples}")
    record("streams.explicit_streams_ms", "measured",
           f"n_parallel={n_parallel} compute threads each binding its own cp.cuda.Stream(): "
           "median wall time (3 reps, persistent thread pool, handles pre-warmed)",
           value=stream_med, unit="ms", notes=f"samples={stream_samples}")
    record("streams.verdict", "measured", verdict_note, value=diff, unit="ms (default - explicit)")
    provider.close()
    return {"default": default_samples, "streams": stream_samples}


def cell_peak_memory(path, source, algo_version, grid, tiles, channel, num_channels,
                      io_workers, compute_workers, method, timeout_s, n_precompute_channels):
    """Item 7: peak process RSS with an 8 GB corrected cache, holding the
    current viewport PLUS roughly twice that area under precompute (more
    channels queued at low priority, same tile footprint, concurrently with
    the current viewport at high priority)."""
    provider, raw_cache, corrected_cache, compute, scheduler, warm_result = build_warmed_scheduler(
        path, source, io_workers, compute_workers, grid,
        corrected_cache_bytes=8 * 1024 * 1024 * 1024,
        warmup_label="peak_memory")

    rss_samples = []
    stop_flag = threading.Event()

    def sampler():
        while not stop_flag.is_set():
            v = rss_mb()
            if v is not None:
                rss_samples.append(v)
            stop_flag.wait(0.05)

    sampler_thread = threading.Thread(target=sampler, daemon=True)
    sampler_thread.start()

    precompute_channels = [(channel + 3 + i) % num_channels for i in range(n_precompute_channels)]
    gen_hot = ("peakmem_hot", 0)
    gen_pre = ("peakmem_pre", 0)
    n_total = len(tiles) * (1 + len(precompute_channels))
    done = threading.Event()
    remaining = [n_total]
    lock = threading.Lock()

    def cb(tr):
        with lock:
            remaining[0] -= 1
            if remaining[0] <= 0:
                done.set()

    for i, tile in enumerate(tiles):
        key = make_key(source, channel, tile, method, BASE_PARAM, algo_version)
        scheduler.request(TileRequest(key=key, generation=gen_hot, priority=0), cb)
    for ch in precompute_channels:
        for i, tile in enumerate(tiles):
            key = make_key(source, ch, tile, method, BASE_PARAM, algo_version)
            scheduler.request(TileRequest(key=key, generation=gen_pre, priority=1000 + i), cb)

    ok = done.wait(timeout=timeout_s * 4)
    stop_flag.set()
    sampler_thread.join(timeout=5.0)

    gc.collect()
    corrected_stats = corrected_cache.stats()
    raw_stats = raw_cache.stats()
    gpu = gpu_mem_mb()
    peak_rss = max(rss_samples) if rss_samples else rss_mb()

    record("peak_memory.rss_mb", "measured",
           f"peak process RSS while holding current viewport ({len(tiles)} tiles) PLUS "
           f"{n_precompute_channels} channels (~{n_precompute_channels / max(1, len(tiles) and 1):.1f}x "
           "the viewport's channel count) under concurrent precompute, 8 GB corrected-cache cap "
           f"(finished={ok})",
           value=peak_rss, unit="MB", notes=f"n_rss_samples={len(rss_samples)}")
    record("peak_memory.corrected_cache_stats", "measured",
           "LRUByteCache.stats() for the corrected (8 GB cap) cache at end of this cell",
           value=corrected_stats)
    record("peak_memory.raw_cache_stats", "measured",
           "LRUByteCache.stats() for the raw cache at end of this cell", value=raw_stats)
    record("peak_memory.gpu_mempool", "measured",
           "cupy default memory pool used/total at end of this cell", value=gpu)

    scheduler.shutdown()
    provider.close()
    return {"peak_rss_mb": peak_rss, "corrected_stats": corrected_stats,
            "raw_stats": raw_stats, "gpu": gpu}


def profiling_breakdown(per_tile_entries, label):
    """#profiling: TIFF read (io_ms), halo staging (staging_wall_ms if
    present), GPU kernel (kernel_ms — includes H2D/D2H, NOT separable here
    per CorrectionCompute.compute's own comment), and Python overhead
    (wall - accounted)."""
    io_total = 0.0
    kernel_total = 0.0
    staging_total = 0.0
    wall_total = 0.0
    n = 0
    for e in per_tile_entries:
        t = e["timing"]
        io_total += t.get("io_ms", 0.0) or 0.0
        kernel_total += t.get("kernel_ms", 0.0) or 0.0
        staging_total += t.get("staging_wall_ms", 0.0) or 0.0
        wall_total += e["ms"]
        n += 1
    if n == 0:
        return None
    accounted = io_total + kernel_total + staging_total
    overhead = wall_total - accounted
    result = {
        "n_tiles": n,
        "tiff_read_io_ms_total": io_total,
        "halo_staging_ms_total": staging_total,
        "gpu_kernel_ms_total_incl_transfers": kernel_total,
        "host_device_transfer_ms": "not separable — kernel_includes_transfers=True (CorrectionCompute.compute)",
        "wall_ms_total": wall_total,
        "python_scheduling_overhead_ms_total": overhead,
        "python_overhead_fraction_of_wall": (overhead / wall_total) if wall_total else None,
    }
    record(f"profiling.{label}", "measured",
           f"per-tile profiling breakdown ({label}): TIFF read / halo staging / GPU kernel "
           "(incl. transfers, not separable) / Python overhead", value=result)
    return result


# ── main ──────────────────────────────────────────────────────────────────

def build_argparser():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", default=DEFAULT_PATH)
    ap.add_argument("--channel-index", type=int, default=1)
    ap.add_argument("--methods", nargs="+", default=["tophat", "cucim"],
                     choices=["tophat", "cucim"])
    ap.add_argument("--batch-sizes", type=int, nargs="+", default=[1, 2, 4, 8])
    ap.add_argument("--io-workers", type=int, default=8)
    ap.add_argument("--compute-workers", type=int, default=4)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED,
                     help="seed for the channel-order RNG used by the neighbour/batch cells")
    ap.add_argument("--out", default=None, help="path to write JSON results")
    ap.add_argument("--quick", action="store_true",
                     help="reduced matrix for smoke-testing (finishes in ~1-2 min)")
    ap.add_argument("--timeout", type=float, default=60.0,
                     help="per-phase timeout in seconds (guards against silent stalls)")
    return ap


def main():
    args = build_argparser().parse_args()

    if not os.path.isfile(args.path):
        print(f"ERROR: --path does not exist: {args.path}", file=sys.stderr)
        print("Refusing to fabricate numbers for a missing dataset.", file=sys.stderr)
        sys.exit(2)

    quick = args.quick
    cols, rows = (2, 2) if quick else (4, 5)  # quick: 4 tiles; full: 20 tiles
    batch_sizes = [1, 2] if quick else args.batch_sizes
    io_worker_values = [1, 2] if quick else [1, 2, 4, 8]
    compute_worker_values = [1, 2] if quick else [1, 2, 4, 8]
    reps_per_cell = 3 if quick else 10
    n_bg_channels_click = 3 if quick else 8
    n_bg_channels_cancel = 4 if quick else 10
    n_bg_channels_degrad = 3 if quick else 8
    timeout_s = args.timeout
    rng = random.Random(args.seed)

    record("bench.seed", "measured", "seed for the RNG that draws channels for the "
           "neighbour and batch-size cells (--seed)", value=args.seed)

    log(f"opening provider: {args.path}")
    t0 = time.perf_counter()
    provider0, err, _ = run_with_timeout(RawTileProvider, timeout_s, args.path)
    if err:
        print(f"ERROR: could not open provider: {err}", file=sys.stderr)
        sys.exit(2)
    log(f"provider opened in {time.perf_counter()-t0:.2f}s; "
        f"{provider0.num_channels} channels, level0={provider0.level_shape(0)}, "
        f"{provider0.num_levels} levels, dtype={provider0._dtype}")

    channel = args.channel_index % provider0.num_channels
    channel_name = provider0.channel_names[channel]
    num_channels = provider0.num_channels

    log(f"picking tissue viewport for channel {channel} ({channel_name})")
    overview_level, ty0, tx0, y0_l0, x0_l0 = pick_viewport_tile_origin(provider0, channel, cols, rows)
    log(f"chosen window: overview_level={overview_level}, tile origin (ty0,tx0)=({ty0},{tx0}), "
        f"level0 pixel origin (y0,x0)=({y0_l0},{x0_l0}), viewport={rows}x{cols}={rows*cols} tiles")
    record("viewport.window_chosen", "measured",
           "tissue window chosen by block-means over a coarse overview level "
           "(explore_view.py::_pick_calibration_windows logic)",
           value={"overview_level": overview_level, "ty0": ty0, "tx0": tx0,
                  "y0_l0": y0_l0, "x0_l0": x0_l0, "rows": rows, "cols": cols,
                  "n_tiles": rows * cols, "channel_index": channel, "channel_name": channel_name})

    grid = TileGridSpec(tile_size=TILE_SIZE)
    tiles = make_tiles(grid, ty0, tx0, rows, cols)
    source = provider0.source_identity()
    algo_version = bg_correction.BG_CORRECTION_ALGO_VERSION
    provider0.close()

    record("env.rss_mb.start", "measured", "process RSS at start", value=rss_mb(), unit="MB")
    record("env.gpu_mem.start", "measured", "GPU memory (cupy mempool) at start", value=gpu_mem_mb())
    record("env.cupy_available", "measured", "cupy import", value=_CUPY)
    record("env.gpu_morph_available", "measured", "bg_correction.GPU_MORPH_AVAILABLE "
           "(whether the GPU kernels actually engaged, vs CPU fallback)",
           value=bg_correction.GPU_MORPH_AVAILABLE)

    # ── sequential single-threaded baseline (CORRECTNESS baseline) ─────────
    # Deliberately OS-cache-cold for this file just before the very first
    # read of this run, so this number is an honest cold start; every later
    # cell's OS-cache state is documented via DEFAULT_CACHE_NOTE or (for the
    # headline cell) deliberately controlled.
    log("evicting OS page cache for this file (this file only) before the baseline")
    evict_os_cache(args.path)

    log("running sequential single-threaded baseline (correctness + timing)")
    baseline_results = {}
    for method in args.methods:
        provider = RawTileProvider(args.path)
        raw_cache = LRUByteCache(256 * 1024 * 1024)

        def _do(method=method, provider=provider, raw_cache=raw_cache):
            return sequential_viewport(provider, raw_cache, method, channel, tiles, source, algo_version)
        (res, per_tile, first_ms, total_ms), err, timed_out = run_with_timeout(_do, timeout_s)
        provider.close()
        if err:
            record(f"baseline.{method}.full_coverage_ms", "measured",
                   f"sequential baseline method={method} FAILED", notes=err)
            continue
        record(f"baseline.{method}.first_tile_ms", "measured",
               f"SEQUENTIAL (single-threaded) baseline, method={method}: first-tile",
               value=first_ms, unit="ms",
               notes=cache_state_note("cold (evicted for this file just before the first method's run)"
                                      if method == args.methods[0] else "warm (prior method already read this window)",
                                      "cold (fresh)", "n/a (sequential)"))
        record(f"baseline.{method}.full_coverage_ms", "measured",
               f"SEQUENTIAL (single-threaded) baseline, method={method}: full-coverage "
               f"({len(tiles)} tiles) — this is the CORRECTNESS ground truth for all "
               "parallel/batched comparisons below",
               value=total_ms, unit="ms")
        baseline_results[method] = {"results": res, "per_tile": per_tile}
        profiling_breakdown(per_tile, f"baseline.{method}")

    # ── current viewport, first-tile/full-coverage, per method ──────────────
    log("current-viewport timings (redundant-but-direct measurement, fresh providers)")
    cell_current_viewport(args.path, channel, tiles, source, algo_version, args.methods, timeout_s)

    # ── item 9 / #9: cache cold vs warm (sequential) ────────────────────────
    log("cache cold-vs-warm (sequential)")
    for method in args.methods:
        cell_cache_cold_warm(args.path, source, algo_version, channel, tiles, method, timeout_s)

    # ── item 5: headline OS-cache/app-cache cold-vs-warm (scheduler-backed) ─
    log("cache-state headline cell (OS cold / OS warm+app cold / all warm)")
    cell_cache_state_headline(args.path, source, algo_version, grid, tiles, channel,
                               args.io_workers, args.compute_workers, args.methods[0], timeout_s)

    # ── item 1: io_workers sweep, compute_workers fixed ─────────────────────
    log(f"io_workers sweep: {io_worker_values} (compute_workers fixed at {args.compute_workers})")
    io_winner, io_sweep_results = cell_io_workers_sweep(
        args.path, source, algo_version, grid, tiles, channel, io_worker_values,
        args.compute_workers, args.methods[0], timeout_s, baseline_results)

    # ── item 2: compute_workers sweep, io_workers fixed at the item-1 winner ─
    log(f"compute_workers sweep: {compute_worker_values} (io_workers fixed at winner={io_winner})")
    compute_winner, compute_sweep_results = cell_compute_workers_sweep(
        args.path, source, algo_version, grid, tiles, channel, io_winner,
        compute_worker_values, args.methods[0], timeout_s, baseline_results)

    # ── item 3 + item 4: channel batch-size sweep, randomised channel order ─
    log(f"channel batch-size sweep: {batch_sizes}, io_workers={io_winner} "
        f"compute_workers={compute_winner}, reps={reps_per_cell}, seed={args.seed}")
    cell_batch_sizes_v2(args.path, source, algo_version, grid, tiles, channel, num_channels,
                         args.methods[0], batch_sizes, io_winner, compute_winner, timeout_s,
                         baseline_results, rng, reps_per_cell)

    # ── item 4/6: neighbour-channel prep, randomised base channel per rep ───
    log(f"neighbour-channel (+-1/+-2) prep timing, reps={reps_per_cell}, seed={args.seed}")
    cell_neighbors_v2(args.path, source, algo_version, grid, tiles, num_channels,
                       io_winner, compute_winner, args.methods[0], timeout_s, rng, reps_per_cell)

    # ── #7: far-channel click promotion under HOT+COVERAGE load ────────────
    log("far-channel click promotion under load")
    cell_far_click_under_load(args.path, source, algo_version, grid, tiles, channel,
                               num_channels, args.io_workers, args.compute_workers, args.methods[0],
                               timeout_s, n_bg_channels_click)

    # ── background-degradation check (spec target, not #-numbered) ────────
    log("background-load degradation of visible viewport")
    cell_bg_degradation(args.path, source, algo_version, grid, tiles, channel, num_channels,
                        args.io_workers, args.compute_workers, args.methods[0], timeout_s,
                        n_bg_channels_degrad)

    # ── #8: cancellation latency ────────────────────────────────────────────
    log("cancellation latency")
    cell_cancellation_latency(args.path, source, algo_version, grid, tiles, channel,
                               num_channels, args.io_workers, args.compute_workers, args.methods[0],
                               timeout_s, n_bg_channels_cancel)

    # ── #11: shared vs independent raw staging ──────────────────────────────
    if len(args.methods) >= 2:
        log("shared-vs-independent raw staging (tophat + cucim)")
        cell_shared_vs_independent_staging(args.path, source, algo_version, channel, tiles, timeout_s)
    else:
        record("shared_staging.hit_miss", "measured",
               "skipped — needs at least 2 methods (only one passed via --methods)", value=None)

    # ── CUDA streams sweep ───────────────────────────────────────────────────
    log("CUDA streams sweep")
    n_parallel_streams = 2 if quick else min(4, compute_winner)
    cell_streams_sweep(args.path, source, algo_version, channel, tiles, args.methods[0],
                        n_parallel_streams, timeout_s)

    # ── item 7: peak memory, 8 GB corrected cache ───────────────────────────
    n_precompute = 2 if quick else 8  # ~2x the viewport's own footprint's worth of channels
    log(f"peak memory: 8 GB corrected cache, viewport + {n_precompute} precompute channels")
    cell_peak_memory(args.path, source, algo_version, grid, tiles, channel, num_channels,
                      io_winner, compute_winner, args.methods[0], timeout_s, n_precompute)

    # ── resource use, end of run ─────────────────────────────────────────────
    gc.collect()
    record("env.rss_mb.end", "measured", "process RSS at end", value=rss_mb(), unit="MB")
    record("env.gpu_mem.end", "measured", "GPU memory (cupy mempool) at end", value=gpu_mem_mb())

    # ── print table + JSON ───────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print(f"{'id':<45} {'label':<10} {'value':<25} {'verdict':<12}")
    print("=" * 100)
    for m in MEASUREMENTS:
        val = m["value"]
        if isinstance(val, (dict, list)):
            val_str = json.dumps(val, default=str)[:60]
        else:
            val_str = str(val)[:25]
        verdict = m["verdict"] or ""
        target = f" (target={m['target']})" if m["target"] is not None else ""
        print(f"{m['id']:<45} {m['label']:<10} {val_str:<25} {verdict:<12}{target}")
    print("=" * 100)

    payload = {
        "path": args.path,
        "channel_index": channel,
        "channel_name": channel_name,
        "quick": quick,
        "seed": args.seed,
        "args": vars(args),
        "measurements": MEASUREMENTS,
    }
    json_str = json.dumps(payload, indent=2, default=str)
    print("\n### JSON RESULTS ###")
    print(json_str)

    if args.out:
        with open(args.out, "w") as f:
            f.write(json_str)
        log(f"wrote {args.out}")

    log("done")


if __name__ == "__main__":
    main()
