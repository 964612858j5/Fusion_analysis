"""Data versions of one session (block DV; docs/v16_data_versions_application.md).

A session is a workspace ``rois/<workspace_id>/`` (one slide). A DATA VERSION
is one Step0 + Step1 parameter set together with the products it made: the
corrected Zarr it references and one fused Zarr per region. Versions live
under ``<workspace>/versions/``:

    versions/index.json                 {"versions": [...], "current": "v002"}
    versions/v001_20261003_120000/      a version's own folder
        version.json                    its record ("complete": true, written last)
        correction_config.json, step0_channel_remap.json,
        step1_fusion_settings.json      copies of its parameter files
        fused_<region>.zarr             one per region
    versions/corrected/c003_<stamp>/    corrected products (with their coarse
        corrected_channels.zarr         sidecar). A version REFERENCES one in
        corrected_coarse.zarr           place; once referenced it is read-only,
                                        and a Save that must change pixels
                                        copies it into a new cNNN folder (§3.15).
                                        One that no version references yet is
                                        the dirty draft's.

A version EXISTS only when it is listed in ``index.json`` and its
``version.json`` says ``"complete": true`` (application §3.12). A folder that
is neither -- a Generate that was cancelled, failed or killed -- is removed by
`cleanup_incomplete`. A published version, and every product it references, is
never written again (§3.14).

Ids are a counter plus a timestamp (``v003_20261003_231500``): no hash
(application §3.2). Whether two versions are the same is decided by comparing
fields that already exist (`same_content`).
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from typing import Dict, List, Optional

VERSIONS_DIR = "versions"
INDEX = "index.json"
RECORD = "version.json"
CORRECTED_DIR = "corrected"
CORRECTED_ZARR = "corrected_channels.zarr"
LEGACY_LABEL = "v1 (registered from an earlier workspace)"


# ── paths ────────────────────────────────────────────────────────────────

def versions_dir(workspace_dir) -> str:
    return os.path.join(os.path.abspath(workspace_dir), VERSIONS_DIR)


def index_path(workspace_dir) -> str:
    return os.path.join(versions_dir(workspace_dir), INDEX)


def corrected_root(workspace_dir) -> str:
    return os.path.join(versions_dir(workspace_dir), CORRECTED_DIR)


def new_corrected_folder(workspace_dir, now=None) -> str:
    """A new corrected-product folder for the dirty draft; returns the path
    of the corrected Zarr inside it (not created)."""
    root = corrected_root(workspace_dir)
    os.makedirs(root, exist_ok=True)
    used = [int(n[1:4]) for n in os.listdir(root) if n.startswith("c") and n[1:4].isdigit()]
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    folder = os.path.join(root, f"c{(max(used) if used else 0) + 1:03d}_{stamp}")
    os.makedirs(folder, exist_ok=False)
    return os.path.join(folder, CORRECTED_ZARR)


def version_dir(workspace_dir, folder) -> str:
    return os.path.join(versions_dir(workspace_dir), folder)


# ── reading ──────────────────────────────────────────────────────────────

def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def load_index(workspace_dir) -> Dict:
    data = _load(index_path(workspace_dir))
    if not isinstance(data, dict):
        data = {}
    data.setdefault("versions", [])
    data.setdefault("current", "")
    return data


def read_record(workspace_dir, entry) -> Optional[Dict]:
    """The complete record of an index entry, or None."""
    rec = _load(os.path.join(version_dir(workspace_dir, entry.get("folder", "")), RECORD))
    if not isinstance(rec, dict) or rec.get("complete") is not True:
        return None
    return rec


def list_versions(workspace_dir) -> List[Dict]:
    """The versions that exist, oldest first: index entries whose record is
    complete. Each item is the record (with ``folder`` added)."""
    out = []
    for entry in load_index(workspace_dir)["versions"]:
        rec = read_record(workspace_dir, entry)
        if rec is not None:
            out.append(dict(rec, folder=entry.get("folder", "")))
    return out


def get_version(workspace_dir, version_id) -> Optional[Dict]:
    for rec in list_versions(workspace_dir):
        if rec.get("version") == version_id:
            return rec
    return None


def current_version(workspace_dir) -> Optional[Dict]:
    cur = load_index(workspace_dir).get("current") or ""
    return get_version(workspace_dir, cur) if cur else None


# ── writing ──────────────────────────────────────────────────────────────

def _write_json_atomic(path, payload):
    from ..core.provenance import write_json_atomic
    write_json_atomic(path, payload)


def new_version_folder(workspace_dir, now=None) -> Dict:
    """Allocate the next version id and create its folder. The version does
    not exist yet: only `commit_version` makes it exist."""
    index = load_index(workspace_dir)
    used = [int(str(e.get("version", "v0"))[1:] or 0) for e in index["versions"]
            if str(e.get("version", "")).startswith("v")]
    # Folders of failed attempts are not in the index; never reuse their number.
    root = versions_dir(workspace_dir)
    if os.path.isdir(root):
        for name in os.listdir(root):
            if name.startswith("v") and name[1:4].isdigit():
                used.append(int(name[1:4]))
    n = (max(used) if used else 0) + 1
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    vid = f"v{n:03d}"
    folder = f"{vid}_{stamp}"
    os.makedirs(version_dir(workspace_dir, folder), exist_ok=False)
    return {"version": vid, "folder": folder,
            "path": version_dir(workspace_dir, folder)}


def commit_version(workspace_dir, allocated, record, make_current=True) -> Dict:
    """Publish a version whose products are all in place (§3.12 steps 4-5):
    write ``version.json`` with ``complete: true``, then add it to the index
    and make it current -- the index is written LAST. Returns the record."""
    record = dict(record)
    record.update({"version": allocated["version"], "complete": True})
    record.setdefault("label", allocated["version"])
    record.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
    _write_json_atomic(os.path.join(allocated["path"], RECORD), record)
    index = load_index(workspace_dir)
    index["versions"] = [e for e in index["versions"]
                         if e.get("version") != allocated["version"]]
    index["versions"].append({"version": allocated["version"],
                              "folder": allocated["folder"],
                              "created_at": record["created_at"]})
    if make_current:
        index["current"] = allocated["version"]
    _write_json_atomic(index_path(workspace_dir), index)
    return dict(record, folder=allocated["folder"])


def set_current(workspace_dir, version_id) -> bool:
    """Make an existing version the current one (loading it back, §3.6)."""
    if get_version(workspace_dir, version_id) is None:
        return False
    index = load_index(workspace_dir)
    if index.get("current") != version_id:
        index["current"] = version_id
        _write_json_atomic(index_path(workspace_dir), index)
    return True


def cleanup_incomplete(workspace_dir) -> List[str]:
    """Remove version folders that do not exist as versions: not in the
    index, or without a complete record (a Generate that was cancelled,
    failed or killed). Corrected-product folders are kept (the draft may be
    one of them). Returns the removed folders."""
    root = versions_dir(workspace_dir)
    if not os.path.isdir(root):
        return []
    listed = {e.get("folder") for e in load_index(workspace_dir)["versions"]
              if read_record(workspace_dir, e) is not None}
    # A deleted version whose move to the trash was interrupted is not
    # listed either: it is the trash's, never removed here (codex review 2, #2).
    project = os.path.dirname(os.path.dirname(os.path.abspath(workspace_dir)))
    pending = pending_trash_sources(project)
    removed = []
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if name in (INDEX, CORRECTED_DIR) or name.startswith(".") or not os.path.isdir(path):
            continue
        if name not in listed and not any(is_inside(path, p) or is_inside(p, path)
                                          for p in pending):
            shutil.rmtree(path, ignore_errors=True)
            removed.append(name)
    return removed


# ── comparing (existing fields only, no hash is made) ────────────────────

def region_geometry(regions) -> List[Dict]:
    """The spatial part of `regions[]`, in a comparable form."""
    out = []
    for r in regions or []:
        poly = r.get("polygon_fullres")
        out.append({
            "roi_name": str(r.get("roi_name") or ""),
            "roi_id": str(r.get("roi_id") or ""),
            "bbox_fullres": [int(v) for v in (r.get("bbox_fullres") or [])],
            "polygon_fullres": ([[round(float(a), 3) for a in p] for p in poly]
                                if poly else None),
        })
    return sorted(out, key=lambda d: (d["roi_name"], d["roi_id"]))


def content_key(record) -> Dict:
    """What makes two versions the same (§3.2): slide, region geometry, the
    corrected product's channel signatures and region boxes, Step0's
    Intensity file hash, Step1's fusion-settings hash. All existing fields."""
    corr = record.get("corrected") or {}
    return {
        "slide_id": record.get("slide_id") or "",
        "regions": region_geometry(record.get("regions")),
        "corrected_signatures": {str(k): list(v) for k, v in
                                 (corr.get("signatures") or {}).items()},
        "corrected_bboxes": sorted([int(x) for x in b] for b in corr.get("bboxes") or []),
        "corrected_source": corr.get("source_identity") or None,
        # which channels are corrected, and how: a channel switched back to
        # raw leaves its old array in the product, so signatures alone miss it
        "corrected_decisions": {str(k): str(v).strip().lower() for k, v in
                                (record.get("channel_decisions") or {}).items()
                                if str(v).strip().lower() in ("tophat", "cucim")},
        "step0_remap_hash": record.get("step0_remap_hash") or "",
        "fusion_settings_hash": record.get("fusion_settings_hash") or "",
        "method": record.get("method") or "",
    }


