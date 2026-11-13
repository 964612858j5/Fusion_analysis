"""What the TMA cores look like outside the Regions page
(docs/f5_tma_display_plan.md parts 2 and 9): one outline per piece, green
for a core the analysis runs, yellow for one it does not, labelled with the
same name the Regions page shows ("X1-Y06 <donor block>") when the core
is bound to a sheet position (user ruling 2026-10-19, second round).

Pure functions, no Qt and no cache. Two sources:
- the last Save (`saved_items`): Step1 and the Tissue preview, before a run;
- a frozen analysis snapshot (`snapshot_items`): Step2 with its hand-over or
  run and Step3 -- the picture as it was when the run was frozen, whatever
  Regions holds now.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

#: One scheme for the cores, Step 0.5 to Step 3 (backlog 28,
#: docs/v17_core_colours_plan.md): green analysed, yellow not.
GREEN = "#43a047"
YELLOW = "#fdd835"
#: Every core outline's width in screen pixels (`ui/regions/overlay.py`
#: BOUND_WIDTH: core has no Qt; a test keeps them equal).
CORE_WIDTH = 3.0


def analysed_uids(doc: Dict, study: Optional[Dict]) -> set:
    """The cores of an open Regions document the analysis would take, by its
    selection rules without the geometry (`analysis.select_cores`,
    `select_cores_without_map`; plan §2.1): with a map sheet a core bound
    alone to a position with a donor block, Run ticked, mapping `matched`,
    QC not `excluded`; without a map sheet a core whose Run tick is on. A
    mapped document without its sheet, or a sheet not attached to the
    cores: none (the analysis refuses). Not checked: an outline without
    area, a core outside a workspace's region, overlaps."""
    cores = doc.get("cores") or {}
    if doc.get("map_sha256"):
        if not study or study["map_sheet"]["sha256"] != doc["map_sha256"]:
            return set()
        from ..tma.cores import position_key
        sheet = study["map_sheet"]
        holders_of: Dict[str, List[str]] = {}
        for u, c in cores.items():
            if c.get("position_key"):
                holders_of.setdefault(c["position_key"], []).append(u)
        out = set()
        for p in sheet["positions"]:
            if not p.get("donor_block_id"):
                continue
            key = position_key(study["slide_id"], sheet["sha256"], p["position_id"])
            pos = (doc.get("positions") or {}).get(key, {})
            if not pos.get("run") or pos.get("qc") == "excluded":
                continue
            holders = holders_of.get(key, [])
            if len(holders) == 1 and cores[holders[0]].get("mapping") == "matched":
                out.add(holders[0])
        return out
    if study is not None:
        return set()
    return {u for u, c in cores.items() if c.get("run", True)}


def core_name(label, donor) -> str:
    """The Regions page's name of a bound core: "X1-Y06 <donor block>"."""
    return f"{label or '?'} {donor or ''}".strip()


def _names(study: Optional[Dict]) -> Dict[str, str]:
    """position_key -> the core's Regions name, for every sheet position."""
    if not study:
        return {}
    from ..tma.cores import position_key
    sheet = study["map_sheet"]
    return {position_key(study["slide_id"], sheet["sha256"], p["position_id"]):
            core_name(f"X{p['x']}-Y{int(p['y']):02d}", p.get("donor_block_id"))
            for p in sheet["positions"]}


def display_cores(doc: Dict, study: Optional[Dict], run_uids, why: Optional[Dict] = None,
                  why_uid: Optional[Dict] = None) -> List[Dict]:
    """Every core of a Regions document, for drawing: ``{uid, position_key,
    polygons (its pieces' own outlines), name, run, reason}``, in uid
    order. `run_uids`: the cores the analysis runs; `why`: position_key ->
    the reason a bound core does not run; `why_uid`: uid -> the reason a
    core of a map-less snapshot does not run (its inventory)."""
    names = _names(study)
    keys = set()
    if study:
        from ..tma.cores import position_key
        sheet = study["map_sheet"]
        keys = {position_key(study["slide_id"], sheet["sha256"], p["position_id"])
                for p in sheet["positions"]}
    run_uids, why = set(run_uids or ()), dict(why or {})
    out = []
    for uid, c in sorted(doc.get("cores", {}).items()):
        polys = [[list(q) for q in doc["pieces"][pid]["polygon"]] for pid in c["pieces"]
                 if pid in doc["pieces"] and len(doc["pieces"][pid]["polygon"]) >= 3]
        if not polys:
            continue
        key = c.get("position_key") if c.get("position_key") in keys else None
        run = uid in run_uids
        out.append({"uid": uid, "position_key": key, "polygons": polys,
                    "name": names.get(key, "") if key else "", "run": run,
                    "reason": "" if run else (why.get(key, "") if key else
                                              (why_uid or {}).get(uid, "not_bound"))})
    return out


