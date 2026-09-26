"""Block M: Step2's eight methods run in the engine process, however their
parameters arrive.

Manual parameters and old params files (no `preseg_contract` block) go
through the same `seg_runner` engine as a Step1 hand-over:

  * equality: a manual run's global mask equals running the same runner tile
    by tile and pasting the way Step2 pastes (Cellpose ×3, StarDist ×2, both
    loops; Mesmer is skipped as "not accepted" on a machine without its model,
    never mocked);
  * Stop during a manual run's inference ends it within 2 s, nothing
    registered; a tile the engine fails on ends the run;
  * Use GPU unchecked runs the engine on the CPU, manual and hand-over alike;
  * the StarDist model is always the fixed one;
  * the page: one Use GPU row for every method and both parameter sources;
    Mesmer shows Step1's parameters only; the StarDist model is read-only;
    an old file's settings the engine does not use are listed before the run,
    and Cancel does not run it.

Synthetic projects in the test's temporary directory only.
"""

import json
import os
import signal
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")
from PyQt5 import QtWidgets  # noqa: E402

from block01.core import preseg_contract  # noqa: E402
from block01.seg_runner import engines as seg_engines  # noqa: E402

import test_preseg_contract as tpc  # noqa: E402
import test_step2_runner_path as rp  # noqa: E402

ENGINE_METHODS = sorted(rp.PARAMS)
MESMER = ("mesmer_whole_cell", "mesmer_nuclei", "mesmer_nuclear_guided")


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _manual(method, **extra):
    """Manual parameters: what the page hands over without a contract."""
    return dict(rp.PARAMS[method], method=method, **extra)


def _meta(worker):
    with open(os.path.join(worker.output_dir, "segmentation_meta.json"), encoding="utf-8") as f:
        return json.load(f)


# ── worker ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("loop", ["full", "roi"])
@pytest.mark.parametrize("method", ENGINE_METHODS)
def test_a_manual_run_equals_the_runner(app, tmp_path, method, loop):
    import numpy as np
    import zarr
    reason = rp._engine_missing(seg_engines.METHOD_ENGINE[method])
    if reason:
        pytest.skip(reason)
    img = rp._image()
    worker = rp._worker(tmp_path, rp._fused_zarr(tmp_path, img), _manual(method),
                        rois=[{"name": "A"}] if loop == "roi" else None)
    assert preseg_contract.validate(worker.seg_config) is None        # no contract
    got = rp._collect(worker)
    worker.run()
    assert got["error"] == [] and len(got["finished"]) == 1
    prim, nuclei, total = rp._oracle(method, img)
    assert total > 0 and got["finished"][0] == total
    sfx = "_A" if loop == "roi" else ""
    mask = np.asarray(zarr.open(os.path.join(worker.output_dir, f"global_mask{sfx}.zarr"), mode="r"))
    np.testing.assert_array_equal(mask, prim)
    if method == "mesmer_nuclear_guided":
        nz = zarr.open(os.path.join(worker.output_dir, f"global_nuclei_mask{sfx}.zarr"), mode="r")
        np.testing.assert_array_equal(np.asarray(nz), nuclei)
    eng = _meta(worker)["seg_engine"]
    assert eng["engine"] == seg_engines.METHOD_ENGINE[method] and eng["step1_identity"] is None
    assert rp._registered(worker)
    rp._assert_nothing_left(worker)


def test_a_stop_during_manual_inference_ends_the_run_at_once(app, tmp_path):
    if rp._engine_missing("cellpose"):
        pytest.skip("no Cellpose")
    worker = rp._worker(tmp_path, rp._fused_zarr(tmp_path, rp._image()),
                        _manual("cellpose_wholecell_fusion"), rois=[{"name": "A"}])
    got = rp._collect(worker)
    worker.start()
    try:
        rp._wait_for(lambda: rp._in_flight(worker))
        pgid = worker._engine.proc.pid
        assert rp._timed_stop(worker) < 0.05                  # the GUI thread never waits
        t0 = time.monotonic()
        rp._wait_for(lambda: worker.isFinished())
        assert time.monotonic() - t0 < 2.0                   # not the end of the tile
    finally:
        worker.wait(60000)
    assert got["finished"] == [] and got["error"] == ["Stopped by user."]
    assert not rp._registered(worker)
    rp._assert_nothing_left(worker, pgid)


