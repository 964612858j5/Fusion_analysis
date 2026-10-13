"""R4 plan §2: is the screen fit for timing? A small QOpenGLWidget redraws
for SECONDS (default 3) and every frameSwapped is timed; the X11 login
session's lock state is read from logind. Passes when the swap interval's
median is 15.7-17.7 ms, its 95th percentile under 20 ms and the session is
not locked (the compositor drops to 1 frame/s when locked, when another
output such as VGA-2-1 is on, or when the monitor sleeps: A9 §23.6).

Usage: a9_swapcheck.py OUT_JSONL [SECONDS]    (appends one JSON line;
exit 0 when it passes)"""
import json
import subprocess
import sys
import time

from PyQt5 import QtCore, QtWidgets


def locked():
    """LockedHint of the active x11 session, or None when not found."""
    try:
        rows = subprocess.run(["loginctl", "list-sessions", "--no-legend"], capture_output=True,
                              text=True, timeout=5).stdout.split("\n")
        for sid in (r.split()[0] for r in rows if r.strip()):
            props = dict(line.split("=", 1) for line in subprocess.run(
                ["loginctl", "show-session", sid, "-p", "Type", "-p", "Active", "-p", "LockedHint"],
                capture_output=True, text=True, timeout=5).stdout.split("\n") if "=" in line)
            if props.get("Type") == "x11" and props.get("Active") == "yes":
                return props.get("LockedHint") == "yes"
    except Exception:                                   # noqa: BLE001 -- reported as unknown
        pass
    return None


def main(out, seconds=3.0):
    seconds = float(seconds)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    w = QtWidgets.QOpenGLWidget()
    w.resize(200, 120)
    w.move(60, 60)
    swaps = []
    w.frameSwapped.connect(lambda: (swaps.append(time.monotonic()), w.update()))
    w.show()
    QtCore.QTimer.singleShot(int(seconds * 1000) + 500, app.quit)
    app.exec_()
    w.close()
    gaps = sorted((b - a) * 1000.0 for a, b in zip(swaps[1:], swaps[2:]))   # the first is start-up
    med = gaps[len(gaps) // 2] if gaps else None
    p95 = gaps[min(len(gaps) - 1, int(round(0.95 * (len(gaps) - 1))))] if gaps else None
    lock = locked()
    ok = med is not None and 15.7 <= med <= 17.7 and p95 < 20.0 and lock is False
    rec = {"t": time.monotonic(), "wall": time.strftime("%H:%M:%S"), "swaps": len(swaps),
           "median_ms": None if med is None else round(med, 2),
           "p95_ms": None if p95 is None else round(p95, 2), "locked": lock, "ok": ok}
    with open(out, "a") as f:
        f.write(json.dumps(rec) + "\n")
    print(json.dumps(rec))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main(*sys.argv[1:3])
