"""The pre-segmentation run without a window (plan block C, step 3).

Input construction (read window, fusion, polygon, the array per method),
the run records and what may be chosen, the pixel key, and the background
job end to end through a real StarDist engine process (it skips where
StarDist is missing). Everything is written under tmp_path.
"""
import json
import os

os.environ.setdefault("KERAS_BACKEND", "tensorflow")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from block01.core import config_hash, label_ownership, preseg_input, preseg_run
from block01.utils import segmentation_param_schema as ps


# ── the shared hash is the window's hash ────────────────────────────────────
def test_config_hash_gives_the_digests_the_window_gave_before_the_move():
    # Digests computed with MainWindow._step1_config_hash at 584fd03.
    samples = [
        ({"b": 1.23456789, "a": [1, 2.0000004, {"z": np.float32(0.1), "created_at": "x"}],
          "n": np.int64(3)}, "ba4b160d625b98d6e0299ab808ffc9c75e47abe9c60c4628df2ced39bebfc815"),
        ({"fusion_config": {"groups": {"g": {"channels": {"CD3": 0.5}}}},
          "display_mapping": {"CD3": {"min": 1, "max": 200.5, "gamma": 1.0}}},
         "910b8ce0841e0fcb38332357675ffe0200efd3cf6d98bc8caeaccb5afecc5298"),
        ([], "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"),
        ("str", "72495b0e1f3c6c961f46d3f249ce968ac758c43b88e50d32aa138681f8a38804"),
        (None, "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b"),
        ({"saved_at": 1, "k": (1, 2)},
         "f917c469ead2dde9740dc5f586b36c60e2be16cfa9c25ef90d85f9dc2a25c6f5"),
    ]
    from block01.ui.main_window import MainWindow
    for value, digest in samples:
        assert config_hash.config_hash(value) == digest
        assert MainWindow._step1_config_hash(value) == digest


# ── inputs ──────────────────────────────────────────────────────────────────
class _Loader:
    """A slide of two channels: DAPI = nuclei, CD3 = membrane, uint8 raw."""

    def __init__(self, h=300, w=320):
        from block01.seg_runner import synthetic
        self.shape = (h, w)
        self.ch_map = {"DAPI": 0, "CD3": 1}
        self.data = {"DAPI": (synthetic.nuclei(h, w) * 255).astype(np.uint8),
                     "CD3": (synthetic.membrane(h, w) * 255).astype(np.uint8)}
        self.reads = []

    def read_region(self, ch, y0, y1, x0, x1, downsample=1, normalize=False):
        self.reads.append((ch, y0, y1, x0, x1))
        return self.data[ch][y0:y1, x0:x1]


SNAP = {"hash": "fh1",
        "fusion_config": {"groups": {"T": {"channels": {"CD3": 1.0}, "group_weight": 1.0}},
                          "nucleus": {"channel": "DAPI", "weight": 1.0}},
        "display_mapping": {"DAPI": {"min": 0, "max": 255, "gamma": 1.0},
                            "CD3": {"min": 0, "max": 255, "gamma": 1.0}}}


def test_the_read_window_is_the_patch_plus_halo_cut_to_the_region():
    assert preseg_input.read_window((100, 150, 100, 150), 20, (0, 300, 0, 320)) == (80, 170, 80, 170)
    win = preseg_input.read_window((5, 50, 290, 320), 20, (0, 300, 0, 320))
    assert win == (0, 70, 270, 320)                                  # no padding
    assert preseg_input.own_local((5, 50, 290, 320), win) == (5, 50, 20, 50)
    with pytest.raises(preseg_input.InputRefused):
        preseg_input.read_window((400, 410, 0, 10), 5, (0, 300, 0, 320))


def test_the_polygon_zeroes_the_same_pixels_as_step2s_fused_zarr():
    pytest.importorskip("PyQt5")
    from block01.ui.step0.overview_panel import FullFusionWorker
    roi_bbox = (13, 263, 7, 297)
    poly = [(20.7, 15.2), (290.4, 40.9), (250.5, 260.1), (60.3, 200.8), (9.9, 120.5)]
    ry0, ry1, rx0, rx1 = roi_bbox
    step2 = FullFusionWorker._poly_mask(poly, ry0, rx0, ry1 - ry0, rx1 - rx0)
    for win in [(13, 100, 7, 120), (50, 263, 100, 297), (120, 180, 7, 60), (13, 263, 7, 297)]:
        ours = preseg_input.RoiMask(poly, roi_bbox).window(win)
        np.testing.assert_array_equal(
            ours, step2[win[0] - ry0:win[1] - ry0, win[2] - rx0:win[3] - rx0])


