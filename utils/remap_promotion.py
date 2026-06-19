"""block01/utils/remap_promotion.py — 2.1c-b source-identity promotion.

Promote a Step1.5 *preview* remap config to a Step2-ready config ONLY when its
recorded calibration source identity matches the source Step2 will actually
resolve. The match is established by COMPARISON against a ``ResolvedHQSource``
produced by the 2.1c-a shared resolver (``workers.hq_source_resolver
.resolve_hq_marker_source``) — this module never resolves sources itself.

Promotion changes ONLY provenance/alignment metadata. Manual remap transform
values (min/max/brightness/contrast/gamma) are carried over byte-for-byte.

``step2_ready=true`` here means ONLY source path/shape/intensity-space identity is
verified. It does NOT mean the patch-level params are representative of the whole
ROI or AF-validated; the promoted config records
``step2_ready_scope="source_identity_only"`` and
``representativeness_validated=false``.
"""

from __future__ import annotations

import copy
import os

from .channel_remap_config import (
    normalize_channel_remap_config,
    validate_step2_remap_config,
    _NON_STEP2_INTENSITY_SPACES,
)
from .segmentation_config import is_hq2_csd_method

PROMOTION_CREATED_FROM = "step2_1c_source_identity_promotion"
NON_HQ2_CSD_REFUSAL = (
    "Step1.5 Channel Conditioning configs are only valid for HQ2/CSD segmentation.")
REPRESENTATIVENESS_NOTE = (
    "Step2-ready means only that source path, shape, and intensity-space identity "
    "match Step2's resolved HQ source. Patch-to-whole-ROI representativeness and "
    "AF-heavy-region behavior of Min/Max/Gamma are NOT validated and require "
    "separate QC."
)


def _canon(path):
    """Canonicalize a path for comparison: absolute + realpath (resolves symlinks)."""
    if not path:
        return ""
    return os.path.realpath(os.path.abspath(str(path)))


def _as_hw(shape):
    """Coerce a shape to a [H, W] int list, or None if not a valid 2-tuple."""
    if shape is None:
        return None
    try:
        seq = list(shape)
    except TypeError:
        return None
    if len(seq) < 2:
        return None
    try:
        return [int(seq[-2]), int(seq[-1])]
    except (TypeError, ValueError):
        return None


