"""G3.2b: where the FIRST cold preparation and a source rebind actually spend.

Not imported by any production module; it writes no real project. The rig is
the synthetic one the G3 takeover suite already builds, driven through the
public product entries only.

Honest limits, stated once:

* the GUI heartbeat is a 5 ms QTimer on the GUI thread. Its largest gap is
  the longest interval in which the event loop could not run -- that is a
  real "the window is not responding" measure, and it is NOT the display's
  input-to-photon latency.
* `grabFramebuffer()` forces a render; it is used only where marked.
* the synthetic pyramid reads far faster than the demo slide. Absolute
  milliseconds here are a LOWER BOUND; the shape of the breakdown is the
  finding, and real read costs are quoted separately from the demo slide.

Usage: python scripts/benchmark_step1_gpu_g32b.py OUT.json [label] [mode]
  mode: cold | rebind | both (default both)
"""

import importlib.util
import json
import pathlib
import sys
import time

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
if "block01" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "block01", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["block01"] = _module
    _spec.loader.exec_module(_module)
sys.path.insert(0, str(ROOT.parent))

_spec2 = importlib.util.spec_from_file_location(
    "g3_takeover_rig", ROOT / "tests/test_step1_gpu_takeover.py")
RIG = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(RIG)

from PyQt5 import QtCore, QtWidgets  # noqa: E402

APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class Heartbeat(QtCore.QObject):
    """A 5 ms tick on the GUI thread; its largest gap is the longest freeze."""

    def __init__(self, interval_ms=5):
        super().__init__()
        self.ticks = []
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(lambda: self.ticks.append(time.perf_counter()))
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def gaps_ms(self):
        return [(b - a) * 1000.0 for a, b in zip(self.ticks, self.ticks[1:])]

    def worst_ms(self):
        gaps = self.gaps_ms()
        return round(max(gaps), 1) if gaps else 0.0


class Stages:
    """Wall-clock stamps, in milliseconds from a named zero."""

    def __init__(self):
        self.t0 = time.perf_counter()
        self.marks = {}

    def mark(self, name):
        self.marks.setdefault(name, round((time.perf_counter() - self.t0) * 1000.0, 2))

    def done(self):
        return dict(self.marks)


def loop(seconds, on_tick=None, interval_ms=5):
    ev = QtCore.QEventLoop()
    stepper = QtCore.QTimer()
    stepper.setInterval(interval_ms)
    if on_tick is not None:
        stepper.timeout.connect(on_tick)
    stopper = QtCore.QTimer()
    stopper.setSingleShot(True)
    stopper.timeout.connect(ev.quit)
    stepper.start()
    stopper.start(int(seconds * 1000))
    ev.exec_()
    stepper.stop()


def counters(rig):
    layer = rig.mount.gpu_layer
    return {
        "provider_reads": sum(len(raw.reads) for raw in rig.raws),
        "scheduler_requests": sum(len(s.public_requests) for s in rig.schedulers),
        "raw_uploads": layer.cache_stats()["uploads"] if layer else 0,
        "gpu_submits": len(rig.mount.gpu_binding.descriptor_history)
                       if rig.mount.gpu_binding else 0,
    }


def _watch_stages(rig, stages):
    """Stamp the public stages of a preparation, from the binding's own state."""
    binding = rig.mount.gpu_binding
    controller = rig.mount.host.stack.controller

    def tick():
        stats = binding.stats()
        if stats["requests"] > 0:
            stages.mark("first_scheduler_request")
        if stats["coarse_channels"]:
            stages.mark("coarse_complete")
        if stats["fine_tiles"]:
            stages.mark("first_fine_plane")
        if rig.mount.gpu_layer.cache_stats()["uploads"] > 0:
            stages.mark("first_raw_upload")
        if binding.descriptor_history:
            stages.mark("first_gpu_submit")
        level = int(controller.level)
        wanted = {(tx, ty) for tx, ty in controller.snapshot().visible_tiles}
        for channel, planes in binding._published_fine.items():
            have = {(k.tile.tx, k.tile.ty) for k in planes
                    if int(k.tile.level) == level}
            if wanted and wanted <= have:
                stages.mark(f"target_viewport_complete[{channel}]")
    return tick


