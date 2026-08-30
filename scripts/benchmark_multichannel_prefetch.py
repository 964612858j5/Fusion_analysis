"""Multichannel prefetch/correction benchmark — MEASUREMENT ONLY.

This script does not wire anything into the live viewer. It reuses the
production scheduling/compute primitives (`block01.viewer.*`,
`block01.core.bg_correction`) against the real 57-channel OME-TIFF to
measure the questions in the review spec: viewport fill time, per-method
timing, channel-batch concurrency, worker-count sweeps, a strict
single-threaded correctness baseline, neighbour-channel prep time, far-
channel promotion under background load, cancellation latency, cache
cold/warm behavior, resource use, shared-vs-independent raw staging, and
(if cupy is available) a CUDA-streams sweep.

Every reported number is labelled measured / inferred / proposed. Targets
from the review spec are printed next to measured values with a
MET/NOT MET/INCONCLUSIVE verdict computed strictly from this script's own
measurements — this script does not editorialise about whether a target
"should" be considered met by some other standard.

Usage:
    cd /sda1/Fusion/analysis_pipline/block01_v14
    python scripts/benchmark_multichannel_prefetch.py --quick
    python scripts/benchmark_multichannel_prefetch.py --out /tmp/bench.json
"""

import argparse
import copy
import gc
import json
import math
import os
import statistics
import sys
import threading
import time
import traceback
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
                              raw_cache=None, corrected_cache=None):
    """Fresh (unless caches passed in) caches + scheduler; request every
    (channel, tile) in `channels` x `tiles` concurrently. Returns a dict
    with per-channel first/full timings, results, and the live objects
    (raw_cache, corrected_cache, scheduler) for the caller to inspect
    before tearing down."""
    if raw_cache is None:
        raw_cache = LRUByteCache(raw_cache_bytes)
    if corrected_cache is None:
        corrected_cache = LRUByteCache(corrected_cache_bytes)
    compute = CorrectionCompute(provider, raw_cache)
    scheduler = TileScheduler(provider, compute, raw_cache, corrected_cache,
                               io_workers=io_workers, compute_workers=compute_workers)

    gen = ("bench", 0)
    n_total = len(channels) * len(tiles)
    done_event = threading.Event()
    lock = threading.Lock()
    state = {"count": 0, "first_ms": {}, "results": {ch: {} for ch in channels},
             "t_start": time.perf_counter(), "errors": []}

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
    return {
        "finished": finished,
        "total_ms": total_ms,
        "first_ms": dict(state["first_ms"]),
        "results": state["results"],
        "errors": state["errors"],
        "scheduler": scheduler,
        "raw_cache": raw_cache,
        "corrected_cache": corrected_cache,
        "compute": compute,
    }


def teardown(bundle_or_scheduler, provider=None):
    sched = bundle_or_scheduler["scheduler"] if isinstance(bundle_or_scheduler, dict) else bundle_or_scheduler
    try:
        sched.shutdown()
    except Exception:
        pass
    if provider is not None:
        try:
            provider.close()
        except Exception:
            pass


# ── benchmark cells ───────────────────────────────────────────────────────

def cell_current_viewport(path, channel, tiles, source, algo_version, methods, timeout_s):
    """#1/#2: current viewport (~20 tiles), one channel, first-tile / full-
    coverage wall time, per method."""
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
                   f"first-tile wall time, method={method}", value=first_ms, unit="ms")
            record(f"viewport.{method}.full_coverage_ms", "measured",
                   f"full-coverage wall time ({len(tiles)} tiles), method={method}",
                   value=total_ms, unit="ms")
            out[method] = {"results": res, "per_tile": per_tile,
                            "first_tile_ms": first_ms, "total_ms": total_ms}
        provider.close()
    return out


