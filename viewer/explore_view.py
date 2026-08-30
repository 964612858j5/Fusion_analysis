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

## Level switching without clearing; active draw set

Switching the displayed pyramid level does NOT clear previously-drawn
items from either pool -- their world rects remain correct (they were
computed from their own level's geometry) so they stay visually aligned;
only their z-order and VISIBILITY change relative to the new level's
tiles. Each pooled item's zValue is `layer_base_z + (num_levels - level)`,
so a FINER level (smaller `level` int, higher resolution) draws ABOVE a
coarser one within the same layer -- caching is not visibility, and this
z-order fact is exactly why an item finer than the current level can never
be allowed to stay visible: it would draw on top and show stale,
differently-corrected pixels (this was bug: a leftover fine-level tile
sitting at the viewport center after zoom-out never got pruned because it
was inside the viewport, and it stayed both cached AND visible forever).
`TileItemPool.apply_visibility(current_level, ...)` is the active-draw-set
policy that fixes this: an item strictly FINER than `current_level` is
ALWAYS hidden (never drawn, regardless of budget/viewport); an item AT
`current_level` follows the caller's `current_level_visible` flag; an item
COARSER than `current_level` (a fallback while the current level fills in)
follows `coarser_visible`. Hiding is independent of pooling/pruning --
a hidden item stays cached and is instantly re-shown the moment zooming
back in makes it the current (or a coarser-fallback) level again. Pruning
(eviction from the pool entirely) still only happens once the per-layer
budget is exceeded, for items both off-level and outside the viewport
(with margin).

## Anti-checkerboard for precise tiles (design doc §1.2 / cheap group gate)
## + corrected floor + single-stage motion guarantee

Corrected (precise) tiles are brightness-normalized per-tile relative to
their own local background; sitting a corrected tile next to a raw tile
produces a visible cross-stage seam. `_coverage_complete()` still computes
a single cheap boolean -- "every tile in the CURRENT wanted set (at the
current level, under the current selection context) has a matching,
current `CorrectionKey` recorded against it" -- and `_precise_visible` /
`view.precise_visible` still mean exactly that; `_maybe_exit_provisional`
still uses it for the provisional badge. What CHANGED is what that boolean
is used to gate.

An earlier round used `covered` as an ATOMIC visibility gate on the whole
current level: any single missing tile (e.g. one more tile entering the
wanted set mid-pan) hid EVERY current-level precise item at once, dropping
the whole viewport down to the coarser fallback and snapping back --
visible as a whole-viewport flash on every camera motion, worse than the
bug it was meant to prevent. That was only ever justified as anti-
checkerboard protection against a corrected tile sitting next to a RAW
tile (a hard cross-stage seam). It was NOT protection against a corrected
tile sitting next to another CORRECTED-stage image (a coarser precise tile
or the corrected floor) -- that is a sharpness difference, not a stage
seam, and was always fine to show.

So the atomic gate now applies ONLY while raw pixels can still reach the
screen, i.e. only until the corrected floor is ready (`floor_ok` below):

    current_level_visible = True if floor_ok else covered

Once the floor is ready the raw layer is forced entirely invisible (next
paragraph), so whatever sits under a missing current-level precise tile is
itself corrected-stage -- floor, or a coarser precise tile -- and current-
level tiles are shown PROGRESSIVELY, per tile, the moment each one lands
(still gated by `key_ok=_precise_key_current_for_level`, so a shown tile
can never be stale-method/stale-radius). This is a DELIBERATE trade made
after manual testing, not an oversight: newly-exposed regions may briefly
show the coarser floor beneath already-sharp neighboring tiles (a visible
sharpness boundary) in exchange for the viewport never flashing all at
once. Before the floor is ready, the old atomic behavior is unchanged
(anti-checkerboard against raw still holds), and COARSER-level precise
items are, as before, exempt from `covered` entirely and stay visible
underneath as a sharper-than-floor fallback while the current level fills
in -- they are the same (corrected) stage as the current-level tiles, so
there is no cross-stage seam, only a sharper/blurrier transition.

The raw layer holds a different, single-stage pixel pipeline than the
precise layer, so raw-next-to-precise is itself a seam. When no correction
method is selected the raw layer is simply always shown (there is nothing
else to show). When a method IS selected, the raw layer must never be the
thing the user sees mixed with corrected pixels during motion -- that was
the other bug: `_update_precise_visibility` used to hide only the ENTIRE
precise layer on incomplete coverage, which let the always-visible raw
layer show through and made the whole image flash brighter every time the
camera moved. The fix is a "corrected floor": `ExploreController`
maintains one extra always-covering item, `view.corrected_floor_item`
(z=`FLOOR_Z`, between the raw overview at z=0 and the raw tile pool at
`RAW_BASE_Z`) -- a whole-array correction of a coarse pyramid level
(`_pick_floor_level_and_stride`), computed off the GUI thread and requantized whenever
selection changes. Once that floor is ready for the current selection
context, it is shown and the ENTIRE raw layer (both current- and
coarser-level items) is forced invisible via `apply_visibility`, so the
screen shows only corrected-stage pixels: floor -> coarser corrected tiles
-> current-level corrected tiles, in increasing sharpness, and NEVER a
raw-stage pixel. The raw layer is the honest fallback only until the floor
is ready for the very first time after a selection change (better than a
black screen); the always-resident raw overview (z=0) is the ultimate
never-black-screen guarantee underneath everything, but once the floor
covers the world rect it is never actually visible.

