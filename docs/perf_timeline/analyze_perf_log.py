#!/usr/bin/env python3
"""Read a BLOCK01_PERF_LOG timeline and answer the questions by hand-free
arithmetic, not by eye.

    python analyze_perf_log.py perf1.log                  # the summary
    python analyze_perf_log.py perf1.log --gaps           # every GUI stall,
                                                          # attributed
    python analyze_perf_log.py perf1.log --at 1234.56     # what was running
                                                          # at one instant

Three things it exists to compute, because reading them off a log of
thousands of lines is how wrong conclusions get made:

  * WHAT HELD THE GUI THREAD. A `gui.gap` line says the event loop was
    unavailable for N ms and when the silence began. Every span carries its
    own begin (`t_begin`) and end (`t`), so the gap is attributed to the spans
    that CONTAIN it -- by interval, never by which line happens to sit next to
    it in the file.
  * WHAT WAS READING AT THE TIME. `job.begin`/`job.end` and
    `read.begin`/`read.end` are both-ended, so the set of jobs and reads open
    at any instant can be reconstructed exactly, including a read whose job
    was cancelled while it was still inside `read_region`.
  * HOW FAR BEHIND THE PICTURE WAS. `intensity.in`/`step1.mapping_in` carry an
    input revision and `step1.publish` carries the revision it drew, so the
    lag is a number of events, and the frames actually published during a drag
    can be counted.
"""

import argparse
import collections
import gzip
import sys


def runs(records):
    """Split the log at each `run.begin`.

    A new process restarts job ids and input revisions from 1, so pairing a
    `read.end` with a `read.begin` across that boundary invents intervals, and
    a revision lag computed across it is nonsense. Lines before the first
    `run.begin` are their own segment: a log from before this marker existed
    is still readable.
    """
    out, current = [], []
    for rec in records:
        if rec.get("ev") == "run.begin" and current:
            out.append(current)
            current = []
        current.append(rec)
    if current:
        out.append(current)
    return out


def read(path):
    opener = gzip.open if path.endswith(".gz") else open
    out = []
    with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line.startswith("PERF "):
                continue
            rec = {}
            for token in line.split():
                if "=" in token:
                    key, value = token.split("=", 1)
                    rec.setdefault(key, value)
            rec["_line"] = line
            out.append(rec)
    return out


def num(rec, key, default=None):
    try:
        return float(rec[key])
    except (KeyError, TypeError, ValueError):
        return default


def spans(records):
    """Every timed region as (begin, end, event, record).

    A span line is written when the region ENDS and carries `t_begin`, so one
    line is one interval -- no pairing, and no assumption that the log is in
    order.
    """
    out = []
    for rec in records:
        end = num(rec, "t")
        dur = num(rec, "dur_ms")
        if end is None or dur is None:
            continue
        # `t - dur_ms` is exact; `t_begin` was written through a `%.4g`
        # formatter in the first instrumented run, which turned 759147.127727
        # into 7.591e+05 and lost tens of seconds. So the arithmetic is the
        # authority and `t_begin` is only a cross-check -- used when it agrees
        # to a millisecond, ignored when it does not.
        computed = end - dur / 1000.0
        declared = num(rec, "t_begin")
        begin = (declared if declared is not None
                 and abs(declared - computed) < 0.001 else computed)
        out.append((begin, end, rec.get("ev", "?"), rec))
    out.sort()
    return out


def intervals(records, kind):
    """Both-ended background work: [(begin, end_or_None, fields)] per id.

    `kind` is "job" or "read". A begin with no end is an interval that was
    still open when the log stopped -- reported as such rather than dropped,
    because "a read that never returned" is a finding.
    """
    open_by_key = {}
    out = []
    for rec in records:
        ev = rec.get("ev", "")
        if ev == f"{kind}.begin":
            key = (rec.get("job"), rec.get("patch"), rec.get("channel"))
            open_by_key.setdefault(key, []).append(rec)
        elif ev == f"{kind}.end":
            key = (rec.get("job"), rec.get("patch"), rec.get("channel"))
            started = open_by_key.get(key)
            begin_rec = started.pop(0) if started else None
            begin = (num(begin_rec, "t") if begin_rec is not None
                     else num(rec, "t", 0.0) - (num(rec, "dur_ms", 0.0) / 1000.0))
            out.append((begin, num(rec, "t"), rec))
    for key, pending in open_by_key.items():
        for rec in pending:
            out.append((num(rec, "t"), None, rec))
    out.sort(key=lambda item: item[0] or 0.0)
    return out


