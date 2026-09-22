"""G3.2b.4B2C: what stopping the hidden controller's own tile reads buys.

Two arms over FRESH WINDOWS, told apart only by the controller's own public
switch, so the A/B is the product's own interface and nothing else:

* `before` -- `set_viewport_requests_enabled(True)`, i.e. exactly what the
  mount did before this block: the GPU layer is the picture, and the
  controller underneath it goes on reading tiles for its own invisible
  ImageItems;
* `after`  -- what the mount now does: the switch is off from the moment the
  GPU layer is confirmed initialised.

Everything else is identical: same slide, same corrected products, same
channel, same patches, same landing points, same order.

WHAT IS REAL HERE

The whole window is the product's: a real `MainWindow`, its real `Step0Page`
and Step1 tab, the real `Step1WholeSlideMount` with its real
`Step1ViewerHost` / `ExploreStack` / `TileScheduler` / `Step1TileProvider` /
`Step1GpuLayer` / `Step1GpuBinding`, over the real demo slide and the real
Step0 cuCIM/tophat products.

* the PATCH action is a real `QTest.mouseClick` on the real `P1..Pn`
  button that `_rebuild_patch_buttons` builds, whose `clicked` signal is
  wired to `_select_preview_patch` -- so everything that slot does
  (session autosave, channel caching, preview refresh, loader) runs;
* the TISSUE action emits the real `navigate_requested(y, x)` that the
  shared Tissue Preview's overview emits on a click, into the real
  `_on_step1_tissue_navigate` slot. G3.2b.3 already proved a real click on
  that canvas produces exactly this signal; building the popup's overview
  over the real slide as well would add nothing the two paths differ by.

Nothing private is called to fake a completion, and no production file is
edited: the counters below only COUNT.

HONESTY, stated once and kept

* the OS page cache is NOT dropped (needs root, would disturb other
  windows). "Cold" here means what the user actually does: a region of the
  slide this session has never visited. It is written as
  "new-region first product access", never as a cold DISK read.
* the GUI heartbeat is a 5 ms QTimer on the GUI thread; its largest gap is
  how long the event loop could not run. It is NOT input-to-photon latency.
* a natural paint is a Qt presentation event on the mounted widget, counted
  only where no `grabFramebuffer()` was called.
* the demo project and the slide are opened READ-ONLY. Step1's session
  autosave is left switched ON -- it is one of the things under measurement
  -- but its output directory is pointed at a scratch dir, so the real
  project is never written.

Usage: python scripts/benchmark_step1_patch_vs_tissue.py OUT.json [label]
"""

import collections
import importlib.util
import json
import os
import pathlib
import statistics
import sys
import threading
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

from PyQt5 import QtCore, QtTest, QtWidgets  # noqa: E402

APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

PROJECT = pathlib.Path("/sda1/Fusion/tmp/rois/full_wsi_20260813_023722_f270")
SLIDE = "/sda1/Fusion/benchmark/biopsy.ome.tif"
SCRATCH = pathlib.Path(os.environ.get(
    "B2_SCRATCH", "/tmp/block01_g324b2_scratch"))
#: The channels Step1 actually shows. One is the simplest comparison; the
#: user's real session has several, which is when the eight I/O workers are
#: actually saturated and the wasted reads can cost wall-clock rather than
#: just capacity. `B2C_CHANNELS` selects it.
CHANNELS = [c for c in os.environ.get("B2C_CHANNELS", "CD22").split(",") if c]
CHANNEL = CHANNELS[0]                 # Step0 decision `cucim`, real product
NUCLEUS = "DAPI"

#: Five level-0 points this run never otherwise visits, for the Tissue
#: landings, and two extra patch rectangles so there are five patches.
TISSUE_POINTS = [(4200, 5200), (15200, 16800), (9600, 18400),
                 (16800, 4200), (6200, 9200)]
#: The project's own three patches are not enough: every measured landing
#: has to be a region this run has never visited, and there are eleven of
#: them. These six are the SAME SCALE as the real ones, at places nothing
#: else in this run touches. The product treats them identically -- they go
#: into `_all_patches` and get real P-buttons.
EXTRA_PATCHES = [(2048, 3136, 16384, 17696),   # 3
                 (15360, 16448, 2048, 3360),   # 4
                 (3200, 4288, 9600, 10912),    # 5
                 (11800, 12888, 16000, 17312), # 6
                 (17600, 18688, 12800, 14112), # 7
                 (5400, 6488, 2200, 3512),     # 8
                 (12400, 13488, 3000, 4312),   # 9   C sweep
                 (7600, 8688, 17000, 18312),   # 10  C sweep
                 (18200, 19288, 8000, 9312),   # 11  C sweep
                 (10200, 11288, 11000, 12312)] # 12  C sweep

