"""Paired A/B gate for the HOT multi-channel prefetch controller.

Baseline and HOT alternate on a FIXED trajectory, five pairs, on real data.
Two metrics, chosen because they measure different failure modes:

  drag  -- in-motion coverage: the fraction of the viewport already showing
           current-level corrected tiles, sampled at every step. This is the
           metric the foreground work was tuned against.

  zoom  -- mixed-level frames and post-gesture convergence, NOT coverage.
           Zoom coverage has measured 8-24 percentage points of run-to-run
           spread throughout this project, which makes a 1pp gate
           undecidable on it. What the user actually sees during a zoom is
           whether the viewport shows more than one pyramid level at once
           (visible block boundaries) and how long after letting go it takes
           to become a single sharp level.

The gate is 1 percentage point of degradation. If an ARM'S OWN spread
exceeds that, the result is reported INCONCLUSIVE -- the bar is not relaxed
after the fact.

Every HOT arm prints the controller's own counters. A run where HOT never
reached SETTLED must not be mistaken for a passing gate; an earlier
measurement of exactly that kind was invalid and had to be discarded.
"""
import argparse
import sys
import time
import importlib.util
import pathlib

import numpy as np


def _register_block01_alias():
    root = pathlib.Path(__file__).resolve().parent.parent
    if "block01" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "block01", root / "__init__.py", submodule_search_locations=[str(root)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["block01"] = mod
    spec.loader.exec_module(mod)
    sys.path.insert(0, str(root.parent))


_register_block01_alias()

import pyqtgraph as pg  # noqa: E402
from PyQt5 import QtWidgets  # noqa: E402

pg.setConfigOptions(imageAxisOrder="row-major")

from block01.viewer.caches import LRUByteCache  # noqa: E402
from block01.viewer.correction_compute import CorrectionCompute  # noqa: E402
from block01.viewer.explore_view import ExploreController, ExploreView  # noqa: E402
from block01.viewer.multichannel_prefetch import MultiChannelPrefetchController  # noqa: E402
from block01.viewer.prefetch_policy import ChannelCorrectionSpec  # noqa: E402
from block01.viewer.raw_tile_provider import RawTileProvider  # noqa: E402
from block01.viewer.scheduler import (  # noqa: E402
    DEFAULT_COMPUTE_WORKERS, DEFAULT_IO_WORKERS, TileScheduler)
from block01.viewer.tile_types import TileGridSpec  # noqa: E402

DEFAULT_PATH = ("/sda1/Fusion/benchmark/tonsil/"
                "2025.12.21_Final_28127_22_Slice2_Tonsil.ome.tif")
GATE_PP = 1.0
IDLE_AFTER_GESTURE_S = 2.0   # 80ms gesture_quiet + 120ms confirm, then work


def build(app, args, hot):
    provider = RawTileProvider(args.path)
    channel = provider.channel_names[args.channel_index]
    grid = TileGridSpec(tile_size=512, source_chunk_shape=(), grid_version="v1")
    raw_cache = LRUByteCache(1024 * 1024 * 1024)
    corr_cache = LRUByteCache(4096 * 1024 * 1024)
    compute = CorrectionCompute(provider, raw_cache)
    scheduler = TileScheduler(provider, compute, raw_cache, corr_cache,
                              io_workers=args.io_workers,
                              compute_workers=args.compute_workers)
    view = ExploreView()
    view.resize(1400, 1000)
    view.show()
    ctrl = ExploreController(provider, scheduler, compute, grid, view, channel)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(args.param,))
    deadline = time.perf_counter() + 8.0
    while time.perf_counter() < deadline and not ctrl._floor_ready:
        app.processEvents()
        time.sleep(0.005)

    hot_ctl = None
    if hot:
        specs = [ChannelCorrectionSpec(channel=name,
                                       tophat_radius=args.param,
                                       cucim_sigma=args.param)
                 for name in provider.channel_names]
        hot_ctl = MultiChannelPrefetchController(ctrl, scheduler, specs, grid)
    return provider, scheduler, view, ctrl, hot_ctl


def coverage(ctrl):
    visible = ctrl._visible_tiles
    if not visible:
        return 1.0
    ctx = ctrl.selection_key_context()
    ok = 0
    for tx, ty in visible:
        entry = ctrl._precise_pool.get(ctrl.level, tx, ty)
        if entry is not None and entry.key is not None \
                and ctrl._key_matches_context(entry.key, ctx):
            ok += 1
    return ok / len(visible)


def levels_on_screen(ctrl):
    """Distinct pyramid levels topmost-visible across the viewport. More than
    one means visible block boundaries."""
    ts = ctrl.grid.tile_size
    ds_y, ds_x = ctrl._downsample_yx(ctrl.level)
    seen = set()
    for tx, ty in ctrl._visible_tiles:
        wy = (ty * ts + ts / 2) * ds_y
        wx = (tx * ts + ts / 2) * ds_x
        best = None
        for entry in ctrl._precise_pool.entries.values():
            if not entry.item.isVisible() or not entry.rect.contains(wx, wy):
                continue
            if best is None or entry.level < best:
                best = entry.level
        seen.add(best if best is not None else 99)
    return seen


