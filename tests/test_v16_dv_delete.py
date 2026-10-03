"""Block DV-D: deleting from Step0's chooser into a 30-day trash
(docs/v16_DV_delete_application.md).

Part 1 -- the records and the trash (`utils/data_versions.py`,
`utils/trash.py`): what a deletion moves, what it leaves byte-for-byte alone,
the records it drops, recovery from a deletion interrupted half-way, the
30-day purge and Empty trash, the A3 `deletion` entry.

Synthetic projects in the test's temporary directory only.
"""

import json
import os
import shutil
from datetime import datetime, timedelta

import pytest

from block01.utils import data_versions as dv
from block01.utils import trash


def _tree(path):
    out = []
    for base, _dirs, files in os.walk(path):
        for name in files:
            p = os.path.join(base, name)
            with open(p, "rb") as f:
                out.append((os.path.relpath(p, path), f.read()))
    return sorted(out)


def _project(tmp_path):
    from block01.utils.roi_project import ensure_project_manifest
    proj = tmp_path / "proj"
    ws = proj / "rois" / "ws1"
    (ws / "step0").mkdir(parents=True)
    (ws / "roi_manifest.json").write_text(json.dumps({"roi_id": "ws1"}))
    ensure_project_manifest(str(proj))
    with open(proj / "roi_index.json", "w") as f:
        json.dump({"version": 1, "active_roi_id": "ws1",
                   "rois": [{"roi_id": "ws1"}, {"roi_id": "ws0"}]}, f)
    return str(proj), str(ws)


def _version(ws, corrected=None, n_regions=1):
    alloc = dv.new_version_folder(ws)
    if corrected is None:
        corrected = dv.new_corrected_folder(ws)
        os.makedirs(corrected)
        with open(os.path.join(corrected, "px.bin"), "wb") as f:
            f.write(os.urandom(64))
    regions = []
    for i in range(n_regions):
        z = os.path.join(alloc["path"], f"fused_R{i}.zarr")
        os.makedirs(z)
        regions.append({"roi_name": f"R{i}", "roi_id": f"r{i}", "bbox_fullres": [0, 5, 0, 5],
                        "polygon_fullres": None, "fused_zarr_path": z})
    return dv.commit_version(ws, alloc, {"corrected": {"path": corrected}, "regions": regions})


def _run(ws, version, name):
    run = os.path.join(ws, "step2", "segmentation_runs", name)
    os.makedirs(run)
    with open(os.path.join(run, "segmentation_meta.json"), "w") as f:
        json.dump({"run_id": name, "method": "stardist", "data_version": version,
                   "created_at": name}, f)
    quant = os.path.join(ws, "step4", "quantification_runs", name, "Full WSI")
    os.makedirs(quant)
    with open(os.path.join(quant, "cells.h5ad"), "wb") as f:
        f.write(b"x")
    index_path = os.path.join(ws, "roi_index.json")
    index = json.load(open(index_path)) if os.path.exists(index_path) else {}
    index.setdefault("segmentation_runs", {})[name] = {
        "run_id": name, "status": "done", "path": f"step2/segmentation_runs/{name}"}
    index["active_segmentation_run"] = name
    index.setdefault("latest_by_method", {})["stardist"] = name
    json.dump(index, open(index_path, "w"))
    return run


def _handoff(ws, corrected):
    with open(os.path.join(ws, "step0", "step0_roi_result.json"), "w") as f:
        json.dump({"corrected_zarr_path": corrected}, f)


# ── runs of a version ────────────────────────────────────────────────────

def test_the_runs_of_a_version_are_found(tmp_path):
    _proj, ws = _project(tmp_path)
    _version(ws)
    _version(ws)
    a = _run(ws, "v001", "seg_20261003_100000_a")
    b = _run(ws, "v001", "seg_20261003_110000_b")
    _run(ws, "v002", "seg_20261003_120000_c")
    assert [r.run_dir for r in dv.runs_of_version(ws, "v001")] == [
        os.path.realpath(b), os.path.realpath(a)]


# ── one segmentation run (the combination) ───────────────────────────────

