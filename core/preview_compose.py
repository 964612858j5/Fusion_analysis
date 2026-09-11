"""The Step1 preview's pixels, as functions of their inputs.

WHY THIS IS A MODULE. The two previews -- the additive overlay and the fusion
preview -- used to be written out inside `MainWindow`, which made them
unavailable to anything that is not the GUI thread. Moving the arrays off that
thread needs the arithmetic to be callable from a worker, and copying it there
is how "the screen and the saved file drifted apart" happened once already
(see `_render_current_patch`'s own comment). So there is ONE implementation
here, with its caches passed in, and two callers: the window's immediate
redraw and the background frame worker.

No Qt, no widget, no window state. Everything these need is an argument:
the raw arrays, the display windows, the colours and weights, and two
callables for the fallbacks a channel without a window needs.
"""

import collections
import weakref

import numpy as np

from .channel_remap import (
    apply_channel_remap, compute_qupath_auto_minmax, tint_and_sum_grays,
)
from .fusion_engine import fuse_channels

# One entry per (patch, channel); a 1024x1024 float32 array is 4 MiB, so a
# 30-channel panel's working set is ~120 MiB and that is the most this may
# hold. See `PreviewCache`.
DEFAULT_MAX_ENTRIES = 64
DEFAULT_MAX_BYTES = 160 * 1024 * 1024


class PreviewCache:
    """Per-channel intermediate arrays, bounded, and verified against their
    source.

    Two rules learned the hard way:

    * `id(arr)` is not an identity. Once a raw array is released CPython may
      hand its address to the next one, and a channel, shape and window that
      all match would then serve the previous patch's pixels. An entry holds
      a WEAK reference to the array it was computed from and a hit re-verifies
      it -- which also means an entry cannot keep a dropped array alive.
    * A window the user has dragged past has no reader. Keeping a version per
      slider step was ~4 MiB each on a real slide; there is ONE version per
      (patch, channel), replaced when the window changes.

    Order is recency of USE (a hit moves its entry to the back), because
    after a patch switch the current patch's channels are the ones every
    frame hits while the previous patch's are what nothing asks for.
    """

    def __init__(self, max_entries=DEFAULT_MAX_ENTRIES,
                 max_bytes=DEFAULT_MAX_BYTES):
        self._entries = collections.OrderedDict()
        self.max_entries = int(max_entries)
        self.max_bytes = int(max_bytes)

    def __len__(self):
        return len(self._entries)

    def __contains__(self, key):
        return key in self._entries

    def __iter__(self):
        return iter(list(self._entries))

    def keys(self):
        return list(self._entries)

    def nbytes(self):
        return sum(entry["nbytes"] for entry in self._entries.values())

    def get(self, key, source, window):
        entry = self._entries.get(key)
        if entry is None or entry["window"] != window:
            return None
        if entry["ref"]() is not source:
            return None
        self._entries.move_to_end(key)
        return entry["value"]

    def put(self, key, source, window, value):
        self._entries.pop(key, None)
        try:
            ref = weakref.ref(source)
        except TypeError:
            return value                     # unreferenceable: do not cache
        self._entries[key] = {
            "ref": ref, "window": window, "value": value,
            "nbytes": int(getattr(value, "nbytes", 0) or 0)}
        self._evict()
        return value

    def drop_channel(self, channel):
        """One channel's entries, for every patch: what a Min/Max edit
        invalidates. The other channels' arrays are the same pixels through
        the same numbers and are kept."""
        self._entries = collections.OrderedDict(
            (key, entry) for key, entry in self._entries.items()
            if key[1] != channel)

    def entry(self, key):
        """The stored record, for a caller that needs to see the window or the
        source it was computed from (the tests, and nothing else)."""
        return self._entries.get(key)

    def drop_patch(self, patch):
        self._entries = collections.OrderedDict(
            (key, entry) for key, entry in self._entries.items()
            if key[0] != patch)

    def clear(self):
        self._entries.clear()

    def _evict(self):
        for key, entry in list(self._entries.items()):
            if entry["ref"]() is None:
                self._entries.pop(key, None)   # its array is gone; unhittable
        while (len(self._entries) > self.max_entries
               or self.nbytes() > self.max_bytes):
            oldest = next(iter(self._entries), None)
            if oldest is None:
                break
            self._entries.pop(oldest, None)


def window_key(channel, remap, keys=("min", "max", "brightness", "contrast",
                                     "gamma")):
    """A cache key made of the display window's VALUES.

    Not of its provenance: a slider moved in the Intensity window changes the
    numbers while the source stays "step0", so a key made of labels would
    keep handing back yesterday's pixels.
    """
    params = (remap or {}).get(channel)
    if not params:
        return ("auto",)
    return tuple((name, _round_value(params.get(name))) for name in keys)


def _round_value(value):
    if value is None:
        return None
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return value


