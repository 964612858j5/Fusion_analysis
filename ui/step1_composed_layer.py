"""Where Step1's composed RGBA tiles actually land on screen.

Block C4 of `docs/step1_rework_plan.md`. C2 plans a frame and composes it off
the GUI thread; C3 says what it is composed from; this puts the result in the
ONE view, on the ONE camera, through the SAME `TileItemPool` every other layer
of this viewer uses.

NO NEW MACHINERY AND NO NEW SURFACE. The pool, the world-rect arithmetic and
the per-level z ordering are the viewer's own (`viewer/explore_view.py`); what
this adds is a pool whose items hold RGBA rather than one channel's raw
values, and one call -- `ExploreController.set_marker_visible(False)` -- that
puts the single-channel layers to sleep while a composition is on screen. No
button, window, dock or bus.

RGBA, NOT A LOOKUP TABLE. A composed tile is already the picture: the item
takes fixed levels `(0, 255)` and no colour table, so nothing re-maps it at
paint time. Absence stays absence -- `alpha = 0` outside the analysis region,
never a black fill -- because that is what the composition put in the array.

A COMPOSED TILE IS REPLACED, NOT ACCUMULATED. Moving a weight re-composes the
same coordinates, and the item is updated in place. A change of MODE or of
SOURCE is different: the old pixels are not a coarser version of the new
picture but a different picture, and a tile of it left in the pool would
reappear the moment the user pans back. Those clear the pool.
"""

import numpy as np
from PyQt5 import QtCore

from ..viewer.explore_view import (
    DEFAULT_ITEM_BUDGET, OVERLAY_BASE_Z, ExploreView, TileItemPool,
)

#: Z base for the composed pool. Above every single-channel layer's
#: `base_z + num_levels`, including the overlay's -- asserted against the
#: real pools when this attaches rather than assumed.
COMPOSED_BASE_Z = OVERLAY_BASE_Z + 300

#: What a composed tile's item levels are. The array IS the picture.
COMPOSED_LEVELS = (0, 255)


class Step1ComposedLayer(QtCore.QObject):
    """The composed frame's items in the one view.

    Owns its pool and nothing else: the view, the camera, the provider's
    geometry and the controller are the stack's.
    """

    def __init__(self, stack, budget=DEFAULT_ITEM_BUDGET, parent=None):
        super().__init__(parent)
        self._stack = stack
        provider = stack.provider
        num_levels = int(getattr(provider, "num_levels", 1) or 1)
        self._pool = TileItemPool(stack.view.view_box, COMPOSED_BASE_Z,
                                  num_levels, budget)
        # FIXED LEVELS, NO TABLE: an RGBA tile is not re-mapped at paint
        # time, and a lookup table would recolour a picture that already has
        # its colours.
        self._pool.set_levels_for_level(lambda _level: COMPOSED_LEVELS)
        self._attached = False
        self._tiles = 0

    # ── what it owns ──────────────────────────────────────────────────
    @property
    def pool(self):
        return self._pool

    @property
    def attached(self):
        return self._attached

    @property
    def tiles_blitted(self):
        return self._tiles

    def z_above_every_marker_layer(self):
        """The composed pool's floor against the layers it must cover.

        Checked rather than trusted: a pool adds `num_levels - level` to its
        base, so a base that merely LOOKS higher can still paint underneath
        a coarse tile of another layer.
        """
        controller = self._stack.controller
        others = []
        for name in ("_raw_pool", "_precise_pool"):
            pool = getattr(controller, name, None)
            if pool is not None:
                others.append(pool.base_z + pool.num_levels)
        overlay = getattr(controller, "overlay", None)
        overlay_pool = getattr(overlay, "pool", None) if overlay else None
        if overlay_pool is not None:
            others.append(overlay_pool.base_z + overlay_pool.num_levels)
        return COMPOSED_BASE_Z > max(others) if others else True

    # ── attaching ─────────────────────────────────────────────────────
    def attach(self):
        """Take the screen: the composition shows, the single channel sleeps.

        `set_marker_visible` is the controller's own switch -- opacity, not
        `setVisible`, so the pools' per-level policy goes on owning what it
        owns and nothing fights over it.
        """
        if self._attached:
            return False
        if not self.z_above_every_marker_layer():
            raise RuntimeError(
                "the composed layer would paint under a single-channel tile")
        self._stack.controller.set_marker_visible(False)
        self._attached = True
        return True

    def detach(self):
        """Give the screen back, and leave no composed pixel behind."""
        if not self._attached:
            return False
        self.clear()
        self._stack.controller.set_marker_visible(True)
        self._attached = False
        return True

    # ── the frames ────────────────────────────────────────────────────
    def on_tile_composed(self, level, tx, ty, rgba, valid=None):
        """One composed tile, at its own world rectangle.

        The rect is the tile's grid position scaled by the level's UNROUNDED
        per-axis downsample -- the same arithmetic every other layer of this
        viewer uses, so a composed tile lands exactly where the single
        channel it was made from would have.
        """
        rgba = np.asarray(rgba)
        if rgba.ndim != 3 or rgba.shape[2] != 4:
            raise ValueError("a composed tile is HxWx4 RGBA")
        provider = self._stack.provider
        grid = self._stack.controller.grid
        ds_y, ds_x = provider.level_downsample_yx(int(level))
        rect = ExploreView.world_rect(int(ty) * grid.tile_size,
                                      int(tx) * grid.tile_size,
                                      rgba.shape[0], rgba.shape[1],
                                      ds_y, ds_x)
        entry = self._pool.put(int(level), int(tx), int(ty), rect, rgba,
                               (int(level), int(tx), int(ty)))
        self._tiles += 1
        self.apply_visibility()
        return entry

    def apply_visibility(self, current_level=None):
        """The pool's own per-level policy, on the camera's current level."""
        if current_level is None:
            current_level = int(getattr(self._stack.controller, "level", 0))
        self._pool.apply_visibility(int(current_level))

    def clear(self):
        """Drop every composed item. For a MODE or SOURCE change: those
        pixels are a different picture, not a coarser one."""
        self._pool.clear()

    def teardown(self):
        """Release the items and the screen. Idempotent."""
        self.clear()
        if self._attached:
            try:
                self._stack.controller.set_marker_visible(True)
            except RuntimeError:                            # C++ side gone
                pass
            self._attached = False
