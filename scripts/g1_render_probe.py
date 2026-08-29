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


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--path", required=True)
    ap.add_argument("--channel", required=True, help="channel name or integer index")
    ap.add_argument("--out", default="/tmp/g1_render_probe")
    ap.add_argument("--offscreen", action="store_true")
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

    view = ExploreView()
    view.resize(1024, 768)
    view.show()
    app.processEvents()

    ctrl = ExploreController(provider, scheduler, compute, grid, view, channel,
                              settle_ms=80, probe=True)
    ctrl.set_selection(method="tophat", params=(25,))

    fill_timings = run_scripted_sequence(ctrl, view, provider, seed=0)

    frame_ms = ctrl.timings
    over_budget = sum(1 for t in frame_ms if t > FRAME_BUDGET_MS)

    report = {
        "environment": environment_block(offscreen),
        "dataset_path": os.path.abspath(args.path),
        "channel": channel,
        "n_frames": len(frame_ms),
        "frame_prep_ms_p50": pct(frame_ms, 50),
        "frame_prep_ms_p95": pct(frame_ms, 95),
        "frame_prep_ms_max": max(frame_ms) if frame_ms else None,
        "frames_over_16_7ms_budget": over_budget,
        "time_to_first_overview_pixel_ms": fill_timings["time_to_first_overview_pixel_ms"],
        "raw_fill_latency_ms_p50": pct(fill_timings["raw_fill_latencies_ms"], 50),
        "precise_fill_latency_ms_p50": pct(fill_timings["precise_fill_latencies_ms"], 50),
        "n_raw_fill_samples": len(fill_timings["raw_fill_latencies_ms"]),
        "n_precise_fill_samples": len(fill_timings["precise_fill_latencies_ms"]),
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
        f.write("\n## Frame prep timing\n\n")
        f.write(f"- n_frames: {report['n_frames']}\n")
        f.write(f"- p50: {fmt(report['frame_prep_ms_p50'])} ms\n")
        f.write(f"- p95: {fmt(report['frame_prep_ms_p95'])} ms\n")
        f.write(f"- max: {fmt(report['frame_prep_ms_max'])} ms\n")
        f.write(f"- frames over 16.7ms budget: {report['frames_over_16_7ms_budget']}\n\n")
        f.write("## Fill latencies\n\n")
        f.write(f"- time-to-first-overview-pixel: "
                f"{fmt(report['time_to_first_overview_pixel_ms'])} ms\n")
        f.write(f"- raw fill latency p50 ({report['n_raw_fill_samples']} samples): "
                f"{fmt(report['raw_fill_latency_ms_p50'])} ms\n")
        f.write(f"- precise fill latency p50 ({report['n_precise_fill_samples']} samples): "
                f"{fmt(report['precise_fill_latency_ms_p50'])} ms\n")
        f.write(f"\n## Controller stats\n\n")
        for k, v in report["stats"].items():
            f.write(f"- {k}: {v}\n")

    print(md_path)


if __name__ == "__main__":
    main()
