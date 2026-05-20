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


def local_own_bbox(tile) -> BBox:
    """Return own_bbox coordinates local to tile.read_bbox."""
    if hasattr(tile, "local_own_bbox"):
        return tile.local_own_bbox
    oy0, oy1, ox0, ox1 = tile["own"]
    ry0, _ry1, rx0, _rx1 = tile["read"]
    return (oy0 - ry0, oy1 - ry0, ox0 - rx0, ox1 - rx0)


def crop_valid_region(arr, tile, copy=True):
    """
    Crop the tile's own/valid region from an array defined on tile.read_bbox.
    Supports Step2Tile and legacy dict tile.
    """
    y0, y1, x0, x1 = local_own_bbox(tile)
    cropped = arr[y0:y1, x0:x1]
    return cropped.copy() if copy else cropped


def _tile_own_bbox(tile) -> BBox:
    return tile.own_bbox if hasattr(tile, "own_bbox") else tile["own"]


def _tile_read_bbox(tile) -> BBox:
    return tile.read_bbox if hasattr(tile, "read_bbox") else tile["read"]


def _tile_row(tile) -> int:
    return int(tile.row if hasattr(tile, "row") else tile.get("row", 0))


def _tile_col(tile) -> int:
    return int(tile.col if hasattr(tile, "col") else tile.get("col", 0))


def _tile_overlap(tile) -> int:
    if hasattr(tile, "overlap"):
        return int(tile.overlap)
    if "overlap" in tile:
        return int(tile["overlap"])
    oy0, oy1, ox0, ox1 = _tile_own_bbox(tile)
    ry0, ry1, rx0, rx1 = _tile_read_bbox(tile)
    return int(max(oy0 - ry0, ry1 - oy1, ox0 - rx0, rx1 - ox1, 0))


def compute_tile_grid_metrics(tiles, full_h, full_w):
    """
    Compute tile/grid metrics for Step2.
    tiles: list of Step2Tile or legacy tile dict
    full_h, full_w: full image/ROI shape
    Returns a JSON-serializable dict.
    """
    tiles = list(tiles or [])
    full_h = int(full_h)
    full_w = int(full_w)
    full_pixels = int(full_h * full_w)
    n_tiles = len(tiles)

    own_pixels = []
    read_pixels = []
    read_heights = []
    read_widths = []
    for tile in tiles:
        oy0, oy1, ox0, ox1 = _tile_own_bbox(tile)
        ry0, ry1, rx0, rx1 = _tile_read_bbox(tile)
        own_pixels.append(int(max(0, oy1 - oy0) * max(0, ox1 - ox0)))
        read_h = int(max(0, ry1 - ry0))
        read_w = int(max(0, rx1 - rx0))
        read_pixels.append(int(read_h * read_w))
        read_heights.append(read_h)
        read_widths.append(read_w)

    own_pixels_total = int(sum(own_pixels))
    read_pixels_total = int(sum(read_pixels))
    duplicate_pixels = int(read_pixels_total - own_pixels_total)
    rows = [_tile_row(tile) for tile in tiles]
    cols = [_tile_col(tile) for tile in tiles]
    overlaps = [_tile_overlap(tile) for tile in tiles]

    def _mpx(pixels):
        return round(float(pixels) / 1e6, 6)

    def _mean(values):
        return float(sum(values) / len(values)) if values else 0.0

    return {
        "full_h": full_h,
        "full_w": full_w,
        "full_pixels": full_pixels,
        "full_mpx": _mpx(full_pixels),
        "n_tiles": int(n_tiles),
        "n_rows": int(max(rows) + 1) if rows else 0,
        "n_cols": int(max(cols) + 1) if cols else 0,
        "overlap": int(max(overlaps)) if overlaps else 0,
        "own_pixels_total": own_pixels_total,
        "read_pixels_total": read_pixels_total,
        "own_mpx_total": _mpx(own_pixels_total),
        "read_mpx_total": _mpx(read_pixels_total),
        "duplicate_pixels": duplicate_pixels,
        "duplicate_mpx": _mpx(duplicate_pixels),
        "duplicate_factor": round(float(read_pixels_total) / full_pixels, 6) if full_pixels else 0.0,
        "overlap_overhead_factor": round(float(read_pixels_total) / own_pixels_total, 6) if own_pixels_total else 0.0,
        "mean_own_mpx": _mpx(_mean(own_pixels)),
        "mean_read_mpx": _mpx(_mean(read_pixels)),
        "max_read_mpx": _mpx(max(read_pixels) if read_pixels else 0),
        "min_read_mpx": _mpx(min(read_pixels) if read_pixels else 0),
        "mean_read_h": round(_mean(read_heights), 3),
        "mean_read_w": round(_mean(read_widths), 3),
        "max_read_h": int(max(read_heights) if read_heights else 0),
        "max_read_w": int(max(read_widths) if read_widths else 0),
    }
