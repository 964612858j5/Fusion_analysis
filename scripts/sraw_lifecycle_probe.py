"""v16_5 S-Raw: the Rust-drawn Step1 viewer's lifecycle on the REAL mount.

    sraw_lifecycle_probe.py SCENARIO --slide PATH   -> one JSON line

One scenario per process (one GL widget per process):
  pause_resume    paused: no read, the camera may move; resumed: the
                  picture catches up and is complete again
  fusion_fallback switching to Fusion hands the picture to the Python GPU
                  path; its scheduler starts only then; the Rust readers end
  source_change   a rebuilt viewer (whole-slide region) is Rust again
  partial_roi     a region inside the slide hands over to the Python path
  close           closing ends the Rust reader threads and leaves no layer
"""

import argparse
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def readers():
    return sum(1 for t in threading.enumerate() if t.name.startswith("fvc-read"))


def os_readers():
    """Rust threads are not Python threads: count them in /proc."""
    count = 0
    for tid in os.listdir("/proc/self/task"):
        try:
            with open(f"/proc/self/task/{tid}/comm") as fh:
                if fh.read().startswith("fvc-read"):
                    count += 1
        except OSError:
            pass
    return count


def wait_for(rig, predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        rig.pump(0.05)
    return predicate()


def s_pause_resume(rig, out):
    from sraw_rig import stable
    box = rig.mount.host.stack.view.view_box
    h0, w0 = rig.shape
    _shot, out["stable_before"] = stable(rig)
    rig.mount.pause_requests()
    reads0 = rig.mount.gpu_layer.core.stats()["reads"]
    box.setRange(xRange=(w0 * 0.3, w0 * 0.3 + 500), yRange=(h0 * 0.3, h0 * 0.3 + 500), padding=0)
    rig.pump(1.0)
    out["reads_while_paused"] = rig.mount.gpu_layer.core.stats()["reads"] - reads0
    out["hidden_while_paused"] = rig.mount.gpu_layer.hidden
    rig.mount.resume_requests()
    _shot, out["stable_after"] = stable(rig)
    report = rig.mount.gpu_layer.last_report
    out["complete_after"] = report.get("shown") == report.get("wanted") and report.get("wanted", 0) > 0
    out["ok"] = (out["stable_before"] and out["reads_while_paused"] == 0 and out["hidden_while_paused"]
                 and out["stable_after"] and out["complete_after"])


def s_fusion_fallback(rig, out):
    from sraw_rig import stable
    from block01.ui.step1_viewer_mount import LEGACY_FUSION
    _shot, out["stable_rust"] = stable(rig)
    scheduler = rig.mount.host.stack.scheduler
    out["scheduler_started_before"] = scheduler.started
    out["readers_before"] = os_readers()
    rig.mount.set_mode(LEGACY_FUSION)
    ok = wait_for(rig, lambda: type(rig.mount.gpu_binding).__name__ == "Step1GpuBinding")
    out["handed_over"] = ok
    out["layer"] = type(rig.mount.gpu_layer).__name__
    _shot, out["stable_python"] = stable(rig)
    out["scheduler_started_after"] = scheduler.started
    out["readers_after"] = (wait_for(rig, lambda: os_readers() == 0, 5.0), os_readers())
    out["rust_refused"] = rig.mount._rust_refused
    out["ok"] = (out["stable_rust"] and not out["scheduler_started_before"] and out["readers_before"] > 0
                 and ok and out["layer"] == "Step1GpuLayer" and out["stable_python"]
                 and out["scheduler_started_after"] and out["readers_after"][0])


def s_source_change(rig, out):
    from sraw_rig import stable
    _shot, out["stable_before"] = stable(rig)
    first_core = rig.mount.gpu_layer.core
    h0, w0 = rig.shape
    rig.window._active_roi = {"name": "ROI_2", "bbox_fullres": [0, h0, 0, w0]}
    rig.mount.source_changed("roi")
    rig.pump(0.5)
    out["binding"] = type(rig.mount.gpu_binding).__name__
    out["new_core"] = rig.mount.gpu_layer is not None and rig.mount.gpu_layer.core is not first_core
    _shot, out["stable_after"] = stable(rig)
    out["readers"] = os_readers()
    out["ok"] = (out["stable_before"] and out["binding"] == "Step1CoreBinding" and out["new_core"]
                 and out["stable_after"] and out["readers"] <= 6)


def s_partial_roi(rig, out):
    """A region that is not the whole slide is outside the thin slice: the
    rebuilt viewer takes the Python GPU path and says why."""
    from sraw_rig import stable
    h0, w0 = rig.shape
    rig.window._active_roi = {"name": "ROI_3", "bbox_fullres": [100, h0 - 100, 80, w0 - 80]}
    rig.mount.source_changed("roi")
    ok = wait_for(rig, lambda: type(rig.mount.gpu_binding).__name__ == "Step1GpuBinding")
    out["binding"] = type(rig.mount.gpu_binding).__name__
    out["rust_refused"] = rig.mount._rust_refused
    _shot, out["stable"] = stable(rig)
    out["ok"] = ok and "whole slide" in out["rust_refused"] and out["stable"]


def s_close(rig, out):
    from sraw_rig import stable
    _shot, out["stable"] = stable(rig)
    out["readers_open"] = os_readers()
    rig.mount.close()
    out["readers_closed"] = (wait_for(rig, lambda: os_readers() == 0, 5.0), os_readers())
    out["layer_after"] = rig.mount.gpu_layer
    out["ok"] = (out["stable"] and out["readers_open"] == 6 and out["readers_closed"][0]
                 and out["layer_after"] is None)


SCENARIOS = {n[2:]: f for n, f in globals().items() if n.startswith("s_")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", choices=sorted(SCENARIOS))
    ap.add_argument("--slide", required=True)
    args = ap.parse_args()
    from sraw_rig import open_rig
    out = {"scenario": args.scenario}
    try:
        rig = open_rig("rust", args.slide)
        out["binding_at_start"] = type(rig.mount.gpu_binding).__name__
        SCENARIOS[args.scenario](rig, out)
        if args.scenario != "close":
            rig.mount.close()
    except Exception as exc:                                # noqa: BLE001 -- reported
        import traceback
        out.update(ok=False, error=repr(exc), trace=traceback.format_exc()[-2000:])
    print(json.dumps(out, default=str), flush=True)
    os._exit(0 if out.get("ok") else 1)


if __name__ == "__main__":
    main()