def test_deleting_one_run_moves_only_it_and_its_step4_results(tmp_path):
    proj, ws = _project(tmp_path)
    v1 = _version(ws)
    _handoff(ws, v1["corrected"]["path"])
    a = _run(ws, "v001", "seg_a")
    b = _run(ws, "v001", "seg_b")
    keep = _tree(dv.versions_dir(ws)), _tree(b)
    plan = dv.plan_delete(ws, run_dir=os.path.realpath(a))
    entry = dv.execute_delete(plan)
    assert not os.path.exists(a)
    assert not os.path.exists(os.path.join(ws, "step4", "quantification_runs", "seg_a"))
    assert os.path.isdir(os.path.join(entry["folder"], "rois", "ws1", "step2",
                                      "segmentation_runs", "seg_a"))
    assert (_tree(dv.versions_dir(ws)), _tree(b)) == keep     # version + other run untouched
    index = json.load(open(os.path.join(ws, "roi_index.json")))
    assert "seg_a" not in index["segmentation_runs"] and "seg_b" in index["segmentation_runs"]
    assert dv.get_version(ws, "v001") is not None
    assert entry["status"] == "complete"


def test_deleting_the_last_run_with_its_version(tmp_path):
    proj, ws = _project(tmp_path)
    v1 = _version(ws)
    v2 = _version(ws)
    _handoff(ws, v2["corrected"]["path"])
    a = _run(ws, "v001", "seg_a")
    plan = dv.plan_delete(ws, run_dir=os.path.realpath(a), version_id="v001")
    dv.execute_delete(plan)
    assert dv.get_version(ws, "v001") is None and dv.current_version(ws)["version"] == "v002"
    assert not os.path.exists(dv.version_dir(ws, v1["folder"]))
    assert not os.path.exists(v1["corrected"]["path"])       # only v1 used it
    assert os.path.isdir(v2["corrected"]["path"])


def test_a_corrected_product_another_version_uses_stays(tmp_path):
    proj, ws = _project(tmp_path)
    v1 = _version(ws)
    v2 = _version(ws, corrected=v1["corrected"]["path"])      # shared (§3.4)
    _handoff(ws, v2["corrected"]["path"])
    before = _tree(os.path.dirname(v1["corrected"]["path"]))
    dv.execute_delete(dv.plan_delete(ws, version_id="v001"))
    assert _tree(os.path.dirname(v1["corrected"]["path"])) == before


def test_the_current_versions_corrected_stays_unless_the_handoff_is_released(tmp_path):
    proj, ws = _project(tmp_path)
    v1 = _version(ws)
    _handoff(ws, v1["corrected"]["path"])
    keep = dv.plan_delete(ws, version_id="v001")
    assert dv._corrected_folder(v1["corrected"]["path"]) not in keep["paths"]
    released = dv.plan_delete(ws, version_id="v001", release_handoff=True)
    assert dv._corrected_folder(v1["corrected"]["path"]) in released["paths"]
    dv.execute_delete(released)
    assert dv.list_versions(ws) == [] and dv.load_index(ws)["current"] == ""


def test_a_legacy_v1s_products_in_place_go_with_it(tmp_path):
    proj, ws = _project(tmp_path)
    corrected = os.path.join(ws, "step0", "corrected_channels.zarr")
    fused = os.path.join(ws, "step1", "fused_Full WSI.zarr")
    os.makedirs(corrected)
    os.makedirs(fused)
    alloc = dv.new_version_folder(ws)
    dv.commit_version(ws, alloc, {"corrected": {"path": corrected}, "legacy": True,
                                  "regions": [{"roi_name": "Full WSI", "fused_zarr_path": fused}]})
    _handoff(ws, os.path.join(ws, "versions", "corrected", "c001", "corrected_channels.zarr"))
    dv.execute_delete(dv.plan_delete(ws, version_id="v001"))
    assert not os.path.exists(corrected) and not os.path.exists(fused)


# ── the draft and the workspace ──────────────────────────────────────────

def test_discarding_the_draft_moves_only_its_own_corrected(tmp_path):
    proj, ws = _project(tmp_path)
    v1 = _version(ws)
    draft = dv.new_corrected_folder(ws)
    os.makedirs(draft)
    _handoff(ws, draft)
    plan = dv.plan_delete(ws, draft=True)
    assert plan["paths"] == [os.path.dirname(draft)]
    dv.execute_delete(plan)
    assert not os.path.exists(draft) and os.path.isdir(v1["corrected"]["path"])
    # a handoff naming a version's product is no draft product
    _handoff(ws, v1["corrected"]["path"])
    assert dv.plan_delete(ws, draft=True)["paths"] == []


