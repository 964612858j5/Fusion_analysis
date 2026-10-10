"""How long after the hand stops the picture keeps changing (block A9
§41): per gesture, last injection -> last visible change before the next
gesture (the sharpening that follows the motion). Median and max over
gestures. Usage: a9_settle.py MARKS INJECTIONS CHANGES"""
import json
import sys


def main(marks_path, inj_path, ch_path):
    marks = [(float(l.split("\t")[0]), l.rstrip("\n").split("\t")[1]) for l in open(marks_path)]
    inj = [json.loads(l)["t"] for l in open(inj_path) if l.strip()]
    ch = [json.loads(l)["t"] for l in open(ch_path) if '"t"' in l]
    out = []
    for i, (t0, label) in enumerate(marks[:-1]):
        t1 = marks[i + 1][0]
        ts = [t for t in inj if t0 <= t < t1]
        if not ts:
            continue
        last = ts[-1]
        after = [c for c in ch if last < c < t1]
        out.append(((after[-1] - last) * 1000.0 if after else 0.0, label))
    v = sorted(x for x, _ in out)
    print(f"settle after the last input: median {v[len(v) // 2]:.0f} ms, max {v[-1]:.0f} ms  "
          + " ".join(f"{l.split()[-1] if l.startswith('x') else l[:6]}={x:.0f}" for x, l in out))


if __name__ == "__main__":
    main(*sys.argv[1:4])