def overlapping(items, begin, end):
    """Every interval that intersects [begin, end].

    An interval with no end is open until the log stops -- but it still only
    counts from where it STARTED, or a job that began after a stall would be
    reported as having been active during it.
    """
    hits = []
    for start, stop, rec in items:
        if start is None or start > end:
            continue
        if stop is None or stop >= begin:
            hits.append((start, stop, rec))
    return hits


def actives_at(records, when):
    jobs = overlapping(intervals(records, "job"), when, when)
    reads = overlapping(intervals(records, "read"), when, when)
    return jobs, reads


def pct(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(int(q * len(ordered)), len(ordered) - 1)]


def summarise(records):
    by_event = collections.defaultdict(list)
    for begin, end, ev, rec in spans(records):
        by_event[ev].append(num(rec, "dur_ms", 0.0))
    marks = collections.Counter(rec.get("ev") for rec in records
                                if "dur_ms" not in rec)

    print(f"lines: {len(records)}")
    runs = [rec for rec in records if rec.get("ev") == "run.begin"]
    for rec in runs:
        print(f"  run={rec.get('run')} pid={rec.get('pid')}")
    dropped = [rec for rec in records if rec.get("ev") == "perf.dropped"]
    if dropped:
        worst = max(int(float(rec.get("total", 0))) for rec in dropped)
        print(f"  DROPPED lines: {worst} (queue was full; timeline has holes)")
    print()
    print("timed regions, worst first by p99:")
    print("  %-34s %6s %8s %8s %8s %8s" % ("event", "n", "p50", "p90", "p99",
                                           "max"))
    for ev, values in sorted(by_event.items(),
                             key=lambda kv: -(pct(kv[1], .99) or 0)):
        print("  %-34s %6d %8.2f %8.2f %8.2f %8.2f"
              % (ev, len(values), pct(values, .5), pct(values, .9),
                 pct(values, .99), max(values)))
    print()
    print("instants: " + ", ".join(f"{ev}={n}" for ev, n in marks.most_common()
                                   if ev))

    gaps = [rec for rec in records if rec.get("ev") == "gui.gap"]
    if gaps:
        values = [num(rec, "gap_ms", 0.0) for rec in gaps]
        print()
        print("GUI thread unavailable: %d times, p50=%.0f p99=%.0f max=%.0f ms"
              % (len(gaps), pct(values, .5), pct(values, .99), max(values)))

    lag = revision_lag(records)
    if lag:
        print()
        print("published frames behind the input, in events: "
              "p50=%.0f p90=%.0f max=%.0f  (frames=%d)"
              % (pct(lag, .5), pct(lag, .9), max(lag), len(lag)))


def revision_lag(records):
    """How many input events had arrived when each frame was published."""
    latest = 0
    out = []
    for rec in sorted(records, key=lambda r: num(r, "t", 0.0)):
        ev = rec.get("ev")
        if ev in ("intensity.in", "step1.mapping_in"):
            rev = num(rec, "rev")
            if rev is not None:
                latest = max(latest, rev)
        elif ev == "step1.publish":
            rev = num(rec, "rev")
            if rev is not None:
                out.append(max(0.0, latest - rev))
    return out


