"""The Regions annotation document (docs/region_qc_module_plan.md §3): one
JSON-able dict per slide holding the TMA pieces, cores and position marks
and the blur / fold exclusions. Pure functions; the page keeps the draft,
its undo history and the drawing.

* Pieces -- tissue pieces, each a polygon (whole level-0 pixels): a
  detected candidate (its outline, format 2) or a shape the user drew.
  ``owner`` is the core it belongs to (None = unassigned); ``dropped`` is
  the reason it is left out, None otherwise.
* Cores -- ``core_uid`` (never changes) -> pieces, the bound map position
  (F1 ``position_key``), a custom label, ``mapping`` matched / unmatched /
  review.
* Positions -- per ``position_key``: ``observed`` present / missing / None
  (+ reason), ``qc`` unreviewed / accepted / excluded (+ reasons), ``run``.
  The map use comes from the map sheet; the four are kept apart.
* Exclusions -- ``exclusion_uid`` (never changes) -> one polygon, reasons
  (blur / fold / other), channels ("all" or names), note, the workspace it
  was drawn in. Stored as drawn, never clipped (plan §3).
* ``handoffs`` -- the Step0 handoff of every workspace the document was
  edited in (``set_session``), for the lock's provenance.

Numbering (target records, binding) is in `numbering`; the lock and the
files in `lock`.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime
from typing import Dict, Optional, Sequence, Tuple

from . import geometry as G

SCHEMA_VERSION = 2              # 2: the Regions document (F3's TMA drafts were 1)
CORE_PIECE_FRAC = 0.5           # a detected piece of at least this share of a core's area starts as its own core
MAPPING = ("matched", "unmatched", "review")
QC = ("unreviewed", "accepted", "excluded")
REASONS = ("blur", "fold", "other")


class RegionsError(ValueError):
    pass


# ── the document ────────────────────────────────────────────────────────

def new_document(slide_id: str, slide_shape: Tuple[int, int], based_on: Optional[int] = None) -> Dict:
    if not slide_id:
        raise RegionsError("a document needs the slide id")
    return {"schema_version": SCHEMA_VERSION, "slide_id": slide_id, "slide_shape": [int(v) for v in slide_shape],
            "based_on": based_on, "map_sha256": None, "candidates_version": None, "core_diameter_px": None,
            "pieces": {}, "cores": {}, "positions": {}, "exclusions": {}, "handoffs": {},
            "operations": [], "coverage_confirmed": None, "session": None, "piece_counter": 0}


def attach_tma(d: Dict, study: Dict, candidates: Dict) -> None:
    """(Re)build the TMA part from the study (F1) and a candidate list (F2):
    every candidate is a piece (its outline, or a 64-gon for a format-1
    list); one of at least `CORE_PIECE_FRAC` of a core's area starts as its
    own unmatched core, a smaller one stays unassigned. Exclusions stay."""
    from ..tma.cores import position_key
    if study["slide_id"] != d["slide_id"]:
        raise RegionsError("the study is for another slide")
    sheet = study["map_sheet"]
    shape = tuple(d["slide_shape"])
    dia = float(candidates["core_diameter_px"])
    core_area = math.pi * (dia / 2) ** 2
    d["piece_counter"] = piece_counter(d)          # the old pieces' highest id, before they go
    d.update(map_sha256=sheet["sha256"], candidates_version=int(candidates["version"]), core_diameter_px=dia,
             pieces={}, cores={}, positions={})
    for p in sheet["positions"]:
        key = position_key(study["slide_id"], sheet["sha256"], p["position_id"])
        d["positions"][key] = {"observed": None, "missing_reason": "", "qc": "unreviewed", "qc_reasons": [],
                               "run": False}
    for c in candidates["candidates"]:
        pid = f"p{int(c['candidate_id'])}"
        poly = [list(q) for q in c.get("outline") or []]
        if len(poly) < 3:
            poly = G.circle_polygon(c["cy"], c["cx"], c["radius"], shape)
        d["pieces"][pid] = {"polygon": poly, "source": "detected", "candidate_id": int(c["candidate_id"]),
                            "tissue": c.get("tissue_fraction"), "owner": None, "dropped": None}
        if c["area"] >= CORE_PIECE_FRAC * core_area:
            new_core(d, [pid])
    d["piece_counter"] = max(piece_counter(d), *[int(c["candidate_id"]) for c in candidates["candidates"]] or [0])
    log(d, {"kind": "attach_tma", "candidates_version": d["candidates_version"]})


def set_session(d: Dict, workspace_id: str, handoff_path: str, handoff_sha256: str) -> None:
    """The workspace the document is being edited in (not an edit: no undo
    step). Every later operation records it; the lock lists all of them."""
    d["session"] = {"workspace_id": workspace_id or "", "handoff_path": handoff_path or "",
                    "handoff_sha256": handoff_sha256 or ""}
    if workspace_id:
        # keyed by workspace AND content: a Step0 saved again in between is
        # another input, not a replacement of the first
        d["handoffs"][f"{workspace_id}|{handoff_sha256 or ''}"] = {
            "workspace_id": workspace_id, "path": handoff_path or "", "sha256": handoff_sha256 or ""}


def log(d: Dict, op: Dict) -> None:
    s = d.get("session") or {}
    d["operations"].append(dict(op, at=_now(), workspace_id=s.get("workspace_id", ""),
                                handoff_sha256=s.get("handoff_sha256", "")))


def shape_of(d: Dict) -> Tuple[int, int]:
    return tuple(d["slide_shape"])


# ── pieces and cores ────────────────────────────────────────────────────

def piece_counter(d) -> int:
    """The highest piece number used in this version line (plan §9.1): kept
    in the document; an old document without it starts at its highest id."""
    have = max([int(k[1:]) for k in d["pieces"] if k[1:].isdigit()] or [0])
    return max(int(d.get("piece_counter") or 0), have)


def _next_piece_id(d):
    n = piece_counter(d) + 1
    d["piece_counter"] = n                     # never reused, even after a deletion
    return f"p{n}"


def add_piece(d: Dict, polygon) -> str:
    """A shape the user drew (a circle is drawn as a 64-gon): a new
    unassigned piece."""
    poly = [list(map(int, q)) for q in polygon]
    if len(poly) < 3 or G.area(poly) <= 0:
        raise RegionsError("a piece needs a polygon with an area")
    pid = _next_piece_id(d)
    d["pieces"][pid] = {"polygon": poly, "source": "drawn", "candidate_id": None, "tissue": None,
                        "owner": None, "dropped": None}
    log(d, {"kind": "add_piece", "piece": pid})
    return pid


def add_circle_piece(d: Dict, cy: float, cx: float, r: float) -> str:
    if not (r > 0 and all(math.isfinite(v) for v in (cy, cx, r))):
        raise RegionsError("a circle needs a finite centre and a positive radius")
    return add_piece(d, G.circle_polygon(cy, cx, r, shape_of(d)))


def add_drawn_core(d: Dict, polygon) -> str:
    """A shape drawn in the Cores tab (user 2026-10-18): a new piece that is
    at once its own core -- not numbered, shown like a Find-cores core.
    Returns the piece id."""
    pid = add_piece(d, polygon)
    new_core(d, [pid])
    return pid


def new_core(d: Dict, piece_ids: Sequence[str]) -> str:
    uid = "core_" + uuid.uuid4().hex[:12]
    d["cores"][uid] = {"pieces": list(piece_ids), "position_key": None, "custom_label": None,
                       "mapping": "unmatched"}
    for pid in piece_ids:
        d["pieces"][pid]["owner"] = uid
    return uid


def core_of_object(d: Dict, obj: str) -> str:
    """A core uid as is; a piece -> its owner, or a new core made of it."""
    if obj in d["cores"]:
        return obj
    piece = d["pieces"].get(obj)
    if piece is None:
        raise RegionsError(f"unknown object {obj}")
    if piece["dropped"]:
        raise RegionsError(f"{obj} is left out; restore it first")
    return piece["owner"] or new_core(d, [obj])


def core_centre(d: Dict, uid: str) -> Tuple[float, float]:
    """Area-weighted centre of a core's pieces (level 0, (y, x))."""
    ps = [d["pieces"][p]["polygon"] for p in d["cores"][uid]["pieces"]]
    ws = [max(G.area(p), 1.0) for p in ps]
    cs = [G.centroid(p) for p in ps]
    tot = sum(ws)
    return (sum(w * c[0] for w, c in zip(ws, cs)) / tot, sum(w * c[1] for w, c in zip(ws, cs)) / tot)


