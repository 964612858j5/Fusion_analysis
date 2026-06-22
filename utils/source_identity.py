"""
block01/utils/source_identity.py — v14.5a source-aware schema primitives.

Pure-Python, dependency-light schema/validation layer for FUTURE source-aware
Channel Conditioning. v14.5a adds these primitives ONLY — nothing here changes
runtime behavior. There is no resolver, promotion, Step2, or camp/Gi code in this
module, and it is not consumed by any runtime path yet.

Two distinct concepts (never conflate them):

  SourceRequest             — what the GUI/config ASKS for (raw_ome | corrected_zarr).
                              A request, NOT proof of the actual pixel source.
  CalibrationSourceIdentity — what was ACTUALLY resolved from the pixels that the
                              viewer/backend read. Authoritative identity.

Plus:
  source_mixture_mode  — homogeneous_raw | homogeneous_corrected | mixed_raw_corrected
  camp_source_policy   — raw_gi_only (DEFAULT) | selected_source

camp_source_policy defaults to raw_gi_only because background correction is a
NONLINEAR transform (white-tophat + clip) and local-z (Gi*) statistics are not
comparable across raw vs corrected channels; corrected channels must not silently
enter camp/Gi. Wiring this into Step2 is a later phase (v14.5d).
"""

from __future__ import annotations

# ── SourceRequest values ─────────────────────────────────────────────────────
REQUESTED_SOURCE_RAW_OME = "raw_ome"
REQUESTED_SOURCE_CORRECTED_ZARR = "corrected_zarr"
REQUESTED_SOURCES = frozenset({REQUESTED_SOURCE_RAW_OME, REQUESTED_SOURCE_CORRECTED_ZARR})

# ── actual (resolved) source kinds ───────────────────────────────────────────
ACTUAL_SOURCE_RAW_OME = "raw_ome"
ACTUAL_SOURCE_CORRECTED_ZARR = "corrected_zarr"
ACTUAL_SOURCE_KINDS = frozenset({ACTUAL_SOURCE_RAW_OME, ACTUAL_SOURCE_CORRECTED_ZARR})

# Intensity-space label for a background-corrected marker array (the corrected
# zarr per-channel identity). Distinct from raw_ome_native_float.
CORRECTED_INTENSITY_SPACE = "background_corrected_marker_image"
RAW_OME_INTENSITY_SPACE = "raw_ome_native_float"

# ── source mixture modes ─────────────────────────────────────────────────────
SOURCE_MIXTURE_HOMOGENEOUS_RAW = "homogeneous_raw"
SOURCE_MIXTURE_HOMOGENEOUS_CORRECTED = "homogeneous_corrected"
SOURCE_MIXTURE_MIXED = "mixed_raw_corrected"
SOURCE_MIXTURE_MODES = frozenset({
    SOURCE_MIXTURE_HOMOGENEOUS_RAW,
    SOURCE_MIXTURE_HOMOGENEOUS_CORRECTED,
    SOURCE_MIXTURE_MIXED,
})

# ── camp source policy ───────────────────────────────────────────────────────
CAMP_SOURCE_POLICY_RAW_GI_ONLY = "raw_gi_only"
CAMP_SOURCE_POLICY_SELECTED_SOURCE = "selected_source"
CAMP_SOURCE_POLICIES = frozenset({
    CAMP_SOURCE_POLICY_RAW_GI_ONLY,
    CAMP_SOURCE_POLICY_SELECTED_SOURCE,
})
DEFAULT_CAMP_SOURCE_POLICY = CAMP_SOURCE_POLICY_RAW_GI_ONLY


# ── shared kind/fallback decision (v14.5c.2 Q2 primitive) ────────────────────
# ONE truth table, two tolerances via allow_corrected_to_raw_fallback. The
# preview reader (utils/calibration_source.py, allow=True / Strategy B) and the
# promotion-side per-channel resolver (workers/hq_source_resolver.py, allow=False)
# both express THIS rule. Pure-data; no pixel reads (callers determine
# availability). Returns one of the RESOLUTION_* outcomes below.
RESOLUTION_USE_RAW = "raw_ome"
RESOLUTION_USE_CORRECTED = "corrected_zarr"
RESOLUTION_FAIL = "fail"


def decide_source_resolution(requested_source, *, corrected_available,
                             raw_available, allow_corrected_to_raw_fallback):
    """Decide which actual source to use for a channel (or fail).

    requested_source : raw_ome | corrected_zarr (a SourceRequest)
    corrected_available / raw_available : whether the caller could open that source
        for this channel (the caller does the pixel-level availability check).
    allow_corrected_to_raw_fallback : True (preview/Strategy B) tolerates a
        corrected-unavailable -> raw fallback; False (promotion) does NOT.

    Returns RESOLUTION_USE_RAW | RESOLUTION_USE_CORRECTED | RESOLUTION_FAIL.
    """
    if requested_source == REQUESTED_SOURCE_RAW_OME:
        return RESOLUTION_USE_RAW if raw_available else RESOLUTION_FAIL
    if requested_source == REQUESTED_SOURCE_CORRECTED_ZARR:
        if corrected_available:
            return RESOLUTION_USE_CORRECTED
        if allow_corrected_to_raw_fallback and raw_available:
            return RESOLUTION_USE_RAW
        return RESOLUTION_FAIL
    return RESOLUTION_FAIL


