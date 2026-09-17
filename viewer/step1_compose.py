"""Step1's whole-slide composition: N channel tiles into one picture.

Block C of `docs/step1_rework_plan.md`. Block B decided which pixels each
channel may show and delivered them one channel at a time; this turns a set of
them into the Overlay or the Fusion the user asked for.

THE FORMULAE ARE NOT REWRITTEN HERE. Overlay is
`core.channel_remap.apply_channel_remap` per channel, scaled by its weight,
tinted and summed by `tint_and_sum_grays` -- the same two calls
`core.preview_compose.overlay_rgb_u8` makes, so the patch preview and the
whole slide cannot drift apart. Fusion is `core.fusion_engine.fuse_channels`
followed by the engine's own `to_rgb`. A channel at weight <= 0 is DROPPED
from the overlay rather than multiplied by zero, and a channel absent from
`signals` is skipped by the fusion rather than treated as dark: both are the
existing contracts, and both are gated.

THE WINDOW IS THE SHARED STATE'S. Nothing here computes an automatic one: no
percentile, no `compute_qupath_auto_minmax`, no `_norm`. The caller passes the
three numbers it read from `ChannelDisplayState` (B.3), and a channel with no
window is not composed at all -- it is named in `missing_windows` so the host
can ask for a seed.

ABSENCE SURVIVES COMPOSITION. Block B marks "no pixels here" with NaN; a tile
composed from channels that are all absent at a pixel stays absent there, and
the RGBA alpha is 0. A pixel where at least one channel has data is opaque,
and the channels that are absent contribute nothing to it.
"""

import numpy as np

from ..core.channel_remap import apply_channel_remap, tint_and_sum_grays
from ..core.fusion_engine import FusionEngine, fuse_channels

#: What `compose` was asked for.
MODE_OVERLAY = "overlay"
MODE_FUSION = "fusion"


def _remap_params(mapping):
    """The three numbers as `apply_channel_remap` wants them.

    Brightness and contrast are pinned at the neutral 0.0 / 1.0 -- the same
    pin `core.tissue_compose` documents -- because those are the values at
    which a remap IS a display window.
    """
    lo, hi, gamma = mapping
    return {"min": float(lo), "max": float(hi), "gamma": float(gamma),
            "brightness": 0.0, "contrast": 1.0}


def channel_signal(values, valid, mapping):
    """One channel's [0, 1] signal, with its absent pixels as zeros.

    The remap is the product's own (`apply_channel_remap`), which already
    maps a non-finite pixel to 0; `valid` is carried separately so the caller
    can tell "absent" from "dark", which the pixels alone cannot.
    """
    values = np.asarray(values, np.float32)
    signal = np.asarray(apply_channel_remap(values, _remap_params(mapping)),
                        np.float32)
    if valid is not None:
        signal = np.where(np.asarray(valid, bool), signal, 0.0)
    return signal


def _valid_union(tiles):
    union = None
    for _values, valid in tiles.values():
        if valid is None:
            continue
        union = valid.copy() if union is None else (union | valid)
    return union


def _rgba(rgb, valid):
    """An HxWx4 uint8 picture: absent pixels transparent, the rest opaque."""
    rgb = np.asarray(rgb, np.uint8)
    out = np.zeros(rgb.shape[:2] + (4,), np.uint8)
    out[..., :3] = rgb
    out[..., 3] = 255 if valid is None else np.where(valid, 255, 0)
    return out


def compose_overlay(tiles, weights, colors, mappings):
    """The additive overlay, through the same two calls the patch preview uses.

    `tiles` is `{channel: (values, valid)}`; `weights`, `colors` and
    `mappings` are the answers the callers read from the owners. Returns
    `(rgba, valid, missing_windows)`.
    """
    grays, tints, missing = {}, {}, []
    for channel, (values, valid) in tiles.items():
        weight = float(weights.get(channel, 0.0) or 0.0)
        if weight <= 0.0:
            # DROPPED, not multiplied by zero: `overlay_rgb_u8`'s own rule,
            # and the reason a zero-weight channel is never mapped at all.
            continue
        mapping = mappings.get(channel)
        if mapping is None:
            missing.append(channel)
            continue
        gray = channel_signal(values, valid, mapping)
        grays[channel] = gray if weight >= 1.0 else gray * weight
        tints[channel] = colors.get(channel, (1.0, 1.0, 1.0))

    union = _valid_union({ch: tiles[ch] for ch in grays})
    if not grays:
        return None, union, missing
    rgb = tint_and_sum_grays(grays, tints)
    if rgb is None:
        return None, union, missing
    rgb = (np.clip(rgb, 0.0, 1.0) * 255).astype(np.uint8)
    return _rgba(rgb, union), union, missing