# ── trajectory A: the first cold patch / the first channel ────────────

def cold_patch(rig, bbox, label):
    binding = rig.mount.gpu_binding
    controller = rig.mount.host.stack.controller
    heart = Heartbeat()
    before = counters(rig)
    stages = Stages()
    stages.mark("entry_accepted")
    rig.mount.show_patch(bbox)
    stages.mark("camera_updated")
    watcher = _watch_stages(rig, stages)
    level = int(controller.level)
    wanted = {(tx, ty) for tx, ty in controller.snapshot().visible_tiles}

    def done():
        have = {(k.tile.tx, k.tile.ty)
                for k in binding._published_fine.get("CD3", {})
                if int(k.tile.level) == level}
        return bool(wanted) and wanted <= have

    deadline = time.perf_counter() + 15.0
    while not done() and time.perf_counter() < deadline:
        loop(0.02, watcher)
    watcher()
    stages.mark("target_level_drawable")
    loop(0.1)
    heart.stop()
    after = counters(rig)
    return {
        "label": label,
        "stages_ms": stages.done(),
        "provider_reads": after["provider_reads"] - before["provider_reads"],
        "scheduler_requests": after["scheduler_requests"] - before["scheduler_requests"],
        "cache_hits": (after["scheduler_requests"] - before["scheduler_requests"]
                       - (after["provider_reads"] - before["provider_reads"])),
        "raw_uploads": after["raw_uploads"] - before["raw_uploads"],
        "gpu_submits": after["gpu_submits"] - before["gpu_submits"],
        "worst_gui_gap_ms": heart.worst_ms(),
        "reached_target_level": bool(done()),
    }


def first_channel_serialisation(rig, channel="CD8", hold_ms=40):
    """How much of a new channel's wait is 'fine only starts after coarse'.

    Every read is held for a fixed time, so the two phases are separable:
    the complete coarse transaction must land before this channel's fine is
    even asked for.
    """
    import threading
    binding = rig.mount.gpu_binding
    controller = rig.mount.host.stack.controller
    raw = rig.raws[0]
    released = threading.Event()
    original = raw.read_region

    def slow(ch, level, y0, y1, x0, x1):
        values = original(ch, level, y0, y1, x0, x1)
        released.wait(hold_ms / 1000.0)
        return values

    # THE TARGET LEVEL MUST NOT BE THE COARSE LEVEL, or "fine" and "coarse"
    # are the same RawKeys and the serialisation cannot be seen at all.
    rig.mount.host.jump_to(320, 320, 384, 384)
    RIG._settled_on_level(APP, rig, 0, timeout=40)
    loop(0.3)
    assert int(controller.level) == 0, "this measurement needs level 0"
    raw.read_region = slow
    heart = Heartbeat()
    stages = Stages()
    try:
        stages.mark("tick_accepted")
        with rig.state.using_scope(RIG.STEP1_SCOPE):
            rig.state.set_display_visible(channel, True)
        stages.mark("enable_returned")           # the GUI call itself
        first_fine_request = {"t": None}
        level = int(controller.level)
        wanted = {(tx, ty) for tx, ty in controller.snapshot().visible_tiles}

        def tick():
            if channel in binding.stats()["coarse_channels"]:
                stages.mark("complete_coarse_landed")
            if first_fine_request["t"] is None:
                for request in rig.schedulers[0].public_requests:
                    if (request.key.channel == channel
                            and int(request.key.tile.level) == level
                            and request.priority != 100):
                        first_fine_request["t"] = True
                        stages.mark("first_fine_REQUEST_issued")
                        break
            have = {(k.tile.tx, k.tile.ty)
                    for k in binding._published_fine.get(channel, {})
                    if int(k.tile.level) == level}
            if have:
                stages.mark("first_fine_plane_landed")
            if wanted and wanted <= have:
                stages.mark("viewport_fine_complete")

        deadline = time.perf_counter() + 25.0
        while ("viewport_fine_complete" not in stages.marks
               and time.perf_counter() < deadline):
            loop(0.02, tick)
        tick()
        stages.mark("channel_sharp")
    finally:
        released.set()
        raw.read_region = original
        heart.stop()
    marks = stages.done()
    coarse_phase = marks.get("complete_coarse_landed", 0.0)
    return {
        "channel": channel,
        "held_read_ms": hold_ms,
        "stages_ms": marks,
        "coarse_phase_ms": round(coarse_phase, 2),
        "fine_phase_ms": round(marks.get("viewport_fine_complete", 0.0)
                               - coarse_phase, 2),
        "fine_requested_before_coarse_landed": bool(
            marks.get("first_fine_REQUEST_issued", 1e9) < coarse_phase),
        "coarse_tiles": len(binding._published_coarse.get(channel, ())),
        "fine_tiles": binding.stats()["fine_tiles"].get(channel, 0),
        "worst_gui_gap_ms": heart.worst_ms(),
    }


