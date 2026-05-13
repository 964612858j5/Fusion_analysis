"""ROI-scoped project directory helpers.

This module keeps the new ROI identity/path rules isolated from the UI pages.
Display names such as ROI_1 are not stable identifiers; roi_id is the primary
key used for on-disk storage.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime


PROJECT_MANIFEST = "project_manifest.json"
PROJECT_ROI_INDEX = "roi_index.json"
ROI_MANIFEST = "roi_manifest.json"
ROI_INDEX = "roi_index.json"


def _now_compact():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _abs(path):
    return os.path.abspath(path) if path else ""


def _rel(path, base):
    try:
        return os.path.relpath(path, base)
    except Exception:
        return path


def generate_roi_id():
    return f"roi_{_now_compact()}_{uuid.uuid4().hex[:4]}"


def generate_full_wsi_id():
    return f"full_wsi_{_now_compact()}_{uuid.uuid4().hex[:4]}"


def roi_shape_from_bbox(bbox):
    if not bbox or len(bbox) != 4:
        return [0, 0]
    y0, y1, x0, x1 = [int(v) for v in bbox]
    return [max(0, y1 - y0), max(0, x1 - x0)]


def project_manifest_path(project_dir):
    return os.path.join(project_dir, PROJECT_MANIFEST)


def project_roi_index_path(project_dir):
    return os.path.join(project_dir, PROJECT_ROI_INDEX)


def roi_dir(project_dir, roi_id):
    return os.path.join(project_dir, "rois", roi_id)


def roi_step_dir(project_dir, roi_id, step_name):
    return os.path.join(roi_dir(project_dir, roi_id), step_name)


def roi_manifest_path(roi_dir_path):
    return os.path.join(roi_dir_path, ROI_MANIFEST)


def roi_index_path(roi_dir_path):
    return os.path.join(roi_dir_path, ROI_INDEX)


def load_json(path, default=None):
    if not path or not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def ensure_project_manifest(project_dir, raw_ome_path=None):
    os.makedirs(project_dir, exist_ok=True)
    path = project_manifest_path(project_dir)
    payload = load_json(path, {}) or {}
    payload.setdefault("version", 1)
    payload.setdefault("created_at", _now_iso())
    payload["updated_at"] = _now_iso()
    payload["project_dir"] = _abs(project_dir)
    if raw_ome_path:
        payload["source_ome"] = _abs(raw_ome_path)
    save_json(path, payload)
    return payload


def _default_roi_index(roi_id):
    return {
        "version": 1,
        "roi_id": roi_id,
        "active_step": "step0",
        "steps": {
            "step0": {"status": "pending", "path": "step0/"},
            "step1": {"status": "pending", "path": "step1/"},
            "step2": {"status": "pending", "path": "step2/"},
            "step3": {"status": "pending", "path": "step3/"},
        },
    }


def update_project_roi_index(project_dir, roi_manifest):
    idx_path = project_roi_index_path(project_dir)
    data = load_json(idx_path, None) or {"version": 1, "active_roi_id": "", "rois": []}
    rois = list(data.get("rois") or [])
    roi_id = roi_manifest["roi_id"]
    entry = {
        "roi_id": roi_id,
        "display_name": roi_manifest.get("display_name", ""),
        "type": roi_manifest.get("type", roi_manifest.get("analysis_region_type", "roi")),
        "created_at": roi_manifest.get("created_at", ""),
        "status": roi_manifest.get("status", "active"),
        "manifest": _rel(os.path.join(roi_dir(project_dir, roi_id), ROI_MANIFEST), project_dir),
    }
    replaced = False
    for i, old in enumerate(rois):
        if old.get("roi_id") == roi_id:
            rois[i] = entry
            replaced = True
            break
    if not replaced:
        rois.append(entry)
    data["version"] = 1
    data["active_roi_id"] = roi_id
    data["rois"] = rois
    save_json(idx_path, data)
    return data


def create_roi_context(project_dir, roi, source_ome=None, display_name=None):
    """Create a fresh immutable ROI context, regardless of display_name reuse."""
    project_dir = _abs(project_dir)
    ensure_project_manifest(project_dir, source_ome)
    roi_id = generate_roi_id()
    display_name = display_name or str(roi.get("name") or roi.get("display_name") or "ROI_1")
    bbox = [int(v) for v in (roi.get("bbox_fullres") or [])] if len(roi.get("bbox_fullres") or []) == 4 else []
    manifest = {
        "roi_id": roi_id,
        "display_name": display_name,
        "created_at": _now_iso(),
        "source_ome": _abs(source_ome),
        "bbox_fullres": bbox,
        "polygon_fullres": roi.get("polygon_fullres") or [],
        "shape": roi.get("shape") or roi_shape_from_bbox(bbox),
        "status": "active",
    }
    rdir = roi_dir(project_dir, roi_id)
    os.makedirs(rdir, exist_ok=True)
    for step in ("step0", "step1", "step2", "step3", "future"):
        os.makedirs(os.path.join(rdir, step), exist_ok=True)
    save_json(roi_manifest_path(rdir), manifest)
    save_json(roi_index_path(rdir), _default_roi_index(roi_id))
    update_project_roi_index(project_dir, manifest)
    print(f"[ROI] created roi_id={roi_id}")
    print(f"[ROI] display_name={display_name}")
    print(f"[ROI] roi_dir={rdir}")
    print(f"[ROI] bbox={bbox}")
    return build_roi_context(project_dir, roi_id)


def create_full_wsi_context(project_dir, image_shape, source_ome=None):
    """Create a fresh immutable ROI-like context for full-slide processing."""
    project_dir = _abs(project_dir)
    ensure_project_manifest(project_dir, source_ome)
    h, w = [int(v) for v in image_shape[:2]]
    roi_id = generate_full_wsi_id()
    bbox = [0, h, 0, w]
    manifest = {
        "roi_id": roi_id,
        "display_name": "Full WSI",
        "type": "full_wsi",
        "analysis_region_type": "full_wsi",
        "created_at": _now_iso(),
        "source_ome": _abs(source_ome),
        "bbox_fullres": bbox,
        "polygon_fullres": None,
        "shape": [h, w],
        "status": "active",
    }
    rdir = roi_dir(project_dir, roi_id)
    os.makedirs(rdir, exist_ok=True)
    for step in ("step0", "step1", "step2", "step3", "future"):
        os.makedirs(os.path.join(rdir, step), exist_ok=True)
    save_json(roi_manifest_path(rdir), manifest)
    save_json(roi_index_path(rdir), _default_roi_index(roi_id))
    update_project_roi_index(project_dir, manifest)
    print(f"[ROI] created roi_id={roi_id}")
    print("[ROI] display_name=Full WSI")
    print(f"[ROI] roi_dir={rdir}")
    print(f"[ROI] bbox={bbox}")
    return build_roi_context(project_dir, roi_id)


def build_roi_context(project_dir, roi_id):
    project_dir = _abs(project_dir)
    rdir = roi_dir(project_dir, roi_id)
    return {
        "project_dir": project_dir,
        "roi_id": roi_id,
        "roi_dir": rdir,
        "manifest_path": roi_manifest_path(rdir),
        "index_path": roi_index_path(rdir),
        "step_dirs": {
            step: os.path.join(rdir, step)
            for step in ("step0", "step1", "step2", "step3", "future")
        },
    }


def mark_roi_step(project_dir, roi_id, step_name, status="done"):
    ctx = build_roi_context(project_dir, roi_id)
    idx = load_json(ctx["index_path"], None) or _default_roi_index(roi_id)
    idx.setdefault("steps", {})
    idx["active_step"] = step_name
    idx["steps"].setdefault(step_name, {"path": f"{step_name}/"})
    idx["steps"][step_name]["status"] = status
    idx["steps"][step_name]["path"] = f"{step_name}/"
    save_json(ctx["index_path"], idx)
    return idx


def update_roi_segmentation_run(roi_dir_path, run_entry):
    """Append/update one Step2 segmentation run in the ROI-level index."""
    if not roi_dir_path or not run_entry:
        return None
    manifest = load_json(roi_manifest_path(roi_dir_path), {}) or {}
    roi_id = manifest.get("roi_id") or os.path.basename(roi_dir_path)
    idx_path = roi_index_path(roi_dir_path)
    idx = load_json(idx_path, None) or _default_roi_index(roi_id)
    run_id = run_entry.get("run_id")
    if not run_id:
        return idx
    idx.setdefault("segmentation_runs", {})
    idx["segmentation_runs"][run_id] = {
        "run_id": run_id,
        "method": run_entry.get("method", ""),
        "created_at": run_entry.get("created_at", ""),
        "path": run_entry.get("path", ""),
        "status": run_entry.get("status", "done"),
        "meta_path": run_entry.get("meta_path", ""),
    }
    idx["active_segmentation_run"] = run_id
    method = run_entry.get("method")
    if method:
        idx.setdefault("latest_by_method", {})[method] = run_id
    idx["active_step"] = "step2"
    idx.setdefault("steps", {}).setdefault("step2", {"path": "step2/"})
    idx["steps"]["step2"]["status"] = "done"
    idx["steps"]["step2"]["path"] = "step2/"
    save_json(idx_path, idx)
    return idx


def resolve_roi_context(path, default_project_dir=None):
    """Resolve project dir, ROI dir, step0 dir, or step0 manifest to context."""
    path = _abs(path or default_project_dir or "")
    if not path:
        return None
    if os.path.isfile(path):
        if os.path.basename(path) == "step0_roi_result.json":
            manifest = load_json(path, {}) or {}
            roi_id = manifest.get("roi_id")
            roi_dir_path = manifest.get("roi_dir")
            project_dir = manifest.get("project_output_dir")
            if roi_id and roi_dir_path:
                project_dir = project_dir or os.path.dirname(os.path.dirname(roi_dir_path))
                ctx = build_roi_context(project_dir, roi_id)
                ctx["step0_manifest_path"] = path
                return ctx
        path = os.path.dirname(path)

    cur = path
    if os.path.basename(cur) == "step0":
        roi_dir_path = os.path.dirname(cur)
        manifest = load_json(roi_manifest_path(roi_dir_path), {}) or {}
        roi_id = manifest.get("roi_id") or os.path.basename(roi_dir_path)
        project_dir = os.path.dirname(os.path.dirname(roi_dir_path))
        ctx = build_roi_context(project_dir, roi_id)
        ctx["step0_manifest_path"] = os.path.join(cur, "step0_roi_result.json")
        return ctx

    if os.path.exists(roi_manifest_path(cur)):
        manifest = load_json(roi_manifest_path(cur), {}) or {}
        roi_id = manifest.get("roi_id") or os.path.basename(cur)
        project_dir = os.path.dirname(os.path.dirname(cur))
        return build_roi_context(project_dir, roi_id)

    idx_path = project_roi_index_path(cur)
    if os.path.exists(idx_path):
        data = load_json(idx_path, {}) or {}
        roi_id = data.get("active_roi_id")
        if roi_id:
            return build_roi_context(cur, roi_id)

    print("[Project] legacy layout detected")
    return {
        "project_dir": path,
        "roi_id": "",
        "roi_dir": "",
        "manifest_path": "",
        "index_path": "",
        "step_dirs": {
            "step0": path,
            "step1": path,
            "step2": path,
            "step3": path,
        },
        "legacy": True,
    }
