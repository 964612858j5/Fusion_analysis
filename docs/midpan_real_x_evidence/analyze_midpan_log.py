"""Read a BLOCK01_MIDPAN_LOG file (plain or .gz) and summarise it one
gesture per line.

Classifies each gesture the way the execution prompt asks for:
  A  the press never reached the viewport (stray middle-button moves)
  B  the press arrived and moves were cancelled, refused or never handled
  C  moves handled and the camera moved, but the repaint was late
  D  the GUI thread was unavailable, so the events waited after being
     stamped

A hole with late_ms near zero is NOT attributed here: it says only that
nothing waited inside this application. Whether the input stopped, a
device or driver reported nothing, or events were coalesced upstream of
the platform's timestamp is outside what this log can measure.
"""
import gzip, sys, collections

PATH = sys.argv[1]
LATE_PAINT_MS = 40.0

def fields(line):
    out = {}
    for tok in line.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = v
    return out

gest = collections.OrderedDict()
marks = []
opener = gzip.open if PATH.endswith(".gz") else open
for raw in opener(PATH, "rt", encoding="utf-8", errors="replace"):
    line = raw.rstrip("\n")
    if line.startswith("MIDPAN-MARK"):
        marks.append(line)
        continue
    if not line.startswith("MIDPAN "):
        continue
    f = fields(line)
    gid = f.get("gid", "?")
    what = f.get("what", "?")
    g = gest.setdefault(gid, {"n": collections.Counter(), "paints": [],
                              "gaps": [], "steps": 0, "closed": None,
                              "t0": None, "t1": None, "marks": len(marks),
                              "first_move_buttons": None, "active": None})
    g["n"][what] += 1
    try:
        t = float(f.get("t", "nan"))
        g["t0"] = t if g["t0"] is None else g["t0"]
        g["t1"] = t
    except ValueError:
        pass
    if what == "press":
        g["active"] = f.get("active")
    if what == "move" and g["first_move_buttons"] is None:
        g["first_move_buttons"] = f.get("buttons")
    if what == "move-applied":
        g["steps"] += 1
    if what == "paint":
        try:
            g["paints"].append(float(f.get("after_step_ms", "nan")))
        except ValueError:
            pass
    if what == "loop-gap":
        try:
            g["gaps"].append(float(f.get("gap_ms", "nan")))
        except ValueError:
            pass
    if "closed_by" in line:
        g["closed"] = line.split("closed_by=", 1)[1]

print("marks in the log: %d" % len(marks))
for m in marks:
    print("  " + m)
print()
hdr = ("gid  press moves applied blank contradicted stray paint_max gap_max "
       "dur_ms first_move_buttons active closed_by")
print(hdr)
for gid, g in gest.items():
    n = g["n"]
    dur = "" if (g["t0"] is None or g["t1"] is None) else "%.0f" % (
        (g["t1"] - g["t0"]) * 1000.0)
    print("%-4s %-5d %-5d %-7d %-5d %-12d %-5d %-9s %-7s %-6s %-18s %-6s %s" % (
        gid, n["press"], n["move"], g["steps"], n["move-blank"],
        n["move-contradicted"], n["stray-move"],
        ("%.1f" % max(g["paints"])) if g["paints"] else "-",
        ("%.0f" % max(g["gaps"])) if g["gaps"] else "-",
        dur, g["first_move_buttons"] or "-", g["active"] or "-",
        g["closed"] or "(still open)"))

print()
for gid, g in gest.items():
    n = g["n"]
    verdict = []
    if n["stray-move"] and not n["press"]:
        verdict.append("A: no press reached the viewport, %d middle-down moves did"
                       % n["stray-move"])
    if n["press"] and n["move"] == 0:
        verdict.append("A/B: press taken, not one move followed")
    if n["move-contradicted"] or (n["move-blank"] and g["closed"]):
        verdict.append("B: the gesture was closed by the move evidence (%s)"
                       % g["closed"])
    if g["steps"] and g["paints"] and max(g["paints"]) > LATE_PAINT_MS:
        verdict.append("C: camera moved, first repaint %.0f ms late"
                       % max(g["paints"]))
    if g["gaps"] and max(g["gaps"]) > 100.0:
        verdict.append("D: GUI thread unavailable for up to %.0f ms"
                       % max(g["gaps"]))
    if g["steps"] and n["move"] and g["steps"] < n["move"]:
        verdict.append("moves handled %d, camera steps %d -- %d move(s) "
                       "translated nothing" % (n["move"], g["steps"],
                                               n["move"] - g["steps"]))
    if not verdict:
        verdict.append("normal: %d moves, %d camera steps, repaint <= %s ms"
                       % (n["move"], g["steps"],
                          ("%.1f" % max(g["paints"])) if g["paints"] else "n/a"))
    print("gid=%s: %s" % (gid, "; ".join(verdict)))
