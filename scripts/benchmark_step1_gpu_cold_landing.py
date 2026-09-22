"""G3.2b.4: where a FIRST cold Patch and a FIRST cold Tissue landing spend.

Real slide, real corrected product, real public entries. Nothing here is
imported by a production module and nothing here writes to a project: the
demo project is opened READ-ONLY and the corrected zarr is opened with
`mode="r"` by the production opener itself.

WHAT IS REAL AND WHAT IS A STAND-IN, stated once:

* REAL: the slide (`biopsy.ome.tif`, 4 pyramid levels, LZW 256 tiles), the
  corrected cuCIM/tophat products written by Step0 for that project, the
  production `Step1WholeSlideMount` / `Step1ViewerHost` / `build_step1_stack`
  / `RawTileProvider` / `Step1TileProvider` / `TileScheduler` /
  `ExploreController` / `Step1GpuLayer` / `Step1GpuBinding`, the production
  `TILE_SIZE`, the production I/O and compute worker counts, and the two
  public navigation entries `mount.show_patch()` and `mount.jump_to_point()`.
* A STAND-IN: only the *owner window object* the mount reads its display
  state, fusion draft, ROI and corrected-decision answers from. It supplies
  exactly the attributes the mount already reads and nothing else -- the
  same shape `tests/test_step1_gpu_takeover.py` uses.

MEASUREMENT, and what each number is NOT:

* the GUI heartbeat is a 5 ms QTimer on the GUI thread. Its largest gap is
  the longest interval the event loop could not run. That is a real "the
  window is not responding" measure and it is NOT input-to-photon latency.
* a natural Qt paint is a Qt presentation event on the mounted widget. It is
  not a photon on the display, and it is counted only where no
  `grabFramebuffer()` has been called.
* "cold" here means NEW-REGION FIRST PRODUCT ACCESS / in-process cold path.
  The OS page cache is NOT dropped (that needs root and would disturb other
  windows), so no number below is called a cold DISK read.

Usage:
    python scripts/benchmark_step1_gpu_cold_landing.py OUT.json [label]
"""

import collections
import importlib.util
import json
import os
import pathlib
import sys
import threading
import time
from types import SimpleNamespace

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

from PyQt5 import QtCore, QtWidgets  # noqa: E402

from block01.core.display_identity import DatasetIdentity  # noqa: E402
from block01.core.fusion_domain import FusionDomainModel  # noqa: E402
from block01.ui.block01_display import ChannelDisplayState  # noqa: E402
from block01.ui.step1_draft_spec import STEP1_SCOPE  # noqa: E402
from block01.ui.step1_viewer_host import Step1ViewerHost  # noqa: E402
from block01.ui.step1_viewer_mount import (  # noqa: E402
    BACKEND_GPU, Step1WholeSlideMount)

APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

# ── the demo project, READ ONLY ───────────────────────────────────────
PROJECT = pathlib.Path(
    "/sda1/Fusion/tmp/rois/full_wsi_20260813_023722_f270")
SLIDE = "/sda1/Fusion/benchmark/biopsy.ome.tif"
CORRECTED_ZARR = str(PROJECT / "step0/corrected_channels.zarr")
ROI_NAME = "Full WSI"
ROI_BBOX = [0, 19480, 0, 21804]

#: One channel whose Step0 decision is `cucim` (a real corrected product on
#: disk) and one whose decision is `original` (the raw pyramid).
CORRECTED_CHANNEL = "CD22"
RAW_CHANNEL = "CD8"
NUCLEUS = "DAPI"

VIEW_W, VIEW_H = 1200, 900

#: Three configurations: one corrected channel on its own, one raw channel on
#: its own, and the four-corrected-channel overlay that is closer to what the
#: demo actually shows. Every one of them goes through the same public entries.
CONFIGURATIONS = [
    ("CD22_corrected", ["CD22"]),
    ("CD8_raw", ["CD8"]),
    ("four_corrected", ["CD22", "CD4", "PD1", "FOXP3"]),
]

#: The project's own three patches, plus two more of the same scale at
#: places this run never visits otherwise. All five go through the public
#: `show_patch()`.
PATCHES = [
    ("P1", [8896, 9984, 5632, 6944]),
    ("P2", [12672, 14080, 8256, 10144]),
    ("P3", [6848, 8000, 12384, 13376]),
    ("P4", [2048, 3136, 16384, 17696]),
    ("P5", [15360, 16448, 2048, 3360]),
]

