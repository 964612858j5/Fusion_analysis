"""Long-dwell measurement for HOT + COVERAGE multi-channel prefetch.

Parks the camera and lets COVERAGE walk the whole channel list, then reports
peak RSS, the corrected cache's items/bytes/evictions, how long the plan took
to drain, and how many channels end up switch-ready. The point of the
measurement is NOT throughput -- COVERAGE completes either way -- but
RETENTION: how much of the background work the corrected cache can still
hold when COVERAGE is done. Run it once per cache budget and compare.

Usage (no display needed):
    cd /sda1/Fusion/analysis_pipline/block01_v14
    QT_QPA_PLATFORM=offscreen python scripts/benchmark_coverage_dwell.py \
        --path /sda1/Albert/fusion/.../..._Scan1.tiff.ome.tif \
        --corrected-cache-gb 8 --dwell 90

Drain is PHYSICAL, not notional: a COVERAGE slot is released only by a
callback (ordinary, or the opt-in terminal one for a stale generation), and
"drained" additionally requires the batch counter at zero and the whole
channel order consumed -- between two batches the queues are momentarily
empty while channels are still waiting to be planned.

Archived results: docs/benchmarks/2026-09-01_57ch_coverage_long_dwell.md
"""

import argparse
import sys
import threading
import time


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
from PyQt5 import QtWidgets  # noqa: E402

# Same global as main.py / explore_demo.py: standalone use must not render
# transposed just because it never touched this.
pg.setConfigOptions(imageAxisOrder="row-major")

from block01.viewer.caches import LRUByteCache  # noqa: E402
from block01.viewer.correction_compute import CorrectionCompute  # noqa: E402
from block01.viewer.explore_view import (  # noqa: E402
    ExploreController, ExploreView)
from block01.viewer.multichannel_prefetch import (  # noqa: E402
    MultiChannelPrefetchController)
from block01.viewer.prefetch_policy import ChannelCorrectionSpec  # noqa: E402
from block01.viewer.raw_tile_provider import RawTileProvider  # noqa: E402
from block01.viewer.scheduler import TileScheduler  # noqa: E402
from block01.viewer.tile_types import TileGridSpec  # noqa: E402

DEFAULT_PATH = ("/sda1/Albert/fusion/20260210/"
                "20260210_Ming_Albert_HCC_test_01_Scan1.tiff.ome.tif")


def rss_mb():
    with open("/proc/self/status") as fh:
        return int(fh.read().split("VmRSS:")[1].split()[0]) / 1024


