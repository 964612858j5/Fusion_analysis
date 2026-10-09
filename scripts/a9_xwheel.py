"""Real-input wheel injector for smoothness measurement (block A9, §30 F6).

A SEPARATE PROCESS that injects system-level wheel notches through XTest
(button 4 = zoom in, 5 = zoom out) on absolute deadlines, never waiting for
the program under test -- like a human hand: a busy program makes the
notches queue up, it cannot delay them. Each injection is logged with
`time.monotonic()` (the same clock as the perf log) as a JSON line.

Usage: a9_xwheel.py X Y PATTERN LOG
PATTERN: comma-separated tokens, each `+N@MS` (N notches in, MS ms apart),
`-N@MS` (out), `wMS` (wait MS ms), or `>DX:DY:N@MS` (a left-button drag by
DX, DY pixels in N motion steps MS ms apart; each step is one injection).
Example: "+4@30,w300,+4@30,w300,>300:0:30@8".
"""

import ctypes
import json
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


def parse(pattern):
    steps = []
    for tok in pattern.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok[0] == "w":
            steps.append(("wait", float(tok[1:]) / 1000.0))
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
    dpy = x11.XOpenDisplay(None)
    if not dpy:
        raise SystemExit("cannot open display")
    xtst.XTestFakeMotionEvent(dpy, -1, x, y, 0)
    x11.XSync(dpy, 0)
    time.sleep(0.3)
    due = time.monotonic()
    seq = 0
    with open(log, "a") as out:
        for kind, value in parse(pattern):
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
            if kind == "move":
                xtst.XTestFakeMotionEvent(dpy, -1, int(round(x + value[0])), int(round(y + value[1])), 0)
            else:
                xtst.XTestFakeButtonEvent(dpy, value, 1, 0)
                xtst.XTestFakeButtonEvent(dpy, value, 0, 0)
            x11.XFlush(dpy)
            out.write(json.dumps({"seq": seq, "t": t, "kind": kind,
                                  "button": value if kind == "notch" else None,
                                  "late_ms": round((t - due) * 1000.0, 3)}) + "\n")
    x11.XSync(dpy, 0)


if __name__ == "__main__":
    main()