def test_the_fused_window_is_fuse_fullres_with_the_outside_zeroed():
    from block01.core.fusion_engine import FusionEngine
    ld = _Loader()
    win = (40, 140, 60, 180)
    poly = [(70, 45), (175, 50), (150, 135), (65, 120)]
    rm = preseg_input.RoiMask(poly, (0, 300, 0, 320))
    got = preseg_input.fused_window(ld, win, SNAP, rm)
    ref = FusionEngine().fuse_fullres(ld, *win, {"T": {"CD3": 1.0}}, {"T": 1.0}, "DAPI", 1.0,
                                      channel_remap_params=SNAP["display_mapping"])
    inside = rm.window(win)
    assert got.dtype == np.uint16 and got.shape == (100, 120, 2)
    np.testing.assert_array_equal(got[inside], ref[inside])
    assert not got[~inside].any() and inside.any() and (~inside).any()


def test_each_method_gets_the_array_of_the_input_table():
    fused = np.random.default_rng(0).integers(0, 65536, (8, 9, 2)).astype(np.uint16)
    F = fused.astype(np.float32) / 65535.0
    table = {
        "cellpose_wholecell_fusion": np.stack([F[..., 0], F[..., 0], F[..., 1]], -1),
        "cellpose_nuclei_dapi": F[..., 1], "cellpose_nuclei_expansion": F[..., 1],
        "stardist_nuclei_dapi": F[..., 1], "stardist_nuclei_expansion": F[..., 1],
        "mesmer_whole_cell": np.stack([F[..., 1], F[..., 0]], -1),
        "mesmer_nuclear_guided": np.stack([F[..., 1], F[..., 0]], -1),
        "mesmer_nuclei": np.stack([F[..., 1], np.zeros_like(F[..., 1])], -1),
    }
    assert set(table) == set(ps.UI_METHODS) == set(preseg_input.INPUT_KIND)
    for method, want in table.items():
        got = preseg_input.model_input(preseg_input.INPUT_KIND[method], fused)
        assert got.dtype == np.float32
        np.testing.assert_array_equal(got, want)


@pytest.mark.parametrize("change,why", [
    (lambda s: s["fusion_config"]["nucleus"].update(weight=0.0), "weight"),
    (lambda s: s["fusion_config"]["nucleus"].update(channel=""), "no nucleus channel"),
    (lambda s: s["display_mapping"].pop("DAPI"), "display window"),
])
def test_a_meaningless_nucleus_input_is_refused(change, why):
    snap = json.loads(json.dumps(SNAP))
    assert preseg_input.check_fusion(snap) == "DAPI"
    change(snap)
    with pytest.raises(preseg_input.InputRefused, match=why):
        preseg_input.check_fusion(snap)


# ── tasks, the pixel key, what may be chosen ────────────────────────────────
def _vals(method, **kw):
    v = ps.default_values(method)
    v.update(kw)
    return v


def test_tasks_are_every_combination_times_every_patch_once():
    methods = [{"method": "stardist_nuclei_dapi", "values": _vals("stardist_nuclei_dapi",
                                                                   prob_thresh=[0.4, 0.6])},
               {"method": "stardist_nuclei_dapi", "values": _vals("stardist_nuclei_dapi",
                                                                   prob_thresh=[0.6, 0.7])}]
    patches = [{"id": 1, "name": "P1", "bbox": [0, 64, 0, 64]},
               {"id": 3, "name": "edge", "bbox": [64, 128, 0, 64]}]
    combos, tasks = preseg_run.build_tasks("r1", methods, patches)
    assert [c["params"]["prob_thresh"] for c in combos] == [0.4, 0.6, 0.7]   # 0.6 once
    assert len(tasks) == 6 and len({t["task_id"] for t in tasks}) == 6
    assert tasks[1]["patch_label"] == "edge" and tasks[1]["patch_bbox"] == [64, 128, 0, 64]
    assert tasks[0]["task_id"] == f"{combos[0]['combo_id']}__0_64_0_64"