#: Four never-landed points for the zoom sweep, one per zoom.
SWEEP_POINTS = [(3400, 13200), (14200, 6400), (8200, 3400), (16200, 15200)]
#: The zooms a user might be working at when they click a patch, as the
#: SHORT SIDE of the viewport in level-0 pixels.
SWEEP_ZOOMS = [300, 700, 1500, 3000]

#: WHERE THE CAMERA IS PARKED BEFORE EVERY MEASURED ACTION.
#:
#: This is the whole reason the first attempt at this benchmark measured
#: nothing: a Tissue landing keeps the CURRENT viewport size, so once a patch
#: click had set the camera to patch size, every later Tissue landing
#: inherited exactly that size and the two entries became the same
#: measurement. The user does not work that way -- they are zoomed in on
#: something and then click a patch. So every action below starts from the
#: SAME zoomed-in camera, parked on a region that is visited once and never
#: measured, and lands somewhere this run has never been.
HOME_Y, HOME_X = 2600, 2600
HOME_SIZE = 700


# ── instrumentation ───────────────────────────────────────────────────

class Heartbeat(QtCore.QObject):
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
    def __init__(self, widget):
        super().__init__(widget)
        self.stamps = []
        widget.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.Paint:
            self.stamps.append(time.perf_counter())
        return False


class Ledger:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests = []
        self.reads = []
        self.cache_gets = []
        self.reset_marks()

    def reset_marks(self):
        self.mark = (len(self.requests), len(self.reads), len(self.cache_gets))

    def since(self):
        with self.lock:
            return (self.requests[self.mark[0]:], self.reads[self.mark[1]:],
                    self.cache_gets[self.mark[2]:])


def _origin(generation):
    if isinstance(generation, tuple) and generation:
        if generation[0] == "step1-gpu-binding":
            return f"gpu-binding/{generation[2]}"
        return f"legacy-controller/{generation[0]}"
    return f"other/{generation!r}"


def _keyinfo(key):
    tile = getattr(key, "tile", None)
    if tile is None:
        return {"kind": type(key).__name__,
                "channel": str(getattr(key, "channel", ""))}
    return {"kind": type(key).__name__,
            "channel": str(getattr(key, "channel", "")),
            "level": int(tile.level), "tx": int(tile.tx), "ty": int(tile.ty)}


def instrument_stack(stack, ledger, table):
    scheduler = stack.scheduler
    provider = stack.provider
    raw_cache = scheduler.raw_cache
    real_request = scheduler.request

    def request(req, callback):
        record = {"t_request": time.perf_counter(), "key": _keyinfo(req.key),
                  "priority": int(req.priority),
                  "origin": _origin(req.generation),
                  "t_callback": None, "error": None}
        with ledger.lock:
            ledger.requests.append(record)

        def wrapped(result, _rec=record):
            _rec["t_callback"] = time.perf_counter()
            _rec["error"] = getattr(result, "error", None)
            return callback(result)

        return real_request(req, wrapped)

    scheduler.request = request
    real_read_tile = provider.read_tile

    def read_tile(channel, tile):
        source = table.source_of(str(channel)) if table is not None else "?"
        t0 = time.perf_counter()
        try:
            return real_read_tile(channel, tile)
        finally:
            t1 = time.perf_counter()
            with ledger.lock:
                ledger.reads.append({
                    "t0": t0, "t1": t1, "ms": (t1 - t0) * 1000.0,
                    "channel": str(channel), "level": int(tile.level),
                    "tx": int(tile.tx), "ty": int(tile.ty), "source": source,
                    "thread": threading.current_thread().name})

    provider.read_tile = read_tile
    real_get = raw_cache.get

    def get(key):
        value = real_get(key)
        with ledger.lock:
            ledger.cache_gets.append(value is not None)
        return value

    raw_cache.get = get