# ── helpers ──────────────────────────────────────────────────────────────────
def _is_hw(shape):
    """True if `shape` is a 2-element [H, W] of positive ints."""
    try:
        if len(shape) != 2:
            return False
        return all(int(v) > 0 for v in shape)
    except (TypeError, ValueError):
        return False


def validate_source_request(req):
    """Validate a SourceRequest dict. Returns list[str] of errors (empty == ok).

    Shape: {requested_source: raw_ome|corrected_zarr, channel_name: str,
            reason: str|None}. A request, never authoritative source identity.
    """
    errors = []
    if not isinstance(req, dict):
        return ["source_request must be a dict"]
    rs = req.get("requested_source")
    if rs not in REQUESTED_SOURCES:
        errors.append(
            f"requested_source must be one of {sorted(REQUESTED_SOURCES)}, got {rs!r}")
    name = req.get("channel_name")
    if not isinstance(name, str) or not name.strip():
        errors.append("source_request.channel_name must be a non-empty string")
    if "reason" in req and req["reason"] is not None \
            and not isinstance(req["reason"], str):
        errors.append("source_request.reason must be a string or None")
    return errors


def make_source_request(channel_name, requested_source, reason=None):
    """Build a SourceRequest dict (no validation side effects)."""
    return {
        "requested_source": requested_source,
        "channel_name": str(channel_name),
        "reason": reason,
    }


# Required keys for an authoritative CalibrationSourceIdentity.
_CSI_REQUIRED = ("actual_source_kind", "channel_name", "source_shape", "dtype",
                 "intensity_space")


def validate_calibration_source_identity(csi):
    """Validate a CalibrationSourceIdentity dict. Returns list[str] of errors.

    Required: actual_source_kind, channel_name, source_shape ([H,W]), dtype,
    intensity_space. Optional: actual_source_path, channel_index (int|None),
    channel_key, roi_id, roi_name, roi_bbox_fullres.
    """
    errors = []
    if not isinstance(csi, dict):
        return ["calibration_source_identity must be a dict"]
    for key in _CSI_REQUIRED:
        if key not in csi:
            errors.append(f"calibration_source_identity missing required '{key}'")
    if "actual_source_kind" in csi and csi["actual_source_kind"] not in ACTUAL_SOURCE_KINDS:
        errors.append(
            f"actual_source_kind must be one of {sorted(ACTUAL_SOURCE_KINDS)}, "
            f"got {csi.get('actual_source_kind')!r}")
    if "channel_name" in csi and (
            not isinstance(csi["channel_name"], str) or not csi["channel_name"].strip()):
        errors.append("calibration_source_identity.channel_name must be a non-empty string")
    if "source_shape" in csi and not _is_hw(csi["source_shape"]):
        errors.append("calibration_source_identity.source_shape must be [H, W] positive ints")
    if "dtype" in csi and not isinstance(csi["dtype"], str):
        errors.append("calibration_source_identity.dtype must be a string")
    if "intensity_space" in csi and not isinstance(csi["intensity_space"], str):
        errors.append("calibration_source_identity.intensity_space must be a string")
    ci = csi.get("channel_index", None)
    if ci is not None and not isinstance(ci, int):
        errors.append("calibration_source_identity.channel_index must be int or None")
    return errors


def make_calibration_source_identity(actual_source_kind, channel_name, source_shape,
                                     dtype, intensity_space, actual_source_path=None,
                                     channel_index=None, channel_key=None,
                                     roi_id=None, roi_name=None, roi_bbox_fullres=None):
    """Build a CalibrationSourceIdentity dict from actually-resolved pixel facts."""
    return {
        "actual_source_kind": actual_source_kind,
        "actual_source_path": actual_source_path,
        "channel_name": str(channel_name),
        "channel_index": channel_index,
        "channel_key": channel_key,
        "source_shape": [int(source_shape[0]), int(source_shape[1])],
        "dtype": str(dtype),
        "intensity_space": intensity_space,
        "roi_id": roi_id,
        "roi_name": roi_name,
        "roi_bbox_fullres": list(roi_bbox_fullres) if roi_bbox_fullres else None,
    }


def validate_source_mixture_mode(mode):
    """Returns list[str] of errors (empty == ok)."""
    if mode in SOURCE_MIXTURE_MODES:
        return []
    return [f"source_mixture_mode must be one of {sorted(SOURCE_MIXTURE_MODES)}, got {mode!r}"]


def validate_camp_source_policy(policy):
    """Returns list[str] of errors (empty == ok)."""
    if policy in CAMP_SOURCE_POLICIES:
        return []
    return [f"camp_source_policy must be one of {sorted(CAMP_SOURCE_POLICIES)}, got {policy!r}"]


