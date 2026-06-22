"""block01/workers/hq_source_resolver.py — shared HQ marker source resolver.

Single source of truth for Step2's HQ marker source DECISION (Phase 2.1c-a).

Step2's true HQ source resolution is NOT just "open the corrected zarr". It is a
two-step decision that historically lived split across
``SegmentMergeWorker._open_hq_channel_group`` (initial corrected-vs-raw choice +
ROI-group match) and ``SegmentMergeWorker._validate_hq_config`` (the
"corrected present but missing a requested channel; raw OME has all of them ->
fall back the WHOLE source to raw OME" rule).

This module extracts that combined decision into a pure function so the future
promotion phase (2.1c-b) reuses Step2's REAL resolution instead of a parallel
reimplementation. It is behavior-preserving: ``SegmentMergeWorker`` delegates the
DECISION here and keeps all of its own side effects (seg_config mutation,
validate_hq_channels raises, [Step2-HQ] debug prints, _hq_resolved_source_path).

Purity: the resolver reads files (opens the corrected zarr read-only, constructs
the raw OME loader to read channel names/shapes) but mutates nothing and has no
side effects beyond those reads. Config mutation and raising stay in the worker.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import zarr

from .hq_marker_segmentation import resolve_hq_channels

# Intensity-space label for a raw OME native-float source. This already exists in
# the v13.1 channel-remap schema; we never invent a new corrected-source label.
RAW_OME_INTENSITY_SPACE = "raw_ome_native_float"
UNKNOWN_INTENSITY_SPACE = "unknown"

# ── source_mode: a structural property of the active Step2 ALGORITHM ──────────
# (2.1c-b.1) The caller decides this from the algorithm family, never from any
# config-declared field. It is NOT something a config self-asserts.
SOURCE_MODE_CORRECTED_THEN_RAW = "corrected_then_raw_fallback"  # DEFAULT (non-HQ2/CSD, legacy)
SOURCE_MODE_RAW_ONLY = "raw_ome_only"                           # HQ2/CSD manual-remap default
SOURCE_MODE_CORRECTED_ONLY = "corrected_zarr_only"             # RESERVED — raises if called
_VALID_SOURCE_MODES = {
    SOURCE_MODE_CORRECTED_THEN_RAW, SOURCE_MODE_RAW_ONLY, SOURCE_MODE_CORRECTED_ONLY,
}


# ── group / channel-name helpers (the single definitions; the worker delegates) ──

def group_keys(root):
    groups = list(getattr(root, "group_keys", lambda: [])())
    if not groups and hasattr(root, "groups"):
        groups = [name for name, _group in root.groups()]
    return groups


def channel_array_names(group):
    """Channel names for a resolved HQ source (zarr group or raw_ome dict)."""
    if isinstance(group, dict) and group.get("kind") == "raw_ome":
        return group["loader"].channel_names()
    if hasattr(group, "array_keys"):
        return list(group.array_keys())
    return [k for k in group.keys() if hasattr(group[k], "shape")]


def available_groups_debug(root):
    debug = []
    for group_name in group_keys(root):
        group = root[group_name]
        debug.append(
            {
                "group": group_name,
                "attrs": dict(group.attrs),
                "channels": channel_array_names(group),
            }
        )
    return debug


def _channel_shape(group, name):
    """(H, W) of a channel in the resolved source, or None if unavailable."""
    try:
        if isinstance(group, dict) and group.get("kind") == "raw_ome":
            shape = tuple(getattr(group["loader"], "shape", ()) or ())
        else:
            shape = tuple(getattr(group[name], "shape", ()) or ())
    except Exception:
        return None
    if len(shape) >= 2:
        return (int(shape[-2]), int(shape[-1]))
    return None


@dataclass
class ResolvedHQSource:
    """Final HQ marker source decision (the corrected-vs-raw + fallback result)."""

    kind: str                       # "corrected_zarr" | "raw_ome"
    source_path: str                # absolute resolved path
    intensity_space: str            # raw_ome -> raw_ome_native_float; else metadata or "unknown"
    available_channels: list        # channel names present in the resolved source
    resolved_channels: list         # requested channels resolved against the source
    missing_channels: list          # requested channels still missing after corrected->raw decision
    fell_back_to_raw: bool          # corrected chosen first, then fell back to raw OME
    group: object                   # the live handle the worker reads from (zarr group | raw_ome dict)
    group_name: str = ""            # selected group name ("raw_ome" or zarr group name / root)
    group_attrs: dict = field(default_factory=dict)
    root_attrs: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    fallback_error: str = ""        # non-empty if raw-fallback inspection raised (swallowed, as before)
    source_mode: str = SOURCE_MODE_CORRECTED_THEN_RAW  # the policy this resolution ran under
    source_selected_by: str = ""    # "policy" (raw_ome_only) | "fallback" | "corrected_primary" | "raw_primary"
    _channel_shapes: dict = field(default_factory=dict)

    def channel_shape(self, name):
        if name in self._channel_shapes:
            return self._channel_shapes[name]
        shp = _channel_shape(self.group, name)
        self._channel_shapes[name] = shp
        return shp


def _intensity_space_for(kind, source_path, root_attrs):
    """Conservative intensity-space label.

    raw_ome -> the established native-float label. corrected_zarr -> ONLY an
    explicit value recorded in the source's own metadata; never a guessed native
    label. If undeterminable -> "unknown".
    """
    if kind == "raw_ome":
        return RAW_OME_INTENSITY_SPACE
    space = root_attrs.get("intensity_space") if isinstance(root_attrs, dict) else None
    if isinstance(space, str) and space.strip():
        return space.strip()
    return UNKNOWN_INTENSITY_SPACE


def open_hq_channel_group(*, multichannel_source_path, raw_channel_source_path,
                          roi_id, requested_roi_names, param_file,
                          abs_fn, loader_factory):
    """Step A only: corrected-vs-raw initial choice + ROI-group match.

    Pure (reads files only). Returns ``(group, kind, source_path)`` and raises the
    SAME FileNotFoundError / ValueError the worker raised before. No missing-channel
    fallback here — that is Step B in :func:`resolve_hq_marker_source`. Kept separate
    so non-HQ callers (e.g. Mesmer) reuse the identical initial choice.
    """
    path = multichannel_source_path
    if not path or not os.path.exists(path):
        raw_path = raw_channel_source_path
        if raw_path:
            return ({"kind": "raw_ome", "path": raw_path, "loader": loader_factory(raw_path)},
                    "raw_ome", raw_path)
        raise FileNotFoundError(
            "Cellpose nuclei + HQ requires corrected_channels.zarr or raw OME channel source, but neither was found.\n"
            f"Requested HQ source: {path or '(none)'}\n"
            f"Resolved raw source: {raw_path or '(none)'}\n"
            f"loaded param_file path: {param_file or '(none)'}"
        )
    source_path = abs_fn(path)
    root = zarr.open(path, mode="r")
    mode = str(root.attrs.get("mode", "")).strip().lower()
    if mode == "roi_only":
        groups = group_keys(root)
        requested_roi_id = str(roi_id or "")
        requested_names = {str(n) for n in (requested_roi_names or set()) if str(n or "").strip()}
        for group_name in groups:
            group = root[group_name]
            if requested_roi_id and str(group.attrs.get("roi_id") or "") == requested_roi_id:
                return group, "corrected_zarr", source_path
        for group_name in groups:
            group = root[group_name]
            group_names = {
                str(group_name),
                str(group.attrs.get("roi_name") or ""),
                str(group.attrs.get("display_name") or ""),
                str(group.attrs.get("roi_display_name") or ""),
            }
            if requested_names and requested_names & {name for name in group_names if name}:
                return group, "corrected_zarr", source_path
        raise ValueError(
            "Could not match ROI group in corrected_channels.zarr for HQ segmentation.\n"
            f"Found corrected_channels.zarr at: {path}\n"
            f"Requested ROI id: {requested_roi_id or '(none)'}\n"
            f"Requested ROI name(s): {sorted(requested_names) if requested_names else '(none)'}\n"
            f"Available groups: {groups}\n"
            f"Available channels per group: {json.dumps(available_groups_debug(root), indent=2, default=str)}"
        )
    return root, "corrected_zarr", source_path


def _resolve_raw_ome_only(requested, raw_channel_source_path, param_file,
                          abs_fn, loader_factory):
    """raw_ome_only (HQ2/CSD policy): resolve raw OME directly, never touch corrected.

    corrected_channels.zarr is NOT opened or inspected. If raw cannot satisfy the
    requested channels, the missing channels are reported (worker raises as today);
    there is NO fallback to corrected. fell_back_to_raw stays False — raw is chosen
    by policy, recorded as source_selected_by="policy".
    """
    if not raw_channel_source_path:
        raise FileNotFoundError(
            "HQ2/CSD manual-remap requires a raw OME channel source (source_mode="
            "raw_ome_only), but none was found.\n"
            f"loaded param_file path: {param_file or '(none)'}")
    loader = loader_factory(raw_channel_source_path)
    group = {"kind": "raw_ome", "path": raw_channel_source_path, "loader": loader}
    available = channel_array_names(group)
    resolved, missing, warnings = resolve_hq_channels(requested, available)
    out = ResolvedHQSource(
        kind="raw_ome",
        source_path=abs_fn(raw_channel_source_path),
        intensity_space=RAW_OME_INTENSITY_SPACE,
        available_channels=list(available),
        resolved_channels=list(resolved),
        missing_channels=list(missing),
        fell_back_to_raw=False,
        group=group,
        group_name="raw_ome",
        group_attrs={},
        root_attrs={},
        warnings=list(warnings),
        fallback_error="",
        source_mode=SOURCE_MODE_RAW_ONLY,
        source_selected_by="policy",
    )
    for ch in resolved:
        out._channel_shapes[ch] = _channel_shape(group, ch)
    return out


def resolve_hq_marker_source(*, requested_channels, multichannel_source_path,
                             raw_channel_source_path, roi_id, requested_roi_names,
                             param_file="", abs_fn=None, loader_factory=None,
                             source_mode=SOURCE_MODE_CORRECTED_THEN_RAW):
    """Resolve the final HQ marker source under the given ``source_mode``.

    ``source_mode`` is a structural property of the active Step2 algorithm, decided
    by the caller (never from a config-declared field):

      - corrected_then_raw_fallback (DEFAULT): Step A initial corrected-vs-raw
        choice + Step B corrected-missing-channel -> raw whole-source fallback.
        Byte-for-byte the 2.1c-a behavior.
      - raw_ome_only: resolve raw OME directly; corrected is never opened; no
        fallback to corrected; fell_back_to_raw=False, source_selected_by="policy".
      - corrected_zarr_only: RESERVED — raises NotImplementedError (no partial
        implementation; no automatic caller selects it in this phase).

    ``requested_channels`` is required: source identity cannot be resolved without it.
    """
    if source_mode not in _VALID_SOURCE_MODES:
        raise ValueError(f"unknown source_mode {source_mode!r}; "
                         f"valid: {sorted(_VALID_SOURCE_MODES)}")
    if requested_channels is None:
        raise ValueError(
            "resolve_hq_marker_source requires requested_channels; HQ source "
            "identity cannot be resolved without the requested channel list")
    if abs_fn is None:
        abs_fn = os.path.abspath
    if loader_factory is None:  # pragma: no cover - worker always injects OMETIFFLoader
        from ..core.io_loader import OMETIFFLoader
        loader_factory = OMETIFFLoader

    requested = [str(ch).strip() for ch in requested_channels if str(ch).strip()]

    if source_mode == SOURCE_MODE_CORRECTED_ONLY:
        raise NotImplementedError(
            "source_mode='corrected_zarr_only' is reserved for a future explicit "
            "HQ2/CSD mode in which Step1.5 calibrates on the SAME corrected source "
            "Step2 reads. It is not implemented; no automatic path selects it.")

    if source_mode == SOURCE_MODE_RAW_ONLY:
        return _resolve_raw_ome_only(
            requested, raw_channel_source_path, param_file, abs_fn, loader_factory)

    # ── corrected_then_raw_fallback (default, legacy) ──
    # ── Step A ──
    group, kind, source_path = open_hq_channel_group(
        multichannel_source_path=multichannel_source_path,
        raw_channel_source_path=raw_channel_source_path,
        roi_id=roi_id, requested_roi_names=requested_roi_names,
        param_file=param_file, abs_fn=abs_fn, loader_factory=loader_factory)

    available = channel_array_names(group)
    resolved, missing, warnings = resolve_hq_channels(requested, available)

    # ── Step B: corrected missing a requested channel -> whole-source raw fallback ──
    fell_back = False
    fallback_error = ""
    if missing and kind != "raw_ome" and raw_channel_source_path:
        try:
            raw_loader = loader_factory(raw_channel_source_path)
            raw_available = raw_loader.channel_names()
            raw_resolved, raw_missing, raw_warnings = resolve_hq_channels(requested, raw_available)
            if not raw_missing:
                group = {"kind": "raw_ome", "path": raw_channel_source_path, "loader": raw_loader}
                kind = "raw_ome"
                available = raw_available
                source_path = abs_fn(raw_channel_source_path)
                resolved, missing, warnings = raw_resolved, raw_missing, raw_warnings
                fell_back = True
            # else: raw also missing -> keep corrected + its missing; worker raises.
        except Exception as exc:  # swallowed exactly as the worker did, signalled out
            fallback_error = f"{exc}"

    root_attrs = {}
    if source_path and os.path.exists(source_path) and str(source_path).endswith(".zarr"):
        try:
            root_attrs = dict(zarr.open(source_path, mode="r").attrs)
        except Exception:
            root_attrs = {}
    group_name = "raw_ome" if isinstance(group, dict) else (getattr(group, "name", "") or "")
    group_attrs = {} if isinstance(group, dict) else dict(getattr(group, "attrs", {}))
    if fell_back:
        selected_by = "fallback"
    elif kind == "raw_ome":
        selected_by = "raw_primary"   # corrected absent -> raw chosen as initial source
    else:
        selected_by = "corrected_primary"

    out = ResolvedHQSource(
        kind=kind,
        source_path=source_path,
        intensity_space=_intensity_space_for(kind, source_path, root_attrs),
        available_channels=list(available),
        resolved_channels=list(resolved),
        missing_channels=list(missing),
        fell_back_to_raw=fell_back,
        group=group,
        group_name=group_name,
        group_attrs=group_attrs,
        root_attrs=root_attrs,
        warnings=list(warnings),
        fallback_error=fallback_error,
        source_mode=SOURCE_MODE_CORRECTED_THEN_RAW,
        source_selected_by=selected_by,
    )
    for ch in resolved:
        out._channel_shapes[ch] = _channel_shape(group, ch)
    return out


# ── v14.5c.1 per-channel source-map container (PURE STRUCTURE) ───────────────
# Additive only: ResolvedHQSource and resolve_hq_marker_source above are
# UNCHANGED. This container wraps a {channel -> ResolvedHQSource} map for the
# future source-aware path (5c.2 resolution, 5c.3 promotion). It performs NO
# resolution, NO promotion, NO pixel reads. The single-source path is the
# unchanged legacy code; this is a parallel structure entered only when a config
# is source-aware. source_mixture_mode reuses the v14.5a vocabulary.
from ..utils.source_identity import (  # noqa: E402  (additive, end-of-module)
    SOURCE_MIXTURE_HOMOGENEOUS_RAW,
    SOURCE_MIXTURE_HOMOGENEOUS_CORRECTED,
    SOURCE_MIXTURE_MIXED,
)


class NotHomogeneousError(ValueError):
    """homogeneous_single() called on a heterogeneous per-channel source map.

    Subclasses ValueError so callers may catch either. Raised when channels
    resolved to differing source identities (kind / source_path / group_name)."""


def _source_mixture_mode_from_kinds(kinds):
    """Derive source_mixture_mode from a set of ResolvedHQSource.kind values."""
    ks = set(kinds)
    if ks == {"raw_ome"}:
        return SOURCE_MIXTURE_HOMOGENEOUS_RAW
    if ks == {"corrected_zarr"}:
        return SOURCE_MIXTURE_HOMOGENEOUS_CORRECTED
    return SOURCE_MIXTURE_MIXED


class PerChannelResolvedSource:
    """A {channel -> ResolvedHQSource} map + a derived source_mixture_mode.

    Pure container (v14.5c.1). Each channel carries its OWN ResolvedHQSource; the
    per-channel ResolvedHQSource already exposes its geometry via channel_shape,
    so this container surfaces NO geometry field. When 5c.2/5c.3 add geometry
    they must use the unambiguous names ``resolved_source_shape`` (pixels the
    resolver read) and ``calibration_window_shape`` (v14.5b patch-scale) — never
    a bare ``source_shape``.

    Attributes
    ----------
    per_channel : dict[str, ResolvedHQSource]
    source_mixture_mode : str   # DERIVED from per-channel kinds, never a request
    """

    def __init__(self, per_channel):
        if not per_channel:
            raise ValueError(
                "PerChannelResolvedSource requires at least one channel; an empty "
                "per-channel source map has no defined source_mixture_mode")
        self.per_channel = dict(per_channel)
        self.source_mixture_mode = _source_mixture_mode_from_kinds(
            str(src.kind) for src in self.per_channel.values())

    @staticmethod
    def _identity(src):
        """Source identity tuple: (kind, source_path, group_name). Two corrected
        channels from different ROI groups in the same zarr differ by group_name
        and are therefore NOT the same identity."""
        return (str(getattr(src, "kind", "")),
                str(getattr(src, "source_path", "")),
                str(getattr(src, "group_name", "")))

    def is_homogeneous(self):
        """True iff every channel resolved to the SAME identity (kind AND
        source_path AND group_name) — not merely the same mixture mode."""
        return len({self._identity(s) for s in self.per_channel.values()}) == 1

    def homogeneous_single(self):
        """Return the ONE shared ResolvedHQSource iff the map is identity-
        homogeneous; otherwise raise NotHomogeneousError.

        Homogeneity is identity-based (kind, source_path, group_name all equal),
        NOT mixture_mode-based: two channels both corrected_zarr but from
        different corrected sources / ROI groups are heterogeneous and raise.
        """
        groups = {}
        for ch, src in self.per_channel.items():
            groups.setdefault(self._identity(src), []).append(ch)
        if len(groups) != 1:
            detail = "; ".join(
                f"{ident} -> {sorted(chs)}" for ident, chs in sorted(groups.items()))
            raise NotHomogeneousError(
                "per-channel source map is not homogeneous; differing "
                f"(kind, source_path, group_name) identities: {detail}")
        return next(iter(self.per_channel.values()))


# ── v14.5c.2 per-channel source RESOLVER (resolver only — no promotion) ──────
# resolve_per_channel_marker_sources resolves EACH channel's REQUESTED source
# from pixels, reusing the existing single-source machinery (open_hq_channel_group
# for corrected, the raw_ome_only path for raw) without changing its behavior. It
# does NOT read a config's recorded identity, does NOT do the recorded↔resolved↔
# runtime 3-way, does NOT apply the geometry guard vs step2_input_shape, and does
# NOT emit step2_ready — all of that is v14.5c.3 promotion.
#
# INDEPENDENCE: this resolver does its OWN pixel reads. It does NOT import or call
# the preview reader (utils/calibration_source.py), so recorded↔resolved stays a
# genuine cross-check in 5c.3. It shares only the schema constants + the Q2
# kind/fallback decision helper from utils/source_identity.
from ..utils.source_identity import (  # noqa: E402  (additive, end-of-module)
    decide_source_resolution,
    RESOLUTION_USE_RAW,
    RESOLUTION_USE_CORRECTED,
    REQUESTED_SOURCE_CORRECTED_ZARR,
)


class PerChannelResolutionError(Exception):
    """A requested per-channel source could not be resolved from pixels.

    Mechanism A (no partial): raised as soon as ANY requested source cannot be
    resolved — e.g. a channel that requested corrected_zarr whose corrected source
    cannot be opened while allow_corrected_to_raw_fallback=False (NO silent raw
    substitution). 5c.3 treats this raise as a promotion refusal.
    """

    def __init__(self, channel, requested_source, reason=""):
        self.channel = channel
        self.requested_source = requested_source
        self.reason = reason
        super().__init__(
            f"could not resolve source for channel {channel!r} "
            f"(requested {requested_source!r}): {reason}")


def _root_attrs_for(source_path):
    if source_path and os.path.exists(source_path) and str(source_path).endswith(".zarr"):
        try:
            return dict(zarr.open(source_path, mode="r").attrs)
        except Exception:
            return {}
    return {}


def _build_corrected_resolved(channel, group, source_path, root_attrs, available):
    """Build a corrected_zarr ResolvedHQSource for ONE channel from an already-open
    corrected group. Geometry (resolved_source_shape) is the opened array's real
    shape via the shared _channel_shape; never an attr/request value."""
    group_name = "raw_ome" if isinstance(group, dict) else (getattr(group, "name", "") or "")
    out = ResolvedHQSource(
        kind="corrected_zarr",
        source_path=source_path,
        intensity_space=_intensity_space_for("corrected_zarr", source_path, root_attrs),
        available_channels=list(available),
        resolved_channels=[channel],
        missing_channels=[],
        fell_back_to_raw=False,
        group=group,
        group_name=group_name,
        group_attrs=({} if isinstance(group, dict) else dict(getattr(group, "attrs", {}))),
        root_attrs=dict(root_attrs),
        warnings=[],
        fallback_error="",
        source_mode=SOURCE_MODE_CORRECTED_THEN_RAW,
        source_selected_by="corrected_primary",
    )
    out._channel_shapes[channel] = _channel_shape(group, channel)
    return out


def _build_raw_resolved(channel, raw_loader, raw_source_path, available, abs_fn):
    """Build a raw_ome ResolvedHQSource for ONE channel from an already-open raw
    loader. resolved_source_shape is the loader's real shape via _channel_shape."""
    group = {"kind": "raw_ome", "path": raw_source_path, "loader": raw_loader}
    out = ResolvedHQSource(
        kind="raw_ome",
        source_path=abs_fn(raw_source_path),
        intensity_space=RAW_OME_INTENSITY_SPACE,
        available_channels=list(available),
        resolved_channels=[channel],
        missing_channels=[],
        fell_back_to_raw=False,
        group=group,
        group_name="raw_ome",
        group_attrs={},
        root_attrs={},
        warnings=[],
        fallback_error="",
        source_mode=SOURCE_MODE_RAW_ONLY,
        source_selected_by="policy",
    )
    out._channel_shapes[channel] = _channel_shape(group, channel)
    return out


