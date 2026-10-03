"""Runs: one button click's folder, its parameters and the run it used (block RM).

docs/v16_run_model_application.md §2-§8. Layout of one ROI workspace:

    rois/<roi_id>/
        roi_manifest.json           the workspace's identity (A3 finds it)
        session.json                what was being viewed and edited (§6)
        settings/                   saved configuration, mutable
            step0/                  Step0 handoff + correction_config + remap
            step1_fusion_settings.json, segmentation_params/
        runs/
            correct_<stamp>/        params.json · inputs.json · products · .done
            preseg_<stamp>/
            fuse_<stamp>/

A run EXISTS only when its ``.done`` is written, and ``.done`` is written
last. After that its scientific products are never written again. A folder
without ``.done`` is a run that never finished; `cleanup_incomplete` removes
it when a workspace is opened -- the caller makes sure no job of this
program is writing one at that moment.

Every run names exactly one PIXEL upstream in ``inputs.json``: a correct run
names the raw slide, every other run names another run. Records hold
paths relative to the project folder, so a project can be copied or
renamed as a whole. Two runs are "the same" when their upstream and their
``reuse_key`` (the fields that change the computation) are equal, compared
field by field (§8; no hash).
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from typing import Dict, List, Optional

RUNS_DIR = "runs"
SETTINGS_DIR = "settings"
SESSION = "session.json"
PARAMS = "params.json"
INPUTS = "inputs.json"
DONE = ".done"
PROJECT_MANIFEST = "project_manifest.json"
RAW = "raw"

KINDS = ("correct", "preseg", "fuse", "segment", "quant")


# ── json ─────────────────────────────────────────────────────────────────

def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json_atomic(path, payload):
    """Write `payload` to `path` through a temporary file and one rename."""
    folder = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, f".{os.path.basename(path)}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ── paths ────────────────────────────────────────────────────────────────

def project_dir_of(path) -> Optional[str]:
    """The project folder (holding project_manifest.json) at or above `path`."""
    if not path:
        return None
    cur = os.path.abspath(path)
    while True:
        if os.path.isfile(os.path.join(cur, PROJECT_MANIFEST)):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def rel(project_dir, path) -> str:
    """`path` relative to the project, with '/' separators.

    Raises ValueError for a path outside the project: a record must never
    name another project's file."""
    project = os.path.realpath(project_dir)
    full = os.path.realpath(path)
    if full != project and not full.startswith(project + os.sep):
        raise ValueError(f"{path} is not inside the project {project_dir}")
    return os.path.relpath(full, project).replace(os.sep, "/")


def resolve(project_dir, rel_path) -> str:
    """The absolute path of a project-relative record ('' stays '')."""
    if not rel_path:
        return ""
    if os.path.isabs(rel_path):
        return os.path.abspath(rel_path)
    return os.path.abspath(os.path.join(project_dir, *str(rel_path).split("/")))


def runs_dir(roi_dir) -> str:
    return os.path.join(os.path.abspath(roi_dir), RUNS_DIR)


def settings_dir(roi_dir) -> str:
    return os.path.join(os.path.abspath(roi_dir), SETTINGS_DIR)


def session_path(roi_dir) -> str:
    return os.path.join(os.path.abspath(roi_dir), SESSION)


