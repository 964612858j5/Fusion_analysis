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
import json
import os
import subprocess
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
from block01.viewer.multichannel_prefetch import (  # noqa: E402
    HOT_INFLIGHT, MultiChannelPrefetchController)
from block01.viewer.prefetch_policy import ChannelCorrectionSpec  # noqa: E402
from block01.viewer.raw_tile_provider import RawTileProvider  # noqa: E402
from block01.viewer.scheduler import (  # noqa: E402
    DEFAULT_COMPUTE_WORKERS, DEFAULT_IO_WORKERS, TileScheduler)
from block01.viewer.tile_types import TileGridSpec  # noqa: E402

DEFAULT_PATH = ("/sda1/Fusion/benchmark/tonsil/"
                "2025.12.21_Final_28127_22_Slice2_Tonsil.ome.tif")
GATE_PP = 1.0                 # non-inferiority margin, percentage points
ZOOM_STEPS = 110              # >= 100 frames per rep (see run_arm)
ZOOM_STEP_FACTOR = 0.985      # 0.985**110 ~ 0.19, i.e. the same ~5x span
PAIRS = 15                    # locked in advance; not extended on results
# Identical for BOTH arms. Chosen from a pilot: HOT holds exactly
# HOT_INFLIGHT in flight from ~110ms to ~617ms after the foreground settles.
PRE_GESTURE_IDLE_S = 0.35
HOT_DRAIN_TIMEOUT_S = 20.0
T95_ONE_SIDED = {             # Student t, one-sided 95%, by degrees of freedom
    4: 2.132, 5: 2.015, 6: 1.943, 7: 1.895, 8: 1.860, 9: 1.833,
    10: 1.812, 11: 1.796, 12: 1.782, 13: 1.771, 14: 1.761, 15: 1.753,
    16: 1.746, 17: 1.740, 18: 1.734, 19: 1.729, 20: 1.725, 24: 1.711,
    29: 1.699,
}
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

    # THE POINT OF THIS GATE IS CONTENTION. Starting the gesture while HOT
    # is idle measures nothing: an earlier run had `conf=1` in every arm,
    # meaning HOT only ever confirmed AFTER the gesture, in the idle phase,
    # so the 120 tiles it completed proved only that it runs when the user
    # has stopped -- never that a user who starts moving WHILE it works is
    # unaffected. So wait here until HOT has confirmed once AND has work
    # actually in flight, then move the camera and force it to cancel.
    # BOTH arms idle for the SAME fixed window before the gesture, and the
    # HOT arm must be at FULL in-flight capacity at the instant the camera
    # moves. Two earlier versions of this got it wrong:
    #
    #   - waiting only in the HOT arm gave it extra settling time the
    #     baseline never got, and produced a +1.45pp result in the feature's
    #     favour that was entirely the unequal idle;
    #   - then a 3s equal window let HOT finish and go idle long before the
    #     gesture. "Was active at some point during the idle" is not
    #     contention. The tell was in the log: 216 tiles per drag arm, i.e.
    #     one whole batch before the gesture and another after, not a batch
    #     interrupted at a cap of 2.
    #
    # Pilot measurement (scratch, informal): after the foreground settles,
    # HOT holds exactly HOT_INFLIGHT in flight from about 110ms to about
    # 617ms, then drains to zero by about 1185ms. PRE_GESTURE_IDLE_S sits in
    # the middle of that window. An arm that is not at full capacity at the
    # gesture is INVALID -- it is not evidence about contention, so it is
    # not allowed to contribute.
    active_seen_during_idle = hot_ctl is None
    deadline = time.perf_counter() + PRE_GESTURE_IDLE_S
    while time.perf_counter() < deadline:
        app.processEvents()
        time.sleep(0.002)
        if hot_ctl is not None and len(hot_ctl._active_requests) > 0:
            active_seen_during_idle = True

    # Read the REAL state one line before the camera moves.
    if hot_ctl is None:
        active_at_gesture = None
        at_gesture_note = ""
    else:
        st = hot_ctl.stats
        active_at_gesture = len(hot_ctl._active_requests)
        at_gesture_note = (f"at_gesture: active={active_at_gesture} "
                           f"req={st['hot_tiles_requested']} "
                           f"done={st['hot_tiles_completed']} | ")
        req_gt_done = st["hot_tiles_requested"] > st["hot_tiles_completed"]

    covs, mixed, frames = [], 0, 0
    # Zoom is sampled finely: at 12 coarse steps the mixed-level fraction can
    # only take values in steps of 8.3pp, so it structurally cannot resolve a
    # 1pp gate. A continuous trajectory of ZOOM_STEPS small steps covers the
    # same span. Each REP still contributes ONE sample to the statistics --
    # 110 frames are not 110 independent repeats.
    steps = 25 if kind == "drag" else ZOOM_STEPS
    for _ in range(steps):
        rng = view.view_box.viewRange()
        if kind == "drag":
            dx = span * 0.08
            view.view_box.setRange(xRange=(rng[0][0] + dx, rng[0][1] + dx),
                                   yRange=tuple(rng[1]), padding=0)
        else:
            f = ZOOM_STEP_FACTOR
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

    # Second confirmation and a drained queue: the gesture cancelled the
    # first batch, so a valid arm must show HOT recovering, running again and
    # finishing. This wait comes AFTER the equal-length idle above, so it
    # cannot bias any measured value -- coverage was sampled during the
    # gesture and convergence inside that equal window; this is validity
    # bookkeeping only, and it is why it may run longer in the HOT arm.
    #
    # `drained` is now a PHYSICAL guarantee, not a local one: a slot is
    # released only when a callback arrives, and an abandoned task delivers a
    # terminal one (`notify_on_stale_completion`). An empty
    # `_active_requests` therefore means every task HOT started has actually
    # finished -- which an earlier version could not claim, since it released
    # abandoned slots without knowing whether the work had stopped.
    if hot_ctl is not None:
        deadline = time.perf_counter() + HOT_DRAIN_TIMEOUT_S
        while time.perf_counter() < deadline:
            app.processEvents()
            time.sleep(0.005)
            if (hot_ctl.stats["settle_confirmations"] >= 2
                    and not hot_ctl._active_requests
                    and not hot_ctl._tile_queue):
                break

    stats = dict(hot_ctl.stats) if hot_ctl is not None else None
    if stats is None:
        valid = True
        note = "HOT off"
    else:
        drained = (len(hot_ctl._active_requests) == 0
                   and len(hot_ctl._tile_queue) == 0)
        valid = (active_at_gesture == HOT_INFLIGHT
                 and req_gt_done
                 and stats["settle_confirmations"] >= 2
                 and stats["hot_tiles_failed"] == 0
                 and stats["overviews_failed"] == 0
                 and drained)
        note = (f"{at_gesture_note}"
                f"seen_idle={active_seen_during_idle} "
                f"conf={stats['settle_confirmations']} "
                f"req={stats['hot_tiles_requested']} "
                f"done={stats['hot_tiles_completed']} fail={stats['hot_tiles_failed']} "
                f"ovw={stats['overviews_requested']}/{stats['overviews_failed']} "
                f"abort={stats['settle_aborted']} "
                f"abandoned_done={stats['hot_abandoned_finished']} "
                f"drained={drained} "
                f"{'VALID' if valid else 'INVALID'}")
    print(f"[{label}] {kind:4s} cov={np.mean(covs) * 100:5.1f}%  "
          f"mixed={mixed / max(1, frames) * 100:5.1f}%  "
          f"converge={converged_ms:6.1f}ms  | {note}", flush=True)

    # Full teardown, in order. Closing only the scheduler and provider left
    # the controller's timers and signal connections alive, so a previous
    # arm kept running inside the NEXT arm's processEvents() -- contaminating
    # its measurement and touching an already-closed provider.
    # `ExploreController.teardown()` shuts the scheduler and provider down
    # itself, so they are not closed again here.
    if hot_ctl is not None:
        hot_ctl.stop()
    ctrl.teardown()
    view.close()
    view.deleteLater()
    app.processEvents()
    return {"cov": np.mean(covs) * 100,
            "mixed": mixed / max(1, frames) * 100,
            "converge": converged_ms,
            "valid": bool(valid)}


