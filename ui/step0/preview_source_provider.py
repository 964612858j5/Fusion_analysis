"""Step0 PreviewSourceProvider — the single data-access seam for the future
merged Channel Conditioning workspace (v15 Workstream B, step 1).

Contract (agreed 2026-08-29):

- Background correction and channel remap interact LIVE on the same data:
  remap no longer waits for a background-correction Save — the corrected
  stage is served from the in-memory preview results (``page._preview_cache``)
  computed with the current per-channel method/params.
- Both sides' current state is mutually visible through :meth:`describe`.
- ``region=None`` means "the current patch" — the only region today. The
  signature already carries ``region`` so Workstream C can later swap this
  provider for a viewport tile provider without touching the UI.

Stages (explicit pipeline order):

    raw -> corrected (optional, live preview) -> remapped (display transform)

The remapped stage runs the PRODUCTION algorithm
(:func:`core.channel_remap.apply_channel_remap`) on the corrected stage —
preview is a local execution of the production pipeline, never a separate
display filter.
"""

from PyQt5.QtCore import QObject, pyqtSignal

import numpy as np

from ...core.channel_remap import apply_channel_remap

STAGE_RAW = "raw"
STAGE_CORRECTED = "corrected"
STAGE_REMAPPED = "remapped"

# Methods with a single corrected array in the preview payload.
_PAYLOAD_KEY = {"tophat": "tophat_raw", "cucim": "cucim_raw"}


class Step0PreviewSourceProvider(QObject):
    """Serves per-channel pixels for a pipeline stage + mutual-state summary.

    Signals
    -------
    stage_invalidated(str channel, str stage):
        The named stage of a channel changed (new preview computed, method or
        params changed). Consumers (e.g. the remap workbench) should drop
        their cached pixels for that channel and re-pull.
    """

    stage_invalidated = pyqtSignal(str, str)

    def __init__(self, page):
        super().__init__(page)
        self._page = page

    # ── pixels ────────────────────────────────────────────────────────────
    def get_pixels(self, channel, stage=STAGE_CORRECTED, region=None):
        """Return float32 pixels of `channel` at `stage` for `region`.

        region=None -> current patch. Falls back down the pipeline when a
        stage is not available (corrected without a live preview -> raw), so
        callers always get the best currently-true data, never stale saved
        output.
        """
        if region is not None:
            raise NotImplementedError(
                "viewport regions arrive with the v15 viewer foundation; "
                "only the current patch (region=None) is supported")
        if stage == STAGE_RAW:
            return self._raw(channel)
        if stage == STAGE_CORRECTED:
            return self._corrected(channel)
        if stage == STAGE_REMAPPED:
            corrected = self._corrected(channel)
            if corrected is None:
                return None
            return apply_channel_remap(corrected, self._remap_params(channel))
        raise ValueError(f"unknown stage: {stage}")

    def _raw(self, channel):
        page = self._page
        cached = page._preload_cache.get(page.current_patch_idx, {}).get(channel)
        if cached is not None:
            return cached
        return page._read_cond_patch_channel(channel, normalize=False)

    def _corrected(self, channel):
        """Live corrected pixels: the in-memory preview result for the
        channel's CURRENT single method — no Save required. Channels without
        a single-method assignment (unassigned / original / both) or without
        a computed preview fall back to raw."""
        page = self._page
        method = self.active_method(channel)
        key = _PAYLOAD_KEY.get(method)
        if key:
            payload = page._preview_cache.get(
                (channel, page.current_patch_idx)) or {}
            arr = payload.get(key)
            if arr is not None:
                return np.asarray(arr, dtype=np.float32)
        return self._raw(channel)

    # ── mutual state visibility ─────────────────────────────────────────────
    def active_method(self, channel):
        """The channel's current single correction method, or None."""
        page = self._page
        if channel == page.nucleus_channel:
            return None
        m = (page._channel_methods.get(channel)
             or page._channel_decisions.get(channel))
        return m if m in _PAYLOAD_KEY else None

    def _remap_params(self, channel):
        wb = getattr(self._page, "_cond_workbench", None)
        if wb is None:
            return None
        return wb._params.get(channel)

    def describe(self, channel):
        """One dict both sides can read: what correction AND remap currently
        think about this channel (live, unsaved state included)."""
        page = self._page
        method = self.active_method(channel)
        payload_ready = bool(
            method and (page._preview_cache.get(
                (channel, page.current_patch_idx)) or {}).get(
                _PAYLOAD_KEY[method]) is not None)
        tr, cs = None, None
        cp = page._channel_params.get(channel)
        if cp:
            tr, cs = cp.get("tophat_radius"), cp.get("cucim_sigma")
        remap = self._remap_params(channel) or {}
        return {
            "channel": channel,
            "correction": {
                "assigned_method": page._channel_decisions.get(channel),
                "preview_method": page._channel_methods.get(channel),
                "active_method": method,
                "params": {"tophat_radius": tr, "cucim_sigma": cs},
                "preview_computed": payload_ready,
                "saved": channel in getattr(page, "_computed_channels", set()),
            },
            "remap": {
                "min": remap.get("min"),
                "max": remap.get("max"),
                "gamma": remap.get("gamma"),
                "user_adjusted": bool(
                    getattr(getattr(page, "_cond_workbench", None),
                            "_user_adjusted", {}).get(channel)),
            },
            "served_corrected_stage": ("corrected_preview" if payload_ready
                                       else "raw"),
        }

    # ── invalidation ─────────────────────────────────────────────────────────
    def invalidate(self, channel, stage=STAGE_CORRECTED):
        """Announce that a stage changed (new preview, method/params edit)."""
        self.stage_invalidated.emit(channel, stage)