def coverage_drained(hot):
    """Mirror of scripts/benchmark_hot_ab.py's `_coverage_drained`."""
    return (not hot._tile_queue
            and not hot._active_requests
            and not hot._coverage_queue
            and not hot._coverage_active_requests
            and hot._coverage_batch_remaining == 0
            and hot.stats["coverage_batches"] > 0
            and hot.stats["coverage_tiles_requested"] > 0
            and (hot._coverage_order_position
                 >= len(hot._coverage_full_order)))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", default=DEFAULT_PATH)
    ap.add_argument("--corrected-cache-gb", type=float, default=8.0,
                    help="corrected-tile cache budget; the variable under "
                         "test (default: 8, the value proposed in "
                         "docs/v15_multichannel_settled_prefetch.md)")
    ap.add_argument("--raw-cache-gb", type=float, default=2.0)
    ap.add_argument("--dwell", type=float, default=90.0,
                    help="seconds to hold still; stops early once drained")
    ap.add_argument("--method", default="tophat", choices=("tophat", "cucim"))
    ap.add_argument("--param", type=int, default=25)
    ap.add_argument("--center", type=int, nargs=2, default=(25606, 15360),
                    metavar=("Y", "X"))
    ap.add_argument("--span", type=float, default=1400.0,
                    help="viewport side length in level-0 pixels")
    ap.add_argument("--report-every", type=float, default=30.0)
    args = ap.parse_args()

    app = QtWidgets.QApplication([])
    provider = RawTileProvider(args.path)
    names = provider.channel_names
    grid = TileGridSpec(tile_size=512, source_chunk_shape=(), grid_version="v1")
    raw_cache = LRUByteCache(int(args.raw_cache_gb * 1024) << 20)
    corrected_cache = LRUByteCache(int(args.corrected_cache_gb * 1024) << 20)
    compute = CorrectionCompute(provider, raw_cache)
    scheduler = TileScheduler(provider, compute, raw_cache, corrected_cache)

    view = ExploreView()
    view.resize(1400, 1000)
    view.show()
    controller = ExploreController(provider, scheduler, compute, grid, view,
                                   names[0])
    controller.load_overview()
    controller.set_selection(method=args.method, params=(args.param,))

    # The floor must exist before the camera is parked, or the first frame
    # measures overview installation instead of COVERAGE.
    t = time.perf_counter()
    while time.perf_counter() - t < 10 and not controller._floor_ready:
        app.processEvents()
        time.sleep(0.005)

    specs = [ChannelCorrectionSpec(channel=n, tophat_radius=args.param,
                                   cucim_sigma=args.param) for n in names]
    hot = MultiChannelPrefetchController(controller, scheduler, specs, grid,
                                         coverage=True)

    cy, cx = args.center
    half = args.span / 2.0
    view.view_box.setRange(xRange=(cx - half, cx + half),
                           yRange=(cy - half, cy + half), padding=0)

    peak = [rss_mb()]
    stop = threading.Event()

    def monitor():
        while not stop.is_set():
            peak[0] = max(peak[0], rss_mb())
            time.sleep(0.05)

    threading.Thread(target=monitor, daemon=True).start()

    print(f"channels={len(names)} corrected_cache={args.corrected_cache_gb:.2f} GB "
          f"dwell={args.dwell:.0f}s method={args.method}({args.param}) "
          f"RSS start {rss_mb():.0f} MB", flush=True)

    t0 = time.perf_counter()
    last = 0.0
    while time.perf_counter() - t0 < args.dwell:
        app.processEvents()
        time.sleep(0.01)
        elapsed = time.perf_counter() - t0
        if elapsed - last >= args.report_every:
            last = elapsed
            st, cs = hot.stats, corrected_cache.stats()
            print(f"  t={elapsed:5.0f}s batches={st['coverage_batches']:3d} "
                  f"req={st['coverage_tiles_requested']:5d} "
                  f"done={st['coverage_tiles_completed']:5d} "
                  f"fail={st['coverage_tiles_failed']} | cache "
                  f"items={cs['items']:5d} {cs['bytes'] / 1e9:5.2f} GB "
                  f"evict={cs['evictions']:5d} | RSS {rss_mb():.0f} MB",
                  flush=True)
        if coverage_drained(hot):
            break

    st, cs = hot.stats, corrected_cache.stats()
    print(f"\nFINAL after {time.perf_counter() - t0:.0f}s")
    print(f"  drained={coverage_drained(hot)} "
          f"hot_queue={len(hot._tile_queue)} hot_active={len(hot._active_requests)} "
          f"cov_queue={len(hot._coverage_queue)} "
          f"cov_active={len(hot._coverage_active_requests)} "
          f"batch_remaining={hot._coverage_batch_remaining} "
          f"order={hot._coverage_order_position}/{len(hot._coverage_full_order)}")
    print(f"  coverage: batches={st['coverage_batches']} "
          f"req={st['coverage_tiles_requested']} "
          f"done={st['coverage_tiles_completed']} "
          f"fail={st['coverage_tiles_failed']} "
          f"cancelled={st['coverage_cancelled']} "
          f"abandoned_done={st['coverage_abandoned_finished']}")
    print(f"  hot: req={st['hot_tiles_requested']} done={st['hot_tiles_completed']} "
          f"fail={st['hot_tiles_failed']} "
          f"ovw={st['overviews_requested']}/{st['overviews_failed']}")
    print(f"  corrected cache: items={cs['items']} bytes={cs['bytes'] / 1e9:.2f} GB "
          f"evictions={cs['evictions']} hits={cs['hits']} misses={cs['misses']}")
    print(f"  RSS peak {peak[0]:.0f} MB, now {rss_mb():.0f} MB")
    ready = sum(1 for n in names
                if hot.is_channel_ready(n, controller.snapshot()))
    print(f"  channels reported ready: {ready}/{len(names)}")

    stop.set()
    hot.stop()
    controller.teardown()


if __name__ == "__main__":
    main()
