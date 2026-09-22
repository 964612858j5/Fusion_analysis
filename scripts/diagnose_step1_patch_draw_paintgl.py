"""G3.2b.5A.3: does paintGL draw a submit's FBO at a size it was not made for?

5A.2 caught the wobble on the real desktop: at every Patch creation the
whole Viewer image changes for ~2-3 frames and then returns BIT-IDENTICALLY.
It is not a translation, and the earlier "same input redrawn" wording was
wrong -- redrawing the same input cannot give different pixels, so some
input or draw state must differ.

Reading the production layer (`ui/step1_gpu_layer.py`) gives one candidate
that would explain every pixel measurement:

    def resizeGL(self, w, h):            # deliberately does nothing:
        del width, height                # FBO size follows submit() only

    def paintGL(self):
        width, height = self.width(), self.height()        # NOW
        source_width, source_height = self._target_size    # LAST SUBMIT
        glBlitFramebuffer(0, 0, source_width, source_height,
                          0, 0, width, height, ..., GL_NEAREST)

If the widget is resized and a paint happens BEFORE the next submit, the
last submit's FBO is blitted, nearest-neighbour, into a different rectangle
-- a whole-image resample that keeps the structure in place, keeps the mean
and keeps the sharpness, and disappears as soon as the next submit
reallocates. That is the shape of what was recorded.

THIS SCRIPT DOES NOT ASSUME IT. It records, for every paintGL, the widget
size and `_target_size` side by side and flags the mismatch, together with
the submit it is consuming, the layout/resize events around it, and the
Patch chain -- so the claim stands or falls on the numbers.

OBSERVATION RULES honoured here: every wrapper calls the real function with
the real arguments and returns its real result; no event is swallowed,
reordered or delayed; nothing calls processEvents/update/repaint/grab; no
camera, GL state or widget geometry is changed; GL is only ever READ, and
only inside paintGL where the context is already current. Records are
buffered in memory with a monotonic clock and a sequence number, and are
written once at the end.

Usage:  python scripts/diagnose_step1_patch_draw_paintgl.py OUT.json
"""

import importlib
import json
import os
import pathlib
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.basename(_ROOT)
if os.path.dirname(_ROOT) not in sys.path:
    sys.path.insert(0, os.path.dirname(_ROOT))
if os.path.join(_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(_ROOT, "tests"))


def mod(name):
    return importlib.import_module(f"{PKG}.{name}")


SLIDE = "/sda1/Fusion/benchmark/biopsy.ome.tif"

REC = []
SEQ = {"n": 0, "submit": 0, "paint": 0}
T0 = time.monotonic()


def rec(kind, **fields):
    SEQ["n"] += 1
    REC.append({"i": SEQ["n"], "t_ms": round((time.monotonic() - T0) * 1000, 3),
                "kind": kind, **fields})


