"""G3.2b.4B1.2.1: per-call attribution for Step0's first flip to the other method.

B1.2 reported two numbers it could not explain: the first flip to the other
method on a freshly selected channel still cost "12 `correct_array` calls"
and "12 level+1 tiles". This script does not re-report those totals. It
records, for EVERY `CorrectionCompute.correct_array()` call, an attribution
that cannot be confused with another call's:

  seq, monotonic t, thread, PURPOSE (tile / foreground floor / background
  floor / gain calibration), channel, method, effective param, source
  identity, level, stride, tile (tx, ty), floor cache key, HOT generation,
  before-or-after the user's switch, before-or-after the frame was complete,
  and -- for tiles -- which scheduler generation(s) had asked for that key.

PURPOSE is not inferred from a stack walk. `CorrectionCompute.compute()`
(the corrected-TILE path) calls `self.correct_array()` internally
(`viewer/correction_compute.py:130`), and so does
`ExploreController._calibrate_level_gains()` (once per calibration window
per pyramid level) and the floor job itself. Each of those three entry
points is wrapped to set a thread-local purpose, so every inner
`correct_array` is attributed to the entry point that really made it.

For the level+1 fallback it records the FULL SETS, not counts: what HOT
queued, what actually reached the corrected cache, what the foreground asks
for at the instant of the switch (split into the tiles that COVER the
viewport and the speculative one-tile look-ahead RING that
`_issue_settled_request` adds via `FALLBACK_HALO_TILES`), the hits, the
misses, and the symmetric difference with every differing field spelled out.

Honest limits: the OS page cache is NOT dropped; nothing here is a cold disk
read. The demo slide and project are opened READ-ONLY. Only
`biopsy.ome.tif` is used -- the larger slide in the user's own log is not
available here and no number is invented for it.

Usage: python scripts/diagnose_step0_b121_attribution.py OUT.json [label]
"""

import collections
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
PARAM = {"tophat": TOPHAT_RADIUS, "cucim": CUCIM_SIGMA, None: None}

T0 = time.perf_counter()
_LOCAL = threading.local()


def now_ms():
    return round((time.perf_counter() - T0) * 1000.0, 3)


def key_fields(key):
    """Every identity field of a CorrectionKey, as plain JSON."""
    if key is None:
        return None
    return {
        "source": str(key.source),
        "channel": key.channel,
        "level": key.tile.level,
        "tx": key.tile.tx,
        "ty": key.tile.ty,
        "tile_size": key.tile.grid.tile_size,
        "grid_version": key.tile.grid.grid_version,
        "method": key.method,
        "params": list(key.params),
        "algorithm_version": key.algorithm_version,
        "boundary_mode": key.boundary_mode,
        "quality": key.quality,
    }


def key_id(key):
    """A short, exact, comparable identity string for a CorrectionKey."""
    f = key_fields(key)
    return ("{channel}|{method}|{params}|L{level}|tx{tx},ty{ty}"
            "|{quality}|{algorithm_version}|{boundary_mode}|{source}").format(**{
                **f, "params": ",".join(str(p) for p in f["params"])})


