"""C4.5d: full Step0 Save is authority over stale geometry-only work.

A geometry-only ROI edit must block Step1: old corrected pixels belong to old
ROI. Full Save publishes fresh canonical handoff. This module proves only that
successful canonical boundary retires old worker failure/work; failures, newer
edits, stale datasets, and late workers still fail closed.

All files are synthetic under pytest tmp_path. Real GeometryPersistWorker and
real handoff writer/manifest code are used throughout.
"""

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")
pytest.importorskip("zarr")

from PyQt5 import QtWidgets  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _roi(bbox=(0, 32, 0, 32), name="ROI_1"):
    y0, y1, x0, x1 = bbox
    return {"name": name, "bbox_fullres": [y0, y1, x0, x1],
            "polygon_fullres": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}


class _Loader:
    shape = (96, 96)
    ch_map = {"DAPI": 0, "CD3": 1}

    def __init__(self, raw):
        self.filepath = str(raw)

    def channel_names(self):
        return ["DAPI", "CD3"]


def _page(app, tmp_path):
    """Real Step0Page with one real published synthetic handoff."""
    from block01.ui.step0 import step0_page as sp

    tmp_path.mkdir(parents=True, exist_ok=True)
    raw = tmp_path / "raw.ome.tif"
    raw.write_bytes(b"raw-geometry-authority")
    page = sp.Step0Page()
    page.loader = _Loader(raw)
    page.ome_path = str(raw)
    page.output_dir = str(tmp_path)
    page.panel_csv_path = ""
    page.panel_groups = {}
    page.nucleus_channel = "DAPI"
    page.overview.loader = page.loader
    page.overview.full_h = page.overview.full_w = 96
    page.overview.full_wsi_mode = False
    page.overview._rois = [_roi()]
    page.overview._patches = [{"roi_idx": 0, "coords": (0, 16, 0, 16)}]
    page.patches = [(0, 16, 0, 16)]

    step0_dir = tmp_path / "roi-1" / "step0"
    step0_dir.mkdir(parents=True)
    page._roi_context = {
        "roi_id": "roi-1", "roi_dir": str(tmp_path / "roi-1"),
        "project_dir": str(tmp_path),
        "step_dirs": {"step0": str(step0_dir),
                      "step1": str(tmp_path / "roi-1" / "step1"),
                      "step2": str(tmp_path / "roi-1" / "step2")},
    }
    page._roi_context_sig = page._roi_context_signature(page._standard_rois())
    zarr_path = str(step0_dir / "corrected_channels.zarr")
    page._write_step0_handoff(_config(), zarr_path)
    return page, step0_dir, zarr_path


def _config():
    return {"method_params": {"tophat_radius": 25, "cucim_sigma": 30},
            "channel_decisions": {}}


def _manifest(step0_dir):
    with open(step0_dir / "step0_roi_result.json", encoding="utf-8") as f:
        return json.load(f)


def _settle(page, timeout=10.0):
    deadline = time.monotonic() + timeout
    worker = getattr(page, "_geometry_persist_worker", None)
    while time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        if worker is None or not worker.is_busy():
            QtWidgets.QApplication.processEvents()
            return page.geometry_persist_state()
        time.sleep(0.003)
    raise AssertionError("geometry worker did not settle")


def _blocked_new_roi(page):
    """Real geometry-only refusal; page keeps new ROI in memory."""
    page.overview._rois = [_roi((0, 48, 0, 48), "ROI_new")]
    assert page._persist_geometry_edit() is True
    assert _settle(page) == "failed"
    return page._geometry_persist_worker


def _entry_probe(page):
    """Actual MainWindow gate against actual page/worker state, no fake ready."""
    from block01.ui.main_window import MainWindow

    class _Status:
        def __init__(self): self.text = ""
        def setText(self, value): self.text = str(value)

    class _Stack:
        def __init__(self): self.index = 0
        def setCurrentIndex(self, value): self.index = int(value)

    window = MainWindow.__new__(MainWindow)
    window._step0 = page
    window._step1_context_ready = True
    window._step1_entry_waits = 40  # block reports immediately; no QTimer retry
    window.prev_status = _Status()
    window._stack = _Stack()
    window._set_step_active = lambda _step: None
    window._log_step1_layout = lambda _reason: None
    return window


