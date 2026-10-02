"""Block A6 (E5): a job's late answer never changes what the user has moved to.

Every test here is a controlled delay: the old job is held at a known point,
the user's next action happens, then the old job is let go. Each one was run
on the pre-A6 code first and failed there (the reverse injection the
application's acceptance gate asks for).

G1  fusion cancel / restart: the UI unlocks only after the thread has ended;
    each run builds in its own temporary store; a stop pressed after the last
    tile still keeps the result off the real path.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import glob
import os
import threading

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")
zarr = pytest.importorskip("zarr")

from PyQt5 import QtCore, QtWidgets  # noqa: E402

import test_step1_fusion_isolation as iso  # noqa: E402
import test_step1_result_publication as pub  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _pump(n=30):
    for _ in range(n):
        QtWidgets.QApplication.processEvents()


# ── G1: the worker ──────────────────────────────────────────────────────

def _held_after_first_tile(worker):
    """Hold `worker` inside its first tile until `.go` is set."""
    reached, go = threading.Event(), threading.Event()
    real_fuse = worker._fuse_tile

    def _fuse(*a, **k):
        out = real_fuse(*a, **k)
        reached.set()
        assert go.wait(20)
        return out
    worker._fuse_tile = _fuse
    return reached, go


def test_a_stopped_runs_cleanup_does_not_delete_the_next_runs_store(tmp_path):
    """Run A is cancelled; run B starts on the same output while A's thread is
    still inside a tile. A then ends and clears its half-built store. With one
    fixed `.inprogress` name that store was B's, and B published a zarr with
    no array metadata."""
    a = pub._worker(tmp_path, n_rows=2, hash_="A")
    b = pub._worker(tmp_path, n_rows=2, hash_="B")
    a_reached, a_go = _held_after_first_tile(a)
    b_reached, b_go = _held_after_first_tile(b)
    b_errors = []
    b.error.connect(b_errors.append)

    ta = threading.Thread(target=a.run)
    ta.start()
    assert a_reached.wait(20)
    a.stop()                                     # Cancel on A
    tb = threading.Thread(target=b.run)
    tb.start()
    assert b_reached.wait(20)                    # B is writing its store
    a_go.set()
    ta.join(20)                                  # A's `finally` has run
    b_go.set()
    tb.join(20)

    assert not b_errors, b_errors
    z = zarr.open(str(tmp_path / "fused.zarr"), mode="r")
    assert z.attrs["config_hash"] == "B"
    assert z.attrs["complete"] is True
    assert z.shape == (16, 16, 2)
    assert not glob.glob(str(tmp_path / "fused.zarr.inprogress*"))


def test_a_stop_after_the_last_tile_does_not_publish(tmp_path):
    """Cancel pressed while the last tile is being fused: the run reports the
    stop and the previous result stays on the real path."""
    pub._worker(tmp_path, hash_="first").run()
    path = str(tmp_path / "fused.zarr")

    late = pub._worker(tmp_path, n_rows=1, hash_="late")
    real_fuse = late._fuse_tile

    def _fuse_then_cancel(*a, **k):
        out = real_fuse(*a, **k)
        late.stop()                              # the user's late Cancel
        return out
    late._fuse_tile = _fuse_then_cancel
    errors, finished = [], []
    late.error.connect(errors.append)
    late.finished.connect(finished.append)
    late.run()

    assert errors and not finished
    assert zarr.open(path, mode="r").attrs["config_hash"] == "first"
    assert not glob.glob(path + ".inprogress*")


def test_the_meta_files_are_swapped_in_whole(tmp_path, monkeypatch):
    """`fusion_meta.json` / `roi_config.json` go through write-aside-and-
    replace: a failure while writing leaves the previous file whole."""
    import json
    pub._worker(tmp_path, hash_="first").run()
    meta = tmp_path / "fusion_meta.json"
    before = meta.read_text()

    real_dump = json.dump

    def _dump_half_then_fail(obj, f, *a, **k):
        if isinstance(obj, dict) and obj.get("mode") == "full_wsi":
            f.write('{"mode": ')
            raise OSError("disk full")
        return real_dump(obj, f, *a, **k)
    monkeypatch.setattr(json, "dump", _dump_half_then_fail)
    errors = []
    second = pub._worker(tmp_path, hash_="second")
    second.error.connect(errors.append)
    second.run()

    assert errors
    assert meta.read_text() == before
    assert not glob.glob(str(tmp_path / ".fusion_meta.json.tmp.*"))


# ── G1: the window ──────────────────────────────────────────────────────

class _SlowStop(iso._FakeFusion):
    """Answers a stop the way FullFusionWorker does — `error` from inside
    `run()` — and then takes a while to end (its `finally` clearing stores)."""

    def __init__(self):
        super().__init__()
        self.exit_gate = threading.Event()

    def run(self):
        self._release.wait(10)
        self.error.emit("Fusion stopped by user.")
        self.exit_gate.wait(10)


def test_cancel_unlocks_only_after_the_thread_has_ended(app, tmp_path, monkeypatch):
    shown = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical",
                        lambda *a, **k: shown.append(a[2] if len(a) > 2 else ""))
    w = iso._window(app, tmp_path)
    worker = _SlowStop()
    try:
        w.show()
        w._stack.setCurrentIndex(1)
        w._set_step_active(1)
        w._start_fusion_worker(worker, job_name="fusion", n_rows=1, n_cols=1)
        _pump()
        assert not w.config.isEnabled()

        w._fusion_dialog.findChild(QtWidgets.QPushButton).click()   # Cancel
        _pump()
        assert worker.stop_called
        # `error` has arrived, the thread has not ended: still locked, and the
        # dialog says why.
        assert worker.isRunning()
        assert not w.config.isEnabled()
        assert not w._btn_back_to_step0.isEnabled()
        assert w._fusion_dialog is not None and w._fusion_dialog.isVisible()
        assert "Stopping" in w._fusion_dialog.labelText()
        assert not shown

        worker.exit_gate.set()
        worker.wait(5000)
        _pump()
        assert w.config.isEnabled()
        assert w._fusion_dialog is None
        assert shown == ["Fusion stopped by user."]
    finally:
        worker.release()
        worker.exit_gate.set()
        worker.wait(5000)
        w.close()


def test_a_new_job_is_refused_while_the_old_thread_is_alive(app, tmp_path, monkeypatch):
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *a, **k: None)
    w = iso._window(app, tmp_path)
    first, second = _SlowStop(), _SlowStop()
    try:
        assert w._start_fusion_worker(first, job_name="fusion",
                                      n_rows=1, n_cols=1) is not None
        first.release()                          # `error` sent, thread alive
        _pump()
        assert first.isRunning()
        assert w._start_fusion_worker(second, job_name="fusion",
                                      n_rows=1, n_cols=1) is None
        assert not second.isRunning()
        assert w._fusion_worker is first
    finally:
        for wk in (first, second):
            wk.release()
            wk.exit_gate.set()
            wk.wait(5000)
        _pump()
        w.close()


# ── G2: Step2's run answers ─────────────────────────────────────────────

class _FakeStep2Run(QtCore.QObject):
    """The SegmentMergeWorker signal contract; `start` runs nothing, the test
    emits the answers itself, as late as it likes."""
    progress = QtCore.pyqtSignal(int, int, str)
    tile_done = QtCore.pyqtSignal(int, int, int)
    tile_skipped = QtCore.pyqtSignal(int, int)
    finished = QtCore.pyqtSignal(str, int)
    error = QtCore.pyqtSignal(str)

    def __init__(self, **kw):
        super().__init__()
        self.kw = kw

    def start(self):
        pass

    def isRunning(self):
        return False

    def stop(self):
        pass


@pytest.fixture
def step2(app, tmp_path, monkeypatch):
    from block01.ui import step2_page as s2
    zp = str(tmp_path / "fused_Full WSI.zarr")
    z = zarr.open(zp, mode="w", shape=(64, 64, 2), chunks=(64, 64, 2), dtype=np.uint16)
    z[...] = 7
    seen = {"marked": [], "boxes": [], "done": []}
    monkeypatch.setattr(s2, "SegmentMergeWorker", _FakeStep2Run)
    monkeypatch.setattr(s2, "mark_roi_step",
                        lambda project, roi, step, state: seen["marked"].append(roi))
    monkeypatch.setattr(s2.QMessageBox, "exec_",
                        lambda self: seen["boxes"].append(self.text()) or 0)
    monkeypatch.setattr(s2.QMessageBox, "critical",
                        lambda *a, **k: seen["boxes"].append(a[2]))
    p = s2.Step2Page()
    monkeypatch.setattr(p, "_promote_step0_remap", lambda cfg: None)
    p._zarr_edit.setText(zp)
    p._load_zarr_info()
    p._set_param_source("manual")
    p.segmentation_done.connect(seen["done"].append)
    a_dir = tmp_path / "proj" / "rois" / "roi_A"
    p.set_roi_context(roi_id="roi_A", roi_dir=str(a_dir),
                      step2_dir=str(a_dir / "step2"))
    yield p, seen, tmp_path
    p.deleteLater()


def _switch_workspace(p, tmp_path):
    b_dir = tmp_path / "proj" / "rois" / "roi_B"
    p.set_roi_context(roi_id="roi_B", roi_dir=str(b_dir),
                      step2_dir=str(b_dir / "step2"))


def test_a_step2_run_finishing_after_the_workspace_changed_shows_nothing(step2):
    p, seen, tmp_path = step2
    p._run()
    worker = p._worker
    assert isinstance(worker, _FakeStep2Run) and not p._btn_run.isEnabled()
    label_before = p._prog_lbl.text()

    _switch_workspace(p, tmp_path)               # e.g. Save as in Step0
    worker.finished.emit("/old/run", 1234)       # the old run, late
    _pump()

    assert seen["done"] == []                    # no hand-off to Step3
    assert seen["boxes"] == []                   # no "complete" box
    assert p._prog_lbl.text() == label_before    # no result text
    assert seen["marked"] == ["roi_A"]           # its OWN workspace, not B
    assert p._btn_run.isEnabled() and not p._run_active


def test_a_step2_run_failing_after_the_workspace_changed_shows_nothing(step2):
    p, seen, tmp_path = step2
    p._run()
    worker = p._worker
    _switch_workspace(p, tmp_path)
    worker.error.emit("engine died")
    _pump()
    assert seen["boxes"] == []
    assert p._btn_run.isEnabled()


def test_a_step2_run_in_the_same_workspace_reports_as_before(step2):
    p, seen, tmp_path = step2
    p._run()
    p._worker.finished.emit("/run", 5)
    _pump()
    assert seen["done"] == ["/run"]
    assert len(seen["boxes"]) == 1
    assert seen["marked"] == ["roi_A"]


# ── G3: random patches ──────────────────────────────────────────────────

class _Gen:
    patches = [(0, 16, 0, 16)]
    shortfall = 0
    requested = 1

    def record(self):
        return {"requested": 1}


def test_random_patches_for_the_previous_slide_are_dropped(app, tmp_path, monkeypatch):
    from block01.ui.step1_presegmentation import random_job as rj
    gate = threading.Event()

    def _slow_generation(request):
        assert gate.wait(20)
        return {"generation": _Gen(), "error": ""}
    monkeypatch.setattr(rj, "run_generation", _slow_generation)
    monkeypatch.setattr(QtWidgets.QMessageBox, "information", lambda *a, **k: None)
    w = iso._window(app, tmp_path)
    w.loader.read_region_lowres = lambda *a, **k: np.zeros((4, 4), np.float32)
    added = []
    monkeypatch.setattr(w._step0, "add_patches", lambda p: added.append(p) or list(p))
    try:
        w._on_random_patches_requested(1, 16, 16)
        assert w._random_patch_job.is_running()
        # The user loads another slide while the job reads the old one.
        w._dataset_gen_seen += 1
        w.loader = iso._Loader()
        gate.set()
        w._random_patch_job.wait(10)
        _pump()
        assert added == []
    finally:
        gate.set()
        w._random_patch_job.wait(10)
        w.close()


def test_random_patches_for_this_slide_still_arrive(app, tmp_path, monkeypatch):
    from block01.ui.step1_presegmentation import random_job as rj
    monkeypatch.setattr(rj, "run_generation",
                        lambda request: {"generation": _Gen(), "error": ""})
    w = iso._window(app, tmp_path)
    w.loader.read_region_lowres = lambda *a, **k: np.zeros((4, 4), np.float32)
    added = []
    monkeypatch.setattr(w._step0, "add_patches", lambda p: added.append(p) or list(p))
    try:
        w._on_random_patches_requested(1, 16, 16)
        w._random_patch_job.wait(10)
        _pump()
        assert added == [_Gen.patches]
    finally:
        w.close()


# ── G5: the end of a pre-segmentation run ───────────────────────────────

class _FakePresegJob:
    made = []

    def __init__(self, step1_dir, run, loader_factory, python=None,
                 on_record=None, on_progress=None, on_finished=None):
        self.run, self.on_finished = run, on_finished
        self.running = True
        _FakePresegJob.made.append(self)

    def start(self):
        pass

    def stop(self):
        pass

    def is_running(self):
        return self.running

    def close(self, timeout=30.0):
        self.running = False       # its thread outlived the close timeout


def test_a_late_end_of_an_old_preseg_run_keeps_the_new_run(app, tmp_path, monkeypatch):
    import test_step1_preseg_run_ui as pre
    from block01.ui import main_window as mw
    _FakePresegJob.made = []
    monkeypatch.setattr(mw, "PresegRunJob", _FakePresegJob)
    w = pre._window(app, tmp_path, monkeypatch)
    try:
        pre._quiet(monkeypatch)
        w._preseg_methods.adopt(pre.SD, pre._vals(pre.SD))
        w._on_preseg_run()
        old = _FakePresegJob.made[-1]
        old.close()                              # e.g. the Step1 context went away
        w._on_preseg_run()
        new = _FakePresegJob.made[-1]
        assert new is not old and w._preseg_job is new

        old.on_finished({})                      # the old thread finally ends
        _pump()
        assert w._preseg_job is new              # the new run is still the run
        assert not w._preseg_methods.btn_run.isEnabled()
    finally:
        w.close()


# ── G4: Step4 and its batch dialog ──────────────────────────────────────

class _FakeExtract(QtCore.QThread):
    """FeatureExtractWorker's contract; runs until stopped or released, then
    answers the way the real one does."""
    progress = QtCore.pyqtSignal(int, int, str)
    extraction_done = QtCore.pyqtSignal(str, str)
    error = QtCore.pyqtSignal(str)
    made = []

    def __init__(self, **kw):
        super().__init__()
        self.kw = kw
        self.outputs = {}
        self.go = threading.Event()
        self.leave = threading.Event()   # held after answering until set
        self.leave.set()
        self.stopped = False
        _FakeExtract.made.append(self)

    def stop(self):
        self.stopped = True
        self.go.set()

    def run(self):
        self.go.wait(20)
        if self.stopped:
            self.error.emit("Stopped by user.")
        else:
            self.extraction_done.emit(self.kw.get("output_dir", ""), "cell_features")
        self.leave.wait(20)


@pytest.fixture
def step4(app, tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from test_quant_sources import build_project
    from block01.ui import step4_page as s4
    _FakeExtract.made = []
    monkeypatch.setattr(s4, "FeatureExtractWorker", _FakeExtract)
    p = build_project(tmp_path)
    page = s4.Step4Page()
    page._announced, page._errors = [], []
    page._announce = lambda title, text: page._announced.append(text)
    page._report_error = lambda msg: page._errors.append(msg)
    page.set_run(p["run_dir"], open_slide=p["slide"])
    assert page._btn_run.isEnabled()
    yield page
    for wk in _FakeExtract.made:
        wk.go.set()
        wk.wait(5000)
    page.deleteLater()


def _controls(page):
    return {w: w.isEnabled() for w in page.findChildren(QtWidgets.QWidget)
            if isinstance(w, (QtWidgets.QAbstractButton, QtWidgets.QComboBox,
                              QtWidgets.QLineEdit, QtWidgets.QAbstractSpinBox))}


def test_step4_is_frozen_while_it_computes_except_stop(step4):
    page = step4
    before = _controls(page)
    page._run()
    worker = page._worker
    live = [w for w, on in _controls(page).items() if on]
    assert live == [page._btn_stop]
    assert not page._marker_list.isEnabled()     # disabled with its group

    worker.go.set()
    worker.wait(5000)
    _pump()
    after = _controls(page)
    assert after == before                       # every control as it was
    assert len(page._announced) == 1


def test_step4_does_not_swap_its_job_while_running(step4, tmp_path):
    page = step4
    page._run()
    run_dir = page._job.run_dir
    page.set_run(str(tmp_path / "another_run"))  # Step3 hands over another
    assert page._job is not None and page._job.run_dir == run_dir


def test_a_step4_answer_after_a_dataset_switch_shows_nothing(step4):
    page = step4
    page._run()
    worker = page._worker
    page._dataset_gen_seen = 1                   # the page is its own window here
    assert page.stop_background_jobs() is True   # what the switch asks
    assert worker.stopped
    worker.wait(5000)
    _pump()
    assert page._announced == [] and page._errors == []
    assert page._btn_run.isEnabled() and not page._btn_stop.isEnabled()


def test_closing_the_batch_dialog_stops_waits_then_closes(app, tmp_path, monkeypatch):
    from block01.ui import batch_step4_dialog as bd
    _FakeExtract.made = []
    monkeypatch.setattr(bd, "FeatureExtractWorker", _FakeExtract)
    boxes = []
    monkeypatch.setattr(bd.QMessageBox, "information",
                        lambda *a, **k: boxes.append(a[2]))
    ome = tmp_path / "s.ome.tiff"
    ome.write_bytes(b"x")
    mask = tmp_path / "run" / "global_mask.dat"
    mask.parent.mkdir()
    mask.write_bytes(b"x")
    dlg = bd.BatchStep4Dialog()
    dlg._add_row(True, "s", str(ome), str(mask), str(tmp_path / "out"), "s")
    dlg._add_row(True, "t", str(ome), str(mask), str(tmp_path / "out2"), "t")
    dlg.show()
    try:
        dlg._run_batch()
        worker = _FakeExtract.made[-1]
        worker.leave.clear()                     # it answers, then takes a while
        assert worker.isRunning()

        dlg._close_btn.click()                   # Close, mid-run
        _pump()
        assert worker.stopped
        assert dlg.isVisible()                   # waits for the thread
        worker.leave.set()
        worker.wait(5000)
        _pump()
        assert not dlg.isVisible()
        assert len(_FakeExtract.made) == 1       # the next sample never started
        assert boxes == []                       # no "Batch complete"
    finally:
        for wk in _FakeExtract.made:
            wk.go.set()
            wk.leave.set()
            wk.wait(5000)
        dlg.deleteLater()


# ── G2: Step2's results index is swapped in whole ───────────────────────

def test_step2s_results_index_survives_a_failed_write(tmp_path, monkeypatch):
    """The index is read, changed and written back; a write that fails half
    way leaves the previous index whole (`write_json_atomic`)."""
    import json
    from block01.workers.segment_merge_worker import SegmentMergeWorker
    wk = SegmentMergeWorker("unused.zarr", seg_config={"method": "cellpose_wholecell_fusion"},
                            output_dir=str(tmp_path / "run"))
    wk.project_output_dir = str(tmp_path / "step2")
    entry = {"result_id": "r1", "method": "m", "status": "done"}
    path = wk._update_results_index(entry)
    with open(path) as f:
        before = f.read()

    real_dump = json.dump

    def _half_then_fail(obj, f, *a, **k):
        if isinstance(obj, dict) and "runs" in obj:
            f.write('{"runs": [')
            raise OSError("disk full")
        return real_dump(obj, f, *a, **k)
    monkeypatch.setattr(json, "dump", _half_then_fail)
    with pytest.raises(OSError):
        wk._update_results_index(dict(entry, result_id="r2"))
    with open(path) as f:
        assert f.read() == before
    assert not glob.glob(os.path.join(os.path.dirname(path), ".*.tmp.*"))


# ── §6: the two commit protocols the compliant rows rely on ─────────────

def test_protocol_a_single_file_is_replaced_whole(tmp_path, monkeypatch):
    """`write_json_atomic`: temp file + fsync + `os.replace`. A failing
    replace leaves the old file and no temp file."""
    from block01.core.provenance import write_json_atomic
    path = str(tmp_path / "x.json")
    write_json_atomic(path, {"v": 1})
    real_replace = os.replace

    def _fail(src, dst, *a, **k):
        if dst == path:
            raise OSError("killed")
        return real_replace(src, dst, *a, **k)
    monkeypatch.setattr(os, "replace", _fail)
    with pytest.raises(OSError):
        write_json_atomic(path, {"v": 2})
    import json
    with open(path) as f:
        assert json.load(f) == {"v": 1}
    assert os.listdir(tmp_path) == ["x.json"]


def test_protocol_b_the_completion_mark_is_written_last(tmp_path, monkeypatch):
    """Step3's label pyramid: built under `.partial`, `complete` last, then
    renamed. A failure before the mark leaves the previous pyramid readable
    and no partial directory."""
    import test_label_pyramid as tlp
    from block01.core import label_pyramid as lp
    slide = tlp._slide(tmp_path)
    bbox = (37, 37 + 176, 101, 101 + 208)
    src, _ = tlp._mask(tmp_path, (176, 208))
    out = lp.build(src, str(tmp_path / "pyr.zarr"), lp.raw_level_shapes(slide), bbox, "cell")
    first = lp.read(out)
    assert first is not None

    real_attrs = lp._attrs

    def _fail_before_the_mark(*a, **k):
        raise RuntimeError("died before complete")
    monkeypatch.setattr(lp, "_attrs", _fail_before_the_mark)
    with pytest.raises(RuntimeError):
        lp.build(src, out, lp.raw_level_shapes(slide), bbox, "cell")
    monkeypatch.setattr(lp, "_attrs", real_attrs)
    assert lp.read(out) == first
    assert not os.path.exists(out + ".partial")


# ── REV blocker 1: a same-dataset handoff invalidation does not stop Step4 ─

def _window_with_step4_running(app, tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from test_quant_sources import build_project
    from block01.ui import step4_page as s4
    _FakeExtract.made = []
    monkeypatch.setattr(s4, "FeatureExtractWorker", _FakeExtract)
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *a, **k: None)
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *a, **k: None)
    w = iso._window(app, tmp_path)
    page = w._step4
    page._errors, page._announced = [], []
    page._report_error = lambda msg: page._errors.append(msg)
    page._announce = lambda title, text: page._announced.append(text)
    (tmp_path / "q").mkdir()
    p = build_project(tmp_path / "q")
    page.set_run(p["run_dir"], open_slide=p["slide"])
    page._run()
    return w, page, _FakeExtract.made[-1]


def test_a_same_dataset_invalidation_leaves_step4_running(app, tmp_path, monkeypatch):
    """Step4 extracting, the user edits a saved ROI in Step0: the handoff is
    invalidated for the SAME dataset. Step4 is not stopped and no
    "Stopped by user." is reported."""
    w, page, worker = _window_with_step4_running(app, tmp_path, monkeypatch)
    try:
        w._step0.handoff_invalidated.emit({
            "step0_manifest_path": w.step0_output["step0_manifest_path"],
            "reason": "roi_changed"})
        _pump()
        assert worker.stopped is False
        assert page._running is True and page._errors == []
    finally:
        for wk in _FakeExtract.made:
            wk.go.set()
            wk.wait(5000)
        _pump()
        w.close()


def test_a_dataset_switch_stops_step4_and_drops_its_answer(app, tmp_path, monkeypatch):
    w, page, worker = _window_with_step4_running(app, tmp_path, monkeypatch)
    try:
        w._step0.dataset_committed.emit({"gen": int(w._dataset_gen_seen) + 1})
        worker.wait(5000)
        _pump()
        assert worker.stopped is True
        assert page._errors == [] and page._announced == []
        assert not page._running
    finally:
        for wk in _FakeExtract.made:
            wk.go.set()
            wk.wait(5000)
        _pump()
        w.close()


# ── post-REV corrections (user rulings 2026-10-02) ──────────────────────

class _StopsAtOnce(_FakeExtract):
    """Ends INSIDE `stop()`: the thread is gone before the caller's next line."""

    def stop(self):
        super().stop()
        self.wait(5000)


