"""Interactive Explore demo — MANUAL testing entry point.

Unlike scripts/g1_render_probe.py (which DRIVES the camera itself for
measurement and will fight any manual dragging), this window is purely
interactive: pyqtgraph's native mouse pan (left-drag) and wheel zoom drive
the viewport; the controller only reacts.

Usage (desktop session, real display):
    cd /sda1/Fusion/analysis_pipline/block01_v14
    python scripts/explore_demo.py \
        --path /sda1/Fusion/benchmark/tonsil/..._Tonsil.ome.tif \
        --channel 1 [--method tophat --param 25]

A log file with any Python/Qt fault traceback is written next to the
terminal output (faulthandler enabled), so crashes are diagnosable.
"""

import argparse
import faulthandler
import os
import sys
import time
import traceback


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

import pyqtgraph as pg  # noqa: E402
from PyQt5 import QtCore, QtWidgets  # noqa: E402

# Match main.py's global config (imageAxisOrder="row-major"): standalone use
# of this demo must not render transposed just because it never touched
# this global. Every ImageItem in explore_view.py ALSO sets axisOrder
# explicitly per-item, so this is belt-and-suspenders, not the only fix.
pg.setConfigOptions(imageAxisOrder="row-major")

from block01.core import bg_correction  # noqa: E402
from block01.viewer.caches import LRUByteCache  # noqa: E402
from block01.viewer.correction_compute import CorrectionCompute  # noqa: E402
from block01.viewer.explore_view import ExploreController, ExploreView  # noqa: E402
from block01.viewer.multichannel_prefetch import (  # noqa: E402
    MultiChannelPrefetchController,
)
from block01.viewer.prefetch_policy import ChannelCorrectionSpec  # noqa: E402
from block01.viewer.raw_tile_provider import RawTileProvider  # noqa: E402
from block01.viewer.scheduler import (  # noqa: E402
    DEFAULT_COMPUTE_WORKERS,
    DEFAULT_IO_WORKERS,
    TileScheduler,
)
from block01.viewer.tile_types import TileGridSpec  # noqa: E402



# ── channel-switch diagnostics ───────────────────────────────────────────
#
# MEASURES WHAT IS ON SCREEN, not what a signal says was computed. A
# delivery signal means "the pixels exist"; it does not mean the user can
# see them -- visibility is decided afterwards by the display policy
# (`_update_layer_visibility`: floor readiness, per-level gating, key
# context). So this monitor samples the actual scene on a timer: the
# overview item, the corrected-floor item, and the pooled ImageItems whose
# rect intersects the viewport, reading `isVisible()` and each entry's
# blitted key. It only reads; it never asks the controller for anything.
#
# Milestones per switch, all measured from the moment `set_selection` was
# called:
#   overview      target channel's overview visible
#   any           ANY target-channel pixels visible (overview/floor/raw/precise)
#   floor         target channel's corrected floor visible
#   first_precise first current-level corrected tile of the target on screen
#   full_precise  EVERY visible tile of the viewport covered by a
#                 current-level corrected tile of the target -- the
#                 completion time that decides whether a switch felt done
# A milestone printed as `--` was NOT observed before the switch finished:
# sampling stops at full_precise (or at the 2s timeout), so e.g. `floor=--`
# means the corrected floor never became visible because the current-level
# corrected tiles beat it, not that the floor is broken.
#
# Frame counters: blank (no target pixels at all), raw (a target-channel
# RAW tile is on screen, i.e. uncorrected pixels are being shown), wrong
# (any visible layer whose channel identity is NOT the target -- a hard
# zero; anything else is a scientific-identity defect, not a latency one).
# blank/raw also carry a duration, since three 16ms frames is a blink and
# three 200ms frames is a hole.

SWITCH_TIMEOUT_S = 2.0
SWITCH_SAMPLE_MS = 16


