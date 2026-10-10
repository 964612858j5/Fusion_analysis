"""§38: slow notches' time OUTSIDE every span, attributed to what the GUI
thread was doing by the Qt event timeline (`BLOCK01_A9_EVENTS`): idle in the
event loop, or the event (type, receiver class) being processed.
Usage: a9_event_attr.py PERF_LOG EVENTS [THRESHOLD_MS=50] [PHASE=all|before|after]
"""
import bisect
import json
import re
import sys
from collections import Counter

LINE = re.compile(r"^PERF t=(\S+) ev=(\S+)(.*)$")
FIELD = re.compile(r"(\w+)=(\S+)")
WORKER = {"sched.read", "tissue.compose", "preview.compose", "preview.gray", "tissue.seed"}


def type_name(kind):
    if kind == -1:
        return "IDLE"
    if kind == -2:
        return "awake"
    try:
        from PyQt5 import QtCore
        for name in dir(QtCore.QEvent):
            if getattr(QtCore.QEvent, name) == kind and name[0].isupper():
                return name
    except Exception:
        pass
    return str(kind)


def main(path, events, threshold=50.0, phase="all"):
    threshold = float(threshold)
    ev = []
    for line in open(path, errors="replace"):
        m = LINE.match(line.strip())
        if m:
            ev.append((float(m.group(1)), m.group(2), dict(FIELD.findall(m.group(3)))))
    inj = [json.loads(l) for l in open(path + ".xwheel.jsonl")]
    rx = [(t, int(f["seq"]), int(f.get("xts", 0))) for t, e, f in ev if e == "a9.wheel_rx"]
    pres = [(t, int(f["seq"])) for t, e, f in ev
            if e == "gpu.present" and f.get("seq", "None") != "None"]
    busy = []
    for b, e in sorted((float(f["t_begin"]), t) for t, e, f in ev
                       if "dur_ms" in f and "t_begin" in f and e not in WORKER):
        if busy and b <= busy[-1][1]:
            busy[-1][1] = max(busy[-1][1], e)
        else:
            busy.append([b, e])
    rows = []
    for line in open(events):
        t, k, c = line.rstrip("\n").split("\t")
        rows.append((float(t), int(k), c))
    rows.sort()
    times = [r[0] for r in rows]
    names = {}
    # segments: row i lasts until row i+1; after <block> the thread is idle
    # until <awake>; after <awake> until the next event it is in the loop
    def label(i):
        t, k, c = rows[i]
        if k == -1:
            return "IDLE (event loop asleep)"
        if k == -2:
            return "loop (woken, before the next event)"
        if k not in names:
            names[k] = type_name(k)
        return f"{names[k]} -> {c}"

    def untraced(lo, hi):
        """[lo, hi] minus the span cover."""
        out, cur = [], lo
        i = bisect.bisect_left([b[1] for b in busy], lo)
        while i < len(busy) and busy[i][0] < hi:
            b, e = busy[i]
            if b > cur:
                out.append((cur, min(b, hi)))
            cur = max(cur, e)
            i += 1
        if cur < hi:
            out.append((cur, hi))
        return out

    acc, n, total = Counter(), 0, 0.0
    for r in inj:
        t0 = r["t"]
        got = next(((t, sq) for t, sq, xts in rx if xts >= int(t0 * 1000.0) - 1), None)
        if got is None:
            continue
        shown = next((t for t, sq in pres if sq >= got[1] and t >= t0), None)
        if shown is None or (shown - t0) * 1000.0 < threshold:
            continue
        lo, hi = {"before": (t0, got[0]), "after": (got[0], shown)}.get(phase, (t0, shown))
        n += 1
        for a, b in untraced(lo, hi):
            i = bisect.bisect_right(times, a) - 1
            cur = a
            while cur < b:
                nxt = times[i + 1] if i + 1 < len(times) else b
                end = min(nxt, b)
                if end > cur and i >= 0:
                    acc[label(i)] += (end - cur) * 1000.0
                    total += (end - cur) * 1000.0
                cur = end
                i += 1
    print(f"{n} slow notches ({phase}); untraced ms per notch: {total / max(1, n):.1f}")
    for k, v in acc.most_common(20):
        print(f"  {v / max(1, n):6.1f}  {k}")


if __name__ == "__main__":
    main(*sys.argv[1:5])