def holder_of(d: Dict, key: str) -> Optional[str]:
    for uid, c in d["cores"].items():
        if c["position_key"] == key:
            return uid
    return None


def merge(d: Dict, uid: str, piece_id: str) -> None:
    """Put a piece into core `uid`; the receiving core's number goes to
    review (and the giving core's, if it keeps other pieces)."""
    piece = d["pieces"][piece_id]
    if piece["dropped"]:
        raise RegionsError(f"{piece_id} is left out; restore it first")
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
    log(d, {"kind": "merge", "core": uid, "piece": piece_id})


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
    log(d, {"kind": "unmerge", "core": uid, "piece": piece_id})


def release_position(d, key):
    """A position left without its core (plan §9.1): what was seen there is
    unconfirmed again, its QC judgement stays, its Run tick goes."""
    pos = d["positions"][key]
    pos["observed"], pos["missing_reason"], pos["run"] = None, "", False


def _delete_core(d, uid):
    key = d["cores"][uid]["position_key"]
    if key:
        release_position(d, key)
    del d["cores"][uid]


def _to_review(d, uid):
    c = d["cores"][uid]
    if c["position_key"]:
        c["mapping"] = "review"


def drop_piece(d: Dict, piece_id: str, reason: str) -> None:
    unmerge(d, piece_id)
    d["pieces"][piece_id]["dropped"] = reason or "left out"
    log(d, {"kind": "drop", "piece": piece_id, "reason": reason})


