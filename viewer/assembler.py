"""Raw-tile-assembly path for arbitrary (possibly halo-padded) pixel windows.

`CorrectionCompute` needs a halo-padded rectangle that in general spans more
than one canonical raw tile. Rather than issuing an ad-hoc
`provider.read_region` call (which bypasses the RawTileCache and duplicates
I/O across overlapping halos of neighboring tiles), `RawTileAssembler`
decomposes the request into the SAME canonical grid (`TileGridSpec`) the
scheduler uses for raw tiles, fetches each covering tile through the shared
`RawTileCache` (cache hit, or `provider.read_tile` + `cache.put` on miss),
and stitches the requested window back together.

Tiles are cached and returned in the source's NATIVE dtype (see
`RawTileProvider.read_tile`); the stitched output is therefore also native
dtype. Callers cast to float32 at the compute boundary, not here.
"""

import time

import numpy as np

from .tile_types import RawKey, TileAddress


class RawTileAssembler:
    """Assembles an arbitrary pixel window from canonical raw tiles."""

    def __init__(self, provider, raw_cache):
        self.provider = provider
        self.raw_cache = raw_cache

    def assemble(self, source, grid, channel, level, y0, y1, x0, x1):
        """Fetch/stitch the CLAMPED window [y0:y1, x0:x1] at `level`.

        Returns (array (native dtype), (actual_y0, actual_x0), stats) where
        stats = {"tiles_total": n, "tiles_hit": h, "io_ms": total_io_of_misses}.
        """
        h, w = self.provider.level_shape(level)
        cy0, cy1 = max(0, min(y0, h)), max(0, min(y1, h))
        cx0, cx1 = max(0, min(x0, w)), max(0, min(x1, w))

        stats = {"tiles_total": 0, "tiles_hit": 0, "io_ms": 0.0}
        if cy1 <= cy0 or cx1 <= cx0:
            return np.zeros((0, 0), dtype=np.float32), (cy0, cx0), stats

        ts = grid.tile_size
        tx0, tx1 = cx0 // ts, (cx1 - 1) // ts
        ty0, ty1 = cy0 // ts, (cy1 - 1) // ts

        out = None
        for ty in range(ty0, ty1 + 1):
            for tx in range(tx0, tx1 + 1):
                stats["tiles_total"] += 1
                addr = TileAddress(grid=grid, level=level, tx=tx, ty=ty)
                key = RawKey(source=source, channel=channel, tile=addr)
                arr = self.raw_cache.get(key)
                if arr is not None:
                    stats["tiles_hit"] += 1
                else:
                    t0 = time.perf_counter()
                    arr, _io_ms = self.provider.read_tile(channel, addr)
                    stats["io_ms"] += (time.perf_counter() - t0) * 1000.0
                    self.raw_cache.put(key, arr)

                if arr.size == 0:
                    continue
                if out is None:
                    out = np.zeros((cy1 - cy0, cx1 - cx0), dtype=arr.dtype)

                ty0_abs, tx0_abs = ty * ts, tx * ts
                ty1_abs, tx1_abs = ty0_abs + arr.shape[0], tx0_abs + arr.shape[1]

                oy0, oy1 = max(ty0_abs, cy0), min(ty1_abs, cy1)
                ox0, ox1 = max(tx0_abs, cx0), min(tx1_abs, cx1)
                if oy1 <= oy0 or ox1 <= ox0:
                    continue

                sy0, sy1 = oy0 - ty0_abs, oy1 - ty0_abs
                sx0, sx1 = ox0 - tx0_abs, ox1 - tx0_abs
                dy0, dy1 = oy0 - cy0, oy1 - cy0
                dx0, dx1 = ox0 - cx0, ox1 - cx0
                out[dy0:dy1, dx0:dx1] = arr[sy0:sy1, sx0:sx1]

        if out is None:
            out = np.zeros((cy1 - cy0, cx1 - cx0), dtype=np.float32)
        return out, (cy0, cx0), stats
