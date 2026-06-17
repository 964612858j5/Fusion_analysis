"""
block01/utils/channel_remap_config.py — v13.1 channel remap config schema.

Load / save / validate the `segmentation_preprocess_config` used by the v13.1
manual channel remap path. See
docs/v13_1_channel_conditioning/02_PIPELINE_ARCHITECTURE.md (config separation)
and 05_DATA_PROVENANCE_AND_H5AD.md (used_for must be segmentation_only).

This config drives segmentation conditioning ONLY. It must never be used as the
default h5ad expression source. The `used_for` field makes that explicit and is
forced to "segmentation_only" on normalize.
"""

from __future__ import annotations

import json
import os

CONFIG_VERSION = "v13.1"
CONFIG_MODE = "manual_remap"
AUTO_ALGORITHM = "qupath_percentile"
USED_FOR = "segmentation_only"
DEFAULT_AUTO_SATURATION = 0.1

# Source / intensity provenance. Min/Max parameters are meaningless without
# knowing what intensity space they live in (normalized display vs raw vs
# corrected-native float). Every config records this. Conservative defaults
# mark a config preview-only and the intensity space unknown until the producer
# (e.g. Step3) states otherwise. See
# docs/v13_1_channel_conditioning/05_DATA_PROVENANCE_AND_H5AD.md.
_SOURCE_POLICY_DEFAULTS = {
    "source": "unknown",            # who produced it, e.g. step3_current_roi
    "intensity_space": "unknown",   # e.g. corrected_zarr_native_float
    "normalization": "unknown",     # e.g. none / minmax_per_read
    "scope": "unknown",             # e.g. roi_preview
    "preview_only": True,           # not yet guaranteed Step2-ready
    "note": (
        "Min/Max are in the intensity_space named here. A preview_only config "
        "is not guaranteed to match the Step2 segmentation input."
    ),
}

