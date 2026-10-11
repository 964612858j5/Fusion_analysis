"""v16_5 R1 (plan §7 gates 5-6): read speed and memory of the Rust Step1
reader against the Python one, on the server's real corrected product.

    r1_speed_probe.py corrected   -> corrected tiles, 6 threads: warm, warm
                                     with a busy GUI thread, cold -- Python
                                     threads calling each reader, and the
                                     Rust reader's own threads (`read_many`,
                                     as the reader pool runs)
    r1_speed_probe.py coarse      -> the whole-region coarse tile, one at a
                                     time (Python on the main thread and on
                                     a worker; Rust), and 6 at once in Rust
                                     with the process's peak RSS
    r1_speed_probe.py zstd        -> a synthetic zstd slide: Rust vs
                                     imagecodecs per block
One JSON line per mode. Read only.
"""

import json
import os
import sys
import tempfile
import threading
import time
import types

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import conftest  # noqa: E402,F401

from block01.ui.step1_core_adapter import build_corrected_manifest, build_raw_manifest  # noqa: E402
from block01.ui.step1_viewer_host import Step1TileProvider  # noqa: E402
from block01.viewer.raw_tile_provider import RawTileProvider  # noqa: E402
from block01.viewer.step1_source import Step1SourceTable  # noqa: E402

import fusion_viewer_core as core  # noqa: E402

PROJECT = "/sda1/Fusion/tmp/rois/full_wsi_20260810_051003_7a0d/step0"
SLIDE = "/sda1/Fusion/benchmark/biopsy.ome.tif"
CHANNELS = ["CD45RA", "CD45RO", "HsBAg"]
TILE = 512


def rig():
    decisions = json.load(open(os.path.join(PROJECT, "correction_config.json")))["channel_decisions"]
    raw = RawTileProvider(SLIDE)
    table = Step1SourceTable(decisions=decisions,
                             corrected_zarr_path=os.path.join(PROJECT, "corrected_channels.zarr"),
                             roi_name="Full WSI", roi_bbox=[0, 19480, 0, 21804])
    provider = Step1TileProvider(raw, table)
    strides = [max(1, int(round(raw.level_downsample(l)))) for l in range(raw.num_levels)]
    reader = core.Step1Reader(build_raw_manifest(raw), build_corrected_manifest(table, raw, strides))
    return raw, provider, reader


def tile(level, ty, tx):
    return types.SimpleNamespace(level=level, ty=ty, tx=tx, grid=types.SimpleNamespace(tile_size=TILE))


def drop_product_cache():
    """posix_fadvise(DONTNEED) on every chunk file of the product."""
    root = os.path.join(PROJECT, "corrected_channels.zarr")
    for folder, _dirs, files in os.walk(root):
        for name in files:
            fd = os.open(os.path.join(folder, name), os.O_RDONLY)
            try:
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            finally:
                os.close(fd)


def busy_gui(stop):
    """A pure-Python thread that keeps taking the GIL, like a busy GUI."""
    x = 0
    while not stop.is_set():
        for i in range(2000):
            x += i * i


