"""The viewers' slide reader, through the `PixelSource` contract (block A9-P).

Plan v2.4 §20.3: every pixel a viewer shows comes through a `PixelSource`.
The viewers were written against `RawTileProvider`; `SourceTileProvider`
keeps that interface for them and sends every pixel read through an
`OmeTiffSource` built around the viewer's own (already open) provider, so
the pixels are bitwise the ones the provider returns -- the old path is the
oracle (tests/test_v16_a9p_source_parity.py) -- while an optimisation of the
source (A9 toolbox item 1) now reaches every viewer.

Metadata (level shapes, downsamples, channel names, the source identity)
and handle warming are the source's own business and are answered by the
reader inside it; the viewer no longer opens the TIFF itself.
"""

import numpy as np

from . import raw_tile_provider


class SourceTileProvider:
    """`RawTileProvider`'s interface, pixels through `source.read_region`."""

    def __init__(self, source):
        self.source = source
        self._raw = source._provider

    # ── metadata: the reader inside the source ─────────────────────────
    @property
    def path(self):
        return self._raw.path

    @property
    def handle_mode(self):
        return getattr(self._raw, "handle_mode", "")

    @property
    def num_levels(self):
        return self._raw.num_levels

    @property
    def num_channels(self):
        return self._raw.num_channels

    @property
    def channel_names(self):
        return self._raw.channel_names

    @property
    def open_count(self):
        return getattr(self._raw, "open_count", 0)

    def __getattr__(self, name):
        # Anything else a viewer asks of its provider (`describe`,
        # `source_identity`, `level_downsample_yx`, ...) is the reader's
        # answer, unchanged. Pixels never come this way: `read_tile` and
        # `read_region` are defined below.
        if name.startswith("__") or name in ("source", "_raw"):
            raise AttributeError(name)
        return getattr(self._raw, name)

    def channel_index(self, channel):
        return self._raw.channel_index(channel)

    def level_shape(self, level):
        return self._raw.level_shape(level)

    def level_downsample(self, level):
        return self._raw.level_downsample(level)

    def source_identity(self):
        return self._raw.source_identity()

    def warm_thread_handle(self, levels=(0,)):
        return self._raw.warm_thread_handle(levels)

    def close(self):
        self.source.close()

    # ── pixels: through the source ──────────────────────────────────────
    def read_tile(self, channel, tile):
        """One tile, through the source to the reader's own `read_tile` (for
        `RawTileProvider`: the clamped region of the tile, timed)."""
        if getattr(self.source, "_closed", False):
            raise RuntimeError(
                f"RawTileProvider for {self.path!r} is closed; "
                "no further reads are allowed")
        return self.source.read_provider_tile(channel, tile)

    def read_region(self, channel, level, y0, y1, x0, x1):
        """Clamped exactly as `RawTileProvider.read_region` clamps; an empty
        clamp reads nothing (the contract refuses an empty request) and
        answers the empty array the provider would have sliced."""
        if getattr(self.source, "_closed", False):
            raise RuntimeError(
                f"RawTileProvider for {self.path!r} is closed; "
                "no further reads are allowed")
        h, w = self.level_shape(level)
        cy0 = max(0, min(y0, h))
        cy1 = max(0, min(y1, h))
        cx0 = max(0, min(x0, w))
        cx1 = max(0, min(x1, w))
        if cy1 <= cy0 or cx1 <= cx0:
            dtype = getattr(self._raw, "_dtype", None)
            if dtype is None:          # a stand-in without one: its own answer
                return self._raw.read_region(channel, level, cy0, cy1, cx0, cx1)
            if hasattr(self._raw, "channel_index"):
                self._raw.channel_index(channel)      # same refusal of a bad name
            return (np.empty((max(0, cy1 - cy0), max(0, cx1 - cx0)), np.dtype(dtype)),
                    (cy0, cx0))
        arr, origin = self.source.read_region(channel, level, cy0, cy1, cx0, cx1)
        return arr, (int(origin[0]), int(origin[1]))


def open_viewer_source(path):
    """The provider a viewer opens for the slide at `path`: its own
    `RawTileProvider` (looked up on the module at call time, so a test's
    stand-in is still the one used) inside an `OmeTiffSource`."""
    from ..sources.ome_tiff import OmeTiffSource
    raw = raw_tile_provider.RawTileProvider(path)
    return SourceTileProvider(OmeTiffSource(path, provider=raw))


__all__ = ["SourceTileProvider", "open_viewer_source"]