def _assert_no_staged(step0_dir):
    assert not list(step0_dir.glob("*.tmp.*"))


def test_roi_block_then_real_full_save_unblocks_actual_step1_gate(app, tmp_path):
    page, step0_dir, zarr_path = _page(app, tmp_path)
    try:
        worker = _blocked_new_roi(page)
        before = _manifest(step0_dir)
        window = _entry_probe(page)

        assert page.geometry_ready_for_consumers() is False
        assert page.geometry_blocked_reason() == "roi_changed"
        window._go_to_step1()
        assert window._stack.index == 0

        _config_out, rois, _patches, manifest = page._write_step0_handoff(
            _config(), zarr_path)
        revision = int(manifest["geometry_revision"])
        assert rois[0]["bbox_fullres"] == [0, 48, 0, 48]
        with open(manifest["roi_config_path"], encoding="utf-8") as f:
            assert json.load(f)[0]["bbox_fullres"] == [0, 48, 0, 48]
        assert worker.consumable(int(page._dataset_gen), revision) is True
        assert page.geometry_ready_for_consumers() is True
        assert page.geometry_blocked_reason() is None

        window._go_to_step1()
        assert window._stack.index == 1
    finally:
        page.stop_background_jobs()
        page.deleteLater()


@pytest.mark.parametrize("failure", ["corrected", "handoff", "manifest"])
def test_failed_full_save_keeps_roi_block_and_old_manifest(
        app, tmp_path, monkeypatch, failure):
    """No early authority: three writer failure sites retain fail-closed state."""
    from block01.core import step0_handoff

    page, step0_dir, zarr_path = _page(app, tmp_path)
    try:
        worker = _blocked_new_roi(page)
        before = _manifest(step0_dir)
        called = []
        real_adopt = worker.adopt_authoritative_publish

        def record_adopt(*args, **kwargs):
            called.append((args, kwargs))
            return real_adopt(*args, **kwargs)

        monkeypatch.setattr(worker, "adopt_authoritative_publish", record_adopt)
        if failure == "corrected":
            missing = str(step0_dir / "new-corrected.zarr")
            monkeypatch.setattr(step0_handoff, "ensure_empty_corrected_zarr",
                                lambda *_a, **_k: (_ for _ in ()).throw(
                                    RuntimeError("corrected product failed")))
            target = missing
        elif failure == "handoff":
            monkeypatch.setattr(step0_handoff, "_write_json_staged",
                                lambda *_a, **_k: (_ for _ in ()).throw(
                                    RuntimeError("handoff write failed")))
            target = zarr_path
        else:
            real_replace = step0_handoff.os.replace

            def reject_manifest(src, dst):
                if os.path.abspath(str(dst)) == os.path.abspath(
                        str(step0_dir / "step0_roi_result.json")):
                    raise OSError("manifest publish failed")
                return real_replace(src, dst)

            monkeypatch.setattr(step0_handoff.os, "replace", reject_manifest)
            target = zarr_path

        with pytest.raises((RuntimeError, OSError)):
            page._write_step0_handoff(_config(), target)
        assert called == []
        assert _manifest(step0_dir) == before
        assert worker.blocked()[1] == "roi_changed"
        assert page.geometry_ready_for_consumers() is False
        probe = _entry_probe(page)
        probe._go_to_step1()
        assert probe._stack.index == 0
        _assert_no_staged(step0_dir)
    finally:
        page.stop_background_jobs()
        page.deleteLater()


def test_authority_retires_late_failure_without_invalidating_new_handoff(
        app, tmp_path, monkeypatch):
    """Old worker result released after Save cannot re-block or emit failure."""
    from block01.core import step0_handoff

    page, step0_dir, zarr_path = _page(app, tmp_path)
    try:
        worker = page._geometry_persist()
        started, release = threading.Event(), threading.Event()
        failed = []
        worker.failed.connect(failed.append)

        def late_failure(_task, **_kwargs):
            started.set()
            assert release.wait(5.0)
            raise RuntimeError("old geometry failed late")

        monkeypatch.setattr(step0_handoff, "commit_geometry_only", late_failure)
        page.overview._patches.append(
            {"roi_idx": 0, "coords": (16, 32, 16, 32)})
        assert page._persist_geometry_edit() is True
        assert started.wait(3.0)
        old_revision = page._geometry_revision

        _config_out, _rois, _patches, manifest = page._write_step0_handoff(
            _config(), zarr_path)
        assert int(manifest["geometry_revision"]) >= old_revision
        canonical = _manifest(step0_dir)
        release.set()
        assert _settle(page) in ("saving", "published", "staged", "failed")
        QtWidgets.QApplication.processEvents()

        assert _manifest(step0_dir) == canonical
        assert worker.blocked() is None
        assert worker.consumable(int(page._dataset_gen),
                                 int(manifest["geometry_revision"])) is True
        assert failed == []
    finally:
        page.stop_background_jobs()
        page.deleteLater()


