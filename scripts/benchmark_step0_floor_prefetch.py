"""G3.2b.4B1.2: what the EXISTING floor cache already does, and what it misses.

`ExploreController` has had a floor cache since `7d7bb5a` -- an 8-entry
`OrderedDict` keyed by `(ctx, floor_level, stride)` where `ctx` is
`(source, channel, method, effective_params)`. This measures the three
things that matters for:

  A  a channel the user has just switched to: the OTHER method's floor has
     never been computed, so the first flip to it computes one;
  B  Full Image -> Compare: the cache is per CONTROLLER, so the strip's
     three controllers cannot see the full image's floors at all;
  C  flipping back and forth on a channel whose floors are already in that
     controller's cache: the existing cache already answers, and this
     proves the gap is (A) and (B), not the cache itself.

COUNTING (corrected G3.2b.4B1.2.1). `CorrectionCompute.compute()` -- the
corrected TILE path -- calls `self.correct_array()` internally
(`viewer/correction_compute.py:130`), and the gain calibration folded into
every floor job calls it once per window per pyramid level. A bare
`correct_array` counter therefore reports one level+1 TILE as one "floor
correction": that is how the first revision of this script reported "12
floor `correct_array` calls" for a flip whose floor was a cache HIT and
whose floor job count was zero. Floor work is counted at the floor's own
entry point, `ExploreController._run_floor_job`, and tile work at
`compute()`; the nesting is subtracted rather than double counted.

Honest limits: the OS page cache is NOT dropped; the demo project and slide
are read-only; "hit" means the floor came from the existing OrderedDict.

Usage: python scripts/benchmark_step0_floor_prefetch.py OUT.json [label]
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
ORDER = ["CD22", "CD4", "PD1", "FOXP3", "TCF1", "TIM3"]
VIEWPORT = (9000, 9000, 2048, 2048)
PARAM = {"tophat": TOPHAT_RADIUS, "cucim": CUCIM_SIGMA, None: None}


def drain(ms=400, step=10):
    end = time.perf_counter() + ms / 1000.0
    while time.perf_counter() < end:
        APP.processEvents()
        QtTest.QTest.qWait(step)


class Counters:
    """Tile work, floor work and every `correct_array`, kept apart.

    `floor_jobs` is the floor's OWN entry point. `arrays` is every
    `correct_array` call there is -- including the ones `compute()` and the
    gain calibration make internally -- and is reported only so the
    difference between the three numbers stays visible instead of being
    quietly collapsed into one.
    """

    def __init__(self):
        from block01.viewer import correction_compute as cc
        from block01.viewer import explore_view as ev
        self.tiles = []
        self.arrays = []
        self.floor_jobs = []
        real_compute = cc.CorrectionCompute.compute
        real_array = cc.CorrectionCompute.correct_array
        real_floor = ev.ExploreController._run_floor_job

        def compute(inner, key):
            self.tiles.append(key)
            return real_compute(inner, key)

        def correct_array(inner, arr, method, param):
            self.arrays.append((method, param))
            return real_array(inner, arr, method, param)

        def run_floor_job(inner, request):
            self.floor_jobs.append(request)
            return real_floor(inner, request)

        cc.CorrectionCompute.compute = compute
        cc.CorrectionCompute.correct_array = correct_array
        ev.ExploreController._run_floor_job = run_floor_job

    def mark(self):
        return (len(self.tiles), len(self.arrays), len(self.floor_jobs))

    def since(self, mark):
        return (self.tiles[mark[0]:], self.arrays[mark[1]:],
                self.floor_jobs[mark[2]:])


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
    drain(500)
    tab.stack.controller.jump_to(*VIEWPORT)
    drain(4000)
    return tab


def _floor_stats(controller):
    return {
        "floor_cache_items": len(controller._floor_cache),
        "floor_cache_limit": controller._floor_cache_limit,
        "floor_cache_hits": controller.stats.get("floor_cache_hits", 0),
    }


def switch_and_count(tab, counters, channel, method, dwell_s=3.0):
    """Show (channel, method); return what it cost."""
    controller = tab.stack.controller
    param = PARAM[method]
    mark = counters.mark()
    hits_before = controller.stats.get("floor_cache_hits", 0)
    level = controller.level
    start = time.perf_counter()
    tab.show_source(channel, method, () if param is None else (param,))
    # The frame the user asked for is finished when `show_source` returns:
    # the cached floor is installed synchronously and the pooled tiles are
    # already pooled. Read here, before the dwell, so background work that
    # runs AFTER the picture is complete cannot be charged to the switch.
    at_return = {
        "floor_ready": bool(controller._floor_ready),
        "floor_jobs": len(counters.since(mark)[2]),
        "tile_computes": len(counters.since(mark)[0]),
    }
    drain(int(dwell_s * 1000))
    tiles, arrays, floor_jobs = counters.since(mark)
    covering = _fallback_covering(controller, level)
    return {
        "channel": channel, "method": method,
        "wall_ms": round((time.perf_counter() - start) * 1000.0, 1),
        "at_show_source_return": at_return,
        "tile_computes": len(tiles),
        "tile_computes_by_level": dict(collections.Counter(
            k.tile.level for k in tiles)),
        # The level+1 band, split the way the foreground itself splits it:
        # the tiles COVERING the viewport, and the speculative one-tile
        # look-ahead RING (`FALLBACK_HALO_TILES`) queued below the current
        # level for a pan that has not happened.
        "fallback_covering_computes": sum(
            1 for k in tiles
            if k.tile.level == level + 1
            and (k.tile.tx, k.tile.ty) in covering),
        "fallback_ring_computes": sum(
            1 for k in tiles
            if k.tile.level == level + 1
            and (k.tile.tx, k.tile.ty) not in covering),
        # FLOOR work, at the floor's own entry point -- not `correct_array`.
        "floor_jobs_run": len(floor_jobs),
        "floor_jobs_foreground": sum(1 for r in floor_jobs if r.foreground),
        "floor_jobs_background": sum(1 for r in floor_jobs
                                     if not r.foreground),
        # Reported so the nesting stays visible: this counts the tile path's
        # and the gain calibration's internal calls too.
        "correct_array_calls_all_causes": len(arrays),
        "floor_cache_hits_delta": (controller.stats.get("floor_cache_hits", 0)
                                   - hits_before),
        "floor_cache": _floor_stats(controller),
    }


def _fallback_covering(controller, level):
    """The level+1 tiles COVERING the current viewport, host's own rule."""
    from block01.viewer import request_planning as planning
    from block01.viewer.tile_types import tiles_covering

    flevel = int(level) + 1
    bbox = controller._current_bbox
    if bbox is None or flevel >= controller.provider.num_levels:
        return set()
    ds = controller.provider.level_downsample(flevel)
    return tiles_covering(planning.bbox_to_level(bbox, ds),
                          controller.grid.tile_size)


