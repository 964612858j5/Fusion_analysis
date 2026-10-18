"""TMA layout lock (F3, docs/tma_f3_page_application.md §3.2, §3.3): the
checks before a lock, the locked version (``tma_cores_v<n>.parquet`` in
F1's schema + ``tma_layout_v<n>.json``, registered as ``tma_layout``) and
the draft file. Versions only count up; nothing is overwritten.
"""

from __future__ import annotations

import copy
import json
import os
from typing import Dict, List, Optional, Tuple

from .layout import SCHEMA_VERSION, LayoutError, _now, core_centre, status_text
from .layout_geometry import core_geometry, fill

# a draft position's own fields (the locked record adds map use, mapping, core uid)
DRAFT_POSITION_FIELDS = (("observed", None), ("missing_reason", ""), ("qc", "unreviewed"),
                         ("qc_reasons", []), ("run", False))


def lock_problems(d: Dict, study: Dict, shape: Tuple[int, int]) -> List[str]:
    """Why the draft cannot be locked; [] when it can."""
    from .cores import position_key
    from ..project_identity import normalize_polygon
    sheet = study["map_sheet"]
    out = []
    held = {}
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
            out.append(f"piece {pid} at ({round(p['cy'])}, {round(p['cx'])}) belongs to no core")
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
    geoms = {}
    for key, uid in held.items():
        if d["positions"][key]["qc"] == "excluded":
            continue                                   # excluded: no region is written (plan §3.3)
        g = core_geometry([d["pieces"][p] for p in d["cores"][uid]["pieces"]], shape)
        if not _has_area(g, normalize_polygon):
            out.append(f"{_label(study, key)}: the outline is degenerate (outside the slide?)")
            continue
        geoms[key] = g
    keys = sorted(geoms)
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if _overlap(geoms[a], geoms[b]):
                out.append(f"{_label(study, a)} and {_label(study, b)}: the regions overlap")
    return out


def _has_area(g, normalize_polygon) -> bool:
    y0, y1, x0, x1 = g["bbox_fullres"]
    if normalize_polygon(g["polygon_fullres"]) is None or y1 - y0 < 2 or x1 - x0 < 2:
        return False
    xs = [p[0] for p in g["polygon_fullres"]]
    ys = [p[1] for p in g["polygon_fullres"]]
    area2 = sum(xs[i] * ys[i - 1] - xs[i - 1] * ys[i] for i in range(len(xs)))
    return area2 != 0


def _overlap(ga, gb) -> bool:
    ay0, ay1, ax0, ax1 = ga["bbox_fullres"]
    by0, by1, bx0, bx1 = gb["bbox_fullres"]
    y0, y1, x0, x1 = max(ay0, by0), min(ay1, by1), max(ax0, bx0), min(ax1, bx1)
    if y0 >= y1 or x0 >= x1:
        return False
    fa = fill(ga["polygon_fullres"], ga["bbox_fullres"])[y0 - ay0:y1 - ay0, x0 - ax0:x1 - ax0]
    fb = fill(gb["polygon_fullres"], gb["bbox_fullres"])[y0 - by0:y1 - by0, x0 - bx0:x1 - bx0]
    return bool((fa & fb).any())


def _label(study, key) -> str:
    from .cores import position_key
    sheet = study["map_sheet"]
    for p in sheet["positions"]:
        if position_key(study["slide_id"], sheet["sha256"], p["position_id"]) == key:
            return f"X{p['x']}-Y{int(p['y']):02d}"
    return str(key)


def layout_path(project_dir, version: int) -> str:
    from .study import STUDY_DIR
    return os.path.join(project_dir, STUDY_DIR, f"tma_layout_v{int(version)}.json")


def draft_path(project_dir) -> str:
    from .study import STUDY_DIR
    return os.path.join(project_dir, STUDY_DIR, "tma_layout_draft.json")


def locked_versions(project_dir) -> List[int]:
    """The complete locked versions: both files present."""
    from . import cores as tc
    return [v for v in _taken_versions(project_dir)
            if os.path.isfile(layout_path(project_dir, v)) and os.path.isfile(tc.table_path(project_dir, v))]


def _taken_versions(project_dir) -> List[int]:
    """Every version number with any file (an interrupted lock keeps its
    number taken; the next lock uses the next one)."""
    from .study import STUDY_DIR
    d = os.path.join(project_dir, STUDY_DIR)
    out = set()
    if os.path.isdir(d):
        for f in os.listdir(d):
            for pre, suf in (("tma_layout_v", ".json"), ("tma_cores_v", ".parquet")):
                if f.startswith(pre) and f.endswith(suf) and f[len(pre):-len(suf)].isdigit():
                    v = int(f[len(pre):-len(suf)])
                    if v >= 1:
                        out.add(v)
    return sorted(out)


