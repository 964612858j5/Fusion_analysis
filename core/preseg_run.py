"""One pre-segmentation run on disk: its frozen snapshot, tasks and records.

No Qt, no engines. Layout (plan 7.3):

    <step1_dir>/presegmentation_runs/<run_id>/
        run.json                                  frozen at Run (plan 7.4)
        records/<combo_id>__<y0_y1_x0_x1>.json    one per task, published last
        masks/<combo_id>__<y0_y1_x0_x1>.cell.npy  uint32, the patch itself
        masks/<combo_id>__<y0_y1_x0_x1>.nucleus.npy

A record exists only once every mask it names is complete (masks first, the
record last, each by an atomic replace). Old runs are never deleted here.
"""

import json
import os
import secrets
from datetime import datetime

from . import config_hash
from ..seg_runner import protocol
from ..utils import segmentation_param_schema as ps

RUNS_DIRNAME = "presegmentation_runs"
RECORD_SCHEMA = 1

OK, FAILED, CANCELLED = protocol.OK, protocol.FAILED, protocol.CANCELLED
NOT_PRODUCED = "not_produced"


# ── identity ────────────────────────────────────────────────────────────────
def new_run_id(now=None):
    now = now or datetime.now()
    return f"{now.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}"


def bbox_key(bbox):
    return "_".join(str(int(v)) for v in bbox)


def pixel_key(identity):
    """Whether the PIXELS a result was computed from are still the ones
    there (plan 7.4 S1). `identity` is the structured dict of `pixel_identity`;
    patch lists, P numbers and publication times are not in it."""
    return config_hash.config_hash(identity)


def pixel_identity(manifest, roi, channels, products):
    """The structured identity behind `pixel_key` (plan 7.4).

    `manifest` is the Step0 handoff manifest; `roi` the analysis ROI
    ({bbox_fullres, polygon_fullres}) or None for a whole slide; `channels`
    the slide's channel names; `products` {channel: product meta or None} for
    the corrected arrays (`product_meta`).
    """
    manifest = manifest or {}
    decisions = {str(k): str(v).strip().lower()
                 for k, v in (manifest.get("corrected_decisions") or {}).items()}
    src = manifest.get("source_identity") or {}
    names = sorted(set(channels or []) | set(decisions))
    return {
        "raw": {"dataset_path": src.get("dataset_path"),
                "dataset_fingerprint": src.get("dataset_fingerprint")},
        "roi": {"bbox_fullres": [int(v) for v in (roi or {}).get("bbox_fullres") or []] or None,
                "polygon_fullres": [[float(a) for a in p] for p in
                                    ((roi or {}).get("polygon_fullres") or [])] or None},
        "remap": str(manifest.get("channel_remap_config_hash") or ""),
        "channels": {ch: {"decision": decisions.get(ch, "original"),
                          "product": (products or {}).get(ch) if ch in decisions else None}
                     for ch in names},
        "handoff_schema_version": manifest.get("handoff_schema_version"),
    }


def product_meta(array):
    """A corrected array's own identity (the fields of the viewer's product
    token, structured): shape, dtype and its attrs. None when absent."""
    if array is None:
        return None
    attrs = dict(getattr(array, "attrs", {}) or {})
    return {"shape": [int(v) for v in getattr(array, "shape", ())],
            "dtype": str(getattr(array, "dtype", "")),
            "correction_method": attrs.get("correction_method", ""),
            "roi_name": attrs.get("roi_name", ""),
            "source_identity": attrs.get("source_identity", ""),
            "written_at": attrs.get("written_at", "")}


# ── tasks ───────────────────────────────────────────────────────────────────
def build_tasks(run_id, methods, patches):
    """Every (combination x patch). `methods` = [{method, values}] (the
    Methods blocks), `patches` = [{id, name, bbox}]. A combination that two
    blocks both list is ONE task per patch: the same method and parameters on
    the same pixels, and one file name (kept in the order first listed)."""
    combos, seen = [], set()
    for entry in methods:
        method = entry["method"]
        for params in ps.combinations(method, entry["values"]):
            cid = ps.combo_id(method, params)
            if cid in seen:
                continue
            seen.add(cid)
            combos.append({"combo_id": cid, "method": method, "params": params})
    tasks = []
    for combo in combos:
        for p in patches:
            bbox = [int(v) for v in p["bbox"]]
            tasks.append({
                "task_id": f"{combo['combo_id']}__{bbox_key(bbox)}",
                "run_id": run_id, "combo_id": combo["combo_id"],
                "method": combo["method"], "params": dict(combo["params"]),
                "patch_bbox": bbox, "patch_id": p.get("id"),
                "patch_label": str(p.get("name") or ""),
            })
    return combos, tasks


# ── the run directory ───────────────────────────────────────────────────────
def runs_dir(step1_dir):
    return os.path.join(step1_dir, RUNS_DIRNAME)