def test_r1_a_sample_that_ends_inside_stop_still_closes_the_dialog(app, tmp_path, monkeypatch):
    from block01.ui import batch_step4_dialog as bd
    _FakeExtract.made = []
    monkeypatch.setattr(bd, "FeatureExtractWorker", _StopsAtOnce)
    monkeypatch.setattr(bd.QMessageBox, "information", lambda *a, **k: None)
    ome = tmp_path / "s.ome.tiff"
    ome.write_bytes(b"x")
    mask = tmp_path / "run" / "global_mask.dat"
    mask.parent.mkdir()
    mask.write_bytes(b"x")
    dlg = bd.BatchStep4Dialog()
    dlg._add_row(True, "s", str(ome), str(mask), str(tmp_path / "out"), "s")
    dlg.show()
    try:
        dlg._run_batch()
        assert _FakeExtract.made[-1].isRunning()
        dlg._close_btn.click()
        _pump()
        assert not dlg.isVisible()
    finally:
        for wk in _FakeExtract.made:
            wk.go.set()
            wk.wait(5000)
        dlg.deleteLater()


def test_r2_fusion_writes_no_preview_png(tmp_path):
    pub._worker(tmp_path).run()
    assert os.path.isdir(tmp_path / "fused.zarr")
    assert not glob.glob(str(tmp_path / "*_preview.png"))