#: Five Tissue Preview landings, each far from every patch above and from
#: each other, through the public `jump_to_point()`.
LANDINGS = [
    ("T1", 3500, 4200, 1200),
    ("T2", 17000, 18500, 1200),
    ("T3", 10500, 19000, 1200),
    ("T4", 16000, 11000, 1200),
    ("T5", 4800, 9600, 1200),
]


# ── instrumentation, all on public objects ────────────────────────────

class Heartbeat(QtCore.QObject):
    """A 5 ms tick on the GUI thread; its largest gap is the longest freeze."""

    def __init__(self, interval_ms=5):
        super().__init__()
        self.ticks = []
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(lambda: self.ticks.append(time.perf_counter()))
        self._timer.start()

    def reset(self):
        self.ticks = [time.perf_counter()]

    def worst_ms(self):
        gaps = [(b - a) * 1000.0 for a, b in zip(self.ticks, self.ticks[1:])]
        return round(max(gaps), 1) if gaps else 0.0

    def stop(self):
        self._timer.stop()


class PaintCounter(QtCore.QObject):
    """Counts natural Qt paints delivered to the mounted GPU widget."""

    def __init__(self, widget):
        super().__init__(widget)
        self.stamps = []
        widget.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.Paint:
            self.stamps.append(time.perf_counter())
        return False


class Ledger:
    """Every public request, read and delivery, with who asked and when."""

    def __init__(self):
        self.lock = threading.Lock()
        self.requests = []          # dicts
        self.reads = []             # dicts
        self.cache_gets = []        # (t, key, hit)
        self.reset_marks()

    def reset_marks(self):
        self.mark = SimpleNamespace(requests=len(self.requests),
                                    reads=len(self.reads),
                                    cache_gets=len(self.cache_gets))

    def since(self):
        with self.lock:
            return (self.requests[self.mark.requests:],
                    self.reads[self.mark.reads:],
                    self.cache_gets[self.mark.cache_gets:])


def _origin(generation):
    """Who issued this request -- the GPU binding, or the legacy controller."""
    if isinstance(generation, tuple) and generation and \
            generation[0] == "step1-gpu-binding":
        return f"gpu-binding/{generation[2]}"
    if isinstance(generation, tuple) and generation:
        return f"legacy-controller/{generation[0]}"
    return f"other/{generation!r}"


def _keyinfo(key):
    tile = getattr(key, "tile", None)
    if tile is None:
        return {"kind": type(key).__name__, "channel": getattr(key, "channel", "")}
    return {"kind": type(key).__name__,
            "channel": str(getattr(key, "channel", "")),
            "level": int(tile.level), "tx": int(tile.tx), "ty": int(tile.ty)}


def instrument(stack, ledger, table):
    """Wrap the PUBLIC methods of the stack's own scheduler/provider/cache.

    Nothing private is touched and no production module is edited: these are
    instance-level wrappers a benchmark puts on the objects it was handed,
    exactly as a stopwatch is put on a runner.
    """
    scheduler = stack.scheduler
    provider = stack.provider
    raw_cache = scheduler.raw_cache

    real_request = scheduler.request

    def request(req, callback):
        t0 = time.perf_counter()
        record = {"t_request": t0, "key": _keyinfo(req.key),
                  "raw_key": req.key, "priority": int(req.priority),
                  "origin": _origin(req.generation),
                  "t_callback": None, "error": None, "sync": False}
        with ledger.lock:
            ledger.requests.append(record)

        def wrapped(result, _rec=record):
            _rec["t_callback"] = time.perf_counter()
            _rec["error"] = result.error
            _rec["sync"] = (threading.current_thread() is threading.main_thread()
                            and _rec.get("_inside", False))
            return callback(result)

        record["_inside"] = True
        try:
            return real_request(req, wrapped)
        finally:
            record["_inside"] = False

    scheduler.request = request

    real_read_tile = provider.read_tile

    def read_tile(channel, tile):
        source = table.source_of(str(channel))
        t0 = time.perf_counter()
        try:
            return real_read_tile(channel, tile)
        finally:
            t1 = time.perf_counter()
            with ledger.lock:
                ledger.reads.append({
                    "t0": t0, "t1": t1, "ms": (t1 - t0) * 1000.0,
                    "channel": str(channel), "level": int(tile.level),
                    "tx": int(tile.tx), "ty": int(tile.ty),
                    "source": source,
                    "thread": threading.current_thread().name})

    provider.read_tile = read_tile

    real_get = raw_cache.get

    def get(key):
        value = real_get(key)
        with ledger.lock:
            ledger.cache_gets.append((time.perf_counter(), _keyinfo(key),
                                      value is not None))
        return value

    raw_cache.get = get