def channel_gray(patch, channel, arr, remap, cache, span=None):
    """One channel as a [0,1] image, remapped once and kept.

    Measured on a 1024x1024 patch: the percentile/remap is ~32 ms per channel
    and the blend that follows is ~0.5 ms, so a tick that recomposited from
    raw pixels would pay the whole mapping again for every channel already on
    screen.
    """
    key = (patch, channel)
    window = window_key(channel, remap)
    hit = cache.get(key, arr, window) if cache is not None else None
    if hit is not None:
        return hit
    params = (remap or {}).get(channel)
    with (span("preview.gray", channel=channel) if span else _Nothing()):
        if not params:
            lo, hi = compute_qupath_auto_minmax(arr, exclude_zero=True)
            params = {"min": float(lo), "max": float(hi)}
        gray = np.asarray(apply_channel_remap(arr, params), dtype=np.float32)
    if cache is None:
        return gray
    return cache.put(key, arr, window, gray)


def channel_signal(patch, channel, arr, remap, cache, fallback_norm,
                   span=None):
    """One channel's [0,1] signal for the fusion preview.

    `arr` is RAW/corrected native intensity. A channel with a manual remap
    uses `apply_channel_remap` (Min/Max/Gamma in raw units -- the same as the
    disk worker); others use `fallback_norm`, which is the loader's own
    percentile normalisation, so their appearance is unchanged.
    """
    key = (patch, channel)
    window = window_key(channel, remap)
    hit = cache.get(key, arr, window) if cache is not None else None
    if hit is not None:
        return hit
    params = (remap or {}).get(channel)
    with (span("preview.signal", channel=channel) if span else _Nothing()):
        if params:
            signal = np.asarray(apply_channel_remap(arr, params),
                                dtype=np.float32)
        else:
            signal = np.asarray(fallback_norm(arr))
    if cache is None:
        return signal
    return cache.put(key, arr, window, signal)


def overlay_rgb_u8(patch, arrays, remap, colors, weights, cache, span=None):
    """The additive overlay: each channel mapped, tinted and summed.

    Returns a uint8 HxWx3 array, or None when nothing contributes. The grays
    are cached per channel, so a colour or weight change re-tints without
    re-mapping, which is the difference between a ~50 ms frame and an ~8 ms
    one.
    """
    grays, tints = {}, {}
    for channel, arr in arrays.items():
        weight = float(weights.get(channel, 0.0) or 0.0)
        if weight <= 0.0:
            continue
        gray = channel_gray(patch, channel, arr, remap, cache, span=span)
        grays[channel] = gray if weight >= 1.0 else gray * weight
        tints[channel] = colors.get(channel, (1.0, 1.0, 1.0))
    with (span("preview.compose", channels=len(grays)) if span
          else _Nothing()):
        rgb = tint_and_sum_grays(grays, tints)
    if rgb is None:
        return None
    return (np.clip(rgb, 0.0, 1.0) * 255).astype(np.uint8)


def fusion_rgb_u8(patch, arrays, remap, groups, group_weights, nucleus,
                  cache, fallback_norm, to_rgb, span=None):
    """The fusion preview: THE fusion core's output, turned into pixels.

    `to_rgb` is the engine's own cyto/nucleus-to-RGB step, passed in rather
    than reimplemented -- the screen and the file have to agree, and they only
    do while there is one implementation of each stage.

    Returns `(uint8 HxWx3 or None, fused_to_nothing)`.
    """
    nuc_ch, nuc_w = nucleus
    wanted = {nuc_ch} if nuc_ch else set()
    for ch_weights in groups.values():
        wanted.update(ch_weights.keys())
    with (span("preview.signals", channels=len(wanted)) if span
          else _Nothing()):
        signals = {ch: channel_signal(patch, ch, arrays[ch], remap, cache,
                                      fallback_norm, span=span)
                   for ch in wanted if ch in arrays}
    if not signals:
        return None, False
    shape = next(iter(signals.values())).shape
    with (span("preview.fuse", channels=len(signals)) if span
          else _Nothing()):
        cyto, nuc = fuse_channels(signals, groups, group_weights,
                                  nuc_ch, nuc_w)
    if cyto is None:
        cyto = np.zeros(shape, dtype=np.float32)
        nuc = np.zeros(shape, dtype=np.float32)
    fused_to_nothing = (float(cyto.max()) <= 0.0 and float(nuc.max()) <= 0.0)
    rgb = to_rgb(cyto, nuc)
    if rgb is None:
        return None, fused_to_nothing
    rgb = np.asarray(rgb)
    if rgb.dtype.kind == "f":
        rgb = (np.clip(rgb, 0.0, 1.0) * 255).astype(np.uint8)
    else:
        rgb = rgb.astype(np.uint8)
    return rgb, fused_to_nothing


class _Nothing:
    """`span=None` means "do not trace", and this is what that costs."""

    __slots__ = ()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False
