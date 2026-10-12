"""v16_5 R3: the Rust-drawn Step1 viewer falls back at run time, on the
REAL mount (R3 plan §2.5).

    r3_fallback_probe.py SCENARIO --slide PATH  -> one JSON line

The caller sets `BLOCK01_RUST_TEST_FAULT` (the core's test-only fault
injection, which drives the real counting); one scenario per process:
  stays           (tile:2 or all:7) the failures stay under the limits:
                  the picture stays Rust and becomes complete
  falls_back      (tile:3, all:8 or paint:N) the core gives up: the Python
                  GPU path takes over on the same camera, starts its
                  scheduler, the picture is stable; a failed paint drew
                  nothing; the Rust readers end within the exit's one
                  second
  reinit          (reinit) the viewer moves to another window, Qt builds a
                  new GL context, the core's re-initialisation fails: the
                  Python path takes over
  init            (init) the first GL initialisation fails: the mount
                  starts on the Python path
  not_in_slide    a Fusion group names a channel the slide lacks
The view never moves: a still view must reach the limits too.
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sraw_lifecycle_probe import os_readers, wait_for  # noqa: E402


def _python_now(rig):
    """Asked each step: the Rust layer is told to paint (as `stable` does),
    so its paint checks the failures."""
    layer = rig.mount.gpu_layer
    if layer is not None:
        layer.update()
    return type(rig.mount.gpu_binding).__name__ == "Step1GpuBinding"


def _after_fallback(rig, out, scheduler):
    from sraw_rig import stable
    from block01.utils import closing_threads
    out["rust_refused"] = rig.mount._rust_refused
    _shot, out["stable_python"] = stable(rig)
    out["scheduler_started"] = scheduler.started
    started = time.monotonic()
    left = closing_threads.wait_all(1.0)
    out["exit_wait_s"] = round(time.monotonic() - started, 3)
    out["exit_left"] = left
    out["rust_readers_after"] = os_readers()
    return (out["stable_python"] and out["scheduler_started"] and out["exit_wait_s"] <= 1.1
            and left["rust_left"] == 0 and out["rust_readers_after"] == 0)


def s_stays(rig, out):
    from sraw_rig import stable
    _shot, out["stable"] = stable(rig, timeout=60.0)
    out["binding"] = type(rig.mount.gpu_binding).__name__
    out["read_errors"] = int(rig.mount.gpu_layer.core.stats()["read_errors"])
    report = rig.mount.gpu_layer.last_report
    out["complete"] = report.get("shown") == report.get("wanted") and report.get("wanted", 0) > 0
    out["ok"] = (out["stable"] and out["binding"] == "Step1CoreBinding" and out["read_errors"] > 0
                 and out["complete"] and not rig.mount._rust_refused)


def s_falls_back(rig, out):
    scheduler = rig.mount.host.stack.scheduler
    layer = rig.mount.gpu_layer
    reports = []
    paint = layer.paintGL

    def watched():
        paint()
        report = layer.last_report
        if report and (not reports or report is not reports[-1]):
            reports.append(report)
    layer.paintGL = watched
    out["scheduler_started_before"] = scheduler.started
    out["handed_over"] = wait_for(rig, lambda: _python_now(rig), 30.0)
    failed = [r for r in reports if r.get("error") or r.get("read_failure")]
    # a failed paint or given-up reads stop before the blit (draw_ms is set after it)
    out["failed_frames_drawn"] = sum(1 for r in failed if float(r.get("draw_ms", 0.0)) > 0.0)
    out["ok"] = (out["handed_over"] and not out["scheduler_started_before"]
                 and out["failed_frames_drawn"] == 0 and _after_fallback(rig, out, scheduler))


def s_reinit(rig, out):
    from PyQt5 import QtWidgets
    from sraw_rig import stable
    scheduler = rig.mount.host.stack.scheduler
    _shot, out["stable_rust"] = stable(rig)
    host = rig.mount.host
    other = QtWidgets.QWidget()
    other.resize(host.width(), host.height())
    lay = QtWidgets.QVBoxLayout(other)
    lay.setContentsMargins(0, 0, 0, 0)
    host.setParent(other)
    lay.addWidget(host)
    other.show()
    host.show()
    rig.keep = other
    out["handed_over"] = wait_for(rig, lambda: _python_now(rig), 30.0)
    out["ok"] = (out["stable_rust"] and out["handed_over"] and "initialisation" in str(rig.mount._rust_refused)
                 and _after_fallback(rig, out, scheduler))


def s_init(rig, out):
    """(init) the core's first GL initialisation fails: the mount starts
    on the Python GPU path and says why."""
    from sraw_rig import stable
    out["binding"] = type(rig.mount.gpu_binding).__name__
    out["rust_refused"] = rig.mount._rust_refused
    _shot, out["stable"] = stable(rig)
    out["ok"] = (out["binding"] == "Step1GpuBinding" and "initialisation" in str(out["rust_refused"])
                 and out["stable"])


def s_not_in_slide(rig, out):
    """A Fusion group names a channel the slide does not have: the Python
    GPU path takes over (it draws what it can) and says why. The product's
    Fusion model already drops channels that are not enabled, so the
    display is given the stray channel here, at the binding (a guard the
    product paths do not reach today)."""
    import dataclasses
    from block01.ui.step1_viewer_mount import LEGACY_FUSION
    scheduler = rig.mount.host.stack.scheduler
    binding = rig.mount.gpu_binding
    source = binding._display_source

    def with_stray():
        display = source()
        groups = {g: dict(chs, GHOST=1.0) for g, chs in display.groups.items()}
        return dataclasses.replace(display, groups=groups)
    binding._display_source = with_stray
    rig.mount.set_mode(LEGACY_FUSION)
    out["handed_over"] = wait_for(rig, lambda: _python_now(rig), 30.0)
    out["ok"] = (out["handed_over"] and "not in the slide" in str(rig.mount._rust_refused)
                 and _after_fallback(rig, out, scheduler))


SCENARIOS = {n[2:]: f for n, f in globals().items() if n.startswith("s_")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", choices=sorted(SCENARIOS))
    ap.add_argument("--slide", required=True)
    args = ap.parse_args()
    from sraw_rig import open_rig
    out = {"scenario": args.scenario, "fault": os.environ.get("BLOCK01_RUST_TEST_FAULT", "")}
    try:
        rig = open_rig("rust", args.slide)
        out["binding_at_start"] = type(rig.mount.gpu_binding).__name__
        if out["binding_at_start"] != "Step1CoreBinding" and args.scenario != "init":
            raise RuntimeError(f"not the Rust viewer: {out['binding_at_start']}")
        SCENARIOS[args.scenario](rig, out)
        rig.mount.close()
    except Exception as exc:                                # noqa: BLE001 -- reported
        import traceback
        out.update(ok=False, error=repr(exc), trace=traceback.format_exc()[-2000:])
    print(json.dumps(out, default=str), flush=True)
    os._exit(0 if out.get("ok") else 1)


if __name__ == "__main__":
    main()
