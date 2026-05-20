"""Canonical Step2 tile geometry container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


BBox = Tuple[int, int, int, int]


@dataclass(frozen=True)
class Step2Tile:
    index: int
    row: int
    col: int
    own_bbox: BBox
    read_bbox: BBox
    overlap: int
    out_prefix: str = ""

    @property
    def tile_id(self) -> str:
        return f"{self.out_prefix}:{self.index}" if self.out_prefix else str(self.index)

    @property
    def own_h(self) -> int:
        return self.own_bbox[1] - self.own_bbox[0]

    @property
    def own_w(self) -> int:
        return self.own_bbox[3] - self.own_bbox[2]

    @property
    def read_h(self) -> int:
        return self.read_bbox[1] - self.read_bbox[0]

    @property
    def read_w(self) -> int:
        return self.read_bbox[3] - self.read_bbox[2]

    @property
    def read_shape(self) -> tuple:
        return (self.read_h, self.read_w)

    @property
    def local_own_bbox(self) -> BBox:
        oy0, oy1, ox0, ox1 = self.own_bbox
        ry0, _ry1, rx0, _rx1 = self.read_bbox
        return (oy0 - ry0, oy1 - ry0, ox0 - rx0, ox1 - rx0)

    def as_legacy_dict(self) -> dict:
        return {
            "row": self.row,
            "col": self.col,
            "own": self.own_bbox,
            "read": self.read_bbox,
        }