def cell_batch_sizes(path, source, algo_version, grid, tiles, channel0, num_channels,
                      methods, batch_sizes, io_workers, compute_workers, timeout_s,
                      baseline_results):
    out = {}
    for method in methods:
        out[method] = {}
        for batch in batch_sizes:
            channels = [(channel0 + i) % num_channels for i in range(batch)]
            provider = RawTileProvider(path)

            def _do():
                return scheduler_fetch_channels(
                    provider, source, algo_version, grid, tiles, method, channels,
                    io_workers, compute_workers, timeout_s=timeout_s)

            bundle, err, timed_out = run_with_timeout(_do, timeout_s + 15)
            if err:
                record(f"batch.{method}.n{batch}", "measured",
                       f"batch={batch} method={method}: FAILED", notes=err)
                out[method][batch] = {"failed": True, "error": err}
                provider.close()
                continue

            per_channel_full_ms = {ch: bundle["total_ms"] for ch in channels}  # aggregate only; see note
            # Correctness check: only channel0 is present in the sequential
            # baseline, so that is the only channel we can compare here.
            match, max_diff, n_cmp, n_missing = (None, None, 0, 0)
            if channel0 in bundle["results"] and method in baseline_results:
                match, max_diff, n_cmp, n_missing = compare_to_baseline(
                    baseline_results[method]["results"], bundle["results"][channel0])

            verdict_note = None
            if match is False:
                verdict_note = "output MISMATCHES sequential baseline — timing NOT reported for this cell per spec"
            record(f"batch.{method}.n{batch}.aggregate_ms", "measured",
                   f"batch={batch} channels method={method}: aggregate wall time "
                   f"for all {batch} channel(s) x {len(tiles)} tiles concurrently",
                   value=bundle["total_ms"], unit="ms",
                   notes=(f"correctness vs seq baseline (channel {channel0} only): "
                          f"max_abs_diff={max_diff}, compared={n_cmp}, missing={n_missing}"
                          if match is not None else "no baseline overlap to compare")
                   if match is not False else verdict_note)
            if match is not False:
                record(f"batch.{method}.n{batch}.per_channel_ms", "measured",
                       f"batch={batch} method={method}: per-channel share of aggregate "
                       "(wall clock is shared across concurrent channels, not additive)",
                       value=bundle["first_ms"], unit="ms (first-tile-of-channel arrival time)")
            out[method][batch] = bundle
            teardown(bundle, provider)
    return out


def cell_worker_sweep(path, source, algo_version, grid, tiles, channel, io_workers,
                       compute_worker_values, method, timeout_s):
    out = {}
    prev_ms = None
    for cw in compute_worker_values:
        provider = RawTileProvider(path)

        def _do():
            return scheduler_fetch_channels(
                provider, source, algo_version, grid, tiles, method, [channel],
                io_workers, cw, timeout_s=timeout_s)

        bundle, err, timed_out = run_with_timeout(_do, timeout_s + 15)
        if err:
            record(f"workers.compute{cw}", "measured",
                   f"io_workers={io_workers} compute_workers={cw}: FAILED", notes=err)
            out[cw] = {"failed": True, "error": err}
            provider.close()
            continue
        ms = bundle["total_ms"]
        helps_note = None
        if prev_ms is not None:
            helps_note = ("faster than previous step" if ms < prev_ms
                          else "NOT faster than previous step (no help or noise)")
        record(f"workers.compute{cw}.full_coverage_ms", "measured",
               f"io_workers={io_workers} compute_workers={cw}: full-coverage "
               f"({len(tiles)} tiles, 1 channel) wall time",
               value=ms, unit="ms", notes=helps_note)
        out[cw] = bundle
        prev_ms = ms
        teardown(bundle, provider)
    return out


