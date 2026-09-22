"""G3.2a.3: measure what the Step1 GPU backend does DURING an interaction.

Not imported by any production module and it writes no real project: the rig
is the synthetic one the G3 takeover suite already builds.  A real Qt event
loop drives the camera, so a continuous drag is measured as a continuous
drag and not as a series of still pictures with `processEvents()` in between.

Honest limits, stated once:

* `frameSwapped` counts the buffer swaps Qt performed for the mounted
  QOpenGLWidget.  It is a real presentation event, but it is NOT the
  display's input-to-photon latency and nothing here claims to measure that.
* `grabFramebuffer()` forces a render.  It is used only a handful of times,
  at points that are marked in the output, and those samples are never
  counted as naturally occurring frames.

Usage: python scripts/benchmark_step1_gpu_interaction.py OUT.json [label]
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


class _SubmitLog:
    """Wraps the G1 layer to timestamp every submission the binding makes."""

    def __init__(self, layer):
        self._layer = layer
        self.entries = []

    def submit(self, descriptor, display, viewport):
        started = time.perf_counter()
        stats = self._layer.submit(descriptor, display, viewport)
        self.entries.append({
            "t": started,
            "world_rect": [round(float(v), 3) for v in viewport.world_rect],
            "coarse_planes": sum(len(s.coarse) for s in descriptor.channels),
            "fine_planes": sum(len(s.fine) for s in descriptor.channels),
            "cpu_submit_ms": round(float(stats.get("cpu_submit_ms", 0.0)), 3),
            "uploads": int(stats.get("cache", {}).get("uploads", 0)),
        })
        return stats

    def __getattr__(self, name):
        return getattr(self._layer, name)


def instrument(rig):
    binding = rig.mount.gpu_binding
    log = _SubmitLog(binding.layer)
    binding.layer = log
    swaps = []
    binding.layer.frameSwapped.connect(lambda: swaps.append(time.perf_counter()))
    return log, swaps


def counters(rig):
    layer = rig.mount.gpu_layer
    return {
        "provider_reads": sum(len(raw.reads) for raw in rig.raws),
        "scheduler_requests": sum(len(s.public_requests) for s in rig.schedulers),
        "raw_uploads": layer.cache_stats()["uploads"],
    }


def run_loop(seconds, on_tick, interval_ms=10):
    """A REAL Qt event loop for `seconds`, ticking every `interval_ms`."""
    loop = QtCore.QEventLoop()
    stepper = QtCore.QTimer()
    stepper.setInterval(interval_ms)
    stepper.timeout.connect(on_tick)
    stopper = QtCore.QTimer()
    stopper.setSingleShot(True)
    stopper.timeout.connect(loop.quit)
    stepper.start()
    stopper.start(int(seconds * 1000))
    loop.exec_()
    stepper.stop()


def quiet(seconds):
    loop = QtCore.QEventLoop()
    stopper = QtCore.QTimer()
    stopper.setSingleShot(True)
    stopper.timeout.connect(loop.quit)
    stopper.start(int(seconds * 1000))
    loop.exec_()


def drag(rig, log, swaps, *, seconds=2.0, interval_ms=10, span=512,
         start=(256, 256), pixels_per_step=6):
    """Hold the button down and keep moving: a real, uninterrupted gesture."""
    view_box = rig.mount.host.stack.view.view_box
    steps = []
    state = {"n": 0}

    def tick():
        state["n"] += 1
        offset = state["n"] * pixels_per_step
        y0, x0 = start[0] + offset, start[1] + offset
        view_box.setRange(xRange=(x0, x0 + span), yRange=(y0, y0 + span),
                          padding=0)
        steps.append({"t": time.perf_counter(), "y0": y0, "x0": x0})

    before = counters(rig)
    first_submit = len(log.entries)
    first_swap = len(swaps)
    t0 = time.perf_counter()
    run_loop(seconds, tick, interval_ms)
    t1 = time.perf_counter()
    after = counters(rig)
    submits = log.entries[first_submit:]
    centres = [(e["world_rect"][0] + e["world_rect"][1]) / 2.0 for e in submits]
    return {
        "seconds": round(t1 - t0, 3),
        "camera_steps": len(steps),
        "submits_during_drag": len(submits),
        "distinct_submit_centres": len(set(round(c, 2) for c in centres)),
        "first_submit_centre": centres[0] if centres else None,
        "last_submit_centre": centres[-1] if centres else None,
        "camera_first": [steps[0]["x0"], steps[0]["y0"]] if steps else None,
        "camera_last": [steps[-1]["x0"], steps[-1]["y0"]] if steps else None,
        "frame_swaps_during_drag": len(swaps) - first_swap,
        "submit_gaps_ms": [round((b["t"] - a["t"]) * 1000.0, 1)
                           for a, b in zip(submits, submits[1:])][:40],
        "largest_submit_gap_ms": round(max(
            [(b["t"] - a["t"]) * 1000.0 for a, b in zip(submits, submits[1:])]
            or [0.0]), 1),
        "provider_read_delta": after["provider_reads"] - before["provider_reads"],
        "scheduler_request_delta": (after["scheduler_requests"]
                                    - before["scheduler_requests"]),
        "raw_upload_delta": after["raw_uploads"] - before["raw_uploads"],
    }


class _PaintCounter(QtCore.QObject):
    """Counts Qt paint events delivered to the mounted GPU widget."""

    def __init__(self, widget):
        super().__init__()
        self.count = 0
        widget.installEventFilter(self)

    def eventFilter(self, _watched, event):
        if event.type() == QtCore.QEvent.Paint:
            self.count += 1
        return False


def hot_drag(rig, log, seconds=1.5, interval_ms=10, span=512):
    """Drag BACK AND FORTH inside ground that is already loaded."""
    view_box = rig.mount.host.stack.view.view_box
    state = {"n": 0}

    def tick():
        state["n"] += 1
        offset = (state["n"] % 12) * 4          # a few pixels, never leaves
        view_box.setRange(xRange=(256 + offset, 256 + offset + span),
                          yRange=(256 + offset, 256 + offset + span),
                          padding=0)

    before = counters(rig)
    first = len(log.entries)
    run_loop(seconds, tick, interval_ms)
    after = counters(rig)
    return {
        "submits": len(log.entries) - first,
        "provider_read_delta": after["provider_reads"] - before["provider_reads"],
        "raw_upload_delta": after["raw_uploads"] - before["raw_uploads"],
        "scheduler_request_delta": (after["scheduler_requests"]
                                    - before["scheduler_requests"]),
    }


def sampled_drag(rig, log, seconds=1.5, interval_ms=10, span=384,
                 pixels_per_step=6, samples=3):
    """A drag with a few MARKED forced framebuffer reads inside the gesture."""
    import hashlib
    view_box = rig.mount.host.stack.view.view_box
    state = {"n": 0}
    marks = []
    every = max(1, int(seconds * 1000 / interval_ms / (samples + 1)))

    def tick():
        state["n"] += 1
        offset = state["n"] * pixels_per_step
        # INSIDE the analysis region, or every sample is the same empty
        # picture and proves nothing about following the camera.
        view_box.setRange(xRange=(320 + offset, 320 + offset + span),
                          yRange=(320, 320 + span), padding=0)
        if state["n"] % every == 0 and len(marks) < samples:
            frame = rig.mount.gpu_layer.grabFramebuffer()
            frame = frame.convertToFormat(4)          # RGB32, enough to hash
            buffer = frame.constBits()
            buffer.setsize(frame.byteCount())
            marks.append({
                "note": "FORCED grabFramebuffer inside the gesture",
                "camera_x0": 320 + offset,
                "sha256_16": hashlib.sha256(bytes(buffer)).hexdigest()[:16],
                "submits_so_far": len(log.entries),
            })

    run_loop(seconds, tick, interval_ms)
    return {"marked_samples": marks,
            "distinct_sample_pictures": len({m["sha256_16"] for m in marks})}


def repeat_far_jumps(rig, log):
    """Two far Tissue Preview landings back to back, with no settling."""
    binding = rig.mount.gpu_binding
    out = []
    for index, (y, x) in enumerate(((1120, 1120), (320, 1120))):
        before = counters(rig)
        first = len(log.entries)
        t0 = time.perf_counter()
        moved = rig.mount.jump_to_point(y, x, 320)
        immediate = len(log.entries) - first
        stats = binding.stats()
        out.append({
            "click": index + 1, "target": [y, x], "accepted": bool(moved),
            "camera_moved_in_call": True,
            "submits_in_call": immediate,
            "requested_fine_in_call": stats["requested_fine_last_epoch"],
            "retained_fine_in_call": stats["retained_fine_last_epoch"],
            "requests_in_call": (counters(rig)["scheduler_requests"]
                                 - before["scheduler_requests"]),
            "call_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        })
        quiet(0.05)                                  # a fast double click
    quiet(1.0)
    controller = rig.mount.host.stack.controller
    out.append({"settled_on_target_level": bool(
        RIG._settled_on_level(APP, rig, int(controller.level), timeout=30)),
        "fine_tiles": binding.stats()["fine_tiles"]})
    return out


def slow_far_jumps(rig, log, hold_ms=120):
    """Two far landings while reads are SLOW: where does the second wait?

    The scheduler's own contract is that `cancel_generation` stops work
    that has not started; a read already running always finishes. So a
    second landing's tiles can only start once an I/O worker comes free.
    This measures that, it does not assume it.
    """
    import threading
    raw = rig.raws[0]
    binding = rig.mount.gpu_binding
    controller = rig.mount.host.stack.controller
    released = threading.Event()

    original = raw.read_region

    def slow(channel, level, y0, y1, x0, x1):
        values = original(channel, level, y0, y1, x0, x1)
        released.wait(hold_ms / 1000.0)
        return values

    raw.read_region = slow
    try:
        legs = []
        slide = 1536 if len(raw._shapes) < 4 else 8192
        far = ((slide * 3 // 4, slide * 3 // 4), (slide // 4, slide * 3 // 4))
        for index, (y, x) in enumerate(far):
            t0 = time.perf_counter()
            before = counters(rig)
            rig.mount.jump_to_point(y, x, slide // 8)
            requested = binding.stats()["requested_fine_last_epoch"]
            wanted = {(tx, ty) for tx, ty in controller.snapshot().visible_tiles}
            level = int(controller.level)

            def arrived():
                have = {(k.tile.tx, k.tile.ty)
                        for k in binding._published_fine.get("CD3", {})
                        if int(k.tile.level) == level}
                return bool(wanted) and wanted <= have

            deadline = time.perf_counter() + 8.0
            while not arrived() and time.perf_counter() < deadline:
                quiet(0.02)
            legs.append({
                "click": index + 1, "target": [y, x],
                "requested_fine": requested,
                "requests": counters(rig)["scheduler_requests"] - before["scheduler_requests"],
                "reads": counters(rig)["provider_reads"] - before["provider_reads"],
                "ms_until_target_on_screen": round(
                    (time.perf_counter() - t0) * 1000.0, 1),
                "arrived": bool(arrived()),
            })
        return {"held_read_ms": hold_ms, "io_workers": 24, "legs": legs,
                "note": ("a read already started cannot be cancelled -- the "
                         "second landing waits for a free I/O worker")}
    finally:
        released.set()
        raw.read_region = original


def patch_round_trip(rig, log):
    """A -> B -> A with the same source and the same display parameters."""
    binding = rig.mount.gpu_binding
    legs = []
    for name, bbox in (("A", (256, 768, 256, 768)), ("B", (1024, 1280, 1024, 1280)),
                       ("A again", (256, 768, 256, 768))):
        before = counters(rig)
        first = len(log.entries)
        rig.mount.show_patch(bbox)
        RIG._settled_on_level(APP, rig, int(rig.mount.host.stack.controller.level),
                              timeout=40)
        quiet(0.4)
        after = counters(rig)
        legs.append({
            "leg": name,
            "scheduler_requests": after["scheduler_requests"] - before["scheduler_requests"],
            "provider_reads": after["provider_reads"] - before["provider_reads"],
            "raw_uploads": after["raw_uploads"] - before["raw_uploads"],
            "gpu_submits": len(log.entries) - first,
            "fine_tiles": dict(binding.stats()["fine_tiles"]),
        })
    return legs


def untick_and_reenable(rig, log):
    """Untick a channel and tick it back with the camera standing still."""
    binding = rig.mount.gpu_binding
    before = counters(rig)
    first = len(log.entries)
    RIG._enable(rig, "CD3", visible=False)
    quiet(0.2)
    off = counters(rig)
    RIG._enable(rig, "CD3", visible=True)
    RIG._settled_on_level(APP, rig, int(rig.mount.host.stack.controller.level),
                          timeout=40)
    quiet(0.4)
    on = counters(rig)
    return {
        "untick": {
            "scheduler_requests": off["scheduler_requests"] - before["scheduler_requests"],
            "provider_reads": off["provider_reads"] - before["provider_reads"],
            "raw_uploads": off["raw_uploads"] - before["raw_uploads"],
        },
        "retick": {
            "scheduler_requests": on["scheduler_requests"] - off["scheduler_requests"],
            "provider_reads": on["provider_reads"] - off["provider_reads"],
            "raw_uploads": on["raw_uploads"] - off["raw_uploads"],
        },
        "gpu_submits_total": len(log.entries) - first,
        "fine_tiles_after": dict(binding.stats()["fine_tiles"]),
        "note": ("a request that hits the raw cache costs no read; an upload "
                 "happens only for a plane identity the GPU does not hold"),
    }


def new_channel_timeline(rig):
    """Enable a second channel and stamp every step of its first appearance."""
    binding = rig.mount.gpu_binding
    marks = {}
    t0 = time.perf_counter()

    def stamp(name):
        marks.setdefault(name, round((time.perf_counter() - t0) * 1000.0, 1))

    before = counters(rig)
    RIG._enable(rig, "CD8", visible=True)
    stamp("enable_returned")

    def tick():
        stats = binding.stats()
        if "CD8" in stats["coarse_channels"]:
            stamp("coarse_complete")
        if stats["fine_tiles"].get("CD8", 0) > 0:
            stamp("first_fine_plane")
        wanted = {(tx, ty) for tx, ty
                  in rig.mount.host.stack.controller.snapshot().visible_tiles}
        have = {(k.tile.tx, k.tile.ty)
                for k in binding._published_fine.get("CD8", {})
                if int(k.tile.level) == int(rig.mount.host.stack.controller.level)}
        if wanted and wanted <= have:
            stamp("target_viewport_fine_complete")

    run_loop(4.0, tick, 5)
    after = counters(rig)
    return {
        "milestones_ms": marks,
        "scheduler_requests": after["scheduler_requests"] - before["scheduler_requests"],
        "provider_reads": after["provider_reads"] - before["provider_reads"],
        "raw_uploads": after["raw_uploads"] - before["raw_uploads"],
        "coarse_tiles": len(binding._published_coarse.get("CD8", ())),
        "fine_tiles": binding.stats()["fine_tiles"].get("CD8", 0),
        "note": ("fine for a channel starts only after its COMPLETE coarse "
                 "transaction lands: that serialisation is the wait"),
    }


class _PlanProbe:
    """Records every viewport planning the binding actually performs."""

    def __init__(self, binding):
        self._binding = binding
        self._original = binding.update_viewport
        self.calls = []
        binding.update_viewport = self._wrapped

    def _wrapped(self, snapshot=None):
        snap = (self._binding.controller.snapshot() if snapshot is None
                else snapshot)
        before = self._binding.stats()
        self._original(snapshot)
        after = self._binding.stats()
        self.calls.append({
            "t": time.perf_counter(),
            "epoch": int(getattr(snap, "epoch", -1)),
            "level": int(getattr(snap, "level", -1)),
            "tiles": len(getattr(snap, "visible_tiles", ()) or ()),
            "requested": after["requested_fine_last_epoch"],
            "requests_delta": after["requests"] - before["requests"],
            "fine_epoch": after.get("revision", 0),
        })

    def release(self):
        self._binding.update_viewport = self._original

    def epochs(self):
        return [c["epoch"] for c in self.calls]


def jump_during_motion(rig):
    """Trajectory A: a landing while the motion timer is already running."""
    binding = rig.mount.gpu_binding
    view_box = rig.mount.host.stack.view.view_box
    probe = _PlanProbe(binding)
    state = {"n": 0, "jumped": False, "jump_epoch": None}

    def tick():
        state["n"] += 1
        if state["n"] <= 3:
            view_box.setRange(xRange=(320 + state["n"] * 6, 320 + state["n"] * 6 + 384),
                              yRange=(320, 704), padding=0)
            return
        if not state["jumped"]:
            state["jumped"] = True
            assert binding._motion_timer.isActive(), \
                "the motion timer must be running for this trajectory"
            rig.mount.jump_to_point(1000, 1000, 320)
            state["jump_epoch"] = int(
                rig.mount.host.stack.controller.snapshot().epoch)

    # 4 ticks of camera, then 400 ms of pure waiting: long enough for the
    # 33 ms motion timer AND the controller's 80 ms quiet to both land.
    run_loop(0.45, tick, 10)
    probe.release()
    jump_epoch = state["jump_epoch"]
    same = [c for c in probe.calls if c["epoch"] == jump_epoch]
    return {
        "jump_epoch": jump_epoch,
        "plannings_total": len(probe.calls),
        "plannings_for_the_jump_position": len(same),
        "epoch_trail": probe.epochs(),
        "requests_for_the_jump_position": sum(c["requests_delta"] for c in same),
    }


def two_fast_far_jumps(rig):
    """Trajectory B: A then B inside one throttle window, then hands off."""
    import hashlib
    binding = rig.mount.gpu_binding
    controller = rig.mount.host.stack.controller
    probe = _PlanProbe(binding)
    t0 = time.perf_counter()
    rig.mount.jump_to_point(1120, 1120, 320)
    first_epoch = int(controller.snapshot().epoch)
    first_rect = [round(float(v), 1) for pair in
                  rig.mount.host.stack.view.view_box.viewRange() for v in pair]
    quiet(0.015)                                   # well under 33 ms
    rig.mount.jump_to_point(320, 1120, 320)
    second_epoch = int(controller.snapshot().epoch)
    second_rect = [round(float(v), 1) for pair in
                   rig.mount.host.stack.view.view_box.viewRange() for v in pair]
    camera_changed_at = round((time.perf_counter() - t0) * 1000.0, 1)

    # NO third click, NO drag, NO zoom: just let it settle by itself.
    level = int(controller.level)
    wanted = {(tx, ty) for tx, ty in controller.snapshot().visible_tiles}

    def arrived():
        have = {(k.tile.tx, k.tile.ty)
                for k in binding._published_fine.get("CD3", {})
                if int(k.tile.level) == level}
        return bool(wanted) and wanted <= have

    deadline = time.perf_counter() + 8.0
    while not arrived() and time.perf_counter() < deadline:
        quiet(0.02)
    settled_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    # Keep watching past the controller's 80 ms quiet, so a duplicate
    # planning triggered by gesture_quiet is counted, not missed.
    run_loop(0.35, lambda: None, 50)
    probe.release()

    frame = rig.mount.gpu_layer.grabFramebuffer().convertToFormat(4)
    buf = frame.constBits(); buf.setsize(frame.byteCount())
    descriptor = binding.descriptor_history[-1]
    return {
        "first": {"epoch": first_epoch, "camera": first_rect},
        "second": {"epoch": second_epoch, "camera": second_rect},
        "camera_changed_for_second_after_ms": camera_changed_at,
        "plannings_for_first": sum(1 for c in probe.calls if c["epoch"] == first_epoch),
        "plannings_for_second": sum(1 for c in probe.calls if c["epoch"] == second_epoch),
        "epoch_trail": probe.epochs(),
        "target_tiles_wanted": len(wanted),
        "target_tiles_on_screen": len(
            {(k.tile.tx, k.tile.ty) for k in binding._published_fine.get("CD3", {})
             if int(k.tile.level) == level}),
        "reached_target_level_without_further_input": bool(arrived()),
        "ms_from_first_click_to_target_on_screen": settled_ms,
        "descriptor_viewport": [round(float(v), 1) for v in descriptor[2].world_rect],
        "final_frame_sha256_16": hashlib.sha256(bytes(buf)).hexdigest()[:16],
        "final_frame_note": "FORCED grabFramebuffer, once, for the final check",
        "late_results_rejected": binding.stats()["rejected_late_results"],
    }


def first_entry_idle(rig, seconds=2.0):
    """Trajectory D: enter Step1 and touch nothing at all."""
    binding = rig.mount.gpu_binding
    controller = rig.mount.host.stack.controller
    before = counters(rig)
    run_loop(seconds, lambda: None, 50)
    level = int(controller.level)
    wanted = {(tx, ty) for tx, ty in controller.snapshot().visible_tiles}
    have = {(k.tile.tx, k.tile.ty)
            for k in binding._published_fine.get("CD3", {})
            if int(k.tile.level) == level}
    stats = binding.stats()
    after = counters(rig)
    return {
        "target_level": level,
        "visible_tiles": len(wanted),
        "target_tiles_on_screen": len(have),
        "all_target_tiles_on_screen": bool(wanted and wanted <= have),
        "coarse_channels": list(stats["coarse_channels"]),
        "fine_levels": {c: list(v) for c, v in stats["fine_levels"].items()},
        "fine_budget_refused": list(stats["fine_budget_refused"]),
        "last_error": stats["last_error"],
        "requests_while_idle": after["scheduler_requests"] - before["scheduler_requests"],
        "reads_while_idle": after["provider_reads"] - before["provider_reads"],
        "stage_reached": ("target level fully on screen"
                          if wanted and wanted <= have else
                          "stopped before every target tile was on screen"),
    }


class _PaintWatcher(QtCore.QObject):
    """Records, at every natural Qt paint, what the picture then consisted of."""

    def __init__(self, rig, channel="CD3"):
        super().__init__()
        self._rig = rig
        self._channel = channel
        self.frames = []
        rig.mount.gpu_layer.installEventFilter(self)

    def eventFilter(self, _watched, event):
        if event.type() == QtCore.QEvent.Paint:
            binding = self._rig.mount.gpu_binding
            controller = self._rig.mount.host.stack.controller
            level = int(controller.level)
            resident = {(k.tile.tx, k.tile.ty)
                        for k in binding._published_fine.get(self._channel, {})
                        if int(k.tile.level) == level}
            wanted = {(tx, ty) for tx, ty in controller.snapshot().visible_tiles}
            self.frames.append({
                "target_tiles_on_screen": len(resident),
                "target_tiles_wanted": len(wanted),
                "complete": bool(wanted and wanted <= resident),
                "submits_so_far": len(binding.descriptor_history),
            })
        return False


def patch_return_first_paint(rig, log):
    """A -> B -> A, and what the FIRST natural paint after returning shows."""
    import hashlib
    binding = rig.mount.gpu_binding
    controller = rig.mount.host.stack.controller
    A = (320, 704, 320, 704)
    B = (896, 1216, 896, 1216)

    def settle(bbox):
        rig.mount.show_patch(bbox)
        RIG._settled_on_level(APP, rig, int(controller.level), timeout=40)
        quiet(0.4)

    settle(A)
    a_frame = rig.mount.gpu_layer.grabFramebuffer().convertToFormat(4)
    a_buf = a_frame.constBits(); a_buf.setsize(a_frame.byteCount())
    a_sha = hashlib.sha256(bytes(a_buf)).hexdigest()[:16]
    settle(B)

    watcher = _PaintWatcher(rig)
    before = counters(rig)
    first_submit = len(log.entries)
    rig.mount.show_patch(A)                    # the public entry, then return
    submits_in_call = len(log.entries) - first_submit
    in_call = {
        "target_tiles_on_screen_when_the_call_returned": len(
            {(k.tile.tx, k.tile.ty)
             for k in binding._published_fine.get("CD3", {})
             if int(k.tile.level) == int(controller.level)}),
        "target_tiles_wanted": len(controller.snapshot().visible_tiles),
    }
    # Now let Qt paint naturally. No grabFramebuffer, no processEvents storm.
    run_loop(0.25, lambda: None, 25)
    after = counters(rig)

    final = rig.mount.gpu_layer.grabFramebuffer().convertToFormat(4)
    f_buf = final.constBits(); f_buf.setsize(final.byteCount())
    return {
        "return_to_A": {
            "scheduler_requests": after["scheduler_requests"] - before["scheduler_requests"],
            "provider_reads": after["provider_reads"] - before["provider_reads"],
            "raw_uploads": after["raw_uploads"] - before["raw_uploads"],
            "gpu_submits_inside_the_call": submits_in_call,
            "gpu_submits_total": len(log.entries) - first_submit,
        },
        "when_show_patch_returned": in_call,
        "natural_paints": len(watcher.frames),
        "first_natural_paint": watcher.frames[0] if watcher.frames else None,
        "paint_trail": watcher.frames[:8],
        "coarse_only_or_partial_paints": sum(
            1 for f in watcher.frames if not f["complete"]),
        "final_frame_matches_before_leaving_A": bool(
            hashlib.sha256(bytes(f_buf)).hexdigest()[:16] == a_sha),
        "final_frame_note": "FORCED grabFramebuffer, twice, as the pixel oracle",
    }


def real_dataset_costs(dataset):
    """Read-only: what one tile actually costs on the demo slide."""
    from block01.viewer.raw_tile_provider import RawTileProvider
    from block01.viewer import step1_source as sources
    from block01.ui.step1_viewer_host import Step1TileProvider, TILE_SIZE
    from block01.viewer.tile_types import TileAddress, TileGridSpec

    if not pathlib.Path(dataset).exists():
        return {"skipped": f"{dataset} is not present"}
    raw = RawTileProvider(dataset)
    table = sources.Step1SourceTable({}, "", "", lambda *a: None)
    provider = Step1TileProvider(raw, table)
    grid = TileGridSpec(tile_size=TILE_SIZE, source_chunk_shape=(),
                        grid_version="v1")
    channel = raw.channel_names[0]
    out = {"dataset": dataset, "channel": channel,
           "levels": raw.num_levels, "tile_size": TILE_SIZE,
           "note": ("read/decode only. This rig has no corrected product, so "
                    "the correction-reduction cost is NOT measured here.")}
    for name, level, (tx, ty) in (("coarsest", raw.num_levels - 1, (0, 0)),
                                  ("level_0", 0, (8, 8))):
        tile = TileAddress(grid=grid, level=level, tx=tx, ty=ty)
        t0 = time.perf_counter()
        values, _io = provider.read_tile(channel, tile)
        cold = (time.perf_counter() - t0) * 1000.0
        t0 = time.perf_counter()
        provider.read_tile(channel, tile)
        warm = (time.perf_counter() - t0) * 1000.0
        out[name] = {"level": level, "cold_read_ms": round(cold, 2),
                     "second_read_ms": round(warm, 2),
                     "bytes": int(np.asarray(values, np.float32).nbytes)}
    raw.close()
    return out


def main_g324(out_path, label):
    """G3.2a.4: landing trajectories A, B, C and D only."""
    report = {"label": label, "notes": [
        "grabFramebuffer forces a render; the one final check is marked",
        "paint events are a Qt presentation signal, not photon latency",
    ]}
    rig = RIG._mount(APP, visible=("CD3",))
    binding = rig.mount.gpu_binding
    report["backend"] = rig.mount.backend
    report["renderer"] = rig.mount.gpu_status()["environment"].get("gl_renderer")
    RIG._wait(APP, lambda: binding.stats()["coarse_channels"] == ("CD3",))

    # D first: the mount has just opened and nothing has been touched.
    report["D_first_entry_idle"] = first_entry_idle(rig)

    rig.mount.host.jump_to(320, 320, 384, 384)
    RIG._settled_on_level(APP, rig, 0, timeout=40)
    quiet(0.4)
    report["A_jump_during_motion"] = jump_during_motion(rig)
    quiet(0.4)
    RIG._close(rig)

    # B needs a rig that has never been to either landing.
    cold = RIG._mount(APP, visible=("CD3",))
    cold_binding = cold.mount.gpu_binding
    RIG._wait(APP, lambda: cold_binding.stats()["coarse_channels"] == ("CD3",))
    quiet(0.3)
    painter = _PaintCounter(cold.mount.gpu_layer)
    report["B_two_fast_far_jumps"] = two_fast_far_jumps(cold)
    report["B_paint_events"] = painter.count
    log, _swaps = instrument(cold)
    report["C_patch_round_trip"] = patch_round_trip(cold, log)
    RIG._close(cold)

    out_path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


def main_g325(out_path, label):
    """G3.2a.5: what the first natural paint after a hot patch return shows."""
    report = {"label": label, "notes": [
        "grabFramebuffer forces a render; the two pixel oracles are marked",
        "paint events are a Qt presentation signal, not photon latency",
    ]}
    rig = RIG._mount(APP, visible=("CD3",))
    binding = rig.mount.gpu_binding
    report["backend"] = rig.mount.backend
    report["renderer"] = rig.mount.gpu_status()["environment"].get("gl_renderer")
    RIG._wait(APP, lambda: binding.stats()["coarse_channels"] == ("CD3",))
    quiet(0.3)
    log, _swaps = instrument(rig)
    report["patch_return"] = patch_return_first_paint(rig, log)
    RIG._close(rig)
    out_path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


def main():
    out_path = pathlib.Path(sys.argv[1])
    label = sys.argv[2] if len(sys.argv) > 2 else "run"
    if len(sys.argv) > 3 and sys.argv[3] == "g324":
        return main_g324(out_path, label)
    if len(sys.argv) > 3 and sys.argv[3] == "g325":
        return main_g325(out_path, label)
    report = {"label": label, "notes": [
        "frameSwapped is a Qt presentation event, not display photon latency",
        "grabFramebuffer forces a render; the samples below are marked",
    ]}

    rig = RIG._mount(APP, visible=("CD3",))
    binding = rig.mount.gpu_binding
    report["backend"] = rig.mount.backend
    report["renderer"] = rig.mount.gpu_status()["environment"].get("gl_renderer")
    RIG._wait(APP, lambda: binding.stats()["coarse_channels"] == ("CD3",))
    quiet(0.3)

    # ── 1. first entry, with no gesture at all ────────────────────────
    entry = counters(rig)
    quiet(1.0)
    report["first_entry_no_gesture"] = {
        "controller_level": int(rig.mount.host.stack.controller.level),
        "visible_tiles": len(rig.mount.host.stack.controller.snapshot().visible_tiles),
        "coarse_channels": list(binding.stats()["coarse_channels"]),
        "fine_channels": list(binding.stats()["fine_channels"]),
        "fine_tiles": binding.stats()["fine_tiles"],
        "fine_levels": {c: list(v) for c, v in binding.stats()["fine_levels"].items()},
        "fine_budget_refused": list(binding.stats()["fine_budget_refused"]),
        "requested_fine_last_epoch": binding.stats()["requested_fine_last_epoch"],
        "reads_after_entry": counters(rig)["provider_reads"] - entry["provider_reads"],
        "grab_sample_marked": "forced render",
        "picture_rgb_max": int(RIG._grab(rig)[..., 0:3].max()),
    }

    # ── 2. the drag ───────────────────────────────────────────────────
    rig.mount.host.jump_to(256, 256, 512, 512)
    RIG._settled_on_level(APP, rig, 0, timeout=40)
    quiet(0.5)
    log, swaps = instrument(rig)
    painter = _PaintCounter(rig.mount.gpu_layer)
    report["drag"] = drag(rig, log, swaps)
    quiet(0.5)
    settled = binding.stats()
    view_box = rig.mount.host.stack.view.view_box
    (x0, x1), (y0, y1) = view_box.viewRange()
    last = log.entries[-1]["world_rect"] if log.entries else None
    report["drag_after_release"] = {
        "camera_world_rect": [round(float(v), 3) for v in (x0, x1, y0, y1)],
        "last_submit_world_rect": last,
        "matches_camera": bool(last is not None and
                               abs(last[0] - x0) < 1.0 and abs(last[2] - y0) < 1.0),
        "fine_tiles": settled["fine_tiles"],
        "fine_budget_refused": list(settled["fine_budget_refused"]),
        "settled_on_target_level": bool(
            RIG._settled_on_level(APP, rig, int(rig.mount.host.stack.controller.level),
                                  timeout=30)),
    }
    # ── 3. a drag that never leaves loaded ground ─────────────────────
    rig.mount.host.jump_to(256, 256, 512, 512)
    RIG._settled_on_level(APP, rig, 0, timeout=40)
    quiet(0.5)
    report["hot_drag"] = hot_drag(rig, log)

    # ── 4. marked pixel samples taken DURING a gesture ────────────────
    report["sampled_drag"] = sampled_drag(rig, log)

    # ── 5. two far landings back to back ──────────────────────────────
    report["repeat_far_jumps"] = repeat_far_jumps(rig, log)


    # ── 6. patch A -> B -> A ──────────────────────────────────────────
    report["patch_round_trip"] = patch_round_trip(rig, log)

    # ── 7. untick and tick back, camera still ─────────────────────────
    report["untick_and_reenable"] = untick_and_reenable(rig, log)

    # ── 8. a second channel's first appearance ────────────────────────
    report["new_channel_timeline"] = new_channel_timeline(rig)
    report["paint_events_on_mounted_widget"] = painter.count
    report["paint_note"] = (
        "Paint events ARE delivered to the offscreen widget; buffer SWAPS are "
        "not, which is why frameSwapped reads zero here. Neither number is "
        "the display's input-to-photon latency.")
    RIG._close(rig)

    # ── 9. the same two far landings on a COLD rig with slow reads ────
    cold = RIG._mount(APP, visible=("CD3",))
    cold_binding = cold.mount.gpu_binding
    RIG._wait(APP, lambda: cold_binding.stats()["coarse_channels"] == ("CD3",))
    quiet(0.3)
    cold_log, _cold_swaps = instrument(cold)
    report["slow_far_jumps_cold"] = slow_far_jumps(cold, cold_log)
    RIG._close(cold)

    # ── 9b. the same probe with the PRODUCTION I/O width (8 workers) ──
    narrow = RIG._big_mount(APP, visible=("CD3",))
    narrow_binding = narrow.mount.gpu_binding
    RIG._wait(APP, lambda: narrow_binding.stats()["coarse_channels"] == ("CD3",),
              timeout=60)
    quiet(0.3)
    narrow_log, _narrow_swaps = instrument(narrow)
    report["slow_far_jumps_eight_io_workers"] = slow_far_jumps(
        narrow, narrow_log, hold_ms=120)
    report["slow_far_jumps_eight_io_workers"]["io_workers"] = 8
    RIG._close(narrow)

    # ── 10. what a tile really costs on the demo slide ────────────────
    report["real_dataset_costs"] = real_dataset_costs(
        "/sda1/Fusion/benchmark/tonsil/"
        "2025.12.21_Final_28127_22_Slice2_Tonsil.ome.tif")

    out_path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items()
                      if k in ("label", "backend", "first_entry_no_gesture",
                               "drag", "drag_after_release")}, indent=2))


main()
