"""G3.2b.4B1.2.3a: which floor does HOT actually ask for, and in what order.

Real-machine acceptance found an asymmetry the automation had not caught:

    TopHat  -> new channel -> stand still -> cuCIM    passes
    cuCIM   -> new channel -> stand still -> TopHat   FAILS (recomputes)
    Original-> new channel -> stand still -> either   passes

This script does not assume why. It records, in time order, every event on
the floor path for all three starting methods:

  * every `prepare_floor_async(channel, method, params)` -- with the floor
    cache key it resolved to, what `has_cached_floor` said, whether a job
    was already running and WHICH request that was (foreground or
    background, and for which channel/method), what sat in the single
    background waiting slot BEFORE and AFTER the call, `_floor_pending`,
    and what the call returned;
  * every `_run_floor_job` -- i.e. the order jobs really START;
  * every floor result delivered -- i.e. the order jobs really FINISH,
    and whether the result was remembered;
  * every `_start_next_floor_job` -- what it picked up, or why it dropped.

Then it reports, per trajectory, whether BOTH methods' floors ended up in
the cache, and what the user's first click on the other method costs.

Honest limits: the OS page cache is NOT dropped; nothing here is a cold
disk read. The demo slide is opened READ-ONLY. Only `biopsy.ome.tif` is
used -- the larger slide in the user's own log is not available here and no
number is invented for it.

FORCING THE WINDOW. On this demo slide the foreground floor finishes BEFORE
HOT ever asks (HOT only asks once `_hot_idle()`, i.e. after its tile batch),
so the race cannot occur here at all -- which is itself a finding, and why
the automation missed what the real machine hit. `--slow-floor MS` makes the
floor worker's own `correct_array` sleep, so the foreground floor is still
running when `_prepare_centre_floors()` fires, exactly as on a slide whose
level is large enough. It sleeps on the FLOOR WORKER THREAD only and changes
no decision, no key and no result. A slowed floor job also TAKES longer, so
`--dwell MS` lengthens the stand-still to match -- the user on the big slide
stands still for as long as they stand still, and a dwell too short for the
work is a measurement artefact, not a finding.

Usage:
    python scripts/diagnose_step0_b123a_floor_order.py OUT.json [label]
                                                       [--slow-floor MS]
"""

import importlib.util
import json
import os
import pathlib
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = pathlib.Path(__file__).resolve().parents[1]
if "block01" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "block01", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["block01"] = _module
    _spec.loader.exec_module(_module)
sys.path.insert(0, str(ROOT.parent))

from PyQt5 import QtTest, QtWidgets  # noqa: E402

APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

SLIDE = "/sda1/Fusion/benchmark/biopsy.ome.tif"
TOPHAT_RADIUS = 15
CUCIM_SIGMA = 50
ORDER = ["CD22", "CD4", "PD1", "FOXP3", "TCF1", "TIM3"]
VIEWPORT = (9000, 9000, 2048, 2048)
PARAM = {"tophat": (TOPHAT_RADIUS,), "cucim": (CUCIM_SIGMA,), None: ()}

T0 = time.perf_counter()


def now_ms():
    return round((time.perf_counter() - T0) * 1000.0, 3)


def ctx_text(ctx, level=None, stride=None):
    if ctx is None:
        return None
    text = "|".join(str(part) for part in ctx)
    if level is not None:
        text += f"|L{level}|s{stride}"
    return text


def request_text(request):
    if request is None:
        return None
    return {
        "channel": request.channel, "method": request.method,
        "base_params": list(request.base_params),
        "foreground": bool(request.foreground),
        "ctx": ctx_text(request.ctx, request.floor_level, request.stride),
    }