class ProductWork:
    """Did the OTHER product steps a patch click owns actually run?"""

    def __init__(self, window):
        self.window = window
        self.loaders = 0
        self.ensure_channels_cached = 0
        self.refresh_patch_preview = 0
        self.session_saves_scheduled = 0
        self.session_saves_written = 0
        self.session_save_ms = []
        from block01.ui import main_window as mw

        real_thread = mw.PreviewLoaderThread

        def counting_thread(*a, **k):
            self.loaders += 1
            return real_thread(*a, **k)

        mw.PreviewLoaderThread = counting_thread
        self._restore = (mw, real_thread)

        for name in ("_ensure_channels_cached", "_refresh_patch_preview"):
            self._wrap(name)
        self._wrap_save()

    def _wrap(self, name):
        real = getattr(self.window, name)
        counter = name.lstrip("_")

        def wrapper(*a, **k):
            setattr(self, counter, getattr(self, counter) + 1)
            return real(*a, **k)

        setattr(self.window, name, wrapper)

    def _wrap_save(self):
        real_schedule = self.window._schedule_step1_session_save
        real_save = self.window._save_step1_session

        def schedule(*a, **k):
            self.session_saves_scheduled += 1
            return real_schedule(*a, **k)

        def save(*a, **k):
            start = time.perf_counter()
            try:
                return real_save(*a, **k)
            finally:
                self.session_saves_written += 1
                self.session_save_ms.append(
                    (time.perf_counter() - start) * 1000.0)

        self.window._schedule_step1_session_save = schedule
        self.window._save_step1_session = save

    def snapshot(self):
        return {"preview_loader_threads": self.loaders,
                "ensure_channels_cached": self.ensure_channels_cached,
                "refresh_patch_preview": self.refresh_patch_preview,
                "session_saves_scheduled": self.session_saves_scheduled,
                "session_saves_written": self.session_saves_written,
                "session_save_ms": [round(v, 2) for v in self.session_save_ms]}

    def restore(self):
        module, real = self._restore
        module.PreviewLoaderThread = real


# ── the window ────────────────────────────────────────────────────────

def drain(ms=400, step=10):
    end = time.perf_counter() + ms / 1000.0
    while time.perf_counter() < end:
        APP.processEvents()
        QtTest.QTest.qWait(step)


def wait_for(predicate, timeout_s=180.0):
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        APP.processEvents()
        if predicate():
            return True
        QtTest.QTest.qWait(5)
    return False


def build_window(ledger):
    from block01.core.io_loader import OMETIFFLoader
    from block01.ui.main_window import MainWindow
    from block01.ui.step1_draft_spec import STEP1_SCOPE

    SCRATCH.mkdir(parents=True, exist_ok=True)
    decisions = json.loads(
        (PROJECT / "step0/correction_config.json").read_text())["channel_decisions"]
    patches = json.loads((PROJECT / "step0/patch_config.json").read_text())
    roi = json.loads((PROJECT / "step0/roi_config.json").read_text())[0]

    window = MainWindow()
    window.loader = OMETIFFLoader(SLIDE)
    window._step0.loader = window.loader
    window._step0.nucleus_channel = NUCLEUS
    window._step0.ome_path = SLIDE
    window._step0._rebuild_channel_list()
    names = window.loader.channel_names()
    window.config.set_channels(names)

    manifest = SCRATCH / "step0_roi_result.json"
    manifest.write_text(json.dumps(
        {"handoff_schema_version": 2,
         "channel_remap_config_hash": "g324b2",
         "source_identity": {"dataset_path": SLIDE, "stage": "raw"}}),
        encoding="utf-8")
    # THE REAL PROJECT IS NEVER WRITTEN: the session autosave stays on (it is
    # one of the things measured) and lands here instead.
    window.step0_output = {"step1_dir": str(SCRATCH), "output_dir": str(SCRATCH),
                           "step0_manifest_path": str(manifest),
                           "channel_remap_config_hash": "g324b2"}
    window._corrected_decisions = dict(decisions)
    window._corrected_zarr_path = str(PROJECT / "step0/corrected_channels.zarr")
    window._active_roi = {"name": roi["name"],
                          "bbox_fullres": list(roi["bbox_fullres"])}
    window._all_patches = ([tuple(p["bbox_fullres"]) for p in patches]
                           + [tuple(b) for b in EXTRA_PATCHES])
    window._rebuild_patch_buttons(window._all_patches)
    window.resize(1400, 900)
    window.show()
    APP.processEvents()

    window._set_step_active(1)
    assert wait_for(lambda: getattr(window, "_step1_mount", None) is not None
                    and window._step1_mount.host.stack is not None, 120.0), \
        "Step1 never opened its viewer"
    mount = window._step1_mount

    # The user's own two gestures: tick the channel, give it a weight, and
    # give it an Intensity window.
    state = window._display.state
    with state.using_scope(STEP1_SCOPE):
        for name in set(CHANNELS) | {NUCLEUS}:
            state.set_display_visible(name, name in CHANNELS)
            state.set_mapping(name, 0.0, 30.0, 1.0)
        state.set_selected_channel(CHANNEL)
    domain = mount._domain
    domain.initialize_dataset({"markers": {c: 1.0 for c in CHANNELS}},
                              NUCLEUS, channels=names)
    for name in CHANNELS:
        domain.set_fusion_enabled(name, True)
        domain.edit_channel_weight(name, 1.0)
    mount.gpu_binding.refresh_display()

    table = getattr(mount.host.stack.provider, "source_table", None)
    instrument_stack(mount.host.stack, ledger, table)

    assert wait_for(lambda: set(CHANNELS) <= set(
        mount.gpu_binding.stats()["coarse_channels"]), 1800.0), \
        "a channel's complete coarse never landed"
    drain(800)
    return window, mount, table


