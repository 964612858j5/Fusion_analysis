"""G3.2b.4C: where the FIRST enable of a cuCIM channel spends its seconds.

The trajectory this measures is the user's own, through public entries only:

    Step0 publishes CD22 as cuCIM  ->  Step1 comes forward (`sync_source`)
    ->  the user ticks CD22        ->  the current viewport is complete and
                                       sharp, on a natural Qt paint.

Nothing here is imported by a production module and nothing writes to a
project: the demo project is opened READ-ONLY and the corrected zarr is
opened `mode="r"` by the production opener itself. The rig, the window
stand-in and the instrumentation are reused from
`scripts/benchmark_step1_gpu_cold_landing.py` (G3.2b.4) so the numbers are
comparable with that block's.

WHAT EACH NUMBER IS NOT:

* the GUI heartbeat is a 5 ms QTimer on the GUI thread; its largest gap is
  the longest interval the event loop could not run. It is not
  input-to-photon latency.
* a natural paint is a Qt presentation event on the mounted GPU widget. It
  is not a photon, and it is counted only where no `grabFramebuffer()` has
  been called. The one forced grab this script can make is explicitly
  labelled `forced_pixel_check` and happens AFTER every timing is taken.
* "cold" is in-process first access. The OS page cache is NOT dropped (that
  needs root and would disturb other windows), so nothing here is a cold
  DISK read.
* the demo slide is NOT the user's slide. Absolute milliseconds here are not
  the user's real-machine seconds; the dataset is printed with every run.

Usage:
    python scripts/diagnose_step1_first_cucim_enable.py OUT.json [rounds]
"""

import collections
import importlib.util
import json
import math
import os
import pathlib
import sys
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = pathlib.Path(__file__).resolve().parents[1]
if "block01" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "block01", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["block01"] = _module
    _spec.loader.exec_module(_module)
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "scripts"))

import benchmark_step1_gpu_cold_landing as rig4  # noqa: E402

from PyQt5 import QtCore  # noqa: E402

from block01.ui.block01_display import ChannelDisplayState  # noqa: E402
from block01.ui.step1_draft_spec import STEP1_SCOPE  # noqa: E402
from block01.ui.step1_viewer_host import Step1ViewerHost  # noqa: E402
from block01.ui.step1_viewer_mount import (  # noqa: E402
    BACKEND_GPU, Step1WholeSlideMount)

APP = rig4.APP

#: What `viewer.open()` spends its time on, recorded once for the process.
#: Both are wrappers that call the real function and return its real result;
#: no production file is edited.
STACK_BUILD = []


def _install_deep_probes():
    from block01.ui import step1_viewer_host as host_module
    from block01.viewer.explore_view import ExploreController

    real_build = host_module.build_step1_stack

    def build(*args, **kwargs):
        t0 = time.perf_counter()
        try:
            return real_build(*args, **kwargs)
        finally:
            STACK_BUILD.append({"phase": "build_step1_stack", "t0": t0,
                                "t1": time.perf_counter()})
    host_module.build_step1_stack = build

    real_overview = ExploreController.load_overview

    def load_overview(self, *args, **kwargs):
        t0 = time.perf_counter()
        try:
            return real_overview(self, *args, **kwargs)
        finally:
            STACK_BUILD.append({"phase": "controller.load_overview", "t0": t0,
                                "t1": time.perf_counter()})
    ExploreController.load_overview = load_overview


_install_deep_probes()
CUCIM_CHANNEL = rig4.CORRECTED_CHANNEL      # CD22, a real cuCIM product
RAW_CHANNEL = rig4.RAW_CHANNEL              # CD8, the original pyramid