def lock(project_dir, d: Dict, study: Dict, shape: Tuple[int, int]) -> Dict:
    """Write the next locked version: ``tma_cores_v<n>.parquet`` (F1's
    schema) and ``tma_layout_v<n>.json``; register ``tma_layout``. Refuses
    while `lock_problems` lists anything or the coverage is unconfirmed."""
    from .. import provenance as prov
    from ..project_identity import region_id
    from . import cores as tc
    problems = lock_problems(d, study, shape)
    if not d.get("coverage_confirmed"):
        problems.append("the coverage is not confirmed")
    if problems:
        raise LayoutError("; ".join(problems))
    version = (_taken_versions(project_dir) or [0])[-1] + 1
    for p in (layout_path(project_dir, version), tc.table_path(project_dir, version)):
        if os.path.exists(p):
            raise FileExistsError(p)
    occupied = {p["position_id"]: bool(p["donor_block_id"]) for p in study["map_sheet"]["positions"]}
    held = {c["position_key"]: uid for uid, c in d["cores"].items() if c["position_key"]}
    rows, cores_out = [], {}
    for row in tc.draft_rows(study):
        key = row["position_key"]
        row["layout_version"] = version
        row["status"] = status_text(d, key, occupied[row["position_id"]])
        uid = held.get(key)
        if uid:
            rid = None
            if d["positions"][key]["qc"] != "excluded":     # excluded: identity and pieces kept, no region
                g = core_geometry([d["pieces"][p] for p in d["cores"][uid]["pieces"]], shape)
                g["region_id"] = rid = region_id(d["slide_id"], "TMA_core", g["bbox_fullres"], g["polygon_fullres"])
                row["region_id"], row["geometry"] = rid, json.dumps(g, separators=(",", ":"))
            cores_out[uid] = {"position_key": key, "pieces": list(d["cores"][uid]["pieces"]),
                              "mapping": d["cores"][uid]["mapping"], "region_id": rid}
        rows.append(row)
    positions_out = {}
    for pos in study["map_sheet"]["positions"]:
        key = tc.position_key(d["slide_id"], d["map_sha256"], pos["position_id"])
        uid = held.get(key)
        positions_out[key] = dict(d["positions"][key], position_id=pos["position_id"],
                                  map_use="sample" if pos["donor_block_id"] else "empty",
                                  mapping=d["cores"][uid]["mapping"] if uid else "unmatched",
                                  core_uid=uid)
    rec = {"schema_version": SCHEMA_VERSION, "version": version, "based_on": d.get("based_on"),
           "created_at": _now(), "slide_id": d["slide_id"], "map_sha256": d["map_sha256"],
           "candidates_version": d["candidates_version"], "core_diameter_px": d["core_diameter_px"],
           "coordinates": "level-0 pixels, origin top-left, x right, y down; bbox [y0, y1, x0, x1] half-open",
           "coverage_confirmed": d["coverage_confirmed"], "cores": cores_out,
           "pieces": d["pieces"], "positions": positions_out, "operations": d["operations"],
           "run": sorted(k for k, v in d["positions"].items() if v["run"])}
    table = tc.write_table(project_dir, rows, version)
    path = layout_path(project_dir, version)
    prov.write_json_atomic(path, rec)
    entries = prov.load_entries(project_dir)
    sheet = [e["artifact_id"] for e in entries if e["kind"] == "tma_map_sheet" and e.get("token") == d["map_sha256"]]
    cands = [e["artifact_id"] for e in entries if e["kind"] == "tma_dearray"
             and e.get("parameters", {}).get("version") == d["candidates_version"]]
    unresolved = ([] if sheet else [{"role": "tma_map_sheet", "map_sha256": d["map_sha256"]}]) + \
        ([] if cands else [{"role": "tma_dearray", "version": d["candidates_version"]}])
    aid = prov.safe_register(project_dir, "tma_layout", prov.location(project_dir, path),
                             f"v{version}", slide_id=d["slide_id"], depends_on=sheet[-1:] + cands[-1:],
                             unresolved_inputs=unresolved,
                             parameters={"version": version, "based_on": d.get("based_on"),
                                         "cores": len(cores_out), "table": os.path.basename(table)},
                             software={"module": "core.tma.layout", "commit": prov.git_commit()})
    if aid is None:
        print(f"[TMA] {path} saved but NOT registered in the provenance")
    return rec


def load_locked(project_dir, version: int) -> Dict:
    with open(layout_path(project_dir, version), "r", encoding="utf-8") as f:
        rec = json.load(f)
    if rec.get("schema_version") != SCHEMA_VERSION:
        raise LayoutError(f"tma_layout_v{version}: schema_version {rec.get('schema_version')!r}")
    return rec


def draft_from_locked(rec: Dict) -> Dict:
    """A new draft based on a locked version (its pieces, cores and marks)."""
    d = {"schema_version": SCHEMA_VERSION, "slide_id": rec["slide_id"], "map_sha256": rec["map_sha256"],
         "candidates_version": rec["candidates_version"], "core_diameter_px": rec["core_diameter_px"],
         "based_on": rec["version"], "pieces": copy.deepcopy(rec["pieces"]), "cores": {},
         "positions": {k: {f: copy.deepcopy(v.get(f, dflt)) for f, dflt in DRAFT_POSITION_FIELDS}
                       for k, v in rec["positions"].items()},
         "operations": [], "coverage_confirmed": None}
    for uid, c in rec["cores"].items():
        d["cores"][uid] = {"pieces": list(c["pieces"]), "position_key": c["position_key"],
                           "custom_label": None, "mapping": "matched"}
    for uid, c in d["cores"].items():
        for p in c["pieces"]:
            d["pieces"][p]["owner"] = uid
    return d


def save_draft(project_dir, d: Dict) -> str:
    from .. import provenance as prov
    path = draft_path(project_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    prov.write_json_atomic(path, d)
    return path


def load_draft(project_dir) -> Optional[Dict]:
    path = draft_path(project_dir)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    if d.get("schema_version") != SCHEMA_VERSION:
        raise LayoutError(f"{path}: schema_version {d.get('schema_version')!r}")
    return d


def discard_draft(project_dir) -> None:
    path = draft_path(project_dir)
    if os.path.isfile(path):
        os.remove(path)
