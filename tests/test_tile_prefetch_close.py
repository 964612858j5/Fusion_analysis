"""`TilePrefetcher.close()` settles its reads (docs/v17_step2_prefetch_cleanup_plan.md
§1.1): queued reads never start, the read under way has finished when
`close()` returns True and the prefetch thread has exited; a read past the
budget gives False and one warning; twice is harmless.
"""

import threading
import time

from block01.utils.tile_prefetch import TilePrefetcher


def _prefetch_threads():
    return [t for t in threading.enumerate() if t.name.startswith("step2-tile-prefetch")]


class _Log:
    def __init__(self):
        self.warnings = []

    def warning(self, msg):
        self.warnings.append(msg)

    def debug(self, msg):
        pass


def _slow_first(started, release, done):
    def load(index, tile):
        if index == 0:
            started.set()
            release.wait(timeout=10)
        done.append(index)
        return index
    return load


def test_close_waits_for_the_read_under_way_and_cancels_the_rest():
    started, release, done = threading.Event(), threading.Event(), []
    p = TilePrefetcher(range(6), _slow_first(started, release, done), prefetch_queue_size=3)
    assert started.wait(timeout=5)
    threading.Timer(0.2, release.set).start()
    t0 = time.perf_counter()
    assert p.close() is True
    assert time.perf_counter() - t0 >= 0.15            # it waited for read 0
    assert done == [0]                                 # reads 1, 2 were queued: never ran
    for t in _prefetch_threads():
        t.join(timeout=2)
    assert not _prefetch_threads()


def test_a_read_past_the_budget_gives_false_and_one_warning():
    started, release, done = threading.Event(), threading.Event(), []
    log = _Log()
    p = TilePrefetcher(range(3), _slow_first(started, release, done), prefetch_queue_size=1,
                       logger=log)
    assert started.wait(timeout=5)
    try:
        t0 = time.perf_counter()
        assert p.close(wait_s=0.2) is False
        assert 0.15 <= time.perf_counter() - t0 < 2.0
        assert len(log.warnings) == 1 and "still running after 0.2 s" in log.warnings[0]
        assert p.close() is False and len(log.warnings) == 1     # the first answer, at once
    finally:
        release.set()
    for t in _prefetch_threads():
        t.join(timeout=5)
    assert done == [0] and not _prefetch_threads()      # finishing later does no harm


def test_close_twice_and_without_a_queue():
    p = TilePrefetcher(range(3), lambda i, t: i, prefetch_queue_size=2)
    assert p.get(0) == 0
    assert p.close() is True and p.close() is True
    assert TilePrefetcher(range(3), lambda i, t: i, prefetch_queue_size=0).close() is True


def test_a_read_that_raises_while_closing_is_ignored():
    started = threading.Event()

    def load(index, tile):
        started.set()
        time.sleep(0.1)
        raise OSError("drive gone")
    p = TilePrefetcher(range(2), load, prefetch_queue_size=1)
    assert started.wait(timeout=5)
    assert p.close() is True
