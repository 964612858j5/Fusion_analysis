"""Step0 PreviewSourceProvider — the single data-access seam between the
background-correction workspace and the remap workspace (v15, revised).

Contract (revised 2026-08-29 after design review):

- HARD BOUNDARY: remap consumes ONLY the saved corrected artifact. Save is
  the single hand-off point — in-memory correction previews never feed remap,
  so remap Min/Max/Gamma is always calibrated in a stable, identified
  intensity space.
- The corrected stage is whatever the loader serves for a channel:
  * after Save, `loader.set_corrected_zarr_store(...)` makes `read_region`
    return the SAVED corrected pixels for channels with a saved decision;
  * before Save (or for Original channels) it is the raw source, and
    :meth:`describe` / `source_note` say so honestly
    ("raw — background correction not saved").
- Mutual state visibility stays: :meth:`describe` exposes both sides'
  current state (correction method/params/saved + remap min/max/gamma).
- ``region=None`` means "the current patch". The signature carries
  ``region`` so the v15 viewer foundation can swap in a viewport tile
  provider without touching the UI; a true raw bypass stage (raw pixels
  even after Save) also arrives with that foundation.

Stages:

    corrected (= loader-served: saved corrected, else raw)
        -> remapped (production display transform)

The remapped stage runs the PRODUCTION algorithm
(:func:`core.channel_remap.apply_channel_remap`) on the corrected stage.
"""

from PyQt5.QtCore import QObject, pyqtSignal

import numpy as np

from ...core.channel_remap import apply_channel_remap

STAGE_CORRECTED = "corrected"
STAGE_REMAPPED = "remapped"


class Step0PreviewSourceProvider(QObject):
    """Serves per-channel pixels for a pipeline stage + mutual-state summary.

    Signals
    -------
    stage_invalidated(str channel, str stage):
        The saved corrected artifact for the channel changed (a Save landed);
        consumers should drop cached pixels for that channel and re-pull.
    """

    stage_invalidated = pyqtSignal(str, str)

    def __init__(self, page):
        super().__init__(page)
        self._page = page

    # ── pixels ────────────────────────────────────────────────────────────
    def get_pixels(self, channel, stage=STAGE_CORRECTED, region=None):
        """Return float32 pixels of `channel` at `stage` for `region`.

        region=None -> current patch (the only region until the viewer
        foundation lands).
        """
        if region is not None:
            raise NotImplementedError(
                "viewport regions arrive with the v15 viewer foundation; "
                "only the current patch (region=None) is supported")
        if stage == STAGE_CORRECTED:
            return self._corrected(channel)
        if stage == STAGE_REMAPPED:
            corrected = self._corrected(channel)
            if corrected is None:
                return None
            return apply_channel_remap(corrected, self._remap_params(channel))
        raise ValueError(f"unknown stage: {stage}")

    def _corrected(self, channel):
        """Loader-served pixels: SAVED corrected data for channels with a
        saved decision (the preload cache is hot-swapped on Save), raw
        otherwise. Never an unsaved in-memory preview."""
        page = self._page
        cached = page._preload_cache.get(page.current_patch_idx, {}).get(channel)
        if cached is not None:
            return cached
        return page._read_cond_patch_channel(channel, normalize=False)

    # ── mutual state visibility ─────────────────────────────────────────────
    def is_saved_corrected(self, channel):
        """True when the loader serves SAVED corrected pixels for `channel`."""
        loader = getattr(self._page, "loader", None)
        decisions = getattr(loader, "_corrected_decisions", None) or {}
        path = getattr(loader, "_corrected_zarr_path", None)
        return bool(path) and channel in decisions

    def saved_method(self, channel):
        loader = getattr(self._page, "loader", None)
        decisions = getattr(loader, "_corrected_decisions", None) or {}
        return decisions.get(channel)

    def source_note(self, channel):
        """Honest one-line provenance for UI display next to the channel."""
        if channel == self._page.nucleus_channel:
            return "raw (nucleus — excluded from correction)"
        if self.is_saved_corrected(channel):
            return f"corrected ({self.saved_method(channel)}, saved)"
        return "raw — background correction not saved"

    def _remap_params(self, channel):
        wb = getattr(self._page, "_cond_workbench", None)
        if wb is None:
            return None
        return wb._params.get(channel)

    def describe(self, channel):
        """One dict both sides can read: what correction AND remap currently
        think about this channel.

        Two kinds of correction parameter, and the difference matters to a
        consumer that wants to RENDER with them:

        * ``params`` -- the channel's explicit per-channel override, or
          ``None`` where the user has never set one. Unchanged; it still
          answers "has this channel been tuned?".
        * ``effective_tophat_radius`` / ``effective_cucim_sigma`` -- what a
          preview of this channel would ACTUALLY use right now. Never None.

        The effective values come from the page's own
        ``_resolve_channel_params``, which is also what the Per-Channel
        Decision panel loads into its inputs, so the two cannot drift. The
        fallback rule (module defaults, deliberately NOT the live global
        sliders) lives there and is not repeated here -- a second copy of
        it would be a second answer to the same question.

        ``assigned_method`` and ``preview_method`` are passed through
        verbatim, "both" included; interpreting them is the caller's job.
        """
        page = self._page
        saved = self.is_saved_corrected(channel)
        tr, cs = None, None
        cp = page._channel_params.get(channel)
        if cp:
            tr, cs = cp.get("tophat_radius"), cp.get("cucim_sigma")
        eff_tr, eff_cs = page._resolve_channel_params(channel)
        remap = self._remap_params(channel) or {}
        return {
            "channel": channel,
            "correction": {
                "assigned_method": page._channel_decisions.get(channel),
                "preview_method": page._channel_methods.get(channel),
                "saved_method": self.saved_method(channel),
                "params": {"tophat_radius": tr, "cucim_sigma": cs},
                "effective_tophat_radius": eff_tr,
                "effective_cucim_sigma": eff_cs,
                "saved": saved,
            },
            "remap": {
                "min": remap.get("min"),
                "max": remap.get("max"),
                "gamma": remap.get("gamma"),
                "user_adjusted": bool(
                    getattr(getattr(page, "_cond_workbench", None),
                            "_user_adjusted", {}).get(channel)),
            },
            "served_corrected_stage": ("corrected_saved" if saved
                                       else "raw_unsaved"),
            "source_note": self.source_note(channel),
        }

    # ── invalidation (Save is the only trigger) ──────────────────────────────
    def invalidate(self, channel, stage=STAGE_CORRECTED):
        """Announce that the saved corrected artifact changed for `channel`."""
        self.stage_invalidated.emit(channel, stage)
