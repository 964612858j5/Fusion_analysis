"""Artifact provenance records (block A3; plan v2.3 §6.8, v2.4 P3; the A3+A4
application v2 §3.5).

One JSON file per artifact under ``<project>/provenance/<artifact_id>.json``,
written once (temp file + fsync + ``os.replace``) and never rewritten, so
several producers can register at the same time without a lock.

An entry::

    schema_version, artifact_id, kind, created_at, workspace_id, slide_id,
    location {path, member, relative_to}, token,
    operates_on   [region_id, ...]      spatial / biological SCOPE
    depends_on    [artifact_id, ...]    LINEAGE -- registered artifact ids only
    unresolved_inputs [{role, path}]    inputs read that were never registered
    parameters, software, flags

`depends_on` never holds a region id, a path or anything that is not an
already registered artifact; `operates_on` holds region ids only. An input
that was never registered (made before A3, or by a producer that does not
register) is written to `unresolved_inputs` as it is -- never guessed.

`token` is the artifact's own version token; (kind, location, token)
identifies an artifact, so registering the same thing twice returns the
same id.

Producers call `safe_register`: registration happens AFTER the product is
committed and is auxiliary (like `mark_roi_step`). A failure is logged; the
product is never touched and the GUI never sees an exception.
"""

import json
import os
import re
import threading
import uuid
from datetime import datetime
from typing import Dict, Iterable, List, Optional

from .project_identity import PROJECT_SCHEMA_VERSION, read_project_schema

SCHEMA_VERSION = 1
PROVENANCE_DIR = "provenance"

KINDS = ("raw_slide", "corrected_channel", "fused", "segmentation_run", "step4_h5ad",
         "cells_parquet", "regions_parquet",
         # P3: the schema is defined here; §5.8's own producer registers it
         "corrected_coarse_levels",
         # block DV-D: what a user deletion moved into the trash (appended;
         # the entries of the moved artifacts are never rewritten)
         "deletion")

FLAG_RUN_ID_COLLISION = "segmentation_run_id_collision"
FLAG_REGION_MISMATCH = "region_mismatch"

_ARTIFACT_ID = re.compile(r"^(art_[a-z0-9_]+_[0-9a-f]{12}|slide_[0-9a-f]{16})$")
_REGION_ID = re.compile(r"^reg_[0-9a-f]{16}$")
_lock = threading.Lock()


class ProvenanceError(ValueError):
    """An entry that breaks the provenance rules."""


# ── files ───────────────────────────────────────────────────────────────

