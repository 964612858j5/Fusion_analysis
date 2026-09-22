"""G3.2b.5A.2 step 2: a PASSIVE observer of the real app, on one clock.

5A.2 recorded the real desktop and found, at every Patch creation, a ~2-3
frame change of the whole Viewer image that then returns bit-identically.
It is NOT a translation and NOT a large coarse-level substitution -- but
"the same input redrawn" cannot produce different pixels, so some INPUT or
DISPLAY STATE must differ on those frames. This finds out which.

It records, on `time.time()` -- the same wall clock the screen recording is
stamped with:

  * `Step1GpuLayer.submit(source, display, viewport)` -- and, from its
    arguments, exactly what the GPU was asked to draw: every channel's
    `selected_level` ("coarse"/"fine"), its plane identities and world
    rects, the whole `DisplaySnapshot` (mode, per-channel lo/hi/gamma,
    weights, colours, nucleus) and the `ViewportSnapshot` (world rect,
    logical size, device pixel ratio);
  * the Patch chain: `patches_changed`, `geometry_committed`,
    `_on_step0_geometry_committed`, `_on_patches`,
    `_rebuild_patch_buttons` (entry AND return);
  * the ViewBox's real range and the Viewer/GPU widget geometry, sampled on
    a timer and on every `sigRangeChanged`.

EVERY HOOK IS RECORD-ONLY. Each wrapper calls the real function with the
real arguments and returns its real result; nothing is reordered, delayed,
suppressed or synthesised, and no production file is modified -- the
wrapping happens here, in this process, after import.

TIME ALIGNMENT. Both this log and the recorder's metadata use
`time.time()`. The screen recording additionally shows the `patches=N`
label, and `_on_patches` is logged here, so the two can be pinned to each
other by that event rather than by trusting the clocks alone.

CAUSALITY. Co-occurrence in time is a LEAD, not a proof. This script's job
is to say what changed during the anomalous frames; it does not by itself
establish that the change caused them.

Usage (the user drives the app exactly as normal):
    python scripts/observe_step1_patch_wobble.py --out /tmp/observe.jsonl
"""

import argparse
import importlib
import json
import os
import sys
import threading
import time

#: `python -m block01_v14.main` is how the product starts, so the package
#: is this file's grandparent directory name -- never a hard-coded
#: `block01`, which is a DIFFERENT, older package sitting beside it.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.basename(_ROOT)
if os.path.dirname(_ROOT) not in sys.path:
    sys.path.insert(0, os.path.dirname(_ROOT))


def mod(name):
    return importlib.import_module(f"{PKG}.{name}")

_LOG_LOCK = threading.Lock()
_LOG = None


def emit(kind, **fields):
    if _LOG is None:
        return
    row = {"t": time.time(), "kind": kind}
    row.update(fields)
    with _LOG_LOCK:
        _LOG.write(json.dumps(row, default=str) + "\n")
        _LOG.flush()


def jsonable(value, depth=0):
    """Small, bounded, loss-marked rendering of an arbitrary payload."""
    import numpy as np
    if depth > 4:
        return "<deep>"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, np.ndarray):
        return {"ndarray": list(value.shape), "dtype": str(value.dtype)}
    if isinstance(value, dict):
        return {str(k): jsonable(v, depth + 1) for k, v in list(value.items())[:64]}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v, depth + 1) for v in list(value)[:64]]
    return str(value)


def install_hooks():
    """Wrap the product's own entry points. Record only."""
    gpu = mod("ui.step1_gpu_layer")
    MainWindow = mod("ui.main_window").MainWindow
    OverviewPanel = mod("ui.step0.overview_panel").OverviewPanel
    Step0Page = mod("ui.step0.step0_page").Step0Page

    # ── the GPU submit: what was the layer actually asked to draw? ──
    real_submit = gpu.Step1GpuLayer.submit

    def submit(self, source_descriptor, display_snapshot, viewport_snapshot):
        try:
            channels = []
            for item in getattr(source_descriptor, "channels", ()) or ():
                planes = {
                    "coarse": [{"identity": str(p.identity),
                                "world_rect": list(p.world_rect),
                                "shape": list(getattr(p.values, "shape", ()) or ())}
                               for p in (item.coarse or ())],
                    "fine": [{"identity": str(p.identity),
                              "world_rect": list(p.world_rect),
                              "shape": list(getattr(p.values, "shape", ()) or ())}
                             for p in (item.fine or ())],
                }
                channels.append({
                    "channel": item.channel,
                    "selected_level": item.selected_level,
                    "n_coarse": len(item.coarse or ()),
                    "n_fine": len(item.fine or ()),
                    "planes": planes,
                })
            emit("gpu.submit.call",
                 channels=channels,
                 display={
                     "mode": getattr(display_snapshot, "mode", None),
                     "mappings": jsonable(getattr(display_snapshot, "mappings", {})),
                     "weights": jsonable(getattr(display_snapshot, "weights", {})),
                     "colors": jsonable(getattr(display_snapshot, "colors", {})),
                     "groups": jsonable(getattr(display_snapshot, "groups", {})),
                     "group_weights": jsonable(getattr(display_snapshot, "group_weights", {})),
                     "nucleus": jsonable(getattr(display_snapshot, "nucleus", None)),
                 },
                 viewport={
                     "world_rect": list(getattr(viewport_snapshot, "world_rect", ()) or ()),
                     "logical_size": list(getattr(viewport_snapshot, "logical_size", ()) or ()),
                     "device_pixel_ratio": getattr(viewport_snapshot, "device_pixel_ratio", None),
                     "physical_size": list(getattr(viewport_snapshot, "physical_size", ()) or ()),
                 })
        except Exception as exc:                            # noqa: BLE001
            emit("gpu.submit.log_error", error=str(exc))
        started = time.time()
        result = real_submit(self, source_descriptor, display_snapshot,
                             viewport_snapshot)
        emit("gpu.submit.done", ms=round((time.time() - started) * 1000, 3),
             stats=jsonable(result))
        return result

    gpu.Step1GpuLayer.submit = submit

    # ── the Patch chain ────────────────────────────────────────────
    def wrap(owner, name, kind):
        real = getattr(owner, name, None)
        if real is None:
            return

        def called(self, *a, **kw):
            emit(f"{kind}.enter", n_args=len(a))
            try:
                return real(self, *a, **kw)
            finally:
                emit(f"{kind}.return")

        setattr(owner, name, called)

    wrap(MainWindow, "_on_patches", "main._on_patches")
    wrap(MainWindow, "_rebuild_patch_buttons", "main._rebuild_patch_buttons")
    wrap(MainWindow, "_on_step0_geometry_committed", "main._geometry_committed")
    wrap(MainWindow, "_select_preview_patch", "main._select_preview_patch")
    wrap(Step0Page, "_persist_geometry_edit", "step0._persist_geometry_edit")
    wrap(Step0Page, "_on_geometry_persist_published", "step0._persist_published")
    wrap(OverviewPanel, "_add_patch", "overview._add_patch")
    wrap(OverviewPanel, "_rebuild_patch_artists", "overview._rebuild_artists")

    # ── the mount's navigation entries, so a jump cannot go unseen ──
    mount_mod = mod("ui.step1_viewer_mount")
    for name in ("show_patch", "jump_to_point"):
        wrap(mount_mod.Step1WholeSlideMount, name, f"mount.{name}")

    return True


