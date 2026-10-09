"""Per-notch report for `xwheel` runs (block A9, §30 F6).

For every real injected notch: queue = viewer receipt - injection; shown =
first presented GPU frame whose input count includes it - injection. Also
per gesture: notches merged into one presented frame, and the longest gap
between presented frames while a notch was still owed.
Usage: a9_xwheel_report.py PERF_LOG   (reads PERF_LOG.xwheel.jsonl too)
"""

import json
import re
import sys

LINE = re.compile(r"^PERF t=(\S+) ev=(\S+)(.*)$")
FIELD = re.compile(r"(\w+)=(\S+)")


def load(path):
    out = []
    for line in open(path, errors="replace"):
        m = LINE.match(line.strip())
        if m:
            out.append((float(m.group(1)), m.group(2), dict(FIELD.findall(m.group(3)))))
    return out


def pct(values, q):
    if not values:
        return float("nan")
    v = sorted(values)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


def main(path):
    ev = load(path)
    inj = [json.loads(l) for l in open(path + ".xwheel.jsonl")]
    # split injections into runs (seq restarts at 1)
    runs, cur = [], []
    for r in inj:
        if r["seq"] == 1 and cur:
            runs.append(cur)
            cur = []
        cur.append(r)
    if cur:
        runs.append(cur)
    starts = [(t, f) for t, e, f in ev if e == "a9.xwheel"]
    # mark labels contain spaces: take them from the raw line
    labels = []
    for line in open(path, errors="replace"):
        if " ev=a9.action " in line and " do=mark " in line and " label=" in line:
            labels.append((float(line.split()[1][2:]), line.split(" label=", 1)[1].split(" expect=")[0].strip()))
    ends = [t for t, e, f in ev if e == "a9.xwheel_end"]
    if not (len(runs) == len(starts) == len(ends)):
        raise SystemExit(f"runs {len(runs)} / starts {len(starts)} / ends {len(ends)} disagree")
    print(f"{'gesture':16s} {'n':>3s} {'rx':>3s} | queue p50/p95/max ms | shown p50/p95/max ms | >50ms | merge max | owed gap max ms")
    allq, alls = [], []
    for i, run in enumerate(runs):
        t0 = starts[i][0]
        # this gesture only: up to the next gesture's start
        t1 = starts[i + 1][0] if i + 1 < len(starts) else ev[-1][0] + 1.0
        label = [l for t, l in labels if t <= t0]
        label = label[-1] if label else "?"
        # (time, seq, X server time ms) of every received input; Qt may
        # merge pointer motions, so an injection is matched to the first
        # receipt whose X time is not earlier than it (X time is this
        # server's CLOCK_MONOTONIC in ms, the injector's clock)
        rx = [(t, int(f["seq"]), int(f.get("xts", 0))) for t, e, f in ev
              if e == "a9.wheel_rx" and t0 <= t <= t1]
        pres = [(t, int(f["seq"])) for t, e, f in ev
                if e == "gpu.present" and t0 <= t <= t1 and f.get("seq", "None") != "None"]
        q, s, merge = [], [], {}
        for k, r in enumerate(run, 1):
            got = next(((t, sq) for t, sq, xts in rx if xts >= int(r["t"] * 1000.0) - 1), None)
            if got is None:
                continue
            q.append((got[0] - r["t"]) * 1000)
            shown = next((t for t, sq in pres if sq >= got[1] and t >= r["t"]), None)
            if shown is not None:
                s.append((shown - r["t"]) * 1000)
                merge[shown] = merge.get(shown, 0) + 1
        # longest wait while a notch was owed: from the later of (the
        # previous presented frame, the oldest owed notch's injection) to
        # the next presented frame -- idle time with nothing owed excluded
        gaps = []
        prev_t, shown_upto = None, 0
        # the receipt seq that covers each injection (or a huge one: never)
        covers = [next((sq for _t, sq, xts in rx if xts >= int(r["t"] * 1000.0) - 1), 10 ** 9)
                  for r in run]
        for t, sq in pres:
            owed = [r["t"] for r, need in zip(run, covers) if need > shown_upto and r["t"] <= t]
            if owed and sq > shown_upto:
                start = owed[0] if prev_t is None else max(prev_t, owed[0])
                gaps.append((t - start) * 1000)
            if sq > shown_upto:
                shown_upto = sq
            prev_t = t
        allq += q
        alls += s
        print(f"{label[:16]:16s} {len(run):3d} {len(rx):3d} | {pct(q,.5):6.1f} {pct(q,.95):6.1f} {max(q or [0]):7.1f} |"
              f" {pct(s,.5):6.1f} {pct(s,.95):6.1f} {max(s or [0]):7.1f} | {sum(x > 50 for x in s):5d} |"
              f" {max(merge.values() or [0]):9d} | {max(gaps or [0]):8.1f}"
              + ("" if len(s) == len(run) else f"  (shown {len(s)}/{len(run)})"))
    print(f"ALL queue p50 {pct(allq,.5):.1f} p95 {pct(allq,.95):.1f} max {max(allq or [0]):.1f} | "
          f"shown p50 {pct(alls,.5):.1f} p95 {pct(alls,.95):.1f} p99 {pct(alls,.99):.1f} max {max(alls or [0]):.1f}")


if __name__ == "__main__":
    main(sys.argv[1])
