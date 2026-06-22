"""
block01/utils/calibration_source.py — v14.5b preview-time calibration source reader.

Builds a per-channel CalibrationSourceIdentity from the ACTUAL pixel source that
was opened/read, for the Step0 Channel Conditioning preview config. This is NOT
the Step2 HQ source resolver (workers/hq_source_resolver.py) and NOT promotion —
it never decides Step2 runtime source, never produces step2_ready output, and is
pure (Qt-free) so it is unit-testable without the GUI.

Key invariant (Strategy B):
  SourceRequest          = what the config/user ASKED for.
  CalibrationSourceIdentity = what was ACTUALLY opened/read from pixels.
They may legitimately differ: a corrected request whose channel is unavailable
falls back to raw, records requested_source=corrected_zarr +
actual_source_kind=raw_ome + reason=corrected_unavailable_fallback. A corrected
identity is NEVER written unless a corrected channel array was actually opened.

For a corrected channel, source_shape and dtype are grounded in the OPENED zarr
array object (.shape/.dtype), not blindly trusted from attrs; the self-reported
attr source_shape, if it disagrees, is recorded but does not override the actual
array shape.
"""

from __future__ import annotations

import os

import numpy as np

from .source_identity import (
    REQUESTED_SOURCE_RAW_OME,
    REQUESTED_SOURCE_CORRECTED_ZARR,
    ACTUAL_SOURCE_RAW_OME,
    ACTUAL_SOURCE_CORRECTED_ZARR,
    RAW_OME_INTENSITY_SPACE,
    CORRECTED_INTENSITY_SPACE,
    SOURCE_MIXTURE_HOMOGENEOUS_RAW,
    SOURCE_MIXTURE_HOMOGENEOUS_CORRECTED,
    SOURCE_MIXTURE_MIXED,
    make_source_request,
    make_calibration_source_identity,
)

# SourceRequest.reason values.
REASON_USER_SELECTED = "user_selected"
REASON_DEFAULT_RAW = "default_raw"
REASON_CORRECTED_FALLBACK = "corrected_unavailable_fallback"


class SourceAwareIdentityError(Exception):
    """A conditioned channel's source identity could not be resolved.

    v14.5b.1 Strategy A: a single channel's failure aborts the WHOLE source-aware
    preview save — no partial config is written. Carries the failed channel name,
    its requested source, and a message so the UI can surface exactly what failed.
    """

    def __init__(self, channel, requested_source=None, cause=None, message=None):
        self.channel = channel
        self.requested_source = requested_source
        self.cause = cause
        self.message = message or (str(cause) if cause is not None else "")
        super().__init__(
            f"source identity failed for channel {channel!r} "
            f"(requested {requested_source!r}): {self.message}")


def open_corrected_channel_array(corrected_zarr_path, channel_name, roi_name=None):
    """Open the ACTUAL corrected channel array object, or None if unavailable.

    Corrected zarr is root[<roi_group>][<channel>] (roi_only). Prefers a group
    matching roi_name, else the first group that contains an array named
    channel_name. Returns the live zarr array object (so callers read .shape/
    .dtype/.attrs from the real pixel object). None on any failure — never raises.
    """
    if not corrected_zarr_path or not os.path.exists(corrected_zarr_path):
        return None
    try:
        import zarr
        root = zarr.open_group(corrected_zarr_path, mode="r")
    except Exception:
        return None
    group_names = list(root.group_keys())
    ordered = ([roi_name] if (roi_name and roi_name in group_names) else []) \
        + [g for g in group_names if g != roi_name]
    for gname in ordered:
        try:
            grp = root[gname]
            if channel_name in list(grp.array_keys()):
                return grp[channel_name]
        except Exception:
            continue
    return None


