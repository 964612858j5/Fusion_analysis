"""Lightweight Step2 tile scheduler shell."""

from __future__ import annotations

from .merge_policy import CentroidOwnershipMergePolicy
from .step2_tile import Step2Tile, compute_tile_grid_metrics


class TileScheduler:
    """
    Lightweight Step2 tile scheduler shell.

    Step 4A scope:
    - own tiles
    - expose iter_tiles()
    - expose metrics()
    - expose crop_valid_region()
    - no prefetch orchestration
    - no payload loading
    - no merge policy orchestration
    """

    def __init__(self, full_h, full_w, n_rows, n_cols, overlap_px, out_prefix="", merge_policy=None):
        self.full_h = int(full_h)
        self.full_w = int(full_w)
        self.n_rows = int(n_rows)
        self.n_cols = int(n_cols)
        self.overlap_px = int(overlap_px)
        self.out_prefix = str(out_prefix or "")
        self.merge_policy = merge_policy or CentroidOwnershipMergePolicy()
        self.tile_h, self.tile_w, self.tiles = self._build_tiles()

    def _build_tiles(self):
        tile_h = -(-self.full_h // self.n_rows)
        tile_w = -(-self.full_w // self.n_cols)
        tiles = []
        for r in range(self.n_rows):
            for c in range(self.n_cols):
                oy0 = r * tile_h
                oy1 = min(oy0 + tile_h, self.full_h)
                ox0 = c * tile_w
                ox1 = min(ox0 + tile_w, self.full_w)
                ry0 = max(0, oy0 - self.overlap_px)
                ry1 = min(self.full_h, oy1 + self.overlap_px)
                rx0 = max(0, ox0 - self.overlap_px)
                rx1 = min(self.full_w, ox1 + self.overlap_px)
                tiles.append(Step2Tile(
                    index=len(tiles),
                    row=r,
                    col=c,
                    own_bbox=(oy0, oy1, ox0, ox1),
                    read_bbox=(ry0, ry1, rx0, rx1),
                    overlap=self.overlap_px,
                    out_prefix=self.out_prefix,
                ))
        return tile_h, tile_w, tiles

    def iter_tiles(self):
        return iter(self.tiles)

    def __len__(self):
        return len(self.tiles)

    def metrics(self):
        return compute_tile_grid_metrics(self.tiles, self.full_h, self.full_w)

    def crop_valid_region(self, arr, tile, copy=True):
        return self.merge_policy.crop_valid_region(arr, tile, copy=copy)

    def as_legacy_tiles(self):
        return [tile.as_legacy_dict() for tile in self.tiles]
