"""Which Step0 workspace is open, and where Save writes (block A6 W1-W3).

A project directory (``project_manifest.json``) holds workspaces under
``rois/<workspace_id>/``. Loading a slide into a project that already has
workspaces OF THE SAME SLIDE opens one of them, so Save writes back into it
instead of creating another; Save as always creates a new one.

"The same slide" is decided by the A3 ``slide_id`` (file content), never by
path: a workspace's slide is the ``slide_id`` of its provenance entries, or,
for a workspace made before A3 registered anything, the ``slide_id`` of the
slide file its manifest names. A workspace whose slide cannot be identified
is not offered.

Only a workspace with a committed Step0 result (``step0/step0_roi_result.json``)
is offered: one that was created by a Save that never finished has nothing to
open.

A8 lifts this into ProjectState (plan §17.1); there is no second copy.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import roi_project

STEP0_RESULT = "step0_roi_result.json"


@dataclass
class Workspace:
    """One openable workspace of the slide being loaded."""
    project_dir: str
    workspace_id: str
    display_name: str
    created_at: str
    region_type: str
    steps: Dict[str, str] = field(default_factory=dict)   # step -> status
    active: bool = False                                    # the index's active one

    @property
    def workspace_dir(self) -> str:
        return roi_project.roi_dir(self.project_dir, self.workspace_id)

    @property
    def step0_dir(self) -> str:
        return os.path.join(self.workspace_dir, "step0")

    def label(self) -> str:
        """One line for the chooser: name, id, when, how far it got."""
        done = [s for s in ("step1", "step2", "step3") if self.steps.get(s) == "done"]
        reached = ("Step0 + " + ", ".join(s.capitalize() for s in done)) if done else "Step0"
        when = self.created_at.replace("T", " ") if self.created_at else "?"
        return f"{self.display_name}  —  {when}  —  {reached}  ({self.workspace_id})"


def _load(path) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _workspace_slide_ids(project_dir) -> Dict[str, str]:
    """``{workspace_id: slide_id}`` from the provenance entries (block A3)."""
    from ..core import provenance as prov
    out = {}
    try:
        entries = prov.load_entries(project_dir)
    except (OSError, ValueError):
        return out
    for e in entries:
        ws, sid = e.get("workspace_id"), e.get("slide_id")
        if ws and sid:
            out.setdefault(ws, sid)
    return out


def slide_id_of(path, sources=None) -> Optional[str]:
    """The A3 ``slide_id`` of the slide file at `path`, or None when it
    cannot be read. `sources` is the manifest's cache (reused when the
    file's fingerprint is unchanged)."""
    from ..core import project_identity as pid
    if not path:
        return None
    if os.path.exists(path):
        try:
            return pid.describe_slide(path, sources)[0]
        except Exception as exc:                     # noqa: BLE001 -- not identifiable
            print(f"[Workspace] could not identify {path} ({type(exc).__name__}: {exc})")
            return None
    # The file is gone; the manifest may still say what it was.
    absp = os.path.abspath(path)
    for sid, entry in (sources or {}).items():
        if isinstance(entry, dict) and entry.get("path") == absp:
            return sid
    return None


def find_workspaces(project_dir, slide_path) -> Tuple[Optional[str], List[Workspace]]:
    """``(slide_id, workspaces)`` of `slide_path` in `project_dir`, newest
    first. No project, an unreadable slide or no match: an empty list."""
    manifest = _load(roi_project.project_manifest_path(project_dir))
    if not isinstance(manifest, dict):
        return None, []
    sources = manifest.get("sources") or {}
    sid = slide_id_of(slide_path, sources)
    if sid is None:
        return None, []
    index = _load(roi_project.project_roi_index_path(project_dir)) or {}
    active_id = index.get("active_roi_id") or ""
    recorded = _workspace_slide_ids(project_dir)
    by_path: Dict[str, Optional[str]] = {}
    found = []
    for entry in index.get("rois") or []:
        ws_id = entry.get("roi_id") or ""
        if not ws_id:
            continue
        wdir = roi_project.roi_dir(project_dir, ws_id)
        if not os.path.isfile(os.path.join(wdir, "step0", STEP0_RESULT)):
            continue
        ws_manifest = _load(roi_project.roi_manifest_path(wdir)) or {}
        ws_sid = recorded.get(ws_id)
        if ws_sid is None:
            src = ws_manifest.get("source_ome") or ""
            if src not in by_path:
                by_path[src] = slide_id_of(src, sources)
            ws_sid = by_path[src]
        if ws_sid != sid:
            continue
        ws_index = _load(roi_project.roi_index_path(wdir)) or {}
        steps = {name: str((info or {}).get("status") or "")
                 for name, info in (ws_index.get("steps") or {}).items()}
        found.append(Workspace(
            project_dir=os.path.abspath(project_dir), workspace_id=ws_id,
            display_name=str(ws_manifest.get("display_name") or entry.get("display_name")
                             or ws_id),
            created_at=str(ws_manifest.get("created_at") or entry.get("created_at") or ""),
            region_type=str(ws_manifest.get("type") or entry.get("type") or "roi"),
            steps=steps, active=(ws_id == active_id)))
    found.sort(key=lambda w: (w.created_at, w.workspace_id), reverse=True)
    return sid, found


def workspace_regions(ws: Workspace) -> List[dict]:
    """The analysis regions the workspace's Step0 committed (its
    ``roi_config.json``), or the one its manifest describes."""
    rois = _load(os.path.join(ws.step0_dir, "roi_config.json"))
    if isinstance(rois, list) and rois:
        return [dict(r) for r in rois if isinstance(r, dict)]
    manifest = _load(roi_project.roi_manifest_path(ws.workspace_dir)) or {}
    if not manifest:
        return []
    return [dict(manifest, name=manifest.get("display_name") or "ROI_1")]


def open_context(ws: Workspace) -> dict:
    """The ``roi_context`` Step0 writes through, for `ws` (the same shape
    `create_roi_context` returns)."""
    return roi_project.build_roi_context(ws.project_dir, ws.workspace_id)


def mark_active(ws: Workspace):
    """Make `ws` the project's active workspace (the index's
    ``active_roi_id``), the way a Save into it would."""
    path = roi_project.project_roi_index_path(ws.project_dir)
    data = _load(path)
    if not isinstance(data, dict) or data.get("active_roi_id") == ws.workspace_id:
        return
    from ..core.provenance import write_json_atomic
    data["active_roi_id"] = ws.workspace_id
    write_json_atomic(path, data)


def rewrite_geometry(roi_context, roi, full_wsi=False):
    """W2, the user kept the workspace although its region changed: its
    manifest takes the new geometry (the Save that follows rewrites every
    product under it)."""
    from ..core.provenance import write_json_atomic
    path = roi_context["manifest_path"]
    manifest = _load(path) or {}
    bbox = [int(v) for v in (roi.get("bbox_fullres") or [])]
    if full_wsi:
        manifest["display_name"] = "Full WSI"
    elif roi.get("name"):
        manifest["display_name"] = str(roi["name"])
    manifest["bbox_fullres"] = bbox
    manifest["polygon_fullres"] = None if full_wsi else (roi.get("polygon_fullres") or [])
    manifest["shape"] = roi_project.roi_shape_from_bbox(bbox)
    if full_wsi:
        manifest["type"] = manifest["analysis_region_type"] = "full_wsi"
    else:
        manifest["type"] = manifest["analysis_region_type"] = str(
            roi.get("type") or roi.get("analysis_region_type") or "roi")
    write_json_atomic(path, manifest)
    roi_project.update_project_roi_index(roi_context["project_dir"], manifest)
