"""The TMA cores drawn over a viewer outside the Regions page
(docs/f5_tma_display_plan.md part 2): a read-only `RegionsOverlay` (no tool,
so every mouse event goes on to the view) on whatever view the owner has
now, filled from `core/regions/display.py` items.

`refresh()` is called by the owner whenever the view, the source of the
cores or the switch may have changed (step entry, a viewer opened or
re-synced, another run chosen); it attaches to the current view, or detaches
when there is none, and sets the items. Nothing is cached between calls.
"""

from __future__ import annotations

from typing import Callable, List, Optional

from .overlay import RegionsOverlay

#: A core smaller than this on screen shows no name (user ruling 2026-10-19:
#: names only from a suitable zoom; tuned on the real machine).
CORE_LABEL_MIN_PX = 60


class CoreOutlines:
    def __init__(self, view: Callable[[], Optional[object]], items: Callable[[], List[dict]],
                 shown: Callable[[], bool], to_view: Optional[Callable] = None):
        """`view()`: the viewer's `ExploreView`-like object (``graphics``,
        ``view_box``) or None; `items()`: overlay items in level-0
        coordinates; `shown()`: the switch; `to_view(x, y)`: level-0 to the
        view's own coordinates (None: they are the same)."""
        self._view, self._items, self._shown, self._to_view = view, items, shown, to_view
        self.overlay: Optional[RegionsOverlay] = None

    def refresh(self) -> int:
        """Attach to the view on screen and draw (when switched on); the
        number of items there are to draw, shown or not."""
        view = self._view()
        if view is None:
            self.detach()
            return 0
        try:
            found = self._items()
        except Exception:
            self.clear()                         # nothing stale stays on screen
            raise
        graphics, vb = view.graphics, view.view_box
        ov = self.overlay
        if ov is None or ov.graphics is not graphics or ov.vb is not vb:
            self.detach()
            ov = self.overlay = RegionsOverlay(graphics, vb, None, label_min_px=CORE_LABEL_MIN_PX)
        items = found if self._shown() else []
        if self._to_view is not None:
            f = self._to_view
            items = [dict(it, polygon=[list(f(x, y)) for x, y in it["polygon"]]) for it in items]
        ov.set_items(items)
        ov.raise_()
        return len(found)

    def clear(self):
        if self.overlay is not None:
            self.overlay.set_items([])

    def detach(self):
        if self.overlay is not None:
            self.overlay.detach()
            self.overlay = None
