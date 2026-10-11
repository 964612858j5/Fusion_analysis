"""v16_5 R0: Rust raw-tile decoding against the existing Python reader.

    compare PATH [--levels 0,1] [--sample N] [--threads 6]
        every tile of every channel (or N random tiles per level) read by
        `RawTileProvider.read_region` and by `fusion_viewer_core.RawReader`,
        compared byte for byte.
    speed PATH [--tiles 2000] [--threads 1,6] [--state cold|warm] [--busy]
        the same random tile set read by each path; tiles/ms and per-tile
        latency. cold drops the file from the page cache before EACH run
        (posix_fadvise, this file only). --busy adds one pure-Python thread
        that keeps taking the GIL, standing in for a busy GUI thread.

Measurement only; touches no project and no product state.
"""

import argparse
import concurrent.futures as cf
import json
import os
import random
import sys
import threading
import time

import numpy as np

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.dirname(_REPO), _REPO]
import conftest  # noqa: E402,F401 -- maps the `block01` package to this checkout

from block01.ui.step1_core_adapter import build_raw_manifest  # noqa: E402
from block01.viewer.raw_tile_provider import RawTileProvider  # noqa: E402

import fusion_viewer_core as core  # noqa: E402

TILE = 512


def open_both(path):
    provider = RawTileProvider(path)
    provider.warm_thread_handle()
    reader = core.RawReader(build_raw_manifest(provider))
    return provider, reader


def zarr_provider(path):
    """The same file read through tifffile's own decoding (the path the
    product takes with BLOCK01_DIRECT_TILES=0): the reference for a tile
    the direct path cannot decode."""
    provider = RawTileProvider(path)
    provider._direct_tiles = False
    return provider


