"""G3.2b.4B1: what a TopHat <-> cuCIM switch costs in Step0, A/B.

The A/B is done through the PUBLIC API only: a `Step0ExploreTab` with no
HOT specs provider installed behaves exactly as it did before this block
(`start_hot` returns None without one), and one with a provider installed
mounts the production `MultiChannelPrefetchController`. Nothing is
monkeypatched to switch the behaviour -- only the counters below are
wrapped, and only to count.

Honest limits, stated once:

* the slide is SYNTHETIC (a ramp over a 4096-square 4-level pyramid). The
  absolute milliseconds are a lower bound on a real slide; the SHAPE of the
  breakdown is the finding.
* this measures when the corrected tiles for the current viewport are
  RESIDENT, i.e. when the switch stops waiting on computation. It is not a
  display input-to-photon latency and is not called one.
* the corrected FLOOR has no cache of its own and is recomputed per method.
  It is counted separately and never folded into the tile numbers.

Usage:
    python scripts/benchmark_step0_method_switch.py OUT.json [label] [real]

`real` runs the same A/B against the READ-ONLY demo slide through the real
`RawTileProvider`, so the correction is the production GPU one over real
pixels. Nothing is written: the slide is opened for reading and closed.
"""

import importlib.util
import json
import os
import pathlib
import statistics
import sys
import time

import numpy as np

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

SLIDE = 4096
CHANNELS = ["DAPI", "CD3", "CD20"]
TOPHAT_RADIUS = 15
CUCIM_SIGMA = 50


class Provider:
    """A 2x pyramid over a synthetic square slide, counting every read."""

    num_levels = 4
    channel_names = list(CHANNELS)
    open_count = 1

    def __init__(self):
        self.reads = []
        self.tile_reads = []

    def close(self):
        pass

    def source_identity(self):
        return ("fake", "slide")

    def level_shape(self, level):
        return (SLIDE >> int(level), SLIDE >> int(level))

    def level_downsample(self, level):
        return float(1 << int(level))

    def level_downsample_yx(self, level):
        return (float(1 << int(level)),) * 2

    def channel_index(self, channel):
        return CHANNELS.index(channel) if channel in CHANNELS else 0

    def _ramp(self, channel, cy0, cy1, cx0, cx1):
        ys = np.arange(cy0, cy1, dtype=np.float32)[:, None]
        xs = np.arange(cx0, cx1, dtype=np.float32)[None, :]
        return ys * 10000.0 + xs + (1000.0 if channel == "DAPI" else 0.0)

    def read_region(self, channel, level, y0, y1, x0, x1):
        h, w = self.level_shape(level)
        cy0, cy1 = max(0, min(int(y0), h)), max(0, min(int(y1), h))
        cx0, cx1 = max(0, min(int(x0), w)), max(0, min(int(x1), w))
        self.reads.append((channel, level, cy0, cy1, cx0, cx1))
        return self._ramp(channel, cy0, cy1, cx0, cx1), (cy0, cx0)

    def read_tile(self, channel, tile):
        h, w = self.level_shape(tile.level)
        size = tile.grid.tile_size
        cy0, cx0 = tile.ty * size, tile.tx * size
        cy1, cx1 = min(cy0 + size, h), min(cx0 + size, w)
        self.tile_reads.append((channel, tile.level, cy0, cy1, cx0, cx1))
        return self._ramp(channel, cy0, cy1, cx0, cx1), 0.0


def _install_counters():
    """Count corrected TILE work and corrected ARRAY (floor) work apart."""
    from block01.viewer import correction_compute as cc

    tiles, arrays = [], []
    real_compute = cc.CorrectionCompute.compute
    real_array = cc.CorrectionCompute.correct_array

    def compute(self, key):
        tiles.append(key)
        return real_compute(self, key)

    def correct_array(self, arr, method, param):
        arrays.append((method, param))
        return real_array(self, arr, method, param)

    cc.CorrectionCompute.compute = compute
    cc.CorrectionCompute.correct_array = correct_array
    return tiles, arrays


