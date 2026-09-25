"""The Step1 -> Step2 hand-over of a chosen pre-segmentation result.

Step2 hook-up, step 2. For every method the new UI offers, a combination
chosen with Use goes: Save's params file -> Step2's page (loaded through the
index, as after a real Save) -> `get_seg_config()` -> the Step2 worker's
config -> `preseg_contract.runner_params`, and arrives unchanged, with no
Cellpose defaults added. The file carries the HALO, pixels, Fusion settings,
run, combination and the engine identity of THAT run. Since step 3 the page
lets such a file run and the worker takes it to the engine process, never
to the old path (tests/test_step2_runner_path.py); a Step2 edit is refused
as a mismatch; a broken contract refuses to load or to save.
"""

import json
import os
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from block01.core import preseg_contract as pc
from block01.core import preseg_run

pytest.importorskip("PyQt5")
from PyQt5 import QtWidgets  # noqa: E402

IDENT = {
    "cellpose": {"engine": "cellpose", "lock_hash": "L1", "lib_versions": {"cellpose": "4.0.1"},
                 "model_checksum": "m1", "runner_version": "r1"},
    "stardist": {"engine": "stardist", "lock_hash": "L1", "lib_versions": {"stardist": "0.9"},
                 "model_checksum": "m2", "runner_version": "r1"},
    "mesmer": {"engine": "mesmer", "lock_hash": "L1", "lib_versions": {"deepcell": "0.12"},
               "model_checksum": "m3", "runner_version": "r1"},
}

# One non-default combination per method; every key the method editor offers.
COMBOS = {
    "cellpose_wholecell_fusion": {"diameter": 25.0, "flow_threshold": 0.6,
                                  "cellprob_threshold": -1.0, "min_size": 30},
    "cellpose_nuclei_dapi": {"diameter": None, "flow_threshold": 0.4,
                             "cellprob_threshold": 0.5, "min_size": 40},
    "cellpose_nuclei_expansion": {"diameter": 18.5, "flow_threshold": 0.8,
                                  "cellprob_threshold": 0.0, "min_size": 20,
                                  "expand_distance": 5.5},
    "stardist_nuclei_dapi": {"prob_thresh": 0.55, "nms_thresh": None,
                             "model_name": "2D_versatile_fluo"},
    "stardist_nuclei_expansion": {"prob_thresh": None, "nms_thresh": 0.35,
                                  "expand_distance": 12.0, "model_name": "2D_versatile_fluo"},
    "mesmer_whole_cell": {"maxima_threshold": 0.05, "interior_threshold": 0.3,
                          "image_mpp": 0.325, "postprocess_min_size": 12},   # 3 decimals
    "mesmer_nuclei": {"maxima_threshold": 0.125, "interior_threshold": 0.15,
                      "image_mpp": 0.5, "postprocess_min_size": 0},
    "mesmer_nuclear_guided": {"maxima_threshold": 0.075, "interior_threshold": 0.25,
                              "image_mpp": 0.4, "postprocess_min_size": 7},
}
CELLPOSE_ONLY = {"flow_threshold", "cellprob_threshold", "min_size", "diameter",
                 "phase1_diameter", "model_type"}


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _run_on_disk(step1_dir, method, params, n_patches=2, identity=None, run_engine=None,
                 halo=200):
    """A finished run of one combination, published the way run_job does:
    run.json (with the engine that started) and one record + mask per patch."""
    import numpy as np
    engine = preseg_run.ps_engine(method)
    ident = identity if identity is not None else IDENT[engine]
    cid = "c_" + method
    run_ident = run_engine if run_engine is not None else (
        ident[0] if isinstance(ident, list) else ident)
    run = {"run_id": "run_" + method, "halo_px": halo,
           "source": {"pixel_key": "px1"}, "fusion": {"hash": "fh1"},
           "combos": [{"combo_id": cid, "method": method, "params": dict(params)}],
           "tasks": [], "engines": {engine: run_ident}}
    rdir = preseg_run.write_run(step1_dir, run)
    for i in range(n_patches):
        task = {"task_id": f"{cid}__p{i}", "combo_id": cid, "method": method,
                "params": dict(params), "patch_bbox": [0, 8, 8 * i, 8 * i + 8]}
        run["tasks"].append(task)
        lab = np.zeros((8, 8), np.uint32)
        lab[2:5, 2:5] = 1
        rec = preseg_run.publish_result(rdir, run, task, preseg_run.OK,
                                        masks={"cell": lab, "nucleus": lab})
        # What the engine said at hello, per patch (run.json may disagree).
        rec["engine_identity"] = ident[i] if isinstance(ident, list) else ident
        with open(preseg_run.record_path(rdir, task["task_id"]), "w") as f:
            json.dump(rec, f)
    preseg_run.write_run(step1_dir, run)
    return run, rdir