class Sampler:
    """The real ViewBox range and the real widget sizes, on a timer."""

    def __init__(self, window, period_ms=16):
        from PyQt5 import QtCore
        self.window = window
        self.last = None
        self.timer = QtCore.QTimer(window)
        self.timer.setInterval(period_ms)
        self.timer.timeout.connect(self.sample)
        self.timer.start()
        emit("sampler.started", period_ms=period_ms)

    def state(self):
        w = self.window
        mount = getattr(w, "_step1_mount", None)
        out = {"window_size": [w.width(), w.height()]}
        try:
            controller = mount.host.stack.controller
            box = controller.view.view_box
            (x0, x1), (y0, y1) = box.viewRange()
            out["view_range"] = [round(x0, 4), round(x1, 4),
                                 round(y0, 4), round(y1, 4)]
            out["view_pixel_size"] = round(float(box.viewPixelSize()[0]), 9)
            vp = controller.view.graphics.viewport()
            out["graphics_viewport"] = [vp.width(), vp.height()]
        except Exception:                                   # noqa: BLE001
            pass
        for attr in ("widget", "gpu_widget", "_gpu_widget"):
            widget = getattr(mount, attr, None)
            if widget is not None and hasattr(widget, "width"):
                out[f"mount_{attr}"] = [widget.width(), widget.height()]
        try:
            out["patch_buttons"] = len(w._patch_sel_btns)
            out["preview_patch_idx"] = w._preview_patch_idx
            out["n_patches"] = len(w._all_patches)
        except Exception:                                   # noqa: BLE001
            pass
        try:
            for splitter in w.findChildren(
                    __import__("PyQt5.QtWidgets", fromlist=["QSplitter"]).QSplitter):
                if splitter.isVisible() and splitter.count() >= 2:
                    out["splitter_sizes"] = splitter.sizes()
                    break
        except Exception:                                   # noqa: BLE001
            pass
        return out

    def sample(self):
        now = self.state()
        if now != self.last:
            emit("state.changed", state=now, previous=self.last)
            self.last = now


def main():
    global _LOG
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="/tmp/step1_patch_wobble_observe.jsonl")
    args = parser.parse_args()

    _LOG = open(args.out, "w", buffering=1)
    emit("observer.start", out=args.out, pid=os.getpid(),
         note="time is time.time(); the screen recorder stamps the same clock")

    import pyqtgraph as pg
    from PyQt5 import QtGui
    from PyQt5.QtWidgets import QApplication

    pg.setConfigOptions(antialias=True, imageAxisOrder="row-major")
    install_hooks()
    emit("observer.hooks_installed")

    # From here on this is the product's own entry, unchanged: the same
    # QApplication, the same palette, the same MainWindow.
    MainWindow = mod("ui.main_window").MainWindow
    start_gui_watchdog = mod("utils.gui_watchdog").start_gui_watchdog

    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    pal = QtGui.QPalette()
    for role, rgb in ((QtGui.QPalette.Window, (28, 28, 28)),
                      (QtGui.QPalette.WindowText, (220, 220, 220)),
                      (QtGui.QPalette.Base, (18, 18, 18)),
                      (QtGui.QPalette.AlternateBase, (38, 38, 38)),
                      (QtGui.QPalette.Text, (220, 220, 220)),
                      (QtGui.QPalette.Button, (48, 48, 48)),
                      (QtGui.QPalette.ButtonText, (220, 220, 220)),
                      (QtGui.QPalette.Highlight, (42, 130, 218)),
                      (QtGui.QPalette.HighlightedText, (0, 0, 0))):
        pal.setColor(role, QtGui.QColor(*rgb))
    app.setPalette(pal)
    start_gui_watchdog()

    win = MainWindow()
    win.show()
    emit("observer.window_shown", title=win.windowTitle())
    Sampler(win)
    code = app.exec_()
    emit("observer.exit", code=code)
    _LOG.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