class SwitchMonitor(QtCore.QObject):
    def __init__(self, ctrl, view, provider, hot, parent=None,
                 timeout_s=SWITCH_TIMEOUT_S):
        super().__init__(parent)
        self.timeout_s = timeout_s
        self.ctrl = ctrl
        self.view = view
        self.provider = provider
        self.hot = hot
        self.active = None
        self.sample_us = []
        self.results = []
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(SWITCH_SAMPLE_MS)
        self.timer.timeout.connect(self._sample)

    # -- lifecycle ---------------------------------------------------
    def arm(self, previous, target, hot_neighbourhood, *, t0,
            overview_before, tile_ready, tiles_cached=None):
        """Called immediately after `set_selection(channel=target)`, but with
        state captured BEFORE it.

        `t0` must be taken before the call: `set_selection` does synchronous
        work (an atomic cached swap, floor job start, gain install), and a
        clock started afterwards charges none of it -- an atomic swap would
        be reported as ~0-1ms no matter how long it actually blocked the GUI
        thread. `overview_before` / `tile_ready` likewise describe what was
        prepared BEFORE the switch; asking afterwards would partly measure
        the switch's own effect."""
        self.active = {
            "t0": t0,
            "prev": previous,
            "target": target,
            "class": "near" if target in hot_neighbourhood else "far",
            "overview_before": overview_before,
            "tile_ready": tile_ready,
            "tiles_cached": tiles_cached,
            "t": {},
            "blank": 0,
            "raw": 0,
            "wrong": 0,
            "frames": 0,
            # Frame COUNTS alone do not describe the experience -- three
            # blank frames at 16ms is a blink, three at 200ms is a hole.
            # Each sample charges the interval since the previous one to
            # whatever it observed.
            "blank_ms": 0.0,
            "raw_ms": 0.0,
            "last_sample": None,
        }
        self.sample_us = []
        self.timer.start()
        self._sample()

    def _mark(self, name, now):
        rec = self.active
        if name not in rec["t"]:
            rec["t"][name] = (now - rec["t0"]) * 1000.0

    # -- one read-only look at the scene ------------------------------
    def _sample(self):
        rec = self.active
        if rec is None:
            return
        t_enter = time.perf_counter()
        target = rec["target"]
        ctrl, view = self.ctrl, self.view
        source = self.provider.source_identity()

        # Every visible layer must PROVE it belongs to the target. An
        # identity that cannot be checked (no recorded identity, no blitted
        # key) is not innocent -- pixels are on screen and nothing says
        # whose they are -- so it counts as wrong, not as skipped.
        overview_shown = view.overview_item.isVisible()
        overview_ok = overview_shown and ctrl._overview_identity == (source, target)
        floor_shown = view.corrected_floor_item.isVisible()
        live_floor_ctx = ctrl._current_floor_ctx(ctrl._floor_level,
                                                 ctrl._floor_stride)
        floor_ok = (floor_shown and ctrl._floor_ctx is not None
                    and live_floor_ctx is not None
                    and ctrl._floor_ctx == live_floor_ctx)

        # Current-level corrected coverage of the viewport, per tile,
        # counting only tiles whose ImageItem is actually visible.
        ctx = ctrl.selection_key_context()
        level = ctrl.level
        visible_tiles = set(ctrl._visible_tiles)
        precise_hits = 0
        for tx, ty in visible_tiles:
            entry = ctrl._precise_pool.get(level, tx, ty)
            if (entry is not None and entry.key is not None
                    and entry.item.isVisible()
                    and ctrl._key_matches_context(entry.key, ctx)):
                precise_hits += 1
        full_precise = bool(visible_tiles) and precise_hits == len(visible_tiles)

        # Wrong-channel and raw-stage detection over every pooled item that
        # is BOTH visible and inside the viewport. Bounded by the pool
        # budget, and rect-intersection is cheap -- the sampling cost is
        # reported as monitor_p95 so this claim is checkable, not assumed.
        bbox = ctrl._current_bbox
        raw_on_screen = False
        wrong = (overview_shown and not overview_ok) or (floor_shown and not floor_ok)
        for pool, is_raw in ((ctrl._precise_pool, False), (ctrl._raw_pool, True)):
            for entry in pool.entries.values():
                if not entry.item.isVisible():
                    continue
                if not _rect_hits_bbox(entry.rect, bbox):
                    continue
                key = entry.key
                if key is None:
                    wrong = True
                    continue
                if is_raw:
                    # RawKey carries no method/params: source + channel IS
                    # its whole identity.
                    if key.source != source or key.channel != target:
                        wrong = True
                    else:
                        raw_on_screen = True
                elif not ctrl._precise_key_current_for_level(key, entry.level):
                    # The pool's own visibility predicate -- source,
                    # channel, method, level-scaled params, level and
                    # quality, not just the channel.
                    wrong = True

        any_target = overview_ok or floor_ok or precise_hits > 0 or raw_on_screen
        now = time.perf_counter()
        rec["frames"] += 1
        last = rec["last_sample"]
        delta_ms = 0.0 if last is None else (now - last) * 1000.0
        rec["last_sample"] = now
        if wrong:
            rec["wrong"] += 1
        if not any_target:
            rec["blank"] += 1
            rec["blank_ms"] += delta_ms
        if raw_on_screen:
            rec["raw"] += 1
            rec["raw_ms"] += delta_ms
        if overview_ok:
            self._mark("overview", now)
        if any_target:
            self._mark("any", now)
        if floor_ok:
            self._mark("floor", now)
        if precise_hits > 0:
            self._mark("first_precise", now)
        if full_precise:
            self._mark("full_precise", now)

        rec["level"] = level
        rec["visible_tiles"] = len(visible_tiles)
        self.sample_us.append((time.perf_counter() - t_enter) * 1e6)
        if full_precise or (now - rec["t0"]) >= self.timeout_s:
            self._finish(done=full_precise)

    def _finish(self, done):
        self.timer.stop()
        rec = self.active
        self.active = None
        us = sorted(self.sample_us)
        p95 = us[min(len(us) - 1, int(0.95 * len(us)))] if us else 0.0
        rec["monitor_p95_ms"] = p95 / 1000.0
        rec["verdict"] = ("PASS" if (done and rec["wrong"] == 0)
                          else ("TIMEOUT" if not done else "FAIL(wrong)"))
        self.results.append(rec)

        def ms(name):
            v = rec["t"].get(name)
            return f"{v:.0f}ms" if v is not None else "--"

        cached = rec.get("tiles_cached")
        cached_txt = ("" if not cached else
                      " cached=" + ",".join(f"{m} {h}/{n}"
                                            for m, (h, n) in cached.items()))
        print(f"[switch] {rec['prev']} -> {rec['target']} class={rec['class']} "
              f"level={rec.get('level')} visible_tiles={rec.get('visible_tiles')} "
              f"overview_before={'yes' if rec['overview_before'] else 'no'} "
              f"tile_ready={'n/a' if rec['tile_ready'] is None else ('yes' if rec['tile_ready'] else 'no')}"
              f"{cached_txt}\n"
              f"         overview={ms('overview')} any={ms('any')} "
              f"floor={ms('floor')} first_precise={ms('first_precise')} "
              f"full_precise={ms('full_precise')}\n"
              f"         blank={rec['blank']}({rec['blank_ms']:.0f}ms) "
              f"raw={rec['raw']}({rec['raw_ms']:.0f}ms) "
              f"wrong={rec['wrong']} frames={rec['frames']} "
              f"monitor_p95={rec['monitor_p95_ms']:.2f}ms {rec['verdict']}",
              flush=True)