# ── one measured action ───────────────────────────────────────────────

def park(mount, size=HOME_SIZE):
    """Put the camera back on the parking spot, at the zoom a user works at.

    Public entry only (`host.jump_to`, the same one both actions use). The
    parking region is read once at the start and is never one of the measured
    targets, so what it warms is never what a measurement then asks for.
    """
    mount.host.jump_to(HOME_Y, HOME_X, size, size)
    drain(1600)


def _camera(mount):
    rect = mount.host.stack.view.view_box.viewRect()
    return {"x": round(rect.x(), 2), "y": round(rect.y(), 2),
            "w": round(rect.width(), 2), "h": round(rect.height(), 2),
            "cx": round(rect.x() + rect.width() / 2.0, 2),
            "cy": round(rect.y() + rect.height() / 2.0, 2)}


def measure(window, mount, ledger, heart, paints, work, label, kind, arg,
            table):
    binding = mount.gpu_binding
    layer = mount.gpu_layer
    uploads0 = layer.cache_stats().get("uploads", 0)
    bytes0 = layer.cache_stats().get("bytes", 0)
    submits0 = len(binding.descriptor_history)
    stats0 = binding.stats()
    paints0 = len(paints.stamps)
    work0 = work.snapshot()
    ledger.reset_marks()
    heart.reset()

    camera_before = _camera(mount)
    stages = {}
    t0 = time.perf_counter()

    def mark(name, when=None):
        stages.setdefault(
            name, round(((when or time.perf_counter()) - t0) * 1000.0, 2))

    if kind == "patch":
        button = window._patch_sel_btns[arg]
        QtTest.QTest.mouseClick(button, QtCore.Qt.LeftButton)
    else:
        window._on_step1_tissue_navigate(int(arg[0]), int(arg[1]))
    mark("T1_slot_returned")

    snapshot = binding._latest_snapshot
    target_level = int(getattr(snapshot, "level", -1))
    visible = sorted(getattr(snapshot, "visible_tiles", ()) or ())
    camera_after = _camera(mount)

    def ready():
        return all(binding._viewport_fine_ready(c) for c in CHANNELS)

    first_upload = first_submit = ready_at = None
    deadline = time.perf_counter() + 240.0
    while time.perf_counter() < deadline:
        APP.processEvents()
        if first_upload is None and \
                layer.cache_stats().get("uploads", 0) > uploads0:
            first_upload = time.perf_counter()
            mark("T10_first_raw_texture_upload", first_upload)
        if first_submit is None and \
                len(binding.descriptor_history) > submits0:
            first_submit = time.perf_counter()
            mark("T10b_first_gpu_submit", first_submit)
        if ready_at is None and ready():
            ready_at = time.perf_counter()
            mark("T9_viewport_target_fine_ready", ready_at)
            break
        QtTest.QTest.qWait(2)
    if ready_at is None:
        stages["T9_viewport_target_fine_ready"] = None
    # The first NATURAL paint after everything is in hand.
    before_paints = len(paints.stamps)
    drain(250)
    if len(paints.stamps) > before_paints:
        mark("T11_first_natural_paint_after_ready",
             paints.stamps[before_paints])

    requests, reads, gets = ledger.since()
    stats1 = binding.stats()
    work1 = work.snapshot()

    binding_reqs = [r for r in requests if r["origin"].startswith("gpu-binding")]
    legacy_reqs = [r for r in requests if r["origin"].startswith("legacy-controller")]
    if binding_reqs:
        mark("T4_first_binding_request",
             min(r["t_request"] for r in binding_reqs))
        done = [r["t_callback"] for r in binding_reqs if r["t_callback"]]
        if done:
            mark("T8_first_binding_callback", min(done))
    if reads:
        mark("T6_first_provider_read_start", min(r["t0"] for r in reads))
        mark("T6b_first_provider_read_end", min(r["t1"] for r in reads))
    if requests:
        mark("T5_first_scheduler_request",
             min(r["t_request"] for r in requests))
        mark("T7_last_request_issued",
             max(r["t_request"] for r in requests))

    def keyset(items):
        return {(r["key"].get("channel"), r["key"].get("level"),
                 r["key"].get("tx"), r["key"].get("ty"))
                for r in items if "level" in r["key"]}

    read_counts = collections.Counter(
        (r["channel"], r["level"], r["tx"], r["ty"]) for r in reads)
    corrected = [r for r in reads if r["source"] == "corrected"]
    raw_reads = [r for r in reads if r["source"] == "raw"]

    def summary(values):
        if not values:
            return None
        ordered = sorted(values)
        return {"n": len(ordered), "min": round(ordered[0], 2),
                "median": round(statistics.median(ordered), 2),
                "max": round(ordered[-1], 2), "sum": round(sum(ordered), 2)}

    downsample = None
    try:
        downsample = mount.host.stack.provider.level_downsample(target_level)
    except Exception:                                       # noqa: BLE001
        pass

    return {
        "label": label, "kind": kind,
        "patch_index": arg if kind == "patch" else None,
        "patch_bbox": (list(window._all_patches[arg]) if kind == "patch"
                       else None),
        "tissue_point_yx": (list(arg) if kind == "tissue" else None),
        "camera_before": camera_before, "camera_after": camera_after,
        "target_level": target_level, "level_downsample": downsample,
        "visible_tiles": len(visible),
        "visible_tile_range": ({"tx": [min(t[0] for t in visible),
                                       max(t[0] for t in visible)],
                                "ty": [min(t[1] for t in visible),
                                       max(t[1] for t in visible)]}
                               if visible else None),
        "visible_world_area_l0": round(
            camera_after["w"] * camera_after["h"], 1),
        "stages_ms": stages,
        "gui_worst_gap_ms": heart.worst_ms(),
        "natural_paints": len(paints.stamps) - paints0,
        "counts": {
            "scheduler_requests": len(requests),
            "requests_by_origin": dict(collections.Counter(
                r["origin"] for r in requests)),
            "binding_requests": len(binding_reqs),
            "legacy_requests": len(legacy_reqs),
            "unique_binding_rawkeys": len(keyset(binding_reqs)),
            "unique_legacy_rawkeys": len(keyset(legacy_reqs)),
            "legacy_keys_binding_never_asked": len(
                keyset(legacy_reqs) - keyset(binding_reqs)),
            "duplicate_requests": len(requests) - len(keyset(requests)),
            "provider_reads": len(reads),
            "provider_reads_corrected": len(corrected),
            "provider_reads_raw": len(raw_reads),
            "duplicate_real_reads": {f"{k[0]}/L{k[1]}/{k[2]},{k[3]}": n
                                     for k, n in read_counts.items() if n > 1},
            "raw_cache_hits": sum(1 for hit in gets if hit),
            "raw_cache_misses": sum(1 for hit in gets if not hit),
            "gpu_uploads": layer.cache_stats().get("uploads", 0) - uploads0,
            "gpu_texture_bytes_delta":
                layer.cache_stats().get("bytes", 0) - bytes0,
            "gpu_submits": len(binding.descriptor_history) - submits0,
            "rejected_late_results": (stats1["rejected_late_results"]
                                      - stats0["rejected_late_results"]),
            "cancelled_requests": sum(1 for r in requests
                                      if r["error"] == "cancelled"),
        },
        "read_ms": {"corrected": summary([r["ms"] for r in corrected]),
                    "raw": summary([r["ms"] for r in raw_reads]),
                    "all": summary([r["ms"] for r in reads])},
        "product_work_delta": {k: (work1[k] - work0[k]
                                   if isinstance(work1[k], int)
                                   else work1[k][len(work0[k]):])
                               for k in work0},
        "binding_stats": {k: stats1[k] for k in
                          ("coarse_channels", "fine_channels", "fine_tiles",
                           "fine_levels", "fine_budget_refused", "last_error")},
    }