def first_channel(rig, channel="CD8"):
    binding = rig.mount.gpu_binding
    controller = rig.mount.host.stack.controller
    heart = Heartbeat()
    before = counters(rig)
    stages = Stages()
    stages.mark("tick_accepted")
    RIG._enable(rig, channel, visible=True)
    stages.mark("enable_returned")
    watcher = _watch_stages(rig, stages)
    level = int(controller.level)
    wanted = {(tx, ty) for tx, ty in controller.snapshot().visible_tiles}

    def done():
        have = {(k.tile.tx, k.tile.ty)
                for k in binding._published_fine.get(channel, {})
                if int(k.tile.level) == level}
        return bool(wanted) and wanted <= have and channel in binding.stats()["coarse_channels"]

    deadline = time.perf_counter() + 15.0
    while not done() and time.perf_counter() < deadline:
        loop(0.02, watcher)
    watcher()
    stages.mark("channel_sharp")
    loop(0.1)
    heart.stop()
    after = counters(rig)
    return {
        "channel": channel,
        "stages_ms": stages.done(),
        "coarse_tiles": len(binding._published_coarse.get(channel, ())),
        "fine_tiles": binding.stats()["fine_tiles"].get(channel, 0),
        "provider_reads": after["provider_reads"] - before["provider_reads"],
        "scheduler_requests": after["scheduler_requests"] - before["scheduler_requests"],
        "raw_uploads": after["raw_uploads"] - before["raw_uploads"],
        "gpu_submits": after["gpu_submits"] - before["gpu_submits"],
        "worst_gui_gap_ms": heart.worst_ms(),
        "reached": bool(done()),
    }


# ── trajectory B: a source rebind with reads in flight ────────────────

