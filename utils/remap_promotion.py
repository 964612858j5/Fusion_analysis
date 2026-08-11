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


def project_marker_only_config(saved_config, selected_channels):
    """Project a full Step0 remap config down to the SELECTED marker channels.

    v14.5d Workstream A. The saved Step0 config carries every channel (incl the DAPI
    reference layer, which Step1 remap + the DAPI-input-zarr cache depend on — it is
    NEVER stripped at save). Step2 marker promotion must run on a marker-only view:
    the selected HQ2/CSD marker channels minus reference layers (DAPI/mask/fusion).

    Two invariants:
      1. Coverage (no silent drop): every selected non-reference marker MUST be present
         in the saved config. `selected_non_reference − saved_channels ≠ ∅` → refuse with
         an explicit 'uncovered-marker' reason (an intersection would silently drop it).
      2. Mixture recompute: the projection drops any full-config top-level
         source_mixture_mode (a 29-channel 'mixed' value must not leak onto a homogeneous
         selected subset). `intended_source_mixture_mode` is derived here from the
         selected markers' recorded actual_source_kind (advisory); the AUTHORITATIVE
         mixture is recomputed by the 5c.2 resolver over the same selected set.

    Returns (projected_config | None, report). Pure — no file/pixel reads.
    """
    from .channel_remap_config import _coerce_channel_list, _is_reference_channel_name
    from .source_identity import (
        SOURCE_MIXTURE_HOMOGENEOUS_RAW, SOURCE_MIXTURE_HOMOGENEOUS_CORRECTED,
        SOURCE_MIXTURE_MIXED)

    report = {"projected": False, "failures": [], "marker_channels": [],
              "intended_source_mixture_mode": None}
    saved_channels = (saved_config or {}).get("channels", {}) or {}
    selected = _coerce_channel_list(selected_channels)
    non_ref = [c for c in selected if not _is_reference_channel_name(c)]
    if not non_ref:
        report["failures"].append(
            "no non-reference marker channels selected (only reference layers "
            "DAPI/mask/fusion, or empty selection)")
        return None, report
    uncovered = [c for c in non_ref if c not in saved_channels]
    if uncovered:
        report["failures"].append(
            "uncovered-marker: selected marker(s) not in the saved Step0 remap config: "
            + ", ".join(uncovered))
        return None, report

    marker_channels = non_ref
    projected = copy.deepcopy(saved_config)
    projected["channels"] = {c: copy.deepcopy(saved_channels[c]) for c in marker_channels}
    # Invariant 2: drop any stale full-config mixture; recomputed per selected marker.
    projected.pop("source_mixture_mode", None)
    sp = projected.get("source_policy")
    if isinstance(sp, dict):
        sp.pop("source_mixture_mode", None)

    kinds = set()
    for c in marker_channels:
        csi = (saved_channels[c] or {}).get("calibration_source_identity")
        kinds.add(csi.get("actual_source_kind") if isinstance(csi, dict) else None)
    if not kinds or None in kinds:
        intended = None            # a marker lacks recorded identity — promotion refuses
    elif kinds == {"raw_ome"}:
        intended = SOURCE_MIXTURE_HOMOGENEOUS_RAW
    elif kinds == {"corrected_zarr"}:
        intended = SOURCE_MIXTURE_HOMOGENEOUS_CORRECTED
    else:
        intended = SOURCE_MIXTURE_MIXED

    report["projected"] = True
    report["marker_channels"] = list(marker_channels)
    report["intended_source_mixture_mode"] = intended
    return projected, report


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