def run_arm(controller_requests_enabled, label):
    """One arm, on a FRESH window -- the coldest state the product can give.

    `build_window` leaves the mount's own answer in place; this then sets the
    controller's public switch to whatever the arm is, so the only difference
    between the two arms is that one boolean.
    """
    ledger = Ledger()
    window, mount, table = build_window(ledger)
    controller = mount.host.stack.controller
    controller.set_viewport_requests_enabled(controller_requests_enabled)
    heart = Heartbeat()
    paints = PaintCounter(mount.gpu_layer)
    work = ProductWork(window)

    arm = {
        "arm": label,
        "controller_viewport_requests_enabled":
            controller.viewport_requests_enabled,
        "backend": mount.backend,
        "actions": [], "matched_geometry": [], "hot": [],
    }

    park(mount)
    order = [("patch", 0), ("tissue", 0), ("tissue", 1), ("patch", 1),
             ("patch", 2), ("tissue", 2), ("tissue", 3), ("patch", 3),
             ("patch", 4), ("tissue", 4)]
    for index, (kind, slot) in enumerate(order):
        arg = slot if kind == "patch" else TISSUE_POINTS[slot]
        park(mount)
        arm["actions"].append(
            measure(window, mount, ledger, heart, paints, work,
                    f"{label}-{index + 1}-{kind}-{slot}", kind, arg, table))
        drain(400)

    # matched geometry: the patch's own fitted rect, reached both ways
    park(mount)
    patch_run = measure(window, mount, ledger, heart, paints, work,
                        f"{label}-matched-patch", "patch", 7, table)
    fitted = patch_run["camera_after"]
    mount.host.jump_to(HOME_Y, HOME_X, int(fitted["h"]), int(fitted["h"]))
    drain(1600)
    qy0, qy1, qx0, qx1 = window._all_patches[8]
    tissue_run = measure(window, mount, ledger, heart, paints, work,
                         f"{label}-matched-tissue", "tissue",
                         ((qy0 + qy1) // 2, (qx0 + qx1) // 2), table)
    arm["matched_geometry"] = [patch_run, tissue_run]

    # hot returns: go back to somewhere already prepared
    park(mount)
    arm["hot"].append(measure(window, mount, ledger, heart, paints, work,
                              f"{label}-hot-patch", "patch", 0, table))
    drain(400)
    arm["hot"].append(measure(window, mount, ledger, heart, paints, work,
                              f"{label}-hot-patch-again", "patch", 0, table))
    park(mount)
    arm["hot"].append(measure(window, mount, ledger, heart, paints, work,
                              f"{label}-hot-tissue", "tissue",
                              TISSUE_POINTS[0], table))

    heart.stop()
    work.restore()
    try:
        window._display.shutdown("g324b2c")
        window.deleteLater()
        APP.processEvents()
    except Exception:                                       # noqa: BLE001
        pass
    drain(400)
    return arm


def main():
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                       else "request_gate.json")
    label = sys.argv[2] if len(sys.argv) > 2 else "g324b2c"
    report = {
        "label": label, "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "slide": SLIDE, "project": str(PROJECT), "channel": CHANNEL,
        "cold_definition": (
            "each arm is a FRESH window and viewer stack; inside an arm every "
            "measured landing is a region that arm has never visited. The OS "
            "page cache is NOT dropped, so no number here is a cold disk read"),
        "honesty": [
            "the two arms differ ONLY by the controller's public switch",
            "the patch action is a real QTest.mouseClick on the real P-button",
            "the tissue action is the real navigate_requested slot",
            "the GUI heartbeat's worst gap is event-loop unresponsiveness",
            "the demo project is read-only; the session autosave writes to a "
            "scratch directory",
        ],
    }
    # ORDER IS AN ARGUMENT, because it is a confound: the two arms share one
    # process and one OS page cache, so whichever runs second reads regions
    # the first arm already touched. Running the pair once each way and
    # reporting all four is what cancels it.
    order = (sys.argv[3].split(",") if len(sys.argv) > 3
             else ["before", "after"])
    report["arm_order"] = list(order)
    for name in order:
        key = ("before_controller_requests_on" if name == "before"
               else "after_controller_requests_off")
        report[key] = run_arm(name == "before", name)
        out.write_text(json.dumps(report, indent=2, default=str))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
