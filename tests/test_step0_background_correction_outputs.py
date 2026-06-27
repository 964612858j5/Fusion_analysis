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


@pytest.fixture(scope="module")
def app():
    from PyQt5 import QtWidgets
    a = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return a


# ── validity report: empty group is NOT a valid corrected output ─────────────
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
    methods, bboxes = read_corrected_zarr_state(zp)
    assert methods == {"CD68": "tophat", "CK19": "cucim"}
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
    methods, _ = read_corrected_zarr_state(zp)
    assert methods == {"CD68": "tophat", "Ki67": "tophat"}
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
    methods, _ = read_corrected_zarr_state(str(tmp_path / "corrected_channels.zarr"))
    assert methods["CD68"] == "cucim"          # changed
    assert methods["Ki67"] == "tophat"         # untouched


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
    # seed cache with stale values for both channels
    p._preload_cache = {0: {"CD68": np.zeros((20, 20), np.float32),
                            "Ki67": np.zeros((20, 20), np.float32)}}
    # hot-swap restricted to CD68 only
    p._hotswap_corrected({"CD68": "tophat", "Ki67": "tophat"}, only={"CD68"})
    assert float(p._preload_cache[0]["CD68"].mean()) == 7.0   # reprocessed -> updated
    assert float(p._preload_cache[0]["Ki67"].mean()) == 0.0   # skipped -> untouched


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

    # the corrected zarr already holds both channels with the SAME methods
    monkeypatch.setattr(sp, "read_corrected_zarr_state",
                        lambda zp: ({"CD68": "tophat", "Ki67": "tophat"},
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
