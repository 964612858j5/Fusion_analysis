"""G3.2b.4B1.1: where the wait after a Step0 channel switch actually is.

Real `Step0ExploreTab` over the REAL demo slide, with the production
`build_default_stack` (real `RawTileProvider`, real `CorrectionCompute` on
the GPU, real `TileScheduler`, real `ExploreController`) and the production
`MultiChannelPrefetchController` mounted exactly as `Step0Page` mounts it.

The point is to separate four costs that a single "it still waits" hides:

* the CURRENT VIEWPORT's corrected tiles at the display level -- what HOT
  actually prepares;
* the LEVEL+1 FALLBACK batch the controller issues for the same viewport;
* the CORRECTED FLOOR (`compute.correct_array`), which has no cache at all
  and is recomputed per (channel, method, params);
* the OVERVIEW read HOT gates itself on.

Honest limits, stated once:

* the OS page cache is NOT dropped; nothing here is called a cold disk read.
* the demo project and slide are opened READ-ONLY.
* "resident" means the corrected tile is in the corrected LRU -- not a
  photon on a display, and never called one.

Usage:
    python scripts/benchmark_step0_channel_switch_readiness.py OUT.json [label]
"""

import collections
import importlib.util
import json
import os
import pathlib
import sys
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

PROJECT = pathlib.Path("/sda1/Fusion/tmp/rois/full_wsi_20260813_023722_f270")
SLIDE = "/sda1/Fusion/benchmark/biopsy.ome.tif"
TOPHAT_RADIUS = 15
CUCIM_SIGMA = 50
#: Channels the user can switch between, in list order.
ORDER = ["CD22", "CD4", "PD1", "FOXP3", "TCF1", "TIM3", "TOX", "CD45RA"]
VIEWPORT = (9000, 9000, 2048, 2048)


def drain(ms=300, step=10):
    end = time.perf_counter() + ms / 1000.0
    while time.perf_counter() < end:
        APP.processEvents()
        QtTest.QTest.qWait(step)


class Counters:
    """Corrected TILE work by level, corrected FLOOR work, provider reads."""

    def __init__(self):
        from block01.viewer import correction_compute as cc
        from block01.viewer import raw_tile_provider as rtp

        self.tiles = []            # CorrectionKey
        self.floors = []           # (method, param)
        self.reads = []            # (channel, level)
        real_compute = cc.CorrectionCompute.compute
        real_array = cc.CorrectionCompute.correct_array
        real_read = rtp.RawTileProvider.read_region

        def compute(inner, key):
            self.tiles.append(key)
            return real_compute(inner, key)

        def correct_array(inner, arr, method, param):
            start = time.perf_counter()
            try:
                return real_array(inner, arr, method, param)
            finally:
                self.floors.append((method, param,
                                    round((time.perf_counter() - start) * 1000, 2)))

        def read_region(inner, channel, level, y0, y1, x0, x1):
            self.reads.append((str(channel), int(level)))
            return real_read(inner, channel, level, y0, y1, x0, x1)

        cc.CorrectionCompute.compute = compute
        cc.CorrectionCompute.correct_array = correct_array
        rtp.RawTileProvider.read_region = read_region

    def mark(self):
        return (len(self.tiles), len(self.floors), len(self.reads))

    def since(self, mark):
        return (self.tiles[mark[0]:], self.floors[mark[1]:], self.reads[mark[2]:])


def build_tab(counters):
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
    drain(400)
    tab.stack.controller.jump_to(*VIEWPORT)
    drain(4000)
    return tab


def visible_keys(stack, channel, method, base_param, level=None):
    from block01.viewer.tile_types import (
        CorrectionKey, TileAddress, effective_param)
    controller = stack.controller
    snapshot = controller.snapshot()
    level = snapshot.level if level is None else level
    downsample = controller.provider.level_downsample(level)
    param = effective_param(base_param, level, downsample)
    return [CorrectionKey(source=snapshot.source, channel=channel,
                          tile=TileAddress(grid=controller.grid, level=level,
                                           tx=tx, ty=ty),
                          method=method, params=(param,),
                          algorithm_version=snapshot.algorithm_version,
                          quality=snapshot.quality)
            for tx, ty in sorted(snapshot.visible_tiles)]


def resident(stack, channel, method, base_param, level=None):
    cache = stack.scheduler.corrected_cache
    keys = visible_keys(stack, channel, method, base_param, level)
    return sum(1 for key in keys if cache.get(key) is not None), len(keys)


def sample_readiness(tab, channel, seconds=3.0, step_ms=100):
    """Every `step_ms`, how much of the current viewport each method has."""
    stack = tab.stack
    samples = []
    start = time.perf_counter()
    while (time.perf_counter() - start) < seconds:
        APP.processEvents()
        QtTest.QTest.qWait(step_ms)
        samples.append({
            "ms": round((time.perf_counter() - start) * 1000.0, 1),
            "tophat": list(resident(stack, channel, "tophat", TOPHAT_RADIUS)),
            "cucim": list(resident(stack, channel, "cucim", CUCIM_SIGMA)),
        })
    return samples