def compose_fusion(tiles, groups, group_weights, nucleus, mappings,
                   to_rgb=None):
    """The fusion, through `fuse_channels` and the engine's own RGB step.

    `groups` is `{group: {channel: weight}}` and `nucleus` is
    `(channel, weight)` -- the shapes `fuse_channels` already takes. Returns
    `(rgba, valid, missing_windows)`.
    """
    to_rgb = FusionEngine.to_rgb if to_rgb is None else to_rgb
    wanted = {nucleus[0]} if nucleus and nucleus[0] else set()
    for channel_weights in (groups or {}).values():
        wanted.update(channel_weights.keys())

    signals, missing = {}, []
    for channel in sorted(wanted):
        if channel not in tiles:
            # ABSENT, not dark: `fuse_channels` skips a channel it was not
            # given, which is not the same as giving it zeros.
            continue
        mapping = mappings.get(channel)
        if mapping is None:
            missing.append(channel)
            continue
        values, valid = tiles[channel]
        signals[channel] = channel_signal(values, valid, mapping)

    union = _valid_union({ch: tiles[ch] for ch in signals})
    if not signals:
        return None, union, missing
    cyto, nuc = fuse_channels(signals, groups or {}, group_weights or {},
                              nucleus[0] if nucleus else "",
                              float(nucleus[1]) if nucleus else 0.0)
    if cyto is None:
        return None, union, missing
    rgb = to_rgb(cyto, nuc)
    rgb = np.asarray(rgb)
    if rgb.dtype.kind == "f":
        rgb = (np.clip(rgb, 0.0, 1.0) * 255).astype(np.uint8)
    return _rgba(rgb.astype(np.uint8), union), union, missing


def compose(mode, tiles, *, weights=None, colors=None, mappings=None,
            groups=None, group_weights=None, nucleus=("", 0.0), to_rgb=None):
    """One frame of `mode`, from tiles the caller already has."""
    mappings = dict(mappings or {})
    if mode == MODE_FUSION:
        return compose_fusion(tiles, groups or {}, group_weights or {},
                              nucleus, mappings, to_rgb=to_rgb)
    if mode == MODE_OVERLAY:
        return compose_overlay(tiles, dict(weights or {}),
                               dict(colors or {}), mappings)
    raise ValueError(f"unknown Step1 composition mode: {mode!r}")


def composition_key(mode, tile_keys, *, weights=None, colors=None,
                    mappings=None, groups=None, group_weights=None,
                    nucleus=("", 0.0)):
    """What makes this composed tile what it is (C.2).

    The underlying tile keys carry the dataset, the channel, the source and
    the level; everything the COMPOSITION adds is here -- the mode, which
    channels took part, their weights, the group weights, the nucleus, the
    display windows and the colours. Nothing in this key belongs in a tile
    key, which is why moving a weight re-composes without re-reading.
    """
    def _channels(values):
        return tuple(sorted((str(k), _round(v))
                            for k, v in dict(values or {}).items()))

    def _round(value):
        if isinstance(value, (list, tuple)):
            return tuple(_round(v) for v in value)
        try:
            return round(float(value), 6)
        except (TypeError, ValueError):
            return value

    group_part = tuple(sorted(
        (str(name), _channels(channels))
        for name, channels in dict(groups or {}).items()))
    return (str(mode),
            tuple(sorted(tile_keys)),
            _channels(weights),
            _channels(colors),
            _channels(mappings),
            group_part,
            _channels(group_weights),
            (str(nucleus[0]) if nucleus else "",
             _round(nucleus[1]) if nucleus else 0.0))