class FloorTrace:
    """Wraps the four floor entry points. Records only; decides nothing."""

    def __init__(self):
        from block01.viewer import explore_view as ev

        self.events = []
        self.running = None          # the _FloorRequest now on the worker
        self.phase = "setup"
        self._lock = threading.Lock()

        real_prepare = ev.ExploreController.prepare_floor_async
        real_run = ev.ExploreController._run_floor_job
        real_handle = ev.ExploreController._handle_floor_result
        real_next = ev.ExploreController._start_next_floor_job

        def add(**row):
            with self._lock:
                self.events.append({"t_ms": now_ms(), "phase": self.phase,
                                    **row})

        def prepare(inner, channel, method, params=()):
            level, stride = inner._pick_floor_level_and_stride()
            ctx = inner._floor_ctx_for(channel, method, params, level, stride)
            before = request_text(inner._floor_background_request)
            cached = inner.has_cached_floor(channel, method, params)
            running = request_text(self.running)
            job_running = bool(inner._floor_job_running)
            pending = bool(getattr(inner, "_floor_pending", False))
            out = real_prepare(inner, channel, method, params)
            add(event="prepare_floor_async",
                asked_channel=str(channel), asked_method=method,
                asked_params=list(params or ()),
                floor_key=ctx_text(ctx, level, stride),
                has_cached_floor=bool(cached),
                floor_job_running=job_running,
                running_request=running,
                background_slot_before=before,
                background_slot_after=request_text(
                    inner._floor_background_request),
                floor_pending=pending,
                returned=bool(out))
            return out

        def run_job(inner, request):
            self.running = request
            add(event="floor_job_START", request=request_text(request))
            return real_run(inner, request)

        def handle(inner, payload):
            request = payload[8] if len(payload) > 8 else None
            ctx, level, stride = payload[1], payload[2], payload[3]
            add(event="floor_result",
                request=request_text(request),
                ctx=ctx_text(ctx, level, stride),
                foreground=(request is None or request.foreground),
                error=str(payload[5]) if payload[5] is not None else None,
                floor_pending_before=bool(
                    getattr(inner, "_floor_pending", False)),
                background_slot_before=request_text(
                    inner._floor_background_request))
            self.running = None
            out = real_handle(inner, payload)
            add(event="floor_result_done",
                cached_now=sorted(ctx_text(k[0], k[1], k[2])
                                  for k in inner._floor_cache))
            return out

        def start_next(inner):
            waiting = inner._floor_background_request
            add(event="start_next_floor_job",
                waiting=request_text(waiting),
                job_running=bool(inner._floor_job_running),
                already_cached=(
                    waiting is not None
                    and (waiting.ctx, waiting.floor_level, waiting.stride)
                    in inner._floor_cache))
            return real_next(inner)

        ev.ExploreController.prepare_floor_async = prepare
        ev.ExploreController._run_floor_job = run_job
        ev.ExploreController._handle_floor_result = handle
        ev.ExploreController._start_next_floor_job = start_next

    def mark(self):
        with self._lock:
            return len(self.events)

    def since(self, mark):
        with self._lock:
            return self.events[mark:]


class SlowFloor:
    """Delay the floor worker's own correction, and nothing else.

    A sleep on the floor worker thread reproduces a big slide's timing
    without touching a single line of production code, a decision or a
    pixel. Tile corrections run on the scheduler's `tile-compute-*` threads
    and are deliberately left alone.
    """

    def __init__(self, delay_ms):
        from block01.viewer import correction_compute as cc
        self.delay_s = delay_ms / 1000.0
        real_array = cc.CorrectionCompute.correct_array

        def correct_array(inner, arr, method, param):
            # ONCE PER FLOOR JOB, on the floor's own whole-level array. The
            # gain calibration runs on this same thread and calls this same
            # function once per window per level (12 times here), so a
            # blanket sleep would multiply by 13 and starve the dwell
            # instead of reproducing a big slide.
            if (threading.current_thread().name.startswith(
                    "explore-floor-compute")
                    and getattr(arr, "size", 0) > 1_000_000):
                time.sleep(self.delay_s)
            return real_array(inner, arr, method, param)

        cc.CorrectionCompute.correct_array = correct_array


class Counters:
    """Corrected tiles, and the provider's raw reads."""

    def __init__(self):
        from block01.viewer import correction_compute as cc
        self.tiles = []
        real_compute = cc.CorrectionCompute.compute

        def compute(inner, key):
            self.tiles.append(key)
            return real_compute(inner, key)

        cc.CorrectionCompute.compute = compute


def drain(ms=400, step=10):
    end = time.perf_counter() + ms / 1000.0
    while time.perf_counter() < end:
        APP.processEvents()
        QtTest.QTest.qWait(step)


def build_tab():
    from block01.ui.step0.step0_explore_tab import Step0ExploreTab
    from block01.viewer.prefetch_policy import ChannelCorrectionSpec

    tab = Step0ExploreTab(page=None)
    tab.set_hot_specs_provider(lambda: [
        ChannelCorrectionSpec(channel=name, tophat_radius=TOPHAT_RADIUS,
                              cucim_sigma=CUCIM_SIGMA) for name in ORDER])
    tab.resize(1100, 800)
    tab.show()
    APP.processEvents()
    tab.set_dataset(SLIDE)
    assert tab.show_source(ORDER[0], "tophat", PARAM["tophat"]) is True
    drain(500)
    tab.stack.controller.jump_to(*VIEWPORT)
    drain(4000)
    return tab


def floor_key_of(controller, channel, method, params):
    level, stride = controller._pick_floor_level_and_stride()
    ctx = controller._floor_ctx_for(channel, method, params, level, stride)
    return ctx_text(ctx, level, stride)