def report_gaps(records, limit=20):
    """Every GUI stall with what contained it and what was reading."""
    all_spans = spans(records)
    jobs = intervals(records, "job")
    reads = intervals(records, "read")
    gaps = sorted((rec for rec in records if rec.get("ev") == "gui.gap"),
                  key=lambda rec: -num(rec, "gap_ms", 0.0))
    if not gaps:
        print("no gui.gap lines: the event loop never went quiet longer than "
              "its heartbeat allows")
        return
    for rec in gaps[:limit]:
        end = num(rec, "t", 0.0)
        computed = end - num(rec, "gap_ms", 0.0) / 1000.0
        declared = num(rec, "t_begin")
        begin = (declared if declared is not None
                 and abs(declared - computed) < 0.001 else computed)
        print("gui.gap %.0f ms  [%.6f .. %.6f]  active_jobs=%s active_reads=%s"
              % (num(rec, "gap_ms", 0.0), begin, end,
                 rec.get("active_jobs", "?"), rec.get("active_reads", "?")))
        containing = [(b, e, ev, r) for b, e, ev, r in all_spans
                      if b <= begin and e >= end]
        # Everything else that overlapped the stall at all -- a span that
        # started inside it and ran past its end is part of the story too, and
        # a "fully inside" filter would drop exactly those.
        inside = [(b, e, ev, r) for b, e, ev, r in all_spans
                  if b <= end and e >= begin
                  and (b, e, ev, r) not in containing]
        if containing:
            print("    held by (span contains the whole gap):")
            for b, e, ev, r in sorted(containing, key=lambda s: s[0]):
                print("      %-32s %8.2f ms %s" % (ev, num(r, "dur_ms", 0.0),
                                                   _extra(r)))
        else:
            print("    no GUI span contains it: the thread was in code that is "
                  "not instrumented, or outside this process")
        if inside:
            print("    and, overlapping the gap:")
            worst_first = sorted(inside, key=lambda s: -num(s[3], "dur_ms", 0.0))
            for b, e, ev, r in worst_first[:6]:
                print("      %-32s %8.2f ms %s" % (ev, num(r, "dur_ms", 0.0),
                                                   _extra(r)))
        hit_reads = overlapping(reads, begin, end)
        hit_jobs = overlapping(jobs, begin, end)
        print("    background at the time: %d job(s), %d read(s)"
              % (len(hit_jobs), len(hit_reads)))
        for b, e, r in hit_reads[:8]:
            print("      read job=%s %s ch=%s %s"
                  % (r.get("job"), f"patch={r.get('patch')}",
                     r.get("channel"),
                     "still open" if e is None
                     else f"{num(r, 'dur_ms', 0.0):.1f} ms"))
        print()


def _extra(rec):
    keep = ("channel", "patch", "mode", "channels", "rev", "who", "source",
            "shape", "outcome")
    return " ".join(f"{k}={rec[k]}" for k in keep if k in rec)


def report_at(records, when):
    jobs, reads = actives_at(records, when)
    print("at t=%.6f: %d job(s), %d read(s)" % (when, len(jobs), len(reads)))
    for b, e, rec in jobs:
        print("  job=%s source=%s patch=%s %s"
              % (rec.get("job"), rec.get("source"), rec.get("patch"),
                 "still open" if e is None else "closed"))
    for b, e, rec in reads:
        print("  read job=%s patch=%s ch=%s %s"
              % (rec.get("job"), rec.get("patch"), rec.get("channel"),
                 "still open" if e is None else "closed"))
    containing = [(b, e, ev, r) for b, e, ev, r in spans(records)
                  if b <= when <= e]
    for b, e, ev, r in sorted(containing, key=lambda s: s[0]):
        print("  in span %-30s %8.2f ms %s" % (ev, num(r, "dur_ms", 0.0),
                                               _extra(r)))


def _pct(values, q):
    values = sorted(values)
    if not values:
        return None
    k = max(0, min(len(values) - 1, int(round(q / 100.0 * (len(values) - 1)))))
    return values[k]


#: WSL-only (user ruling 2026-10-07: deployment is native Windows): a GPU
#: touched 250-1000 ms after its last frame blocks the first GL sync ~500 ms
WAKE_MIN_MS, WAKE_IDLE_MS = 400.0, (250.0, 1000.0)


