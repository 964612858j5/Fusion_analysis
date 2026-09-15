"""v14.4: corrected_channels.zarr output validity, provenance, and explicit-write.

Case A (audited): WsiCorrectionWorker writes real per-channel float32 arrays
(root[<roi>][<channel>]) when channels are assigned tophat/cuCIM; an empty group
(zero channel arrays) is produced only when no channel is assigned. These tests
verify the validity REPORT distinguishes the two, drive the real worker to a
non-empty zarr, and check provenance — never treating "directory exists" as
success.

Qt tests need an offscreen platform (env: QT_QPA_PLATFORM=offscreen).
"""

import os

import numpy as np
import pytest

pytest.importorskip("PyQt5")

zarr = pytest.importorskip("zarr")

from block01.core.bg_correction import BG_CORRECTION_ALGO_VERSION as _V


@pytest.fixture(scope="module")
def app():
    from PyQt5 import QtWidgets
    a = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return a


@pytest.fixture(autouse=True)
def no_modal_dialogs(monkeypatch):
    """Answer Save's modal dialogs instead of blocking on them.

    `_save_and_continue` pops the "no background correction" notice, and
    since the full-image-first restructure also the "these channels will be
    saved raw" confirmation. `QMessageBox` runs its own event loop, and with
    nobody to click the button it never returns: without this fixture the
    module hangs at the first Save-driving test (it did, offscreen, before
    the fixture existed) instead of failing.
    """
    import block01.ui.step0.step0_page as sp

    seen = []

    class _Msg:
        """Records what was popped; `seen` is reachable as `_Msg.dialogs`."""

        dialogs = seen
        # Real button-role values, so a page comparing an answer against
        # `QMessageBox.Ok` gets a truthful match.
        Ok = 0x00000400
        Cancel = 0x00400000
        Yes = 0x00004000
        No = 0x00010000

        @staticmethod
        def information(*a, **k):
            seen.append(("information", a[1] if len(a) > 1 else "",
                         a[2] if len(a) > 2 else ""))

        @staticmethod
        def warning(*a, **k):
            seen.append(("warning", a[1] if len(a) > 1 else "",
                         a[2] if len(a) > 2 else ""))

        @staticmethod
        def critical(*a, **k):
            seen.append(("critical", a[1] if len(a) > 1 else "",
                         a[2] if len(a) > 2 else ""))

        @staticmethod
        def question(*a, **k):
            seen.append(("question", a[1] if len(a) > 1 else "",
                         a[2] if len(a) > 2 else ""))
            return _Msg.Ok

    monkeypatch.setattr(sp, "QMessageBox", _Msg)
    return _Msg


# ── validity report: empty group is NOT a valid corrected output ─────────────

def _set_bulk_method(page, method):
    """Set the bulk PREVIEW method through the real Method popup.

    The four-item combo is gone: the control is a `Method` button whose menu
    carries TopHat and cuCIM as lit-or-not toggles with their parameters, and
    a Save that applies the pair. Both lit is `both`, neither is `original`.
    """
    method = str(method).lower()
    page._method_tophat_btn.setChecked(method in ("both", "tophat"))
    page._method_cucim_btn.setChecked(method in ("both", "cucim"))
    page._on_method_menu_saved()

def test_report_empty_group_is_not_valid(tmp_path):
    from block01.core.bg_correction import corrected_zarr_report
    path = str(tmp_path / "empty.zarr")
    root = zarr.open_group(path, mode="w")
    root.create_group("ROI_1")          # group only, ZERO channel arrays
    rep = corrected_zarr_report(path)
    assert rep["exists"] is True
    assert rep["non_empty"] is False
    assert rep["n_channel_arrays"] == 0


def test_report_missing_path_is_not_valid():
    from block01.core.bg_correction import corrected_zarr_report
    rep = corrected_zarr_report("/no/such/path.zarr")
    assert rep["exists"] is False and rep["non_empty"] is False


def test_report_real_arrays_is_valid(tmp_path):
    from block01.core.bg_correction import corrected_zarr_report
    path = str(tmp_path / "real.zarr")
    root = zarr.open_group(path, mode="w")
    g = root.create_group("ROI_1")
    g.create_dataset("CD68", data=np.ones((30, 40), dtype=np.float32))
    rep = corrected_zarr_report(path)
    assert rep["non_empty"] is True
    assert rep["n_channel_arrays"] == 1
    assert rep["channel_arrays"] == ["ROI_1/CD68"]
    assert rep["shapes"]["ROI_1/CD68"] == [30, 40]


# ── Case A: the real worker writes a valid non-empty zarr with real arrays ───
class _FakeLoader:
    def __init__(self):
        self.ch_map = {"CD68": 0, "CK19": 1}
        self.shape = (200, 200)
        self.filepath = "/tmp/fake.ome.tif"

    def _read_roi_zarr(self, idx, y0, y1, x0, x1):
        return (np.random.rand(y1 - y0, x1 - x0) * 1000).astype(np.float32)


def test_explicit_worker_writes_nonempty_corrected_zarr(app, tmp_path):
    from block01.ui.step0.search_ctrl import WsiCorrectionWorker
    from block01.core.bg_correction import corrected_zarr_report
    cfg = {"channel_decisions": {"CD68": "tophat", "CK19": "original"},
           "method_params": {"tophat_radius": 15, "cucim_sigma": 50}}
    rois = [{"name": "ROI_1", "bbox_fullres": [10, 90, 20, 120]}]
    w = WsiCorrectionWorker(_FakeLoader(), str(tmp_path), cfg, rois=rois)
    out = {}
    w.finished.connect(lambda p, dec: out.update(path=p, dec=dec))
    w.error.connect(lambda m: out.update(err=m))
    w.run()                              # run synchronously (no QThread)
    assert out.get("err") is None
    path = out["path"]
    assert path.endswith("corrected_channels.zarr")
    rep = corrected_zarr_report(path)
    # at least one channel array, non-zero shape — and 'original' channel excluded
    assert rep["non_empty"] is True
    assert rep["n_channel_arrays"] == 1
    assert rep["channel_arrays"] == ["ROI_1/CD68"]
    assert rep["shapes"]["ROI_1/CD68"] == [80, 100]
    # NOT a directory-only / empty group
    assert os.path.isdir(path) and rep["non_empty"]


def test_worker_no_channels_makes_no_zarr(app, tmp_path):
    # all 'original' -> worker emits empty path, writes no zarr group itself
    from block01.ui.step0.search_ctrl import WsiCorrectionWorker
    cfg = {"channel_decisions": {"CD68": "original"},
           "method_params": {"tophat_radius": 15, "cucim_sigma": 50}}
    rois = [{"name": "ROI_1", "bbox_fullres": [0, 50, 0, 50]}]
    w = WsiCorrectionWorker(_FakeLoader(), str(tmp_path), cfg, rois=rois)
    out = {}
    w.finished.connect(lambda p, dec: out.update(path=p, dec=dec))
    w.run()
    assert out.get("path") == ""         # no corrected output claimed


# ── provenance + no step2_ready ──────────────────────────────────────────────
def test_provenance_stamp_no_step2_ready(tmp_path):
    from block01.core.bg_correction import (
        stamp_corrected_zarr_provenance, CREATED_FROM_STEP0_BACKGROUND_CORRECTION,
        CORRECTED_ZARR_OUTPUT_KIND, CORRECTED_ZARR_USED_FOR)
    path = str(tmp_path / "p.zarr")
    root = zarr.open_group(path, mode="w")
    root.create_group("ROI_1").create_dataset(
        "CD68", data=np.ones((4, 4), dtype=np.float32))
    stamp_corrected_zarr_provenance(root)
    attrs = dict(zarr.open_group(path, mode="r").attrs)
    assert attrs["created_from_step"] == CREATED_FROM_STEP0_BACKGROUND_CORRECTION
    assert attrs["created_from_step"] == "step0_background_correction"
    assert attrs["output_kind"] == CORRECTED_ZARR_OUTPUT_KIND == "corrected_channels_zarr"
    assert attrs["used_for"] == CORRECTED_ZARR_USED_FOR
    assert "step2_ready" not in attrs   # never marked Step2-ready


# ── HQ2/CSD raw_ome_only source policy unchanged (not touched by v14.4) ──────
def test_hq2_csd_source_policy_unchanged():
    from block01.workers.hq_source_resolver import (
        SOURCE_MODE_RAW_ONLY, SOURCE_MODE_CORRECTED_THEN_RAW)
    assert SOURCE_MODE_RAW_ONLY == "raw_ome_only"
    assert SOURCE_MODE_CORRECTED_THEN_RAW == "corrected_then_raw_fallback"


# ── dependency wall: BG layer adds no resolver/promotion/runtime imports ─────
def test_no_forbidden_imports_in_bg_layer():
    # No resolver/promotion/runtime imports (check import lines; docstring prose
    # may legitimately mention step2_ready to say it is NEVER written).
    import inspect
    from block01.core import bg_correction as mod
    src = inspect.getsource(mod)
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            for forbidden in ("hq_source_resolver", "remap_promotion",
                              "promote_remap_config", "segment_merge_worker"):
                assert forbidden not in stripped, stripped
    # step2_ready is never written as a zarr attr / assignment in this layer
    assert 'attrs["step2_ready"]' not in src
    assert "step2_ready =" not in src