def same_content(a, b) -> bool:
    return content_key(a) == content_key(b)


def same_corrected(a, b) -> bool:
    """Whole-product sharing rule (§3.4): every channel's signature, the
    region boxes and the source identity of the corrected product match."""
    ka, kb = content_key(a), content_key(b)
    keys = ("slide_id", "corrected_signatures", "corrected_bboxes", "corrected_source")
    return all(ka[k] == kb[k] for k in keys) and \
        [r["bbox_fullres"] for r in ka["regions"]] == [r["bbox_fullres"] for r in kb["regions"]]


def find_same(workspace_dir, record) -> Optional[Dict]:
    """An existing version with the same content, newest first."""
    for rec in reversed(list_versions(workspace_dir)):
        if same_content(rec, record):
            return rec
    return None


def find_same_corrected(workspace_dir, record) -> Optional[Dict]:
    for rec in reversed(list_versions(workspace_dir)):
        if same_corrected(rec, record):
            return rec
    return None


# ── disk usage ───────────────────────────────────────────────────────────

def folder_size(path) -> int:
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
    return total


def version_size(workspace_dir, record) -> int:
    """Bytes this version holds in its OWN folder: a corrected product it
    references from another version is not counted again (§3.10)."""
    return folder_size(version_dir(workspace_dir, record.get("folder", "")))


