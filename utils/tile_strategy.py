"""Adaptive Step2 tile/grid recommendations.

This module is intentionally policy-only: it suggests tile geometry but does
not change merge ownership, overlap semantics, or label assignment.
"""

from __future__ import annotations

import math


_CELLPOSE_METHODS = {
    "cellpose_wholecell_fusion",
    "legacy_cellpose_wholecell_fusion",
    "cellpose_nuclei_dapi",
    "cellpose_nuclei_expansion",
    "cellpose_nuclei_hq",
    "cellpose_nuclei_hq2",
}
_HQ_METHODS = {"cellpose_nuclei_hq", "cellpose_nuclei_hq2"}
_MESMER_METHODS = {"mesmer_whole_cell", "mesmer_nuclei", "mesmer_nuclear_guided"}


def _norm_backend(backend):
    return str(backend or "").strip().lower()


def _target_mpx(backend, vram_gb=None, channel_count=1, target_tile_mpx=None):
    if target_tile_mpx is not None:
        return max(1.0, float(target_tile_mpx))

    b = _norm_backend(backend)
    vram = float(vram_gb or 0.0)
    channels = max(1, int(channel_count or 1))

    if b in _HQ_METHODS or "hq" in b:
        # HQ/HQ2 are CPU/memory-heavy and often read extra channels.
        return max(10.0, min(30.0, 24.0 - max(0, channels - 2) * 2.0))
    if b in _MESMER_METHODS or "mesmer" in b:
        if vram >= 40:
            return 35.0
        if vram >= 16:
            return 28.0
        return 18.0
    if b in _CELLPOSE_METHODS or "cellpose" in b:
        if vram >= 40:
            return 64.0
        if vram >= 20:
            return 40.0
        if vram >= 10:
            return 28.0
        return 18.0
    return 24.0


def _overlap_for_backend(backend):
    b = _norm_backend(backend)
    if b in _MESMER_METHODS or "mesmer" in b:
        return 160
    if b in _HQ_METHODS or "hq" in b:
        return 224
    return 200


def suggest_tile_strategy(
    full_h,
    full_w,
    backend,
    vram_gb=None,
    channel_count=1,
    target_tile_mpx=None,
):
    """Return a backend-aware Step2 tile/grid suggestion.

    The grid is chosen to keep own-tile area near the target megapixels while
    preserving the existing row/column tiling model.  Estimated VRAM is a
    conservative float32 working-set approximation, not a hard allocator.
    """
    full_h = max(1, int(full_h or 1))
    full_w = max(1, int(full_w or 1))
    channels = max(1, int(channel_count or 1))
    target = _target_mpx(backend, vram_gb, channels, target_tile_mpx)

    image_mpx = full_h * full_w / 1e6
    desired_tiles = max(1, int(math.ceil(image_mpx / target)))
    aspect = full_w / float(full_h)
    n_cols = max(1, int(round(math.sqrt(desired_tiles * aspect))))
    n_rows = max(1, int(math.ceil(desired_tiles / float(n_cols))))

    # Avoid pathological under-tiling after rounding.
    while (full_h / n_rows) * (full_w / n_cols) / 1e6 > target * 1.20:
        if (full_w / n_cols) >= (full_h / n_rows):
            n_cols += 1
        else:
            n_rows += 1

    tile_h = int(math.ceil(full_h / float(n_rows)))
    tile_w = int(math.ceil(full_w / float(n_cols)))
    tile_mpx = tile_h * tile_w / 1e6

    # Rough Step2 working set: input + normalized float32 + backend temp.
    backend_factor = 5.5
    b = _norm_backend(backend)
    if b in _HQ_METHODS or "hq" in b:
        backend_factor = 4.0 + min(channels, 8) * 0.7
    elif b in _MESMER_METHODS or "mesmer" in b:
        backend_factor = 6.5
    estimated_vram = tile_h * tile_w * max(2, channels) * 4 * backend_factor / (1024.0 ** 3)

    return {
        "tile_h": int(tile_h),
        "tile_w": int(tile_w),
        "n_rows": int(n_rows),
        "n_cols": int(n_cols),
        "overlap": int(_overlap_for_backend(backend)),
        "estimated_tile_mpx": float(tile_mpx),
        "estimated_vram_usage": float(estimated_vram),
    }
