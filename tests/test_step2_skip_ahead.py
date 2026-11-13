"""Step2 starts on the first tile to segment (backlog 20,
docs/v17_acceptance_notes_plan.md B20): every skipped tile -- the skip
plan's, and a tile without a selected TMA core -- is told to the page before
the first tile, nothing is read for it before the segmented tiles, the
prefetcher reads ahead only the tiles that run, and its DAPI is written
after them (the outputs are byte-identical to before: checked against the
previous code with a scratch script, 2026-11-13). Also: the run's start no
longer imports torch for a memory line, and its steps are timed.

Synthetic inputs and the fake model of `test_step2_skip_empty_tiles`.
"""

import importlib.abc
import logging
import os
import sys

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("block01.qt")
zarr = pytest.importorskip("zarr")

from test_step2_ownership_move import _labels  # noqa: E402
from test_step2_skip_empty_tiles import _plan, _run, _worker, app  # noqa: E402,F401

from block01.utils.tile_prefetch import TilePrefetcher  # noqa: E402
from block01.utils.tile_scheduler import TileScheduler  # noqa: E402

ROWS, COLS, OVERLAP = 3, 4, 24
LOOPS = ["run", "segment_one_zarr"]


def test_the_prefetcher_reads_ahead_only_the_tiles_given():
    read = []

    def load(i, t):
        read.append(i)
        return i
    p = TilePrefetcher(list(range(6)), load, prefetch_queue_size=2, order=[3, 5])
    assert p.get(3) == 3 and p.get(5) == 5
    p.close()
    assert sorted(read) == [3, 5]
    s = TileScheduler(100, 100, 2, 3, 10)
    s.attach_payload_loader(lambda i, t, profile_tile_base=None: i)
    assert s.attach_prefetcher(queue_size=2, order=[4])._order == [4]
    s.close()
    default = TilePrefetcher(list(range(3)), load, prefetch_queue_size=0)
    assert default._order == [0, 1, 2]


def _go(worker, zp, loop):
    if loop == "segment_one_zarr":
        worker._regions = None
        return worker._segment_one_zarr(zp, "t", model=None, use_gpu=False, log=logging.getLogger("t"))
    return _run(worker, zp, "run")


@pytest.mark.parametrize("loop", LOOPS)
def test_the_run_starts_on_the_first_tile_to_segment(app, tmp_path, monkeypatch, loop):
    from block01.workers.segment_merge_worker import SegmentMergeWorker as W
    lab = _labels(3)
    calls = []
    worker, zp = _worker(tmp_path, monkeypatch, lab, ROWS, COLS, OVERLAP,
                         skip=_plan(ROWS, COLS, OVERLAP, [], [0, 1, 2, 3, 9]), calls=calls)
    reads = []
    original = W._prepare_tile_payload

    def payload(self, zarr_path, z, tile, *a, skip=False, **k):
        reads.append((tile.tile_id if hasattr(tile, "tile_id") else tile, skip, len(calls)))
        return original(self, zarr_path, z, tile, *a, skip=skip, **k)
    monkeypatch.setattr(W, "_prepare_tile_payload", payload)
    order = []
    worker.tile_skipped.connect(lambda i, n: order.append(("skipped", i)))
    worker.progress.connect(lambda i, n, m: order.append(("progress", i)))
    worker.tile_done.connect(lambda i, n, c: order.append(("done", i)))
    _go(worker, zp, loop)
    skipped = {0, 1, 2, 3, 9}
    # every skipped tile is told first, in tile order, before any progress
    assert [e for e in order[:5]] == [("skipped", i) for i in sorted(skipped)]
    assert ("skipped", 0) not in order[5:]
    first_progress = next(e for e in order if e[0] == "progress")
    assert first_progress == ("progress", 4)
    # nothing was read for a skipped tile before the segmented tiles were done
    before = [r for r in reads if r[2] < len(calls)]
    assert all(not r[1] for r in before), "a skipped tile was read before the segmented ones"
    later = [r for r in reads if r[1]]
    assert len(later) == len(skipped) and all(r[2] == len(calls) for r in later)
    assert len(calls) == ROWS * COLS - len(skipped)
    # the global DAPI has no hole: the input's DAPI everywhere
    dapi_in = np.asarray(zarr.open(zp, mode="r")[..., 1])
    import tifffile
    name = "global_dapi_t.ome.tiff" if loop == "segment_one_zarr" else "global_dapi.ome.tiff"
    found = [os.path.join(b, name) for b, d, f in os.walk(worker.output_dir) if name in f]
    assert found, f"no {name}"
    assert np.array_equal(tifffile.imread(found[0]), dapi_in)


def test_a_skipped_tile_announced_next_stays_skipped(app):
    from block01.ui.step2_page import Step2Page
    page = Step2Page()
    page._begin_run_progress(1, 3)
    page._on_tile_skipped(1, 3)
    page._on_progress(1, 3, "tile 1 done")          # a finished tile announces the next index
    assert page._run_progress["state"][1] == "skipped"
    page._run_progress["forced"] = {2}
    page._on_tile_skipped(2, 3)
    page._on_progress(2, 3, "")
    assert page._run_progress["state"][2] == "skipped_forced"
    page._on_progress(0, 3, "")
    assert page._run_progress["state"][0] == "running"


