"""Benchmark the v15 viewer-foundation prototype on a real OME-TIFF.

Usage:
    python -m block01.scripts.benchmark_viewer_prototype \
        --path /path/to/slide.ome.tif --out /tmp/bench [--channel DAPI] [--quick]

This is a MEASUREMENT script, not a promise. Every "speedup" number it
prints is measured-only, on whatever machine/dataset it ran on — never
extrapolated or claimed as a guaranteed number elsewhere. It does not
render anything: the "cache-hit lookup" phase below is a re-request of
already-cached tiles through the scheduler, NOT a frame render and NOT an
FPS measurement (the actual G1 render-path gate — pyqtgraph/GL draw at
60 FPS — is untested by this script).

For each (tile_size, level) config, this randomizes the method order (so
warm-up/thermal/GC order effects don't always favor the same method),
recomputes a 2048x2048-viewport tile set for R=3 rounds (round 1 =
"app-cold": fresh in-process caches; note the OS page-cache state is
UNKNOWN — we don't drop_caches), rounds 2-3 = "warm" (same caches reused).
It also runs a redesigned continuous 8-step pan trajectory and reports
per-cache-instance stats, RSS, and GPU memory-pool/device info.

Writes <out>/benchmark_results.json (raw numbers) and
<out>/benchmark_report.md (summary tables), and prints the report path.
"""

import argparse
import json
import os
import random
import resource
import socket
import statistics
import sys
import threading
import time

import numpy as np