def _manifest():
    return {"source_identity": {"dataset_path": "/x.ome.tif", "dataset_fingerprint": "1:2"},
            "channel_remap_config_hash": "rh", "handoff_schema_version": 2,
            "corrected_decisions": {"CD3": "tophat"},
            "n_patches": 3, "geometry_revision": 4, "patch_config_path": "/p.json",
            "created_at": "t0", "next_patch_id": 5}


ROI = {"bbox_fullres": [0, 300, 0, 320], "polygon_fullres": [[1, 1], [300, 2], [200, 290]]}
PROD = {"CD3": {"shape": [300, 320], "dtype": "uint16", "correction_method": "tophat",
                "roi_name": "ROI_1", "source_identity": "s", "written_at": "w1"}}


def _key(manifest=None, roi=None, products=None):
    return preseg_run.pixel_key(preseg_run.pixel_identity(
        manifest or _manifest(), ROI if roi is None else roi, ["DAPI", "CD3"],
        PROD if products is None else products))


def test_the_pixel_key_follows_the_pixels_and_not_the_patch_list():
    base = _key()
    changed = []
    m = _manifest()
    m["corrected_decisions"] = {}
    changed.append(_key(manifest=m))                                      # a decision
    changed.append(_key(products={"CD3": dict(PROD["CD3"], written_at="w2")}))  # regenerated
    changed.append(_key(roi=dict(ROI, polygon_fullres=[[1, 1], [300, 2], [201, 290]])))
    m = _manifest()
    m["channel_remap_config_hash"] = "rh2"
    changed.append(_key(manifest=m))                                      # remap
    assert all(k != base for k in changed)
    for field, value in (("n_patches", 9), ("geometry_revision", 5), ("patch_config_path", "/q"),
                         ("created_at", "t1"), ("next_patch_id", 9)):
        m = _manifest()
        m[field] = value
        assert _key(manifest=m) == base, field                            # patches, republish


def _run_with(tmp_path, statuses, pixel="pk", fusion="fh1"):
    run = {"run_id": "r", "source": {"pixel_key": pixel}, "fusion": {"hash": fusion},
           "tasks": [{"task_id": f"t{i}", "combo_id": "c", "method": "stardist_nuclei_dapi",
                      "params": {}, "patch_bbox": [0, 4, 0, 4 + i]} for i in range(len(statuses))]}
    rdir = preseg_run.write_run(str(tmp_path), run)
    for t, st in zip(run["tasks"], statuses):
        if st is None:
            continue
        masks = {"nucleus": np.ones((4, 4), np.uint32)} if st == "ok" else None
        preseg_run.publish_result(rdir, run, t, st, masks=masks)
    return run, rdir, preseg_run.load_records(rdir)


def test_what_may_be_chosen(tmp_path):
    def sel(statuses, **kw):
        run, _, recs = _run_with(tmp_path / str(len(os.listdir(tmp_path))), statuses)
        return preseg_run.selectable(run, recs, "c", kw.get("pk", "pk"), kw.get("fh", "fh1"))
    assert sel(["ok", "ok"]) == (True, "")
    assert sel(["ok", "failed"]) == (True, "1 of 2 patches failed")        # the caller asks
    assert sel(["ok", None])[0] is False
    assert "cancelled" in sel(["ok", "cancelled"])[1]
    assert sel(["failed", "failed"]) == (False, "no patch succeeded")
    assert "changed" in sel(["ok"], pk="other")[1]
    assert "changed" in sel(["ok"], fh="other")[1]
    run, rdir, recs = _run_with(tmp_path / "gone", ["ok"])
    os.remove(recs["t0"]["nucleus"]["path"])
    assert preseg_run.selectable(run, recs, "c", "pk", "fh1") == (False, "a result file is missing")