def _use_and_save(tmp_path, method, params, **kw):
    """Use (the real `_on_preseg_use`) then Save's params file (the real
    `_preseg_segmentation_config` + `save_segmentation_params`)."""
    from block01.ui.main_window import MainWindow
    from block01.utils.segmentation_params import save_segmentation_params
    step1 = str(tmp_path / "step1")
    run, rdir = _run_on_disk(step1, method, params, **kw)
    w = types.SimpleNamespace(
        _preseg_run=run, _preseg_records=preseg_run.load_records(rdir),
        _preseg_current=lambda: ("px1", "fh1"), _check_save_unlock=lambda: None,
        _refresh_preseg_results=lambda: None, _preseg_step1_dir=lambda: step1)
    assert MainWindow._on_preseg_use(w, run["combos"][0]["combo_id"]) is True
    cfg = MainWindow._preseg_segmentation_config(w)
    out = str(tmp_path / "out")
    path, _ = save_segmentation_params(out, cfg)
    return run, out, path


def _page_config(app, out):
    from block01.ui.step2_page import Step2Page
    page = Step2Page()
    assert page._load_seg_params_index(out, silent=False, apply_active=True)
    page._set_param_source("index")
    return page


@pytest.mark.parametrize("method", sorted(COMBOS))
def test_the_chosen_combination_reaches_the_engine_unchanged(app, tmp_path, method):
    from block01.utils.segmentation_config import normalize_segmentation_config
    from block01.workers.segment_merge_worker import SegmentMergeWorker
    params = COMBOS[method]
    run, out, path = _use_and_save(tmp_path, method, params)

    with open(path, encoding="utf-8") as f:
        saved = json.load(f)
    block = saved[pc.CONTRACT_KEY]
    rules = pc.fixed_rules(method)
    want = dict(params, **rules)                     # combination + fixed rules
    assert block == {
        "version": 1, "method": method, "params": want, "fixed_rules": sorted(rules),
        "halo_px": 200,
        "pixel_key": "px1", "fusion_settings_hash": "fh1", "preseg_run_id": run["run_id"],
        "combo_id": run["combos"][0]["combo_id"],
        "engine_identity": IDENT[preseg_run.ps_engine(method)]}
    for k, v in want.items():                        # the file's own params: the same
        assert saved["params"][k] == v and saved[k] == v, k
    if not method.startswith("cellpose_"):
        assert not CELLPOSE_ONLY & set(saved["params"]), "Cellpose defaults leaked in"
    if method.startswith("mesmer_"):
        assert saved["params"]["normalize_input"] is False
        assert saved["params"]["threshold_target"] == (
            "nuclear" if method == "mesmer_nuclei" else "whole_cell")

    page = _page_config(app, out)
    seg = page.get_seg_config()
    assert pc.validate(seg) == block
    assert pc.mismatches(block, normalize_segmentation_config(seg)) == []
    worker = SegmentMergeWorker("unused.zarr", seg_config=seg, output_dir=str(tmp_path / "s2"))
    assert pc.runner_params(worker.seg_config) == dict(want, method=method)


