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
from .tile_types import (CorrectionKey, PixelBuffer, QualityLevel, RawKey, TileAddress,
                         TileRequest, TileResult, tiles_covering)


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

    def _halo_bbox(self, key: CorrectionKey):
        """Shared bbox/halo derivation used by both `compute()` and
        `raw_keys_for()`, so staging and assembly always cover identical
        tiles (unclamped padded window; clamping happens in the caller)."""
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
        return y0, y1, x0, x1, py0, py1, px0, px1, halo

    # ── level-aware ways (viewer/correction_plan.py) ─────────────────
    def _is_native_coarse(self, key):
        return key.quality == QualityLevel.NATIVE and key.tile.level > 0

    def _level_rect(self, key):
        """The tile's LEVEL-k rectangle, clamped to the level."""
        tile = key.tile
        ts = tile.grid.tile_size
        h, w = self.provider.level_shape(tile.level)
        y0, x0 = tile.ty * ts, tile.tx * ts
        return y0, min(y0 + ts, h), x0, min(x0 + ts, w)

    def _native_bbox(self, key):
        """Level-0 core and halo-padded window of a NATIVE coarse tile."""
        from .correction_plan import stride_of
        s = stride_of(self.provider.level_downsample(key.tile.level))
        y0, y1, x0, x1 = self._level_rect(key)
        halo = halo_for(key.method, int(key.params[0]))
        cy0, cy1, cx0, cx1 = y0 * s, y1 * s, x0 * s, x1 * s
        return s, (y0, y1, x0, x1), (cy0, cy1, cx0, cx1), \
            (cy0 - halo, cy1 + halo, cx0 - halo, cx1 + halo)

    def raw_keys_for(self, key: CorrectionKey):
        """Canonical raw tiles covering `key`'s halo-padded, edge-clamped
        bbox — identical coverage to what `compute()` assembles, so the
        scheduler can stage exactly these through the I/O pool ahead of
        compute."""
        if key.quality == QualityLevel.SAVED:
            return []           # the saved product is read, not corrected
        if self._is_native_coarse(key):
            _s, _lr, _core, (py0, py1, px0, px1) = self._native_bbox(key)
            level = 0
        else:
            _y0, _y1, _x0, _x1, py0, py1, px0, px1, _halo = self._halo_bbox(key)
            level = key.tile.level
        h, w = self.provider.level_shape(level)
        cy0, cy1 = max(0, min(py0, h)), max(0, min(py1, h))
        cx0, cx1 = max(0, min(px0, w)), max(0, min(px1, w))
        if cy1 <= cy0 or cx1 <= cx0:
            return []

        ts = key.tile.grid.tile_size
        coords = tiles_covering((cy0, cx0, cy1, cx1), ts)
        return [
            RawKey(
                source=key.source, channel=key.channel,
                tile=TileAddress(grid=key.tile.grid, level=level, tx=tx, ty=ty),
            )
            for tx, ty in coords
        ]

    def correct_array(self, arr: np.ndarray, method: str, param: int) -> np.ndarray:
        """Run the background-correction kernel for `method` on a whole
        float32 array (no tiling, no halo -- the caller passes a complete
        region so there is no seam to pad against). Same kernels as
        `compute()`, so floor and tile numerics never diverge."""
        arr_f32 = arr.astype(np.float32, copy=False)
        if method == "tophat":
            return bg_correction._apply_tophat_gpu_or_cpu(arr_f32, param)
        elif method == "cucim":
            return bg_correction._apply_cucim_or_cpu(arr_f32, param, prefer_gpu=True)
        else:
            raise ValueError(f"CorrectionCompute does not handle method {method!r}")

    def compute(self, key: CorrectionKey) -> TileResult:
        """Run the correction for `key.method` and return a TileResult.

        Not used for raw tiles ("original"/RawKey) — the scheduler/raw cache
        serve those directly via the provider.
        """
        if key.quality == QualityLevel.SAVED:
            return self._compute_saved(key)
        if self._is_native_coarse(key):
            return self._compute_native(key)
        y0, y1, x0, x1, py0, py1, px0, px1, halo = self._halo_bbox(key)
        tile = key.tile
        param = int(key.params[0])

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
        corrected = self.correct_array(padded, key.method, param)
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

    # ── SAVED / NATIVE (viewer/correction_plan.py) ───────────────────
    def _result(self, key, pixels_arr, timing):
        arr = np.ascontiguousarray(pixels_arr.astype(np.float32, copy=False))
        request = TileRequest(key=key, generation=0, priority=0)
        pixels = PixelBuffer(residency="cpu", dtype="float32",
                             shape=tuple(arr.shape), handle=arr)
        return TileResult(request=request, pixels=pixels, quality=key.quality,
                          provisional=False, timing=timing, error=None)

    def _compute_saved(self, key):
        """The saved product itself, block-averaged to the tile's level."""
        from .correction_plan import saved_product, stride_of
        t0 = time.perf_counter()
        saved = saved_product(key.source.dataset_path, key.channel, key.method,
                              key.params, self.provider.level_shape(0))
        if saved is None or not key.algorithm_version.endswith(
                f"+saved:{saved.token}"):
            raise RuntimeError(f"saved product for {key.channel!r} is gone "
                               "or no longer the one this key names")
        s = stride_of(self.provider.level_downsample(key.tile.level))
        y0, y1, x0, x1 = self._level_rect(key)
        tile = saved.tile((y0, y1, x0, x1), s)
        ms = (time.perf_counter() - t0) * 1000.0
        return self._result(key, tile, {
            "io_ms": ms, "h2d_ms": None, "kernel_ms": 0.0, "d2h_ms": None,
            "total_ms": ms, "kernel_includes_transfers": True, "halo": 0,
            "raw_tiles_total": 0, "raw_tiles_hit": 0, "source": "saved"})

    def _compute_native(self, key):
        """Correct at LEVEL 0 with the real parameter, then block-average:
        what Save produces, seen at this level (exact, not approximate)."""
        s, (y0, y1, x0, x1), (cy0, cy1, cx0, cx1), (py0, py1, px0, px1) = \
            self._native_bbox(key)
        padded_native, (ry0, rx0), assemble_stats = self.assembler.assemble(
            key.source, key.tile.grid, key.channel, 0, py0, py1, px0, px1)
        padded = padded_native.astype(np.float32, copy=False)
        t1 = time.perf_counter()
        corrected = self.correct_array(padded, key.method, int(key.params[0]))
        kernel_ms = (time.perf_counter() - t1) * 1000.0
        h0, w0 = self.provider.level_shape(0)
        cy1, cx1 = min(cy1, h0), min(cx1, w0)
        core = corrected[cy0 - ry0:cy1 - ry0, cx0 - rx0:cx1 - rx0]
        out_h, out_w = y1 - y0, x1 - x0
        hh, ww = -(-core.shape[0] // s), -(-core.shape[1] // s)
        sums = np.zeros((hh * s, ww * s), np.float64)
        counts = np.zeros_like(sums)
        sums[:core.shape[0], :core.shape[1]] = core
        counts[:core.shape[0], :core.shape[1]] = 1.0
        mean = (sums.reshape(hh, s, ww, s).sum(axis=(1, 3))
                / np.maximum(counts.reshape(hh, s, ww, s).sum(axis=(1, 3)), 1.0))
        tile = np.zeros((out_h, out_w), np.float32)
        th, tw = min(out_h, hh), min(out_w, ww)
        tile[:th, :tw] = mean[:th, :tw]
        io_ms = assemble_stats.get("io_ms", 0.0)
        return self._result(key, tile, {
            "io_ms": io_ms, "h2d_ms": None, "kernel_ms": kernel_ms, "d2h_ms": None,
            "total_ms": io_ms + kernel_ms, "kernel_includes_transfers": True,
            "halo": halo_for(key.method, int(key.params[0])),
            "raw_tiles_total": assemble_stats.get("tiles_total", 0),
            "raw_tiles_hit": assemble_stats.get("tiles_hit", 0),
            "source": "native_level0"})
