"""Cancelling a whole-slide correction stops it, and the progress dialog
does not freeze the rest of the application.

Reported from manual testing: Cancel on the Save progress dialog did
nothing, and the Tissue Preview window could not be closed while the run
went on. Two causes, one claim each:

  1. the worker checked its cancel flag only BETWEEN channels; in Full-WSI
     mode one channel is the whole slide. It is now checked per tile, and
     what a cancel leaves behind is defined: a fresh save drops the zarr, an
     incremental save drops only the partial dataset;
  2. the dialog was application-modal, which blocks every other window of
     the application. It is shown non-modally; the GPU entry points the
     user could now reach during a run are gated on
     `production_correction_busy()` instead.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")
zarr = pytest.importorskip("zarr")

from PyQt5 import QtCore, QtTest, QtWidgets  # noqa: E402

from block01.ui.step0 import search_ctrl as sc  # noqa: E402
from block01.ui.step0 import step0_page as sp  # noqa: E402

from test_step0_background_correction_tab import _GpuPathLoader, app  # noqa: E402,F401


# ── 1. the worker cancels at a tile boundary ─────────────────────────────

class _Loader:
    """Two channels, an ROI tall enough for several 4096-row tiles, and a
    hook that flips the worker's cancel flag after `cancel_after` reads."""

    def __init__(self, cancel_after=None):
        self.shape = (9000, 64)
        self.ch_map = {"DAPI": 0, "CD3": 1, "CD20": 2}
        self.filepath = "/fake/slide.ome.tif"
        self.reads = 0
        self.cancel_after = cancel_after
        self.worker = None

    def _read_roi_zarr(self, page_idx, y0, y1, x0, x1):
        self.reads += 1
        if self.cancel_after is not None and self.reads == self.cancel_after:
            self.worker.stop_after_current_channel()
        return np.full((y1 - y0, x1 - x0), 10.0 + page_idx, np.float32)


def _config(channels):
    return {"channel_decisions": {ch: "tophat" for ch in channels},
            "method_params": {"tophat_radius": 2, "cucim_sigma": 5},
            "channel_params": {}}


def _roi():
    return {"name": "ROI_1", "bbox_fullres": [0, 9000, 0, 64],
            "polygon_fullres": None, "shape": [9000, 64]}


def _run(worker):
    got = {"finished": [], "canceled": [], "error": []}
    worker.finished.connect(lambda p, d: got["finished"].append((p, d)))
    worker.canceled.connect(lambda p: got["canceled"].append(p))
    worker.error.connect(lambda m: got["error"].append(m))
    worker.run()                      # synchronously, on this thread
    return got


def test_a_cancel_mid_channel_stops_at_the_next_tile(tmp_path):
    loader = _Loader(cancel_after=1)
    worker = sc.WsiCorrectionWorker(loader, str(tmp_path), _config(["CD3"]),
                                    rois=[_roi()])
    loader.worker = worker

    got = _run(worker)

    assert got["canceled"] and not got["finished"] and not got["error"]
    # 9000 rows at 4096 -> 3 tiles; cancelled after the first read, so the
    # second tile was never read, let alone the third.
    assert loader.reads == 1
    # Fresh save: nothing is left on disk.
    assert not os.path.exists(os.path.join(str(tmp_path), "corrected_channels.zarr"))


def test_an_incremental_cancel_keeps_prior_channels_and_drops_the_partial_one(tmp_path):
    zarr_path = os.path.join(str(tmp_path), "corrected_channels.zarr")
    # A previous, complete save of CD3.
    first = sc.WsiCorrectionWorker(_Loader(), str(tmp_path), _config(["CD3"]),
                                   rois=[_roi()])
    got = _run(first)
    assert got["finished"] and os.path.isdir(zarr_path)
    before = np.asarray(zarr.open(zarr_path, mode="r")["ROI_1"]["CD3"][:])

    loader = _Loader(cancel_after=1)
    worker = sc.WsiCorrectionWorker(loader, str(tmp_path), _config(["CD3", "CD20"]),
                                    rois=[_roi()], process_channels={"CD20"},
                                    incremental=True)
    loader.worker = worker
    got = _run(worker)

    assert got["canceled"] and not got["finished"]
    root = zarr.open(zarr_path, mode="r")
    assert "CD3" in root["ROI_1"], "the complete prior channel must survive"
    assert np.array_equal(np.asarray(root["ROI_1"]["CD3"][:]), before)
    assert "CD20" not in root["ROI_1"], "the half-written channel must not"


