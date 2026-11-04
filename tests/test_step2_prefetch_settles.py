"""Step2's prefetch reads settle before the run answers
(docs/v17_step2_prefetch_cleanup_plan.md §1.2), in the ROI loop and in the
full-slide loop, the worker on its own QThread (`start()`):

  * a tile whose payload read raises (prefetched AND the synchronous retry)
    while the next tile's prefetched read is under way: when `error` is
    emitted, that read has finished and no prefetch thread is alive;
  * a Stop while a prefetched read is under way: the same at `error`;
  * a read past the (lowered) cleanup budget: the run still answers, about
    the budget later, and the warning is logged;

The held read is released only when the cleanup is reached, then takes a
little longer, so an answer that did not wait comes before it ends. The
answer is observed where it is emitted (on the worker thread).
  * a normal run: `finished`, no prefetch thread alive.

The synthetic project of `test_step2_legacy_stop.py` (real StarDist,
skipped when not installed); temporary directories only.
"""

import threading
import time

import pytest

from test_step2_legacy_stop import COLS, _worker, app, roi_dir  # noqa: F401 -- fixtures

from block01.qt import QtCore  # noqa: E402

MODES = {"roi": [{"name": "A"}], "full": None}


def _prefetch_alive():
    return [t.name for t in threading.enumerate() if t.name.startswith("step2-tile-prefetch")]


def _in_prefetch():
    return threading.current_thread().name.startswith("step2-tile-prefetch")


def _index(tile):
    return int(tile.row) * COLS + int(tile.col)


class _Reads:
    """Wraps the worker's payload read: tile `fail` raises; tile `slow`'s
    prefetched read is held until the cleanup is reached (`release`, set by
    `TilePrefetcher.close`), then takes `slow_s` more -- so an answer that
    does not wait for it comes before `slow_done`."""

    def __init__(self, real, fail=None, slow=None, slow_s=0.4):
        self.real, self.fail, self.slow, self.slow_s = real, fail, slow, slow_s
        self.slow_started, self.slow_done = threading.Event(), threading.Event()
        self.release = threading.Event()
        self.close_at = None

    def __call__(self, worker, zarr_path, z, tile, *a, **k):
        i = _index(tile)
        if i == self.slow and _in_prefetch():
            self.slow_started.set()
            self.release.wait(timeout=60)
            time.sleep(self.slow_s)
            try:
                return self.real(worker, zarr_path, z, tile, *a, **k)
            finally:
                self.slow_done.set()               # after the real read
        if i == self.fail:
            if not _in_prefetch():
                # the synchronous retry fails only once the next read is
                # under way, so the cleanup has a read to wait for
                self.slow_started.wait(timeout=10)
            raise OSError(f"tile {i}: injected read failure (test)")
        return self.real(worker, zarr_path, z, tile, *a, **k)


def _run(worker, timeout=300):
    seen = {"finished": [], "error": [], "alive_at_answer": None, "slow_done": None,
            "answer_at": None}
    reads = worker.__dict__.get("_test_reads")

    def answer(kind, value):
        seen[kind].append(value)
        seen["alive_at_answer"] = _prefetch_alive()
        seen["slow_done"] = reads.slow_done.is_set() if reads else None
        seen["answer_at"] = time.perf_counter()
    d = QtCore.Qt.DirectConnection
    worker.finished.connect(lambda o, n: answer("finished", n), d)
    worker.error.connect(lambda m: answer("error", m), d)
    t0 = time.perf_counter()
    worker.start()
    assert worker.wait(timeout * 1000), "the worker did not end"
    seen["seconds"] = time.perf_counter() - t0
    return seen


