"""How many frames a Tissue Preview drag actually publishes, per step.

WHY THIS EXISTS. "The tests pass" is not the claim under review. The claim is
that a user dragging Min/Max/Gamma (or a weight) sees the picture move WHILE
the hand is moving, at a rate the frame clock explains, with the last value
never lost. That is four numbers per step -- intermediate frames published,
the revision of the last one, the deepest the pending queue ever got, and the
worst GUI callback -- and this prints them.

WHAT IT DRIVES. The real `TissuePreviewCoordinator` with the real compose
code, over a synthetic whole-slide array. The compose cost is imposed
(`--compose-ms`) rather than measured, because the point is the SCHEDULER's
behaviour when a frame costs more than the interval between inputs: 40 ms and
80 ms bracket what a real whole-slide frame was measured at.

Not a test. Nothing here asserts; it prints a table.

    python block01_v14/scripts/benchmark_tissue_preview_frames.py
    python block01_v14/scripts/benchmark_tissue_preview_frames.py --compose-ms 80

Run as a FILE, from the repo root. The package is imported as `block01` while
this checkout may be named something else (`block01_v14`, a worktree), and an
older `block01` may sit on `sys.path` -- the same hazard `conftest.py` exists
to close for the tests. The bootstrap below closes it the same way.
"""