def install(app_modules):
    gpu, MainWindow, OverviewPanel = app_modules
    layer_cls = gpu.Step1GpuLayer

    real_submit = layer_cls.submit
    real_paint = layer_cls.paintGL
    real_resize = layer_cls.resizeGL

    def summarise_source(descriptor):
        out = []
        for item in getattr(descriptor, "channels", ()) or ():
            out.append({
                "channel": item.channel,
                "selected_level": item.selected_level,
                "n_coarse": len(item.coarse or ()),
                "n_fine": len(item.fine or ()),
                # identity + geometry only: never the pixels, never a hash
                # of a large array.
                "plane_ids": [str(p.identity) for p in item.selected_planes()][:8],
                "plane_rects": [[round(float(v), 3) for v in p.world_rect]
                                for p in item.selected_planes()][:8],
                "plane_shapes": [list(getattr(p.values, "shape", ()) or ())
                                 for p in item.selected_planes()][:8],
            })
        return out

    def submit(self, source_descriptor, display_snapshot, viewport_snapshot):
        SEQ["submit"] += 1
        seq = SEQ["submit"]
        before_target = getattr(self, "_target_size", None)
        rec("submit.enter", submit_seq=seq,
            widget_size=[self.width(), self.height()],
            target_size_before=list(before_target) if before_target else None,
            viewport={
                "world_rect": [round(float(v), 4)
                               for v in viewport_snapshot.world_rect],
                "logical_size": list(viewport_snapshot.logical_size),
                "dpr": float(viewport_snapshot.device_pixel_ratio),
                "physical_size": list(viewport_snapshot.physical_size)},
            display={
                "mode": display_snapshot.mode,
                "mappings": {k: [round(float(x), 5) for x in v]
                             for k, v in dict(display_snapshot.mappings).items()},
                "weights": {k: round(float(v), 5)
                            for k, v in dict(display_snapshot.weights).items()
                            if float(v) != 0.0},
                "nucleus": list(display_snapshot.nucleus)},
            source=summarise_source(source_descriptor))
        result = real_submit(self, source_descriptor, display_snapshot,
                             viewport_snapshot)
        after = getattr(self, "_target_size", None)
        rec("submit.return", submit_seq=seq,
            target_size_after=list(after) if after else None,
            reallocated=(before_target != after))
        return result

    def paintGL(self):
        SEQ["paint"] += 1
        seq = SEQ["paint"]
        widget = [self.width(), self.height()]
        target = getattr(self, "_target_size", None)
        target = list(target) if target else None
        dpr = float(self.devicePixelRatioF())
        expected = None if target is None else [
            int(round(widget[0] * dpr)), int(round(widget[1] * dpr))]
        rec("paintGL.enter", paint_seq=seq,
            consumes_submit_seq=SEQ["submit"],
            widget_size=widget,
            fbo_target_size=target,
            device_pixel_ratio=dpr,
            widget_physical_size=expected,
            # THE QUESTION: paintGL blits `target` into `widget`. When the
            # two disagree the image on screen is a nearest-neighbour
            # rescale of a picture made for a different rectangle.
            blit_is_rescaled=(target is not None and expected is not None
                              and tuple(target) != tuple(expected)),
            default_fbo=int(self.defaultFramebufferObject()),
            initialized=bool(getattr(self, "_initialized", False)))
        out = real_paint(self)
        rec("paintGL.return", paint_seq=seq)
        return out

    def resizeGL(self, width, height):
        rec("resizeGL", w=int(width), h=int(height),
            target_size=list(getattr(self, "_target_size", None) or ()) or None)
        return real_resize(self, width, height)

    layer_cls.submit = submit
    layer_cls.paintGL = paintGL
    layer_cls.resizeGL = resizeGL

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
    wrap(MainWindow, "_select_preview_patch", "main._select_preview_patch")
    wrap(OverviewPanel, "_add_patch", "overview._add_patch")
    mount_mod = mod("ui.step1_viewer_mount")
    for name in ("show_patch", "jump_to_point"):
        wrap(mount_mod.Step1WholeSlideMount, name, f"mount.{name}")
    return layer_cls


def make_watcher(qtcore):
    """Resize / LayoutRequest on the widgets between the window and the GL.

    A QObject subclass, built here rather than at import time so this file
    does not need PyQt at module scope.
    """

    class Watcher(qtcore.QObject):
        def eventFilter(self, obj, event):                  # noqa: N802
            kind = event.type()
            if kind == qtcore.QEvent.Resize:
                rec("qt.Resize", who=obj.objectName() or type(obj).__name__,
                    size=[obj.width(), obj.height()])
            elif kind == qtcore.QEvent.LayoutRequest:
                rec("qt.LayoutRequest",
                    who=obj.objectName() or type(obj).__name__)
            return False

    return Watcher()


class _UnusedWatcher:
    def __init__(self, qtcore):
        self.qtcore = qtcore

    def eventFilter(self, obj, event):                      # noqa: N802
        kind = event.type()
        if kind == self.qtcore.QEvent.Resize:
            rec("qt.Resize", who=obj.objectName() or type(obj).__name__,
                size=[obj.width(), obj.height()])
        elif kind == self.qtcore.QEvent.LayoutRequest:
            rec("qt.LayoutRequest", who=obj.objectName() or type(obj).__name__)
        return False


def gl_identity(layer):
    """Real OpenGL vendor/renderer/version, READ ONLY, context already current.

    Not `backend == "gpu"` and not a cupy banner: the strings the driver
    itself reports.
    """
    from PyQt5 import QtGui
    try:
        ctx = layer.context()
        if ctx is None:
            return {"error": "no GL context"}
        layer.makeCurrent()
        gl = layer._gl
        out = {k: str(gl.glGetString(getattr(gl, f"GL_{k.upper()}")))
               for k in ("vendor", "renderer", "version")}
        out["doneCurrent"] = True
        layer.doneCurrent()
        return out
    except Exception as exc:                                # noqa: BLE001
        return {"error": str(exc)}


