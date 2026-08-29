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
BLIT_MODES = ("float_full", "uint8_full", "uint8_incremental")


def aggregate_windows(frame_events, window_ms=WINDOW_MS):
    """Frame aggregation (measured-only; NOT exact vsync frames): bucket
    (timestamp, cost_ms) samples -- tagging BOTH range_handler and blit_tick
    work -- into `window_ms`-wide windows by wall-clock time, sum the cost
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
        deadline = time.time() + 5.0
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

    # 30 pan steps of 1/8 viewport.
    step = max(1, vp // 8)
    for i in range(30):
        dx = step if i % 2 == 0 else 0
        dy = 0 if i % 2 == 0 else step
        x0, x1 = x0 + dx, x1 + dx
        y0, y1 = y0 + dy, y1 + dy
        set_range_and_measure_fill(y0, x0, y1, x1)

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


def run_compare_blit_modes(provider, scheduler, compute, grid, channel, app):
    """Run the SAME scripted sequence once per blit_mode, in order
    (float_full, uint8_full, uint8_incremental), against a fresh
    ExploreController/ExploreView each time. The provider/scheduler/compute
    stack is REUSED across modes (deliberate, per the approved plan) -- so
    raw/precise tile results are cache-warm for every mode after the first;
    this is noted explicitly in the report and does not affect the
    blit-path costs being measured (those live entirely downstream of the
    scheduler callback, in the controller/view). Only the LAST mode's
    controller does a full teardown (scheduler.shutdown + provider.close);
    earlier modes stop their own timers/signals only, via
    `teardown(shutdown_backend=False)`."""
    from block01.viewer.explore_view import ExploreController, ExploreView

    rows = []
    for i, mode in enumerate(BLIT_MODES):
        view = ExploreView()
        view.resize(1024, 768)
        view.show()
        app.processEvents()

        ctrl = ExploreController(provider, scheduler, compute, grid, view, channel,
                                  settle_ms=80, probe=True, blit_mode=mode)
        ctrl.set_selection(method="tophat", params=(25,))
        run_scripted_sequence(ctrl, view, provider, seed=0)

        blit_ms = ctrl.timings["blit_tick_ms"]
        window_agg = aggregate_windows(ctrl.timings["frame_events"])
        rows.append({
            "mode": mode,
            "blit_tick_p50": pct(blit_ms, 50),
            "blit_tick_p95": pct(blit_ms, 95),
            "set_image_p50": pct(ctrl.timings["set_image_ms"], 50),
            "set_image_p95": pct(ctrl.timings["set_image_ms"], 95),
            "tile_convert_p50": pct(ctrl.timings["tile_convert_ms"], 50),
            "tile_convert_p95": pct(ctrl.timings["tile_convert_ms"], 95),
            "window_p95": window_agg["p95"],
            "windows_over_budget": window_agg["over_budget"],
            "n_windows": window_agg["n_windows"],
            "rgba_canvas_allocs": ctrl.stats["rgba_canvas_allocs"],
        })

        is_last = (i == len(BLIT_MODES) - 1)
        ctrl.teardown(shutdown_backend=is_last)
        view.close()

    return rows


def write_compare_report(out_dir, args, offscreen, channel, rows):
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "g1_render_probe_compare.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "dataset_path": os.path.abspath(args.path),
            "channel": channel,
            "environment": environment_block(offscreen),
            "note": ("provider/scheduler/compute stack reused across modes -- "
                     "caches are warm equally for every mode after the first"),
            "rows": rows,
            "measured_only": True,
        }, f, indent=2, default=str)

    md_path = os.path.join(out_dir, "g1_render_probe_compare.md")
    cols = [
        ("mode", "mode"),
        ("blit_tick p50/p95", None),
        ("set_image p50/p95", None),
        ("tile_convert p50/p95", None),
        ("window-agg p95", "window_p95"),
        ("windows over budget", None),
        ("rgba_canvas_allocs", "rgba_canvas_allocs"),
    ]
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# G1-render blit-mode comparison (measured-only)\n\n")
        f.write(f"Dataset: `{os.path.abspath(args.path)}`  Channel: `{channel}`\n\n")
        f.write("Provider/scheduler/compute stack reused across modes: caches "
                "are warm equally for every mode from mode 2 onward (not a "
                "cold-cache comparison).\n\n")
        f.write("| mode | blit_tick p50/p95 (ms) | set_image p50/p95 (ms) | "
                "tile_convert p50/p95 (ms) | window-agg p95 (ms) | "
                "windows over budget | rgba_canvas_allocs |\n")
        f.write("|---|---|---|---|---|---|---|\n")
        for row in rows:
            f.write(
                f"| {row['mode']} "
                f"| {fmt(row['blit_tick_p50'])}/{fmt(row['blit_tick_p95'])} "
                f"| {fmt(row['set_image_p50'])}/{fmt(row['set_image_p95'])} "
                f"| {fmt(row['tile_convert_p50'])}/{fmt(row['tile_convert_p95'])} "
                f"| {fmt(row['window_p95'])} "
                f"| {row['windows_over_budget']}/{row['n_windows']} "
                f"| {row['rgba_canvas_allocs']} |\n"
            )
        f.write("\n(measured-only; window-agg is 16.7ms-bucket-summed cost, "
                "not exact vsync frames.)\n")

    print(md_path)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--path", required=True)
    ap.add_argument("--channel", required=True, help="channel name or integer index")
    ap.add_argument("--out", default="/tmp/g1_render_probe")
    ap.add_argument("--offscreen", action="store_true")
    ap.add_argument("--blit-mode", choices=BLIT_MODES, default="uint8_incremental",
                     help="ExploreController blit_mode to measure (default: "
                          "uint8_incremental, the stage-B persistent-canvas path).")
    ap.add_argument("--compare-blit-modes", action="store_true",
                     help="Run the SAME scripted sequence once per blit_mode "
                          "(fresh controller/view per mode; the provider/"
                          "scheduler/compute stack IS reused across modes, so "
                          "caches are warm equally from mode 2 onward -- noted "
                          "in the report) and print a comparison table instead "
                          "of the single-mode report.")
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
    scheduler = TileScheduler(provider, compute, raw_cache, corr_cache,
                               io_workers=1, compute_workers=1)
    grid = TileGridSpec(tile_size=512, source_chunk_shape=(), grid_version="v1")

    if args.compare_blit_modes:
        rows = run_compare_blit_modes(provider, scheduler, compute, grid, channel, app)
        write_compare_report(args.out, args, offscreen, channel, rows)
        return

    view = ExploreView()
    view.resize(1024, 768)
    view.show()
    app.processEvents()

    ctrl = ExploreController(provider, scheduler, compute, grid, view, channel,
                              settle_ms=80, probe=True, blit_mode=args.blit_mode)
    ctrl.set_selection(method="tophat", params=(25,))

    fill_timings = run_scripted_sequence(ctrl, view, provider, seed=0)

    # Two real per-frame costs, measured separately (finding 3):
    #   range_handler_ms -- the range-changed handler (level pick, raw
    #                        request issuance, canvas resize).
    #   blit_tick_ms      -- the coalesced tick: canvas->RGBA compose +
    #                        setImage + setRect for every dirty layer.
    # The two costs are NOT 1:1 per frame (blit ticks fire only when dirty
    # and can outnumber or undernumber range handlers), so index-pairing
    # would be a methodology error. We report each distribution separately
    # plus a conservative WORST-CASE frame estimate = p95(range) + p95(blit)
    # (upper bound: assumes both worst halves land in one frame). Over-budget
    # counts are reported per distribution.
    range_ms = ctrl.timings["range_handler_ms"]
    blit_ms = ctrl.timings["blit_tick_ms"]
    def _p(v, q):
        return pct(v, q) or 0.0
    worst_case_p95 = _p(range_ms, 95) + _p(blit_ms, 95)
    over_budget_range = sum(1 for t in range_ms if t > FRAME_BUDGET_MS)
    over_budget_blit = sum(1 for t in blit_ms if t > FRAME_BUDGET_MS)

    window_agg = aggregate_windows(ctrl.timings["frame_events"])

    report = {
        "environment": environment_block(offscreen),
        "dataset_path": os.path.abspath(args.path),
        "channel": channel,
        "blit_mode": args.blit_mode,
        "rgba_canvas_allocs": ctrl.stats["rgba_canvas_allocs"],
        "tile_convert_ms_p50": pct(ctrl.timings["tile_convert_ms"], 50),
        "tile_convert_ms_p95": pct(ctrl.timings["tile_convert_ms"], 95),
        "set_image_ms_p50": pct(ctrl.timings["set_image_ms"], 50),
        "set_image_ms_p95": pct(ctrl.timings["set_image_ms"], 95),
        "window_aggregated_p50_ms": window_agg["p50"],
        "window_aggregated_p95_ms": window_agg["p95"],
        "windows_over_budget": window_agg["over_budget"],
        "n_windows": window_agg["n_windows"],
        "n_range_handler_samples": len(range_ms),
        "n_blit_tick_samples": len(blit_ms),
        "range_handler_ms_p50": pct(range_ms, 50),
        "range_handler_ms_p95": pct(range_ms, 95),
        "range_handler_ms_max": max(range_ms) if range_ms else None,
        "blit_tick_ms_p50": pct(blit_ms, 50),
        "blit_tick_ms_p95": pct(blit_ms, 95),
        "blit_tick_ms_max": max(blit_ms) if blit_ms else None,
        "frame_prep_worst_case_p95_ms": worst_case_p95,
        "range_handler_over_budget": over_budget_range,
        "blit_tick_over_budget": over_budget_blit,
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
        f.write("\n## Frame prep timing (range_handler + blit_tick)\n\n")
        f.write(f"- n_range_handler_samples: {report['n_range_handler_samples']}, "
                f"n_blit_tick_samples: {report['n_blit_tick_samples']}\n")
        f.write(f"- range_handler_ms: p50={fmt(report['range_handler_ms_p50'])} "
                f"p95={fmt(report['range_handler_ms_p95'])} "
                f"max={fmt(report['range_handler_ms_max'])}\n")
        f.write(f"- blit_tick_ms: p50={fmt(report['blit_tick_ms_p50'])} "
                f"p95={fmt(report['blit_tick_ms_p95'])} "
                f"max={fmt(report['blit_tick_ms_max'])}\n")
        f.write(f"- frame_prep worst-case estimate (p95 range + p95 blit, upper bound): "
                f"{fmt(report['frame_prep_worst_case_p95_ms'])} ms\n")
        f.write(f"- over 16.7ms budget: range_handler {report['range_handler_over_budget']}, "
                f"blit_tick {report['blit_tick_over_budget']} "
                f"(counted per distribution; blit samples exclude idle ticks)\n")
        f.write("\n")

        f.write("## Blit-path breakdown (measured-only)\n\n")
        f.write(f"- blit_mode: `{report['blit_mode']}`\n")
        f.write(f"- tile_convert_ms: p50={fmt(report['tile_convert_ms_p50'])} "
                f"p95={fmt(report['tile_convert_ms_p95'])}\n")
        f.write(f"- set_image_ms: p50={fmt(report['set_image_ms_p50'])} "
                f"p95={fmt(report['set_image_ms_p95'])}\n")
        f.write(f"- window-aggregated (16.7ms buckets, summed cost): "
                f"p50={fmt(report['window_aggregated_p50_ms'])} "
                f"p95={fmt(report['window_aggregated_p95_ms'])}, "
                f"{report['windows_over_budget']}/{report['n_windows']} windows over budget "
                f"(window-aggregation, NOT exact vsync frames)\n")
        f.write(f"- rgba_canvas_allocs: {report['rgba_canvas_allocs']} "
                f"(steady-state should be ~0 for uint8_incremental)\n")
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