class Recorder:
    """Wraps the three real entry points that reach `correct_array`."""

    def __init__(self):
        from block01.viewer import correction_compute as cc
        from block01.viewer import explore_view as ev
        from block01.viewer import scheduler as sch

        self.events = []          # every correct_array call, attributed
        self.tile_computes = []   # every CorrectionCompute.compute call
        self.requests = []        # every scheduler.request (key -> generation)
        self.hot_queued = []      # every key HOT put on its own queue
        self.floor_jobs = []      # every _FloorRequest that really ran
        self.phase = "setup"
        self.current_floor_request = None
        self.switch_t = None
        self.frame_done_t = None
        self._seq = 0
        self._lock = threading.Lock()

        real_compute = cc.CorrectionCompute.compute
        real_array = cc.CorrectionCompute.correct_array
        real_calib = ev.ExploreController._calibrate_level_gains
        real_floor = ev.ExploreController._run_floor_job
        real_request = sch.TileScheduler.request
        real_queue = None

        def compute(inner, key):
            prev = getattr(_LOCAL, "purpose", None)
            prev_key = getattr(_LOCAL, "key", None)
            _LOCAL.purpose, _LOCAL.key = "tile", key
            with self._lock:
                self.tile_computes.append({
                    "seq": self._seq, "t_ms": now_ms(), "phase": self.phase,
                    "key": key_id(key), "fields": key_fields(key),
                })
            try:
                return real_compute(inner, key)
            finally:
                _LOCAL.purpose, _LOCAL.key = prev, prev_key

        def correct_array(inner, arr, method, param):
            purpose = getattr(_LOCAL, "purpose", None)
            key = getattr(_LOCAL, "key", None)
            ctx = None
            if purpose is None or purpose == "gain_calibration":
                # The floor job's own `correct_array` and its folded-in gain
                # calibration both run on the floor worker thread, which the
                # GUI-thread wrapper's thread-local cannot reach. Exactly ONE
                # floor job is ever in flight (`_floor_job_running`), so the
                # request recorded when that job was dispatched identifies it
                # without ambiguity -- including whether it is the user's
                # foreground floor or a background preparation.
                job = self.current_floor_request
                if threading.current_thread().name.startswith(
                        "explore-floor-compute") and job is not None:
                    kind = ("foreground_floor" if job.foreground
                            else "background_floor")
                    purpose = (f"{kind}:gain_calibration"
                               if purpose == "gain_calibration" else kind)
                    ctx = ("|".join(str(part) for part in job.ctx)
                           + f"|L{job.floor_level}|s{job.stride}")
                elif purpose is None:
                    purpose = "UNATTRIBUTED"
            with self._lock:
                self._seq += 1
                self.events.append({
                    "seq": self._seq,
                    "t_ms": now_ms(),
                    "thread": threading.current_thread().name,
                    "purpose": purpose,
                    "phase": self.phase,
                    "after_switch": (self.switch_t is not None
                                     and time.perf_counter() >= self.switch_t),
                    "after_frame_complete": (self.frame_done_t is not None
                                             and time.perf_counter() >= self.frame_done_t),
                    "method": method,
                    "effective_param": param,
                    "array_shape": list(getattr(arr, "shape", ()) or ()),
                    "tile_key": key_id(key) if key is not None else None,
                    "tile_fields": key_fields(key),
                    "floor_ctx": ctx,
                })
            return real_array(inner, arr, method, param)

        def calibrate(inner, provider, compute_obj, channel, method,
                      base_params, overview_arr, overview_level):
            prev = getattr(_LOCAL, "purpose", None)
            _LOCAL.purpose = "gain_calibration"
            try:
                return real_calib(inner, provider, compute_obj, channel,
                                  method, base_params, overview_arr,
                                  overview_level)
            finally:
                _LOCAL.purpose = prev

        def run_floor_job(inner, request):
            with self._lock:
                self.floor_jobs.append({
                    "t_ms": now_ms(), "phase": self.phase,
                    "foreground": bool(request.foreground),
                    "channel": request.channel, "method": request.method,
                    "base_params": list(request.base_params),
                    "floor_level": request.floor_level,
                    "stride": request.stride,
                    "ctx": [str(part) for part in request.ctx],
                    "generation": request.generation,
                })
            # Only one floor job runs at a time; `correct_array` on the floor
            # worker thread reads this to name itself.
            self.current_floor_request = request
            return real_floor(inner, request)

        def request(inner, req, callback):
            with self._lock:
                self.requests.append({
                    "t_ms": now_ms(), "phase": self.phase,
                    "generation": str(req.generation),
                    "priority": req.priority,
                    "key": key_id(req.key) if hasattr(req.key, "method") else None,
                    "raw": not hasattr(req.key, "method"),
                })
            return real_request(inner, req, callback)

        cc.CorrectionCompute.compute = compute
        cc.CorrectionCompute.correct_array = correct_array
        ev.ExploreController._calibrate_level_gains = calibrate
        ev.ExploreController._run_floor_job = run_floor_job
        sch.TileScheduler.request = request

        # HOT's own queue, so "what the background PLANNED" is recorded
        # from the real coordinator rather than recomputed here.
        from block01.viewer import multichannel_prefetch as mcp
        real_queue = mcp.MultiChannelPrefetchController._queue_channel_tiles

        def queue_channel_tiles(inner, snapshot, channel, hot_index, generation):
            before = len(inner._tile_queue)
            out = real_queue(inner, snapshot, channel, hot_index, generation)
            added = list(inner._tile_queue)[before:]
            with self._lock:
                for gen, _prio, key in added:
                    self.hot_queued.append({
                        "t_ms": now_ms(), "phase": self.phase,
                        "generation": str(gen), "channel": channel,
                        "key": key_id(key), "fields": key_fields(key),
                        "snapshot_level": snapshot.level,
                        "snapshot_bbox_l0": list(snapshot.bbox_l0 or ()),
                    })
            return out

        mcp.MultiChannelPrefetchController._queue_channel_tiles = queue_channel_tiles

    # ── slicing ─────────────────────────────────────────────────────────
    def mark(self):
        with self._lock:
            return (len(self.events), len(self.tile_computes),
                    len(self.requests), len(self.hot_queued),
                    len(self.floor_jobs))

    def since(self, mark):
        with self._lock:
            return {
                "events": self.events[mark[0]:],
                "tile_computes": self.tile_computes[mark[1]:],
                "requests": self.requests[mark[2]:],
                "hot_queued": self.hot_queued[mark[3]:],
                "floor_jobs": self.floor_jobs[mark[4]:],
            }