@pytest.mark.parametrize("sweep", [True, False])
def test_r7_a_killed_runs_leftover_is_swept_only_when_allowed(tmp_path, sweep):
    dead = tmp_path / "fused.zarr.inprogress.deadbeef"
    dead.mkdir()
    (dead / "chunk").write_bytes(b"x")
    wk = pub._worker(tmp_path)
    wk.sweep_leftovers = sweep
    wk.run()
    assert dead.exists() is (not sweep)


def test_r7_the_window_allows_the_sweep_only_with_no_live_retired_thread(app, tmp_path,
                                                                        monkeypatch):
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *a, **k: None)
    w = iso._window(app, tmp_path)
    retired, fresh = _SlowStop(), _SlowStop()
    retired.sweep_leftovers = fresh.sweep_leftovers = None
    try:
        retired.start()
        w._retired_fusion_workers.append(retired)
        w._start_fusion_worker(fresh, job_name="fusion", n_rows=1, n_cols=1)
        assert fresh.sweep_leftovers is False         # a retired one may still write
    finally:
        for wk in (retired, fresh):
            wk.release()
            wk.exit_gate.set()
            wk.wait(5000)
        _pump()
        w.close()


def test_r6_the_segmentation_registry_survives_a_failed_write(tmp_path, monkeypatch):
    import json
    from block01.utils import segmentation_registry as reg
    reg.save_registry(str(tmp_path), {"version": 1, "results": [{"id": "a"}]})
    path = reg.registry_path(str(tmp_path))
    with open(path) as f:
        before = f.read()
    real_dump = json.dump

    def _half(obj, f, *a, **k):
        if isinstance(obj, dict) and "results" in obj:
            f.write('{"results": [')
            raise OSError("disk full")
        return real_dump(obj, f, *a, **k)
    monkeypatch.setattr(json, "dump", _half)
    with pytest.raises(OSError):
        reg.save_registry(str(tmp_path), {"version": 1, "results": [{"id": "b"}]})
    with open(path) as f:
        assert f.read() == before


