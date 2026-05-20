"""Persistent channel readers and small LRU cache for Step2 engine IO."""

from __future__ import annotations

import threading
from collections import OrderedDict

import numpy as np
import tifffile
import zarr


class SharedChannelStore:
    """Cache zarr/TIFF handles and repeated channel tile reads.

    Reads return copies so downstream segmentation code can mutate arrays
    without corrupting cached data.
    """

    def __init__(self, max_cache_items=32, logger=None):
        self.max_cache_items = max(1, int(max_cache_items or 32))
        self.logger = logger
        self._lock = threading.RLock()
        self._zarr = {}
        self._tiffs = {}
        self._cache = OrderedDict()
        self.metrics = {
            "cache_hit": 0,
            "cache_miss": 0,
            "cache_bytes": 0,
            "cache_evictions": 0,
        }

    def close(self):
        with self._lock:
            for item in self._tiffs.values():
                try:
                    item["tif"].close()
                except Exception:
                    pass
            self._tiffs.clear()
            self._zarr.clear()
            self._cache.clear()
            self.metrics["cache_bytes"] = 0

    def snapshot_metrics(self):
        with self._lock:
            return dict(self.metrics)

    def zarr_group(self, path):
        path = str(path)
        with self._lock:
            group = self._zarr.get(path)
            if group is None:
                group = zarr.open(path, mode="r")
                self._zarr[path] = group
                self._log(f"[ChannelCache] opened zarr path={path}")
            return group

    def read_fused(self, path, y0, y1, x0, x1):
        key = ("fused", str(path), int(y0), int(y1), int(x0), int(x1), 1)
        return self._cached(key, lambda: np.asarray(self.zarr_group(path)[y0:y1, x0:x1, :]))

    def read_dapi(self, path, y0, y1, x0, x1):
        key = ("dapi", str(path), int(y0), int(y1), int(x0), int(x1), 1)
        return self._cached(key, lambda: np.asarray(self.zarr_group(path)[y0:y1, x0:x1, 1]))

    def read_zarr_channel(self, source_id, group, channel_name, y0, y1, x0, x1):
        key = ("zarr_channel", str(source_id), str(channel_name), int(y0), int(y1), int(x0), int(x1), 1)
        return self._cached(key, lambda: np.asarray(group[channel_name][y0:y1, x0:x1], dtype=np.float32))

    def read_raw_ome(self, loader, channel_name, y0, y1, x0, x1, normalize=False):
        key = ("raw_ome", str(loader.filepath), str(channel_name), int(y0), int(y1), int(x0), int(x1), 1, bool(normalize))

        def _read():
            arr = self._read_raw_ome_region(loader, channel_name, y0, y1, x0, x1)
            arr = loader._apply_configured_correction(channel_name, arr, loader.correction_config)
            if normalize:
                return loader._norm(arr)
            return arr.astype(np.float32, copy=False)

        return self._cached(key, _read)

    def _read_raw_ome_region(self, loader, channel_name, y0, y1, x0, x1):
        if channel_name not in loader.ch_map:
            raise KeyError(f"Channel '{channel_name}' not found")
        item = self._tiff_item(loader.filepath)
        z0 = item["z0"]
        page_idx = int(loader.ch_map[channel_name])
        with self._lock:
            if z0.ndim == 3:
                return np.asarray(z0[page_idx, y0:y1, x0:x1])
            if z0.ndim == 4:
                return np.asarray(z0[0, page_idx, y0:y1, x0:x1])
            if z0.ndim == 2:
                return np.asarray(z0[y0:y1, x0:x1])
        raise ValueError(f"Unknown zarr dimensions: {z0.ndim}")

    def _tiff_item(self, path):
        path = str(path)
        with self._lock:
            item = self._tiffs.get(path)
            if item is not None:
                return item
            tif = tifffile.TiffFile(path)
            store = tif.aszarr()
            z = zarr.open(store, mode="r")
            group_type = getattr(getattr(zarr, "hierarchy", None), "Group", None)
            if group_type is not None and isinstance(z, group_type):
                z0 = z[0] if "0" in z else next(iter(z.values()))
            elif hasattr(z, "array_keys") or hasattr(z, "groups"):
                z0 = z[0] if "0" in z else next(iter(z.values()))
            else:
                z0 = z
            item = {"tif": tif, "store": store, "z": z, "z0": z0}
            self._tiffs[path] = item
            self._log(f"[ChannelCache] opened TIFF path={path}")
            return item

    def _cached(self, key, loader):
        with self._lock:
            arr = self._cache.get(key)
            if arr is not None:
                self._cache.move_to_end(key)
                self.metrics["cache_hit"] += 1
                self._log(f"[ChannelCache] hit {key[0]} bbox={key[-5:-1]}")
                return np.array(arr, copy=True)
            self.metrics["cache_miss"] += 1
            self._log(f"[ChannelCache] miss {key[0]} bbox={key[-5:-1]}")

        arr = np.asarray(loader())
        cached = np.array(arr, copy=True)
        with self._lock:
            self._cache[key] = cached
            self.metrics["cache_bytes"] += int(cached.nbytes)
            while len(self._cache) > self.max_cache_items:
                _old_key, old = self._cache.popitem(last=False)
                self.metrics["cache_bytes"] -= int(getattr(old, "nbytes", 0))
                self.metrics["cache_evictions"] += 1
        return np.array(cached, copy=True)

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
