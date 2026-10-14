"""R6 actions for the A9 driver (docs/v16_5_r6_motion_clarity_plan.md).

Measurement only, loaded with a9_drive: nothing here changes the program.

  {"do": "smooth", "on": false}            the Smooth button, as a click sets it
  {"do": "navpopup", "rect": [x, y, w, h]} open the Tissue Preview and put it
      at this global rectangle (outside the canvas, so it is in no capture)
  {"do": "refshot", "dir": "...", "name": "...", "timeout_s": 15, "camera": [cx, cy, scale]}
      R6 §4 reference: the camera put there first (inside the action, so
      the settle waits for a frame of the NEW view), then after the driver's own settle (no read, schedulers
      idle, a presented frame complete at its target level), the canvas is
      grabbed every 100 ms until it is unchanged for `still_s` (1 s); saves
      NAME.png and NAME.json (camera, timings, whether either wait timed out)
  {"do": "xstroke", "pattern": "...", "start": [fx, fy], "nav": [x, y]}
      a9_xwheel from the viewer point at fractions (fx, fy) of the canvas;
      with "nav", "{NX}" / "{NY}" in the pattern become the global point of
      level-0 (x, y) on the Tissue Preview, "{SX}" / "{SY}" the start point;
      with "capture": {"out": PREFIX, "seconds": S}, a9_framecap grabs the
      canvas (root window, global rectangle) for S seconds, and the first
      injection is due 0.5 s after it is up (plan §4: 0.5 s before);
      with "at_t0": {"step": N} or {"camera": [cx, cy, scale]} (R6 §12.4),
      that step change / Step1 jump happens (with "rect": "last_refshot"
      the capture and the start point use Step1's picture as the last
      settled shot found it) 0.5 s after the capture is up,
      is logged in the injection log as kind "switch", and the first
      injection is due "after_ms" (default 1000) later
  {"do": "camlog"}                          the camera, into the perf log
  {"do": "gpumem", "label": "..."}          R6 §12.4: every registered viewer
      layer -- which step's, its backend class, hidden, GPU bytes -- and the
      target, into the perf log
"""
import json
import os
import subprocess
import sys
import time

import numpy as np
from PyQt5 import QtCore, QtWidgets

from block01.utils import perf_trace

HERE = os.path.dirname(os.path.abspath(__file__))


#: R6 step 0: the viewer widget draws a 9 px frame around the picture; the
#: picture inside it is 1307 x 788 (maximised window), the canvas measured
INSET = int(os.environ.get("A9_CANVAS_INSET", "9"))


def _canvas(drv):
    """The viewer widget the driver measures and the global rectangle of the
    picture inside its frame."""
    from .a9_drive import _VIEWER_CACHE
    drv._viewer()
    best = _VIEWER_CACHE.get("best")
    if best is None:
        return None, None
    g = best.mapToGlobal(QtCore.QPoint(0, 0))
    return best, (g.x() + INSET, g.y() + INSET, best.width() - 2 * INSET, best.height() - 2 * INSET)


def _camera(drv, step=1):
    mount = getattr(drv.w, f"_step{step}_mount", None)
    cam = mount.current_camera() if mount is not None else None
    return None if cam is None else [round(float(v), 4) for v in cam]


def _do_smooth(drv, a, _t):
    btn = getattr(drv.w, "_btn_smooth", None)
    if btn is None:
        raise RuntimeError("no Smooth button")
    btn.setChecked(bool(a.get("on", False)))
    perf_trace.mark("a9.smooth", on=btn.isChecked())
    drv._next(int(a.get("wait_ms", 500)))


def _navigator(drv):
    display = getattr(drv.w, "_display", None)
    return display.ensure_navigator() if hasattr(display, "ensure_navigator") else \
        getattr(display, "_navigator", None)


def _do_navpopup(drv, a, _t):
    """Open the Tissue Preview the way the user does (Step1's button), then
    move it out of the canvas."""
    btn = getattr(drv.w, "_btn_step1_tissue_nav", None)
    if btn is not None:
        btn.click()

    def place():
        nav = getattr(getattr(drv.w, "_display", None), "_navigator", None)
        if nav is None:
            perf_trace.mark("a9.navpopup", ok=False)
            drv._next(0)
            return
        x, y, w, h = (int(v) for v in a["rect"])
        if nav.is_minimized():
            nav.restore_from_bar()
        nav.setGeometry(x, y, w, h)
        nav.show()
        nav.raise_()

        def record():
            g = nav.frameGeometry()
            perf_trace.mark("a9.navpopup", ok=True, x=g.x(), y=g.y(), w=g.width(), h=g.height())
            drv.w.activateWindow()
            drv._next(int(a.get("wait_ms", 1000)))
        QtCore.QTimer.singleShot(1500, record)
    QtCore.QTimer.singleShot(1000, place)


