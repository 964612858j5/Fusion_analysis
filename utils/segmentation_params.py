"""JSON helpers for Step1 -> Step2 segmentation parameter handoff."""

import json
import os
from datetime import datetime

from .segmentation_config import normalize_segmentation_config


PARAM_DIRNAME = "segmentation_params"
PARAM_INDEX = "segmentation_params_index.json"


def _abs(path):
    return os.path.abspath(path) if path else ""


def params_dir(output_dir):
    return os.path.join(output_dir, PARAM_DIRNAME)


def params_index_path(output_dir):
    return os.path.join(params_dir(output_dir), PARAM_INDEX)


def _timestamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def save_segmentation_params(output_dir, config):
    """Save a timestamped method-specific config and update active index."""
    cfg = normalize_segmentation_config(config)
    method = cfg.get("method")
    out_dir = params_dir(output_dir)
    os.makedirs(out_dir, exist_ok=True)

    created_at = datetime.now().isoformat()
    cfg["created_at"] = cfg.get("created_at") or created_at
    cfg["source"] = cfg.get("source") or "Step1"
    filename = f"{method}_{_timestamp()}.json"
    path = os.path.join(out_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

    index = load_params_index(output_dir)
    updated = created_at
    index["active_method"] = method
    index["active_param_file"] = filename
    index["updated_at"] = updated
    method_info = index.setdefault("methods", {}).setdefault(method, {})
    history = list(method_info.get("history") or [])
    if filename not in history:
        history.append(filename)
    method_info.update({
        "latest": filename,
        "history": history,
        "updated_at": updated,
    })
    with open(params_index_path(output_dir), "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)
    return _abs(path), index


def load_params_index(output_dir):
    path = params_index_path(output_dir)
    if not os.path.exists(path):
        return {"active_method": "", "active_param_file": "", "updated_at": "", "methods": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"active_method": "", "active_param_file": "", "updated_at": "", "methods": {}}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("active_method", "")
    data.setdefault("active_param_file", "")
    data.setdefault("updated_at", "")
    data.setdefault("methods", {})
    return data


def active_params_path(output_dir):
    index = load_params_index(output_dir)
    rel = index.get("active_param_file") or ""
    if not rel:
        return ""
    path = rel if os.path.isabs(rel) else os.path.join(params_dir(output_dir), rel)
    return _abs(path) if os.path.exists(path) else ""


def load_active_segmentation_params(output_dir):
    path = active_params_path(output_dir)
    if not path:
        return None, ""
    with open(path, "r", encoding="utf-8") as f:
        return normalize_segmentation_config(json.load(f)), path
