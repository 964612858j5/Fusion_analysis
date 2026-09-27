"""Block S4-0: a baseline of today's Step4 (`FeatureExtractWorker`).

Not imported by the product. Runs the CURRENT worker read-only on a finished
Step2 run and reports, per configuration:

  * split timings that are not double-counted: mask load, morphology
    (coordinate moments + erosion), raw channel reads (`_read_roi_zarr`),
    runtime background correction (`_apply_configured_correction`), reads of
    Step0's persisted corrected product (reference B), per-statistic
    `scipy.ndimage` time, output writing;
  * peak memory two ways: process RSS sampled every 0.2 s, and
    `resource.getrusage(RUSAGE_SELF).ru_maxrss`; peak GPU memory when a
    cuCIM channel is corrected;
  * provenance: git commit, data paths and identities, run, mask, Step0
    manifest, corrected product, machine.

Configurations: `default` (mean only -- the Step4 page's default), `fast`
(mean / sum / std / min / max), `dist` (median + p90 on TWO channels only,
the worker's own calls replicated; any all-channel figure is a PROJECTION).
`--source corrected` runs the worker with Step0's persisted corrected
product wired into its loader (reference B, the source-equivalent truth for
S4-1); `--source legacy` (default) is today's behaviour (raw + runtime
correction).

Writes only under `--output-dir` (default
`~/fusionflux/bench_step4/<project>/<date>_<git>/`); never into the project.

    python scripts/benchmark_step4_baseline.py --project ~/fusion_data/test1 \\
        --run <run dir> --configs default fast dist
"""

import argparse
import datetime
import glob
import json
import os
import platform
import resource
import subprocess
import sys
import threading
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _import_block01():
    import importlib.util
    if "block01" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "block01", os.path.join(ROOT, "__init__.py"), submodule_search_locations=[ROOT])
        mod = importlib.util.module_from_spec(spec)
        sys.modules["block01"] = mod
        spec.loader.exec_module(mod)


# ── measuring ────────────────────────────────────────────────────────────

class Timers:
    def __init__(self):
        self.t = {}
        self.n = {}
        self.phase = "morphology"

    def add(self, key, dt):
        self.t[key] = self.t.get(key, 0.0) + dt
        self.n[key] = self.n.get(key, 0) + 1

    def wrap(self, owner, name, key=None, phased=False):
        """Time `owner.name`; returns what to put back (a staticmethod stays one)."""
        import inspect
        raw = inspect.getattr_static(owner, name)
        real = getattr(owner, name)
        timers = self

        def wrapped(*a, **k):
            t0 = time.perf_counter()
            try:
                return real(*a, **k)
            finally:
                label = key or name
                if phased:
                    label = f"{timers.phase}:{label}"
                timers.add(label, time.perf_counter() - t0)
        setattr(owner, name, staticmethod(wrapped) if isinstance(raw, staticmethod) else wrapped)
        return raw


class RssSampler(threading.Thread):
    def __init__(self, period=0.2):
        super().__init__(daemon=True)
        import psutil
        self.proc = psutil.Process()
        self.period = period
        self.peak = 0
        self.stop_flag = False

    def run(self):
        while not self.stop_flag:
            self.peak = max(self.peak, self.proc.memory_info().rss)
            time.sleep(self.period)


def _gpu_peak():
    try:
        import cupy
        return int(cupy.get_default_memory_pool().total_bytes())
    except Exception:  # noqa: BLE001 -- no GPU accounting
        return None


def _sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=20).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


# ── the data ─────────────────────────────────────────────────────────────

def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _workspace_of(run_dir):
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(run_dir))))


def _product_mask(run_dir):
    """The mask the product hands Step4 (`main_window._go_to_step4`):
    `global_mask.dat` when there, else `global_mask.ome.tiff`."""
    dat = os.path.join(run_dir, "global_mask.dat")
    if os.path.exists(dat):
        return dat
    return os.path.join(run_dir, "global_mask.ome.tiff")


