"""Block B3: three problems found in testing (user report 2026-09-27).

  1. Step1's Save progress: `FullFusionWorker` reports progress per TILE --
     a one-ROI `2 x 2` run climbs 25 / 50 / 75 / 100 %, two ROIs likewise,
     never down; `Save Fusion Settings` is shown under the Fusion tab only,
     its place kept while Pre-segmentation is up;
  2. Step3 as a general result viewer: the runs of every ROI workspace of the
     project made on the open slide (the current one first, labelled by
     workspace), each on its own region; `Load…` brings in a run from another
     project made on this slide and selects it; a result of another slide is
     refused with the reason; a region outside the current ROI is said;
  3. coming back to Step1 asks for the Pre-seg Results pictures again.

Synthetic data and projects in the test's temporary directory only.
"""

import importlib.util
import json
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")
pytest.importorskip("pyqtgraph")

from PyQt5 import QtWidgets  # noqa: E402

from block01.core import step3_masks as sm  # noqa: E402

import test_step1_viewer_mount as vm  # noqa: E402
import test_step3_viewer as sv  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "_masks_helpers", os.path.join(os.path.dirname(__file__), "test_step3_masks.py"))
mh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mh)
_spec2 = importlib.util.spec_from_file_location(
    "_publication", os.path.join(os.path.dirname(__file__), "test_step1_result_publication.py"))
pub = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(pub)

