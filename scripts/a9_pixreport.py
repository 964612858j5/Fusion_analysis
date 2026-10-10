"""App-neutral smoothness report (block A9 §39) from a pixwatch record.

Per gesture (marks: `t<TAB>label`, the next mark ends it):
  * resp: injection -> first visible change after it (ms). For gestures
    whose notches are >= 300 ms apart this is the true response time;
    inside fast bursts it is optimistic (a change may be from the
    previous notch).
  * how many notches waited more than 33 ms (two frames at 60 Hz) and
    more than 50 ms for the picture to change.
Usage: a9_pixreport.py MARKS INJECTIONS_JSONL CHANGES_JSONL
"""
import json
import sys


def pct(v, q):
    if not v:
        return float("nan")
    v = sorted(v)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


def main(marks_path, inj_path, ch_path):
    marks = [(float(l.split("\t")[0]), l.rstrip("\n").split("\t")[1]) for l in open(marks_path)]
    inj = [json.loads(l)["t"] for l in open(inj_path) if l.strip()]
    ch = [json.loads(l)["t"] for l in open(ch_path) if '"t"' in l]
    print(f"{'gesture':16s} {'n':>4s} | resp p50/p95/max ms   | >33ms >50ms")
    all_resp, all_iso, all_burst, n33, n50 = [], [], [], 0, 0
    for i, (t0, label) in enumerate(marks[:-1]):
        t1 = marks[i + 1][0]
        ts = [t for t in inj if t0 <= t < t1]
        if not ts:
            continue
        resp = []
        for t in ts:
            nxt = next((c for c in ch if c >= t), None)
            if nxt is not None and nxt < t1:
                resp.append((nxt - t) * 1000)
        g33 = sum(r > 33 for r in resp)
        g50 = sum(r > 50 for r in resp)
        spaced = len(ts) > 1 and min(b - a for a, b in zip(ts, ts[1:])) >= 0.3
        all_resp += resp
        if spaced or "slow" in label:
            all_iso += resp
        if not (spaced or "slow" in label):
            all_burst += resp
            n33 += g33
            n50 += g50
        print(f"{label[:16]:16s} {len(ts):4d} | {pct(resp,.5):6.1f} {pct(resp,.95):6.1f} {max(resp or [0]):7.1f} |"
              f" {g33:5d} {g50:5d}")
    print(f"SPACED notches (slow/iso) resp p50 {pct(all_iso,.5):.1f} p95 {pct(all_iso,.95):.1f} "
          f"max {max(all_iso or [0]):.1f} (n={len(all_iso)})")
    print(f"FAST gestures (bursts, flicks, drags) resp p50 {pct(all_burst,.5):.1f} p95 {pct(all_burst,.95):.1f} "
          f"p99 {pct(all_burst,.99):.1f} max {max(all_burst or [0]):.1f} (n={len(all_burst)}); "
          f">33 ms: {n33}, >50 ms: {n50}")


if __name__ == "__main__":
    main(*sys.argv[1:4])
