"""
Block A0-1 of v16 (docs/v16_A0_application.md): where does the viewer move
when the user switches between Step0, Step1 and Step3? Read-only diagnosis,
not imported by the product.

Instrumentation lives ONLY in this script's process: product methods are
wrapped so that the real method runs unchanged and a record of the camera /
geometry before and after is appended to a JSONL log. Nothing in the product
is modified and no product behaviour depends on the wrappers.

    python scripts/diagnose_v16_a0_camera.py copy-project [--dest D]
    python scripts/diagnose_v16_a0_camera.py offscreen   [--dest D] [--loops N]
    python scripts/diagnose_v16_a0_camera.py summarize   LOG.jsonl

`copy-project` copies ~/fusion_data/test1 (project files + ONE workspace)
into D and rewrites every absolute path to the copy, so the product reads and
writes the copy; the slide itself stays at its real path and is only read.
Before/after every run the original project's file tree (paths, sizes,
mtimes) is fingerprinted, and a change is reported as a failure.

`offscreen` drives the REAL window through the public step entries
(`_go_to_step0/1/3`, i.e. `setCurrentIndex` then `_set_step_active`), samples
the cameras right after the call and after 0 / 50 / 500 ms of event loop, and
runs the transitions of the plan plus a drift loop.
"""

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
import time

HOME = os.path.expanduser("~")
ORIG_PROJECT = os.path.join(HOME, "fusion_data", "test1")
WORKSPACE = "full_wsi_20260927_121444_6bad"
DEFAULT_DEST = os.path.join(HOME, "fusionflux", "bench_a0", "test1_copy")
TEXT_SUFFIXES = (".json", ".zattrs", ".zarray", ".zgroup", ".csv", ".log", ".txt")


# ── the project copy ─────────────────────────────────────────────────────