def test_deleting_the_workspace_moves_it_whole_and_drops_it_from_the_project(tmp_path):
    proj, ws = _project(tmp_path)
    _version(ws)
    dv.execute_delete(dv.plan_delete(ws, workspace=True))
    assert not os.path.exists(ws)
    index = json.load(open(os.path.join(proj, "roi_index.json")))
    assert [r["roi_id"] for r in index["rois"]] == ["ws0"] and index["active_roi_id"] == ""


# ── interrupted, purge, empty, A3 ────────────────────────────────────────

def test_an_interrupted_deletion_is_finished_records_first(tmp_path, monkeypatch):
    proj, ws = _project(tmp_path)
    _version(ws)
    v2 = _version(ws)
    _handoff(ws, v2["corrected"]["path"])
    plan = dv.plan_delete(ws, version_id="v001")
    entry = trash.begin(proj, "ws1", plan["what"], plan["paths"],
                        {k: plan[k] for k in ("run_dir", "version", "draft", "delete_workspace")})
    # ... the program stopped here: nothing dropped, nothing moved
    assert dv.get_version(ws, "v001") is not None
    done = dv.resume_deletions(proj)
    assert done == [entry["folder"]]
    assert dv.get_version(ws, "v001") is None
    assert all(not os.path.exists(p) for p in plan["paths"])
    assert trash.entries(proj)[0]["status"] == "complete"


def test_complete_entries_older_than_30_days_are_purged(tmp_path):
    proj, ws = _project(tmp_path)
    for name in ("old", "recent"):
        os.makedirs(os.path.join(ws, name))
    now = datetime(2026, 11, 10, 12, 0, 0)
    old = trash.finish(trash.begin(proj, "ws1", "old", [os.path.join(ws, "old")],
                                   now=now - timedelta(days=31)))
    recent = trash.finish(trash.begin(proj, "ws1", "recent", [os.path.join(ws, "recent")],
                                      now=now - timedelta(days=29)))
    assert trash.purge(proj, now=now) == [old["folder"]]
    assert not os.path.exists(old["folder"]) and os.path.isdir(recent["folder"])


def test_empty_trash_removes_every_complete_entry(tmp_path):
    proj, ws = _project(tmp_path)
    os.makedirs(os.path.join(ws, "a"))
    os.makedirs(os.path.join(ws, "b"))
    trash.finish(trash.begin(proj, "ws1", "a", [os.path.join(ws, "a")]))
    pending = trash.begin(proj, "ws1", "b", [os.path.join(ws, "b")])
    assert trash.size(proj) > 0
    trash.empty(proj)
    assert [e["folder"] for e in trash.entries(proj)] == [pending["folder"]]   # finished first


def test_the_trash_is_never_listed_as_a_workspace(tmp_path, monkeypatch):
    from block01.utils import workspace_session as wsess
    proj, ws = _project(tmp_path)
    _version(ws)
    dv.execute_delete(dv.plan_delete(ws, workspace=True))
    assert not os.path.isdir(os.path.join(proj, "rois", "ws1"))
    assert os.path.isdir(os.path.join(trash.trash_root(proj)))
    assert all(not trash.is_in_trash(proj, p) for p in
               [os.path.join(proj, "rois")])


def test_a_deletion_appends_an_a3_record(tmp_path):
    from block01.core import provenance as prov
    from block01.utils.roi_project import ensure_project_manifest
    proj, ws = _project(tmp_path)
    ensure_project_manifest(proj)
    _version(ws)
    v2 = _version(ws)
    _handoff(ws, v2["corrected"]["path"])
    a = _run(ws, "v001", "seg_a")
    before = len(prov.load_entries(proj))
    dv.execute_delete(dv.plan_delete(ws, run_dir=os.path.realpath(a)))
    entries = prov.load_entries(proj)
    assert len(entries) == before + 1
    rec = [e for e in entries if e["kind"] == "deletion"][-1]
    assert rec["parameters"]["data_version"] == ""          # a run only: no version deleted
    assert rec["parameters"]["run_dir"].endswith("seg_a")