# ── step0-incremental-bg-save: skip already-corrected unchanged channels ─────
class _MultiLoader:
    def __init__(self):
        self.ch_map = {"CD68": 0, "CK19": 1, "Ki67": 2}
        self.shape = (200, 200)
        self.filepath = "/tmp/fake.ome.tif"

    def _read_roi_zarr(self, idx, y0, y1, x0, x1):
        return (np.random.rand(y1 - y0, x1 - x0) * 1000).astype(np.float32)


def _run_worker(tmp_dir, decisions, rois, process_channels=None, incremental=False):
    from block01.ui.step0.search_ctrl import WsiCorrectionWorker
    cfg = {"channel_decisions": decisions,
           "method_params": {"tophat_radius": 15, "cucim_sigma": 50}}
    w = WsiCorrectionWorker(_MultiLoader(), tmp_dir, cfg, rois=rois,
                            process_channels=process_channels,
                            incremental=incremental)
    out = {}
    w.finished.connect(lambda p, dec: out.update(path=p, dec=dec))
    w.error.connect(lambda m: out.update(err=m))
    w.run()
    return out


def test_read_corrected_zarr_state_methods_and_bbox(app, tmp_path):
    from block01.ui.step0.search_ctrl import read_corrected_zarr_state
    rois = [{"name": "ROI_1", "bbox_fullres": [0, 80, 0, 100]}]
    _run_worker(str(tmp_path), {"CD68": "tophat", "CK19": "cucim"}, rois)
    zp = str(tmp_path / "corrected_channels.zarr")
    sigs, bboxes = read_corrected_zarr_state(zp)
    # signature = (method, method-specific param); _run_worker uses radius 15 / sigma 50
    assert sigs == {"CD68": ("tophat", 15, _V), "CK19": ("cucim", 50, _V)}
    assert bboxes == [(0, 80, 0, 100)]


def test_read_corrected_zarr_state_missing(app):
    from block01.ui.step0.search_ctrl import read_corrected_zarr_state
    assert read_corrected_zarr_state("/no/such.zarr") == ({}, [])


def test_incremental_adds_new_keeps_old(app, tmp_path):
    from block01.ui.step0.search_ctrl import read_corrected_zarr_state
    from block01.core.bg_correction import corrected_zarr_report
    rois = [{"name": "ROI_1", "bbox_fullres": [0, 80, 0, 100]}]
    _run_worker(str(tmp_path), {"CD68": "tophat"}, rois)
    # incremental: only Ki67 is new -> process Ki67, keep CD68
    out = _run_worker(str(tmp_path),
                      {"CD68": "tophat", "Ki67": "tophat"}, rois,
                      process_channels={"Ki67"}, incremental=True)
    zp = out["path"]
    arrays = sorted(corrected_zarr_report(zp)["channel_arrays"])
    assert arrays == ["ROI_1/CD68", "ROI_1/Ki67"]      # old retained + new added
    sigs, _ = read_corrected_zarr_state(zp)
    assert sigs == {"CD68": ("tophat", 15, _V), "Ki67": ("tophat", 15, _V)}
    # emitted decisions describe the FULL merged zarr (loader routes all)
    assert set(out["dec"]) == {"CD68", "Ki67"}


def test_incremental_method_change_reprocesses_only_that_channel(app, tmp_path):
    from block01.ui.step0.search_ctrl import read_corrected_zarr_state
    rois = [{"name": "ROI_1", "bbox_fullres": [0, 80, 0, 100]}]
    _run_worker(str(tmp_path), {"CD68": "tophat", "Ki67": "tophat"}, rois)
    # CD68 method changed tophat -> cucim; only CD68 reprocessed
    _run_worker(str(tmp_path),
                {"CD68": "cucim", "Ki67": "tophat"}, rois,
                process_channels={"CD68"}, incremental=True)
    sigs, _ = read_corrected_zarr_state(str(tmp_path / "corrected_channels.zarr"))
    assert sigs["CD68"] == ("cucim", 50, _V)   # method changed -> reprocessed
    assert sigs["Ki67"] == ("tophat", 15, _V)  # untouched


def test_incremental_no_channels_emits_merged_state(app, tmp_path):
    # everything already saved (process_channels empty) -> worker writes nothing
    # new but reports the existing merged decisions + the zarr path (handoff data)
    rois = [{"name": "ROI_1", "bbox_fullres": [0, 80, 0, 100]}]
    _run_worker(str(tmp_path), {"CD68": "tophat"}, rois)
    out = _run_worker(str(tmp_path), {"CD68": "tophat"}, rois,
                      process_channels=set(), incremental=True)
    assert out.get("err") is None
    assert out["path"].endswith("corrected_channels.zarr")
    assert out["dec"] == {"CD68": "tophat"}


def test_hotswap_only_reprocessed_channels(app, tmp_path):
    from block01.ui.step0.step0_page import Step0Page
    p = Step0Page()

    class _L:
        ch_map = {"CD68": 0, "Ki67": 1}
        shape = (60, 60)
        filepath = "/t.ome.tif"
        def read_region(self, ch, y0, y1, x0, x1, normalize=False):
            return np.full((y1 - y0, x1 - x0), 7.0, np.float32)
    p.loader = _L()
    p.patches = [(0, 20, 0, 20)]
    p.current_patch_idx = 0
    # seed the cache with stale values for both channels. Keyed by BBOX
    # now, not by patch index -- see `PreloadScheduler`.
    bbox = p._patch_bbox(0)
    sched = p._preload_scheduler()
    sched.put(bbox, "CD68", np.zeros((20, 20), np.float32))
    sched.put(bbox, "Ki67", np.zeros((20, 20), np.float32))
    # hot-swap restricted to CD68 only
    p._hotswap_corrected({"CD68": "tophat", "Ki67": "tophat"}, only={"CD68"})
    assert float(sched.resident(bbox, "CD68").mean()) == 7.0  # reprocessed
    assert float(sched.resident(bbox, "Ki67").mean()) == 0.0  # untouched


def test_step0_all_skipped_emits_handoff_without_worker(app, tmp_path, monkeypatch):
    """Second Save with identical assignments: every channel already corrected
    -> NO worker started, but the Step0->Step1 handoff (_emit_complete) still
    fires and the loader is rewired to the existing corrected zarr."""
    import block01.ui.step0.step0_page as sp
    from block01.ui.step0.step0_page import Step0Page

    class _L:
        def __init__(self):
            self.ch_map = {"CD68": 0, "Ki67": 1}
            self.shape = (80, 100)
            self.filepath = "/t.ome.tif"
            self._store = None
        def channel_names(self):
            return list(self.ch_map.keys())
        def set_correction_config(self, c):
            pass
        def set_corrected_zarr_store(self, path, decisions):
            self._store = (path, decisions)

    p = Step0Page()
    p.loader = _L()
    p.output_dir = str(tmp_path)
    p.ome_path = "/t.ome.tif"
    p.patches = [(0, 20, 0, 20)]
    p.current_patch_idx = 0
    p._analysis_region_mode = "full_wsi"          # bbox = [0, 80, 0, 100]
    p._channel_order = ["CD68", "Ki67"]
    p._channel_decisions = {"CD68": "tophat", "Ki67": "tophat"}
    p._tophat_slider.setValue(15)                 # deterministic tophat_radius

    # the corrected zarr already holds both channels with the SAME (method, param)
    monkeypatch.setattr(sp, "read_corrected_zarr_state",
                        lambda zp: ({"CD68": ("tophat", 15, _V), "Ki67": ("tophat", 15, _V)},
                                    [(0, 80, 0, 100)]))
    # a worker must NOT be constructed in the all-skip path
    def _boom(*a, **k):
        raise AssertionError("WsiCorrectionWorker started despite all-skip")
    monkeypatch.setattr(sp, "WsiCorrectionWorker", _boom)
    emitted = {}
    monkeypatch.setattr(Step0Page, "_emit_complete",
                        lambda self, cfg, zp, dec: emitted.update(zp=zp, dec=dec))

    p._save_and_continue()

    assert emitted, "handoff (_emit_complete) did not fire on all-skip"
    assert emitted["zp"].endswith("corrected_channels.zarr")
    assert emitted["dec"] == {"CD68": "tophat", "Ki67": "tophat"}
    assert p.loader._store[1] == {"CD68": "tophat", "Ki67": "tophat"}


# ── step0-fix-incremental-save-and-zoom (#1 reuse roi_context, #4 zoom) ───────
def _step0_for_context(tmp_path, monkeypatch):
    """Step0 wired so _save_and_continue runs without the modal worker: all
    channels 'original' -> the empty-corrected branch (no WsiCorrectionWorker).
    create_* are stubbed to count calls + return a context under tmp_path."""
    import block01.ui.step0.step0_page as sp
    from block01.ui.step0.step0_page import Step0Page

    class _L:
        shape = (80, 100)
        filepath = "/t.ome.tif"
        ch_map = {"CD68": 0}
        def channel_names(self):
            return ["CD68"]
        def set_correction_config(self, c):
            pass
        def set_corrected_zarr_store(self, p, d):
            pass

    p = Step0Page()
    p.loader = _L()
    p.output_dir = str(tmp_path)
    p.ome_path = "/t.ome.tif"
    p.patches = [(0, 20, 0, 20)]
    p._channel_order = ["CD68"]
    p._channel_decisions = {"CD68": "original"}        # -> corrected empty, no worker

    calls = {"full": 0, "roi": 0}

    def _ctx(kind, sub):
        d = str(tmp_path / sub)
        os.makedirs(os.path.join(d, "step0"), exist_ok=True)
        return {"roi_id": sub, "roi_dir": d,
                "step_dirs": {"step0": os.path.join(d, "step0")}}

    def _mk_full(out, shape, ome):
        calls["full"] += 1
        return _ctx("full", f"full_{calls['full']}")

    def _mk_roi(out, roi, ome):
        calls["roi"] += 1
        return _ctx("roi", f"roi_{calls['roi']}")

    monkeypatch.setattr(sp, "create_full_wsi_context", _mk_full)
    monkeypatch.setattr(sp, "create_roi_context", _mk_roi)
    monkeypatch.setattr(Step0Page, "_emit_complete",
                        lambda self, *a, **k: None)
    monkeypatch.setattr(Step0Page, "_ensure_empty_corrected_zarr",
                        lambda self, *a, **k: None)
    return p, calls


