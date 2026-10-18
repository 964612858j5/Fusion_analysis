"""TMA layout (F3, docs/tma_f3_page_application.md): the user's numbering
and correction of the candidate cores, and the locked versions.

Pure functions over one JSON-able *draft* dict; no Qt. The page (ui/tma)
keeps the draft, its undo history and the drawing.

* Pieces -- one per tissue piece: a detected candidate (F2) or a circle the
  user drew. ``owner`` is the core it belongs to, None while unassigned;
  ``dropped`` is the reason it is left out ("不纳入"), None otherwise.
* Cores -- ``core_uid`` (never changes) -> its pieces, the bound map
  position (``position_key`` of F1, or None), a custom label for a number
  that is not on the map, and ``mapping``: matched / unmatched / review.
* Positions -- per ``position_key``: ``observed`` (present / missing /
  None), ``qc`` (unreviewed / accepted / excluded) with reasons, and the
  ``run`` tick for F4. The map use (sample / empty) comes from the map
  sheet. The four are kept apart (Sept spec, plan §2.4); the table's status
  text is derived from them.

Numbering never guesses: the user gives the image order (a line with a
band, clicks, one core) and the target records (start / end + order, or a
list); the counts must agree; every conflict is shown before a rebind.
Geometry is in `layout_geometry`, the lock and the files in `layout_lock`.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

SCHEMA_VERSION = 1
CORE_PIECE_FRAC = 0.5        # a detected piece of at least this share of a core's area starts as its own core
ORDERS = ("column", "row", "column_rev", "row_rev")
MAPPING = ("matched", "unmatched", "review")
OBSERVED = ("present", "missing")
QC = ("unreviewed", "accepted", "excluded")


class LayoutError(ValueError):
    pass


# ── the draft ───────────────────────────────────────────────────────────

def new_draft(study: Dict, candidates: Dict, based_on: Optional[int] = None) -> Dict:
    """A fresh draft from the study (F1) and a saved candidate list (F2):
    every candidate is a piece; one at least `CORE_PIECE_FRAC` of the
    expected core area starts as its own unmatched core, a smaller one stays
    unassigned (a fragment for the user to merge or leave out)."""
    from .cores import position_key
    sheet = study["map_sheet"]
    core_area = math.pi * (float(candidates["core_diameter_px"]) / 2) ** 2
    d = {"schema_version": SCHEMA_VERSION, "slide_id": study["slide_id"],
         "map_sha256": sheet["sha256"], "candidates_version": int(candidates["version"]),
         "core_diameter_px": float(candidates["core_diameter_px"]),
         "based_on": based_on, "pieces": {}, "cores": {}, "positions": {},
         "operations": [], "coverage_confirmed": None}
    for p in sheet["positions"]:
        key = position_key(study["slide_id"], sheet["sha256"], p["position_id"])
        d["positions"][key] = {"observed": None, "missing_reason": "", "qc": "unreviewed", "qc_reasons": [],
                               "run": False}
    for c in candidates["candidates"]:
        pid = f"p{int(c['candidate_id'])}"
        d["pieces"][pid] = {"cy": float(c["cy"]), "cx": float(c["cx"]), "r": float(c["radius"]),
                            "source": "detected", "candidate_id": int(c["candidate_id"]),
                            "tissue": c.get("tissue_fraction"), "owner": None, "dropped": None}
        if c["area"] >= CORE_PIECE_FRAC * core_area:
            new_core(d, [pid])
    return d


def new_core(d: Dict, piece_ids: Sequence[str]) -> str:
    uid = "core_" + uuid.uuid4().hex[:12]
    d["cores"][uid] = {"pieces": list(piece_ids), "position_key": None, "custom_label": None,
                       "mapping": "unmatched"}
    for pid in piece_ids:
        d["pieces"][pid]["owner"] = uid
    return uid


def add_piece(d: Dict, cy: float, cx: float, r: float) -> str:
    """A circle the user drew: a new unassigned piece."""
    if not (r > 0 and all(math.isfinite(v) for v in (cy, cx, r))):
        raise LayoutError("a circle needs a finite centre and a positive radius")
    n = 1 + max([int(k[1:]) for k in d["pieces"] if k[1:].isdigit()] or [0])
    pid = f"p{n}"
    d["pieces"][pid] = {"cy": float(cy), "cx": float(cx), "r": float(r), "source": "drawn",
                        "candidate_id": None, "tissue": None, "owner": None, "dropped": None}
    return pid


def core_of_object(d: Dict, obj: str) -> str:
    """The core of a selected object: a core uid as is; a piece -> its
    owner, or a new core made of it (an unassigned piece being numbered)."""
    if obj in d["cores"]:
        return obj
    piece = d["pieces"].get(obj)
    if piece is None:
        raise LayoutError(f"unknown object {obj}")
    if piece["dropped"]:
        raise LayoutError(f"{obj} is left out; restore it first")
    return piece["owner"] or new_core(d, [obj])


def core_centre(d: Dict, uid: str) -> Tuple[float, float]:
    """Area-weighted centre of a core's pieces (level 0, (y, x))."""
    ps = [d["pieces"][p] for p in d["cores"][uid]["pieces"]]
    w = np.array([p["r"] ** 2 for p in ps])
    return (float(np.dot(w, [p["cy"] for p in ps]) / w.sum()),
            float(np.dot(w, [p["cx"] for p in ps]) / w.sum()))


