"""
block01/core/fusion_engine.py — Channel fusion and mask overlay utilities.
"""

import gc
import numpy as np

from .channel_remap import apply_channel_remap

# Which fusion arithmetic produced a result.  It lives here, beside the maths,
# so a change to the formula and a change to this number are the same edit.
#
# 1 = the historical family: the preview and FusionEngine combined channels one
#     way while the on-disk FullFusionWorker re-normalised per group, per tile
#     and globally, so the same configuration produced different pixels
#     depending on which path made them, and a different tile grid produced
#     different pixels again.
# 2 = one implementation, `fuse_channels`, called by all three, over channels
#     mapped through a window that was committed once and does not depend on
#     the patch, region or tile in hand.
#
# Anything that carries no version at all predates the field and cannot be
# assumed to match: a reader must treat a missing version as "unknown", never
# as "current".
FUSION_FORMULA_VERSION = 2


def fuse_channels(signals, groups, group_weights, nuc_ch, nuc_w):
    """THE Step1 fusion. One implementation; every path calls this one.

    `signals` are per-channel images ALREADY mapped to [0, 1] — the caller
    decides what that mapping is (a user's Min/Max/Gamma, or a frozen automatic
    window) and applies it exactly once. This function does no reading, no
    mapping, no normalisation of its own: given the same signals and the same
    weights it returns the same pixels, whether it was called for one patch on
    screen, one region for a segmentation preview, or one tile being written to
    disk.

        cyto    = max over groups of  clip( gw · Σ w_ch · signal_ch )
        nucleus = clip( nuc_w · signal_nuc )

    Weights are clipped to [0, 1] first, so a hand-edited config cannot push a
    group past the others by scale alone. Channels absent from `signals` are
    skipped rather than treated as zero — a channel that has not arrived is not
    the same as a channel that is dark.

    Returns `(cyto, nucleus)` float32 in [0, 1], the shape of the inputs, or
    `(None, None)` when there is nothing to fuse.
    """
    if not signals:
        return None, None
    shape = next(iter(signals.values())).shape

    cyto = np.zeros(shape, dtype=np.float32)
    for gname, ch_weights in (groups or {}).items():
        gw = float(np.clip(group_weights.get(gname, 1.0), 0.0, 1.0))
        accum = np.zeros(shape, dtype=np.float32)
        for ch, w in ch_weights.items():
            if ch in signals and w > 0:
                accum += signals[ch] * float(np.clip(w, 0.0, 1.0))
        accum *= gw
        np.clip(accum, 0.0, 1.0, out=accum)
        np.maximum(cyto, accum, out=cyto)
    np.clip(cyto, 0.0, 1.0, out=cyto)

    nucleus = np.zeros(shape, dtype=np.float32)
    if nuc_ch and nuc_ch in signals and nuc_w > 0:
        nucleus = signals[nuc_ch] * float(np.clip(nuc_w, 0.0, 1.0))
        np.clip(nucleus, 0.0, 1.0, out=nucleus)

    return cyto, nucleus


class FusionEngine:

    @staticmethod
    def _normalize_intensity(img):
        # FIX: intensity normalization
        arr = np.asarray(img, dtype=np.float32)
        if arr.size == 0:
            return arr
        mn = float(np.min(arr))
        mx = float(np.max(arr))
        eps = 1e-6
        return np.clip((arr - mn) / (mx - mn + eps), 0.0, 1.0)

    def compute(self, cache, groups, group_weights, nuc_ch, nuc_w,
                prenormalized=False):
        """Returns (cyto, nucleus) float32 [0,1].

        prenormalized=True: `cache` already holds per-channel [0,1] signals (e.g.
        remap/percentile applied by the caller) — skip the internal min-max so the
        caller's normalization (incl. the manual remap) is preserved."""
        if not cache:
            return None, None
        if prenormalized:
            signals = cache
        else:
            # Only what the fusion will actually read: normalising a channel
            # nobody weighs would be work for nothing, and the old inline loop
            # did not do it either.
            wanted = {nuc_ch} if nuc_ch else set()
            for ch_weights in (groups or {}).values():
                wanted.update(ch_weights.keys())
            signals = {ch: self._normalize_intensity(arr)
                       for ch, arr in cache.items() if ch in wanted}
        return fuse_channels(signals, groups, group_weights, nuc_ch, nuc_w)

    def fuse_fullres(self, loader, y0, y1, x0, x1,
                     groups, group_weights, nuc_ch, nuc_w,
                     channel_remap_params=None):
        """Full-resolution fusion, returns (H,W,2) uint16 for Cellpose.

        When `channel_remap_params` ({ch: {min,max,gamma,...}}) is given, that
        channel's RAW intensities are conditioned with apply_channel_remap (Step0
        manual remap, raw units) — matching the on-screen preview and the disk
        FullFusionWorker; channels without a remap use the loader's percentile
        norm. Channels are read RAW (normalize=False) so the raw-unit remap window
        is correct."""
        remap = channel_remap_params or {}
        needed = {nuc_ch} if nuc_ch else set()
        for cw in groups.values():
            needed.update(cw.keys())

        cache = {}
        for ch in needed:
            if ch in loader.ch_map:
                p = remap.get(ch)
                if not p:
                    # No committed window means no agreed scale for this
                    # channel; giving it one derived from this region would make
                    # the result depend on which region was asked for.
                    print(f"[Fusion] {ch} has no committed display window; "
                          "it takes no part in this fusion")
                    continue
                raw = loader.read_region(ch, y0, y1, x0, x1, downsample=1,
                                         normalize=False)
                cache[ch] = apply_channel_remap(raw, p).astype(np.float32)

        cyto, nucleus = self.compute(cache, groups, group_weights, nuc_ch, nuc_w,
                                     prenormalized=True)
        result = np.stack([
            (cyto    * 65535).astype(np.uint16),
            (nucleus * 65535).astype(np.uint16),
        ], axis=-1)
        del cache
        gc.collect()
        return result

    @staticmethod
    def to_rgb(cyto, nucleus):
        r = (np.clip(cyto,    0, 1) * 255).astype(np.uint8)
        g = np.zeros_like(r)
        b = (np.clip(nucleus, 0, 1) * 255).astype(np.uint8)
        return np.stack([r, g, b], axis=-1)

    @staticmethod
    def overlay_mask(rgb, mask):
        import cv2
        out     = rgb.copy()
        n_cells = int(mask.max())
        if n_cells == 0:
            return out

        # ── Per-cell color (semi-transparent fill) ──────────────────────
        rng    = np.random.RandomState(42)
        colors = rng.randint(80, 255, size=(n_cells + 1, 3), dtype=np.uint8)
        colors[0] = [0, 0, 0]   # background color (unused)

        cell_area = mask > 0
        if cell_area.any():
            mask_clipped = np.clip(mask, 0, n_cells).astype(np.int32)
            fill_color   = colors[mask_clipped]          # (H, W, 3)
            alpha        = 0.30
            out[cell_area] = (
                out[cell_area].astype(np.float32) * (1.0 - alpha)
                + fill_color[cell_area].astype(np.float32) * alpha
            ).astype(np.uint8)

        # ── Thick green boundary (dilate−erode) ─────────────────────────
        bin_mask = (mask > 0).astype(np.uint8)
        kernel   = np.ones((5, 5), np.uint8)
        boundary = (cv2.dilate(bin_mask, kernel, iterations=1)
                    - cv2.erode(bin_mask, kernel, iterations=1))
        out[boundary > 0] = [0, 255, 80]
        return out