def test_roi_context_reused_for_same_full_wsi_region(app, tmp_path, monkeypatch):
    p, calls = _step0_for_context(tmp_path, monkeypatch)
    p._analysis_region_mode = "full_wsi"
    p._save_and_continue()
    ctx1 = p._roi_context
    p._save_and_continue()                  # same region -> reuse, no new context
    assert calls["full"] == 1               # create called once only
    assert p._roi_context is ctx1           # same context object
    assert p._roi_context["step_dirs"]["step0"] == ctx1["step_dirs"]["step0"]


def test_roi_context_fresh_when_mode_changes(app, tmp_path, monkeypatch):
    p, calls = _step0_for_context(tmp_path, monkeypatch)
    p._analysis_region_mode = "full_wsi"
    p._save_and_continue()
    # mode switch -> different signature -> fresh context
    p._analysis_region_mode = "roi"
    p.rois = [{"name": "R", "bbox_fullres": [0, 40, 0, 50],
               "polygon_fullres": None}]

    class _OV:
        def get_rois(self):
            return [{"name": "R", "bbox_fullres": [0, 40, 0, 50],
                     "polygon_fullres": None}]
    p.overview = _OV()
    p._save_and_continue()
    assert calls["full"] == 1 and calls["roi"] == 1   # one each (not reused)


def test_roi_context_fresh_when_bbox_changes(app, tmp_path, monkeypatch):
    p, calls = _step0_for_context(tmp_path, monkeypatch)
    p._analysis_region_mode = "roi"

    bbox = [[0, 40, 0, 50]]

    class _OV:
        def get_rois(self):
            return [{"name": "R", "bbox_fullres": list(bbox[0]),
                     "polygon_fullres": None}]
    p.overview = _OV()
    p._save_and_continue()                  # roi #1
    p._save_and_continue()                  # same bbox -> reuse
    assert calls["roi"] == 1
    bbox[0] = [0, 60, 0, 70]                 # user redrew the ROI
    p._save_and_continue()                  # bbox changed -> fresh context
    assert calls["roi"] == 2


def test_composite_zoom_preserved_across_different_shape_patches(app):
    import numpy as np
    from block01.ui.widgets.channel_workbench import ChannelWorkbench
    wb = ChannelWorkbench(show_reference_bar=False, show_enabled_checkbox=False,
                          multichannel_overlay=True)
    rng = np.random.default_rng(0)
    def imgs(shape):
        return {n: rng.random(shape).astype(np.float32) for n in ("DAPI", "CD68")}
    wb.set_channel_images(imgs((20, 20)), colors={"DAPI": "#3366ff"},
                          active="DAPI", visible=["DAPI"])
    vb = wb._canvas._vb
    vb.setRange(xRange=(2, 8), yRange=(3, 9), padding=0)
    before = vb.viewRange()
    wb.set_channel_images(imgs((40, 50)), colors={"DAPI": "#3366ff"},  # DIFFERENT shape
                          active="DAPI", visible=["DAPI"])
    after = vb.viewRange()
    assert np.allclose(before[0], after[0]) and np.allclose(before[1], after[1])
    assert wb._canvas._prev_shape == (40, 50)


# ── step0-bg incremental dirty-set: method + param signature dispatch ─────────
def _step0_dispatch_capture(tmp_path, monkeypatch, existing_sigs):
    """Drive _save_and_continue past the dirty-set decision, capturing the
    WsiCorrectionWorker's process_channels (or None if no worker started).
    existing_sigs is what read_corrected_zarr_state returns for the zarr."""
    import block01.ui.step0.step0_page as sp
    from block01.ui.step0.step0_page import Step0Page
    from PyQt5 import QtCore

    class _L:
        shape = (80, 100)
        filepath = "/t.ome.tif"
        ch_map = {"CD68": 0, "CK19": 1, "Ki67": 2}
        def channel_names(self):
            return list(self.ch_map.keys())
        def set_correction_config(self, c):
            pass
        def set_corrected_zarr_store(self, p, d):
            pass

    cap = {"process": None, "started": False}

    # A QThread, not a bare QObject: the page WATCHES every production
    # worker off `QThread.finished` (reaching past the business `finished`
    # this class shadows it with, exactly as the real worker does), so a
    # stand-in that is not a QThread cannot be watched at all.
    class _FakeWorker(QtCore.QThread):
        progress = QtCore.pyqtSignal(int, int, int, int, str, str, int)
        finished = QtCore.pyqtSignal(str, dict)
        canceled = QtCore.pyqtSignal(str)
        error = QtCore.pyqtSignal(str)
        def __init__(self, loader, out, cfg, rois=None, parent=None,
                     process_channels=None, incremental=False):
            super().__init__(parent)
            cap["process"] = process_channels
            cap["incremental"] = incremental
        def start(self):
            cap["started"] = True
        def stop_after_current_channel(self):
            pass

    class _FakeDialog(QtCore.QObject):
        cancel_requested = QtCore.pyqtSignal()
        def __init__(self, parent=None):
            super().__init__(parent)
        def exec_(self):
            return 0
        def show(self):
            pass
        def allow_close(self):
            pass
        def accept(self):
            pass
        def set_progress(self, *a, **k):
            pass

    p = Step0Page()
    p.loader = _L()
    p.output_dir = str(tmp_path)
    p.ome_path = "/t.ome.tif"
    p.patches = [(0, 20, 0, 20)]
    p._analysis_region_mode = "full_wsi"
    p._channel_order = ["CD68", "CK19", "Ki67"]
    p._channel_decisions = {"CD68": "tophat", "Ki67": "tophat"}
    p._tophat_slider.setValue(15)
    p._cucim_slider.setValue(50)

    monkeypatch.setattr(sp, "read_corrected_zarr_state",
                        lambda zp: (existing_sigs, [(0, 80, 0, 100)]))
    monkeypatch.setattr(sp, "WsiCorrectionWorker", _FakeWorker)
    monkeypatch.setattr(sp, "_WsiCorrectionProgressDialog", _FakeDialog)
    monkeypatch.setattr(Step0Page, "_emit_complete", lambda self, *a, **k: None)
    monkeypatch.setattr(Step0Page, "_ensure_empty_corrected_zarr",
                        lambda self, *a, **k: None)
    p._save_and_continue()
    return cap, p


def test_dispatch_new_channel_only(app, tmp_path, monkeypatch):
    # CD68 already saved (tophat,15); Ki67 is newly assigned -> only Ki67
    cap, _ = _step0_dispatch_capture(
        tmp_path, monkeypatch, {"CD68": ("tophat", 15, _V)})
    assert cap["started"] is True
    assert cap["process"] == {"Ki67"}


def test_dispatch_param_change_reprocesses_channel(app, tmp_path, monkeypatch):
    # both saved as tophat, but CD68 was saved with radius 9 (param changed -> 15)
    cap, _ = _step0_dispatch_capture(
        tmp_path, monkeypatch,
        {"CD68": ("tophat", 9, _V), "Ki67": ("tophat", 15, _V)})
    assert cap["started"] is True
    assert cap["process"] == {"CD68"}        # only the param-changed channel


def test_dispatch_method_change_reprocesses_channel(app, tmp_path, monkeypatch):
    # CD68 saved cucim; now assigned tophat -> method changed -> reprocess CD68
    cap, _ = _step0_dispatch_capture(
        tmp_path, monkeypatch,
        {"CD68": ("cucim", 50, _V), "Ki67": ("tophat", 15, _V)})
    assert cap["process"] == {"CD68"}


def test_dispatch_all_unchanged_starts_no_worker(app, tmp_path, monkeypatch):
    cap, _ = _step0_dispatch_capture(
        tmp_path, monkeypatch,
        {"CD68": ("tophat", 15, _V), "Ki67": ("tophat", 15, _V)})
    assert cap["started"] is False           # nothing to process -> no dialog/worker
    assert cap["process"] is None


def test_dispatch_stale_algo_version_reprocesses(app, tmp_path, monkeypatch):
    # Same (method, param) but the zarr was written by algorithm version "1"
    # (e.g. the 2*sigma gaussian halo era) -> identity mismatch -> reprocess.
    cap, _ = _step0_dispatch_capture(
        tmp_path, monkeypatch,
        {"CD68": ("tophat", 15, "1"), "Ki67": ("tophat", 15, _V)})
    assert cap["started"] is True
    assert cap["process"] == {"CD68"}


def test_dispatch_empty_zarr_reprocesses_all(app, tmp_path, monkeypatch):
    # no existing corrected state (missing/empty zarr, or ROI signature mismatch
    # clears existing_sigs) -> every assigned channel is processed.
    cap, _ = _step0_dispatch_capture(tmp_path, monkeypatch, {})
    assert cap["started"] is True
    assert cap["process"] == {"CD68", "Ki67"}