#: Two REAL datasets, both opened read-only. "demo" is the one G3.2b.4/4A
#: measured, so those numbers stay comparable; "real_roi" is a real study
#: project of the user's own kind -- a 52560x28800 slide, a real ROI and a
#: cuCIM product Step0 wrote there -- so nothing below rests on the demo
#: slide alone. Neither of them is the user's machine.
DATASETS = {
    "demo": {
        "project": "/sda1/Fusion/tmp/rois/full_wsi_20260813_023722_f270",
        "slide": "/sda1/Fusion/benchmark/biopsy.ome.tif",
        "corrected_zarr": "/sda1/Fusion/tmp/rois/full_wsi_20260813_023722_f270"
                          "/step0/corrected_channels.zarr",
        "decisions_json": "/sda1/Fusion/tmp/rois/full_wsi_20260813_023722_f270"
                          "/step0/correction_config.json",
        "roi_name": "Full WSI",
        "roi_bbox": [0, 19480, 0, 21804],
        "cucim_channel": "CD22",
        "raw_channel": "CD8",
        "nucleus": "DAPI",
        "patches": list(rig4.PATCHES),
    },
    # THE USER'S OWN SLIDE, and a project their Step0 wrote on 2026-09-21:
    # full WSI 59040x35520 (2.10 Gpx, 4.9x the demo's level-0 area) with CD22
    # and CD163 published as cuCIM. Read-only, like every dataset here.
    "user_wsi": {
        "project": "/nvme0n1p1/2025.12.20_Final_17209_16_Slice4/Scan1/"
                   "pipeline_v2/rois/full_wsi_20260921_175811_4908",
        "slide": "/nvme0n1p1/2025.12.20_Final_17209_16_Slice4/Scan1/"
                 "2025.12.20_Final_17209_16_Slice4_Scan1.ome.tif",
        "corrected_zarr": "/nvme0n1p1/2025.12.20_Final_17209_16_Slice4/Scan1/"
                          "pipeline_v2/rois/full_wsi_20260921_175811_4908/"
                          "step0/corrected_channels.zarr",
        "decisions_json": "/nvme0n1p1/2025.12.20_Final_17209_16_Slice4/Scan1/"
                          "pipeline_v2/rois/full_wsi_20260921_175811_4908/"
                          "step0/correction_config.json",
        "roi_name": "Full WSI",
        "roi_bbox": [0, 59040, 0, 35520],
        "cucim_channel": "CD22",
        "raw_channel": "CD8",
        "nucleus": "DAPI",
        "patches": [("U1", [12000, 13088, 8000, 9312]),
                    ("U2", [24000, 25088, 14000, 15312]),
                    ("U3", [36000, 37088, 20000, 21312]),
                    ("U4", [45000, 46088, 11000, 12312]),
                    ("U5", [18000, 19088, 24000, 25312])],
    },
    # A /tmp synthetic project written by the REAL Step0 worker, with the
    # persisted coarse plane beside it (G3.2b.4E). Renaming the sidecar away
    # turns the same project back into the old behaviour, which is how the
    # before/after below is measured on one product.
    "tmp_plane": {
        "project": "/tmp/g324e_save_bench/with0",
        "slide": "/tmp/g324e_save_bench/slide.ome.tif",
        "corrected_zarr": "/tmp/g324e_save_bench/with0/corrected_channels.zarr",
        "decisions_json": "/tmp/g324e_save_bench/with0/correction_config.json",
        "roi_name": "ROI_1",
        "roi_bbox": [0, 8704, 0, 6144],
        "cucim_channel": "CH_A",
        "raw_channel": "CH_B",
        "nucleus": "CH_B",
        "patches": [("S1", [1000, 2088, 800, 2112]),
                    ("S2", [4000, 5088, 2000, 3312]),
                    ("S3", [6000, 7088, 3000, 4312]),
                    ("S4", [2000, 3088, 4000, 5312]),
                    ("S5", [7000, 8088, 1000, 2312])],
    },
    "real_roi": {
        "project": "/sda1/Fusion/Chaofan/2025.12.21_Final_28127_22_Slice2/Scan1/test3",
        "slide": "/sda1/Fusion/Chaofan/2025.12.21_Final_28127_22_Slice2/Scan1/"
                 "2025.12.21_Final_28127_22_Slice2_Scan1.ome.tif",
        "corrected_zarr": "/sda1/Fusion/Chaofan/2025.12.21_Final_28127_22_Slice2"
                          "/Scan1/test3/corrected_channels.zarr",
        "decisions_json": "/sda1/Fusion/Chaofan/2025.12.21_Final_28127_22_Slice2"
                          "/Scan1/test3/correction_config.json",
        "roi_name": "ROI_1",
        "roi_bbox": [31328, 52544, 3104, 24192],
        "cucim_channel": "TOX",
        "raw_channel": "CD8",
        "nucleus": "DRAQ5",
        "patches": [("R1", [33000, 34088, 6000, 7312]),
                    ("R2", [40000, 41088, 12000, 13312]),
                    ("R3", [46000, 47088, 9000, 10312]),
                    ("R4", [36000, 37088, 18000, 19312]),
                    ("R5", [49000, 50088, 15000, 16312])],
    },
}