def drain(ms=400, step=10):
    end = time.perf_counter() + ms / 1000.0
    while time.perf_counter() < end:
        APP.processEvents()
        QtTest.QTest.qWait(step)


# ── the foreground's OWN planning rules, read off the live controller ────

def planned_sets(controller, channel, method, params):
    """Exactly what `_issue_settled_request` would ask for, for a selection.

    Uses the controller's own helpers and its own constants -- no second
    coordinate formula lives here.
    """
    from block01.viewer import explore_view as ev
    from block01.viewer import request_planning as planning
    from block01.viewer.tile_types import (CorrectionKey, TileAddress,
                                           effective_param, tiles_covering)

    bbox = controller._current_bbox
    grid = controller.grid
    tile_size = grid.tile_size
    level = controller.level
    source = controller.provider.source_identity()

    def make(lvl, tx, ty):
        ds = controller.provider.level_downsample(lvl)
        eff = tuple(effective_param(p, lvl, ds) for p in tuple(params or ()))
        return CorrectionKey(
            source=source, channel=str(channel),
            tile=TileAddress(grid=grid, level=lvl, tx=tx, ty=ty),
            method=method, params=eff,
            algorithm_version=ev.BG_CORRECTION_ALGO_VERSION,
            quality=controller.quality)

    ds = controller.provider.level_downsample(level)
    current = tiles_covering(planning.bbox_to_level(bbox, ds), tile_size)

    inner, ring = set(), set()
    flevel = level + 1
    if flevel < controller.provider.num_levels:
        fds = controller.provider.level_downsample(flevel)
        fbbox = (int(bbox[0] / fds), int(bbox[1] / fds),
                 int(bbox[2] / fds), int(bbox[3] / fds))
        inner = tiles_covering(fbbox, tile_size)
        fh, fw = controller.provider.level_shape(flevel)
        pad = ev.FALLBACK_HALO_TILES * tile_size
        fpad = (max(0, fbbox[0] - pad), max(0, fbbox[1] - pad),
                min(fh, fbbox[2] + pad), min(fw, fbbox[3] + pad))
        ring = tiles_covering(fpad, tile_size) - inner

    floor_level, stride = controller._pick_floor_level_and_stride()
    floor_ctx = controller._floor_ctx_for(channel, method, params,
                                          floor_level, stride)
    return {
        "level": level,
        "bbox_l0": list(bbox or ()),
        "tile_size": tile_size,
        "fallback_level": flevel if flevel < controller.provider.num_levels else None,
        "current_keys": {key_id(make(level, tx, ty)): key_fields(make(level, tx, ty))
                         for tx, ty in sorted(current)},
        "fallback_inner_keys": {key_id(make(flevel, tx, ty)): key_fields(make(flevel, tx, ty))
                                for tx, ty in sorted(inner)},
        "fallback_ring_keys": {key_id(make(flevel, tx, ty)): key_fields(make(flevel, tx, ty))
                               for tx, ty in sorted(ring)},
        "floor_key": {
            "ctx": [str(part) for part in floor_ctx] if floor_ctx else None,
            "floor_level": floor_level, "stride": stride,
            "cached": controller.has_cached_floor(channel, method, params),
        },
    }