def wsl_gpu_wakes(records):
    """(start, end) of GL work that waited on WSL's GPU wake-up: a paint ->
    present, or ONE submit (its first GL call is where the wait lands),
    taking >= WAKE_MIN_MS when the GPU's last work ended 250-1000 ms before.

    A paint's idle is counted from the previous PRESENT, not from the submit
    just before it (codex suggested the latter; measured against it: a
    submit only records commands -- in the A9 logs an 8 ms submit is followed
    by a 496 ms paint -> present, so the wait lands at the swap, and the
    submit baseline found 2 of the 55 wakes). SUSPECTED, not proven: the raw
    tables are always printed beside the ones without them."""
    out, last_present, last_submit, paint = [], None, None, None

    def waited(t0, t1, since):
        idle = None if since is None else (t0 - since) * 1000.0
        return ((t1 - t0) * 1000.0 >= WAKE_MIN_MS and idle is not None
                and WAKE_IDLE_MS[0] <= idle <= WAKE_IDLE_MS[1])
    for r in records:
        ev = r.get("ev")
        if ev == "gpu.paint" and paint is None:
            paint = num(r, "t")
        elif ev == "gpu.submit" and num(r, "t_begin") is not None:
            t0, t1 = num(r, "t_begin"), num(r, "t")
            since = max([t for t in (last_present, last_submit) if t is not None],
                        default=None)
            if waited(t0, t1, since):
                out.append((t0, t1))
            last_submit = t1
        elif ev == "gpu.present":
            t = num(r, "t")
            if paint is not None and waited(paint, t, last_present):
                out.append((paint, t))
            last_present, paint = t, None
    return out


