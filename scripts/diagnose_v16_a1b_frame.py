"""Block A1b acceptance: where every part of each step page sits, in window
coordinates -- the frame slots, the Channels frame and dock, the graphics
viewport, the ViewBox and (under real GL) the GPU layer. Read-only; runs on
the path-rewritten test1 copy made by `diagnose_v16_a0_camera.py copy-project`.

    python scripts/diagnose_v16_a1b_frame.py [--size W H ...] [--steps 0 1 3] [--json OUT]

Prints one table per window size and, for every part, whether the steps
agree. S1 guarantees Step1 == Step3; Step0 / Step2 / Step4 join in S2-S4.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import diagnose_v16_a0_camera as a0  # noqa: E402


def measure(w, step):
    from PyQt5 import QtCore, QtWidgets

    def rect(widget):
        if widget is None or not widget.isVisible():
            return None
        tl = widget.mapTo(w, QtCore.QPoint(0, 0))
        return [tl.x(), tl.y(), widget.width(), widget.height()]

    out = {}
    frame = {1: getattr(w, "_step1_page_widget", None),
             3: getattr(getattr(w, "_step3", None), "_frame", None)}.get(step)
    if frame is not None and hasattr(frame, "title_slot"):
        out["title"] = rect(frame.title_slot)
        out["left tab bar"] = rect(frame.left_tabs.tabBar())
        out["right tab bar"] = rect(frame.right_tabs.tabBar())
        out["left column"] = rect(frame.left_tabs)
        out["right column"] = rect(frame.right_tabs)
        page = frame.right_tabs.currentWidget()
        item = page.layout().itemAt(0) if page is not None and page.layout() else None
        out["tool row"] = rect(item.widget()) if item is not None else None
        out["bottom"] = rect(frame.bottom_slot)
    boxes = [b for b in w._stack.currentWidget().findChildren(QtWidgets.QGroupBox)
             if b.title() == "Channels" and b.isVisible()]
    out["Channels frame"] = rect(boxes[0]) if boxes else None
    dock = getattr(w, "_channel_dock", None)
    out["dock"] = rect(dock)
    if step == 0:
        stack = getattr(getattr(w._step0, "_explore_tab", None), "stack", None)
        view, layer = getattr(stack, "view", None), None
    else:
        mount = w._step1_mount if step == 1 else w.__dict__.get("_step3_mount")
        stack = getattr(getattr(mount, "host", None), "stack", None)
        view, layer = getattr(stack, "view", None), getattr(mount, "gpu_layer", None)
    if view is not None:
        vb = view.view_box
        scene = vb.mapRectToScene(vb.rect())
        corner = view.graphics.viewport().mapTo(w, view.graphics.mapFromScene(scene.topLeft()))
        out["graphics viewport"] = rect(view.graphics.viewport())
        out["viewbox"] = [corner.x(), corner.y(), round(scene.width()), round(scene.height())]
        out["gpu layer"] = rect(layer)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--size", type=int, nargs=2, action="append", metavar=("W", "H"))
    ap.add_argument("--steps", type=int, nargs="+", default=[0, 1, 3])
    ap.add_argument("--dest", default=a0.DEFAULT_DEST)
    ap.add_argument("--json")
    args = ap.parse_args()
    sizes = args.size or [[1600, 1000], [2050, 1330]]

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import pyqtgraph as pg
    from PyQt5 import QtWidgets
    pg.setConfigOptions(antialias=True, imageAxisOrder="row-major")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setStyle("Fusion")
    from block01.ui.main_window import MainWindow

    log = a0.Log(os.path.join(os.path.dirname(args.dest), "a1b_frame_log.jsonl"))
    a0.neutralise_modals(log)
    before = a0.tree_fingerprint(a0.ORIG_PROJECT)
    w = MainWindow()
    w.resize(*sizes[0])
    w.show()
    a0.pump(300)
    a0.open_copy(w, args.dest, log)
    report = {}
    for size in sizes:
        w.resize(*size)
        a0.pump(500)
        table = {}
        for step in args.steps:
            a0.go(w, step)
            a0.pump(1500)
            table[step] = measure(w, step)
        report[f"{size[0]}x{size[1]}"] = table
        keys = sorted({k for t in table.values() for k in t})
        print(f"=== window {size[0]} x {size[1]}  (steps {args.steps})")
        for key in keys:
            vals = [table[s].get(key) for s in args.steps]
            same = "SAME" if len({json.dumps(v) for v in vals}) == 1 else "differ"
            print(f"  {key:18s} {same:6s} " + "  ".join(f"s{s}={v}" for s, v in zip(args.steps, vals)))
    ok = before == a0.tree_fingerprint(a0.ORIG_PROJECT)
    print(f"original project unchanged: {ok}")
    if args.json:
        json.dump(report, open(args.json, "w"), indent=1)
    sys.stdout.flush()
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    main()