def resident(controller, key_ids):
    """Which of these key ids are in the SHARED corrected cache right now.

    Read off `LRUByteCache._store` directly: `get()` would move the entry to
    the MRU end and count a hit, i.e. the probe would change what it probes.
    """
    store = controller.scheduler.corrected_cache._store
    live = {key_id(k) for k in list(store.keys()) if hasattr(k, "method")}
    return sorted(set(key_ids) & live), sorted(set(key_ids) - live)


def frame_state(controller, channel, method, params, plan):
    """What the user-visible frame still owes: floor + current-level cover.

    Reported per poll tick so the moment the frame completes can be read
    against the moment each level+1 tile was computed, instead of assuming
    which of the two came first.
    """
    floor_level, stride = controller._pick_floor_level_and_stride()
    want_ctx = controller._floor_ctx_for(channel, method, params,
                                         floor_level, stride)
    floor_ok = bool(controller._floor_ready
                    and controller._floor_ctx == want_ctx)
    _hits, cur_missing = resident(controller, plan["current_keys"].keys())
    _ih, inner_missing = resident(controller,
                                  plan["fallback_inner_keys"].keys())
    _rh, ring_missing = resident(controller, plan["fallback_ring_keys"].keys())
    pooled = sum(
        1 for tx, ty in _pool_coords(plan)
        if controller._precise_pool.get(plan["level"], tx, ty) is not None)
    return {
        "t_ms": now_ms(),
        "floor_ready": bool(controller._floor_ready),
        "floor_ctx_matches": floor_ok,
        "current_missing_in_cache": len(cur_missing),
        "current_pooled_on_screen": pooled,
        "current_total": len(plan["current_keys"]),
        "fallback_inner_missing": len(inner_missing),
        "fallback_ring_missing": len(ring_missing),
        "complete": floor_ok and not cur_missing and pooled >= len(
            plan["current_keys"]),
    }


def _pool_coords(plan):
    return [(f["tx"], f["ty"]) for f in plan["current_keys"].values()]


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
    assert tab.show_source(ORDER[0], "tophat", (TOPHAT_RADIUS,)) is True
    drain(500)
    tab.stack.controller.jump_to(*VIEWPORT)
    drain(4000)
    return tab


