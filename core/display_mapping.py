"""One display mapping for every view of a channel.

A channel is shown as raw (or background-corrected) intensity through an
explicit mapping `(display_min, display_max, gamma)`:

    shown = clip((value - display_min) / (display_max - display_min), 0, 1) ** gamma

applied at PAINT time by the image items (`levels` for min/max, the lookup
table for gamma and colour). Nothing is normalised into the pixels: the
compare panels, the full image and its DAPI overlay all read the same
mapping for a channel, so the same tissue has the same brightness in every
view and the mapping can be changed without touching a pixel.

The mapping is seeded ONCE per channel and dataset with the same automatic
window the Channel Remap "Auto" uses (QuPath-style percentiles,
`compute_qupath_auto_minmax`, 0.1% saturation), computed over the whole
slide's tissue pixels (an overview level, zeros excluded) -- not over one
patch, which is what made patches incomparable. From then on it is a pair
of explicit numbers the user may edit. It is display state: it never enters
the h5ad, the corrected zarr or the segmentation remap config.
"""

import numpy as np

# Per-side saturation for the automatic seed, in percent: 0.1 / 99.9 -- the
# same rule and default as `core.channel_remap.compute_qupath_auto_minmax`
# (the Channel Remap "Auto"). Re-stated here in plain numpy rather than
# imported: that module pulls in the GPU stack lazily, and a display seed
# must not initialise CUDA in the middle of a paint.
DISPLAY_SEED_SATURATION = 0.1


def seed_display_range(arr):
    """`(display_min, display_max)` for `arr`, the QuPath-style automatic
    window over its non-zero, finite pixels. Degenerate input (empty,
    constant, all zero) yields a unit-wide window above the minimum so a
    span-based mapping never divides by ~0. Both values are plain floats."""
    a = np.asarray(arr)
    if a.size == 0:
        return 0.0, 1.0
    valid = a[np.isfinite(a) & (a != 0)] if a.dtype.kind == "f" else a[a != 0]
    if valid.size == 0:
        return 0.0, 1.0
    valid = valid.astype(np.float32, copy=False)
    lo = float(np.percentile(valid, DISPLAY_SEED_SATURATION))
    hi = float(np.percentile(valid, 100.0 - DISPLAY_SEED_SATURATION))
    if not np.isfinite(lo):
        lo = 0.0
    if not np.isfinite(hi) or hi <= lo:
        hi = lo + 1.0
    return lo, hi


def build_display_lut(rgb=None, gamma=1.0, size=256):
    """A `size x 3` uint8 lookup table: entry i is `(i/(size-1))**gamma`
    scaled by `rgb` (floats 0..1), or grey when `rgb` is None. With the
    image item's `levels` set to `(display_min, display_max)` this IS the
    mapping in the module docstring, per channel, colour included."""
    gamma = max(float(gamma), 1e-3)
    ramp = (np.arange(size, dtype=np.float32) / float(size - 1)) ** gamma
    colour = (np.ones(3, dtype=np.float32) if rgb is None
              else np.asarray(rgb, dtype=np.float32)[:3])
    table = ramp[:, None] * colour[None, :] * 255.0
    return np.clip(np.round(table), 0, 255).astype(np.uint8)
