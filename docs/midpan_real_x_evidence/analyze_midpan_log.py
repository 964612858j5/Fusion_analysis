#!/usr/bin/env python3
"""Recompute every number this directory's README states, from the logs it
ships. Reads a BLOCK01_MIDPAN_LOG file, plain or gzipped.

    python analyze_midpan_log.py run1_before_the_fix.log.gz
    python analyze_midpan_log.py run1_before_the_fix.log.gz --gestures

A gesture's identity is (panel, gid), never gid alone: every OverviewPanel
counts its own gestures from 1, and the page's thumbnail and the Tissue
Navigator popup's are separate panels in one process, so grouping by gid
merges unrelated drags -- one "gesture" with three presses spanning four
minutes, and a total that is simply wrong.

Classification of a gesture:

  A  the press never reached the viewport (middle-button moves arrive with
     no gesture open: `stray-move`)
  B  the press arrived and moves were cancelled, refused, or handled
     without moving the camera
  C  the camera moved but the repaint was late
  D  the GUI thread was unavailable, so events waited after being stamped

A hole in the timeline whose `late_ms` is near zero is deliberately NOT
attributed: it says only that nothing waited inside the application after
the platform stamped the event. Whether the input stopped, a device or
driver reported nothing, or events were coalesced upstream of the stamp is
outside what this log can measure.
"""
import collections
import gzip
import re
import sys

LATE_PAINT_MS = 40.0        # a repaint this long after its step is visible
LATE_EVENT_MS = 40.0        # an event this late waited inside the app
BIG_HOLE_MS = 80.0          # a gap in a drag that a hand would notice


def read(path):
    opener = gzip.open if path.endswith(".gz") else open
    out = []
    with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.startswith("MIDPAN "):
                continue
            rec = {}
            for token in line.split():
                if "=" in token:
                    key, value = token.split("=", 1)
                    rec.setdefault(key, value)
            reason = re.search(r"closed_by=(.*)$", line.rstrip("\n"))
            if reason:
                rec["closed_by"] = reason.group(1)
            out.append(rec)
    return out


def gestures(recs):
    """(panel, gid) -> its lines, in order."""
    out = collections.OrderedDict()
    for rec in recs:
        out.setdefault((rec.get("panel"), rec.get("gid")), []).append(rec)
    return out


def num(rec, key):
    try:
        return float(rec[key])
    except (KeyError, TypeError, ValueError):
        return None


def pct(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(int(q * len(ordered)), len(ordered) - 1)]


def undone_pairs(lines):
    """Consecutive camera steps where the second exactly negates the first,
    with the index of the step being undone. An exact negation to the last
    bit of a double is not a hand: it says the later move carried a
    position already visited."""
    steps = [r for r in lines if r.get("what") == "move-applied"]
    found = []
    for index, (first, second) in enumerate(zip(steps, steps[1:])):
        ax, ay = num(first, "dx_view"), num(first, "dy_view")
        bx, by = num(second, "dx_view"), num(second, "dy_view")
        if None in (ax, ay, bx, by) or not (ax or ay):
            continue
        if (abs(ax + bx) <= 1e-6 * max(1.0, abs(ax))
                and abs(ay + by) <= 1e-6 * max(1.0, abs(ay))):
            found.append((index, second))
    return found