def _newest_run(project):
    runs = sorted(glob.glob(os.path.join(project, "rois", "*", "step2", "segmentation_runs", "*",
                                         "segmentation_meta.json")),
                  key=os.path.getmtime, reverse=True)
    if not runs:
        raise SystemExit(f"no finished Step2 run under {project}")
    return os.path.dirname(runs[0])


def provenance(args, run_dir, ws, mask_path, channel_ws):
    meta = _load_json(os.path.join(run_dir, "segmentation_meta.json"))
    raw = args.slide or (meta.get("paths") or {}).get("raw_ome") or \
        _load_json(os.path.join(ws, "roi_manifest.json")).get("source_ome", "")
    st = os.stat(raw) if raw and os.path.exists(raw) else None
    corrected = os.path.join(channel_ws, "step0", "corrected_channels.zarr")
    cfg = _load_json(os.path.join(channel_ws, "step0", "correction_config.json"))
    decisions = cfg.get("channel_decisions") or {}
    return {
        "git_commit": _sh(f"git -C {ROOT} rev-parse --short HEAD"),
        "git_dirty": bool(_sh(f"git -C {ROOT} status --porcelain")),
        "project": os.path.realpath(args.project),
        "run_dir": run_dir, "run_id": meta.get("run_id") or meta.get("result_id"),
        "method": meta.get("method"), "mask_path": mask_path,
        "mask_bytes": os.path.getsize(mask_path) if os.path.exists(mask_path) else None,
        "workspace": ws, "channel_workspace": channel_ws,
        "step0_manifest": os.path.join(channel_ws, "step0", "step0_roi_result.json"),
        "slide": raw, "slide_bytes": st.st_size if st else None,
        "slide_mtime": st.st_mtime if st else None,
        "correction_decisions": {m: sum(1 for v in decisions.values() if v == m)
                                 for m in set(decisions.values())},
        "corrected_product": corrected if os.path.exists(corrected) else None,
        "machine": {"cpu": _sh("lscpu | grep 'Model name' | sed 's/.*: *//'"),
                    "cores": os.cpu_count(),
                    "ram_gb": round(__import__("psutil").virtual_memory().total / 2 ** 30, 1),
                    "gpu": _sh("nvidia-smi --query-gpu=name,memory.total --format=csv,noheader"),
                    "platform": platform.platform(),
                    "storage": _sh(f"df -h {os.path.dirname(raw) or '/'} | tail -1")},
    }


# ── one configuration ────────────────────────────────────────────────────