def roi_dir_of(run_dir) -> str:
    """The workspace a run folder belongs to (``rois/<id>/runs/<run>``)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(run_dir)))


def kind_of(run_dir) -> str:
    name = os.path.basename(os.path.normpath(run_dir))
    kind = name.split("_", 1)[0]
    return kind if kind in KINDS else ""


# ── one run ──────────────────────────────────────────────────────────────

def new_run(roi_dir, kind, suffix="", now=None) -> str:
    """Create and return a new, unpublished run folder.

    Named ``<kind>_<YYYYmmdd_HHMMSS>[_<suffix>]``; a second run in the same
    second gets ``-2``, ``-3`` … after the stamp."""
    if kind not in KINDS:
        raise ValueError(f"unknown run kind {kind!r}")
    root = runs_dir(roi_dir)
    os.makedirs(root, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    tail = f"_{suffix}" if suffix else ""
    n = 1
    while True:
        name = f"{kind}_{stamp}{'' if n == 1 else f'-{n}'}{tail}"
        path = os.path.join(root, name)
        try:
            os.makedirs(path, exist_ok=False)
            break
        except FileExistsError:
            n += 1
    return path


def write_params(run_dir, params: Dict):
    write_json_atomic(os.path.join(run_dir, PARAMS), params)


def read_params(run_dir) -> Dict:
    data = _load(os.path.join(run_dir, PARAMS))
    return data if isinstance(data, dict) else {}


def write_inputs(run_dir, upstream=None, slide_id=""):
    """``inputs.json``: the one pixel upstream, as a project-relative path,
    or ``raw`` plus the slide id for a correct run."""
    if upstream is None:
        payload = {"upstream": RAW, "slide_id": str(slide_id or "")}
    else:
        project = project_dir_of(run_dir)
        if project is None:
            raise ValueError(f"{run_dir} is not inside a project")
        payload = {"upstream": rel(project, upstream)}
    write_json_atomic(os.path.join(run_dir, INPUTS), payload)


def read_inputs(run_dir) -> Dict:
    data = _load(os.path.join(run_dir, INPUTS))
    return data if isinstance(data, dict) else {}


def upstream_of(run_dir) -> str:
    """The absolute path of the run's upstream run, ``raw``, or ''."""
    up = str(read_inputs(run_dir).get("upstream") or "")
    if not up or up == RAW:
        return up
    project = project_dir_of(run_dir)
    return resolve(project, up) if project else ""


def publish(run_dir):
    """Write ``.done``: from now on the run exists and is read-only."""
    write_json_atomic(os.path.join(run_dir, DONE),
                      {"done_at": datetime.now().isoformat(timespec="seconds")})


def is_done(run_dir) -> bool:
    return bool(run_dir) and os.path.isfile(os.path.join(run_dir, DONE))


def discard(run_dir):
    """Remove a run that was never published (a cancelled or failed job)."""
    if not run_dir or is_done(run_dir):
        return
    shutil.rmtree(run_dir, ignore_errors=True)


# ── many runs ────────────────────────────────────────────────────────────

def list_runs(roi_dir, kind=None, upstream=None) -> List[str]:
    """Published runs of the workspace, oldest first; optionally only one
    kind, and only those whose upstream is `upstream`."""
    root = runs_dir(roi_dir)
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    want_up = os.path.abspath(upstream) if upstream else None
    out = []
    for name in names:
        path = os.path.join(root, name)
        if not os.path.isdir(path) or not is_done(path):
            continue
        if kind and kind_of(path) != kind:
            continue
        if want_up is not None and upstream_of(path) != want_up:
            continue
        out.append(path)
    return out


def latest_run(roi_dir, kind, upstream=None) -> str:
    runs = list_runs(roi_dir, kind, upstream)
    return runs[-1] if runs else ""


def chain(run_dir) -> List[str]:
    """`run_dir` and every run above it, nearest first; stops at raw."""
    out = []
    cur = os.path.abspath(run_dir) if run_dir else ""
    while cur and cur != RAW and cur not in out:
        out.append(cur)
        cur = upstream_of(cur)
    return out


def correct_run_of(run_dir) -> str:
    for r in chain(run_dir):
        if kind_of(r) == "correct":
            return r
    return ""


def downstream_of(run_dir) -> List[str]:
    """Every published run whose chain contains `run_dir` (itself excluded),
    furthest downstream first -- the order a deletion must take."""
    target = os.path.abspath(run_dir)
    found = [r for r in list_runs(roi_dir_of(run_dir)) if r != target and target in chain(r)]
    return sorted(found, key=lambda r: -len(chain(r)))