def test_old_publish_waits_for_save_then_is_superseded_without_overwrite(
        app, tmp_path, monkeypatch):
    """Real commit is staged while Save owns barrier; final check sees authority."""
    from block01.core import step0_handoff

    page, step0_dir, zarr_path = _page(app, tmp_path)
    try:
        worker = page._geometry_persist()
        started = threading.Event()
        published = []
        worker.published.connect(published.append)
        real_commit = step0_handoff.commit_geometry_only

        def started_commit(task, **kwargs):
            started.set()
            return real_commit(task, **kwargs)

        monkeypatch.setattr(step0_handoff, "commit_geometry_only", started_commit)
        page.overview._patches.append(
            {"roi_idx": 0, "coords": (16, 32, 16, 32)})
        with worker.authoritative_publish_barrier():
            assert page._persist_geometry_edit() is True
            assert started.wait(3.0)
            # Worker stages real revision while blocked at shared final lock.
            _config_out, _rois, _patches, manifest = page._write_step0_handoff(
                _config(), zarr_path)
            canonical = _manifest(step0_dir)
        assert _settle(page) in ("saving", "published", "staged", "failed")
        QtWidgets.QApplication.processEvents()

        assert _manifest(step0_dir) == canonical
        assert worker.blocked() is None
        assert worker.consumable(int(page._dataset_gen),
                                 int(manifest["geometry_revision"])) is True
        assert published == []
    finally:
        page.stop_background_jobs()
        page.deleteLater()


def test_new_edit_after_save_blocks_then_recovers_normally(app, tmp_path):
    page, _step0_dir, zarr_path = _page(app, tmp_path)
    try:
        worker = _blocked_new_roi(page)
        _config_out, _rois, _patches, manifest = page._write_step0_handoff(
            _config(), zarr_path)
        saved = int(manifest["geometry_revision"])
        assert worker.consumable(int(page._dataset_gen), saved)
        # Full Save's normal ROI-context publication occurs before this point.
        # This minimal synthetic fixture has one fixed directory, so mirror the
        # new geometry baseline before asking geometry-only patch persistence.
        page._roi_context_sig = page._roi_context_signature(
            page._standard_rois())

        # New patch edit is newer than authority. It must become unsafe now,
        # then return to normal geometry-only persistence behavior.
        page.overview._patches.append(
            {"roi_idx": 0, "coords": (48, 64, 48, 64)})
        assert page._persist_geometry_edit() is True
        assert page.geometry_ready_for_consumers() is False
        assert _settle(page) == "published"
        assert page.geometry_ready_for_consumers() is True
        assert worker.published_revision() > saved
    finally:
        page.stop_background_jobs()
        page.deleteLater()


def test_authority_is_dataset_scoped_and_no_worker_save_stays_compatible(
        app, tmp_path):
    from block01.workers.geometry_persist_worker import GeometryPersistWorker

    page, _step0_dir, zarr_path = _page(app, tmp_path)
    try:
        assert getattr(page, "_geometry_persist_worker", None) is None
        _config_out, _rois, _patches, manifest = page._write_step0_handoff(
            _config(), zarr_path)
        assert getattr(page, "_geometry_persist_worker", None) is None
        assert page.geometry_ready_for_consumers() is True

        worker = GeometryPersistWorker()
        worker.invalidate(20)
        worker.submit({"dataset_gen": 20, "revision": 3})
        # Pending B is not released by stale A, even with equal/reused number.
        assert worker.adopt_authoritative_publish(19, 3) is False
        assert worker.consumable(20, 3) is False
        worker.invalidate(21)
        assert worker.adopt_authoritative_publish(20, 99) is False
        assert worker.blocked() is None
        assert worker.consumable(21, 0) is True
    finally:
        page.stop_background_jobs()
        page.deleteLater()


