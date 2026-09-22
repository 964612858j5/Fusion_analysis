"""G3.2b.5A.4: on a real Patch creation, what changes FIRST, and who did it?

One question only. 5A.3 showed that the bench's Patch chain raised
LayoutRequest and nothing else -- no resize, no submit, no paintGL -- while
the desktop recording shows a real repaint. 5A.3 also proved, separately,
that a widget resize before the next submit makes `paintGL` stretch the
previous submit's FBO; that is a REAL but SEPARATE finding whose relevance
to this wobble is NOT established, and it is not treated as a cause here.

So this run keeps the real window relationships (the Tissue Preview is the
product's own independent top-level window), keeps the FULL persistence
path (a temp project is bound, so geometry is really written), drives the
drag automatically, and records the FIRST display-state change after the
gesture together with the PYTHON STACK that produced it.

"Display state" means, in order of precedence, whichever happens first:
    resizeGL | Step1GpuLayer.submit | paintGL | GL-widget geometry change |
    ViewBox range change | a Qt Resize on the viewer subtree

Observation rules (unchanged from 5A.3): every wrapper calls the real
function with the real arguments and returns its real result; nothing is
swallowed, reordered or delayed; nothing calls processEvents/update/
repaint/grab to drive drawing; GL is only read where the context is already
current; records are buffered with a monotonic clock and written once.

Usage:  python scripts/diagnose_step1_patch_first_change.py OUT.json
"""