def summarise(path):
    recs = read(path)
    gest = gestures(recs)
    what = collections.Counter(r.get("what") for r in recs)
    with_press = [k for k, v in gest.items()
                  if any(r.get("what") == "press" for r in v)]
    moves = [r for r in recs if r.get("what") == "move"]
    blank = [r for r in moves if r.get("buttons") == "NoButton"]
    blank_app_middle = [r for r in blank
                        if r.get("app_buttons") == "Middle"]
    undone_first = undone_later = 0
    for lines in gest.values():
        for index, _ in undone_pairs(lines):
            if index == 0:
                undone_first += 1
            else:
                undone_later += 1

    print("file: %s" % path)
    print("  gestures, identified by (panel, gid): %d   panels: %d"
          % (len(gest), len({k[0] for k in with_press})))
    print("  gestures containing a press: %d" % len(with_press))
    print("  moves: %d   camera steps: %d   refused as a grab replay: %d"
          % (what["move"], what["move-applied"], what["stale-grab-move"]))
    print("  moves reporting buttons=NoButton: %d (of those, %d had "
          "QApplication.mouseButtons()==Middle)"
          % (len(blank), len(blank_app_middle)))
    print("  moves reporting some other button: %d   stray (no gesture "
          "open): %d" % (what["move-contradicted"], what["stray-move"]))
    print("  exactly-undone camera steps: %d   of which undid the "
          "gesture's FIRST step: %d, a later step: %d"
          % (undone_first + undone_later, undone_first, undone_later))

    def table(name, values, unit="ms"):
        if not values:
            print("  %-34s not recorded in this run" % name)
            return
        print("  %-34s p50=%7.2f p90=%7.2f p99=%7.2f max=%7.2f  n=%d  (%s)"
              % (name, pct(values, .5), pct(values, .9), pct(values, .99),
                 max(values), len(values), unit))

    table("camera step cost",
          [num(r, "took_ms") for r in recs
           if r.get("what") == "move-applied" and num(r, "took_ms") is not None])
    table("first repaint after a step",
          [num(r, "after_step_ms") for r in recs
           if r.get("what") == "paint" and num(r, "after_step_ms") is not None])
    table("move-to-move arrival gap",
          [num(r, "arr_gap") for r in moves if num(r, "arr_gap") is not None])
    table("late_ms (arrival gap - stamp gap)",
          [num(r, "late_ms") for r in moves if num(r, "late_ms") is not None])
    gaps = [num(r, "gap_ms") for r in recs if r.get("what") == "loop-gap"]
    print("  5 ms heartbeat gaps: %d lines, worst %s"
          % (len(gaps), "%.1f ms" % max(gaps) if gaps else "-"))

    holes = [r for r in moves
             if (num(r, "arr_gap") or 0) > BIG_HOLE_MS
             and num(r, "late_ms") is not None]
    late_holes = [r for r in holes if num(r, "late_ms") > LATE_EVENT_MS]
    if holes:
        print("  holes longer than %.0f ms: %d, of which the EVENT itself "
              "waited (>%.0f ms): %d" % (BIG_HOLE_MS, len(holes),
                                         LATE_EVENT_MS, len(late_holes)))
        print("  the rest opened before the platform stamped the event; "
              "what opened them is not measurable here")
    print("  how gestures ended: %s"
          % dict(collections.Counter(r["closed_by"] for r in recs
                                     if "closed_by" in r)))
    return gest


def per_gesture(gest):
    print()
    print("panel  gid  press moves steps refused blank contra stray "
          "paint_max gap_max dur_ms verdict")
    for (panel, gid), lines in gest.items():
        what = collections.Counter(r.get("what") for r in lines)
        paints = [num(r, "after_step_ms") for r in lines
                  if r.get("what") == "paint" and num(r, "after_step_ms") is not None]
        gaps = [num(r, "gap_ms") for r in lines if r.get("what") == "loop-gap"]
        times = [num(r, "t") for r in lines if num(r, "t") is not None]
        undone = undone_pairs(lines)
        verdict = []
        if what["stray-move"] and not what["press"]:
            verdict.append("A: no press reached the viewport")
        if what["press"] and not what["move"]:
            verdict.append("A/B: press taken, no move followed")
        if undone:
            verdict.append("B: %d camera step(s) exactly undone" % len(undone))
        if what["move-contradicted"]:
            verdict.append("B: closed by a move reporting another button")
        if paints and max(paints) > LATE_PAINT_MS:
            verdict.append("C: repaint %.0f ms after its step" % max(paints))
        if gaps and max(gaps) > 100.0:
            verdict.append("D: GUI thread unavailable %.0f ms" % max(gaps))
        if not verdict:
            verdict.append("normal")
        print("%-6s %-4s %-5d %-5d %-5d %-7d %-5d %-6d %-5d %-9s %-7s %-6s %s"
              % (panel, gid, what["press"], what["move"],
                 what["move-applied"], what["stale-grab-move"],
                 what["move-blank"], what["move-contradicted"],
                 what["stray-move"],
                 "%.1f" % max(paints) if paints else "-",
                 "%.0f" % max(gaps) if gaps else "-",
                 "%.0f" % ((max(times) - min(times)) * 1000) if times else "-",
                 "; ".join(verdict)))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    for path in args:
        gest = summarise(path)
        if "--gestures" in sys.argv:
            per_gesture(gest)
        print()
