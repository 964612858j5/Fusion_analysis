"""v16_5 R3 (plan §5 gate 2): the GL state around the Rust layer's paints
WITH Step3's masks, on the real mount.

    r3_gl_state_probe.py --slide PATH   -> one JSON line

A Step3-kind viewer (it draws masks) on `--slide`, its region the label
tests' concave polygon. Both ways masks reach the screen are wrapped with
R0's full glGet snapshot (`r0_gl_probe.snapshot`), before and after, whose
difference must be empty: every `paintGL`, and every `set_labels` (which
draws the masks at once, alone, in the layer's own context). Steps:
  outline / fill / nucleus  the label tests' tiles in each style
  edge      the camera across the polygon's edge, masks shown
  relabel   label-only changes (alpha), no channel composed
  resize    the canvas resized twice, masks shown
  off       masks off again
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tests"))
CONCAVE = ((100.0, 60.0), (900.0, 60.0), (900.0, 300.0), (420.0, 420.0),
           (900.0, 560.0), (900.0, 760.0), (100.0, 760.0))
WORLD = (3.3, 1003.3, 11.0, 811.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slide", required=True)
    args = ap.parse_args()
    out = {}
    try:
        from sraw_rig import open_rig, stable
        import r0_gl_probe as r0
        rig = open_rig("rust", args.slide, labels=True, roi=[60, 760, 100, 900], polygon=CONCAVE)
        from test_step3_label_render import _tiles
        from block01.ui.step1_gpu_layer import LABEL_FILL, LABEL_OUTLINE, LabelLayer, LabelSnapshot
        mount = rig.mount
        out["binding"] = type(mount.gpu_binding).__name__
        mount._stop_label_binding()
        layer = mount.gpu_layer
        record = {"paints": 0, "sets": 0, "diffs": []}
        paint = layer.paintGL

        def wrapped_paint():
            before = r0.snapshot()
            paint()
            d = r0.diff(before, r0.snapshot())
            record["paints"] += 1
            if d:
                record["diffs"].append(("paint", {k: [str(v[0]), str(v[1])] for k, v in d.items()}))
        layer.paintGL = wrapped_paint
        set_labels = layer.set_labels

        def wrapped_set(snapshot):
            layer.makeCurrent()
            try:
                before = r0.snapshot()
                result = set_labels(snapshot)
                layer.makeCurrent()
                d = r0.diff(before, r0.snapshot())
            finally:
                layer.doneCurrent()
            record["sets"] += 1
            if d:
                record["diffs"].append(("set_labels", {k: [str(v[0]), str(v[1])] for k, v in d.items()}))
            return result
        layer.set_labels = wrapped_set
        box = mount.host.stack.view.view_box
        planes = _tiles()

        def snap(*layers):
            return LabelSnapshot(tuple(layers))

        steps = []

        def step(name, action):
            record["paints"], record["sets"], record["diffs"] = 0, 0, []
            action()
            _shot, ok = stable(rig)
            steps.append({"step": name, "paints": record["paints"], "sets": record["sets"], "stable": ok,
                          "diff_count": len(record["diffs"]), "diffs": record["diffs"][:2],
                          "shown": bool(layer.label_stats().get("shown"))})

        box.setRange(xRange=WORLD[:2], yRange=WORLD[2:], padding=0)
        stable(rig)
        step("outline", lambda: layer.set_labels(snap(LabelLayer("cell", planes, width=2, mode=LABEL_OUTLINE))))
        step("fill", lambda: layer.set_labels(snap(LabelLayer("cell", planes, alpha=0.6, mode=LABEL_FILL))))
        step("nucleus", lambda: layer.set_labels(snap(
            LabelLayer("cell", planes, color=(1.0, 0.0, 0.0), alpha=1.0, mode=LABEL_FILL),
            LabelLayer("nucleus", planes, color=(0.0, 0.0, 1.0), alpha=1.0, width=1, mode=LABEL_OUTLINE))))
        step("edge", lambda: box.setRange(xRange=(700, 1100), yRange=(200, 600), padding=0))

        def relabel():
            for alpha in (0.3, 0.6, 0.9):
                layer.set_labels(snap(LabelLayer("cell", planes, alpha=alpha, mode=LABEL_FILL)))
        step("relabel", relabel)

        def resize():
            host = mount.host
            host.resize(host.width() + 120, host.height() + 80)
            rig.pump(0.3)
            host.resize(host.width() - 60, host.height() - 40)
        step("resize", resize)
        step("off", lambda: layer.set_labels(LabelSnapshot()))
        out["steps"] = steps
        out["ok"] = (out["binding"] == "Step1CoreBinding"
                     and all(s["diff_count"] == 0 and s["paints"] + s["sets"] > 0 and s["stable"] for s in steps)
                     and all(s["shown"] for s in steps if s["step"] != "off")
                     and not steps[-1]["shown"])
        mount.close()
    except Exception as exc:                                # noqa: BLE001 -- reported
        import traceback
        out.update(ok=False, error=repr(exc), trace=traceback.format_exc()[-2000:])
    print(json.dumps(out, default=str), flush=True)
    os._exit(0 if out.get("ok") else 1)


if __name__ == "__main__":
    main()
