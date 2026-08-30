"""G1-render measurement harness for the Step0 Explore view.

Runs a scripted pan/zoom/jump sequence against a REAL OME-TIFF through the
real provider/scheduler/compute stack and the real ExploreView/
ExploreController, and reports frame-prep timing (per docs/
v15_step0_explore_integration.md §4). Measured-only wording; this is NOT a
promise about any other machine/dataset.

Usage:
    python scripts/g1_render_probe.py --path /path/to/slide.ome.tif \
        --channel DAPI --out /tmp/g1_probe [--offscreen]

With --offscreen (or if no real display is available), sets
QT_QPA_PLATFORM=offscreen before importing Qt: the reported numbers then
measure frame PREP cost only ("offscreen: excludes real compositor/vsync"),
never claimed as on-screen smoothness.

Do NOT run this against the real WSI as part of an automated test suite --
it is a manual measurement script (see module docstring of
scripts/benchmark_viewer_prototype.py for the same convention).
"""

import argparse
import json
import os
import random
import socket
import sys
import time


def _register_block01_alias():
    """Bind the `block01` package name to THIS checkout, regardless of the
    directory name it lives in (e.g. a `block01_v14` git worktree) -- mirrors
    the repo-root conftest.py shim so `python scripts/g1_render_probe.py` and
    `pytest` resolve the same module tree."""
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
            f"'block01' already imported from {existing_root!r}, not from "
            f"{root!r}; refusing to measure against the wrong checkout.")
    spec = importlib.util.spec_from_file_location(
        "block01", root / "__init__.py", submodule_search_locations=[str(root)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["block01"] = mod
    spec.loader.exec_module(mod)
    sys.path.insert(0, str(root.parent))


_register_block01_alias()

FRAME_BUDGET_MS = 1000.0 / 60.0  # 16.7ms
WINDOW_MS = FRAME_BUDGET_MS


def aggregate_windows(frame_events, window_ms=WINDOW_MS):
    """Frame aggregation (measured-only; NOT exact vsync frames): bucket
    (timestamp, cost_ms) samples -- tagging range_handler, request_issue, and
    tile_item_update work -- into `window_ms`-wide windows by wall-clock time, sum the cost
    within each window, and report the per-window summed-cost distribution.
    This replaces the earlier worst-case-sum caveat (p95(range) + p95(blit))
    with a real per-window total, still window-aggregation rather than a
    vsync-accurate frame trace."""
    if not frame_events:
        return {"p50": None, "p95": None, "max": None, "over_budget": 0, "n_windows": 0}
    frame_events = sorted(frame_events)
    t_start = frame_events[0][0]
    buckets = {}
    for t, ms in frame_events:
        idx = int((t - t_start) * 1000.0 / window_ms)
        buckets[idx] = buckets.get(idx, 0.0) + ms
    values = list(buckets.values())
    return {
        "p50": pct(values, 50),
        "p95": pct(values, 95),
        "max": max(values),
        "over_budget": sum(1 for v in values if v > window_ms),
        "n_windows": len(values),
    }


def pct(values, p):
    if not values:
        return None
    values = sorted(values)
    idx = min(len(values) - 1, int(round(p / 100.0 * (len(values) - 1))))
    return values[idx]


def fmt(v):
    return "n/a" if v is None else f"{v:.3f}"


def environment_block(offscreen):
    return {
        "hostname": socket.gethostname(),
        "python": sys.version.split()[0],
        "qt_qpa_platform": os.environ.get("QT_QPA_PLATFORM"),
        "offscreen": offscreen,
        "offscreen_note": ("offscreen: excludes real compositor/vsync -- "
                            "numbers measure frame PREP cost only")
                           if offscreen else None,
    }


def run_scripted_sequence(ctrl, view, provider, seed=0):
    """load overview; 30 pan steps of 1/8 viewport; 5 zoom in; 5 zoom out;
    3 jumps to random corners (seeded). Returns timing sample dicts."""
    from PyQt5 import QtTest

    rng = random.Random(seed)

    timings = {
        "time_to_first_overview_pixel_ms": None,
        "raw_fill_latencies_ms": [],
        "precise_fill_latencies_ms": [],
    }

    t0 = time.perf_counter()
    ctrl.load_overview()
    timings["time_to_first_overview_pixel_ms"] = (time.perf_counter() - t0) * 1000.0

    h0, w0 = provider.level_shape(0)
    vp = min(2048, h0, w0)
    y0, x0 = h0 // 2 - vp // 2, w0 // 2 - vp // 2
    y1, x1 = y0 + vp, x0 + vp

    def set_range_and_measure_fill(y0, x0, y1, x1):
        raw_before = ctrl.stats["raw_tiles_blitted"]
        t_raw0 = time.perf_counter()
        view.view_box.setRange(xRange=(x0, x1), yRange=(y0, y1), padding=0)
        QtTest.QTest.qWait(5)
        deadline = time.time() + 3.0
        while ctrl.stats["raw_tiles_blitted"] == raw_before and time.time() < deadline:
            QtTest.QTest.qWait(5)
        if ctrl.stats["raw_tiles_blitted"] > raw_before:
            timings["raw_fill_latencies_ms"].append((time.perf_counter() - t_raw0) * 1000.0)

        precise_before = ctrl.stats["precise_tiles_blitted"]
        t_precise0 = time.perf_counter()
        deadline = time.time() + max(0.2, ctrl.settle_ms / 1000.0 + 2.0)
        while ctrl.stats["precise_tiles_blitted"] == precise_before and time.time() < deadline:
            QtTest.QTest.qWait(10)
        if ctrl.stats["precise_tiles_blitted"] > precise_before:
            timings["precise_fill_latencies_ms"].append((time.perf_counter() - t_precise0) * 1000.0)

    # 30 pan steps of 1/8 viewport. Fill latency is SAMPLED on every 4th
    # step (waiting for raw+precise on all 30 made the scripted run exceed
    # practical wall time on cold data); other steps only advance the camera.
    step = max(1, vp // 8)
    for i in range(30):
        dx = step if i % 2 == 0 else 0
        dy = 0 if i % 2 == 0 else step
        x0, x1 = x0 + dx, x1 + dx
        y0, y1 = y0 + dy, y1 + dy
        if i % 4 == 0:
            set_range_and_measure_fill(y0, x0, y1, x1)
        else:
            view.view_box.setRange(xRange=(x0, x1), yRange=(y0, y1), padding=0)
            QtTest.QTest.qWait(50)

    # 5 zoom-in steps.
    for _ in range(5):
        cy, cx = (y0 + y1) / 2.0, (x0 + x1) / 2.0
        new_vp = max(64, int((y1 - y0) * 0.8))
        y0, y1 = int(cy - new_vp / 2), int(cy + new_vp / 2)
        x0, x1 = int(cx - new_vp / 2), int(cx + new_vp / 2)
        set_range_and_measure_fill(y0, x0, y1, x1)

    # 5 zoom-out steps.
    for _ in range(5):
        cy, cx = (y0 + y1) / 2.0, (x0 + x1) / 2.0
        new_vp = int((y1 - y0) * 1.25)
        y0, y1 = int(cy - new_vp / 2), int(cy + new_vp / 2)
        x0, x1 = int(cx - new_vp / 2), int(cx + new_vp / 2)
        set_range_and_measure_fill(y0, x0, y1, x1)

    # 3 jumps to random corners (seeded).
    for _ in range(3):
        jy0 = rng.randint(0, max(0, h0 - vp))
        jx0 = rng.randint(0, max(0, w0 - vp))
        t_jump0 = time.perf_counter()
        ctrl.jump_to(jy0, jx0, vp, vp)
        QtTest.QTest.qWait(int(ctrl.settle_ms) + 50)
        timings.setdefault("jump_wall_ms", []).append((time.perf_counter() - t_jump0) * 1000.0)

    return timings


def main():
    from block01.viewer.scheduler import DEFAULT_COMPUTE_WORKERS, DEFAULT_IO_WORKERS

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--path", required=True)
    ap.add_argument("--channel", required=True, help="channel name or integer index")
    ap.add_argument("--out", default="/tmp/g1_render_probe")
    ap.add_argument("--offscreen", action="store_true")
    ap.add_argument("--io-workers", type=int, default=DEFAULT_IO_WORKERS,
                    help=f"raw I/O worker threads (default {DEFAULT_IO_WORKERS}; "
                         "see viewer/scheduler.py for the measured basis)")
    ap.add_argument("--compute-workers", type=int, default=DEFAULT_COMPUTE_WORKERS,
                    help=f"correction compute worker threads (default {DEFAULT_COMPUTE_WORKERS})")
    args = ap.parse_args()

    offscreen = args.offscreen or not os.environ.get("DISPLAY")
    if offscreen:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    os.makedirs(args.out, exist_ok=True)

    from PyQt5 import QtWidgets

    import block01.core.bg_correction as bg_correction  # noqa: F401
    from block01.viewer.caches import LRUByteCache
    from block01.viewer.correction_compute import CorrectionCompute
    from block01.viewer.raw_tile_provider import RawTileProvider
    from block01.viewer.scheduler import TileScheduler
    from block01.viewer.tile_types import TileGridSpec
    from block01.viewer.explore_view import ExploreController, ExploreView

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])

    provider = RawTileProvider(args.path, handle_mode="per_thread")
    try:
        channel_index = int(args.channel)
        channel = provider.channel_names[channel_index]
    except ValueError:
        channel = args.channel

    raw_cache = LRUByteCache(512 * 1024 * 1024)
    corr_cache = LRUByteCache(512 * 1024 * 1024)
    compute = CorrectionCompute(provider, raw_cache)
    # Worker counts are the largest measured lever on how sharp the image
    # is DURING motion (viewer/explore_view.py "Worker counts"): the
    # fraction of the viewport already covered mid-drag goes from 52.4% at
    # io=1/cw=1 to 89.8% at io=8/cw=4 (tophat), and mid-zoom from 25.0% to
    # 77.8%. These scripts previously pinned io=1/cw=1 -- BELOW
    # TileScheduler's own io_workers=4 default -- so every manual test ran
    # on a single I/O thread. Parallel output was verified byte-identical
    # to the serial path before raising these.
    scheduler = TileScheduler(provider, compute, raw_cache, corr_cache,
                               io_workers=args.io_workers,
                               compute_workers=args.compute_workers)
    grid = TileGridSpec(tile_size=512, source_chunk_shape=(), grid_version="v1")

    view = ExploreView()
    view.resize(1024, 768)
    view.show()
    app.processEvents()

    ctrl = ExploreController(provider, scheduler, compute, grid, view, channel,
                              settle_ms=80, probe=True)
    ctrl.set_selection(method="tophat", params=(25,))

    fill_timings = run_scripted_sequence(ctrl, view, provider, seed=0)

    # Two real per-frame costs, measured separately:
    #   range_handler_ms  -- the (cheap, un-debounced) sigRangeChanged
    #                        handler: level pick, wanted-tile-set recompute,
    #                        prune check.
    #   request_issue_ms  -- the debounced (30ms motion timer) callback that
    #                        actually issues raw tile requests.
    # tile_item_update_ms (quantize + setImage + setRect per delivered
    # tile) is the third real cost, reported separately below since it is
    # per-TILE, not per-frame.
    # The three costs are NOT 1:1 per frame, so index-pairing across them
    # would be a methodology error; each distribution is reported
    # separately plus a conservative WORST-CASE frame estimate =
    # p95(range) + p95(request_issue) (upper bound: assumes both worst
    # halves land in one frame).
    range_ms = ctrl.timings["range_handler_ms"]
    issue_ms = ctrl.timings["request_issue_ms"]
    tile_update_ms = ctrl.timings["tile_item_update_ms"]
    def _p(v, q):
        return pct(v, q) or 0.0
    worst_case_p95 = _p(range_ms, 95) + _p(issue_ms, 95)
    over_budget_range = sum(1 for t in range_ms if t > FRAME_BUDGET_MS)
    over_budget_issue = sum(1 for t in issue_ms if t > FRAME_BUDGET_MS)

    window_agg = aggregate_windows(ctrl.timings["frame_events"])

    report = {
        "environment": environment_block(offscreen),
        "dataset_path": os.path.abspath(args.path),
        "channel": channel,
        "items_created": ctrl.stats["items_created"],
        "items_pruned": ctrl.stats["items_pruned"],
        "tile_item_update_ms_p50": pct(tile_update_ms, 50),
        "tile_item_update_ms_p95": pct(tile_update_ms, 95),
        "window_aggregated_p50_ms": window_agg["p50"],
        "window_aggregated_p95_ms": window_agg["p95"],
        "windows_over_budget": window_agg["over_budget"],
        "n_windows": window_agg["n_windows"],
        "n_range_handler_samples": len(range_ms),
        "n_request_issue_samples": len(issue_ms),
        "range_handler_ms_p50": pct(range_ms, 50),
        "range_handler_ms_p95": pct(range_ms, 95),
        "range_handler_ms_max": max(range_ms) if range_ms else None,
        "request_issue_ms_p50": pct(issue_ms, 50),
        "request_issue_ms_p95": pct(issue_ms, 95),
        "request_issue_ms_max": max(issue_ms) if issue_ms else None,
        "frame_prep_worst_case_p95_ms": worst_case_p95,
        "range_handler_over_budget": over_budget_range,
        "request_issue_over_budget": over_budget_issue,
        "time_to_first_observed_overview_ms": fill_timings["time_to_first_overview_pixel_ms"],
        "time_to_first_observed_raw_fill_ms_p50": pct(fill_timings["raw_fill_latencies_ms"], 50),
        "time_to_first_observed_precise_fill_ms_p50": pct(fill_timings["precise_fill_latencies_ms"], 50),
        "n_raw_fill_samples": len(fill_timings["raw_fill_latencies_ms"]),
        "n_precise_fill_samples": len(fill_timings["precise_fill_latencies_ms"]),
        # Per settled batch: issue -> first/all currently-visible tiles
        # matching (finding 3).
        "viewport_first_raw_tile_ms_p50": pct(ctrl.timings["viewport_first_raw_tile_ms"], 50),
        "viewport_full_raw_tile_ms_p50": pct(ctrl.timings["viewport_full_raw_tile_ms"], 50),
        "viewport_first_precise_tile_ms_p50": pct(ctrl.timings["viewport_first_precise_tile_ms"], 50),
        "viewport_full_precise_ms_p50": pct(ctrl.timings["viewport_full_precise_ms"], 50),
        "jump_wall_ms": fill_timings.get("jump_wall_ms", []),
        "stats": dict(ctrl.stats),
        "measured_only": True,
    }

    ctrl.teardown()

    json_path = os.path.join(args.out, "g1_render_probe.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    md_path = os.path.join(args.out, "g1_render_probe.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# G1-render probe (measured-only)\n\n")
        f.write(f"Dataset: `{report['dataset_path']}`  Channel: `{report['channel']}`\n\n")
        f.write("## Environment\n\n")
        for k, v in report["environment"].items():
            f.write(f"- {k}: `{v}`\n")
        if offscreen:
            f.write("\n**offscreen: excludes real compositor/vsync — numbers "
                     "measure frame PREP cost only**\n")
        f.write("\n## Frame prep timing (range_handler + request_issue)\n\n")
        f.write(f"- n_range_handler_samples: {report['n_range_handler_samples']}, "
                f"n_request_issue_samples: {report['n_request_issue_samples']}\n")
        f.write(f"- range_handler_ms: p50={fmt(report['range_handler_ms_p50'])} "
                f"p95={fmt(report['range_handler_ms_p95'])} "
                f"max={fmt(report['range_handler_ms_max'])}\n")
        f.write(f"- request_issue_ms: p50={fmt(report['request_issue_ms_p50'])} "
                f"p95={fmt(report['request_issue_ms_p95'])} "
                f"max={fmt(report['request_issue_ms_max'])}\n")
        f.write(f"- frame_prep worst-case estimate (p95 range + p95 request_issue, upper bound): "
                f"{fmt(report['frame_prep_worst_case_p95_ms'])} ms\n")
        f.write(f"- over 16.7ms budget: range_handler {report['range_handler_over_budget']}, "
                f"request_issue {report['request_issue_over_budget']}\n")
        f.write("\n")

        f.write("## Per-tile item update breakdown (measured-only)\n\n")
        f.write(f"- tile_item_update_ms (quantize + setImage/setRect per delivered tile): "
                f"p50={fmt(report['tile_item_update_ms_p50'])} "
                f"p95={fmt(report['tile_item_update_ms_p95'])}\n")
        f.write(f"- window-aggregated (16.7ms buckets, summed cost): "
                f"p50={fmt(report['window_aggregated_p50_ms'])} "
                f"p95={fmt(report['window_aggregated_p95_ms'])}, "
                f"{report['windows_over_budget']}/{report['n_windows']} windows over budget "
                f"(window-aggregation, NOT exact vsync frames)\n")
        f.write(f"- items_created: {report['items_created']}, "
                f"items_pruned: {report['items_pruned']}\n")
        f.write("\n")

        f.write("## Fill latencies\n\n")
        f.write(f"- time_to_first_observed_overview_ms: "
                f"{fmt(report['time_to_first_observed_overview_ms'])} ms\n")
        f.write(f"- time_to_first_observed_raw_fill_ms p50 ({report['n_raw_fill_samples']} samples): "
                f"{fmt(report['time_to_first_observed_raw_fill_ms_p50'])} ms\n")
        f.write(f"- time_to_first_observed_precise_fill_ms p50 ({report['n_precise_fill_samples']} samples): "
                f"{fmt(report['time_to_first_observed_precise_fill_ms_p50'])} ms\n")
        f.write("\n## Viewport-first/full (issue -> first/all visible tiles matching)\n\n")
        f.write(f"- viewport_first_raw_tile_ms p50: {fmt(report['viewport_first_raw_tile_ms_p50'])} ms\n")
        f.write(f"- viewport_full_raw_tile_ms p50: {fmt(report['viewport_full_raw_tile_ms_p50'])} ms\n")
        f.write(f"- viewport_first_precise_tile_ms p50: {fmt(report['viewport_first_precise_tile_ms_p50'])} ms\n")
        f.write(f"- viewport_full_precise_ms p50: {fmt(report['viewport_full_precise_ms_p50'])} ms\n")
        f.write(f"\n## Controller stats\n\n")
        for k, v in report["stats"].items():
            f.write(f"- {k}: {v}\n")

    print(md_path)


if __name__ == "__main__":
    main()