def one_switch(tab, counters, shown_method, new_channel, dwell_s):
    """Show `shown_method`, click `new_channel`, dwell, then switch method."""
    stack = tab.stack
    hot = tab.hot
    param = {"tophat": TOPHAT_RADIUS, "cucim": CUCIM_SIGMA,
             None: None}[shown_method]
    other = "cucim" if shown_method != "cucim" else "tophat"
    other_param = CUCIM_SIGMA if other == "cucim" else TOPHAT_RADIUS

    events = []
    controller = stack.controller
    t0 = [0.0]

    def on_interaction(kind, _snap):
        events.append((kind, round((time.perf_counter() - t0[0]) * 1000.0, 1)))

    def on_quiet(_snap):
        events.append(("gesture_quiet",
                       round((time.perf_counter() - t0[0]) * 1000.0, 1)))

    controller.interaction_event.connect(on_interaction)
    controller.gesture_quiet.connect(on_quiet)
    hot_batches_before = hot.stats["hot_batches"] if hot else 0
    active_before = len(hot._active_requests) if hot else 0
    mark = counters.mark()

    t0[0] = time.perf_counter()
    tab.show_source(new_channel, shown_method,
                    () if param is None else (param,))
    samples = sample_readiness(tab, new_channel, seconds=dwell_s)
    if hot is not None and hot.stats["hot_batches"] > hot_batches_before:
        events.append(("hot_confirm_batches",
                       hot.stats["hot_batches"] - hot_batches_before))
    plan = [name for name, _i in (hot._overview_plan if hot else [])]

    tiles, floors, reads = counters.since(mark)
    dwell_report = {
        "shown_method": shown_method, "new_channel": new_channel,
        "dwell_s": dwell_s,
        "events_ms": events,
        "hot_plan_after_dwell": plan,
        "hot_plan_centre_first": bool(plan and plan[0] == new_channel),
        "hot_active_requests_before_click": active_before,
        "hot_stats": dict(hot.stats) if hot else None,
        "readiness_samples": samples,
        "final_resident": {
            "tophat": list(resident(stack, new_channel, "tophat", TOPHAT_RADIUS)),
            "cucim": list(resident(stack, new_channel, "cucim", CUCIM_SIGMA)),
        },
        "during_dwell": {
            "corrected_tile_computes": len(tiles),
            "corrected_tile_computes_by_level": dict(collections.Counter(
                k.tile.level for k in tiles)),
            "corrected_floor_computes": len(floors),
            "corrected_floor_ms": round(sum(f[2] for f in floors), 1),
            "provider_reads": len(reads),
        },
    }

    # ...now the method switch the user complains about.
    mark = counters.mark()
    cache = stack.scheduler.corrected_cache
    hits_before = cache.stats()["hits"]
    wanted = set(visible_keys(stack, new_channel, other, other_param))
    switch_start = time.perf_counter()
    tab.show_source(new_channel, other, (other_param,))
    deadline = switch_start + 60.0
    ready_ms = None
    while time.perf_counter() < deadline:
        APP.processEvents()
        have, want = resident(stack, new_channel, other, other_param)
        if want and have == want:
            ready_ms = (time.perf_counter() - switch_start) * 1000.0
            break
        QtTest.QTest.qWait(5)
    drain(500)
    tiles, floors, reads = counters.since(mark)
    recomputed = [k for k in tiles if k in wanted]
    controller.interaction_event.disconnect(on_interaction)
    controller.gesture_quiet.disconnect(on_quiet)

    dwell_report["switch"] = {
        "to_method": other,
        "ms_until_viewport_resident": (None if ready_ms is None
                                       else round(ready_ms, 2)),
        "viewport_tiles_recomputed": len(recomputed),
        "corrected_tile_computes": len(tiles),
        "corrected_tile_computes_by_level": dict(collections.Counter(
            k.tile.level for k in tiles)),
        "corrected_floor_computes": len(floors),
        "corrected_floor_ms": round(sum(f[2] for f in floors), 1),
        "provider_reads": len(reads),
        "cache_hits_delta": cache.stats()["hits"] - hits_before,
    }
    return dwell_report


def main():
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                       else "channel_switch.json")
    label = sys.argv[2] if len(sys.argv) > 2 else "g324b11"
    counters = Counters()
    tab = build_tab(counters)
    report = {
        "label": label, "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "slide": SLIDE, "project": str(PROJECT), "viewport_l0": list(VIEWPORT),
        "honesty": [
            "the OS page cache was NOT dropped; nothing here is a cold disk read",
            "'resident' means the corrected tile is in the corrected LRU",
            "the corrected floor has no cache and is counted separately",
            "the demo slide and project are opened read-only",
        ],
        "runs": [],
    }
    index = 1
    for shown in ("tophat", "cucim", None):
        for dwell in (1.0, 2.0, 3.0):
            channel = ORDER[index % len(ORDER)]
            index += 1
            report["runs"].append(
                one_switch(tab, counters, shown, channel, dwell))
            out.write_text(json.dumps(report, indent=2, default=str))
            drain(600)
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"wrote {out}")
    try:
        tab.teardown()
    except Exception:                                       # noqa: BLE001
        pass


if __name__ == "__main__":
    main()