def source_rebind(rig, hold_ms=150):
    """The public `sync_source()` path, with reads genuinely in flight."""
    import threading
    binding = rig.mount.gpu_binding
    raw = rig.raws[0]
    released = threading.Event()
    original_read = raw.read_region

    def slow(channel, level, y0, y1, x0, x1):
        values = original_read(channel, level, y0, y1, x0, x1)
        released.wait(hold_ms / 1000.0)
        return values

    stages = Stages()
    phase = {}

    viewer = rig.mount.viewer
    host = rig.mount.host
    original_moved = viewer.source_moved
    original_teardown = host.teardown
    original_open = host.open

    def timed_moved():
        t = time.perf_counter()
        out = original_moved()
        phase["source_moved_check_ms"] = round((time.perf_counter() - t) * 1000.0, 2)
        return out

    def timed_teardown(*a, **k):
        t = time.perf_counter()
        out = original_teardown(*a, **k)
        phase["teardown_and_scheduler_join_ms"] = round(
            (time.perf_counter() - t) * 1000.0, 2)
        return out

    def timed_open(*a, **k):
        t = time.perf_counter()
        out = original_open(*a, **k)
        phase["new_stack_build_ms"] = round((time.perf_counter() - t) * 1000.0, 2)
        return out

    viewer.source_moved = timed_moved
    host.teardown = timed_teardown
    host.open = timed_open

    raw.read_region = slow
    heart = Heartbeat()
    before = counters(rig)
    old_source = rig.mount.host.stack.provider.source_identity()

    # Make sure reads really are in flight: move somewhere cold first.
    rig.mount.jump_to_point(1120, 1120, 320)
    loop(0.03)

    result = {}

    def fire():
        stages.mark("handoff_visible")
        rig.window.step0_output = {"channel_remap_config_hash": "rev-cucim-1"}
        t = time.perf_counter()
        moved = rig.mount.sync_source("cucim")
        result["sync_source_total_ms"] = round((time.perf_counter() - t) * 1000.0, 2)
        result["sync_source_returned"] = bool(moved)
        stages.mark("sync_source_returned")

    QtCore.QTimer.singleShot(0, fire)
    loop(0.6)
    released.set()
    raw.read_region = original_read

    new_binding = rig.mount.gpu_binding
    controller = rig.mount.host.stack.controller
    watcher = _watch_stages(rig, stages)
    level = int(controller.level)
    wanted = {(tx, ty) for tx, ty in controller.snapshot().visible_tiles}

    def done():
        have = {(k.tile.tx, k.tile.ty)
                for k in new_binding._published_fine.get("CD3", {})
                if int(k.tile.level) == level}
        return bool(wanted) and wanted <= have

    deadline = time.perf_counter() + 15.0
    while not done() and time.perf_counter() < deadline:
        loop(0.02, watcher)
    watcher()
    stages.mark("new_source_sharp")
    loop(0.1)
    heart.stop()
    viewer.source_moved = original_moved
    host.teardown = original_teardown
    host.open = original_open

    new_source = rig.mount.host.stack.provider.source_identity()
    after = counters(rig)
    stale = [p.identity.source for _d, *_r in () for p in ()]
    descriptors = new_binding.descriptor_history
    old_pixels = any(plane.identity.source != new_source
                     for descriptor, *_rest in descriptors
                     for source in descriptor.channels
                     for plane in source.coarse + source.fine)
    result.update({
        "old_source_identity": str(old_source),
        "new_source_identity": str(new_source),
        "identity_changed": bool(old_source != new_source),
        "phase_ms": phase,
        "stages_ms": stages.done(),
        "worst_gui_gap_ms": heart.worst_ms(),
        "held_read_ms": hold_ms,
        "provider_reads": after["provider_reads"] - before["provider_reads"],
        "scheduler_requests": after["scheduler_requests"] - before["scheduler_requests"],
        "raw_uploads": after["raw_uploads"] - before["raw_uploads"],
        "gpu_submits": after["gpu_submits"] - before["gpu_submits"],
        "any_old_source_pixel_published": bool(old_pixels),
        "rejected_late_results": new_binding.stats()["rejected_late_results"],
        "reached_new_source_sharp": bool(done()),
    })
    del stale
    return result


def unchanged_source(rig):
    """The same entry when the identity did NOT move: it must cost nothing."""
    binding = rig.mount.gpu_binding
    layer = rig.mount.gpu_layer
    stack = rig.mount.host.stack
    heart = Heartbeat()
    before = counters(rig)
    fine_before = dict(binding.stats()["fine_tiles"])
    t = time.perf_counter()
    moved = rig.mount.sync_source("no-move")
    took = round((time.perf_counter() - t) * 1000.0, 2)
    loop(0.15)
    heart.stop()
    after = counters(rig)
    return {
        "sync_source_returned": bool(moved),
        "sync_source_ms": took,
        "stack_rebuilt": rig.mount.host.stack is not stack,
        "gpu_layer_rebuilt": rig.mount.gpu_layer is not layer,
        "binding_rebuilt": rig.mount.gpu_binding is not binding,
        "provider_reads": after["provider_reads"] - before["provider_reads"],
        "raw_uploads": after["raw_uploads"] - before["raw_uploads"],
        "fine_tiles_before": fine_before,
        "fine_tiles_after": dict(binding.stats()["fine_tiles"]),
        "worst_gui_gap_ms": heart.worst_ms(),
    }