# Per-channel parameter defaults for a freshly added channel. min/max are None
# until set manually or via Auto (then frozen to concrete numbers).
_CHANNEL_DEFAULTS = {
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

# Numeric per-channel keys and their (lo, hi) sane bounds for validation.
# None bound means "unbounded on that side".
_NUMERIC_BOUNDS = {
    "brightness": (-1.0, 1.0),
    "contrast": (0.0, None),
    "gamma": (0.0, None),
    "opacity": (0.0, 1.0),
    "weight": (0.0, None),
}


def default_channel_remap_params():
    """Return a fresh copy of the per-channel parameter defaults."""
    return dict(_CHANNEL_DEFAULTS)


def normalize_channel_remap_params(params):
    """Return a full, type-coerced per-channel params dict.

    Fills missing keys from defaults, coerces numeric fields to float (leaving
    min/max as None if unset), and coerces the boolean flags. Unknown keys are
    preserved (forward-compat) but not validated.
    """
    out = dict(_CHANNEL_DEFAULTS)
    if params:
        out.update(params)

    for key in ("min", "max"):
        if out[key] is not None:
            out[key] = float(out[key])
    for key in ("brightness", "contrast", "gamma", "opacity", "weight"):
        out[key] = float(out[key])
    for key in ("enabled", "auto"):
        out[key] = bool(out[key])
    return out


def default_source_policy():
    """Return a fresh copy of the conservative source-policy defaults."""
    return dict(_SOURCE_POLICY_DEFAULTS)


def normalize_source_policy(policy):
    """Return a full source_policy, filling missing keys from defaults."""
    out = dict(_SOURCE_POLICY_DEFAULTS)
    if policy:
        out.update(policy)
    out["preview_only"] = bool(out.get("preview_only", True))
    for key in ("source", "intensity_space", "normalization", "scope"):
        out[key] = str(out.get(key, "unknown"))
    return out


def default_channel_remap_config(channel_names=None):
    """Build a default top-level config, optionally seeding channel entries."""
    channels = {}
    for name in (channel_names or []):
        channels[str(name)] = default_channel_remap_params()
    return {
        "version": CONFIG_VERSION,
        "mode": CONFIG_MODE,
        "auto_algorithm": AUTO_ALGORITHM,
        "auto_saturation": DEFAULT_AUTO_SATURATION,
        "used_for": USED_FOR,
        "source_policy": default_source_policy(),
        "channels": channels,
    }


def normalize_channel_remap_config(config):
    """Return a normalized copy of a full config.

    Forces the provenance-critical fields (version/mode/auto_algorithm/used_for)
    and normalizes every channel's params. `used_for` is always forced to
    "segmentation_only" — remapped intensity is never an expression source.
    """
    cfg = dict(config or {})
    cfg["version"] = cfg.get("version", CONFIG_VERSION) or CONFIG_VERSION
    cfg["mode"] = cfg.get("mode", CONFIG_MODE) or CONFIG_MODE
    cfg["auto_algorithm"] = cfg.get("auto_algorithm", AUTO_ALGORITHM) or AUTO_ALGORITHM
    cfg["auto_saturation"] = float(cfg.get("auto_saturation", DEFAULT_AUTO_SATURATION))
    cfg["used_for"] = USED_FOR  # forced — segmentation only
    cfg["source_policy"] = normalize_source_policy(cfg.get("source_policy"))

    channels = cfg.get("channels", {}) or {}
    cfg["channels"] = {
        str(name): normalize_channel_remap_params(p)
        for name, p in channels.items()
    }
    return cfg


def validate_channel_remap_config(config):
    """Validate a config. Returns a list of error strings (empty == valid).

    Checks structure, used_for, and per-channel parameter sanity, including the
    critical `max > min` rule (a degenerate window produces a zero conditioning
    image — see core/channel_remap.py).
    """
    errors = []
    if not isinstance(config, dict):
        return ["config must be a dict"]

    if config.get("used_for", USED_FOR) != USED_FOR:
        errors.append(
            f"used_for must be '{USED_FOR}' (remapped intensity is "
            f"segmentation-only, never an expression source)"
        )

    sat = config.get("auto_saturation", DEFAULT_AUTO_SATURATION)
    try:
        sat = float(sat)
        if not (0.0 <= sat < 50.0):
            errors.append("auto_saturation must be in [0, 50)")
    except (TypeError, ValueError):
        errors.append("auto_saturation must be a number")

    # source_policy is required: Min/Max are ambiguous without an intensity
    # space. It must at least declare preview_only.
    policy = config.get("source_policy", None)
    if not isinstance(policy, dict):
        errors.append(
            "config missing 'source_policy' (records source / intensity_space "
            "/ preview_only — Min/Max are ambiguous without it)")
    elif "preview_only" not in policy:
        errors.append("source_policy missing 'preview_only'")

    channels = config.get("channels", None)
    if channels is None:
        errors.append("config missing 'channels'")
        return errors
    if not isinstance(channels, dict):
        return errors + ["'channels' must be a dict of name -> params"]

    for name, params in channels.items():
        if not isinstance(params, dict):
            errors.append(f"channel '{name}': params must be a dict")
            continue

        for key, (lo, hi) in _NUMERIC_BOUNDS.items():
            if key not in params:
                continue
            try:
                val = float(params[key])
            except (TypeError, ValueError):
                errors.append(f"channel '{name}': {key} must be a number")
                continue
            if lo is not None and val < lo:
                errors.append(f"channel '{name}': {key}={val} below min {lo}")
            if hi is not None and val > hi:
                errors.append(f"channel '{name}': {key}={val} above max {hi}")

        mn = params.get("min", None)
        mx = params.get("max", None)
        if mn is not None and mx is not None:
            try:
                if float(mx) <= float(mn):
                    errors.append(
                        f"channel '{name}': max ({mx}) must be > min ({mn}); "
                        f"a degenerate window yields a zero conditioning image"
                    )
            except (TypeError, ValueError):
                errors.append(f"channel '{name}': min/max must be numbers")

    return errors


def load_channel_remap_config(path):
    """Load and normalize a config from a JSON file.

    Raises FileNotFoundError if missing, ValueError if it fails validation.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"channel remap config not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    cfg = normalize_channel_remap_config(raw)
    errors = validate_channel_remap_config(cfg)
    if errors:
        raise ValueError(
            "invalid channel remap config:\n  " + "\n  ".join(errors)
        )
    return cfg


def save_channel_remap_config(config, path):
    """Normalize, validate, and write a config to JSON. Returns the saved dict.

    Creates parent directories as needed. Raises ValueError if invalid (we do
    not persist invalid configs — they would silently corrupt segmentation).
    """
    cfg = normalize_channel_remap_config(config)
    errors = validate_channel_remap_config(cfg)
    if errors:
        raise ValueError(
            "refusing to save invalid channel remap config:\n  "
            + "\n  ".join(errors)
        )

    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, sort_keys=False)
    return cfg