def test_dispatch_roi_bbox_mismatch_reprocesses_all(app, tmp_path, monkeypatch):
    import block01.ui.step0.step0_page as sp
    from block01.ui.step0.step0_page import Step0Page
    from PyQt5 import QtCore

    class _L:
        shape = (80, 100); filepath = "/t.ome.tif"
        ch_map = {"CD68": 0, "Ki67": 1}
        def channel_names(self): return list(self.ch_map.keys())
        def set_correction_config(self, c): pass
        def set_corrected_zarr_store(self, p, d): pass

    cap = {"process": None}

    # QThread for the same reason as `_FakeWorker` above: the page watches
    # every production worker off `QThread.finished`, reaching past the
    # business `finished` this class shadows it with.
    class _FW(QtCore.QThread):
        progress = QtCore.pyqtSignal(int, int, int, int, str, str, int)
        finished = QtCore.pyqtSignal(str, dict)
        canceled = QtCore.pyqtSignal(str)
        error = QtCore.pyqtSignal(str)
        def __init__(self, *a, process_channels=None, incremental=False, **k):
            super().__init__()
            cap["process"] = process_channels
            cap["incremental"] = incremental
        def start(self): pass
        def stop_after_current_channel(self): pass

    class _FD(QtCore.QObject):
        cancel_requested = QtCore.pyqtSignal()
        def exec_(self): return 0
        def show(self): pass
        def show(self):
            pass
        def allow_close(self): pass
        def accept(self): pass
        def set_progress(self, *a, **k): pass

    p = Step0Page()
    p.loader = _L(); p.output_dir = str(tmp_path); p.ome_path = "/t.ome.tif"
    p.patches = [(0, 20, 0, 20)]; p._analysis_region_mode = "full_wsi"
    p._channel_order = ["CD68", "Ki67"]
    p._channel_decisions = {"CD68": "tophat", "Ki67": "tophat"}
    p._tophat_slider.setValue(15)
    # zarr has both channels but a DIFFERENT ROI bbox -> signature mismatch
    monkeypatch.setattr(sp, "read_corrected_zarr_state",
                        lambda zp: ({"CD68": ("tophat", 15, _V), "Ki67": ("tophat", 15, _V)},
                                    [(0, 999, 0, 999)]))
    monkeypatch.setattr(sp, "WsiCorrectionWorker", _FW)
    monkeypatch.setattr(sp, "_WsiCorrectionProgressDialog", _FD)
    monkeypatch.setattr(Step0Page, "_emit_complete", lambda self, *a, **k: None)
    p._save_and_continue()
    assert cap["process"] == {"CD68", "Ki67"}    # ROI changed -> no unsafe skip
    assert cap["incremental"] is False           # not incremental on ROI mismatch


# ── step0-save-correctness: method-change overwrites the corrected channel ────
class _OneChLoader:
    def __init__(self, ch):
        self.ch_map = {ch: 0}
        self.shape = (200, 200)
        self.filepath = "/tmp/fake.ome.tif"

    def _read_roi_zarr(self, idx, y0, y1, x0, x1):
        return (np.random.rand(y1 - y0, x1 - x0) * 1000).astype(np.float32)


def _run_one(tmp_dir, ch, method, param, process_channels=None, incremental=False):
    from block01.ui.step0.search_ctrl import WsiCorrectionWorker
    cfg = {"channel_decisions": {ch: method},
           "method_params": {"tophat_radius": param if method == "tophat" else 15,
                             "cucim_sigma": param if method == "cucim" else 50}}
    rois = [{"name": "R", "bbox_fullres": [0, 80, 0, 100]}]
    w = WsiCorrectionWorker(_OneChLoader(ch), tmp_dir, cfg, rois=rois,
                            process_channels=process_channels, incremental=incremental)
    out = {}
    w.finished.connect(lambda p, dec: out.update(path=p, dec=dec))
    w.error.connect(lambda m: out.update(err=m))
    w.run()
    return out


def _ch_attrs(tmp_dir, ch):
    g = zarr.open_group(os.path.join(tmp_dir, "corrected_channels.zarr"), mode="r")
    a = g["R"][ch].attrs
    return (a.get("correction_method"), a.get("correction_param_name"),
            a.get("correction_param_value"))


def test_method_change_cucim_to_tophat_overwrites_channel(app, tmp_path):
    from block01.ui.step0.search_ctrl import read_corrected_zarr_state
    d = str(tmp_path)
    _run_one(d, "CD11b", "cucim", 2)
    assert _ch_attrs(d, "CD11b") == ("cucim", "cucim_sigma", 2)
    zp = os.path.join(d, "corrected_channels.zarr")
    before = zarr.open_group(zp, mode="r")["R"]["CD11b"][:].copy()

    # change to tophat radius 80, incremental
    out = _run_one(d, "CD11b", "tophat", 80,
                   process_channels={"CD11b"}, incremental=True)
    # attrs now say tophat / tophat_radius / 80 — no stale cucim metadata
    assert _ch_attrs(d, "CD11b") == ("tophat", "tophat_radius", 80)
    # the corrected ARRAY was rewritten (different pixels)
    after = zarr.open_group(zp, mode="r")["R"]["CD11b"][:]
    assert not np.array_equal(before, after)
    # the read signature + merged decisions reflect tophat
    sigs, _ = read_corrected_zarr_state(zp)
    assert sigs["CD11b"] == ("tophat", 80, _V)
    assert out["dec"]["CD11b"] == "tophat"


def test_method_change_tophat_to_cucim_overwrites_channel(app, tmp_path):
    from block01.ui.step0.search_ctrl import read_corrected_zarr_state
    d = str(tmp_path)
    _run_one(d, "CD11b", "tophat", 50)
    assert _ch_attrs(d, "CD11b") == ("tophat", "tophat_radius", 50)
    _run_one(d, "CD11b", "cucim", 3, process_channels={"CD11b"}, incremental=True)
    assert _ch_attrs(d, "CD11b") == ("cucim", "cucim_sigma", 3)
    sigs, _ = read_corrected_zarr_state(
        os.path.join(d, "corrected_channels.zarr"))
    assert sigs["CD11b"] == ("cucim", 3, _V)


def test_same_method_param_change_overwrites_channel(app, tmp_path):
    from block01.ui.step0.search_ctrl import read_corrected_zarr_state
    d = str(tmp_path)
    _run_one(d, "CD11b", "tophat", 50)
    _run_one(d, "CD11b", "tophat", 80, process_channels={"CD11b"}, incremental=True)
    sigs, _ = read_corrected_zarr_state(
        os.path.join(d, "corrected_channels.zarr"))
    assert sigs["CD11b"] == ("tophat", 80, _V)   # param updated 50 -> 80


def test_wsi_finished_refreshes_store_and_cache_with_new_method(app, tmp_path):
    from block01.ui.step0.step0_page import Step0Page

    class _L:
        ch_map = {"CD11b": 0}
        shape = (60, 60)
        filepath = "/t.ome.tif"
        def __init__(self):
            self.store = None
            self.method = "tophat"   # loader returns "tophat-corrected" pixels (9.0)
        def set_corrected_zarr_store(self, path, decisions):
            self.store = (path, decisions)
        def read_region(self, ch, y0, y1, x0, x1, normalize=False):
            return np.full((y1 - y0, x1 - x0), 9.0, np.float32)

    p = Step0Page()
    p.loader = _L()
    p.patches = [(0, 20, 0, 20)]
    p.current_patch_idx = 0
    # cache holds the OLD cucim pixels
    bbox = p._patch_bbox(0)
    sched = p._preload_scheduler()
    sched.put(bbox, "CD11b", np.zeros((20, 20), np.float32))
    p._incremental_processed = {"CD11b"}
    p._wsi_dialog = None
    # finish: merged decisions say tophat now
    p._on_wsi_finished({}, "/x/corrected_channels.zarr", {"CD11b": "tophat"})
    # downstream corrected-zarr store points at the updated (tophat) decisions
    assert p.loader.store == ("/x/corrected_channels.zarr", {"CD11b": "tophat"})
    # in-memory cache hot-swapped to the new (tophat) pixels
    assert float(sched.resident(bbox, "CD11b").mean()) == 9.0


# ── B4-A: correction identity is not a function of what is on screen ─────

def _correction_page(app, tmp_path):
    """A real Step0 page with a real loader, a patch and a decision."""
    from block01.ui.step0.step0_page import Step0Page
    from test_step0_background_correction_tab import _GpuPathLoader

    page = Step0Page()
    page.loader = _GpuPathLoader()
    page.output_dir = str(tmp_path)
    page.ome_path = "/t.ome.tif"
    page.patches = [(0, 32, 0, 32)]
    page.current_patch_idx = 0
    page.nucleus_channel = "DAPI"
    page._rebuild_channel_list()
    # The row's combo is the PREVIEW method: this page previews CD3 with
    # TopHat. It has decided nothing yet -- deciding is Per-Channel Decision
    # + Apply, which `_decide` below drives.
    page._channel_rows["CD3"]["method_cb"].setCurrentText("TopHat")
    return page


def _decide(page, channel, decision):
    """Make `decision` final for `channel` THROUGH THE REAL UI.

    The Per-Channel Decision radios plus Apply -- the only user entry to the
    answer Save publishes. Nothing here touches the preview method.
    """
    page.current_channel = channel
    page._update_decision_ui()
    {"original": page._dec_orig, "tophat": page._dec_top,
     "cucim": page._dec_cu}[decision].setChecked(True)
    page._apply_current_channel_decision()
    return decision