def drain(ms=900, step=20):
    for _ in range(max(1, ms // step)):
        APP.processEvents()
        QtTest.QTest.qWait(step)


def _keys(stack, channel, method, base_param):
    from block01.viewer.tile_types import (
        CorrectionKey, TileAddress, effective_param)
    controller = stack.controller
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


def _resident(stack, channel, method, base_param):
    cache = stack.scheduler.corrected_cache
    keys = _keys(stack, channel, method, base_param)
    return sum(1 for key in keys if cache.get(key) is not None), len(keys)


def _wait_resident(stack, channel, method, base_param, timeout=30.0):
    """Wall clock until every visible tile of that method is resident."""
    start = time.perf_counter()
    deadline = start + timeout
    while time.perf_counter() < deadline:
        APP.processEvents()
        have, want = _resident(stack, channel, method, base_param)
        if want and have == want:
            return (time.perf_counter() - start) * 1000.0
        QtTest.QTest.qWait(5)
    return None


#: The demo slide, opened READ-ONLY for the `real` mode.
REAL_SLIDE = "/sda1/Fusion/benchmark/biopsy.ome.tif"
REAL_CHANNELS = ("CD22", "CD4")
REAL_VIEWPORT = (9000, 9000, 2048, 2048)


def run(with_hot, tiles, arrays, real=False):
    from block01.ui.step0.step0_explore_tab import Step0ExploreTab
    from block01.viewer import raw_tile_provider as rtp
    from block01.viewer.prefetch_policy import ChannelCorrectionSpec

    if real:
        import importlib
        importlib.reload(rtp)                    # the genuine provider
        path = REAL_SLIDE
        channels = REAL_CHANNELS
        viewport = REAL_VIEWPORT
        provider = None
    else:
        provider = Provider()
        rtp.RawTileProvider = lambda _path: provider
        path = "/fake/slide.ome.tif"
        channels = ("CD3", "CD20")
        viewport = (1024, 1024, 1024, 1024)

    tab = Step0ExploreTab(page=None)
    if with_hot:
        tab.set_hot_specs_provider(lambda: [
            ChannelCorrectionSpec(channel=name, tophat_radius=TOPHAT_RADIUS,
                                  cucim_sigma=CUCIM_SIGMA)
            for name in channels])
    tab.resize(800, 600)
    tab.show()
    APP.processEvents()
    tab.set_dataset(path)
    tab.show_source(channels[0], "tophat", (TOPHAT_RADIUS,))
    drain(200)
    tab.stack.controller.jump_to(*viewport)
    drain(6000 if real else 2000)
    if real:
        provider = tab.stack.controller.provider

    stack = tab.stack
    snapshot = stack.controller.snapshot()
    report = {
        "with_hot": bool(with_hot),
        "level": int(snapshot.level),
        "visible_tiles": len(snapshot.visible_tiles),
        "hot_mounted": tab.hot is not None,
        "hot_stats": tab.hot_stats(),
        "channels": list(channels),
        "after_settle_resident": {
            f"{channel}/{method}": list(_resident(stack, channel, method, param))
            for channel in channels
            for method, param in (("tophat", TOPHAT_RADIUS),
                                  ("cucim", CUCIM_SIGMA))},
    }

    cache = stack.scheduler.corrected_cache
    reads_of = (lambda: (len(provider.reads), len(provider.tile_reads))
                if hasattr(provider, "reads") else (0, 0))
    mark_reads = reads_of()
    marks = (mark_reads[0], mark_reads[1], len(tiles),
             len(arrays), cache.stats()["hits"])
    switch = []
    for index in range(3):
        for method, param in (("cucim", CUCIM_SIGMA),
                              ("tophat", TOPHAT_RADIUS)):
            before = time.perf_counter()
            tab.show_source(channels[0], method, (param,))
            waited = _wait_resident(stack, channels[0], method, param)
            switch.append({"to": method, "round": index + 1,
                           "ms_until_viewport_resident": (
                               None if waited is None else round(waited, 2)),
                           "ms_since_call": round(
                               (time.perf_counter() - before) * 1000.0, 2)})
            drain(400)
    report["switches"] = switch
    waits = [s["ms_until_viewport_resident"] for s in switch
             if s["ms_until_viewport_resident"] is not None]
    report["switch_ms_median"] = (round(statistics.median(waits), 2)
                                  if waits else None)
    report["switch_ms_max"] = round(max(waits), 2) if waits else None
    end_reads = reads_of()
    report["deltas_over_six_switches"] = {
        "provider_read_region": end_reads[0] - marks[0],
        "provider_read_tile": end_reads[1] - marks[1],
        "corrected_tile_computes": len(tiles) - marks[2],
        "corrected_floor_computes": len(arrays) - marks[3],
        "corrected_cache_hits": cache.stats()["hits"] - marks[4],
    }
    try:
        tab.teardown()
    except Exception:                                       # noqa: BLE001
        pass
    drain(200)
    return report


def main():
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                       else "step0_method_switch.json")
    label = sys.argv[2] if len(sys.argv) > 2 else "g324b1"
    real = len(sys.argv) > 3 and sys.argv[3] == "real"
    tiles, arrays = _install_counters()
    report = {
        "label": label,
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "slide": (REAL_SLIDE if real
                  else "synthetic 4096-square, 4 levels, ramp"),
        "mode": "real" if real else "synthetic",
        "honesty": [
            "synthetic slide: absolute ms are a lower bound, the shape is the finding",
            "'resident' means the corrected tiles are in the cache, not photons on a display",
            "the corrected floor has no cache and is counted separately",
            "the A/B is the public API: no specs provider installed = the old behaviour",
        ],
        "before_no_hot": run(False, tiles, arrays, real=real),
        "after_with_hot": run(True, tiles, arrays, real=real),
    }
    out.write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps({k: v for k, v in report.items()
                      if k.startswith(("before", "after"))}, indent=2,
                     default=str))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
