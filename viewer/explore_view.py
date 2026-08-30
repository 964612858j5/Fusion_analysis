"""Step0 Explore: fixed-world-coordinate per-tile pyqtgraph view + controller.

See docs/v15_step0_explore_integration.md (authoritative design) and the
architecture-correction task that produced this rewrite. This module
implements ONLY: ExploreView (the per-tile-item widget) and
ExploreController (viewport tracking, tile requesting, settle debounce,
provisional-state management, teardown). Explicitly OUT of scope here:
Step0Page mounting, Compare 2x2, idle prefetch, OpenGL.

## Odon-style per-tile rendering (replaces the earlier mosaic-canvas design)

Every delivered tile becomes its OWN `pg.ImageItem`, placed ONCE in the
full-resolution (level-0) WORLD coordinate system via `setRect` computed
from its own (level, tx, ty) using UNROUNDED, PER-AXIS downsample factors
(`provider.level_downsample_yx`). Items never move and are never migrated
into a shared canvas; there is no mosaic reallocation. A `TileItemPool` per
layer (raw, precise) keys items by `(level, tx, ty)` and updates an
existing item's pixels in place (`setImage`) when a newer result lands for
the same address, instead of creating a new item. The pinned overview stays
a single always-resident `pg.ImageItem`.

## World coordinate system

A tile at pyramid level L with top-left (y0, x0) in level-L pixels is drawn
via `setRect(QRectF(x0*ds_x, y0*ds_y, w*ds_x, h*ds_y))` where
`(ds_y, ds_x) = provider.level_downsample_yx(L)` -- unrounded, independent
per axis, so non-square-ratio pyramids and long tile runs never accumulate
drift. Level switches therefore never shift already-drawn content, and
`jump_to()` addresses level-0 coordinates directly.

Every `pg.ImageItem` (overview, raw-pool, precise-pool) is constructed with
`axisOrder="row-major"` EXPLICITLY -- never relying on the process-global
`pg.setConfigOptions(imageAxisOrder=...)`, which a standalone script (or a
future host window) might not have set, producing a transposed render.

## Level switching without clearing

Switching the displayed pyramid level does NOT clear previously-drawn
items from either pool -- their world rects remain correct (they were
computed from their own level's geometry) so they stay visually aligned;
only their z-order changes relative to the new level's tiles. Each pooled
item's zValue is `layer_base_z + (num_levels - level)`, so a FINER level
(smaller `level` int, higher resolution) draws ABOVE a coarser one within
the same layer. Off-level items are eventually pruned once they fall
outside the viewport (with margin); the pinned overview always covers any
gap in the meantime.

## Anti-checkerboard for precise tiles (design doc §1.2 / cheap group gate)

Corrected (precise) tiles are brightness-normalized per-tile relative to
their own local background; sitting a corrected tile next to a raw (or
differently-corrected) tile produces a visible seam. To avoid this, the
ENTIRE precise layer is hidden (a single cheap boolean flag applied to
every pooled precise item) unless every tile in the CURRENT wanted set (at
the current level, under the current selection context) has a matching,
current `CorrectionKey` recorded against it. Coverage completing flips the
whole layer visible in one step (atomic, never a per-tile checkerboard).
The raw layer is exempt -- it holds only single-stage pixels, so it fills
in progressively, tile by tile, with no cross-stage seam.

## Camera contract: no debounce on the range handler itself

The ViewBox's native mouse pan/wheel interaction is completely unmodified
(pyqtgraph's default: wheel zooms about the mouse cursor). `sigRangeChanged`
runs a CHEAP handler on every single event: recompute the wanted tile set
and run the (cheap) item-pool prune check. Nothing here touches the
scheduler. Actual REQUEST ISSUING is what gets debounced -- a 30ms
single-shot "motion" timer coalesces a burst of range-change events into
one batch of raw tile requests, cancelling the previous batch's now-stale
`("raw", n)` generation token. This is what keeps a fast wheel-zoom from
flooding the scheduler's ready-queue while never fighting the camera.

## Generation namespaces

`view_generation` (raw layer) and `_settled_generation` (precise layer) are
independent counters. To guarantee they can never collide (a fast zoom
cancelling view-generation 5 must never accidentally cancel a live
precise-generation 5), every generation token handed to the scheduler is a
namespaced tuple: `("raw", n)` / `("precise", n)`. `TileScheduler` treats
generation tokens purely by hashable-equality, so this requires no
scheduler changes beyond the type hint/docstring already updated there.

## GUI-side delivery guard

A scheduler callback may fire on a worker thread; it is marshalled to the
GUI thread via a Qt QUEUED connection. A queued signal can therefore be
sitting in the event queue at the moment its generation gets cancelled (the
cancellation itself runs on the GUI thread, synchronously, before the
queued slot executes). Every delivery slot re-checks, on the GUI thread,
at handling time: (a) the token still matches the CURRENT generation (not
merely "not yet marked stale" -- an exact-match check against the live
counter, which is strictly stronger and immune to the queued-signal race),
(b) the tile address is still in the CURRENT wanted set for its layer, and
(c) source/channel/level still match. Any failure drops the result and
bumps a counter; nothing is ever forced onto a stale layer.
"""