def run_dir(step1_dir, run_id):
    return os.path.join(runs_dir(step1_dir), run_id)


def write_run(step1_dir, run):
    """Create the run directory and publish run.json (the frozen snapshot)."""
    d = run_dir(step1_dir, run["run_id"])
    for sub in ("records", "masks"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    protocol.publish_json(os.path.join(d, "run.json"), run)
    return d


def record_path(rdir, task_id):
    return os.path.join(rdir, "records", f"{task_id}.json")


def mask_path(rdir, task_id, kind):
    return os.path.join(rdir, "masks", f"{task_id}.{kind}.npy")


def publish_result(rdir, run, task, status, masks=None, error="", device="", runtime_s=None,
                   paired=None):
    """Masks first, then the record; returns the record. `masks` =
    {"cell": array|None, "nucleus": array|None} for an ok task."""
    import numpy as np
    outputs = {}
    for kind in ("cell", "nucleus"):
        arr = (masks or {}).get(kind)
        if status != OK or arr is None:
            outputs[kind] = {"status": NOT_PRODUCED} if status == OK else {"status": status}
            continue
        path = mask_path(rdir, task["task_id"], kind)
        protocol.publish_array(path, np.asarray(arr, dtype=np.uint32))
        outputs[kind] = {"status": OK, "path": path,
                         "count": int((np.unique(arr) > 0).sum())}
    record = {
        "schema": RECORD_SCHEMA, "run_id": run["run_id"], "task_id": task["task_id"],
        "combo_id": task["combo_id"], "method": task["method"], "params": task["params"],
        "patch_bbox": task["patch_bbox"], "patch_id": task.get("patch_id"),
        "patch_label": task.get("patch_label", ""),
        "source": run.get("source") or {},
        "fusion_settings_hash": (run.get("fusion") or {}).get("hash"),
        "halo_px": run.get("halo_px"),
        "engine_identity": (run.get("engines") or {}).get(ps_engine(task["method"])),
        "status": status, "error": str(error or "")[-4000:],
        "cell": outputs["cell"], "nucleus": outputs["nucleus"],
        "device": device, "runtime_s": runtime_s,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    if paired is not None:
        record["paired"] = bool(paired)
    protocol.publish_json(record_path(rdir, task["task_id"]), record)
    return record


def ps_engine(method):
    return method.split("_", 1)[0]


def load_records(rdir):
    out = {}
    d = os.path.join(rdir, "records")
    for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, name), encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        out[rec.get("task_id")] = rec
    return out


# ── what may be chosen (plan 7.7) ───────────────────────────────────────────
def is_stale(run, current_pixel_key, current_fusion_hash):
    """Shown, but never chosen: the pixels or the fusion settings changed."""
    src = run.get("source") or {}
    return (src.get("pixel_key") != current_pixel_key
            or (run.get("fusion") or {}).get("hash") != current_fusion_hash)


def combo_status(run, records, combo_id):
    """{total, ok, failed, cancelled, pending, cells, complete} of one
    combination over the run's FROZEN patch set."""
    tids = [t["task_id"] for t in run.get("tasks") or [] if t["combo_id"] == combo_id]
    st = {"total": len(tids), OK: 0, FAILED: 0, CANCELLED: 0, "pending": 0, "cells": 0,
          "files_ok": True}
    for tid in tids:
        rec = records.get(tid)
        if rec is None:
            st["pending"] += 1
            continue
        st[rec.get("status")] = st.get(rec.get("status"), 0) + 1
        if rec.get("status") == OK:
            for kind in ("cell", "nucleus"):
                out = rec.get(kind) or {}
                if out.get("status") == OK:
                    if not out.get("path") or not os.path.isfile(out["path"]):
                        st["files_ok"] = False
            main = rec.get("cell") if (rec.get("cell") or {}).get("status") == OK else rec.get("nucleus")
            st["cells"] += int((main or {}).get("count") or 0)
    st["complete"] = st["pending"] == 0
    return st


def selectable(run, records, combo_id, current_pixel_key, current_fusion_hash):
    """(may be chosen, why not / what to warn about). The five conditions of
    plan 7.7 (E1): all tasks ended and none cancelled; every file there; at
    least one ok; not stale. Failures do not block: the caller asks."""
    st = combo_status(run, records, combo_id)
    if not st["complete"]:
        return False, f"{st['pending']} of {st['total']} patches are not finished"
    if st[CANCELLED]:
        return False, f"{st[CANCELLED]} of {st['total']} patches were cancelled"
    if not st["files_ok"]:
        return False, "a result file is missing"
    if not st[OK]:
        return False, "no patch succeeded"
    if is_stale(run, current_pixel_key, current_fusion_hash):
        return False, "the image or the Fusion settings changed since this run"
    return True, (f"{st[FAILED]} of {st['total']} patches failed" if st[FAILED] else "")
