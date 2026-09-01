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
from block01.viewer.experimental.coverage_prefetch import (  # noqa: E402
    COVERAGE_INFLIGHT, CoverageMultiChannelPrefetchController)
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
# COVERAGE only ever runs when HOT is idle (strict priority), so "HOT at its
# cap" and "COVERAGE at its cap" can never hold at the same instant -- they
# are mutually exclusive BY DESIGN, not by timing. One arm therefore cannot
# verify both, and "COVERAGE was at its cap at some point" is the same
# ever-active weakening that was rejected for HOT. So COVERAGE gets its OWN
# arm, gated on COVERAGE being at its cap at the gesture instant, paired
# against a BASE arm that waits exactly as long. Pilot: COVERAGE holds its
# cap continuously from about 871ms (once HOT has drained) out past 6s.
PRE_GESTURE_IDLE_COVERAGE_S = 1.5
MODE_BASE_SHORT, MODE_HOT, MODE_BASE_LONG, MODE_COVERAGE = "0", "1", "2", "3"
MODE_LABELS = {MODE_BASE_SHORT: "BASE", MODE_HOT: "HOT ",
               MODE_BASE_LONG: "BASE-long", MODE_COVERAGE: "COVERAGE"}
MODE_IDLE_S = {MODE_BASE_SHORT: PRE_GESTURE_IDLE_S,
               MODE_HOT: PRE_GESTURE_IDLE_S,
               MODE_BASE_LONG: PRE_GESTURE_IDLE_COVERAGE_S,
               MODE_COVERAGE: PRE_GESTURE_IDLE_COVERAGE_S}
HOT_DRAIN_TIMEOUT_S = 20.0
T95_ONE_SIDED = {             # Student t, one-sided 95%, by degrees of freedom
    4: 2.132, 5: 2.015, 6: 1.943, 7: 1.895, 8: 1.860, 9: 1.833,
    10: 1.812, 11: 1.796, 12: 1.782, 13: 1.771, 14: 1.761, 15: 1.753,
    16: 1.746, 17: 1.740, 18: 1.734, 19: 1.729, 20: 1.725, 24: 1.711,
    29: 1.699,
}
IDLE_AFTER_GESTURE_S = 2.0   # 80ms gesture_quiet + 120ms confirm, then work


def build(app, args, hot, coverage_prefetch=False):
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
        # The COVERAGE arm instantiates the EXPERIMENTAL subclass; the
        # BASE/HOT arms use the production controller, which has no
        # COVERAGE at all. That keeps those arms exactly as measured before
        # COVERAGE existed -- now by construction rather than by a flag.
        cls = (CoverageMultiChannelPrefetchController if coverage_prefetch
               else MultiChannelPrefetchController)
        hot_ctl = cls(ctrl, scheduler, specs, grid)
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


def _coverage_drained(hot_ctl):
    """True only when COVERAGE has genuinely finished, by all four measures.

    Empty queues alone are not enough, and neither is batch accounting:

    * `_coverage_active_requests` -- physically in flight. A slot is released
      only by a callback (ordinary or the opt-in terminal stale one), so this
      is real concurrency, not a generation count.
    * `_coverage_queue` -- issued-but-not-yet-requested tiles.
    * `_coverage_batch_remaining == 0` -- a request the scheduler never
      accepted delivers no callback, so a batch can sit above zero with an
      empty queue and nothing in flight: a stuck plan that looks exactly
      like a drained one from the outside.
    * the whole plan consumed -- `_coverage_order_position` has reached the
      end of `_coverage_full_order`. Between two batches every one of the
      three conditions above holds momentarily while channels are still
      waiting to be planned, so without this an arm could claim COVERAGE
      finished after the first batch of four channels out of fifty-seven.
    """
    return (not hot_ctl._coverage_active_requests
            and not hot_ctl._coverage_queue
            and hot_ctl._coverage_batch_remaining == 0
            and (hot_ctl._coverage_order_position
                 >= len(hot_ctl._coverage_full_order)))