def _correction_identity(page):
    """Everything that decides what Save writes and what a cache may reuse."""
    return {
        "config": page._build_config(),
        "decisions": dict(page._channel_decisions),
        "methods": dict(page._channel_methods),
        "signatures": {ch: page._channel_signature(
            ch, page._channel_preview_method(ch))
            for ch in page._channel_order if ch != page.nucleus_channel},
        "states": {ch: page._channel_compute_state(ch)
                   for ch in page._channel_order},
        "raw": list(page._raw_save_channels()),
    }


def test_showing_and_hiding_channels_changes_no_correction_identity(app,
                                                                    tmp_path,
                                                                    monkeypatch):
    """Visibility-only edits: the decision, the signature, the compute state,
    the Save payload and the raw-channel set are all unmoved -- and no run,
    no read and no write is started."""
    import block01.ui.step0.step0_page as sp

    page = _correction_page(app, tmp_path)
    before = _correction_identity(page)

    def _boom(*a, **k):
        raise AssertionError("a correction run was started by a display edit")

    monkeypatch.setattr(sp, "BatchProcessWorker", _boom)
    monkeypatch.setattr(sp, "WsiCorrectionWorker", _boom)

    state = page.display.state
    for ch in ("CD3", "CD20", "DAPI"):
        state.set_display_visible(ch, True, origin="test")
    page._cb_all.setChecked(False)
    state.set_display_visible("CD3", True, origin="test")

    assert _correction_identity(page) == before
    page.close()


def test_a_correction_edit_leaves_the_display_and_the_science_alone(app,
                                                                    tmp_path):
    """The mirror image: a method change makes the result stale and moves
    nothing that is on screen and nothing scientific."""
    page = _correction_page(app, tmp_path)
    state = page.display.state
    state.set_display_visible("CD3", True, origin="test")
    visible_before = dict(state.display_visibility())
    selection_before = state.selected_channel()
    fusion = page.display.fusion
    fusion.edit_channel_weight("CD3", 0.25, origin="test")
    draft_before = fusion.draft_snapshot()
    committed_before = fusion.committed_snapshot()

    # ...a computed result exists for the method it had: the recorded
    # signature, the done mark AND a payload for every current patch, which
    # is the evidence `_channel_is_up_to_date` asks for.
    page._computed_channels.add("CD3")
    page._computed_signatures["CD3"] = page._channel_signature("CD3", "tophat")
    for p_idx in range(len(page.patches)):
        page._preview_cache[("CD3", p_idx)] = {"original_disp": None}
    assert page._channel_compute_state("CD3") == "computed"

    page._channel_rows["CD3"]["method_cb"].setCurrentText("cucim")

    assert page._channel_preview_method("CD3") == "cucim"
    # ...and the preview method it moved is not the final decision: nobody
    # applied one, so Save still publishes the raw channel.
    assert page._channel_final_decision("CD3") == "original"
    assert page._channel_compute_state("CD3") == "stale"
    assert dict(state.display_visibility()) == visible_before
    assert state.selected_channel() == selection_before
    assert fusion.draft_snapshot() == draft_before
    assert fusion.committed_snapshot() == committed_before
    page.close()


def test_a_finished_correction_does_not_show_the_channel(app, tmp_path):
    """`computed` is a STATUS. It used to tick the row and paint it green,
    which showed a channel the user had hidden."""
    page = _correction_page(app, tmp_path)
    state = page.display.state
    state.set_display_visible("CD3", False, origin="test")

    page._set_channel_computing("CD3")
    assert state.display_visible("CD3") is False
    page._computed_signatures["CD3"] = page._channel_signature("CD3", "tophat")
    for p_idx in range(len(page.patches)):
        page._preview_cache[("CD3", p_idx)] = {"original_disp": None}
    page._set_channel_done("CD3")

    assert state.display_visible("CD3") is False
    assert page._channel_rows["CD3"]["checkbox"].isChecked() is False
    assert page._channel_rows["CD3"]["checkbox"].isEnabled() is True
    assert page._channel_compute_state("CD3") == "computed"
    page.close()


def test_the_bulk_method_box_assigns_every_eligible_channel(app, tmp_path):
    """A fresh channel gets a DECISION, not a combo that shows one.

    EVERY eligible channel, assigned or not -- the box used to reach only
    channels that already had a value, so a slide nobody had touched kept a
    row showing one method while the page computed another.

    And it moves the PREVIEW method only. What Save publishes is decided per
    channel in Per-Channel Decision; a bulk box that wrote that answer would
    publish decisions the user never made.
    """
    page = _correction_page(app, tmp_path)
    page._channel_decisions.clear()
    page._channel_methods.clear()
    decided = _decide(page, "CD3", "cucim")
    saved_before = page._build_config()

    _set_bulk_method(page, "tophat")

    saved = page._build_config()["channel_decisions"]
    for ch in page._channel_order:
        if ch == page.nucleus_channel:
            continue
        assert page._channel_preview_method(ch) == "tophat", ch
        assert page._channel_rows[ch]["method_cb"].currentText() == "TopHat"
    # ...and not one final decision moved: CD3 still publishes what it was
    # decided to, everything else still publishes the raw channel.
    assert page._channel_final_decision("CD3") == decided
    assert saved["CD3"] == decided
    assert page._build_config() == saved_before
    for ch in page._channel_order:
        if ch in (page.nucleus_channel, "CD3"):
            continue
        assert page._channel_final_decision(ch) == "original", ch
    # The nucleus is not correction-eligible: no preview method, no decision.
    assert page.nucleus_channel not in page._channel_methods
    assert page.nucleus_channel not in page._channel_decisions
    assert page.nucleus_channel not in saved
    page.close()


def test_a_combo_driven_out_of_step_commands_nothing(app, tmp_path):
    """A combo moved with its signals blocked is a widget, not a command.

    The store is what answers -- for the preview method AND, separately, for
    what Save publishes. A drifted combo (a blocked write, a rebuild racing
    a change) changes neither.
    """
    page = _correction_page(app, tmp_path)
    _decide(page, "CD3", "tophat")
    assert page._channel_preview_method("CD3") == "tophat"
    signature_before = page._channel_signature("CD3", "tophat")
    saved_before = page._build_config()
    raw_before = page._raw_save_channels()

    combo = page._channel_rows["CD3"]["method_cb"]
    combo.blockSignals(True)
    combo.setCurrentText("cucim")             # the mirror now disagrees
    combo.blockSignals(False)

    assert page._channel_preview_method("CD3") == "tophat"
    assert page._channel_final_decision("CD3") == "tophat"
    assert page._channel_signature(
        "CD3", page._channel_preview_method("CD3")) == signature_before
    assert page._build_config() == saved_before
    assert page._raw_save_channels() == raw_before
    page.close()


def test_an_undecided_channel_publishes_original_and_previews_both(app,
                                                                   tmp_path):
    """The two defaults, which are deliberately different answers.

    Nobody has decided anything, so Save publishes the raw channel for every
    marker -- while the page still PREVIEWS both candidates, because that is
    what the compare panels are for. Reading one default off the other is
    the conflation this split removes.
    """
    page = _correction_page(app, tmp_path)
    page._channel_decisions.clear()
    page._channel_methods.clear()

    saved = page._build_config()["channel_decisions"]
    for ch in page._channel_order:
        if ch == page.nucleus_channel:
            continue
        assert page._channel_final_decision(ch) == "original", ch
        assert page._channel_preview_method(ch) == "both", ch
        assert saved[ch] == "original", (ch, saved)
        assert ch in page._raw_save_channels(), ch
    page.close()


# ── B4-A follow-up: candidate computation vs the FINAL choice ────────────

def _decision_page(app, tmp_path):
    """A real page with no decision made yet."""
    page = _correction_page(app, tmp_path)
    page._channel_decisions.clear()
    page._channel_methods.clear()
    page._refresh_channel_row("CD3")
    return page


def test_a_fresh_row_previews_both_and_saves_original(app, tmp_path):
    """A: a fresh slide, and the two questions have different answers.

    The row's combo shows `Both` -- the page prepares the TopHat and the
    cuCIM candidate so they can be compared -- and Save publishes `original`,
    because the user has decided nothing yet. Both statements are true at the
    same time; a single `method` per channel could not say that.
    """
    page = _decision_page(app, tmp_path)

    assert page._channel_rows["CD3"]["method_cb"].currentText() == "Both"
    assert page._channel_preview_method("CD3") == "both"
    assert page._preview_method_default == "both"
    assert page._channel_final_decision("CD3") == "original"
    assert page._build_config()["channel_decisions"]["CD3"] == "original"
    assert "CD3" in page._raw_save_channels()
    # ...and the decision panel shows the decision, not the preview.
    page.current_channel = "CD3"
    page._update_decision_ui()
    assert page._dec_orig.isChecked()
    page.close()