def camp_source_policy_of(config):
    """Return a config's camp_source_policy, defaulting to raw_gi_only when absent.

    Reading only — does not mutate the config. The default is deliberately the
    comparability-safe policy (corrected channels never silently enter camp/Gi).
    """
    if not isinstance(config, dict):
        return DEFAULT_CAMP_SOURCE_POLICY
    return config.get("camp_source_policy", DEFAULT_CAMP_SOURCE_POLICY) \
        or DEFAULT_CAMP_SOURCE_POLICY


# ── source-aware field registry (single source of truth) ─────────────────────
# A config is "source-aware" if it carries ANY of these v14.5 marker keys
# ANYWHERE in its structure (top-level, source_policy, per-channel params, or a
# nested per-channel metadata dict/list). Such a config must NOT be
# runtime-authoritative (step2_ready) until the v14.5c/v14.5d resolver + Step2
# runtime exist — see the hard-reject guard in
# utils/channel_remap_config.validate_step2_remap_config.
#
# Each marker key name is defined ONCE as a SOURCE_AWARE_KEY_* constant; the
# registry is built from those constants. A meta-test
# (test_source_identity_schema) asserts every SOURCE_AWARE_KEY_* constant is in
# the registry, so a new marker cannot be defined without being registered.
#
# Two kinds of marker:
#   PRESENCE    — mere presence of the key marks the config source-aware.
#   CONDITIONAL — source-aware only for a NON-default value (camp_source_policy:
#                 the safe default raw_gi_only is NOT itself source-aware).
SOURCE_AWARE_KEY_SOURCE_REQUEST = "source_request"
SOURCE_AWARE_KEY_CALIBRATION_SOURCE_IDENTITY = "calibration_source_identity"
SOURCE_AWARE_KEY_ACTUAL_SOURCE_KIND = "actual_source_kind"
SOURCE_AWARE_KEY_ACTUAL_SOURCE_PATH = "actual_source_path"
SOURCE_AWARE_KEY_SOURCE_MIXTURE_MODE = "source_mixture_mode"
SOURCE_AWARE_KEY_CAMP_SOURCE_POLICY = "camp_source_policy"

SOURCE_AWARE_PRESENCE_KEYS = frozenset({
    SOURCE_AWARE_KEY_SOURCE_REQUEST,
    SOURCE_AWARE_KEY_CALIBRATION_SOURCE_IDENTITY,
    SOURCE_AWARE_KEY_ACTUAL_SOURCE_KIND,
    SOURCE_AWARE_KEY_ACTUAL_SOURCE_PATH,
    SOURCE_AWARE_KEY_SOURCE_MIXTURE_MODE,
})
SOURCE_AWARE_CONDITIONAL_KEYS = frozenset({
    SOURCE_AWARE_KEY_CAMP_SOURCE_POLICY,
})
# Every source-aware marker key name. Single source of truth.
SOURCE_AWARE_FIELD_REGISTRY = SOURCE_AWARE_PRESENCE_KEYS | SOURCE_AWARE_CONDITIONAL_KEYS


def _key_is_source_aware(key, value):
    """True if a single (key, value) pair is a source-aware marker trigger."""
    if key in SOURCE_AWARE_PRESENCE_KEYS:
        return True
    if key == SOURCE_AWARE_KEY_CAMP_SOURCE_POLICY:
        return value is not None and value != DEFAULT_CAMP_SOURCE_POLICY
    return False


def _scan_source_aware(obj):
    """Recursively scan a dict/list tree for any source-aware marker key.

    Identifies marker KEYS only; it does not validate nested values (that is the
    job of the SourceRequest / CalibrationSourceIdentity validators). Configs are
    JSON-shaped (no cycles), so plain recursion is safe.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if _key_is_source_aware(k, v):
                return True
            if _scan_source_aware(v):
                return True
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            if _scan_source_aware(item):
                return True
    return False


def source_aware_trigger_value(marker):
    """A sample value that makes `marker` trigger source-awareness (test helper).

    Presence markers trigger on any value; the conditional camp_source_policy
    triggers only on a non-default value.
    """
    if marker == SOURCE_AWARE_KEY_CAMP_SOURCE_POLICY:
        return CAMP_SOURCE_POLICY_SELECTED_SOURCE
    return {"_source_aware_marker": True}


def config_is_source_aware(config):
    """True if the config carries any registered source-aware marker key.

    Scans the WHOLE config tree recursively: top-level, source_policy, per-channel
    params, and nested per-channel metadata dicts/lists. A plain
    camp_source_policy=raw_gi_only (the safe default) is NOT source-aware. Old
    non-source-aware fields (step2_compatible, calibration_source_matches_step2,
    intensity_space, step2_pre_remap_source, and promotion's source_kind/
    source_path) are NOT registered markers and never trip this gate.
    """
    if not isinstance(config, dict):
        return False
    return _scan_source_aware(config)