def restore_piece(d: Dict, piece_id: str) -> None:
    """Back as an unassigned piece (undo restores an earlier ownership)."""
    d["pieces"][piece_id]["dropped"] = None
    log(d, {"kind": "restore", "piece": piece_id})


# ── shape edits (pieces and exclusions alike) ──────────────────────────

def _shape_owner(d: Dict, ident: str) -> Dict:
    if ident in d["pieces"]:
        return d["pieces"][ident]
    if ident in d["exclusions"]:
        return d["exclusions"][ident]
    raise RegionsError(f"unknown shape {ident}")


def translate_shape(d: Dict, ident: str, dy: float, dx: float) -> None:
    o = _shape_owner(d, ident)
    o["polygon"] = G.translate(o["polygon"], dy, dx, shape_of(d))
    log(d, {"kind": "move", "shape": ident})


def scale_shape(d: Dict, ident: str, factor: float) -> None:
    o = _shape_owner(d, ident)
    poly = G.scale(o["polygon"], factor, shape_of(d))
    if G.area(poly) <= 0:
        raise RegionsError("the shape would vanish")
    o["polygon"] = poly
    log(d, {"kind": "scale", "shape": ident, "factor": factor})


def move_vertex(d: Dict, ident: str, index: int, y: float, x: float) -> None:
    o = _shape_owner(d, ident)
    if not 0 <= index < len(o["polygon"]):
        raise RegionsError("no such vertex")
    o["polygon"] = G.move_vertex(o["polygon"], index, y, x, shape_of(d))
    log(d, {"kind": "vertex", "shape": ident, "index": index})


def set_polygon(d: Dict, ident: str, polygon) -> None:
    """Replace a shape at once (the end of a drag)."""
    poly = [list(map(int, q)) for q in polygon]
    if len(poly) < 3 or G.area(poly) <= 0:
        raise RegionsError("a shape needs a polygon with an area")
    _shape_owner(d, ident)["polygon"] = poly
    log(d, {"kind": "reshape", "shape": ident})


# ── position marks ──────────────────────────────────────────────────────

def set_missing(d: Dict, key: str, missing: bool = True, reason: str = "") -> None:
    if missing and holder_of(d, key):
        raise RegionsError("a core is bound to this position; unbind it first")
    d["positions"][key]["observed"] = "missing" if missing else None
    d["positions"][key]["missing_reason"] = (reason or "") if missing else ""
    log(d, {"kind": "missing", "position_key": key, "missing": missing, "reason": reason or ""})


def set_qc(d: Dict, key: str, qc: str, reasons: Sequence[str] = ()) -> None:
    if qc not in QC:
        raise RegionsError(f"qc must be one of {QC}")
    if qc == "excluded" and not reasons:
        raise RegionsError("an exclusion needs a reason")
    d["positions"][key]["qc"], d["positions"][key]["qc_reasons"] = qc, list(reasons)
    log(d, {"kind": "qc", "position_key": key, "qc": qc, "reasons": list(reasons)})


def set_run(d: Dict, key: str, run: bool) -> None:
    d["positions"][key]["run"] = bool(run)
    log(d, {"kind": "run", "position_key": key, "run": bool(run)})


def unbind(d: Dict, uid: str) -> None:
    c = d["cores"][uid]
    if c["position_key"]:
        d["positions"][c["position_key"]]["observed"] = None
    c["position_key"], c["mapping"] = None, "unmatched"
    log(d, {"kind": "unbind", "core": uid})


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


# ── exclusions (plan §3) ────────────────────────────────────────────────