def _nav_point(drv, x, y):
    """Global screen point of level-0 (x, y) on the Tissue Preview."""
    nav = getattr(getattr(drv.w, "_display", None), "_navigator", None)
    panel = nav.overview if nav is not None else None
    if panel is None or not getattr(panel, "ds", None):
        return None
    ds = float(panel.ds)
    scene = panel.vb.mapViewToScene(QtCore.QPointF(x / ds, y / ds))
    local = panel.gview.mapFromScene(scene)
    g = panel.gview.viewport().mapToGlobal(local)
    return g.x(), g.y()


def _do_refshot(drv, a, _t):
    from .a9_xgrab import Grabber
    out = a["dir"]
    os.makedirs(out, exist_ok=True)
    t_begin = time.monotonic()
    state = {"settle_timed_out": None}
    real_next = drv._next

    def after_settle():
        # the settle already marked a9.settled; whether it timed out is in
        # the perf log, and is re-checked here from the probe
        from block01.viewer import coverage_probe
        f = coverage_probe.LAST_BY.get("gpu") or {}
        state["settle_timed_out"] = time.monotonic() - t_begin >= float(a.get("timeout_s", 15)) - 0.05
        state["coverage"] = {k: f.get(k) for k in ("gap_cells", "exact_fraction", "target_fraction")}

    # _after_settle calls then() and then drv._next(0): hold that _next back
    # until the pixels are still as well
    def hold(_delay=0):
        drv._next = real_next
        _, rect = _canvas(drv)
        if rect is None:
            perf_trace.mark("a9.refshot", name=a["name"], ok=False, why="no canvas")
            real_next(0)
            return
        root = QtWidgets.QApplication.desktop().winId()
        g = Grabber(int(root), *rect)
        still = float(a.get("still_s", 1.0))
        deadline = t_begin + float(a.get("timeout_s", 15))
        last = {"raw": None, "since": time.monotonic()}

        def poll():
            raw = g.grab_raw()
            now = time.monotonic()
            if raw != last["raw"]:
                last["raw"], last["since"] = raw, now
            if now - last["since"] >= still or now >= deadline:
                rgb = g.to_rgb(raw)
                import cv2
                cv2.imwrite(os.path.join(out, a["name"] + ".png"), rgb[:, :, ::-1])
                meta = {"name": a["name"], "camera": _camera(drv), "rect": list(rect),
                        "still_s": round(now - last["since"], 3),
                        "pixels_timed_out": now - last["since"] < still,
                        "elapsed_s": round(now - t_begin, 3), **state}
                json.dump(meta, open(os.path.join(out, a["name"] + ".json"), "w"))
                drv._r6_rect = list(rect)          # Step1's picture, for a later xstroke
                perf_trace.mark("a9.refshot", name=a["name"], ok=True,
                                timed_out=bool(meta["pixels_timed_out"] or meta["settle_timed_out"]))
                real_next(0)
                return
            QtCore.QTimer.singleShot(100, poll)
        poll()
    drv._next = hold
    if a.get("camera"):
        mount = getattr(drv.w, "_step1_mount", None)
        ok = bool(mount is not None and mount.apply_camera(*(float(v) for v in a["camera"])))
        # R6 step 0: also tell the window's camera owner, as an explicit
        # jump does (patch, Tissue Preview) -- otherwise Step3, and Step1
        # after Step3, go to the owner's older place
        published = bool(ok and mount.publish_camera("a9-r6", jump=True))
        perf_trace.mark("a9.camera_set", ok=ok, published=published, cx=a["camera"][0], cy=a["camera"][1],
                        scale=a["camera"][2])
    drv._after_settle(after_settle, timeout_s=float(a.get("timeout_s", 15)))


def _do_camlog(drv, a, _t):
    perf_trace.mark("a9.camlog", camera=json.dumps(_camera(drv)), step3=json.dumps(_camera(drv, 3)),
                    owner=repr(getattr(getattr(drv.w, "_camera_owner", None), "_shot", None)))
    drv._next(0)