def is_inside(path, folder) -> bool:
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(folder)]) \
            == os.path.abspath(folder)
    except ValueError:
        return False


def localize(workspace_dir, path, must_exist=True) -> str:
    """`path` inside THIS workspace (acceptance finding 2026-10-03): a path
    already inside it is returned as it is; one into another folder -- a
    project copied with its absolute paths -- is mapped to the same place
    under this workspace (the part after the workspace's own folder name),
    when that exists (or `must_exist` is False). "" when there is none.
    What DV writes, moves or protects is always the workspace's own."""
    if not path:
        return ""
    ws = os.path.abspath(workspace_dir)
    if is_inside(path, ws):
        return os.path.abspath(path)
    name = os.path.basename(os.path.normpath(ws))
    parts = os.path.abspath(path).split(os.sep)
    if name in parts:
        here = os.path.join(ws, *parts[len(parts) - parts[::-1].index(name):])
        if os.path.exists(here) or not must_exist:
            return here
    return ""


def is_published_product(workspace_dir, path) -> bool:
    """True when `path` belongs to a published version (read-only, §3.14):
    inside a version folder, or a legacy product a version references."""
    if not path:
        return False
    for rec in list_versions(workspace_dir):
        if is_inside(path, version_dir(workspace_dir, rec.get("folder", ""))):
            return True
        refs = [(rec.get("corrected") or {}).get("path")] + \
            [r.get("fused_zarr_path") for r in rec.get("regions") or []]
        want = {os.path.abspath(path), localize(workspace_dir, path, must_exist=False)}
        if any(p and (os.path.abspath(p) in want
                      or localize(workspace_dir, p, must_exist=False) in want)
               for p in refs):
            return True
    return False


