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
        begin = num(rec, "t_begin")
        if begin is None:
            begin = end - dur / 1000.0
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
        begin = num(rec, "t_begin", end)
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
            for b, e, ev, r in sorted(inside, key=lambda s: -num(r, "dur_ms", 0.0))[:6]:
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


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path")
    ap.add_argument("--gaps", action="store_true",
                    help="every GUI stall, attributed to the spans containing "
                         "it and the reads overlapping it")
    ap.add_argument("--at", type=float, default=None,
                    help="reconstruct what was running at one monotonic time")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args(argv)

    records = read(args.path)
    if not records:
        print(f"no PERF lines in {args.path}", file=sys.stderr)
        return 1
    if args.at is not None:
        report_at(records, args.at)
        return 0
    if args.gaps:
        report_gaps(records, limit=args.limit)
        return 0
    summarise(records)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
