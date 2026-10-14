"""R6 plan §4: continuous full-canvas capture (docs/v16_5_r6_motion_clarity_plan.md).

Grabs a window region through MIT-SHM (a9_xgrab) on absolute deadlines
(default 60 per second) and keeps every grab in memory -- including the
unchanged ones, they measure freezes -- then writes them out after the
capture ends, so writing never competes with capturing.

Usage: a9_framecap.py WINDOW X Y W H SECONDS OUT [HZ]
  OUT.frames: raw BGRX rows (H x stride bytes per frame, in order);
  OUT.json:   {"w", "h", "stride", "hz", "t0", "t1": [...]} where t0/t1
              are time.monotonic() just before / after each grab (the same
              clock as the injector and the perf log).
Stops early on SIGTERM / SIGINT (what was captured is still written).
"""
import json
import signal
import sys
import time

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from a9_xgrab import Grabber  # noqa: E402

_STOP = [False]


def main():
    win, x, y, w, h = sys.argv[1], *map(int, sys.argv[2:6])
    seconds, out = float(sys.argv[6]), sys.argv[7]
    hz = float(sys.argv[8]) if len(sys.argv) > 8 else 60.0
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: _STOP.__setitem__(0, True))
    g = Grabber(win, x, y, w, h)
    g.grab_raw()                                   # warm-up, not kept
    frames, t0s, t1s = [], [], []
    start = due = time.monotonic()
    period = 1.0 / hz
    while not _STOP[0] and due - start < seconds:
        while True:                                # absolute deadlines: no drift
            left = due - time.monotonic()
            if left <= 0:
                break
            time.sleep(min(left, 0.001))
        a = time.monotonic()
        frames.append(g.grab_raw())
        b = time.monotonic()
        t0s.append(a)
        t1s.append(b)
        due += period
        if b > due:                                # late: skip missed slots, never bunch up
            due += period * int((b - due) / period + 1)
    with open(out + ".frames", "wb") as f:
        for fr in frames:
            f.write(fr)
    json.dump({"w": w, "h": h, "stride": g.stride, "hz": hz, "shm": g.shm is not None,
               "t0": t0s, "t1": t1s}, open(out + ".json", "w"))
    print(json.dumps({"frames": len(frames), "seconds": round(t1s[-1] - t0s[0], 3) if frames else 0}))


if __name__ == "__main__":
    main()