def _do_xstroke(drv, a, _t):
    best, rect = _canvas(drv)
    if a.get("rect") == "last_refshot":
        # S3: started on Step3, measured on Step1 after the switch -- the
        # rectangle of Step1's picture from the last settled shot
        rect = tuple(drv._r6_rect)
    if best is None:
        raise RuntimeError("no viewer on screen")
    fx, fy = a.get("start", [0.5, 0.5])
    sx, sy = int(rect[0] + rect[2] * fx), int(rect[1] + rect[3] * fy)
    top = QtWidgets.QApplication.widgetAt(QtCore.QPoint(sx, sy))
    if top is None or top.window() is not drv.w:
        raise RuntimeError(f"start point {sx},{sy} is covered by {type(top).__name__}")
    pattern = a["pattern"].replace("{SX}", str(sx)).replace("{SY}", str(sy))
    if "nav" in a:
        p = _nav_point(drv, *a["nav"])
        if p is None:
            raise RuntimeError("no Tissue Preview to click")
        pattern = pattern.replace("{NX}", str(p[0])).replace("{NY}", str(p[1]))
    log = a.get("log") or os.environ.get("BLOCK01_PERF_LOG", "a9") + ".xwheel.jsonl"
    cap, env = None, dict(os.environ)
    if a.get("capture"):
        root = QtWidgets.QApplication.desktop().winId()
        cap = subprocess.Popen([sys.executable, os.path.join(HERE, "a9_framecap.py"), str(int(root)),
                                *map(str, rect), str(a["capture"]["seconds"]), a["capture"]["out"]])
        time.sleep(0.6)                      # the grabber is up (GUI blocked: nothing moves yet)
        env["A9_XWHEEL_T0"] = repr(time.monotonic() + 0.5)
    if a.get("at_t0"):
        t_switch = time.monotonic() + 0.5
        env["A9_XWHEEL_T0"] = repr(t_switch + float(a.get("after_ms", 1000)) / 1000.0)

        def switch(what=a["at_t0"]):
            t = time.monotonic()
            if "step" in what:
                getattr(drv.w, f"_go_to_step{int(what['step'])}")()
            else:
                mount = getattr(drv.w, "_step1_mount", None)
                if mount.apply_camera(*(float(v) for v in what["camera"])):
                    mount.publish_camera("a9-r6", jump=True)
            with open(log, "a") as fh:
                fh.write(json.dumps({"kind": "switch", "t": t, "what": what,
                                     "late_ms": round((t - t_switch) * 1000.0, 3)}) + "\n")
        QtCore.QTimer.singleShot(max(0, int((t_switch - time.monotonic()) * 1000)), switch)
    perf_trace.mark("a9.xstroke", n=drv.i, x=sx, y=sy, pattern=pattern, camera=json.dumps(_camera(drv)),
                    t0=env.get("A9_XWHEEL_T0"), rect=json.dumps(list(rect)))
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "a9_xwheel.py"), str(sx), str(sy),
                             pattern, log, str(int(drv.w.winId()))], env=env)

    def poll():
        if proc.poll() is None or (cap is not None and cap.poll() is None):
            QtCore.QTimer.singleShot(50, poll)
            return
        perf_trace.mark("a9.xstroke_end", n=drv.i, rc=proc.returncode, camera=json.dumps(_camera(drv)),
                        cap_rc=None if cap is None else cap.returncode)
        drv._next(int(a.get("tail_ms", 500)))
    QtCore.QTimer.singleShot(50, poll)


def _do_gpumem(drv, a, _t):
    from block01.ui import gpu_memory
    owners = {}
    for step, name in ((1, "_step1_mount"), (3, "_step3_mount")):
        mount = drv.w.__dict__.get(name)
        layer = getattr(mount, "gpu_layer", None) if mount is not None else None
        if layer is not None:
            owners[id(layer)] = step
    for layer in list(gpu_memory._layers):
        try:
            nbytes = int(layer.gpu_bytes())
        except Exception:                                    # noqa: BLE001
            nbytes = -1
        perf_trace.mark("a9.gpumem", label=a.get("label", ""), viewer=f"{id(layer):x}",
                        step=owners.get(id(layer), 0), kind=type(layer).__name__,
                        hidden=bool(getattr(layer, "hidden", False)), bytes=nbytes,
                        target=int(gpu_memory.VIEW_VRAM_TARGET))
    drv._next(0)


def _do_settings(drv, a, _t):
    """R6 §12.2: what the restored session actually shows -- smooth, the
    backend of each mount, the camera."""
    from block01.ui import gpu_memory
    for step, name in ((1, "_step1_mount"), (3, "_step3_mount")):
        mount = drv.w.__dict__.get(name)
        if mount is not None:
            perf_trace.mark("a9.settings", step=step, backend=str(getattr(mount, "backend", "")),
                            binding=type(getattr(mount, "gpu_binding", None)).__name__,
                            layer=type(getattr(mount, "gpu_layer", None)).__name__,
                            rust_refused=str(getattr(mount, "_rust_refused", ""))[:120].replace(" ", "_"))
    perf_trace.mark("a9.settings", smooth=gpu_memory.smooth_pixels(), camera=json.dumps(_camera(drv)))
    drv._next(0)


def _do_waitpresent(drv, a, _t):
    """R7a §3: wait until the Step1 viewer presents a frame newer than the
    one at this action's start (after a step change); logs how long it
    took, then waits `then_ms` more."""
    from block01.viewer import coverage_probe
    start = dict(coverage_probe.PUBLISHED_BY)
    t0 = time.monotonic()
    limit = float(a.get("timeout_ms", 5000)) / 1000.0

    def poll():
        now = time.monotonic()
        if coverage_probe.PUBLISHED_BY.get("gpu", 0) > start.get("gpu", 0) or now - t0 >= limit:
            perf_trace.mark("a9.waitpresent", ms=round((now - t0) * 1000.0, 1),
                            timed_out=now - t0 >= limit, label=a.get("label", ""))
            drv._next(int(a.get("then_ms", 0)))
            return
        QtCore.QTimer.singleShot(10, poll)
    QtCore.QTimer.singleShot(0, poll)


def install(cls):
    for name, fn in list(globals().items()):
        if name.startswith("_do_"):
            setattr(cls, name, fn)


_ = np  # numpy is a9_xgrab's; imported here so a missing one fails at load