def main_cold5(out_path, label, repeats=5):
    """Five independent cold runs of the first-channel preparation."""
    import statistics
    runs = []
    for index in range(repeats):
        rig = RIG._mount(APP, visible=("CD3",))
        binding = rig.mount.gpu_binding
        RIG._wait(APP, lambda: binding.stats()["coarse_channels"] == ("CD3",))
        loop(0.25)
        runs.append(first_channel_serialisation(rig))
        RIG._close(rig)
    sharp = [r["stages_ms"]["channel_sharp"] for r in runs]
    coarse = [r["coarse_phase_ms"] for r in runs]
    fine = [r["fine_phase_ms"] for r in runs]
    report = {
        "label": label, "repeats": repeats,
        "channel_sharp_ms": [round(v, 2) for v in sharp],
        "median_channel_sharp_ms": round(statistics.median(sharp), 2),
        "median_coarse_phase_ms": round(statistics.median(coarse), 2),
        "median_fine_phase_after_coarse_ms": round(statistics.median(fine), 2),
        "fine_requested_before_coarse_landed": [
            r["fine_requested_before_coarse_landed"] for r in runs],
        "worst_gui_gap_ms": max(r["worst_gui_gap_ms"] for r in runs),
        "runs": runs,
    }
    out_path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "runs"}, indent=2))


def main():
    out_path = pathlib.Path(sys.argv[1])
    label = sys.argv[2] if len(sys.argv) > 2 else "run"
    mode = sys.argv[3] if len(sys.argv) > 3 else "both"
    if mode == "cold5":
        return main_cold5(out_path, label)
    report = {"label": label, "mode": mode, "notes": [
        "GUI heartbeat is a 5 ms QTimer; its largest gap is the longest "
        "interval the event loop could not run. Not photon latency.",
        "the synthetic pyramid reads much faster than the demo slide: the "
        "absolute milliseconds are a lower bound, the breakdown is the point",
    ]}

    if mode in ("cold", "both"):
        rig = RIG._mount(APP, visible=("CD3",))
        binding = rig.mount.gpu_binding
        report["backend"] = rig.mount.backend
        report["renderer"] = rig.mount.gpu_status()["environment"].get("gl_renderer")
        RIG._wait(APP, lambda: binding.stats()["coarse_channels"] == ("CD3",))
        loop(0.3)
        report["A_cold_patch"] = [
            cold_patch(rig, (320, 704, 320, 704), "first visit, cold"),
            cold_patch(rig, (896, 1216, 896, 1216), "second patch, cold"),
            cold_patch(rig, (320, 704, 320, 704), "return, hot"),
        ]
        report["A_first_channel_serialisation"] = first_channel_serialisation(rig)
        RIG._close(rig)

    if mode in ("rebind", "both"):
        rig = RIG._mount(APP, visible=("CD3",))
        binding = rig.mount.gpu_binding
        report.setdefault("backend", rig.mount.backend)
        report.setdefault(
            "renderer", rig.mount.gpu_status()["environment"].get("gl_renderer"))
        RIG._wait(APP, lambda: binding.stats()["coarse_channels"] == ("CD3",))
        loop(0.3)
        report["B_unchanged_source"] = unchanged_source(rig)
        report["B_source_rebind"] = source_rebind(rig)
        RIG._close(rig)

    out_path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


main()
