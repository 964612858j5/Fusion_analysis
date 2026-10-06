"""`OmeTiffSource`: the source OME-TIFF behind the `PixelSource` contract
(block A2a).

A legacy adapter. Region, tile and multiscale access DELEGATE to the
viewer's `RawTileProvider` (per-thread handles); the level-0 scan delegates
to Step4's `TiffTileReader` (its tile-decode fast path and its aszarr
fallback). Neither reader is changed.
"""

import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Tuple

import numpy as np

from ..core.pixel_source import (OutOfBounds, PixelSource, PixelSourceIdentity,
                                 SourceClosed, intersect)

# OME length units -> micrometres. Anything else is "not recorded".
_UM_PER_UNIT = {"µm": 1.0, "um": 1.0, "micron": 1.0, "micrometer": 1.0,
                "nm": 1e-3, "mm": 1e3, "cm": 1e4, "m": 1e6}


def _first_pixels(xml):
    """The first OME ``Pixels`` element, or None."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == "Pixels":
            return el
    return None


def channel_names_from_ome(xml, num_channels) -> List[str]:
    """Rule (c) of the A2a application: an OME-named channel keeps its name;
    a channel without one is ``ch_NN`` (its page index) on its own. When the
    OME ``Channel`` elements do not number the pages, names and pages can no
    longer be matched, so every channel is ``ch_NN``. No `name_map`: that is
    a display alias, configuration, not the source."""
    fallback = [f"ch_{i:02d}" for i in range(num_channels)]
    pixels = _first_pixels(xml) if xml else None
    if pixels is None:
        return fallback
    elements = [el for el in pixels if el.tag.rsplit("}", 1)[-1] == "Channel"]
    if len(elements) != num_channels:
        return fallback
    return [el.attrib.get("Name") or fallback[i] for i, el in enumerate(elements)]


def physical_size_from_ome(xml) -> Optional[Tuple[float, float]]:
    """``(dy_um, dx_um)`` from ``PhysicalSizeY/X`` (OME's default unit is
    µm), or None when either is missing or its unit is not a length we
    know."""
    pixels = _first_pixels(xml) if xml else None
    if pixels is None:
        return None
    out = []
    for axis in ("Y", "X"):
        value = pixels.attrib.get(f"PhysicalSize{axis}")
        unit = pixels.attrib.get(f"PhysicalSize{axis}Unit", "µm")
        factor = _UM_PER_UNIT.get(unit)
        try:
            size = float(value)
        except (TypeError, ValueError):
            return None
        if factor is None or not size > 0:
            return None
        out.append(size * factor)
    return out[0], out[1]


class OmeTiffSource(PixelSource):
    """The raw slide: every channel, every pyramid level, native dtype."""

    def __init__(self, path, handle_mode="per_thread", scan_threads=8, provider=None):
        """`provider` (block A9-P): the viewer's own `RawTileProvider`,
        already open -- used as this source's reader instead of a second set
        of handles. The TIFF's metadata is then read only when something
        asks for it, and a channel is handed to the provider as given, to be
        resolved by its own rule, so a viewer reading through this source
        sees the channel naming it always had."""
        self._scan_threads = int(scan_threads)
        self._scan_reader = None
        self._scan_lock = threading.Lock()
        self._closed = False
        self._injected = provider is not None
        if self._injected:
            self._provider = provider
            self.path = getattr(provider, "path", path)
            self._meta_loaded = False
            return
        from ..viewer.raw_tile_provider import RawTileProvider

        self._provider = RawTileProvider(path, handle_mode=handle_mode)
        self.path = self._provider.path
        self._load_meta()

    def _load_meta(self):
        import tifffile

        with tifffile.TiffFile(self.path) as tf:
            xml = tf.ome_metadata
            self._dtype = np.dtype(tf.series[0].dtype)
            self._native = []
            for lv in tf.series[0].levels:
                page = lv.pages[0]
                page = page.aspage() if hasattr(page, "aspage") else page
                if getattr(page, "is_tiled", False):
                    self._native.append((int(page.tilelength), int(page.tilewidth)))
                else:                                   # a strip is the block
                    rows = int(getattr(page, "rowsperstrip", 0) or page.imagelength)
                    self._native.append((min(rows, int(page.imagelength)),
                                         int(page.imagewidth)))
        n = self._provider.num_channels
        self._names = channel_names_from_ome(xml, n)
        self._physical = physical_size_from_ome(xml)
        self._meta_loaded = True

    def _meta(self):
        if not self._meta_loaded:
            self._load_meta()

    # ── identity and metadata ────────────────────────────────────────
    def source_identity(self) -> PixelSourceIdentity:
        ident = self._provider.source_identity()
        return PixelSourceIdentity(dataset_path=ident.dataset_path,
                                   dataset_fingerprint=ident.dataset_fingerprint,
                                   stage="raw")

    def channel_names(self) -> List[str]:
        self._meta()
        return list(self._names)

    def _index(self, channel) -> int:
        if self._injected:
            # the reader resolves the channel by its own rule (names,
            # aliases, indices), exactly as when the viewer called it
            return channel
        if isinstance(channel, (int, np.integer)):
            index = int(channel)
            if not 0 <= index < len(self._names):
                raise KeyError(f"no channel {index}")
            return index
        try:
            return self._names.index(channel)
        except ValueError:
            raise KeyError(f"unknown channel: {channel!r}") from None

    def dtype(self, channel) -> np.dtype:
        self._index(channel)
        self._meta()
        return self._dtype

    def level_count(self) -> int:
        return self._provider.num_levels

    def level_shape(self, level: int) -> Tuple[int, int]:
        return self._provider.level_shape(level)

    def valid_bounds(self, level: int):
        h, w = self.level_shape(level)
        return 0, h, 0, w

    def native_tile_shape(self, level: int) -> Tuple[int, int]:
        """The TIFF tile of `level` (or its strip, for a stripped level)."""
        self._meta()
        return self._native[int(level)]

    def legacy_level_downsample_rounded(self, level: int) -> float:
        """Today's `RawTileProvider.level_downsample`: a CACHE-KEY
        compatibility hint only. Never for geometry, physical distances or
        scaling a scientific parameter; not part of the contract."""
        return self._provider.level_downsample(level)

    def physical_size(self) -> Optional[Tuple[float, float]]:
        self._meta()
        return self._physical

    # ── reads ────────────────────────────────────────────────────────
    def _check_open(self):
        if self._closed:
            raise SourceClosed(f"OmeTiffSource for {self.path!r} is closed")

    def read_region(self, channel, level, y0, y1, x0, x1):
        self._check_open()
        index = self._index(channel)
        cy0, cy1, cx0, cx1 = intersect((y0, y1, x0, x1), self.valid_bounds(level))
        arr, origin = self._provider.read_region(index, level, cy0, cy1, cx0, cx1)
        return arr, (int(origin[0]), int(origin[1]))

    def read_provider_tile(self, channel, tile):
        """Injected mode (block A9-P): one tile of the viewer's tile grid, by
        the reader's own `read_tile` -- for `RawTileProvider` exactly the
        clamped `read_region` of the tile's rectangle -- returning
        `(array, io_ms)` as it does."""
        self._check_open()
        if not self._injected:
            raise RuntimeError("read_provider_tile needs an injected provider")
        return self._provider.read_tile(channel, tile)

    def read_regions(self, channels, level, y0, y1, x0, x1):
        """Level 0: Step4's `TiffTileReader` (tile-parallel decode, the fast
        batch path; block A2c 2/3) -- bitwise the per-channel reads. Other
        levels: the contract's per-channel default."""
        if int(level) != 0:
            return super().read_regions(channels, level, y0, y1, x0, x1)
        self._check_open()
        indices = [self._index(ch) for ch in channels]
        if not indices:
            raise ValueError("no channels")
        cy0, cy1, cx0, cx1 = intersect((y0, y1, x0, x1), self.valid_bounds(0))
        return self._reader().read(indices, cy0, cy1, cx0, cx1), (cy0, cx0)

    def scan_reader_mode(self) -> str:
        """Which path the level-0 batch reader takes ("tiff_tiles" or
        "tifffile_zarr"), for the provenance."""
        return self._reader().mode

    def _reader(self):
        with self._scan_lock:
            if self._scan_reader is None:
                from ..core.quant_sources import TiffTileReader
                self._scan_reader = TiffTileReader(self.path, threads=self._scan_threads)
            return self._scan_reader

    def scan(self, channels, level, tile_size):
        """Level 0 only (A2a ruling 3): Step4's reader, one block read ahead
        on a private thread."""
        self._check_open()
        if int(level) != 0:
            raise NotImplementedError("OmeTiffSource.scan reads level 0 only")
        indices = [self._index(ch) for ch in channels]
        reader = self._reader()
        by0, by1, bx0, bx1 = self.valid_bounds(0)
        ts = int(tile_size)
        windows = [(y0, min(y0 + ts, by1), x0, min(x0 + ts, bx1))
                   for y0 in range(by0, by1, ts) for x0 in range(bx0, bx1, ts)]
        return self._scan_iter(reader, indices, windows)

    def _scan_iter(self, reader, indices, windows):
        pool = ThreadPoolExecutor(1, thread_name_prefix="pixel-source-scan")
        try:
            pending = pool.submit(reader.read, indices, *windows[0]) if windows else None
            for k, (y0, y1, x0, x1) in enumerate(windows):
                block = pending.result()
                pending = (pool.submit(reader.read, indices, *windows[k + 1])
                           if k + 1 < len(windows) else None)
                self._check_open()
                yield y0, x0, block
        finally:
            pool.shutdown(wait=True)

    # ── lifecycle ────────────────────────────────────────────────────
    def close(self):
        if self._closed:
            return
        self._closed = True
        with self._scan_lock:
            reader, self._scan_reader = self._scan_reader, None
        if reader is not None:
            reader.close()
        self._provider.close()


__all__ = ["OmeTiffSource", "OutOfBounds", "channel_names_from_ome",
           "physical_size_from_ome"]
