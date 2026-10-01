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
         "corrected_coarse_levels")

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
    project = os.path.abspath(project_dir)
    full = os.path.abspath(path)
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
        return os.path.join(os.path.abspath(project_dir), *loc["path"].split("/"))
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


__all__ = ["SCHEMA_VERSION", "KINDS", "ProvenanceError", "write_json_atomic", "location",
           "resolve_location", "load_entries", "lookup", "validate", "register",
           "safe_register", "is_artifact_id", "is_region_id", "provenance_dir",
           "FLAG_RUN_ID_COLLISION", "FLAG_REGION_MISMATCH"]