def _register_block01_alias():
    """Bind the `block01` package name to THIS checkout, regardless of the
    directory name it lives in (e.g. a `block01_v14` git worktree) — mirrors
    the repo-root conftest.py shim so `python scripts/benchmark_...py` and
    `pytest` resolve the same module tree.

    If a DIFFERENT `block01` checkout is already imported, this is a hard
    failure (mirrors conftest.py): silently keeping the foreign module would
    make this script measure the wrong code without any indication."""
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
            f"'block01' already imported from {existing_root!r}, not from "
            f"{root!r}; refusing to benchmark against the wrong checkout.")
    spec = importlib.util.spec_from_file_location(
        "block01", root / "__init__.py", submodule_search_locations=[str(root)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["block01"] = mod
    spec.loader.exec_module(mod)
    sys.path.insert(0, str(root.parent))


_register_block01_alias()

import block01.viewer as _viewer_pkg
import block01.core.bg_correction as bg_correction
from block01.viewer.caches import LRUByteCache
from block01.viewer.correction_compute import CorrectionCompute, halo_for
from block01.viewer.raw_tile_provider import RawTileProvider
from block01.viewer.scheduler import TileScheduler
from block01.viewer.tile_types import (
    CorrectionKey,
    QualityLevel,
    RawKey,
    TileAddress,
    TileGridSpec,
    TileRequest,
    effective_param,
    tiles_covering,
)

VIEWPORT_PX = 2048
METHOD_PARAMS = [("tophat", 25), ("tophat", 50), ("cucim", 50), ("cucim", 100)]
TILE_SIZES = [256, 512, 1024]
LEVELS = [0, 1, 2]
ALGO_VERSION = bg_correction.BG_CORRECTION_ALGO_VERSION
ROUNDS = 3
RAW_CACHE_BYTES = 512 * 1024 * 1024   # 512 MB -- provisional default candidate
CORR_CACHE_BYTES = 512 * 1024 * 1024  # 512 = provisional default candidate


# ─────────────────────────── environment block ────────────────────────────

def environment_block():
    env = {
        "hostname": socket.gethostname(),
        "python": sys.version.split()[0],
        "viewer_module_file": getattr(_viewer_pkg, "__file__", None),
        "bg_correction_module_file": getattr(bg_correction, "__file__", None),
        "gpu_morph_available_before": bool(bg_correction.GPU_MORPH_AVAILABLE),
        "cupy_version": None,
        "cuda_runtime_version": None,
        "gpu_device_name": None,
    }
    try:
        import cupy
        env["cupy_version"] = cupy.__version__
        try:
            env["cuda_runtime_version"] = int(cupy.cuda.runtime.runtimeGetVersion())
        except Exception as exc:
            env["cuda_runtime_version_error"] = str(exc)
        try:
            props = cupy.cuda.runtime.getDeviceProperties(0)
            name = props.get("name", b"")
            env["gpu_device_name"] = name.decode() if isinstance(name, (bytes, bytearray)) else str(name)
        except Exception as exc:
            env["gpu_device_name_error"] = str(exc)
    except Exception as exc:
        env["cupy_import_error"] = str(exc)
    return env


def gpu_mem_pool_stats():
    try:
        import cupy
        pool = cupy.get_default_memory_pool()
        return {"used_bytes": int(pool.used_bytes()), "total_bytes": int(pool.total_bytes())}
    except Exception:
        return {"used_bytes": None, "total_bytes": None}


def gpu_device_mem_info():
    try:
        import cupy
        free_b, total_b = cupy.cuda.Device().mem_info
        return {"free_bytes": int(free_b), "total_bytes": int(total_b)}
    except Exception:
        return {"free_bytes": None, "total_bytes": None}


def rss_kb():
    """Current+peak process RSS. ru_maxrss is the PEAK (kB on Linux)."""
    ru = resource.getrusage(resource.RUSAGE_SELF)
    peak_kb = ru.ru_maxrss
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    current_kb = int(line.split()[1])
                    return current_kb, peak_kb
    except Exception:
        pass
    return None, peak_kb


def kernel_first_touch_cost():
    """Time the very first GPU kernel call (JIT/context init cost), isolated
    from the main matrix so it doesn't pollute per-method timing samples."""
    t0 = time.perf_counter()
    try:
        arr = np.random.RandomState(0).rand(64, 64).astype(np.float32)
        _ = bg_correction._apply_cucim_or_cpu(arr, 4, prefer_gpu=True)
        ok = True
        err = None
    except Exception as exc:
        ok = False
        err = str(exc)
    ms = (time.perf_counter() - t0) * 1000.0
    return {"first_kernel_call_ms": ms, "ok": ok, "error": err}


# ─────────────────────────── helpers ──────────────────────────────────────

def pct(values, p):
    if not values:
        return None
    values = sorted(values)
    idx = min(len(values) - 1, int(round(p / 100.0 * (len(values) - 1))))
    return values[idx]


def fmt(v):
    return "n/a" if v is None else f"{v:.2f}"


def tiles_for_viewport(level_shape, tile_size, viewport_px):
    h, w = level_shape
    vp = min(viewport_px, h, w)
    cy, cx = h // 2, w // 2
    y0 = max(0, cy - vp // 2)
    x0 = max(0, cx - vp // 2)
    y1 = min(h, y0 + vp)
    x1 = min(w, x0 + vp)
    bbox = (y0, x0, y1, x1)
    tiles = sorted(tiles_covering(bbox, tile_size))
    return tiles, bbox


def fill_sync(scheduler, requests):
    """Issue all `requests`; block until every callback fired. Returns
    (results_by_key, wall_ms, per_tile_records) where per_tile_records is a
    list of dicts with per-tile prep timing (as delivered in TileResult)."""
    done = threading.Event()
    remaining = [len(requests)]
    lock = threading.Lock()
    results = {}
    records = []

    def make_cb(key):
        def cb(result):
            with lock:
                results[key] = result
                t = result.timing or {}
                records.append({
                    "key_repr": repr(key)[:80],
                    "io_ms": t.get("io_ms"),
                    "kernel_ms": t.get("kernel_ms"),
                    "total_ms": t.get("total_ms"),
                    "cache_hit": t.get("cache") == "hit",
                })
                remaining[0] -= 1
                if remaining[0] == 0:
                    done.set()
        return cb

    t0 = time.perf_counter()
    for req in requests:
        scheduler.request(req, make_cb(req.key))
    done.wait(timeout=300)
    wall_ms = (time.perf_counter() - t0) * 1000.0
    return results, wall_ms, records


def sample_channel_stats(provider, channel_index, level=0, tile_size=512):
    """Signal stats (min/mean/p99/max) of one sampled tile, for the report."""
    h, w = provider.level_shape(level)
    y0, x0 = h // 2, w // 2
    y1, x1 = min(h, y0 + tile_size), min(w, x0 + tile_size)
    arr, _off = provider.read_region(channel_index, level, y0, y1, x0, x1)
    arr_f = arr.astype(np.float64, copy=False)
    return {
        "dtype": str(arr.dtype),
        "min": float(arr_f.min()) if arr_f.size else None,
        "mean": float(arr_f.mean()) if arr_f.size else None,
        "p99": float(np.percentile(arr_f, 99)) if arr_f.size else None,
        "max": float(arr_f.max()) if arr_f.size else None,
    }


# ─────────────────────────── pan trajectory ───────────────────────────────

def pan_trajectory_steps(tile_size, n_steps=12):
    """N-step continuous trajectory: quarter-tile steps, alternating +x/+y,
    so some steps cross a tile boundary (new column/row) and some don't.
    Extended past the original 8 steps so the cumulative shift (n_steps/2
    quarter-tiles per axis) walks beyond the ~25-tile footprint warmed by
    the initial (unaligned) viewport fill and MUST fetch new tiles."""
    q = tile_size // 4
    steps = []
    for i in range(n_steps):
        if i % 2 == 0:
            steps.append((q, 0))
        else:
            steps.append((0, q))
    return steps


def run_pan_test(sched, make_key, start_bbox, tile_size, level_shape, generation_base=1000):
    """Walk a continuous pan trajectory over a FLOATING viewport bbox.

    Unlike a naive "fixed tile count from an aligned origin" pan test, this
    tracks the actual (possibly unaligned) pixel bbox, shifts it by a
    quarter-tile each step, and recomputes the covering tile set FROM THE
    BBOX each step via `tiles_covering` (same floor/ceil convention as the
    initial fill). This guarantees steps eventually request tiles outside
    the initially-warmed footprint (n_new_tiles > 0), unlike re-deriving a
    fixed-size tile block from an aligned tile origin (which can re-request
    already-warmed tiles forever).
    """
    h, w = level_shape
    y0, x0, y1, x1 = start_bbox
    known_tiles = set(tiles_covering(start_bbox, tile_size))
    prev_tileset = set(known_tiles)

    steps = pan_trajectory_steps(tile_size)
    step_records = []
    for i, (dx, dy) in enumerate(steps):
        y0, y1 = y0 + dy, y1 + dy
        x0, x1 = x0 + dx, x1 + dx
        # Clamp to level bounds (keep viewport size constant where possible).
        if y1 > h:
            y0, y1 = y0 - (y1 - h), h
        if x1 > w:
            x0, x1 = x0 - (x1 - w), w
        y0, x0 = max(0, y0), max(0, x0)
        bbox = (y0, x0, y1, x1)

        step_tiles = tiles_covering(bbox, tile_size)
        new_tiles = step_tiles - known_tiles
        crossed = step_tiles != prev_tileset
        known_tiles |= step_tiles
        prev_tileset = step_tiles

        keys = [make_key(tx, ty) for tx, ty in step_tiles]
        reqs = [TileRequest(key=k, generation=generation_base + i, priority=0) for k in keys]
        _results, wall_ms, records = fill_sync(sched, reqs)

        new_tile_io = [r["io_ms"] for r in records if r["io_ms"] is not None]
        new_tile_kernel = [r["kernel_ms"] for r in records if r["kernel_ms"] is not None]

        step_records.append({
            "step": i,
            "dx": dx, "dy": dy,
            "crossed_boundary": bool(crossed),
            "new_column": bool(crossed and dx != 0),
            "new_row": bool(crossed and dy != 0),
            "n_new_tiles": len(new_tiles),
            "n_tiles_total": len(step_tiles),
            "wall_ms": wall_ms,
            "new_tile_io_ms": new_tile_io,
            "new_tile_kernel_ms": new_tile_kernel,
        })
    return step_records


# ─────────────────────────── main per-config run ──────────────────────────

def run_config(provider, channel, tile_size, level, out):
    source = provider.source_identity()
    grid = TileGridSpec(tile_size=tile_size, source_chunk_shape=(), grid_version="v1")
    level_shape = provider.level_shape(level)
    downsample = provider.level_downsample(level)

    tiles, vp_box = tiles_for_viewport(level_shape, tile_size, VIEWPORT_PX)

    def make_key(tx, ty, method_param=None):
        addr = TileAddress(grid=grid, level=level, tx=tx, ty=ty)
        if method_param is None:
            return RawKey(source=source, channel=channel, tile=addr)
        method, base_param = method_param
        eff_param = effective_param(base_param, level, downsample) \
            if level > 0 else base_param
        return CorrectionKey(
            source=source, channel=channel, tile=addr, method=method,
            params=(eff_param,), algorithm_version=ALGO_VERSION,
            quality=QualityLevel.INTERACTIVE if level > 0 else QualityLevel.NATIVE,
        ), base_param, eff_param

    def new_stack():
        raw_cache = LRUByteCache(RAW_CACHE_BYTES)
        corr_cache = LRUByteCache(CORR_CACHE_BYTES)
        compute = CorrectionCompute(provider, raw_cache)
        sched = TileScheduler(provider, compute, raw_cache, corr_cache,
                               io_workers=4, compute_workers=1)
        return sched, raw_cache, corr_cache

    # Randomized method order for this config, seed recorded for reproducibility.
    rand_seed = random.randrange(1 << 30)
    rng = random.Random(rand_seed)
    method_order = list(METHOD_PARAMS)
    rng.shuffle(method_order)

    config_result = {
        "tile_size": tile_size, "level": level, "downsample": downsample,
        "n_tiles": len(tiles), "random_seed": rand_seed,
        "method_order": [f"{m}_{p}" for m, p in method_order],
        "rounds": [],
        "decoder_cold": None,
        "pan": None,
        "cache_hit_lookup": None,
        "cache_stats": {},
        "rss": {}, "gpu_mem_pool_peak": {}, "gpu_device_mem_info": None,
    }

    sched, raw_cache, corr_cache = new_stack()
    gen = 0
    decoder_cold_recorded = False
    peak_gpu_used = 0

    for round_idx in range(ROUNDS):
        phase_label = "app-cold" if round_idx == 0 else "warm"
        if round_idx == 0:
            # Fresh caches for round 1 ("app-cold"); OS page-cache state is
            # NOT controlled/known by this script.
            sched.shutdown()
            sched, raw_cache, corr_cache = new_stack()

        round_records = {"phase": phase_label, "round": round_idx, "methods": {}}
        for method, base_param in method_order:
            key_param_pairs = [make_key(tx, ty, (method, base_param)) for tx, ty in tiles]
            keys = [kp[0] for kp in key_param_pairs]
            eff_param = key_param_pairs[0][2] if key_param_pairs else base_param
            gen += 1
            reqs = [TileRequest(key=k, generation=gen, priority=0) for k in keys]

            t_round_start = time.perf_counter()
            _results, wall_ms, records = fill_sync(sched, reqs)
            round_wall_ms = (time.perf_counter() - t_round_start) * 1000.0

            if round_idx == 0 and not decoder_cold_recorded:
                # decoder-cold datum: the very first raw fill of round 1 of the
                # first method in this config's randomized order.
                first_io = next((r["io_ms"] for r in records if r["io_ms"] is not None), None)
                config_result["decoder_cold"] = {
                    "method": method, "base_param": base_param,
                    "first_tile_io_ms": first_io,
                    "note": "decoder-cold (first in-process decode), NOT OS-cold",
                }
                decoder_cold_recorded = True

            io_list = [r["io_ms"] for r in records if r["io_ms"] is not None]
            kernel_list = [r["kernel_ms"] for r in records if r["kernel_ms"] is not None]
            gpu_stats = gpu_mem_pool_stats()
            if gpu_stats["used_bytes"]:
                peak_gpu_used = max(peak_gpu_used, gpu_stats["used_bytes"])

            round_records["methods"][f"{method}_{base_param}"] = {
                "base_param": base_param, "effective_param": eff_param,
                "n_tiles": len(keys),
                "wall_ms": round_wall_ms,
                "io_ms_p50": pct(io_list, 50), "io_ms_p90": pct(io_list, 90),
                "kernel_ms_p50": pct(kernel_list, 50), "kernel_ms_p90": pct(kernel_list, 90),
                "n_samples": len(records),
                "gpu_mem_pool": gpu_stats,
            }
        config_result["rounds"].append(round_records)
        config_result["gpu_mem_pool_peak"][phase_label] = max(
            config_result["gpu_mem_pool_peak"].get(phase_label, 0), peak_gpu_used
        )

    # "cache-hit lookup" phase (NOT FPS / NOT G1-render): re-request the last
    # method's tile set, expect all cache hits.
    last_method, last_param = method_order[-1]
    key_param_pairs = [make_key(tx, ty, (last_method, last_param)) for tx, ty in tiles]
    keys = [kp[0] for kp in key_param_pairs]
    gen += 1
    reqs = [TileRequest(key=k, generation=gen, priority=0) for k in keys]
    lookup_results, lookup_wall_ms, lookup_records = fill_sync(sched, reqs)
    hits = sum(1 for r in lookup_records if r["cache_hit"] or r["io_ms"] is None)
    config_result["cache_hit_lookup"] = {
        "wall_ms": lookup_wall_ms, "n_tiles": len(keys),
        "per_tile_ms": lookup_wall_ms / max(1, len(keys)),
        "label": "G1-data-cache: cache-hit lookup (render path untested)",
    }

    # Pan trajectory.
    def pan_make_key(tx, ty):
        return make_key(tx, ty, (last_method, last_param))[0]
    config_result["pan"] = run_pan_test(sched, pan_make_key, vp_box, tile_size, level_shape)

    config_result["cache_stats"] = {"raw": raw_cache.stats(), "corrected": corr_cache.stats()}
    current_kb, peak_kb = rss_kb()
    config_result["rss"] = {"current_kb": current_kb, "peak_kb_ru_maxrss": peak_kb}
    config_result["gpu_device_mem_info"] = gpu_device_mem_info()

    sched.shutdown()
    return config_result


def summarize_pan(pan_steps):
    """Four buckets, reported separately (never conflated):

    - non_crossing: cache-hit-only steps (tile set unchanged from prior step)
    - crossing_new_column: boundary crossing that introduced new column tiles
    - crossing_new_row: boundary crossing that introduced new row tiles
    - new_tile_fill: ALL steps that actually fetched >=1 new tile (overall
      wall p50/p95) — this is the number that speaks to boundary-crossing
      performance; the other three are diagnostic breakdowns.

    Also runs the hard self-check: if NO step in this run fetched a new
    tile, `warning` is set so the caller can print it into the report
    instead of silently claiming boundary performance.
    """
    def bucket_stats(steps):
        walls = [s["wall_ms"] for s in steps]
        return {
            "n_steps": len(steps),
            "wall_p50_ms": pct(walls, 50), "wall_p95_ms": pct(walls, 95),
            "wall_max_ms": max(walls) if walls else None,
        }

    non_crossing = [s for s in pan_steps if not s["crossed_boundary"]]
    crossing_new_column = [s for s in pan_steps if s["new_column"]]
    crossing_new_row = [s for s in pan_steps if s["new_row"]]
    new_tile_fill = [s for s in pan_steps if s["n_new_tiles"] > 0]

    any_new_tiles = any(s["n_new_tiles"] > 0 for s in pan_steps)
    warning = None
    if not any_new_tiles:
        warning = ("WARNING: pan trajectory fetched ZERO new tiles in every "
                    "step (n_new_tiles=0 for all steps) — this run measured "
                    "cache-hit lookups only; it says NOTHING about "
                    "boundary-crossing / new-tile-fill performance.")

    return {
        "non_crossing": bucket_stats(non_crossing),
        "crossing_new_column": bucket_stats(crossing_new_column),
        "crossing_new_row": bucket_stats(crossing_new_row),
        "new_tile_fill_overall": bucket_stats(new_tile_fill),
        "n_steps_total": len(pan_steps),
        "any_new_tiles": any_new_tiles,
        "warning": warning,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--path", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--channel", default=None, help="channel name or integer index")
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)

    env_before = environment_block()
    first_kernel = kernel_first_touch_cost()

    provider = RawTileProvider(args.path)

    if args.channel is None:
        channel_index = 0
    else:
        try:
            channel_index = int(args.channel)
        except ValueError:
            channel_index = provider.channel_index(args.channel)
    channel_name = provider.channel_names[channel_index]

    tile_sizes = [512] if args.quick else TILE_SIZES
    levels = [0, 1] if args.quick else LEVELS

    channel_stats = sample_channel_stats(provider, channel_index)

    results = []
    for tile_size in tile_sizes:
        for level in levels:
            if level >= provider.num_levels:
                continue
            results.append(run_config(provider, channel_name, tile_size, level, args.out))

    env_after = {"gpu_morph_available_after": bool(bg_correction.GPU_MORPH_AVAILABLE)}
    fallback_occurred = env_before["gpu_morph_available_before"] and not env_after["gpu_morph_available_after"]

    output = {
        "environment": env_before,
        "environment_after": env_after,
        "gpu_fallback_occurred_mid_run": fallback_occurred,
        "kernel_first_touch": first_kernel,
        "channel": {"name": channel_name, "index": channel_index, **channel_stats},
        "raw_cache_budget_bytes": RAW_CACHE_BYTES,
        "corrected_cache_budget_bytes": CORR_CACHE_BYTES,
        "configs": results,
    }

    results_path = os.path.join(args.out, "benchmark_results.json")
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, default=str)

    report_path = os.path.join(args.out, "benchmark_report.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("# Viewer prototype benchmark (measured-only)\n\n")
        f.write(f"Dataset: `{args.path}`\n\n")
        f.write("## Environment\n\n")
        for k, v in env_before.items():
            f.write(f"- {k}: `{v}`\n")
        f.write(f"- gpu_morph_available_after_run: `{env_after['gpu_morph_available_after']}`\n")
        f.write(f"- gpu_fallback_occurred_mid_run: `{fallback_occurred}`\n")
        f.write("- kernel wall time includes transfers; backend=GPU per env block\n")
        f.write(f"- kernel first-touch (init cost): {fmt(first_kernel['first_kernel_call_ms'])} ms\n\n")
        f.write(f"## Channel: {channel_name} (index {channel_index})\n\n")
        f.write(f"dtype={channel_stats['dtype']} min={fmt(channel_stats['min'])} "
                f"mean={fmt(channel_stats['mean'])} p99={fmt(channel_stats['p99'])} "
                f"max={fmt(channel_stats['max'])}\n\n")
        f.write(f"Cache budgets: raw={RAW_CACHE_BYTES / 1e6:.0f} MB, "
                f"corrected={CORR_CACHE_BYTES / 1e6:.0f} MB "
                "(512 = provisional default candidate)\n\n")

        f.write("## Per-config results\n\n")
        for cfg in results:
            f.write(f"### tile={cfg['tile_size']} level={cfg['level']} "
                    f"(downsample={cfg['downsample']})\n\n")
            f.write(f"random_seed={cfg['random_seed']} method_order={cfg['method_order']}\n\n")
            if cfg["decoder_cold"]:
                dc = cfg["decoder_cold"]
                f.write(f"- decoder-cold (NOT OS-cold): first tile io_ms="
                        f"{fmt(dc['first_tile_io_ms'])} ({dc['method']}_{dc['base_param']})\n")
            f.write("\n| phase | method | base | eff | tiles | wall ms | io p50/p90 "
                    "| kernel p50/p90 | n |\n")
            f.write("|---|---|---|---|---|---|---|---|---|\n")
            for rnd in cfg["rounds"]:
                for name, m in rnd["methods"].items():
                    f.write(f"| {rnd['phase']} | {name} | {m['base_param']} | "
                            f"{m['effective_param']} | {m['n_tiles']} | {fmt(m['wall_ms'])} "
                            f"| {fmt(m['io_ms_p50'])}/{fmt(m['io_ms_p90'])} "
                            f"| {fmt(m['kernel_ms_p50'])}/{fmt(m['kernel_ms_p90'])} "
                            f"| {m['n_samples']} |\n")
            lookup = cfg["cache_hit_lookup"]
            f.write(f"\n{lookup['label']}: {fmt(lookup['wall_ms'])}ms total, "
                    f"{fmt(lookup['per_tile_ms'])}ms/tile over {lookup['n_tiles']} tiles "
                    f"(measured-only)\n")
            f.write(f"\nG1-data-cache: cache-hit lookup <= {fmt(lookup['per_tile_ms'])}ms "
                    "(render path untested)\n")

            pan_summary = summarize_pan(cfg["pan"])
            if pan_summary["warning"]:
                f.write(f"\n{pan_summary['warning']}\n")
            f.write(f"\nPan ({pan_summary['n_steps_total']}-step, quarter-tile, "
                    "alternating x/y, floating bbox):\n\n")
            for label, key in (
                ("non-crossing (cache-hit)", "non_crossing"),
                ("crossing, new column", "crossing_new_column"),
                ("crossing, new row", "crossing_new_row"),
                ("new-tile fill (overall)", "new_tile_fill_overall"),
            ):
                b = pan_summary[key]
                f.write(f"- {label}: n={b['n_steps']} wall p50={fmt(b['wall_p50_ms'])}ms "
                        f"p95={fmt(b['wall_p95_ms'])}ms max={fmt(b['wall_max_ms'])}ms\n")

            cs = cfg["cache_stats"]
            f.write(f"\nCache stats: raw={cs['raw']} corrected={cs['corrected']}\n")
            f.write(f"RSS: current={cfg['rss']['current_kb']}KB "
                    f"peak(ru_maxrss)={cfg['rss']['peak_kb_ru_maxrss']}KB\n")
            f.write(f"GPU mem pool peak: {cfg['gpu_mem_pool_peak']}\n")
            f.write(f"GPU device mem_info (free/total): {cfg['gpu_device_mem_info']}\n\n")

    print(report_path)


if __name__ == "__main__":
    main()
