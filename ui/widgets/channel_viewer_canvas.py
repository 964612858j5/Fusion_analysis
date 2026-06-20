"""
block01/ui/widgets/channel_viewer_canvas.py — v13.1 center viewer canvas.

v14.3: the implementation was extracted into the reusable core
ui/widgets/high_quality_image_viewer.HighQualityImageViewer. ChannelViewerCanvas
is now a thin backward-compatible subclass so existing imports
(ChannelWorkbench, tests) keep working unchanged. There is a single source of
viewer logic — this module adds no behavior and must not diverge.

The module-level display helpers (display_normalize / _to_display / _to_rgb01)
are re-exported here for backward compatibility with existing imports.
"""

from __future__ import annotations

from .high_quality_image_viewer import (  # noqa: F401  (re-exported)
    HighQualityImageViewer,
    display_normalize,
    _to_display,
    _to_rgb01,
)


class ChannelViewerCanvas(HighQualityImageViewer):
    """Backward-compatible alias of HighQualityImageViewer (no added behavior)."""
    pass
