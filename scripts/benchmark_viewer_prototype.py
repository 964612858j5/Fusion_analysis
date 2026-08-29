"""Benchmark the v15 viewer-foundation prototype on a real OME-TIFF.

Usage:
    python -m block01.scripts.benchmark_viewer_prototype \
        --path /path/to/slide.ome.tif --out /tmp/bench [--channel DAPI] [--quick]

For each (tile_size, level, method) configuration, computes the tiles
covering a 2048x2048 viewport at the image center, then measures:
  - COLD fill (fresh caches): per-tile io_ms/kernel_ms/total_ms.
  - WARM re-fill of the same viewport (expect all cache hits).
  - PAN: viewport shifted by half a tile; reused vs. new tile counts, fill time.
Also measures a raw-only (uncorrected) fill per tile size/level.

Writes <out>/benchmark_results.json (raw numbers) and
<out>/benchmark_report.md (summary tables), and prints the report path.
"""

import argparse
import json
import os
import resource
import statistics
import sys
import threading
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from block01.viewer.caches import LRUByteCache
from block01.viewer.correction_compute import CorrectionCompute
from block01.viewer.raw_tile_provider import RawTileProvider
from block01.viewer.scheduler import TileScheduler
from block01.viewer.tile_types import (
    CorrectionKey,
    QualityLevel,
    RawKey,
    TileAddress,
    TileGridSpec,
    TileRequest,
)

VIEWPORT_PX = 2048
METHODS = [("tophat", 25), ("tophat", 50), ("cucim", 50), ("cucim", 100)]
TILE_SIZES = [256, 512, 1024]
LEVELS = [0, 1, 2]
ALGO_VERSION = "v1"


def gpu_mem_bytes():
    try:
        import cupy
        return int(cupy.get_default_memory_pool().used_bytes())
    except Exception:
        return None


def rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def tiles_for_viewport(level_shape, tile_size, viewport_px):
    """List of (tx, ty) tiles covering a `viewport_px` box centered on the level."""
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


def shift_tiles(tiles, tile_size, dx, dy):
    """PAN: return tile coords shifted by (dx, dy) tiles (may be fractional -> rounds)."""
    return [(tx + dx, ty + dy) for tx, ty in tiles]


def fill_sync(scheduler, requests):
    """Issue all `requests` and block until every callback has fired.

    Returns (results_by_key, wall_ms). Single-threaded collection via an
    Event per request, as specified.
    """
    done = threading.Event()
    remaining = [len(requests)]
    lock = threading.Lock()
    results = {}

    def make_cb(key):
        def cb(result):
            with lock:
                results[key] = result
                remaining[0] -= 1
                if remaining[0] == 0:
                    done.set()
        return cb

    t0 = time.perf_counter()
    for req in requests:
        scheduler.request(req, make_cb(req.key))
    done.wait(timeout=300)
    wall_ms = (time.perf_counter() - t0) * 1000.0
    return results, wall_ms


def pct(values, p):
    if not values:
        return None
    values = sorted(values)
    idx = min(len(values) - 1, int(round(p / 100.0 * (len(values) - 1))))
    return values[idx]