def _install(monkeypatch, worker, reads):
    from block01.utils.tile_prefetch import TilePrefetcher
    from block01.workers import segment_merge_worker as smw
    real = smw.SegmentMergeWorker._prepare_tile_payload
    reads.real = real
    monkeypatch.setattr(smw.SegmentMergeWorker, "_prepare_tile_payload",
                        lambda self, *a, **k: reads(self, *a, **k))
    real_close = TilePrefetcher.close

    def close(self, *a, **k):                      # the cleanup is reached
        if reads.close_at is None:
            reads.close_at = time.perf_counter()
        reads.release.set()
        return real_close(self, *a, **k)
    monkeypatch.setattr(TilePrefetcher, "close", close)
    worker._test_reads = reads


def _settle(reads):
    """Teardown, always (in `finally`): the held read is let go and every
    prefetch thread ends before the next test."""
    reads.release.set()
    if reads.slow_started.is_set():
        reads.slow_done.wait(timeout=30)
    for t in [t for t in threading.enumerate() if t.name.startswith("step2-tile-prefetch")]:
        t.join(timeout=10)


@pytest.mark.parametrize("mode", sorted(MODES))
def test_a_failed_read_answers_after_the_prefetch_settles(app, roi_dir, monkeypatch, mode):
    worker = _worker(roi_dir, MODES[mode])
    reads = _Reads(None, fail=1, slow=2)
    _install(monkeypatch, worker, reads)
    try:
        seen = _run(worker)
    finally:
        _settle(reads)
    assert seen["finished"] == [] and len(seen["error"]) == 1
    assert "injected read failure" in seen["error"][0]
    assert reads.slow_started.is_set() and reads.close_at is not None
    assert seen["slow_done"] is True and seen["alive_at_answer"] == []


@pytest.mark.parametrize("mode", sorted(MODES))
def test_a_stop_answers_after_the_prefetch_settles(app, roi_dir, monkeypatch, mode):
    worker = _worker(roi_dir, MODES[mode])
    reads = _Reads(None, slow=2)
    _install(monkeypatch, worker, reads)
    # Stop once tile 2's prefetched read is under way (tile 0 is being segmented)
    threading.Thread(target=lambda: reads.slow_started.wait(timeout=60) and worker.stop(),
                     daemon=True).start()
    try:
        seen = _run(worker)
    finally:
        _settle(reads)
    assert seen["finished"] == [] and seen["error"] == ["Stopped by user."]
    assert seen["slow_done"] is True and seen["alive_at_answer"] == []


@pytest.mark.parametrize("mode", sorted(MODES))
def test_a_read_past_the_budget_still_lets_the_run_answer(app, roi_dir, monkeypatch, mode):
    from block01.utils import tile_prefetch
    monkeypatch.setattr(tile_prefetch, "CLOSE_WAIT_S", 0.3)
    worker = _worker(roi_dir, MODES[mode])
    reads = _Reads(None, fail=1, slow=2, slow_s=4.0)
    _install(monkeypatch, worker, reads)
    warnings = []
    from block01.utils.tile_prefetch import TilePrefetcher
    real_warn = TilePrefetcher._warn
    monkeypatch.setattr(TilePrefetcher, "_warn", lambda self, m: (warnings.append(m), real_warn(self, m)))
    try:
        seen = _run(worker)
    finally:
        _settle(reads)
    assert len(seen["error"]) == 1 and "injected read failure" in seen["error"][0]
    assert seen["slow_done"] is False                  # answered without waiting for it
    assert seen["answer_at"] - reads.close_at < 3.0    # about the budget, not the 4 s read
    assert len(warnings) == 1 and "still running after 0.3 s" in warnings[0]
    assert reads.slow_done.is_set() and _prefetch_alive() == []   # it finished later, harmlessly


@pytest.mark.parametrize("mode", sorted(MODES))
def test_a_normal_run_finishes_with_no_prefetch_thread(app, roi_dir, mode):
    worker = _worker(roi_dir, MODES[mode])
    seen = _run(worker)
    assert seen["error"] == [] and len(seen["finished"]) == 1
    assert seen["alive_at_answer"] == []