def test_emit_complete_adopts_only_after_real_success_and_before_signal(
        app, tmp_path, monkeypatch):
    """Page-level ordering: Save emit sees authority; failed Save never does."""
    from block01.ui.step0 import step0_page as step0_page_module
    from block01.core import step0_handoff

    page, step0_dir, zarr_path = _page(app, tmp_path)
    try:
        worker = _blocked_new_roi(page)
        order = []
        real_adopt = worker.adopt_authoritative_publish

        def record_adopt(*args, **kwargs):
            order.append("adopt")
            return real_adopt(*args, **kwargs)

        worker.adopt_authoritative_publish = record_adopt
        page.step0_complete.connect(lambda _payload: order.append("emit"))
        monkeypatch.setattr(page, "_persist_step0_remap_config", lambda: True)
        assert page._emit_complete(_config(), zarr_path, {}) is True
        assert order == ["adopt", "emit"]
        assert worker.consumable(int(page._dataset_gen),
                                 int(_manifest(step0_dir)["geometry_revision"]))

        # Fresh blocked page: writer failure is before authoritative adoption.
        page.stop_background_jobs()
        page.deleteLater()
        page, step0_dir, zarr_path = _page(app, tmp_path / "failed")
        worker = _blocked_new_roi(page)
        calls = []
        worker.adopt_authoritative_publish = lambda *_a, **_k: calls.append(1)
        monkeypatch.setattr(page, "_persist_step0_remap_config", lambda: True)
        monkeypatch.setattr(step0_page_module.QMessageBox, "warning",
                            lambda *_a, **_k: None)
        monkeypatch.setattr(step0_handoff, "write_handoff",
                            lambda *_a, **_k: (_ for _ in ()).throw(
                                RuntimeError("writer failed")))
        assert page._emit_complete(_config(), zarr_path, {}) is False
        assert calls == []
        assert worker.blocked()[1] == "roi_changed"
    finally:
        page.stop_background_jobs()
        page.deleteLater()


def test_late_old_invalidation_cannot_clear_new_saved_context(app, tmp_path):
    """A queued old failure is data for the old ROI, not the new Save."""
    from block01.ui.main_window import MainWindow

    window = MainWindow.__new__(MainWindow)
    discarded = []
    window.step0_output = {
        "step0_manifest_path": str(tmp_path / "new" / "step0_roi_result.json"),
        "roi_id": "roi-new",
        "roi_dir": str(tmp_path / "new"),
        "geometry_revision": 2,
    }
    window._step0 = SimpleNamespace(_dataset_gen=7)
    window._forget_fusion_settings = lambda _reason: discarded.append("forget")
    window._discard_step1_dataset_state = lambda **_kw: discarded.append("discard")
    window._return_to_step0 = lambda _reason: discarded.append("return")
    window._update_next_button = lambda: discarded.append("update")

    window._on_step0_handoff_invalidated({
        "step0_manifest_path": window.step0_output["step0_manifest_path"],
        "dataset_gen": 7,
        "geometry_revision": 1,
        "roi_id": "roi-old",
        "roi_dir": str(tmp_path / "old"),
        "reason": "roi_changed",
    })

    assert discarded == []