def test_a_record_is_published_after_its_masks_and_says_what_is_not_produced(tmp_path):
    run, rdir, recs = _run_with(tmp_path, ["ok", "failed"])
    ok, bad = recs["t0"], recs["t1"]
    assert ok["cell"] == {"status": "not_produced"} and ok["nucleus"]["count"] == 1
    assert os.path.isfile(ok["nucleus"]["path"]) and ok["fusion_settings_hash"] == "fh1"
    assert bad["status"] == "failed" and bad["nucleus"] == {"status": "failed"}
    assert not [n for n in os.listdir(os.path.join(rdir, "masks")) if ".tmp" in n]


# ── ownership of a HALO window ──────────────────────────────────────────────
def test_expansion_nuclei_follow_their_cells_and_nuclear_guided_is_not_paired():
    from block01.ui.step1_presegmentation.run_job import own_masks
    cell = np.zeros((10, 10), np.uint32)
    cell[1:5, 1:5] = 4                         # owned (centroid 2.5, 2.5)
    cell[5:10, 5:10] = 2                       # centroid outside the own region
    nuc = np.where(cell > 0, cell, 0).astype(np.uint32)
    nuc[1:5, 1:5] = 0
    nuc[2:4, 2:4] = 4
    nuc[4, 4] = 4                              # the nucleus pokes into [4:, 4:] too
    own = (0, 5, 0, 5)
    m, paired = own_masks("stardist_nuclei_expansion", {"cell": cell, "nucleus": nuc}, own)
    assert paired is True and m["cell"].shape == (5, 5)
    assert set(np.unique(m["cell"])) == {0, 1} and set(np.unique(m["nucleus"])) == {0, 1}
    m, paired = own_masks("mesmer_nuclear_guided", {"cell": cell, "nucleus": nuc}, own)
    assert paired is False
    m, paired = own_masks("stardist_nuclei_dapi", {"cell": None, "nucleus": nuc}, own)
    assert paired is None and m["cell"] is None and m["nucleus"].shape == (5, 5)


# ── the job, end to end, with a real engine process ─────────────────────────
def _job_run(tmp_path, methods, patches, halo=24, roi=None):
    run_id = preseg_run.new_run_id()
    combos, tasks = preseg_run.build_tasks(run_id, methods, patches)
    return {"run_id": run_id, "combos": combos, "tasks": tasks, "patches": patches,
            "fusion": SNAP, "source": {"pixel_key": "pk"}, "halo_px": halo,
            "bounds": [0, 300, 0, 320], "roi": roi or {}}


PATCHES = [{"id": 1, "name": "P1", "bbox": [20, 120, 30, 150]},
           {"id": 2, "name": "P2", "bbox": [180, 290, 200, 318]}]


def test_the_job_equals_the_steps_done_by_hand(tmp_path):
    pytest.importorskip("stardist")
    from csbdeep.utils import normalize
    from skimage.segmentation import expand_labels
    from stardist.models import StarDist2D
    from block01.ui.step1_presegmentation.run_job import PresegRunJob
    methods = [{"method": "stardist_nuclei_expansion",
                "values": _vals("stardist_nuclei_expansion", expand_distance=[6.0])}]
    run = _job_run(tmp_path, methods, PATCHES)
    got, progress = [], []
    job = PresegRunJob(str(tmp_path), run, _Loader, on_record=got.append,
                       on_progress=lambda d, n: progress.append((d, n)))
    job.start()
    assert job.wait(600)
    assert [r["status"] for r in got] == ["ok", "ok"] and progress[-1] == (2, 2)

    model = StarDist2D.from_pretrained("2D_versatile_fluo")
    ld = _Loader()
    for rec, p in zip(got, PATCHES):
        win = preseg_input.read_window(p["bbox"], 24, (0, 300, 0, 320))
        F1 = preseg_input.model_input("nucleus", preseg_input.fused_window(ld, win, SNAP))
        nuc, _ = model.predict_instances(normalize(F1, 1, 99.8, axis=(0, 1)))
        nuc = nuc.astype(np.uint32)
        own = preseg_input.own_local(p["bbox"], win)
        c, n, k = label_ownership.apply_ownership(expand_labels(nuc, distance=6.0), own,
                                                  shared=nuc)
        cell, nucleus = np.load(rec["cell"]["path"]), np.load(rec["nucleus"]["path"])
        y0, y1, x0, x1 = p["bbox"]
        assert cell.shape == nucleus.shape == (y1 - y0, x1 - x0)
        np.testing.assert_array_equal(cell, label_ownership.crop(c, own))
        np.testing.assert_array_equal(nucleus, label_ownership.crop(n, own))
        assert rec["paired"] is True and rec["patch_label"] == p["name"]
        assert rec["engine_identity"]["engine"] == "stardist" and rec["device"]
    rdir = preseg_run.run_dir(str(tmp_path), run["run_id"])
    saved = json.load(open(os.path.join(rdir, "run.json")))
    assert saved["tasks"] == run["tasks"] and saved["fusion"]["hash"] == "fh1"
    assert sorted(os.listdir(rdir)) == ["engine_stardist.log", "masks", "records", "run.json"]


