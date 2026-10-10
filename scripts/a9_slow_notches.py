"""Where the slow notches went (block A9 §38): for every injected notch
shown later than THRESHOLD ms, the GUI-thread spans inside [injection,
shown], summed by name (nested spans: each moment counted once, to the innermost span), and
what is left uncovered (Qt / the driver / idle waits).
Usage: a9_slow_notches.py PERF_LOG [THRESHOLD_MS=50]
"""

import json
import re
import sys
from collections import Counter

LINE = re.compile(r"^PERF t=(\S+) ev=(\S+)(.*)$")
FIELD = re.compile(r"(\w+)=(\S+)")
# spans that run on the GUI thread (worker spans are left out)
WORKER = {"sched.read", "tissue.compose", "preview.compose", "preview.gray", "tissue.seed"}


def main(path, threshold=50.0):
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
    spans = sorted(((float(f["t_begin"]), -t, e) for t, e, f in ev
                    if "dur_ms" in f and "t_begin" in f and e not in WORKER))
    spans = [(b, -nt, e) for b, nt, e in spans]
    # EXCLUSIVE segments: each moment goes to the innermost span open then
    # (spans on one thread nest; a child starts after and ends before its parent)
    outer, stack, cursor = [], [], None
    events = []
    for b, t, e in spans:
        events.append((b, 1, t, e))
    events.sort(key=lambda x: (x[0], -x[2]))
    for b, _kind, t, e in events:
        while stack and stack[-1][0] <= b:
            end_t, name = stack.pop()
            if cursor is not None and end_t > cursor:
                outer.append((cursor, end_t, name))
            cursor = end_t
        if stack and cursor is not None and b > cursor:
            outer.append((cursor, b, stack[-1][1]))
        stack.append((t, e))
        cursor = b
    while stack:
        end_t, name = stack.pop()
        if end_t > cursor:
            outer.append((cursor, end_t, name))
        cursor = end_t
    # the GUI loop's own stalls (`gui.gap`): time the thread was blocked
    gaps = [(float(f["t_begin"]), t) for t, e, f in ev
            if e == "gui.gap" and f.get("where") == "main" and "t_begin" in f]
    total, slow, uncovered, blocked = Counter(), 0, 0.0, 0.0
    for r in inj:
        t0 = r["t"]
        got = next(((t, sq) for t, sq, xts in rx if xts >= int(t0 * 1000.0) - 1), None)
        if got is None:
            continue
        shown = next((t for t, sq in pres if sq >= got[1] and t >= t0), None)
        if shown is None or (shown - t0) * 1000.0 < threshold:
            continue
        slow += 1
        busy = 0.0
        for b, t, e in outer:
            lo, hi = max(b, t0), min(t, shown)
            if hi > lo:
                total[e] += (hi - lo) * 1000.0
                busy += (hi - lo) * 1000.0
        uncovered += (shown - t0) * 1000.0 - busy
        # blocked but in no span: gap time minus the spans inside it
        for gb, gt in gaps:
            lo, hi = max(gb, t0), min(gt, shown)
            if hi <= lo:
                continue
            inside = sum(max(0.0, min(t, hi) - max(b, lo)) for b, t, _e in outer)
            blocked += (hi - lo - inside) * 1000.0
    print(f"{slow} notches shown later than {threshold:.0f} ms; per slow notch, ms inside its wait:")
    for e, ms in total.most_common(15):
        print(f"  {ms / max(1, slow):7.1f}  {e}")
    print(f"  {uncovered / max(1, slow):7.1f}  (no GUI span: Qt event loop, paint/swap wait, input queue)")
    print(f"     of which {blocked / max(1, slow):5.1f}  GUI thread blocked (a gui.gap) in untraced code")


if __name__ == "__main__":
    main(*sys.argv[1:3])
