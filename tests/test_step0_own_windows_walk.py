"""Step0 keeps its own Min/Max/Gamma across a walk through the steps.

User report 2026-10-27: back from Step1, Step0's Min/Max/Gamma had changed
(Step1's restored windows overwrote them). User ruling: Step0 keeps its own;
Step1/2/3/Regions share one set. docs/v17_step0_own_ranges_plan.md.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")

from block01.core.display_identity import STEP0_TABLE  # noqa: E402
from block01.utils import run_store as rs  # noqa: E402

from test_block01_tissue_preview_contract import (  # noqa: E402,F401
    _goto, _pump, _thumb, _window, app)


def _show(w, step, channel="CD3"):
    _goto(w, step)
    w._display.state.set_display_visible(channel, True, origin="walk-test")
    _pump(w)


def test_step0s_windows_and_picture_survive_a_step1_round_trip(app):
    w = _window(app)
    try:
        state = w._display.state
        _show(w, 0)
        lo, hi, _g = w._step0.display_window("CD3")
        w._step0.set_display_mapping("CD3", lo, lo + (hi - lo) * 0.4, 0.8)
        _pump(w)
        mine = state.mapping("CD3", table=STEP0_TABLE)
        before = np.array(_thumb(w), copy=True)

        _show(w, 1)
        # Step1 comes back with ITS saved windows (Q2a) and is edited there.
        w._step0.adopt_intensity({"CD3": {"min": lo, "max": hi * 2.0}},
                                 origin="Step1's saved fusion settings")
        state.set_mapping("CD3", lo, hi * 1.5, 1.3, origin="step1")
        _pump(w)
        assert state.mapping("CD3")[:2] == (lo, hi * 1.5)

        _goto(w, 0)
        assert state.mapping("CD3", table=STEP0_TABLE) == mine
        assert w._step0.display_window("CD3") == mine
        assert np.array_equal(np.asarray(_thumb(w)), before), \
            "Step0's Tissue Preview changed with Step1's windows"
    finally:
        w.close()


def test_a_step0_window_moves_nothing_step1_fuses_with(app):
    w = _window(app)
    try:
        state = w._display.state
        _show(w, 1)
        state.set_mapping("CD3", 3.0, 300.0, 1.0, origin="step1")
        cfg = {"nucleus": {"channel": "DAPI", "weight": 1.0},
               "groups": {"g": {"group_weight": 1.0, "channels": {"CD3": 1.0}}},
               "channel_remap_params": {}}
        draft = w._display_mapping(channels=["CD3"])
        identity = w._display_mapping_identity(cfg)

        _goto(w, 0)
        w._step0.set_display_mapping("CD3", 50.0, 60.0, 2.0)
        _goto(w, 1)

        assert w._display_mapping(channels=["CD3"]) == draft
        assert w._display_mapping_identity(cfg) == identity
        assert (draft["CD3"]["min"], draft["CD3"]["max"]) == (3.0, 300.0)
    finally:
        w.close()


def test_step1_step2_and_step3_share_one_set(app):
    w = _window(app)
    try:
        state = w._display.state
        _show(w, 1)
        state.set_mapping("CD3", 4.0, 44.0, 1.1, origin="step1")
        for step in (2, 3, 1):
            _goto(w, step)
            assert state.mapping("CD3") == (4.0, 44.0, 1.1), step
        assert state.mapping("CD3", table=STEP0_TABLE) != (4.0, 44.0, 1.1)
    finally:
        w.close()


def test_only_step1s_own_windows_drop_its_remapped_pixels(app, monkeypatch):
    w = _window(app)
    try:
        dropped = []
        monkeypatch.setattr(w._overlay_display_cache, "drop_channel",
                            dropped.append)
        _show(w, 0)
        w._step0.display_window("CD3")         # first windows: a seed fills
        _pump(w)                               # BOTH tables, Step1's included
        dropped.clear()
        w._step0.set_display_mapping("CD3", 1.0, 2.0, 1.0)
        _pump(w)
        assert dropped == []
        w._display.state.set_mapping("CD3", 1.0, 3.0, 1.0, origin="step1")
        assert dropped == ["CD3"]
    finally:
        w.close()


def test_the_shared_record_is_written_while_step1s_restore_is_still_pending(app, tmp_path):
    """Reopen -> Regions -> edit -> close: Step1's own draft may not be
    written yet (its restore waits for Step1), the shared windows are."""
    w = _window(app)
    try:
        ws = tmp_path / "proj" / "rois" / "ws"
        ws.mkdir(parents=True)
        (ws / "roi_manifest.json").write_text("{}")
        sess = rs.load_session(str(ws))
        sess["editing"] = {"correct_run": "rois/ws/runs/correct_a",
                           "geometry_revision": 0}
        rs.save_session(str(ws), sess)
        w._step0._roi_context = {"roi_dir": str(ws)}
        w._rm_auto_step1 = True
        w._display.state.set_mapping("CD3", 6.0, 66.0, 1.0, origin="regions")

        w._rm_save_view(5)

        record = rs.load_session(str(ws))["drafts"]["shared_windows"]
        assert (record["params"]["CD3"]["min"], record["params"]["CD3"]["max"]) == (6.0, 66.0)
        assert record["user_adjusted"] == ["CD3"]
    finally:
        w.close()