def trajectory(tab, trace, counters, channel, shown, clicked, dwell_ms=4000):
    """Arrive on `channel` showing `shown`; stand still; then click `clicked`."""
    controller = tab.stack.controller
    out = {"channel": channel, "shown_method": shown, "clicked": clicked}

    trace.phase = f"arrive:{channel}:{shown}"
    mark = trace.mark()
    tiles_mark = len(counters.tiles)
    tab.show_source(channel, shown, PARAM[shown])
    drain(dwell_ms)
    out["arrive_events"] = trace.since(mark)
    out["arrive_tile_computes"] = len(counters.tiles) - tiles_mark

    # ── what is in the cache after standing still ────────────────────
    out["both_floors_cached_after_dwell"] = {
        method: bool(controller.has_cached_floor(channel, method,
                                                 PARAM[method]))
        for method in ("tophat", "cucim")}
    out["floor_keys"] = {
        method: floor_key_of(controller, channel, method, PARAM[method])
        for method in ("tophat", "cucim")}
    out["floor_cache_contents"] = sorted(
        ctx_text(k[0], k[1], k[2]) for k in controller._floor_cache)

    # ── the user's first click on the other method ───────────────────
    trace.phase = f"click:{channel}:{clicked}"
    mark = trace.mark()
    tiles_mark = len(counters.tiles)
    hits_before = controller.stats.get("floor_cache_hits", 0)
    started = time.perf_counter()
    tab.show_source(channel, clicked, PARAM[clicked])
    at_return_ms = round((time.perf_counter() - started) * 1000.0, 1)
    floor_ready_at_return = bool(controller._floor_ready)
    drain(2500)
    click_events = trace.since(mark)
    out["click"] = {
        "show_source_return_ms": at_return_ms,
        "floor_ready_at_show_source_return": floor_ready_at_return,
        "floor_cache_hits_delta": (controller.stats.get("floor_cache_hits", 0)
                                   - hits_before),
        "floor_jobs_started": sum(1 for e in click_events
                                  if e["event"] == "floor_job_START"),
        "tile_computes": len(counters.tiles) - tiles_mark,
        "events": click_events,
        "selection": {"channel": controller.channel,
                      "method": controller.method,
                      "params": list(controller.params)},
    }
    return out


def main():
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                       else "b123a_floor_order.json")
    argv = sys.argv[2:]
    slow_ms, dwell_ms = 0, 4000
    for flag, setter in (("--slow-floor", "slow"), ("--dwell", "dwell")):
        if flag in argv:
            index = argv.index(flag)
            value = int(argv[index + 1])
            argv = argv[:index] + argv[index + 2:]
            if setter == "slow":
                slow_ms = value
            else:
                dwell_ms = value
    label = argv[0] if argv else "g32b4b123a"
    if slow_ms:
        SlowFloor(slow_ms)
    trace = FloorTrace()
    counters = Counters()
    tab = build_tab()
    controller = tab.stack.controller

    report = {
        "label": label, "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "slide": SLIDE, "viewport_l0": list(VIEWPORT),
        "floor_level_stride": list(controller._pick_floor_level_and_stride()),
        "slow_floor_ms": slow_ms,
        "dwell_ms": dwell_ms,
        "honesty": [
            "the OS page cache was NOT dropped; nothing here is a cold read",
            "the demo slide is opened read-only",
            "every entry point below is WRAPPED TO RECORD ONLY; no decision "
            "is changed by this script",
            "--slow-floor sleeps on the FLOOR WORKER THREAD only, to "
            "reproduce a large slide's timing; it changes no decision, no "
            "key and no pixel",
        ],
        "trajectories": [],
    }

    for index, (shown, clicked) in enumerate((("tophat", "cucim"),
                                              ("cucim", "tophat"),
                                              (None, "tophat"),
                                              (None, "cucim"))):
        report["trajectories"].append(
            trajectory(tab, trace, counters, ORDER[1 + index], shown,
                       clicked, dwell_ms=dwell_ms))

    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"wrote {out}")
    for traj in report["trajectories"]:
        click = traj["click"]
        print(f"{traj['channel']:6s} {str(traj['shown_method']):8s} -> "
              f"{traj['clicked']:6s} | cached after dwell: "
              f"{traj['both_floors_cached_after_dwell']} | click: "
              f"hits+{click['floor_cache_hits_delta']} "
              f"jobs={click['floor_jobs_started']} "
              f"tiles={click['tile_computes']} "
              f"ready_at_return={click['floor_ready_at_show_source_return']}")
    try:
        tab.teardown()
    except Exception:                                       # noqa: BLE001
        pass


if __name__ == "__main__":
    main()
