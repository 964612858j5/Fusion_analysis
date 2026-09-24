"""Run / Stop and the Results list in the Pre-segmentation tab (block C, step 4).

On a window with a published Step0 handoff (synthetic project under
tmp_path): Run refuses without saved Fusion settings or with a nucleus
weight of 0; more than 10 tasks asks; a run goes through a real StarDist
engine process and fills the list; nothing is chosen until Use; Use unlocks
Save with the combination's own method and parameters; new Fusion settings
make the choice out of date and lock Save again; Stop cancels; a result
arriving in the old flow is never a choice. Own module: page-heavy PyQt
suites crash pyqtgraph offscreen when combined.
"""
import json
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("KERAS_BACKEND", "tensorflow")

import numpy as np
import pytest

pytest.importorskip("PyQt5")
from PyQt5 import QtWidgets  # noqa: E402

from block01.utils import segmentation_param_schema as ps  # noqa: E402

SD = "stardist_nuclei_dapi"
SNAP = {"hash": "fh1",
        "fusion_config": {"groups": {"T": {"channels": {"CD3": 1.0}, "group_weight": 1.0}},
                          "nucleus": {"channel": "DAPI", "weight": 1.0}},
        "display_mapping": {"DAPI": {"min": 0, "max": 255, "gamma": 1.0},
                            "CD3": {"min": 0, "max": 255, "gamma": 1.0}}}


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Slide:
    """What the run's own loader reads: synthetic nuclei and membrane."""

    def __init__(self, h=128, w=128):
        from block01.seg_runner import synthetic
        self.shape = (h, w)
        self.ch_map = {"DAPI": 0, "CD3": 1}
        self.data = {"DAPI": (synthetic.nuclei(h, w) * 255).astype(np.uint8),
                     "CD3": (synthetic.membrane(h, w) * 255).astype(np.uint8)}

    def read_region(self, ch, y0, y1, x0, x1, downsample=1, normalize=False):
        return self.data[ch][y0:y1, x0:x1]


