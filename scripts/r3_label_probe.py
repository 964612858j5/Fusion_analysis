"""v16_5 R3: Step3's masks over the REAL mount's picture, either backend.

    r3_label_probe.py --backend rust|python --slide PATH --out NPZ [--clip]

Opens a Step3-kind viewer (it draws masks) on `--slide`, waits for a stable
picture over the label tests' view, stops the label binding (the cases set
the snapshots themselves) and draws the cases of `test_step3_label_render`
(its tiles, styles and concave region with `--clip`). Saves per case the
screen id image and what is presented, the picture under the masks, the
view and the label counters -- for `tests/test_v16_5_r3_labels.py` to check
against the independent references and to compare the two backends.
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tests"))

WORLD = (3.3, 1003.3, 11.0, 811.0)


def grab(layer):
    """Qt's own picture of the widget (`grabFramebuffer`), RGBA rows top first."""
    import numpy as np
    from PyQt5 import QtGui
    image = layer.grabFramebuffer().convertToFormat(QtGui.QImage.Format_RGBA8888)
    ptr = image.constBits()
    ptr.setsize(image.byteCount())
    return np.frombuffer(ptr, np.uint8).reshape(image.height(), image.bytesPerLine() // 4, 4)[:, :image.width()].copy()
CONCAVE = ((100.0, 60.0), (900.0, 60.0), (900.0, 300.0), (420.0, 420.0),
           (900.0, 560.0), (900.0, 760.0), (100.0, 760.0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=("rust", "python"), required=True)
    ap.add_argument("--slide", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--clip", action="store_true")
    args = ap.parse_args()
    import numpy as np
    from sraw_rig import open_rig, stable
    out = {"backend": args.backend}
    try:
        kw = dict(roi=[60, 760, 100, 900], polygon=CONCAVE) if args.clip else {}
        rig = open_rig(args.backend, args.slide, labels=True, **kw)
        from test_step3_label_render import _tiles
        from block01.ui.step1_gpu_layer import (LABEL_FILL, LABEL_OUTLINE, LabelLayer, LabelPlane,
                                                LabelSnapshot)
        mount = rig.mount
        out["binding"] = type(mount.gpu_binding).__name__
        rig.mount.host.stack.view.view_box.setRange(xRange=WORLD[:2], yRange=WORLD[2:], padding=0)
        picture, out["stable"] = stable(rig)
        mount._stop_label_binding()                         # the cases set the snapshots
        layer = mount.gpu_layer
        out["view"] = layer._view_rect() if hasattr(layer, "_view_rect") else None
        if out["view"] is None:
            (x0, x1), (y0, y1) = mount.host.stack.view.view_box.viewRange()
            out["view"] = (x0, x1, y0, y1)
        saved = {"picture": layer.readback_rgba_for_test()}

        def snap(planes, **style):
            return LabelSnapshot((LabelLayer("cell", planes, **style),))

        def draw(name, snapshot):
            layer.set_labels(snapshot)
            saved[f"{name}_ids"] = layer.readback_label_ids_for_test()
            saved[f"{name}_shown"] = layer.readback_shown_for_test()

        planes = _tiles()
        if args.clip:
            for mode in (LABEL_FILL, LABEL_OUTLINE):
                draw(f"clip_{mode}", snap(planes, alpha=1.0, mode=mode, width=2))
            draw("clip_green", snap(planes, alpha=1.0, mode=LABEL_FILL, color=(0, 1, 0)))
        else:
            draw("ids", snap(planes))
            old = _tiles(seed=1)
            x0, y0 = old[0].world_rect[0], old[0].world_rect[2]
            new_ids = np.zeros((5, 7), np.uint32)
            new_ids[1:3, 1:4] = 2 ** 24 + 99
            new = LabelPlane(("new",), (x0 + 50.0, x0 + 400.0, y0 + 30.0, y0 + 300.0), new_ids)
            draw("overwrite", snap(old + (new,)))
            for width in (0, 1, 2, 4):
                draw(f"outline{width}", snap(planes, color=(1.0, 0.0, 1.0), alpha=1.0, width=width,
                                             mode=LABEL_OUTLINE))
            for width in (1, 2):                             # dashed (multi-result plan §4)
                draw(f"dash{width}", snap(planes, color=(1.0, 0.0, 1.0), alpha=1.0, width=width,
                                          mode=LABEL_OUTLINE, dash=True))
            draw("fill", snap(planes, alpha=1.0, mode=LABEL_FILL))
            draw("fill_half", snap(planes, alpha=0.5, mode=LABEL_FILL))
            draw("nucleus", LabelSnapshot((
                LabelLayer("cell", planes, color=(1.0, 0.0, 0.0), alpha=1.0, mode=LABEL_FILL),
                LabelLayer("nucleus", planes, color=(0.0, 0.0, 1.0), alpha=1.0, width=1,
                           mode=LABEL_OUTLINE))))
            saved["nucleus_grab"] = grab(layer)              # what Qt presents
            host = mount.host                                # a new size: targets made again
            host.resize(host.width() + 96, host.height() + 64)
            stable(rig)
            draw("resized", snap(planes, color=(1.0, 0.0, 1.0), alpha=1.0, width=2, mode=LABEL_OUTLINE))
            saved["resized_picture"] = layer.readback_rgba_for_test()
            saved["resized_grab"] = grab(layer)
            (x0, x1), (y0, y1) = mount.host.stack.view.view_box.viewRange()
            out["resized_view"] = (x0, x1, y0, y1)
            before = layer.label_stats()
            for alpha in (0.3, 0.6, 0.9):
                layer.set_labels(snap(planes, alpha=alpha))
            after = layer.label_stats()
            out["no_channel"] = {k: (before[k], after[k]) for k in ("image_submissions", "label_compositions")}
            draw("none", LabelSnapshot())
            out["none_shown_flag"] = bool(layer.label_stats()["shown"])
            big = LabelPlane(("big",), (0.0, 10.0, 0.0, 10.0), np.zeros((8192, 8193), np.uint32))
            try:
                layer.set_labels(snap((big,)))
                out["budget_refused"] = ""
            except RuntimeError as exc:
                out["budget_refused"] = str(exc)
        np.savez_compressed(args.out, **saved)
        out["ok"] = bool(out["stable"])
        mount.close()
    except Exception as exc:                                # noqa: BLE001 -- reported
        import traceback
        out.update(ok=False, error=repr(exc), trace=traceback.format_exc()[-2000:])
    print(json.dumps(out, default=str), flush=True)
    os._exit(0 if out.get("ok") else 1)


if __name__ == "__main__":
    main()
