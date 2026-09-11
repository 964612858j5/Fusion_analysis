"""The Tissue Preview's pixels, as functions of their inputs.

WHY THIS IS A MODULE. The whole-slide thumbnail used to be composed inside
`Step0Page._update_tissue_preview`, on the GUI thread, from whatever fields
that page happened to be holding. Two things were wrong with that and both
are the reason this file exists:

* a numpy pass over a ~923x555 whole-slide array per slider step is GUI-thread
  work, so it could only ever be done AFTER the hand stopped moving -- the
  trailing debounce the user reported;
* the picture is not Step0's. Step1 shows an overlay of its ticked channels
  or its fusion preview, and Step2/Step3 show whatever context they pinned.
  A function of `self.current_channel` cannot say any of that.

So the arithmetic is here, callable from a worker thread, with everything it
needs passed in: the arrays, the display windows, the colours, the weights and
the fusion configuration. No Qt, no widget, no page.

THREE MODES, and they are deliberately not one generalised formula:

* `"step0"` is the single-channel picture with DAPI composited additively. It
  keeps the `build_display_lut` form the full image and the compare panels
  draw with, because "the thumbnail is the channel as you are seeing it" is
  checkable only while there is one table behind all three.
* `"overlay"` and `"fusion"` delegate to `core.preview_compose`, which is THE
  implementation Step1's main viewer and the disk writer already share. A
  second approximation of the fusion here would be a second answer to what
  the fused image is.
"""

import math

import numpy as np

from . import preview_compose
from .display_mapping import build_display_lut

# The cache key's "patch" slot. The whole-slide arrays are not a patch, but
# `preview_compose`'s keys are (patch, channel, kind) and the tissue frames
# need their own namespace inside the cache they are given -- a Step1 patch
# entry and a whole-slide entry for the same channel are different pixels.
TISSUE_PATCH_KEY = "tissue"

MODE_STEP0 = "step0"
MODE_OVERLAY = "overlay"
MODE_FUSION = "fusion"


def lowres_tinted(arr, mapping, color):
    """`arr` through one display window and one colour, as uint8 RGB.

    The LUT is `build_display_lut` -- the SAME table the image items are
    given -- indexed by the same `(value - min) / (max - min)` the `levels`
    pair means. This is `Step0Page._lowres_tinted` moved here unchanged, so
    the thumbnail keeps the pixels it had; what changed is only which thread
    may run it.
    """
    lo, hi, gamma = mapping
    span = float(hi) - float(lo)
    if not (span > 0 and math.isfinite(span)):
        span = 1.0
    a = np.asarray(arr, dtype=np.float32)
    idx = np.clip((a - float(lo)) * (255.0 / span), 0.0, 255.0)
    lut = build_display_lut(color, gamma)
    return lut[idx.astype(np.uint8)]


def step0_rgb_u8(channel, arrays, mappings, colors, nucleus=""):
    """The single-channel thumbnail, with DAPI composited additively.

    `nucleus` is the nucleus channel's name when its layer is ON and it is a
    different channel from `channel`; "" otherwise. Compositing is
    `CompositionMode_Plus` in numpy -- the same switch the full image and the
    compare panels obey, so the same setting gives the same picture in all
    three.
    """
    arr = arrays.get(channel)
    if arr is None:
        return None
    rgb = lowres_tinted(arr, mappings[channel], colors[channel])
    nuc_arr = arrays.get(nucleus) if nucleus else None
    if nuc_arr is not None and nuc_arr.shape[:2] == arr.shape[:2]:
        rgb = np.clip(
            rgb.astype(np.uint16)
            + lowres_tinted(nuc_arr, mappings[nucleus], colors[nucleus]),
            0, 255).astype(np.uint8)
    return rgb


def _remap_from_mappings(mappings):
    """The `(min, max, gamma)` triples this module speaks, as the parameter
    dicts `core.preview_compose` speaks.

    Brightness/contrast are pinned to the neutral 0.0 / 1.0 -- the same pin
    `Step0Page._display_mapping_for` documents -- because those are exactly
    the values at which remap semantics equal a display window, and the
    Tissue Preview has never had any other.
    """
    remap = {}
    for channel, mapping in (mappings or {}).items():
        if mapping is None:
            continue
        lo, hi, gamma = mapping
        remap[channel] = {"min": float(lo), "max": float(hi),
                          "gamma": float(gamma),
                          "brightness": 0.0, "contrast": 1.0}
    return remap


def overlay_rgb_u8(arrays, mappings, colors, weights, cache, span=None):
    """The whole-slide additive overlay: Step1's overlay over the thumbnail.

    THE same `preview_compose.overlay_rgb_u8` the patch viewer publishes, so
    a weight or a colour means the same thing in both pictures. The grays are
    cached per channel, so moving one weight re-tints rather than re-maps.
    """
    return preview_compose.overlay_rgb_u8(
        TISSUE_PATCH_KEY, arrays, _remap_from_mappings(mappings),
        colors, weights, cache, span=span)


def fusion_rgb_u8(arrays, mappings, groups, group_weights, nucleus,
                  cache, fallback_norm, to_rgb, span=None):
    """The whole-slide fusion preview: THE fusion core over the thumbnail.

    `fuse_channels` and the engine's own cyto/nucleus-to-RGB step, passed in
    rather than reimplemented -- red=cyto, blue=nucleus, one implementation.
    """
    rgb, _fused_to_nothing = preview_compose.fusion_rgb_u8(
        TISSUE_PATCH_KEY, arrays, _remap_from_mappings(mappings),
        groups, group_weights, nucleus, cache, fallback_norm, to_rgb,
        span=span)
    return rgb


def compose(request, cache, span=None):
    """One frame, from the snapshot a coordinator handed over.

    Returns the uint8 HxWx3 array, or None when the request describes nothing
    drawable (no arrays, nothing contributing). The caller decides what an
    undrawable frame means for the screen; this only says there are no pixels.
    """
    mode = request.get("mode")
    arrays = request.get("arrays") or {}
    if not arrays:
        return None
    mappings = request.get("mappings") or {}
    colors = request.get("colors") or {}
    if mode == MODE_STEP0:
        return step0_rgb_u8(request.get("channel"), arrays, mappings, colors,
                            nucleus=request.get("nucleus_layer") or "")
    if mode == MODE_OVERLAY:
        return overlay_rgb_u8(arrays, mappings, colors,
                              request.get("weights") or {}, cache, span=span)
    if mode == MODE_FUSION:
        return fusion_rgb_u8(
            arrays, mappings, request.get("groups") or {},
            request.get("group_weights") or {},
            request.get("nucleus") or ("", 0.0), cache,
            request["fallback_norm"], request["to_rgb"], span=span)
    raise ValueError(f"unknown tissue render mode: {mode!r}")
