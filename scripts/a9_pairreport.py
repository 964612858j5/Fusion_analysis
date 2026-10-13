"""§44 step 0 / R4 plan §2-§4: paired comparison per gesture over rounds,
A (ours) against B (Odon, or our frozen version), with censored values.

Per round and gesture:
  settle   the screenshot settle time (a9_shotdiff) -- or CENSORED when it
           did not settle in the window, has no screenshots, or its
           reference (the last shot) cannot be confirmed final. Primary
           rule, the same for both sides: no pixwatch change from the real
           capture of the 1500 ms shot to the next gesture's mark (`done`
           for the last), with heartbeats proving the watcher sampled the
           whole span (one at or before its start, one at or after its end,
           captures advancing, at most 1.5 s apart, no unreadable rows).
           Side evidence, ours: the in-app target COMPLETE before the last
           shot (a9_tiletl); Odon: its loading state idle, asked after the
           last shot's capture. Disagreements are listed by round.
  @50      the shot nearest 50 ms after the stop: differing share all /
           unsaturated / worst 64x64 cell, and that shot's real offset
  resp     each injection -> first observed change of the SAMPLED STRIPS
           (a9_pixwatch) before the next mark; no change: CENSORED
  complete / shown   our in-app target times (CENSORED when absent)
  channels the frames' channel count during the gesture: the expected
           count must be more than half of the gesture's frames (ours).

Runs: with --status DIR (written by the batch, one TAG.json each), a run
whose final status is "missing" (measurement failure, attempts exhausted)
drops its pair; a run without data otherwise (the product did not start,
crashed) is a product result: every gesture CENSORED. A gesture absent
from a run that has data is CENSORED too.

CENSORED sorts after every number. A median of an even count averages the
two middle values when both are numbers, else it is CENSORED; a
percentile landing on CENSORED is CENSORED. Comparing A with B: times
within one frame (16.7 ms) tie; counts compare exactly; one side
CENSORED is the slower; both CENSORED is inconclusive (R4 plan §2, §4).

Verdict per gesture:
  --mode odon (20 ch; 5 rounds, >= 4 pairs): settle median, response p95
      (pooled over the pairs) and the median per-run count of responses
      over 50 ms (CENSORED counts) each not worse than B;
  --mode final (3 ch; 3 rounds, >= 2 pairs): also the in-app completion
      median; the >50 ms counts summed over the pairs; relative changes;
  --mode stress (69 ch; 2 rounds): no verdict, p99 and maxima reported.
Fewer pairs than the minimum, any both-CENSORED comparison or a run with
the wrong channel count: inconclusive. A "not worse" that rests on B's
censored values is flagged per item with B's raw records (plan §2).

Usage: a9_pairreport.py BENCH_DIR ODON_DIR COND:A_PREFIX:B_PREFIX ...
         [--mode odon|final|stress] [--rounds N] [--min-pairs N]
         [--channels N] [--status DIR] [--json OUT]"""
import json
import math
import os
import re
import statistics
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LABELS = ["drag right", "drag down", "drag diag", "zoom in", "zoom out", "zoom back",
          "flick", "iso in", "iso out"]
CENS = math.inf
FRAME_MS = 16.7
REF_SHOT, LAST_SHOT = 1500, 2300
MODES = {"odon": (5, 4), "final": (3, 2), "stress": (2, 1)}


def fmt(v, nd=0):
    if v is None:
        return "-"
    return ">win" if v == CENS else f"{v:.{nd}f}"


def ordered_at(values, q):
    v = sorted(values)
    if not v:
        return None
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


