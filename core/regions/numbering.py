"""Regions numbering (plan §4; ported from F3's `core/tma/layout.py`, the
logic unchanged): the target records (start / end + order, or a custom
list; empty positions skipped, missing ones not; ranges may cross columns
and rows), the band selection of a line, and the binding with its conflict
preview, rebind, exchange, custom numbers and review confirmation.

Numbering never guesses: the user gives the image order and the target
records; the counts must agree; every conflict is shown before a rebind.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

from .document import RegionsError, core_of_object, holder_of, log

ORDERS = ("column", "row", "column_rev", "row_rev")


def _order_key(order: str):
    if order in ("column", "column_rev"):
        return lambda p: (p["x"], p["y"])
    if order in ("row", "row_rev"):
        return lambda p: (p["y"], p["x"])
    raise RegionsError(f"unknown order {order!r}; one of {ORDERS}")


def _record(p, key) -> Dict:
    return {"position_id": int(p["position_id"]), "x": int(p["x"]), "y": int(p["y"]),
            "label": f"X{p['x']}-Y{int(p['y']):02d}", "donor": p["donor_block_id"],
            "sample": p["sample_id"], "position_key": key}


def target_records(study: Dict, start_id: int, end_id: int, order: str) -> List[Dict]:
    """The map records from `start_id` to `end_id` walking in `order`,
    across columns / rows too; empty positions skipped."""
    from ..tma.cores import position_key
    sheet = study["map_sheet"]
    ps = sorted(sheet["positions"], key=_order_key(order))
    if order.endswith("_rev"):
        ps.reverse()
    ids = [p["position_id"] for p in ps]
    if start_id not in ids or end_id not in ids:
        raise RegionsError(f"positions {start_id} / {end_id} are not on the map")
    i, j = ids.index(start_id), ids.index(end_id)
    if i > j:
        raise RegionsError("the end comes before the start in this order; choose the reverse order")
    return [_record(p, position_key(study["slide_id"], sheet["sha256"], p["position_id"]))
            for p in ps[i:j + 1] if p["donor_block_id"]]


def walk_records(study: Dict, start_id: int, end_id: int) -> List[Dict]:
    """The map records from `start_id` to `end_id` with no order to choose
    (user 2026-10-18): along the column when both lie in one column, along
    the row when in one row, otherwise in position order (column by
    column); walked backwards when the end comes first. Empty positions are
    skipped; start == end is that one record."""
    by = {p["position_id"]: p for p in study["map_sheet"]["positions"]}
    if start_id not in by or end_id not in by:
        raise RegionsError(f"positions {start_id} / {end_id} are not on the map")
    a, b = by[start_id], by[end_id]
    order = "row" if a["y"] == b["y"] and a["x"] != b["x"] else "column"
    ps = sorted(by.values(), key=_order_key(order))
    ids = [p["position_id"] for p in ps]
    i, j = ids.index(start_id), ids.index(end_id)
    seg = ps[i:j + 1] if i <= j else list(reversed(ps[j:i + 1]))
    from ..tma.cores import position_key
    sheet = study["map_sheet"]
    return [_record(p, position_key(study["slide_id"], sheet["sha256"], p["position_id"]))
            for p in seg if p["donor_block_id"]]


def records_by_ids(study: Dict, ids: Sequence[int]) -> List[Dict]:
    """A custom target list in the order given; an empty position or a
    repeated id is refused."""
    from ..tma.cores import position_key
    sheet = study["map_sheet"]
    by = {p["position_id"]: p for p in sheet["positions"]}
    if len(set(ids)) != len(ids):
        raise RegionsError("a record is listed twice")
    out = []
    for i in ids:
        p = by.get(int(i))
        if p is None:
            raise RegionsError(f"position {i} is not on the map")
        if not p["donor_block_id"]:
            raise RegionsError(f"position {i} is empty on the map")
        out.append(_record(p, position_key(study["slide_id"], sheet["sha256"], p["position_id"])))
    return out


def band_select(objects: Sequence[Tuple[str, float, float]], p0: Tuple[float, float],
                p1: Tuple[float, float], half_width: float) -> List[str]:
    """Objects ``(id, y, x)`` whose centre lies within `half_width` of the
    segment p0 -> p1 (level 0, (y, x)), ordered along it (ties by id)."""
    (y0, x0), (y1, x1) = p0, p1
    dy, dx = y1 - y0, x1 - x0
    length = math.hypot(dy, dx)
    if length == 0:
        raise RegionsError("the line has no length")
    uy, ux = dy / length, dx / length
    hits = []
    for oid, y, x in objects:
        t = (y - y0) * uy + (x - x0) * ux
        off = abs(-(y - y0) * ux + (x - x0) * uy)
        if off <= half_width and -half_width <= t <= length + half_width:
            hits.append((t, oid))
    return [oid for _t, oid in sorted(hits)]


def plan_binding(d: Dict, objects: Sequence[str], records: Sequence[Dict]) -> Dict:
    """The preview of binding `objects` (cores or pieces, image order) to
    `records` one by one; ``conflicts`` lists both kinds."""
    pairs, problems = [], []
    if len(objects) != len(records):
        problems.append(f"selected {len(objects)}, target {len(records)}")
    if len(set(objects)) != len(objects):
        problems.append("an object is selected twice")
    owners = [d["pieces"][o]["owner"] if o in d["pieces"] else o for o in objects]
    if len([u for u in owners if u]) != len({u for u in owners if u}):
        problems.append("two selected pieces belong to the same core")
    for o in objects:
        if o in d["pieces"] and d["pieces"][o]["dropped"]:
            problems.append(f"{o} is left out")
    selected = {u for u in owners if u}
    conflicts = []
    for o, owner, r in zip(objects, owners, records):
        cur = d["cores"][owner]["position_key"] if owner else None
        if cur and cur != r["position_key"]:
            conflicts.append({"kind": "object_numbered", "object": o, "core": owner, "position_key": cur})
        held = holder_of(d, r["position_key"])
        if held and held != owner and held not in selected:
            conflicts.append({"kind": "record_held", "object": o, "core": held, "position_key": r["position_key"]})
        pairs.append({"object": o, "core": owner, "record": r})
    return {"ok": not problems, "problems": problems, "pairs": pairs, "conflicts": conflicts}


def apply_binding(d: Dict, plan: Dict, method: str, rebind: bool = False, extra: Optional[Dict] = None) -> List[str]:
    """Bind as previewed; conflicts need ``rebind=True``; a core pushed off
    keeps its pieces and becomes unmatched."""
    if not plan["ok"]:
        raise RegionsError("; ".join(plan["problems"]))
    if plan["conflicts"] and not rebind:
        raise RegionsError(f"{len(plan['conflicts'])} conflicts; confirm the rebind")
    uids = [core_of_object(d, p["object"]) for p in plan["pairs"]]
    targets = {p["record"]["position_key"] for p in plan["pairs"]}
    for uid, c in d["cores"].items():
        if c["position_key"] in targets or uid in uids:
            if c["position_key"] and c["position_key"] not in targets:
                d["positions"][c["position_key"]]["observed"] = None
            c["position_key"], c["mapping"] = None, "unmatched"
    for uid, p in zip(uids, plan["pairs"]):
        c = d["cores"][uid]
        c["position_key"], c["mapping"], c["custom_label"] = p["record"]["position_key"], "matched", None
        d["positions"][c["position_key"]]["observed"] = "present"
        d["positions"][c["position_key"]]["missing_reason"] = ""
    log(d, dict(extra or {}, kind="bind", method=method, cores=uids,
                position_keys=[p["record"]["position_key"] for p in plan["pairs"]]))
    return uids


def swap(d: Dict, uid_a: str, uid_b: str) -> None:
    """Exchange the numbers of two numbered cores; both count as confirmed."""
    a, b = d["cores"][uid_a], d["cores"][uid_b]
    if not (a["position_key"] and b["position_key"]):
        raise RegionsError("only two numbered cores can exchange their numbers; use a rebind")
    a["position_key"], b["position_key"] = b["position_key"], a["position_key"]
    a["mapping"] = b["mapping"] = "matched"
    log(d, {"kind": "swap", "cores": [uid_a, uid_b]})


def set_custom(d: Dict, obj: str, label: str) -> str:
    if not label.strip():
        raise RegionsError("a custom number needs text")
    uid = core_of_object(d, obj)
    c = d["cores"][uid]
    if c["position_key"]:
        d["positions"][c["position_key"]]["observed"] = None
    c["position_key"], c["custom_label"], c["mapping"] = None, label.strip(), "unmatched"
    log(d, {"kind": "custom", "core": uid, "label": label.strip()})
    return uid


def confirm_number(d: Dict, uid: str) -> None:
    c = d["cores"][uid]
    if c["mapping"] != "review":
        return
    if not c["position_key"]:
        raise RegionsError("the core has no number to confirm")
    c["mapping"] = "matched"
    log(d, {"kind": "confirm", "core": uid})