SHAPES = [(vm.SLIDE, vm.SLIDE), (vm.SLIDE // 4, vm.SLIDE // 4)]
BBOX = tuple(vm.ROI)
SLIDE = "/x/c4.ome.tif"                      # the rig's loader.filepath


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    for name in ("information", "critical", "warning"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name, staticmethod(lambda *a, **k: None))


# ── 1. Step1's Save ──────────────────────────────────────────────────────

def _percents(worker):
    seen = []
    worker.progress.connect(lambda d, t, m: seen.append(int(d / t * 100) if t else 0))
    worker.run()
    return seen


def _units(worker, chunk, wide):
    """Block PA-3: the work is cut into chunk-aligned units, not the asked-for
    grid; small chunks give this tiny slide several of them."""
    worker.zarr_chunk, worker.UNIT_CHUNKS_WIDE = chunk, wide
    return worker


def test_one_roi_two_by_two_climbs_per_tile(app, tmp_path):
    seen = _percents(_units(pub._worker(tmp_path, n_rows=2, n_cols=2), 8, 1))
    assert seen == sorted(seen) and seen[-1] == 100
    assert {25, 50, 75} <= set(seen)


def test_two_rois_climb_per_tile_too(app, tmp_path):
    worker = _units(pub._worker(tmp_path, n_rows=2, n_cols=2), 4, 2)
    worker.rois = [{"name": "A", "bbox_fullres": [0, 8, 0, 16], "polygon_fullres": None},
                   {"name": "B", "bbox_fullres": [8, 16, 0, 16], "polygon_fullres": None}]
    seen = _percents(worker)
    assert seen == sorted(seen) and seen[-1] == 100
    assert {12, 25, 37, 50, 62, 75, 87} <= set(seen)


def test_save_fusion_settings_belongs_to_the_fusion_tab(app, tmp_path):
    w = sv.ds._window(app, tmp_path)
    try:
        btn, tabs = w._btn_save_fusion_settings, w._step1_left_tabs
        assert btn.sizePolicy().retainSizeWhenHidden()
        tabs.setCurrentIndex(tabs.indexOf(tabs.widget(1)))
        assert tabs.tabText(tabs.currentIndex()) == "Pre-segmentation" and btn.isHidden()
        tabs.setCurrentIndex(0)
        assert not btn.isHidden()
    finally:
        w.close()


# ── 2. Step3, a general result viewer ────────────────────────────────────

def _workspace(root, roi_id, created, slide=SLIDE, name="Full WSI"):
    rdir = mh._workspace(root, roi_id=roi_id, bbox=BBOX)
    manifest = json.load(open(os.path.join(rdir, "roi_manifest.json")))
    manifest.update({"display_name": name, "created_at": created, "source_ome": slide})
    json.dump(manifest, open(os.path.join(rdir, "roi_manifest.json"), "w"))
    return rdir


def _run(rdir, run_id, created, roi_id, **kw):
    return mh._run(rdir, run_id, "cellpose_wholecell_fusion", bbox=BBOX, shapes=SHAPES,
                   created_at=created, roi_id=roi_id, **kw)


def test_every_workspace_of_the_slide_is_listed_current_first(app, tmp_path):
    cur = _workspace(tmp_path / "p", "roi_now", "2026-09-27T10:00:00")
    old = _workspace(tmp_path / "p", "roi_old", "2026-09-26T09:00:00")
    other = _workspace(tmp_path / "p", "roi_else", "2026-09-26T08:00:00", slide="/y/other.tif")
    _run(cur, "seg_c", "2026-09-27T10:05:00", "roi_now")
    _run(old, "seg_o1", "2026-09-26T09:05:00", "roi_old")
    _run(old, "seg_o2", "2026-09-26T09:30:00", "roi_old")
    _run(other, "seg_x", "2026-09-26T08:05:00", "roi_else")
    runs = sm.list_project_runs(cur, SLIDE)
    assert [r.run_id for r in runs] == ["seg_c", "seg_o2", "seg_o1"]     # other slide left out
    assert runs[1].workspace_label == "Full WSI · 2026-09-26 09:00"
    items = sm.entries(runs)
    assert all(e.bbox == BBOX for e in items)
    pick = sm.choose_entry(items, workspace=cur)
    assert pick.run.run_id == "seg_c"


def _rig_with(app, tmp_path, monkeypatch, cur):
    rig = sv._rig(app, tmp_path, monkeypatch)
    rig.w.step0_output["roi_dir"] = cur
    return rig


def test_step3_lists_earlier_sessions_and_loads_another_project(app, tmp_path, monkeypatch):
    cur = _workspace(tmp_path / "p", "roi_now", "2026-09-27T10:00:00")
    old = _workspace(tmp_path / "p", "roi_old", "2026-09-26T09:00:00")
    _run(old, "seg_o1", "2026-09-26T09:05:00", "roi_old")               # nothing in the current one
    far = _workspace(tmp_path / "q", "roi_far", "2026-09-20T09:00:00")
    far_run = _run(far, "seg_far", "2026-09-20T09:05:00", "roi_far")
    alien = _workspace(tmp_path / "r", "roi_alien", "2026-09-20T09:00:00", slide="/y/other.tif")
    alien_run = _run(alien, "seg_alien", "2026-09-20T09:05:00", "roi_alien")
    rig = _rig_with(app, tmp_path, monkeypatch, cur)
    w, bar = rig.w, rig.w._step3_mask_bar
    try:
        sv._enter(rig, 3)
        assert bar.run_combo.count() == 1 and "[Full WSI · 2026-09-26 09:00]" in bar.run_combo.itemText(0)
        assert w._step3_mount.mask_sources()["cell"] is not None
        # Load… another project's run of this slide
        w._step3_pick_run_folder = lambda: far_run.dir
        bar.load_button.click()
        assert bar.run_combo.count() == 2
        assert bar.current_run_dir() == os.path.realpath(far_run.dir)
        assert w._step3_mount.mask_sources()["cell"].mask_path == os.path.realpath(far_run.mask)
        # Load… a run of another slide: refused, said, list unchanged
        w._step3_pick_run_folder = lambda: alien_run.dir
        bar.load_button.click()
        assert bar.run_combo.count() == 2
        assert bar.hint.full_text().startswith("Not loaded: this result was made on another slide")
        # Load… a folder that is no run
        w._step3_pick_run_folder = lambda: str(tmp_path)
        bar.load_button.click()
        assert bar.hint.full_text().startswith("Not loaded: no segmentation_meta.json")
        # choosing again clears the refusal
        bar.run_chosen.emit(bar.run_combo.itemData(0))
        assert not bar.hint.full_text().startswith("Not loaded")
    finally:
        w.close()


def test_a_region_outside_the_current_roi_is_said(app, tmp_path, monkeypatch):
    cur = _workspace(tmp_path / "p", "roi_now", "2026-09-27T10:00:00")
    _run(cur, "seg_c", "2026-09-27T10:05:00", "roi_now")
    rig = _rig_with(app, tmp_path, monkeypatch, cur)
    w, bar = rig.w, rig.w._step3_mask_bar
    try:
        w._active_roi = {"name": "ROI_1", "bbox_fullres": [600, 900, 600, 900]}   # smaller
        sv._enter(rig, 3)
        assert w._step3_mount.mask_sources()["cell"] is not None
        assert w._step3_mask_note.startswith("Part of this result lies outside the current ROI")
    finally:
        w.close()


# ── 3. back in Step1, the montage draws again ────────────────────────────

def test_coming_back_to_step1_asks_for_the_montage_again(app, tmp_path, monkeypatch):
    rig = sv._rig(app, tmp_path, monkeypatch)
    w = rig.w
    try:
        calls = []
        monkeypatch.setattr(w, "_request_montage_images", lambda *a, **k: calls.append(1))
        sv._enter(rig, 1)
        sv._enter(rig, 2)
        calls.clear()
        sv._enter(rig, 1)
        assert calls, "entering Step1 must ask for the montage's pictures"
    finally:
        w.close()