def cell_neighbors(path, source, algo_version, grid, tiles, channel, num_channels,
                    io_workers, compute_workers, method, timeout_s, reps):
    """#6: from a settled state (channel already resident), measure +-1 and
    +-2 neighbour full-coverage prep time."""
    provider = RawTileProvider(path)
    raw_cache = LRUByteCache(256 * 1024 * 1024)
    corrected_cache = LRUByteCache(512 * 1024 * 1024)
    compute = CorrectionCompute(provider, raw_cache)
    scheduler = TileScheduler(provider, compute, raw_cache, corrected_cache,
                               io_workers=io_workers, compute_workers=compute_workers)

    def fetch_one(ch, gen_id):
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

    # Settle the base channel first (not timed as a neighbor result).
    fetch_one(channel, "settle0")

    offsets = {"+-1": [1, -1], "+-2": [2, -2]}
    out = {}
    for label, offs in offsets.items():
        samples = []
        for rep in range(reps):
            for off in offs:
                ch = (channel + off) % num_channels
                ms = fetch_one(ch, f"{label}-{rep}-{off}")
                if ms is not None:
                    samples.append(ms)
        if samples:
            p95 = float(np.percentile(samples, 95))
            target = TARGET_NEIGHBOR1_P95_MS if label == "+-1" else TARGET_NEIGHBOR2_P95_MS
            verdict = "MET" if p95 <= target else "NOT MET"
            record(f"neighbor.{label}.p95_ms", "measured",
                   f"neighbour {label} full-coverage prep time from settled state "
                   f"({reps} rep(s) x {len(offs)} offset(s), n={len(samples)})",
                   value=p95, unit="ms", target=target, verdict=verdict,
                   notes=f"samples_ms={samples}")
        else:
            record(f"neighbor.{label}.p95_ms", "measured",
                   f"neighbour {label}: no samples completed within timeout",
                   value=None, target=(TARGET_NEIGHBOR1_P95_MS if label == "+-1" else TARGET_NEIGHBOR2_P95_MS),
                   verdict="INCONCLUSIVE")
        out[label] = samples
    teardown(scheduler, provider)
    return out


def cell_far_click_under_load(path, source, algo_version, grid, tiles, channel,
                               num_channels, io_workers, compute_workers, method,
                               timeout_s, n_background_channels):
    """#7: HOT + COVERAGE work queued (many background channels at low
    priority), then a FAR channel is requested at high priority (simulating
    a user click) — measure its full-coverage completion time."""
    provider = RawTileProvider(path)
    raw_cache = LRUByteCache(256 * 1024 * 1024)
    corrected_cache = LRUByteCache(512 * 1024 * 1024)
    compute = CorrectionCompute(provider, raw_cache)
    scheduler = TileScheduler(provider, compute, raw_cache, corrected_cache,
                               io_workers=io_workers, compute_workers=compute_workers)

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
           f"background channels are queued: time to finish its own {len(tiles)}-tile viewport",
           value=click_ms, unit="ms", target=TARGET_FAR_CLICK_P95_MS, verdict=verdict)
    teardown(scheduler, provider)
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
    provider = RawTileProvider(path)
    raw_cache = LRUByteCache(256 * 1024 * 1024)
    corrected_cache = LRUByteCache(512 * 1024 * 1024)
    compute = CorrectionCompute(provider, raw_cache)
    scheduler = TileScheduler(provider, compute, raw_cache, corrected_cache,
                               io_workers=io_workers, compute_workers=compute_workers)

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
                  "exists for them; this is not a script bug)"))
    teardown(scheduler, provider)
    return stop_latency_ms