def _cached_tile_counts(hot, scheduler, snapshot, target):
    """How many of the viewport's tiles for `target` are ALREADY in the
    corrected cache, per method, before the switch happens.

    Read-only in the strict sense: it tests membership in the cache's
    ordered store directly rather than calling `get()`, so it neither
    reorders the LRU nor pollutes the hit/miss counters. Without this the
    claim "COVERAGE had the tiles and the overview was the gate" rests on
    the coincidence of `overview == first_precise`, which is evidence, not
    proof.
    """
    if hot is None:
        return None
    spec = hot._spec_by_channel.get(target)
    if spec is None or not snapshot.visible_tiles:
        return None
    store = scheduler.corrected_cache._store
    counts = {}
    for method, base in (("tophat", spec.tophat_radius),
                         ("cucim", spec.cucim_sigma)):
        hits = 0
        for tx, ty in snapshot.visible_tiles:
            if hot._make_key(snapshot, target, tx, ty, method, base) in store:
                hits += 1
        counts[method] = (hits, len(snapshot.visible_tiles))
    return counts


def _rect_hits_bbox(rect, bbox):
    """`rect` is a QRectF in world (level-0 pixel) coordinates; `bbox` is
    the controller's current (y0, x0, y1, x1) viewport in the same space."""
    if bbox is None:
        return True
    y0, x0, y1, x1 = bbox
    return not (rect.right() <= x0 or rect.left() >= x1
                or rect.bottom() <= y0 or rect.top() >= y1)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--path", required=True)
    ap.add_argument("--channel", default="1")
    ap.add_argument("--method", default="tophat", choices=["tophat", "cucim", "none"])
    ap.add_argument("--param", type=int, default=25)
    ap.add_argument("--log", default="/tmp/explore_demo_crash.log")
    ap.add_argument("--io-workers", type=int, default=DEFAULT_IO_WORKERS,
                    help=f"raw I/O worker threads (default {DEFAULT_IO_WORKERS}; "
                         "see viewer/scheduler.py for the measured basis)")
    ap.add_argument("--compute-workers", type=int, default=DEFAULT_COMPUTE_WORKERS,
                    help=f"correction compute worker threads (default {DEFAULT_COMPUTE_WORKERS})")
    ap.add_argument("--intermediate-fallback", dest="intermediate_fallback",
                    action="store_true", default=True,
                    help="request corrected tiles at level+1 as a fallback "
                         "underlay during motion (default: on)")
    ap.add_argument("--no-intermediate-fallback", dest="intermediate_fallback",
                    action="store_false",
                    help="disable the level+1 intermediate corrected fallback "
                         "(for A/B comparison against the level-2 floor only)")
    ap.add_argument("--directional-prefetch", dest="directional_prefetch",
                    action="store_true", default=True,
                    help="prefetch current-level corrected tiles ahead of a "
                         "sustained pan, cache-only, capped to one in-flight "
                         "request (default: on)")
    ap.add_argument("--no-directional-prefetch", dest="directional_prefetch",
                    action="store_false",
                    help="disable directional prefetch (for A/B comparison)")
    ap.add_argument("--hot", dest="hot", action="store_true", default=True,
                    help="enable multi-channel HOT prefetch (default: on)")
    ap.add_argument("--no-hot", dest="hot", action="store_false",
                    help="disable multi-channel HOT prefetch")
    ap.add_argument("--coverage", dest="coverage", action="store_true",
                    default=False,
                    help="enable multi-channel COVERAGE prefetch, cache-only "
                         "background preparation of every remaining "
                         "channel's current viewport once HOT is idle "
                         "(default: OFF -- this demo's corrected cache is "
                         "512MB, and a long dwell on the real 57-channel "
                         "slide evicted 1305 of the 1817 tiles COVERAGE "
                         "produced, so most of the background work is "
                         "discarded again; only meaningful with --hot)")
    ap.add_argument("--no-coverage", dest="coverage", action="store_false",
                    help="disable multi-channel COVERAGE prefetch (for A/B "
                         "comparison against HOT alone)")
    ap.add_argument("--corrected-cache-gb", type=float, default=0.5,
                    help="corrected-tile cache budget in GB (default: 0.5, "
                         "unchanged). TEST KNOB: raise it for far-channel "
                         "switch measurement -- at 0.5GB a long COVERAGE "
                         "dwell on the 57-channel slide evicted 1305 of the "
                         "1817 tiles it produced (see "
                         "docs/benchmarks/2026-09-01_57ch_coverage_long_dwell.md), "
                         "which would contaminate any 'tile-ready far "
                         "channel' conclusion")
    ap.add_argument("--auto-switch", default=None, metavar="CH[,CH...]",
                    help="drive this channel sequence automatically instead "
                         "of clicking, for a repeatable smoke test of the "
                         "[switch] diagnostics; names or indices")
    ap.add_argument("--auto-switch-settle-ms", type=int, default=400,
                    help="quiet time between automatic switches (default: 400)")
    ap.add_argument("--auto-exit", action="store_true",
                    help="quit after an --auto-switch sequence finishes")
    ap.add_argument("--switch-timeout-s", type=float, default=SWITCH_TIMEOUT_S,
                    help="a switch that is not fully corrected within this "
                         "many seconds is scored TIMEOUT (default: 2.0). "
                         "Lower it to exercise the failure path itself -- an "
                         "--auto-exit sequence must exit non-zero when any "
                         "switch fails")
    ap.add_argument("--start-view", default=None, metavar="Y,X,SPAN",
                    help="open at this level-0 centre and span instead of "
                         "the whole slide; a small span puts the viewer on "
                         "level 0, which is where the atomic no-raw-flash "
                         "contract applies (the whole-slide view sits on a "
                         "coarse level, where a raw frame is expected)")
    args = ap.parse_args()

    # Crash diagnosis: native faults AND Python exceptions land in the log.
    log_f = open(args.log, "w")
    faulthandler.enable(file=log_f, all_threads=True)

    def _excepthook(exc_type, exc, tb):
        traceback.print_exception(exc_type, exc, tb, file=log_f)
        log_f.flush()
        traceback.print_exception(exc_type, exc, tb)
    sys.excepthook = _excepthook

    app = QtWidgets.QApplication(sys.argv)

    provider = RawTileProvider(args.path)  # per_thread default
    names = provider.channel_names
    channel = (names[int(args.channel)] if str(args.channel).isdigit()
               else args.channel)
    grid = TileGridSpec(tile_size=512, source_chunk_shape=(), grid_version="v1")
    raw_cache = LRUByteCache(512 * 1024 * 1024)
    corrected_cache = LRUByteCache(int(args.corrected_cache_gb * 1024) << 20)
    compute = CorrectionCompute(provider, raw_cache)
    # Worker counts are the largest measured lever on how sharp the image
    # is DURING motion (viewer/explore_view.py "Worker counts"): the
    # fraction of the viewport already covered mid-drag goes from 52.4% at
    # io=1/cw=1 to 89.8% at io=8/cw=4 (tophat), and mid-zoom from 25.0% to
    # 77.8%. These scripts previously pinned io=1/cw=1 -- BELOW
    # TileScheduler's own io_workers=4 default -- so every manual test ran
    # on a single I/O thread. Parallel output was verified byte-identical
    # to the serial path before raising these.
    scheduler = TileScheduler(provider, compute, raw_cache, corrected_cache,
                              io_workers=args.io_workers,
                              compute_workers=args.compute_workers)

    view = ExploreView()

    # Minimal channel selector. The point is to drive REAL channel switches
    # through `ctrl.set_selection(channel=...)` so the switch path can be
    # accepted by hand -- overview identity, corrected floor, gain
    # calibration and the raw request all belong to the channel the combo
    # says. It is deliberately the smallest possible control: the shared
    # ChannelDock is a Step0 concern and this demo is not where it mounts.
    channel_bar = QtWidgets.QWidget()
    _bar = QtWidgets.QHBoxLayout(channel_bar)
    _bar.setContentsMargins(6, 4, 6, 4)
    _bar.addWidget(QtWidgets.QLabel("Channel:"))
    channel_combo = QtWidgets.QComboBox()
    channel_combo.addItems(list(provider.channel_names))
    channel_combo.setCurrentText(channel)
    _bar.addWidget(channel_combo, 1)
    view.layout().insertWidget(0, channel_bar)

    base_title = f"Explore demo — {os.path.basename(args.path)} · {channel}"
    view.setWindowTitle(base_title)
    view.resize(1400, 1000)

    ctrl = ExploreController(provider, scheduler, compute, grid, view, channel,
                             intermediate_corrected_fallback=args.intermediate_fallback,
                             directional_prefetch=args.directional_prefetch)

    hot = None
    if args.hot:
        hot_specs = [
            ChannelCorrectionSpec(
                channel=name,
                tophat_radius=args.param,
                cucim_sigma=args.param,
            )
            for name in provider.channel_names
        ]
        if args.coverage:
            # Imported HERE, not at module scope: the ordinary demo path
            # must not pull `viewer.experimental` in at all.
            from block01.viewer.experimental.coverage_prefetch import (  # noqa: E402
                CoverageMultiChannelPrefetchController,
            )
            hot = CoverageMultiChannelPrefetchController(
                ctrl, scheduler, hot_specs, grid)
        else:
            hot = MultiChannelPrefetchController(ctrl, scheduler, hot_specs,
                                                 grid)

    def _on_floor_preparing_changed(preparing):
        suffix = " — Preparing corrected preview…" if preparing else ""
        view.setWindowTitle(base_title + suffix)

    ctrl.floor_preparing_changed.connect(_on_floor_preparing_changed)

    # [floor] diagnostics -- verifiable from the terminal even if the
    # in-view badge / title suffix is missed. `_start_floor_job` is a
    # plain `self._start_floor_job(gen)` call (not a signal slot), so
    # overriding it at the INSTANCE level intercepts every internal call
    # -- unlike `_handle_floor_result`, which is already bound into a
    # QueuedConnection at ExploreController.__init__ time and would not
    # see an instance-level override, hence the landing side below uses
    # the `floor_ready_changed` signal instead.
    _floor_diag = {"start": None}
    _real_start_floor_job = ctrl._start_floor_job

    def _start_floor_job_traced(gen):
        _real_start_floor_job(gen)
        _floor_diag["start"] = time.perf_counter()
        level, stride = ctrl._floor_level, ctrl._floor_stride
        h, w = provider.level_shape(level)
        approx_shape = (h // stride, w // stride)
        ctx = ctrl._current_floor_ctx(level, stride)
        eff_params = ctx[3] if ctx else ()
        print(f"[floor] job started: level={level} stride={stride} "
              f"approx_shape={approx_shape} effective_param={eff_params} "
              f"method={ctrl.method} channel={ctrl.channel}")

    ctrl._start_floor_job = _start_floor_job_traced

    def _on_floor_ready_changed(accepted):
        start = _floor_diag.get("start")
        elapsed_ms = (time.perf_counter() - start) * 1000.0 if start is not None else float("nan")
        level, stride = ctrl.stats["floor_level"], ctrl.stats["floor_stride"]
        shape = None
        if level is not None:
            h, w = provider.level_shape(level)
            shape = (h // stride, w // stride)
        # effective_param here is against the CURRENT live selection, which
        # may have moved on since this job started (that is in fact why a
        # result gets dropped as stale) -- reported for context, not as a
        # claim that it matches the job that just finished.
        ctx = ctrl._current_floor_ctx(level, stride) if level is not None else None
        eff_params = ctx[3] if ctx else ()
        status = "accepted" if accepted else "dropped-as-stale-or-failed"
        print(f"[floor] job landed: level={level} stride={stride} shape={shape} "
              f"effective_param={eff_params} elapsed_ms={elapsed_ms:.1f} status={status}")
        # [gain] diagnostics -- interactive DISPLAY gain only (see
        # explore_view.py module docstring): never claim this matches
        # production numerics.
        print(f"[gain] calibrated={ctrl.stats['gain_calibrated']} "
              f"table={ctrl.stats['level_display_gain']} "
              f"failed_count={ctrl.stats['gain_calibration_failed']}")

    ctrl.floor_ready_changed.connect(_on_floor_ready_changed)

    # Show the window and pump the event loop BEFORE load_overview()/
    # set_selection() start (and possibly finish) the floor job -- a
    # window that only appears afterward can never show the transient
    # "Preparing corrected preview…" title/badge state.
    monitor = SwitchMonitor(ctrl, view, provider, hot,
                            timeout_s=args.switch_timeout_s)
    names_list = list(provider.channel_names)

    def _hot_neighbourhood(of_channel):
        """HOT prefetches i-1, i+1, i-2, i+2 (prefetch_policy.hot_order), so
        a switch within +/-2 of where the camera settled is a NEAR switch --
        the one HOT was built to make instant. Anything else is FAR."""
        try:
            i = names_list.index(of_channel)
        except ValueError:
            return set()
        return {names_list[j] for j in (i - 2, i - 1, i + 1, i + 2)
                if 0 <= j < len(names_list)}

    def _on_channel_picked(name):
        previous = ctrl.channel
        neighbourhood = _hot_neighbourhood(previous)
        # Captured BEFORE the switch: the clock, so `set_selection`'s own
        # synchronous cost is inside the measurement, and the readiness
        # flags, so they describe what was prepared in advance rather than
        # what the switch itself just caused.
        pre_snapshot = ctrl.snapshot()
        overview_before = ctrl.has_overview_record(name)
        # `tile_ready` is STRICT channel readiness -- it requires an
        # installed overview record as well as the tiles, so a channel
        # COVERAGE fully prepared still reports False. `tiles_cached` is
        # the tile half on its own, which is what actually says whether
        # COVERAGE's work was there.
        tile_ready = (hot.is_channel_ready(name, pre_snapshot)
                      if hot is not None else None)
        tiles_cached = _cached_tile_counts(hot, scheduler, pre_snapshot, name)
        t0 = time.perf_counter()
        ctrl.set_selection(channel=name)
        view.setWindowTitle(
            f"Explore demo — {os.path.basename(args.path)} · {name}")
        print(f"[channel] -> {name} in {(time.perf_counter()-t0)*1000:.1f} ms "
              f"(atomic cached swaps so far: "
              f"{ctrl.stats.get('atomic_channel_swaps', 0)})", flush=True)
        monitor.arm(previous, name, neighbourhood, t0=t0,
                    overview_before=overview_before, tile_ready=tile_ready,
                    tiles_cached=tiles_cached)

    channel_combo.currentTextChanged.connect(_on_channel_picked)

    view.show()
    app.processEvents()

    ctrl.load_overview()
    if args.method != "none":
        ctrl.set_selection(method=args.method, params=(args.param,))

    # Start looking at the whole slide.
    h0, w0 = provider.level_shape(0)
    if args.start_view:
        cy, cx, span = (float(v) for v in args.start_view.split(","))
        half = span / 2.0
        view.view_box.setRange(xRange=(cx - half, cx + half),
                               yRange=(cy - half, cy + half), padding=0)
    else:
        view.view_box.setRange(xRange=(0, w0), yRange=(0, h0), padding=0)

    print(f"[explore_demo] channel={channel} method={args.method} param={args.param}")
    print(f"[explore_demo] corrected cache {args.corrected_cache_gb:.2f} GB, "
          f"hot={'on' if args.hot else 'off'} "
          f"coverage={'on' if (args.hot and args.coverage) else 'off'}")
    print(f"[explore_demo] drag = pan, wheel = zoom; crash log -> {args.log}")

    if args.auto_switch:
        sequence = [names[int(t)] if t.strip().isdigit() else t.strip()
                    for t in args.auto_switch.split(",") if t.strip()]
        unknown = [c for c in sequence if c not in names_list]
        if unknown:
            raise SystemExit(f"--auto-switch: unknown channel(s) {unknown}")
        pending = list(sequence)
        print(f"[explore_demo] auto-switch sequence: {sequence}", flush=True)

        # Drives the SAME path a human click drives -- the combo -- so the
        # smoke test cannot pass through a code path the manual test does
        # not use. One switch is started only once the previous one has
        # been scored and a quiet interval has passed, so switches never
        # overlap and each measurement is of a single switch.
        driver = QtCore.QTimer()
        driver.setInterval(args.auto_switch_settle_ms)
        quiet_since = [time.perf_counter()]

        def _drive():
            if monitor.active is not None:
                quiet_since[0] = time.perf_counter()
                return
            if (time.perf_counter() - quiet_since[0]) * 1000.0 < args.auto_switch_settle_ms:
                return
            if not pending:
                driver.stop()
                done = sum(1 for r in monitor.results if r["verdict"] == "PASS")
                wrong = sum(r["wrong"] for r in monitor.results)
                failed = [r for r in monitor.results if r["verdict"] != "PASS"]
                print(f"[switch] sequence finished: {done}/{len(monitor.results)} "
                      f"PASS, wrong_channel_frames total={wrong}", flush=True)
                for r in failed:
                    print(f"[switch] FAILED: {r['prev']} -> {r['target']} "
                          f"{r['verdict']}", flush=True)
                if args.auto_exit:
                    # A smoke test that exits 0 on TIMEOUT or a wrong-channel
                    # frame is worse than no smoke test: a script or CI job
                    # would read the failure as a pass.
                    QtWidgets.QApplication.instance().exit(1 if failed else 0)
                return
            quiet_since[0] = time.perf_counter()
            channel_combo.setCurrentText(pending.pop(0))

        driver.timeout.connect(_drive)
        driver.start()

    code = app.exec_()
    if hot is not None:
        hot.stop()
        print(f"[hot] {hot.stats}")
    else:
        print("[hot] disabled")
    ctrl.teardown()
    print(f"[floor] at exit: failed={ctrl.stats['floor_compute_failed']} "
          f"level={ctrl.stats['floor_level']} stride={ctrl.stats['floor_stride']}")
    print(f"[gain] at exit: calibrated={ctrl.stats['gain_calibrated']} "
          f"table={ctrl.stats['level_display_gain']} "
          f"failed_count={ctrl.stats['gain_calibration_failed']}")
    log_f.close()
    sys.exit(code)


if __name__ == "__main__":
    main()
