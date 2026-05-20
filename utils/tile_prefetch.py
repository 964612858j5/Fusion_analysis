"""Small background prefetcher for Step2 tiles."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor


class TilePrefetcher:
    """Prefetch inference-ready tile payloads with bounded concurrency."""

    def __init__(self, tiles, load_fn, prefetch_queue_size=2, logger=None, profiler=None):
        self.tiles = list(tiles or [])
        self.load_fn = load_fn
        self.prefetch_queue_size = max(0, int(prefetch_queue_size or 0))
        self.logger = logger
        self.profiler = profiler
        self._executor = None
        self._futures = {}
        self._next_submit = 0
        self._closed = False
        self.metrics = {
            "prefetch_wait_seconds": 0.0,
            "prefetch_hit": 0,
            "prefetch_miss": 0,
            "prefetch_queue_depth": 0,
        }
        if self.prefetch_queue_size > 0:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="step2-tile-prefetch")
            self._fill()

    def close(self):
        self._closed = True
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)

    def get(self, index, sync_load_fn=None):
        if self.prefetch_queue_size <= 0 or self._executor is None:
            self.metrics["prefetch_miss"] += 1
            return (sync_load_fn or self.load_fn)(index, self.tiles[index])
        self._fill()
        start = time.perf_counter()
        future = self._futures.pop(index, None)
        if future is None:
            self.metrics["prefetch_miss"] += 1
            self._log(f"[TilePrefetch] prefetch miss tile={index}")
            payload = (sync_load_fn or self.load_fn)(index, self.tiles[index])
        else:
            try:
                payload = future.result()
                self.metrics["prefetch_hit"] += 1
                self._log(f"[TilePrefetch] prefetch hit tile={index}")
            except Exception as exc:
                self.metrics["prefetch_miss"] += 1
                self._log(f"[TilePrefetch] warning tile={index} fallback sync read: {exc}")
                payload = (sync_load_fn or self.load_fn)(index, self.tiles[index])
        wait_s = max(0.0, time.perf_counter() - start)
        self.metrics["prefetch_wait_seconds"] += wait_s
        self.metrics["prefetch_queue_depth"] = len(self._futures)
        if self.profiler:
            try:
                self.profiler.log_tile_stage(
                    str(index),
                    "tile_prefetch_wait",
                    wait_s,
                    prefetch_hit=1 if future is not None else 0,
                    prefetch_queue_depth=len(self._futures),
                )
            except Exception:
                pass
        self._fill()
        return payload

    def snapshot_metrics(self):
        out = dict(self.metrics)
        out["prefetch_queue_depth"] = len(self._futures)
        return out

    def _fill(self):
        if self._closed or self._executor is None:
            return
        done = [idx for idx, fut in self._futures.items() if fut.cancelled()]
        for idx in done:
            self._futures.pop(idx, None)
        while len(self._futures) < self.prefetch_queue_size and self._next_submit < len(self.tiles):
            idx = self._next_submit
            self._next_submit += 1
            if idx in self._futures:
                continue
            self._futures[idx] = self._executor.submit(self.load_fn, idx, self.tiles[idx])
        self.metrics["prefetch_queue_depth"] = len(self._futures)

    def _log(self, msg):
        if self.logger:
            try:
                self.logger.debug(msg)
            except Exception:
                pass
        else:
            try:
                print(msg)
            except Exception:
                pass
