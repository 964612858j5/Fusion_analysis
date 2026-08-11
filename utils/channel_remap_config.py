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

# v14.5a: source-aware schema primitives (pure-Python, no runtime coupling). Used
# only by the Step2 validation guard below to hard-reject a source-aware config
# that claims step2_ready before runtime support exists.
from .source_identity import (
    config_is_source_aware, SOURCE_MIXTURE_MODES,
    SOURCE_MIXTURE_HOMOGENEOUS_RAW, SOURCE_MIXTURE_HOMOGENEOUS_CORRECTED)

# v14.5c.3: the ONLY source_alignment_mode a per-channel source-aware promotion
# emits. mixed_raw_corrected is a source_mixture_mode value, NEVER an alignment
# mode — the two axes are distinct.
SOURCE_ALIGNMENT_PER_CHANNEL_NATIVE = "per_channel_native"

CONFIG_VERSION = "v13.1"
CONFIG_MODE = "manual_remap"
AUTO_ALGORITHM = "qupath_percentile"
USED_FOR = "segmentation_only"
DEFAULT_AUTO_SATURATION = 0.1

# Registered provenance values for a preview remap config's `created_from_step`
# — the GUI surface that authored it. Enumerated (not ad-hoc free strings) so
# v14.5 promotion can recognize each producer explicitly. This is a pure-data
# constant layer: it must never import promotion / resolver / Step2 runtime.
#   step1_5_channel_conditioning — legacy Step1.5 page (kept internal in v14)
#   step0_channel_conditioning   — v14 Step0 Setup & Preprocessing workbench
CREATED_FROM_STEP1_5_CONDITIONING = "step1_5_channel_conditioning"
CREATED_FROM_STEP0_CONDITIONING = "step0_channel_conditioning"
CREATED_FROM_STEPS = frozenset({
    CREATED_FROM_STEP1_5_CONDITIONING,
    CREATED_FROM_STEP0_CONDITIONING,
})


def is_registered_created_from_step(value):
    """True if `value` is a known/enumerated preview-config provenance string."""
    return value in CREATED_FROM_STEPS

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
    # ── Step3<->Step2 source alignment (Phase 2.1c) ──────────────────
    # The remap Min/Max live in the PRE-REMAP source intensity space. A config
    # is only Step2-ready when Step3 calibrated on the same pre-remap source
    # that Step2 will read (native/corrected float, normalize=False), AND Step2
    # runtime integration exists. See
    # docs/v13_1_channel_conditioning/10_STEP3_STEP2_SOURCE_ALIGNMENT.md.
    "step2_pre_remap_source": "unknown",      # source Step2 will read pre-remap
    "calibration_source_matches_step2": False,  # marker source == Step2 source?
    # How the marker channels align with the Step2 source (Phase 2.1d):
    #   single_native_source        — all markers, one native source, matched
    #   per_channel_native          — valid per-channel native mix, all matched
    #   partial_or_preview_fallback — >=1 marker preview-only/mismatched
    #   none                        — no markers
    "source_alignment_mode": "unknown",
    "step2_ready": False,           # always False until Step2 integration lands
    "alignment_note": (
        "step2_ready stays false until Step2 runtime integration is "
        "implemented. calibration_source_matches_step2 reports only whether the "
        "marker pre-remap source matches the expected Step2 source."
    ),
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
    for key in ("preview_only", "calibration_source_matches_step2", "step2_ready"):
        out[key] = bool(out.get(key, _SOURCE_POLICY_DEFAULTS[key]))
    for key in ("source", "intensity_space", "normalization", "scope",
                "step2_pre_remap_source", "source_alignment_mode"):
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
    elif bool(policy.get("step2_ready", False)) and bool(policy.get("preview_only", True)):
        # A config cannot be both preview_only and Step2-ready.
        errors.append(
            "source_policy inconsistent: step2_ready=true requires "
            "preview_only=false")

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