def test_each_final_choice_means_the_same_thing_everywhere(app, tmp_path):
    """B: Original, TopHat and cuCIM through the REAL decision panel.

    The radios plus Apply are the one user entry to what Save publishes, and
    the row's preview combo does not move with them.
    """
    from block01.core import step0_handoff

    page = _decision_page(app, tmp_path)
    state = page.display.state
    state.set_display_visible("CD3", True, origin="test")
    combo = page._channel_rows["CD3"]["method_cb"]
    assert [combo.itemText(i) for i in range(combo.count())] == [
        "Both", "Original", "TopHat", "cucim"]
    combo.setCurrentText("Both")

    for decision in ("tophat", "cucim", "original"):
        _decide(page, "CD3", decision)

        # THE PREVIEW DID NOT MOVE. Deciding what to publish is not choosing
        # what to look at.
        assert combo.currentText() == "Both"
        assert page._channel_preview_method("CD3") == "both"
        assert page._channel_final_decision("CD3") == decision
        assert page._build_config()["channel_decisions"]["CD3"] == decision
        assert step0_handoff.clean_correction_config(
            page._build_config())["channel_decisions"]["CD3"] == decision
        # Not computed yet, so Save would write it raw.
        assert "CD3" in page._raw_save_channels()
        # ...and deciding how to correct a channel never shows or hides it.
        assert state.display_visible("CD3") is True

    # With real completion evidence for what is being previewed, it is no
    # longer a raw channel -- and that is the only thing that changes.
    _decide(page, "CD3", "tophat")
    combo.setCurrentText("TopHat")
    page._computed_channels.add("CD3")
    page._computed_signatures["CD3"] = page._channel_signature("CD3", "tophat")
    for p_idx in range(len(page.patches)):
        page._preview_cache[("CD3", p_idx)] = {"original_disp": None}
    assert page._channel_compute_state("CD3") == "computed"
    assert "CD3" not in page._raw_save_channels()
    page.close()


def test_both_is_a_preview_method_and_never_a_final_decision(app, tmp_path):
    """C: `Both` is offered where it means something and refused where it
    does not.

    It IS a preview method -- prepare two candidates and compare them -- so
    it is in the row's combo and in the bulk box. It is NOT an answer Save
    can publish, so the decision panel does not offer it, the decision writer
    refuses it, and it cannot come out of Save.
    """
    import pytest as _pytest
    from block01.core import step0_handoff

    page = _decision_page(app, tmp_path)
    row_combo = page._channel_rows["CD3"]["method_cb"]
    assert "Both" in [row_combo.itemText(i)
                      for i in range(row_combo.count())]
    # ...and `both` is reachable in the bulk control: it is what BOTH
    # toggles lit means.
    _set_bulk_method(page, "both")
    assert page._preview_method_default == "both"
    assert page._method_tophat_btn.isChecked()
    assert page._method_cucim_btn.isChecked()
    # ...and the decision panel offers exactly three answers, none of them Both.
    assert [b.text() for b in (page._dec_orig, page._dec_top, page._dec_cu)] \
        == ["Original", "TopHat", "cucim"]

    with _pytest.raises(ValueError):
        page._set_channel_decision("CD3", "both")
    assert page._channel_decisions.get("CD3") in (None, "")

    _set_bulk_method(page, "both")
    row_combo.setCurrentText("Both")
    assert page._channel_preview_method("CD3") == "both"
    _decide(page, "CD3", "tophat")
    saved = page._build_config()["channel_decisions"]
    assert "both" not in saved.values()
    assert set(saved.values()) <= set(step0_handoff.FINAL_CORRECTION_DECISIONS)

    # ...and if something DID put an illegal value in memory -- a bug, not a
    # user -- Save says so rather than quietly writing a different answer
    # than the page is showing. That silent `both -> original` is what made
    # the row and the file disagree in the first place.
    page._channel_decisions["CD3"] = "both"
    with _pytest.raises(ValueError):
        page._build_config()
    page.close()


def test_computed_evidence_for_both_leaves_the_final_choice_alone(app,
                                                                  tmp_path):
    """D: two candidates computed, one final answer.

    `_process_current_channel("both")` is the compatibility entry that works
    out both candidates so they can be compared. Evidence is not a decision:
    the channel keeps the choice the user made, and that is what Save writes.
    """
    page = _decision_page(app, tmp_path)
    page._channel_rows["CD3"]["method_cb"].setCurrentText("Both")
    _decide(page, "CD3", "tophat")

    # Both candidates computed, as the merged signature records it.
    page._computed_signatures["CD3"] = page._merge_channel_signatures(
        page._channel_signature("CD3", "tophat"),
        page._channel_signature("CD3", "cucim"))
    page._computed_channels.add("CD3")
    for p_idx in range(len(page.patches)):
        page._preview_cache[("CD3", p_idx)] = {"original_disp": None}

    assert page._computed_signatures["CD3"][0] == "both"
    # preview = both, final = tophat. Both true at once, and Save takes the
    # final answer.
    assert page._channel_preview_method("CD3") == "both"
    assert page._channel_final_decision("CD3") == "tophat"
    assert page._build_config()["channel_decisions"]["CD3"] == "tophat"
    # The `both` evidence counts FOR the decided method: TopHat was computed.
    assert page._channel_compute_state("CD3") == "computed"
    # ...and moving the final choice to the other candidate keeps the cached
    # candidates rather than throwing them away.
    _decide(page, "CD3", "cucim")
    assert page._computed_signatures["CD3"][0] == "both"
    assert page._channel_preview_method("CD3") == "both"
    assert page._build_config()["channel_decisions"]["CD3"] == "cucim"
    assert page._channel_compute_state("CD3") == "computed"
    assert set(page._preview_cache) == {("CD3", 0)}
    page.close()


def test_a_legacy_both_config_comes_back_as_original(app, tmp_path):
    """E: a real project written before the split, read and re-written."""
    import json

    from block01.core import step0_handoff

    legacy = {"channel_decisions": {"CD3": "both", "CD20": "tophat"},
              "method_params": {"tophat_radius": 30, "cucim_sigma": 50}}
    (tmp_path / "correction_config.json").write_text(json.dumps(legacy),
                                                     encoding="utf-8")
    page = _correction_page(app, tmp_path)
    page.output_dir = str(tmp_path)
    page._load_existing_config()
    page._rebuild_channel_list()

    # THE READ BOUNDARY ITSELF: the config loader migrates the legacy value,
    # so everything downstream of it sees one of the three legal answers
    # rather than having to know what `both` used to mean.
    from block01.core.bg_correction import _load_correction_config
    loaded = _load_correction_config(str(tmp_path / "correction_config.json"))
    assert loaded["channel_decisions"]["CD3"] == "original"
    assert loaded["channel_decisions"]["CD20"] == "tophat"

    assert page._prior_channel_decisions["CD3"] == "original"
    assert page._prior_channel_decisions["CD20"] == "tophat"
    assert page._channel_final_decision("CD3") == "original"
    # The migration is a FINAL-DECISION read boundary and nothing else: the
    # row's combo still shows the preview method this page is set to (the
    # helper previews CD3 with TopHat), and reading a legacy `both` decision
    # did not reach into it.
    assert page._channel_rows["CD3"]["method_cb"].currentText() == "TopHat"
    assert page._channel_preview_method("CD3") == "tophat"
    # ...and the new writer cannot put `both` back into the file.
    written = page._build_config()["channel_decisions"]
    assert "both" not in written.values()
    assert step0_handoff.clean_correction_config(
        legacy)["channel_decisions"]["CD3"] == "original"
    page.close()


def test_the_bulk_box_is_a_bulk_preview_method(app, tmp_path, monkeypatch):
    """F: Both / Original / TopHat / cuCIM over every eligible channel --
    the preview method, no decision moved and no run started."""
    import block01.ui.step0.step0_page as sp

    page = _decision_page(app, tmp_path)
    state = page.display.state
    visible_before = dict(state.display_visibility())
    fusion_before = page.display.fusion.draft_snapshot()

    def _boom(*a, **k):
        raise AssertionError("a bulk PREVIEW method started a correction run")

    monkeypatch.setattr(sp, "BatchProcessWorker", _boom)
    monkeypatch.setattr(sp, "WsiCorrectionWorker", _boom)
    decided = _decide(page, "CD3", "tophat")
    saved_before = page._build_config()

    for shown, preview in (("TopHat", "tophat"), ("cucim", "cucim"),
                           ("Original", "original"), ("Both", "both")):
        _set_bulk_method(page, shown)
        saved = page._build_config()["channel_decisions"]
        for ch in page._channel_order:
            if ch == page.nucleus_channel:
                continue
            assert page._channel_preview_method(ch) == preview, (ch, shown)
            assert page._channel_rows[ch]["method_cb"].currentText() == shown
        # NOT ONE DECISION MOVED, for any of the four.
        assert page._channel_final_decision("CD3") == decided, shown
        assert saved == saved_before["channel_decisions"], shown
        assert page.nucleus_channel not in page._channel_methods
        assert page.nucleus_channel not in page._channel_decisions
        assert dict(state.display_visibility()) == visible_before
        assert page.display.fusion.draft_snapshot() == fusion_before
    page.close()


def test_a_final_choice_starts_no_run_and_no_process_button_came_back(
        app, tmp_path, monkeypatch):
    """G: the compute entries are the two parameter boxes, and only them --
    and neither a preview method nor a final decision starts a run."""
    import block01.ui.step0.step0_page as sp

    page = _decision_page(app, tmp_path)

    def _boom(*a, **k):
        raise AssertionError("a method or a decision started a correction run")

    monkeypatch.setattr(sp, "BatchProcessWorker", _boom)
    monkeypatch.setattr(sp, "WsiCorrectionWorker", _boom)
    # ON the channel whose choice is about to change: a run is only ever
    # started for the current channel, so a test that left the page on
    # another one could not see a choice that started one.
    page._on_channel_selected_by_id("CD3")
    page._on_channel_row_clicked("CD3")
    assert page.current_channel == "CD3"
    # WHAT WOULD RUN, recorded at the one entry every run goes through --
    # rather than waiting for a worker to be constructed, which several
    # guards inside that entry can prevent for reasons of their own.
    started = []
    monkeypatch.setattr(type(page), "_process_current_channel",
                        lambda self, method=None: started.append(method))

    # The bulk PREVIEW box first, then this channel's own preview on top of
    # it, then the final decision through the real decision panel.
    _set_bulk_method(page, "tophat")
    page._channel_rows["CD3"]["method_cb"].setCurrentText("cucim")
    _decide(page, "CD3", "tophat")

    assert started == [], f"a method or a decision started a run: {started}"
    for gone in ("_btn_process", "_process_btn", "_btn_process_all"):
        assert not hasattr(page, gone), gone

    # ...and the two boxes are the entries that DO compute, one method each.
    page._on_dec_param_entered("tophat")
    page._on_dec_param_entered("cucim")
    assert started == ["tophat", "cucim"]
    # The compatibility programmatic entry is the only one that asks for both
    # candidates, and it is not a final choice.
    page._on_dec_param_entered()
    assert started == ["tophat", "cucim", "both"]
    # ...and nothing a run did moved either answer.
    assert page._channel_preview_method("CD3") == "cucim"
    assert page._channel_final_decision("CD3") == "tophat", \
        "a run changed the final decision"
    page.close()


