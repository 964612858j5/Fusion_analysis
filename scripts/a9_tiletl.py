"""§44 step 0: where does each tile's time go, per gesture? Reads the
in-app tile trace (BLOCK01_A9_TILETRACE, utils/tile_trace.py) and the
injector log (gestures = runs starting at seq == 1; the stop = a run's last
injection).

Per gesture:
  target   when the FINAL target (level + viewport) had all its admitted
           tiles of the drawn channels on the GPU and stayed so (complete_ms
           after the stop; the in-app check that the reference picture is
           really the target level), and the first frame presented after
           that (shown_ms)
  tiles    fine tile READS inside the gesture's start .. stop+WINDOW, each
           paired with its own attempt (the latest request before it was
           started; a failed read or a delivery rejected as stale ends the
           attempt): queue wait (req->start), read wall / CPU, the reader's
           split (pread, decode, copy, wrap), delivery (read->recv on the GUI
           thread), upload wait (recv->gpu), gpu->next present; cache hits
           (a request applied without a read) and re-requests are counted
  rate     tiles read per ms between the first request and the last read
  issue    from a change of the target level to the last request for it
  resp     in-app response: injection -> Qt received it -> the first frame
           PAINTED after that -> its present (p50/p95, max, > 33 / > 50 ms;
           an injection with no such frame counts as unanswered). Not
           affected by an outside observer, so runs with and without one
           compare
  stall    with --perf PERF_LOG: GUI-thread stalls (gui.gap > 33 ms) during
           the gesture and in the 400 ms after the stop

Usage: a9_tiletl.py TILETRACE_JSONL INJECTIONS_JSONL [LABELS_COMMA]
                    [--perf PERF_LOG] [--json OUT]"""
import json
import sys

WINDOW_S = 2.3


def pct(values, q):
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(round(q / 100.0 * (len(v) - 1))))]


def fmt(values, scale=1.0):
    if not values:
        return "-"
    return f"{pct(values, 50) * scale:.1f}/{pct(values, 95) * scale:.1f}"


def gestures(injections):
    runs = []
    for r in injections:
        if r.get("seq") == 1:
            runs.append([])
        if runs:
            runs[-1].append(r["t"])
    return [(run[0], run[-1]) for run in runs if run]


def gaps(perf_path):
    out = []
    if not perf_path:
        return out
    for line in open(perf_path):
        if "ev=gui.gap " not in line:
            continue
        f = dict(p.split("=", 1) for p in line.split()[1:] if "=" in p)
        out.append((float(f["t_begin"]), float(f["t"]), float(f["gap_ms"])))
    return out


def attempts(events):
    """One record per read attempt of a key, in time order."""
    out, req, cur = [], None, None
    for e in events:
        s = e["s"]
        if s == "req":
            req = e                       # a read already started keeps its own
            continue
        if s == "start":
            cur = {"req": req, "start": e}
            req = None
            out.append(cur)
        elif cur is None:
            if s == "recv" and e.get("ok") == 1 and req is not None:
                out.append({"req": req, "hit": e})       # answered from the cache
                req = None
            continue
        elif s == "read":
            cur["read"] = e
            if e.get("err"):
                cur = None
        elif s == "recv":
            # one read can answer a stale waiter (ok=0) and the current one:
            # only an accepted delivery moves the attempt on
            if e.get("ok") == 1 and "read" in cur and "recv" not in cur:
                cur["recv"] = e
        elif s == "gpu" and "recv" in cur:
            cur["gpu"] = e
            cur = None
    return out