def write_json_atomic(path, payload):
    """Write JSON next to `path`, fsync, then ``os.replace``: a crash leaves
    the old file whole and no temporary behind."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, f".{os.path.basename(path)}.tmp.{uuid.uuid4().hex[:8]}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def provenance_dir(project_dir) -> str:
    return os.path.join(project_dir, PROVENANCE_DIR)


def to_posix(path: str, sep: str = os.sep) -> str:
    """A relative path with ``/`` separators whatever the OS (`sep` is the
    OS's separator; Windows' ``\\`` becomes ``/``)."""
    return str(path).replace(sep, "/") if sep != "/" else str(path)


def location(project_dir, path, member=None) -> Dict:
    """Where an artifact lives: relative to the project with ``/`` separators
    on every OS when inside it, absolute otherwise."""
    project = os.path.realpath(project_dir)
    full = os.path.realpath(path)
    try:
        rel = os.path.relpath(full, project)
    except ValueError:                      # another drive (Windows)
        rel = None
    if rel is not None and rel != ".." and not rel.startswith(".." + os.sep):
        out = {"path": to_posix(rel), "relative_to": "project"}
    else:
        out = {"path": full, "relative_to": "absolute"}
    if member:
        out["member"] = to_posix(str(member), "\\")   # a zarr member: "group/array"
    return out


def resolve_location(project_dir, loc: Dict) -> str:
    if loc.get("relative_to") == "project":
        return os.path.join(os.path.realpath(project_dir), *loc["path"].split("/"))
    return loc["path"]


def _same_location(a: Dict, b: Dict) -> bool:
    return (a.get("path") == b.get("path") and a.get("relative_to") == b.get("relative_to")
            and a.get("member") == b.get("member"))


def load_entries(project_dir) -> List[Dict]:
    """Every entry of the project, oldest first (read only)."""
    out = []
    pdir = provenance_dir(project_dir)
    if not os.path.isdir(pdir):
        return out
    for name in sorted(os.listdir(pdir)):
        if not name.endswith(".json") or name.startswith("."):
            continue
        with open(os.path.join(pdir, name), "r", encoding="utf-8") as f:
            out.append(json.load(f))
    out.sort(key=lambda e: (e.get("created_at", ""), e.get("artifact_id", "")))
    return out


def lookup(project_dir, kind, loc: Dict, token) -> Optional[str]:
    """The id registered for (kind, location, token), or None."""
    for entry in load_entries(project_dir):
        if (entry.get("kind") == kind and str(entry.get("token")) == str(token)
                and _same_location(entry.get("location") or {}, loc)):
            return entry["artifact_id"]
    return None


def is_artifact_id(value) -> bool:
    return isinstance(value, str) and bool(_ARTIFACT_ID.match(value))


def is_region_id(value) -> bool:
    return isinstance(value, str) and bool(_REGION_ID.match(value))


# ── validation and registration ─────────────────────────────────────────

def validate(project_dir, entry: Dict):
    """`ProvenanceError` unless `entry` keeps the rules (module docstring)."""
    if entry.get("kind") not in KINDS:
        raise ProvenanceError(f"unknown artifact kind {entry.get('kind')!r}")
    if not is_artifact_id(entry.get("artifact_id")):
        raise ProvenanceError(f"not an artifact id: {entry.get('artifact_id')!r}")
    for dep in entry.get("depends_on") or []:
        if not is_artifact_id(dep):
            raise ProvenanceError(f"depends_on holds {dep!r}, which is not an artifact id "
                                  "(regions go to operates_on, unregistered inputs to "
                                  "unresolved_inputs)")
        if not os.path.exists(os.path.join(provenance_dir(project_dir), f"{dep}.json")):
            raise ProvenanceError(f"depends_on names {dep!r}, which is not registered")
    for rid in entry.get("operates_on") or []:
        if not is_region_id(rid):
            raise ProvenanceError(f"operates_on holds {rid!r}, which is not a region id")
    for item in entry.get("unresolved_inputs") or []:
        if not isinstance(item, dict) or not item.get("role"):
            raise ProvenanceError(f"an unresolved input needs a role: {item!r}")


def register(project_dir, kind: str, loc: Dict, token, *, depends_on: Iterable = (),
             operates_on: Iterable = (), unresolved_inputs: Iterable = (),
             parameters: Optional[Dict] = None, software: Optional[Dict] = None,
             workspace_id: str = "", slide_id: str = "", flags: Iterable = (),
             artifact_id: Optional[str] = None) -> str:
    """Write one entry and return its id; the existing id when (kind,
    location, token) is already registered. Raises on a project of another
    schema (`ProjectSchemaError`; a legacy project too: new code stamps it
    first) or a broken entry (`ProvenanceError`)."""
    schema = read_project_schema(project_dir)
    if schema != PROJECT_SCHEMA_VERSION:
        raise ProvenanceError(f"{project_dir}: project_schema_version is not "
                              f"{PROJECT_SCHEMA_VERSION}; nothing is registered")
    with _lock:
        existing = lookup(project_dir, kind, loc, token)
        if existing is not None:
            return existing
        aid = artifact_id or f"art_{kind}_{uuid.uuid4().hex[:12]}"
        entry = {
            "schema_version": SCHEMA_VERSION,
            "artifact_id": aid,
            "kind": kind,
            "created_at": datetime.now().isoformat(timespec="microseconds"),
            "workspace_id": workspace_id or "",
            "slide_id": slide_id or "",
            "location": dict(loc),
            "token": str(token),
            "operates_on": sorted(set(operates_on)),
            "depends_on": sorted(set(depends_on)),
            "unresolved_inputs": list(unresolved_inputs),
            "parameters": dict(parameters or {}),
            "software": dict(software or {}),
            "flags": sorted(set(flags)),
        }
        validate(project_dir, entry)
        write_json_atomic(os.path.join(provenance_dir(project_dir), f"{aid}.json"), entry)
        return aid


def safe_register(project_dir, kind, loc, token, **kw) -> Optional[str]:
    """`register` for producers: never raises, logs a failure, returns None."""
    try:
        return register(project_dir, kind, loc, token, **kw)
    except Exception as exc:                # auxiliary: the product stays as it is
        print(f"[Provenance] {kind} not registered ({type(exc).__name__}: {exc})")
        return None


def git_commit() -> str:
    """The checkout's commit for `software`, or "" (never raises)."""
    try:
        import subprocess
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=here,
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