def _items(cores: Sequence[Dict]) -> List[Dict]:
    """Overlay items (`ui/regions/overlay.py` `set_items`): one per piece,
    the label on the core's first piece only."""
    items = []
    for c in cores:
        for k, poly in enumerate(c["polygons"]):
            it = {"id": f"{c['uid']}#{k}", "polygon": poly, "color": GREEN if c["run"] else YELLOW,
                  "width": CORE_WIDTH}
            if k == 0 and c.get("name"):
                it["label"] = c["name"]
            items.append(it)
    return items


def saved_items(doc: Dict, study: Optional[Dict], run_uids) -> List[Dict]:
    """The last Save's cores (Step1, the Tissue preview)."""
    return _items(display_cores(doc, study, run_uids))


def snapshot_items(snap: Dict) -> List[Dict]:
    """A frozen snapshot's cores, as at the freeze: its `display_cores`
    (green and yellow), or -- a snapshot older than that field -- only the
    cores it ran, green, their joined outline split back into rings. Names
    come from the snapshot's own sheet positions."""
    names = {p["position_key"]: core_name(p.get("label"), p.get("donor_block_id"))
             for p in snap.get("positions") or []}
    if "display_cores" in snap:
        return _items([dict(c, name=c.get("name") or names.get(c.get("position_key"), ""))
                       for c in snap["display_cores"]])
    cores = [{"uid": c.get("uid") or c["position_key"], "polygons": split_keyhole(c["polygon"]),
              "name": names.get(c["position_key"]) or core_name(c.get("label"), c.get("donor_block_id")),
              "run": True}
             for c in snap.get("cores") or []]
    return _items(cores)


#: The Regions page's exclusion colour (`ui/regions/exclusions_panel.py`
#: EXCL_COLOR; a test keeps them equal). Step2 / Step3 (backlog 21).
EXCLUSION = "#e040fb"


def _exclusion_items(found) -> List[Dict]:
    """Overlay items for (uid, polygon) pairs: hatched as on the Regions
    page (B27's screen hatch), no label."""
    return [{"id": f"exclusion:{uid}", "polygon": [list(q) for q in poly], "color": EXCLUSION,
             "hatch": True, "hatch_style": "screen", "width": CORE_WIDTH}
            for uid, poly in found if len(poly or ()) >= 3]


def exclusion_items(snap: Dict) -> List[Dict]:
    """A frozen snapshot's exclusions (Step2 during its run, Step3)."""
    return _exclusion_items((e.get("uid", k), e.get("polygon"))
                            for k, e in enumerate(snap.get("exclusions") or []))


def saved_exclusion_items(doc: Dict) -> List[Dict]:
    """The last Save's exclusions (Step2 before a run): every exclusion of
    the document, as a run started now freezes them (one without area is
    left out there; here it draws as a line, if at all)."""
    return _exclusion_items((uid, e.get("polygon"))
                            for uid, e in sorted((doc.get("exclusions") or {}).items()))


def split_keyhole(poly: Sequence) -> List[List]:
    """The rings a `geometry.keyhole` ring was joined from: every bridge is
    one edge walked there (P -> Q) and later back (Q -> P) with the joined
    ring between; the innermost pair is cut out first. A ring without
    bridges comes back as itself. For drawing only."""
    pts = [list(q) for q in poly]
    rings = []
    while True:
        n = len(pts)
        edges = {}
        for k in range(n - 1):
            edges.setdefault((tuple(pts[k]), tuple(pts[k + 1])), []).append(k)
        best = None
        for k in range(n - 1):
            back = edges.get((tuple(pts[k + 1]), tuple(pts[k])))
            if not back:
                continue
            for l in back:
                if l > k + 1 and (best is None or l - k < best[1] - best[0]):
                    best = (k, l)
        if best is None:
            break
        k, l = best
        inner = pts[k + 1:l]
        if len(inner) >= 3:
            rings.append(inner)
        pts = pts[:k + 1] + pts[l + 2:]
    if len(pts) >= 3:
        rings.insert(0, pts)
    return rings