import time
from typing import Dict, Optional, Tuple

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtWidgets
from PyQt5.QtCore import QRectF

from .tile_types import (
    CorrectionKey,
    QualityLevel,
    RawKey,
    TileAddress,
    TileGridSpec,
    TileRequest,
    effective_param,
    tiles_covering,
)
from ..core.bg_correction import BG_CORRECTION_ALGO_VERSION


# Sentinel distinguishing "argument not passed" from "argument passed as
# None" for ExploreController.set_selection (an explicit method=None must
# still take effect -- it means "no precise layer").
_UNSET = object()

# Z-order bases: overview (single item) sits at 0; every pooled raw item
# gets `RAW_BASE_Z + (num_levels - level)`, every pooled precise item gets
# `PRECISE_BASE_Z + (num_levels - level)` -- so within a layer a finer
# level always draws above a coarser one, and the precise layer as a whole
# always draws above the raw layer.
RAW_BASE_Z = 100
PRECISE_BASE_Z = 200

# Default per-layer pooled-item budget (design doc: "e.g. 400 per layer").
DEFAULT_ITEM_BUDGET = 400

# Prune-eligibility viewport margin, in TILE units (matches the canvas-era
# cover margin) -- an item within this many tiles of the current viewport,
# even off-level, is kept a little longer to absorb small pans.
PRUNE_MARGIN_TILES = 1


# ── TileItemPool ─────────────────────────────────────────────────────────────

class _PoolEntry:
    __slots__ = ("item", "level", "tx", "ty", "rect", "key")

    def __init__(self, item, level, tx, ty, rect):
        self.item = item
        self.level = level
        self.tx = tx
        self.ty = ty
        self.rect = rect
        self.key = None  # RawKey / CorrectionKey last successfully blitted


class TileItemPool:
    """One `pg.ImageItem` per delivered tile, keyed by `(level, tx, ty)`.

    Items are created once and updated in place thereafter (`setImage`);
    their `setRect` placement is computed ONCE at creation and never
    changed. Pruning removes items that are both off the current level and
    outside the current viewport (with margin), farthest-first, only once
    the pool exceeds `budget`.
    """

    def __init__(self, view_box, base_z: int, num_levels: int,
                 budget: int = DEFAULT_ITEM_BUDGET):
        self.view_box = view_box
        self.base_z = base_z
        self.num_levels = max(1, num_levels)
        self.budget = budget
        self.entries: Dict[Tuple[int, int, int], _PoolEntry] = {}
        self.items_created = 0
        self.items_pruned = 0

    def _z_for_level(self, level: int) -> float:
        return self.base_z + (self.num_levels - level)

    def get(self, level: int, tx: int, ty: int) -> Optional[_PoolEntry]:
        return self.entries.get((level, tx, ty))

    def put(self, level: int, tx: int, ty: int, rect: QRectF,
            arr_uint8: np.ndarray, key) -> _PoolEntry:
        """Create (once) or update the item at `(level, tx, ty)`."""
        coord = (level, tx, ty)
        entry = self.entries.get(coord)
        if entry is None:
            item = pg.ImageItem(arr_uint8, axisOrder="row-major")
            item.setZValue(self._z_for_level(level))
            item.setLevels((0, 255))
            item.setRect(rect)
            self.view_box.addItem(item)
            entry = _PoolEntry(item, level, tx, ty, rect)
            self.entries[coord] = entry
            self.items_created += 1
        else:
            entry.item.setImage(arr_uint8, autoLevels=False, levels=(0, 255))
            # rect/level are immutable after creation (design contract):
            # entries never move.
        entry.key = key
        return entry

    def set_visible(self, visible: bool):
        for entry in self.entries.values():
            entry.item.setVisible(visible)

    def clear(self):
        """Remove every item (used only for a hard invalidation, e.g. a
        channel/source change -- normal level switches do NOT call this)."""
        for entry in self.entries.values():
            self.view_box.removeItem(entry.item)
        self.entries.clear()

    def prune(self, current_level: int, viewport_world_rect: QRectF,
              margin_world: float, keep_coords):
        """Enforce `self.budget`: if over budget, remove items that are
        (not at `current_level`) AND (outside `viewport_world_rect`
        expanded by `margin_world`), farthest-from-viewport-center first.
        `keep_coords` (a set of `(level, tx, ty)`) is NEVER pruned even if
        it would otherwise qualify (defensive -- normally the wanted set is
        already at `current_level` and inside the viewport)."""
        if len(self.entries) <= self.budget:
            return
        expanded = viewport_world_rect.adjusted(
            -margin_world, -margin_world, margin_world, margin_world)
        cx = viewport_world_rect.center().x()
        cy = viewport_world_rect.center().y()

        candidates = []
        for coord, entry in self.entries.items():
            if coord in keep_coords:
                continue
            if entry.level == current_level:
                continue
            if expanded.intersects(entry.rect):
                continue
            rc = entry.rect.center()
            dist2 = (rc.x() - cx) ** 2 + (rc.y() - cy) ** 2
            candidates.append((dist2, coord))

        if not candidates:
            return
        candidates.sort(reverse=True)  # farthest first
        n_over = len(self.entries) - self.budget
        for _dist2, coord in candidates[:n_over]:
            entry = self.entries.pop(coord)
            self.view_box.removeItem(entry.item)
            self.items_pruned += 1


