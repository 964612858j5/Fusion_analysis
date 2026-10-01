"""Block S0P: Step0's background correction in parallel on the CPU
(docs/v16_Step0_cpu_parallel_application.md v2, gates P1-P8).

Segment 1: the bounded in-order executor (`core/bg_parallel.py`) and the
worker-count rule, on their own.
"""

import threading
import time

import numpy as np
import pytest

from block01.core import bg_parallel as bp  # noqa: E402


# ── the worker-count rule (ruling 4) ────────────────────────────────────

@pytest.mark.parametrize("cpu,tiles,avail,want", [
    (16, 16, None, 4),                      # this machine, memory unknown
    (16, 16, int(8e9), 4),
    (16, 2, None, 2),                       # never more than the tiles
    (16, 16, int(2.6e9), 2),                # (2.6 - 1.0) / 0.75 = 2
    (16, 16, int(1.5e9), 1),                # < 2 fit: serial
    (4, 16, None, 1),                       # 4 // 2 - 1 = 1: serial
    (6, 16, None, 2),
    (None, 16, None, 1),
])
def test_choose_workers(cpu, tiles, avail, want):
    assert bp.choose_workers(cpu, tiles, avail) == want


def test_mem_available_is_read_or_none():
    v = bp.mem_available_bytes()
    assert v is None or v > 0


# ── the executor ────────────────────────────────────────────────────────

def _task(delay_of=lambda i: 0.01, reads=None, started=None):
    def task(i, read_done):
        if started is not None:
            started.append(i)
        if reads is not None:
            reads.append(i)
        read_done()
        time.sleep(delay_of(i))
        return i * 10
    return task


def test_results_come_in_order_whatever_finishes_first():
    # later tiles finish first
    out = list(bp.ordered_results(_task(lambda i: 0.05 * (5 - i)), range(6), 4))
    assert out == [0, 10, 20, 30, 40, 50]


def test_at_most_n_tiles_are_in_flight():
    stats = {}
    list(bp.ordered_results(_task(lambda i: 0.02), range(12), 3, stats=stats))
    assert stats["max_in_flight"] == 3 and stats["submitted"] == 12


def test_the_corrections_overlap():
    t0 = time.perf_counter()
    list(bp.ordered_results(_task(lambda i: 0.2), range(8), 4))
    assert time.perf_counter() - t0 < 0.2 * 8 / 2          # serial would be 1.6 s


def test_a_stop_during_a_read_starts_nothing_new():
    """`test_wsi_cancel`'s semantics: a cancel raised while tile 0 is read
    -> tile 0 completes, no other tile is read."""
    flag = threading.Event()
    reads = []

    def task(i, read_done):
        reads.append(i)
        if i == 0:
            time.sleep(0.1)                 # the read takes a while ...
            flag.set()                      # ... and the cancel arrives during it
        read_done()
        time.sleep(0.05)
        return i
    out = list(bp.ordered_results(task, range(5), 4, should_stop=flag.is_set))
    assert reads == [0] and out == [0]


def test_leaving_early_cancels_the_unstarted_and_waits_for_the_running():
    started, done = [], []

    def task(i, read_done):
        started.append(i)
        read_done()
        time.sleep(0.1)
        done.append(i)
        return i
    gen = bp.ordered_results(task, range(20), 3)
    assert next(gen) == 0
    gen.close()                              # the consumer stops (cancel)
    assert sorted(done) == sorted(started)   # every started tile finished
    assert len(started) <= 1 + 3             # nothing beyond the window started
    assert not [t for t in threading.enumerate() if t.name.startswith("bg-tiles")]


def test_an_exception_in_a_tile_reaches_the_consumer_in_order():
    def task(i, read_done):
        read_done()
        if i == 2:
            raise RuntimeError("tile 2")
        return i
    got = []
    with pytest.raises(RuntimeError, match="tile 2"):
        for v in bp.ordered_results(task, range(6), 3):
            got.append(v)
    assert got == [0, 1]


def test_one_worker_is_todays_serial_loop():
    order = []

    def task(i, read_done):
        order.append(("start", i))
        read_done()
        order.append(("end", i))
        return i
    assert list(bp.ordered_results(task, range(3), 1)) == [0, 1, 2]
    assert order == [("start", 0), ("end", 0), ("start", 1), ("end", 1),
                     ("start", 2), ("end", 2)]


def test_a_task_that_never_reports_its_read_still_lets_the_next_start():
    out = list(bp.ordered_results(lambda i, rd: i, range(5), 3))
    assert out == list(range(5))


def test_array_results_are_delivered_untouched():
    rng = np.random.default_rng(0)
    data = [rng.random((8, 8), dtype=np.float32) for _ in range(6)]

    def task(i, read_done):
        read_done()
        return data[i].copy()
    for a, b in zip(bp.ordered_results(task, range(6), 4), data):
        assert np.array_equal(a, b)