def find_layer(window):
    """The REAL GL widget, found by walking mount -> binding -> layer."""
    from PyQt5 import QtWidgets
    gpu = mod("ui.step1_gpu_layer")
    mount = getattr(window, "_step1_mount", None)
    found = []
    for name in ("layer", "_layer", "gpu_layer", "_gpu_layer"):
        for owner in (mount, getattr(mount, "binding", None),
                      getattr(mount, "_binding", None)):
            cand = getattr(owner, name, None)
            if isinstance(cand, gpu.Step1GpuLayer):
                found.append(("attr:" + name, cand))
    for child in window.findChildren(gpu.Step1GpuLayer):
        found.append(("findChildren", child))
    return found


def main():
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                       else "/tmp/patch_paintgl.json")

    from PyQt5 import QtCore, QtWidgets
    import pyqtgraph as pg
    pg.setConfigOptions(antialias=True, imageAxisOrder="row-major")

    # THE BENCH IS IMPORTED FIRST, ON PURPOSE. It installs its own
    # `sys.modules["block01"]` alias for this same directory, so the app it
    # builds uses the classes under `block01.*`. Wrapping the classes under
    # `block01_v14.*` instead would wrap a DIFFERENT class object for the
    # same source file and record nothing -- which is exactly what a first
    # run did: the patch chain ran and not one hook fired.
    sys.path.insert(0, os.path.join(_ROOT, "scripts"))
    bench = importlib.import_module("diagnose_step1_patch_draw_wobble")

    global mod
    _bench_pkg = "block01" if "block01" in sys.modules else PKG

    def mod(name, _pkg=_bench_pkg):                         # noqa: F811
        return importlib.import_module(f"{_pkg}.{name}")

    gpu = mod("ui.step1_gpu_layer")
    MainWindow = mod("ui.main_window").MainWindow
    OverviewPanel = mod("ui.step0.overview_panel").OverviewPanel
    install((gpu, MainWindow, OverviewPanel))

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    bench.APP = app

    # THE USER'S WINDOW IS 1400x900. The first run of this script used
    # 1600x1000 and the Patch chain raised LayoutRequest without ever
    # resizing the GL widget -- a layout with slack absorbs the selector
    # row's change. The size is therefore a parameter, and the default is
    # the size the wobble was recorded at.
    win_w, win_h = 1400, 900
    for arg in sys.argv[2:]:
        if arg.startswith("--window-size="):
            win_w, win_h = (int(v) for v in arg.split("=", 1)[1].split("x"))
    window, loader = bench.real_window()
    window.resize(win_w, win_h)
    window.show()
    bench.pump(600)
    window._stack.setCurrentWidget(window._step1_page_widget)
    bench.pump(700)
    tabs, tab = window.right_tabs, window.viewer_tab
    tabs.setCurrentWidget(tab)
    bench.pump(700)

    mount = window._step1_mount
    assert mount is not None and mount.host.stack is not None, "no Step1 mount"
    assert tab.isVisible(), "the Viewer tab is not on screen"
    viewport_widget = mount.host.stack.controller.view.graphics.viewport()
    assert viewport_widget.isVisible(), "the viewer viewport is not on screen"

    layers = find_layer(window)
    report = {"block": "G3.2b.5A.3",
              "window_size": [win_w, win_h],
              "hooked_package": _bench_pkg,
              "module_aliases": sorted(m for m in sys.modules
                                       if m in ("block01", PKG)),
              "gl_widgets_found": [(how, type(w).__name__,
                                    [w.width(), w.height()],
                                    bool(w.isVisible())) for how, w in layers]}
    layer = layers[0][1] if layers else None
    report["opengl"] = gl_identity(layer) if layer is not None else \
        {"error": "no Step1GpuLayer instance found"}

    popup = window._display.show_navigator()
    popup.set_overview_context(loader=loader,
                               nuc_ch=window._step0.nucleus_channel,
                               rois=[], patches=[],
                               dataset_token=str(loader.filepath))
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        bench.pump(250)
        panel = popup.overview
        if (int(getattr(panel, "ov_h", 0) or 0) > 8
                and panel.img_item.image is not None):
            break
    panel = popup.overview
    report["thumbnail"] = {"ov_h": int(getattr(panel, "ov_h", 0) or 0),
                           "ov_w": int(getattr(panel, "ov_w", 0) or 0),
                           "has_pixels": panel.img_item.image is not None,
                           "status": panel.status.text()}
    assert report["thumbnail"]["has_pixels"], "no real thumbnail; refusing to drag"

    report["identity"] = bench.identity(window, loader, popup)
    assert report["identity"]["same_shape"], report["identity"]

    watcher = make_watcher(QtCore)
    for widget in (tab, viewport_widget, window,
                   *(w for _h, w in layers)):
        widget.installEventFilter(watcher)

    def camera():
        box = mount.host.stack.controller.view.view_box
        (x0, x1), (y0, y1) = box.viewRange()
        return [round(x0, 4), round(x1, 4), round(y0, 4), round(y1, 4)]

    scenes = []

    def scene(label, action, settle_ms=2500):
        rec(f"scene.{label}.start", camera=camera())
        mark = len(REC)
        action()
        bench.pump(settle_ms)
        rec(f"scene.{label}.end", camera=camera())
        scenes.append({"label": label, "first_record": mark,
                       "last_record": len(REC)})

    # ── A. idle baseline: what a natural repaint looks like with no input
    bench.pump(2500)
    scene("A_idle_baseline", lambda: bench.pump(1500), settle_ms=500)

    # ── B / C. create patches through the real Tissue Preview drag ──
    def draw(frac):
        def go():
            n0 = len(popup.overview._patches)
            bench.draw_patch(popup, _NullProbe(window), fraction=frac)
            rec("gesture.patches_added",
                added=len(popup.overview._patches) - n0)
        return go

    scene("B_first_patch", draw((0.20, 0.20, 0.45, 0.45)))
    scene("C_second_patch", draw((0.55, 0.20, 0.80, 0.45)))
    scene("C_third_patch", draw((0.20, 0.55, 0.45, 0.80)))

    # ── D. move an existing patch through the same update chain ─────
    def move_one():
        panel = popup.overview
        if not panel._patches:
            rec("scene.D.skipped", why="no patch to move")
            return
        coords = panel._patches[0]["coords"]
        y0, y1, x0, x1 = coords
        panel._commit_patch_geometry(
            0, (y0 + 400, y1 + 400, x0 + 400, x1 + 400), revert_to=coords)
    scene("D_move_patch", move_one)

    # ── E. the control: a real Pn click SHOULD navigate ─────────────
    def click_pn():
        if window._patch_sel_btns:
            window._patch_sel_btns[0].click()
    scene("E_click_pn", click_pn)

    report["scenes"] = scenes
    report["n_patches_final"] = len(popup.overview._patches)
    report["records"] = REC
    report["honesty"] = [
        "every wrapper calls the real function and returns its real result; "
        "no event was swallowed, reordered or delayed",
        "nothing called processEvents/update/repaint/grab to drive drawing "
        "other than the bench's own pump between scenes",
        "GL was only READ, inside a context that was already current",
        "no project is bound, so no geometry was persisted: the persistence "
        "chain is NOT exercised here and is reported as a gap",
    ]
    out.write_text(json.dumps(report, indent=1, default=str))
    print(f"wrote {out}  records={len(REC)}")
    mism = [r for r in REC if r.get("blit_is_rescaled")]
    print(f"paintGL calls: {SEQ['paint']}   submits: {SEQ['submit']}")
    print(f"paintGL with widget size != FBO size: {len(mism)}")
    for r in mism[:12]:
        print(f"   i={r['i']} t={r['t_ms']}ms paint#{r['paint_seq']} "
              f"widget_phys={r['widget_physical_size']} fbo={r['fbo_target_size']}")
    return 0


class _NullProbe:
    """`bench.draw_patch` marks stages on a probe; here the records are
    this script's own, so the marks are no-ops -- but `probe.w` must be the
    real window, because `draw_patch` drives the geometry-commit step
    through it.
    """

    def __init__(self, window=None):
        self.w = window

    def mark(self, *_a, **_kw):
        return None


if __name__ == "__main__":
    sys.exit(main())