def _check_exclusion_fields(reasons, channels):
    if not reasons or any(r not in REASONS for r in reasons):
        raise RegionsError(f"an exclusion needs at least one reason of {REASONS}")
    if channels != "all" and (not isinstance(channels, (list, tuple)) or not channels
                              or not all(isinstance(c, str) and c for c in channels)):
        raise RegionsError('channels are "all" or a list of channel names')


def add_exclusion(d: Dict, polygon, reasons: Sequence[str], channels="all", note: str = "") -> str:
    poly = [list(map(int, q)) for q in polygon]
    if len(poly) < 3 or G.area(poly) <= 0:
        raise RegionsError("an exclusion needs a polygon with an area")
    reasons = sorted(set(reasons))
    _check_exclusion_fields(reasons, channels)
    uid = "excl_" + uuid.uuid4().hex[:12]
    s = d.get("session") or {}
    d["exclusions"][uid] = {"polygon": poly, "reasons": reasons,
                            "channels": "all" if channels == "all" else list(channels), "note": note or "",
                            "created_in_workspace": s.get("workspace_id", "")}
    log(d, {"kind": "add_exclusion", "exclusion": uid})
    return uid


def update_exclusion(d: Dict, uid: str, reasons=None, channels=None, note=None) -> None:
    e = d["exclusions"][uid]
    new_r = sorted(set(reasons)) if reasons is not None else e["reasons"]
    new_c = channels if channels is not None else e["channels"]
    _check_exclusion_fields(new_r, new_c)
    e["reasons"], e["channels"] = new_r, ("all" if new_c == "all" else list(new_c))
    if note is not None:
        e["note"] = note
    log(d, {"kind": "update_exclusion", "exclusion": uid})


def delete_exclusion(d: Dict, uid: str) -> None:
    del d["exclusions"][uid]
    log(d, {"kind": "delete_exclusion", "exclusion": uid})


def exclusion_type(reasons: Sequence[str]) -> str:
    """The A3 region type of an exclusion (plan §3): blur only -> blur, fold
    only -> fold, anything else -> manual_annotation."""
    rs = set(reasons)
    if rs == {"blur"}:
        return "blur"
    if rs == {"fold"}:
        return "fold"
    return "manual_annotation"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ── batch delete and merge (docs/regions_tools_redesign.md §9.1) ───────

def delete_pieces(d: Dict, piece_ids: Sequence[str]) -> None:
    """Delete pieces (they vanish -- user 2026-10-18): out of the document
    and out of their core; a core keeping other pieces goes to review, one
    losing its last piece goes and releases its position. The F2 candidate
    file is untouched."""
    ids = [p for p in dict.fromkeys(piece_ids) if p in d["pieces"]]
    d["piece_counter"] = piece_counter(d)          # an old document: fix its highest id before it goes
    touched = set()
    for pid in ids:
        uid = d["pieces"][pid]["owner"]
        if uid is not None and uid in d["cores"]:
            d["cores"][uid]["pieces"].remove(pid)
            touched.add(uid)
        del d["pieces"][pid]
    for uid in touched:
        if not d["cores"][uid]["pieces"]:
            _delete_core(d, uid)
        else:
            _to_review(d, uid)
    log(d, {"kind": "delete_pieces", "pieces": ids})


def delete_exclusions(d: Dict, uids: Sequence[str]) -> None:
    ids = [u for u in dict.fromkeys(uids) if u in d["exclusions"]]
    for u in ids:
        del d["exclusions"][u]
    log(d, {"kind": "delete_exclusions", "exclusions": ids})


def _position_number(d, key, study):
    from ..tma.cores import position_key
    sheet = study["map_sheet"] if study else None
    if sheet:
        for p in sheet["positions"]:
            if position_key(d["slide_id"], sheet["sha256"], p["position_id"]) == key:
                return int(p["position_id"])
    return 10 ** 9