# ── deleting (block DV-D; docs/v16_DV_delete_application.md) ──────────────

def runs_of_version(workspace_dir, version_id) -> List:
    """The workspace's segmentation runs made from `version_id`, newest
    first (`core.step3_masks.Run`)."""
    from ..core import step3_masks
    return [r for r in step3_masks.list_runs(workspace_dir)
            if str((r.meta or {}).get("data_version") or "") == str(version_id)]


def handoff_corrected(workspace_dir) -> str:
    manifest = _load(os.path.join(workspace_dir, "step0", "step0_roi_result.json")) or {}
    return localize(workspace_dir, manifest.get("corrected_zarr_path") or "",
                    must_exist=False)


def corrected_users(workspace_dir, path, excluding=()) -> List[str]:
    """The versions (ids) other than `excluding` that reference the
    corrected product at `path`."""
    want = localize(workspace_dir, path, must_exist=False) or os.path.abspath(path)
    return [rec["version"] for rec in list_versions(workspace_dir)
            if rec.get("version") not in excluding
            and (rec.get("corrected") or {}).get("path")
            and localize(workspace_dir, rec["corrected"]["path"], must_exist=False) == want]


def _corrected_folder(path):
    """What moves with a corrected product: its own cNNN folder (with the
    coarse sidecar) when it has one, else the Zarr alone."""
    parent = os.path.dirname(os.path.abspath(path))
    if os.path.basename(os.path.dirname(parent)) == CORRECTED_DIR:
        return parent
    return os.path.abspath(path)


def plan_delete(workspace_dir, *, run_dir=None, version_id=None, draft=False,
                workspace=False, release_handoff=False) -> Dict:
    """What a deletion moves and which records it changes -- read only.

    * `run_dir`: the run and its Step4 results;
    * `version_id`: the version's folder, and its corrected product when no
      other version references it -- nor the workspace's handoff, unless
      `release_handoff` (the current version deleted without loading another:
      the handoff is left naming a product that is gone);
    * `draft`: the handoff's corrected product when it is a draft (inside
      ``versions/corrected``, referenced by no version);
    * `workspace`: the whole workspace folder."""
    ws = os.path.abspath(workspace_dir)
    paths, what = [], []
    plan = {"workspace": ws, "run_dir": "", "version": "", "draft": False,
            "delete_workspace": bool(workspace)}
    if workspace:
        return dict(plan, paths=[ws], what="workspace")
    if run_dir:
        run_dir = os.path.abspath(run_dir)
        paths.append(run_dir)
        quant = os.path.join(ws, "step4", "quantification_runs", os.path.basename(run_dir))
        if os.path.isdir(quant):
            paths.append(quant)
        paths.extend(p for p in _recorded_outputs(ws, run_dir) if not any(
            is_inside(p, q) for q in paths))
        plan["run_dir"] = run_dir
        what.append(os.path.basename(run_dir))
    handoff = handoff_corrected(ws)
    if version_id:
        rec = get_version(ws, version_id)
        if rec is None:
            raise ValueError(f"no data version {version_id!r} in {ws}")
        paths.append(version_dir(ws, rec["folder"]))
        # only this workspace's own products ever move (a copied project's
        # records may still name the original's)
        corrected = localize(ws, (rec.get("corrected") or {}).get("path") or "")
        if corrected and os.path.exists(corrected) \
                and not corrected_users(ws, corrected, excluding=(version_id,)) \
                and (release_handoff or os.path.abspath(corrected) != handoff) \
                and not is_inside(corrected, version_dir(ws, rec["folder"])):
            paths.append(_corrected_folder(corrected))
        for region in rec.get("regions") or []:        # a legacy v1's fused, in place
            fused = localize(ws, region.get("fused_zarr_path") or "")
            if fused and os.path.exists(fused) \
                    and not is_inside(fused, version_dir(ws, rec["folder"])):
                paths.append(os.path.abspath(fused))
        plan["version"] = version_id
        what.append(version_id)
    if draft:
        if handoff and is_inside(handoff, corrected_root(ws)) \
                and not corrected_users(ws, handoff) and os.path.exists(handoff):
            paths.append(_corrected_folder(handoff))
        plan["draft"] = True
        what.append("draft")
    seen, unique = set(), []
    for p in paths:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return dict(plan, paths=unique, what="_".join(what) or "nothing")


