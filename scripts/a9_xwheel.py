"""Real-input wheel injector for smoothness measurement (block A9, §30 F6).

A SEPARATE PROCESS that injects system-level wheel notches through XTest
(button 4 = zoom in, 5 = zoom out) on absolute deadlines, never waiting for
the program under test -- like a human hand: a busy program makes the
notches queue up, it cannot delay them. Each injection is logged with
`time.monotonic()` (the same clock as the perf log) as a JSON line.

Usage: a9_xwheel.py X Y PATTERN LOG [TARGET_WINDOW]
PATTERN: comma-separated tokens, each `+N@MS` (N notches in, MS ms apart),
`-N@MS` (out), `wMS` (wait MS ms), or `>DX:DY:N@MS` (a left-button drag by
DX, DY pixels in N motion steps MS ms apart; each step is one injection),
or `/DX:DY:N@MS:BACK` (R6 plan §3, a "stroke": the same drag, but the button
is released AT THE END POINT, then the pointer moves back to the start with
no button held and the injector waits BACK ms; logged as kinds "move",
"release_end" (xy = where it was released) and "hover").
`cX:Y` clicks the left button at the absolute screen point (X, Y) (R6 S2:
the Tissue Preview), logged as kind "click"; a stroke first moves the
pointer to its start (kind "hover"), so it can follow a click elsewhere.
Example: "+4@30,w300,+4@30,w300,>300:0:30@8", "/900:0:90@8:50",
"c120:300,w100,/900:0:45@8:50".

R4 plan §2 (delivery evidence, the same for every program): each logged
injection also records the top-level window under the pointer when it was
sent (`under`), whether that is TARGET_WINDOW's top-level (`on_target`,
None without a target) and the X errors so far (`x_errors`); a target that
cannot be found is logged as {"error": ...} in LOG.err.
"""

import ctypes
import json
import os
import sys
import time

x11 = ctypes.CDLL("libX11.so.6")
xtst = ctypes.CDLL("libXtst.so.6")
x11.XOpenDisplay.restype = ctypes.c_void_p
x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
x11.XFlush.argtypes = [ctypes.c_void_p]
x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
xtst.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
xtst.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
x11.XDefaultRootWindow.restype = ctypes.c_ulong
x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
_W = ctypes.c_ulong
x11.XQueryPointer.argtypes = [ctypes.c_void_p, _W, ctypes.POINTER(_W), ctypes.POINTER(_W),
                              ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
                              ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
                              ctypes.POINTER(ctypes.c_uint)]
x11.XQueryTree.argtypes = [ctypes.c_void_p, _W, ctypes.POINTER(_W), ctypes.POINTER(_W),
                           ctypes.POINTER(ctypes.POINTER(_W)), ctypes.POINTER(ctypes.c_uint)]
x11.XFree.argtypes = [ctypes.c_void_p]
_ERRORS = [0]
_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(
    lambda _d, _e: _ERRORS.__setitem__(0, _ERRORS[0] + 1) or 0)
x11.XSetErrorHandler.argtypes = [ctypes.c_void_p]


def top_level(dpy, root, win):
    """The root's child that contains `win` (its window-manager frame), or
    None when `win` does not exist."""
    errors = _ERRORS[0]
    while True:
        r, parent, kids, n = _W(), _W(), ctypes.POINTER(_W)(), ctypes.c_uint()
        if not x11.XQueryTree(dpy, win, ctypes.byref(r), ctypes.byref(parent), ctypes.byref(kids),
                              ctypes.byref(n)) or _ERRORS[0] != errors:
            return None
        if kids:
            x11.XFree(kids)
        if parent.value in (0, root):
            return win
        win = parent.value


def under_pointer(dpy, root):
    r, child = _W(), _W()
    i = ctypes.c_int()
    m = ctypes.c_uint()
    x11.XQueryPointer(dpy, root, ctypes.byref(r), ctypes.byref(child), ctypes.byref(i), ctypes.byref(i),
                      ctypes.byref(i), ctypes.byref(i), ctypes.byref(m))
    return child.value


