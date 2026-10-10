"""§38: slow notches split into BEFORE the viewer received the notch
(injection -> Qt delivery) and AFTER (delivery -> on screen), with how much
of each the GUI thread spent inside spans (busy) and in gui.gap stalls.
Usage: a9_notch_split.py PERF_LOG [THRESHOLD_MS=50]
"""
import json
import re
import sys

LINE = re.compile(r"^PERF t=(\S+) ev=(\S+)(.*)$")
FIELD = re.compile(r"(\w+)=(\S+)")
WORKER = {"sched.read", "tissue.compose", "preview.compose", "preview.gray", "tissue.seed"}


def overlap(iv, lo, hi):
    return sum(max(0.0, min(e, hi) - max(b, lo)) for b, e in iv) * 1000.0


def merge(iv):
    out = []
    for b, e in sorted(iv):
        if out and b <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([b, e])
    return out


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
    busy = merge([(float(f["t_begin"]), t) for t, e, f in ev
                  if "dur_ms" in f and "t_begin" in f and e not in WORKER])
    gaps = merge([(float(f["t_begin"]), t) for t, e, f in ev
                  if e == "gui.gap" and f.get("where") == "main" and "t_begin" in f])
    rows = []
    for r in inj:
        t0 = r["t"]
        got = next(((t, sq) for t, sq, xts in rx if xts >= int(t0 * 1000.0) - 1), None)
        if got is None:
            continue
        shown = next((t for t, sq in pres if sq >= got[1] and t >= t0), None)
        if shown is None or (shown - t0) * 1000.0 < threshold:
            continue
        trx = got[0]
        rows.append(((trx - t0) * 1000, overlap(busy, t0, trx), overlap(gaps, t0, trx),
                     (shown - trx) * 1000, overlap(busy, trx, shown), overlap(gaps, trx, shown)))
    n = len(rows) or 1
    avg = [sum(r[i] for r in rows) / n for i in range(6)]
    print(f"{len(rows)} notches shown later than {threshold:.0f} ms (averages, ms)")
    print(f"  before delivery {avg[0]:6.1f}: GUI in spans {avg[1]:5.1f}, GUI stalled (gap) {avg[2]:5.1f}")
    print(f"  after  delivery {avg[3]:6.1f}: GUI in spans {avg[4]:5.1f}, GUI stalled (gap) {avg[5]:5.1f}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
