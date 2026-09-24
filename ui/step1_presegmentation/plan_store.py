"""Saved method plans (plan block B, 4.3). No Qt; no computation.

    <step1_dir>/segmentation_search_plans/plan_<YYYYmmdd_HHMMSS>[_NN].json
    <step1_dir>/segmentation_search_plans/index.json

A plan is `{version, created_at, source_identity, methods: [{method, values}],
patches: [{id, name, bbox}], selected_ids: [id]}` -- EVERY patch with its
position and size, and which were ticked (user ruling, 2026-09-24), so that
loading can bring the patches back. It is kept apart from the final
segmentation parameters (`segmentation_params/`). Ticks that name a patch
that is not there are dropped and counted -- matched by the permanent id
(plan block P).
"""

import json
import os
from datetime import datetime

PLAN_DIRNAME = "segmentation_search_plans"
INDEX_NAME = "index.json"
VERSION = 1


def plans_dir(step1_dir):
    return os.path.join(step1_dir, PLAN_DIRNAME)


def _write_json(path, data):
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def list_plans(step1_dir):
    """The index, newest first: [{file, created_at, n_methods, n_patches}]."""
    try:
        with open(os.path.join(plans_dir(step1_dir), INDEX_NAME), encoding="utf-8") as f:
            entries = json.load(f).get("plans") or []
    except (OSError, ValueError, AttributeError):
        return []
    return sorted(entries, key=lambda e: e.get("created_at", ""), reverse=True)


def save_plan(step1_dir, methods, patches, selected_ids, source_identity=None, now=None):
    """Write a new plan file and add it to the index. Returns its path."""
    now = now or datetime.now()
    out = plans_dir(step1_dir)
    os.makedirs(out, exist_ok=True)
    stem = f"plan_{now.strftime('%Y%m%d_%H%M%S')}"
    name, n = f"{stem}.json", 1
    while os.path.exists(os.path.join(out, name)):
        name, n = f"{stem}_{n:02d}.json", n + 1
    plan = {
        "version": VERSION,
        "created_at": now.isoformat(timespec="seconds"),
        "source_identity": source_identity or {},
        "methods": [{"method": m["method"], "values": m["values"]} for m in methods],
        "patches": [
            {"id": p.get("id"), "name": p.get("name"), "bbox": list(p.get("bbox") or [])}
            for p in patches],
        "selected_ids": [int(i) for i in selected_ids],
    }
    path = os.path.join(out, name)
    _write_json(path, plan)
    entries = list_plans(step1_dir) + [{
        "file": name, "created_at": plan["created_at"],
        "n_methods": len(plan["methods"]), "n_patches": len(plan["patches"]),
        "n_selected": len(plan["selected_ids"])}]
    _write_json(os.path.join(out, INDEX_NAME), {"version": VERSION, "plans": entries})
    return path


def load_plan(step1_dir, file_name):
    with open(os.path.join(plans_dir(step1_dir), file_name), encoding="utf-8") as f:
        plan = json.load(f)
    if not isinstance(plan, dict) or plan.get("version") != VERSION:
        raise ValueError(f"{file_name} is not a plan this version can read")
    return plan


def resolve_patches(plan, current_ids):
    """(the plan's ticked ids that exist now, how many of them do not)."""
    live = set(current_ids)
    wanted = list(plan.get("selected_ids") or [])
    kept = [pid for pid in wanted if pid in live]
    return kept, len(wanted) - len(kept)