import importlib
import json
import os
import pathlib
import shutil
import sys
import time
import traceback

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.basename(_ROOT)
for extra in (os.path.dirname(_ROOT), os.path.join(_ROOT, "scripts"),
              os.path.join(_ROOT, "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

TMP_PROJECT = pathlib.Path("/tmp/g325a4_temp_project")

REC = []
SEQ = {"n": 0, "submit": 0, "paint": 0}
T0 = time.monotonic()
ARMED = {"on": False, "label": None, "first": None}


def rec(kind, **fields):
    SEQ["n"] += 1
    row = {"i": SEQ["n"], "t_ms": round((time.monotonic() - T0) * 1000, 3),
           "kind": kind, **fields}
    REC.append(row)
    return row


def product_stack():
    """Who called us, in product frames -- the answer to 'who triggered it'."""
    out = []
    for frame in traceback.extract_stack()[:-1]:
        name = frame.filename
        if "/block01" not in name:
            continue
        if "/scripts/" in name:          # this script's own wrappers
            continue
        out.append(f"{os.path.basename(name)}:{frame.lineno} {frame.name}")
    return out[-14:]


def note_first(kind, row):
    """The FIRST display-state change after the gesture was armed."""
    if not ARMED["on"] or ARMED["first"] is not None:
        return
    ARMED["first"] = {"scene": ARMED["label"], "kind": kind,
                      "record_index": row["i"], "t_ms": row["t_ms"],
                      "stack": product_stack()}
    rec("FIRST_DISPLAY_CHANGE", of=kind, scene=ARMED["label"],
        stack=ARMED["first"]["stack"])


def mod(name, pkg=None):
    return importlib.import_module(f"{pkg or PKG}.{name}")


def install(gpu, MainWindow, OverviewPanel, mount_cls, Step0Page):
    layer_cls = gpu.Step1GpuLayer
    real_submit, real_paint = layer_cls.submit, layer_cls.paintGL
    real_resize = layer_cls.resizeGL

    def submit(self, source, display, viewport):
        SEQ["submit"] += 1
        before = getattr(self, "_target_size", None)
        row = rec("submit.enter", submit_seq=SEQ["submit"],
                  widget_size=[self.width(), self.height()],
                  target_size_before=list(before) if before else None,
                  viewport_logical=list(viewport.logical_size),
                  viewport_physical=list(viewport.physical_size),
                  world_rect=[round(float(v), 4) for v in viewport.world_rect],
                  levels=[(c.channel, c.selected_level)
                          for c in (source.channels or ())])
        note_first("submit", row)
        out = real_submit(self, source, display, viewport)
        after = getattr(self, "_target_size", None)
        rec("submit.return", submit_seq=SEQ["submit"],
            target_size_after=list(after) if after else None,
            reallocated=(before != after))
        return out

    def paintGL(self):
        SEQ["paint"] += 1
        widget = [self.width(), self.height()]
        target = getattr(self, "_target_size", None)
        target = list(target) if target else None
        dpr = float(self.devicePixelRatioF())
        phys = None if target is None else [int(round(widget[0] * dpr)),
                                            int(round(widget[1] * dpr))]
        row = rec("paintGL.enter", paint_seq=SEQ["paint"],
                  consumes_submit_seq=SEQ["submit"], widget_size=widget,
                  fbo_target_size=target, widget_physical_size=phys,
                  blit_is_rescaled=(target is not None and phys is not None
                                    and tuple(target) != tuple(phys)))
        note_first("paintGL", row)
        out = real_paint(self)
        rec("paintGL.return", paint_seq=SEQ["paint"])
        return out

    def resizeGL(self, width, height):
        row = rec("resizeGL", w=int(width), h=int(height),
                  target_size=list(getattr(self, "_target_size", None) or ())
                  or None)
        note_first("resizeGL", row)
        return real_resize(self, width, height)

    layer_cls.submit, layer_cls.paintGL, layer_cls.resizeGL = (
        submit, paintGL, resizeGL)

    def wrap(owner, name, kind):
        real = getattr(owner, name, None)
        if real is None:
            return

        def called(self, *a, **kw):
            rec(f"{kind}.enter")
            try:
                return real(self, *a, **kw)
            finally:
                rec(f"{kind}.return")
        setattr(owner, name, called)

    wrap(MainWindow, "_on_patches", "main._on_patches")
    wrap(MainWindow, "_rebuild_patch_buttons", "main._rebuild_patch_buttons")
    wrap(MainWindow, "_on_step0_geometry_committed", "main._geometry_committed")
    wrap(MainWindow, "_show_active_roi_preview", "main._show_active_roi_preview")
    wrap(MainWindow, "_refresh_patch_preview", "main._refresh_patch_preview")
    wrap(Step0Page, "_persist_geometry_edit", "step0._persist_submit")
    wrap(Step0Page, "_on_geometry_persist_published", "step0._persist_published")
    wrap(OverviewPanel, "_add_patch", "overview._add_patch")
    for name in ("show_patch", "jump_to_point"):
        wrap(mount_cls, name, f"mount.{name}")
    return layer_cls


def make_watcher(qtcore, layer):
    class Watcher(qtcore.QObject):
        def eventFilter(self, obj, event):                  # noqa: N802
            kind = event.type()
            who = obj.objectName() or type(obj).__name__
            if kind == qtcore.QEvent.Resize:
                row = rec("qt.Resize", who=who, size=[obj.width(), obj.height()],
                          is_gl_layer=(obj is layer))
                note_first("qt.Resize", row)
            elif kind == qtcore.QEvent.LayoutRequest:
                rec("qt.LayoutRequest", who=who)
            return False
    return Watcher()


def temp_project():
    """A throwaway project the geometry worker may really write into."""
    if TMP_PROJECT.exists():
        shutil.rmtree(TMP_PROJECT)
    (TMP_PROJECT / "step0").mkdir(parents=True)
    return TMP_PROJECT


def main():
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                       else "/tmp/patch_first_change.json")
    from PyQt5 import QtCore, QtWidgets
    import pyqtgraph as pg
    pg.setConfigOptions(antialias=True, imageAxisOrder="row-major")

    # The 5A.1 bench first: it owns the `block01` alias the app is built
    # from, so the hooks must go on THAT package (5A.3 learned this the
    # hard way -- hooks on `block01_v14` recorded nothing).
    bench = importlib.import_module("diagnose_step1_patch_draw_wobble")
    pkg = "block01" if "block01" in sys.modules else PKG
    gpu = mod("ui.step1_gpu_layer", pkg)
    MainWindow = mod("ui.main_window", pkg).MainWindow
    OverviewPanel = mod("ui.step0.overview_panel", pkg).OverviewPanel
    Step0Page = mod("ui.step0.step0_page", pkg).Step0Page
    mount_cls = mod("ui.step1_viewer_mount", pkg).Step1WholeSlideMount
    install(gpu, MainWindow, OverviewPanel, mount_cls, Step0Page)

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    bench.APP = app

    # THE REAL WORKER MUST PUBLISH, not the bench. 5A.1's bench drives
    # `_on_geometry_persist_published` itself because it had no project to
    # write into; here a temp project IS bound, so that short-cut is
    # switched off and the product's own persistence runs to disk. A first
    # run with the short-cut still in place wrote 0 files -- the driven
    # call simply pre-empted the worker.
    bench.commit_geometry = lambda *_a, **_kw: None

    project = temp_project()
    window, loader = bench.real_window()
    window.resize(1400, 900)
    window.show()
    bench.pump(600)
    window._stack.setCurrentWidget(window._step1_page_widget)
    bench.pump(700)
    window.right_tabs.setCurrentWidget(window.viewer_tab)
    bench.pump(700)

    page = window._step0
    # THE FULL PERSISTENCE PATH, pointed at the temp project: the worker
    # builds its task from `_roi_context["step_dirs"]["step0"]`, so with a
    # real directory there the geometry is really written -- and written
    # HERE, never into the user's project.
    ctx = dict(getattr(page, "_roi_context", None) or {})
    ctx.setdefault("step_dirs", {})
    ctx["step_dirs"] = dict(ctx["step_dirs"])
    ctx["step_dirs"]["step0"] = str(project / "step0")
    page._roi_context = ctx
    window.step0_output = dict(getattr(window, "step0_output", None) or {})
    window.step0_output["step0_manifest_path"] = str(
        (project / "step0" / "step0_roi_result.json").resolve())

    mount = window._step1_mount
    layer = None
    for cand in window.findChildren(gpu.Step1GpuLayer):
        layer = cand
        break
    assert layer is not None, "no Step1GpuLayer instance"
    layer.makeCurrent()
    gl_info = {k: str(layer._gl.glGetString(getattr(layer._gl, f"GL_{k.upper()}")))
               for k in ("vendor", "renderer", "version")}
    layer.doneCurrent()

    popup = window._display.show_navigator()
    popup.set_overview_context(loader=loader, nuc_ch=page.nucleus_channel,
                               rois=[], patches=[],
                               dataset_token=str(loader.filepath))
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        bench.pump(250)
        panel = popup.overview
        if int(getattr(panel, "ov_h", 0) or 0) > 8 and panel.img_item.image is not None:
            break
    panel = popup.overview
    assert panel.img_item.image is not None, "no real thumbnail; refusing to drag"

    watcher = make_watcher(QtCore, layer)
    for widget in (window, window.viewer_tab, layer,
                   mount.host.stack.controller.view.graphics.viewport()):
        widget.installEventFilter(watcher)

    report = {
        "block": "G3.2b.5A.4",
        "question": "on a real Patch creation, what display state changes "
                    "FIRST, and who triggered it?",
        "opengl": gl_info,
        "gl_layer": {"size": [layer.width(), layer.height()],
                     "visible": bool(layer.isVisible()),
                     "is_top_level": bool(layer.isWindow())},
        "popup_is_independent_window": bool(popup.isWindow()),
        "temp_project": str(project),
        "identity": bench.identity(window, loader, popup),
        "thumbnail_grid": [int(panel.ov_h), int(panel.ov_w)],
        "window_size": [window.width(), window.height()],
        "firsts": [],
    }

    def gesture(label, frac):
        ARMED.update(on=True, label=label, first=None)
        rec(f"scene.{label}.start")
        n0 = len(panel._patches)
        bench.draw_patch(popup, _Probe(window), fraction=frac)
        # the real worker writes on its own thread and publishes back on
        # the GUI thread; give it room rather than driving it.
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            bench.pump(300)
            if any(p.is_file() for p in project.rglob("*")):
                break
        bench.pump(2000)
        rec(f"scene.{label}.end", patches_added=len(panel._patches) - n0)
        ARMED["on"] = False
        report["firsts"].append({"scene": label,
                                 "patches_added": len(panel._patches) - n0,
                                 "first_change": ARMED["first"]})

    bench.pump(2500)
    gesture("patch_1", (0.20, 0.20, 0.45, 0.45))
    gesture("patch_2", (0.55, 0.20, 0.80, 0.45))
    gesture("patch_3", (0.20, 0.55, 0.45, 0.80))

    written = sorted(str(p.relative_to(project))
                     for p in project.rglob("*") if p.is_file())
    report["temp_project_files_written"] = written
    report["persistence_reached_disk"] = bool(written)
    report["records"] = REC
    report["honesty"] = [
        "the FBO/widget size mismatch proved in 5A.3 is a SEPARATE finding; "
        "its relevance to this wobble is not established and it is not "
        "treated as a cause here",
        "co-occurrence in time is a lead, not a proof of causation",
        "every wrapper calls the real function and returns its real result",
    ]
    out.write_text(json.dumps(report, indent=1, default=str))
    print(f"wrote {out}   records={len(REC)}")
    print(f"OpenGL: {gl_info['renderer']}")
    print(f"popup independent top-level: {report['popup_is_independent_window']}")
    print(f"persistence reached disk: {report['persistence_reached_disk']} "
          f"({len(written)} files)")
    for item in report["firsts"]:
        print(f"\n--- {item['scene']}  patches_added={item['patches_added']}")
        first = item["first_change"]
        if first is None:
            print("    NO display-state change was recorded at all")
            continue
        print(f"    FIRST change: {first['kind']} at t={first['t_ms']}ms")
        for line in first["stack"]:
            print(f"      {line}")
    return 0


class _Probe:
    def __init__(self, window):
        self.w = window

    def mark(self, *_a, **_kw):
        return None


if __name__ == "__main__":
    sys.exit(main())
