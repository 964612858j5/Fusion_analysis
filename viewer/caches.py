"""Byte-bounded LRU caches for raw and corrected tiles.

One instance of `LRUByteCache` is used for raw tiles (keyed by RawKey) and a
separate instance for corrected tiles (keyed by CorrectionKey) — see
docs/v15_viewer_foundation_interfaces.md §5. Thread-safe: all mutating
operations are serialized by a single lock.
"""

import threading
from collections import OrderedDict

import numpy as np


class LRUByteCache:
    """LRU cache evicted by total bytes of stored numpy arrays."""

    def __init__(self, max_bytes: int):
        self.max_bytes = int(max_bytes)
        self._store: "OrderedDict[object, np.ndarray]" = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(self, key):
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
                self._hits += 1
                return self._store[key]
            self._misses += 1
            return None

    def put(self, key, value: np.ndarray):
        with self._lock:
            if key in self._store:
                self._bytes -= self._store[key].nbytes
                del self._store[key]
            self._store[key] = value
            self._bytes += value.nbytes
            self._store.move_to_end(key)
            self._evict_locked()

    def _evict_locked(self):
        while self._bytes > self.max_bytes and self._store:
            _, evicted = self._store.popitem(last=False)
            self._bytes -= evicted.nbytes
            self._evictions += 1

    def stats(self) -> dict:
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
                "bytes": self._bytes,
                "items": len(self._store),
            }

    def clear(self):
        with self._lock:
            self._store.clear()
            self._bytes = 0
