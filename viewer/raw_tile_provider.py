"""Pyramid raw-tile I/O for OME-TIFF sources.

Opens the OME-TIFF once at construction to record pyramid geometry, dtype
and channel names. Pixel reads then go through one of three handle modes
(see `handle_mode` in `__init__`):

- "per_call" (default, UNCHANGED baseline behavior): each `read_region` call
  opens its own `tifffile.TiffFile` handle, so concurrent calls from an I/O
  thread pool are trivially safe (tifffile/zarr handles are not guaranteed
  thread-safe to share) -- at the cost of a fresh TiffFile open per call.
- "per_thread": each thread lazily opens and caches its OWN TiffFile (and
  {level: zarr array} view) in thread-local storage, reused for the life of
  the provider. Avoids repeated opens for a thread that reads many tiles,
  while still never sharing a single handle across threads.
- "shared_lock": ONE TiffFile (and {level: zarr array} view), opened lazily,
  shared by all threads and serialized through a single `threading.Lock`.
  Measures whether handle reuse alone (no parallel decode) helps.

`open_count` tracks how many `tifffile.TiffFile(...)` opens this provider
instance has actually performed, across all modes, so handle reuse is
verifiable in benchmark output.
"""

import os
import re
import threading
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple

import numpy as np

from .tile_types import SourceIdentity, TileAddress

_OME_NS = {"ome": "http://www.openmicroscopy.org/Schemas/OME/2016-06"}

_HANDLE_MODES = ("per_call", "per_thread", "shared_lock")