def holder_of(d: Dict, key: str) -> Optional[str]:
    for uid, c in d["cores"].items():
        if c["position_key"] == key:
            return uid
    return None


# ── target records (plan §1.1) ──────────────────────────────────────────

def _order_key(order: str):
    if order in ("column", "column_rev"):
        return lambda p: (p["x"], p["y"])
    if order in ("row", "row_rev"):
        return lambda p: (p["y"], p["x"])
    raise LayoutError(f"unknown order {order!r}; one of {ORDERS}")


def target_records(study: Dict, start_id: int, end_id: int, order: str) -> List[Dict]:
    """The map records from `start_id` to `end_id` (position ids) walking
    in `order` -- across columns / rows too (user ruling 2026-10-18: a line
    may be drawn along a row, a column or a diagonal); empty positions are
    skipped, missing ones are not (there is no way to know them here). Each record: position_id, x, y, label,
    donor, sample, position_key."""
    from .cores import position_key
    sheet = study["map_sheet"]
    ps = sorted(sheet["positions"], key=_order_key(order))
    if order.endswith("_rev"):
        ps.reverse()
    ids = [p["position_id"] for p in ps]
    if start_id not in ids or end_id not in ids:
        raise LayoutError(f"positions {start_id} / {end_id} are not on the map")
    i, j = ids.index(start_id), ids.index(end_id)
    if i > j:
        raise LayoutError("the end comes before the start in this order; choose the reverse order")
    out = []
    for p in ps[i:j + 1]:
        if not p["donor_block_id"]:
            continue
        out.append(_record(study, p, position_key(study["slide_id"], sheet["sha256"], p["position_id"])))
    return out


def records_by_ids(study: Dict, ids: Sequence[int]) -> List[Dict]:
    """A custom target list, in the order given; an empty position or a
    repeated id is refused."""
    from .cores import position_key
    sheet = study["map_sheet"]
    by = {p["position_id"]: p for p in sheet["positions"]}
    if len(set(ids)) != len(ids):
        raise LayoutError("a record is listed twice")
    out = []
    for i in ids:
        p = by.get(int(i))
        if p is None:
            raise LayoutError(f"position {i} is not on the map")
        if not p["donor_block_id"]:
            raise LayoutError(f"position {i} is empty on the map")
        out.append(_record(study, p, position_key(study["slide_id"], sheet["sha256"], p["position_id"])))
    return out


def _record(study, p, key) -> Dict:
    return {"position_id": int(p["position_id"]), "x": int(p["x"]), "y": int(p["y"]),
            "label": f"X{p['x']}-Y{int(p['y']):02d}", "donor": p["donor_block_id"],
            "sample": p["sample_id"], "position_key": key}


# ── image order (plan §1.2, §1.3) ───────────────────────────────────────