# ── the owner the mount reads from ────────────────────────────────────

def _decisions():
    with open(PROJECT / "step0/correction_config.json") as handle:
        return dict(json.load(handle)["channel_decisions"])


class Window:
    """Exactly the attributes the production mount reads from a window."""

    def __init__(self, state, domain):
        self.seeds = []

        def _seed(channel, nucleus=False):
            self.seeds.append(str(channel))
            return True

        self._display = SimpleNamespace(state=state, fusion=domain,
                                        request_mapping_seed=_seed)
        self.loader = SimpleNamespace(filepath=SLIDE)
        self._corrected_decisions = _decisions()
        self._corrected_zarr_path = CORRECTED_ZARR
        self._active_roi = {"name": ROI_NAME, "bbox_fullres": list(ROI_BBOX)}
        self.step0_output = {"channel_remap_config_hash": "g324-readonly"}


def _identity():
    return DatasetIdentity(path=SLIDE, fingerprint="g324")


def _domain(channels):
    domain = FusionDomainModel()
    domain.bind_dataset(_identity())
    domain.prepare_restore(_identity(), {
        "groups": {"markers": {"group_weight": 1.0,
                               "channels": {name: 1.0 for name in channels}}},
        "nucleus": {"channel": NUCLEUS, "weight": 0.0},
        "enabled": list(channels)})
    domain.commit_restore("g324")
    domain.install_committed_snapshot(
        {"hash": "g324", "fusion_config": domain.effective_config()})
    return domain


def _state(channels, order):
    state = ChannelDisplayState()
    state.bind(_identity(), install={"order": list(order)})
    with state.using_scope(STEP1_SCOPE):
        for name in order:
            state.set_display_visible(name, name in channels)
            state.set_color(name, "#00ff00")
            state.set_mapping(name, 0.0, 255.0, 1.0)
        state.set_selected_channel(channels[0])
    return state


def settle(seconds=0.2):
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        APP.processEvents()
        time.sleep(0.002)


def wait_for(predicate, timeout, heartbeat=None):
    deadline = time.perf_counter() + timeout
    while True:
        APP.processEvents()
        if predicate():
            return True
        if time.perf_counter() >= deadline:
            return False
        time.sleep(0.002)


def build_rig(channels, ledger):
    channels = list(channels)
    channel = channels[0]
    from block01.viewer.raw_tile_provider import RawTileProvider
    import tifffile
    with tifffile.TiffFile(SLIDE) as handle:
        order = []
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(handle.ome_metadata)
            ns = {"ome": "http://www.openmicroscopy.org/Schemas/OME/2016-06"}
            order = [c.attrib.get("Name") for c in root.findall(".//ome:Channel", ns)]
        except Exception:                                    # noqa: BLE001
            order = []
    order = [name for name in order if name] or [channel, NUCLEUS]

    state = _state(channels, order)
    domain = _domain(channels)
    window = Window(state, domain)

    from block01.ui import step1_viewer_host as host_module
    captured = {}

    def stack_factory(dataset_path, chan, table, parent):
        stack = host_module.build_step1_stack(dataset_path, chan, table, parent)
        captured["table"] = table
        instrument(stack, ledger, table)
        return stack

    host = Step1ViewerHost(stack_factory=stack_factory)
    host.setAttribute(QtCore.Qt.WA_DontShowOnScreen, True)
    host.resize(VIEW_W, VIEW_H)
    host.show()
    APP.processEvents()
    mount = Step1WholeSlideMount(window, host=host, gpu=True)
    mount.open(channel)
    settle(0.4)
    return SimpleNamespace(mount=mount, host=host, window=window, state=state,
                           domain=domain, channel=channel, channels=channels,
                           table=captured.get("table"),
                           stack=host.stack)


# ── one measured action ───────────────────────────────────────────────