def resolve_per_channel_marker_sources(per_channel_requests, *,
                                       raw_channel_source_path="",
                                       corrected_zarr_path="",
                                       roi_id="", requested_roi_names=None,
                                       param_file="", abs_fn=None, loader_factory=None,
                                       allow_corrected_to_raw_fallback=False):
    """Resolve EACH channel's REQUESTED source from pixels into a
    PerChannelResolvedSource. Resolver only — no recorded-identity read, no 3-way,
    no geometry guard, no step2_ready.

    per_channel_requests : {channel -> "raw_ome" | "corrected_zarr"}  (SourceRequest)
    Returns a PerChannelResolvedSource iff EVERY channel resolved (mechanism A).
    A channel requesting corrected_zarr that cannot be opened with
    allow_corrected_to_raw_fallback=False raises PerChannelResolutionError — never
    a silent raw substitution.
    """
    if not per_channel_requests:
        raise ValueError(
            "resolve_per_channel_marker_sources requires a non-empty "
            "per_channel_requests map")
    if abs_fn is None:
        abs_fn = os.path.abspath
    if loader_factory is None:  # pragma: no cover - caller injects OMETIFFLoader
        from ..core.io_loader import OMETIFFLoader
        loader_factory = OMETIFFLoader

    # ── open each source AT MOST ONCE (availability + reuse) ──
    corr_group = None
    corr_source_path = ""
    corr_available = set()
    corr_root_attrs = {}
    needs_corrected = any(
        str(r) == REQUESTED_SOURCE_CORRECTED_ZARR for r in per_channel_requests.values())
    if needs_corrected and corrected_zarr_path:
        try:
            g, kind, sp = open_hq_channel_group(
                multichannel_source_path=corrected_zarr_path,
                raw_channel_source_path="",   # no raw fallback at the corrected open
                roi_id=roi_id, requested_roi_names=requested_roi_names,
                param_file=param_file, abs_fn=abs_fn, loader_factory=loader_factory)
            if kind == "corrected_zarr":
                corr_group, corr_source_path = g, sp
                corr_available = set(channel_array_names(g))
                corr_root_attrs = _root_attrs_for(sp)
        except (FileNotFoundError, ValueError):
            corr_group = None   # corrected unavailable / ROI group unmatched

    raw_loader = None
    raw_available = set()
    if raw_channel_source_path:
        try:
            raw_loader = loader_factory(raw_channel_source_path)
            raw_available = set(raw_loader.channel_names())
        except Exception:
            raw_loader = None

    per_channel = {}
    for ch, requested in per_channel_requests.items():
        ch = str(ch)
        requested = str(requested)
        corrected_ok = corr_group is not None and ch in corr_available
        raw_ok = raw_loader is not None and ch in raw_available
        decision = decide_source_resolution(
            requested, corrected_available=corrected_ok, raw_available=raw_ok,
            allow_corrected_to_raw_fallback=allow_corrected_to_raw_fallback)
        if decision == RESOLUTION_USE_CORRECTED:
            per_channel[ch] = _build_corrected_resolved(
                ch, corr_group, corr_source_path, corr_root_attrs, corr_available)
        elif decision == RESOLUTION_USE_RAW:
            per_channel[ch] = _build_raw_resolved(
                ch, raw_loader, raw_channel_source_path, raw_available, abs_fn)
        else:
            reason = (
                f"requested corrected_zarr but corrected source unavailable for "
                f"this channel (corrected_open={corr_group is not None}, "
                f"channel_in_corrected={corrected_ok}, "
                f"allow_corrected_to_raw_fallback={allow_corrected_to_raw_fallback})"
                if requested == REQUESTED_SOURCE_CORRECTED_ZARR
                else f"requested {requested} but that source is unavailable "
                     f"(raw_available={raw_ok})")
            raise PerChannelResolutionError(
                channel=ch, requested_source=requested, reason=reason)

    return PerChannelResolvedSource(per_channel)