def run_arm(app, args, kind, hot, label):
    provider, scheduler, view, ctrl, hot_ctl = build(app, args, hot)
    cy, cx = args.center_y, args.center_x
    span = 1400.0 if kind == "drag" else 6000.0
    view.view_box.setRange(xRange=(cx - span / 2, cx + span / 2),
                           yRange=(cy - span / 2, cy + span / 2), padding=0)
    deadline = time.perf_counter() + 8.0
    while time.perf_counter() < deadline and coverage(ctrl) < 1.0:
        app.processEvents()
        time.sleep(0.002)

    covs, mixed, frames = [], 0, 0
    steps = 25 if kind == "drag" else 12
    for _ in range(steps):
        rng = view.view_box.viewRange()
        if kind == "drag":
            dx = span * 0.08
            view.view_box.setRange(xRange=(rng[0][0] + dx, rng[0][1] + dx),
                                   yRange=tuple(rng[1]), padding=0)
        else:
            f = 0.82
            w = (rng[0][1] - rng[0][0]) * f
            h = (rng[1][1] - rng[1][0]) * f
            mx = (rng[0][0] + rng[0][1]) / 2
            my = (rng[1][0] + rng[1][1]) / 2
            view.view_box.setRange(xRange=(mx - w / 2, mx + w / 2),
                                   yRange=(my - h / 2, my + h / 2), padding=0)
        t = time.perf_counter()
        while time.perf_counter() - t < 0.016:
            app.processEvents()
        covs.append(coverage(ctrl))
        frames += 1
        if len(levels_on_screen(ctrl)) > 1:
            mixed += 1

    # Post-gesture: how long until a single sharp level, and does HOT run?
    t_stop = time.perf_counter()
    converged_ms = float("nan")
    while time.perf_counter() - t_stop < IDLE_AFTER_GESTURE_S:
        app.processEvents()
        time.sleep(0.005)
        if np.isnan(converged_ms) and coverage(ctrl) >= 1.0 \
                and len(levels_on_screen(ctrl)) == 1:
            converged_ms = (time.perf_counter() - t_stop) * 1000.0

    stats = dict(hot_ctl.stats) if hot_ctl is not None else None
    note = ("HOT off" if stats is None else
            f"HOT conf={stats['settle_confirmations']} req={stats['hot_tiles_requested']} "
            f"done={stats['hot_tiles_completed']} fail={stats['hot_tiles_failed']} "
            f"ovw={stats['overviews_requested']}/{stats['overviews_failed']} "
            f"abort={stats['settle_aborted']}")
    print(f"[{label}] {kind:4s} cov={np.mean(covs) * 100:5.1f}%  "
          f"mixed={mixed / max(1, frames) * 100:5.1f}%  "
          f"converge={converged_ms:6.1f}ms  | {note}", flush=True)

    if hot_ctl is not None:
        hot_ctl.stop()
    scheduler.shutdown()
    provider.close()
    return {"cov": np.mean(covs) * 100,
            "mixed": mixed / max(1, frames) * 100,
            "converge": converged_ms}


def verdict(name, base, hot, lower_is_better, gated=True, unit="pp"):
    """`gated=False` reports the numbers without a pass/fail. The 1pp gate
    is defined in PERCENTAGE POINTS; applying it to a millisecond metric
    would be a category error, so timing metrics are informational."""
    b = np.array(base, dtype=float)
    h = np.array(hot, dtype=float)
    b, h = b[~np.isnan(b)], h[~np.isnan(h)]
    if b.size == 0 or h.size == 0:
        return f"{name}: no usable samples -> INCONCLUSIVE"
    delta = h.mean() - b.mean()
    if lower_is_better:
        delta = -delta
    spread = max(b.max() - b.min(), h.max() - h.min())
    if not gated:
        return (f"{name}: BASE {b.mean():6.2f} (spread {b.max() - b.min():5.2f}) | "
                f"HOT {h.mean():6.2f} (spread {h.max() - h.min():5.2f}) | "
                f"delta {delta:+6.2f} {unit} -> informational, not gated")
    if spread > GATE_PP:
        tag = f"INCONCLUSIVE (arm spread {spread:.2f} > {GATE_PP}pp)"
    else:
        tag = "PASS" if delta >= -GATE_PP else "FAIL"
    return (f"{name}: BASE {b.mean():6.2f} (spread {b.max() - b.min():5.2f}) | "
            f"HOT {h.mean():6.2f} (spread {h.max() - h.min():5.2f}) | "
            f"delta {delta:+6.2f} -> {tag}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--path", default=DEFAULT_PATH)
    ap.add_argument("--channel-index", type=int, default=1)
    ap.add_argument("--param", type=int, default=25)
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--center-y", type=int, default=25606)
    ap.add_argument("--center-x", type=int, default=15360)
    ap.add_argument("--io-workers", type=int, default=DEFAULT_IO_WORKERS)
    ap.add_argument("--compute-workers", type=int, default=DEFAULT_COMPUTE_WORKERS)
    args = ap.parse_args()

    app = QtWidgets.QApplication(sys.argv)
    out = {(k, h): [] for k in ("drag", "zoom") for h in (False, True)}
    for rep in range(args.reps):
        for kind in ("drag", "zoom"):
            for hot in (False, True):
                label = f"{'HOT ' if hot else 'BASE'} r{rep}"
                out[(kind, hot)].append(run_arm(app, args, kind, hot, label))

    print()
    print(verdict("drag in-motion coverage %",
                  [r["cov"] for r in out[("drag", False)]],
                  [r["cov"] for r in out[("drag", True)]], lower_is_better=False))
    print(verdict("zoom mixed-level frames %",
                  [r["mixed"] for r in out[("zoom", False)]],
                  [r["mixed"] for r in out[("zoom", True)]], lower_is_better=True))
    print(verdict("zoom converge-after-stop ms",
                  [r["converge"] for r in out[("zoom", False)]],
                  [r["converge"] for r in out[("zoom", True)]],
                  lower_is_better=True, gated=False, unit="ms"))


if __name__ == "__main__":
    main()