# ── v14.5c.3 source-aware per-channel promotion (PARALLEL path; no runtime) ──
# Entered only for config_is_source_aware(config). Reads the config's per-channel
# recorded calibration_source_identity (v14.5b), gets an INDEPENDENT per-channel
# resolution from the 5c.2 resolver, and cross-validates per channel on TWO
# separate axes:
#   IDENTITY (recorded ↔ resolved): kind, source_path, intensity_space,
#       channel_index, corrected ROI/group. The recorded patch source_shape is a
#       calibration WINDOW (e.g. [64,64]) and is NEVER a geometry gate (Q3 trap).
#   GEOMETRY (resolved ↔ runtime): resolved channel_shape == step2_input_shape,
#       per channel and all equal.
# No-partial: any channel failing identity/geometry, or unknown geometry, refuses
# the WHOLE promotion. Output is a source-aware promoted CANDIDATE with
# step2_ready=FALSE (runtime is v14.5d); this path NEVER emits step2_ready=true.
# Independent: imports the 5c.2 resolver, NOT utils/calibration_source.
from .source_identity import (
    config_is_source_aware,
    RAW_OME_INTENSITY_SPACE,
    DEFAULT_CAMP_SOURCE_POLICY,
    REQUESTED_SOURCE_RAW_OME,
    REQUESTED_SOURCE_CORRECTED_ZARR,
)

SOURCE_AWARE_PROMOTION_CREATED_BY = "step0_source_aware_per_channel_promotion"
SOURCE_ALIGNMENT_PER_CHANNEL_NATIVE = "per_channel_native"


def _recorded_channel_identity(params):
    """Recorded per-channel identity from the config (v14.5b). None if absent.

    source_shape (patch window) is intentionally NOT returned for the geometry
    gate — only identity fields are compared on the identity axis."""
    csi = params.get("calibration_source_identity")
    if not isinstance(csi, dict):
        return None
    return {
        "kind": csi.get("actual_source_kind"),
        "source_path": csi.get("actual_source_path"),
        "intensity_space": csi.get("intensity_space"),
        "channel_index": csi.get("channel_index"),
        "roi_name": csi.get("roi_name"),
    }


def _resolved_channel_identity(resolved_src, channel):
    """Identity extracted INDEPENDENTLY from the 5c.2 resolved source object.

    For corrected, reads the per-array v14.5a attrs (intensity_space/channel_index)
    the same place recorded did, so the identity compare is apples-to-apples; for
    raw, the loader's ch_map index + the native intensity space."""
    kind = str(getattr(resolved_src, "kind", ""))
    source_path = str(getattr(resolved_src, "source_path", ""))
    group = getattr(resolved_src, "group", None)
    group_attrs = dict(getattr(resolved_src, "group_attrs", {}) or {})
    intensity_space = None
    channel_index = None
    roi_name = None
    if kind == "raw_ome":
        intensity_space = RAW_OME_INTENSITY_SPACE
        loader = group.get("loader") if isinstance(group, dict) else None
        cmap = getattr(loader, "ch_map", None)
        if isinstance(cmap, dict):
            channel_index = cmap.get(channel)
    elif kind == "corrected_zarr":
        roi_name = group_attrs.get("roi_name")
        try:
            arr_attrs = dict(group[channel].attrs)
        except Exception:
            arr_attrs = {}
        intensity_space = arr_attrs.get("intensity_space")
        channel_index = arr_attrs.get("channel_index")
    return {
        "kind": kind,
        "source_path": source_path,
        "intensity_space": intensity_space,
        "channel_index": channel_index,
        "roi_name": roi_name,
        "group_name": str(getattr(resolved_src, "group_name", "")),
    }