def tree_fingerprint(root):
    """sha256 over (relative path, size, mtime_ns) of every file under root."""
    h = hashlib.sha256()
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            h.update(f"{os.path.relpath(p, root)}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
            n += 1
    return {"files": n, "sha256": h.hexdigest()}


def cmd_copy_project(args):
    dest = os.path.abspath(args.dest)
    if os.path.realpath(dest).startswith(os.path.realpath(os.path.join(HOME, "fusion_data"))):
        raise SystemExit("the copy must not live under ~/fusion_data")
    before = tree_fingerprint(ORIG_PROJECT)
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.makedirs(os.path.join(dest, "rois"))
    for name in os.listdir(ORIG_PROJECT):
        src = os.path.join(ORIG_PROJECT, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(dest, name))
    shutil.copytree(os.path.join(ORIG_PROJECT, "rois", WORKSPACE), os.path.join(dest, "rois", WORKSPACE))
    rewritten = []
    old = ORIG_PROJECT.encode()
    for dirpath, _dirs, files in os.walk(dest):
        for name in files:
            if not (name.endswith(TEXT_SUFFIXES) or name.startswith(".z")):
                continue
            p = os.path.join(dirpath, name)
            data = open(p, "rb").read()
            if old in data:
                open(p, "wb").write(data.replace(old, dest.encode()))
                rewritten.append(os.path.relpath(p, dest))
    left = []
    for dirpath, _dirs, files in os.walk(dest):
        for name in files:
            p = os.path.join(dirpath, name)
            if os.path.getsize(p) < 50_000_000 and old in open(p, "rb").read():
                left.append(os.path.relpath(p, dest))
    after = tree_fingerprint(ORIG_PROJECT)
    info = {"dest": dest, "workspace": WORKSPACE, "rewritten": rewritten,
            "still_mentioning_original": left, "original_unchanged": before == after,
            "original_fingerprint": before}
    with open(os.path.join(dest, "A0_COPY_INFO.json"), "w") as fh:
        json.dump(info, fh, indent=1)
    print(json.dumps({k: v for k, v in info.items() if k != "rewritten"}, indent=1))
    print(f"rewritten {len(rewritten)} files")
    return 0 if before == after and not left else 1


# ── observation ──────────────────────────────────────────────────────────


class Log:
    def __init__(self, path):
        self.path = path
        self.fh = open(path, "w")
        self.t0 = time.perf_counter()
        self.depth = 0

    def write(self, kind, **fields):
        rec = {"t_ms": round((time.perf_counter() - self.t0) * 1000.0, 2), "kind": kind,
               "depth": self.depth, **fields}
        self.fh.write(json.dumps(rec, default=_jsonable) + "\n")
        self.fh.flush()


def _jsonable(v):
    if isinstance(v, float) and not math.isfinite(v):
        return str(v)
    try:
        return float(v)
    except Exception:  # noqa: BLE001
        return repr(v)


def _geom(widget):
    if widget is None:
        return None
    try:
        g = widget.geometry()
        return [g.x(), g.y(), g.width(), g.height()]
    except Exception:  # noqa: BLE001
        return None


def view_state(view, layer=None, controller=None):
    """Camera and geometry of one ExploreView (+ its GPU layer if any)."""
    if view is None:
        return None
    vb = getattr(view, "view_box", None)
    graphics = getattr(view, "graphics", None)
    out = {"visible": bool(view.isVisible())}
    try:
        (x0, x1), (y0, y1) = vb.viewRange()
        px, py = vb.viewPixelSize()
        out.update(cx=(x0 + x1) / 2.0, cy=(y0 + y1) / 2.0,
                   scale_x=1.0 / px, scale_y=1.0 / py,
                   world=[x0, x1, y0, y1])
        tr = vb.targetRect()
        out["target"] = [tr.x(), tr.y(), tr.width(), tr.height()]
        g = vb.geometry()
        out["viewbox"] = [g.x(), g.y(), g.width(), g.height()]
    except Exception as exc:  # noqa: BLE001
        out["error"] = repr(exc)
    if graphics is not None:
        vp = graphics.viewport()
        out["viewport"] = [vp.width(), vp.height()]
        try:
            out["dpr"] = float(graphics.devicePixelRatioF())
        except Exception:  # noqa: BLE001
            pass
    if layer is not None:
        out["gpu_layer"] = _geom(layer)
        out["gpu_target_size"] = list(getattr(layer, "_target_size", None) or []) or None
        try:
            out["gpu_dpr"] = float(layer.devicePixelRatioF())
        except Exception:  # noqa: BLE001
            pass
    if controller is not None:
        for name in ("level", "_overview_level", "_floor_level"):
            try:
                v = getattr(controller, name)
                out[name.lstrip("_")] = v() if callable(v) else v
            except Exception:  # noqa: BLE001
                pass
    return out


def window_state(w):
    """Every viewer's camera, the shared camera and the page on screen."""
    st = {"current_step": getattr(w, "_current_step", None),
          "stack_index": w._stack.currentIndex()}
    shot = getattr(w, "_shared_camera", None)
    st["shared"] = None if shot is None else [shot.cx, shot.cy, shot.scale, shot.origin]
    tab = getattr(w._step0, "_explore_tab", None)
    stack0 = getattr(tab, "stack", None) if tab else None
    st["step0"] = view_state(getattr(stack0, "view", None), controller=getattr(stack0, "controller", None))
    for key, attr in (("step1", "_step1_mount"), ("step3", "_step3_mount")):
        mount = w.__dict__.get(attr)
        stack = getattr(getattr(mount, "host", None), "stack", None)
        st[key] = view_state(getattr(stack, "view", None), getattr(mount, "gpu_layer", None),
                             getattr(stack, "controller", None))
    return st


def camera_of(entry):
    if not entry or "cx" not in entry:
        return None
    return (entry["cx"], entry["cy"], entry["scale_x"])


def install_hooks(log, MainWindow, extra=()):
    """Wrap the camera path: record the call, its arguments, the window
    state before and after. The real method always runs, unchanged."""
    import pyqtgraph as pg

    from block01.ui import step1_viewer_mount as mm
    from block01.ui.step0 import step0_page as sp
    from block01.viewer import explore_view as ev

    win_ref = {}

    def wrap(owner, name, tag, state=True, args_repr=True):
        orig = getattr(owner, name, None)
        if orig is None or getattr(orig, "_a0_wrapped", False):
            return

        def wrapper(self, *a, **k):
            w = win_ref.get("w")
            fields = {"call": tag}
            if args_repr:
                fields["args"] = [repr(x)[:120] for x in a]
            if state and w is not None:
                fields["before"] = window_state(w)
            log.write("enter", **fields)
            log.depth += 1
            try:
                result = orig(self, *a, **k)
            finally:
                log.depth -= 1
            out = {"call": tag, "result": repr(result)[:120]}
            if state and w is not None:
                out["after"] = window_state(w)
            log.write("exit", **out)
            return result

        wrapper._a0_wrapped = True
        setattr(owner, name, wrapper)

    for name in ("_set_step_active", "_capture_camera_of", "_apply_shared_camera_to",
                 "_step1_whole_slide_step_changed", "_mount_channels_dock",
                 "_match_step1_bottom_bar", "_hold_step1_channel_floor",
                 "_go_to_step0", "_go_to_step1", "_go_to_step3"):
        wrap(MainWindow, name, f"MainWindow.{name}")
    wrap(MainWindow, "_remember_camera", "MainWindow._remember_camera", state=False)
    for name in ("apply_camera", "open", "source_changed", "jump_to_point", "show_patch"):
        wrap(mm.Step1WholeSlideMount, name, f"Mount.{name}")
    wrap(sp.Step0Page, "apply_camera_snapshot", "Step0Page.apply_camera_snapshot")
    for name in ("jump_to", "set_view_rect_l0"):
        wrap(ev.ExploreController, name, f"ExploreController.{name}", state=False)
    for name in ("resizeEvent", "updateViewRange", "setRange"):
        wrap(pg.ViewBox, name, f"ViewBox.{name}", state=False)
    for owner, name, tag in extra:
        wrap(owner, name, tag)
    return win_ref


# ── offscreen run ────────────────────────────────────────────────────────


def pump(ms):
    from PyQt5 import QtTest, QtWidgets
    QtWidgets.QApplication.processEvents()
    if ms:
        QtTest.QTest.qWait(ms)
    QtWidgets.QApplication.processEvents()


def neutralise_modals(log):
    """An offscreen run must never block on a modal dialog: record it, return
    the default answer. Diagnosis-only, in this process."""
    from PyQt5 import QtWidgets
    for name in ("warning", "information", "critical", "question"):
        def fake(*a, _n=name, **k):
            log.write("modal", which=_n, text=[repr(x)[:200] for x in a[1:3]])
            return QtWidgets.QMessageBox.No if _n == "question" else QtWidgets.QMessageBox.Ok
        setattr(QtWidgets.QMessageBox, name, staticmethod(fake))


def open_copy(w, dest, log):
    """The user's path: Step0 Load (slide + project), then Step1's Load Step0."""
    ws = os.path.join(dest, "rois", WORKSPACE)
    manifest = json.load(open(os.path.join(ws, "roi_manifest.json")))
    slide = manifest["source_ome"]
    page = w._step0
    page._ome_path_edit.setText(slide)
    page._out_path_edit.setText(dest)
    if os.path.exists(os.path.join(dest, "panel.csv")):
        page._panel_csv_edit.setText(os.path.join(dest, "panel.csv"))
    log.write("phase", name="step0.load", slide=slide, project=dest)
    page._reload_from_paths()
    pump(2000)
    w.step0_output = {"step0_manifest_path": os.path.join(ws, "step0", "step0_roi_result.json"),
                      "handoff_schema_version": 2}
    log.write("phase", name="step1.load_step0_result")
    ok = w._load_step0_roi_result(auto=True)
    log.write("phase", name="step1.load_step0_result.done", ok=bool(ok))
    pump(500)
    return ok


def sample(log, w, label):
    for ms in (None, 0, 50, 500):
        if ms is not None:
            pump(ms)
        log.write("sample", label=label, after_ms=ms, state=window_state(w))


def go(w, step):
    getattr(w, f"_go_to_step{step}")()


def cmd_offscreen(args):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    dest = os.path.abspath(args.dest)
    if not os.path.exists(os.path.join(dest, "A0_COPY_INFO.json")):
        raise SystemExit("run copy-project first")
    before = tree_fingerprint(ORIG_PROJECT)
    out_dir = args.out or os.path.join(os.path.dirname(dest), "camera_logs")
    os.makedirs(out_dir, exist_ok=True)
    log = Log(os.path.join(out_dir, f"offscreen_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"))

    import pyqtgraph as pg
    from PyQt5 import QtWidgets
    pg.setConfigOptions(antialias=True, imageAxisOrder="row-major")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setStyle("Fusion")
    from block01.ui.main_window import MainWindow

    neutralise_modals(log)
    win_ref = install_hooks(log, MainWindow)
    w = MainWindow()
    win_ref["w"] = w
    w.resize(args.width, args.height)
    w.show()
    pump(200)
    ok = open_copy(w, dest, log)
    log.write("phase", name="opened", ok=bool(ok), state=window_state(w))

    # A non-trivial camera in Step0 first: zoom into tissue off-centre.
    go(w, 0)
    pump(1000)
    page = w._step0
    vb = page._full_image_view_box()
    if vb is not None:
        page._apply_full_image_camera(9000.0, 7000.0, 0.37)
        pump(300)
    sample(log, w, "start step0")

    sequence = [(0, 1), (1, 3), (3, 1), (1, 0), (0, 3), (3, 0)]
    for a, b in sequence:
        log.write("phase", name=f"transition {a}->{b}")
        go(w, b)
        sample(log, w, f"{a}->{b}")
    for i in range(args.loops):
        for a, b in ((0, 1), (1, 3), (3, 1), (1, 0)):
            go(w, b)
            pump(20)
        log.write("loop", i=i, state=window_state(w))
    pump(300)
    # The same handoff loaded again while Step1 is on screen: a source rebind
    # would only happen if the source moved (C4).
    go(w, 1)
    pump(300)
    log.write("phase", name="reload handoff (same source)")
    w.step0_output = {"step0_manifest_path": os.path.join(dest, "rois", WORKSPACE, "step0",
                                                          "step0_roi_result.json"),
                      "handoff_schema_version": 2}
    w._load_step0_roi_result(auto=True)
    sample(log, w, "reload")
    log.write("phase", name="end", state=window_state(w))
    after = tree_fingerprint(ORIG_PROJECT)
    log.write("guard", original_unchanged=before == after)
    print(f"log: {log.path}; original project unchanged: {before == after}")
    try:
        w._display.shutdown("a0")
    except Exception:  # noqa: BLE001
        pass
    w.hide()
    return 0 if before == after else 1


# ── real-GL picture check (C1) ───────────────────────────────────────────


def qimage_gray(img):
    import numpy as np
    from PyQt5 import QtGui
    img = img.convertToFormat(QtGui.QImage.Format_RGB888)
    w, h, bpl = img.width(), img.height(), img.bytesPerLine()
    ptr = img.bits()
    ptr.setsize(h * bpl)
    a = np.frombuffer(ptr, np.uint8).reshape(h, bpl)[:, :w * 3].reshape(h, w, 3)
    return a.astype(np.float32).mean(axis=2)


def world_to_screen(e, wx, wy, content):
    """Screen position of a world point: `content` = (x, y, w, h) the world
    rect is drawn into (the ViewBox for the CPU picture; the whole GPU layer
    for what the GPU layer actually does)."""
    x0, x1, y0, y1 = e["world"]
    cx, cy, cw, ch = content
    return cx + (wx - x0) / (x1 - x0) * cw, cy + (wy - y0) / (y1 - y0) * ch


def cmd_gl_image(args):
    """One process, few GL contexts: Step0 then Step1 at the SAME camera, one
    raw channel (DAPI) in white, the same display window, no fusion, no mask;
    grab both, then measure translation on patches around five world
    anchors. Scale comes from the transform numbers, not from the images."""
    import numpy as np
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    dest = os.path.abspath(args.dest)
    before = tree_fingerprint(ORIG_PROJECT)
    out_dir = args.out or os.path.join(os.path.dirname(dest), "camera_gl_image")
    os.makedirs(out_dir, exist_ok=True)
    log = Log(os.path.join(out_dir, f"gl_image_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"))
    import pyqtgraph as pg
    from PyQt5 import QtWidgets
    pg.setConfigOptions(antialias=True, imageAxisOrder="row-major")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setStyle("Fusion")
    from block01.ui.main_window import MainWindow, STEP1_PREVIEW_OVERLAY
    from skimage.registration import phase_cross_correlation

    neutralise_modals(log)
    w = MainWindow()
    w.resize(args.width, args.height)
    w.show()
    pump(200)
    open_copy(w, dest, log)
    state = w._display.state
    ch = args.channel
    go(w, 0)
    pump(800)
    state.set_selected_channel(ch, origin="a0")
    state.set_color(ch, "#ffffff", origin="a0")
    for nuc in (False, True):
        state.set_mapping(ch, args.lo, args.hi, 1.0, nucleus=nuc, origin="a0")
    cam = (args.cx, args.cy, args.scale)
    w._step0._apply_full_image_camera(*cam)
    pump(args.settle_ms)
    s0 = window_state(w)["step0"]
    tab = w._step0._explore_tab
    img0 = qimage_gray(tab.stack.view.graphics.viewport().grab().toImage())

    go(w, 1)
    pump(1000)
    w.set_preview_mode(STEP1_PREVIEW_OVERLAY, force=True)
    for name in list(state.channel_order() or []):
        state.set_display_visible(name, name == ch, origin="a0")
    state.set_selected_channel(ch, origin="a0")
    # The SAME camera, without the C2/C3 effects: solved for this ViewBox and
    # set through the controller's exact (no-refit) entry.
    mount = w._step1_mount
    vb = mount.host.stack.view.view_box
    ww, hh = vb.width() / cam[2], vb.height() / cam[2]
    mount.host.stack.controller.set_view_rect_l0(cam[0] - ww / 2, cam[1] - hh / 2, ww, hh)
    pump(args.settle_ms)
    s1 = window_state(w)["step1"]
    layer = mount.gpu_layer
    img1_gpu = qimage_gray(layer.grabFramebuffer()) if layer is not None else None
    log.write("states", step0=s0, step1=s1)

    res = {"camera": cam, "channel": ch, "window": [args.lo, args.hi],
           "step0": {k: s0.get(k) for k in ("cx", "cy", "scale_x", "scale_y", "viewbox", "viewport", "world")},
           "step1": {k: s1.get(k) for k in ("cx", "cy", "scale_x", "scale_y", "viewbox", "viewport",
                                             "world", "gpu_layer", "gpu_target_size")}}
    try:
        from PIL import Image
        Image.fromarray(img0.astype(np.uint8)).save(os.path.join(out_dir, "step0.png"))
        if img1_gpu is not None:
            Image.fromarray(img1_gpu.astype(np.uint8)).save(os.path.join(out_dir, "step1_gpu.png"))
    except Exception as exc:  # noqa: BLE001
        res["png_error"] = repr(exc)
    if img1_gpu is not None:
        vb1 = s1["viewbox"]
        gl_geom = s1["gpu_layer"]
        content1_intended = (vb1[0], vb1[1], vb1[2], vb1[3])
        content1_gpu = (0, 0, gl_geom[2], gl_geom[3])
        x0, x1, y0, y1 = s1["world"]
        half = args.patch // 2
        anchors = {"centre": (0.5, 0.5), "mid-left": (0.25, 0.5), "mid-right": (0.75, 0.5),
                   "top-left": (0.15, 0.15), "top-right": (0.85, 0.15),
                   "bottom-left": (0.15, 0.85), "bottom-right": (0.85, 0.85)}
        rows = {}
        for name, (fx, fy) in anchors.items():
            wx, wy = x0 + fx * (x1 - x0), y0 + fy * (y1 - y0)
            p0 = world_to_screen(s0, wx, wy, tuple(s0["viewbox"]))
            p1 = world_to_screen(s1, wx, wy, content1_intended)
            q1 = world_to_screen(s1, wx, wy, content1_gpu)
            a = img0[int(p0[1]) - half:int(p0[1]) + half, int(p0[0]) - half:int(p0[0]) + half]
            b = img1_gpu[int(p1[1]) - half:int(p1[1]) + half, int(p1[0]) - half:int(p1[0]) + half]
            if a.shape != (args.patch, args.patch) or b.shape != a.shape or a.std() < 1 or b.std() < 1:
                rows[name] = {"skipped": [list(a.shape), list(b.shape), float(a.std()), float(b.std())]}
                continue
            shift, err, _ = phase_cross_correlation(a, b, upsample_factor=10)
            rows[name] = {"measured_shift_yx_px": [float(-shift[0]), float(-shift[1])],
                          "predicted_by_C1_yx_px": [q1[1] - p1[1], q1[0] - p1[0]],
                          "error": float(err)}
        res["patches"] = rows
        diff = None
        try:
            from PIL import Image
            # Step1's GPU picture resampled onto the ViewBox rect it SHOULD
            # occupy vs the Step0 picture, both cropped to the ViewBox.
            # The world rect both show; each cropped where its intended
            # (ViewBox) mapping puts it. Same scale, so the crops are the
            # same size up to rounding.
            wx0 = max(s0["world"][0], s1["world"][0]); wx1 = min(s0["world"][1], s1["world"][1])
            wy0 = max(s0["world"][2], s1["world"][2]); wy1 = min(s0["world"][3], s1["world"][3])
            a0 = world_to_screen(s0, wx0, wy0, tuple(s0["viewbox"]))
            b0 = world_to_screen(s1, wx0, wy0, content1_intended)
            wpx = int((wx1 - wx0) * cam[2]) - 2
            hpx = int((wy1 - wy0) * cam[2]) - 2
            a = img0[int(round(a0[1])):int(round(a0[1])) + hpx, int(round(a0[0])):int(round(a0[0])) + wpx]
            b = img1_gpu[int(round(b0[1])):int(round(b0[1])) + hpx, int(round(b0[0])):int(round(b0[0])) + wpx]
            hmin, wmin = min(a.shape[0], b.shape[0]), min(a.shape[1], b.shape[1])
            a, b = a[:hmin, :wmin], b[:hmin, :wmin]
            ov = np.stack([a, b, np.zeros_like(a)], axis=2).astype(np.uint8)
            Image.fromarray(ov).save(os.path.join(out_dir, "overlay_step0_red_step1gpu_green.png"))
            diff = np.abs(a - b)
            Image.fromarray(np.clip(diff * 4, 0, 255).astype(np.uint8)).save(os.path.join(out_dir, "difference_x4.png"))
            res["mean_abs_difference"] = float(diff.mean())
        except Exception as exc:  # noqa: BLE001
            res["overlay_error"] = repr(exc)
    after = tree_fingerprint(ORIG_PROJECT)
    res["original_unchanged"] = before == after
    with open(os.path.join(out_dir, "gl_image_result.json"), "w") as fh:
        json.dump(res, fh, indent=1, default=_jsonable)
    print(json.dumps(res, indent=1, default=_jsonable), flush=True)
    sys.stdout.flush()
    os._exit(0 if before == after else 1)


# ── the real app, driven by the user ─────────────────────────────────────


def cmd_realapp(args):
    """Start the real application with the hooks installed; the user does
    the transitions. Also records every page switch with samples at 0 / 50 /
    500 ms (timers, so the user's event loop is never blocked)."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    out_dir = args.out or os.path.join(HOME, "fusionflux", "bench_a0", "camera_logs_real")
    os.makedirs(out_dir, exist_ok=True)
    log = Log(os.path.join(out_dir, f"realapp_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"))
    before = tree_fingerprint(ORIG_PROJECT)
    log.write("guard", original_fingerprint=before)
    import multiprocessing as mp
    mp.set_start_method("spawn", force=True)
    from PyQt5 import QtCore
    from block01 import main as app_main
    from block01.ui import main_window as mw

    win_ref = install_hooks(log, mw.MainWindow)
    original_init = mw.MainWindow.__init__

    def init(self, *a, **k):
        original_init(self, *a, **k)
        win_ref["w"] = self
        log.write("screen", dpr=float(self.devicePixelRatioF()))

        def on_page(index):
            label = f"stack->{index}"
            log.write("page", index=index, state=window_state(self))
            for ms in (0, 50, 500):
                QtCore.QTimer.singleShot(ms, lambda ms=ms: log.write(
                    "sample", label=label, after_ms=ms, state=window_state(self)))
        self._stack.currentChanged.connect(on_page)

    mw.MainWindow.__init__ = init
    print(f"[A0] logging to {log.path}", flush=True)
    try:
        app_main.main()
    finally:
        after = tree_fingerprint(ORIG_PROJECT)
        log.write("guard", original_unchanged=before == after)


# ── summary ──────────────────────────────────────────────────────────────


def cmd_summarize(args):
    rows = [json.loads(line) for line in open(args.log)]
    prev = {}
    print(f"{'label':14s} {'ms':>5s} {'view':6s} {'cx':>10s} {'cy':>10s} {'scale':>9s} "
          f"{'dcx':>8s} {'dcy':>8s} {'dscale%':>8s}  viewbox / viewport / gpu")
    ref = None
    for r in rows:
        if r["kind"] != "sample":
            continue
        st = r["state"]
        view = {0: "step0", 1: "step1", 3: "step3"}.get(st.get("current_step"))
        e = st.get(view) if view else None
        cam = camera_of(e)
        if cam is None:
            continue
        if r["label"] == "start step0" and r["after_ms"] == 500:
            ref = cam
        base = ref or cam
        d = (cam[0] - base[0], cam[1] - base[1], 100.0 * (cam[2] / base[2] - 1.0))
        print(f"{r['label']:14s} {str(r['after_ms']):>5s} {view:6s} {cam[0]:10.2f} {cam[1]:10.2f} "
              f"{cam[2]:9.5f} {d[0]:8.2f} {d[1]:8.2f} {d[2]:8.3f}  "
              f"{e.get('viewbox')} {e.get('viewport')} {e.get('gpu_layer')}")
    loops = [r for r in rows if r["kind"] == "loop"]
    if loops and ref:
        for r in (loops[0], loops[-1]):
            e = r["state"].get("step0")
            cam = camera_of(e)
            if cam:
                print(f"loop {r['i']:3d} step0: d=({cam[0] - ref[0]:.2f}, {cam[1] - ref[1]:.2f}) "
                      f"scale {100.0 * (cam[2] / ref[2] - 1):.3f}%")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("copy-project")
    p.add_argument("--dest", default=DEFAULT_DEST)
    p = sub.add_parser("offscreen")
    p.add_argument("--dest", default=DEFAULT_DEST)
    p.add_argument("--out")
    p.add_argument("--loops", type=int, default=50)
    p.add_argument("--width", type=int, default=1600)
    p.add_argument("--height", type=int, default=1000)
    p = sub.add_parser("gl-image")
    p.add_argument("--dest", default=DEFAULT_DEST)
    p.add_argument("--out")
    p.add_argument("--width", type=int, default=1600)
    p.add_argument("--height", type=int, default=1000)
    p.add_argument("--channel", default="DAPI")
    p.add_argument("--lo", type=float, default=5.0)
    p.add_argument("--hi", type=float, default=120.0)
    p.add_argument("--cx", type=float, default=9000.0)
    p.add_argument("--cy", type=float, default=7000.0)
    p.add_argument("--scale", type=float, default=0.37)
    p.add_argument("--patch", type=int, default=128)
    p.add_argument("--settle-ms", type=int, default=6000)
    p = sub.add_parser("realapp")
    p.add_argument("--out")
    p = sub.add_parser("summarize")
    p.add_argument("log")
    args = ap.parse_args()
    return {"copy-project": cmd_copy_project, "offscreen": cmd_offscreen,
            "gl-image": cmd_gl_image, "realapp": cmd_realapp, "summarize": cmd_summarize}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