def select_dataset(name):
    """Point the shared rig at one of the two real datasets. Read-only."""
    global CUCIM_CHANNEL, RAW_CHANNEL
    spec = DATASETS[name]
    rig4.PROJECT = pathlib.Path(spec["project"])
    rig4.SLIDE = spec["slide"]
    rig4.CORRECTED_ZARR = spec["corrected_zarr"]
    rig4.ROI_NAME = spec["roi_name"]
    rig4.ROI_BBOX = list(spec["roi_bbox"])
    rig4.CORRECTED_CHANNEL = spec["cucim_channel"]
    rig4.RAW_CHANNEL = spec["raw_channel"]
    rig4.NUCLEUS = spec["nucleus"]
    rig4.PATCHES = list(spec["patches"])
    decisions_path = spec["decisions_json"]
    rig4._decisions = lambda: dict(
        json.loads(pathlib.Path(decisions_path).read_text())["channel_decisions"])
    CUCIM_CHANNEL = spec["cucim_channel"]
    RAW_CHANNEL = spec["raw_channel"]
    return spec


def _state_all_hidden(order, selected):
    """The display state Step1 opens with when nothing is ticked yet."""
    state = ChannelDisplayState()
    state.bind(rig4._identity(), install={"order": list(order)})
    with state.using_scope(STEP1_SCOPE):
        for name in order:
            state.set_display_visible(name, False)
            state.set_color(name, "#00ff00")
            state.set_mapping(name, 0.0, 255.0, 1.0)
        state.set_selected_channel(selected)
    return state


def _channel_order():
    import tifffile
    import xml.etree.ElementTree as ET
    with tifffile.TiffFile(rig4.SLIDE) as handle:
        try:
            root = ET.fromstring(handle.ome_metadata)
            ns = {"ome": "http://www.openmicroscopy.org/Schemas/OME/2016-06"}
            names = [c.attrib.get("Name") for c in root.findall(".//ome:Channel", ns)]
        except Exception:                                   # noqa: BLE001
            names = []
    return [n for n in names if n] or [CUCIM_CHANNEL, rig4.NUCLEUS]


class Phases:
    """Instance-level stopwatches on the mount's own rebind steps."""

    def __init__(self, mount):
        self.records = []
        self._mount = mount
        self._wrap(mount, "_stop_gpu_backend", "stop_gpu_backend")
        self._wrap(mount.viewer, "open", "viewer_open")
        self._wrap(mount, "_start_gpu_backend", "start_gpu_backend")
        self._wrap(mount.host, "teardown", "host_teardown")

    def _wrap(self, obj, name, label):
        real = getattr(obj, name)

        def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            try:
                return real(*args, **kwargs)
            finally:
                t1 = time.perf_counter()
                self.records.append({"phase": label, "t0": t0, "t1": t1,
                                     "ms": round((t1 - t0) * 1000.0, 2)})
        setattr(obj, name, wrapper)

    def watch_scheduler(self, stack):
        """`TileScheduler.shutdown()` joins in-flight reads on the GUI thread."""
        scheduler = stack.scheduler
        real = scheduler.shutdown

        def shutdown(*args, **kwargs):
            t0 = time.perf_counter()
            try:
                return real(*args, **kwargs)
            finally:
                t1 = time.perf_counter()
                self.records.append({"phase": "scheduler_shutdown_join",
                                     "t0": t0, "t1": t1,
                                     "ms": round((t1 - t0) * 1000.0, 2)})
        scheduler.shutdown = shutdown

    def since(self, t0):
        rows = list(self.records) + [
            {**r, "ms": round((r["t1"] - r["t0"]) * 1000.0, 2)}
            for r in STACK_BUILD]
        return sorted(
            ({**r, "at_ms": round((r["t0"] - t0) * 1000.0, 2)}
             for r in rows if r["t0"] >= t0),
            key=lambda r: r["t0"])