def test_min_size_is_no_longer_forced_to_15(app, tmp_path):
    _, out, path = _use_and_save(tmp_path, "cellpose_wholecell_fusion",
                                 COMBOS["cellpose_wholecell_fusion"])
    with open(path, encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["min_size"] == saved["params"]["min_size"] == 30
    page = _page_config(app, out)
    assert page._cp_minsize.value() == 30 and page.get_seg_config()["min_size"] == 30


def _told(monkeypatch):
    said = []
    for name in ("information", "warning"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name,
                            staticmethod(lambda *a, **k: said.append((name, a[2]))))
    return said


def test_step2_lets_an_unchanged_hand_over_run(app, tmp_path, monkeypatch):
    _, out, _ = _use_and_save(tmp_path, "stardist_nuclei_dapi", COMBOS["stardist_nuclei_dapi"])
    page = _page_config(app, out)
    said = _told(monkeypatch)
    assert page._check_preseg_contract(page.get_seg_config()) is True
    assert said == []


def test_a_step2_edit_is_refused_as_a_mismatch(app, tmp_path, monkeypatch):
    _, out, _ = _use_and_save(tmp_path, "cellpose_wholecell_fusion",
                              COMBOS["cellpose_wholecell_fusion"])
    page = _page_config(app, out)
    page._cp_minsize.setValue(31)
    said = _told(monkeypatch)
    assert page._check_preseg_contract(page.get_seg_config()) is False
    assert "not the ones chosen in Step1" in said[-1][1] and "min_size" in said[-1][1]


def test_manual_parameters_drop_the_hand_over(app, tmp_path, monkeypatch):
    """Manual source: the user's own parameters, old path -- the contract
    of an earlier loaded file must not ride along."""
    _, out, _ = _use_and_save(tmp_path, "cellpose_nuclei_dapi", COMBOS["cellpose_nuclei_dapi"])
    page = _page_config(app, out)
    page._set_param_source("manual")
    started = {}
    monkeypatch.setattr(page, "_zarr_path", str(tmp_path))
    from block01.ui import step2_page as s2

    class _Stop(Exception):
        pass

    def fake_worker(**kw):
        started.update(kw)
        raise _Stop()

    monkeypatch.setattr(s2, "SegmentMergeWorker", fake_worker)
    monkeypatch.setattr(page, "_promote_step0_remap", lambda cfg: None)
    with pytest.raises(_Stop):
        page._run()
    assert pc.CONTRACT_KEY not in started["seg_config"]


def test_the_worker_takes_a_hand_over_to_the_engine_process(app, tmp_path, monkeypatch):
    """Never the old in-process Mesmer path: the worker starts the contract's
    engine process (refused to start here, so nothing runs)."""
    from block01.seg_runner.client import EngineProcess, EngineStartError
    from block01.workers.segment_merge_worker import SegmentMergeWorker
    run, out, path = _use_and_save(tmp_path, "mesmer_whole_cell", COMBOS["mesmer_whole_cell"])
    with open(path, encoding="utf-8") as f:
        seg = json.load(f)
    started = []

    def start(self):
        started.append(self.engine)
        raise EngineStartError("not in this test")

    monkeypatch.setattr(EngineProcess, "start", start)
    worker = SegmentMergeWorker("unused.zarr", seg_config=seg, output_dir=str(tmp_path / "s2"))
    errors = []
    worker.error.connect(errors.append)
    worker.run()
    assert started == ["mesmer"]
    assert errors and "the mesmer engine did not start" in errors[0]


def test_a_broken_contract_does_not_load(app, tmp_path, monkeypatch):
    _, out, path = _use_and_save(tmp_path, "stardist_nuclei_dapi", COMBOS["stardist_nuclei_dapi"])
    with open(path, encoding="utf-8") as f:
        saved = json.load(f)
    del saved[pc.CONTRACT_KEY]["engine_identity"]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(saved, f)
    from block01.ui.step2_page import Step2Page
    page = Step2Page()
    said = _told(monkeypatch)
    assert page._load_seg_params_file(path, silent=False) is False
    assert "lacks: engine_identity" in said[-1][1]


# ── the contract itself ─────────────────────────────────────────────────────
@pytest.mark.parametrize("case,expect", [
    ("no_identity", "engine identity is missing"),
    ("mixed", "different engines"),
    ("run_differs", "disagree on the engine"),
    ("no_halo", "does not record: halo_px"),
])
def test_build_refuses_what_it_cannot_vouch_for(tmp_path, case, expect):
    method, params = "stardist_nuclei_dapi", COMBOS["stardist_nuclei_dapi"]
    other = dict(IDENT["stardist"], lib_versions={"stardist": "0.8"})
    kw = {"no_identity": {"identity": {}},
          "mixed": {"identity": [IDENT["stardist"], other]},
          "run_differs": {"run_engine": other},
          "no_halo": {"halo": None}}[case]
    if case == "mixed":
        kw["run_engine"] = IDENT["stardist"]
    run, rdir = _run_on_disk(str(tmp_path), method, params, **kw)
    with pytest.raises(pc.ContractError, match=expect):
        pc.build(run, run["combos"][0]["combo_id"], preseg_run.load_records(rdir))


def test_a_refused_save_writes_nothing(app, tmp_path, monkeypatch):
    """Save builds the contract before writing anything."""
    from block01.ui import main_window as mw
    monkeypatch.setattr(mw, "OUTPUT_DIR", str(tmp_path / "out"))
    run, rdir = _run_on_disk(str(tmp_path / "step1"), "stardist_nuclei_dapi",
                             COMBOS["stardist_nuclei_dapi"], identity={})
    said = _told(monkeypatch)
    w = types.SimpleNamespace(
        _p2_params={"method": "stardist_nuclei_dapi", "params": {}},
        _params_source=mw.PRESEG_SOURCE,
        _preseg_selected={"run": run, "combo_id": run["combos"][0]["combo_id"]},
        _preseg_selection_valid=lambda: (True, ""),
        _preseg_step1_dir=lambda: str(tmp_path / "step1"),
        search=types.SimpleNamespace(_method_combo=types.SimpleNamespace(currentData=lambda: None)))
    w._preseg_segmentation_config = lambda: mw.MainWindow._preseg_segmentation_config(w)
    mw.MainWindow._save(w)
    assert "cannot be saved for Step2: the engine identity is missing" in said[-1][1]
    assert not os.path.exists(tmp_path / "out")


@pytest.mark.parametrize("mutate,expect", [
    (lambda b: b.update(version=2), "unknown pre-segmentation contract version 2"),
    (lambda b: b.pop("pixel_key"), "lacks: pixel_key"),
    (lambda b: b.update(engine_identity=dict(b["engine_identity"], engine="mesmer")),
     "engine identity does not fit"),
])
def test_validate_goes_by_the_version_and_the_fields(mutate, expect):
    block = {"version": 1, "method": "stardist_nuclei_dapi", "params": {"prob_thresh": 0.5},
             "halo_px": 200, "pixel_key": "px", "fusion_settings_hash": "fh",
             "preseg_run_id": "r", "combo_id": "c", "engine_identity": IDENT["stardist"]}
    assert pc.validate({"method": "stardist_nuclei_dapi"}) is None          # old file
    assert pc.validate({"method": "stardist_nuclei_dapi", pc.CONTRACT_KEY: block}) == block
    with pytest.raises(pc.ContractError, match="not the contract's"):
        pc.validate({"method": "cellpose_nuclei_dapi", pc.CONTRACT_KEY: block})
    mutate(block)
    with pytest.raises(pc.ContractError, match=expect):
        pc.validate({pc.CONTRACT_KEY: block})


def test_step2s_image_mpp_box_keeps_step1s_precision(app):
    """Step1 offers image mpp to 3 decimals (0.325 at 20x); Step2's box must
    not round it on the way (approved 2026-09-25)."""
    from block01.ui.step2_page import Step2Page
    from block01.utils import segmentation_param_schema as ps
    spec = next(s for s in ps.specs("mesmer_whole_cell") if s.key == "image_mpp")
    page = Step2Page()
    assert page._mesmer_mpp.decimals() == spec.decimals == 3
    page._mesmer_mpp.setValue(0.325)
    assert page._mesmer_mpp.value() == 0.325


def test_mesmer_fixed_rules_are_the_engines():
    """One statement of the rules, and the engine applies the same target."""
    from block01.seg_runner import engines
    for method, target in engines.MESMER_THRESHOLD_TARGET.items():
        assert pc.fixed_rules(method) == {"normalize_input": False, "threshold_target": target}
    assert pc.fixed_rules("cellpose_nuclei_dapi") == {} == pc.fixed_rules("stardist_nuclei_dapi")


@pytest.mark.parametrize("method", ["mesmer_whole_cell", "mesmer_nuclei", "mesmer_nuclear_guided"])
def test_ticking_normalize_input_in_step2_is_a_mismatch(app, tmp_path, monkeypatch, method):
    _, out, _ = _use_and_save(tmp_path, method, COMBOS[method])
    page = _page_config(app, out)
    assert page._mesmer_norm.isChecked() is False
    page._mesmer_norm.setChecked(True)
    said = _told(monkeypatch)
    assert page._check_preseg_contract(page.get_seg_config()) is False
    assert "not the ones chosen in Step1" in said[-1][1] and "normalize_input" in said[-1][1]


def test_a_mesmer_contract_without_its_fixed_rules_is_refused():
    block = {"version": 1, "method": "mesmer_nuclei",
             "params": {"maxima_threshold": 0.1, "normalize_input": False},
             "halo_px": 200, "pixel_key": "px", "fusion_settings_hash": "fh",
             "preseg_run_id": "r", "combo_id": "c", "engine_identity": IDENT["mesmer"]}
    with pytest.raises(pc.ContractError, match="fixed rules"):
        pc.validate({pc.CONTRACT_KEY: block})
    block["params"]["threshold_target"] = "whole_cell"                       # wrong target
    with pytest.raises(pc.ContractError, match="fixed rules"):
        pc.validate({pc.CONTRACT_KEY: block})
    block["params"]["threshold_target"] = "nuclear"
    assert pc.validate({pc.CONTRACT_KEY: block}) == block