# ── producers (block A3 2/3, 3/3) ───────────────────────────────────────
#
# Each producer calls ONE of these after its product is committed. They
# never raise: a failure is logged and the product stays as it is.

def find_workspace(path):
    """``(project_dir, workspace_dir)`` of a path inside
    ``<project>/rois/<workspace_id>/...``, or ``(None, None)``."""
    cur = os.path.abspath(path)
    while True:
        parent = os.path.dirname(cur)
        if os.path.basename(parent) == "rois" and os.path.isfile(
                os.path.join(cur, "roi_manifest.json")):
            return os.path.dirname(parent), cur
        if parent == cur:
            return None, None
        cur = parent


def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def ensure_raw_slide(project_dir, slide_path) -> str:
    """The slide's id, registering its `raw_slide` entry if needed (the P2
    description is its parameters). Raises on failure."""
    from .project_identity import describe_slide
    manifest = _load(os.path.join(project_dir, "project_manifest.json")) or {}
    sid, desc = describe_slide(slide_path, manifest.get("sources"))
    register(project_dir, "raw_slide", location(project_dir, slide_path), sid,
             artifact_id=sid, slide_id=sid, parameters=desc)
    return sid


def workspace_regions(workspace_dir, slide_id) -> Dict[str, Dict]:
    """``{roi_name: {"region_id", "bbox_fullres", "roi"}}`` from the
    workspace's committed Step0 ``settings/step0/roi_config.json`` (or its
    ``roi_manifest.json`` for a workspace without one)."""
    from .project_identity import region_id_of_roi
    rois = _load(os.path.join(workspace_dir, "settings", "step0", "roi_config.json"))  # block RM
    if not isinstance(rois, list) or not rois:
        manifest = _load(os.path.join(workspace_dir, "roi_manifest.json")) or {}
        rois = [dict(manifest, name=manifest.get("display_name", ""))] if manifest else []
    out = {}
    for roi in rois:
        name = str(roi.get("name") or roi.get("display_name") or "")
        out[name] = {"region_id": region_id_of_roi(slide_id, roi),
                     "bbox_fullres": [int(v) for v in roi.get("bbox_fullres") or []],
                     "roi": roi}
    return out


def _scope(regions, name, bbox):
    """``(operates_on, flags)`` for a product made for region `name` with
    `bbox`: the workspace region's id, or nothing and `region_mismatch` when
    the name is unknown or the geometry differs (never guessed)."""
    reg = regions.get(str(name))
    if reg is None or (bbox is not None and [int(v) for v in bbox] != reg["bbox_fullres"]):
        return [], [FLAG_REGION_MISMATCH]
    return [reg["region_id"]], []


def corrected_group(root, roi_name):
    """The corrected zarr's group for `roi_name` (the strict rule of
    `CorrectedZarrSource`: its ``roi_name`` attribute or its folder name)."""
    from .quant_sources import region_folder
    hits = [g for g in root.group_keys()
            if root[g].attrs.get("roi_name") == roi_name or g == region_folder(roi_name)]
    return hits[0] if len(hits) == 1 else None


def _corrected_token(attrs):
    return attrs.get("source_identity") or ""


def register_corrected_channels(project_dir, workspace_dir, zarr_path, raw_path):
    """Step0 (`write_handoff`): one `corrected_channel` per array of the
    committed corrected product."""
    try:
        import zarr
        sid = ensure_raw_slide(project_dir, raw_path)
        regions = workspace_regions(workspace_dir, sid)
        root = zarr.open_group(zarr_path, mode="r")
        out = []
        for gname in sorted(root.group_keys()):
            group = root[gname]
            roi_name = str(group.attrs.get("roi_name") or gname)
            bbox = group.attrs.get("bbox_fullres") or None
            scope, flags = _scope(regions, roi_name, bbox)
            for name in sorted(group.array_keys()):
                attrs = dict(group[name].attrs)
                token = _corrected_token(attrs)
                if not token:
                    print(f"[Provenance] corrected {gname}/{name} has no source_identity; "
                          "not registered")
                    continue
                params = {k: attrs.get(k) for k in (
                    "channel_name", "channel_index", "correction_method",
                    "correction_param_name", "correction_param_value",
                    "bg_correction_algo_version", "written_at", "dtype")}
                params["roi_name"] = roi_name
                params["valid_bounds"] = [int(v) for v in bbox] if bbox else None
                out.append(safe_register(
                    project_dir, "corrected_channel",
                    location(project_dir, zarr_path, f"{gname}/{name}"), token,
                    depends_on=[sid], operates_on=scope, flags=flags,
                    workspace_id=os.path.basename(workspace_dir), slide_id=sid,
                    parameters=params, software={"git_commit": git_commit()}))
        return out
    except Exception as exc:
        print(f"[Provenance] corrected channels not registered ({type(exc).__name__}: {exc})")
        return []