def find_same(roi_dir, kind, upstream, reuse_key) -> str:
    """The newest published run of `kind` on `upstream` whose reuse_key
    equals `reuse_key` field by field, or ''."""
    for r in reversed(list_runs(roi_dir, kind, upstream)):
        if read_params(r).get("reuse_key") == reuse_key:
            return r
    return ""


def cleanup_incomplete(roi_dir) -> List[str]:
    """Remove run folders without ``.done`` (never finished). Only call it
    when no job is writing a run of this workspace. Returns the removed
    paths."""
    root = runs_dir(roi_dir)
    try:
        names = os.listdir(root)
    except OSError:
        return []
    removed = []
    for name in names:
        path = os.path.abspath(os.path.join(root, name))
        if os.path.isdir(path) and not is_done(path):
            shutil.rmtree(path, ignore_errors=True)
            removed.append(path)
    return removed


def display_name(run_dir) -> str:
    """``10-03 12:00 · <summary>``, made from the run's own records."""
    params = read_params(run_dir)
    name = os.path.basename(os.path.normpath(run_dir))
    parts = name.split("_")
    when = ""
    if len(parts) >= 3 and len(parts[1]) == 8 and len(parts[2]) >= 6:
        d, t = parts[1], parts[2]
        when = f"{d[4:6]}-{d[6:8]} {t[0:2]}:{t[2:4]}"
    summary = str(params.get("summary") or "")
    return " · ".join(p for p in (when, summary) if p) or name


# ── session ──────────────────────────────────────────────────────────────

def load_session(roi_dir) -> Dict:
    data = _load(session_path(roi_dir))
    return data if isinstance(data, dict) else {}


def save_session(roi_dir, payload: Dict):
    write_json_atomic(session_path(roi_dir), payload)


def put_draft(sess: Dict, step: str, draft: Dict) -> Dict:
    """`sess` with `draft` as ``drafts[step]``. A draft already there that
    was edited against ANOTHER context is not overwritten: it joins the list
    ``drafts[step + "_kept"]``, one entry per context, the newest kept
    (application §6: not restored, not dropped)."""
    drafts = dict(sess.get("drafts") or {})
    old = drafts.get(step)
    if isinstance(old, dict) and old.get("edited_against") != draft.get("edited_against"):
        kept = [k for k in (drafts.get(step + "_kept") or [])
                if isinstance(k, dict) and k.get("edited_against") != old.get("edited_against")]
        drafts[step + "_kept"] = kept + [old]
    drafts[step] = draft
    return dict(sess, drafts=drafts)


def update_session(roi_dir, **fields) -> Dict:
    """Merge top-level `fields` into session.json and write it."""
    sess = load_session(roi_dir)
    sess.update(fields)
    save_session(roi_dir, sess)
    return sess


# ── records with paths ───────────────────────────────────────────────────

# Path fields whose names do not end in _path / _dir.
_PATH_KEYS = ("ome_tiff", "background_correction_source", "hq_source_zarr")


def _is_path_key(key) -> bool:
    key = str(key)
    return key.endswith("_path") or key.endswith("_dir") or key in _PATH_KEYS


def to_records(obj, project_dir):
    """A copy of `obj` (nested dicts and lists) whose path fields -- keys ending in
    ``_path`` or ``_dir`` -- are project-relative when they lie inside the
    project. Other values, and paths outside the project, are unchanged."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _is_path_key(k) and isinstance(v, str) and v and os.path.isabs(v) \
                    and project_dir:
                try:
                    out[k] = rel(project_dir, v)
                    continue
                except ValueError:
                    pass
            out[k] = to_records(v, project_dir)
        return out
    if isinstance(obj, list):
        return [to_records(v, project_dir) for v in obj]
    return obj


def from_records(obj, project_dir):
    """The inverse of `to_records`: relative path fields become absolute."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _is_path_key(k) and isinstance(v, str) and v and not os.path.isabs(v) \
                    and project_dir:
                out[k] = resolve(project_dir, v)
            else:
                out[k] = from_records(v, project_dir)
        return out
    if isinstance(obj, list):
        return [from_records(v, project_dir) for v in obj]
    return obj