def run_worker(args, statistics, out_dir, source, run_dir, mask_path, channel_ws):
    """Today's worker, timed. Returns the report dict."""
    import scipy.ndimage as ndi
    from PyQt5 import QtCore
    QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])
    from block01.core import io_loader
    from block01.core.bg_correction import _load_correction_config
    from block01.workers import feature_extract_worker as few

    timers = Timers()
    restore = []
    # raw reads and runtime correction -- separate, never nested in each other
    restore.append((io_loader.OMETIFFLoader, "_read_roi_zarr",
                    timers.wrap(io_loader.OMETIFFLoader, "_read_roi_zarr", "read_raw")))
    restore.append((io_loader.OMETIFFLoader, "_apply_configured_correction",
                    timers.wrap(io_loader.OMETIFFLoader, "_apply_configured_correction",
                                "correct_runtime")))
    restore.append((few.FeatureExtractWorker, "_load_mask",
                    timers.wrap(few.FeatureExtractWorker, "_load_mask", "load_mask")))
    for fn in ("mean", "sum", "median", "standard_deviation", "minimum", "maximum",
               "labeled_comprehension", "binary_erosion"):
        restore.append((ndi, fn, timers.wrap(ndi, fn, fn, phased=True)))
    # the channel phase starts at the first channel read
    real_read_region = io_loader.OMETIFFLoader.read_region

    def read_region(self, *a, **k):
        timers.phase = "channels"
        t0 = time.perf_counter()
        try:
            return real_read_region(self, *a, **k)
        finally:
            timers.add("read_region_total", time.perf_counter() - t0)
    io_loader.OMETIFFLoader.read_region = read_region
    restore.append((io_loader.OMETIFFLoader, "read_region", real_read_region))
    import pandas as pd
    restore.append((pd.DataFrame, "to_csv", timers.wrap(pd.DataFrame, "to_csv", "write_csv")))

    cfg_path = os.path.join(channel_ws, "step0", "correction_config.json")
    correction = _load_correction_config(cfg_path)
    if source == "corrected":
        corrected = os.path.join(channel_ws, "step0", "corrected_channels.zarr")
        decisions = (correction or {}).get("channel_decisions") or {}
        wanted = {c: m for c, m in decisions.items() if m in ("tophat", "cucim")}
        if not wanted or not os.path.exists(corrected):
            raise SystemExit("reference B needs Step0's corrected_channels.zarr and at "
                             "least one tophat / cuCIM decision")
        real_loader = few.OMETIFFLoader

        def loader_with_product(path, correction_config=None, **kw):
            loader = real_loader(path, correction_config=None, **kw)
            loader.set_corrected_zarr_store(corrected, wanted)
            return loader
        few.OMETIFFLoader = loader_with_product
        restore.append((few, "OMETIFFLoader", real_loader))
        correction = None                     # never corrected at runtime here

    slide = args.slide or (_load_json(os.path.join(run_dir, "segmentation_meta.json"))
                           .get("paths") or {}).get("raw_ome") or \
        _load_json(os.path.join(_workspace_of(run_dir), "roi_manifest.json")).get("source_ome")
    worker = few.FeatureExtractWorker(mask_path=mask_path, ome_tiff_path=slide,
                                      output_dir=out_dir, statistics=statistics,
                                      correction_config=correction)
    errors = []
    worker.error.connect(errors.append)
    sampler = RssSampler()
    sampler.start()
    t0 = time.perf_counter()
    try:
        worker.run()
    finally:
        wall = time.perf_counter() - t0
        sampler.stop_flag = True
        sampler.join()
        for owner, name, real in reversed(restore):
            setattr(owner, name, real)
    ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    stats = {k: round(v, 2) for k, v in sorted(timers.t.items())}
    channel_stats = sum(v for k, v in timers.t.items() if k.startswith("channels:"))
    morph = sum(v for k, v in timers.t.items() if k.startswith("morphology:"))
    corrected_read = (timers.t.get("read_region_total", 0.0) - timers.t.get("read_raw", 0.0)
                      - timers.t.get("correct_runtime", 0.0)) if source == "corrected" else 0.0
    return {
        "statistics": statistics, "source": source, "errors": errors, "wall_s": round(wall, 1),
        "split_s": {
            "load_mask": round(timers.t.get("load_mask", 0.0), 1),
            "morphology_ndimage": round(morph, 1),
            "read_raw": round(timers.t.get("read_raw", 0.0), 1),
            "correct_runtime": round(timers.t.get("correct_runtime", 0.0), 1),
            "read_corrected_product": round(max(0.0, corrected_read), 1),
            "channel_statistics": round(channel_stats, 1),
            "write_csv": round(timers.t.get("write_csv", 0.0), 1),
        },
        "calls_s": stats, "calls_n": dict(sorted(timers.n.items())),
        "peak_rss_gb_sampled": round(sampler.peak / 2 ** 30, 2),
        "peak_rss_gb_ru_maxrss": round(ru / 2 ** 30, 2),
        "peak_gpu_bytes": _gpu_peak(),
        "outputs": sorted(os.listdir(out_dir)) if os.path.isdir(out_dir) else [],
    }


