"""Step1's Generate without a pre-segmentation run (docs/
v17_step1_generate_decouple_plan.md §2, §4; user rulings 2026-11-10).

* Generate's button follows the saved fusion settings only.
* One image for every method: the DAPI-input kind, nucleus at weight 1, no
  `method` in its identity.
* What Step2 takes from the last Generate: the parameters it saved (a result
  chosen with Use) -- also over a hand-edited Step2 draft -- else its manual
  default, the older parameter file still on offer.
* Parameters found on other fusion settings than the input image's: asked at
  Run (Cancel runs nothing, Run anyway is recorded in the run's seg_config).

Own module: one real window per test, as in test_step1_preseg_run_ui.
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("block01.qt")
zarr = pytest.importorskip("zarr")
import numpy as np  # noqa: E402

from test_step1_preseg_run_ui import _quiet, _window, app  # noqa: E402,F401
from test_step1_step2_handoff_e2e import _chosen_and_saved  # noqa: E402

from block01.core import preseg_contract  # noqa: E402
from block01.qt import QtWidgets  # noqa: E402
from block01.utils import run_store  # noqa: E402

METHOD = "stardist_nuclei_dapi"


def _answer(monkeypatch, button):
    shown = []

    def exec(box):
        shown.append(box.text())
        [b for b in box.buttons() if b.text() == button][0].click()
        return 0
    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", exec)
    return shown


def _fuse_run(tmp_path, fusion_hash, name="fuse_20261110_000000"):
    """A published-looking fuse run: its params.json names the fusion
    settings its image was generated with."""
    run = tmp_path / "runs" / name
    run.mkdir(parents=True)
    path = str(run / "fused.zarr")
    z = zarr.open(path, mode="w", shape=(32, 32, 2), chunks=(32, 32, 2), dtype="uint16")
    z[:] = np.ones((32, 32, 2), dtype="uint16")
    run_store.write_params(str(run), {"kind": "fuse", "fusion_settings": {"hash": fusion_hash}})
    return path


def _in_step2(app, tmp_path, monkeypatch, method=METHOD):
    """Use + Generate (params saved) + the real way into Step2."""
    w, out, path = _chosen_and_saved(app, tmp_path, monkeypatch, method)
    monkeypatch.setattr(type(w), "_load_step0_roi_result", lambda self, *a, **k: True)
    w.step1_output = {"zarr_path": _fuse_run(tmp_path, "fh1"), "output_dir": out,
                      "step2_dir": str(tmp_path / "step2"), "fusion_config_path": "",
                      "roi_info": [], "roi_id": "", "roi_dir": ""}
    w._go_to_step2()
    return w, out, path


# ── the button ─────────────────────────────────────────────────────────

def test_generate_needs_saved_fusion_settings_and_nothing_else(app, tmp_path, monkeypatch):
    w = _window(app, tmp_path, monkeypatch)
    _quiet(monkeypatch)
    try:
        assert w._generate_params_selection() is None        # nothing chosen
        w._fusion_settings_dirty = lambda: False
        w._check_save_unlock()
        assert w.btn_save.isEnabled()
        assert w.btn_save.text() == "Generate DAPI input zarr"
        w._fusion_settings_dirty = lambda: True               # unsaved changes
        w._check_save_unlock()
        assert not w.btn_save.isEnabled()
        w._fusion_settings_dirty = lambda: False
        w._committed_fusion_settings = lambda: None          # never saved
        w._check_save_unlock()
        assert not w.btn_save.isEnabled()
    finally:
        w.close()


# ── one image for every method ─────────────────────────────────────────

def test_one_image_for_every_method_and_no_params_without_a_choice(app, tmp_path, monkeypatch):
    from block01.ui import main_window as mw
    w = _window(app, tmp_path, monkeypatch)
    _quiet(monkeypatch)
    saved = []
    monkeypatch.setattr(mw, "save_segmentation_params",
                        lambda *a, **k: saved.append(a) or ("", ""))
    monkeypatch.setattr(type(w), "_start_fusion_worker", lambda self, *a, **k: None)
    w._fusion_settings_dirty = lambda: False
    try:
        metas = {}
        for method in ("cellpose_wholecell_fusion", "stardist_nuclei_dapi",
                       "mesmer_whole_cell", "cellpose_nuclei_dapi"):
            w._active_segmentation_method = method
            w._pending_dapi_input_meta = None
            w._save()
            meta = w._pending_dapi_input_meta
            assert meta is not None, method
            metas[method] = meta
            assert meta["artifact_kind"] == "step1_dapi_input_zarr"
            assert "method" not in meta
            assert meta["fusion_config"]["nucleus"]["weight"] == 1.0
            assert w._pending_step2_handover[0] == "manual"
            assert w._expected_meta_for_current_config()["artifact_kind"] == \
                "step1_dapi_input_zarr"
        assert len({m["config_hash"] for m in metas.values()}) == 1
        assert saved == []                         # nothing chosen: no params file
    finally:
        w.close()


# ── what Step2 takes from the last Generate ────────────────────────────

def test_a_generate_with_a_choice_wins_over_a_hand_edited_step2(app, tmp_path, monkeypatch):
    w, out, path = _in_step2(app, tmp_path, monkeypatch)
    page = w._step2
    try:
        assert page._param_source_combo.currentData() == "index"
        want = page.get_seg_config()["params"]
        # the user edits Step2 by hand (manual source, another threshold)
        page._set_param_source("manual")
        page._sd_prob.setValue(0.9)
        assert page.get_seg_config()["params"] != want
        # a new Generate with the chosen result: its params win (§5 Q3)
        w._pending_step2_handover = ("params", out)
        w._on_fusion_done(w.step1_output["zarr_path"])
        assert page._param_source_combo.currentData() == "index"
        assert page.get_seg_config()["params"] == want
        assert os.path.abspath(page._applied_param_file) == os.path.abspath(path)
        assert w.__dict__.get("_rm_step2_draft_pending") is False   # no older draft over it
    finally:
        w.close()


def test_a_generate_without_a_choice_leaves_step2_on_its_manual_default(app, tmp_path,
                                                                        monkeypatch):
    w, out, path = _in_step2(app, tmp_path, monkeypatch)
    page = w._step2
    try:
        assert page._param_source_combo.currentData() == "index"
        w._pending_step2_handover = ("manual", out)
        w._on_fusion_done(w.step1_output["zarr_path"])
        assert page._param_source_combo.currentData() == "manual"
        assert preseg_contract.CONTRACT_KEY not in page.get_seg_config()
        assert page._applied_param_file == ""
        # the older file stays on offer, to be chosen on purpose (§5 Q1)
        assert page._seg_params_index
        assert os.path.isfile(path)
    finally:
        w.close()


def test_a_generate_without_a_choice_before_step2_is_ever_shown(app, tmp_path, monkeypatch):
    """codex round 1 (P2): older parameters on disk, Step2 never shown in
    this session (its index field empty), Generate without Use, THEN the
    first entry into Step2: the manual default, not the older file."""
    w, out, path = _chosen_and_saved(app, tmp_path, monkeypatch, METHOD)
    page = w._step2
    try:
        assert not page._seg_params_edit.text().strip()      # never entered
        monkeypatch.setattr(type(w), "_load_step0_roi_result", lambda self, *a, **k: True)
        w.step1_output = {"zarr_path": _fuse_run(tmp_path, "fh1"), "output_dir": out,
                          "step2_dir": str(tmp_path / "step2"), "fusion_config_path": "",
                          "roi_info": [], "roi_id": "", "roi_dir": ""}
        w._drop_preseg_selection()                         # no result chosen now
        w._pending_step2_handover = ("manual", out)
        w._on_fusion_done(w.step1_output["zarr_path"])     # also the reuse path's call
        w._go_to_step2()                                   # first entry
        assert page._param_source_combo.currentData() == "manual"
        assert preseg_contract.CONTRACT_KEY not in page.get_seg_config()
        assert page._seg_params_index                      # the older file still on offer
        assert page._applied_param_file == ""
    finally:
        w.close()


def test_the_hand_over_waits_while_a_step2_run_is_going(app, tmp_path, monkeypatch):
    w, out, path = _in_step2(app, tmp_path, monkeypatch)
    page = w._step2
    try:
        page._run_active = True
        w._pending_step2_handover = ("manual", out)
        w._on_fusion_done(w.step1_output["zarr_path"])
        assert page._param_source_combo.currentData() == "index"    # not yet
        page._run_active = False
        assert w._apply_step2_handover() is True
        assert page._param_source_combo.currentData() == "manual"
    finally:
        w.close()


# ── parameters from other fusion settings ──────────────────────────────

def _runnable(w, page, monkeypatch, tmp_path, input_hash):
    """Step2 on an input generated with fusion settings `input_hash`; the
    worker is recorded, not started."""
    from block01.ui import step2_page
    zp = _fuse_run(tmp_path, input_hash, name=f"fuse_20261110_00000{len(input_hash)}")
    page._zarr_path = zp
    page._roi_dir = ""
    block = preseg_contract.validate(page.get_seg_config())
    page._overlap_spin.setValue(int(block["halo_px"]))
    monkeypatch.setattr(page, "_stardist_grid_ok_for_run", lambda: True)
    monkeypatch.setattr(page, "_confirm_ignored_settings", lambda path: True)
    started = []

    class _Stop(Exception):
        pass

    def worker(**kw):
        started.append(kw["seg_config"])
        raise _Stop()
    monkeypatch.setattr(step2_page, "SegmentMergeWorker", worker)
    return started, _Stop


def test_params_from_other_fusion_settings_are_asked_at_run(app, tmp_path, monkeypatch):
    w, out, path = _in_step2(app, tmp_path, monkeypatch)
    page = w._step2
    try:
        started, stop = _runnable(w, page, monkeypatch, tmp_path, "other-settings")
        shown = _answer(monkeypatch, "Cancel")
        page._run()
        assert started == []                                  # Cancel runs nothing
        assert len(shown) == 1 and "other fusion settings" in shown[0]

        shown = _answer(monkeypatch, "Run anyway")
        with pytest.raises(stop):
            page._run()
        assert len(shown) == 1
        record = started[0]["settings_mismatch_confirmed"]
        assert record["params_hash"] == "fh1"
        assert record["input_hash"] == "other-settings"
    finally:
        w.close()


def test_params_from_the_input_settings_run_without_a_question(app, tmp_path, monkeypatch):
    w, out, path = _in_step2(app, tmp_path, monkeypatch)
    page = w._step2
    try:
        started, stop = _runnable(w, page, monkeypatch, tmp_path, "fh1")
        shown = _answer(monkeypatch, "Cancel")
        with pytest.raises(stop):
            page._run()
        assert shown == []
        assert "settings_mismatch_confirmed" not in started[0]
    finally:
        w.close()


def test_nothing_is_asked_without_both_hashes(app, tmp_path, monkeypatch):
    w, out, path = _in_step2(app, tmp_path, monkeypatch)
    page = w._step2
    try:
        cfg = page.get_seg_config()
        shown = _answer(monkeypatch, "Cancel")
        page._zarr_path = str(tmp_path / "elsewhere" / "fused.zarr")   # not a fuse run
        assert page._confirm_fusion_settings_match(cfg) == (True, None)
        manual = {k: v for k, v in cfg.items() if k != preseg_contract.CONTRACT_KEY}
        page._zarr_path = _fuse_run(tmp_path, "other", name="fuse_20261110_000009")
        assert page._confirm_fusion_settings_match(manual) == (True, None)
        assert shown == []
    finally:
        w.close()