def _recorded_outputs(ws, run_dir) -> List[str]:
    """Files A3 records as made from this run wherever the user put them
    inside the project: Step4 results in another Output dir (the h5ad, its
    csv and provenance file) and the object tables (codex review 2, #7).
    Only files -- a folder may hold other runs' results."""
    from ..core import provenance as prov
    project = os.path.dirname(os.path.dirname(ws))
    meta = _load(os.path.join(run_dir, "segmentation_meta.json")) or {}
    run_id = str(meta.get("run_id") or os.path.basename(run_dir))
    try:
        entries = prov.load_entries(project)
    except Exception:                       # noqa: BLE001 -- no records, nothing extra
        return []
    out = []
    for e in entries:
        params = e.get("parameters") or {}
        if e.get("kind") not in ("step4_h5ad", "cells_parquet", "regions_parquet") \
                or str(params.get("segmentation_run_id") or "") != run_id \
                or (e.get("workspace_id") and e["workspace_id"] != os.path.basename(ws)):
            continue
        try:
            main = prov.resolve_location(project, e.get("location") or {})
        except Exception:                   # noqa: BLE001
            continue
        files = [main]
        if e.get("kind") == "step4_h5ad":
            named = {k: (v if os.path.isabs(v) else os.path.join(os.path.dirname(main), v))
                     for k, v in (params.get("files") or {}).items() if v}
            # The files may since have been replaced by another run's result
            # of the same name (codex review 3, #1): they go only while their
            # own provenance file still names THIS run.
            side = _load(named.get("provenance", "")) or {}
            if str((side.get("segmentation_run") or {}).get("run_id") or "") != run_id:
                continue
            files.extend(named.values())
        for f in files:
            f = os.path.abspath(f)
            if os.path.isfile(f) and is_inside(f, project) and f not in out:
                out.append(f)
    return out


def _drop_run_records(ws, run_dir):
    """`roi_index.json` stops naming the run (active / latest included)."""
    path = os.path.join(ws, "roi_index.json")
    index = _load(path)
    if not isinstance(index, dict):
        return
    runs = dict(index.get("segmentation_runs") or {})
    gone = [k for k, e in runs.items()
            if os.path.abspath(os.path.join(ws, (e or {}).get("path") or "")) == run_dir
            or k == os.path.basename(run_dir)]
    if not gone:
        return
    for k in gone:
        runs.pop(k, None)
    index["segmentation_runs"] = runs
    if index.get("active_segmentation_run") in gone:
        index["active_segmentation_run"] = ""
    latest = dict(index.get("latest_by_method") or {})
    for method, rid in list(latest.items()):
        if rid in gone:
            latest.pop(method)
    index["latest_by_method"] = latest
    _write_json_atomic(path, index)