def run_config(provider, tile_size, level, method_params, out):
    method, param = method_params if method_params else (None, None)
    source = provider.source_identity()
    grid = TileGridSpec(tile_size=tile_size, source_chunk_shape=(), grid_version="v1")
    level_shape = provider.level_shape(level)
    channel = provider.channel_names[0]

    tiles, vp_box = tiles_for_viewport(level_shape, tile_size, VIEWPORT_PX)

    def make_key(tx, ty):
        addr = TileAddress(grid=grid, level=level, tx=tx, ty=ty)
        if method is None:
            return RawKey(source=source, channel=channel, tile=addr)
        return CorrectionKey(
            source=source, channel=channel, tile=addr, method=method,
            params=(param,), algorithm_version=ALGO_VERSION,
            quality=QualityLevel.INTERACTIVE,
        )

    def new_stack():
        raw_cache = LRUByteCache(2_000_000_000)
        corr_cache = LRUByteCache(2_000_000_000)
        compute = CorrectionCompute(provider)
        sched = TileScheduler(provider, compute, raw_cache, corr_cache,
                               io_workers=4, compute_workers=1)
        return sched, raw_cache, corr_cache

    sched, raw_cache, corr_cache = new_stack()
    keys = [make_key(tx, ty) for tx, ty in tiles]
    reqs = [TileRequest(key=k, generation=0, priority=0) for k in keys]

    cold_results, cold_wall_ms = fill_sync(sched, reqs)
    io_list, kernel_list = [], []
    for r in cold_results.values():
        t = r.timing or {}
        if t.get("io_ms") is not None:
            io_list.append(t["io_ms"])
        if t.get("kernel_ms") is not None:
            kernel_list.append(t["kernel_ms"])

    warm_reqs = [TileRequest(key=k, generation=1, priority=0) for k in keys]
    warm_results, warm_wall_ms = fill_sync(sched, warm_reqs)
    warm_hits = sum(1 for r in warm_results.values() if r.timing.get("cache") == "hit")

    half = tile_size // 2
    panned_tiles = tiles_for_viewport(
        (level_shape[0], level_shape[1]), tile_size, VIEWPORT_PX
    )[0]
    # shift viewport box by half a tile in x, recompute tile coverage
    vp_y0, vp_x0, vp_y1, vp_x1 = vp_box
    shifted_x0 = min(level_shape[1] - 1, vp_x0 + half)
    shifted_tx0 = shifted_x0 // tile_size
    dx = shifted_tx0 - min(tx for tx, _ in tiles)
    pan_tile_coords = sorted(set(shift_tiles(tiles, tile_size, dx, 0)))
    max_tx = level_shape[1] // tile_size
    max_ty = level_shape[0] // tile_size
    pan_tile_coords = [(tx, ty) for tx, ty in pan_tile_coords if 0 <= tx <= max_tx and 0 <= ty <= max_ty]
    pan_keys = [make_key(tx, ty) for tx, ty in pan_tile_coords]
    reused = sum(1 for k in pan_keys if k in keys)
    pan_reqs = [TileRequest(key=k, generation=2, priority=0) for k in pan_keys]
    pan_results, pan_wall_ms = fill_sync(sched, pan_reqs)

    sched.shutdown()

    return {
        "tile_size": tile_size,
        "level": level,
        "method": method,
        "param": param,
        "n_tiles": len(tiles),
        "cold_wall_ms": cold_wall_ms,
        "io_ms_median": statistics.median(io_list) if io_list else None,
        "io_ms_p90": pct(io_list, 90),
        "kernel_ms_median": statistics.median(kernel_list) if kernel_list else None,
        "kernel_ms_p90": pct(kernel_list, 90),
        "warm_wall_ms": warm_wall_ms,
        "warm_hit_pct": 100.0 * warm_hits / max(1, len(warm_results)),
        "pan_n_tiles": len(pan_keys),
        "pan_reused": reused,
        "pan_reuse_pct": 100.0 * reused / max(1, len(pan_keys)),
        "pan_wall_ms": pan_wall_ms,
        "rss_mb": rss_mb(),
        "gpu_mem_bytes": gpu_mem_bytes(),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--path", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--channel", default=None)
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    provider = RawTileProvider(args.path)

    tile_sizes = [512] if args.quick else TILE_SIZES
    levels = [0, 1] if args.quick else LEVELS
    methods = [("tophat", 25), ("cucim", 50)] if args.quick else METHODS

    results = []
    for tile_size in tile_sizes:
        for level in levels:
            if level >= provider.num_levels:
                continue
            results.append(run_config(provider, tile_size, level, None, args.out))
            for m in methods:
                results.append(run_config(provider, tile_size, level, m, args.out))

    results_path = os.path.join(args.out, "benchmark_results.json")
    with open(results_path, "w") as f:
        json.dump(results, f, indent=2)

    report_path = os.path.join(args.out, "benchmark_report.md")
    with open(report_path, "w") as f:
        f.write("# Viewer prototype benchmark\n\n")
        f.write(f"Dataset: `{args.path}`\n\n")
        f.write("| tile | level | method | param | tiles | cold s | io ms med/p90 "
                "| kernel ms med/p90 | warm ms | pan reuse% | pan ms | RSS MB | GPU MB |\n")
        f.write("|---|---|---|---|---|---|---|---|---|---|---|---|---|\n")
        for r in results:
            gpu_mb = f"{r['gpu_mem_bytes'] / 1e6:.1f}" if r["gpu_mem_bytes"] is not None else "n/a"
            f.write(
                f"| {r['tile_size']} | {r['level']} | {r['method'] or 'raw'} | {r['param'] or '-'} "
                f"| {r['n_tiles']} | {r['cold_wall_ms'] / 1000.0:.2f} "
                f"| {fmt(r['io_ms_median'])}/{fmt(r['io_ms_p90'])} "
                f"| {fmt(r['kernel_ms_median'])}/{fmt(r['kernel_ms_p90'])} "
                f"| {r['warm_wall_ms']:.1f} | {r['pan_reuse_pct']:.0f}% | {r['pan_wall_ms']:.1f} "
                f"| {r['rss_mb']:.1f} | {gpu_mb} |\n"
            )

    print(report_path)


def fmt(v):
    return "n/a" if v is None else f"{v:.2f}"


if __name__ == "__main__":
    main()
