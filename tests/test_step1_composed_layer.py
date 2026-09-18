"""Step1's composed RGBA on the real screen layer.

Block C4 of `docs/step1_rework_plan.md`, the display half: what the
coordinator composes has to land in the ONE view, at the right world
rectangle, above the single-channel layers, with absence still transparent --
and a change of mode or source must leave none of the old picture behind.

The gates here read the REAL `pg.ImageItem.qimage`, not the signal's
arguments: a window grab would say (0, 0, 0, 255) for a transparent pixel
because the black ViewBox shows through, and a signal argument says nothing
about what was painted.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import os
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")
pytest.importorskip("pyqtgraph")

from PyQt5 import QtWidgets  # noqa: E402

from block01.ui.step1_composed_layer import (  # noqa: E402
    COMPOSED_BASE_Z, Step1ComposedLayer,
)
from block01.viewer.explore_view import (  # noqa: E402
    OVERLAY_BASE_Z, ExploreView, TileItemPool,
)
from block01.viewer.tile_types import TileGridSpec  # noqa: E402

GRID = TileGridSpec(tile_size=512, source_chunk_shape=(), grid_version="v1")


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Provider:
    num_levels = 3

    def level_downsample_yx(self, level):
        return (2.0 ** level, 2.0 ** level)


class _Controller:
    """As much of `ExploreController` as a layer touches."""

    def __init__(self, view):
        self.grid = GRID
        self.level = 0
        self.marker_visible = True
        self._raw_pool = TileItemPool(view.view_box, 0, 3)
        self._precise_pool = TileItemPool(view.view_box, 100, 3)

    def set_marker_visible(self, visible):
        self.marker_visible = bool(visible)


def _stack(app):
    view = ExploreView()
    controller = _Controller(view)
    return SimpleNamespace(view=view, controller=controller,
                           provider=_Provider())


def _layer(app):
    stack = _stack(app)
    layer = Step1ComposedLayer(stack)
    layer.attach()
    return layer, stack


def _rgba_tile(r=10, g=20, b=30, alpha=255, size=8):
    tile = np.zeros((size, size, 4), np.uint8)
    tile[..., 0] = r
    tile[..., 1] = g
    tile[..., 2] = b
    tile[..., 3] = alpha
    return tile


def _pixel(item, row, col):
    from PyQt5.QtGui import QColor

    item.render()
    image = item.qimage
    assert image is not None, "the composed item never rendered"
    colour = QColor.fromRgba(image.pixel(col, row))
    return (colour.red(), colour.green(), colour.blue(), colour.alpha())


# ── 1. the pixels that are painted ────────────────────────────────────

def test_the_painted_pixels_are_the_composed_ones(app):
    """Gate 1: the display layer, not the signal argument."""
    layer, _stack_ = _layer(app)
    rgba = _rgba_tile(r=200, g=100, b=50)
    rgba[2, 3] = (7, 8, 9, 255)

    entry = layer.on_tile_composed(0, 1, 1, rgba)

    assert _pixel(entry.item, 0, 0) == (200, 100, 50, 255)
    assert _pixel(entry.item, 2, 3) == (7, 8, 9, 255)


def test_absence_stays_transparent_rather_than_black(app):
    """Gate 5: outside the analysis region there are no pixels, and the
    item says so -- `alpha = 0`, not a black fill."""
    layer, _stack_ = _layer(app)
    rgba = _rgba_tile(alpha=255)
    rgba[0:4, :, 3] = 0                      # the half outside the region

    entry = layer.on_tile_composed(0, 0, 0, rgba)

    assert _pixel(entry.item, 0, 0)[3] == 0, "absence was painted opaque"
    assert _pixel(entry.item, 6, 6)[3] == 255


def test_a_composed_item_carries_no_lookup_table_and_fixed_levels(app):
    """An RGBA tile IS the picture: nothing may re-map it at paint time."""
    layer, _stack_ = _layer(app)

    entry = layer.on_tile_composed(0, 1, 1, _rgba_tile())

    assert entry.item.lut is None
    assert tuple(entry.item.levels) == (0, 255)


# ── 2. where they land ────────────────────────────────────────────────

@pytest.mark.parametrize("level,tx,ty", [(0, 1, 1), (0, 3, 2), (2, 1, 0)])
def test_a_tile_lands_at_its_own_world_rectangle(app, level, tx, ty):
    layer, stack = _layer(app)
    rgba = _rgba_tile(size=8)

    entry = layer.on_tile_composed(level, tx, ty, rgba)

    ds = 2.0 ** level
    expected = ExploreView.world_rect(ty * GRID.tile_size, tx * GRID.tile_size,
                                      8, 8, ds, ds)
    assert entry.item.boundingRect().isValid()
    assert entry.rect == expected


def test_the_composed_layer_paints_above_every_single_channel_layer(app):
    layer, stack = _layer(app)

    entry = layer.on_tile_composed(0, 1, 1, _rgba_tile())

    assert layer.z_above_every_marker_layer()
    assert COMPOSED_BASE_Z > OVERLAY_BASE_Z + _Provider.num_levels
    controller = stack.controller
    for pool in (controller._raw_pool, controller._precise_pool):
        floor = pool.base_z + pool.num_levels
        assert entry.item.zValue() > floor


def test_attaching_puts_the_single_channel_layers_to_sleep(app):
    stack = _stack(app)
    layer = Step1ComposedLayer(stack)
    assert stack.controller.marker_visible is True

    layer.attach()
    assert stack.controller.marker_visible is False

    layer.detach()
    assert stack.controller.marker_visible is True


# ── 3. what happens to the old picture ────────────────────────────────

def test_a_recomposed_tile_replaces_the_item_in_place(app):
    """A weight moved: same coordinates, same item, new pixels -- the
    picture does not blink."""
    layer, _stack_ = _layer(app)
    first = layer.on_tile_composed(0, 1, 1, _rgba_tile(r=10))
    created = layer.pool.items_created

    second = layer.on_tile_composed(0, 1, 1, _rgba_tile(r=250))

    assert second.item is first.item
    assert layer.pool.items_created == created
    assert _pixel(second.item, 0, 0)[0] == 250


def test_clearing_leaves_no_composed_item_behind(app):
    """Gate 2: a mode or source change may not leave a tile of the old
    picture in the pool, where panning back would show it again."""
    layer, stack = _layer(app)
    for tx, ty in ((1, 1), (2, 1), (1, 2)):
        layer.on_tile_composed(0, tx, ty, _rgba_tile())
    items = [entry.item for entry in layer.pool.entries.values()]
    assert len(items) == 3

    layer.clear()

    assert layer.pool.entries == {}
    scene_items = set(stack.view.view_box.addedItems)
    assert not any(item in scene_items for item in items), (
        "a composed item stayed in the view after the layer was cleared")


def test_teardown_gives_the_screen_back(app):
    layer, stack = _layer(app)
    layer.on_tile_composed(0, 1, 1, _rgba_tile())

    layer.teardown()

    assert layer.pool.entries == {}
    assert stack.controller.marker_visible is True
    assert layer.attached is False
    layer.teardown()                         # idempotent


def test_a_finer_tile_is_not_shown_above_the_current_level(app):
    """The pool's own per-level policy, unchanged: a level-0 tile may not
    paint over level 2 while the camera is zoomed out."""
    layer, stack = _layer(app)
    fine = layer.on_tile_composed(0, 1, 1, _rgba_tile())
    coarse = layer.on_tile_composed(2, 1, 1, _rgba_tile())

    stack.controller.level = 2
    layer.apply_visibility()

    assert fine.item.isVisible() is False
    assert coarse.item.isVisible() is True


def test_a_tile_that_is_not_rgba_is_refused(app):
    layer, _stack_ = _layer(app)

    with pytest.raises(ValueError):
        layer.on_tile_composed(0, 1, 1, np.zeros((8, 8), np.uint8))
