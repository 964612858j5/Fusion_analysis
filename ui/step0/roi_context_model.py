"""
block01/ui/step0/roi_context_model.py — v14.2b single authoritative ROI/context model.

Plain-data model owned by Step0Page. It is the ONE source of truth for the
ROI / patch / full-WSI-mode context shared between the Step0 main OverviewPanel
and the TissueNavigatorPopup OverviewPanel. Both panels are views/editors over
this model: an edit on either panel is adopted into this model, then BOTH panels
are re-rendered from it. The panels' internal `_rois`/`_patches` are only render
caches derived from this model — never the authority.

This object holds plain Python data only (no Qt widget, no OverviewPanel import),
so it cannot become a second widget-coupled ROI store. utils/roi_project.py is a
stateless serialization/id helper, not a stateful model, so it is not used as the
authority here.
"""

from __future__ import annotations


class RoiContextModel:
    """Authoritative ROI/context state: rois, patches, full_wsi_mode, loader, nuc."""

    def __init__(self):
        self.loader = None
        self.nucleus_channel = ""
        self.rois = []            # list[dict]  — ROI dicts (name/polygon/color/...)
        self.patches = []         # list[tuple] — (y0, y1, x0, x1)
        self.full_wsi_mode = False

    # ── adoption (write-back from whichever panel was edited) ─────────────
    def adopt_rois(self, rois):
        self.rois = [dict(r) for r in (rois or [])]

    def adopt_patches(self, patches):
        out = []
        for p in (patches or []):
            y0, y1, x0, x1 = (int(v) for v in p)
            out.append((y0, y1, x0, x1))
        self.patches = out

    def adopt(self, rois=None, patches=None, full_wsi_mode=None,
              loader=Ellipsis, nucleus_channel=Ellipsis):
        """Adopt the authoritative state. Only provided fields are replaced.

        loader / nucleus_channel use Ellipsis as the "unset" sentinel so an
        explicit None (clearing the loader) is still honored.
        """
        if rois is not None:
            self.adopt_rois(rois)
        if patches is not None:
            self.adopt_patches(patches)
        if full_wsi_mode is not None:
            self.full_wsi_mode = bool(full_wsi_mode)
        if loader is not Ellipsis:
            self.loader = loader
        if nucleus_channel is not Ellipsis:
            self.nucleus_channel = nucleus_channel or ""

    def clear_rois_and_patches(self):
        self.rois = []
        self.patches = []

    # ── read-only views ──────────────────────────────────────────────────
    def roi_count(self):
        return len(self.rois)

    def snapshot(self):
        """Plain dict copy for feeding a panel (rois/patches are fresh copies)."""
        return {
            "loader": self.loader,
            "nucleus_channel": self.nucleus_channel,
            "rois": [dict(r) for r in self.rois],
            "patches": list(self.patches),
            "full_wsi_mode": self.full_wsi_mode,
        }