def promote_step1_5_config_for_step2(preview_config, resolved_source,
                                     step2_input_shape, active_method=None):
    """Promote a Step1.5 preview config iff its calibration identity matches Step2.

    Parameters
    ----------
    preview_config : dict
        A Step1.5 preview remap config (raw or normalized). Not mutated.
    resolved_source : ResolvedHQSource
        Result of the 2.1c-a shared resolver run with the ACTIVE Step2 params,
        resolved under the algorithm-derived source_mode (raw_ome_only for HQ2/CSD).
        The caller (CLI) builds this; this function does NOT resolve sources.
    step2_input_shape : sequence | None
        Step2's actual segmentation-input geometry, [H, W]. MUST come from a
        reliable active-Step2 source. If None/underivable, promotion REFUSES
        (the geometry guard is never skipped — unknown geometry => refuse).
    active_method : str | None
        Active Step2 algorithm. When provided and NOT HQ2/CSD-family, promotion
        refuses: Step1.5 Channel Conditioning configs are only valid for HQ2/CSD.
        (2.1c-b.1 — source policy is a structural property of the algorithm.)

    Returns
    -------
    (promoted | None, report)
        promoted : dict — a Step2-ready config (only on success), else None.
        report   : dict — {"promoted": bool, "failures": [...], ...}.
    """
    report = {"promoted": False, "failures": [], "channels_checked": [],
              "checks": {}}

    # ── algorithm gate: Step1.5 remap configs are HQ2/CSD-only ──
    if active_method is not None and not is_hq2_csd_method(active_method):
        report["failures"].append(NON_HQ2_CSD_REFUSAL)
        report["active_method"] = active_method
        return None, report

    cfg = normalize_channel_remap_config(preview_config)
    sp = cfg.get("source_policy", {}) or {}

    # ── preconditions: calibration identity must be present (never inferred) ──
    cal_path = sp.get("calibration_source_path")
    cal_shape = _as_hw(sp.get("calibration_source_shape"))
    cal_ispace = sp.get("calibration_intensity_space")
    cal_kind = sp.get("calibration_source_kind")
    if not cal_path:
        report["failures"].append(
            "preview config has no calibration_source_path; cannot promote "
            "(calibration identity is never inferred from ROI defaults)")
    if cal_shape is None:
        report["failures"].append(
            "preview config has no usable calibration_source_shape; cannot promote")
    if not cal_ispace:
        report["failures"].append(
            "preview config has no calibration_intensity_space; cannot promote")

    # ── geometry guard input: step2_input_shape must be known ──
    step2_hw = _as_hw(step2_input_shape)
    if step2_hw is None:
        report["failures"].append(
            "step2_input_shape is unknown/underivable; refusing (the geometry "
            "guard is never skipped — unknown geometry defaults to refuse)")

    channels = cfg.get("channels", {}) or {}
    if not channels:
        report["failures"].append("preview config has no marker channels")

    if report["failures"]:
        return None, report

    res_ispace = str(getattr(resolved_source, "intensity_space", "unknown"))
    res_kind = str(getattr(resolved_source, "kind", ""))
    res_path = str(getattr(resolved_source, "source_path", ""))
    available = set(getattr(resolved_source, "available_channels", []) or [])

    # ── per-channel checks (1-3) ──
    for name in channels:
        report["channels_checked"].append(name)
        if name not in available:
            report["failures"].append(
                f"channel '{name}' not present in Step2 resolved source "
                f"(available: {sorted(available)})")
        if str(cal_ispace) != res_ispace:
            report["failures"].append(
                f"channel '{name}': calibration intensity_space '{cal_ispace}' != "
                f"resolved source intensity_space '{res_ispace}'")
        if res_ispace in _NON_STEP2_INTENSITY_SPACES:
            report["failures"].append(
                f"channel '{name}': resolved intensity_space '{res_ispace}' is not "
                "a Step2-compatible native source")

    # ── whole-source identity checks (4) ──
    report["checks"]["calibration_source_kind"] = cal_kind
    report["checks"]["resolved_source_kind"] = res_kind
    if str(cal_kind) != res_kind:
        report["failures"].append(
            f"calibration_source_kind '{cal_kind}' != resolved source kind '{res_kind}'")
    if _canon(cal_path) != _canon(res_path):
        report["failures"].append(
            f"calibration_source_path does not match resolved source path "
            f"(calibration={_canon(cal_path)}, resolved={_canon(res_path)})")

    # resolved full/marker source shape (per resolved channel; [H, W])
    res_shape = None
    for name in channels:
        s = _as_hw(resolved_source.channel_shape(name)) if name in available else None
        if s is not None:
            res_shape = s
            break
    report["checks"]["calibration_source_shape"] = cal_shape
    report["checks"]["resolved_source_shape"] = res_shape
    report["checks"]["step2_input_shape"] = step2_hw
    if res_shape is None:
        report["failures"].append(
            "could not read resolved source channel shape; cannot verify geometry")
    else:
        if cal_shape != res_shape:
            report["failures"].append(
                f"calibration_source_shape {cal_shape} != resolved source shape "
                f"{res_shape} (Step1.5 did not calibrate on the same source geometry)")
        # ── geometry guard (5): marker source shape == Step2 input shape ──
        if res_shape != step2_hw:
            report["failures"].append(
                f"geometry guard: resolved marker source shape {res_shape} != "
                f"step2_input_shape {step2_hw} (marker-source vs segmentation-input "
                "geometry mismatch)")

    if report["failures"]:
        return None, report

    # ── build promoted config: provenance only; transform values byte-for-byte ──
    promoted = copy.deepcopy(cfg)
    promoted["used_for"] = "segmentation_only"
    promoted["created_from_step"] = PROMOTION_CREATED_FROM

    promoted["source_policy"] = {
        "source": res_kind,
        "source_path": res_path,
        "intensity_space": res_ispace,
        "normalization": "none",
        "scope": "step2_source_identity",
        "preview_only": False,
        "step2_ready": True,
        "step2_pre_remap_source": res_ispace,
        "calibration_source_matches_step2": True,
        "source_alignment_mode": "single_native_source",
        "source_shape": list(res_shape),
        "step2_input_shape": list(step2_hw),
        "step2_ready_scope": "source_identity_only",
        "representativeness_validated": False,
        "representativeness_note": REPRESENTATIVENESS_NOTE,
        "calibration_patch_bbox": sp.get("calibration_patch_bbox"),
        "calibration_patch_index": sp.get("calibration_patch_index"),
    }

    # AUDIT-ONLY trace (no validator reads this; not evidence of alignment).
    promoted["calibration_source"] = {
        "source_path": cal_path,
        "source_kind": cal_kind,
        "source_shape": list(cal_shape),
        "intensity_space": cal_ispace,
        "patch_bbox": sp.get("calibration_patch_bbox"),
        "patch_index": sp.get("calibration_patch_index"),
    }

    # Per channel: flip provenance to verified-native. min/max/brightness/contrast/
    # gamma are untouched (carried byte-for-byte from the preview config).
    for name, params in promoted["channels"].items():
        params["step2_compatible"] = True
        params["calibration_source_matches_step2"] = True
        params["intensity_space"] = res_ispace
        params["step2_pre_remap_source"] = res_ispace

    # Final self-check: never emit a config that does not pass Step2 validation.
    errors, _ = validate_step2_remap_config(promoted, allow_preview_remap=False)
    if errors:
        report["failures"].extend(
            [f"promoted config failed validate_step2_remap_config: {e}" for e in errors])
        return None, report

    report["promoted"] = True
    report["source_path"] = res_path
    report["source_kind"] = res_kind
    return promoted, report