def trajectory(tab, rec, channel, shown, other, dwell_ms=3000):
    """One 'arrive on a new channel, settle, flip to the other method' run."""
    controller = tab.stack.controller
    out = {"channel": channel, "shown_method": shown, "flip_to": other}

    # ── arrive, and let the background prepare ────────────────────────
    rec.phase = f"arrive:{channel}:{shown}"
    rec.switch_t = None
    rec.frame_done_t = None
    mark = rec.mark()
    tab.show_source(channel, shown,
                    () if PARAM[shown] is None else (PARAM[shown],))
    drain(dwell_ms)
    out["arrive"] = summarize(rec.since(mark))
    out["hot_stats_after_arrive"] = dict(tab.hot_stats() or {})

    # ── what the flip is ABOUT to need, and what is already resident ──
    other_params = () if PARAM[other] is None else (PARAM[other],)
    plan = planned_sets(controller, channel, other, other_params)
    out["plan_at_switch_instant"] = plan
    for name in ("current_keys", "fallback_inner_keys", "fallback_ring_keys"):
        hits, misses = resident(controller, plan[name].keys())
        out[f"{name}_resident"] = hits
        out[f"{name}_missing"] = misses
    out["floor_cached_before_switch"] = plan["floor_key"]["cached"]

    # HOT's own level+1 plan for this channel, from the real coordinator
    hot_fallback = sorted({
        row["key"] for row in rec.hot_queued
        if row["channel"] == channel
        and row["fields"]["level"] == (plan["fallback_level"] or -1)
        and row["fields"]["method"] == other})
    out["hot_queued_fallback_keys"] = hot_fallback
    hot_hits, hot_miss = resident(controller, hot_fallback)
    out["hot_queued_fallback_resident"] = hot_hits
    out["hot_queued_fallback_not_resident"] = hot_miss

    fg_inner = set(plan["fallback_inner_keys"])
    fg_ring = set(plan["fallback_ring_keys"])
    hot_set = set(hot_fallback)
    out["fallback_set_algebra"] = {
        "foreground_inner_count": len(fg_inner),
        "foreground_ring_count": len(fg_ring),
        "hot_planned_count": len(hot_set),
        "hot_minus_foreground_inner": sorted(hot_set - fg_inner),
        "foreground_inner_minus_hot": sorted(fg_inner - hot_set),
        "foreground_ring_minus_hot": sorted(fg_ring - hot_set),
        "inner_equal": hot_set == fg_inner,
    }

    # ── the flip itself: foreground critical path, then the rest ──────
    rec.phase = f"flip:{channel}:{other}"
    hits_before = controller.stats.get("floor_cache_hits", 0)
    rec.switch_t = time.perf_counter()
    rec.frame_done_t = None
    mark = rec.mark()
    tab.show_source(channel, other, other_params)

    # THE FIRST OBSERVATION IS THE DECISIVE ONE: `show_source()` has
    # returned, so whatever it did synchronously (install the cached floor,
    # keep the pooled current-level tiles) is already done. A later poll can
    # only be late -- the GUI thread is busy running the worker results --
    # so the frame's completion is timed here, not by the loop below.
    tick0 = frame_state(controller, channel, other, other_params, plan)
    if tick0["complete"]:
        rec.frame_done_t = time.perf_counter()
    ticks = [tick0]
    deadline = time.perf_counter() + dwell_ms / 1000.0
    while time.perf_counter() < deadline:
        APP.processEvents()
        QtTest.QTest.qWait(10)
        state = frame_state(controller, channel, other, other_params, plan)
        if len(ticks) < 40:
            ticks.append(state)
        if rec.frame_done_t is None and state["complete"]:
            rec.frame_done_t = time.perf_counter()
    out["frame_ticks"] = ticks
    out["switch_t_ms"] = round((rec.switch_t - T0) * 1000.0, 3)
    out["frame_complete_ms"] = (
        None if rec.frame_done_t is None
        else round((rec.frame_done_t - rec.switch_t) * 1000.0, 1))
    out["flip"] = summarize(rec.since(mark))

    # ── the exit gate's own accounting, split by what the FRAME needed ──
    events = out["flip"]["attribution_table"]
    needed = set(plan["current_keys"]) | set(plan["fallback_inner_keys"])
    ring = set(plan["fallback_ring_keys"])
    out["gate_accounting"] = {
        # the gate: "the first foreground method switch adds 0 correct_array
        # for the FLOOR". A floor correction is the floor job's own
        # `correct_array` plus the gain calibration folded into that job.
        "floor_correct_array_new": sum(
            1 for e in events if "floor" in e["purpose"]),
        "floor_jobs_run": len(out["flip"]["floor_jobs"]),
        "floor_cache_hits_delta": (controller.stats.get("floor_cache_hits", 0)
                                   - hits_before),
        # work for a key the completed frame actually needed
        "correct_array_for_keys_the_frame_needed": sum(
            1 for e in events if e["tile_key"] in needed),
        # work for the speculative level+1 look-ahead ring
        "correct_array_for_lookahead_ring": sum(
            1 for e in events if e["tile_key"] in ring),
        "correct_array_for_neither": sum(
            1 for e in events
            if e["tile_key"] not in needed and e["tile_key"] not in ring),
        "frame_complete_at_show_source_return": bool(tick0["complete"]),
        "ring_still_missing_when_frame_complete":
            tick0["fallback_ring_missing"],
    }
    out["hot_stats_after_flip"] = dict(tab.hot_stats() or {})
    out["selection_after_flip"] = {
        "channel": controller.channel, "method": controller.method,
        "params": list(controller.params), "level": controller.level,
    }
    rec.switch_t = None
    rec.frame_done_t = None
    return out


