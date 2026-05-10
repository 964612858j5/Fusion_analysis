"""Segmentation result registry helpers.

The registry is intentionally small and JSON-based so Step2 can append
completed runs and Step3 can discover comparable results without knowing
which segmentation method produced them.
"""

import json
import os
import re
from datetime import datetime

from .segmentation_config import CELLPOSE_WHOLECELL_FUSION, normalize_segmentation_config
from .segmentation_params import active_params_path


REGISTRY_DIRNAME = "segmentation_results"
REGISTRY_FILENAME = "segmentation_registry.json"
LEGACY_METHOD = "legacy_cellpose_wholecell_fusion"


def _abs(path):
    return os.path.abspath(path) if path else ""


def _safe_name(text):
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(text or "").strip())
    return value.strip("._") or "segmentation"


def registry_dir(project_output_dir):
    return os.path.join(project_output_dir, REGISTRY_DIRNAME)


def registry_path(project_output_dir):
    return os.path.join(registry_dir(project_output_dir), REGISTRY_FILENAME)


def make_result_id(method, created_at=None):
    dt = created_at or datetime.now()
    return f"{dt.strftime('%Y%m%d_%H%M%S')}_{_safe_name(method)}"


def create_result_dir(project_output_dir, method):
    os.makedirs(registry_dir(project_output_dir), exist_ok=True)
    now = datetime.now()
    base_id = make_result_id(method, now)
    result_id = base_id
    out_dir = os.path.join(registry_dir(project_output_dir), result_id)
    suffix = 1
    while os.path.exists(out_dir):
        result_id = f"{base_id}_{suffix:02d}"
        out_dir = os.path.join(registry_dir(project_output_dir), result_id)
        suffix += 1
    os.makedirs(out_dir, exist_ok=True)
    return result_id, out_dir, now.isoformat()


def load_registry(project_output_dir):
    path = registry_path(project_output_dir)
    if not os.path.exists(path):
        return {"version": 1, "results": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"version": 1, "results": []}
    if isinstance(data, list):
        return {"version": 1, "results": data}
    if not isinstance(data, dict):
        return {"version": 1, "results": []}
    data.setdefault("version", 1)
    data.setdefault("results", [])
    return data


def save_registry(project_output_dir, data):
    os.makedirs(registry_dir(project_output_dir), exist_ok=True)
    with open(registry_path(project_output_dir), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def upsert_result(project_output_dir, entry):
    data = load_registry(project_output_dir)
    results = data.setdefault("results", [])
    rid = entry.get("result_id")
    for i, old in enumerate(results):
        if old.get("result_id") == rid:
            results[i] = entry
            break
    else:
        results.append(entry)
    save_registry(project_output_dir, data)
    return entry


def register_legacy_result(project_output_dir):
    """Register old project-root global_mask/global_dapi outputs if present."""
    mask = os.path.join(project_output_dir, "global_mask.ome.tiff")
    dapi = os.path.join(project_output_dir, "global_dapi.ome.tiff")
    if not (os.path.exists(mask) and os.path.exists(dapi)):
        return None

    data = load_registry(project_output_dir)
    mask_abs = _abs(mask)
    for item in data.get("results", []):
        if item.get("mask_path") == mask_abs:
            return item

    config_path = active_params_path(project_output_dir)
    meta_path = os.path.join(project_output_dir, "segmentation_meta.json")
    fusion_path = os.path.join(project_output_dir, "fused.zarr")
    multichannel = os.path.join(project_output_dir, "corrected_channels.zarr")

    now = datetime.now().isoformat()
    cfg = normalize_segmentation_config({"method": CELLPOSE_WHOLECELL_FUSION})
    entry = {
        "result_id": LEGACY_METHOD,
        "method": LEGACY_METHOD,
        "display_name": "Legacy Cellpose whole-cell (Fusion + DAPI)",
        "created_at": now,
        "status": "completed",
        "mask_path": mask_abs,
        "dapi_path": _abs(dapi),
        "fusion_path": _abs(fusion_path) if os.path.exists(fusion_path) else "",
        "multichannel_source_path": _abs(multichannel) if os.path.exists(multichannel) else "",
        "config_path": _abs(config_path) if os.path.exists(config_path) else "",
        "meta_path": _abs(meta_path) if os.path.exists(meta_path) else "",
        "output_dir": _abs(project_output_dir),
        "notes": f"Auto-registered legacy output as {cfg['display_name']}.",
    }
    data.setdefault("results", []).append(entry)
    save_registry(project_output_dir, data)
    return entry