# ── ExploreView: the widget ─────────────────────────────────────────────────

class ExploreView(QtWidgets.QWidget):
    """pyqtgraph GraphicsLayoutWidget hosting one ViewBox: one pinned
    overview ImageItem plus two per-tile TileItemPools (raw, precise), all
    positioned in the full-resolution world coordinate system.

    Z-order (bottom to top): overview (0) < raw pool (RAW_BASE_Z..) <
    precise pool (PRECISE_BASE_Z..).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.graphics = pg.GraphicsLayoutWidget()
        layout.addWidget(self.graphics)

        self.view_box: pg.ViewBox = self.graphics.addViewBox()
        self.view_box.setAspectLocked(True)
        self.view_box.invertY(True)
        # The controller drives the viewport explicitly (pan/zoom/jump); a
        # pyqtgraph ViewBox's default auto-range-to-content behavior would
        # otherwise fire a SPURIOUS sigRangeChanged whenever an ImageItem's
        # bounding rect changes (e.g. load_overview()'s first setImage),
        # picking an unrelated display level out from under the caller.
        # Native mouse pan/wheel-zoom-about-cursor interaction is untouched
        # (pyqtgraph ViewBox default) -- disabling auto-RANGE does not
        # disable mouse interaction.
        self.view_box.disableAutoRange()

        # Every ImageItem constructed with axisOrder="row-major" EXPLICITLY
        # (module docstring) -- never relying on a process-global config.
        self.overview_item = pg.ImageItem(axisOrder="row-major")
        self.overview_item.setZValue(0)
        self.view_box.addItem(self.overview_item)

    @staticmethod
    def world_rect(y0: float, x0: float, h: float, w: float,
                    ds_y: float, ds_x: float) -> QRectF:
        """World-space rect for a level-L tile/region, using UNROUNDED,
        per-axis downsample factors (module docstring)."""
        return QRectF(x0 * ds_x, y0 * ds_y, w * ds_x, h * ds_y)


# ── ExploreController ────────────────────────────────────────────────────────

class ExploreController(QtCore.QObject):
    """Drives an ExploreView from a provider/scheduler/compute stack.

    Selection state (channel/method/params) plus the viewport define the
    current "settled" request set. Raw tiles fill in immediately on
    viewport change; precise (corrected) tiles are requested only after a
    `settle_ms` quiet period (or immediately via `jump_to`).
    """

    provisional_changed = QtCore.pyqtSignal(bool)

    # Internal cross-thread delivery signals (scheduler callbacks fire on
    # worker threads; Qt widgets must only be touched on the GUI thread).
    _raw_delivered = QtCore.pyqtSignal(object)
    _precise_delivered = QtCore.pyqtSignal(object)

    def __init__(self, provider, scheduler, compute, grid: TileGridSpec,
                 view: ExploreView, channel: str, settle_ms: int = 80,
                 probe: bool = False, item_budget: int = DEFAULT_ITEM_BUDGET):
        super().__init__()
        self.provider = provider
        self.scheduler = scheduler
        self.compute = compute
        self.grid = grid
        self.view = view
        self.settle_ms = settle_ms
        self.probe = probe

        # ── selection state ──
        self.channel = channel
        self.method: Optional[str] = None
        self.params: Tuple[int, ...] = ()
        self.quality = QualityLevel.INTERACTIVE

        # ── generations (namespaced tuples -- see module docstring) ──
        self._raw_gen_n = 0
        self._settled_gen_n = 0
        self.view_generation = ("raw", self._raw_gen_n)
        self._settled_generation = ("precise", self._settled_gen_n)

        # ── display level / viewport bookkeeping ──
        self.level = 0
        self._current_bbox = None  # (y0, x0, y1, x1) in level-0 coords
        self._visible_tiles = set()  # {(tx, ty)} at self.level

        # ── item pools ──
        num_levels = getattr(provider, "num_levels", 1)
        self._raw_pool = TileItemPool(view.view_box, RAW_BASE_Z, num_levels, item_budget)
        self._precise_pool = TileItemPool(view.view_box, PRECISE_BASE_Z, num_levels, item_budget)

        # ── provisional state ──
        self._provisional = False
        self._precise_visible = False
        self.view.precise_visible = False  # for tests/introspection

        # ── stable display levels (finding 6, carried forward): fixed at
        # load_overview, reapplied identically to overview/raw/precise. New
        # tile arrivals must never rescale brightness.
        self._display_lo = 0.0
        self._display_hi = 1.0
        self._overview_arr: Optional[np.ndarray] = None

        # ── teardown bookkeeping ──
        self._teardown_order = []
        self._torn_down = False

        # ── stats (exposed for tests / probe) ──
        self.stats = {
            "raw_tiles_blitted": 0,
            "precise_tiles_blitted": 0,
            "stale_precise_dropped": 0,
            "mismatched_key_dropped": 0,
            "mismatched_raw_dropped": 0,
            "late_raw_rejected": 0,
            "late_precise_rejected": 0,
            "items_created": 0,
            "items_pruned": 0,
        }
        # probe-only timing samples (populated only when probe=True).
        self.timings = {
            "range_handler_ms": [],
            "request_issue_ms": [],
            "tile_item_update_ms": [],
            "viewport_first_raw_tile_ms": [],
            "viewport_full_raw_tile_ms": [],
            "viewport_first_precise_tile_ms": [],
            "viewport_full_precise_ms": [],
            # (timestamp, cost_ms) pairs for range_handler/request_issue/
            # tile_item_update samples -- consumed by g1_render_probe.py's
            # window-aggregation post-processing (bucketed 16.7ms windows,
            # NOT exact vsync frames).
            "frame_events": [],
        }
        self._raw_probe_batch = None
        self._precise_probe_batch = None
        # guard against a jump's manual settle firing twice.
        self._jumping = False

        # ── timers ──
        self._settle_timer = QtCore.QTimer(self)
        self._settle_timer.setSingleShot(True)
        self._settle_timer.setInterval(self.settle_ms)
        self._settle_timer.timeout.connect(self._on_settle)

        # Coalescing "motion" timer: request ISSUING only (the range handler
        # itself runs on every sigRangeChanged, cheaply -- module docstring
        # "Camera contract").
        self.MOTION_MS = 30
        self._motion_timer = QtCore.QTimer(self)
        self._motion_timer.setSingleShot(True)
        self._motion_timer.setInterval(self.MOTION_MS)
        self._motion_timer.timeout.connect(self._issue_raw_requests)

        # Level-switch hysteresis threshold (fraction) -- avoids z-order /
        # request thrash when the zoom sits right at a level boundary.
        self.LEVEL_HYSTERESIS = 0.2

        self._raw_delivered.connect(self._handle_raw_result, QtCore.Qt.QueuedConnection)
        self._precise_delivered.connect(self._handle_precise_result, QtCore.Qt.QueuedConnection)

        self.view.view_box.sigRangeChanged.connect(self._on_range_changed)

    # ── source identity / correction key helpers ─────────────────────────

    def selection_key_context(self):
        """(source, channel, method, effective params, level, quality) —
        the identity tuple a precise tile must match to be considered
        current (design doc §1.2)."""
        source = self.provider.source_identity()
        ds = self.provider.level_downsample(self.level)
        eff_params = tuple(
            effective_param(p, self.level, ds) for p in self.params
        )
        return (source, self.channel, self.method, eff_params, self.level, self.quality)

    def _wants_precise(self) -> bool:
        return self.method is not None

    def _make_correction_key(self, tx: int, ty: int) -> CorrectionKey:
        source, channel, method, eff_params, level, quality = self.selection_key_context()
        addr = TileAddress(grid=self.grid, level=level, tx=tx, ty=ty)
        return CorrectionKey(
            source=source, channel=channel, tile=addr, method=method,
            params=eff_params, algorithm_version=BG_CORRECTION_ALGO_VERSION,
            quality=quality,
        )

    def _make_raw_key(self, tx: int, ty: int) -> RawKey:
        source = self.provider.source_identity()
        addr = TileAddress(grid=self.grid, level=self.level, tx=tx, ty=ty)
        return RawKey(source=source, channel=self.channel, tile=addr)

    # ── selection ─────────────────────────────────────────────────────────

    def set_selection(self, channel=_UNSET, method=_UNSET, params=_UNSET):
        """Change channel/method/params. Marks the precise layer
        PROVISIONAL and re-issues the settled request for the current
        viewport immediately (no wait). A channel change also invalidates
        both pools outright (a channel change means every already-drawn
        pixel is simply the wrong data -- unlike a method/param change,
        there is no "still technically raw and fine" fallback)."""
        channel_changed = channel is not _UNSET and channel != self.channel
        if channel is not _UNSET:
            self.channel = channel
        if method is not _UNSET:
            self.method = method
        if params is not _UNSET:
            self.params = params

        if channel_changed:
            self._raw_pool.clear()
            self._precise_pool.clear()

        self._enter_provisional()
        if self._current_bbox is not None:
            self._issue_settled_request()

    def _enter_provisional(self):
        self._provisional = True
        self.provisional_changed.emit(True)
        self._update_precise_visibility()

    def _maybe_exit_provisional(self):
        """Restore full visibility / clear provisional once every visible
        tile has a matching, current precise key."""
        if not self._provisional:
            return
        if not self._visible_tiles:
            return
        if not self._wants_precise():
            return
        if self._coverage_complete():
            self._provisional = False
            self.provisional_changed.emit(False)
        self._update_precise_visibility()

    def _coverage_complete(self) -> bool:
        ctx = self.selection_key_context()
        for tx, ty in self._visible_tiles:
            entry = self._precise_pool.get(self.level, tx, ty)
            if entry is None or entry.key is None or not self._key_matches_context(entry.key, ctx):
                return False
        return True

    def _update_precise_visibility(self):
        """Anti-checkerboard contract (module docstring): the ENTIRE
        precise layer (every pooled item) is visible only when every tile
        in `_visible_tiles` has a blitted precise result whose key matches
        the CURRENT selection context -- a single cheap boolean flag,
        never a per-tile decision."""
        fully_covered = (
            self._wants_precise()
            and bool(self._visible_tiles)
            and self._coverage_complete()
        )
        self._precise_visible = fully_covered
        self.view.precise_visible = fully_covered
        self._precise_pool.set_visible(fully_covered)

    @staticmethod
    def _key_matches_context(key: CorrectionKey, ctx) -> bool:
        source, channel, method, eff_params, level, quality = ctx
        return (
            key.source == source and key.channel == channel and key.method == method
            and key.params == eff_params and key.tile.level == level and key.quality == quality
        )

    # ── startup ───────────────────────────────────────────────────────────

    def load_overview(self):
        """Pick the smallest pyramid level with max(h, w) >= 512 pixels
        (else the smallest available level), read the WHOLE level
        synchronously, and set it as the never-evicted overview layer."""
        num_levels = self.provider.num_levels
        chosen = num_levels - 1  # default: the smallest (coarsest) level
        for level in range(num_levels - 1, -1, -1):
            h, w = self.provider.level_shape(level)
            if max(h, w) >= 512:
                chosen = level
                break

        h, w = self.provider.level_shape(chosen)
        arr, _offset = self.provider.read_region(self.channel, chosen, 0, h, 0, w)
        arr = arr.astype(np.float32, copy=False)

        ds_y, ds_x = self._downsample_yx(chosen)
        rect = ExploreView.world_rect(0, 0, h, w, ds_y, ds_x)

        lo, hi = self._compute_display_levels(arr)
        self._display_lo, self._display_hi = lo, hi
        self._overview_arr = arr

        # Overview is always fully valid (whole level read synchronously) --
        # plain grayscale, never masked; fixed levels only, never autoLevels.
        self.view.overview_item.setImage(arr, autoLevels=False, levels=(lo, hi))
        self.view.overview_item.setRect(rect)
        self._overview_level = chosen
        self._overview_shape = (h, w)

    def _downsample_yx(self, level: int) -> Tuple[float, float]:
        """(ds_y, ds_x) for `level`, using the provider's unrounded
        per-axis API when available, else falling back to the single
        rounded factor (for minimal fakes in older tests)."""
        fn = getattr(self.provider, "level_downsample_yx", None)
        if fn is not None:
            return fn(level)
        ds = self.provider.level_downsample(level)
        return ds, ds

    @staticmethod
    def _compute_display_levels(arr: np.ndarray) -> Tuple[float, float]:
        """(0, 99.5th percentile), guarding the degenerate all-zero/constant
        case so span-based normalization never divides by ~0."""
        lo = 0.0
        hi = float(np.percentile(arr, 99.5)) if arr.size else 0.0
        if not np.isfinite(hi) or hi <= lo:
            hi = lo + 1.0
        return lo, hi

    def set_display_levels(self, lo: float, hi: float):
        """Public: reapply fixed display levels to overview/raw/precise.
        Never triggered automatically by new tile arrivals. Re-quantizes
        every currently-pooled item's pixels from its LAST delivered raw
        array is not tracked (only the quantized uint8 is kept) -- so this
        only affects the overview immediately; already-pooled tile items
        keep their existing quantization until next re-delivery. (This
        mirrors the fixed-levels contract: brightness is fixed at arrival
        time and does not silently rescale existing pixels.)"""
        self._display_lo = float(lo)
        self._display_hi = float(hi)
        if self._overview_arr is not None:
            self.view.overview_item.setImage(
                self._overview_arr, autoLevels=False,
                levels=(self._display_lo, self._display_hi))

    # ── level selection ───────────────────────────────────────────────────

    def _pick_display_level_with_hysteresis(self, screen_px_per_world_px: float) -> int:
        ideal_level = self._pick_display_level(screen_px_per_world_px)
        if ideal_level == self.level:
            return self.level
        if screen_px_per_world_px <= 0:
            return ideal_level
        ideal_ds = 1.0 / screen_px_per_world_px
        cur_ds = self.provider.level_downsample(self.level)
        ratio = ideal_ds / cur_ds if cur_ds else 1.0
        if abs(ratio - 1.0) > self.LEVEL_HYSTERESIS:
            return ideal_level
        return self.level

    def _pick_display_level(self, screen_px_per_world_px: float) -> int:
        """Nearest-below choice: for a given zoom (screen pixels per WORLD
        pixel), the ideal pyramid level has downsample ~= 1 /
        screen_px_per_world_px. Picks the level whose downsample is the
        largest one that does not exceed that ideal ratio, falling back to
        the finest level if none qualifies, and the coarsest if the ideal
        ratio is smaller than every available downsample."""
        if screen_px_per_world_px <= 0:
            return 0
        ideal_ds = 1.0 / screen_px_per_world_px
        best_level = 0
        best_ds = self.provider.level_downsample(0)
        for level in range(self.provider.num_levels):
            ds = self.provider.level_downsample(level)
            if ds <= ideal_ds and ds >= best_ds:
                best_level = level
                best_ds = ds
        return best_level

    # ── viewport / range-change handling ─────────────────────────────────

    def _on_range_changed(self, *_args):
        """Runs on EVERY sigRangeChanged (no debounce here -- module
        docstring "Camera contract"). Cheap-only work: recompute the wanted
        tile set + z-order/visibility bookkeeping and run the prune check.
        Actual scheduler request issuing is what's debounced (below)."""
        t0 = time.perf_counter() if self.probe else None

        vb = self.view.view_box
        (x0, x1), (y0, y1) = vb.viewRange()
        screen_w = max(1, vb.width())
        world_w = max(1e-9, x1 - x0)
        screen_px_per_world_px = screen_w / world_w
        new_level = self._pick_display_level_with_hysteresis(screen_px_per_world_px)

        h0, w0 = self.provider.level_shape(0)
        wy0, wy1 = max(0, min(y0, h0)), max(0, min(y1, h0))
        wx0, wx1 = max(0, min(x0, w0)), max(0, min(x1, w0))
        bbox_l0 = (int(wy0), int(wx0), int(wy1), int(wx1))
        self._current_bbox = bbox_l0

        self.level = new_level
        ds = self.provider.level_downsample(self.level)
        bbox_level = (
            int(bbox_l0[0] / ds), int(bbox_l0[1] / ds),
            int(bbox_l0[2] / ds), int(bbox_l0[3] / ds),
        )
        self._visible_tiles = tiles_covering(bbox_level, self.grid.tile_size)
        self._update_precise_visibility()

        viewport_rect = QRectF(x0, y0, x1 - x0, y1 - y0)
        margin_world = self.grid.tile_size * ds * PRUNE_MARGIN_TILES
        keep = {(self.level, tx, ty) for tx, ty in self._visible_tiles}
        self._raw_pool.prune(self.level, viewport_rect, margin_world, keep)
        self._precise_pool.prune(self.level, viewport_rect, margin_world, keep)
        self.stats["items_created"] = self._raw_pool.items_created + self._precise_pool.items_created
        self.stats["items_pruned"] = self._raw_pool.items_pruned + self._precise_pool.items_pruned

        # Debounced: only ISSUING raw requests waits for motion to settle.
        self._motion_timer.start(self.MOTION_MS)
        self._settle_timer.start(self.settle_ms)

        if self.probe and t0 is not None:
            dt_ms = (time.perf_counter() - t0) * 1000.0
            self.timings["range_handler_ms"].append(dt_ms)
            self.timings["frame_events"].append((time.perf_counter(), dt_ms))

    def _issue_raw_requests(self):
        """Debounced request-issuing callback (module docstring). Cancels
        the previous raw generation and issues center-out requests for the
        current wanted set's cache misses."""
        t0 = time.perf_counter() if self.probe else None

        self.scheduler.cancel_generation(self.view_generation)
        self._raw_gen_n += 1
        self.view_generation = ("raw", self._raw_gen_n)

        bbox_l0 = self._current_bbox
        if bbox_l0 is None:
            return
        ds = self.provider.level_downsample(self.level)
        bbox_level = (
            int(bbox_l0[0] / ds), int(bbox_l0[1] / ds),
            int(bbox_l0[2] / ds), int(bbox_l0[3] / ds),
        )
        visible = self._visible_tiles

        if self.probe:
            self._raw_probe_batch = {
                "start": time.perf_counter(), "visible": set(visible),
                "first": False, "full": False,
            }

        cy = (bbox_level[0] + bbox_level[2]) / 2.0
        cx = (bbox_level[1] + bbox_level[3]) / 2.0
        tile_size = self.grid.tile_size

        def dist(coord):
            tx, ty = coord
            tcy = ty * tile_size + tile_size / 2.0
            tcx = tx * tile_size + tile_size / 2.0
            return (tcy - cy) ** 2 + (tcx - cx) ** 2

        missing = sorted(
            (coord for coord in visible
             if self._raw_pool.get(self.level, *coord) is None),
            key=dist,
        )
        gen = self.view_generation
        for i, (tx, ty) in enumerate(missing):
            key = self._make_raw_key(tx, ty)
            req = TileRequest(key=key, generation=gen, priority=i)
            self.scheduler.request(req, self._on_raw_result)

        self._maybe_exit_provisional()

        if self.probe and t0 is not None:
            dt_ms = (time.perf_counter() - t0) * 1000.0
            self.timings["request_issue_ms"].append(dt_ms)
            self.timings["frame_events"].append((time.perf_counter(), dt_ms))

    def jump_to(self, y0: int, x0: int, w: int, h: int):
        """Navigator / checkpoint jump: level-0 coordinates. Actually moves
        the camera (ViewBox.setRange with the world rect, padding=0) so the
        real range-changed handling path runs. The settled batch AND the
        debounced raw-request batch are then both fired immediately
        (bypassing their timers)."""
        rect = QRectF(float(x0), float(y0), float(w), float(h))
        self._jumping = True
        try:
            self.view.view_box.setRange(rect=rect, padding=0)
        finally:
            self._jumping = False
        self._motion_timer.stop()
        self._issue_raw_requests()
        self._settle_timer.stop()
        self._on_settle()

    # ── settle / precise request ─────────────────────────────────────────

    def _on_settle(self):
        self._issue_settled_request()

    def _issue_settled_request(self):
        """Cancel the previous settled generation, start a new one, and
        issue CorrectionKeys for the current selection (skipping precise
        entirely when method is None)."""
        self.scheduler.cancel_generation(self._settled_generation)
        self._settled_gen_n += 1
        self._settled_generation = ("precise", self._settled_gen_n)
        gen = self._settled_generation

        if not self._wants_precise() or self._current_bbox is None:
            return

        ds = self.provider.level_downsample(self.level)
        bbox_level = (
            int(self._current_bbox[0] / ds), int(self._current_bbox[1] / ds),
            int(self._current_bbox[2] / ds), int(self._current_bbox[3] / ds),
        )
        tile_size = self.grid.tile_size
        visible = tiles_covering(bbox_level, tile_size)

        cy = (bbox_level[0] + bbox_level[2]) / 2.0
        cx = (bbox_level[1] + bbox_level[3]) / 2.0

        def dist(coord):
            tx, ty = coord
            tcy = ty * tile_size + tile_size / 2.0
            tcx = tx * tile_size + tile_size / 2.0
            return (tcy - cy) ** 2 + (tcx - cx) ** 2

        if self.probe:
            self._precise_probe_batch = {
                "start": time.perf_counter(), "visible": set(visible),
                "first": False, "full": False,
            }

        ordered = sorted(visible, key=dist)
        for i, (tx, ty) in enumerate(ordered):
            key = self._make_correction_key(tx, ty)
            req = TileRequest(key=key, generation=gen, priority=i)
            self.scheduler.request(req, self._on_precise_result)

    # ── delivery: raw ─────────────────────────────────────────────────────

    def _on_raw_result(self, result):
        """Scheduler callback — fires on a worker thread (or synchronously
        on a cache hit). Marshal to the GUI thread via a queued signal."""
        self._raw_delivered.emit(result)

    def _handle_raw_result(self, result):
        """GUI-side delivery guard (module docstring): the generation must
        match the LIVE view_generation exactly (not merely "not stale" --
        immune to the queued-signal race), the tile must still be in the
        current wanted set at the current level, and channel/source/level
        must match."""
        if result.error is not None or result.pixels is None:
            return
        req = result.request
        key = req.key
        if not isinstance(key, RawKey):
            return
        if req.generation != self.view_generation:
            self.stats["late_raw_rejected"] += 1
            return
        tile = key.tile
        current_source = self.provider.source_identity()
        if key.channel != self.channel or key.source != current_source or \
                tile.level != self.level:
            self.stats["mismatched_raw_dropped"] += 1
            return
        if (tile.tx, tile.ty) not in self._visible_tiles:
            self.stats["late_raw_rejected"] += 1
            return

        t0 = time.perf_counter() if self.probe else None
        arr = result.pixels.handle
        rgba_or_gray = self._quantize_tile_uint8(arr)
        ds_y, ds_x = self._downsample_yx(tile.level)
        rect = ExploreView.world_rect(
            tile.ty * self.grid.tile_size, tile.tx * self.grid.tile_size,
            arr.shape[0], arr.shape[1], ds_y, ds_x)
        self._raw_pool.put(tile.level, tile.tx, tile.ty, rect, rgba_or_gray, key)
        self.stats["raw_tiles_blitted"] += 1
        self.stats["items_created"] = self._raw_pool.items_created + self._precise_pool.items_created
        if self.probe and t0 is not None:
            dt_ms = (time.perf_counter() - t0) * 1000.0
            self.timings["tile_item_update_ms"].append(dt_ms)
            self.timings["frame_events"].append((time.perf_counter(), dt_ms))
            self._probe_note_raw_progress(tile.tx, tile.ty)

    # ── delivery: precise ─────────────────────────────────────────────────

    def _on_precise_result(self, result):
        self._precise_delivered.emit(result)

    def _handle_precise_result(self, result):
        req = result.request
        if req.generation != self._settled_generation:
            self.stats["stale_precise_dropped"] += 1
            return
        if result.error is not None or result.pixels is None:
            return
        key = req.key
        if not isinstance(key, CorrectionKey):
            return
        current_ctx = self.selection_key_context()
        if not self._key_matches_context(key, current_ctx):
            self.stats["mismatched_key_dropped"] += 1
            return
        tile = key.tile
        if (tile.tx, tile.ty) not in self._visible_tiles or tile.level != self.level:
            self.stats["late_precise_rejected"] += 1
            return

        t0 = time.perf_counter() if self.probe else None
        arr = result.pixels.handle
        gray = self._quantize_tile_uint8(arr)
        ds_y, ds_x = self._downsample_yx(tile.level)
        rect = ExploreView.world_rect(
            tile.ty * self.grid.tile_size, tile.tx * self.grid.tile_size,
            arr.shape[0], arr.shape[1], ds_y, ds_x)
        self._precise_pool.put(tile.level, tile.tx, tile.ty, rect, gray, key)
        self.stats["precise_tiles_blitted"] += 1
        self.stats["items_created"] = self._raw_pool.items_created + self._precise_pool.items_created
        if self.probe and t0 is not None:
            dt_ms = (time.perf_counter() - t0) * 1000.0
            self.timings["tile_item_update_ms"].append(dt_ms)
            self.timings["frame_events"].append((time.perf_counter(), dt_ms))
            self._probe_note_precise_progress(tile.tx, tile.ty)

        self._maybe_exit_provisional()
        self._update_precise_visibility()

    # ── quantization (once, at arrival) ─────────────────────────────────

    def _quantize_tile_uint8(self, arr: np.ndarray) -> np.ndarray:
        """uint8 grayscale quantization under the FIXED display levels,
        performed exactly ONCE per delivered tile (module docstring).
        `rgb = round(clip((v-lo)/(hi-lo), 0, 1) * 255)`; no alpha channel
        is needed since each tile item is fully opaque within its own rect
        (there is no shared canvas with unfilled holes any more)."""
        span = max(self._display_hi - self._display_lo, 1e-6)
        norm = np.clip((arr.astype(np.float32, copy=False) - self._display_lo) / span, 0.0, 1.0)
        return np.round(norm * 255.0).astype(np.uint8)

    # ── probe-only viewport-first/full progress tracking ─────────────────

    def _probe_note_raw_progress(self, tx, ty):
        batch = self._raw_probe_batch
        if batch is None or (tx, ty) not in batch["visible"]:
            return
        now = time.perf_counter()
        blitted = {(e.tx, e.ty) for e in self._raw_pool.entries.values() if e.level == self.level}
        if not batch["first"]:
            batch["first"] = True
            self.timings["viewport_first_raw_tile_ms"].append((now - batch["start"]) * 1000.0)
        if not batch["full"] and batch["visible"] <= blitted:
            batch["full"] = True
            self.timings["viewport_full_raw_tile_ms"].append((now - batch["start"]) * 1000.0)

    def _probe_note_precise_progress(self, tx, ty):
        batch = self._precise_probe_batch
        if batch is None or (tx, ty) not in batch["visible"]:
            return
        now = time.perf_counter()
        ctx = self.selection_key_context()
        covered = {
            (e.tx, e.ty) for e in self._precise_pool.entries.values()
            if e.level == self.level and e.key is not None and self._key_matches_context(e.key, ctx)
        }
        if not batch["first"]:
            batch["first"] = True
            self.timings["viewport_first_precise_tile_ms"].append((now - batch["start"]) * 1000.0)
        if not batch["full"] and batch["visible"] <= covered:
            batch["full"] = True
            self.timings["viewport_full_precise_ms"].append((now - batch["start"]) * 1000.0)

    # ── teardown ──────────────────────────────────────────────────────────

    def teardown(self, shutdown_backend: bool = True):
        """Stop timers, disconnect signals, then (if `shutdown_backend`)
        scheduler.shutdown(), then provider.close(). Order recorded in
        `_teardown_order`."""
        if self._torn_down:
            return
        self._torn_down = True

        self._settle_timer.stop()
        self._motion_timer.stop()
        try:
            self.view.view_box.sigRangeChanged.disconnect(self._on_range_changed)
        except (TypeError, RuntimeError):
            pass
        try:
            self._raw_delivered.disconnect(self._handle_raw_result)
        except (TypeError, RuntimeError):
            pass
        try:
            self._precise_delivered.disconnect(self._handle_precise_result)
        except (TypeError, RuntimeError):
            pass

        if not shutdown_backend:
            return

        self._teardown_order.append("scheduler.shutdown")
        self.scheduler.shutdown()
        self._teardown_order.append("provider.close")
        self.provider.close()