def merge_plan(d: Dict, piece_ids: Sequence[str], study: Optional[Dict]) -> Dict:
    """What merging `piece_ids` would do: the receiving core (None = a new
    one), the cores whose numbers are released, the custom labels cleared.
    Rules (plan §9.1): by owning core, not by piece count; several numbered
    cores -> the smallest position number wins (custom labels give way to
    position numbers; among custom labels only, the alphabetically first)."""
    ids = [p for p in dict.fromkeys(piece_ids) if p in d["pieces"] and not d["pieces"][p]["dropped"]]
    if len(ids) < 2:
        raise RegionsError("select at least two pieces to merge")
    owners = [d["pieces"][p]["owner"] for p in ids]
    cores = [u for u in dict.fromkeys(o for o in owners if o)]
    if len(cores) == 1 and all(owners):
        return {"pieces": ids, "receiver": cores[0], "released": [], "cleared_labels": [], "noop": True}
    pos = [u for u in cores if d["cores"][u]["position_key"]]
    cus = [u for u in cores if not d["cores"][u]["position_key"] and d["cores"][u]["custom_label"]]
    if pos:
        receiver = min(pos, key=lambda u: _position_number(d, d["cores"][u]["position_key"], study))
    elif cus:
        receiver = min(cus, key=lambda u: d["cores"][u]["custom_label"])
    elif cores:
        receiver = max(cores, key=lambda u: (owners.count(u), -owners.index(u)))
    else:
        receiver = None
    released = [u for u in pos if u != receiver]
    cleared = [u for u in cus if u != receiver]
    return {"pieces": ids, "receiver": receiver, "released": released, "cleared_labels": cleared, "noop": False}


def merge_pieces(d: Dict, piece_ids: Sequence[str], study: Optional[Dict] = None) -> Optional[str]:
    """Merge the pieces into one core as `merge_plan` says (one undo step
    when called through the draft holder). Returns the receiving core."""
    plan = merge_plan(d, piece_ids, study)
    if plan["noop"]:
        return plan["receiver"]
    for u in plan["released"]:                    # their numbers are released
        key = d["cores"][u]["position_key"]
        d["cores"][u]["position_key"], d["cores"][u]["mapping"] = None, "unmatched"
        release_position(d, key)
    for u in plan["cleared_labels"]:
        d["cores"][u]["custom_label"] = None
    receiver = plan["receiver"]
    if receiver is None:
        receiver = new_core(d, [])
    moved = False
    for pid in plan["pieces"]:
        old = d["pieces"][pid]["owner"]
        if old == receiver:
            continue
        if old is not None:
            d["cores"][old]["pieces"].remove(pid)
            if not d["cores"][old]["pieces"]:
                del d["cores"][old]               # its number (if any) was released above
        d["pieces"][pid]["owner"] = receiver
        d["cores"][receiver]["pieces"].append(pid)
        moved = True
    if moved:
        _to_review(d, receiver)
    log(d, {"kind": "merge_pieces", "core": receiver, "pieces": plan["pieces"],
            "released": plan["released"], "cleared_labels": plan["cleared_labels"]})
    return receiver


def union_pieces(d: Dict, piece_ids: Sequence[str], study: Optional[Dict] = None) -> str:
    """Merge ▸ Union (user 2026-10-18, plan §10.5): the selected pieces fuse
    into ONE new piece (their outer outline, holes filled) owned by the core
    `merge_plan` picks -- that core keeps its id, number, QC and Run; the
    other numbered cores are released as in Group; unselected pieces stay.
    Pieces in one core fuse too. Returns the new piece id."""
    ids = [p for p in dict.fromkeys(piece_ids) if p in d["pieces"] and not d["pieces"][p]["dropped"]]
    if len(ids) < 2:
        raise RegionsError("select at least two pieces to fuse")
    outline, why = G.union_outline([d["pieces"][p]["polygon"] for p in ids], shape_of(d))
    if outline is None:
        raise RegionsError("these pieces are too far apart to fuse" if why == "too_far" else
                           "these pieces do not touch, so they cannot be fused; use Group to put them in one core")
    plan = merge_plan(d, ids, study)
    for u in plan["released"]:
        key = d["cores"][u]["position_key"]
        d["cores"][u]["position_key"], d["cores"][u]["mapping"] = None, "unmatched"
        release_position(d, key)
    for u in plan["cleared_labels"]:
        d["cores"][u]["custom_label"] = None
    receiver = plan["receiver"] or new_core(d, [])
    pid = _next_piece_id(d)                       # first: the receiver is never left empty
    d["pieces"][pid] = {"polygon": outline, "source": "union", "candidate_id": None, "tissue": None,
                        "owner": receiver, "dropped": None}
    d["cores"][receiver]["pieces"].append(pid)
    for old in ids:
        owner = d["pieces"].pop(old)["owner"]
        if owner is not None:
            d["cores"][owner]["pieces"].remove(old)
            if owner != receiver and not d["cores"][owner]["pieces"]:
                del d["cores"][owner]             # its number (if any) was released above
    if d["cores"][receiver]["position_key"]:
        _to_review(d, receiver)
    log(d, {"kind": "union_pieces", "core": receiver, "piece": pid, "from": ids,
            "released": plan["released"], "cleared_labels": plan["cleared_labels"]})
    return pid