def test_a_cancel_after_the_last_tile_still_cancels_cleanly(tmp_path):
    loader = _Loader(cancel_after=3)          # flag set during the LAST tile
    worker = sc.WsiCorrectionWorker(loader, str(tmp_path), _config(["CD3"]),
                                    rois=[_roi()])
    loader.worker = worker

    got = _run(worker)

    assert got["canceled"] and not got["finished"]
    assert not os.path.exists(os.path.join(str(tmp_path), "corrected_channels.zarr"))


def test_without_a_cancel_the_run_finishes(tmp_path):
    loader = _Loader()
    worker = sc.WsiCorrectionWorker(loader, str(tmp_path), _config(["CD3"]),
                                    rois=[_roi()])
    got = _run(worker)
    assert got["finished"] and not got["canceled"]
    assert loader.reads == 3


# ── 2. the dialog does not freeze the application ────────────────────────

def test_the_progress_dialog_is_not_modal_but_still_refuses_to_close(app):
    dlg = sc._WsiCorrectionProgressDialog()
    try:
        assert dlg.isModal() is False
        assert dlg.windowModality() == QtCore.Qt.NonModal

        dlg.show()
        dlg.reject()                       # X / Escape while running
        assert dlg.isVisible(), "closed while the run was going"

        seen = []
        dlg.cancel_requested.connect(lambda: seen.append(1))
        dlg._cancel.click()
        assert seen == [1]
        assert "tile" in dlg._label.text().lower()
        assert dlg._cancel.isEnabled() is False

        dlg.allow_close()
        dlg.reject()
        assert dlg.isVisible() is False
    finally:
        dlg.close()
        dlg.deleteLater()


def test_the_save_path_shows_the_dialog_instead_of_running_a_modal_loop(
        app, monkeypatch, tmp_path):
    from test_step0_full_image_recovery import _RecordingTab, _drive_real_save

    tab = _RecordingTab(released=True)
    page, timeline = _drive_real_save(app, monkeypatch, tmp_path, tab)
    worker = page._wsi_worker
    try:
        assert "dialog.show" in timeline
        assert "dialog.exec_" not in timeline
    finally:
        worker.may_exit.set()
        worker.wait(5000)
        QtTest.QTest.qWait(50)


# ── 3. what the user can now reach during a Save is gated ────────────────

def _page(app):
    page = sp.Step0Page()
    page.loader = _GpuPathLoader()
    page.patches = [(0, 32, 0, 32)]
    page.current_patch_idx = 0
    page.nucleus_channel = "DAPI"
    page._rebuild_channel_list()
    page.current_channel = "CD3"
    return page


def test_the_gpu_entry_points_refuse_while_a_save_runs(app, monkeypatch):
    page = _page(app)
    monkeypatch.setattr(page, "production_correction_busy",
                        lambda: "whole-slide correction (Save)")
    boxes = []
    monkeypatch.setattr(sp.QMessageBox, "information",
                        staticmethod(lambda *a, **k: boxes.append(a[1])))
    started = []
    monkeypatch.setattr(sp, "BatchProcessWorker",
                        lambda *a, **k: started.append(1) or _NeverStarts())
    row = page._channel_rows.get("CD3")
    row["checkbox"].setChecked(True)

    page._process_current_channel()            # Apply
    page._on_process_clicked()                 # ▶ Process
    page._start_ondemand("CD3")                # clicking an uncomputed channel

    assert started == [], "a GPU run started during a Save"
    assert boxes == ["Busy", "Busy"]
    assert "running" in page._preview_status.text().lower()


class _NeverStarts:
    def __getattr__(self, _name):
        raise AssertionError("worker must not be used while a Save runs")