The floor is an INTERACTIVE-quality DISPLAY PROXY ONLY -- it is never
claimed to match production numerics; it exists solely so the screen never
shows raw-stage pixels while in corrected mode. When no pyramid level is
both >= `FLOOR_MIN_MAX_DIM` on its long side and under the `FLOOR_MAX_PIXELS`
safety cap, `_pick_floor_level_and_stride` falls back to decimating the
coarsest big-enough level by an integer stride `k` -- via an area/box-mean
downsample (`_box_downsample`), not point-sampling, so a bright structure
that happens to land off the sample grid is never missed or over-weighted
-- so the correction kernel still runs on a bounded number of pixels; the
floor's
effective per-axis downsample becomes `(ds_y*k, ds_x*k)` and the
correction param is scaled by the same total factor, so the decimated
floor still lands exactly on the full world rect at the right physical
scale.

A stale coarser-level precise tile is exactly the same class of bug as a
stale raw tile: `TileItemPool.apply_visibility` accepts an optional
`key_ok(entry.key, entry.level)` predicate so the precise pool can ALSO
require a coarser entry's `CorrectionKey` to still match the current
selection context (recomputed at THAT entry's level, since
`effective_param` is level-dependent) before it is allowed to stay
visible -- an old-method/old-radius coarse tile is hidden immediately on
a selection change, never just faded back in as a stale fallback. The raw
pool needs no such predicate: `RawKey` carries no method/params, and
source/channel mismatches are already rejected at delivery time.

Only ONE corrected-floor computation ever runs at a time
(`_floor_job_running`). A selection change that arrives while a floor job
is in flight does not start a second one -- it bumps `_floor_gen` (so the
in-flight job's result is dropped as stale on arrival, by the same
generation/context guard as `_handle_precise_result`) and coalesces into a
single pending flag; when the stale result lands, the pending job (using
whatever selection is CURRENT at that point, i.e. always the latest) is
what actually starts. This guarantees the floor path never launches two
overlapping GPU/CPU-bound corrections.

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

## Per-level display gain for CORRECTED pixels (interactive display only)

Background correction is a high-pass operation, and a downsampled pyramid
level has already lost the high frequencies it acts on -- so the SAME
tissue, corrected at different pyramid levels with the level-scaled radius
(`effective_param`), comes out at systematically different brightness.
Measured on the real tonsil slide (channel index 1, tophat radius 25, a
4096x4096 level-0 tissue window): corrected mean drops from 3.69 (level 0,
effective radius 25) to 2.29 (level 1, radius 6) to 1.33 (level 2, radius
2) to 0.46 (level 3, radius 1); corrected p99.5 drops from 16 to 13 to 9 to
5 over the same levels. Every corrected image (tiles and the floor) is
quantized through one fixed display range derived from the RAW overview,
so the floor's level renders noticeably darker than level 0 -- a brightness
jump whenever the displayed level changes or the floor shows through. No
radius tuning fixes this: the signal genuinely is not present at coarse
levels.

`ExploreController._level_gain` is a per-pyramid-level multiplicative gain,
calibrated once per selection (`_calibrate_level_gains`, module-level
constants `GAIN_WINDOW_L0`/`GAIN_WINDOWS`/`GAIN_PERCENTILE`/`GAIN_CLAMP`)
and applied ONLY when quantizing CORRECTED pixels (`_quantize_corrected_
uint8`, used by the precise-tile and floor delivery paths) -- never to raw
pixels, which vary only ~1.2x across levels and carry no correction. The
table is installed only alongside a successful, context-matching corrected-
floor result (`_handle_floor_result`); `_display_gain_for_level` returns
1.0 for every level whenever the live selection no longer matches the
context the table was calibrated for, so a stale or absent table never
silently scales pixels.

Read the following plainly, and do not extend these claims:

- This is an INTERACTIVE DISPLAY gain only. It never touches computed,
  cached, or saved pixel values -- the preview's brightness therefore does
  NOT match production output.
- It aligns HIGHLIGHTS (a p99.5 ratio between level 0 and level L), not the
  whole distribution. Coarse levels stay darker in the midtones because the
  signal is genuinely absent there -- measured: level 2's corrected p50 is
  0 where level 0's is 3.
