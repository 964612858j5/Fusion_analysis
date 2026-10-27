"""The full remap records behind Block01's two window tables.

User ruling 2026-10-27 (docs/v17_step0_own_ranges_plan.md): Step0 keeps its
own Min/Max/Gamma, and Step1, Step2, Step3 and Regions share one set. The
display state holds each table's `(min, max, gamma)`; the RECORDS here hold
the whole remap dict a Save writes and Step1 fuses with (gamma, brightness,
contrast, opacity, weight, auto, ...), plus which channels somebody typed.
They live in the dataset's display namespace (`DisplayNamespace.records`),
so they follow the slide exactly as the windows do.

Plain functions over one table's record dict, so the page and its tests do
not each re-implement "put", "seed" and "export".
"""

from ...utils.channel_remap_config import (default_channel_remap_params,
                                           normalize_channel_remap_params)


def window_params(lo, hi, gamma=1.0, base=None, auto=False):
    """A remap dict carrying the window `(lo, hi, gamma)`.

    Brightness/contrast are pinned to the neutral 0/1, where a remap equals a
    display window -- Step0 has no UI for them. Every other key of `base` is
    kept.
    """
    params = dict(base) if base else default_channel_remap_params()
    params.update({"min": float(lo), "max": float(hi), "gamma": float(gamma),
                   "brightness": 0.0, "contrast": 1.0, "auto": bool(auto)})
    return normalize_channel_remap_params(params)


def get(records, channel):
    """`channel`'s remap dict, or None when it has no real window."""
    return (records or {}).get("params", {}).get(channel)


def is_user(records, channel):
    return channel in (records or {}).get("user_adjusted", ())


def put(records, channel, params, user):
    """Record a window somebody chose (`user=True`) or that is the table's
    own answer from elsewhere (a restore, a handoff copy)."""
    records["params"][channel] = normalize_channel_remap_params(dict(params))
    if user:
        records["user_adjusted"].add(channel)


def put_seed(records, channel, lo, hi, gamma=1.0):
    """File an AUTOMATIC window, only where the table has none. True when
    written. A seed never marks a channel typed and never moves one that is
    -- or one this table already holds."""
    if channel in records["params"]:
        return False
    records["params"][channel] = window_params(lo, hi, gamma, auto=True)
    return True


def export(records, keys=None):
    """Copies of the remap dicts, every real window or just `keys` of each."""
    params = (records or {}).get("params", {})
    out = {}
    for channel, p in params.items():
        out[str(channel)] = ({k: p[k] for k in keys if k in p}
                             if keys else dict(p))
    return out


def window_of(params):
    """`(min, max, gamma)` of a remap dict."""
    return (float(params["min"]), float(params["max"]),
            float(params.get("gamma", 1.0)))