def build_rig(order, decisions_override=None, ledger=None):
    """A real Step1 mount with NOTHING ticked yet."""
    state = _state_all_hidden(order, CUCIM_CHANNEL)
    domain = rig4._domain([CUCIM_CHANNEL, RAW_CHANNEL])
    window = rig4.Window(state, domain)
    if decisions_override:
        window._corrected_decisions = dict(window._corrected_decisions)
        window._corrected_decisions.update(decisions_override)

    from block01.ui import step1_viewer_host as host_module
    captured = {}

    def stack_factory(dataset_path, chan, table, parent, **kwargs):
        # `**kwargs` is not decoration: G3.2b.4D lets the mount build the
        # stack WITHOUT the synchronous overview read when the GPU backend
        # is what will draw, and it leaves a factory that cannot take that
        # keyword alone. A bench whose factory swallowed it would keep
        # measuring the old path and call it the new one.
        # Module attribute lookup at call time: the deep probe above wrapped it.
        stack = host_module.build_step1_stack(dataset_path, chan, table, parent,
                                              **kwargs)
        captured["table"] = table
        rig4.instrument(stack, ledger, table)
        return stack

    host = Step1ViewerHost(stack_factory=stack_factory)
    host.setAttribute(QtCore.Qt.WA_DontShowOnScreen, True)
    host.resize(rig4.VIEW_W, rig4.VIEW_H)
    host.show()
    APP.processEvents()
    mount = Step1WholeSlideMount(window, host=host, gpu=True)
    phases = Phases(mount)
    mount.open(CUCIM_CHANNEL)
    rig4.settle(0.4)
    if host.stack is not None:
        phases.watch_scheduler(host.stack)
    return SimpleNamespace(mount=mount, host=host, window=window, state=state,
                           domain=domain, table=captured.get("table"),
                           phases=phases, stack=host.stack)


