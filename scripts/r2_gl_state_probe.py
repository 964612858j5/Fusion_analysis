"""v16_5 R2 (plan §5.6): the GL state around the PRODUCTION Rust paint.

    r2_gl_state_probe.py --project P.json   -> one JSON line

Opens the real Step1 mount with the Rust backend on a project with a
polygon region (`r2_synth_project.py --roi ... --polygon ...`) and wraps
the layer's `paintGL`: a full glGet snapshot (R0's `r0_gl_probe.snapshot`:
~100 states, 16 texture units) before and after every paint, whose
difference must be empty. Then drives:
  overlay      the polygon region, Overlay, camera over and across the edge
  fusion       the same in Fusion
  offscreen    the camera wholly outside the region (the empty-scissor return)
  resize       the canvas resized twice
  reparent     the host moved to another parent window and back
  other        another painter (R0's `OtherPainter`) drawing after the core
               in the same paintGL: its square shows, the state still holds
Reports per step the paints seen, the differing states, and whether the
picture is stable.
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    args = ap.parse_args()
    sys.path.insert(0, HERE)
    import numpy as np
    from sraw_rig import open_rig, stable
    import r0_gl_probe as r0
    from PyQt5 import QtWidgets

    project = json.load(open(args.project))
    rig = open_rig("rust", project["slide"], channels=["a", "b", "c", "f"], roi=project["roi"],
                   decisions=project["decisions"], zarr=project["zarr"], polygon=project.get("polygon"))
    mount, pump = rig.mount, rig.pump
    layer = mount.gpu_layer
    record = {"paints": 0, "diffs": []}
    extra = {"painter": None}
    original = layer.paintGL

    def wrapped():
        before = r0.snapshot()
        original()
        if extra["painter"] is not None:
            extra["painter"].paint()
        after = r0.snapshot()
        record["paints"] += 1
        d = r0.diff(before, after)
        if d:
            record["diffs"].append({k: [str(v[0]), str(v[1])] for k, v in d.items()})

    layer.paintGL = wrapped
    box = mount.host.stack.view.view_box
    h0, w0 = rig.shape
    y0, y1, x0, x1 = project["roi"]
    steps = []

    def step(name, action):
        record["paints"], record["diffs"] = 0, []
        action()
        shot, ok = stable(rig)
        steps.append({"step": name, "paints": record["paints"], "stable": ok,
                      "binding": type(mount.gpu_binding).__name__,
                      "diffs": record["diffs"][:3], "diff_count": len(record["diffs"])})
        return shot

    step("overlay", lambda: box.setRange(xRange=(x0 - 200, x0 + 900), yRange=(y0 - 200, y0 + 900), padding=0))
    from block01.ui.step1_viewer_mount import LEGACY_FUSION, LEGACY_OVERLAY
    step("fusion", lambda: mount.set_mode(LEGACY_FUSION))
    step("offscreen", lambda: box.setRange(xRange=(w0 + 500, w0 + 900), yRange=(h0 + 500, h0 + 900), padding=0))
    step("back", lambda: (mount.set_mode(LEGACY_OVERLAY),
                          box.setRange(xRange=(0, w0), yRange=(0, h0), padding=0)))
    host = mount.host
    step("resize", lambda: (host.resize(700, 460), pump(0.3), host.resize(530, 530)))
    other_window = QtWidgets.QWidget()
    other_window.resize(600, 600)
    parent = host.parentWidget()

    def reparent():
        host.setParent(other_window)
        other_window.show()
        host.show()
        pump(0.5)
        host.setParent(parent)
        host.show()
    step("reparent", reparent)

    def other():
        layer.makeCurrent()
        extra["painter"] = r0.OtherPainter()
        layer.doneCurrent()
    shot = step("other", other)
    # the other painter draws on Qt's framebuffer, after the core's blit
    image = layer.grabFramebuffer()
    centre = image.pixelColor(image.width() // 2, image.height() // 2)
    green = int(centre.green())
    layer.makeCurrent()
    extra["painter"].release()
    extra["painter"] = None
    layer.doneCurrent()
    ok = all(s["diff_count"] == 0 and s["paints"] > 0 and s["stable"] and s["binding"] == "Step1CoreBinding"
             for s in steps)
    ok = ok and green >= 100
    result = {"ok": ok, "steps": steps, "other_green_centre": green}
    mount.close()
    rig.app.processEvents()
    print(json.dumps(result, default=str), flush=True)
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    main()
