"""Regions files (plan §3; Save instead of Lock, user 2026-10-18).

* ``<project>/tma/regions_draft.json`` -- the working copy, saved
  automatically (written atomically): nothing is lost between Saves.
* ``<project>/tma/regions.json`` -- the result, written by Save at any
  time and overwritten by the next one (no versions): the regions computed
  from the document (cores, exclusions never clipped) plus the whole
  document itself, so a reopen continues exactly where the user was; and
  ``tma_cores.parquet`` (F1's schema) when a map sheet is attached. A Save
  is never refused for unfinished work: what is still open
  (`open_issues`: the F3 core rules when a map sheet is attached, the
  exclusion checks always) is written into the file (``complete`` false)
  for later steps to see. The table is written first and ``regions.json``
  records its checksum: a Save interrupted between the two is seen by
  `table_matches`. Registered as ``regions_layout`` under the content's
  checksum (an unchanged Save registers nothing new and keeps its count).
* ``regions_v<n>.json`` -- the numbered versions an earlier build locked:
  only read, as the starting point when no ``regions.json`` exists yet.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from typing import Dict, List, Optional, Tuple

from . import geometry as G
from .document import (SCHEMA_VERSION, RegionsError, _now, core_centre, exclusion_type, piece_counter, shape_of,
                       status_text)

DRAFT_FILE = "regions_draft.json"
SAVED_FILE = "regions.json"
TABLE_FILE = "tma_cores.parquet"
DRAFT_POSITION_FIELDS = (("observed", None), ("missing_reason", ""), ("qc", "unreviewed"),
                         ("qc_reasons", []), ("run", False))


def _dir(project_dir):
    from ..tma.study import STUDY_DIR
    return os.path.join(project_dir, STUDY_DIR)


def draft_path(project_dir) -> str:
    return os.path.join(_dir(project_dir), DRAFT_FILE)


def saved_path(project_dir) -> str:
    return os.path.join(_dir(project_dir), SAVED_FILE)


def table_path(project_dir) -> str:
    return os.path.join(_dir(project_dir), TABLE_FILE)


def version_path(project_dir, version: int) -> str:
    return os.path.join(_dir(project_dir), f"regions_v{int(version)}.json")


def _numbered(project_dir, pre, suf):
    d = _dir(project_dir)
    out = set()
    if os.path.isdir(d):
        for f in os.listdir(d):
            if f.startswith(pre) and f.endswith(suf) and f[len(pre):-len(suf)].isdigit():
                out.add(int(f[len(pre):-len(suf)]))
    return out


def locked_versions(project_dir) -> List[int]:
    """The complete Regions versions (an interrupted lock is not listed)."""
    return sorted(v for v in _numbered(project_dir, "regions_v", ".json") if v >= 1)


# ── checks ──────────────────────────────────────────────────────────────

def _label(study, key) -> str:
    from ..tma.cores import position_key
    sheet = study["map_sheet"]
    for p in sheet["positions"]:
        if position_key(study["slide_id"], sheet["sha256"], p["position_id"]) == key:
            return f"X{p['x']}-Y{int(p['y']):02d}"
    return str(key)


def core_regions(d: Dict, include_excluded: bool = False) -> Dict[str, Dict]:
    """position_key -> region outline of every bound core; QC-excluded
    cores only with `include_excluded` (they write no region, but an
    exclusion lying in one still belongs to it)."""
    out = {}
    for uid, c in d["cores"].items():
        key = c["position_key"]
        if key and (include_excluded or d["positions"][key]["qc"] != "excluded"):
            out[key] = G.region_outline([d["pieces"][p]["polygon"] for p in c["pieces"]], shape_of(d))
    return out


def core_problems(d: Dict, study: Dict) -> List[str]:
    from ..tma.cores import position_key
    sheet = study["map_sheet"]
    out, held = [], {}
    for uid, c in d["cores"].items():
        if c["custom_label"]:
            out.append(f"custom core {c['custom_label']}: information to be completed")
        if c["mapping"] == "review":
            out.append(f"{_label(study, c['position_key'])}: number to be confirmed after a merge")
        if c["position_key"]:
            if c["position_key"] in held:
                out.append(f"{_label(study, c['position_key'])}: two cores")
            held[c["position_key"]] = uid
        elif not c["custom_label"]:
            out.append(f"a core at {tuple(round(v) for v in core_centre(d, uid))} has no number")
    for pid, p in d["pieces"].items():
        if p["owner"] is None and not p["dropped"]:
            y, x = G.centroid(p["polygon"])
            out.append(f"piece {pid} at ({round(y)}, {round(x)}) belongs to no core")
    for pos in sheet["positions"]:
        if not pos["donor_block_id"]:
            continue
        key = position_key(study["slide_id"], sheet["sha256"], pos["position_id"])
        st = d["positions"][key]
        if key in held:
            if st["observed"] != "present":
                out.append(f"{_label(study, key)}: bound but not marked present")
        elif st["observed"] != "missing":
            out.append(f"{_label(study, key)}: neither bound nor confirmed missing")
        if st["run"] and (key not in held or st["qc"] == "excluded"):
            out.append(f"{_label(study, key)}: ticked to run but not a bound, included core")
    regs = {}
    for key, g in core_regions(d).items():
        if not G.has_area(g):
            out.append(f"{_label(study, key)}: the outline is degenerate (outside the slide?)")
        else:
            regs[key] = g
    keys = sorted(regs)
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if G.overlap(regs[a], regs[b]):
                out.append(f"{_label(study, a)} and {_label(study, b)}: the regions overlap")
    return out


def exclusion_regions(d: Dict) -> Dict[str, Dict]:
    """exclusion_uid -> outline + type + region_id."""
    from ..project_identity import region_id
    out = {}
    for uid, e in d["exclusions"].items():
        g = G.region_outline([e["polygon"]], shape_of(d))
        g["type"] = exclusion_type(e["reasons"])
        g["region_id"] = region_id(d["slide_id"], g["type"], g["bbox_fullres"], g["polygon_fullres"]) \
            if G.has_area(g) else None
        out[uid] = g
    return out


def exclusion_problems(d: Dict, channel_names: Optional[List[str]] = None) -> List[str]:
    out, seen = [], {}
    for uid, g in exclusion_regions(d).items():
        e = d["exclusions"][uid]
        if g["region_id"] is None:
            out.append(f"exclusion {uid}: the outline is degenerate")
        elif g["region_id"] in seen:
            out.append(f"exclusions {seen[g['region_id']]} and {uid}: the same shape and type")
        else:
            seen[g["region_id"]] = uid
        if not e["reasons"]:
            out.append(f"exclusion {uid}: no reason")
        if channel_names is not None and e["channels"] != "all":
            unknown = [c for c in e["channels"] if c not in channel_names]
            if unknown:
                out.append(f"exclusion {uid}: unknown channels {unknown}")
    return out


def open_issues(d: Dict, study: Optional[Dict], channel_names: Optional[List[str]] = None) -> List[str]:
    """What is still unfinished ([] = complete): never blocks a Save. The
    core part only when a map sheet is attached. Raises only when the
    document and the study do not belong together."""
    if d.get("map_sha256") and study is None:
        raise RegionsError("the document has a map sheet: its study is needed to check the cores")
    if study is not None and d.get("map_sha256") and study["map_sheet"]["sha256"] != d["map_sha256"]:
        raise RegionsError("the study's map sheet is not the document's")
    out = core_problems(d, study) if d.get("map_sha256") else []
    return out + exclusion_problems(d, channel_names)


def exclusion_cores(d: Dict, excl: Dict[str, Dict], cores: Dict[str, Dict]) -> Dict[str, Optional[str]]:
    """exclusion_uid -> core_uid it overlaps most, or None (plan §3)."""
    by_key = {c["position_key"]: uid for uid, c in d["cores"].items() if c["position_key"]}
    out = {}
    for uid, g in excl.items():
        best = None
        if g["region_id"] is not None:
            for key, cg in cores.items():
                a = G.overlap_area(g, cg)
                if a > 0 and (best is None or a > best[0]):
                    best = (a, by_key[key])
        out[uid] = best[1] if best else None
    return out


# ── save ────────────────────────────────────────────────────────────────

def save(project_dir, d: Dict, study: Optional[Dict], channel_names: Optional[List[str]] = None) -> Dict:
    """Write ``regions.json`` (and the core table), replacing the last
    Save, and register ``regions_layout``. Unfinished work is saved as it
    is and listed in ``open_issues``; a core whose outline has no area gets
    no region."""
    from .. import provenance as prov
    from ..project_identity import region_id
    issues = open_issues(d, study, channel_names)
    if study is not None and d.get("map_sha256") is None and study.get("map_sheet"):
        issues.insert(0, "the map sheet is chosen but the cores are not found yet")
    path = saved_path(project_dir)
    try:
        old = load_saved(project_dir)
    except (RegionsError, ValueError, OSError):
        old = None
    content = _content_sha(d, issues)
    same = old is not None and old.get("content_sha256") == content
    count = int(old.get("save_count", 0)) + (0 if same else 1) if old else 1   # an unchanged Save: the same count
    shape = shape_of(d)
    cores_out, table, table_sha = {}, None, None
    creg = {k: g for k, g in (core_regions(d) if d.get("map_sha256") else {}).items() if G.has_area(g)}
    if study is not None and d.get("map_sha256"):
        from ..tma import cores as tc
        occupied = {p["position_id"]: bool(p["donor_block_id"]) for p in study["map_sheet"]["positions"]}
        held = {c["position_key"]: uid for uid, c in d["cores"].items() if c["position_key"]}
        rows = []
        for row in tc.draft_rows(study):
            key = row["position_key"]
            row["layout_version"] = count
            row["status"] = status_text(d, key, occupied[row["position_id"]])
            uid = held.get(key)
            if uid:
                rid = None
                if key in creg:
                    g = dict(creg[key], pieces=[d["pieces"][p]["polygon"] for p in d["cores"][uid]["pieces"]])
                    g["region_id"] = rid = region_id(d["slide_id"], "TMA_core", g["bbox_fullres"], g["polygon_fullres"])
                    row["region_id"], row["geometry"] = rid, json.dumps(g, separators=(",", ":"))
                cores_out[uid] = {"position_key": key, "pieces": list(d["cores"][uid]["pieces"]),
                                  "mapping": d["cores"][uid]["mapping"], "region_id": rid}
            rows.append(row)
        table = tc.write_table(project_dir, rows, count, path=table_path(project_dir))
        with open(table, "rb") as f:
            table_sha = hashlib.sha256(f.read()).hexdigest()
    excl = exclusion_regions(d)
    links = exclusion_cores(d, excl, {k: g for k, g in (core_regions(d, include_excluded=True)
                                                        if d.get("map_sha256") else {}).items() if G.has_area(g)})
    excl_out = {uid: dict(d["exclusions"][uid], type=g["type"], region_id=g["region_id"],
                          bbox_fullres=g["bbox_fullres"], core_uid=links[uid])
                for uid, g in excl.items()}
    doc = copy.deepcopy(d)
    doc["session"] = None
    rec = {"schema_version": SCHEMA_VERSION, "kind": "regions_layout", "save_count": count,
           "saved_at": _now(), "slide_id": d["slide_id"],
           "slide_shape": list(shape), "map_sha256": d.get("map_sha256"),
           "candidates_version": d.get("candidates_version"), "core_diameter_px": d.get("core_diameter_px"),
           "coordinates": "level-0 pixels, origin top-left, x right, y down; bbox [y0, y1, x0, x1] half-open",
           "complete": not issues, "open_issues": issues, "content_sha256": content,
           "table": {"file": TABLE_FILE, "sha256": table_sha, "layout_version": count} if table else None,
           "pieces": copy.deepcopy(d["pieces"]), "cores": cores_out, "positions": copy.deepcopy(d["positions"]),
           "exclusions": excl_out, "handoffs": copy.deepcopy(d["handoffs"]),
           "piece_counter": piece_counter(d),
           "run": sorted(k for k, v in d["positions"].items() if v["run"]),
           "document": doc}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    prov.write_json_atomic(path, rec)
    entries = prov.load_entries(project_dir)
    deps = [e["artifact_id"] for e in entries if e["kind"] == "raw_slide" and e["artifact_id"] == d["slide_id"]]
    if d.get("map_sha256"):
        deps += [e["artifact_id"] for e in entries
                 if e["kind"] == "tma_map_sheet" and e.get("token") == d["map_sha256"]][-1:]
        deps += [e["artifact_id"] for e in entries if e["kind"] == "tma_dearray"
                 and e.get("parameters", {}).get("version") == d.get("candidates_version")][-1:]
    unresolved = [{"role": "step0_handoff", "workspace_id": h["workspace_id"], "path": h["path"],
                   "sha256": h["sha256"]} for _k, h in sorted(d["handoffs"].items())]
    whole = region_id(d["slide_id"], "full_wsi", [0, shape[0], 0, shape[1]])   # the document covers the slide
    token = content                               # the content, not the save time: an unchanged Save registers nothing new
    aid = prov.safe_register(project_dir, "regions_layout", prov.location(project_dir, path), token,
                             slide_id=d["slide_id"], depends_on=deps, operates_on=[whole],
                             unresolved_inputs=unresolved,
                             parameters={"save_count": count, "scope": "full_wsi", "complete": not issues,
                                         "open_issues": len(issues), "cores": len(cores_out),
                                         "exclusions": len(excl_out),
                                         "table": os.path.basename(table) if table else None},
                             software={"module": "core.regions.lock", "commit": prov.git_commit()})
    if aid is None:
        print(f"[Regions] {path} saved but NOT registered in the provenance")
    return rec


def _content_sha(d: Dict, issues: List[str]) -> str:
    """What a Save holds, without when it was made or the workspace open
    at the time."""
    doc = dict(d, session=None)
    return hashlib.sha256(json.dumps([doc, issues], sort_keys=True, default=str).encode()).hexdigest()


def table_matches(project_dir, rec: Dict) -> bool:
    """Whether ``tma_cores.parquet`` is the table of this Save (False: a
    Save was interrupted between the two files -- Save again)."""
    t = rec.get("table")
    if t is None:
        return not os.path.isfile(table_path(project_dir)) or rec.get("map_sha256") is None
    if not os.path.isfile(table_path(project_dir)):
        return False
    with open(table_path(project_dir), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest() == t["sha256"]


def load_saved(project_dir, slide_id: Optional[str] = None) -> Optional[Dict]:
    """The last Save, or None."""
    path = saved_path(project_dir)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        rec = json.load(f)
    if rec.get("schema_version") != SCHEMA_VERSION or rec.get("kind") != "regions_layout" or "document" not in rec:
        raise RegionsError(f"{path}: not a Regions file this program reads")
    _check_slide(path, rec, slide_id)
    return rec


def draft_from_saved(rec: Dict) -> Dict:
    """The working document of a Save, exactly as it was."""
    d = copy.deepcopy(rec["document"])
    d["session"] = None
    d["piece_counter"] = piece_counter(d)
    return d


def _check_slide(what, rec, slide_id):
    if slide_id is not None and rec.get("slide_id") != slide_id:
        raise RegionsError(f"{what} is for another slide ({rec.get('slide_id')})")


def load_locked(project_dir, version: int, slide_id: Optional[str] = None) -> Dict:
    with open(version_path(project_dir, version), "r", encoding="utf-8") as f:
        rec = json.load(f)
    if rec.get("schema_version") != SCHEMA_VERSION or rec.get("kind") != "regions_layout":
        raise RegionsError(f"regions_v{version}: not a Regions version this program reads")
    _check_slide(f"regions_v{version}", rec, slide_id)
    return rec


def draft_from_locked(rec: Dict) -> Dict:
    """A new draft based on a locked version."""
    from .document import new_document
    d = new_document(rec["slide_id"], rec["slide_shape"], based_on=rec["version"])
    d.update(map_sha256=rec.get("map_sha256"), candidates_version=rec.get("candidates_version"),
             core_diameter_px=rec.get("core_diameter_px"), pieces=copy.deepcopy(rec["pieces"]),
             handoffs=copy.deepcopy(rec.get("handoffs", {})))
    from .document import piece_counter
    d["piece_counter"] = int(rec.get("piece_counter") or 0)
    d["piece_counter"] = piece_counter(d)
    d["positions"] = {k: {f: copy.deepcopy(v.get(f, dflt)) for f, dflt in DRAFT_POSITION_FIELDS}
                      for k, v in rec["positions"].items()}
    for p in d["pieces"].values():                    # an owner the version does not list: none
        if p.get("owner") not in rec["cores"]:
            p["owner"] = None
    for uid, c in rec["cores"].items():
        d["cores"][uid] = {"pieces": list(c["pieces"]), "position_key": c["position_key"], "custom_label": None,
                           "mapping": "matched"}
        for p in c["pieces"]:
            d["pieces"][p]["owner"] = uid
    keep = ("polygon", "reasons", "channels", "note", "created_in_workspace")
    d["exclusions"] = {uid: {k: copy.deepcopy(e[k]) for k in keep} for uid, e in rec["exclusions"].items()}
    return d


# ── draft file ──────────────────────────────────────────────────────────

def save_draft(project_dir, d: Dict) -> str:
    from .. import provenance as prov
    path = draft_path(project_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    prov.write_json_atomic(path, d)
    return path


def load_draft(project_dir, slide_id: Optional[str] = None) -> Optional[Dict]:
    """The draft, or None. Its session is cleared: whoever opens it sets
    the current workspace again (`document.set_session`)."""
    path = draft_path(project_dir)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    if d.get("schema_version") != SCHEMA_VERSION:
        raise RegionsError(f"{path}: schema_version {d.get('schema_version')!r}")
    _check_slide(path, d, slide_id)
    d["session"] = None
    return d


def discard_draft(project_dir) -> None:
    path = draft_path(project_dir)
    if os.path.isfile(path):
        os.remove(path)
