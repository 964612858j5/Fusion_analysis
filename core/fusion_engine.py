"""
block01/core/fusion_engine.py — Channel fusion and mask overlay utilities.
"""

import gc
import numpy as np

from .channel_remap import apply_channel_remap


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
        shape = next(iter(cache.values())).shape

        def _n(arr):
            return arr if prenormalized else self._normalize_intensity(arr)

        signals = []
        for gname, ch_weights in groups.items():
            gw    = float(np.clip(group_weights.get(gname, 1.0), 0.0, 1.0))
            accum = np.zeros(shape, dtype=np.float32)
            for ch, w in ch_weights.items():
                if ch in cache and w > 0:
                    accum += _n(cache[ch]) * float(np.clip(w, 0.0, 1.0))
            accum *= gw
            np.clip(accum, 0.0, 1.0, out=accum)
            signals.append(accum)

        cyto = np.zeros(shape, dtype=np.float32)
        for s in signals:
            np.maximum(cyto, s, out=cyto)
        np.clip(cyto, 0.0, 1.0, out=cyto)

        nucleus = np.zeros(shape, dtype=np.float32)
        if nuc_ch and nuc_ch in cache and nuc_w > 0:
            nucleus = _n(cache[nuc_ch]) * float(np.clip(nuc_w, 0.0, 1.0))
            np.clip(nucleus, 0.0, 1.0, out=nucleus)

        return cyto, nucleus

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
        needed = set(nuc_ch)
        for cw in groups.values():
            needed.update(cw.keys())
        needed.add(nuc_ch)

        cache = {}
        for ch in needed:
            if ch in loader.ch_map:
                raw = loader.read_region(ch, y0, y1, x0, x1, downsample=1,
                                         normalize=False)
                p = remap.get(ch)
                if p:
                    cache[ch] = apply_channel_remap(raw, p).astype(np.float32)
                else:
                    cache[ch] = loader._norm(raw)   # exact percentile norm (1–99.5)

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