def summary(values):
    if not values:
        return None
    values = sorted(values)
    return {"n": len(values), "min": round(values[0], 2),
            "median": round(values[len(values) // 2], 2),
            "max": round(values[-1], 2), "sum": round(sum(values), 2)}


def measure_tick(rig, ledger, heart, paints, channel, label, timeout=180.0):
    """Tick ONE channel through the product's own display state, and time it."""
    mount = rig.mount
    binding = mount.gpu_binding
    layer = mount.gpu_layer
    uploads_before = layer.cache_stats().get("uploads", 0)
    submits_before = len(binding.descriptor_history)
    paints_before = len(paints.stamps)
    stats_before = binding.stats()
    ledger.reset_marks()
    heart.reset()

    stages = {}
    t0 = time.perf_counter()

    def mark(name, when=None):
        stages.setdefault(
            name, round(((when or time.perf_counter()) - t0) * 1000.0, 2))

    # THE PRODUCT'S OWN COMMAND: the Step1 row's tick.
    with rig.state.using_scope(STEP1_SCOPE):
        rig.state.set_display_visible(channel, True)
    mark("tick_returned")            # how long the GUI thread was held
    APP.processEvents()
    mark("events_processed")

    snapshot = binding._latest_snapshot
    target_level = int(getattr(snapshot, "level", -1))
    target_tiles = len(getattr(snapshot, "visible_tiles", ()) or ())

    coarse_at = fine_at = upload_at = submit_at = paint_at = None
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        APP.processEvents()
        stats = binding.stats()
        if coarse_at is None and channel in stats["coarse_channels"]:
            coarse_at = time.perf_counter()
            mark("complete_coarse_published", coarse_at)
        if upload_at is None and \
                layer.cache_stats().get("uploads", 0) > uploads_before:
            upload_at = time.perf_counter()
            mark("first_texture_upload", upload_at)
        if submit_at is None and \
                len(binding.descriptor_history) > submits_before:
            submit_at = time.perf_counter()
            mark("first_gpu_submit", submit_at)
        if coarse_at is not None and binding._viewport_fine_ready(channel):
            fine_at = time.perf_counter()
            mark("viewport_fine_complete", fine_at)
            break
        time.sleep(0.002)

    # The first NATURAL paint at or after the viewport was complete.
    rig4.settle(0.25)
    for stamp in paints.stamps[paints_before:]:
        if fine_at is not None and stamp >= fine_at:
            paint_at = stamp
            mark("first_natural_paint_after_complete", stamp)
            break

    requests, reads, cache_gets = ledger.since()
    stats_after = binding.stats()

    coarse_requests = [r for r in requests if r["priority"] == 100]
    fine_requests = [r for r in requests if r["priority"] != 100]
    coarse_reads = [r for r in reads
                    if r["level"] == int(mount.host.stack.provider.num_levels) - 1]
    fine_reads = [r for r in reads if r not in coarse_reads]
    corrected_reads = [r for r in reads if r["source"] == "corrected"]
    by_origin = collections.Counter(r["origin"] for r in requests)
    dup = collections.Counter((r["channel"], r["level"], r["tx"], r["ty"])
                              for r in reads)

    first_read = min((r["t0"] for r in reads), default=None)
    last_coarse_read = max((r["t1"] for r in coarse_reads), default=None)
    if first_read is not None:
        mark("first_provider_read", first_read)
    if last_coarse_read is not None:
        mark("last_coarse_read_returned", last_coarse_read)
    if fine_requests:
        mark("first_fine_request", min(r["t_request"] for r in fine_requests))
    if fine_reads:
        mark("first_fine_read", min(r["t0"] for r in fine_reads))
        # WHEN THE VIEWPORT'S OWN TILES WERE ALL IN HAND. Not when they were
        # shown: the accepted contract holds fine until the channel's
        # complete coarse transaction publishes. The gap between this and
        # `complete_coarse_published` is what that rule costs on this run.
        mark("all_fine_reads_returned", max(r["t1"] for r in fine_reads))

    return {
        "label": label, "channel": channel,
        "source_of_channel": rig.table.source_of(channel),
        "target_level": target_level, "target_visible_tiles": target_tiles,
        "num_levels": int(mount.host.stack.provider.num_levels),
        "stages_ms": stages,
        "gui_worst_gap_ms": heart.worst_ms(),
        "natural_paints": len(paints.stamps) - paints_before,
        "phases_ms": rig.phases.since(t0),
        "counts": {
            "requests_total": len(requests),
            "requests_by_origin": dict(by_origin),
            "coarse_requests": len(coarse_requests),
            "fine_requests": len(fine_requests),
            "provider_reads": len(reads),
            "coarse_reads": len(coarse_reads),
            "fine_reads": len(fine_reads),
            "corrected_reads": len(corrected_reads),
            "duplicate_real_reads": {f"{k[0]}/L{k[1]}/{k[2]},{k[3]}": n
                                     for k, n in dup.items() if n > 1},
            "raw_cache_hits": sum(1 for _t, _k, hit in cache_gets if hit),
            "raw_cache_misses": sum(1 for _t, _k, hit in cache_gets if not hit),
            "gpu_uploads": layer.cache_stats().get("uploads", 0) - uploads_before,
            "gpu_submits": len(binding.descriptor_history) - submits_before,
            "rejected_late_results": stats_after["rejected_late_results"]
                                     - stats_before["rejected_late_results"],
        },
        "read_ms": {"coarse": summary([r["ms"] for r in coarse_reads]),
                    "fine": summary([r["ms"] for r in fine_reads]),
                    "corrected": summary([r["ms"] for r in corrected_reads])},
        "binding_stats": {k: stats_after[k] for k in
                          ("coarse_channels", "fine_channels", "fine_tiles",
                           "fine_levels", "fine_budget_refused", "last_error")},
    }


def untick(rig, channel):
    with rig.state.using_scope(STEP1_SCOPE):
        rig.state.set_display_visible(channel, False)
    rig4.settle(0.2)


def arm_first_enable(order, channel, round_index, ledger):
    """A fresh stack, nothing ticked, then ONE tick."""
    rig = build_rig(order, ledger=ledger)
    if rig.mount.backend != BACKEND_GPU:
        reason = rig.mount.gpu_status().get("reason")
        rig.mount.close()
        return {"error": f"GPU backend unavailable: {reason}"}
    heart = rig4.Heartbeat()
    paints = rig4.PaintCounter(rig.mount.gpu_layer)
    out = measure_tick(rig, ledger, heart, paints, channel,
                       f"first_enable/{channel}/round{round_index}")
    # A second tick of the SAME channel, hot in this process.
    untick(rig, channel)
    out_hot = measure_tick(rig, ledger, heart, paints, channel,
                           f"hot_retick/{channel}/round{round_index}")
    heart.stop()
    rig.mount.close()
    return {"cold": out, "hot": out_hot}


def arm_patch_then_enable(order, round_index, ledger):
    """A fresh stack, a public patch landing, THEN the first tick.

    The whole-slide camera makes the target level the coarsest one, so fine
    and coarse are the same tiles. A patch is the realistic case: the
    viewport needs a handful of level-0/1 tiles while the coarse
    transaction still has to reduce the whole product.
    """
    rig = build_rig(order, ledger=ledger)
    if rig.mount.backend != BACKEND_GPU:
        reason = rig.mount.gpu_status().get("reason")
        rig.mount.close()
        return {"error": f"GPU backend unavailable: {reason}"}
    heart = rig4.Heartbeat()
    paints = rig4.PaintCounter(rig.mount.gpu_layer)
    name, bbox = rig4.PATCHES[round_index % len(rig4.PATCHES)]
    rig.mount.show_patch(bbox)
    rig4.settle(0.3)
    out = measure_tick(rig, ledger, heart, paints, CUCIM_CHANNEL,
                       f"patch_then_enable/{name}/round{round_index}")
    heart.stop()
    rig.mount.close()
    return {"patch": name, "cold": out}


def arm_save_then_enable(order, round_index, ledger):
    """Step0 publishes a NEW cuCIM product, Step1 rebinds, then the tick."""
    rig = build_rig(order, decisions_override={CUCIM_CHANNEL: "original"},
                    ledger=ledger)
    if rig.mount.backend != BACKEND_GPU:
        reason = rig.mount.gpu_status().get("reason")
        rig.mount.close()
        return {"error": f"GPU backend unavailable: {reason}"}
    heart = rig4.Heartbeat()
    paints = rig4.PaintCounter(rig.mount.gpu_layer)

    # Step0 Save published: the window's decision for CD22 is now cuCIM and
    # the product on disk is the one the real Step0 wrote for this project.
    rig.window._corrected_decisions = dict(rig.window._corrected_decisions)
    rig.window._corrected_decisions[CUCIM_CHANNEL] = "cucim"
    heart.reset()
    t0 = time.perf_counter()
    moved = rig.mount.viewer.source_moved()
    rebound = rig.mount.sync_source("handoff")
    t1 = time.perf_counter()
    rig4.settle(0.2)
    rebind = {"source_moved": bool(moved), "rebound": bool(rebound),
              "sync_source_ms": round((t1 - t0) * 1000.0, 2),
              "gui_worst_gap_ms": heart.worst_ms(),
              "phases_ms": rig.phases.since(t0)}
    if rig.host.stack is not None:
        rig.phases.watch_scheduler(rig.host.stack)
    paints = rig4.PaintCounter(rig.mount.gpu_layer)

    out = measure_tick(rig, ledger, heart, paints, CUCIM_CHANNEL,
                       f"save_then_enable/round{round_index}")
    heart.stop()
    rig.mount.close()
    return {"rebind": rebind, "cold": out}


def _project_fingerprint(root):
    """Every file's size and mtime -- the read-only proof, before and after."""
    root = pathlib.Path(root)
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            stat = path.stat()
            out[str(path.relative_to(root))] = [stat.st_size, stat.st_mtime_ns]
    return out


def main():
    out_path = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                            else "/tmp/g324c_first_cucim_enable.json")
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    dataset = sys.argv[3] if len(sys.argv) > 3 else "demo"
    spec = select_dataset(dataset)
    before = _project_fingerprint(spec["project"])
    order = _channel_order()
    ledger = rig4.Ledger()

    report = {
        "block": "G3.2b.4C (first cuCIM enable, segmented)",
        "dataset": {"name": dataset, "slide": rig4.SLIDE,
                    "project": str(rig4.PROJECT),
                    "corrected_zarr": rig4.CORRECTED_ZARR,
                    "roi_bbox": rig4.ROI_BBOX,
                    "cucim_channel": CUCIM_CHANNEL, "raw_channel": RAW_CHANNEL,
                    "note": "a REAL project opened read-only; NOT the user's "
                            "machine and not necessarily their slide"},
        "view": [rig4.VIEW_W, rig4.VIEW_H],
        "os_page_cache_dropped": False,
        "rounds": rounds,
        "arms": {"first_cucim": [], "first_raw": [], "save_then_cucim": [],
                 "patch_then_cucim": []},
    }
    for index in range(rounds):
        print(f"── round {index + 1}/{rounds}", flush=True)
        for name, channel in (("first_cucim", CUCIM_CHANNEL),
                              ("first_raw", RAW_CHANNEL)):
            result = arm_first_enable(order, channel, index, ledger)
            report["arms"][name].append(result)
            cold = (result.get("cold") or {}).get("stages_ms", {})
            print(f"   {name:16s} coarse={cold.get('complete_coarse_published')} "
                  f"complete={cold.get('viewport_fine_complete')} ms", flush=True)
        result = arm_patch_then_enable(order, index, ledger)
        report["arms"]["patch_then_cucim"].append(result)
        cold = (result.get("cold") or {}).get("stages_ms", {})
        print(f"   patch_then_cucim coarse={cold.get('complete_coarse_published')} "
              f"fine_reads_done={cold.get('all_fine_reads_returned')} "
              f"complete={cold.get('viewport_fine_complete')} ms", flush=True)

        result = arm_save_then_enable(order, index, ledger)
        report["arms"]["save_then_cucim"].append(result)
        cold = (result.get("cold") or {}).get("stages_ms", {})
        print(f"   save_then_cucim  rebind="
              f"{(result.get('rebind') or {}).get('sync_source_ms')} "
              f"coarse={cold.get('complete_coarse_published')} "
              f"complete={cold.get('viewport_fine_complete')} ms", flush=True)

    def medians(arm, path):
        values = []
        for item in report["arms"][arm]:
            node = item
            for step in path:
                node = (node or {}).get(step) if isinstance(node, dict) else None
            if isinstance(node, (int, float)):
                values.append(float(node))
        return summary(values)

    report["medians"] = {
        "first_cucim_complete": medians("first_cucim",
                                        ["cold", "stages_ms", "viewport_fine_complete"]),
        "first_cucim_coarse": medians("first_cucim",
                                      ["cold", "stages_ms", "complete_coarse_published"]),
        "first_raw_complete": medians("first_raw",
                                      ["cold", "stages_ms", "viewport_fine_complete"]),
        "hot_retick_cucim_complete": medians("first_cucim",
                                             ["hot", "stages_ms", "viewport_fine_complete"]),
        "save_rebind_ms": medians("save_then_cucim", ["rebind", "sync_source_ms"]),
        "save_then_cucim_complete": medians("save_then_cucim",
                                            ["cold", "stages_ms", "viewport_fine_complete"]),
        "patch_then_cucim_complete": medians("patch_then_cucim",
                                             ["cold", "stages_ms", "viewport_fine_complete"]),
        "patch_then_cucim_coarse": medians("patch_then_cucim",
                                           ["cold", "stages_ms", "complete_coarse_published"]),
        "patch_then_cucim_fine_reads_done": medians(
            "patch_then_cucim", ["cold", "stages_ms", "all_fine_reads_returned"]),
    }
    after = _project_fingerprint(spec["project"])
    report["project_readonly"] = {
        "files_before": len(before), "files_after": len(after),
        "changed": sorted(k for k in set(before) | set(after)
                          if before.get(k) != after.get(k))}
    out_path.write_text(json.dumps(report, indent=1, default=str))
    print(f"\nwrote {out_path}")
    for key, value in report["medians"].items():
        print(f"  {key:32s} {value}")
    print("  project files changed:", report["project_readonly"]["changed"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