def corrected_artifact(project_dir, zarr_path, member, token):
    return lookup(project_dir, "corrected_channel", location(project_dir, zarr_path, member),
                  token)


def register_fused(zarr_path, raw_path, region, corrected_path="", corrected_used=(),
                   corrected_unused=(), via_loader=False):
    """Step1 (`FullFusionWorker`): one `fused` after a region is published.
    `region` is ``{"name", "bbox_fullres"}``; `corrected_used` the corrected
    channels whose pixels entered the fusion, `corrected_unused` the ones
    read without a committed display window."""
    try:
        import zarr
        project_dir, ws = find_workspace(zarr_path)
        if project_dir is None:
            print(f"[Provenance] {zarr_path} is not inside a project workspace; not registered")
            return None
        sid = ensure_raw_slide(project_dir, raw_path)
        attrs = dict(zarr.open(zarr_path, mode="r").attrs)
        token = fused_token(attrs)
        scope, flags = _scope(workspace_regions(ws, sid), region["name"],
                              region.get("bbox_fullres"))
        deps, unresolved = [sid], []
        if via_loader:
            # the page loader decided where each pixel came from: not knowable here
            unresolved.append({"role": "pixels_via_loader", "path": corrected_path or ""})
        elif corrected_used:
            root = zarr.open_group(corrected_path, mode="r")
            gname = corrected_group(root, region["name"])
            for ch in sorted(corrected_used):
                token_ch = (_corrected_token(root[gname][ch].attrs)
                            if gname and ch in root[gname] else "")
                aid = corrected_artifact(project_dir, corrected_path, f"{gname}/{ch}", token_ch) \
                    if gname else None
                if aid:
                    deps.append(aid)
                else:
                    unresolved.append({"role": "corrected_channel", "channel": ch,
                                       "path": corrected_path})
        return safe_register(
            project_dir, "fused", location(project_dir, zarr_path), token,
            depends_on=deps, operates_on=scope, flags=flags, unresolved_inputs=unresolved,
            workspace_id=os.path.basename(ws), slide_id=sid,
            parameters={**{k: attrs.get(k) for k in ("fusion_formula_version", "config_hash",
                                                     "artifact_kind", "roi_name",
                                                     "bbox_fullres", "created_at")},
                        "corrected_read_without_window": sorted(corrected_unused)},
            software={"git_commit": git_commit()})
    except Exception as exc:
        print(f"[Provenance] fused not registered ({type(exc).__name__}: {exc})")
        return None


def fused_token(attrs) -> str:
    return f"{attrs.get('created_at', '')}|{attrs.get('config_hash', '')}"


