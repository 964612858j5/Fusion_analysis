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
