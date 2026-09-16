"""Step2 and Step3 open on what Step1 COMMITTED, then keep their own ticks.

User ruling, 2026-09-16. All four steps keep their own display answers, and
the two downstream ones are seeded rather than left empty: opening Step2 or
Step3 for the first time -- and the first time after Step1 commits again --
ticks exactly the channels the committed snapshot enabled.

What that deliberately excludes: Step0's ticks (a viewing decision), and
Step1's UNCOMMITTED draft (not yet what any job would run on). What it
includes: a channel enabled at an explicit 0.0, because the zero is a weight
and not a withdrawal. What it leaves out: a channel disabled with a weight
still in its history.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import json
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    for name in ("information", "critical", "warning"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name,
                            staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.Yes))


class _Loader:
    shape = (128, 128)
    name_map = {}
    correction_config = {}

    def __init__(self, path="/tmp/downstream.ome.tiff"):
        self.filepath = path
        self._names = ["DAPI", "CD3", "CD8", "CD20"]
        self.ch_map = {c: i for i, c in enumerate(self._names)}

    def channel_names(self):
        return list(self._names)

    def read_region(self, channel, y0, y1, x0, x1, downsample=1,
                    normalize=True):
        return np.zeros(((y1 - y0) or 1, (x1 - x0) or 1), np.float32)


def _publish_manifest(tmp_path, loader):
    path = tmp_path / "step0_roi_result.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"handoff_schema_version": 2,
                   "channel_remap_config_hash": "remap-1",
                   "active_roi": "ROI_1",
                   "source_identity": {"dataset_path": loader.filepath,
                                       "stage": "raw"}}, f)
    return path


def _window(app, tmp_path):
    from block01.ui.main_window import MainWindow
    w = MainWindow()
    w._schedule_step1_session_save = lambda: None
    w._save_step1_session = lambda *a, **k: None
    w.loader = _Loader()
    # The channel universe reaches the shared state through Step0's list, as
    # it does in the product; without it `channel_order()` is empty and a test
    # that reads ticks reads nothing.
    w._step0.loader = w.loader
    w._step0.nucleus_channel = "DAPI"
    w._step0._rebuild_channel_list()
    w.config.set_channels(w.loader.channel_names())
    w.config.load_panel({"markers": {"CD3": 0.0, "CD8": 0.0, "CD20": 0.0}},
                        "DAPI")
    w.config.set_nucleus("DAPI", 1.0)
    _publish_manifest(tmp_path, w.loader)
    w.step0_output = {
        "step1_dir": str(tmp_path), "output_dir": str(tmp_path),
        "step0_manifest_path": str(tmp_path / "step0_roi_result.json"),
        "channel_remap_config_hash": "remap-1",
        "source_identity": {"dataset_path": w.loader.filepath,
                            "stage": "raw"}}
    return w


def _close(w):
    w._display.shutdown("test")
    w.deleteLater()


def _in(w, step):
    w._set_step_active(step)
    QtWidgets.QApplication.processEvents()


def _enable(w, channel, on=True, weight=None):
    _in(w, 1)
    w._channel_dock.use_channel(channel, on, origin="test")
    if on and weight is not None:
        w._channel_dock.row(channel).spin.setValue(weight)
    QtWidgets.QApplication.processEvents()


def _commit(w):
    assert w._commit_fusion_settings() is True
    QtWidgets.QApplication.processEvents()


def _ticks(w, step):
    _in(w, step)
    visibility = w._display.state.display_visibility()
    return {ch for ch, on in visibility.items() if on}


# ── 1. an uncommitted draft reaches nobody ────────────────────────────

def test_an_uncommitted_draft_does_not_reach_step2_or_step3(app, tmp_path):
    w = _window(app, tmp_path)
    try:
        _enable(w, "CD3", True)
        for step in (2, 3):
            assert "CD3" not in _ticks(w, step), step
    finally:
        _close(w)


# ── 2. the seed is the committed set ──────────────────────────────────

def test_step2_and_step3_open_on_the_committed_channels(app, tmp_path):
    w = _window(app, tmp_path)
    try:
        _enable(w, "CD3", True)
        _enable(w, "CD8", True)
        _commit(w)
        expected = w._committed_enabled_channels()
        assert {"CD3", "CD8"} <= expected

        for step in (2, 3):
            assert _ticks(w, step) == expected, step
    finally:
        _close(w)


def test_a_channel_committed_at_zero_is_still_ticked_downstream(app, tmp_path):
    """An explicit 0.0 is a weight, not a withdrawal."""
    w = _window(app, tmp_path)
    try:
        _enable(w, "CD3", True, weight=0.0)
        assert w._display.fusion.fusion_enabled("CD3") is True
        _commit(w)
        assert "CD3" in _ticks(w, 2)
        assert "CD3" in _ticks(w, 3)
    finally:
        _close(w)


def test_a_disabled_channel_with_a_weight_in_hand_is_not_ticked(app, tmp_path):
    w = _window(app, tmp_path)
    try:
        _enable(w, "CD20", True, weight=0.7)
        _enable(w, "CD20", False)
        assert w._display.fusion.channel_weight("CD20") == pytest.approx(0.7)
        assert w._display.fusion.fusion_enabled("CD20") is False
        _commit(w)
        assert "CD20" not in _ticks(w, 2)
        assert "CD20" not in _ticks(w, 3)
    finally:
        _close(w)


def test_step0_s_ticks_are_not_what_seeds_the_downstream_steps(app, tmp_path):
    w = _window(app, tmp_path)
    try:
        _enable(w, "CD3", True)
        _commit(w)
        _in(w, 0)
        w._display.state.set_display_visible("CD20", True, origin="step0-test")
        QtWidgets.QApplication.processEvents()

        for step in (2, 3):
            ticks = _ticks(w, step)
            assert "CD3" in ticks, step
            assert "CD20" not in ticks, step
    finally:
        _close(w)


# ── 3. downstream edits stay downstream ───────────────────────────────

def test_a_step2_tick_writes_back_to_nobody(app, tmp_path):
    w = _window(app, tmp_path)
    try:
        _enable(w, "CD3", True)
        _commit(w)
        _ticks(w, 2)                       # seeds Step2
        _ticks(w, 3)                       # seeds Step3

        model = w._display.fusion
        draft = model.draft_snapshot()
        committed = model.committed_snapshot()
        step1_before = _ticks(w, 1)

        _in(w, 2)
        w._display.state.set_display_visible("CD20", True, origin="step2-test")
        QtWidgets.QApplication.processEvents()

        assert model.draft_snapshot() == draft
        assert model.committed_snapshot() == committed
        assert _ticks(w, 1) == step1_before
        assert "CD20" not in _ticks(w, 3)
        assert "CD20" in _ticks(w, 2)
    finally:
        _close(w)


def test_a_step3_tick_writes_back_to_nobody(app, tmp_path):
    w = _window(app, tmp_path)
    try:
        _enable(w, "CD3", True)
        _commit(w)
        _ticks(w, 2)
        _ticks(w, 3)
        step1_before = _ticks(w, 1)

        _in(w, 3)
        w._display.state.set_display_visible("CD8", True, origin="step3-test")
        QtWidgets.QApplication.processEvents()

        assert _ticks(w, 1) == step1_before
        assert "CD8" not in _ticks(w, 2)
        assert "CD8" in _ticks(w, 3)
    finally:
        _close(w)


# ── 4. a new commit re-seeds, once ────────────────────────────────────

def test_a_new_commit_re_seeds_the_downstream_steps_on_next_entry(app,
                                                                  tmp_path):
    w = _window(app, tmp_path)
    try:
        _enable(w, "CD3", True)
        _commit(w)
        assert _ticks(w, 2) == w._committed_enabled_channels()

        _in(w, 2)
        w._display.state.set_display_visible("CD20", True, origin="step2-test")
        QtWidgets.QApplication.processEvents()
        assert "CD20" in _ticks(w, 2)

        _enable(w, "CD8", True)
        _commit(w)
        expected = w._committed_enabled_channels()
        assert "CD8" in expected

        assert _ticks(w, 2) == expected, "Step2 did not follow the new commit"
        assert _ticks(w, 3) == expected
    finally:
        _close(w)


def test_re_entering_without_a_new_commit_keeps_the_step_s_own_ticks(app,
                                                                     tmp_path):
    """Seeding is once per commit, not once per visit."""
    w = _window(app, tmp_path)
    try:
        _enable(w, "CD3", True)
        _commit(w)
        _ticks(w, 2)

        _in(w, 2)
        w._display.state.set_display_visible("CD20", True, origin="step2-test")
        w._display.state.set_display_visible("CD3", False, origin="step2-test")
        QtWidgets.QApplication.processEvents()
        mine = _ticks(w, 2)

        _in(w, 1)
        _in(w, 3)
        assert _ticks(w, 2) == mine
    finally:
        _close(w)
