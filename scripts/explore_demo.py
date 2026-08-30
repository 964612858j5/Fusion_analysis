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
from PyQt5 import QtWidgets  # noqa: E402

# Match main.py's global config (imageAxisOrder="row-major"): standalone use
# of this demo must not render transposed just because it never touched
# this global. Every ImageItem in explore_view.py ALSO sets axisOrder
# explicitly per-item, so this is belt-and-suspenders, not the only fix.
pg.setConfigOptions(imageAxisOrder="row-major")

from block01.core import bg_correction  # noqa: E402
from block01.viewer.caches import LRUByteCache  # noqa: E402
from block01.viewer.correction_compute import CorrectionCompute  # noqa: E402
from block01.viewer.explore_view import ExploreController, ExploreView  # noqa: E402
from block01.viewer.raw_tile_provider import RawTileProvider  # noqa: E402
from block01.viewer.scheduler import TileScheduler  # noqa: E402
from block01.viewer.tile_types import TileGridSpec  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--path", required=True)
    ap.add_argument("--channel", default="1")
    ap.add_argument("--method", default="tophat", choices=["tophat", "cucim", "none"])
    ap.add_argument("--param", type=int, default=25)
    ap.add_argument("--log", default="/tmp/explore_demo_crash.log")
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
    corrected_cache = LRUByteCache(512 * 1024 * 1024)
    compute = CorrectionCompute(provider, raw_cache)
    scheduler = TileScheduler(provider, compute, raw_cache, corrected_cache,
                              io_workers=1, compute_workers=1)

    view = ExploreView()
    base_title = f"Explore demo — {os.path.basename(args.path)} · {channel}"
    view.setWindowTitle(base_title)
    view.resize(1400, 1000)

    ctrl = ExploreController(provider, scheduler, compute, grid, view, channel)

    def _on_floor_preparing_changed(preparing):
        suffix = " — Preparing corrected preview…" if preparing else ""
        view.setWindowTitle(base_title + suffix)

    ctrl.floor_preparing_changed.connect(_on_floor_preparing_changed)

    ctrl.load_overview()
    if args.method != "none":
        ctrl.set_selection(method=args.method, params=(args.param,))

    # Start looking at the whole slide.
    h0, w0 = provider.level_shape(0)
    view.view_box.setRange(xRange=(0, w0), yRange=(0, h0), padding=0)
    view.show()

    print(f"[explore_demo] channel={channel} method={args.method} param={args.param}")
    print(f"[explore_demo] drag = pan, wheel = zoom; crash log -> {args.log}")

    code = app.exec_()
    ctrl.teardown()
    log_f.close()
    sys.exit(code)


if __name__ == "__main__":
    main()
