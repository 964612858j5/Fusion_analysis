"""Pyramid raw-tile I/O for OME-TIFF sources.

Opens the OME-TIFF once at construction to record pyramid geometry, dtype
and channel names; each `read_tile`/`read_region` call opens its own
`tifffile.TiffFile` handle so that concurrent calls from an I/O thread pool
are safe (tifffile/zarr handles are not guaranteed thread-safe to share).
"""

import os
import re
import xml.etree.ElementTree as ET
from typing import List, Optional, Tuple

import numpy as np

from .tile_types import SourceIdentity, TileAddress

_OME_NS = {"ome": "http://www.openmicroscopy.org/Schemas/OME/2016-06"}


class RawTileProvider:
    """Reads float32 tiles/regions from an OME-TIFF pyramid on demand."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)

        import tifffile

        with tifffile.TiffFile(self.path) as tf:
            series0 = tf.series[0]
            self._level_shapes: List[Tuple[int, int, int]] = [
                tuple(level.shape) for level in series0.levels
            ]
            self._dtype = str(series0.dtype)
            self._num_channels = self._level_shapes[0][0]
            self._channel_names = self._parse_channel_names(tf, self._num_channels)

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

    # ── pixel reads ──────────────────────────────────────────────────────

    def _open_level_array(self, tf, level: int):
        import zarr

        z = zarr.open(tf.series[0].levels[level].aszarr(), mode="r")
        if not hasattr(z, "shape"):
            # levels[0].aszarr() of a pyramidal series yields the whole
            # multiscale GROUP (keys "0".."N"); deeper levels yield an Array.
            z = z[str(level)]
        return z

    def read_tile(self, channel, tile: TileAddress):
        """Return (float32 2D array, io_ms). Array may be smaller at edges."""
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
        Timing is the caller's responsibility (see read_tile / correction
        compute, which wrap this call themselves).
        """
        import tifffile

        h, w = self.level_shape(level)
        cy0 = max(0, min(y0, h))
        cy1 = max(0, min(y1, h))
        cx0 = max(0, min(x0, w))
        cx1 = max(0, min(x1, w))

        c = self.channel_index(channel)

        with tifffile.TiffFile(self.path) as tf:
            zarr_arr = self._open_level_array(tf, level)
            data = np.asarray(zarr_arr[c, cy0:cy1, cx0:cx1])

        arr = data.astype(np.float32, copy=False)
        return arr, (cy0, cx0)