def tile_jobs(provider, level, channels=None):
    h, w = provider.level_shape(level)
    chans = range(provider.num_channels) if channels is None else channels
    return [(c, level, ty * TILE, min(h, ty * TILE + TILE), tx * TILE, min(w, tx * TILE + TILE))
            for c in chans for ty in range(-(-h // TILE)) for tx in range(-(-w // TILE))]


def python_read(provider, job):
    c, level, y0, y1, x0, x1 = job
    arr, _ = provider.read_region(c, level, y0, y1, x0, x1)
    return arr


def compare(args):
    provider, reader = open_both(args.path)
    levels = range(provider.num_levels) if not args.levels else [int(v) for v in args.levels.split(",")]
    rng = random.Random(7)
    report = {"path": args.path, "levels": {}}
    pool = cf.ThreadPoolExecutor(args.threads)
    fallback = zarr_provider(args.path)

    def reference(job):
        try:
            return python_read(provider, job), False
        except Exception:                                   # noqa: BLE001 -- counted below
            return python_read(fallback, job), True

    for level in levels:
        jobs = tile_jobs(provider, level)
        if args.sample and len(jobs) > args.sample:
            jobs = rng.sample(jobs, args.sample)
        jobs.sort(key=lambda j: (j[0], j[2], j[4]))      # file order: near-sequential reads
        t0, mismatches, compared, nbytes, direct_failed = time.monotonic(), [], 0, 0, []
        for start in range(0, len(jobs), 4096):
            chunk = jobs[start:start + 4096]
            ours = reader.read_many(chunk, args.threads)
            theirs = list(pool.map(reference, chunk))
            for job, a, (b, failed) in zip(chunk, ours, theirs):
                if failed:
                    direct_failed.append(job)
                compared += 1
                nbytes += len(a)
                if a != np.ascontiguousarray(b).tobytes():
                    mismatches.append(job)
        report["levels"][level] = {"tiles": compared, "bytes": nbytes, "mismatches": len(mismatches),
                                   "first_mismatches": mismatches[:5],
                                   "python_direct_failed": len(direct_failed),
                                   "first_direct_failed": direct_failed[:5],
                                   "seconds": round(time.monotonic() - t0, 1)}
        print(f"level {level}: {compared} tiles, {len(mismatches)} mismatches, "
              f"{len(direct_failed)} the Python direct path could not decode, "
              f"{report['levels'][level]['seconds']} s", flush=True)
    provider.close()
    fallback.close()
    return report


def resident_fraction(path, manifest, jobs, sample=400):
    """How much of these jobs' compressed bytes is in the page cache now
    (mincore on a read-only mapping of the file): the evidence that a
    "cold" run was cold, instead of trusting posix_fadvise (review fix).
    Tiled levels only; a block row/column = the job's top-left block."""
    import ctypes
    import mmap as _mmap

    libc = ctypes.CDLL(None, use_errno=True)
    libc.mmap.restype = ctypes.c_void_p
    libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, ctypes.c_long]
    libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    libc.mincore.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_char_p]
    page = _mmap.PAGESIZE
    size = os.path.getsize(path)
    fd = os.open(path, os.O_RDONLY)
    base = libc.mmap(None, size, _mmap.PROT_READ, _mmap.MAP_SHARED, fd, 0)
    os.close(fd)
    if base in (None, ctypes.c_void_p(-1).value):
        return None
    total = resident = 0
    try:
        for c, level, y0, _y1, x0, _x1 in jobs[:sample]:
            t = manifest["levels"][level]
            across = -(-t["width"] // t["block_cols"])
            blocks = across * -(-t["height"] // t["block_rows"])
            i = c * blocks + (y0 // t["block_rows"]) * across + x0 // t["block_cols"]
            off = int(np.frombuffer(t["offsets"], "<u8", 1, i * 8)[0])
            cnt = int(np.frombuffer(t["counts"], "<u8", 1, i * 8)[0])
            if cnt == 0:
                continue
            start = off // page * page
            pages = -(-(off + cnt - start) // page)
            vec = ctypes.create_string_buffer(pages)
            if libc.mincore(base + start, pages * page, vec) != 0:
                continue
            total += pages
            resident += sum(b & 1 for b in vec.raw)
    finally:
        libc.munmap(base, size)
    return round(resident / total, 4) if total else None


def drop_cache(path):
    fd = os.open(os.path.realpath(path), os.O_RDONLY)
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    finally:
        os.close(fd)


class Busy:
    """One pure-Python thread taking the GIL in short bursts (sys switch
    interval permitting), like a GUI thread doing Python bookkeeping."""

    def __init__(self):
        self._stop = threading.Event()
        self.loops = 0
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            sum(i * i for i in range(2000))
            self.loops += 1

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()


def run_python(provider, jobs, threads):
    spans = []

    def one(job):
        t = time.monotonic()
        arr = python_read(provider, job)
        spans.append((t, time.monotonic()))
        return arr.nbytes

    t0 = time.monotonic()
    with cf.ThreadPoolExecutor(threads) as pool:
        total = sum(pool.map(one, jobs))
    return time.monotonic() - t0, spans, total


def summarise(wall, spans, total, n):
    lat = np.array([(e - s) * 1000.0 for s, e in spans])
    return {"tiles": n, "wall_s": round(wall, 3), "tiles_per_ms": round(n / (wall * 1000.0), 3),
            "MB_per_s": round(total / wall / 1e6, 1),
            "lat_ms_p50": round(float(np.percentile(lat, 50)), 3),
            "lat_ms_p95": round(float(np.percentile(lat, 95)), 3),
            "lat_ms_max": round(float(lat.max()), 3)}


def speed(args):
    provider, reader = open_both(args.path)
    manifest = build_raw_manifest(provider)
    rng = random.Random(11)
    jobs = tile_jobs(provider, 0) + tile_jobs(provider, 1)
    jobs = rng.sample(jobs, args.tiles)
    # tiles the Python direct path cannot decode (qptiff: damaged stream
    # tails, see docs/v16_5_r0_interfaces.md §4.1) are left out of BOTH
    # readers' sets, so the two read exactly the same work
    unreadable = []
    for job in jobs:
        try:
            python_read(provider, job)
        except Exception:                                   # noqa: BLE001 -- counted
            unreadable.append(job)
    jobs = [j for j in jobs if j not in unreadable]
    out = {"path": args.path, "state": args.state, "busy": args.busy,
           "left_out_python_unreadable": len(unreadable), "runs": []}
    for threads in [int(v) for v in args.threads.split(",")]:
        for name in ("rust", "python"):
            if args.state == "cold":
                drop_cache(args.path)
            else:                                            # warm: read once first
                reader.bench(jobs, 6)
            resident = resident_fraction(args.path, manifest, jobs)
            busy = Busy() if args.busy else None
            if busy:
                busy.__enter__()
            try:
                if name == "rust":
                    wall, spans, total, _ = reader.bench(jobs, threads)
                else:
                    wall, spans, total = run_python(provider, jobs, threads)
            finally:
                if busy:
                    busy.__exit__(None, None, None)
            row = {"reader": name, "threads": threads, "resident_before": resident,
                   **summarise(wall, spans, total, len(jobs))}
            if busy:
                row["busy_loops_per_s"] = round(busy.loops / wall, 1)
            out["runs"].append(row)
            print(json.dumps(row), flush=True)
    provider.close()
    return out


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compare")
    c.add_argument("path")
    c.add_argument("--levels", default="")
    c.add_argument("--sample", type=int, default=0)
    c.add_argument("--threads", type=int, default=6)
    s = sub.add_parser("speed")
    s.add_argument("path")
    s.add_argument("--tiles", type=int, default=2000)
    s.add_argument("--threads", default="1,6")
    s.add_argument("--state", choices=("cold", "warm"), default="warm")
    s.add_argument("--busy", action="store_true")
    for p in (c, s):
        p.add_argument("--out", default="")
    args = ap.parse_args()
    report = compare(args) if args.cmd == "compare" else speed(args)
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(report, fh, indent=1)


if __name__ == "__main__":
    main()