def run_distribution(args, run_dir, mask_path, channels):
    """median + p90 on a few channels, exactly the worker's calls."""
    import scipy.ndimage as ndi
    from block01.core.io_loader import OMETIFFLoader
    from block01.workers.feature_extract_worker import FeatureExtractWorker
    import tifffile
    slide = args.slide or (_load_json(os.path.join(run_dir, "segmentation_meta.json"))
                           .get("paths") or {}).get("raw_ome")
    with tifffile.TiffFile(slide) as tf:
        full_h, full_w = tf.pages[0].imagelength, tf.pages[0].imagewidth
    mask = FeatureExtractWorker._load_mask(mask_path, full_h, full_w)
    labels = np.arange(1, int(mask.max()) + 1)
    loader = OMETIFFLoader(slide)
    h, w = mask.shape
    out = {}
    sampler = RssSampler()
    sampler.start()
    for ch in channels:
        t0 = time.perf_counter()
        data = loader.read_region(ch, 0, h, 0, w, downsample=1, normalize=False)
        t_read = time.perf_counter() - t0
        t0 = time.perf_counter()
        ndi.median(data, mask, labels)
        t_med = time.perf_counter() - t0
        t0 = time.perf_counter()
        ndi.labeled_comprehension(data, mask, labels,
                                  lambda v: float(np.percentile(v, 90)), float, default=0.0)
        t_p90 = time.perf_counter() - t0
        out[ch] = {"read_s": round(t_read, 1), "median_s": round(t_med, 1),
                   "p90_s": round(t_p90, 1)}
        del data
    sampler.stop_flag = True
    sampler.join()
    return {"channels": out, "peak_rss_gb_sampled": round(sampler.peak / 2 ** 30, 2),
            "note": "measured on these channels only; any all-channel figure is a projection"}


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--project", required=True)
    p.add_argument("--run", help="a Step2 run folder (default: the newest in the project)")
    p.add_argument("--slide", help="the raw OME (default: the run's own record)")
    p.add_argument("--channel-workspace",
                   help="the ROI workspace whose Step0 correction / corrected product the "
                        "channels come from (default: the run's own)")
    p.add_argument("--configs", nargs="+", default=["default", "fast", "dist"],
                   choices=["default", "fast", "dist"])
    p.add_argument("--source", default="legacy", choices=["legacy", "corrected"])
    p.add_argument("--dist-channels", nargs="+", default=None)
    p.add_argument("--output-dir")
    args = p.parse_args()
    _import_block01()
    args.project = os.path.expanduser(args.project)
    run_dir = os.path.realpath(os.path.expanduser(args.run)) if args.run else _newest_run(args.project)
    ws = _workspace_of(run_dir)
    channel_ws = os.path.realpath(os.path.expanduser(args.channel_workspace)) \
        if args.channel_workspace else ws
    mask_path = _product_mask(run_dir)
    sha = _sh(f"git -C {ROOT} rev-parse --short HEAD") or "nogit"
    out_root = os.path.expanduser(args.output_dir) if args.output_dir else os.path.join(
        os.path.expanduser("~/fusionflux/bench_step4"), os.path.basename(args.project.rstrip("/")),
        f"{datetime.date.today().isoformat()}_{sha}")
    if os.path.realpath(out_root).startswith(os.path.realpath(args.project)):
        raise SystemExit("the output directory must not be inside the project")
    os.makedirs(out_root, exist_ok=True)
    report = {"provenance": provenance(args, run_dir, ws, mask_path, channel_ws), "results": {}}
    for config in args.configs:
        tag = f"{config}_{args.source}"
        print(f"[bench] {tag} …", flush=True)
        if config == "dist":
            chans = args.dist_channels
            if not chans:
                from block01.core.io_loader import OMETIFFLoader
                names = OMETIFFLoader(report["provenance"]["slide"]).channel_names()
                chans = names[:2]
            report["results"][tag] = run_distribution(args, run_dir, mask_path, chans)
        else:
            stats = ["mean"] if config == "default" else ["mean", "sum", "std", "min", "max"]
            out_dir = os.path.join(out_root, tag)
            os.makedirs(out_dir, exist_ok=True)
            report["results"][tag] = run_worker(args, stats, out_dir, args.source, run_dir,
                                                mask_path, channel_ws)
        print(json.dumps(report["results"][tag], indent=1)[:2000], flush=True)
        with open(os.path.join(out_root, f"report_{args.source}.json"), "w",
                  encoding="utf-8") as f:
            json.dump(report, f, indent=1)
    print(f"[bench] written: {out_root}")


if __name__ == "__main__":
    main()
