"""`CorrectedZarrSource`: Step0's committed corrected product behind the
`PixelSource` contract (block A2a).

One region's corrected channels (float32), found by Step4's STRICT rule
(`core.quant_sources._corrected_group`: exactly one group for the ROI, never
another ROI's group that happens to hold the channel). Coordinates are the
slide's level-0 pixels, so it is coordinate-compatible with `OmeTiffSource`
-- not interchangeable with it: one ROI, some channels, one level, float32.

`valid_bounds(0)` is the ROI's bbox; a read returns the request's
intersection with it, and an empty intersection raises `OutOfBounds`. A
pixel outside the ROI is never taken from the raw slide (the silent raw
fall-through of `OMETIFFLoader._read_corrected_roi_only` cannot happen
through this contract).
"""

import os
from typing import List, Optional, Tuple

import numpy as np

from ..core.pixel_source import (PixelSource, PixelSourceIdentity, SourceClosed,
                                 intersect)


class CorrectedZarrSource(PixelSource):
    """The corrected channels of one ROI of `corrected_channels.zarr`."""

    def __init__(self, zarr_path, roi_name, slide_shape: Optional[Tuple[int, int]] = None):
        import zarr

        from ..core.quant_sources import (QuantSourceError, _corrected_group,
                                          region_folder)

        self.path = os.path.realpath(zarr_path)
        self.roi_name = str(roi_name)
        root = zarr.open_group(self.path, mode="r")
        self._root = root
        if str(root.attrs.get("mode", "")).strip().lower() == "roi_only":
            hits = [gname for gname in root.group_keys()
                    if root[gname].attrs.get("roi_name") == self.roi_name
                    or gname == region_folder(self.roi_name)]
            if len(hits) != 1:
                raise QuantSourceError(f"the corrected product {self.path} has "
                                       f"{'no' if not hits else len(hits)} group(s) for "
                                       f"region '{self.roi_name}'")
            try:
                bbox = tuple(int(v) for v in root[hits[0]].attrs.get("bbox_fullres"))
            except (TypeError, ValueError):
                raise QuantSourceError(f"the corrected group '{hits[0]}' records no bbox")
            # The strict rule itself validates the group and its bbox.
            container, offset, _gshape = _corrected_group(root, self.roi_name, bbox, self.path)
            if tuple(offset) != (0, 0):
                raise QuantSourceError("the corrected group does not start at its own bbox")
            self._group = hits[0]
            self._bounds = bbox
        else:
            container = root
            shapes = {tuple(container[k].shape) for k in container.array_keys()}
            if len(shapes) != 1:
                raise QuantSourceError(f"the corrected product {self.path} holds arrays of "
                                       f"shapes {sorted(shapes)}")
            h, w = shapes.pop()
            self._group = ""
            self._bounds = (0, int(h), 0, int(w))
        self._container = container
        self._names = list(container.array_keys())
        for name in self._names:
            if str(container[name].dtype) != "float32":
                raise QuantSourceError(f"the corrected array {name} is "
                                       f"{container[name].dtype}, not float32")
        self._slide_shape = tuple(slide_shape) if slide_shape is not None else \
            self._shape_of_source(root.attrs.get("source_ome"))
        self._closed = False

    def _shape_of_source(self, source_ome) -> Tuple[int, int]:
        """The slide's level-0 shape, from the slide the product records.
        Unknown is an error, never a guess."""
        if not source_ome or not os.path.exists(source_ome):
            raise ValueError(f"the corrected product {self.path} records no readable "
                             f"source slide ({source_ome!r}); pass slide_shape")
        import tifffile
        with tifffile.TiffFile(source_ome) as tf:
            h, w = tf.series[0].levels[0].shape[-2:]
        return int(h), int(w)

    # ── identity and metadata ────────────────────────────────────────
    def source_identity(self) -> PixelSourceIdentity:
        attrs = os.path.join(self.path, ".zattrs")
        st = os.stat(attrs if os.path.exists(attrs) else self.path)
        token = None
        if self._names:
            token = self._container[self._names[0]].attrs.get("source_identity")
        return PixelSourceIdentity(dataset_path=self.path,
                                   dataset_fingerprint=f"{st.st_size}:{st.st_mtime_ns}",
                                   stage="corrected_saved",
                                   product=f"{self._group}:{token}")

    def channel_names(self) -> List[str]:
        return list(self._names)

    def _array(self, channel):
        if channel not in self._names:
            raise KeyError(f"no corrected channel {channel!r} in region '{self.roi_name}'")
        return self._container[channel]

    def dtype(self, channel) -> np.dtype:
        return np.dtype(self._array(channel).dtype)

    def level_count(self) -> int:
        return 1

    def level_shape(self, level: int) -> Tuple[int, int]:
        if int(level) != 0:
            raise IndexError(f"a corrected product has level 0 only, not {level}")
        return self._slide_shape

    def valid_bounds(self, level: int):
        self.level_shape(level)
        return self._bounds

    # ── reads ────────────────────────────────────────────────────────
    def read_region(self, channel, level, y0, y1, x0, x1):
        if self._closed:
            raise SourceClosed(f"CorrectedZarrSource for {self.path!r} is closed")
        arr = self._array(channel)
        cy0, cy1, cx0, cx1 = intersect((y0, y1, x0, x1), self.valid_bounds(level))
        gy0, _gy1, gx0, _gx1 = self._bounds
        data = np.asarray(arr[cy0 - gy0:cy1 - gy0, cx0 - gx0:cx1 - gx0])
        return data, (cy0, cx0)

    # ── lifecycle ────────────────────────────────────────────────────
    def close(self):
        self._closed = True


__all__ = ["CorrectedZarrSource"]