def run_threads(jobs, fn, threads=6):
    lock = threading.Lock()
    queue = list(jobs)

    def worker():
        while True:
            with lock:
                if not queue:
                    return
                job = queue.pop()
            fn(job)

    t0 = time.perf_counter()
    ts = [threading.Thread(target=worker) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return time.perf_counter() - t0


def mode_corrected():
    raw, provider, reader = rig()
    rng = np.random.default_rng(3)
    jobs = []
    for level in (0, 1, 2):
        h, w = raw.level_shape(level)
        ny, nx = -(-h // TILE), -(-w // TILE)
        for _ in range({0: 300, 1: 120, 2: 40}[level]):
            jobs.append((CHANNELS[int(rng.integers(3))], tile(level, int(rng.integers(ny)), int(rng.integers(nx)))))

    def py(job):
        provider.read_tile_native(job[0], job[1])

    def rs(job):
        rect, stride = provider._level_rect(job[1])
        reader.read_tile(job[0], job[1].level, *rect, stride, TILE)

    native_jobs = []
    for ch, t in jobs:
        rect, stride = provider._level_rect(t)
        native_jobs.append((ch, t.level, *rect, stride, TILE))

    def rust_native():
        wall, n = reader.read_many(native_jobs, 6)
        assert n == len(native_jobs)
        return wall

    out = {"mode": "corrected", "tiles": len(jobs)}
    for f in (py, rs):                                  # warm the page cache once
        run_threads(jobs, f)
    for name, f in (("python", py), ("rust", rs)):
        out[f"{name}_warm_tiles_per_s"] = round(len(jobs) / min(run_threads(jobs, f) for _ in range(3)), 1)
        stop = threading.Event()
        b = threading.Thread(target=busy_gui, args=(stop,))
        b.start()
        out[f"{name}_warm_busy_tiles_per_s"] = round(len(jobs) / run_threads(jobs, f), 1)
        stop.set()
        b.join()
    # the readers as the pool runs them: Rust threads, no Python per tile
    rust_native()
    out["rust_native_warm_tiles_per_s"] = round(len(jobs) / min(rust_native() for _ in range(3)), 1)
    stop = threading.Event()
    b = threading.Thread(target=busy_gui, args=(stop,))
    b.start()
    out["rust_native_warm_busy_tiles_per_s"] = round(len(jobs) / min(rust_native() for _ in range(3)), 1)
    stop.set()
    b.join()
    for name, f in (("python", py), ("rust", rs)):
        drop_product_cache()
        out[f"{name}_cold_tiles_per_s"] = round(len(jobs) / run_threads(jobs, f), 1)
    drop_product_cache()
    out["rust_native_cold_tiles_per_s"] = round(len(jobs) / rust_native(), 1)
    raw.close()
    print(json.dumps(out), flush=True)


def rss_mb():
    with open("/proc/self/status") as fh:
        for line in fh:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024.0
    return 0.0


def mode_coarse():
    raw, provider, reader = rig()
    level = raw.num_levels - 1
    t = tile(level, 0, 0)
    rect, stride = provider._level_rect(t)
    out = {"mode": "coarse", "level": level, "stride": stride, "rect": rect}
    t0 = time.perf_counter()
    provider.read_tile_native("CD45RA", t)
    out["python_main_s"] = round(time.perf_counter() - t0, 2)
    box = {}

    def py_worker():
        s = time.perf_counter()
        provider.read_tile_native("CD45RA", t)
        box["s"] = time.perf_counter() - s

    th = threading.Thread(target=py_worker)
    th.start()
    th.join()
    out["python_worker_s"] = round(box["s"], 2)
    t0 = time.perf_counter()
    reader.read_tile("CD45RA", level, *rect, stride, TILE)
    out["rust_s"] = round(time.perf_counter() - t0, 2)
    # six at once: the worst case for the readers' temporary memory
    peak = [rss_mb()]
    base = peak[0]
    stop = threading.Event()

    def sample():
        while not stop.is_set():
            peak[0] = max(peak[0], rss_mb())
            time.sleep(0.01)

    sampler = threading.Thread(target=sample)
    sampler.start()
    s = run_threads([CHANNELS[i % 3] for i in range(6)],
                    lambda ch: reader.read_tile(ch, level, *rect, stride, TILE))
    stop.set()
    sampler.join()
    out["rust_six_at_once_s"] = round(s, 2)
    out["rss_base_mb"] = round(base, 1)
    out["rss_peak_extra_mb"] = round(peak[0] - base, 1)
    raw.close()
    print(json.dumps(out), flush=True)


def mode_zstd():
    import tifffile
    folder = tempfile.mkdtemp(prefix="r1zstd")
    path = os.path.join(folder, "z.ome.tif")
    rng = np.random.default_rng(4)
    base = rng.integers(0, 40, size=(3, 4096, 4096), dtype=np.uint8)
    data = np.cumsum(base, axis=2, dtype=np.uint8)          # image-like, compressible
    with tifffile.TiffWriter(path, ome=True) as tw:
        tw.write(data, tile=(512, 512), compression="zstd",
                 metadata={"axes": "CYX", "Channel": {"Name": ["a", "b", "c"]}})
    provider = RawTileProvider(path)
    reader = core.RawReader(build_raw_manifest(provider))
    jobs = [(c, ty, tx) for c in range(3) for ty in range(8) for tx in range(8)]

    def py(job):
        c, ty, tx = job
        provider.read_region(c, 0, ty * 512, ty * 512 + 512, tx * 512, tx * 512 + 512)

    def rs(job):
        c, ty, tx = job
        reader.read_region(c, 0, ty * 512, ty * 512 + 512, tx * 512, tx * 512 + 512)

    out = {"mode": "zstd", "blocks": len(jobs)}
    for name, f in (("python", py), ("rust", rs)):
        run_threads(jobs, f, 1)
        out[f"{name}_1thread_ms_per_block"] = round(min(run_threads(jobs, f, 1) for _ in range(3)) / len(jobs) * 1000, 3)
        out[f"{name}_6threads_blocks_per_s"] = round(len(jobs) / min(run_threads(jobs, f, 6) for _ in range(3)), 1)
    provider.close()
    os.remove(path)
    os.rmdir(folder)
    print(json.dumps(out), flush=True)


if __name__ == "__main__":
    {"corrected": mode_corrected, "coarse": mode_coarse, "zstd": mode_zstd}[sys.argv[1]]()
