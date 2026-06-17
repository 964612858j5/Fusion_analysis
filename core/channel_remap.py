"""
block01/core/channel_remap.py — v13.1 manual channel remap operator.

Segmentation-conditioning transform that replaces the top-hat / cuCIM
preprocessing path in the v13.1 experimental pipeline. See
docs/v13_1_channel_conditioning/03_CHANNEL_REMAP_SPEC.md.

Design constraints (do not violate):
  - Backend-agnostic. NumPy first; the same code runs on CuPy arrays because
    every array op is dispatched through the array's own module (`_xp`).
  - No Qt and no I/O in this module. It must stay importable by headless Step2
    later and by the Step3 UI prototype now.
  - Output is always float32 in [0, 1].
  - Auto (QuPath-style percentile window) is provided as a *function* only.
    It is NEVER applied per-tile automatically — callers compute it once on a
    reference region and freeze min/max into config. See the spec, "Auto scope
    rule".

Canonical transform order is normative: window -> contrast/brightness -> gamma,
with a clip after each stage. Do not reorder.
"""

from __future__ import annotations

import numpy as np

# Per-channel parameter defaults. `min`/`max` default to None, meaning "derive
# from the image" (image min / image max) at apply time.
DEFAULT_PARAMS = {
    "enabled": True,
    "min": None,
    "max": None,
    "brightness": 0.0,
    "contrast": 1.0,
    "gamma": 1.0,
    "opacity": 1.0,
    "weight": 1.0,
    "auto": False,
}

# Smallest gamma we allow; clamps gamma <= 0 to a safe positive value so the
# power step never sees 0 or negative exponents.
_MIN_GAMMA = 1e-6


def _xp(arr):
    """Return the array module (numpy or cupy) backing `arr`.

    Lets one implementation serve both backends without importing cupy unless
    the caller actually passed a cupy array.
    """
    mod = type(arr).__module__
    if mod.startswith("cupy"):
        import cupy as cp  # local import: optional dependency
        return cp
    return np


def _merge_params(params):
    """Return a full params dict, filling missing keys from DEFAULT_PARAMS."""
    merged = dict(DEFAULT_PARAMS)
    if params:
        merged.update(params)
    return merged


def apply_channel_remap(image, params=None):
    """Apply the v13.1 remap transform to a single channel image.

    Parameters
    ----------
    image : array-like (numpy or cupy), any numeric dtype
        Raw channel intensities.
    params : dict, optional
        Per-channel parameters. Recognised keys: min, max, brightness,
        contrast, gamma. `min`/`max` may be None -> derived from the image.
        `enabled`, `opacity`, `weight`, `auto` are ignored here (they are
        fusion/display concerns); `auto` in particular is never recomputed
        here — pass frozen min/max instead.

    Returns
    -------
    float32 array in [0, 1], same shape as `image`.

    Notes
    -----
    - Non-finite input pixels (NaN / inf) are treated as out-of-tissue and map
      to 0 in the output.
    - A degenerate window (max <= min, e.g. a constant image) yields all zeros.
      This is the safe, reproducible choice; the validator flags such configs.
    """
    p = _merge_params(params)
    xp = _xp(image)

    x = xp.asarray(image).astype(xp.float32)
    if x.size == 0:
        return x  # empty image -> empty float32

    # Track non-finite pixels so we can force them to 0 at the end.
    finite = xp.isfinite(x)
    # Replace non-finite with 0 for arithmetic stability.
    x = xp.where(finite, x, xp.float32(0.0))

    # Resolve window. None means "derive from image" over finite pixels only.
    min_value = p["min"]
    max_value = p["max"]
    if min_value is None or max_value is None:
        if bool(finite.any()):
            finite_vals = x[finite]
            if min_value is None:
                min_value = float(finite_vals.min())
            if max_value is None:
                max_value = float(finite_vals.max())
        else:
            min_value = 0.0 if min_value is None else min_value
            max_value = 1.0 if max_value is None else max_value

    min_value = float(min_value)
    max_value = float(max_value)

    denom = max_value - min_value
    if denom <= 0.0:
        # Degenerate window: no usable contrast. Return zeros (reproducible).
        out = xp.zeros_like(x)
        return out

    contrast = float(p["contrast"])
    brightness = float(p["brightness"])
    gamma = float(p["gamma"])
    if gamma <= 0.0:
        gamma = _MIN_GAMMA

    # ── window ────────────────────────────────────────────────────────
    x = (x - xp.float32(min_value)) / xp.float32(denom)
    x = xp.clip(x, 0.0, 1.0)

    # ── contrast / brightness ─────────────────────────────────────────
    x = (x - xp.float32(0.5)) * xp.float32(contrast) + xp.float32(0.5) + xp.float32(brightness)
    x = xp.clip(x, 0.0, 1.0)

    # ── gamma ─────────────────────────────────────────────────────────
    x = x ** xp.float32(gamma)
    x = xp.clip(x, 0.0, 1.0)

    # Non-finite input pixels map to 0.
    x = xp.where(finite, x, xp.float32(0.0))
    return x.astype(xp.float32)