def test_r5_an_old_runs_progress_is_not_drawn_and_the_page_says_where_it_writes(step2):
    p, seen, tmp_path = step2
    p._run()
    worker = p._worker
    worker.output_dir = str(tmp_path / "proj" / "rois" / "roi_A" / "step2" / "run_1")
    label_before = p._prog_lbl.text()
    cells_before = p._cells_lbl.text()
    _switch_workspace(p, tmp_path)
    worker.progress.emit(3, 12, "tile 4 of 12")
    worker.tile_done.emit(2, 12, 999)
    _pump()
    assert p._prog_lbl.text() == label_before
    assert p._cells_lbl.text() == cells_before
    assert not p._old_run_lbl.isHidden()
    text = p._old_run_lbl.text()
    assert "roi_A" in text and os.path.abspath(worker.output_dir) in text
    assert "still computing" in text
    worker.finished.emit("/old/run", 5)
    _pump()
    assert "has finished" in p._old_run_lbl.text()


def test_r5_the_own_runs_progress_is_drawn(step2):
    p, seen, tmp_path = step2
    p._run()
    p._worker.progress.emit(3, 12, "tile 4 of 12")
    _pump()
    assert p._prog_lbl.text() == "tile 4 of 12"
    assert p._old_run_lbl.isHidden()