def build_corrected_calibration_identity(array, corrected_zarr_path, channel_name):
    """CalibrationSourceIdentity from an OPENED corrected zarr array object.

    source_shape and dtype are grounded in the array object itself. channel_index/
    channel_key/intensity_space/correction_method/roi_name/roi_bbox_fullres come
    from the v14.5a per-array attrs (fields the array object does not provide).
    A self-reported attr source_shape that disagrees with the actual shape is
    recorded as a mismatch but does NOT override the actual array shape.
    """
    attrs = dict(getattr(array, "attrs", {}) or {})
    actual_shape = [int(array.shape[0]), int(array.shape[1])]   # grounded in pixels
    actual_dtype = str(array.dtype)                             # grounded in pixels

    csi = make_calibration_source_identity(
        actual_source_kind=ACTUAL_SOURCE_CORRECTED_ZARR,
        channel_name=channel_name,
        source_shape=actual_shape,
        dtype=actual_dtype,
        intensity_space=attrs.get("intensity_space", CORRECTED_INTENSITY_SPACE),
        actual_source_path=os.path.abspath(corrected_zarr_path),
        channel_index=attrs.get("channel_index"),
        channel_key=attrs.get("channel_key", channel_name),
        roi_name=attrs.get("roi_name"),
        roi_bbox_fullres=attrs.get("roi_bbox_fullres"),
    )
    csi["correction_method"] = attrs.get("correction_method", "unknown")
    attr_shape = attrs.get("source_shape")
    if attr_shape is not None and [int(v) for v in attr_shape] != actual_shape:
        # Honest: keep the actual array shape, flag the disagreement.
        csi["attr_source_shape"] = [int(v) for v in attr_shape]
        csi["attr_source_shape_mismatch"] = True
    return csi


def build_raw_calibration_identity(raw_array, raw_path, channel_name,
                                   channel_index=None, patch_bbox=None, roi_name=None):
    """CalibrationSourceIdentity from the ACTUAL raw array read via the loader.

    shape/dtype grounded in the read array; path/channel_index from the loader.
    """
    arr = np.asarray(raw_array)
    return make_calibration_source_identity(
        actual_source_kind=ACTUAL_SOURCE_RAW_OME,
        channel_name=channel_name,
        source_shape=[int(arr.shape[0]), int(arr.shape[1])],
        dtype=str(arr.dtype),
        intensity_space=RAW_OME_INTENSITY_SPACE,
        actual_source_path=os.path.abspath(raw_path) if raw_path else None,
        channel_index=channel_index,
        channel_key=channel_name,
        roi_name=roi_name,
        roi_bbox_fullres=list(patch_bbox) if patch_bbox else None,
    )


def resolve_channel_calibration(channel_name, requested_source, *, read_raw,
                                raw_path, channel_index=None, patch_bbox=None,
                                corrected_zarr_path=None, roi_name=None,
                                user_selected=False):
    """Strategy B. Return (source_request, calibration_source_identity).

    Opens the ACTUAL pixel source for the request and grounds the identity in it.
    Corrected request + unavailable channel -> raw fallback, recorded truthfully.
    `read_raw(channel_name)` returns the raw np array (the actually-read pixels).
    """
    if requested_source == REQUESTED_SOURCE_CORRECTED_ZARR:
        array = open_corrected_channel_array(corrected_zarr_path, channel_name, roi_name)
        if array is not None:
            req = make_source_request(channel_name, REQUESTED_SOURCE_CORRECTED_ZARR,
                                      reason=REASON_USER_SELECTED)
            csi = build_corrected_calibration_identity(
                array, corrected_zarr_path, channel_name)
            return req, csi
        # Strategy B: corrected requested but unavailable -> raw, recorded honestly.
        req = make_source_request(channel_name, REQUESTED_SOURCE_CORRECTED_ZARR,
                                  reason=REASON_CORRECTED_FALLBACK)
        raw = read_raw(channel_name)
        csi = build_raw_calibration_identity(
            raw, raw_path, channel_name, channel_index, patch_bbox, roi_name)
        return req, csi

    # raw requested (default)
    reason = REASON_USER_SELECTED if user_selected else REASON_DEFAULT_RAW
    req = make_source_request(channel_name, REQUESTED_SOURCE_RAW_OME, reason=reason)
    raw = read_raw(channel_name)
    csi = build_raw_calibration_identity(
        raw, raw_path, channel_name, channel_index, patch_bbox, roi_name)
    return req, csi


def source_mixture_mode_from_identities(identities):
    """Derive source_mixture_mode from ACTUAL identities (not requests).

    all raw -> homogeneous_raw; all corrected -> homogeneous_corrected; any mix ->
    mixed_raw_corrected. Empty -> None (no markers). A corrected request that fell
    back to raw counts as raw here (its actual_source_kind is raw_ome), so a
    partial fallback correctly yields mixed_raw_corrected.
    """
    kinds = {csi.get("actual_source_kind") for csi in (identities or [])}
    kinds.discard(None)
    if not kinds:
        return None
    if kinds == {ACTUAL_SOURCE_RAW_OME}:
        return SOURCE_MIXTURE_HOMOGENEOUS_RAW
    if kinds == {ACTUAL_SOURCE_CORRECTED_ZARR}:
        return SOURCE_MIXTURE_HOMOGENEOUS_CORRECTED
    return SOURCE_MIXTURE_MIXED