def compute_qupath_auto_minmax(image, saturation=0.1, valid_mask=None,
                               exclude_zero=False):
    """QuPath-style percentile auto-contrast window.

    Computes min/max as low/high percentiles of valid pixels:
        min = percentile(valid, saturation)
        max = percentile(valid, 100 - saturation)
    With the default saturation=0.1 this is the 0.1 / 99.9 percentiles.

    Parameters
    ----------
    image : array-like (numpy or cupy)
    saturation : float
        Per-side saturation percentage. Stored in config as `auto_saturation`.
    valid_mask : array-like bool, optional
        True where pixels are valid (in-tissue). Combined with a finite check.
    exclude_zero : bool
        If True, drop exactly-zero pixels (common for padded / background).

    Returns
    -------
    (min_value, max_value) : tuple of float

    Notes
    -----
    Returns (0.0, 1.0) if there are no valid pixels. This is a *reference*
    computation; callers freeze the result into config. It must not be called
    per Step2 tile (see spec, "Auto scope rule").
    """
    xp = _xp(image)
    x = xp.asarray(image)

    finite = xp.isfinite(x)
    mask = finite
    if valid_mask is not None:
        mask = mask & xp.asarray(valid_mask).astype(bool)
    if exclude_zero:
        mask = mask & (x != 0)

    valid = x[mask]
    if valid.size == 0:
        return 0.0, 1.0

    valid = valid.astype(xp.float32)
    low = float(xp.percentile(valid, saturation))
    high = float(xp.percentile(valid, 100.0 - saturation))
    return low, high


def fuse_channels(remapped_channels, weights=None):
    """Weighted combine of already-remapped channels into one float32 0–1 image.

    Parameters
    ----------
    remapped_channels : sequence of float32 arrays in [0, 1]
    weights : sequence of float, optional
        Per-channel fusion weights. Defaults to 1.0 each.

    Returns
    -------
    float32 array in [0, 1] — the fused segmentation-conditioning image.

    Uses a weighted average (sum(w_i * x_i) / sum(w_i)) so the result stays in
    [0, 1]. Returns zeros if total weight is 0 or no channels are given.
    """
    channels = list(remapped_channels)
    if not channels:
        raise ValueError("fuse_channels: no channels provided")

    xp = _xp(channels[0])
    if weights is None:
        weights = [1.0] * len(channels)
    if len(weights) != len(channels):
        raise ValueError("fuse_channels: weights length != channels length")

    total_w = float(sum(weights))
    acc = xp.zeros_like(channels[0].astype(xp.float32))
    if total_w <= 0.0:
        return acc

    for ch, w in zip(channels, weights):
        acc = acc + ch.astype(xp.float32) * xp.float32(w)
    acc = acc / xp.float32(total_w)
    return xp.clip(acc, 0.0, 1.0).astype(xp.float32)


def apply_channel_remap_config(channel_images, config):
    """Apply a full remap config to a set of named channels and fuse them.

    Parameters
    ----------
    channel_images : dict[str, array]
        Maps channel name -> raw channel image.
    config : dict
        A channel-remap config (see utils/channel_remap_config.py). Uses
        config["channels"][name] per-channel params. Disabled channels and
        channels missing from `channel_images` are skipped.

    Returns
    -------
    dict with:
        "remapped" : dict[str, float32 array]  per-channel remapped images
        "fused"    : float32 array in [0, 1] or None  weighted fusion of the
                     enabled channels (None if no enabled channels matched)

    This is the segmentation-conditioning output. It is NOT an expression
    matrix — see docs/.../05_DATA_PROVENANCE_AND_H5AD.md.
    """
    channels_cfg = (config or {}).get("channels", {}) or {}

    remapped = {}
    fuse_list = []
    fuse_weights = []
    for name, params in channels_cfg.items():
        if name not in channel_images:
            continue
        p = _merge_params(params)
        if not p.get("enabled", True):
            continue
        img = channel_images[name]
        out = apply_channel_remap(img, p)
        remapped[name] = out
        fuse_list.append(out)
        fuse_weights.append(float(p.get("weight", 1.0)))

    fused = None
    if fuse_list:
        fused = fuse_channels(fuse_list, fuse_weights)

    return {"remapped": remapped, "fused": fused}


# ── Reference-sketch aliases (forward-compat with the spec's API names) ──────
def remap_channel(image, params=None):
    """Alias of apply_channel_remap (spec sketch name)."""
    return apply_channel_remap(image, params)


def auto_window(valid_pixels, low_pct=0.1, high_pct=99.9):
    """Percentile window from an already-selected reference pixel set.

    Unlike compute_qupath_auto_minmax this takes the *flattened valid pixels*
    directly and explicit low/high percentiles.
    """
    xp = _xp(valid_pixels)
    v = xp.asarray(valid_pixels)
    v = v[xp.isfinite(v)].astype(xp.float32)
    if v.size == 0:
        return 0.0, 1.0
    return float(xp.percentile(v, low_pct)), float(xp.percentile(v, high_pct))