def register_segmentation_run(run_dir, meta):
    """Step2 (`segment_merge_worker`): one `segmentation_run` after its
    ``segmentation_meta.json`` is written."""
    try:
        import zarr
        project_dir, ws = find_workspace(run_dir)
        if project_dir is None:
            print(f"[Provenance] {run_dir} is not inside a project workspace; not registered")
            return None
        manifest = _load(os.path.join(ws, "roi_manifest.json")) or {}
        sid = ensure_raw_slide(project_dir, manifest["source_ome"])
        regions = workspace_regions(ws, sid)
        run_id = str(meta.get("run_id") or os.path.basename(run_dir))
        rois = meta.get("rois") or [{"roi_name": meta.get("roi_display_name", ""),
                                     "bbox_fullres": meta.get("roi_bbox_fullres"),
                                     "fused_zarr_path": meta.get("fused_zarr_path")}]
        scope, flags, deps, unresolved = [], [], [], []
        for roi in rois:
            s, f = _scope(regions, roi.get("roi_name"), roi.get("bbox_fullres"))
            scope += s
            flags += f
            fpath = roi.get("fused_zarr_path") or meta.get("fused_zarr_path")
            if not fpath:
                continue
            aid = None
            if os.path.exists(fpath):
                aid = lookup(project_dir, "fused", location(project_dir, fpath),
                             fused_token(dict(zarr.open(fpath, mode="r").attrs)))
            if aid:
                deps.append(aid)
            else:
                unresolved.append({"role": "fused", "path": fpath})
        loc = location(project_dir, run_dir)
        same_id = [e for e in load_entries(project_dir)
                   if e.get("kind") == "segmentation_run" and e.get("token") == run_id
                   and not _same_location(e.get("location") or {}, loc)]
        if same_id:
            flags.append(FLAG_RUN_ID_COLLISION)
            print(f"[Provenance] segmentation_run_id {run_id} already exists in "
                  f"{same_id[0]['workspace_id']}: registered with a collision flag")
        engine = meta.get("seg_engine") or {}
        seg_config = meta.get("seg_config") or {}
        return safe_register(
            project_dir, "segmentation_run", loc, run_id, depends_on=deps,
            operates_on=scope, flags=flags, unresolved_inputs=unresolved,
            workspace_id=os.path.basename(ws), slide_id=sid,
            parameters={"segmentation_run_id": run_id, "method": meta.get("method"),
                        "engine": engine.get("engine"), "engine_identity": engine.get("identity"),
                        "preseg_contract": seg_config.get("preseg_contract")
                        if isinstance(seg_config, dict) else None,
                        "files": {"segmentation_meta": "segmentation_meta.json",
                                  "label_store": meta.get("label_store")}},
            software={"git_commit": git_commit(), "engine_provenance": engine.get("provenance")})
    except Exception as exc:
        print(f"[Provenance] segmentation run not registered ({type(exc).__name__}: {exc})")
        return None


def register_step4(h5ad_path, job, provenance_record):
    """Step4 (`run_extraction`): one `step4_h5ad` after its outputs are
    replaced into place."""
    try:
        project_dir, ws = find_workspace(h5ad_path)
        if project_dir is None:
            print(f"[Provenance] {h5ad_path} is not inside a project workspace; not registered")
            return None
        sid = ensure_raw_slide(project_dir, job.slide)
        y0, y1, x0, x1 = job.bbox
        scope, flags = _scope(workspace_regions(ws, sid), job.roi_name, [y0, y1, x0, x1])
        deps, unresolved = [sid], []
        seg = lookup(project_dir, "segmentation_run", location(project_dir, job.run_dir),
                     job.run_id)
        if seg:
            deps.append(seg)
        else:
            unresolved.append({"role": "segmentation_run", "path": job.run_dir})
        for ch in job.channels:
            if ch.kind != "corrected":
                continue
            aid = corrected_artifact(project_dir, ch.path, ch.array,
                                     (ch.identity or {}).get("source_identity", ""))
            if aid:
                deps.append(aid)
            else:
                unresolved.append({"role": "corrected_channel", "channel": ch.name,
                                   "path": ch.path})
        outputs = provenance_record.get("outputs") or {}
        return safe_register(
            project_dir, "step4_h5ad", location(project_dir, h5ad_path),
            provenance_record.get("created_at", ""), depends_on=deps, operates_on=scope,
            flags=flags, unresolved_inputs=unresolved, workspace_id=os.path.basename(ws),
            slide_id=sid,
            parameters={"segmentation_run_id": job.run_id, "roi_name": job.roi_name,
                        "statistics": provenance_record.get("statistics"),
                        "schema": provenance_record.get("schema"),
                        "schema_version": provenance_record.get("schema_version"),
                        "files": {"h5ad": outputs.get("h5ad"), "csv": outputs.get("csv"),
                                  "provenance": os.path.basename(h5ad_path)[:-5]
                                  + "_provenance.json"}},
            software={"git_commit": provenance_record.get("git_commit", "")})
    except Exception as exc:
        print(f"[Provenance] Step4 result not registered ({type(exc).__name__}: {exc})")
        return None


__all__ = ["SCHEMA_VERSION", "KINDS", "ProvenanceError", "write_json_atomic", "location",
           "resolve_location", "load_entries", "lookup", "validate", "register",
           "safe_register", "is_artifact_id", "is_region_id", "provenance_dir",
           "FLAG_RUN_ID_COLLISION", "FLAG_REGION_MISMATCH", "find_workspace",
           "ensure_raw_slide", "workspace_regions", "register_corrected_channels",
           "register_fused", "register_segmentation_run", "register_step4"]
