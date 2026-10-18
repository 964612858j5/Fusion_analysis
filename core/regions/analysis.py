"""The analysis snapshot (docs/f4_regions_downstream_plan.md v2 §A, §B.1-§B.2,
§B.4): what Step1 / Step2 / Step4 use of the last Regions Save, frozen
once and carried through the run. No Qt.

* `select_cores` -- the cores that run: ticked Run, bound to exactly one
  core whose number is confirmed (mapping "matched", never "review"), QC
  not excluded (unreviewed runs and is recorded), an outline with an area,
  an occupied position. Every other occupied position is listed with its
  reason. Selected cores that overlap are refused.
* `build_snapshot` -- the Save, the map sheet (Project name read from the
  workbook when an older study lacks it and the file is unchanged), the
  selected cores with their sample fields, the exclusions with
  `seg_invalid` (channels "all" or meeting the segmentation channels),
  the policies. Refused when the Save and its core table disagree, the
  map sheet is not the Save's, or (with a map sheet) nothing is selected.
* `identity` -- the hash of the scientific content only (geometry,
  selection, numbers and QC, channel scopes, policies), not when or by
  whom it was saved.

Every snapshot carries its parent region (the workspace's ROI, or the
whole slide): cores are used only inside it, and a project without a map
sheet gives an "roi" snapshot -- the parent region is the domain, the
exclusions apply, no cores. A project whose map sheet is chosen is a TMA
project even before Find cores: with nothing selected it is refused,
never analysed as a whole region.

`load_snapshot` verifies the scientific identity only; descriptive
fields (save time, Project name completed from the workbook) are not
part of it.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from typing import Dict, List, Optional, Sequence, Tuple

from . import geometry as G
from . import lock as K
from .document import RegionsError, exclusion_type

SNAPSHOT_VERSION = 1
POLICIES = {
    "model_input": "unmasked",              # the model sees the image; cells are filtered afterwards
    "core_assignment": "majority_pixels",   # > MIN_CORE_FRACTION of the object's pixels
    "min_core_fraction": 0.5,
    "segmentation_exclusion": "drop_object_on_any_pixel",
    "measurement_exclusion": "nan_whole_measurement_on_any_pixel",
    "edge_cells": "keep_with_flags",        # analysis_include = wholly inside the core
}
REASONS = {
    "not_ticked": "Run not ticked",
    "not_bound": "no core bound to this position",
    "bound_twice": "two cores bound to this position",
    "review": "number to be confirmed after a merge",
    "qc_excluded": "QC excluded",
    "degenerate": "the outline has no area",
    "outside_region": "outside the workspace's region",
}


class AnalysisError(RegionsError):
    """`omitted`: every ticked core that cannot run, with its reason, when
    that is why the snapshot was refused."""

    def __init__(self, message, omitted=None):
        super().__init__(message)
        self.omitted = list(omitted or [])


def _key(slide_id, map_sha, pid):
    from ..tma.cores import position_key
    return position_key(slide_id, map_sha, pid)


def select_cores(doc: Dict, study: Dict) -> Tuple[List[Dict], List[Dict]]:
    """(selected, omitted) over the map sheet's occupied positions, in
    position order. selected: {uid, position_key, position_id, polygon,
    bbox, region_id}; omitted: {position_key, position_id, label, reason}."""
    from ..project_identity import region_id
    sheet = study["map_sheet"]
    shape = tuple(doc["slide_shape"])
    selected, omitted = [], []
    for p in sorted(sheet["positions"], key=lambda q: q["position_id"]):
        if not p["donor_block_id"]:
            continue
        key = _key(study["slide_id"], sheet["sha256"], p["position_id"])
        label = f"X{p['x']}-Y{p['y']:02d}"
        pos = doc["positions"].get(key, {})
        holders = [u for u, c in doc["cores"].items() if c["position_key"] == key]
        reason = None
        if not pos.get("run"):
            reason = "not_ticked"
        elif not holders:
            reason = "not_bound"
        elif len(holders) > 1:
            reason = "bound_twice"
        elif doc["cores"][holders[0]]["mapping"] != "matched":
            reason = "review"
        elif pos.get("qc") == "excluded":
            reason = "qc_excluded"
        g = None
        if reason is None:
            c = doc["cores"][holders[0]]
            g = G.region_outline([doc["pieces"][q]["polygon"] for q in c["pieces"]], shape)
            if not G.has_area(g):
                reason = "degenerate"
        if reason is not None:
            omitted.append({"position_key": key, "position_id": p["position_id"], "label": label,
                            "reason": reason})
            continue
        selected.append({"uid": holders[0], "position_key": key, "position_id": p["position_id"],
                         "polygon": g["polygon_fullres"], "bbox": g["bbox_fullres"],
                         "region_id": region_id(doc["slide_id"], "TMA_core", g["bbox_fullres"],
                                                g["polygon_fullres"])})
    return selected, omitted


def overlapping(selected: Sequence[Dict]) -> List[Tuple[str, str]]:
    """Pairs of selected cores whose outlines share a pixel."""
    out = []
    geo = [{"polygon_fullres": c["polygon"], "bbox_fullres": c["bbox"]} for c in selected]
    for i in range(len(geo)):
        for j in range(i + 1, len(geo)):
            a, b = geo[i]["bbox_fullres"], geo[j]["bbox_fullres"]
            if a[0] < b[1] and b[0] < a[1] and a[2] < b[3] and b[2] < a[3] and G.overlap(geo[i], geo[j]):
                out.append((selected[i]["position_key"], selected[j]["position_key"]))
    return out


def map_metadata(sheet: Dict) -> Dict:
    """The map sheet's identity fields; Project name is read from the
    workbook when the study predates it and the file is still the same."""
    project = sheet.get("project_name")
    if project is None:
        project = ""
        path = sheet.get("path")
        if path and os.path.isfile(path):
            from ..tma import map_sheet as ms
            try:
                if ms.file_sha256(path) == sheet["sha256"]:
                    project = ms.parse(path).project_name
            except Exception:                               # noqa: BLE001
                project = ""
    return {"sha256": sheet["sha256"], "block_id": sheet.get("block_id", ""),
            "block_name": sheet.get("block_name", ""), "project_name": project,
            "path": sheet.get("path", "")}


def seg_invalid(channels, seg_channels: Sequence[str]) -> bool:
    """An exclusion invalidates segmentation when it covers all channels or
    any channel the segmentation reads."""
    return channels == "all" or bool(set(channels) & set(seg_channels))


def build_snapshot(project_dir, slide_id: str, seg_channels: Sequence[str],
                   study: Optional[Dict] = None, parent: Optional[Dict] = None) -> Dict:
    """The frozen analysis snapshot of the last Save (see the module text).
    `seg_channels`: the channel names the chosen segmentation reads;
    `parent`: the workspace's region ``{"region_id", "bbox_fullres",
    "polygon_fullres"}`` (None: the whole slide)."""
    rec = K.load_saved(project_dir, slide_id)
    if rec is None:
        raise AnalysisError("Regions has not been saved for this slide")
    if not K.table_matches(project_dir, rec):
        raise AnalysisError("the last Regions Save was interrupted: Save again in Regions")
    doc = rec["document"]
    if study is None:
        from ..tma.study import load_study
        study = load_study(project_dir)
    shape = tuple(doc["slide_shape"])
    par = _parent(parent, shape)
    if study is not None and study.get("slide_id") not in (None, slide_id):
        study = None                                       # another slide's study is not this one's
    mode = "tma" if doc.get("map_sha256") or study is not None else "roi"
    cores, omitted, meta, positions = [], [], None, []
    if mode == "tma":
        if not doc.get("map_sha256"):
            raise AnalysisError("the map sheet is chosen but the cores are not found yet: Find cores in Regions")
        if study is None or study["map_sheet"]["sha256"] != doc["map_sha256"]:
            raise AnalysisError("the TMA map sheet is not the one the Regions Save was made with")
        selected, omitted = select_cores(doc, study)
        from .raster import AnalysisRaster
        probe = AnalysisRaster({"mode": "tma", "slide_shape": list(shape), "parent": par,
                                "cores": selected, "exclusions": []})
        inside = []
        for i, c in enumerate(selected):
            if probe.core_meets_parent(i):                 # real pixels, not just the boxes
                inside.append(c)
            else:
                q = next(p for p in study["map_sheet"]["positions"] if p["position_id"] == c["position_id"])
                omitted.append({"position_key": c["position_key"], "position_id": c["position_id"],
                                "label": f"X{q['x']}-Y{q['y']:02d}", "reason": "outside_region"})
        selected = inside
        if not selected:
            ticked = [o for o in omitted if o["reason"] != "not_ticked"]
            if not ticked:
                raise AnalysisError("no core is selected: tick Run for the cores to analyse in Regions")
            listed = "; ".join(f"{o['label']}: {REASONS[o['reason']]}" for o in ticked[:10])
            more = f"; … and {len(ticked) - 10} more" if len(ticked) > 10 else ""
            raise AnalysisError(f"no ticked core can run ({listed}{more})", omitted=ticked)
        pairs = overlapping(selected)
        if pairs:
            raise AnalysisError("selected cores overlap: " + ", ".join(f"{a} / {b}" for a, b in pairs))
        meta = map_metadata(study["map_sheet"])
        by_pid = {p["position_id"]: p for p in study["map_sheet"]["positions"]}
        for c in selected:
            p = by_pid[c["position_id"]]
            cores.append(dict(c, label=f"X{p['x']}-Y{p['y']:02d}", donor_block_id=p["donor_block_id"],
                              tma_unique_sample_id=p.get("sample_id", ""),
                              annotation=p.get("annotation", ""), core_height=p.get("core_height", ""),
                              qc=doc["positions"][c["position_key"]]["qc"]))
        chosen = {c["position_key"] for c in cores}
        why = {o["position_key"]: o["reason"] for o in omitted}
        for pid, p in sorted(by_pid.items()):
            key = _key(study["slide_id"], study["map_sheet"]["sha256"], pid)
            positions.append({"position_key": key, "position_id": pid, "label": f"X{p['x']}-Y{p['y']:02d}",
                              "occupied": bool(p["donor_block_id"]), "donor_block_id": p["donor_block_id"],
                              "tma_unique_sample_id": p.get("sample_id", ""),
                              "annotation": p.get("annotation", ""), "core_height": p.get("core_height", ""),
                              "spot_slide": p.get("spot_slide", ""), "selected": key in chosen,
                              "reason": "" if key in chosen else why.get(key, "empty" if not p["donor_block_id"]
                                                                          else "not_ticked")})
    exclusions = []
    for uid, e in sorted(doc["exclusions"].items()):
        g = G.region_outline([e["polygon"]], shape)
        if not G.has_area(g):
            continue                                       # listed as an open issue by the Save
        chans = e["channels"] if e["channels"] == "all" else sorted(e["channels"])
        exclusions.append({"uid": uid, "type": exclusion_type(e["reasons"]), "reasons": sorted(e["reasons"]),
                           "channels": chans, "polygon": [list(q) for q in e["polygon"]],
                           "bbox": g["bbox_fullres"], "seg_invalid": seg_invalid(chans, seg_channels)})
    snap = {"snapshot_version": SNAPSHOT_VERSION, "mode": mode, "slide_id": slide_id,
            "slide_shape": list(doc["slide_shape"]),
            "regions": {"save_count": rec["save_count"], "content_sha256": rec["content_sha256"],
                        "saved_at": rec["saved_at"]},
            "parent": par, "map_sheet": meta, "cores": cores, "omitted": omitted, "positions": positions,
            "exclusions": exclusions, "seg_channels": sorted(set(seg_channels)),
            "policies": copy.deepcopy(POLICIES)}
    snap["identity"] = identity(snap)
    return snap


def identity(snap: Dict) -> str:
    """The scientific content's hash: what changes the analysed pixels, the
    cells kept or the sample fields -- never save times or edit history."""
    excl = sorted(json.dumps({k: e[k] for k in ("polygon", "channels", "seg_invalid")}, sort_keys=True)
                  for e in snap["exclusions"])               # independent of ids and their order
    science = {"v": snap["snapshot_version"], "mode": snap["mode"], "slide_id": snap["slide_id"],
               "parent": {k: snap["parent"].get(k) for k in ("bbox_fullres", "polygon_fullres")},
               "map": (snap["map_sheet"] or {}).get("sha256"),
               "cores": [{k: c[k] for k in ("position_key", "polygon", "donor_block_id",
                                            "tma_unique_sample_id", "annotation", "qc")} for c in snap["cores"]],
               "exclusions": excl,
               "seg_channels": snap["seg_channels"], "policies": snap["policies"]}
    return hashlib.sha256(json.dumps(science, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _parent(parent: Optional[Dict], shape) -> Dict:
    if not parent:
        return {"region_id": None, "bbox_fullres": [0, int(shape[0]), 0, int(shape[1])], "polygon_fullres": None}
    poly = parent.get("polygon_fullres")
    return {"region_id": parent.get("region_id"), "bbox_fullres": [int(v) for v in parent["bbox_fullres"]],
            "polygon_fullres": [list(map(int, q)) for q in poly] if poly else None}


def save_snapshot(path, snap: Dict) -> str:
    from .. import provenance as prov
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    prov.write_json_atomic(path, snap)
    return path


def load_snapshot(path) -> Dict:
    """A frozen snapshot, checked against its own identity."""
    with open(path, "r", encoding="utf-8") as f:
        snap = json.load(f)
    if snap.get("snapshot_version") != SNAPSHOT_VERSION:
        raise AnalysisError(f"{path}: snapshot version {snap.get('snapshot_version')!r}")
    if identity(snap) != snap.get("identity"):
        raise AnalysisError(f"{path}: the snapshot does not match its identity")
    return snap


def is_current(project_dir, snap: Dict, study: Optional[Dict] = None) -> bool:
    """Whether the last Save would still give this snapshot (a later Save
    only makes this False; it never changes the frozen run)."""
    try:
        now = build_snapshot(project_dir, snap["slide_id"], snap["seg_channels"], study=study,
                             parent=snap.get("parent"))
    except RegionsError:
        return False
    return now["identity"] == snap["identity"]