def band_select(objects: Sequence[Tuple[str, float, float]], p0: Tuple[float, float],
                p1: Tuple[float, float], half_width: float) -> List[str]:
    """Objects ``(id, y, x)`` whose centre lies within `half_width` of the
    segment p0 -> p1 (level 0, (y, x)), ordered along it (ties by id)."""
    (y0, x0), (y1, x1) = p0, p1
    dy, dx = y1 - y0, x1 - x0
    length = math.hypot(dy, dx)
    if length == 0:
        raise LayoutError("the line has no length")
    uy, ux = dy / length, dx / length
    hits = []
    for oid, y, x in objects:
        t = (y - y0) * uy + (x - x0) * ux
        off = abs(-(y - y0) * ux + (x - x0) * uy)
        if off <= half_width and -half_width <= t <= length + half_width:
            hits.append((t, oid))
    return [oid for _t, oid in sorted(hits)]


# ── binding (plan §1.4, §1.5) ───────────────────────────────────────────

def plan_binding(d: Dict, objects: Sequence[str], records: Sequence[Dict]) -> Dict:
    """The preview of binding `objects` (cores or pieces, image order) to
    `records` one by one. ``ok`` is False when the counts differ or an
    object repeats; ``conflicts`` lists both kinds: an object that already
    has another number, and a record held by another, unselected core."""
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
    """Bind as previewed. Conflicts need ``rebind=True``; a core pushed off
    its record becomes unmatched, its pieces kept. Returns the bound core
    uids in order; records the operation."""
    if not plan["ok"]:
        raise LayoutError("; ".join(plan["problems"]))
    if plan["conflicts"] and not rebind:
        raise LayoutError(f"{len(plan['conflicts'])} conflicts; confirm the rebind")
    uids = [core_of_object(d, p["object"]) for p in plan["pairs"]]
    targets = {p["record"]["position_key"] for p in plan["pairs"]}
    for uid, c in d["cores"].items():                 # free every target record and every bound core
        if c["position_key"] in targets or uid in uids:
            if c["position_key"] and c["position_key"] not in targets:
                d["positions"][c["position_key"]]["observed"] = None
            c["position_key"], c["mapping"] = None, "unmatched"
    for uid, p in zip(uids, plan["pairs"]):
        c = d["cores"][uid]
        c["position_key"], c["mapping"], c["custom_label"] = p["record"]["position_key"], "matched", None
        d["positions"][c["position_key"]]["observed"] = "present"
        d["positions"][c["position_key"]]["missing_reason"] = ""
    d["operations"].append(dict(extra or {}, kind="bind", method=method, at=_now(),
                                cores=uids, position_keys=[p["record"]["position_key"] for p in plan["pairs"]]))
    return uids


def swap(d: Dict, uid_a: str, uid_b: str) -> None:
    """Exchange the numbers of two numbered cores (a custom or unnumbered
    core takes a held record by an explicit rebind instead)."""
    a, b = d["cores"][uid_a], d["cores"][uid_b]
    if not (a["position_key"] and b["position_key"]):
        raise LayoutError("only two numbered cores can exchange their numbers; use a rebind")
    a["position_key"], b["position_key"] = b["position_key"], a["position_key"]
    a["mapping"] = b["mapping"] = "matched"            # an explicit exchange confirms both (ruling)
    d["operations"].append({"kind": "swap", "at": _now(), "cores": [uid_a, uid_b]})


def set_custom(d: Dict, obj: str, label: str) -> str:
    """A number that is not on the map: kept as a custom core, information
    to be completed (no position, so it cannot be locked as is)."""
    if not label.strip():
        raise LayoutError("a custom number needs text")
    uid = core_of_object(d, obj)
    c = d["cores"][uid]
    if c["position_key"]:
        d["positions"][c["position_key"]]["observed"] = None
    c["position_key"], c["custom_label"], c["mapping"] = None, label.strip(), "unmatched"
    d["operations"].append({"kind": "custom", "at": _now(), "core": uid, "label": label.strip()})
    return uid


def confirm_number(d: Dict, uid: str) -> None:
    c = d["cores"][uid]
    if c["mapping"] != "review":
        return
    if not c["position_key"]:
        raise LayoutError("the core has no number to confirm")
    c["mapping"] = "matched"
    d["operations"].append({"kind": "confirm", "at": _now(), "core": uid})