def test_a_failed_manual_tile_ends_the_run(app, tmp_path):
    if rp._engine_missing("cellpose"):
        pytest.skip("no Cellpose")
    worker = rp._worker(tmp_path, rp._fused_zarr(tmp_path, rp._image()),
                        _manual("cellpose_nuclei_dapi"))
    got = rp._collect(worker)
    worker.start()
    try:
        rp._wait_for(lambda: rp._in_flight(worker))
        os.kill(worker._engine.proc.pid, signal.SIGKILL)       # the engine child, not Step2
        rp._wait_for(lambda: worker.isFinished())
    finally:
        worker.wait(60000)
    assert got["finished"] == [] and len(got["error"]) == 1
    assert "the engine failed on tile_" in got["error"][0]
    assert not rp._registered(worker)


@pytest.mark.parametrize("source", ["manual", "hand-over"])
def test_use_gpu_unchecked_runs_the_engine_on_the_cpu(app, tmp_path, source):
    # StarDist: Cellpose-SAM on the CPU takes minutes per tile on this
    # machine (measured ~230 s for 120x120); its CPU start is checked below.
    if rp._engine_missing("stardist"):
        pytest.skip("no StarDist")
    method = "stardist_nuclei_dapi"
    cfg = _manual(method) if source == "manual" else rp._contract_config(tmp_path, method)
    cfg["use_gpu"] = False
    worker = rp._worker(tmp_path, rp._fused_zarr(tmp_path, rp._image()), cfg)
    got = rp._collect(worker)
    worker.run()
    assert got["error"] == [] and len(got["finished"]) == 1
    eng = _meta(worker)["seg_engine"]
    assert eng["use_gpu"] is False and eng["device"] == "cpu"
    assert (eng["step1_identity"] is None) == (source == "manual")


def test_a_cpu_only_cellpose_engine_sees_no_gpu(app, tmp_path):
    if rp._engine_missing("cellpose"):
        pytest.skip("no Cellpose")
    from block01.seg_runner.client import EngineProcess
    ep = EngineProcess("cellpose", cpu_only=True, log_path=str(tmp_path / "cp.log"))
    try:
        assert ep.start()["device"] == "cpu"
    finally:
        ep.close()


def test_use_gpu_checked_leaves_the_device_to_the_engine(app, tmp_path):
    if rp._engine_missing("cellpose"):
        pytest.skip("no Cellpose")
    import torch
    worker = rp._worker(tmp_path, rp._fused_zarr(tmp_path, rp._image()),
                        _manual("cellpose_nuclei_dapi", use_gpu=True))
    worker.run()
    eng = _meta(worker)["seg_engine"]
    assert eng["use_gpu"] is True
    assert ("cuda" in eng["device"]) == torch.cuda.is_available()


def test_the_stardist_model_is_always_the_fixed_one(app, tmp_path):
    from block01.workers.segment_merge_worker import SegmentMergeWorker
    seg = _manual("stardist_nuclei_dapi", model_name="2D_paper_dsb2018")
    worker = SegmentMergeWorker("unused.zarr", seg_config=seg, output_dir=str(tmp_path / "s2"))
    assert worker._engine_params()["model_name"] == preseg_contract.STARDIST_MODEL


def test_hq_methods_keep_their_own_path(app, tmp_path):
    from block01.workers.segment_merge_worker import SegmentMergeWorker
    for method in ("cellpose_nuclei_hq", "cellpose_nuclei_hq2", "cellpose_nuclei_csd"):
        worker = SegmentMergeWorker("unused.zarr", seg_config={"method": method},
                                    output_dir=str(tmp_path / method))
        assert not worker._runs_on_engine()
    for method in ENGINE_METHODS:
        worker = SegmentMergeWorker("unused.zarr", seg_config={"method": method},
                                    output_dir=str(tmp_path / method))
        assert worker._runs_on_engine()


# ── the pure list of ignored settings ─────────────────────────────────

def test_an_old_mesmer_file_lists_what_the_engine_does_not_use():
    raw = {"method": "mesmer_whole_cell", "input_mode": "selected_channels",
           "nuclear_channel": "DAPI", "membrane_channels": ["PanCK", "CD45"],
           "normalize_input": True, "tile_size": 512, "overlap": 64, "batch_size": 4}
    keys = [k for k, _, _ in preseg_contract.ignored_settings(raw)]
    assert keys == ["input_mode", "nuclear_channel", "membrane_channels", "normalize_input",
                    "tile_size", "overlap", "batch_size"]
    # the method's own input and no extra stretch: nothing to say
    assert preseg_contract.ignored_settings(
        {"method": "mesmer_whole_cell", "input_mode": "step1_weighted_fusion",
         "normalize_input": False}) == []
    assert preseg_contract.ignored_settings(
        {"method": "mesmer_nuclei", "params": {"input_mode": "DAPI only"}}) == []
    # `params` wins over the top level, as in the normaliser
    assert [k for k, _, _ in preseg_contract.ignored_settings(
        {"method": "mesmer_nuclei", "input_mode": "DAPI only",
         "params": {"input_mode": "step1_weighted_fusion"}})] == ["input_mode"]


