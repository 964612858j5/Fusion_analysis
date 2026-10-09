"""Exclusive GUI-thread time per span (block A9 §34 M0).

Spans in the perf log carry `t_begin` and end at `t`; nested spans (a
publication inside a planning call, a render inside a submission) are
subtracted from their parent so every millisecond is counted once. Only
GUI-thread spans are used (gpu.*, attr.*); worker spans (sched.read, ...)
are reported separately. Window: from the first `a9.xwheel` to the end.
Usage: a9_exclusive.py PERF_LOG [MEM_JSONL]"""

import json
import re
import sys
from collections import defaultdict

LINE = re.compile(r"^PERF t=(\S+) ev=(\S+)(.*)$")
GUI = ("gpu.", "attr.")


def pct(v, q):
    v = sorted(v)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))] if v else float("nan")


def main(path, mem=None):
    spans, worker = [], defaultdict(list)
    start = None
    for line in open(path, errors="replace"):
        m = LINE.match(line.strip())
        if not m:
            continue
        t, ev, rest = float(m.group(1)), m.group(2), m.group(3)
        if ev == "a9.xwheel" and start is None:
            start = t
        b = re.search(r"t_begin=(\S+)", rest)
        if not b:
            continue
        tb = float(b.group(1))
        if ev.startswith(GUI):
            spans.append((tb, t, ev))
        else:
            worker[ev].append((t - tb) * 1000)
    spans = [s for s in spans if start is not None and s[0] >= start]
    spans.sort(key=lambda s: (s[0], -s[1]))
    excl = defaultdict(list)
    stack = []                         # [begin, end, name, child_ms]
    def close(item):
        b, e, name, child = item
        excl[name].append((e - b) * 1000 - child)
        if stack:
            stack[-1][3] += (e - b) * 1000
    for b, e, name in spans:
        while stack and stack[-1][1] <= b:
            close(stack.pop())
        stack.append([b, e, name, 0.0])
    while stack:
        close(stack.pop())
    end = max((s[1] for s in spans), default=start or 0)
    wall = (end - (start or end)) * 1000
    busy = sum(sum(v) for v in excl.values())
    print(f"window {wall/1000:.1f} s, GUI spans busy {busy/1000:.1f} s ({100*busy/max(wall,1):.0f}%)")
    print(f"{'span (exclusive)':26s} {'n':>6s} {'sum s':>7s} {'p50':>7s} {'p95':>7s} {'max':>7s}")
    for name in sorted(excl, key=lambda k: -sum(excl[k])):
        v = excl[name]
        print(f"{name:26s} {len(v):6d} {sum(v)/1000:7.2f} {pct(v,.5):7.2f} {pct(v,.95):7.2f} {max(v):7.1f}")
    for name in ("sched.read",):
        v = worker.get(name, [])
        if v:
            print(f"(worker) {name:17s} {len(v):6d} {sum(v)/1000:7.2f} {pct(v,.5):7.2f} {pct(v,.95):7.2f} {max(v):7.1f}")
    if mem:
        rows = [json.loads(l) for l in open(mem)]
        for key in ("rss_mib", "rss_main_mib", "gpu_main_mib", "gpu_tree_mib", "gpu_device_mib"):
            v = [r[key] for r in rows]
            print(f"memory {key:15s} peak {max(v):9.1f} MiB  (start {v[0]:.1f})")


if __name__ == "__main__":
    main(*sys.argv[1:3])