def promote_source_aware_config_for_step2(preview_config, per_channel_resolved,
                                          step2_input_shape, active_method=None):
    """Source-aware per-channel promotion. Returns (candidate | None, report).

    preview_config        : a v14.5b source-aware preview config (per-channel
                            calibration_source_identity).
    per_channel_resolved  : a PerChannelResolvedSource from the 5c.2 resolver
                            (built by the caller with allow_corrected_to_raw_
                            fallback=False), or None to signal resolution failed.
    step2_input_shape     : runtime geometry [H,W] (None => refuse).
    The candidate is NOT step2_ready (runtime is v14.5d).
    """
    report = {"promoted": False, "failures": [], "channels_checked": [],
              "checks": {}, "source_aware": True}

    if active_method is not None and not is_hq2_csd_method(active_method):
        report["failures"].append(NON_HQ2_CSD_REFUSAL)
        report["active_method"] = active_method
        return None, report

    cfg = normalize_channel_remap_config(preview_config)
    if not config_is_source_aware(cfg):
        report["failures"].append(
            "config is not source-aware; use single-source promotion")
        return None, report

    channels = cfg.get("channels", {}) or {}
    if not channels:
        report["failures"].append("config has no marker channels")
        return None, report
    if per_channel_resolved is None:
        report["failures"].append(
            "per-channel resolution failed (no PerChannelResolvedSource); refusing")
        return None, report

    step2_hw = _as_hw(step2_input_shape)
    if step2_hw is None:
        report["failures"].append(
            "step2_input_shape is unknown/underivable; refusing (the geometry "
            "guard is never skipped — unknown geometry defaults to refuse)")

    per_channel = getattr(per_channel_resolved, "per_channel", {}) or {}
    corrected_group_names = set()

    for ch, params in channels.items():
        report["channels_checked"].append(ch)
        recorded = _recorded_channel_identity(params)
        if recorded is None:
            report["failures"].append(
                f"channel '{ch}': missing recorded calibration_source_identity "
                "(no-partial: every conditioned channel must carry identity)")
            continue
        if ch not in per_channel:
            report["failures"].append(
                f"channel '{ch}': missing from per-channel resolved source "
                "(resolution incomplete); refusing")
            continue
        resolved_src = per_channel[ch]
        resolved = _resolved_channel_identity(resolved_src, ch)

        # ── IDENTITY axis (recorded ↔ resolved) ── (NO patch source_shape gate) ──
        if recorded["kind"] != resolved["kind"]:
            report["failures"].append(
                f"channel '{ch}': recorded kind '{recorded['kind']}' != "
                f"resolved kind '{resolved['kind']}'")
        if _canon(recorded["source_path"]) != _canon(resolved["source_path"]):
            report["failures"].append(
                f"channel '{ch}': recorded source_path != resolved source_path "
                f"({_canon(recorded['source_path'])} vs {_canon(resolved['source_path'])})")
        ri, vi = recorded.get("intensity_space"), resolved.get("intensity_space")
        if ri and vi and str(ri) != str(vi):
            report["failures"].append(
                f"channel '{ch}': recorded intensity_space '{ri}' != resolved '{vi}'")
        rci, vci = recorded.get("channel_index"), resolved.get("channel_index")
        if rci is not None and vci is not None and int(rci) != int(vci):
            report["failures"].append(
                f"channel '{ch}': recorded channel_index {rci} != resolved {vci}")
        if resolved["kind"] == "corrected_zarr":
            rrn, vrn = recorded.get("roi_name"), resolved.get("roi_name")
            if rrn and vrn and str(rrn) != str(vrn):
                report["failures"].append(
                    f"channel '{ch}': recorded roi_name '{rrn}' != resolved "
                    f"corrected group roi_name '{vrn}'")
            corrected_group_names.add(resolved.get("group_name"))

        # ── GEOMETRY axis (resolved channel_shape ↔ step2_input_shape) ──
        res_shape = _as_hw(resolved_src.channel_shape(ch))
        report["checks"].setdefault("resolved_channel_shape", {})[ch] = res_shape
        if res_shape is None:
            report["failures"].append(
                f"channel '{ch}': could not read resolved channel geometry; refusing")
        elif step2_hw is not None and res_shape != step2_hw:
            report["failures"].append(
                f"channel '{ch}': geometry guard — resolved channel_shape "
                f"{res_shape} != step2_input_shape {step2_hw}")

    # ── corrected channels must share ONE ROI group (no mixed coordinate frame) ──
    corrected_group_names.discard(None)
    corrected_group_names.discard("")
    if len(corrected_group_names) > 1:
        report["failures"].append(
            "corrected channels resolve to different ROI groups "
            f"{sorted(corrected_group_names)}; refusing (mixed coordinate frames)")

    report["checks"]["step2_input_shape"] = step2_hw

    if report["failures"]:
        return None, report

    # ── SUCCESS: build the source-aware promoted CANDIDATE (step2_ready=FALSE) ──
    promoted = copy.deepcopy(cfg)
    mixture = getattr(per_channel_resolved, "source_mixture_mode", None)
    sp = dict(promoted.get("source_policy", {}) or {})
    sp.update({
        "preview_only": False,
        "step2_ready": False,                              # runtime is v14.5d
        "source_alignment_mode": SOURCE_ALIGNMENT_PER_CHANNEL_NATIVE,  # ALWAYS
        "calibration_source_matches_step2": True,
        "scope": "step0_source_aware_per_channel",
    })
    promoted["source_policy"] = sp
    promoted["source_mixture_mode"] = mixture              # SEPARATE axis
    promoted["created_by_source_aware_promotion"] = True
    promoted["source_aware_promotion_ready"] = True
    promoted["runtime_supported"] = False
    promoted["created_by"] = SOURCE_AWARE_PROMOTION_CREATED_BY
    promoted.setdefault("camp_source_policy", DEFAULT_CAMP_SOURCE_POLICY)

    for ch, params in promoted["channels"].items():
        resolved = _resolved_channel_identity(per_channel[ch], ch)
        params["step2_compatible"] = True
        params["calibration_source_matches_step2"] = True
        if resolved.get("intensity_space"):
            params["intensity_space"] = resolved["intensity_space"]
            params["step2_pre_remap_source"] = resolved["intensity_space"]
        params["resolved_source_kind"] = resolved["kind"]
        params["resolved_source_path"] = resolved["source_path"]
        params["resolved_group_name"] = resolved.get("group_name")
        params["resolved_source_shape"] = _as_hw(per_channel[ch].channel_shape(ch))

    report["promoted"] = True
    report["source_alignment_mode"] = SOURCE_ALIGNMENT_PER_CHANNEL_NATIVE
    report["source_mixture_mode"] = mixture
    return promoted, report