def consumed_at(injected, receipts, start, end):
    """For each injection, when Qt received an event that includes it.

    A notch is one wheel event: the n-th notch is the n-th wheel receipt of
    the gesture. Moves can be coalesced: a receipt at the position of
    injection j covers every injection up to j, so injection k is consumed
    by the first receipt (in time) that covers an index >= k. Never by an
    earlier receipt that happens to come after it in time but stands for an
    earlier injection (a queue, not a coalesce)."""
    out = [None] * len(injected)
    notches = [k for k, r in enumerate(injected) if r["kind"] == "notch"]
    got = [r for r in receipts if r["kind"] == "notch" and start <= r["t"] <= end]
    for n, k in enumerate(notches):
        if n < len(got) and got[n]["t"] >= injected[k]["t"]:
            out[k] = got[n]["t"]
    moves = [k for k, r in enumerate(injected) if r["kind"] == "move"]
    where = {}
    for i, k in enumerate(moves):
        xy = injected[k].get("xy")
        if xy is not None:
            where[tuple(xy)] = i                    # the latest move to that point
    covered, i_next = -1, 0
    for r in (r for r in receipts if r["kind"] == "move" and start <= r["t"] <= end):
        j = where.get(tuple(r.get("xy") or ()))
        if j is None:
            continue
        covered = max(covered, j)
        while i_next <= covered and i_next < len(moves):
            if r["t"] >= injected[moves[i_next]]["t"]:
                out[moves[i_next]] = r["t"]
            i_next += 1
    return out


