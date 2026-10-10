"""§44 step 0: paired comparison, ours vs Odon, per gesture, over rounds
(independent review: A/B alternated, order swapped; compare medians, p95
and severe long frames gesture by gesture; outliers reported, not averaged).

For every condition (e.g. cold / warm) and gesture:
  settle   first real time after the stop from which the picture stays
           within the shot limits (a9_shotdiff), per round, and its median
  @50      differing share (all / unsaturated / worst cell) at the shot
           nearest 50 ms after the real stop, median over rounds
  resp     injection -> first visible change (a9_pixwatch), pooled over
           rounds: p50 / p95 / max, and > 50 ms per round
  wins     rounds where ours settled no later than Odon
and for ours the in-app checks (a9_tiletl): target complete / shown after
the stop, median over rounds. Odon's loading state at its reference shot
is listed when it was still loading (that reference is then not final).

Usage: a9_pairreport.py BENCH_DIR ODON_DIR COND:OURS_PREFIX:ODON_PREFIX ... --rounds N
  e.g. a9_pairreport.py bench_a9 odon_bench cold:b20c:q20c warm:b20w:q20w --rounds 5"""
import json
import os
import statistics
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LABELS = ["drag right", "drag down", "drag diag", "zoom in", "zoom out", "zoom back",
          "flick", "iso in", "iso out"]


def pct(v, q):
    if not v:
        return None
    v = sorted(v)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


def med(v):
    v = [x for x in v if x is not None]
    return statistics.median(v) if v else None


def responses(marks_path, inj_path, ch_path):
    marks = [(float(l.split("\t")[0]), l.rstrip("\n").split("\t")[1]) for l in open(marks_path)]
    inj = [json.loads(l)["t"] for l in open(inj_path) if l.strip()]
    ch = [json.loads(l)["t"] for l in open(ch_path) if '"t"' in l]
    out = {}
    for i, (t0, label) in enumerate(marks[:-1]):
        t1 = marks[i + 1][0]
        resp = []
        for t in (t for t in inj if t0 <= t < t1):
            nxt = next((c for c in ch if c >= t), None)
            if nxt is not None and nxt < t1:
                resp.append((nxt - t) * 1000.0)
        out[label.replace("d ", "", 1)] = resp
    return out


def diffs(shots_dir):
    path = os.path.join(shots_dir, "diff.json")
    if not os.path.exists(path):
        subprocess.run([sys.executable, os.path.join(HERE, "a9_shotdiff.py"), shots_dir,
                        ",".join(LABELS)], stdout=subprocess.DEVNULL, check=False)
    if not os.path.exists(path):
        return {}
    return {g["label"]: g for g in json.load(open(path))}


def at50(gesture):
    rows = gesture["shots"]
    best = min(rows, key=lambda r: abs(r["real_ms"] - 50.0))
    return best["all"], best["uns"], best["cell"]


def run_data(kind, bench, odon, tag):
    if kind == "ours":
        marks = os.path.join(bench, f"marks_{tag}.tsv")
        inj = os.path.join(bench, f"run_{tag}.log.xwheel.jsonl")
        ch = os.path.join(bench, f"pix_{tag}.jsonl")
        shots = os.path.join(bench, f"shots_{tag}")
    else:
        d = os.path.join(odon, tag)
        marks, inj, ch, shots = (os.path.join(d, "marks.tsv"), os.path.join(d, "part.jsonl"),
                                 os.path.join(d, "changes.jsonl"), os.path.join(d, "shots"))
    if not all(os.path.exists(p) for p in (marks, inj, ch)):
        return None
    data = {"resp": responses(marks, inj, ch), "diff": diffs(shots)}
    if kind == "ours":
        trace = os.path.join(bench, f"tiles_{tag}.jsonl")
        if os.path.exists(trace):
            out = os.path.join(bench, f"tiletl_{tag}.json")
            subprocess.run([sys.executable, os.path.join(HERE, "a9_tiletl.py"), trace, inj,
                            ",".join(LABELS), "--perf", os.path.join(bench, f"run_{tag}.log"),
                            "--json", out], stdout=subprocess.DEVNULL, check=False)
            if os.path.exists(out):
                data["inapp"] = {g["label"]: g for g in json.load(open(out))}
    else:
        path = os.path.join(odon, tag, "loading.jsonl")
        if os.path.exists(path):
            data["loading"] = [json.loads(l) for l in open(path) if l.strip()]
    return data


