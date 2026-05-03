"""
Shared mask rendering utilities for Step1/Step3 viewers.

This module is intentionally UI-free. Step pages can call it later without
owning mask colour generation, alpha blending, or Cellpose-style boundaries.
"""

from __future__ import annotations

import numpy as np


def _as_uint8_rgb(img):
    arr = np.asarray(img)
    if arr.ndim == 2:
        arr = np.stack([arr, arr, arr], axis=-1)
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise ValueError("fusion_rgb must have shape (H, W, 3) or (H, W)")
    if arr.dtype == np.uint8:
        return arr.copy()
    arr = arr.astype(np.float32, copy=False)
    if arr.size and float(np.nanmax(arr)) <= 1.0:
        arr = arr * 255.0
    return np.clip(arr, 0, 255).astype(np.uint8)


def _as_label_mask(masks):
    mask = np.asarray(masks)
    if mask.ndim != 2:
        raise ValueError("masks must have shape (H, W)")
    if mask.size == 0:
        return mask.astype(np.uint32, copy=False)
    return np.asarray(mask, dtype=np.uint32)


def _label_colours(max_label):
    """Stable bright colours for labels 1..max_label."""
    colours = np.zeros((max_label + 1, 3), dtype=np.uint8)
    if max_label <= 0:
        return colours
    rng = np.random.RandomState(42)
    colours[1:] = rng.randint(80, 256, size=(max_label, 3), dtype=np.uint8)
    return colours


def _grayscale_rgb(rgb):
    arr = rgb.astype(np.float32, copy=False)
    grey = (
        arr[:, :, 0] * 0.299 +
        arr[:, :, 1] * 0.587 +
        arr[:, :, 2] * 0.114
    ).astype(np.uint8)
    return np.stack([grey, grey, grey], axis=-1)


def extract_mask_boundaries(masks):
    """
    Return a boolean boundary map for a label mask.

    Uses Cellpose's masks_to_outlines when available. A vectorized label-change
    fallback is kept so the renderer works in lightweight environments.
    """
    mask = _as_label_mask(masks)
    if mask.size == 0:
        return np.zeros(mask.shape, dtype=bool)

    try:
        from cellpose import utils as cellpose_utils
        outlines = cellpose_utils.masks_to_outlines(mask)
        return np.asarray(outlines, dtype=bool)
    except Exception:
        pass

    boundaries = np.zeros(mask.shape, dtype=bool)
    foreground = mask > 0
    boundaries[1:, :] |= (mask[1:, :] != mask[:-1, :]) & foreground[1:, :]
    boundaries[:-1, :] |= (mask[:-1, :] != mask[1:, :]) & foreground[:-1, :]
    boundaries[:, 1:] |= (mask[:, 1:] != mask[:, :-1]) & foreground[:, 1:]
    boundaries[:, :-1] |= (mask[:, :-1] != mask[:, 1:]) & foreground[:, :-1]
    return boundaries


def render_mask_overlay(
    fusion_rgb,
    masks,
    alpha=0.35,
    show_outline=True,
    show_fusion=True,
):
    """
    Render an RGB fusion image with a colourful label-mask overlay.

    Parameters
    ----------
    fusion_rgb:
        RGB image used as the preview background. Accepts uint8 [0,255] or
        float [0,1]/[0,255].
    masks:
        Integer label mask where 0 is background and values >0 are cells.
    alpha:
        Fill opacity for labelled pixels. Outlines remain visible when
        show_outline=True.
    show_outline:
        Draw Cellpose-style mask boundaries.
    show_fusion:
        If False, hide the fusion image and render masks on a black background.

    Returns
    -------
    np.ndarray
        RGB uint8 image.
    """
    rgb = _as_uint8_rgb(fusion_rgb)
    mask = _as_label_mask(masks)
    if rgb.shape[:2] != mask.shape:
        raise ValueError("fusion_rgb and masks must have matching height/width")

    alpha = float(np.clip(alpha, 0.0, 1.0))
    out = rgb if show_fusion else np.zeros_like(rgb, dtype=np.uint8)

    max_label = int(mask.max()) if mask.size else 0
    if max_label > 0 and alpha > 0.0:
        colours = _label_colours(max_label)
        label_rgb = colours[np.clip(mask, 0, max_label)]
        cell_px = mask > 0
        blended = (
            out[cell_px].astype(np.float32) * (1.0 - alpha) +
            label_rgb[cell_px].astype(np.float32) * alpha
        )
        out[cell_px] = np.clip(blended, 0, 255).astype(np.uint8)

    if show_outline and max_label > 0:
        outlines = extract_mask_boundaries(mask)
        if np.any(outlines):
            colours = _label_colours(max_label)
            out[outlines] = colours[np.clip(mask[outlines], 0, max_label)]

    return out.astype(np.uint8, copy=False)