def channel_remap_config_hash(config):
    """Stable short hash of a (normalized) config — for run provenance."""
    import hashlib
    cfg = normalize_channel_remap_config(config)
    blob = json.dumps(cfg, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


# Intensity spaces that are NOT a Step2-compatible native pre-remap source.
_NON_STEP2_INTENSITY_SPACES = {"raw_ome_normalized_0_1", "unknown", ""}

# Reference-layer name fragments that must never appear as marker remap channels.
_REFERENCE_NAME_FRAGMENTS = ("dapi", "nuclei", "nucleus", "mask", "fusion")


def _is_reference_channel_name(name):
    n = str(name or "").strip().lower()
    return any(frag in n for frag in _REFERENCE_NAME_FRAGMENTS)


def validate_step2_remap_config(config, allow_preview_remap=False,
                                allow_source_aware_runtime=False):
    """Validate a remap config for Step2 *runtime* use (Phase 5b).

    Stricter than validate_channel_remap_config: enforces source alignment so the
    saved Min/Max live in Step2's native pre-remap source intensity space, and
    that no reference layer leaked into the marker channels.

    Parameters
    ----------
    config : dict (raw or normalized)
    allow_preview_remap : bool
        Required True to accept a config that is still preview_only / not
        step2_ready (all Phase 2.1c/d Step3 configs are). Without it such a
        config is rejected as experimental.

    Returns
    -------
    (errors, resolved_params)
        errors : list[str] — empty == OK to run
        resolved_params : dict[str, dict] — per-channel remap params for engines
            (only populated when errors is empty)
    """
    # Check the RAW used_for before normalization: normalize_channel_remap_config
    # force-sets used_for="segmentation_only", which would otherwise mask a config
    # that explicitly declared a different (non-segmentation) purpose.
    raw_used_for = config.get("used_for") if isinstance(config, dict) else None

    cfg = normalize_channel_remap_config(config)
    errors = list(validate_channel_remap_config(cfg))

    if raw_used_for != USED_FOR:
        errors.append(
            f"used_for must be '{USED_FOR}' for Step2 remap (raw config "
            f"declared {raw_used_for!r})")

    sp = cfg.get("source_policy", {}) or {}
    mode = sp.get("source_alignment_mode", "unknown")
    if mode == "partial_or_preview_fallback":
        errors.append(
            "source_alignment_mode is 'partial_or_preview_fallback'; at least one "
            "marker is a normalized/unknown preview fallback — refusing to run")
    if not bool(sp.get("calibration_source_matches_step2", False)):
        errors.append(
            "source_policy.calibration_source_matches_step2 is false; marker "
            "source does not match the Step2 pre-remap source")

    preview = bool(sp.get("preview_only", True))
    ready = bool(sp.get("step2_ready", False))
    if (preview or not ready) and not allow_preview_remap:
        errors.append(
            "config is preview_only/step2_ready=false; pass allow_preview_remap="
            "true to run it experimentally")

    # v14.5a hard guard: source-aware fields are schema-only until the v14.5c/d
    # resolver + Step2 runtime exist. A source-aware config that claims
    # step2_ready would otherwise be run with the new fields SILENTLY IGNORED
    # (old single-source behavior). Reject it — hard failure, never a warning.
    #
    # v14.5d: the guard is lifted ONLY via the explicit allow_source_aware_runtime
    # flag AND only for a WELL-FORMED runtime config (validate_source_aware_runtime_
    # config passes). The caller passes allow_source_aware_runtime=True solely from
    # inside the worker's per-channel branch (flag on) — never globally. A config
    # that merely "looks like" a runtime config is not enough: it must validate.
    if config_is_source_aware(cfg) and bool(sp.get("step2_ready", False)):
        runtime_ok = (allow_source_aware_runtime
                      and not validate_source_aware_runtime_config(config))
        if not runtime_ok:
            errors.append(
                "source-aware remap config with step2_ready=true is not supported "
                "without the v14.5d per-channel runtime "
                "(allow_source_aware_runtime + a valid runtime config); Step2 would "
                "otherwise silently ignore the source-aware fields. Refusing.")

    channels = cfg.get("channels", {}) or {}
    if not channels:
        errors.append("config has no marker channels")

    for name, params in channels.items():
        if _is_reference_channel_name(name):
            errors.append(
                f"channel '{name}' looks like a reference layer "
                "(DAPI/nuclei/mask/fusion); reference layers must not be marker "
                "remap channels")
        ispace = str(params.get("intensity_space", "unknown"))
        if ispace in _NON_STEP2_INTENSITY_SPACES:
            errors.append(
                f"channel '{name}': intensity_space '{ispace}' is not a "
                "Step2-compatible native source")
        if not bool(params.get("step2_compatible", False)):
            errors.append(f"channel '{name}': step2_compatible is false")
        ch_matches = bool(params.get("calibration_source_matches_step2", False))
        if not ch_matches:
            errors.append(
                f"channel '{name}': calibration_source_matches_step2 is false")
        # Per-channel source self-consistency: step2_pre_remap_source must be a
        # native Step2 source, and when the channel claims to match Step2 its
        # intensity_space must equal that source (conservative — no equivalence
        # mappings in Phase 5b).
        step2_src = str(params.get("step2_pre_remap_source", "unknown"))
        if step2_src in _NON_STEP2_INTENSITY_SPACES:
            errors.append(
                f"channel '{name}': step2_pre_remap_source '{step2_src}' is not "
                "a Step2-compatible native source")
        elif ch_matches and ispace != step2_src:
            errors.append(
                f"channel '{name}': intensity_space '{ispace}' != "
                f"step2_pre_remap_source '{step2_src}' while "
                "calibration_source_matches_step2 is true (inconsistent metadata)")
        mn, mx = params.get("min"), params.get("max")
        if mn is None or mx is None:
            errors.append(
                f"channel '{name}': min/max must be concrete numbers for Step2 "
                "(Auto must be frozen at calibration, never per tile)")

    if errors:
        return errors, {}

    resolved = {name: dict(params) for name, params in channels.items()}
    return [], resolved


def _coerce_channel_list(selected_channels):
    """Normalize selected_channels into a list[str] of channel names.

    Defensive: a bare string must NOT be iterated character-by-character. A
    string is split on ';' ',' and whitespace; list/tuple/set are stringified
    and stripped; None -> []. (Mirrors parse_hq_channels without importing the
    workers layer.)
    """
    if selected_channels is None:
        return []
    if isinstance(selected_channels, str):
        import re
        return [p.strip() for p in re.split(r"[;,\s]+", selected_channels) if p.strip()]
    if isinstance(selected_channels, (list, tuple, set)):
        return [str(p).strip() for p in selected_channels if str(p).strip()]
    # Any other scalar -> single name.
    return [str(selected_channels).strip()] if str(selected_channels).strip() else []


def validate_remap_covers_selected_channels(resolved_params, selected_channels,
                                            hq_input_mode=None):
    """Ensure every active Step2 marker channel has remap params.

    Prevents a hidden mixed mode where some selected markers are remapped and
    others silently fall back to the legacy percentile/Gi gate.

    Parameters
    ----------
    resolved_params : dict[str, dict]  — channels present in the remap config
    selected_channels : iterable[str]  — Step2's active marker channels
    hq_input_mode : str | None         — Step2 hq_input_mode

    Returns
    -------
    list[str] — errors (empty == OK). Reference layers (DAPI/mask/fusion) are
    not required. step1_weighted_fusion is rejected: manual remap is
    marker-channel based, not a fused single signal.
    """
    if str(hq_input_mode or "").strip().lower() == "step1_weighted_fusion":
        return [
            "channel_remap_config is not supported with "
            "hq_input_mode='step1_weighted_fusion'; manual remap is "
            "marker-channel based. Use a per-marker channel mode."
        ]
    selected = _coerce_channel_list(selected_channels)
    if any(ch == "step1_weighted_fusion" for ch in selected):
        return [
            "channel_remap_config is not supported with the "
            "'step1_weighted_fusion' marker; manual remap is marker-channel based."
        ]
    present = set(resolved_params or {})
    missing = [
        ch for ch in selected
        if not _is_reference_channel_name(ch) and ch not in present
    ]
    if missing:
        return [
            "channel_remap_config missing selected Step2 marker channels: "
            + ", ".join(missing)
        ]
    return []


def validate_source_aware_promoted_candidate(config):
    """Validate a v14.5c.3 source-aware promoted CANDIDATE. Returns list[str] of
    errors (empty == a well-formed candidate).

    A candidate is the output of per-channel source-aware promotion: it passed the
    recorded↔resolved↔runtime 3-way + geometry guard, but is NOT yet runtime-
    executable (Step2 per-channel runtime is v14.5d), so step2_ready MUST be false.
    This is validator ACCEPTANCE of the candidate state — it does NOT make the
    config runtime-executable and does NOT relax the v14.5a.1 step2_ready guard.
    source_alignment_mode must be 'per_channel_native' ONLY; a mixture value
    (mixed_raw_corrected etc.) is never a valid alignment mode.
    """
    errors = []
    if not isinstance(config, dict):
        return ["config must be a dict"]
    cfg = normalize_channel_remap_config(config)
    sp = cfg.get("source_policy", {}) or {}
    if not bool(config.get("created_by_source_aware_promotion", False)):
        errors.append(
            "not a source-aware promoted candidate "
            "(created_by_source_aware_promotion != true)")
    if not bool(config.get("source_aware_promotion_ready", False)):
        errors.append("source_aware_promotion_ready != true")
    align = sp.get("source_alignment_mode")
    if align in SOURCE_MIXTURE_MODES:
        errors.append(
            f"source_alignment_mode must not be a source_mixture_mode value "
            f"({align!r}); the alignment and mixture axes are distinct")
    elif align != SOURCE_ALIGNMENT_PER_CHANNEL_NATIVE:
        errors.append(
            f"source_alignment_mode must be '{SOURCE_ALIGNMENT_PER_CHANNEL_NATIVE}' "
            f"for a per-channel candidate, got {align!r}")
    mixture = config.get("source_mixture_mode")
    if mixture not in SOURCE_MIXTURE_MODES:
        errors.append(
            f"source_mixture_mode must be one of {sorted(SOURCE_MIXTURE_MODES)}, "
            f"got {mixture!r}")
    if bool(sp.get("step2_ready", False)):
        errors.append(
            "a source-aware promoted candidate must have step2_ready=false "
            "(per-channel Step2 runtime is v14.5d)")
    return errors


def validate_source_aware_runtime_config(config):
    """Validate a v14.5d source-aware RUNTIME config (step2_ready=true, executable).

    The runtime analogue of validate_source_aware_promoted_candidate. A runtime config
    is what promote_candidate_to_runtime emits: a re-verified candidate stamped
    runtime_supported=true + step2_ready=true, homogeneous source only, every marker
    carrying its resolved per-channel source (resolved_source_kind / resolved_source_path).
    Returns list[str] errors (empty == a well-formed runtime config).

    This validator does NOT relax validate_step2_remap_config's source-aware guard: a
    runtime config is consumed ONLY by the flag-gated v14.5d per-channel Step2 path
    (ENABLE_STEP2_SOURCE_AWARE_REMAP_RUNTIME). It must never reach the single-source
    worker path, which would ignore the per-channel sources.
    """
    errors = []
    if not isinstance(config, dict):
        return ["config must be a dict"]
    cfg = normalize_channel_remap_config(config)
    sp = cfg.get("source_policy", {}) or {}
    if not bool(config.get("created_by_source_aware_promotion", False)):
        errors.append(
            "not a source-aware promotion (created_by_source_aware_promotion != true)")
    if not bool(config.get("source_aware_promotion_ready", False)):
        errors.append("source_aware_promotion_ready != true (not a completed candidate)")
    if not bool(config.get("runtime_supported", False)):
        errors.append("runtime_supported != true (candidate not promoted to runtime)")
    if not bool(sp.get("step2_ready", False)):
        errors.append("source-aware runtime config must have step2_ready=true")
    align = sp.get("source_alignment_mode")
    if align != SOURCE_ALIGNMENT_PER_CHANNEL_NATIVE:
        errors.append(
            f"source_alignment_mode must be '{SOURCE_ALIGNMENT_PER_CHANNEL_NATIVE}', "
            f"got {align!r}")
    mixture = config.get("source_mixture_mode")
    if mixture not in (SOURCE_MIXTURE_HOMOGENEOUS_RAW, SOURCE_MIXTURE_HOMOGENEOUS_CORRECTED):
        errors.append(
            "source_mixture_mode must be homogeneous (homogeneous_raw or "
            f"homogeneous_corrected) for v14.5d, got {mixture!r}")
    # Every marker's resolved kind must be a known source AND consistent with the
    # homogeneous top-level mixture (no corrected marker under a raw mixture, etc.).
    expected_kind = {SOURCE_MIXTURE_HOMOGENEOUS_RAW: "raw_ome",
                     SOURCE_MIXTURE_HOMOGENEOUS_CORRECTED: "corrected_zarr"}.get(mixture)
    # Read channels from the RAW config so per-channel resolved_source_* custom keys
    # are not lost to normalization.
    channels = config.get("channels", {}) or {}
    if not channels:
        errors.append("config has no marker channels")
    for name, params in channels.items():
        params = params or {}
        if _is_reference_channel_name(name):
            errors.append(
                f"channel '{name}' is a reference layer; not a marker channel")
        kind = params.get("resolved_source_kind")
        if kind not in ("raw_ome", "corrected_zarr"):
            errors.append(
                f"channel '{name}': resolved_source_kind must be raw_ome|corrected_zarr, "
                f"got {kind!r}")
        elif expected_kind and kind != expected_kind:
            errors.append(
                f"channel '{name}': resolved_source_kind '{kind}' is inconsistent with "
                f"source_mixture_mode '{mixture}' (expected {expected_kind})")
        if not params.get("resolved_source_path"):
            errors.append(f"channel '{name}': missing resolved_source_path")
        if not bool(params.get("step2_compatible", False)):
            errors.append(f"channel '{name}': step2_compatible is false")
    return errors