def test_the_memory_line_never_imports_torch(monkeypatch):
    from block01.workers.segment_merge_worker import SegmentMergeWorker as W

    class Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if name == "torch" or name.startswith("torch."):
                raise AssertionError("torch imported")
            return None
    for k in [k for k in sys.modules if k == "torch" or k.startswith("torch.")]:
        monkeypatch.delitem(sys.modules, k)
    monkeypatch.setattr(sys, "meta_path", [Block()] + sys.meta_path)
    assert "RAM" in W._mem_snapshot()
    assert "torch" not in sys.modules


def test_the_run_is_timed_from_the_click(app, tmp_path, monkeypatch, capsys):
    import time
    lab = _labels(3)
    worker, zp = _worker(tmp_path, monkeypatch, lab, ROWS, COLS, OVERLAP,
                         skip=_plan(ROWS, COLS, OVERLAP, [], [0]))
    worker.run_clicked_at = time.perf_counter()
    worker.page_timings = ["[Step2 timing] input and parameters: 3 ms (3 ms after Run)"]
    _run(worker, zp, "run")
    out = capsys.readouterr().out
    for what in ("worker started", "first tile read, segmenting it", "first tile done"):
        assert out.count(f"[Step2 timing] {what}:") == 1, what
    assert "ms after Run" in out
    logs = [os.path.join(b, f) for b, d, fs in os.walk(worker.output_dir) for f in fs
            if f.startswith("segmentation_") and f.endswith(".log")]
    text = open(logs[0]).read()
    assert "[Step2 timing] input and parameters: 3 ms" in text      # the page's, in the run log
    assert "[Step2 timing] first tile done:" in text


def test_the_page_times_its_steps_and_hands_them_to_the_run(app, monkeypatch, tmp_path):
    """The page's `_run` up to the worker's start, on a synthetic input:
    its steps are printed and handed to the worker for its log."""
    from block01.ui import step2_page as P
    page = P.Step2Page()
    started = {}

    class FakeWorker:
        def __init__(self, **kw):
            for sig in ("progress", "tile_done", "tile_skipped", "job_finished", "error"):
                setattr(self, sig, type("S", (), {"connect": lambda self, f: None})())

        def start(self):
            started["timings"] = list(self.page_timings)
            started["clicked"] = self.run_clicked_at
    monkeypatch.setattr(P, "SegmentMergeWorker", FakeWorker)
    zp = str(tmp_path / "fused.zarr")
    zarr.open(zp, mode="w", shape=(64, 64, 2), chunks=(32, 32, 2), dtype=np.uint16)
    page._zarr_path = zp
    page._run()
    assert started, "the run did not start"
    steps = [ln.split("] ", 1)[1].split(":")[0] for ln in started["timings"]]
    assert steps[:2] == ["input and parameters", "TMA cores frozen (with any question answered)"]
    assert "run prepared" in steps


def test_on_run_the_skipped_tiles_stay_purple_and_the_first_tile_that_runs_is_running(
        app, monkeypatch, tmp_path):
    """The user's acceptance (2026-11-13): a Run never takes the skipped
    tiles' purple away (they are recorded skipped in the turn that resets
    the grid), and the running window starts on the first tile that runs,
    not on tile 0."""
    from block01.ui import step2_page as P
    page = P.Step2Page()

    class FakeWorker:
        def __init__(self, **kw):
            for sig in ("progress", "tile_done", "tile_skipped", "job_finished", "error"):
                setattr(self, sig, type("S", (), {"connect": lambda self, f: None})())

        def start(self):
            pass
    monkeypatch.setattr(P, "SegmentMergeWorker", FakeWorker)
    zp = str(tmp_path / "fused.zarr")
    zarr.open(zp, mode="w", shape=(64, 96, 2), chunks=(32, 32, 2), dtype=np.uint16)
    page._zarr_path = zp
    page._auto_tile_strategy.setChecked(False)
    page._rows_spin.setValue(2)
    page._cols_spin.setValue(3)
    page._full_h, page._full_w = 64, 96             # a drawn 2 x 3 grid, as on screen
    page._ov_h, page._ov_w, page._ov_ds = 64, 96, 1
    page._draw_tile_grid()
    assert len(page._tile_status) == 6
    page._skip_empty_cb.setChecked(True)          # (re-plans from a slide this input has not)
    page._skip_plan = {"grid": [2, 3], "overlap": page._overlap_spin.value(), "auto": [0, 1, 4],
                       "user_kept": [4], "user_forced": {2: 0.4}, "fractions": [0.0] * 6, "mask": None}
    colours = []
    monkeypatch.setattr(page, "_set_tile_colour", lambda r, c, st: colours.append((r * 3 + c, st)))
    page._run()
    state = page._run_progress["state"]
    assert state[0] == "skipped" and state[1] == "skipped" and state[2] == "skipped_forced"
    assert state[3] == "running" and state.get(4) != "skipped"          # 4 kept by the user
    assert 0 not in [i for i, st in colours if st == "running"]          # never tile 0
    # the last colour every skipped tile got in this turn is its purple
    last = {}
    for i, st in colours:
        last[i] = st
    assert last[0] == "skipped" and last[1] == "skipped" and last[2] == "skipped_forced"
