"""The project's trash (block DV-D; docs/v16_DV_delete_application.md §5, §6).

What the user deletes from Step0's "Open a workspace" chooser is not removed
at once: it is MOVED into ``<project>/.trash/<entry>/`` -- a rename on the same
disk, so it takes no time and no space -- and permanently removed 30 days
after the deletion, or when the user empties the trash.

    .trash/20261003_143000_<workspace id>_<what>/
        trash.json            {"status": "in_progress" | "complete",
                               "deleted_at", "what", "workspace", "items": [...]}
        <path relative to the project>/...   the moved folders and files

A deletion moves several folders and is not one atomic step (§6): the entry
is written ``in_progress`` FIRST, the caller removes the records that name
the items (so they no longer exist for the program), the items are moved,
and the entry is marked ``complete`` LAST. An entry still ``in_progress`` when
the project is next loaded is finished by `resume_incomplete`. Only complete
entries are ever purged.

Nothing here is discovered by the program as a workspace, a version or a run:
those are found under ``rois/``, never under ``.trash/``.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta
from typing import Dict, List, Optional

TRASH_DIR = ".trash"
ENTRY = "trash.json"
KEEP_DAYS = 30


def trash_root(project_dir) -> str:
    return os.path.join(os.path.abspath(project_dir), TRASH_DIR)


def _write(path, payload):
    from ..core.provenance import write_json_atomic
    write_json_atomic(path, payload)


def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _size(path) -> int:
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
    return total


def begin(project_dir, workspace_id, what, paths, details=None, now=None) -> Dict:
    """Write an `in_progress` entry for moving `paths` (absolute, inside the
    project) into the trash. Nothing is moved yet. Returns the entry."""
    project_dir = os.path.abspath(project_dir)
    now = now or datetime.now()
    root = trash_root(project_dir)
    os.makedirs(root, exist_ok=True)
    base = f"{now.strftime('%Y%m%d_%H%M%S')}_{workspace_id}_{what}".replace(os.sep, "_")
    name, n = base, 1
    while os.path.exists(os.path.join(root, name)):
        n += 1
        name = f"{base}_{n}"
    folder = os.path.join(root, name)
    os.makedirs(folder)
    items = []
    for path in paths:
        path = os.path.abspath(path)
        rel = os.path.relpath(path, project_dir)
        if rel.startswith(os.pardir):
            raise ValueError(f"{path} is not inside the project {project_dir}")
        items.append({"from": path, "to": rel, "bytes": _size(path)})
    entry = {"status": "in_progress", "deleted_at": now.isoformat(timespec="seconds"),
             "what": what, "workspace": workspace_id, "details": dict(details or {}),
             "items": items, "folder": folder}
    _write(os.path.join(folder, ENTRY), entry)
    return entry


def finish(entry) -> Dict:
    """Move every item that is still in place, then mark the entry complete."""
    for item in entry["items"]:
        src = item["from"]
        dst = os.path.join(entry["folder"], item["to"])
        if not os.path.lexists(src):
            continue                       # moved already (a resumed entry)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
    entry = dict(entry, status="complete")
    _write(os.path.join(entry["folder"], ENTRY), entry)
    return entry


def entries(project_dir) -> List[Dict]:
    root = trash_root(project_dir)
    if not os.path.isdir(root):
        return []
    out = []
    for name in sorted(os.listdir(root)):
        folder = os.path.join(root, name)
        entry = _load(os.path.join(folder, ENTRY))
        if entry is not None:
            out.append(dict(entry, folder=folder))
    return out


def resume_incomplete(project_dir) -> List[str]:
    """Move what interrupted deletions left in place (§6). Their records are
    dropped first by `data_versions.resume_deletions`, which calls this.
    Returns their folders."""
    done = []
    for entry in entries(project_dir):
        if entry.get("status") != "complete":
            finish(entry)
            done.append(entry["folder"])
    return done


def purge(project_dir, now=None, keep_days=KEEP_DAYS) -> List[str]:
    """Permanently remove complete entries deleted more than `keep_days`
    days ago. Returns the removed folders."""
    now = now or datetime.now()
    removed = []
    for entry in entries(project_dir):
        if entry.get("status") != "complete":
            continue
        try:
            when = datetime.fromisoformat(str(entry.get("deleted_at")))
        except ValueError:
            continue
        if now - when > timedelta(days=keep_days):
            shutil.rmtree(entry["folder"], ignore_errors=True)
            removed.append(entry["folder"])
    return removed


def empty(project_dir) -> List[str]:
    """Empty the trash: every COMPLETE entry is permanently removed (an
    interrupted one is finished on the next Load, records first)."""
    removed = []
    for entry in entries(project_dir):
        if entry.get("status") != "complete":
            continue
        shutil.rmtree(entry["folder"], ignore_errors=True)
        removed.append(entry["folder"])
    return removed


def size(project_dir) -> int:
    root = trash_root(project_dir)
    return _size(root) if os.path.isdir(root) else 0


def is_in_trash(project_dir, path) -> bool:
    root = trash_root(project_dir)
    try:
        return os.path.commonpath([os.path.abspath(path), root]) == root
    except ValueError:
        return False


def last_entry(project_dir) -> Optional[Dict]:
    found = entries(project_dir)
    return found[-1] if found else None