def report_a9(records, gap_ms=32.0):
    """Block A9-M: the A9 numbers of a scripted run (scripts/a9_drive.py).

    Two latencies per action kind, kept apart (codex A9-M):
      * PRESENTED -- from the last input Qt actually handled for the action
        (`a9.handled`; the delivery mark for a programmatic action) to the
        first presented frame after it whose coverage is complete at its
        target level (`coverage`, gap_cells 0, target_fraction >= 0.999);
      * SETTLED -- to the driver's settled mark, which also waits for reads
        and schedulers to go quiet (>= 300 ms of quiet is part of it).
    Then the GUI stalls over `gap_ms`, frames with a gap after coverage was
    first complete, reads and camera writes per action."""
    for rec in records:
        if rec.get("ev") == "a9.invalid":
            print(f"INVALID RUN: stopped at action {rec.get('n')} "
                  f"({rec.get('reason')}) -- the numbers below measure nothing")
    acts, order = {}, []
    for rec in records:
        ev, n = rec.get("ev"), rec.get("n")
        if ev == "a9.action":
            acts[n] = {"do": rec.get("do"), "start": num(rec, "t"),
                       "expect": rec.get("expect")}
            order.append(n)
        elif ev in ("a9.delivered", "a9.handled") and n in acts:
            acts[n]["input"] = num(rec, "t")          # the LAST one wins
        elif ev == "a9.settled":
            target = n if acts.get(n, {}).get("do") != "settle" else n
            if target in acts:
                acts[target].update(settled=num(rec, "t"),
                                    timed_out=rec.get("timed_out") == "True",
                                    reads=num(rec, "reads", 0),
                                    cam_user=num(rec, "camera_user", 0),
                                    cam_jump=num(rec, "camera_jump", 0))
    covers = [(num(r, "t"), r) for r in records if r.get("ev") == "coverage"]
    platform = [r for r in records if r.get("ev") == "a9.platform"]
    # codex: timing alone does not prove a wake -- classified only on a log
    # that says it is WSL (logs from before the mark were all WSL runs)
    on_wsl = (not platform) or platform[-1].get("wsl") == "True"
    wakes = wsl_gpu_wakes(records) if on_wsl else []
    presented_clean = collections.defaultdict(list)
    presented = collections.defaultdict(list)
    settled = collections.defaultdict(list)
    timeouts = collections.Counter()
    reads = collections.defaultdict(list)
    jumps_in_gestures = 0
    for i, n in enumerate(order):
        a = acts[n]
        if a.get("do") == "settle" and i > 0:
            prev = acts[order[i - 1]]
        elif a.get("do") not in ("settle", "mark", "pause") and a.get("settled") is not None:
            prev = a                              # settles itself (an Intensity window)
        else:
            continue
        if True:
            kind, t_in = prev.get("do"), prev.get("input")
            expect = prev.get("expect")
            if kind in ("mark", "pause", "settle") or t_in is None:
                continue
            if a.get("timed_out"):
                timeouts[kind] += 1
            elif a.get("settled") is not None:
                settled[kind].append((a["settled"] - t_in) * 1000.0)
            first = next((t for t, r in covers if t >= t_in
                          and (expect in (None, "None") or r.get("where") == expect
                               or (expect == "gpu" and r.get("where") == "cpu-step1"))
                          and (num(r, "gap_cells", 1) or 0) == 0
                          and (num(r, "target_fraction", 0) or 0) >= 0.999), None)
            if first is not None and (a.get("settled") is None or first <= a["settled"]):
                presented[kind].append((first - t_in) * 1000.0)
                if not any(t_in <= p1 and p0 <= first for p0, p1 in wakes):
                    presented_clean[kind].append((first - t_in) * 1000.0)
            reads[kind].append(a.get("reads", 0) or 0)
            if kind in ("drag", "wheel"):
                jumps_in_gestures += int(a.get("cam_jump", 0) or 0)

    def line(name, vals):
        if not vals:
            return f"  {name:8s} n=  0"
        return (f"  {name:8s} n={len(vals):3d}  p50={_pct(vals, 50):8.1f}  "
                f"p95={_pct(vals, 95):8.1f}  max={max(vals):8.1f}")
    kinds = sorted(set(presented) | set(settled) | set(timeouts))
    print("A9 PRESENTED latency (handled input -> first complete frame), ms")
    for k in kinds:
        print(line(k, presented.get(k, [])))
    print(f"A9 PRESENTED latency without the {len(wakes)} suspected WSL GPU-wake frame(s) "
          "(actions whose frame waited on one left out), ms")
    for k in kinds:
        print(line(k, presented_clean.get(k, [])))
    print("A9 SETTLED latency (incl. >= 300 ms quiet), ms")
    for k in kinds:
        print(line(k, settled.get(k, [])) + f"  timeouts={timeouts.get(k, 0)}")
    print("reads per action (sum / max):  " + "  ".join(
        f"{k}={int(sum(v))}/{int(max(v))}" for k, v in sorted(reads.items()) if v))
    print(f"program camera jumps during drag/wheel: {jumps_in_gestures}")
    begin = [num(r, "t") for r in records if r.get("ev") == "a9.begin"]
    t0 = begin[0] if begin else None
    gap_recs = [r for r in records if r.get("ev") == "gui.gap"
                and (t0 is None or (num(r, "t") or 0) >= t0)
                and num(r, "gap_ms") is not None]
    gaps = [num(r, "gap_ms") for r in gap_recs]
    over = [g for g in gaps if g > gap_ms]
    print(f"GUI stalls > {gap_ms:.0f} ms: {len(over)}  (max {max(gaps) if gaps else 0:.0f} ms, "
          f"p95 of stalls {_pct(over, 95) or 0:.0f} ms)")
    own = [num(r, "gap_ms") for r in gap_recs if num(r, "gap_ms") > gap_ms
           and not any((num(r, "t_begin") or 0) <= p1 and p0 <= num(r, "t") for p0, p1 in wakes)]
    print(f"GUI stalls > {gap_ms:.0f} ms not overlapping a suspected WSL GPU wake: {len(own)}  "
          f"(max {max(own) if own else 0:.0f} ms, p95 {_pct(own, 95) or 0:.0f} ms)")
    seen_full, gap_frames = False, 0
    covers = [(t, r) for t, r in covers if r.get("where") in ("cpu", "gpu")]
    for _t, r in covers:
        cells = num(r, "gap_cells", 0) or 0
        if cells == 0:
            seen_full = True
        elif seen_full:
            gap_frames += 1
    # every probe EXECUTION (several submissions can share one presented
    # frame, and Step1's hidden CPU view is probed too) -- codex A9-M
    probe = [num(r, "probe_ms", 0) or 0 for r in records if r.get("ev") == "coverage.probe"]
    if probe:
        print(f"coverage probe's own cost: total {sum(probe) / 1000:.2f} s, "
              f"p95 {_pct(probe, 95):.2f} ms, max {max(probe):.2f} ms per frame")
    sampled = {r.get("sampled") for _t, r in covers}
    print(f"frames {len(covers)} (coverage sampled at {sorted(x for x in sampled if x)}); "
          f"frames with a gap after full coverage: {gap_frames}")
    end = [r for r in records if r.get("ev") == "a9.end"]
    if end:
        print(f"camera writes in total: user={end[-1].get('camera_user')} "
              f"jump={end[-1].get('camera_jump')}  reads={end[-1].get('reads_total')}")
    errors = [r for r in records if r.get("ev") == "a9.error"]
    if errors:
        print(f"driver errors: {len(errors)} (first: {errors[0].get('_line')[:160]})")


