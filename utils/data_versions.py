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
    removed = []
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if name in (INDEX, CORRECTED_DIR) or name.startswith(".") or not os.path.isdir(path):
            continue
        if name not in listed:
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
        if any(p and os.path.abspath(p) == os.path.abspath(path) for p in refs):
            return True
    return False
