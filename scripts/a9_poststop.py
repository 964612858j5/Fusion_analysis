"""Is the switch to the sharp picture after the hand stops noticeable?
(block A9 §41). Per gesture: after the last input's own response (the first
change after it), every further change is the picture still settling --
sharper tiles arriving. Reports how much of the sampled picture changed
after that (the sum of per-change shares; 0 = nothing moved, 1 = every
sampled pixel once) and when the last of it happened.
Usage: a9_poststop.py MARKS INJECTIONS CHANGES"""
import json
import sys


def main(marks_path, inj_path, ch_path):
    marks = [(float(l.split("\t")[0]), l.rstrip("\n").split("\t")[1]) for l in open(marks_path)]
    inj = [json.loads(l)["t"] for l in open(inj_path) if l.strip()]
    ch = [json.loads(l) for l in open(ch_path) if '"d"' in l]
    rows = []
    for i, (t0, label) in enumerate(marks[:-1]):
        t1 = marks[i + 1][0]
        ts = [t for t in inj if t0 <= t < t1]
        if not ts:
            continue
        last = ts[-1]
        after = [c for c in ch if last < c["t"] < t1]
        if not after:
            continue
        settle = after[1:]                       # after the last input's own frame
        amount = sum(c["d"] for c in settle)
        when = (settle[-1]["t"] - last) * 1000.0 if settle else 0.0
        rows.append((label, amount, when, len(settle)))
    for label, amount, when, n in rows:
        print(f"  {label[:16]:16s} changed after stop {amount:6.3f} in {n:3d} frames, last at {when:6.0f} ms")
    amounts = sorted(a for _l, a, _w, _n in rows)
    print(f"POST-STOP change: median {amounts[len(amounts) // 2]:.3f}, max {amounts[-1]:.3f}, "
          f"total {sum(amounts):.3f}")


if __name__ == "__main__":
    main(*sys.argv[1:4])