def test_real_save_entry_mints_new_roi_and_clears_worker_authority(
        app, tmp_path, monkeypatch):
    """The actual Step0 Save entry, not a direct handoff writer call."""
    from block01.ui.step0.step0_page import Step0Page
    import block01.ui.step0.step0_page as step0_module

    class SaveLoader(_Loader):
        def set_correction_config(self, _config):
            pass

        def set_corrected_zarr_store(self, _path, _decisions):
            pass

    raw = tmp_path / "real-save.ome.tif"
    raw.write_bytes(b"raw-save")
    page = Step0Page()
    page.loader = SaveLoader(raw)
    page.ome_path = str(raw)
    page.output_dir = str(tmp_path)
    page.panel_csv_path = ""
    page.panel_groups = {}
    page.nucleus_channel = "DAPI"
    page._analysis_region_mode = "roi"
    page._channel_order = ["DAPI"]
    page._channel_decisions = {"DAPI": "original"}
    page.overview.loader = page.loader
    page.overview.full_h = page.overview.full_w = 96
    page.overview._rois = [_roi((0, 32, 0, 32), "ROI_1")]
    page.overview._patches = [{"roi_idx": 0, "coords": (0, 16, 0, 16)}]
    page.patches = [(0, 16, 0, 16)]
    monkeypatch.setattr(page, "_confirm_raw_channels", lambda: True)
    monkeypatch.setattr(page, "_persist_step0_remap_config", lambda: True)
    monkeypatch.setattr(step0_module.QMessageBox, "information",
                        lambda *_a, **_k: None)
    try:
        page._save_and_continue()
        first = page._roi_context
        assert first is not None
        page.overview._rois = [_roi((0, 48, 0, 48), "ROI_new")]
        assert page._persist_geometry_edit() is True
        worker = page._geometry_persist_worker
        assert worker.wait_idle()
        assert page.geometry_blocked_reason() == "roi_changed"

        page._save_and_continue()
        assert page._roi_context is not first
        assert page.geometry_ready_for_consumers() is True
        assert page.geometry_blocked_reason() is None
        manifest = _manifest(Path(page._roi_context["step_dirs"]["step0"]))
        assert manifest["roi_id"] == page._roi_context["roi_id"]
        assert manifest["bbox_fullres"] == [0, 48, 0, 48]
    finally:
        page.stop_background_jobs()
        page.deleteLater()


def test_same_window_full_save_rebinds_mainwindow_and_reenters_step1(
        app, tmp_path, monkeypatch):
    """Real Save signal, authoritative reader and entry gate in one window."""
    import block01.ui.main_window as main_window_module
    from block01.ui.main_window import MainWindow

    class SaveLoader(_Loader):
        def set_correction_config(self, _config):
            pass

        def set_corrected_zarr_store(self, _path, _decisions):
            pass

    raw = tmp_path / "same-window.ome.tif"
    raw.write_bytes(b"same-window-raw")
    w = MainWindow()
    page = w._step0
    page.loader = SaveLoader(raw)
    page.ome_path = str(raw)
    page.output_dir = str(tmp_path)
    page.panel_csv_path = ""
    page.panel_groups = {}
    page.nucleus_channel = "DAPI"
    page._analysis_region_mode = "roi"
    page._channel_order = ["DAPI"]
    page._channel_decisions = {"DAPI": "original"}
    page.overview.loader = page.loader
    page.overview.full_h = page.overview.full_w = 96
    page.overview._rois = [_roi((0, 32, 0, 32), "ROI_1")]
    page.overview._patches = [{"roi_idx": 0, "coords": (0, 16, 0, 16)}]
    page.patches = [(0, 16, 0, 16)]
    monkeypatch.setattr(page, "_confirm_raw_channels", lambda: True)
    monkeypatch.setattr(page, "_persist_step0_remap_config", lambda: True)
    monkeypatch.setattr(main_window_module.QMessageBox, "information",
                        lambda *_a, **_k: None)
    monkeypatch.setattr(main_window_module.QMessageBox, "warning",
                        lambda *_a, **_k: None)
    try:
        page._save_and_continue()
        assert w._step1_context_ready is True
        w._go_to_step1()
        assert w._stack.currentIndex() == 1

        w._go_to_step0()
        page.overview._rois = [_roi((0, 48, 0, 48), "ROI_new")]
        assert page._persist_geometry_edit() is True
        worker = page._geometry_persist_worker
        assert worker.wait_idle()
        assert page.geometry_blocked_reason() == "roi_changed"
        w._go_to_step1()
        assert w._stack.currentIndex() == 0

        page._save_and_continue()
        assert w._step1_context_ready is True
        assert w.step0_output["roi_id"] == page._roi_context["roi_id"]
        assert w.step0_output["geometry_revision"] >= 1
        assert page.geometry_ready_for_consumers() is True
        w._go_to_step1()
        assert w._stack.currentIndex() == 1
        QtWidgets.QApplication.processEvents()
        assert w._step1_context_ready is True
        assert w._stack.currentIndex() == 1
    finally:
        page.stop_background_jobs()
        w.close()
