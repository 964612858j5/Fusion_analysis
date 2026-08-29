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

# Self-bootstrap: make `block01` resolve to THIS tree regardless of the
# checkout directory name (worktrees like block01_v14), same as the repo's
# conftest.py does for pytest. Allows: python scripts/benchmark_viewer_prototype.py
if "block01" not in sys.modules:
    import importlib.util as _ilu
    import pathlib as _pl
    _root = _pl.Path(__file__).resolve().parent.parent
    _spec = _ilu.spec_from_file_location(
        "block01", _root / "__init__.py",
        submodule_search_locations=[str(_root)])
    _mod = _ilu.module_from_spec(_spec)
    sys.modules["block01"] = _mod
    _spec.loader.exec_module(_mod)


def _register_block01_alias():
    """Bind the `block01` package name to THIS checkout, regardless of the
    directory name it lives in (e.g. a `block01_v14` git worktree) — mirrors
    the repo-root conftest.py shim so `python scripts/benchmark_...py` and
    `pytest` resolve the same module tree."""
    import importlib.util
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    existing = sys.modules.get("block01")
    if existing is not None:
        path = getattr(existing, "__file__", "") or ""
        if pathlib.Path(path).resolve().parent == root:
            return
        return  # a different block01 is already imported; don't clobber it
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
)

VIEWPORT_PX = 2048
METHOD_PARAMS = [("tophat", 25), ("tophat", 50), ("cucim", 50), ("cucim", 100)]
TILE_SIZES = [256, 512, 1024]
LEVELS = [0, 1, 2]
ALGO_VERSION = "v1"
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
    tx0, tx1 = x0 // tile_size, (x1 - 1) // tile_size
    ty0, ty1 = y0 // tile_size, (y1 - 1) // tile_size
    tiles = [(tx, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)]
    return tiles, (y0, x0, y1, x1)


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

def pan_trajectory_steps(tile_size, n_steps=8):
    """8-step continuous trajectory: quarter-tile steps, alternating +x/+y,
    so some steps cross a tile boundary and some don't."""
    q = tile_size // 4
    steps = []
    for i in range(n_steps):
        if i % 2 == 0:
            steps.append((q, 0))
        else:
            steps.append((0, q))
    return steps


def run_pan_test(sched, make_key, start_tiles, tile_size, level_shape, generation_base=1000):
    """Walk the continuous pan trajectory; per-step record new-tile count and
    step-prep wall-clock ms."""
    max_tx = level_shape[1] // tile_size
    max_ty = level_shape[0] // tile_size

    # Track viewport top-left in pixel space, starting at the tile-set's
    # min tx/ty * tile_size (approximation for a synthetic viewport).
    px = min(tx for tx, _ in start_tiles) * tile_size
    py = min(ty for _, ty in start_tiles) * tile_size
    known_tiles = set(start_tiles)

    steps = pan_trajectory_steps(tile_size)
    step_records = []
    for i, (dx, dy) in enumerate(steps):
        px += dx
        py += dy
        crossed = (dx != 0 and (px // tile_size) != ((px - dx) // tile_size)) or \
                  (dy != 0 and (py // tile_size) != ((py - dy) // tile_size))
        tx0 = px // tile_size
        ty0 = py // tile_size
        n_tx = max(1, VIEWPORT_PX // tile_size)
        n_ty = max(1, VIEWPORT_PX // tile_size)
        step_tiles = [
            (tx0 + j, ty0 + k) for k in range(n_ty) for j in range(n_tx)
            if 0 <= tx0 + j <= max_tx and 0 <= ty0 + k <= max_ty
        ]
        new_tiles = [t for t in step_tiles if t not in known_tiles]
        known_tiles.update(step_tiles)

        keys = [make_key(tx, ty) for tx, ty in step_tiles]
        reqs = [TileRequest(key=k, generation=generation_base + i, priority=0) for k in keys]
        _results, wall_ms, _records = fill_sync(sched, reqs)

        step_records.append({
            "step": i,
            "dx": dx, "dy": dy,
            "crossed_boundary": bool(crossed),
            "n_new_tiles": len(new_tiles),
            "n_tiles_total": len(step_tiles),
            "prep_ms": wall_ms,
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
    config_result["pan"] = run_pan_test(sched, pan_make_key, tiles, tile_size, level_shape)

    config_result["cache_stats"] = {"raw": raw_cache.stats(), "corrected": corr_cache.stats()}
    current_kb, peak_kb = rss_kb()
    config_result["rss"] = {"current_kb": current_kb, "peak_kb_ru_maxrss": peak_kb}
    config_result["gpu_device_mem_info"] = gpu_device_mem_info()

    sched.shutdown()
    return config_result


def summarize_pan(pan_steps):
    prep_ms = [s["prep_ms"] for s in pan_steps]
    crossed = sum(1 for s in pan_steps if s["crossed_boundary"])
    return {
        "p50_ms": pct(prep_ms, 50), "p95_ms": pct(prep_ms, 95),
        "max_ms": max(prep_ms) if prep_ms else None,
        "n_boundary_crossing": crossed,
        "n_non_crossing": len(pan_steps) - crossed,
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
    with open(results_path, "w") as f:
        json.dump(output, f, indent=2, default=str)

    report_path = os.path.join(args.out, "benchmark_report.md")
    with open(report_path, "w") as f:
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
            f.write(f"\nPan (8-step, quarter-tile, alternating x/y): "
                    f"p50={fmt(pan_summary['p50_ms'])}ms p95={fmt(pan_summary['p95_ms'])}ms "
                    f"max={fmt(pan_summary['max_ms'])}ms, "
                    f"boundary-crossing={pan_summary['n_boundary_crossing']}, "
                    f"non-crossing={pan_summary['n_non_crossing']}\n")

            cs = cfg["cache_stats"]
            f.write(f"\nCache stats: raw={cs['raw']} corrected={cs['corrected']}\n")
            f.write(f"RSS: current={cfg['rss']['current_kb']}KB "
                    f"peak(ru_maxrss)={cfg['rss']['peak_kb_ru_maxrss']}KB\n")
            f.write(f"GPU mem pool peak: {cfg['gpu_mem_pool_peak']}\n")
            f.write(f"GPU device mem_info (free/total): {cfg['gpu_device_mem_info']}\n\n")

    print(report_path)


if __name__ == "__main__":
    main()