def _drop_version_record(ws, version_id):
    index = load_index(ws)
    index["versions"] = [e for e in index["versions"] if e.get("version") != version_id]
    if index.get("current") == version_id:
        index["current"] = ""
    _write_json_atomic(index_path(ws), index)


def _drop_workspace_record(project_dir, workspace_id):
    from .roi_project import project_roi_index_path
    path = project_roi_index_path(project_dir)
    index = _load(path)
    if not isinstance(index, dict):
        return
    index["rois"] = [r for r in index.get("rois") or [] if r.get("roi_id") != workspace_id]
    if index.get("active_roi_id") == workspace_id:
        index["active_roi_id"] = ""
    _write_json_atomic(path, index)


def _drop_records(project, ws, details):
    if details.get("delete_workspace"):
        _drop_workspace_record(project, os.path.basename(ws))
        return
    if details.get("run_dir"):
        _drop_run_records(ws, details["run_dir"])
    if details.get("version"):
        _drop_version_record(ws, details["version"])


def resume_deletions(project_dir) -> List[str]:
    """A deletion interrupted half-way (§6): its records are dropped (again;
    harmless when they already were), then what is still in place moves.
    Called when the project is loaded. Returns the finished entries."""
    from . import trash
    project = os.path.abspath(project_dir)
    for entry in trash.entries(project):
        if entry.get("status") == "complete":
            continue
        ws = os.path.join(project, "rois", str(entry.get("workspace") or ""))
        _drop_records(project, ws, entry.get("details") or {})
    try:
        done = trash.resume_incomplete(project)
    finally:
        # Every complete entry has its A3 record -- also one finished just
        # before a later entry failed (codex review 3, #5). Idempotent.
        for entry in trash.entries(project):
            if entry.get("status") == "complete":
                _register_deletion(project, entry)
    return done


def pending_trash_sources(project_dir) -> List[str]:
    """What interrupted deletions have not moved yet."""
    from . import trash
    return [i["from"] for e in trash.entries(project_dir)
            if e.get("status") != "complete" for i in e.get("items") or []
            if os.path.lexists(i["from"])]


def execute_delete(plan, now=None) -> Dict:
    """Carry out `plan` (§6): the trash entry is written `in_progress`, the
    records stop naming what goes, the folders move, the entry is marked
    complete; then one A3 `deletion` entry is appended. Returns the entry."""
    from . import trash
    from ..core import provenance as prov
    ws = plan["workspace"]
    project = os.path.dirname(os.path.dirname(ws))
    ws_id = os.path.basename(ws)
    details = {k: plan[k] for k in ("run_dir", "version", "draft", "delete_workspace")}
    entry = trash.begin(project, ws_id, plan["what"], plan["paths"], details, now=now)
    _drop_records(project, ws, details)
    entry = trash.finish(entry)
    _register_deletion(project, entry)
    print(f"[Trash] moved {len(entry['items'])} item(s) to {entry['folder']}")
    return entry


def _register_deletion(project, entry):
    """The A3 `deletion` entry of a finished trash entry. Idempotent: A3 finds
    an existing (kind, location, token) and returns it."""
    from ..core import provenance as prov
    details = entry.get("details") or {}
    try:
        prov.register(project, "deletion", prov.location(project, entry["folder"]),
                      os.path.basename(entry["folder"]),
                      workspace_id=str(entry.get("workspace") or ""),
                      parameters={"what": entry.get("what"),
                                  "run_dir": details.get("run_dir", ""),
                                  "data_version": details.get("version", ""),
                                  "draft": bool(details.get("draft")),
                                  "workspace": bool(details.get("delete_workspace")),
                                  "moved": [{"from": i["from"], "to": i["to"]}
                                            for i in entry["items"]]})
    except Exception as exc:               # noqa: BLE001 -- auxiliary record
        print(f"[Provenance] deletion not recorded ({type(exc).__name__}: {exc})")