def median(values):
    v = sorted(values)
    if not v:
        return None
    n = len(v)
    if n % 2:
        return v[n // 2]
    a, b = v[n // 2 - 1], v[n // 2]
    return CENS if CENS in (a, b) else (a + b) / 2.0


def compare(a, b, kind):
    """'better' / 'tie' / 'worse' / 'inconclusive' for A against B."""
    if a is None or b is None or (a == CENS and b == CENS):
        return "inconclusive"
    if a == CENS:
        return "worse"
    if b == CENS:
        return "better"
    if kind == "time":
        return "tie" if abs(a - b) <= FRAME_MS else ("better" if a < b else "worse")
    return "tie" if a == b else ("better" if a < b else "worse")


# ── one run ───────────────────────────────────────────────────────────
def read_marks(path):
    out = []
    for line in open(path):
        if "\t" in line:
            t, label = line.rstrip("\n").split("\t", 1)
            out.append((float(t), label[2:] if label.startswith("d ") else label))
    return out


def gesture_spans(marks):
    """label -> (its mark, the next mark); a gesture the run stopped in (no
    later mark) runs to the end of the records (math.inf)."""
    spans = {}
    for i, (t, label) in enumerate(marks):
        if label in LABELS:
            spans[label] = (t, marks[i + 1][0] if i + 1 < len(marks) else math.inf)
    return spans


def read_pix(path):
    changes, beats = [], []
    for line in open(path):
        if '"hb"' in line:
            r = json.loads(line)
            beats.append((r["hb"], r.get("n", 0), r.get("miss", 0)))
        elif '"t"' in line:
            changes.append(json.loads(line)["t"])
    return changes, beats


def covered(beats, start, end):
    """The watcher sampled the whole of [start, end]: the last heartbeat at
    or before `start` and every one up to the first at or after `end`, at
    most 1.5 s apart, capture counts advancing, no unreadable rows."""
    before = [i for i, b in enumerate(beats) if b[0] <= start]
    after = [i for i, b in enumerate(beats) if b[0] >= end]
    if not before or not after:
        return False
    span = beats[before[-1]:after[0] + 1]
    return (all(b[0] - a[0] <= 1.5 and b[1] > a[1] for a, b in zip(span, span[1:]))
            and all(b[2] == 0 for b in span[1:]))


def censored_gesture():
    """A gesture the run never reached: nothing was sent, so no responses;
    its settle and completion are CENSORED (a product result)."""
    return {"injections": 0, "unanswered": 0, "resp": [], "settle": CENS, "complete": CENS,
            "shown": CENS, "ref_primary": None, "ref_side": None, "channels_ok": None, "channels": None,
            "censored_gesture": True}


def run_data(kind, bench, odon, tag, channels, status_dir):
    status = None
    if status_dir:
        p = os.path.join(status_dir, f"{tag}.json")
        status = json.load(open(p)).get("final") if os.path.exists(p) else None
        if status == "missing":
            return None
    if kind == "ours":
        marks, inj = os.path.join(bench, f"marks_{tag}.tsv"), os.path.join(bench, f"run_{tag}.log.xwheel.jsonl")
        pix, shots = os.path.join(bench, f"pix_{tag}.jsonl"), os.path.join(bench, f"shots_{tag}")
        log = os.path.join(bench, f"run_{tag}.log")
    else:
        d = os.path.join(odon, tag)
        marks, inj, pix, shots = (os.path.join(d, "marks.tsv"), os.path.join(d, "part.jsonl"),
                                  os.path.join(d, "changes.jsonl"), os.path.join(d, "shots"))
        log = None
    if not all(os.path.exists(p) for p in (marks, inj, pix)):
        # with a batch status: the product never got there -- a result, not a gap
        if status in ("ok", "review"):
            return {"product_failure": True, "delivery": None,
                    "gestures": {label: censored_gesture() for label in LABELS}}
        return None
    spans = gesture_spans(read_marks(marks))
    injections = [json.loads(l) for l in open(inj) if l.strip()]
    changes, beats = read_pix(pix)
    diff_path = os.path.join(shots, "diff.json")
    if not os.path.exists(diff_path) and os.path.isdir(shots):
        subprocess.run([sys.executable, os.path.join(HERE, "a9_shotdiff.py"), shots, ",".join(LABELS)],
                       stdout=subprocess.DEVNULL, check=False)
    diffs = {g["label"]: g for g in json.load(open(diff_path))} if os.path.exists(diff_path) else {}
    shot_t = {}
    meta = os.path.join(shots, "shots.json")
    if os.path.exists(meta):
        for s in json.load(open(meta)):
            shot_t[(s["g"], s["ms"])] = s["t"]
    inapp, frames = {}, []
    if kind == "ours":
        trace = os.path.join(bench, f"tiles_{tag}.jsonl")
        out = os.path.join(bench, f"tiletl_{tag}.json")
        if os.path.exists(trace) and not os.path.exists(out):
            subprocess.run([sys.executable, os.path.join(HERE, "a9_tiletl.py"), trace, inj, ",".join(LABELS),
                            "--perf", log, "--json", out], stdout=subprocess.DEVNULL, check=False)
        if os.path.exists(out):
            inapp = {g["label"]: g for g in json.load(open(out))}
        for line in open(log):
            if "ev=gpu.frame" in line:
                m = re.search(r"t=([\d.]+) .*?channels=(\d+)", line)
                b = re.search(r"backend=(\w+)", line)
                if m:
                    frames.append((float(m.group(1)), int(m.group(2)), b.group(1) if b else "python"))
    loading = {}
    lpath = os.path.join(odon, tag, "loading.jsonl") if kind == "odon" else None
    if lpath and os.path.exists(lpath):
        for l in open(lpath):
            if l.strip():
                r = json.loads(l)
                lab = r["label"]
                loading[lab[2:] if lab.startswith("d ") else lab] = r
    complete_run = all(l in spans for l in LABELS) and all(t1 != math.inf for _t0, t1 in spans.values())
    out = {"product_failure": not complete_run, "gestures": {},
           "delivery": {"off_target": sum(1 for i in injections if i.get("on_target") is False),
                        "x_errors": max([i.get("x_errors", 0) for i in injections] or [0]),
                        "errors": os.path.exists(inj + ".err")}}
    for gi, label in enumerate(LABELS, 1):
        if label not in spans:
            out["gestures"][label] = censored_gesture()         # the run stopped before it
            continue
        t0, t1 = spans[label]
        mine = [i for i in injections if t0 <= i["t"] < t1]
        resp = []
        for i in mine:
            nxt = next((c for c in changes if c >= i["t"]), None)
            resp.append((nxt - i["t"]) * 1000.0 if nxt is not None and nxt < t1 else CENS)
        g = {"injections": len(mine), "unanswered": sum(r == CENS for r in resp), "resp": resp,
             "unanswered_injections": [{"seq": i.get("seq"), "t": i["t"]}
                                       for i, r in zip(mine, resp) if r == CENS],
             "records": {"injections": inj, "changes": pix, "shots": shots,
                         "loading": lpath, "log": log}}
        dg = diffs.get(label)
        settle = CENS if dg is None or dg["settle_ms"] is None else dg["settle_ms"]
        ref0, ref1 = shot_t.get((gi, REF_SHOT)), shot_t.get((gi, LAST_SHOT))
        g["stopped_in"] = t1 == math.inf
        primary = (ref0 is not None and t1 != math.inf and covered(beats, ref0, t1)
                   and not any(ref0 <= c < t1 for c in changes))
        if kind == "ours":
            ia = inapp.get(label) or {}
            comp, shown = ia.get("complete_ms"), ia.get("shown_ms")
            side = comp is not None and ref1 is not None and ia.get("stop", 0) + comp / 1000.0 <= ref1
            g["complete"] = CENS if comp is None else comp
            g["shown"] = CENS if shown is None else shown
            counts = {}
            for f in (f for f in frames if t0 <= f[0] < t1):
                counts[f[1]] = counts.get(f[1], 0) + 1
            total = sum(counts.values())
            g["channels"] = {"frames": total, "expected_share": None if not total or channels is None
                             else round(counts.get(channels, 0) / total, 3),
                             "backends": sorted({f[2] for f in frames if t0 <= f[0] < t1})}
            g["channels_ok"] = (channels is None
                                or (total > 0 and counts.get(channels, 0) * 2 > total))
        else:
            rec = loading.get(label)
            side = (rec is not None and ref1 is not None and rec.get("query_start", -1) >= ref1
                    and rec.get("state") is not None and "true" not in json.dumps(rec["state"]).lower())
            g["channels_ok"] = None                    # Odon's channels are set through its API
        g["ref_primary"], g["ref_side"] = primary, side
        g["settle"] = settle if (primary and side) else CENS
        if dg is not None and dg["shots"]:
            best = min(dg["shots"], key=lambda r: abs(r["real_ms"] - 50.0))
            g["at50"] = [best["all"], best["uns"], best["cell"], best["real_ms"]]
        out["gestures"][label] = g
    return out


# ── pairs ─────────────────────────────────────────────────────────────
def side_stats(gs, rounds):
    resp = [v for g in gs for v in g["resp"]]
    over = [sum(1 for v in g["resp"] if v > 50) for g in gs]
    has_comp = any("complete" in g for g in gs)
    return {"settle": dict(zip(rounds, [g["settle"] for g in gs])),
            "settle_med": median([g["settle"] for g in gs]),
            "resp_p50": median(resp), "resp_p95": ordered_at(resp, .95), "resp_p99": ordered_at(resp, .99),
            "resp_max": ordered_at(resp, 1.0), "over50": dict(zip(rounds, over)),
            "over50_med": median(over), "over50_sum": sum(over),
            "unanswered": dict(zip(rounds, [g["unanswered"] for g in gs])),
            "injections": sum(g["injections"] for g in gs),
            "at50": [g.get("at50") for g in gs],
            "complete_med": median([g.get("complete", CENS) for g in gs]) if has_comp else None,
            "shown_med": median([g.get("shown", CENS) for g in gs]) if has_comp else None,
            "ref_disagree": [r for r, g in zip(rounds, gs) if g["ref_primary"] != g["ref_side"]],
            "channels_bad": [r for r, g in zip(rounds, gs) if g.get("channels_ok") is False],
            "channels": dict(zip(rounds, [g.get("channels") for g in gs]))}


def raw_b(pairs, what):
    """B's raw records behind a censored value, by round: the files and,
    for unanswered injections, which ones (seq, t)."""
    out = {}
    for r, _x, y in pairs:
        if what == "settle" and y["settle"] == CENS:
            out[r] = {"ref_primary": y["ref_primary"], "ref_side": y["ref_side"],
                      "stopped_in": y.get("stopped_in"), "files": y.get("records")}
        elif what == "unanswered" and y["unanswered"]:
            out[r] = {"unanswered_injections": y["unanswered_injections"], "files": y.get("records")}
    return out


def summarise(runs_a, runs_b, label, mode, min_pairs):
    pairs = [(r, a["gestures"][label], b["gestures"][label])
             for r, (a, b) in enumerate(zip(runs_a, runs_b), 1) if a is not None and b is not None]
    rounds = [p[0] for p in pairs]
    a, b = side_stats([p[1] for p in pairs], rounds), side_stats([p[2] for p in pairs], rounds)
    per_round = {r: compare(x["settle"], y["settle"], "time") for r, x, y in pairs}
    res = {"pairs": rounds, "A": a, "B": b, "settle_per_round": per_round}
    if mode == "stress":
        res.update(checks={}, verdict="report only", leans_on_B=[])
        return res
    checks = {"settle": compare(a["settle_med"], b["settle_med"], "time"),
              "resp_p95": compare(a["resp_p95"], b["resp_p95"], "time")}
    if mode == "odon":
        checks["over50"] = compare(a["over50_med"], b["over50_med"], "count")
    else:
        checks["over50"] = compare(a["over50_sum"], b["over50_sum"], "count")
        checks["complete"] = compare(a["complete_med"], b["complete_med"], "time")
        res["relative"] = {k: (None if a[f] in (None, CENS) or b[f] in (None, CENS) or not b[f]
                               else round((a[f] - b[f]) / b[f] * 100.0))
                           for k, f in (("settle", "settle_med"), ("complete", "complete_med"))}
    bad_channels = a["channels_bad"] or b["channels_bad"]
    if len(pairs) < min_pairs or "inconclusive" in checks.values() or bad_channels:
        verdict = "inconclusive"
    elif "worse" in checks.values():
        verdict = "worse"
    else:
        verdict = "not worse"
    leans = []
    if verdict == "not worse":
        if checks["settle"] == "better" and b["settle_med"] == CENS:
            leans.append({"item": "settle", "B_settle_by_round": b["settle"],
                          "B_records": raw_b(pairs, "settle")})
        if checks["resp_p95"] == "better" and b["resp_p95"] == CENS:
            leans.append({"item": "resp_p95", "B_unanswered_by_round": b["unanswered"],
                          "B_records": raw_b(pairs, "unanswered")})
        if sum(b["unanswered"].values()) and checks["over50"] in ("better", "tie"):
            leans.append({"item": "over50", "B_unanswered_by_round": b["unanswered"],
                          "B_over50_by_round": b["over50"], "B_records": raw_b(pairs, "unanswered")})
    res.update(checks=checks, verdict=verdict, leans_on_B=leans,
               invalid_channels={"A": a["channels_bad"], "B": b["channels_bad"]} if bad_channels else None)
    return res


def main(bench, odon, *specs):
    args = list(specs)

    def opt(name, default, cast=str):
        if name in args:
            j = args.index(name)
            v = cast(args[j + 1])
            del args[j:j + 2]
            return v
        return default
    mode = opt("--mode", "odon")
    rounds, min_pairs = opt("--rounds", MODES[mode][0], int), opt("--min-pairs", MODES[mode][1], int)
    channels, status_dir, out_json = opt("--channels", None, int), opt("--status", None), opt("--json", None)
    report = {}
    for spec in args:
        cond, a_p, b_p = spec.split(":")
        b_kind = "odon" if mode == "odon" else "ours"
        runs_a = [run_data("ours", bench, odon, f"{a_p}_{r}", channels, status_dir) for r in range(1, rounds + 1)]
        runs_b = [run_data(b_kind, bench, odon, f"{b_p}_{r}", channels if b_kind == "ours" else None, status_dir)
                  for r in range(1, rounds + 1)]
        rep = {"mode": mode, "missing_A": [r for r, x in enumerate(runs_a, 1) if x is None],
               "missing_B": [r for r, x in enumerate(runs_b, 1) if x is None],
               "product_failure_A": [r for r, x in enumerate(runs_a, 1) if x and x["product_failure"]],
               "product_failure_B": [r for r, x in enumerate(runs_b, 1) if x and x["product_failure"]],
               "delivery_A": [x and x["delivery"] for x in runs_a],
               "delivery_B": [x and x["delivery"] for x in runs_b], "gestures": {}}
        report[cond] = rep
        print(f"\n=== {cond} ({mode}): A={a_p} B={b_p}; missing A {rep['missing_A']} B {rep['missing_B']};"
              f" product failure A {rep['product_failure_A']} B {rep['product_failure_B']}")
        print("    resp = injection -> first change of the SAMPLED STRIPS (a9_pixwatch); "
              "settle = screenshot settle (>win = not settled / reference not confirmed final)")
        for label in LABELS:
            s = summarise(runs_a, runs_b, label, mode, min_pairs)
            rep["gestures"][label] = s
            wl = " ".join(f"r{r}:{v}" for r, v in s["settle_per_round"].items())
            extra = (f" leans on B censored: {[x['item'] for x in s['leans_on_B']]}" if s["leans_on_B"] else "")
            extra += f" invalid channels {s['invalid_channels']}" if s.get("invalid_channels") else ""
            extra += f" relative % {s['relative']}" if s.get("relative") else ""
            print(f"{label:10s} pairs {s['pairs']} -> {s['verdict']}  {s['checks']}{extra}")
            print(f"           settle per round (A vs B): {wl}")
            for lean in s["leans_on_B"]:
                for r, recd in lean["B_records"].items():
                    print(f"           B censored record ({lean['item']}) r{r}: {json.dumps(recd, default=str)}")
            for who in ("A", "B"):
                d = s[who]
                at = [x for x in d["at50"] if x]
                a50 = ("/".join(f"{statistics.median([x[i] for x in at]):.1f}" for i in range(3))
                       + f" @{statistics.median([x[3] for x in at]):.0f}ms") if at else "-"
                comp = (f" | complete {fmt(d['complete_med'], 1)} shown {fmt(d['shown_med'], 1)}"
                        if d["complete_med"] is not None else "")
                p99 = f"/p99 {fmt(d['resp_p99'], 1)}" if mode == "stress" else ""
                print(f"   {who} settle {fmt(d['settle_med']):>5s} ({' '.join(fmt(v) for v in d['settle'].values())})"
                      f" | resp {fmt(d['resp_p50'], 1)}/{fmt(d['resp_p95'], 1)}{p99}/max {fmt(d['resp_max'])}"
                      f" >50 {list(d['over50'].values())} unanswered {list(d['unanswered'].values())}"
                      f"/{d['injections']} | @50 {a50}{comp}"
                      + (f" | ref disagree rounds {d['ref_disagree']}" if d["ref_disagree"] else ""))
    if out_json:
        def clean(v):
            if isinstance(v, float) and v == CENS:
                return "censored"
            if isinstance(v, dict):
                return {str(k): clean(x) for k, x in v.items()}
            if isinstance(v, (list, tuple)):
                return [clean(x) for x in v]
            return v
        with open(out_json, "w") as f:
            json.dump(clean(report), f, indent=1)


if __name__ == "__main__":
    main(*sys.argv[1:])