def test_stop_cancels_what_is_left_and_no_engine_remains(tmp_path):
    pytest.importorskip("stardist")
    from block01.ui.step1_presegmentation.run_job import PresegRunJob
    pytest.importorskip("cellpose")
    # Cellpose runs first; the stop there must also keep StarDist from starting.
    methods = [{"method": "stardist_nuclei_dapi",
                "values": _vals("stardist_nuclei_dapi", prob_thresh=[0.3, 0.5])},
               {"method": "cellpose_nuclei_dapi", "values": _vals("cellpose_nuclei_dapi")}]
    run = _job_run(tmp_path, methods, PATCHES)
    job = None
    pids = []

    def first(rec):
        if job._engine is not None and job._engine.proc is not None:
            pids.append(job._engine.proc.pid)
        job.stop()

    job = PresegRunJob(str(tmp_path), run, _Loader, on_record=first)
    job.start()
    assert job.wait(600)
    states = {r["task_id"]: r["status"] for r in job.records.values()}
    assert sorted(states.values()) == ["cancelled"] * 5 + ["ok"]
    assert [r["method"] for r in job.records.values() if r["status"] == "ok"] == \
        ["cellpose_nuclei_dapi"]
    assert len(job.records) == len(run["tasks"])
    assert pids and _group_gone(pids[0])
    rdir = preseg_run.run_dir(str(tmp_path), run["run_id"])
    assert not os.path.exists(os.path.join(rdir, "engine_stardist.log"))   # never started


def test_an_engine_that_cannot_start_fails_its_tasks_and_the_others_still_run(tmp_path):
    pytest.importorskip("stardist")
    from block01.ui.step1_presegmentation.run_job import PresegRunJob
    methods = [{"method": "stardist_nuclei_dapi", "values": _vals("stardist_nuclei_dapi")},
               {"method": "mesmer_whole_cell", "values": _vals("mesmer_whole_cell")}]
    run = _job_run(tmp_path, methods, PATCHES[:1])
    job = PresegRunJob(str(tmp_path), run, _Loader)
    import block01.ui.step1_presegmentation.run_job as rj
    real = rj.EngineProcess

    class _NoMesmer(real):
        def start(self):
            if self.engine == "mesmer":
                raise rj.EngineStartError("mesmer did not start: no deepcell")
            return super().start()

    rj.EngineProcess = _NoMesmer
    try:
        job.start()
        assert job.wait(600)
    finally:
        rj.EngineProcess = real
    by_method = {r["method"]: r for r in job.records.values()}
    assert by_method["stardist_nuclei_dapi"]["status"] == "ok"
    assert by_method["mesmer_whole_cell"]["status"] == "failed"
    assert "did not start" in by_method["mesmer_whole_cell"]["error"]


def test_a_patch_outside_the_region_fails_alone(tmp_path):
    pytest.importorskip("stardist")
    from block01.ui.step1_presegmentation.run_job import PresegRunJob
    patches = PATCHES[:1] + [{"id": 9, "name": "P9", "bbox": [400, 450, 0, 50]}]
    run = _job_run(tmp_path, [{"method": "stardist_nuclei_dapi",
                               "values": _vals("stardist_nuclei_dapi")}], patches)
    job = PresegRunJob(str(tmp_path), run, _Loader)
    job.start()
    assert job.wait(600)
    st = {r["patch_label"]: r for r in job.records.values()}
    assert st["P1"]["status"] == "ok" and st["P9"]["status"] == "failed"
    assert "outside the analysis region" in st["P9"]["error"]


def _group_gone(pid):
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    return False