def main(bench, odon, *specs):
    args = list(specs)
    rounds = 5
    if "--rounds" in args:
        j = args.index("--rounds")
        rounds = int(args[j + 1])
        del args[j:j + 2]
    for spec in args:
        cond, ours_p, odon_p = spec.split(":")
        runs = {"ours": [], "odon": []}
        for r in range(1, rounds + 1):
            runs["ours"].append(run_data("ours", bench, odon, f"{ours_p}_{r}"))
            runs["odon"].append(run_data("odon", bench, odon, f"{odon_p}_{r}"))
        print(f"\n=== {cond}: ours {sum(x is not None for x in runs['ours'])} runs, "
              f"Odon {sum(x is not None for x in runs['odon'])} runs")
        print(f"{'gesture':11s} | {'settle ms (med; per round)':38s} | {'@50 all/uns/cell':22s} |"
              f" {'resp p50/p95/max  >50/run':30s} | wins | in-app complete/shown")
        for label in LABELS:
            line = {}
            for who in ("ours", "odon"):
                settles, a50, resp, over = [], [], [], []
                for data in runs[who]:
                    if data is None:
                        continue
                    g = data["diff"].get(label)
                    if g is not None:
                        settles.append(g["settle_ms"])
                        a50.append(at50(g))
                    rs = data["resp"].get(label, [])
                    resp += rs
                    over.append(sum(x > 50 for x in rs))
                line[who] = (settles, a50, resp, over)
            wins = sum(1 for a, b in zip(line["ours"][0], line["odon"][0])
                       if a is not None and (b is None or a <= b))
            inapp = [d["inapp"].get(label) for d in runs["ours"] if d and "inapp" in d]
            comp = med([g["complete_ms"] for g in inapp if g])
            shown = med([g["shown_ms"] for g in inapp if g])
            for who in ("ours", "odon"):
                settles, a50, resp, over = line[who]
                st = " ".join("-" if s is None else f"{s:.0f}" for s in settles)
                m = med(settles)
                a = ("/".join(f"{med([x[i] for x in a50]):.1f}" for i in range(3))) if a50 else "-"
                rp = (f"{pct(resp, .5):.1f}/{pct(resp, .95):.1f}/{max(resp):.0f}  "
                      f"{','.join(str(o) for o in over)}") if resp else "-"
                extra = (f"{wins}/{len(line['ours'][0])} | {comp}/{shown}" if who == "ours" else "")
                print(f"{(label if who == 'ours' else ''):11s} {who:4s}| "
                      f"{('-' if m is None else f'{m:.0f}'):>5s} ({st:28s}) | {a:22s} | {rp:30s} | {extra}")
        fast = {}
        for who in ("ours", "odon"):
            pooled, over = [], []
            for data in runs[who]:
                if data is None:
                    continue
                rs = [x for label in LABELS if not label.startswith("iso")
                      for x in data["resp"].get(label, [])]
                pooled += rs
                over.append(sum(x > 50 for x in rs))
            fast[who] = pooled
            if pooled:
                print(f"FAST {who}: p50 {pct(pooled, .5):.1f} p95 {pct(pooled, .95):.1f} "
                      f"p99 {pct(pooled, .99):.1f} max {max(pooled):.0f}; >50 per run {over}")
        busy = []
        for r, data in enumerate(runs["odon"], 1):
            for rec in (data or {}).get("loading", []):
                if "true" in json.dumps(rec.get("state")).lower():
                    busy.append(f"r{r}:{rec['label']}@{rec.get('since_stop_ms')}")
        print("Odon still loading at its reference shot:", ", ".join(busy) or "never")


if __name__ == "__main__":
    main(*sys.argv[1:])