def summarize(slice_):
    events = slice_["events"]
    by_purpose = collections.Counter(e["purpose"] for e in events)
    before = [e for e in events if not e["after_frame_complete"]]
    after = [e for e in events if e["after_frame_complete"]]
    return {
        "correct_array_total": len(events),
        "correct_array_by_purpose": dict(by_purpose),
        "correct_array_before_frame_complete": len(before),
        "correct_array_before_frame_complete_by_purpose":
            dict(collections.Counter(e["purpose"] for e in before)),
        "correct_array_after_frame_complete": len(after),
        "correct_array_after_frame_complete_by_purpose":
            dict(collections.Counter(e["purpose"] for e in after)),
        "tile_computes": len(slice_["tile_computes"]),
        "tile_computes_by_level": dict(collections.Counter(
            row["fields"]["level"] for row in slice_["tile_computes"])),
        "floor_jobs": slice_["floor_jobs"],
        "requests_by_generation": dict(collections.Counter(
            row["generation"] for row in slice_["requests"])),
        "attribution_table": events,
    }


def main():
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                       else "b121_attribution.json")
    label = sys.argv[2] if len(sys.argv) > 2 else "g32b4b121"
    from block01.viewer import explore_view as _ev
    rec = Recorder()
    tab = build_tab()
    controller = tab.stack.controller

    report = {
        "label": label,
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "slide": SLIDE,
        "viewport_l0": list(VIEWPORT),
        "num_levels": controller.provider.num_levels,
        "tile_size": controller.grid.tile_size,
        "floor_level_stride": list(controller._pick_floor_level_and_stride()),
        "fallback_halo_tiles": _ev.FALLBACK_HALO_TILES,
        "honesty": [
            "CorrectionCompute.compute() calls correct_array() internally, and so "
            "does _calibrate_level_gains(); purposes below come from wrapping "
            "those entry points, not from counting correct_array alone",
            "the OS page cache was NOT dropped; nothing here is a cold disk read",
            "the demo slide is opened read-only",
        ],
        "trajectories": [],
    }

    for index, (shown, other) in enumerate((("tophat", "cucim"),
                                            ("cucim", "tophat"),
                                            (None, "tophat"),
                                            (None, "cucim"))):
        report["trajectories"].append(
            trajectory(tab, rec, ORDER[1 + index], shown, other))

    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"wrote {out}")
    for traj in report["trajectories"]:
        flip = traj["flip"]
        print(f"{traj['channel']} {traj['shown_method']}->{traj['flip_to']}: "
              f"correct_array={flip['correct_array_total']} "
              f"{flip['correct_array_by_purpose']} "
              f"pre-frame={flip['correct_array_before_frame_complete_by_purpose']} "
              f"inner_miss={len(traj['fallback_inner_keys_missing'])} "
              f"ring_miss={len(traj['fallback_ring_keys_missing'])} "
              f"cur_miss={len(traj['current_keys_missing'])} "
              f"floor_cached={traj['floor_cached_before_switch']}")
    try:
        tab.teardown()
    except Exception:                                       # noqa: BLE001
        pass


if __name__ == "__main__":
    main()