def run_arm(app, args, kind, hot, label, coverage_prefetch=False,
            idle_s=PRE_GESTURE_IDLE_S, require_coverage_at_gesture=False):
    provider, scheduler, view, ctrl, hot_ctl = build(
        app, args, hot, coverage_prefetch=coverage_prefetch)
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
    deadline = time.perf_counter() + idle_s
    while time.perf_counter() < deadline:
        app.processEvents()
        time.sleep(0.002)
        if hot_ctl is not None and len(hot_ctl._active_requests) > 0:
            active_seen_during_idle = True

    coverage_at_gesture = (None if hot_ctl is None or not coverage_prefetch
                           else len(hot_ctl._coverage_active_requests))

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
        if coverage_prefetch:
            # Informational only here (COVERAGE's own gate is
            # `cov_at_gesture`, checked on the line before the camera moves
            # above for why COVERAGE is essentially always 0 at this exact
            # instant when HOT is genuinely still busy, by design).
            coverage_active_at_gesture = len(hot_ctl._coverage_active_requests)
            at_gesture_note += (
                f"cov_active={coverage_active_at_gesture} "
                f"cov_req={st['coverage_tiles_requested']} "
                f"cov_done={st['coverage_tiles_completed']} | ")

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
            drained_now = (hot_ctl.stats["settle_confirmations"] >= 2
                           and not hot_ctl._active_requests
                           and not hot_ctl._tile_queue)
            if coverage_prefetch:
                drained_now = drained_now and _coverage_drained(hot_ctl)
            if drained_now:
                break

    stats = dict(hot_ctl.stats) if hot_ctl is not None else None
    if stats is None:
        valid = True
        note = "HOT off"
    else:
        drained = (len(hot_ctl._active_requests) == 0
                   and len(hot_ctl._tile_queue) == 0)
        if coverage_prefetch:
            drained = drained and _coverage_drained(hot_ctl)
        # In the COVERAGE arm the requirement moves: COVERAGE must be at ITS
        # cap at the gesture instant, and HOT is idle then by construction.
        if require_coverage_at_gesture:
            gesture_ok = (coverage_at_gesture == COVERAGE_INFLIGHT
                          and active_at_gesture == 0)
        else:
            gesture_ok = (active_at_gesture == HOT_INFLIGHT and req_gt_done)
        valid = (gesture_ok
                 and stats["settle_confirmations"] >= 2
                 and stats["hot_tiles_failed"] == 0
                 and stats["overviews_failed"] == 0
                 and drained)
        if coverage_prefetch:
            # See PRE_GESTURE_IDLE_COVERAGE_S for why COVERAGE has its own arm
            # for why this is "reached cap at some point in the run", not
            # "at the same instant as HOT's own gesture check" -- the two
            # are mutually exclusive under strict HOT priority.
            valid = (valid
                     and stats["coverage_batches"] > 0
                     and stats["coverage_tiles_requested"] > 0
                     and stats["coverage_tiles_failed"] == 0)
        note = (f"{at_gesture_note}"
                f"seen_idle={active_seen_during_idle} "
                f"conf={stats['settle_confirmations']} "
                f"req={stats['hot_tiles_requested']} "
                f"done={stats['hot_tiles_completed']} fail={stats['hot_tiles_failed']} "
                f"ovw={stats['overviews_requested']}/{stats['overviews_failed']} "
                f"abort={stats['settle_aborted']} "
                f"abandoned_done={stats['hot_abandoned_finished']} "
                f"drained={drained} ")
        if coverage_prefetch:
            note += (
                     f"cov_at_gesture={coverage_at_gesture} "
                f"cov_batches={stats['coverage_batches']} "
                     f"cov_req={stats['coverage_tiles_requested']} "
                     f"cov_done={stats['coverage_tiles_completed']} "
                     f"cov_fail={stats['coverage_tiles_failed']} "
                     f"cov_cancelled={stats['coverage_cancelled']} "
                     f"cov_abandoned_done={stats['coverage_abandoned_finished']} ")
        note += f"{'VALID' if valid else 'INVALID'}"
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
                          gated=True, hot_label="HOT"):
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
                   f"{hot_label} {h.mean():6.2f} (range {h.max() - h.min():5.2f})")
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
    ap.add_argument("--single", nargs=2, metavar=("KIND", "MODE"),
                    help="internal: run ONE arm in this process and print a "
                         "JSON line. MODE is '0' (BASE), '1' (HOT) or '2' "
                         "(HOT+COVERAGE). Each arm runs in its own "
                         "subprocess -- 60+ arms in one process hit the "
                         "known cupy/Qt teardown segfault (the same reason "
                         "the test suites must be run one file per "
                         "process), and separate processes also isolate the "
                         "arms more completely than any in-process teardown "
                         "can.")
    ap.add_argument("--io-workers", type=int, default=DEFAULT_IO_WORKERS)
    ap.add_argument("--compute-workers", type=int, default=DEFAULT_COMPUTE_WORKERS)
    args = ap.parse_args()

    # MODE -> (label, hot, coverage_prefetch). "0"/"1" are the pre-existing
    # BASE/HOT arms, completely unchanged. "2" is the new HOT+COVERAGE arm.
    if args.single:
        kind, mode = args.single
        hot = mode in (MODE_HOT, MODE_COVERAGE)
        coverage_prefetch = mode == MODE_COVERAGE
        app = QtWidgets.QApplication(sys.argv)
        result = run_arm(app, args, kind, hot, MODE_LABELS[mode],
                         coverage_prefetch=coverage_prefetch,
                         idle_s=MODE_IDLE_S[mode],
                         require_coverage_at_gesture=coverage_prefetch)
        print("RESULT " + json.dumps(result), flush=True)
        return

    def one(kind, mode, rep):
        label = MODE_LABELS[mode]
        cmd = [sys.executable, os.path.abspath(__file__),
               "--single", kind, mode,
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
            print(f"  r{rep} {label} {kind}: ARM FAILED "
                  f"(rc={proc.returncode})", flush=True)
            print("    " + (proc.stderr.strip().splitlines() or ["<no stderr>"])[-1],
                  flush=True)
        return payload

    modes = (MODE_BASE_SHORT, MODE_HOT, MODE_BASE_LONG, MODE_COVERAGE)
    out = {(k, m): [] for k in ("drag", "zoom") for m in modes}
    for rep in range(args.reps):
        # Counterbalanced: always running the same arm first would hand it
        # a warmer OS page cache in every pair/triple, a systematic
        # advantage rather than a measurement. A simple rotation gives each
        # of the three arms an equal share of "goes first" across reps
        # (the original two-arm alternation is the rep%2 case of exactly
        # this rotation).
        order = modes[rep % len(modes):] + modes[:rep % len(modes)]
        for kind in ("drag", "zoom"):
            for mode in order:
                res = one(kind, mode, rep)
                if res is None or not res.get("valid", False):
                    if res is not None:
                        print(f"  r{rep} arm INVALID (HOT was not genuinely "
                              f"working before the gesture, or it failed) -- "
                              f"the run will be INCONCLUSIVE", flush=True)
                    res = {"cov": float("nan"), "mixed": float("nan"),
                           "converge": float("nan"), "valid": False}
                out[(kind, mode)].append(res)

    print()
    # Two comparisons, each between arms that waited EXACTLY as long. Mixing
    # the 0.35s and 1.5s arms would compare different amounts of foreground
    # settling, not the feature.
    for label, base_mode, test_mode in (
            ("HOT", MODE_BASE_SHORT, MODE_HOT),
            ("COVERAGE", MODE_BASE_LONG, MODE_COVERAGE)):
        print(paired_noninferiority(
            f"[{label}] drag in-motion coverage %",
            [r["cov"] for r in out[("drag", base_mode)]],
            [r["cov"] for r in out[("drag", test_mode)]], higher_is_better=True))
        print(paired_noninferiority(
            f"[{label}] zoom mixed-level frames %",
            [r["mixed"] for r in out[("zoom", base_mode)]],
            [r["mixed"] for r in out[("zoom", test_mode)]], higher_is_better=False))
        print(paired_noninferiority(
            f"[{label}] zoom converge-after-stop ms",
            [r["converge"] for r in out[("zoom", base_mode)]],
            [r["converge"] for r in out[("zoom", test_mode)]],
            higher_is_better=False, unit="ms", gated=False))


if __name__ == "__main__":
    main()