- Expected effect: the ~2.8x mean mismatch between the floor level and
  level 0 drops to roughly 1.2-1.5x. It is NOT eliminated.
- The calibrated gain varies by about 25% depending on which tissue window
  is sampled; the median over `GAIN_WINDOWS` (3) windows is a variance
  reduction, not a guarantee.

Calibration runs on the SAME worker thread as the existing corrected-floor
job (`_start_floor_job`'s `work()` closure) -- there is no second thread or
second single-flight mechanism; the floor's generation/context guard
(`_floor_gen` / `_floor_job_running` / `_floor_pending`) covers calibration
too. A calibration failure is caught, leaves the gain table empty (all
1.0), increments `stats["gain_calibration_failed"]`, and never costs the
floor itself -- a successful floor is installed regardless of whether
calibration succeeded.
"""

import threading
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

# Corrected-floor item z: between the pinned raw overview (0) and the raw
# tile pool (RAW_BASE_Z) -- it must draw above the overview (it is the
# thing that replaces the overview as the never-black-screen fallback in
# corrected mode) but always below any raw tile, in case the raw layer
# is ever forced visible as the honest fallback (module docstring).
FLOOR_Z = 50

# Default per-layer pooled-item budget (design doc: "e.g. 400 per layer").
DEFAULT_ITEM_BUDGET = 400

# Prune-eligibility viewport margin, in TILE units (matches the canvas-era
# cover margin) -- an item within this many tiles of the current viewport,
# even off-level, is kept a little longer to absorb small pans.
PRUNE_MARGIN_TILES = 1

# Corrected-floor level selection (module docstring "corrected floor"):
# among pyramid levels, pick the COARSEST one whose max(h, w) still meets
# FLOOR_MIN_MAX_DIM and whose pixel count stays under the safety cap. If no
# level satisfies both, the coarsest level meeting FLOOR_MIN_MAX_DIM alone
# is decimated by an integer stride to bring it under the cap (see
# `_pick_floor_level_and_stride`).
FLOOR_MIN_MAX_DIM = 1024
FLOOR_MAX_PIXELS = 4_000_000

# Per-level display-gain calibration (module docstring "Per-level display
# gain for CORRECTED pixels"): 3 tissue-dense, non-overlapping level-0
# windows, GAIN_WINDOW_L0 pixels square, calibrated against the p99.5
# highlight percentile, gain clamped to GAIN_CLAMP.
GAIN_WINDOW_L0 = 2048
GAIN_WINDOWS = 3
GAIN_PERCENTILE = 99.5
GAIN_CLAMP = (1.0, 8.0)


def _pick_calibration_windows(overview_arr: np.ndarray, ds_y: float, ds_x: float,
                               window_l0: int = GAIN_WINDOW_L0,
                               n_windows: int = GAIN_WINDOWS):
    """Pick up to `n_windows` tissue-dense, NON-OVERLAPPING `window_l0`
    (level-0 pixels) square windows using block means over the already-
    resident `overview_arr`. Blocks are laid out on a simple non-
    overlapping grid at the window size expressed in overview-level pixels
    (`window_l0 / ds_*`), scored by mean, and the highest-scoring blocks are
    returned as level-0 top-left corners `(y0, x0)`. Falls back to one
    window, centred and clamped to the image, when the window does not fit
    the overview at all (a tiny image) or the overview array is empty."""
    if overview_arr is None or overview_arr.size == 0:
        return []
    h, w = overview_arr.shape
    win_h = max(1, min(h, int(round(window_l0 / ds_y))))
    win_w = max(1, min(w, int(round(window_l0 / ds_x))))
    n_by = h // win_h
    n_bx = w // win_w

    if n_by < 1 or n_bx < 1:
        y0 = max(0, (h - win_h) // 2)
        x0 = max(0, (w - win_w) // 2)
        return [(int(round(y0 * ds_y)), int(round(x0 * ds_x)))]

    scores = []
    for by in range(n_by):
        for bx in range(n_bx):
            y0, x0 = by * win_h, bx * win_w
            block = overview_arr[y0:y0 + win_h, x0:x0 + win_w]
            scores.append((float(block.mean()), by, bx))
    scores.sort(key=lambda t: t[0], reverse=True)

    windows_l0 = []
    for _score, by, bx in scores[:n_windows]:
        y0_l0 = int(round(by * win_h * ds_y))
        x0_l0 = int(round(bx * win_w * ds_x))
        windows_l0.append((y0_l0, x0_l0))
    return windows_l0


def _box_downsample(arr: np.ndarray, k: int) -> np.ndarray:
    """Area/box-mean downsample by integer stride `k` on both axes: crop to
    a `(h // k * k, w // k * k)` multiple, reshape to `(h//k, k, w//k, k)`,
    and mean over the two `k`-sized axes -- unlike point-sampling
    (`arr[::k, ::k]`), this cannot miss or over-weight a bright structure
    that happens to land off the sample grid. `k == 1` returns `arr`
    unchanged (no-op). Always returns a C-contiguous float32 array.

    NOTE for the real tonsil pyramid this path is INACTIVE: the floor
    level there is 1963x1800 = 3.53 MP, under `FLOOR_MAX_PIXELS`
    (4,000,000), so `_pick_floor_level_and_stride` picks `k == 1` and this
    function is a no-op. This change is robustness for OTHER pyramid
    layouts (where no level is small enough to skip decimation), not a fix
    for any currently-observed artifact on the tonsil data."""
    if k <= 1:
        return np.ascontiguousarray(arr, dtype=np.float32)
    arr = arr.astype(np.float32, copy=False)
    h, w = arr.shape
    h_crop, w_crop = (h // k) * k, (w // k) * k
    cropped = arr[:h_crop, :w_crop]
    reshaped = cropped.reshape(h_crop // k, k, w_crop // k, k)
    return np.ascontiguousarray(reshaped.mean(axis=(1, 3), dtype=np.float32))


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

    def apply_visibility(self, current_level: int, *,
                         current_level_visible: bool = True,
                         coarser_visible: bool = True,
                         key_ok=None) -> None:
        """Per-level visibility policy. An item FINER than `current_level`
        (entry.level < current_level) is NEVER visible -- it would draw
        above the current level (higher z) and show stale, differently-
        corrected pixels. Items stay in the pool (cached, instantly
        re-shown on zoom back in); only their visibility changes.

        `key_ok`, when given, is called as `key_ok(entry.key, entry.level)`
        and must return True for the entry to be eligible at all; an entry
        whose key does not match the caller's current context is hidden
        regardless of level. This keeps selection-context knowledge (e.g.
        "does this CorrectionKey still match the live method/params?") out
        of this dumb item-container class -- the caller supplies it."""
        for entry in self.entries.values():
            if entry.level < current_level:
                entry.item.setVisible(False)
                continue
            if key_ok is not None and not key_ok(entry.key, entry.level):
                entry.item.setVisible(False)
                continue
            if entry.level == current_level:
                entry.item.setVisible(current_level_visible)
            else:
                entry.item.setVisible(coarser_visible)

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

        # Corrected floor (module docstring "corrected floor + single-stage
        # motion guarantee"): a whole-array corrected preview at a coarse
        # pyramid level, shown while precise tiles fill in so raw-stage
        # pixels never reach the screen in corrected mode.
        self.corrected_floor_item = pg.ImageItem(axisOrder="row-major")
        self.corrected_floor_item.setZValue(FLOOR_Z)
        self.view_box.addItem(self.corrected_floor_item)
        self.corrected_floor_item.setVisible(False)

        # Always-available in-view status badge (e.g. "Preparing corrected
        # preview…") -- a window-title suffix is easy to miss, and a future
        # Step0 host may not even own a window title to append to. Parented
        # to the widget itself (not the graphics scene) so it floats over
        # the graphics area in WIDGET coordinates, unaffected by camera
        # pan/zoom. Transparent-for-mouse so it never eats camera input.
        self.status_label = QtWidgets.QLabel(self)
        self.status_label.setStyleSheet(
            "background-color: rgba(0, 0, 0, 160); color: #eeeeee;"
            "padding: 4px 8px; border-radius: 3px; font-size: 11px;")
        self.status_label.move(8, 8)
        self.status_label.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self.status_label.setVisible(False)
        self.status_label.adjustSize()
        self.status_label.raise_()

    def set_status_text(self, text: Optional[str]):
        """Show `text` in the in-view status badge, or hide it entirely
        when `text` is empty/None."""
        if not text:
            self.status_label.setVisible(False)
            return
        self.status_label.setText(text)
        self.status_label.adjustSize()
        self.status_label.setVisible(True)
        self.status_label.raise_()

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
    floor_preparing_changed = QtCore.pyqtSignal(bool)
    # Emitted from `_handle_floor_result`: True when a floor result was
    # accepted (current generation/context, no error), False when it was
    # dropped as stale or failed to compute. Lets a host (or this class
    # itself, for the in-view status badge) know the floor's outcome
    # without polling `_floor_ready`.
    floor_ready_changed = QtCore.pyqtSignal(bool)

    # Internal cross-thread delivery signals (scheduler callbacks fire on
    # worker threads; Qt widgets must only be touched on the GUI thread).
    _raw_delivered = QtCore.pyqtSignal(object)
    _precise_delivered = QtCore.pyqtSignal(object)
    _floor_delivered = QtCore.pyqtSignal(object)

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

        # ── corrected floor state (module docstring) ──
        self._floor_level: Optional[int] = None
        self._floor_stride: int = 1
        self._floor_ready = False
        self._floor_ctx = None
        self._floor_gen = 0
        self._floor_job_running = False
        self._floor_pending = False
        self._floor_threads = []

        # ── per-level display gain for corrected pixels (module docstring
        # "Per-level display gain for CORRECTED pixels") ──
        self._level_gain: Dict[int, float] = {}
        self._gain_ctx = None

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
            "floor_compute_failed": 0,
            "floor_level": None,
            "floor_stride": 1,
            "level_display_gain": {},
            "gain_calibrated": False,
            "gain_calibration_failed": 0,
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
        self._floor_delivered.connect(self._handle_floor_result, QtCore.Qt.QueuedConnection)

        self.view.view_box.sigRangeChanged.connect(self._on_range_changed)

        # Drive the in-view status badge ourselves (module docstring
        # "make the floor's state observable") -- a host need not connect
        # anything for the badge to work.
        self.floor_preparing_changed.connect(self._on_floor_preparing_changed_for_badge)

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
        if self._wants_precise():
            self._ensure_corrected_floor()
        if self._current_bbox is not None:
            self._issue_settled_request()

    def _enter_provisional(self):
        self._provisional = True
        self.provisional_changed.emit(True)
        self._update_layer_visibility()

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
        self._update_layer_visibility()

    def _coverage_complete(self) -> bool:
        ctx = self.selection_key_context()
        for tx, ty in self._visible_tiles:
            entry = self._precise_pool.get(self.level, tx, ty)
            if entry is None or entry.key is None or not self._key_matches_context(entry.key, ctx):
                return False
        return True

    def _update_layer_visibility(self):
        """Display-policy gate (module docstring: anti-checkerboard +
        corrected floor + single-stage motion guarantee + progressive
        per-tile corrected coverage).

        `covered` is the CURRENT-LEVEL precise coverage boolean (unchanged
        meaning/contract from the old `_update_precise_visibility` -- it
        still drives `_precise_visible` / `view.precise_visible` and
        `_maybe_exit_provisional`, exactly as before). `floor_ok`
        additionally requires a ready corrected-floor image that matches
        the live selection context. While `wants_precise()` is true and the
        floor is not yet ready, the raw layer stays visible as the honest
        (if brighter) fallback; once the floor IS ready, the raw layer is
        forced entirely invisible so no raw-stage pixel can ever appear
        alongside corrected pixels.

        `covered` is NOT used to gate current-level precise visibility any
        more (module docstring): once the floor is ready, anything under a
        missing current-level tile is itself corrected-stage, so tiles are
        shown progressively, per tile, as they land -- the atomic
        all-or-nothing gate is kept only for the floor-not-ready window,
        where a corrected tile next to raw would still be a hard seam."""
        wants = self._wants_precise()
        covered = wants and bool(self._visible_tiles) and self._coverage_complete()
        floor_ctx = self._current_floor_ctx(self._floor_level, self._floor_stride)
        floor_ok = wants and self._floor_ready and self._floor_ctx == floor_ctx

        self._precise_visible = covered
        self.view.precise_visible = covered

        self.view.corrected_floor_item.setVisible(floor_ok)

        raw_on = (not wants) or (not floor_ok)
        self._raw_pool.apply_visibility(
            self.level, current_level_visible=raw_on, coarser_visible=raw_on)

        current_level_visible = True if floor_ok else covered
        self._precise_pool.apply_visibility(
            self.level, current_level_visible=current_level_visible, coarser_visible=True,
            key_ok=self._precise_key_current_for_level)

    # Backward-compatible alias (pre-rename name).
    _update_precise_visibility = _update_layer_visibility

    @staticmethod
    def _key_matches_context(key: CorrectionKey, ctx) -> bool:
        source, channel, method, eff_params, level, quality = ctx
        return (
            key.source == source and key.channel == channel and key.method == method
            and key.params == eff_params and key.tile.level == level and key.quality == quality
        )

    def _precise_key_current_for_level(self, key, level) -> bool:
        """Like `_key_matches_context` but for an arbitrary (typically
        coarser) pooled level: source/channel/method/quality must match the
        LIVE selection and `key.params` must equal the effective params for
        `level` specifically -- `effective_param` is level-dependent, so a
        coarser entry's expected params are NOT the current level's. Used
        as the precise pool's `apply_visibility(key_ok=...)` predicate so a
        stale-method/stale-radius coarse tile is hidden immediately on a
        selection change rather than shown as a fallback."""
        if key is None:
            return False
        source = self.provider.source_identity()
        ds = self.provider.level_downsample(level)
        eff_params = tuple(effective_param(p, level, ds) for p in self.params)
        return (
            key.source == source and key.channel == self.channel and key.method == self.method
            and key.params == eff_params and key.tile.level == level and key.quality == self.quality
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

        if self._wants_precise():
            self._ensure_corrected_floor()

    # ── corrected floor (module docstring "corrected floor + single-stage
    # motion guarantee") ────────────────────────────────────────────────

    def _pick_floor_level_and_stride(self) -> Tuple[int, int]:
        """(level, stride) for the corrected floor. Prefers the COARSEST
        pyramid level whose max(h, w) >= FLOOR_MIN_MAX_DIM and whose pixel
        count stays under FLOOR_MAX_PIXELS (stride 1). If no level
        satisfies both, falls back to the coarsest level meeting
        FLOOR_MIN_MAX_DIM alone, decimated by the smallest integer stride
        `k` making `(h // k) * (w // k) <= FLOOR_MAX_PIXELS` (module
        docstring: the floor is an interactive-quality display proxy
        only). If every level is below FLOOR_MIN_MAX_DIM (a tiny image),
        the coarsest level is used at stride 1."""
        num_levels = self.provider.num_levels
        qualifying = []
        for level in range(num_levels):
            h, w = self.provider.level_shape(level)
            if max(h, w) >= FLOOR_MIN_MAX_DIM and h * w <= FLOOR_MAX_PIXELS:
                qualifying.append(level)
        if qualifying:
            return max(qualifying), 1

        big_enough = [
            level for level in range(num_levels)
            if max(self.provider.level_shape(level)) >= FLOOR_MIN_MAX_DIM
        ]
        if not big_enough:
            return num_levels - 1, 1

        level = max(big_enough)
        h, w = self.provider.level_shape(level)
        k = 1
        while (h // k) * (w // k) > FLOOR_MAX_PIXELS:
            k += 1
        return level, k

    def _current_floor_ctx(self, floor_level: Optional[int], stride: int = 1):
        """(source, channel, method, eff_params_at_floor_level) -- the
        identity tuple a ready floor image must match to be considered
        current for the LIVE selection (same scaling the tile path uses,
        via `effective_param`, but against the floor's TOTAL downsample
        `level_downsample(floor_level) * stride`)."""
        if floor_level is None:
            return None
        ds = self.provider.level_downsample(floor_level) * stride
        eff_params = tuple(effective_param(p, floor_level, ds) for p in self.params)
        source = self.provider.source_identity()
        return (source, self.channel, self.method, eff_params)

    def _display_gain_for_level(self, level: int) -> float:
        """Per-level display gain for CORRECTED pixels (module docstring).
        Returns 1.0 for EVERY level whenever `_gain_ctx` does not match the
        LIVE selection context -- a stale or never-calibrated table must
        never silently scale pixels."""
        if self._gain_ctx is None or self._gain_ctx != self._current_floor_ctx(
                self._floor_level, self._floor_stride):
            return 1.0
        return self._level_gain.get(level, 1.0)

    def _calibrate_level_gains(self, provider, compute, channel: str, method: str,
                                base_params: Tuple[int, ...],
                                overview_arr: np.ndarray, overview_level: int) -> Dict[int, float]:
        """Calibrate a per-level display gain (module docstring) by
        comparing the p99.5 highlight percentile of the SAME tissue window,
        corrected at every pyramid level with its level-scaled param,
        against level 0's. Runs entirely on the caller's thread (the floor
        worker thread -- see `_start_floor_job`). Returns `{level: gain}`;
        level 0 is always 1.0, a level with no non-degenerate contribution
        from any window is 1.0, every other gain is the MEDIAN ratio across
        `GAIN_WINDOWS` windows, clamped to `GAIN_CLAMP`."""
        num_levels = provider.num_levels
        base_param = int(base_params[0]) if base_params else 0

        ds_y, ds_x = self._downsample_yx_for(provider, overview_level)
        windows_l0 = _pick_calibration_windows(overview_arr, ds_y, ds_x)

        ratios_by_level: Dict[int, list] = {L: [] for L in range(num_levels)}
        for (y0_l0, x0_l0) in windows_l0:
            p995_by_level: Dict[int, float] = {}
            for L in range(num_levels):
                ds_L = provider.level_downsample(L)
                h_L, w_L = provider.level_shape(L)
                win_L = max(1, int(round(GAIN_WINDOW_L0 / ds_L)))
                y0 = max(0, min(int(y0_l0 / ds_L), max(0, h_L - 1)))
                x0 = max(0, min(int(x0_l0 / ds_L), max(0, w_L - 1)))
                y1 = min(h_L, y0 + win_L)
                x1 = min(w_L, x0 + win_L)
                arr, _off = provider.read_region(channel, L, y0, y1, x0, x1)
                arr = arr.astype(np.float32, copy=False)
                param = effective_param(base_param, L, ds_L)
                corrected = compute.correct_array(arr, method, param)
                p995_by_level[L] = float(np.percentile(corrected, GAIN_PERCENTILE)) if corrected.size else 0.0

            p0 = p995_by_level.get(0, 0.0)
            for L in range(num_levels):
                pL = p995_by_level[L]
                if p0 <= 0 or pL <= 0:
                    continue  # degenerate -- this window contributes nothing for level L
                ratios_by_level[L].append(p0 / pL)

        gain_lo, gain_hi = GAIN_CLAMP
        gains: Dict[int, float] = {}
        for L in range(num_levels):
            if L == 0:
                gains[0] = 1.0
                continue
            vals = ratios_by_level[L]
            if not vals:
                gains[L] = 1.0
                continue
            gains[L] = float(np.clip(np.median(vals), gain_lo, gain_hi))
        return gains

    @staticmethod
    def _downsample_yx_for(provider, level: int) -> Tuple[float, float]:
        fn = getattr(provider, "level_downsample_yx", None)
        if fn is not None:
            return fn(level)
        ds = provider.level_downsample(level)
        return ds, ds

    def _on_floor_preparing_changed_for_badge(self, preparing: bool):
        self.view.set_status_text("Preparing corrected preview…" if preparing else None)

    def _ensure_corrected_floor(self):
        """(Re)request the corrected floor for the current selection.
        Called whenever `_wants_precise()` is true from `set_selection()`
        and from `load_overview()` (if a method is already set).
        Immediately hides the floor and marks it not-ready (module
        docstring: never show a stale-context floor).

        At most one floor computation runs at a time
        (`_floor_job_running`); a request that arrives while one is in
        flight is coalesced into a single pending flag rather than
        starting a second worker thread -- the in-flight job's result gets
        dropped as stale on arrival (its `_floor_gen` token no longer
        matches), and `_handle_floor_result` starts the pending job then,
        always against whatever selection is CURRENT at that point."""
        self._floor_gen += 1
        gen = self._floor_gen
        self._floor_ready = False
        self.view.corrected_floor_item.setVisible(False)
        self.floor_preparing_changed.emit(True)
        self._update_layer_visibility()

        if self._overview_arr is None:
            # `_display_lo/_display_hi` are fixed by `load_overview()`.
            # Quantizing a floor before that would bake in the placeholder
            # (0.0, 1.0) range and paint a saturated-white floor over the
            # whole slide. `load_overview()` re-enters here once the levels
            # are set, so deferring is safe -- and `floor_preparing_changed`
            # has already been emitted True, which is honest: the floor IS
            # pending.
            return

        if self._floor_job_running:
            self._floor_pending = True
            return
        self._start_floor_job(gen)

    def _start_floor_job(self, gen: int):
        """Read the floor array (GUI thread; reuses `_overview_arr` when
        levels match) and dispatch `compute.correct_array` on a worker
        thread. Exactly one such job is ever in flight (enforced by
        `_ensure_corrected_floor`/`_handle_floor_result`)."""
        self._floor_job_running = True
        floor_level, stride = self._pick_floor_level_and_stride()
        self._floor_level = floor_level
        self._floor_stride = stride
        self.stats["floor_level"] = floor_level
        self.stats["floor_stride"] = stride
        ctx = self._current_floor_ctx(floor_level, stride)

        # Reuse the already-resident overview array when the floor lands on
        # the same level (the common case -- both pickers land on the
        # coarsest level above their min-dimension threshold). Otherwise the
        # read happens on the WORKER thread below: a whole-level
        # `read_region` is blocking I/O and must never run on the GUI
        # thread. The provider hands out per-thread persistent handles, so
        # reading from the worker is safe.
        overview_arr = None
        if floor_level == getattr(self, "_overview_level", None):
            overview_arr = self._overview_arr

        method = self.method
        eff_params = ctx[3]
        param = int(eff_params[0]) if eff_params else 0
        compute = self.compute
        provider = self.provider
        channel = self.channel
        k = stride
        base_params = self.params
        cal_overview_arr = self._overview_arr
        cal_overview_level = getattr(self, "_overview_level", floor_level)

        def work():
            try:
                arr_in = overview_arr
                if arr_in is None:
                    h, w = provider.level_shape(floor_level)
                    arr_in, _off = provider.read_region(channel, floor_level, 0, h, 0, w)
                    arr_in = arr_in.astype(np.float32, copy=False)
                arr_in = _box_downsample(arr_in, k)
                result_arr = compute.correct_array(arr_in, method, param)
                error = None
            except Exception as exc:  # noqa: BLE001 -- reported via signal, never raised on worker thread
                result_arr = None
                error = exc

            # Calibration (module docstring "Per-level display gain for
            # CORRECTED pixels"): folded into this same floor job/thread --
            # no second thread, no second single-flight mechanism. A
            # calibration failure must never cost the user their floor, so
            # it is caught independently of the floor computation above.
            gains: Dict[int, float] = {}
            gain_error = None
            try:
                gains = self._calibrate_level_gains(
                    provider, compute, channel, method, base_params,
                    cal_overview_arr, cal_overview_level)
            except Exception as exc:  # noqa: BLE001 -- reported via signal
                gains = {}
                gain_error = exc

            self._floor_delivered.emit(
                (gen, ctx, floor_level, stride, result_arr, error, gains, gain_error))

        t = threading.Thread(target=work, daemon=True, name="explore-floor-compute")
        # Drop already-finished threads so a long session with many
        # selection changes does not retain one dead Thread per change.
        self._floor_threads = [x for x in self._floor_threads if x.is_alive()]
        self._floor_threads.append(t)
        t.start()

    def _handle_floor_result(self, payload):
        """GUI-side delivery guard (module docstring), same discipline as
        `_handle_precise_result`: drop the result if its generation token
        no longer matches the live `_floor_gen`, or if the selection
        context has since changed. Then, if a newer request was coalesced
        in while this job ran, start it -- never two jobs in flight."""
        gen, ctx, floor_level, stride, result_arr, error, gains, gain_error = payload
        self._floor_job_running = False
        if self._torn_down:
            self._floor_pending = False
            return

        current = gen == self._floor_gen and ctx == self._current_floor_ctx(floor_level, stride)
        accepted = False
        if current:
            if gain_error is not None:
                self.stats["gain_calibration_failed"] += 1
                gains = {}
            # Install the gain table under the same context guard as the
            # floor -- independent of whether the floor computation itself
            # succeeded (module docstring: "a failed calibration must not
            # cost the user their floor", and symmetrically a failed floor
            # must not cost the user a successful calibration).
            self._level_gain = gains
            self._gain_ctx = ctx if gains else None
            if error is not None or result_arr is None:
                self.stats["floor_compute_failed"] += 1
            else:
                # NOTE (module docstring): when stride > 1 the floor's
                # effective downsample exceeds its level's, so
                # gain[floor_level] slightly under-corrects the floor
                # specifically -- accepted, and inactive on the real data
                # (stride is 1 there).
                gray = self._quantize_corrected_uint8(result_arr, floor_level)
                ds_y, ds_x = self._downsample_yx(floor_level)
                ds_y, ds_x = ds_y * stride, ds_x * stride
                h, w = result_arr.shape
                rect = ExploreView.world_rect(0, 0, h, w, ds_y, ds_x)
                self.view.corrected_floor_item.setImage(gray, autoLevels=False, levels=(0, 255))
                self.view.corrected_floor_item.setRect(rect)
                self._floor_ready = True
                self._floor_ctx = ctx
                accepted = True
        self.stats["level_display_gain"] = dict(self._level_gain)
        self.stats["gain_calibrated"] = bool(self._level_gain)
        self.floor_ready_changed.emit(accepted)

        if self._floor_pending:
            self._floor_pending = False
            self._start_floor_job(self._floor_gen)
        else:
            self.floor_preparing_changed.emit(False)

        self._update_layer_visibility()

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
        self._update_layer_visibility()

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

        self._update_layer_visibility()

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
        gray = self._quantize_corrected_uint8(arr, tile.level)
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
        self._update_layer_visibility()

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

    def _quantize_corrected_uint8(self, arr: np.ndarray, level: int) -> np.ndarray:
        """Like `_quantize_tile_uint8`, but for CORRECTED pixels only
        (precise tiles and the floor): multiplies by the calibrated
        per-level display gain (module docstring "Per-level display gain
        for CORRECTED pixels") BEFORE normalizing/clipping against the same
        fixed display range. `_display_gain_for_level` returns 1.0 for an
        uncalibrated or stale table, so this is a no-op difference from
        `_quantize_tile_uint8` in that case."""
        gain = self._display_gain_for_level(level)
        gained = arr.astype(np.float32, copy=False) * gain
        return self._quantize_tile_uint8(gained)

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
        try:
            self._floor_delivered.disconnect(self._handle_floor_result)
        except (TypeError, RuntimeError):
            pass
        try:
            self.floor_preparing_changed.disconnect(self._on_floor_preparing_changed_for_badge)
        except (TypeError, RuntimeError):
            pass

        # Floor-compute worker threads are plain daemon threads (not owned
        # by Qt); join them here so teardown leaves nothing running behind
        # it. A result arriving after `_torn_down = True` is a no-op
        # (`_handle_floor_result`'s guard above), so this join is a
        # best-effort cleanup, not a correctness requirement.
        for t in self._floor_threads:
            if t.is_alive():
                t.join(timeout=2.0)

        if not shutdown_backend:
            return

        self._teardown_order.append("scheduler.shutdown")
        self.scheduler.shutdown()
        self._teardown_order.append("provider.close")
        self.provider.close()