import argparse
import importlib.util
import os
import pathlib
import sys
import time

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = pathlib.Path(__file__).resolve().parent.parent
if __package__ in (None, ""):
    # Bind `block01` to THIS tree before anything imports from it, so the
    # numbers describe the code under review rather than another checkout's.
    spec = importlib.util.spec_from_file_location(
        "block01", _ROOT / "__init__.py",
        submodule_search_locations=[str(_ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["block01"] = module
    spec.loader.exec_module(module)
    __package__ = "block01.scripts"
    sys.path.insert(0, str(_ROOT.parent))

from PyQt5 import QtWidgets                                 # noqa: E402

from ..core import preview_compose, tissue_compose          # noqa: E402
from ..ui import block01_display as bd                      # noqa: E402

LOW_H, LOW_W = 555, 923          # the real slide's overview level


def _pattern(seed, h=LOW_H, w=LOW_W):
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    base = (yy * (seed + 1) + xx * (seed + 2)) % 255.0
    return base.astype(np.float32)


class _Context:
    """One step's render context, with `n` channels of patterned pixels."""

    def __init__(self, mode, channels):
        self.mode = mode
        self.arrays = {ch: _pattern(i) for i, ch in enumerate(channels)}
        self.mappings = {ch: (0.0, 255.0, 1.0) for ch in channels}
        self.colors = {ch: (1.0, 0.4, 0.2) for ch in channels}
        self.weights = {ch: 1.0 for ch in channels}
        self.channels = list(channels)

    def tissue_render_mode(self):
        return self.mode

    def tissue_render_snapshot(self):
        payload = {
            "mode": self.mode, "token": ("bench", "/bench.ome.tiff"),
            "arrays": dict(self.arrays),
            "mappings": dict(self.mappings),
            "colors": dict(self.colors),
        }
        if self.mode == tissue_compose.MODE_STEP0:
            payload["channel"] = self.channels[0]
            payload["nucleus_layer"] = ""
            payload["arrays"] = {self.channels[0]: self.arrays[self.channels[0]]}
        elif self.mode == tissue_compose.MODE_OVERLAY:
            payload["weights"] = dict(self.weights)
        else:
            payload["groups"] = {"g": {ch: 1.0 for ch in self.channels[1:]}}
            payload["group_weights"] = {"g": 1.0}
            payload["nucleus"] = (self.channels[0], 1.0)
            payload["fallback_norm"] = lambda a: np.clip(a / 255.0, 0, 1)
            payload["to_rgb"] = lambda cyto, nuc: np.stack(
                [cyto, np.zeros_like(cyto), nuc], axis=-1)
        return payload


class _Panel:
    """The overview panel, reduced to what publishing touches."""

    def __init__(self):
        self.installs = 0
        self.worst_ms = 0.0

    def adopt_dataset(self, _token):
        pass

    def set_channel_image(self, rgb, _token=None):
        t0 = time.perf_counter()
        self.installs += 1
        _ = np.asarray(rgb).shape
        self.worst_ms = max(self.worst_ms,
                            (time.perf_counter() - t0) * 1000.0)
        return True


class _Frames:
    """The compose thread, inline, on the simulated clock.

    A frame handed over at T comes back at T + `compose_ms`. It does NOT stop
    the clock: input keeps arriving while the thread composes, which is the
    whole situation the scheduler exists for and the one a harness that
    advanced time inside the compose would quietly remove.
    """

    def __init__(self, coordinator, compose_ms, clock):
        self.coordinator = coordinator
        self.compose_ms = float(compose_ms)
        self.clock = clock
        self.cache = preview_compose.PreviewCache()
        self.inflight = None
        self.due_at = None
        coordinator._dispatch = self._submit

    def _submit(self, snapshot):
        # Single-flight is the coordinator's rule; a second submission while
        # one is in flight would mean it had stopped keeping it.
        assert self.inflight is None, "two frames in flight at once"
        self.inflight = snapshot
        self.due_at = self.clock.now() + self.compose_ms / 1000.0
        return True

    def poll(self):
        if self.inflight is None or self.clock.now() < self.due_at:
            return False
        snapshot, self.inflight = self.inflight, None
        self.due_at = None
        result = dict(snapshot)
        result["rgb"] = tissue_compose.compose(snapshot, self.cache)
        result.pop("arrays", None)
        result.pop("to_rgb", None)
        result.pop("fallback_norm", None)
        self.coordinator.on_frame(result)
        return True


class _Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def advance(self, ms):
        self.t += ms / 1000.0


def run_one(name, mode, channels, compose_ms, hz, seconds):
    state = bd.ChannelDisplayState()
    coordinator = bd.TissuePreviewCoordinator(state)
    panel = _Panel()
    coordinator.attach_navigator(lambda: [panel])
    context = _Context(mode, channels)
    coordinator.register_context(name, context)
    state.bind_dataset(("bench", "/bench.ome.tiff"))

    clock = _Clock()
    coordinator._now = clock.now
    armed = {"at": None}
    coordinator._arm = lambda wait_ms: armed.__setitem__(
        "at", clock.now() + max(0.0, wait_ms) / 1000.0)
    frames = _Frames(coordinator, compose_ms, clock)
    coordinator.set_active_context(name, request=False)

    def _tick():
        """Everything the event loop would do at this instant."""
        frames.poll()
        if armed["at"] is not None and clock.now() >= armed["at"]:
            armed["at"] = None
            coordinator._apply_pending_frame()

    step_ms = 1000.0 / float(hz)
    total = int(seconds * hz)
    mid_installs = 0
    for i in range(1, total + 1):
        # One slider step: the display window narrows a little.
        context.mappings = {ch: (0.0, 255.0 - i * 0.2, 1.0)
                            for ch in context.channels}
        state.note_mapping_changed(context.channels[0])
        clock.advance(step_ms)
        _tick()
        if i == total // 2:
            mid_installs = panel.installs

    # The hand stops. The last VALUE must still be published, however long
    # the frame in flight when it stopped takes to come back.
    drag_published = coordinator.frame_stats()["published"]
    for _ in range(200):
        clock.advance(bd.TISSUE_FRAME_MS)
        _tick()
        if (frames.inflight is None
                and coordinator.frame_stats()["pending"] == 0):
            break

    stats = coordinator.frame_stats()
    published = coordinator.last_published()
    coordinator.shutdown("benchmark done")
    return {
        "step": name, "mode": mode, "channels": len(channels),
        "compose_ms": compose_ms,
        "inputs": stats["inputs"],
        "published": stats["published"],
        "during_drag": drag_published,
        "mid_drag_published": mid_installs,
        "last_rev": published.get("rev"),
        "input_rev": stats["input_rev"],
        "max_pending": stats["max_pending_depth"],
        "gui_ms": panel.worst_ms,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hz", type=float, default=200.0)
    ap.add_argument("--seconds", type=float, default=2.0)
    ap.add_argument("--compose-ms", type=float, nargs="*", default=[40.0, 80.0])
    args = ap.parse_args()
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    plans = [
        (bd.STEP0, tissue_compose.MODE_STEP0, ["DAPI"]),
        (bd.STEP1, tissue_compose.MODE_OVERLAY, ["DAPI", "CD3", "CD8"]),
        (bd.STEP1, tissue_compose.MODE_FUSION, ["DAPI", "CD3", "CD8"]),
        (bd.STEP2, tissue_compose.MODE_OVERLAY, ["DAPI", "CD3"]),
        (bd.STEP3, tissue_compose.MODE_OVERLAY, ["DAPI", "CD3"]),
    ]
    header = ("step", "mode", "ch", "compose", "inputs", "published",
              "in drag", "mid-drag", "last rev", "input rev", "pending",
              "gui ms")
    print(f"{args.seconds:g} s of input at {args.hz:g} Hz, "
          f"frame clock {bd.TISSUE_FRAME_MS:g} ms")
    print(" ".join(f"{h:>10}" for h in header))
    for compose_ms in args.compose_ms:
        for name, mode, channels in plans:
            r = run_one(name, mode, channels, compose_ms, args.hz,
                        args.seconds)
            print(" ".join(f"{v:>10}" for v in (
                r["step"], r["mode"], r["channels"], f"{compose_ms:g}ms",
                r["inputs"], r["published"], r["during_drag"],
                r["mid_drag_published"],
                r["last_rev"], r["input_rev"], r["max_pending"],
                f"{r['gui_ms']:.2f}")))


if __name__ == "__main__":
    main()