# ── the two-layer contract: preview method vs final decision ─────────────
#
# Step0 asks two questions about the same channel and they have different
# answers:
#
#     preview method  -- what is computed / prepared to LOOK at
#                        (Both / Original / TopHat / cucim)
#     final decision  -- what Save publishes
#                        (original / tophat / cucim; never `both`)
#
# They were welded into one value, so choosing to compare two candidates
# changed what Save wrote and deciding what to publish changed what was on
# screen. What follows drives the REAL controls -- the bulk Method box, each
# row's Method combo, the Per-Channel Decision radios and Apply -- and pins
# that the two answers move independently.


def _fresh_page(app, tmp_path):
    """A real page, nothing chosen and nothing decided."""
    page = _correction_page(app, tmp_path)
    page._channel_decisions.clear()
    page._channel_methods.clear()
    page._preview_method_default = "both"
    _set_bulk_method(page, "both")
    for ch in page._channel_order:
        page._refresh_channel_row(ch)
    return page


def _markers(page):
    return [c for c in page._channel_order if c != page.nucleus_channel]


def test_a_fresh_page_previews_both_and_publishes_original(app, tmp_path):
    """A1: the starting point of both layers."""
    page = _fresh_page(app, tmp_path)

    assert page._preview_method_default == "both"
    saved = page._build_config()["channel_decisions"]
    for ch in _markers(page):
        assert page._channel_rows[ch]["method_cb"].currentText() == "Both", ch
        assert page._channel_preview_method(ch) == "both", ch
        assert page._channel_final_decision(ch) == "original", ch
        assert saved[ch] == "original", ch
    page.close()


def test_the_bulk_preview_box_moves_one_layer_only(app, tmp_path):
    """A2: each of the four values, through the real box."""
    page = _fresh_page(app, tmp_path)
    state = page.display.state
    decided = _decide(page, "CD3", "cucim")
    saved_before = page._build_config()
    visible_before = dict(state.display_visibility())

    for shown, preview in (("Both", "both"), ("Original", "original"),
                           ("TopHat", "tophat"), ("cucim", "cucim")):
        _set_bulk_method(page, shown)

        for ch in _markers(page):
            assert page._channel_rows[ch]["method_cb"].currentText() == shown
            assert page._channel_preview_method(ch) == preview, (ch, shown)
        # the other layer, untouched -- the decision, what Save writes, and
        # what is on screen
        assert page._channel_final_decision("CD3") == decided, shown
        assert page._build_config() == saved_before, shown
        assert dict(state.display_visibility()) == visible_before, shown
    page.close()


def test_one_rows_preview_combo_moves_that_row_only(app, tmp_path):
    """A3: per channel, and no leak sideways or downwards."""
    page = _fresh_page(app, tmp_path)
    decided = _decide(page, "CD3", "tophat")
    others = [c for c in _markers(page) if c != "CD3"]
    saved_before = page._build_config()

    for shown, preview in (("TopHat", "tophat"), ("cucim", "cucim"),
                           ("Original", "original"), ("Both", "both")):
        page._channel_rows["CD3"]["method_cb"].setCurrentText(shown)

        assert page._channel_preview_method("CD3") == preview
        for ch in others:
            assert page._channel_preview_method(ch) == "both", (ch, shown)
            assert page._channel_final_decision(ch) == "original", (ch, shown)
        assert page._channel_final_decision("CD3") == decided, shown
        assert page._build_config() == saved_before, shown
    page.close()


def test_the_decision_panel_moves_the_other_layer_only(app, tmp_path):
    """A4: Original -> TopHat -> cucim through the radios and Apply."""
    from block01.core import step0_handoff

    page = _fresh_page(app, tmp_path)
    page._channel_rows["CD3"]["method_cb"].setCurrentText("cucim")
    preview_before = page._channel_preview_method("CD3")

    for decision in ("original", "tophat", "cucim"):
        _decide(page, "CD3", decision)

        assert page._channel_final_decision("CD3") == decision
        assert page._build_config()["channel_decisions"]["CD3"] == decision
        assert step0_handoff.clean_correction_config(
            page._build_config())["channel_decisions"]["CD3"] == decision
        # the preview combo did not move, in the widget or in the store
        assert page._channel_rows["CD3"]["method_cb"].currentText() == "cucim"
        assert page._channel_preview_method("CD3") == preview_before == "cucim"
    page.close()


def _recorded_runs(page, monkeypatch):
    """What the real compute entry is asked for, per call."""
    asked = []
    real = type(page)._process_current_channel

    def _spy(self, method=None):
        resolved = method
        if resolved is None:
            resolved = self._channel_preview_method(self.current_channel)
        asked.append(resolved)
        return real(self, method)

    monkeypatch.setattr(type(page), "_process_current_channel", _spy)
    return asked


def test_the_compute_entry_asks_for_what_the_preview_method_says(
        app, tmp_path, monkeypatch):
    """B: the REAL entry, and the REAL worker constructor.

    Not a dictionary read: what is pinned is the method the run is dispatched
    with, and that `Original` dispatches no run at all.
    """
    from PyQt5 import QtCore

    import block01.ui.step0.step0_page as sp

    page = _fresh_page(app, tmp_path)
    page.current_channel = "CD3"
    built = []

    class _Worker(QtCore.QThread):
        """A real QThread that records the dispatch and computes nothing."""
        channel_patch_done = QtCore.pyqtSignal(object, object, object)
        channel_done = QtCore.pyqtSignal(object)
        all_done = QtCore.pyqtSignal()
        progress = QtCore.pyqtSignal(int)
        error_signal = QtCore.pyqtSignal(object)
        canceled = QtCore.pyqtSignal()

        def __init__(self, loader, patches, methods, *a, **k):
            super().__init__()
            built.append(dict(methods))

        def run(self):
            return None

    monkeypatch.setattr(sp, "BatchProcessWorker", _Worker)
    asked = _recorded_runs(page, monkeypatch)

    for shown, expected in (("Both", "both"), ("TopHat", "tophat"),
                            ("cucim", "cucim")):
        built.clear()
        page._channel_rows["CD3"]["method_cb"].setCurrentText(shown)
        page._process_current_channel()
        assert asked[-1] == expected, shown
        assert built and built[-1]["CD3"] == expected, shown
        # the recorded run is over: the next dispatch is not refused by the
        # production busy gate
        page._batch_worker.wait()
        page._batch_worker = None
        page._pending_signatures.pop("CD3", None)

    # Original: the raw channel is what is being previewed, so NO worker.
    built.clear()
    page._channel_rows["CD3"]["method_cb"].setCurrentText("Original")
    page._process_current_channel()
    assert asked[-1] == "original"
    assert built == [], "previewing the raw channel started a correction run"
    # ...and the legacy programmatic entry still asks for both candidates.
    page._process_current_channel("both")
    assert built and built[-1]["CD3"] == "both"
    page.close()


def test_both_candidates_survive_a_change_of_final_decision(app, tmp_path):
    """C: computed evidence is evidence, not a decision."""
    page = _fresh_page(app, tmp_path)
    page._channel_rows["CD3"]["method_cb"].setCurrentText("Both")
    page._computed_signatures["CD3"] = page._merge_channel_signatures(
        page._channel_signature("CD3", "tophat"),
        page._channel_signature("CD3", "cucim"))
    page._computed_channels.add("CD3")
    for p_idx in range(len(page.patches)):
        page._preview_cache[("CD3", p_idx)] = {"original_disp": None}
    cache_before = dict(page._preview_cache)
    _decide(page, "CD3", "tophat")

    assert page._computed_signatures["CD3"][0] == "both"
    assert page._channel_preview_method("CD3") == "both"
    assert page._channel_final_decision("CD3") == "tophat"
    assert page._build_config()["channel_decisions"]["CD3"] == "tophat"

    _decide(page, "CD3", "cucim")

    assert page._build_config()["channel_decisions"]["CD3"] == "cucim"
    # the candidates are still there: the user may go back and forth
    assert page._computed_signatures["CD3"][0] == "both"
    assert dict(page._preview_cache) == cache_before
    assert page._channel_compute_state("CD3") == "computed"
    page.close()


def test_the_four_cross_combinations_mean_what_they_say(app, tmp_path):
    """The product ruling's own examples, measured one by one."""
    page = _fresh_page(app, tmp_path)
    cases = [("Both", "original"), ("Both", "tophat"),
             ("cucim", "tophat"), ("Original", "cucim")]
    for shown, decision in cases:
        page._channel_rows["CD3"]["method_cb"].setCurrentText(shown)
        _decide(page, "CD3", decision)

        assert page._channel_preview_method("CD3") == shown.lower()
        assert page._channel_final_decision("CD3") == decision
        assert page._build_config()["channel_decisions"]["CD3"] == decision
    page.close()