def main():
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "floor.json")
    label = sys.argv[2] if len(sys.argv) > 2 else "g324b12"
    counters = Counters()
    tab = build_tab(counters)
    controller = tab.stack.controller
    report = {
        "label": label, "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "slide": SLIDE, "project": str(PROJECT), "viewport_l0": list(VIEWPORT),
        "floor_level_stride": list(controller._pick_floor_level_and_stride()),
        "floor_cache_limit": controller._floor_cache_limit,
        "honesty": [
            "the floor cache ALREADY EXISTS (explore_view `_floor_cache`, 8 entries)",
            "the OS page cache was NOT dropped; nothing here is a cold disk read",
            "'hit' means the floor came from that existing OrderedDict",
            "the demo slide and project are read-only",
            "`correct_array` is called INSIDE CorrectionCompute.compute() and "
            "inside the gain calibration, so floor work is counted at "
            "_run_floor_job and tile work at compute(); the earlier revision "
            "of this script conflated the three",
        ],
    }

    # ── A. a NEW channel: the other method's floor has never been made ──
    report["A_new_channel_first_flip"] = []
    for index, (shown, other) in enumerate((("tophat", "cucim"),
                                            ("cucim", "tophat"),
                                            (None, "tophat"),
                                            (None, "cucim"))):
        channel = ORDER[1 + index]
        report["A_new_channel_first_flip"].append({
            "arrive": switch_and_count(tab, counters, channel, shown),
            "first_flip": switch_and_count(tab, counters, channel, other),
        })

    # ── C. flipping back and forth on a channel already computed ───────
    channel = ORDER[1]
    report["C_repeat_round_trip"] = [
        switch_and_count(tab, counters, channel, "tophat", dwell_s=1.5),
        switch_and_count(tab, counters, channel, "cucim", dwell_s=1.5),
        switch_and_count(tab, counters, channel, "tophat", dwell_s=1.5),
        switch_and_count(tab, counters, channel, "cucim", dwell_s=1.5),
    ]

    # ── B. Full Image -> Compare: whose floor cache is it? ─────────────
    from block01.ui.step0 import compare_strip as cs
    from block01.ui.step0 import step0_page as sp
    from block01.core.io_loader import OMETIFFLoader

    page = sp.Step0Page()
    page.loader = OMETIFFLoader(SLIDE)
    page.ome_path = SLIDE
    page.patches = []
    page.nucleus_channel = "DAPI"
    page._rebuild_channel_list()
    page.current_channel = channel
    page.resize(1400, 900)
    page.show()
    APP.processEvents()
    strip = page._compare_strip_widget
    strip._stack_factory = cs.build_compare_stacks
    strip.set_dataset(SLIDE)
    mark = counters.mark()
    start = time.perf_counter()
    stacks = strip.ensure_built(
        channel, params_for=page._compare_params_for,
        tint=page._full_image_tint(), nucleus=page._full_image_nucleus_args(),
        viewport_l0=VIEWPORT,
        overview_store=getattr(tab.stack, "overview_store", None),
        caches=getattr(tab.stack, "caches", None),
        floor_cache=getattr(tab.stack, "floor_cache", None))
    drain(8000)
    tiles, arrays, floor_jobs = counters.since(mark)
    report["B_full_to_compare"] = {
        "built": stacks is not None,
        "error": None if stacks is not None else strip._build_error,
        "wall_ms": round((time.perf_counter() - start) * 1000.0, 1),
        "tile_computes": len(tiles),
        "tile_computes_by_level": dict(collections.Counter(
            k.tile.level for k in tiles)),
        "floor_jobs_run": len(floor_jobs),
        "floor_jobs_background": sum(1 for r in floor_jobs
                                     if not r.foreground),
        "correct_array_calls_all_causes": len(arrays),
        "floor_cache_same_object": (
            [c._floor_cache is controller._floor_cache
             for c in stacks.controllers] if stacks is not None else None),
        "raw_cache_same_object": (
            stacks.caches[0] is tab.stack.caches[0] if stacks else None),
        "corrected_cache_same_object": (
            stacks.caches[1] is tab.stack.caches[1] if stacks else None),
    }
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"wrote {out}")
    for closer in (lambda: strip.teardown(wait_for_floor=False),
                   lambda: tab.teardown()):
        try:
            closer()
        except Exception:                                   # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