def cell_cache_cold_warm(path, source, algo_version, channel, tiles, method, timeout_s):
    """#9: raw cache cold vs warm, same provider/raw_cache reused across the
    two runs so the second run is warm."""
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

    record("cache.cold.full_coverage_ms", "measured",
           f"raw cache COLD, method={method}: full-coverage ({len(tiles)} tiles)",
           value=total_cold, unit="ms")
    record("cache.cold.first_tile_ms", "measured",
           f"raw cache COLD, method={method}: first-tile", value=first_cold, unit="ms")
    if not err2:
        record("cache.warm.full_coverage_ms", "measured",
               f"raw cache WARM (same raw tiles reused), method={method}: full-coverage",
               value=total_warm, unit="ms",
               notes="corrected-tile compute still re-runs the kernel; only raw I/O is warm")
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
    hit/miss counts, not asserted."""
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
    full-coverage time with vs without concurrent background load."""
    # Baseline: HOT channel alone, no background load.
    provider1 = RawTileProvider(path)

    def _alone():
        return scheduler_fetch_channels(provider1, source, algo_version, grid, tiles,
                                         method, [channel], io_workers, compute_workers,
                                         timeout_s=timeout_s)
    bundle_alone, err, _ = run_with_timeout(_alone, timeout_s + 15)
    if err:
        record("bg_degradation.alone_ms", "measured", "HOT-alone run FAILED", notes=err)
        provider1.close()
        return None
    ms_alone = bundle_alone["total_ms"]
    teardown(bundle_alone, provider1)

    # With background: HOT at high priority + many background channels at
    # low priority queued at (roughly) the same time.
    provider2 = RawTileProvider(path)
    raw_cache = LRUByteCache(256 * 1024 * 1024)
    corrected_cache = LRUByteCache(512 * 1024 * 1024)
    compute = CorrectionCompute(provider2, raw_cache)
    scheduler = TileScheduler(provider2, compute, raw_cache, corrected_cache,
                               io_workers=io_workers, compute_workers=compute_workers)
    bg_gen = ("bgdeg", 0)
    hot_gen = ("hotdeg", 0)
    bg_channels = [(channel + 5 + i) % num_channels for i in range(n_background_channels)]
    for ch in bg_channels:
        for i, tile in enumerate(tiles):
            key = make_key(source, ch, tile, method, BASE_PARAM, algo_version)
            scheduler.request(TileRequest(key=key, generation=bg_gen, priority=1000 + i), lambda tr: None)

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
        scheduler.request(TileRequest(key=key, generation=hot_gen, priority=0), cb)
    ok = done.wait(timeout=timeout_s)
    ms_with_bg = (time.perf_counter() - t0) * 1000.0 if ok else None
    teardown(scheduler, provider2)

    if ms_with_bg is None:
        record("bg_degradation.with_bg_ms", "measured", "HOT-with-background run did not complete in time",
               verdict="INCONCLUSIVE")
        return None

    frac = (ms_with_bg - ms_alone) / ms_alone if ms_alone else None
    verdict = None
    if frac is not None:
        verdict = "MET" if frac <= TARGET_BG_DEGRADATION_FRAC else "NOT MET"
    record("bg_degradation.alone_ms", "measured",
           f"HOT channel full-coverage, NO background load", value=ms_alone, unit="ms")
    record("bg_degradation.with_bg_ms", "measured",
           f"HOT channel full-coverage, WITH {n_background_channels} background channels queued",
           value=ms_with_bg, unit="ms")
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
    cp.cuda.Stream()."""
    if not _CUPY:
        record("streams.sweep", "measured", "cupy not available — skipped (not guessing)", value=None)
        return None
    provider = RawTileProvider(path)
    raw_cache = LRUByteCache(256 * 1024 * 1024)
    compute = CorrectionCompute(provider, raw_cache)

    # Pre-stage raw tiles so the timed section is compute-only.
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
        with ThreadPoolExecutor(max_workers=n_parallel) as ex:
            list(ex.map(work, tiles[:n_parallel]))
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
        with ThreadPoolExecutor(max_workers=n_parallel) as ex:
            list(ex.map(work, tiles[:n_parallel]))
        return (time.perf_counter() - t0) * 1000.0

    try:
        # warm-up (context/JIT init) not timed
        run_default_stream()
        default_samples = [run_default_stream() for _ in range(3)]
        stream_samples = [run_explicit_streams() for _ in range(3)]
    except Exception as exc:
        record("streams.sweep", "measured",
               f"CUDA streams sweep FAILED cleanly ({type(exc).__name__}: {exc}) — skipped, not guessing",
               value=None)
        provider.close()
        return None

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
           "median wall time (3 reps)", value=default_med, unit="ms", notes=f"samples={default_samples}")
    record("streams.explicit_streams_ms", "measured",
           f"n_parallel={n_parallel} compute threads each binding its own cp.cuda.Stream(): "
           "median wall time (3 reps)", value=stream_med, unit="ms", notes=f"samples={stream_samples}")
    record("streams.verdict", "measured", verdict_note, value=diff, unit="ms (default - explicit)")
    provider.close()
    return {"default": default_samples, "streams": stream_samples}


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
    compute_worker_sweep = [1, 2] if quick else [1, 2, 4]
    neighbor_reps = 1
    n_bg_channels_click = 3 if quick else 8
    n_bg_channels_cancel = 4 if quick else 10
    n_bg_channels_degrad = 3 if quick else 8
    timeout_s = args.timeout

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

    # ── #5/#12: sequential single-threaded baseline (CORRECTNESS baseline) ──
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
               value=first_ms, unit="ms")
        record(f"baseline.{method}.full_coverage_ms", "measured",
               f"SEQUENTIAL (single-threaded) baseline, method={method}: full-coverage "
               f"({len(tiles)} tiles) — this is the CORRECTNESS ground truth for all "
               "parallel/batched comparisons below",
               value=total_ms, unit="ms")
        baseline_results[method] = {"results": res, "per_tile": per_tile}
        profiling_breakdown(per_tile, f"baseline.{method}")

    # ── #1/#2: current viewport, first-tile/full-coverage, per method ──────
    log("current-viewport timings (redundant-but-direct measurement of #1/#2, fresh providers)")
    cell_current_viewport(args.path, channel, tiles, source, algo_version, args.methods, timeout_s)

    # ── #9: cache cold vs warm ──────────────────────────────────────────────
    log("cache cold-vs-warm")
    for method in args.methods:
        cell_cache_cold_warm(args.path, source, algo_version, channel, tiles, method, timeout_s)

    # ── #3: channel batch sizes ─────────────────────────────────────────────
    log(f"channel batch-size sweep: {batch_sizes}")
    cell_batch_sizes(args.path, source, algo_version, grid, tiles, channel, num_channels,
                      args.methods, batch_sizes, args.io_workers, args.compute_workers,
                      timeout_s, baseline_results)

    # ── #4: worker sweep ─────────────────────────────────────────────────────
    log("worker-count sweep")
    method0 = args.methods[0]
    record("workers.defaults", "measured",
           "current default worker configuration under test (NOT changed by this script)",
           value={"io_workers": args.io_workers, "compute_workers": args.compute_workers})
    cell_worker_sweep(args.path, source, algo_version, grid, tiles, channel, args.io_workers,
                       compute_worker_sweep, method0, timeout_s)

    # ── #6: neighbour-channel prep from settled state ───────────────────────
    log("neighbour-channel (+-1/+-2) prep timing")
    cell_neighbors(args.path, source, algo_version, grid, tiles, channel, num_channels,
                   args.io_workers, args.compute_workers, method0, timeout_s, neighbor_reps)

    # ── #7: far-channel click promotion under HOT+COVERAGE load ────────────
    log("far-channel click promotion under load")
    cell_far_click_under_load(args.path, source, algo_version, grid, tiles, channel,
                               num_channels, args.io_workers, args.compute_workers, method0,
                               timeout_s, n_bg_channels_click)

    # ── background-degradation check (spec target, not #-numbered) ────────
    log("background-load degradation of visible viewport")
    cell_bg_degradation(args.path, source, algo_version, grid, tiles, channel, num_channels,
                        args.io_workers, args.compute_workers, method0, timeout_s,
                        n_bg_channels_degrad)

    # ── #8: cancellation latency ────────────────────────────────────────────
    log("cancellation latency")
    cell_cancellation_latency(args.path, source, algo_version, grid, tiles, channel,
                               num_channels, args.io_workers, args.compute_workers, method0,
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
    n_parallel_streams = 2 if quick else min(4, args.compute_workers)
    cell_streams_sweep(args.path, source, algo_version, channel, tiles, method0,
                        n_parallel_streams, timeout_s)

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