def main(trace_path, inj_path, labels="", json_out=None, perf_path=None):
    rows = [json.loads(l) for l in open(trace_path) if l.strip()]
    rows.sort(key=lambda r: r["t"])
    injections = [json.loads(l) for l in open(inj_path) if l.strip()]
    stalls = gaps(perf_path)
    names = labels.split(",") if labels else []
    presents = [r["t"] for r in rows if r["s"] == "present"]
    paints = [r["t"] for r in rows if r["s"] == "paint"]
    inputs = {"notch": [], "move": []}
    input_rows = [r for r in rows if r["s"] == "input"]
    covs = [r for r in rows if r["s"] == "cov"]
    per_key = {}
    for r in rows:
        if "k" in r:
            per_key.setdefault(tuple(r["k"]), []).append(r)
    all_attempts = []
    for key, events in per_key.items():
        for a in attempts(events):
            if a.get("req") is not None and a["req"].get("tier") == "fine":
                all_attempts.append((key, a))
    report = []
    for i, (start, stop) in enumerate(gestures(injections)):
        label = names[i] if i < len(names) else f"g{i + 1}"
        end = stop + WINDOW_S
        # the final target: the last coverage record inside the window
        window = [c for c in covs if c["t"] <= end]
        final = window[-1] if window else None
        complete = None
        if final is not None and final["wanted"] and final["shown"] == final["wanted"]:
            complete = final["t"]
            for c in reversed(window[:-1]):
                same = (c["level"], c.get("vp")) == (final["level"], final.get("vp"))
                if same and c["wanted"] and c["shown"] == c["wanted"]:
                    complete = c["t"]
                else:
                    break
            complete = max(complete, stop)
        shown = next((p for p in presents if complete is not None and p >= complete), None)
        waits, walls, cpus, delivers, uploads, onscreen = [], [], [], [], [], []
        split = {"pread_ms": [], "decode_ms": [], "copy_ms": [], "wrap_ms": []}
        first_req, last_read, reads, hits, requests = None, None, 0, 0, 0
        for key, a in all_attempts:
            if "hit" in a:
                if start <= a["hit"]["t"] <= end:
                    hits += 1
                continue
            if "read" not in a or not (start <= a["read"]["t"] <= end):
                continue
            req = a["req"]["t"]
            first_req = req if first_req is None else min(first_req, req)
            rd = a["read"]
            reads += 1
            last_read = rd["t"] if last_read is None else max(last_read, rd["t"])
            waits.append((a["start"]["t"] - req) * 1000.0)
            walls.append(rd.get("wall_ms", 0.0))
            cpus.append(rd.get("cpu_ms", 0.0))
            for name in split:
                if name in rd:
                    split[name].append(rd[name])
            if "recv" in a:
                delivers.append((a["recv"]["t"] - rd["t"]) * 1000.0)
                if "gpu" in a and a["gpu"]["t"] <= end:   # else not uploaded in time
                    uploads.append((a["gpu"]["t"] - a["recv"]["t"]) * 1000.0)
                    nxt = next((p for p in presents if p >= a["gpu"]["t"]), None)
                    if nxt is not None:
                        onscreen.append((nxt - a["gpu"]["t"]) * 1000.0)
        for key, events in per_key.items():
            requests += sum(1 for e in events if e["s"] == "req" and start <= e["t"] <= end
                            and e.get("tier") == "fine")
        rate = (reads / ((last_read - first_req) * 1000.0)
                if reads and last_read and last_read > first_req else None)
        # request issue span after each change of the target level
        issue = []
        before = [c for c in covs if c["t"] <= start]
        level = before[-1]["level"] if before else None
        for c in [c for c in covs if start <= c["t"] <= end]:
            if c["level"] != level and c["level"] is not None:
                reqs = [e["t"] for key, events in per_key.items() if key[1] == c["level"]
                        for e in events if e["s"] == "req" and c["t"] - 0.05 <= e["t"] <= end]
                if reqs:
                    issue.append(round((max(reqs) - min(reqs)) * 1000.0, 1))
            level = c["level"]
        resp, unanswered = [], 0
        injected = [r for r in injections if start <= r["t"] <= stop and r.get("kind") in inputs]
        consumed = consumed_at(injected, input_rows, start, end)
        for k, r in enumerate(injected):
            got = consumed[k]
            paint = next((p for p in paints if got is not None and p >= got), None)
            shown_at = next((p for p in presents if paint is not None and p >= paint), None)
            if shown_at is None or shown_at > end:
                unanswered += 1
            else:
                resp.append((shown_at - r["t"]) * 1000.0)

        def stalled(a, b):
            return [g for t0, t1, g in stalls if t1 >= a and t0 <= b and g > 33.0]
        during, settle = stalled(start, stop), stalled(stop, stop + 0.4)
        item = {
            "label": label, "stop": stop,
            "target_level": None if final is None else final["level"],
            "wanted": None if final is None else final["wanted"],
            "complete_ms": None if complete is None else round((complete - stop) * 1000.0, 1),
            "shown_ms": None if shown is None else round((shown - stop) * 1000.0, 1),
            "tiles": reads, "requests": requests, "cache_hits": hits,
            "rate_per_ms": None if rate is None else round(rate, 3),
            "wait": fmt(waits), "wall": fmt(walls), "cpu": fmt(cpus),
            "split": {k: fmt(v) for k, v in split.items()},
            "deliver": fmt(delivers), "upload": fmt(uploads), "onscreen": fmt(onscreen),
            "issue_ms": issue,
            "resp": [fmt(resp), round(max(resp, default=0), 1), sum(d > 33 for d in resp),
                     sum(d > 50 for d in resp), len(resp), unanswered],
            "stall_during": [round(g, 1) for g in during],
            "stall_after": [round(g, 1) for g in settle],
        }
        report.append(item)
        print(f"{label[:14]:14s} lvl {item['target_level']} want {item['wanted']} "
              f"complete {item['complete_ms']} shown {item['shown_ms']} ms | "
              f"reads {reads} req {requests} hits {hits} rate {item['rate_per_ms']}/ms\n"
              f"    wait {item['wait']} wall {item['wall']} cpu {item['cpu']} "
              f"pread {item['split']['pread_ms']} dec {item['split']['decode_ms']} "
              f"copy {item['split']['copy_ms']} wrap {item['split']['wrap_ms']} | "
              f"deliver {item['deliver']} upload {item['upload']} screen {item['onscreen']}\n"
              f"    issue {issue} | resp {item['resp'][0]} max {item['resp'][1]} >33 {item['resp'][2]} "
              f">50 {item['resp'][3]} n {item['resp'][4]} unanswered {unanswered} | "
              f"stall in {item['stall_during']} after {item['stall_after']}")
    if json_out:
        with open(json_out, "w") as f:
            json.dump(report, f, indent=1)


if __name__ == "__main__":
    args = sys.argv[1:]
    opts = {}
    for flag, name in (("--json", "json_out"), ("--perf", "perf_path")):
        if flag in args:
            j = args.index(flag)
            opts[name] = args[j + 1]
            del args[j:j + 2]
    main(*args, **opts)
