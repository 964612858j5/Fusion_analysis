"""Explore/Compare mode state contract (v15 Phase 1B — contract only).

Defines the UI/state contract for the future whole-slide viewer:

- Explore: single view, free pan/zoom/Navigator jumps, previews only the final
  selected background method; drag smoothness first (debounce, cancel stale,
  latest-request-wins).
- Compare: locks the current viewport by default and shows the same location
  as Original | Top-hat / cuCIM | Final selected, all four views sharing
  global coordinates, viewport bbox, zoom, pyramid level, channels,
  Min/Max/Gamma, colors and halo/crop rules.

This module intentionally contains NO tile loading, GPU code, or background
computation, and never touches production parameters or configs. All
coordinates are whole-slide (full-resolution, global) pixels — never
patch-local.
"""

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import List, Optional, Tuple

from PyQt5.QtCore import QObject, pyqtSignal

# Rest interval after pan/zoom before recomputing compare results (ms).
COMPARE_DEBOUNCE_MS = 100          # contract: ~80–120 ms

# Fixed 2x2 compare layout, row-major:
#   Original | Top-hat
#   cuCIM    | Final selected
COMPARE_VIEWS = ("original", "tophat", "cucim", "final")

COORDINATE_SPACE = "whole_slide_full_res_pixels"


class ViewerMode(Enum):
    EXPLORE = "explore"
    COMPARE = "compare"


class CompareScope(Enum):
    CURRENT_VIEWPORT = "current_viewport"
    NAVIGATOR_SELECTION = "navigator_selection"


@dataclass
class ViewportState:
    """Shared viewport for synchronized views (global coordinates)."""
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    zoom: float = 1.0
    pyramid_level: int = 0

    def bbox(self) -> Tuple[float, float, float, float]:
        return (self.x, self.y, self.width, self.height)


@dataclass
class ComparisonROI:
    """User-drawn rectangular Comparison ROI in the Navigator.

    Always whole-slide coordinates. Background algorithms may read an external
    halo, but only this rectangle is displayed.
    """
    x: float
    y: float
    width: float
    height: float
    dataset_id: str = ""

    def is_valid(self) -> bool:
        return self.width > 0 and self.height > 0

    def moved(self, dx: float, dy: float) -> "ComparisonROI":
        return ComparisonROI(self.x + dx, self.y + dy,
                             self.width, self.height, self.dataset_id)

    def resized(self, width: float, height: float) -> "ComparisonROI":
        return ComparisonROI(self.x, self.y, width, height, self.dataset_id)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["coordinate_space"] = COORDINATE_SPACE
        return d


@dataclass
class PinnedLocation:
    """Optional pinned comparison location (legacy Patch 1–4 successor)."""
    label: str
    x: float
    y: float
    width: float
    height: float
    dataset_id: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["coordinate_space"] = COORDINATE_SPACE
        return d


class SharedViewportState(QObject):
    """Single source of truth for the 2x2 synchronized compare views.

    All four views subscribe to viewport_changed and render the same bbox /
    zoom / pyramid level; panning or zooming any one view calls set_viewport,
    which fans out to the others. A monotonically increasing generation
    implements latest-request-wins for downstream compute."""

    viewport_changed = pyqtSignal(object)      # ViewportState

    def __init__(self, parent=None):
        super().__init__(parent)
        self._viewport = ViewportState()
        self._generation = 0

    def viewport(self) -> ViewportState:
        return self._viewport

    def generation(self) -> int:
        return self._generation

    def set_viewport(self, vp: ViewportState):
        self._viewport = vp
        self._generation += 1
        self.viewport_changed.emit(vp)

    def is_current(self, generation: int) -> bool:
        """Stale-request check for latest-request-wins schedulers."""
        return generation == self._generation


class CompareModeState(QObject):
    """Explore/Compare mode + compare-scope state machine.

    Holds no production parameters: switching modes or scopes must never
    modify correction/remap/fusion/segmentation configs.
    """

    mode_changed = pyqtSignal(object)       # ViewerMode
    scope_changed = pyqtSignal(object)      # CompareScope
    roi_changed = pyqtSignal(object)        # ComparisonROI | None
    pinned_changed = pyqtSignal(list)       # list[PinnedLocation]

    MAX_PINNED = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mode = ViewerMode.EXPLORE
        self._scope = CompareScope.CURRENT_VIEWPORT
        self._roi: Optional[ComparisonROI] = None
        self._locked_viewport: Optional[ViewportState] = None
        self._pinned: List[PinnedLocation] = []

    # -- mode ----------------------------------------------------------------
    def mode(self) -> ViewerMode:
        return self._mode

    def enter_compare(self, current_viewport: ViewportState):
        """Enter Compare, capturing (locking) the current viewport by default."""
        self._locked_viewport = current_viewport
        self._scope = CompareScope.CURRENT_VIEWPORT
        if self._mode is not ViewerMode.COMPARE:
            self._mode = ViewerMode.COMPARE
            self.mode_changed.emit(self._mode)
        self.scope_changed.emit(self._scope)

    def exit_compare(self):
        if self._mode is not ViewerMode.EXPLORE:
            self._mode = ViewerMode.EXPLORE
            self.mode_changed.emit(self._mode)

    def locked_viewport(self) -> Optional[ViewportState]:
        return self._locked_viewport

    # -- scope / ROI ------------------------------------------------------------
    def scope(self) -> CompareScope:
        return self._scope

    def roi(self) -> Optional[ComparisonROI]:
        return self._roi

    def set_navigator_roi(self, roi: ComparisonROI):
        """Drawing a Navigator ROI switches Compare to that ROI."""
        if not roi.is_valid():
            raise ValueError("Comparison ROI must have positive size")
        self._roi = roi
        if self._scope is not CompareScope.NAVIGATOR_SELECTION:
            self._scope = CompareScope.NAVIGATOR_SELECTION
            self.scope_changed.emit(self._scope)
        self.roi_changed.emit(roi)

    def clear_roi(self):
        self._roi = None
        if self._scope is not CompareScope.CURRENT_VIEWPORT:
            self._scope = CompareScope.CURRENT_VIEWPORT
            self.scope_changed.emit(self._scope)
        self.roi_changed.emit(None)

    # -- pinned locations ---------------------------------------------------------
    def pinned(self) -> List[PinnedLocation]:
        return list(self._pinned)

    def add_pinned(self, loc: PinnedLocation):
        if len(self._pinned) >= self.MAX_PINNED:
            raise ValueError(f"At most {self.MAX_PINNED} pinned locations")
        self._pinned.append(loc)
        self.pinned_changed.emit(self.pinned())

    def remove_pinned(self, label: str):
        before = len(self._pinned)
        self._pinned = [p for p in self._pinned if p.label != label]
        if len(self._pinned) != before:
            self.pinned_changed.emit(self.pinned())