def test_r3_a_run_handed_over_mid_extraction_is_opened_afterwards(step4, tmp_path):
    from test_quant_sources import build_project
    page = step4
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = build_project(other_dir)
    page._run()
    first = page._job.run_dir
    page.set_run(other["run_dir"], open_slide=other["slide"])
    assert page._job.run_dir == first                  # not swapped mid-run
    assert not page._pending_lbl.isHidden()
    assert other["run_dir"] in page._pending_lbl.text()
    page._worker.go.set()
    page._worker.wait(5000)
    _pump()
    assert os.path.realpath(page._job.run_dir) == os.path.realpath(other["run_dir"])
    assert page._pending_lbl.isHidden()


def test_b9_runs_without_provenance_are_tagged_right_aligned(app):
    from PyQt5.QtCore import Qt
    from block01.ui.step3_mask_bar import PROVENANCE_UNKNOWN, TAG_ROLE, Step3MaskBar
    bar = Step3MaskBar()
    bar.set_runs([("cellpose · 2026-10-02 10:00", "/r/new\x1fFull WSI", ""),
                  ("cellpose · 2026-09-27 12:00", "/r/old\x1fFull WSI", PROVENANCE_UNKNOWN)])
    combo = bar.run_combo
    assert combo.itemData(0, TAG_ROLE) in (None, "")
    assert combo.itemData(1, TAG_ROLE) == PROVENANCE_UNKNOWN
    assert combo.itemText(1) == "cellpose · 2026-09-27 12:00"     # the name is unchanged
    assert PROVENANCE_UNKNOWN in combo.itemData(1, Qt.ToolTipRole)
    bar.set_runs([("x", "/r/x")])                                  # two-field items still work
    assert combo.itemText(0) == "x"


def test_b9_a_run_is_known_only_with_its_segmentation_run_entry(tmp_path):
    import json
    from types import SimpleNamespace
    from block01.core import provenance as prov
    from block01.ui.main_window import MainWindow
    proj = tmp_path / "proj"
    ws = proj / "rois" / "ws1"
    (ws / "step2" / "segmentation_runs" / "known").mkdir(parents=True)
    (ws / "step2" / "segmentation_runs" / "legacy").mkdir(parents=True)
    (ws / "roi_manifest.json").write_text("{}")
    (proj / "project_manifest.json").write_text(json.dumps({"version": 1,
                                                            "project_schema_version": 1}))
    known = str(ws / "step2" / "segmentation_runs" / "known")
    legacy = str(ws / "step2" / "segmentation_runs" / "legacy")
    prov.register(str(proj), "segmentation_run", prov.location(str(proj), known), "known",
                  workspace_id="ws1")
    runs = [SimpleNamespace(run_dir=known), SimpleNamespace(run_dir=legacy),
            SimpleNamespace(run_dir=str(tmp_path / "outside"))]
    assert MainWindow._step3_runs_with_provenance(runs) == {known}
