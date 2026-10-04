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
    # Block A6 (user ruling 2026-10-02): read, changed and written back by
    # every finished Step2 run -- swapped in whole, so a failed write leaves
    # the previous registry readable.
    from ..core.provenance import write_json_atomic
    os.makedirs(registry_dir(project_output_dir), exist_ok=True)
    write_json_atomic(registry_path(project_output_dir), data)


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