def _label(rec):
    """A mark's full label (it may contain spaces, which the field split loses)."""
    line = rec.get("_line", "")
    i = line.find(" label=")
    return line[i + 7:].strip() if i >= 0 else rec.get("label", "")


def report_m0(records, gap_ms=32.0):
    """Block A9 M0 (application §25.7/§25.8): the measurement-first numbers.

    Kept apart from `report_a9`, whose output is pinned by tests. Per input
    event: post -> handled (queue), driver timer lateness; per wheel notch:
    handled -> first frame, -> complete as `--a9` counts it (any fine level),
    -> complete AT THE OWED LEVEL (`exact_fraction`, Step1/Step3 only);
    frame intervals during gestures; GUI stalls with and without the driver's
    own work; Step1 fine-budget refusals and per-frame plane / upload counts.
    `gpu.render` is CPU submission time, not GPU execution time."""
    t = lambda r: num(r, "t")                                      # noqa: E731
    begin = [t(r) for r in records if r.get("ev") == "a9.begin"]
    t0 = begin[0] if begin else float("-inf")
    marks = [(t(r), _label(r)) for r in records
             if r.get("ev") == "a9.action" and r.get("do") == "mark"
             and not _label(r).startswith("repeat")]

    def section(when):
        name = "(before)"
        for mt, lab in marks:
            if mt <= when:
                name = lab
        return name

    def stats(vals):
        if not vals:
            return "n=  0"
        return (f"n={len(vals):4d}  p50={_pct(vals, 50):7.1f}  p95={_pct(vals, 95):7.1f}  "
                f"max={max(vals):7.1f}")

    # -- the driver's own work -------------------------------------------
    driver = [(num(r, "t_begin") or t(r), t(r), num(r, "dur_ms", 0) or 0, r.get("cached"))
              for r in records if r.get("ev") == "a9.driver"]
    print("driver viewer lookups (ms): cached " +
          stats([d for _a, _b, d, c in driver if c == "True"]) + " | scanned " +
          stats([d for _a, _b, d, c in driver if c != "True"]))
    # -- GUI stalls, by section, raw and without the driver ----------------
    gaps = [(num(r, "t_begin") or t(r), t(r), num(r, "gap_ms"))
            for r in records if r.get("ev") == "gui.gap" and (t(r) or 0) >= t0
            and num(r, "gap_ms") is not None and num(r, "gap_ms") > gap_ms]
    by_sec = collections.defaultdict(lambda: ([], []))
    for b, e, g in gaps:
        raw, own = by_sec[section(e)]
        raw.append(g)
        if not any(db <= e and b <= de for db, de, _d, _c in driver):
            own.append(g)
    print(f"GUI stalls > {gap_ms:.0f} ms by section (all | without driver lookups)")
    for name, (raw, own) in by_sec.items():
        print(f"  {name[:44]:44s} {stats(raw)} | {stats(own)}")
    # -- per input event: queue time and timer lateness --------------------
    acts = {r.get("n"): r.get("do") for r in records if r.get("ev") == "a9.action"}
    expects = {r.get("n"): r.get("expect") for r in records if r.get("ev") == "a9.action"}
    end_marks = [t(r) for r in records if r.get("ev") == "a9.end"]
    t_end = end_marks[-1] if end_marks else max((t(r) or 0) for r in records)
    posts = {(r.get("n"), r.get("k")): t(r) for r in records if r.get("ev") == "a9.post"}
    queue = collections.defaultdict(list)
    handled = collections.defaultdict(list)       # (n) -> [(seq, t)]
    for r in records:
        if r.get("ev") == "a9.handled" and r.get("seq") is not None:
            handled[r.get("n")].append((int(float(r["seq"])), t(r)))
            tp = posts.get((r.get("n"), r.get("seq")))
            if tp is not None:
                queue[acts.get(r.get("n"))].append((t(r) - tp) * 1000.0)
    late = [num(r, "late_ms") for r in records
            if r.get("ev") == "a9.delivered" and num(r, "late_ms") is not None]
    for kind, vals in sorted(queue.items()):
        print(f"input queue (post -> handled) {str(kind):6s} ms: {stats(vals)}")
    print(f"drag timer lateness ms: {stats(late)}")
    # -- per wheel notch: first frame, complete, complete at owed level -----
    covers = [(t(r), r) for r in records if r.get("ev") == "coverage"]
    # a notch's window ends at the next handled input of ANY action, or at
    # the next action that is not a settle / pause, whichever is first
    action_starts = sorted(t(r) for r in records if r.get("ev") == "a9.action"
                           and r.get("do") not in ("settle", "pause", "mark"))
    all_handled = sorted(th for evs in handled.values() for _s, th in evs)
    # a GPU frame counts for a notch only if it was SUBMITTED after the notch
    # was handled (codex: timestamps alone credit an older pending frame)
    submits = sorted((t(r), num(r, "frame", 0) or 0) for r in records
                     if r.get("ev") == "gpu.frame")
    first, done_any, done_exact = (collections.defaultdict(list) for _ in range(3))
    unfinished = collections.Counter()
    for n, events in handled.items():
        if acts.get(n) != "wheel":
            continue
        where = expects.get(n)
        events.sort()
        for _seq, th in events:
            nxt = [x for x in all_handled if x > th] + [x for x in action_starts if x > th]
            until = min(nxt) if nxt else t_end
            sec = section(th)
            floor = max((f for ts, f in submits if ts <= th), default=-1)
            seen = [(tc, r) for tc, r in covers if th <= tc <= until
                    and r.get("where") == where
                    and (where != "gpu" or (num(r, "frame", 0) or 0) > floor)]
            if seen:
                first[sec].append((seen[0][0] - th) * 1000.0)
            ok = next((tc for tc, r in seen if (num(r, "gap_cells", 1) or 0) == 0
                       and (num(r, "target_fraction", 0) or 0) >= 0.999), None)
            if ok is not None:
                done_any[sec].append((ok - th) * 1000.0)
            ex = next((tc for tc, r in seen if (num(r, "gap_cells", 1) or 0) == 0
                       and (num(r, "exact_fraction", 0) or 0) >= 0.999), None)
            if ex is not None:
                done_exact[sec].append((ex - th) * 1000.0)
            if ok is None:
                unfinished[sec] += 1
    print("wheel notch latency by section, ms (first frame | complete, any fine level"
          " | complete at the owed level, Step1/3)")
    for sec in sorted(set(first) | set(done_any) | set(unfinished)):
        print(f"  {sec[:44]:44s} first {stats(first[sec])}")
        print(f"  {'':44s} any   {stats(done_any[sec])}  not complete before the next input: "
              f"{unfinished[sec]}")
        print(f"  {'':44s} owed  {stats(done_exact[sec])}")
    # -- frame intervals during gestures ----------------------------------
    # a gesture run: drag / wheel actions with only `pause` between them (a
    # `settle` ends it -- idle time is not a frame interval)
    order = [(t(r), r.get("do")) for r in records if r.get("ev") == "a9.action"]
    windows, start = [], None
    for i, (ts, do) in enumerate(order):
        if do in ("drag", "wheel"):
            if start is None:
                start = ts
        elif do != "pause" and start is not None:
            windows.append((start, ts, section(start)))
            start = None
    if start is not None:
        windows.append((start, t_end, section(start)))
    presents = [t(r) for r in records if r.get("ev") == "gpu.present"]
    cpu_frames = [tc for tc, r in covers if r.get("where") == "cpu"]
    ivals = collections.defaultdict(list)
    for a, b, sec in windows:
        for series in (presents, cpu_frames):
            inside = [x for x in series if a <= x <= b]
            ivals[sec].extend((y - x) * 1000.0 for x, y in zip(inside, inside[1:]))
    print("frame intervals during gestures, ms (presented GPU frames / CPU frames)")
    for sec, vals in ivals.items():
        print(f"  {sec[:44]:44s} {stats(vals)}")
    # -- Step1 refusals and per-frame cost ---------------------------------
    refused = collections.Counter((r.get("channel"), r.get("level")) for r in records
                                  if r.get("ev") == "gpu.fine_refused")
    print(f"fine-budget refusals: {sum(refused.values())} "
          + ", ".join(f"{c}@L{lv}x{k}" for (c, lv), k in refused.most_common(8)))
    failed = collections.Counter(r.get("error", "")[:90] for r in records
                                 if r.get("ev") == "gpu.submit_failed")
    print(f"GPU submissions failed: {sum(failed.values())} "
          + "; ".join(f"{k} x{v}" for k, v in failed.most_common(3)))
    frames = [r for r in records if r.get("ev") == "gpu.frame"]
    if frames:
        def col(key):
            return [num(r, key, 0) or 0 for r in frames]
        print(f"gpu frames {len(frames)}: planes {stats(col('planes'))}")
        print(f"  passes {stats(col('passes'))}")
        print(f"  uploads/frame {stats(col('uploads'))}")
        print(f"  fine planes off the owed level {stats(col('fine_off_target'))}")
        print(f"  resident raw texture MiB max {max(col('resident')) / 2**20:.0f}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path")
    ap.add_argument("--gaps", action="store_true",
                    help="every GUI stall, attributed to the spans containing "
                         "it and the reads overlapping it")
    ap.add_argument("--at", type=float, default=None,
                    help="reconstruct what was running at one monotonic time")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--a9", action="store_true",
                    help="the A9 numbers of a scripted run (block A9-M)")
    ap.add_argument("--m0", action="store_true",
                    help="the measurement-first numbers of block A9 M0")
    ap.add_argument("--run", default="last",
                    help='which run in an appended log: "last" (default), '
                         '"all", or a 1-based index')
    args = ap.parse_args(argv)

    records = read(args.path)
    if not records:
        print(f"no PERF lines in {args.path}", file=sys.stderr)
        return 1
    segments = runs(records)
    if len(segments) > 1:
        print(f"{len(segments)} runs in this log "
              f"(job ids and revisions restart with each one)")
    if args.run == "all":
        chosen = list(enumerate(segments, start=1))
    elif args.run == "last":
        chosen = [(len(segments), segments[-1])]
    else:
        try:
            index = int(args.run)
        except ValueError:
            print(f"--run must be last, all or an index: {args.run!r}",
                  file=sys.stderr)
            return 2
        if not 1 <= index <= len(segments):
            print(f"--run {index} is out of range 1..{len(segments)}",
                  file=sys.stderr)
            return 2
        chosen = [(index, segments[index - 1])]
    for index, segment in chosen:
        if len(segments) > 1:
            marker = [r for r in segment if r.get("ev") == "run.begin"]
            name = marker[0].get("run") if marker else "before run.begin"
            print(f"\n=== run {index}/{len(segments)}  ({name}) ===")
        if args.m0:
            report_m0(segment)
        elif args.a9:
            report_a9(segment)
        elif args.at is not None:
            report_at(segment, args.at)
        elif args.gaps:
            report_gaps(segment, limit=args.limit)
        else:
            summarise(segment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
