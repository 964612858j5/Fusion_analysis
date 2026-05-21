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
        self.prefetcher = None
        self.prefetch_enabled = False
        self._load_fn = None
        self._load_logger = None
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

    def attach_payload_loader(self, load_fn, logger=None):
        """
        Register payload loading function.

        load_fn signature:
            load_fn(index, tile, profile_tile_base=None) -> payload

        TileScheduler does not know payload structure.
        It only orchestrates loading.
        """
        self._load_fn = load_fn
        self._load_logger = logger
        return load_fn

    def _call_payload_loader(self, index, tile, profile_tile_base=None):
        if self._load_fn is None:
            raise RuntimeError("TileScheduler.load_tile requires attach_payload_loader() before use")
        return self._load_fn(index, tile, profile_tile_base=profile_tile_base)

    def attach_prefetcher(self, load_fn=None, enabled=True, queue_size=2, logger=None, profiler=None):
        """
        Attach a TilePrefetcher to this scheduler.

        load_fn signature:
            load_fn(index, tile) -> payload

        This method only wires TilePrefetcher to scheduler.tiles.
        It does not define payload loading itself.
        """
        if not enabled:
            self.prefetcher = None
            self.prefetch_enabled = False
            return None

        if load_fn is None:
            load_fn = lambda idx, tile: self._call_payload_loader(idx, tile, profile_tile_base=None)

        from .tile_prefetch import TilePrefetcher
        self.prefetcher = TilePrefetcher(
            self.tiles,
            load_fn,
            prefetch_queue_size=queue_size,
            logger=logger,
            profiler=profiler,
        )
        self.prefetch_enabled = self.prefetcher is not None
        return self.prefetcher

    def get_payload(self, index, sync_load_fn=None):
        """
        Return payload for tile index using prefetcher if attached.
        If no prefetcher is attached, call sync_load_fn(index, tile).
        """
        tile = self.tiles[index]
        if self.prefetcher is not None:
            return self.prefetcher.get(index, sync_load_fn=sync_load_fn)
        if sync_load_fn is None:
            raise RuntimeError("TileScheduler.get_payload requires sync_load_fn when no prefetcher is attached")
        return sync_load_fn(index, tile)

    def load_tile(self, index, profile_tile_base=None, sync_load_fn=None):
        """
        Unified tile payload loading entry.

        Behavior:
        - if prefetcher attached:
            use scheduler.get_payload()
        - else:
            call load_fn directly
        """
        tile = self.tiles[index]
        effective_load_fn = sync_load_fn or (
            lambda idx, t: self._call_payload_loader(
                idx,
                t,
                profile_tile_base=profile_tile_base,
            )
        )
        path = "prefetch" if self.prefetcher is not None else "sync"
        if self._load_logger:
            try:
                self._load_logger.debug("[TileScheduler] load_tile index=%s path=%s", index, path)
            except Exception:
                pass
        if self.prefetcher is not None:
            return self.get_payload(index, sync_load_fn=effective_load_fn)
        return effective_load_fn(index, tile)

    def prefetch_metrics(self):
        if self.prefetcher is None:
            return {}
        return self.prefetcher.snapshot_metrics()

    def close(self):
        if self.prefetcher is not None:
            self.prefetcher.close()
            self.prefetcher = None
            self.prefetch_enabled = False