class RawTileProvider:
    """Reads float32 tiles/regions from an OME-TIFF pyramid on demand."""

    # Default measured 2026-08-30 (docs/benchmarks/..._handle_modes.md):
    # per-call TiffFile+aszarr opening re-parses the TIFF structure on every
    # read (~13x slower cold viewport fill, ~10x slower cold-region pan).
    # "per_thread" reuses one handle per I/O thread; "per_call" remains
    # available as the measurement baseline.
    def __init__(self, path: str, handle_mode: str = "per_thread"):
        if handle_mode not in _HANDLE_MODES:
            raise ValueError(
                f"handle_mode must be one of {_HANDLE_MODES}, got {handle_mode!r}")
        self.path = os.path.abspath(path)
        self.handle_mode = handle_mode

        # open_count: total number of tifffile.TiffFile(...) opens performed
        # by this provider instance (any mode) -- a benchmark-verifiable
        # handle-reuse counter.
        self.open_count = 0
        self._open_count_lock = threading.Lock()

        # per_thread mode state: thread-local cache of (TiffFile, {level:
        # zarr array}), plus a registry (guarded by _registry_lock) of every
        # thread-local TiffFile opened, so close() can close them all.
        self._thread_local = threading.local()
        self._thread_registry: List = []
        self._registry_lock = threading.Lock()

        # shared_lock mode state: one lazily-opened TiffFile + level-array
        # cache, guarded by _shared_lock for both open and every read.
        self._shared_lock = threading.Lock()
        self._shared_tf = None
        self._shared_levels: Dict[int, object] = {}

        self._closed = False

        import tifffile

        with self._counted_tifffile(tifffile, self.path) as tf:
            series0 = tf.series[0]
            self._level_shapes: List[Tuple[int, int, int]] = [
                tuple(level.shape) for level in series0.levels
            ]
            self._dtype = str(series0.dtype)
            self._num_channels = self._level_shapes[0][0]
            self._channel_names = self._parse_channel_names(tf, self._num_channels)

    def _counted_tifffile(self, tifffile_mod, path):
        with self._open_count_lock:
            self.open_count += 1
        return tifffile_mod.TiffFile(path)

    # ── metadata ─────────────────────────────────────────────────────────

    @staticmethod
    def _parse_channel_names(tf, num_channels: int) -> List[str]:
        names: List[str] = []
        try:
            xml = tf.ome_metadata
            if xml:
                root = ET.fromstring(xml)
                for ch in root.findall(".//ome:Channel", _OME_NS):
                    name = ch.attrib.get("Name")
                    if name:
                        names.append(name)
        except Exception:
            names = []
        if len(names) != num_channels:
            names = [f"ch_{i:02d}" for i in range(num_channels)]
        return names

    def source_identity(self) -> SourceIdentity:
        st = os.stat(self.path)
        fingerprint = f"{st.st_size}:{st.st_mtime_ns}"
        return SourceIdentity(
            dataset_path=self.path,
            dataset_fingerprint=fingerprint,
            stage="raw",
        )

    @property
    def num_levels(self) -> int:
        return len(self._level_shapes)

    @property
    def num_channels(self) -> int:
        return self._num_channels

    @property
    def channel_names(self) -> List[str]:
        return list(self._channel_names)

    def channel_index(self, channel) -> int:
        """Resolve a channel name or index to an integer channel index."""
        if isinstance(channel, int):
            return channel
        try:
            return self._channel_names.index(channel)
        except ValueError:
            m = re.match(r"^ch_(\d+)$", str(channel))
            if m:
                return int(m.group(1))
            raise KeyError(f"unknown channel: {channel!r}")

    def level_shape(self, level: int) -> Tuple[int, int]:
        _, h, w = self._level_shapes[level]
        return (h, w)

    def level_downsample(self, level: int) -> float:
        """Downsample factor of `level` relative to level 0 (>= 1), ROUNDED
        to the nearest integer. This is a parameter-scaling convenience
        (see `effective_param`) -- NOT geometry. Any world-rect / placement
        math must use `level_downsample_yx` instead (unrounded, per-axis),
        or cumulative drift creeps in across non-integer pyramids."""
        h0, _w0 = self.level_shape(0)
        hn, _wn = self.level_shape(level)
        if hn <= 0:
            return 1.0
        return round(h0 / hn)

    def level_downsample_yx(self, level: int) -> Tuple[float, float]:
        """(ds_y, ds_x) downsample factors of `level` relative to level 0,
        as UNROUNDED floats, computed independently per axis
        (h0/hL, w0/wL). Pyramids are not always square-ratio per level (a
        3000x1000 level-0 could reduce to 1000x333 -- ds_y=3.0, ds_x=3.003),
        and even a "square" pyramid accumulates rounding drift if a single
        rounded scalar is applied to both axes over many tiles/levels. All
        world-rect / placement geometry in the viewer must use this, not
        `level_downsample`."""
        h0, w0 = self.level_shape(0)
        hn, wn = self.level_shape(level)
        ds_y = (h0 / hn) if hn > 0 else 1.0
        ds_x = (w0 / wn) if wn > 0 else 1.0
        return ds_y, ds_x

    # ── pixel reads ──────────────────────────────────────────────────────

    def _open_level_array(self, tf, level: int):
        import zarr

        z = zarr.open(tf.series[0].levels[level].aszarr(), mode="r")
        if not hasattr(z, "shape"):
            # levels[0].aszarr() of a pyramidal series yields the whole
            # multiscale GROUP (keys "0".."N"); deeper levels yield an Array.
            z = z[str(level)]
        return z

    # ── handle-mode plumbing ─────────────────────────────────────────────

    def _per_thread_state(self):
        """Return this thread's (TiffFile, {level: zarr array}) pair,
        opening + registering it on first use in this thread."""
        state = getattr(self._thread_local, "state", None)
        if state is not None:
            return state

        import tifffile

        tf = self._counted_tifffile(tifffile, self.path).__enter__()
        levels: Dict[int, object] = {}
        state = (tf, levels)
        # Register BEFORE publishing to thread-local storage, and check
        # _closed only after registering: close() walks the registry under
        # _registry_lock and clears it, so whichever of {this registration,
        # that walk} takes the lock second sees the other's effect --
        # either the handle lands in the registry before close() drains it
        # (and gets closed there), or _closed is already True by the time we
        # get here (and we close it ourselves below) -- there is no window
        # where a handle is neither registered-and-closed nor closed here.
        with self._registry_lock:
            self._thread_registry.append(tf)
            closed = self._closed
        if closed:
            # close() already ran (or is running) and may have finished
            # draining the registry before we appended -- either way, close
            # this handle ourselves rather than leaving it open or letting
            # the caller use a handle on a "closed" provider.
            try:
                tf.close()
            except Exception:
                pass
            with self._registry_lock:
                try:
                    self._thread_registry.remove(tf)
                except ValueError:
                    pass
            raise RuntimeError(
                f"RawTileProvider for {self.path!r} is closed; "
                "no further reads are allowed")
        self._thread_local.state = state
        return state

    def _shared_state(self):
        """Return the single shared (TiffFile, {level: zarr array}) pair,
        opening it lazily on first use. Caller must hold `_shared_lock`."""
        if self._shared_tf is None:
            import tifffile

            self._shared_tf = self._counted_tifffile(tifffile, self.path).__enter__()
        return self._shared_tf, self._shared_levels

    def warm_thread_handle(self, levels=(0,)) -> bool:
        """Build THIS calling thread's handle (the same state
        `_per_thread_state()` returns) plus the zarr level arrays named in
        `levels`, paying tifffile's OME-XML/page-table parse cost now
        instead of on the thread's first real read.

        Only meaningful in "per_thread" mode (the only mode with a
        per-thread handle to warm); in "per_call"/"shared_lock" mode this
        is a cheap, harmless no-op that returns True without opening
        anything extra.

        Idempotent: a second call on an already-warmed thread just looks up
        the cached state and cached level arrays again -- cheap.

        Safe on a closed provider: returns False rather than raising or
        reopening a handle. Non-fatal on ANY failure (closed provider,
        I/O error, whatever): caught here, reported as False, never
        propagated -- a worker that fails to warm must still serve
        requests; it simply pays the setup cost on its first real read
        instead of upfront.
        """
        if self._closed:
            return False
        try:
            if self.handle_mode != "per_thread":
                return True
            tf, level_arrays = self._per_thread_state()
            for level in levels:
                if level not in level_arrays:
                    level_arrays[level] = self._open_level_array(tf, level)
            return True
        except Exception:
            return False

    def close(self):
        """Close every handle this provider opened for "per_thread" and
        "shared_lock" modes. Lifecycle contract is uniform across ALL modes
        (per_call included, even though it tracks no handles): after
        close(), any read_tile/read_region raises RuntimeError."""
        self._closed = True
        with self._registry_lock:
            handles = list(self._thread_registry)
            self._thread_registry.clear()
        for tf in handles:
            try:
                tf.close()
            except Exception:
                pass
        with self._shared_lock:
            if self._shared_tf is not None:
                try:
                    self._shared_tf.close()
                except Exception:
                    pass
                self._shared_tf = None
                self._shared_levels.clear()

    def read_tile(self, channel, tile: TileAddress):
        """Return (2D array in NATIVE source dtype, io_ms).

        Array may be smaller at edges. dtype is whatever the source stores
        (e.g. uint8/uint16) — no float cast here; callers (assembler /
        compute) cast to float32 only at the compute boundary.
        """
        import time

        h, w = self.level_shape(tile.level)
        ts = tile.grid.tile_size
        y0 = tile.ty * ts
        x0 = tile.tx * ts
        y1 = min(y0 + ts, h)
        x1 = min(x0 + ts, w)

        t0 = time.perf_counter()
        arr, _offset = self.read_region(channel, tile.level, y0, y1, x0, x1)
        io_ms = (time.perf_counter() - t0) * 1000.0
        return arr, io_ms

    def read_region(
        self,
        channel,
        level: int,
        y0: int,
        y1: int,
        x0: int,
        x1: int,
    ) -> Tuple[np.ndarray, Tuple[int, int]]:
        """Read a clamped region; returns (array, (actual_y0, actual_x0)).

        The requested bounds are clamped to the level's valid extent. The
        returned offset is the actual top-left the array corresponds to,
        which may differ from (y0, x0) when the request ran off the image.
        The array is returned in the source's NATIVE dtype (no float cast) —
        callers cast to float32 only at the compute boundary. Timing is the
        caller's responsibility (see read_tile / RawTileAssembler, which wrap
        this call themselves).
        """
        if self._closed:
            # A stale thread-local handle after close() would be undefined
            # behavior; fail loudly instead. Lifecycle contract: stop the
            # scheduler -> join its workers -> provider.close().
            raise RuntimeError(
                f"RawTileProvider for {self.path!r} is closed; "
                "no further reads are allowed")

        h, w = self.level_shape(level)
        cy0 = max(0, min(y0, h))
        cy1 = max(0, min(y1, h))
        cx0 = max(0, min(x0, w))
        cx1 = max(0, min(x1, w))

        c = self.channel_index(channel)

        if self.handle_mode == "per_call":
            import tifffile

            with self._counted_tifffile(tifffile, self.path) as tf:
                zarr_arr = self._open_level_array(tf, level)
                data = np.asarray(zarr_arr[c, cy0:cy1, cx0:cx1])

        elif self.handle_mode == "per_thread":
            tf, levels = self._per_thread_state()
            zarr_arr = levels.get(level)
            if zarr_arr is None:
                zarr_arr = self._open_level_array(tf, level)
                levels[level] = zarr_arr
            data = np.asarray(zarr_arr[c, cy0:cy1, cx0:cx1])

        elif self.handle_mode == "shared_lock":
            with self._shared_lock:
                tf, levels = self._shared_state()
                zarr_arr = levels.get(level)
                if zarr_arr is None:
                    zarr_arr = self._open_level_array(tf, level)
                    levels[level] = zarr_arr
                data = np.asarray(zarr_arr[c, cy0:cy1, cx0:cx1])

        else:
            raise ValueError(f"unknown handle_mode: {self.handle_mode!r}")

        return data, (cy0, cx0)
