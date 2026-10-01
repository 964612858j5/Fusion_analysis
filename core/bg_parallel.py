"""Bounded, in-order parallel execution of Step0's correction tiles (block S0P;
docs/v16_Step0_cpu_parallel_application.md v2 §3.1, §9).

The tiles of one channel are corrected on up to `n` threads (the kernels are
skimage / scipy C code; threads were as fast as processes and used less
memory, §2.4), and their results are delivered IN TILE ORDER, so the caller
writes the zarr and accumulates the coarse plane exactly as the serial loop
does: the result is bitwise the serial one by construction.

Two bounds keep it predictable:
  * at most `n` tiles submitted and not yet consumed (computing, or done
    and waiting to be written) -- `stats["max_in_flight"]` records the peak;
  * the next tile is submitted only after the previous tile's READ has
    finished, and only while `should_stop()` is false: reads stay one after
    another (cheap), the corrections overlap (expensive), and a cancel
    raised during a read starts nothing new.

Leaving the iteration early (a cancel, an exception, the consumer breaking
out) cancels every tile not yet started and waits for the running ones, so
no thread outlives the channel.

Pure Python + `concurrent.futures`; no Qt.
"""

import math
import os
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Callable, Iterable, Iterator, Optional

#: Fixed constants of the worker-count rule (§9; no user setting).
MAX_WORKERS = 4
MEM_RESERVE_BYTES = 1.0e9          # left free beside the in-flight tiles
MEM_PER_TILE_BYTES = 0.75e9        # one in-flight 4096 tile (measured 2.34 GB / 4 at n=4)


def mem_available_bytes() -> Optional[int]:
    """``MemAvailable`` from /proc/meminfo, or None where it cannot be read."""
    try:
        with open("/proc/meminfo", "r", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def choose_workers(cpu: Optional[int], n_tiles: int, avail_bytes: Optional[int]) -> int:
    """How many tiles of one channel run at once (§9, ruling 4):
    ``min(4, cpu//2 - 1, n_tiles, (MemAvailable - 1 GB) // 0.75 GB)``; the
    memory term is left out when unknown. Below 2 the caller runs serially."""
    n_cpu = max(1, (cpu or 2) // 2 - 1)
    n = min(MAX_WORKERS, n_cpu, max(1, int(n_tiles)))
    if avail_bytes is not None:
        n = min(n, max(0, math.floor((avail_bytes - MEM_RESERVE_BYTES) / MEM_PER_TILE_BYTES)))
    return max(1, n)


class _Slot:
    __slots__ = ("future", "read_done")

    def __init__(self):
        self.future = None
        self.read_done = threading.Event()


def ordered_results(task: Callable, items: Iterable, n: int,
                    should_stop: Callable[[], bool] = lambda: False,
                    stats: Optional[dict] = None) -> Iterator:
    """Yield ``task(item, read_done)`` for every item, in order.

    `task` must call ``read_done()`` once its input is read (if it never does,
    the next tile waits for this one to finish). With ``n <= 1`` everything
    runs on the calling thread, one tile at a time -- today's loop."""
    items = list(items)
    if stats is not None:
        stats.update(max_in_flight=0, submitted=0, workers=max(1, int(n)))
    if n <= 1:
        for item in items:
            if should_stop():
                return
            if stats is not None:
                stats["submitted"] += 1
                stats["max_in_flight"] = max(stats["max_in_flight"], 1)
            yield task(item, lambda: None)
        return
    pool = ThreadPoolExecutor(max_workers=int(n), thread_name_prefix="bg-tiles")
    pending = deque()
    nxt = 0
    try:
        while pending or nxt < len(items):
            while (nxt < len(items) and len(pending) < n
                   and (not pending or pending[-1].read_done.is_set()
                        or pending[-1].future.done())):
                if should_stop():
                    nxt = len(items)                  # start nothing new
                    break
                slot = _Slot()
                slot.future = pool.submit(task, items[nxt], slot.read_done.set)
                pending.append(slot)
                nxt += 1
                if stats is not None:
                    stats["submitted"] += 1
                    stats["max_in_flight"] = max(stats["max_in_flight"], len(pending))
            if not pending:
                break
            head = pending[0]
            if head.future.done():
                pending.popleft()
                yield head.future.result()            # re-raises the task's exception
                continue
            # wait for the head to finish; wake up early so that a finished
            # read can let the next tile start
            wait([head.future], timeout=0.02)
    finally:
        for slot in pending:
            slot.future.cancel()
        pool.shutdown(wait=True)


__all__ = ["choose_workers", "mem_available_bytes", "ordered_results", "MAX_WORKERS"]
