"""A derived `PixelSource` over one already-opened array (block A9-P).

Step1 / Step3 show a corrected channel from the array `Step1SourceTable`
opened for it -- the one whose attrs carry the product's identity and whose
sidecar holds its persisted coarse plane -- and the coarse plane from its
own array. Plan v2.4 §20.3 wants those pixels behind the contract too
("CorrectedZarrSource / derived sources otherwise"). Swapping in
`CorrectedZarrSource` would change which group a channel is taken from
(Step4's strict rule) and lose the plane, so instead this wraps the very
array the table chose: the pixels, the selection and the identity are
exactly what they were.

The array sits in its own frame at `origin` (level-0 pixels for a corrected
region, block coordinates for a coarse plane), as one channel and one level.
"""

import numpy as np

from ..core.pixel_source import PixelSource, PixelSourceIdentity, intersect
from . import read_ledger


class ArrayRegionSource(PixelSource):
    """One channel's array at `origin`, read through `read_region`."""

    def __init__(self, array, origin=(0, 0), channel="", stage="corrected_saved"):
        self.array = array
        self.origin = (int(origin[0]), int(origin[1]))
        self.channel = str(channel)
        self.stage = str(stage)

    def source_identity(self) -> PixelSourceIdentity:
        store = getattr(self.array, "store", None)
        path = str(getattr(store, "path", "") or "")
        return PixelSourceIdentity(dataset_path=path, dataset_fingerprint="",
                                   stage=self.stage)

    def channel_names(self):
        return [self.channel]

    def dtype(self, channel):
        return np.dtype(self.array.dtype)

    def level_count(self):
        return 1

    def level_shape(self, level):
        if int(level) != 0:
            raise ValueError("one level")
        return tuple(int(v) for v in self.array.shape[:2])

    def valid_bounds(self, level):
        h, w = self.level_shape(level)
        oy, ox = self.origin
        return oy, oy + h, ox, ox + w

    def native_tile_shape(self, level):
        chunks = getattr(self.array, "chunks", None)
        if chunks:
            return int(chunks[0]), int(chunks[1])
        return self.level_shape(level)

    def native_tile_origin(self, level):
        return self.origin

    @read_ledger.in_flight_reads
    def read_region(self, channel, level, y0, y1, x0, x1):
        """The request's part inside the array, in native dtype, and where it
        sits; `OutOfBounds` when there is none (the contract's rule)."""
        cy0, cy1, cx0, cx1 = intersect((y0, y1, x0, x1), self.valid_bounds(level))
        oy, ox = self.origin
        arr = np.asarray(self.array[cy0 - oy:cy1 - oy, cx0 - ox:cx1 - ox])
        read_ledger.note(self.stage, level, arr.size, channel=self.channel or None)
        return arr, (cy0, cx0)

    def close(self):
        pass


__all__ = ["ArrayRegionSource"]