def test_a_stardist_model_other_than_the_fixed_one_is_listed():
    assert preseg_contract.ignored_settings(
        {"method": "stardist_nuclei_dapi", "model_name": "2D_paper_dsb2018"}) == [
        ("model_name", "2D_paper_dsb2018", "the engine loads 2D_versatile_fluo only")]
    assert preseg_contract.ignored_settings(
        {"method": "stardist_nuclei_dapi", "model_name": "2D_versatile_fluo"}) == []
    assert preseg_contract.ignored_settings({"method": "cellpose_nuclei_hq", "tile_size": 9}) == []


# ── the page ──────────────────────────────────────────────────────────

def _page(app):
    from block01.ui.step2_page import Step2Page
    page = Step2Page()
    page.resize(500, 1400)
    page.show()
    app.processEvents()
    return page


def _choose(page, method, app):
    page._method_combo.setCurrentIndex(page._method_combo.findData(method))
    app.processEvents()


def _labels(page):
    return {w.text() for w in page.findChildren(QtWidgets.QLabel) if w.isVisible()}


def test_every_method_has_the_use_gpu_row_in_both_sources(app, tmp_path):
    _, out, _ = tpc._use_and_save(tmp_path, "cellpose_nuclei_dapi",
                                  tpc.COMBOS["cellpose_nuclei_dapi"])
    page = _page(app)
    for method in ENGINE_METHODS:
        _choose(page, method, app)
        assert page._cp_gpu.isVisible(), method
    page._cp_gpu.setChecked(False)
    assert page.get_seg_config()["use_gpu"] is False
    assert page._load_seg_params_index(out, silent=False, apply_active=True)
    page._set_param_source("index")
    app.processEvents()
    assert page._cp_gpu.isVisible()


@pytest.mark.parametrize("old, ticked", [("cpu", False), ("auto", True), ("gpu", True)])
def test_an_old_mesmer_device_choice_sets_use_gpu(app, tmp_path, old, ticked):
    from block01.utils.segmentation_params import save_segmentation_params
    out = str(tmp_path / "out")
    save_segmentation_params(out, {"method": "mesmer_whole_cell", "use_gpu": old})
    page = _page(app)
    assert page._load_seg_params_index(out, silent=False, apply_active=True)
    page._set_param_source("index")
    assert page._cp_gpu.isChecked() is ticked
    assert page.get_seg_config()["use_gpu"] is ticked


@pytest.mark.parametrize("method", MESMER)
def test_mesmer_shows_step1s_parameters_only(app, method):
    page = _page(app)
    _choose(page, method, app)
    shown = _labels(page)
    for gone in ("nuclear_channel:", "membrane_channels:", "input_mode:", "use_gpu:",
                 "tile_size:", "overlap:", "batch_size:", "normalize_input:",
                 "percentile_low:", "percentile_high:", "tile size:", "batch size:"):
        assert gone not in shown, gone
    for kept in ("maxima_threshold:", "interior_threshold:", "image_mpp:", "postprocess_min_size:"):
        assert kept in shown, kept
    page._mesmer_maxima.setValue(0.05)
    page._mesmer_interior.setValue(0.3)
    seg = page.get_seg_config()
    assert seg["maxima_threshold"] == 0.05 and seg["interior_threshold"] == 0.3
    assert seg["normalize_input"] is False
    assert seg["threshold_target"] == preseg_contract.fixed_rules(method)["threshold_target"]
    assert seg["input_mode"] == preseg_contract.mesmer_input_mode(method)
    # what an old file carried does not reach the worker
    page._seg_config = {"method": method, "membrane_channels": ["PanCK"],
                        "normalize_input": True, "tile_size": 512, "use_gpu": "cpu"}
    seg = page.get_seg_config()
    assert seg["membrane_channels"] == [] and seg["normalize_input"] is False
    assert seg["tile_size"] != 512 and seg["use_gpu"] is page._cp_gpu.isChecked()


