"""Block E: the whole hand-over, through the public path, for all 8 methods.

A pre-segmentation result chosen with the real `Use`, saved with the real
`_save` (it writes the real params file; the fusion job that follows is not
the subject and is stopped before it starts), Step2 entered with the real
`_go_to_step2`, then run with no edit: what Step2 hands the worker
(`get_seg_config()`) carries the chosen combination unchanged, its contract
checks out, and the engine would receive exactly that combination.

Save takes a chosen result only (plan 4.7, user ruling 2026-09-25): old
Phase 2 parameters, as an old session brings them back, leave it locked.
Own module: one real window per test, as in test_step1_preseg_run_ui.
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5")
zarr = pytest.importorskip("zarr")
import numpy as np  # noqa: E402

from test_step1_preseg_run_ui import _quiet, _window, app  # noqa: E402,F401
import test_preseg_contract as tpc  # noqa: E402

from block01.core import preseg_contract, preseg_run  # noqa: E402


class _Stop(Exception):
    pass


def _chosen_and_saved(app, tmp_path, monkeypatch, method):
    """Use + Save through the window; returns (window, params file path)."""
    from block01.ui import main_window as mw
    from block01.utils import segmentation_params as sp
    w = _window(app, tmp_path, monkeypatch)
    _quiet(monkeypatch)
    out = str(tmp_path / "out")
    monkeypatch.setattr(mw, "OUTPUT_DIR", out)
    written = {}

    def save_then_stop(out_dir, cfg):
        written["path"], _ = sp.save_segmentation_params(out_dir, cfg)
        raise _Stop()                         # before the fusion job

    monkeypatch.setattr(mw, "save_segmentation_params", save_then_stop)
    w._fusion_settings_dirty = lambda: False
    w._params_match_committed_settings = lambda: True
    run, rdir = tpc._run_on_disk(w._preseg_step1_dir(), method, tpc.COMBOS[method])
    w._preseg_run = run
    w._preseg_records = preseg_run.load_records(rdir)
    w._preseg_current = lambda: ("px1", "fh1")
    assert w._on_preseg_use(run["combos"][0]["combo_id"]) is True
    assert w.btn_save.isEnabled()
    with pytest.raises(_Stop):
        w._save()
    return w, out, written["path"]


def _fused_zarr(tmp_path):
    path = str(tmp_path / "fused.zarr")
    z = zarr.open(path, mode="w", shape=(32, 32, 2), chunks=(32, 32, 2), dtype="uint16")
    z[:] = np.ones((32, 32, 2), dtype="uint16")
    return path


@pytest.mark.parametrize("method", sorted(tpc.COMBOS))
def test_step2_runs_the_chosen_combination_unchanged(app, tmp_path, monkeypatch, method):
    w, out, path = _chosen_and_saved(app, tmp_path, monkeypatch, method)
    try:
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
        assert preseg_contract.CONTRACT_KEY in saved
        # what Save leaves for Step2, then the real way into Step2
        monkeypatch.setattr(type(w), "_load_step0_roi_result", lambda self, *a, **k: True)
        w.step1_output = {"zarr_path": _fused_zarr(tmp_path), "output_dir": out,
                          "step2_dir": str(tmp_path / "step2"), "fusion_config_path": "",
                          "roi_info": [], "roi_id": "", "roi_dir": ""}
        w._go_to_step2()
        page = w._step2
        assert page._param_source_combo.currentData() == "index"
        cfg = page.get_seg_config()
        assert page._check_preseg_contract(cfg) is True
        block = preseg_contract.validate(cfg)
        assert preseg_contract.mismatches(block, cfg) == []
        want = dict(tpc.COMBOS[method], **preseg_contract.fixed_rules(method), method=method)
        assert preseg_contract.runner_params(cfg) == want
        assert cfg["method"] == method
    finally:
        w.close()


def test_old_phase2_parameters_do_not_unlock_save(app, tmp_path, monkeypatch):
    """As an old session brings them back: Save stays locked and refuses."""
    w = _window(app, tmp_path, monkeypatch)
    said = _quiet(monkeypatch)
    try:
        w._p2_params = {"method": "cellpose_wholecell_fusion", "diameter": 31.0,
                        "flow_threshold": 0.7, "cellprob_threshold": -0.5}
        w._params_source = "phase2_grid"
        w._check_save_unlock()
        assert not w.btn_save.isEnabled()
        w._save()
        assert "Choose a pre-segmentation result first" in said[-1]
    finally:
        w.close()


def test_the_old_panel_and_patch_results_are_off_the_screen(app, tmp_path, monkeypatch):
    w = _window(app, tmp_path, monkeypatch)
    try:
        w.show()
        from PyQt5 import QtWidgets
        QtWidgets.QApplication.processEvents()
        assert not w.search.isVisibleTo(w)                   # kept, not shown
        tabs = w.right_tabs
        assert not tabs.isTabVisible(tabs.indexOf(w.patch_results_tab))
        assert w.result_grid is not None                     # kept
        w._show_step1_patch_results_tab("test")               # an old caller
        assert tabs.currentWidget() is not w.patch_results_tab
    finally:
        w.close()


def test_save_writes_no_fusion_config_json_and_the_session_keeps_the_record(
        app, tmp_path, monkeypatch):
    """Block U1 (user ruling 2026-09-26): Step1's JSON is step1_session.json;
    what a Save fused with is its `last_save`, kept through a restore."""
    w, out, _ = _chosen_and_saved(app, tmp_path, monkeypatch, "cellpose_wholecell_fusion")
    try:
        assert not os.path.exists(os.path.join(out, "fusion_config.json"))
        record = w._last_save
        assert record and record.get("groups") is not None and "saved_at" in record
        payload = w._step1_session_payload()
        assert payload["last_save"] == record
        # the Step2 info line no longer names a config file
        monkeypatch.setattr(type(w), "_load_step0_roi_result", lambda self, *a, **k: True)
        w.step1_output = {"zarr_path": _fused_zarr(tmp_path), "output_dir": out,
                          "step2_dir": str(tmp_path / "step2"), "roi_info": [],
                          "roi_id": "", "roi_dir": ""}
        w._go_to_step2()
        assert "Config:" not in (w._step2._zarr_info.text() or "")
    finally:
        w.close()


def test_step3_finds_the_raw_slide_in_the_session(app, tmp_path):
    from block01.ui.step3_page import Step3Page
    raw = tmp_path / "slide.ome.tif"
    raw.write_bytes(b"x")
    (tmp_path / "step1_session.json").write_text(
        json.dumps({"raw_ome_path": str(raw)}), encoding="utf-8")
    page = Step3Page()
    try:
        page._output_dir = str(tmp_path)
        page._raw_ome_path = ""
        page._loader = None
        found = page._resolve_raw_ome_path()
        assert found and os.path.abspath(found) == os.path.abspath(str(raw))
    finally:
        page.close()
