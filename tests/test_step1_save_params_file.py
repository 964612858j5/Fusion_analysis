"""The params file Save writes, through the real `_save`.

Step2 hook-up, step 2 changed only Save's branch for a chosen
pre-segmentation result: that branch writes exactly the config built for the
choice (the combination's parameters plus the contract), nothing added on
the way. A Phase 2 / manual choice keeps its old file -- the fixed min size
15 and the Cellpose keys included -- and no contract (old files keep the old
path, user ruling 2026-09-25); the expected file is the one the code before
the change wrote (captured from HEAD 54e825d). Own module: one real window,
as in test_step1_preseg_run_ui.
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5")

from test_step1_preseg_run_ui import _quiet, _window, app  # noqa: E402,F401

BEFORE = {
    'batch_size': 8, 'cellprob_threshold': -0.5, 'diameter': 31.0,
    'display_name': 'Cellpose whole-cell (Fusion + DAPI)', 'flow_threshold': 0.7,
    'input_type': 'fused_channel_plus_dapi', 'method': 'cellpose_wholecell_fusion',
    'min_size': 15, 'model_type': 'cpsam', 'output_type': 'cell_mask',
    'params': {'batch_size': 8, 'cellprob_threshold': -0.5, 'diameter': 31.0,
               'flow_threshold': 0.7, 'min_size': 15, 'model_type': 'cpsam',
               'params_source': 'phase2_grid', 'phase1_diameter': 22.0, 'tile_size': 1024,
               'use_gpu': True},
    'params_source': 'phase2_grid', 'phase1_diameter': 22.0, 'tile_size': 1024,
    'use_gpu': True}


class _Stop(Exception):
    pass


def _saving_window(app, tmp_path, monkeypatch):
    """A window whose Save stops at the params file and hands it back."""
    from block01.ui import main_window as mw
    w = _window(app, tmp_path, monkeypatch)
    said = _quiet(monkeypatch)
    monkeypatch.setattr(mw, "OUTPUT_DIR", str(tmp_path / "out"))
    got = {}

    def capture(out, cfg):
        got["cfg"] = json.loads(json.dumps(cfg, default=str))
        raise _Stop()                         # the file is all these tests are about

    monkeypatch.setattr(mw, "save_segmentation_params", capture)
    w._fusion_settings_dirty = lambda: False
    w._params_match_committed_settings = lambda: True
    return w, got, said


def test_a_chosen_result_saves_exactly_its_own_config(app, tmp_path, monkeypatch):  # noqa: F811
    from block01.ui import main_window as mw
    w, got, said = _saving_window(app, tmp_path, monkeypatch)
    try:
        built = mw.normalize_segmentation_config({
            "method": "cellpose_wholecell_fusion", "params_source": mw.PRESEG_SOURCE,
            "params": {"diameter": 25.0, "flow_threshold": 0.6, "cellprob_threshold": -1.0,
                       "min_size": 30},
            "preseg_contract": {"version": 1, "combo_id": "c1"}})
        w._p2_params = {"method": "cellpose_wholecell_fusion", "params": dict(built["params"])}
        w._params_source = mw.PRESEG_SOURCE
        w._preseg_selection_valid = lambda: (True, "")
        w._preseg_segmentation_config = lambda: json.loads(json.dumps(built))
        with pytest.raises(_Stop):
            w._save()
        assert "cfg" in got, said
        assert got["cfg"] == json.loads(json.dumps(built))
        assert got["cfg"]["min_size"] == 30
    finally:
        w.close()


def test_the_old_panel_save_writes_the_same_file(app, tmp_path, monkeypatch):  # noqa: F811
    w, got, said = _saving_window(app, tmp_path, monkeypatch)
    try:
        w._p1_diam = 22.0
        w._p2_params = {"method": "cellpose_wholecell_fusion", "diameter": 31.0,
                        "flow_threshold": 0.7, "cellprob_threshold": -0.5,
                        "params": {"min_size": 44}}
        w._params_source = "phase2_grid"
        with pytest.raises(_Stop):
            w._save()
        assert "cfg" in got, said
        cfg = got["cfg"]
        cfg.pop("saved_at")
        assert cfg == BEFORE
    finally:
        w.close()
