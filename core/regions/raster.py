"""Regions shapes as pixels for the analysis (docs/f4_regions_downstream_plan.md
v2 §B.6): every selected core and every exclusion is rasterised ONCE over
its own bounding box (`geometry.fill`, the rasterisation `RoiMask` uses),
and a window only takes its slice -- so Step1, Step2 and Step4 decide the
same boundary pixels, and no full-slide mask is ever built.

Windows are ``(y0, y1, x0, x1)`` in level-0 pixels, half-open.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import geometry as G

Window = Tuple[int, int, int, int]


class ShapeRaster:
    """One polygon's pixels over its own box, bit-packed."""

    def __init__(self, polygon: Optional[Sequence[Sequence[int]]], shape: Tuple[int, int], bbox=None):
        """`bbox`: the box to rasterise over instead of the polygon's own (a
        Step0 ROI's saved half-open box, as `RoiMask` uses it); with no
        polygon the box itself is the shape (a rectangle, never rasterised)."""
        h, w = shape
        if bbox is None:
            bbox = G.bbox_of(polygon, shape)
        y0, y1, x0, x1 = (int(v) for v in bbox)
        self.bbox = [max(y0, 0), min(y1, h), max(x0, 0), min(x1, w)]
        y0, y1, x0, x1 = self.bbox
        self._w = x1 - x0
        self._rect = polygon is None
        if y1 <= y0 or x1 <= x0:
            self._bits = None
        elif self._rect:
            self._bits = True
        else:
            self._bits = np.packbits(G.fill(polygon, self.bbox), axis=1)

    @property
    def empty(self) -> bool:
        return self._bits is None

    def overlaps(self, win: Window) -> bool:
        y0, y1, x0, x1 = self.bbox
        return self._bits is not None and win[0] < y1 and y0 < win[1] and win[2] < x1 and x0 < win[3]

    def part(self, win: Window):
        """(window-relative slices, bool block) of the polygon's pixels where
        its box meets the window -- only those bytes are unpacked; None
        when they do not meet."""
        if not self.overlaps(win):
            return None
        wy0, wy1, wx0, wx1 = (int(v) for v in win)
        y0, y1, x0, x1 = self.bbox
        iy0, iy1, ix0, ix1 = max(wy0, y0), min(wy1, y1), max(wx0, x0), min(wx1, x1)
        if self._rect:
            return (slice(iy0 - wy0, iy1 - wy0), slice(ix0 - wx0, ix1 - wx0)), np.ones((iy1 - iy0, ix1 - ix0), bool)
        b0, b1 = (ix0 - x0) // 8, (ix1 - x0 + 7) // 8
        bits = np.unpackbits(self._bits[iy0 - y0:iy1 - y0, b0:b1], axis=1).astype(bool)
        off = ix0 - x0 - 8 * b0
        return (slice(iy0 - wy0, iy1 - wy0), slice(ix0 - wx0, ix1 - wx0)), bits[:, off:off + (ix1 - ix0)]

    def window(self, win: Window) -> np.ndarray:
        """bool array of the window's shape: True inside the polygon."""
        out = np.zeros((int(win[1]) - int(win[0]), int(win[3]) - int(win[2])), dtype=bool)
        p = self.part(win)
        if p is not None:
            out[p[0]] = p[1]
        return out


class AnalysisRaster:
    """The pixels of a frozen analysis snapshot (`analysis.build_snapshot`):

    * `core_index(win)` -- uint16, 0 outside every selected core, i + 1 inside
      core i of ``snapshot["cores"]`` (selected cores never overlap: the
      snapshot refuses overlapping ones);
    * `seg_invalid(win)` -- inside an exclusion that invalidates segmentation;
    * `measurement_exclusions(win)` -- (exclusion index, channels, mask) of the
      measurement-only exclusions touching the window;
    * `has_core(win)` -- whether the window holds any selected-core pixel
      (the tile-skip test).

    Every query is limited to the snapshot's parent region (the
    workspace's ROI or the whole slide). A snapshot without a map sheet (an
    ROI project) has no cores: the parent region itself is the domain
    (index 1 inside it, 0 outside).
    """

    def __init__(self, snapshot: Dict):
        shape = tuple(snapshot["slide_shape"])
        self.tma = snapshot["mode"] == "tma"
        par = snapshot.get("parent") or {}
        self.parent = ShapeRaster(par.get("polygon_fullres") or None, shape,
                                  bbox=par.get("bbox_fullres") or [0, shape[0], 0, shape[1]])
        self.cores = [ShapeRaster(c["polygon"], shape) for c in snapshot["cores"]]
        self.seg = [ShapeRaster(e["polygon"], shape) for e in snapshot["exclusions"] if e["seg_invalid"]]
        self.meas = [(i, list(e["channels"]), ShapeRaster(e["polygon"], shape))
                     for i, e in enumerate(snapshot["exclusions"]) if not e["seg_invalid"]]

    def has_core(self, win: Window) -> bool:
        """Whether the window holds any pixel of the domain (the tile-skip test)."""
        return bool(self.core_index(win).any())

    def core_index(self, win: Window) -> np.ndarray:
        h, w = int(win[1]) - int(win[0]), int(win[3]) - int(win[2])
        out = np.zeros((h, w), dtype=np.uint16)
        par = self.parent.part(win)
        if par is None:
            return out
        if not self.tma:
            out[par[0]] = par[1]
            return out
        for i, r in enumerate(self.cores):
            p = r.part(win)
            if p is not None:
                out[p[0]][p[1]] = i + 1
        inside = np.zeros((h, w), dtype=bool)
        inside[par[0]] = par[1]
        out[~inside] = 0
        return out

    def seg_invalid(self, win: Window) -> np.ndarray:
        out = np.zeros((int(win[1]) - int(win[0]), int(win[3]) - int(win[2])), dtype=bool)
        for r in self.seg:
            p = r.part(win)
            if p is not None:
                out[p[0]] |= p[1]
        return out

    def measurement_exclusions(self, win: Window) -> List[Tuple[int, List[str], np.ndarray]]:
        return [(i, chans, r.window(win)) for i, chans, r in self.meas if r.overlaps(win)]

    def core_meets_parent(self, i: int) -> bool:
        """Whether core i has a pixel inside the parent region."""
        r = self.cores[i]
        if r.empty:
            return False
        win = tuple(r.bbox)
        p = self.parent.part(win)
        return p is not None and bool((r.window(win)[p[0]] & p[1]).any())

    def core_windows(self) -> List[Optional[List[int]]]:
        """Each selected core's box (None for a degenerate one)."""
        return [None if r.empty else r.bbox for r in self.cores]