def measure_action(rig, ledger, heart, paints, label, kind, run):
    """One public landing, with its stages, its counts and its honesty."""
    binding = rig.mount.gpu_binding
    layer = rig.mount.gpu_layer
    uploads_before = layer.cache_stats().get("uploads", 0)
    submits_before = len(binding.descriptor_history)
    stats_before = binding.stats()
    paints_before = len(paints.stamps)
    ledger.reset_marks()
    heart.reset()

    stages = {}
    t0 = time.perf_counter()

    def mark(name, when=None):
        stages.setdefault(name, round(((when or time.perf_counter()) - t0) * 1000.0, 2))

    if kind == "patch":
        moved = rig.mount.show_patch(run["bbox"])
    else:
        moved = rig.mount.jump_to_point(run["y"], run["x"], run["size"])
    mark("public_call_returned")

    snapshot = binding._latest_snapshot            # read-only, for geometry
    target_level = int(getattr(snapshot, "level", -1))
    target_keys = len(getattr(snapshot, "visible_tiles", ()) or ())

    def fine_ready():
        return all(binding._viewport_fine_ready(name) for name in rig.channels)

    # Let the landing finish naturally: no grabFramebuffer, no forced frame.
    deadline = time.perf_counter() + 180.0
    first_upload_at = None
    first_submit_at = None
    ready_at = None
    while time.perf_counter() < deadline:
        APP.processEvents()
        if first_upload_at is None and \
                layer.cache_stats().get("uploads", 0) > uploads_before:
            first_upload_at = time.perf_counter()
            mark("first_raw_texture_upload", first_upload_at)
        if first_submit_at is None and \
                len(binding.descriptor_history) > submits_before:
            first_submit_at = time.perf_counter()
            mark("first_gpu_submit", first_submit_at)
        coarse_now = set(binding.stats()["coarse_channels"])
        if ready_at is None and set(rig.channels) <= coarse_now and fine_ready():
            ready_at = time.perf_counter()
            mark("viewport_target_fine_ready", ready_at)
            break
        time.sleep(0.002)
    if ready_at is None:
        mark("viewport_target_fine_ready", None)
        stages["viewport_target_fine_ready"] = None
    settle(0.15)

    requests, reads, cache_gets = ledger.since()
    stats_after = binding.stats()

    by_origin = collections.Counter(item["origin"] for item in requests)
    binding_requests = [item for item in requests
                        if item["origin"].startswith("gpu-binding")]
    legacy_requests = [item for item in requests
                       if item["origin"].startswith("legacy-controller")]
    if binding_requests:
        mark("first_binding_request", min(i["t_request"] for i in binding_requests))
        done = [i["t_callback"] for i in binding_requests if i["t_callback"]]
        if done:
            mark("first_binding_callback", min(done))

    def queue_ms(items):
        # From the public request() call to the provider actually reading it.
        values = []
        reads_by_key = collections.defaultdict(list)
        for read in reads:
            reads_by_key[(read["channel"], read["level"], read["tx"], read["ty"])].append(read)
        for item in items:
            info = item["key"]
            if "level" not in info:
                continue
            bucket = reads_by_key.get((info["channel"], info["level"],
                                       info["tx"], info["ty"]))
            if not bucket:
                continue
            values.append((min(r["t0"] for r in bucket) - item["t_request"]) * 1000.0)
        return values

    read_keys = collections.Counter(
        (r["channel"], r["level"], r["tx"], r["ty"]) for r in reads)
    duplicate_reads = {f"{k[0]}/L{k[1]}/{k[2]},{k[3]}": n
                       for k, n in read_keys.items() if n > 1}
    corrected_reads = [r for r in reads if r["source"] == "corrected"]
    raw_reads = [r for r in reads if r["source"] == "raw"]

    binding_keys = {(i["key"].get("channel"), i["key"].get("level"),
                     i["key"].get("tx"), i["key"].get("ty"))
                    for i in binding_requests if "level" in i["key"]}
    legacy_keys = {(i["key"].get("channel"), i["key"].get("level"),
                    i["key"].get("tx"), i["key"].get("ty"))
                   for i in legacy_requests if "level" in i["key"]}

    def summary(values):
        if not values:
            return None
        values = sorted(values)
        return {"n": len(values), "min": round(values[0], 2),
                "median": round(values[len(values) // 2], 2),
                "max": round(values[-1], 2),
                "sum": round(sum(values), 2)}

    return {
        "label": label, "kind": kind, "moved": bool(moved),
        "channels": list(rig.channels),
        "source_of_channels": {n: rig.table.source_of(n) for n in rig.channels},
        "target_level": target_level,
        "target_visible_tiles": target_keys,
        "stages_ms": stages,
        "gui_worst_gap_ms": heart.worst_ms(),
        "natural_paints": len(paints.stamps) - paints_before,
        "counts": {
            "scheduler_requests_total": len(requests),
            "scheduler_requests_by_origin": dict(by_origin),
            "binding_requests": len(binding_requests),
            "legacy_controller_requests": len(legacy_requests),
            "unique_binding_rawkeys": len(binding_keys),
            "unique_legacy_rawkeys": len(legacy_keys),
            "legacy_keys_the_binding_never_asked_for":
                len(legacy_keys - binding_keys),
            "provider_reads": len(reads),
            "provider_reads_corrected": len(corrected_reads),
            "provider_reads_raw": len(raw_reads),
            "duplicate_real_reads_of_one_key": duplicate_reads,
            "raw_cache_hits": sum(1 for _t, _k, hit in cache_gets if hit),
            "raw_cache_misses": sum(1 for _t, _k, hit in cache_gets if not hit),
            "gpu_uploads": layer.cache_stats().get("uploads", 0) - uploads_before,
            "gpu_submits": len(binding.descriptor_history) - submits_before,
            "rejected_late_results":
                stats_after["rejected_late_results"]
                - stats_before["rejected_late_results"],
            "fine_requests_by_priority": stats_after["fine_requests_by_priority"],
        },
        "read_ms": {
            "corrected": summary([r["ms"] for r in corrected_reads]),
            "raw": summary([r["ms"] for r in raw_reads]),
            "all": summary([r["ms"] for r in reads]),
        },
        "queue_ms": {
            "binding": summary(queue_ms(binding_requests)),
            "legacy": summary(queue_ms(legacy_requests)),
        },
        "binding_stats": {k: stats_after[k] for k in
                          ("coarse_channels", "fine_channels", "fine_tiles",
                           "fine_levels", "retained_fine_last_epoch",
                           "requested_fine_last_epoch", "fine_budget_refused",
                           "last_error")},
    }


def run_channel(channels, ledger, out):
    channels = list(channels)
    ledger.__init__()
    rig = build_rig(channels, ledger)
    heart = Heartbeat()
    paints = PaintCounter(rig.mount.gpu_layer)
    report = {"channels": channels, "backend": rig.mount.backend,
              "gpu": rig.mount.gpu_status().get("environment", {}),
              "source_of_channels": {name: rig.table.source_of(name)
                                     for name in channels}}
    if rig.mount.backend != BACKEND_GPU:
        report["error"] = rig.mount.gpu_status().get("reason")
        rig.mount.close()
        return report

    # The channel's own complete coarse, from the moment the backend opened.
    t0 = time.perf_counter()
    ok = wait_for(lambda: set(channels) <= set(
        rig.mount.gpu_binding.stats()["coarse_channels"]), 1800.0)
    report["complete_coarse_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
    report["complete_coarse_landed"] = bool(ok)
    reqs, reads, _gets = ledger.since()
    report["complete_coarse_reads"] = len(reads)
    report["complete_coarse_read_ms"] = round(sum(r["ms"] for r in reads), 2)
    settle(0.3)

    report["cold_patches"] = []
    for name, bbox in PATCHES:
        report["cold_patches"].append(
            measure_action(rig, ledger, heart, paints, name, "patch",
                           {"bbox": bbox}))
    report["cold_landings"] = []
    for name, y, x, size in LANDINGS:
        report["cold_landings"].append(
            measure_action(rig, ledger, heart, paints, name, "tissue",
                           {"y": y, "x": x, "size": size}))
    report["hot_returns"] = []
    for i in range(3):
        report["hot_returns"].append(
            measure_action(rig, ledger, heart, paints, f"hot-P1-{i + 1}",
                           "patch", {"bbox": PATCHES[0][1]}))
        if i < 2:
            measure_action(rig, ledger, heart, paints, f"away-{i + 1}",
                           "patch", {"bbox": PATCHES[1][1]})

    heart.stop()
    rig.mount.close()
    settle(0.2)
    return report


def main():
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "cold_landing.json")
    label = sys.argv[2] if len(sys.argv) > 2 else "baseline"
    ledger = Ledger()
    report = {
        "label": label,
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "slide": SLIDE, "project": str(PROJECT),
        "corrected_zarr": CORRECTED_ZARR,
        "view": [VIEW_W, VIEW_H],
        "honesty": [
            "the OS page cache was NOT dropped; 'cold' means new-region "
            "first product access / in-process cold path, never a cold disk read",
            "the GUI heartbeat's worst gap is event-loop unresponsiveness, "
            "not input-to-photon latency",
            "natural paints are Qt presentation events on the mounted widget",
            "the demo project was opened read-only; nothing was written",
        ],
        "channels": {},
    }
    for name, group in CONFIGURATIONS:
        report["channels"][name] = run_channel(group, ledger, out)
        out.write_text(json.dumps(report, indent=2, default=str))
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
