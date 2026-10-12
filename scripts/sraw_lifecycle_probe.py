"""v16_5 S-Raw: the Rust-drawn Step1 viewer's lifecycle on the REAL mount.

    sraw_lifecycle_probe.py SCENARIO --slide PATH   -> one JSON line

One scenario per process (one GL widget per process):
  pause_resume    paused: no read, the camera may move; resumed: the
                  picture catches up and is complete again
  fusion_stays_rust  switching to Fusion and back keeps the Rust picture
                  (R2), stable both ways; the Python scheduler never starts
  source_change   a rebuilt viewer (whole-slide region) is Rust again
  partial_roi_stays_rust  a region inside the slide is drawn by a rebuilt
                  Rust viewer (R2)
  capacity_fallback  (with --project: corrected channels) a picture that
                  needs more blocks of one format than one pass binds hands
                  over to the Python GPU path, which then starts its
                  scheduler; the Rust readers end (R2 plan §2.3)
  format_switch   (with --project; the caller sets a small GPU budget and
                  three samplers) raw channels first, then corrected ones
                  only: the raw blocks are given back and the samplers move
                  to the float family; the picture stays Rust, no core
                  error (codex R2 review: reclaim, split change)
  close           closing ends the Rust reader threads and leaves no layer
(R2 migration, user-authorised 2026-10-12: fusion_fallback and partial_roi
asserted the hand-over the thin slice needed.)
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


def s_fusion_stays_rust(rig, out):
    from sraw_rig import stable
    from block01.ui.step1_viewer_mount import LEGACY_FUSION, LEGACY_OVERLAY
    _shot, out["stable_overlay"] = stable(rig)
    scheduler = rig.mount.host.stack.scheduler
    first_core = rig.mount.gpu_layer.core
    rig.mount.set_mode(LEGACY_FUSION)
    rig.pump(0.5)
    out["binding_fusion"] = type(rig.mount.gpu_binding).__name__
    _shot, out["stable_fusion"] = stable(rig)
    rig.mount.set_mode(LEGACY_OVERLAY)
    rig.pump(0.5)
    out["binding_back"] = type(rig.mount.gpu_binding).__name__
    _shot, out["stable_back"] = stable(rig)
    out["same_core"] = rig.mount.gpu_layer is not None and rig.mount.gpu_layer.core is first_core
    out["scheduler_started"] = scheduler.started
    out["ok"] = (out["stable_overlay"] and out["binding_fusion"] == "Step1CoreBinding"
                 and out["stable_fusion"] and out["binding_back"] == "Step1CoreBinding"
                 and out["stable_back"] and not out["scheduler_started"])


def s_capacity_fallback(rig, out):
    """BLOCK01_RUST_TEST_BLOCK_UNITS=2 (set by the caller): one integer and
    one float block; the corrected channels' tiles at level 0 need more."""
    from sraw_rig import stable
    box = rig.mount.host.stack.view.view_box
    h0, w0 = rig.shape
    scheduler = rig.mount.host.stack.scheduler
    out["scheduler_started_before"] = scheduler.started
    box.setRange(xRange=(w0 * 0.2, w0 * 0.2 + 1400), yRange=(h0 * 0.2, h0 * 0.2 + 1400), padding=0)
    ok = wait_for(rig, lambda: type(rig.mount.gpu_binding).__name__ == "Step1GpuBinding", 30.0)
    out["handed_over"] = ok
    out["rust_refused"] = rig.mount._rust_refused
    _shot, out["stable_python"] = stable(rig)
    out["scheduler_started_after"] = scheduler.started
    out["readers_after"] = (wait_for(rig, lambda: os_readers() == 0, 5.0), os_readers())
    out["ok"] = (ok and not out["scheduler_started_before"] and "one pass" in str(out["rust_refused"])
                 and out["stable_python"] and out["scheduler_started_after"] and out["readers_after"][0])


def s_format_switch(rig, out):
    from sraw_rig import stable
    from block01.ui.step1_draft_spec import STEP1_SCOPE
    box = rig.mount.host.stack.view.view_box
    h0, w0 = rig.shape
    errors = []
    layer = rig.mount.gpu_layer
    paint = layer.paintGL

    def watched():
        paint()
        if (layer.last_report or {}).get("error"):
            errors.append(str(layer.last_report["error"]))
    layer.paintGL = watched

    def core():
        return dict(rig.mount.gpu_binding.stats().get("core") or {})

    def show(channels):
        with rig.state.using_scope(STEP1_SCOPE):
            for c in rig.names:
                rig.state.set_display_visible(c, c in channels)
    raw = [c for c in rig.names if rig.decisions.get(c) == "original"]
    corrected = [c for c in rig.names if rig.decisions.get(c) not in ("original", None) and c != "d"]
    show(raw)
    for k in range(3):                              # level 0 in a few places: the raw blocks fill
        box.setRange(xRange=(w0 * (0.1 + 0.25 * k), w0 * (0.1 + 0.25 * k) + 900),
                     yRange=(h0 * 0.3, h0 * 0.3 + 900), padding=0)
        _shot, out[f"stable_raw_{k}"] = stable(rig)
    before = core()
    out["before"] = {k: before.get(k) for k in ("sampler_split", "gpu_live_blocks")}
    show(corrected)
    _shot, out["stable_corrected"] = stable(rig, timeout=60.0)
    after = core()
    out["after"] = {k: after.get(k) for k in ("sampler_split", "gpu_live_blocks")}
    out["binding"] = type(rig.mount.gpu_binding).__name__
    out["errors"] = errors[:3]
    split_b, split_a = tuple(before.get("sampler_split") or ()), tuple(after.get("sampler_split") or ())
    out["ok"] = (all(out[f"stable_raw_{k}"] for k in range(3)) and out["stable_corrected"]
                 and out["binding"] == "Step1CoreBinding" and not errors
                 and split_b and split_a and split_b[0] >= 2 and split_a[1] > split_b[1]
                 and (after.get("gpu_live_blocks") or {}).get("u8", 0) < (before.get("gpu_live_blocks") or {}).get("u8", 0))


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


def s_partial_roi_stays_rust(rig, out):
    """A region that is not the whole slide is drawn by a rebuilt Rust
    viewer (R2)."""
    from sraw_rig import stable
    h0, w0 = rig.shape
    first_core = rig.mount.gpu_layer.core
    rig.window._active_roi = {"name": "ROI_3", "bbox_fullres": [100, h0 - 100, 80, w0 - 80]}
    rig.mount.source_changed("roi")
    rig.pump(0.5)
    out["binding"] = type(rig.mount.gpu_binding).__name__
    out["new_core"] = rig.mount.gpu_layer is not None and rig.mount.gpu_layer.core is not first_core
    _shot, out["stable"] = stable(rig)
    out["ok"] = out["binding"] == "Step1CoreBinding" and out["new_core"] and out["stable"]


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
    ap.add_argument("--slide", default="")
    ap.add_argument("--project", default="")
    ap.add_argument("--gpu-bytes", type=int, default=0)
    args = ap.parse_args()
    from sraw_rig import open_rig
    out = {"scenario": args.scenario}
    try:
        if args.project:
            project = json.load(open(args.project))
            rig = open_rig("rust", project["slide"], channels=[c for c in project["names"] if c != "d"],
                           roi=project["roi"],
                           decisions=project["decisions"], zarr=project["zarr"], size=1500,
                           gpu_bytes=args.gpu_bytes or None)
            rig.decisions = project["decisions"]
        else:
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