def promote_source_aware_from_sources(preview_config, *, step2_input_shape,
                                      raw_channel_source_path="",
                                      corrected_zarr_path="", roi_id="",
                                      requested_roi_names=None, abs_fn=None,
                                      loader_factory=None, active_method=None):
    """CLI/caller entry: derive per-channel requests from RECORDED kinds, run the
    INDEPENDENT 5c.2 resolver (allow_corrected_to_raw_fallback=False), then
    cross-validate. A PerChannelResolutionError (recorded=corrected but corrected
    unresolvable at promotion) is a whole-promotion refusal — NO raw substitution.
    """
    from ..workers.hq_source_resolver import (
        resolve_per_channel_marker_sources, PerChannelResolutionError)

    report = {"promoted": False, "failures": [], "source_aware": True}
    cfg = normalize_channel_remap_config(preview_config)
    channels = cfg.get("channels", {}) or {}
    if not channels:
        report["failures"].append("config has no marker channels")
        return None, report

    requests = {}
    for ch, params in channels.items():
        csi = params.get("calibration_source_identity")
        kind = csi.get("actual_source_kind") if isinstance(csi, dict) else None
        if kind == "corrected_zarr":
            requests[ch] = REQUESTED_SOURCE_CORRECTED_ZARR
        elif kind == "raw_ome":
            requests[ch] = REQUESTED_SOURCE_RAW_OME
        else:
            report["failures"].append(
                f"channel '{ch}': missing recorded calibration_source_identity")
    if report["failures"]:
        return None, report

    try:
        per_channel_resolved = resolve_per_channel_marker_sources(
            requests, raw_channel_source_path=raw_channel_source_path,
            corrected_zarr_path=corrected_zarr_path, roi_id=roi_id,
            requested_roi_names=requested_roi_names, abs_fn=abs_fn,
            loader_factory=loader_factory, allow_corrected_to_raw_fallback=False)
    except PerChannelResolutionError as exc:
        report["failures"].append(
            f"channel '{exc.channel}': recorded source could not be independently "
            f"re-resolved (requested {exc.requested_source}): {exc.reason}")
        return None, report

    return promote_source_aware_config_for_step2(
        cfg, per_channel_resolved, step2_input_shape, active_method=active_method)