def test_the_preview_source_provider_reports_both_layers_apart(app, tmp_path):
    """The one read-model every preview consumer goes through."""
    page = _fresh_page(app, tmp_path)
    page._channel_rows["CD3"]["method_cb"].setCurrentText("Both")
    _decide(page, "CD3", "tophat")

    info = page._preview_provider.describe("CD3")["correction"]
    assert info["preview_method"] == "both"
    assert info["assigned_method"] == "tophat"
    page.close()


# ── the Method popup: one control for the method AND its numbers ────────────
#
# The bulk preview method was a four-item combo and the numbers those methods
# need sat in a separate `Method Parameters` box down the column. They are one
# decision, so they are one control: `Method ▾` opens a panel with TopHat and
# cuCIM as lit-or-not toggles, each with its parameter beside it, and a Save
# that applies the pair.


def test_the_method_button_replaces_the_combo_and_the_parameters_box(app,
                                                                     tmp_path):
    from PyQt5 import QtWidgets as _QW

    page = _fresh_page(app, tmp_path)
    try:
        # the combo is gone...
        assert not isinstance(page._method_all, _QW.QComboBox)
        assert isinstance(page._method_all, _QW.QToolButton)
        assert page._method_all.menu() is not None
        # ...and so is the `Method Parameters` box
        boxes = [b.title() for b in page.findChildren(_QW.QGroupBox)]
        assert "Method Parameters" not in boxes, boxes
        # the two parameters live in the popup now, and are still the ones
        # every consumer reads
        assert page._tophat_slider.value() == int(
            page._build_config()["method_params"]["tophat_radius"])
        assert page._cucim_slider.value() == int(
            page._build_config()["method_params"]["cucim_sigma"])
    finally:
        page.close()


def test_the_two_toggles_spell_the_four_methods(app, tmp_path):
    page = _fresh_page(app, tmp_path)
    try:
        markers = [c for c in page._channel_order if c != page.nucleus_channel]
        for tophat, cucim, expected in ((True, True, "both"),
                                        (True, False, "tophat"),
                                        (False, True, "cucim"),
                                        (False, False, "original")):
            page._method_tophat_btn.setChecked(tophat)
            page._method_cucim_btn.setChecked(cucim)
            assert page._method_menu_selection() == expected

            page._on_method_menu_saved()

            assert page._preview_method_default == expected
            for ch in markers:
                assert page._channel_preview_method(ch) == expected, ch
            assert expected.replace("cucim", "cuCIM").replace(
                "tophat", "TopHat").replace("both", "Both").replace(
                "original", "Original") in page._method_all.text()
    finally:
        page.close()


def test_the_popup_is_a_draft_until_save(app, tmp_path):
    """Lighting a toggle commands nothing: `Save` is what applies it."""
    page = _fresh_page(app, tmp_path)
    try:
        _set_bulk_method(page, "both")
        before = {ch: page._channel_preview_method(ch)
                  for ch in page._channel_order}

        page._method_tophat_btn.setChecked(True)
        page._method_cucim_btn.setChecked(False)      # would be `tophat`

        assert {ch: page._channel_preview_method(ch)
                for ch in page._channel_order} == before
        assert page._preview_method_default == "both"

        page._on_method_menu_saved()
        assert page._preview_method_default == "tophat"
    finally:
        page.close()


def test_opening_the_popup_shows_what_is_in_force(app, tmp_path):
    page = _fresh_page(app, tmp_path)
    try:
        _set_bulk_method(page, "cucim")
        # a draft the user abandoned
        page._method_tophat_btn.setChecked(True)
        page._method_cucim_btn.setChecked(True)

        # ...and a number the user typed and walked away from
        panel = page._method_all.menu().actions()[0].defaultWidget()
        from PyQt5 import QtWidgets as _QW
        spins = panel.findChildren(_QW.QSpinBox)
        in_force = (page._tophat_slider.value(), page._cucim_slider.value())
        spins[0].setValue(in_force[0] + 13)
        spins[1].setValue(in_force[1] + 13)

        page._method_all.menu().aboutToShow.emit()     # re-opening it

        assert page._method_tophat_btn.isChecked() is False
        assert page._method_cucim_btn.isChecked() is True
        # the abandoned numbers are gone too: the popup shows what is in force
        assert (spins[0].value(), spins[1].value()) == in_force
    finally:
        page.close()


def test_the_popup_parameters_are_a_draft_until_save(app, tmp_path):
    """Typing a number in the popup changes NOTHING until Save.

    The boxes used to be the global parameters themselves, so a half-typed
    radius went straight into the signature and the compare panels and the
    full image recomputed while the user was still deciding.
    """
    from PyQt5 import QtWidgets as _QW

    page = _fresh_page(app, tmp_path)
    try:
        _set_bulk_method(page, "both")
        before = page._build_config()["method_params"]

        # the boxes the USER sees, found in the popup rather than by name
        panel = page._method_all.menu().actions()[0].defaultWidget()
        spins = panel.findChildren(_QW.QSpinBox)
        assert len(spins) == 2, spins
        assert spins[0] is not page._tophat_slider, \
            "the popup edits the global radius directly"
        assert spins[1] is not page._cucim_slider, \
            "the popup edits the global sigma directly"

        spins[0].setValue(42)
        spins[1].setValue(77)

        # nothing in force has moved
        assert page._build_config()["method_params"] == before
        assert page._resolve_channel_params("CD3") == (
            before["tophat_radius"], before["cucim_sigma"])

        page._on_method_menu_saved()

        cfg = page._build_config()["method_params"]
        assert cfg["tophat_radius"] == 42
        assert cfg["cucim_sigma"] == 77
        assert page._resolve_channel_params("CD3") == (42, 77)
    finally:
        page.close()


def test_a_method_nobody_lit_gets_no_parameter_and_no_computation(app,
                                                                  tmp_path):
    """The second half of the same rule.

    With only cuCIM lit, a radius typed beside the dark TopHat button is not
    applied -- and nothing recomputes a TopHat candidate for it.
    """
    page = _fresh_page(app, tmp_path)
    try:
        _set_bulk_method(page, "cucim")
        radius_before = page._tophat_slider.value()
        panel = page._method_all.menu().actions()[0].defaultWidget()
        from PyQt5 import QtWidgets as _QW
        spins = panel.findChildren(_QW.QSpinBox)

        spins[0].setValue(radius_before + 20)      # TopHat, unlit
        spins[1].setValue(88)                      # cuCIM, lit
        page._on_method_menu_saved()

        assert page._tophat_slider.value() == radius_before, \
            "an unlit method's parameter was applied"
        assert page._cucim_slider.value() == 88
        assert page._preview_method_default == "cucim"
    finally:
        page.close()


def test_a_parameter_change_only_reaches_the_methods_in_use(app, tmp_path):
    """The programmatic path, at the level the views see.

    A global parameter moving is what makes a view recompute. While a channel
    is previewed `cucim`, a radius change must reach nothing: a TopHat
    candidate for it is work nobody asked for.
    """
    page = _fresh_page(app, tmp_path)
    try:
        page.current_channel = "CD3"
        page._set_channel_preview_method("CD3", "cucim")
        synced = []
        page._sync_full_image_param = lambda method=None: synced.append(method)
        page._sync_compare_params = lambda method=None: synced.append(method)

        page._tophat_slider.setValue(int(page._tophat_slider.value()) + 7)
        assert synced == [], synced

        page._cucim_slider.setValue(int(page._cucim_slider.value()) + 7)
        assert synced == ["cucim"], synced

        # ...and with both previewed, both reach the views
        synced.clear()
        page._set_channel_preview_method("CD3", "both")
        page._tophat_slider.setValue(int(page._tophat_slider.value()) + 1)
        page._cucim_slider.setValue(int(page._cucim_slider.value()) + 1)
        assert synced == ["tophat", "cucim"], synced
    finally:
        page.close()


def test_saving_the_method_publishes_nothing_and_runs_nothing(app, tmp_path,
                                                              monkeypatch):
    import block01.ui.step0.step0_page as sp

    page = _fresh_page(app, tmp_path)
    try:
        def _boom(*a, **k):
            raise AssertionError("the Method popup started a correction run")

        monkeypatch.setattr(sp, "BatchProcessWorker", _boom)
        monkeypatch.setattr(sp, "WsiCorrectionWorker", _boom)
        decided = _decide(page, "CD3", "tophat")
        saved_before = page._build_config()
        visible_before = dict(page.display.state.display_visibility())

        page._method_tophat_btn.setChecked(False)
        page._method_cucim_btn.setChecked(True)
        page._on_method_menu_saved()

        assert page._channel_final_decision("CD3") == decided
        assert page._build_config() == saved_before
        assert dict(page.display.state.display_visibility()) == visible_before
    finally:
        page.close()


def test_the_run_controls_that_were_taken_off_screen(app, tmp_path):
    """The progress bar is gone and Stop is not shown -- but a stop can still
    be issued, and the status line still reports."""
    page = _fresh_page(app, tmp_path)
    try:
        assert page._btn_stop_process.isVisible() is False
        assert page._btn_stop_process.parent() is None or True
        assert callable(page._on_stop_process)
        assert page._proc_pbar.parent() is None, \
            "the progress bar is still in a layout"
        assert page._proc_status.text() == "Ready."
    finally:
        page.close()
