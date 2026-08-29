"""Halo-padded background-correction compute step for a single tile.

Reuses the production kernels in `block01.core.bg_correction`
(`_apply_tophat_gpu_or_cpu`, `_apply_cucim_or_cpu`) so the interactive path
and the production path share numerics. The halo-padded region is fetched
via `RawTileAssembler` (canonical-grid tiles through the shared RawTileCache,
NOT an ad-hoc provider.read_region call), clamped at image edges, then
cropped back to the tile's core region after the kernel runs.

Halo sizing is method-specific:
  - tophat: halo = 2 * radius (structuring element reach both directions).
  - cucim/gaussian: halo = ceil(4 * sigma), matching the `truncate=4` default
    shared by cupyx.scipy.ndimage.gaussian_filter / scipy / skimage — this is
    the numeric contract enforced by the golden-seam test in
    tests/test_viewer_prototype.py.
"""

import math
import time

import numpy as np

from ..core import bg_correction
from .assembler import RawTileAssembler
from .tile_types import CorrectionKey, PixelBuffer, TileRequest, TileResult


def halo_for(method: str, param: int) -> int:
    """Method-correct halo width in pixels for a given method param."""
    if method == "tophat":
        return 2 * int(param)
    if method == "cucim":
        return int(math.ceil(4 * int(param)))
    raise ValueError(f"unknown method {method!r}")


class CorrectionCompute:
    """Computes a corrected tile (tophat/cucim) for a given CorrectionKey."""

    def __init__(self, provider, raw_cache):
        self.provider = provider
        self.raw_cache = raw_cache
        self.assembler = RawTileAssembler(provider, raw_cache)

    def compute(self, key: CorrectionKey) -> TileResult:
        """Run the correction for `key.method` and return a TileResult.

        Not used for raw tiles ("original"/RawKey) — the scheduler/raw cache
        serve those directly via the provider.
        """
        tile = key.tile
        ts = tile.grid.tile_size
        y0 = tile.ty * ts
        x0 = tile.tx * ts
        y1 = y0 + ts
        x1 = x0 + ts

        param = int(key.params[0])
        halo = halo_for(key.method, param)

        py0, py1 = y0 - halo, y1 + halo
        px0, px1 = x0 - halo, x1 + halo

        padded_native, (ry0, rx0), assemble_stats = self.assembler.assemble(
            key.source, tile.grid, key.channel, tile.level, py0, py1, px0, px1
        )
        # Report the actual raw-fetch time (misses only) so it lines up with
        # the cache-hit/miss accounting in `assemble_stats`.
        io_ms = assemble_stats.get("io_ms", 0.0)

        padded = padded_native.astype(np.float32, copy=False)

        # Core region within `padded`, expressed via the actual clamped offset.
        core_y0 = y0 - ry0
        core_x0 = x0 - rx0
        core_y1 = core_y0 + (y1 - y0)
        core_x1 = core_x0 + (x1 - x0)
        # Clamp the crop window to the padded array in case the tile itself
        # ran off the image edge (core smaller than tile_size there).
        h, w = padded.shape
        core_y1 = min(core_y1, h)
        core_x1 = min(core_x1, w)

        t1 = time.perf_counter()
        if key.method == "tophat":
            corrected = bg_correction._apply_tophat_gpu_or_cpu(padded, param)
        elif key.method == "cucim":
            corrected = bg_correction._apply_cucim_or_cpu(padded, param, prefer_gpu=True)
        else:
            raise ValueError(f"CorrectionCompute does not handle method {key.method!r}")
        kernel_ms = (time.perf_counter() - t1) * 1000.0

        cropped = corrected[core_y0:core_y1, core_x0:core_x1]
        cropped = np.ascontiguousarray(cropped.astype(np.float32, copy=False))

        timing = {
            "io_ms": io_ms,
            "h2d_ms": None,
            "kernel_ms": kernel_ms,
            "d2h_ms": None,
            "total_ms": io_ms + kernel_ms,
            "kernel_includes_transfers": True,
            "halo": halo,
            "raw_tiles_total": assemble_stats.get("tiles_total", 0),
            "raw_tiles_hit": assemble_stats.get("tiles_hit", 0),
        }

        request = TileRequest(key=key, generation=0, priority=0)
        pixels = PixelBuffer(
            residency="cpu",
            dtype="float32",
            shape=tuple(cropped.shape),
            handle=cropped,
        )
        return TileResult(
            request=request,
            pixels=pixels,
            quality=key.quality,
            provisional=False,
            timing=timing,
            error=None,
        )