def _t95(df):
    """One-sided 95% Student t. Falls back to the nearest tabulated df."""
    if df in T95_ONE_SIDED:
        return T95_ONE_SIDED[df]
    keys = sorted(T95_ONE_SIDED)
    if df < keys[0]:
        return T95_ONE_SIDED[keys[0]]
    return T95_ONE_SIDED[min(keys, key=lambda k: abs(k - df))]


def paired_noninferiority(name, base, hot, higher_is_better, unit="pp",
                          gated=True):
    """Paired non-inferiority test on the per-pair differences.

    The earlier rule compared each arm's raw SPREAD against the margin,
    which is not a test: spread grows with the number of repeats, so more
    data made a pass strictly less likely. What must shrink with n is the
    uncertainty of the MEAN paired difference, which is what this measures.
    The margin itself is unchanged at -GATE_PP; nothing is relaxed.

        d_i  = HOT_i - BASE_i        (higher-is-better metrics)
        d_i  = BASE_i - HOT_i        (lower-is-better metrics)

    so a positive d is always "HOT is better". With the one-sided 95%
    interval on mean(d):
        lower bound >= -margin  -> PASS  (HOT is non-inferior)
        upper bound <  -margin  -> FAIL  (HOT is worse by more than the margin)
        otherwise               -> INCONCLUSIVE
    """
    b = np.asarray(base, dtype=float)
    h = np.asarray(hot, dtype=float)
    # No dropping. A gate that discards failed arms and passes on what is
    # left is not a gate: the arms most likely to fail are the ones under
    # the most contention, which is exactly what is being measured. Any
    # incomplete or invalid pair makes the whole run INCONCLUSIVE.
    if b.size != PAIRS or h.size != PAIRS:
        return (f"{name}: {min(b.size, h.size)} of {PAIRS} pairs present "
                f"-> INCONCLUSIVE")
    bad = int(np.count_nonzero(np.isnan(b) | np.isnan(h)))
    if bad:
        return (f"{name}: {bad} of {PAIRS} pairs unusable "
                f"-> INCONCLUSIVE (no pairs are dropped)")

    d = (h - b) if higher_is_better else (b - h)
    n = d.size
    mean = float(d.mean())
    sd = float(d.std(ddof=1))
    # `lo`/`hi` are the two ONE-SIDED 95% bounds, computed with the
    # one-sided t. They are deliberately not a two-sided 95% interval, and
    # are labelled as bounds rather than as an interval so the distinction
    # is not lost in the output.
    half = _t95(n - 1) * sd / np.sqrt(n) if sd > 0 else 0.0
    lo, hi = mean - half, mean + half
    spread_note = (f"BASE {b.mean():6.2f} (range {b.max() - b.min():5.2f}) | "
                   f"HOT {h.mean():6.2f} (range {h.max() - h.min():5.2f})")
    if not gated:
        return (f"{name}: {spread_note} | paired mean {mean:+6.2f} {unit} "
                f"one-sided 95% bounds lower {lo:+.2f} / upper {hi:+.2f} "
                f"-> informational, not gated")
    if lo >= -GATE_PP:
        tag = "PASS (non-inferior)"
    elif hi < -GATE_PP:
        tag = "FAIL (worse by more than the margin)"
    else:
        tag = "INCONCLUSIVE"
    return (f"{name}: {spread_note} | paired mean {mean:+6.2f} {unit}, "
            f"one-sided 95% bounds lower {lo:+.2f} / upper {hi:+.2f}, "
            f"margin {-GATE_PP:+.1f} "
            f"-> {tag}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--path", default=DEFAULT_PATH)
    ap.add_argument("--channel-index", type=int, default=1)
    ap.add_argument("--param", type=int, default=25)
    ap.add_argument("--reps", type=int, default=PAIRS,
                    help="paired repetitions, locked in advance")
    ap.add_argument("--center-y", type=int, default=25606)
    ap.add_argument("--center-x", type=int, default=15360)
    ap.add_argument("--single", nargs=2, metavar=("KIND", "HOT"),
                    help="internal: run ONE arm in this process and print a "
                         "JSON line. Each arm runs in its own subprocess -- "
                         "60 arms in one process hit the known cupy/Qt "
                         "teardown segfault (the same reason the test suites "
                         "must be run one file per process), and separate "
                         "processes also isolate the arms more completely "
                         "than any in-process teardown can.")
    ap.add_argument("--io-workers", type=int, default=DEFAULT_IO_WORKERS)
    ap.add_argument("--compute-workers", type=int, default=DEFAULT_COMPUTE_WORKERS)
    args = ap.parse_args()

    if args.single:
        kind, hot_s = args.single
        hot = hot_s == "1"
        app = QtWidgets.QApplication(sys.argv)
        label = f"{'HOT ' if hot else 'BASE'}"
        result = run_arm(app, args, kind, hot, label)
        print("RESULT " + json.dumps(result), flush=True)
        return

    def one(kind, hot, rep):
        cmd = [sys.executable, os.path.abspath(__file__),
               "--single", kind, "1" if hot else "0",
               "--path", args.path,
               "--channel-index", str(args.channel_index),
               "--param", str(args.param),
               "--center-y", str(args.center_y),
               "--center-x", str(args.center_x),
               "--io-workers", str(args.io_workers),
               "--compute-workers", str(args.compute_workers)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        payload = None
        for line in proc.stdout.splitlines():  # noqa: E501
            if line.startswith("["):
                print(f"  r{rep} {line}", flush=True)
            if line.startswith("RESULT "):
                payload = json.loads(line[len("RESULT "):])
        if payload is None:
            print(f"  r{rep} {'HOT ' if hot else 'BASE'} {kind}: ARM FAILED "
                  f"(rc={proc.returncode})", flush=True)
            print("    " + (proc.stderr.strip().splitlines() or ["<no stderr>"])[-1],
                  flush=True)
        return payload

    out = {(k, h): [] for k in ("drag", "zoom") for h in (False, True)}
    for rep in range(args.reps):
        # Counterbalanced: always running BASE first would hand HOT a warmer
        # OS page cache in every pair, a systematic advantage rather than a
        # measurement.
        order = (False, True) if rep % 2 == 0 else (True, False)
        for kind in ("drag", "zoom"):
            for hot in order:
                res = one(kind, hot, rep)
                if res is None or not res.get("valid", False):
                    if res is not None:
                        print(f"  r{rep} arm INVALID (HOT was not genuinely "
                              f"working before the gesture, or it failed) -- "
                              f"the run will be INCONCLUSIVE", flush=True)
                    res = {"cov": float("nan"), "mixed": float("nan"),
                           "converge": float("nan"), "valid": False}
                out[(kind, hot)].append(res)

    print()
    print(paired_noninferiority(
        "drag in-motion coverage %",
        [r["cov"] for r in out[("drag", False)]],
        [r["cov"] for r in out[("drag", True)]], higher_is_better=True))
    print(paired_noninferiority(
        "zoom mixed-level frames %",
        [r["mixed"] for r in out[("zoom", False)]],
        [r["mixed"] for r in out[("zoom", True)]], higher_is_better=False))
    print(paired_noninferiority(
        "zoom converge-after-stop ms",
        [r["converge"] for r in out[("zoom", False)]],
        [r["converge"] for r in out[("zoom", True)]],
        higher_is_better=False, unit="ms", gated=False))


if __name__ == "__main__":
    main()
