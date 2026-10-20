"""Step2 never flushes or drops the machine's file cache (docs/f5_tma_display_plan.md
part 1): no ``sync`` and no write to /proc/sys/vm/drop_caches, in either loop,
for skipped, empty and filled tiles and the finishing exports. The old per-tile
``sync`` waited for every unwritten byte on the machine (75 s after tile 1 on
the Slice1 run) and the drop made the next tile re-read its input from disk."""

import builtins
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")
pytest.importorskip("zarr")

from test_f4_step2 import _snapshot, _with_regions  # noqa: E402
from test_step2_ownership_move import _labels  # noqa: E402
from test_step2_skip_empty_tiles import LOOPS, _empty_tiles, _plan, _run, _worker  # noqa: E402

ROWS, COLS, OVERLAP = 3, 4, 8


def _watch(monkeypatch):
    said = []
    real_open = builtins.open
    monkeypatch.setattr(os, "system", lambda cmd: said.append(("system", cmd)) or 0)

    def watched_open(file, *a, **k):
        if "drop_caches" in str(file):
            said.append(("open", str(file)))
        return real_open(file, *a, **k)
    monkeypatch.setattr(builtins, "open", watched_open)
    return said


@pytest.mark.parametrize("case", ["plan", "regions", "empty_run"])
@pytest.mark.parametrize("loop", LOOPS)
def test_no_tile_and_no_export_touches_the_machines_file_cache(tmp_path, monkeypatch, loop, case):
    """plan: empty tiles skipped by the tissue plan; regions: and by the core
    rules; empty_run: no plan, so the empty tiles run and return no labels."""
    lab = _labels(3)
    h, w = lab.shape
    empty = _empty_tiles(lab, ROWS, COLS, OVERLAP)
    assert empty
    calls = []
    worker, zp = _worker(tmp_path, monkeypatch, lab, ROWS, COLS, OVERLAP, name="c", calls=calls,
                         skip=None if case == "empty_run" else _plan(ROWS, COLS, OVERLAP, empty))
    if case == "regions":
        _with_regions(monkeypatch, worker, _snapshot(h, w, core_box=(0, 40, 60, 113)))
    skipped = []
    worker.tile_skipped.connect(lambda i, n: skipped.append(i))
    said = _watch(monkeypatch)
    n = _run(worker, zp, loop)
    assert n                                   # tiles with cells ran
    if case == "empty_run":                    # every tile ran the model, the empty ones too
        assert not skipped and len(calls) == ROWS * COLS
    else:
        assert skipped
    assert said == []
    from block01.workers.segment_merge_worker import SegmentMergeWorker
    assert not hasattr(SegmentMergeWorker, "_drop_caches")