# ── merge (plan §2.2) ───────────────────────────────────────────────────

def merge(d: Dict, uid: str, piece_id: str) -> None:
    """Put a piece into core `uid`; a piece that was a core of its own
    gives up that core (its number too, after the preview of the page).
    The receiving core's number goes to review."""
    piece = d["pieces"][piece_id]
    if piece["dropped"]:
        raise LayoutError(f"{piece_id} is left out; restore it first")
    old = piece["owner"]
    if old == uid:
        return
    if old is not None:
        d["cores"][old]["pieces"].remove(piece_id)
        if not d["cores"][old]["pieces"]:
            _delete_core(d, old)
        else:
            _to_review(d, old)
    piece["owner"] = uid
    d["cores"][uid]["pieces"].append(piece_id)
    _to_review(d, uid)
    d["operations"].append({"kind": "merge", "at": _now(), "core": uid, "piece": piece_id})


def unmerge(d: Dict, piece_id: str) -> None:
    piece = d["pieces"][piece_id]
    uid = piece["owner"]
    if uid is None:
        return
    core = d["cores"][uid]
    core["pieces"].remove(piece_id)
    piece["owner"] = None
    if not core["pieces"]:
        _delete_core(d, uid)
    else:
        _to_review(d, uid)
    d["operations"].append({"kind": "unmerge", "at": _now(), "core": uid, "piece": piece_id})


def _delete_core(d, uid):
    key = d["cores"][uid]["position_key"]
    if key:
        d["positions"][key]["observed"] = None
    del d["cores"][uid]


def _to_review(d, uid):
    c = d["cores"][uid]
    if c["position_key"]:
        c["mapping"] = "review"


# ── marks (plan §2.3, §2.4) ─────────────────────────────────────────────

def drop_piece(d: Dict, piece_id: str, reason: str) -> None:
    """Leave a piece out ("不纳入"); undone by `restore_piece`."""
    unmerge(d, piece_id)
    d["pieces"][piece_id]["dropped"] = reason or "left out"
    d["operations"].append({"kind": "drop", "at": _now(), "piece": piece_id, "reason": reason})


def restore_piece(d: Dict, piece_id: str) -> None:
    d["pieces"][piece_id]["dropped"] = None
    d["operations"].append({"kind": "restore", "at": _now(), "piece": piece_id})


def set_missing(d: Dict, key: str, missing: bool = True, reason: str = "") -> None:
    """Confirm that a planned position has no tissue on the slide (with an
    optional reason), or take the confirmation back."""
    if missing and holder_of(d, key):
        raise LayoutError("a core is bound to this position; unbind it first")
    d["positions"][key]["observed"] = "missing" if missing else None
    d["positions"][key]["missing_reason"] = (reason or "") if missing else ""
    d["operations"].append({"kind": "missing", "at": _now(), "position_key": key, "missing": missing,
                            "reason": reason or ""})


def set_qc(d: Dict, key: str, qc: str, reasons: Sequence[str] = ()) -> None:
    if qc not in QC:
        raise LayoutError(f"qc must be one of {QC}")
    if qc == "excluded" and not reasons:
        raise LayoutError("an exclusion needs a reason")
    d["positions"][key]["qc"], d["positions"][key]["qc_reasons"] = qc, list(reasons)
    d["operations"].append({"kind": "qc", "at": _now(), "position_key": key, "qc": qc, "reasons": list(reasons)})


def unbind(d: Dict, uid: str) -> None:
    c = d["cores"][uid]
    if c["position_key"]:
        d["positions"][c["position_key"]]["observed"] = None
    c["position_key"], c["mapping"] = None, "unmatched"
    d["operations"].append({"kind": "unbind", "at": _now(), "core": uid})


def status_text(d: Dict, key: str, occupied: bool) -> str:
    """The table's status, derived from the separate fields."""
    if not occupied:
        return "空位"
    pos = d["positions"][key]
    uid = holder_of(d, key)
    if uid:
        part = "待复核" if d["cores"][uid]["mapping"] == "review" else "已对应"
    elif pos["observed"] == "missing":
        part = "缺失"
    else:
        part = "未对应"
    if pos["qc"] == "excluded":
        part += "·排除"
    return part


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