def _window(app, tmp_path, monkeypatch, n_patches=1):
    """A window on a published handoff, full slide, saved Fusion settings SNAP."""
    from block01.ui import main_window as mw
    from block01.ui.main_window import MainWindow
    from block01.ui.step0.roi_context_model import Patch
    raw = tmp_path / "raw.ome.tif"
    raw.write_bytes(b"x" * 16)

    class _Loader:
        shape = (128, 128)
        ch_map = {"DAPI": 0, "CD3": 1}
        filepath = str(raw)
        name_map = {}
        correction_config = None

        def channel_names(self):
            return ["DAPI", "CD3"]

        def read_region(self, channel, y0, y1, x0, x1, downsample=1, **kw):
            ds = max(1, int(downsample))
            return np.zeros(((y1 - y0) // ds or 1, (x1 - x0) // ds or 1), np.float32)

    patches = [Patch((0, 48, 0, 48), 1), Patch((64, 128, 64, 128), 2),
               Patch((0, 32, 80, 128), 3)][:n_patches]
    w = MainWindow()
    page = w._step0
    page.loader = _Loader()
    page.ome_path, page.output_dir = str(raw), str(tmp_path)
    page.panel_csv_path, page.panel_groups, page.nucleus_channel = "", {}, "DAPI"
    page.overview.loader = page.loader
    page.overview.full_h, page.overview.full_w = 128, 128
    page.overview.full_wsi_mode = True
    page.overview.set_rois_and_patches([], patches)
    page._on_patches_changed(page.overview._patch_coords())
    step0_dir = tmp_path / "full" / "step0"
    step0_dir.mkdir(parents=True, exist_ok=True)
    page._roi_context = {
        "roi_id": "full", "roi_dir": str(tmp_path / "full"), "project_dir": str(tmp_path),
        "step_dirs": {"step0": str(step0_dir), "step1": str(tmp_path / "full" / "step1"),
                      "step2": str(tmp_path / "full" / "step2")}}
    page._roi_context_sig = page._roi_context_signature(page._standard_rois())
    page._roi_model.adopt(rois=[], patches=patches, full_wsi_mode=True,
                          loader=page.loader, nucleus_channel="DAPI")
    page._write_step0_handoff(
        {"method_params": {"tophat_radius": 25, "cucim_sigma": 30}, "channel_decisions": {}},
        str(step0_dir / "corrected_channels.zarr"))
    w.loader = _Loader()
    w.step0_output = {"step0_manifest_path": str(step0_dir / "step0_roi_result.json"),
                      "step0_dir": str(step0_dir), "step1_dir": str(tmp_path / "full" / "step1"),
                      "output_dir": str(step0_dir)}
    w.step0_done = w._step1_context_ready = True
    w._current_step = 1
    w._rois, w._active_roi = [], None
    w._on_patches(list(patches))
    snap = {"value": json.loads(json.dumps(SNAP))}
    w._committed_fusion_settings = lambda: snap["value"]
    w._test_snapshot = snap
    monkeypatch.setattr(mw, "open_loader", lambda spec: _Slide())
    return w


def _vals(method, **kw):
    v = ps.default_values(method)
    v.update(kw)
    return v


def _wait(w, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        if w._preseg_job is None and w._preseg_run is not None:
            break
        time.sleep(0.02)
    for _ in range(5):
        QtWidgets.QApplication.processEvents()
    assert w._preseg_job is None, "the run did not finish"


def _quiet(monkeypatch, answer=QtWidgets.QMessageBox.Yes):
    said = []
    for name in ("information", "warning"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name,
                            staticmethod(lambda *a, **k: said.append(a[2])))
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: (said.append(a[2]), answer)[1]))
    return said


# ── layout and the Run button ───────────────────────────────────────────────
def test_run_and_stop_sit_under_the_total_and_follow_the_task_count(app, tmp_path, monkeypatch):
    w = _window(app, tmp_path, monkeypatch)
    try:
        m = w._preseg_methods
        assert not m.btn_run.isEnabled() and not m.btn_stop.isEnabled()      # no method yet
        m.adopt(SD, _vals(SD))
        assert m.btn_run.isEnabled() and not m.btn_stop.isEnabled()
        w._preseg_patches.set_selected_ids([])
        assert not m.btn_run.isEnabled()                                      # no patch ticked
        lay = w.method_params_tab.layout()
        assert lay.itemAt(2).widget() is w._preseg_results.section_box
        tabs = w.method_params_tab.parentWidget().parentWidget()
        QtWidgets.QApplication.processEvents()
        shown = tabs.minimumSizeHint().width()
        w._preseg_results.set_combos([{"combo_id": "x", "method": "mesmer_nuclear_guided",
                                       "params": ps.combinations("mesmer_nuclear_guided",
                                                                 _vals("mesmer_nuclear_guided"))[0]}])
        w._preseg_results.show_state("x", "12/12 patches · 12345 cells · 3 failed", "warn", True)
        m.hide()
        w._preseg_results.hide()
        QtWidgets.QApplication.processEvents()
        assert tabs.minimumSizeHint().width() == shown                        # no wider column
    finally:
        w.close()


@pytest.mark.parametrize("snap_change,said", [
    (lambda s: None, "Save the Fusion settings first"),
    (lambda s: s["fusion_config"]["nucleus"].update(weight=0.0), "nucleus weight"),
])
def test_run_refuses_without_saved_settings_or_with_an_empty_nucleus(app, tmp_path, monkeypatch,
                                                                     snap_change, said):
    w = _window(app, tmp_path, monkeypatch)
    try:
        told = _quiet(monkeypatch)
        if said.startswith("Save"):
            w._test_snapshot["value"] = None
        else:
            snap_change(w._test_snapshot["value"])
        w._preseg_methods.adopt(SD, _vals(SD))
        assert w._on_preseg_run() is False
        assert said in told[-1] and w._preseg_job is None
    finally:
        w.close()


def test_more_than_ten_tasks_asks_first(app, tmp_path, monkeypatch):
    w = _window(app, tmp_path, monkeypatch, n_patches=3)
    try:
        told = _quiet(monkeypatch, answer=QtWidgets.QMessageBox.No)
        w._preseg_methods.adopt(SD, _vals(SD, prob_thresh=[0.3, 0.4, 0.5, 0.6]))   # 3 x 4
        assert w._on_preseg_run() is False and w._preseg_job is None
        assert "12 tasks" in told[-1]
    finally:
        w.close()


# ── a run, the list, Use and Save ───────────────────────────────────────────
def test_a_run_fills_the_list_and_nothing_is_chosen_until_use(app, tmp_path, monkeypatch):
    pytest.importorskip("stardist")
    w = _window(app, tmp_path, monkeypatch, n_patches=2)
    try:
        _quiet(monkeypatch)
        w._preseg_methods.adopt(SD, _vals(SD, prob_thresh=[0.4, 0.6]))
        assert w._on_preseg_run() is True
        assert not w._preseg_methods.btn_run.isEnabled() and w._preseg_methods.btn_stop.isEnabled()
        _wait(w)
        assert w._preseg_methods.btn_run.isEnabled() and not w._preseg_methods.btn_stop.isEnabled()
        assert w._preseg_methods.progress.text().startswith("Finished: 4/4 done · 4 ok")
        rows = w._preseg_results.rows()
        assert len(rows) == 2
        for row in rows:
            assert row.status.text().startswith("2/2 patches · ") and "cells" in row.status.text()
            assert row.btn_use.isEnabled() and row.btn_use.text() == "Use"
        assert w._p2_params is None and not w.btn_save.isEnabled()            # nothing chosen
        assert w._on_preseg_use(rows[1].combo_id) is True
        combo = w._preseg_run["combos"][1]
        assert w._p2_params["method"] == SD and w._p2_params["params"] == combo["params"]
        assert w._p2_params["fusion_settings_hash"] == "fh1"
        assert w._params_source == "preseg_selected" and w.btn_save.isEnabled()
        assert rows[1].btn_use.text() == "In use" and rows[0].btn_use.text() == "Use"
        assert w._preseg_results.summary.text().startswith("In use: ")
        # new Fusion settings: the choice is out of date, Save is locked again
        w._test_snapshot["value"] = dict(w._test_snapshot["value"], hash="fh2")
        w._refresh_preseg_results()
        assert w._p2_params is None and not w.btn_save.isEnabled()
        assert all("out of date" in r.status.text() and not r.btn_use.isEnabled() for r in rows)
        # the saved result files are where the records say
        rdir = os.path.join(w._preseg_step1_dir(), "presegmentation_runs", w._preseg_run["run_id"])
        assert len(os.listdir(os.path.join(rdir, "records"))) == 4
    finally:
        w.close()


def test_save_refuses_a_choice_whose_files_are_gone(app, tmp_path, monkeypatch):
    pytest.importorskip("stardist")
    w = _window(app, tmp_path, monkeypatch)
    try:
        told = _quiet(monkeypatch)
        w._preseg_methods.adopt(SD, _vals(SD))
        w._on_preseg_run()
        _wait(w)
        assert w._on_preseg_use(w._preseg_run["combos"][0]["combo_id"]) is True
        rec = next(iter(w._preseg_records.values()))
        os.remove(rec["nucleus"]["path"])
        w._save()
        assert "can no longer be used: a result file is missing" in told[-1]
        assert w._p2_params is None and not w.btn_save.isEnabled()
    finally:
        w.close()


def test_stop_cancels_and_run_comes_back(app, tmp_path, monkeypatch):
    pytest.importorskip("stardist")
    w = _window(app, tmp_path, monkeypatch, n_patches=2)
    try:
        _quiet(monkeypatch)
        w._preseg_methods.adopt(SD, _vals(SD, prob_thresh=[0.3, 0.5]))
        w._on_preseg_run()
        w._on_preseg_stop()
        _wait(w)
        states = [r["status"] for r in w._preseg_records.values()]
        assert len(states) == 4 and "cancelled" in states and "ok" not in states[1:]
        assert w._preseg_methods.btn_run.isEnabled()
        for row in w._preseg_results.rows():
            assert "cancelled" in row.status.text() and not row.btn_use.isEnabled()
    finally:
        w.close()


def test_closing_the_window_ends_the_run(app, tmp_path, monkeypatch):
    pytest.importorskip("stardist")
    w = _window(app, tmp_path, monkeypatch)
    _quiet(monkeypatch)
    w._preseg_methods.adopt(SD, _vals(SD))
    w._on_preseg_run()
    job = w._preseg_job
    w.close()
    QtWidgets.QApplication.processEvents()
    assert not job.is_running() and job._engine is None


# ── the old flow: arrival is never a choice ─────────────────────────────────
def test_an_arriving_result_does_not_become_the_saved_params(app, tmp_path, monkeypatch):
    w = _window(app, tmp_path, monkeypatch)
    try:
        params = {"method": "cellpose_wholecell_fusion", "diameter": 30.0, "_phase": 2}
        assert w._record_segmentation_preview_result(0, params, np.ones((4, 4), np.uint32)) is False
        assert w._p2_params is None and not w.btn_save.isEnabled()
        picked = []
        monkeypatch.setattr(w.result_grid, "_select", lambda col: picked.append(col))
        monkeypatch.setattr(w.result_grid, "get_selected", lambda: None)
        w.result_grid._params = [dict(params)]
        w._auto_select_p1()
        assert picked == []                              # a Phase 2 column is not auto-picked
        w.result_grid._params = [dict(params, _phase=1)]
        w._auto_select_p1()
        assert picked == [0]                             # Phase 1 still hands on its diameter
    finally:
        w.close()


# ── saving says it worked (user ruling, 2026-09-24) ─────────────────────────
def test_save_plan_and_save_fusion_settings_say_they_worked(app, tmp_path, monkeypatch):
    w = _window(app, tmp_path, monkeypatch)
    try:
        told = _quiet(monkeypatch)
        w._preseg_methods.adopt(SD, _vals(SD))
        w._preseg_methods.btn_save.click()
        assert told[-1].startswith("Plan saved: 1 method, 1 patch.") and "plan_" in told[-1]
        monkeypatch.setattr(w, "_on_save_preseg_plan",
                            lambda: (_ for _ in ()).throw(OSError("disk full")))
        w._preseg_methods.btn_save.click()
        assert "could not be saved" in told[-1] and "disk full" in told[-1]
        n = len(told)
        monkeypatch.setattr(w, "_commit_fusion_settings", lambda *a: False)
        w._btn_save_fusion_settings.click()
        assert len(told) == n                              # a failure speaks for itself
        monkeypatch.setattr(w, "_commit_fusion_settings", lambda *a: True)
        w._btn_save_fusion_settings.setEnabled(True)
        w._btn_save_fusion_settings.click()
        assert told[-1].startswith("Fusion settings saved.")
    finally:
        w.close()
