"""Block PA-2: a finished, unchanged pre-segmentation result is kept, not run
again (application v1.2 §2; rulings: copy into the new run, look only at the
current run, record where it came from)."""

import os

import numpy as np

from block01.core import preseg_run
from block01.seg_runner import engines as seg_engines

METHOD = "stardist_nuclei_dapi"
IDENT = {"raw": {"dataset_path": "/x.ome.tif", "dataset_fingerprint": "1:2"},
         "roi": {"bbox_fullres": None, "polygon_fullres": None}, "remap": "rh",
         "channels": {"DAPI": {"decision": "original", "product": None}},
         "handoff_schema_version": 2}


def _task(bbox=(0, 64, 0, 64), params=None, run_id="new"):
    params = {"prob_thresh": 0.5} if params is None else params
    cid = f"{METHOD}__p{params['prob_thresh']}"
    return {"task_id": f"{cid}__{preseg_run.bbox_key(bbox)}", "run_id": run_id,
            "combo_id": cid, "method": METHOD, "params": dict(params),
            "patch_bbox": list(bbox), "patch_id": 7, "patch_label": "P7"}


def _old_run(tmp_path, status="ok", ident=IDENT, fusion="fh", halo=24, engine=None):
    rdir = str(tmp_path / "preseg_old")
    run = {"run_id": "preseg_old", "source": {"pixel_key": "pk", "pixel_identity": ident},
           "fusion": {"hash": fusion}, "halo_px": halo,
           "engines": {"stardist": engine or seg_engines.behavior_identity("stardist")}}
    os.makedirs(os.path.join(rdir, "records"))
    os.makedirs(os.path.join(rdir, "masks"))
    t = _task(run_id="preseg_old")
    masks = {"nucleus": np.arange(16, dtype=np.uint32).reshape(4, 4)} if status == "ok" else None
    rec = preseg_run.publish_result(rdir, run, t, status, masks=masks, device="cuda",
                                    runtime_s=1.5)
    return rdir, rec


def _ok(rec, task=None, **kw):
    args = dict(pixel_identity=IDENT, fusion_hash="fh", halo_px=24)
    args.update(kw)
    return preseg_run.reusable(rec, task or _task(), **args)


def test_an_unchanged_finished_result_is_reusable(tmp_path):
    _, rec = _old_run(tmp_path)
    assert _ok(rec)


def test_any_change_makes_it_run_again(tmp_path):
    _, rec = _old_run(tmp_path)
    assert not _ok(rec, _task(bbox=(0, 64, 0, 65)))                 # another patch
    assert not _ok(rec, _task(params={"prob_thresh": 0.6}))          # other parameters
    assert not _ok(rec, pixel_identity=dict(IDENT, remap="other"))   # other pixels
    assert not _ok(rec, fusion_hash="fh2")                           # other fusion settings
    assert not _ok(rec, halo_px=32)                                  # another halo
    assert not _ok(None)


def test_another_engine_makes_it_run_again(tmp_path):
    _, rec = _old_run(tmp_path, engine=dict(seg_engines.behavior_identity("stardist"),
                                            behavior_version=0))
    assert not _ok(rec)


def test_a_failed_or_cancelled_task_or_a_missing_file_runs_again(tmp_path):
    for status in ("failed", "cancelled"):
        _, rec = _old_run(tmp_path / status, status=status)
        assert not _ok(rec)
    _, rec = _old_run(tmp_path / "gone")
    os.remove(rec["nucleus"]["path"])
    assert not _ok(rec)


def test_a_reused_result_is_copied_into_the_new_run_and_says_where_from(tmp_path):
    old_dir, rec = _old_run(tmp_path)
    new_dir = str(tmp_path / "preseg_new")
    os.makedirs(new_dir)
    task = dict(_task(), patch_id=9, patch_label="renamed")
    new = preseg_run.copy_reused(new_dir, {"run_id": "preseg_new"}, task, rec)
    assert new["run_id"] == "preseg_new" and new["task_id"] == task["task_id"]
    assert new["reused_from_run"] == "preseg_old" and new["reused_from_task"] == rec["task_id"]
    for key in ("created_at", "engine_identity", "device", "runtime_s", "source",
                "fusion_settings_hash", "halo_px", "params", "status"):
        assert new[key] == rec[key], key
    assert new["patch_id"] == 9 and new["patch_label"] == "renamed"
    path = new["nucleus"]["path"]
    assert path.startswith(new_dir) and os.path.isfile(path)
    assert np.array_equal(np.load(path), np.load(rec["nucleus"]["path"]))
    assert new["cell"] == {"status": "not_produced"}
    assert preseg_run.load_records(new_dir)[task["task_id"]] == new
    os.remove(rec["nucleus"]["path"])                    # the old run can go
    assert os.path.isfile(path)
