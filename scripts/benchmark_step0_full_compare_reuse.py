"""G3.2b.4B1.1: does entering Compare reuse what Full Image already computed?

Real `Step0Page`, real `Step0ExploreTab`, the production
`build_compare_stacks`, over the REAL demo slide and its real Step0
cuCIM/tophat parameters. The two arms differ by ONE argument on the public
entry the page itself calls:

    strip.ensure_built(..., caches=None)        # `before` -- private caches
    strip.ensure_built(..., caches=full.caches) # `after`  -- borrowed

Honest limits: the OS page cache is NOT dropped, so nothing here is a cold
disk read; "hit" means the `CorrectionKey` was answered from the corrected
LRU rather than recomputed; the demo project and slide are read-only.

Usage: python scripts/benchmark_step0_full_compare_reuse.py OUT.json [label]
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
CHANNEL = "CD22"
NUCLEUS = "DAPI"
TOPHAT_RADIUS = 15
CUCIM_SIGMA = 50
VIEWPORT = (9000, 9000, 2048, 2048)


def drain(ms=400, step=10):
    end = time.perf_counter() + ms / 1000.0
    while time.perf_counter() < end:
        APP.processEvents()
        QtTest.QTest.qWait(step)


class Counters:
    def __init__(self):
        from block01.viewer import correction_compute as cc
        self.tiles = []
        self.floors = []
        real_compute = cc.CorrectionCompute.compute
        real_array = cc.CorrectionCompute.correct_array

        def compute(inner, key):
            self.tiles.append(key)
            return real_compute(inner, key)

        def correct_array(inner, arr, method, param):
            start = time.perf_counter()
            try:
                return real_array(inner, arr, method, param)
            finally:
                self.floors.append(round((time.perf_counter() - start) * 1000, 2))

        cc.CorrectionCompute.compute = compute
        cc.CorrectionCompute.correct_array = correct_array

    def mark(self):
        return (len(self.tiles), len(self.floors))

    def since(self, mark):
        return self.tiles[mark[0]:], self.floors[mark[1]:]


def build_page():
    from block01.core.io_loader import OMETIFFLoader
    from block01.ui.step0 import step0_page as sp
    from block01.viewer.prefetch_policy import ChannelCorrectionSpec

    page = sp.Step0Page()
    page.loader = OMETIFFLoader(SLIDE)
    page.ome_path = SLIDE
    page.patches = []
    page.nucleus_channel = NUCLEUS
    page._rebuild_channel_list()
    page.current_channel = CHANNEL
    page.resize(1400, 900)
    page.show()
    APP.processEvents()

    tab = page._ensure_explore_tab()
    tab.set_hot_specs_provider(lambda: [
        ChannelCorrectionSpec(channel=name, tophat_radius=TOPHAT_RADIUS,
                              cucim_sigma=CUCIM_SIGMA)
        for name in (CHANNEL, "CD4", "PD1")])
    assert tab.show_source(CHANNEL, "tophat", (TOPHAT_RADIUS,)) is True
    drain(500)
    tab.stack.controller.jump_to(*VIEWPORT)
    drain(5000)                       # let HOT prepare both methods
    return page, tab


def keys_for(stack_like, controller, channel, method, base_param):
    from block01.viewer.tile_types import (
        CorrectionKey, TileAddress, effective_param)
    snapshot = controller.snapshot()
    downsample = controller.provider.level_downsample(snapshot.level)
    param = effective_param(base_param, snapshot.level, downsample)
    return [CorrectionKey(source=snapshot.source, channel=channel,
                          tile=TileAddress(grid=controller.grid,
                                           level=snapshot.level, tx=tx, ty=ty),
                          method=method, params=(param,),
                          algorithm_version=snapshot.algorithm_version,
                          quality=snapshot.quality)
            for tx, ty in sorted(snapshot.visible_tiles)]


def run_arm(share, counters):
    from block01.ui.step0 import compare_strip as cs

    page, tab = build_page()
    full = tab.stack
    full_controller = full.controller
    cache = full.scheduler.corrected_cache

    prepared = {}
    for method, param in (("tophat", TOPHAT_RADIUS), ("cucim", CUCIM_SIGMA)):
        keys = keys_for(full, full_controller, CHANNEL, method, param)
        prepared[method] = {
            "keys": keys,
            "resident": sum(1 for k in keys if cache.get(k) is not None),
            "total": len(keys)}

    strip = page._compare_strip_widget
    strip._stack_factory = cs.build_compare_stacks
    strip.set_dataset(SLIDE)
    viewport = tab.stack.view.view_box.viewRect()
    viewport_l0 = (int(viewport.y()), int(viewport.x()),
                   max(1, int(viewport.width())), max(1, int(viewport.height())))

    mark = counters.mark()
    start = time.perf_counter()
    stacks = strip.ensure_built(
        CHANNEL, params_for=page._compare_params_for,
        tint=page._full_image_tint(),
        nucleus=page._full_image_nucleus_args(),
        viewport_l0=viewport_l0,
        overview_store=getattr(full, "overview_store", None),
        caches=(full.caches if share else None))
    drain(6000)
    enter_ms = (time.perf_counter() - start) * 1000.0
    if stacks is None:
        raise SystemExit(
            f"compare build failed: {getattr(strip, '_build_error', '?')}")
    tiles, floors = counters.since(mark)

    wanted = {k for m in prepared for k in prepared[m]["keys"]}
    recomputed = [k for k in tiles if k in wanted]

    report = {
        "shared": bool(share),
        "full_image_prepared": {m: [prepared[m]["resident"], prepared[m]["total"]]
                                for m in prepared},
        "cache_identity": {
            "raw_same_object": stacks.caches[0] is full.caches[0],
            "corrected_same_object": stacks.caches[1] is full.caches[1],
            "scheduler_same_object": stacks.scheduler is full.scheduler,
            "provider_same_object": stacks.provider is full.provider,
            "controller_same_object": stacks.controllers[0] is full_controller,
            "strip_owns_caches": stacks.owns_caches,
        },
        "entering_compare": {
            "wall_ms": round(enter_ms, 1),
            "corrected_tile_computes": len(tiles),
            "already_prepared_keys_recomputed": len(recomputed),
            "prepared_key_total": len(wanted),
            "corrected_floor_computes": len(floors),
            "corrected_floor_ms": round(sum(floors), 1),
            "computes_by_level": dict(collections.Counter(
                k.tile.level for k in tiles)),
        },
    }

    # ...and back to the full image, same place.
    mark = counters.mark()
    start = time.perf_counter()
    page._on_compare_right_click()
    drain(4000)
    back_ms = (time.perf_counter() - start) * 1000.0
    tiles, floors = counters.since(mark)
    report["back_to_full_image"] = {
        "wall_ms": round(back_ms, 1),
        "corrected_tile_computes": len(tiles),
        "already_prepared_keys_recomputed": len(
            [k for k in tiles if k in wanted]),
        "corrected_floor_computes": len(floors),
    }
    # THE OWNERSHIP QUESTION: does the strip's teardown empty the cache it
    # borrowed? Measured before this harness tears anything else down.
    try:
        strip.teardown(wait_for_floor=False)
    except Exception as exc:                                # noqa: BLE001
        report["strip_teardown_error"] = repr(exc)
    drain(500)
    still = sum(1 for k in prepared["tophat"]["keys"]
                if full.scheduler.corrected_cache.get(k) is not None)
    report["after_strip_teardown_full_image_still_resident"] = [
        still, prepared["tophat"]["total"]]
    # This harness's own shutdown is not the product contract under test;
    # the real teardown paths are covered by the Step0 suites. Failures here
    # are recorded, never allowed to lose the measurement above.
    try:
        tab.teardown()
    except Exception as exc:                                # noqa: BLE001
        report["tab_teardown_error"] = repr(exc)
    try:
        page.deleteLater()
        APP.processEvents()
    except Exception as exc:                                # noqa: BLE001
        report["page_close_error"] = repr(exc)
    drain(500)
    return report


def main():
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "full_compare.json")
    label = sys.argv[2] if len(sys.argv) > 2 else "g324b11"
    counters = Counters()
    report = {
        "label": label, "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "slide": SLIDE, "project": str(PROJECT), "channel": CHANNEL,
        "viewport_l0": list(VIEWPORT),
        "honesty": [
            "the OS page cache was NOT dropped; nothing here is a cold disk read",
            "'hit' means the CorrectionKey was answered from the corrected LRU",
            "the corrected floor has no cache and is counted separately",
            "the demo slide and project are read-only",
            "the two arms differ only by ensure_built(caches=...)",
        ],
    }
    report["before_private_caches"] = run_arm(False, counters)
    out.write_text(json.dumps(report, indent=2, default=str))
    report["after_shared_caches"] = run_arm(True, counters)
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