def parse(pattern):
    steps = []
    for tok in pattern.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok[0] == "w":
            steps.append(("wait", float(tok[1:]) / 1000.0))
            continue
        if tok[0] == "c":
            cx, cy = tok[1:].split(":")
            steps.append(("click", (int(cx), int(cy))))
            continue
        if tok[0] == "/":
            spec, rest = tok[1:].split("@")
            ms, back = rest.split(":")
            dx, dy, n = spec.split(":")
            steps.append(("hover", (0.0, 0.0)))
            steps.append(("press", None))
            for k in range(1, int(n) + 1):
                steps.append(("wait", float(ms) / 1000.0))
                steps.append(("move", (float(dx) * k / int(n), float(dy) * k / int(n))))
            steps.append(("wait", float(ms) / 1000.0))
            steps.append(("release_end", (float(dx), float(dy))))
            steps.append(("hover", (0.0, 0.0)))
            steps.append(("wait", float(back) / 1000.0))
            continue
        if tok[0] == ">":
            spec, ms = tok[1:].split("@")
            dx, dy, n = spec.split(":")
            steps.append(("press", None))
            for k in range(1, int(n) + 1):
                steps.append(("wait", float(ms) / 1000.0))
                steps.append(("move", (float(dx) * k / int(n), float(dy) * k / int(n))))
            steps.append(("wait", float(ms) / 1000.0))
            steps.append(("release", None))
            continue
        count, ms = tok[1:].split("@")
        button = 4 if tok[0] == "+" else 5
        for k in range(int(count)):
            if k:
                steps.append(("wait", float(ms) / 1000.0))
            steps.append(("notch", button))
    return steps


def main():
    x, y, pattern, log = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4]
    target = int(sys.argv[5], 0) if len(sys.argv) > 5 else None
    dpy = x11.XOpenDisplay(None)
    if not dpy:
        raise SystemExit("cannot open display")
    x11.XSetErrorHandler(_HANDLER)
    root = x11.XDefaultRootWindow(dpy)
    frame = top_level(dpy, root, target) if target is not None else None
    if target is not None and frame is None:
        with open(log + ".err", "a") as err:            # not in LOG: its readers want "t"
            err.write(json.dumps({"error": f"target window {target:#x} not found"}) + "\n")
    xtst.XTestFakeMotionEvent(dpy, -1, x, y, 0)
    x11.XSync(dpy, 0)
    time.sleep(0.3)
    due = time.monotonic()
    # R6 S2: an absolute first deadline (time.monotonic()), so a stroke can
    # start a fixed time after a jump issued by another process
    if os.environ.get("A9_XWHEEL_T0"):
        due = max(due, float(os.environ["A9_XWHEEL_T0"]))
    seq = 0
    steps = parse(pattern)
    # §44 step 0: the planned moment of the last logged injection, written
    # BEFORE the first one, so a stop-shot watcher can aim at the real stop
    # (it used to wait 50 ms of silence first, and its "20 ms" shot was ~52)
    end = last_due = due
    for kind, value in steps:
        if kind == "wait":
            end += value
        elif kind in ("move", "notch", "release_end", "hover", "click"):
            last_due = end
    with open(log + ".plan", "a") as plan:
        plan.write(json.dumps({"start": due, "end": last_due, "pattern": pattern}) + "\n")
    with open(log, "a") as out:
        for kind, value in steps:
            if kind == "wait":
                due += value
                continue
            while True:                       # absolute deadlines: no drift
                left = due - time.monotonic()
                if left <= 0:
                    break
                time.sleep(min(left, 0.002))
            t = time.monotonic()
            if kind == "press":
                xtst.XTestFakeButtonEvent(dpy, 1, 1, 0)
                x11.XFlush(dpy)
                continue
            if kind == "release":
                xtst.XTestFakeMotionEvent(dpy, -1, x, y, 0)
                xtst.XTestFakeButtonEvent(dpy, 1, 0, 0)
                x11.XFlush(dpy)
                continue
            seq += 1
            if kind == "click":           # R6: absolute point, press + release
                xtst.XTestFakeMotionEvent(dpy, -1, value[0], value[1], 0)
                xtst.XTestFakeButtonEvent(dpy, 1, 1, 0)
                xtst.XTestFakeButtonEvent(dpy, 1, 0, 0)
            elif kind == "release_end":   # R6: release where the stroke ended
                xtst.XTestFakeButtonEvent(dpy, 1, 0, 0)
            elif kind == "hover":         # R6: back to the start, no button held
                xtst.XTestFakeMotionEvent(dpy, -1, x, y, 0)
            elif kind == "move":
                xtst.XTestFakeMotionEvent(dpy, -1, int(round(x + value[0])), int(round(y + value[1])), 0)
            else:
                xtst.XTestFakeButtonEvent(dpy, value, 1, 0)
                xtst.XTestFakeButtonEvent(dpy, value, 0, 0)
            x11.XFlush(dpy)
            under = under_pointer(dpy, root)
            out.write(json.dumps({"seq": seq, "t": t, "kind": kind,
                                  "under": hex(under), "x_errors": _ERRORS[0],
                                  "on_target": None if target is None else (frame is not None and under == frame),
                                  "button": value if kind == "notch" else None,
                                  # §44 step 0: where a move went (pairs it with its receipt)
                                  "xy": list(value) if kind == "click" else
                                  [int(round(x + value[0])), int(round(y + value[1]))]
                                  if kind in ("move", "release_end", "hover") else None,
                                  "late_ms": round((t - due) * 1000.0, 3)}) + "\n")
    x11.XSync(dpy, 0)


if __name__ == "__main__":
    main()
