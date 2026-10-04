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
LEGACY_METHOD = "legacy_cellpose_wholecell_fusion"


def _abs(path):
    return os.path.abspath(path) if path else ""


def _safe_name(text):
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(text or "").strip())
    return value.strip("._") or "segmentation"


def registry_dir(project_output_dir):
    return os.path.join(project_output_dir, REGISTRY_DIRNAME)


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
