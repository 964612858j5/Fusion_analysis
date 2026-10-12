"""v16_5 R3: the Rust-drawn Step1 viewer and `gpu_memory`, on the REAL mount.

    r3_memory_probe.py SCENARIO (--slide PATH | --project JSON) [--shot NPY] [--smooth 0|1]
        -> one JSON line

One scenario per process (one GL widget per process):
  release_hidden  hidden, `gpu_memory.release_hidden` gives back all but the
                  coarse of the channels drawn last (an unticked channel
                  keeps nothing); shown again, the picture is identical
  smooth          Smooth off at run time (the same value again changes
                  nothing); the picture is saved to --shot for the caller
                  to compare with a process started with Smooth off; back
                  on, the first picture comes back
  still           (with --smooth 0) the picture of a process started so,
                  saved to --shot
  pressure        a new GPU block asks `gpu_memory.on_layer_active` AFTER
                  the paint (never inside it); a hidden viewer over the
                  target then gives back
  memory          the core's CPU parts stay under their bound, and so does
                  the process's resident memory growth
  dispose         after dispose nothing is left on the GPU
(R3 plan §2.2, §2.3.)
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _rss():
    with open("/proc/self/status") as fh:
        for line in fh:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    return 0


def _zoom_in(rig):
    """A close view: a texel wider than a screen pixel, where Smooth shows."""
    h0, w0 = rig.shape
    rig.mount.host.stack.view.view_box.setRange(xRange=(w0 * 0.4, w0 * 0.4 + 120),
                                                yRange=(h0 * 0.4, h0 * 0.4 + 120), padding=0)


class _Hidden:
    """Another viewer, hidden, as `gpu_memory` sees one."""
    hidden = True

    def __init__(self):
        self.released = 0

    def gpu_bytes(self):
        return 0

    def set_smooth(self, _smooth):
        pass

    def release_to_coarse(self):
        self.released += 1
        return 0


def s_release_hidden(rig, out):
    from sraw_rig import stable
    from block01.ui import gpu_memory
    layer = rig.mount.gpu_layer
    first, out["stable_before"] = stable(rig)
    from block01.ui.step1_draft_spec import STEP1_SCOPE
    dropped = rig.chosen[-1]
    with rig.state.using_scope(STEP1_SCOPE):
        rig.state.set_display_visible(dropped, False)
    _shot, out["stable_without"] = stable(rig)
    coarsest = max(level for _c, level in layer.core.gpu_slots())
    rig.mount.pause_requests()
    before = layer.gpu_bytes()
    out["released"] = gpu_memory.release_hidden("probe")
    out["bytes"] = (before, layer.gpu_bytes())
    kept = layer.core.gpu_slots()
    index = rig.names.index(dropped)
    out["kept_only_coarse"] = all(level == coarsest for _c, level in kept)
    out["dropped_channel_kept"] = sum(1 for c, _l in kept if c == index)
    rig.mount.resume_requests()
    with rig.state.using_scope(STEP1_SCOPE):
        rig.state.set_display_visible(dropped, True)
    again, out["stable_after"] = stable(rig)
    out["same_picture"] = bool(first is not None and again is not None and (first == again).all())
    out["ok"] = (out["stable_before"] and out["stable_without"] and out["released"] > 0
                 and out["bytes"][1] < out["bytes"][0] and out["kept_only_coarse"]
                 and out["dropped_channel_kept"] == 0 and out["stable_after"] and out["same_picture"])


def s_smooth(rig, out, shot_path):
    import numpy as np
    from sraw_rig import stable
    from block01.ui import gpu_memory
    layer = rig.mount.gpu_layer
    _zoom_in(rig)
    first, out["stable_on"] = stable(rig)
    calls = []
    hook = layer.on_smooth_changed
    layer.on_smooth_changed = lambda: (calls.append(1), hook())
    gpu_memory.set_smooth(True)                        # unchanged: nothing to do
    out["same_value_refreshes"] = len(calls)
    gpu_memory.set_smooth(False)
    off, out["stable_off"] = stable(rig)
    np.save(shot_path, off)
    gpu_memory.set_smooth(True)
    back, out["stable_back"] = stable(rig)
    out["back_identical"] = bool((first == back).all())
    out["off_differs"] = bool(not np.array_equal(first, off))
    out["ok"] = (out["stable_on"] and out["same_value_refreshes"] == 0 and out["stable_off"]
                 and out["stable_back"] and out["back_identical"] and out["off_differs"])


def s_still(rig, out, shot_path):
    import numpy as np
    from sraw_rig import stable
    _zoom_in(rig)
    shot, out["stable"] = stable(rig)
    np.save(shot_path, shot)
    out["ok"] = out["stable"]


def s_pressure(rig, out):
    from sraw_rig import stable
    from block01.ui import gpu_memory
    layer = rig.mount.gpu_layer
    other = _Hidden()
    gpu_memory.register(other)
    in_paint = {"now": False, "calls": [], "inside": 0}
    paint = layer.paintGL

    def painted():
        in_paint["now"] = True
        try:
            paint()
        finally:
            in_paint["now"] = False
    layer.paintGL = painted
    active = gpu_memory.on_layer_active

    def watched(who):
        in_paint["calls"].append(who is layer)
        in_paint["inside"] += int(in_paint["now"])
        return active(who)
    gpu_memory.on_layer_active = watched
    gpu_memory.VIEW_VRAM_TARGET = 0                    # any growth is over the target
    _shot, out["stable"] = stable(rig)
    out["calls"] = len(in_paint["calls"])
    out["inside_paint"] = in_paint["inside"]
    out["other_released"] = other.released
    out["ok"] = (out["stable"] and out["calls"] > 0 and all(in_paint["calls"])
                 and out["inside_paint"] == 0 and out["other_released"] > 0)


def s_memory(rig, out, rss0):
    from sraw_rig import stable
    layer = rig.mount.gpu_layer
    _shot, out["stable"] = stable(rig)
    box = rig.mount.host.stack.view.view_box
    h0, w0 = rig.shape
    for frac in (0.1, 0.5, 0.8):                        # wander: more tiles through the cache
        box.setRange(xRange=(w0 * frac, w0 * frac + 400), yRange=(h0 * frac, h0 * frac + 400), padding=0)
        stable(rig)
    stats = layer.core.stats()
    keys = ("cache_raw_bytes", "cache_corrected_bytes", "queue_extra_bytes", "held_extra_bytes",
            "label_copy_bytes", "cpu_bytes", "cpu_bound_bytes", "worker_allowance_bytes")
    out.update({k: int(stats[k]) for k in keys})
    out["rss_growth"] = _rss() - rss0
    out["ok"] = (out["stable"] and out["cpu_bytes"] <= out["cpu_bound_bytes"]
                 and out["cpu_bytes"] == sum(out[k] for k in keys[:5])
                 and out["rss_growth"] <= out["cpu_bound_bytes"])


def s_dispose(rig, out):
    from sraw_rig import stable
    layer = rig.mount.gpu_layer
    _shot, out["stable"] = stable(rig)
    out["before"] = layer.gpu_bytes()
    out["result"] = layer.dispose()
    out["ok"] = (out["stable"] and out["before"] > 0 and out["result"]["raw_textures_remaining"] == 0
                 and out["result"]["cache_bytes"] == 0
                 and out["result"]["transient_targets_remaining"] == 0)


SCENARIOS = {n[2:]: f for n, f in globals().items() if n.startswith("s_")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", choices=sorted(SCENARIOS))
    ap.add_argument("--slide", default="")
    ap.add_argument("--project", default="")
    ap.add_argument("--shot", default="")
    ap.add_argument("--smooth", type=int, default=1)
    args = ap.parse_args()
    from sraw_rig import open_rig
    out = {"scenario": args.scenario}
    rss0 = _rss()
    try:
        if args.project:
            project = json.load(open(args.project))
            rig = open_rig("rust", project["slide"], channels=[c for c in project["names"] if c != "d"],
                           roi=project["roi"], decisions=project["decisions"], zarr=project["zarr"],
                           size=1500, smooth=args.smooth)
        else:
            rig = open_rig("rust", args.slide, smooth=args.smooth)
        out["binding"] = type(rig.mount.gpu_binding).__name__
        if out["binding"] != "Step1CoreBinding":
            raise RuntimeError(f"not the Rust viewer: {out['binding']}")
        run = SCENARIOS[args.scenario]
        if args.scenario in ("smooth", "still"):
            run(rig, out, args.shot)
        elif args.scenario == "memory":
            run(rig, out, rss0)
        else:
            run(rig, out)
        rig.mount.close()
    except Exception as exc:                                # noqa: BLE001 -- reported
        import traceback
        out.update(ok=False, error=repr(exc), trace=traceback.format_exc()[-2000:])
    print(json.dumps(out, default=str), flush=True)
    os._exit(0 if out.get("ok") else 1)


if __name__ == "__main__":
    main()