def test_the_stardist_model_is_read_only(app):
    page = _page(app)
    _choose(page, "stardist_nuclei_dapi", app)
    assert page._sd_model.isReadOnly() and page._sd_model.isVisible()
    page._apply_seg_config_to_ui({"method": "stardist_nuclei_dapi", "model_name": "other"})
    assert page._sd_model.text() == preseg_contract.STARDIST_MODEL
    assert page.get_seg_config()["model_name"] == preseg_contract.STARDIST_MODEL


def _old_mesmer_index(tmp_path):
    from block01.utils.segmentation_params import save_segmentation_params
    out = str(tmp_path / "out")
    raw = {"method": "mesmer_whole_cell", "input_mode": "selected_channels",
           "membrane_channels": ["PanCK"], "normalize_input": True}
    path, _ = save_segmentation_params(out, raw)
    with open(path, encoding="utf-8") as f:
        written = json.load(f)
    assert preseg_contract.CONTRACT_KEY not in written
    return out, path


def _answer(monkeypatch, button):
    shown = []

    def exec_(box):
        shown.append(box.text())
        [b for b in box.buttons() if b.text() == button][0].click()
        return 0
    monkeypatch.setattr(QtWidgets.QMessageBox, "exec_", exec_)
    return shown


def test_an_old_file_is_run_by_the_contract_only_after_it_is_said(app, tmp_path, monkeypatch):
    out, path = _old_mesmer_index(tmp_path)
    page = _page(app)
    shown = _answer(monkeypatch, "Run per contract")
    assert page._confirm_ignored_settings(path) is True
    assert "input_mode" in shown[0] and "membrane_channels" in shown[0]
    assert "normalize_input" in shown[0] and "contract" in shown[0]
    shown = _answer(monkeypatch, "Cancel")
    assert page._confirm_ignored_settings(path) is False


def test_cancel_does_not_run_the_old_file(app, tmp_path, monkeypatch):
    out, path = _old_mesmer_index(tmp_path)
    page = _page(app)
    page.set_zarr_path(rp._fused_zarr(tmp_path, rp._image()))
    assert page._load_seg_params_index(out, silent=False, apply_active=True)
    page._set_param_source("index")
    shown = _answer(monkeypatch, "Cancel")
    page._run()
    assert len(shown) == 1 and page._worker is None


def test_a_hand_over_with_another_stardist_model_is_not_a_mismatch(app, tmp_path, monkeypatch):
    combo = dict(tpc.COMBOS["stardist_nuclei_dapi"], model_name="2D_paper_dsb2018")
    _, out, path = tpc._use_and_save(tmp_path, "stardist_nuclei_dapi", combo)
    page = tpc._page_config(app, out)
    said = tpc._told(monkeypatch)
    assert page._check_preseg_contract(page.get_seg_config()) is True and said == []
    shown = _answer(monkeypatch, "Run per contract")
    assert page._confirm_ignored_settings(path) is True
    assert "model_name" in shown[0] and "2D_versatile_fluo" in shown[0]


# ── Step1: the StarDist model is fixed there too ──────────────────────

def test_step1_refuses_another_stardist_model_and_shows_it_read_only(app):
    from block01.ui.step1_presegmentation.method_editor import MethodEditorDialog
    from block01.utils import segmentation_param_schema as ps
    spec = {s.key: s for s in ps.specs("stardist_nuclei_dapi")}["model_name"]
    assert spec.fixed and spec.default == preseg_contract.STARDIST_MODEL
    assert ps.parse_values(spec, "2D_versatile_fluo") == (["2D_versatile_fluo"], "")
    with pytest.raises(ps.ParamError, match="fixed"):
        ps.parse_values(spec, "2D_paper_dsb2018")
    # an older plan that named another model runs the fixed one
    combos = ps.combinations("stardist_nuclei_dapi", {"model_name": ["2D_paper_dsb2018"]})
    assert [c["model_name"] for c in combos] == [preseg_contract.STARDIST_MODEL]
    dlg = MethodEditorDialog(method="stardist_nuclei_dapi",
                             values={"model_name": ["2D_paper_dsb2018"]}, lock_method=True)
    _, edit, _ = dlg._fields["model_name"]
    assert edit.isReadOnly() and edit.text() == preseg_contract.STARDIST_MODEL
    assert dlg.values()["model_name"] == [preseg_contract.STARDIST_MODEL]
