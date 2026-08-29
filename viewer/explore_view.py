"""Step0 Explore: three-layer single-channel pyqtgraph view + controller.

See docs/v15_step0_explore_integration.md (authoritative design). This
module implements ONLY: ExploreView (the three-ImageItem widget) and
ExploreController (viewport tracking, tile requesting, settle debounce,
provisional-state management, teardown). Explicitly OUT of scope here:
Step0Page mounting, Compare 2x2, idle prefetch, OpenGL.

## World coordinate system (design doc §1.1)

All three ImageItems live in ONE full-resolution (level-0) pixel coordinate
system. A tile at pyramid level L with top-left (y0, x0) in level-L pixels
is drawn via `setRect(QRectF(x0*ds, y0*ds, w*ds, h*ds))` where
`ds = provider.level_downsample(L)`. Level switches therefore never shift
already-drawn content, and jump_to() addresses level-0 coordinates directly.

## Precise-layer identity (design doc §1.2)

Every blitted precise tile records the full CorrectionKey it was computed
under. A tile only counts as a valid, current precise result while its key
matches the CONTROLLER's current selection_key_context(); on any selection
change the view enters a "provisional" state (opacity 0.5) until every
visible tile has been re-blitted under the new key.
"""

import dataclasses
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


# ── ExploreView: the widget ─────────────────────────────────────────────────

class ExploreView(QtWidgets.QWidget):
    """pyqtgraph GraphicsLayoutWidget hosting one ViewBox with three
    stacked ImageItems, all positioned in the full-resolution world
    coordinate system (design doc §1.1).

    Z-order (bottom to top): overview (0) < raw (1) < precise (2).
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
        self.view_box.disableAutoRange()

        self.overview_item = pg.ImageItem()
        self.raw_item = pg.ImageItem()
        self.precise_item = pg.ImageItem()

        # Explicit z-order: overview under raw under precise.
        self.overview_item.setZValue(0)
        self.raw_item.setZValue(1)
        self.precise_item.setZValue(2)

        self.view_box.addItem(self.overview_item)
        self.view_box.addItem(self.raw_item)
        self.view_box.addItem(self.precise_item)

    @staticmethod
    def world_rect(y0: int, x0: int, h: int, w: int, ds: float) -> QRectF:
        """World-space rect for a level-L tile/region (design doc §1.1)."""
        return QRectF(x0 * ds, y0 * ds, w * ds, h * ds)


# ── ExploreController ────────────────────────────────────────────────────────

class ExploreController(QtCore.QObject):
    """Drives an ExploreView from a provider/scheduler/compute stack.

    Selection state (channel/method/params) plus the viewport define the
    current "settled" request set. Raw tiles fill in immediately on
    viewport change; precise (corrected) tiles are requested only after an
    `settle_ms` quiet period (or immediately via `jump_to`).
    """

    provisional_changed = QtCore.pyqtSignal(bool)

    # Internal cross-thread delivery signals (scheduler callbacks fire on
    # worker threads; Qt widgets must only be touched on the GUI thread).
    _raw_delivered = QtCore.pyqtSignal(object)
    _precise_delivered = QtCore.pyqtSignal(object)

    # Valid `blit_mode` values (docs/benchmarks/2026-08-30_tonsil_g1_probe_v2_
    # offscreen.md): "float_full" is the ORIGINAL full-canvas float32 RGBA
    # compose + setImage (kept verbatim as the measurement baseline).
    # "uint8_full" (stage A) composes into a uint8 RGBA canvas every dirty
    # tick (4x smaller upload, pyqtgraph uint8 fast path). "uint8_incremental"
    # (stage B, the DEFAULT) maintains ONE persistent uint8 RGBA canvas per
    # layer, updated incrementally (only the arriving tile's dirty rect) as
    # tiles land; the 16ms tick then only submits the existing canvas.
    BLIT_MODES = ("float_full", "uint8_full", "uint8_incremental")

    def __init__(self, provider, scheduler, compute, grid: TileGridSpec,
                 view: ExploreView, channel: str, settle_ms: int = 80,
                 probe: bool = False, blit_mode: str = "uint8_incremental"):
        super().__init__()
        if blit_mode not in self.BLIT_MODES:
            raise ValueError(f"unknown blit_mode {blit_mode!r}; expected one of {self.BLIT_MODES}")
        self.provider = provider
        self.scheduler = scheduler
        self.compute = compute
        self.grid = grid
        self.view = view
        self.settle_ms = settle_ms
        self.probe = probe
        self.blit_mode = blit_mode

        # ── selection state ──
        self.channel = channel
        self.method: Optional[str] = None
        self.params: Tuple[int, ...] = ()
        self.quality = QualityLevel.INTERACTIVE

        # ── generations ──
        self.view_generation = 0
        self._settled_generation = 0

        # ── display level / viewport bookkeeping ──
        self.level = 0
        self._current_bbox = None  # (y0, x0, y1, x1) in level-0 coords

        # ── canvases: level-local (data, mask) numpy array pairs covering
        # the current tile set. `data` holds raw float32 pixel values;
        # `mask` (bool) is True only where a real tile has been blitted --
        # unfilled regions must never occlude the overview layer (finding 1).
        self._raw_data: Optional[np.ndarray] = None
        self._raw_mask: Optional[np.ndarray] = None
        self._raw_canvas_origin: Optional[Tuple[int, int]] = None  # (tx0, ty0) tile coords
        self._precise_data: Optional[np.ndarray] = None
        self._precise_mask: Optional[np.ndarray] = None
        self._precise_canvas_origin: Optional[Tuple[int, int]] = None
        self._precise_tile_keys: Dict[Tuple[int, int], CorrectionKey] = {}

        # ── persistent uint8 RGBA canvases (stage A/B only; None in
        # "float_full" mode). Same shape/origin as the corresponding float
        # (data, mask) canvas above; reallocated only alongside it (level
        # switch / viewport-cover resize) -- see stats["rgba_canvas_allocs"].
        self._raw_rgba: Optional[np.ndarray] = None
        self._precise_rgba: Optional[np.ndarray] = None

        self._visible_tiles = set()

        # ── provisional state ──
        self._provisional = False

        # ── dirty flags for the 16ms coalescing blit timer ──
        self._raw_dirty = False
        self._precise_dirty = False

        # ── stable display levels (finding 6): fixed at load_overview,
        # reapplied identically to overview/raw/precise. New tile arrivals
        # must never rescale brightness.
        self._display_lo = 0.0
        self._display_hi = 1.0
        self._overview_arr: Optional[np.ndarray] = None

        # ── teardown bookkeeping ──
        self._teardown_order = []
        self._torn_down = False

        # ── stats (exposed for tests / probe) ──
        self.stats = {
            "frames_prepared": 0,
            "raw_tiles_blitted": 0,
            "precise_tiles_blitted": 0,
            "stale_precise_dropped": 0,
            "mismatched_key_dropped": 0,
            "mismatched_raw_dropped": 0,
            "rgba_canvas_allocs": 0,
        }
        # probe-only timing samples (populated only when probe=True).
        self.timings = {
            "range_handler_ms": [],
            "blit_tick_ms": [],
            "viewport_first_raw_tile_ms": [],
            "viewport_full_raw_tile_ms": [],
            "viewport_first_precise_tile_ms": [],
            "viewport_full_precise_ms": [],
            # stage A/B additions (blit-path optimization).
            "tile_convert_ms": [],
            "set_image_ms": [],
            "tiles_per_tick": [],
            "canvas_realloc_ms": [],
            # (timestamp, cost_ms) pairs for both range_handler and blit_tick
            # samples -- consumed by the probe script's window-aggregation
            # post-processing (bucketed 16.7ms windows, NOT exact vsync
            # frames; see g1_render_probe.py).
            "frame_events": [],
        }
        # count of tile arrivals coalesced since the last blit tick.
        self._tiles_since_tick = 0
        self._raw_probe_batch = None
        self._precise_probe_batch = None
        # guard against a jump's manual settle firing twice (finding 4).
        self._jumping = False

        # ── timers ──
        self._settle_timer = QtCore.QTimer(self)
        self._settle_timer.setSingleShot(True)
        self._settle_timer.setInterval(self.settle_ms)
        self._settle_timer.timeout.connect(self._on_settle)

        self._blit_timer = QtCore.QTimer(self)
        self._blit_timer.setInterval(16)
        self._blit_timer.timeout.connect(self._on_blit_tick)
        self._blit_timer.start()

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
        viewport immediately (no wait).

        Uses the module-level `_UNSET` sentinel as the default so that an
        omitted argument leaves the corresponding state untouched, while an
        EXPLICIT `method=None` still takes effect (meaning: no precise
        layer) -- see finding 5."""
        if channel is not _UNSET:
            self.channel = channel
        if method is not _UNSET:
            self.method = method
        if params is not _UNSET:
            self.params = params

        self._enter_provisional()
        if self._current_bbox is not None:
            self._issue_settled_request()

    def _enter_provisional(self):
        self._provisional = True
        self.view.precise_item.setOpacity(0.5)
        self.provisional_changed.emit(True)
        # Purge registry entries whose key no longer matches the new selection.
        current_ctx = self.selection_key_context()
        stale_coords = [
            coord for coord, key in self._precise_tile_keys.items()
            if self._key_matches_context(key, current_ctx) is False
        ]
        for coord in stale_coords:
            del self._precise_tile_keys[coord]

    def _maybe_exit_provisional(self):
        """Restore full opacity / clear provisional once every visible
        tile has a matching, current precise key."""
        if not self._provisional:
            return
        if not self._visible_tiles:
            return
        if not self._wants_precise():
            return
        if all(coord in self._precise_tile_keys for coord in self._visible_tiles):
            self._provisional = False
            self.view.precise_item.setOpacity(1.0)
            self.provisional_changed.emit(False)

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

        ds = self.provider.level_downsample(chosen)
        rect = ExploreView.world_rect(0, 0, h, w, ds)

        lo, hi = self._compute_display_levels(arr)
        self._display_lo, self._display_hi = lo, hi
        self._overview_arr = arr

        # Overview is always fully valid (whole level read synchronously) --
        # plain grayscale, never RGBA/masked (finding 1); fixed levels only,
        # never autoLevels (finding 6).
        self.view.overview_item.setImage(arr, autoLevels=False, levels=(lo, hi))
        self.view.overview_item.setRect(rect)
        self._overview_level = chosen
        self._overview_shape = (h, w)
        self._overview_ds = ds

    @staticmethod
    def _compute_display_levels(arr: np.ndarray) -> Tuple[float, float]:
        """(0, 99.5th percentile), guarding the degenerate all-zero/constant
        case so span-based normalization never divides by ~0 (finding 6)."""
        lo = 0.0
        hi = float(np.percentile(arr, 99.5)) if arr.size else 0.0
        if not np.isfinite(hi) or hi <= lo:
            hi = lo + 1.0
        return lo, hi

    def set_display_levels(self, lo: float, hi: float):
        """Public: reapply fixed display levels to overview/raw/precise.
        Never triggered automatically by new tile arrivals (finding 6)."""
        self._display_lo = float(lo)
        self._display_hi = float(hi)
        if self._overview_arr is not None:
            self.view.overview_item.setImage(
                self._overview_arr, autoLevels=False,
                levels=(self._display_lo, self._display_hi))
        # uint8 modes: re-quantize existing pixels IN PLACE (same ndarray
        # object -- no reallocation) so the persistent canvas identity
        # contract holds even across a levels change.
        if self.blit_mode != "float_full":
            if self._raw_rgba is not None and self._raw_data is not None:
                self._raw_rgba[...] = self._quantize_tile_rgba(self._raw_data, self._raw_mask)
            if self._precise_rgba is not None and self._precise_data is not None:
                self._precise_rgba[...] = self._quantize_tile_rgba(self._precise_data, self._precise_mask)

        self._raw_dirty = True
        self._precise_dirty = True

    # ── level selection ───────────────────────────────────────────────────

    def _pick_display_level(self, screen_px_per_world_px: float) -> int:
        """Nearest-below choice: pick the FINEST level whose downsample is
        <= 1/screen_px_per_world_px is not quite the framing we want here —
        concretely: for a given zoom (screen pixels per WORLD pixel), the
        ideal pyramid level has downsample ~= 1 / screen_px_per_world_px
        (i.e. one source pixel maps to ~1 screen pixel at level 0 scale).
        We pick the level whose downsample is the largest one that does not
        exceed that ideal ratio (nearest-below), falling back to the finest
        level if none qualifies, and to the coarsest if the ideal ratio is
        smaller than every available downsample."""
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
        t0 = time.perf_counter() if self.probe else None
        self.view_generation += 1

        vb = self.view.view_box
        (x0, x1), (y0, y1) = vb.viewRange()

        # Screen pixels per world pixel, from the ViewBox's screen geometry.
        screen_w = max(1, vb.width())
        world_w = max(1e-9, x1 - x0)
        screen_px_per_world_px = screen_w / world_w

        new_level = self._pick_display_level(screen_px_per_world_px)

        ds0 = self.provider.level_downsample(0)
        # Clamp the world (level-0) bbox to the level-0 extent.
        h0, w0 = self.provider.level_shape(0)
        wy0, wy1 = max(0, min(y0, h0)), max(0, min(y1, h0))
        wx0, wx1 = max(0, min(x0, w0)), max(0, min(x1, w0))
        bbox_l0 = (int(wy0), int(wx0), int(wy1), int(wx1))

        if new_level != self.level:
            self.level = new_level
            self._clear_layer_canvases()

        ds = self.provider.level_downsample(self.level)
        bbox_level = (
            int(bbox_l0[0] / ds), int(bbox_l0[1] / ds),
            int(bbox_l0[2] / ds), int(bbox_l0[3] / ds),
        )
        self._current_bbox = bbox_l0
        self._request_raw_for_bbox(bbox_level)

        self._settle_timer.start(self.settle_ms)

        self.stats["frames_prepared"] += 1
        if self.probe and t0 is not None:
            dt_ms = (time.perf_counter() - t0) * 1000.0
            self.timings["range_handler_ms"].append(dt_ms)
            self.timings["frame_events"].append((time.perf_counter(), dt_ms))

    def jump_to(self, y0: int, x0: int, w: int, h: int):
        """Navigator / checkpoint jump: level-0 coordinates. Actually moves
        the camera (ViewBox.setRange with the world rect, padding=0) so the
        real range-changed handling path runs (view generation bump, level
        pick, raw requests) -- see finding 4. The settled batch is then
        fired immediately (bypassing the settle timer) with a guard against
        firing it twice."""
        rect = QRectF(float(x0), float(y0), float(w), float(h))
        self._jumping = True
        try:
            self.view.view_box.setRange(rect=rect, padding=0)
        finally:
            self._jumping = False
        # `setRange` above may have started the settle timer via
        # _on_range_changed (sigRangeChanged fires synchronously on a direct
        # connection); stop it before firing the settled batch manually so
        # it can never ALSO fire a second, redundant settled batch later.
        self._settle_timer.stop()
        self._on_settle()

    def _request_raw_for_bbox(self, bbox_level):
        tile_size = self.grid.tile_size
        visible = tiles_covering(bbox_level, tile_size)
        self._visible_tiles = visible
        self._ensure_canvas_covers(visible)

        if self.probe:
            self._raw_probe_batch = {
                "start": time.perf_counter(), "visible": set(visible),
                "first": False, "full": False,
            }

        cy = (bbox_level[0] + bbox_level[2]) / 2.0
        cx = (bbox_level[1] + bbox_level[3]) / 2.0

        def dist(coord):
            tx, ty = coord
            tcy = ty * tile_size + tile_size / 2.0
            tcx = tx * tile_size + tile_size / 2.0
            return (tcy - cy) ** 2 + (tcx - cx) ** 2

        missing = sorted(
            (coord for coord in visible if self._raw_is_missing(coord)),
            key=dist,
        )
        gen = self.view_generation
        for i, (tx, ty) in enumerate(missing):
            key = self._make_raw_key(tx, ty)
            req = TileRequest(key=key, generation=gen, priority=i)
            self.scheduler.request(req, self._on_raw_result)

        self._maybe_exit_provisional()

    def _raw_is_missing(self, coord) -> bool:
        return coord not in self._raw_blitted_coords()

    def _raw_blitted_coords(self):
        # We track blitted tiles implicitly via the canvas re-blit registry;
        # simplest correct approach: query the scheduler's cache directly.
        tx, ty = None, None
        return getattr(self, "_raw_blitted_set", set())

    # ── canvases ──────────────────────────────────────────────────────────

    def _tile_bounds(self, visible):
        if not visible:
            return 0, 0, 0, 0
        txs = [tx for tx, _ty in visible]
        tys = [ty for _tx, ty in visible]
        return min(txs), min(tys), max(txs), max(tys)

    def _canvas_tile_bounds(self, origin, shape):
        """(tx0, ty0, tx1, ty1) tile-index bounds a canvas of `shape`
        (rows, cols, ...) anchored at tile-coordinate `origin` covers, or
        None if there is no canvas yet."""
        if origin is None or shape is None:
            return None
        tx0, ty0 = origin
        ts = self.grid.tile_size
        n_rows = shape[0] // ts
        n_cols = shape[1] // ts
        return tx0, ty0, tx0 + n_cols - 1, ty0 + n_rows - 1

    def _level_tile_extent(self):
        """(max_tx, max_ty) tile indices at the CURRENT level -- used to
        clamp the margin so an expanded cover never gets a negative origin
        or reaches past the level's actual extent."""
        h, w = self.provider.level_shape(self.level)
        ts = self.grid.tile_size
        max_tx = max(0, (w - 1) // ts) if w > 0 else 0
        max_ty = max(0, (h - 1) // ts) if h > 0 else 0
        return max_tx, max_ty

    def _expand_with_margin(self, bounds, margin=1):
        tx0, ty0, tx1, ty1 = bounds
        max_tx, max_ty = self._level_tile_extent()
        return (max(0, tx0 - margin), max(0, ty0 - margin),
                min(max_tx, tx1 + margin), min(max_ty, ty1 + margin))

    @staticmethod
    def _bounds_contains(outer, inner):
        otx0, oty0, otx1, oty1 = outer
        itx0, ity0, itx1, ity1 = inner
        return otx0 <= itx0 and oty0 <= ity0 and otx1 >= itx1 and oty1 >= ity1

    def _reallocate_layer(self, data_attr, mask_attr, origin_attr, rgba_attr,
                           dirty_attr, new_bounds):
        """(Re)allocate the (data, mask[, rgba]) canvas trio for one layer
        to cover `new_bounds` (a margin-expanded tile-index rect), carrying
        over already-computed pixels via `_blit_overlap` -- including the
        uint8 RGBA canvas, copied byte-for-byte (NOT requantized; only
        `set_display_levels` requantizes). Counts one
        stats["rgba_canvas_allocs"] event (only for uint8 modes)."""
        ntx0, nty0, ntx1, nty1 = new_bounds
        ts = self.grid.tile_size
        n_cols = ntx1 - ntx0 + 1
        n_rows = nty1 - nty0 + 1
        h, w = n_rows * ts, n_cols * ts

        old_data = getattr(self, data_attr)
        old_mask = getattr(self, mask_attr)
        old_origin = getattr(self, origin_attr)
        old_rgba = getattr(self, rgba_attr)

        new_data = np.zeros((h, w), dtype=np.float32)
        new_mask = np.zeros((h, w), dtype=bool)
        if old_data is not None and old_origin is not None:
            self._blit_overlap(old_data, old_origin, new_data, (ntx0, nty0), ts)
            self._blit_overlap(old_mask, old_origin, new_mask, (ntx0, nty0), ts)
        setattr(self, data_attr, new_data)
        setattr(self, mask_attr, new_mask)
        setattr(self, origin_attr, (ntx0, nty0))
        setattr(self, dirty_attr, True)

        if self.blit_mode != "float_full":
            new_rgba = np.zeros((h, w, 4), dtype=np.uint8)
            if old_rgba is not None and old_origin is not None:
                self._blit_overlap(old_rgba, old_origin, new_rgba, (ntx0, nty0), ts)
            setattr(self, rgba_attr, new_rgba)
            self.stats["rgba_canvas_allocs"] += 1

    def _ensure_canvas_covers(self, visible):
        """Ensure the raw/precise canvases cover `visible` tiles. Margin +
        hysteresis (avoids the realloc-every-pan cost): each canvas is
        allocated to cover the visible tile set EXPANDED by a 1-tile
        border (clamped to the level extent). A reallocation only happens
        when the viewport's visible tiles are no longer fully inside the
        CURRENT cover -- a quarter-tile pan stays within the margin and
        does not reallocate; a level switch already cleared both canvases
        (via _clear_layer_canvases) so this always reallocates then."""
        if not visible:
            return
        visible_bounds = self._tile_bounds(visible)

        t0 = time.perf_counter() if self.probe else None
        did_realloc = False

        raw_cover = self._canvas_tile_bounds(
            self._raw_canvas_origin, self._raw_data.shape if self._raw_data is not None else None)
        if raw_cover is None or not self._bounds_contains(raw_cover, visible_bounds):
            new_bounds = self._expand_with_margin(visible_bounds)
            self._reallocate_layer("_raw_data", "_raw_mask", "_raw_canvas_origin",
                                    "_raw_rgba", "_raw_dirty", new_bounds)
            did_realloc = True

        precise_cover = self._canvas_tile_bounds(
            self._precise_canvas_origin,
            self._precise_data.shape if self._precise_data is not None else None)
        if precise_cover is None or not self._bounds_contains(precise_cover, visible_bounds):
            new_bounds = self._expand_with_margin(visible_bounds)
            self._reallocate_layer("_precise_data", "_precise_mask", "_precise_canvas_origin",
                                    "_precise_rgba", "_precise_dirty", new_bounds)
            did_realloc = True

        if self.probe and t0 is not None and did_realloc:
            self.timings["canvas_realloc_ms"].append((time.perf_counter() - t0) * 1000.0)

        if not hasattr(self, "_raw_blitted_set"):
            self._raw_blitted_set = set()
        self._raw_blitted_set &= visible

    @staticmethod
    def _blit_overlap(old_canvas, old_origin, new_canvas, new_origin, ts):
        """Copy the overlapping region from an old (origin, canvas) pair
        into a freshly (re)sized canvas, so a canvas resize doesn't lose
        already-computed pixels for tiles still in view. Works for 2D
        (float data / bool mask) AND 3D (uint8 RGBA) canvases -- shape[:2]
        is always (rows, cols); a raw byte copy of the RGBA canvas here
        means surviving pixels are carried over WITHOUT requantizing."""
        otx0, oty0 = old_origin
        ntx0, nty0 = new_origin
        dy = (oty0 - nty0) * ts
        dx = (otx0 - ntx0) * ts
        oh, ow = old_canvas.shape[:2]
        nh, nw = new_canvas.shape[:2]

        src_y0, src_x0 = max(0, -dy), max(0, -dx)
        dst_y0, dst_x0 = max(0, dy), max(0, dx)
        copy_h = min(oh - src_y0, nh - dst_y0)
        copy_w = min(ow - src_x0, nw - dst_x0)
        if copy_h > 0 and copy_w > 0:
            new_canvas[dst_y0:dst_y0 + copy_h, dst_x0:dst_x0 + copy_w] = \
                old_canvas[src_y0:src_y0 + copy_h, src_x0:src_x0 + copy_w]

    def _clear_layer_canvases(self):
        """Level switch: clear layers 1-2 (overview covers the gap). Both
        data and mask reset so stale pixels never leak through as opaque."""
        self._raw_data = None
        self._raw_mask = None
        self._raw_canvas_origin = None
        self._raw_rgba = None
        self._precise_data = None
        self._precise_mask = None
        self._precise_canvas_origin = None
        self._precise_rgba = None
        self._precise_tile_keys = {}
        self._raw_blitted_set = set()
        self._raw_dirty = True
        self._precise_dirty = True

    # ── settle / precise request ─────────────────────────────────────────

    def _on_settle(self):
        self._issue_settled_request()

    def _issue_settled_request(self):
        """Cancel the previous settled generation, start a new one, and
        issue CorrectionKeys for the current selection (skipping precise
        entirely when method is None, per §3 of the design doc)."""
        self.scheduler.cancel_generation(self._settled_generation)
        self._settled_generation += 1
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
        on a cache hit, possibly on the calling thread). Marshal to the GUI
        thread via a queued signal."""
        self._raw_delivered.emit(result)

    def _handle_raw_result(self, result):
        """Finding 2: a late raw result is only accepted if its identity
        (channel, source, display level) still matches the CURRENT state,
        and it is only registered in `_raw_blitted_set` once the blit
        actually wrote pixels (`_blit_into` returned True)."""
        if result.error is not None or result.pixels is None:
            return
        key = result.request.key
        if not isinstance(key, RawKey):
            return
        tile = key.tile
        current_source = self.provider.source_identity()
        if key.channel != self.channel or key.source != current_source or \
                tile.level != self.level:
            self.stats["mismatched_raw_dropped"] += 1
            return
        arr = result.pixels.handle
        wrote = self._blit_into(self._raw_data, self._raw_mask, self._raw_canvas_origin,
                                 tile.tx, tile.ty, arr, self.grid.tile_size)
        if not wrote:
            return
        if not hasattr(self, "_raw_blitted_set"):
            self._raw_blitted_set = set()
        self._raw_blitted_set.add((tile.tx, tile.ty))
        self.stats["raw_tiles_blitted"] += 1
        self._raw_dirty = True
        self._tiles_since_tick += 1
        self._maybe_incremental_quantize(
            self._raw_data, self._raw_mask, self._raw_rgba,
            self._raw_canvas_origin, tile.tx, tile.ty, arr)
        if self.probe:
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
        # Flicker-free: blit without clearing existing pixels first. Only
        # register the tile's key once the blit actually wrote pixels
        # (finding 2) -- a late tile fully outside the current canvas must
        # not be registered.
        arr = result.pixels.handle
        wrote = self._blit_into(self._precise_data, self._precise_mask,
                                 self._precise_canvas_origin, tile.tx, tile.ty,
                                 arr, self.grid.tile_size)
        if not wrote:
            return
        self._precise_tile_keys[(tile.tx, tile.ty)] = key
        self.stats["precise_tiles_blitted"] += 1
        self._precise_dirty = True
        self._tiles_since_tick += 1
        self._maybe_incremental_quantize(
            self._precise_data, self._precise_mask, self._precise_rgba,
            self._precise_canvas_origin, tile.tx, tile.ty, arr)
        self._maybe_exit_provisional()
        if self.probe:
            self._probe_note_precise_progress(tile.tx, tile.ty)

    @staticmethod
    def _tile_write_rect(origin, tx, ty, tile_h, tile_w, canvas_h, canvas_w, tile_size):
        """The (dy0, dy1, dx0, dx1) destination-canvas rect a (tx, ty) tile
        of shape (tile_h, tile_w) writes to, clamped to the canvas bounds --
        or None if the tile lies fully outside the canvas (e.g. a late
        arrival after a pan moved the cover origin)."""
        if origin is None:
            return None
        tx0, ty0 = origin
        y0 = (ty - ty0) * tile_size
        x0 = (tx - tx0) * tile_size
        dy0 = max(0, y0)
        dx0 = max(0, x0)
        dy1 = min(y0 + tile_h, canvas_h)
        dx1 = min(x0 + tile_w, canvas_w)
        if dy1 <= dy0 or dx1 <= dx0:
            return None
        return dy0, dy1, dx0, dx1

    @staticmethod
    def _blit_into(data_canvas, mask_canvas, origin, tx, ty, arr, tile_size):
        """Blit `arr` into `data_canvas` (and mark `mask_canvas` True) at
        tile coordinate (tx, ty) relative to `origin`. Returns True only if
        at least one pixel was actually written (finding 2) -- a tile fully
        outside the current canvas writes nothing and returns False."""
        if data_canvas is None or origin is None:
            return False
        h, w = arr.shape
        ch, cw = data_canvas.shape
        rect = ExploreController._tile_write_rect(origin, tx, ty, h, w, ch, cw, tile_size)
        if rect is None:
            return False
        dy0, dy1, dx0, dx1 = rect
        tx0, ty0 = origin
        y0 = (ty - ty0) * tile_size
        x0 = (tx - tx0) * tile_size
        sy0 = dy0 - y0
        sx0 = dx0 - x0
        data_canvas[dy0:dy1, dx0:dx1] = arr[
            sy0:sy0 + (dy1 - dy0), sx0:sx0 + (dx1 - dx0)
        ].astype(np.float32, copy=False)
        if mask_canvas is not None:
            mask_canvas[dy0:dy1, dx0:dx1] = True
        return True

    def _quantize_tile_rgba(self, data: np.ndarray, mask: np.ndarray) -> np.ndarray:
        """uint8 RGBA quantization under the FIXED display levels (finding
        6): rgb = round(clip((v-lo)/(hi-lo), 0, 1) * 255); alpha = 255 where
        `mask` is True, else 0. Used for both a per-tile dirty-rect write
        (stage B) and a full-canvas compose (stage A / rebuild on level
        switch, resize, or a display-levels change)."""
        span = max(self._display_hi - self._display_lo, 1e-6)
        norm = np.clip((data - self._display_lo) / span, 0.0, 1.0)
        gray = np.round(norm * 255.0).astype(np.uint8)
        out = np.empty(data.shape + (4,), dtype=np.uint8)
        out[..., 0] = gray
        out[..., 1] = gray
        out[..., 2] = gray
        out[..., 3] = np.where(mask, np.uint8(255), np.uint8(0))
        return out

    def _maybe_incremental_quantize(self, data_canvas, mask_canvas, rgba_canvas,
                                     origin, tx, ty, arr):
        """Stage B ("uint8_incremental") only: on a tile arrival that was
        already blitted into `data_canvas`/`mask_canvas`, immediately
        quantize just that tile's dirty rect into the persistent
        `rgba_canvas` (same ndarray object -- no reallocation). The 16ms
        tick then only has to submit the existing canvas."""
        if self.blit_mode != "uint8_incremental" or rgba_canvas is None:
            return
        h, w = arr.shape
        ch, cw = data_canvas.shape
        rect = self._tile_write_rect(origin, tx, ty, h, w, ch, cw, self.grid.tile_size)
        if rect is None:
            return
        dy0, dy1, dx0, dx1 = rect
        t0 = time.perf_counter() if self.probe else None
        rgba_canvas[dy0:dy1, dx0:dx1] = self._quantize_tile_rgba(
            data_canvas[dy0:dy1, dx0:dx1], mask_canvas[dy0:dy1, dx0:dx1])
        if self.probe and t0 is not None:
            self.timings["tile_convert_ms"].append((time.perf_counter() - t0) * 1000.0)

    # ── probe-only viewport-first/full progress tracking (finding 3) ─────

    def _probe_note_raw_progress(self, tx, ty):
        batch = self._raw_probe_batch
        if batch is None or (tx, ty) not in batch["visible"]:
            return
        now = time.perf_counter()
        if not batch["first"]:
            batch["first"] = True
            self.timings["viewport_first_raw_tile_ms"].append(
                (now - batch["start"]) * 1000.0)
        if not batch["full"] and batch["visible"] <= self._raw_blitted_set:
            batch["full"] = True
            self.timings["viewport_full_raw_tile_ms"].append(
                (now - batch["start"]) * 1000.0)

    def _probe_note_precise_progress(self, tx, ty):
        batch = self._precise_probe_batch
        if batch is None or (tx, ty) not in batch["visible"]:
            return
        now = time.perf_counter()
        if not batch["first"]:
            batch["first"] = True
            self.timings["viewport_first_precise_tile_ms"].append(
                (now - batch["start"]) * 1000.0)
        if not batch["full"] and batch["visible"] <= set(self._precise_tile_keys.keys()):
            batch["full"] = True
            self.timings["viewport_full_precise_ms"].append(
                (now - batch["start"]) * 1000.0)

    # ── coalesced blit tick (>= once per 16ms, only when dirty) ──────────

    def _compose_rgba(self, data: np.ndarray, mask: np.ndarray) -> np.ndarray:
        """RGB = gray value normalized by the FIXED (self._display_lo,
        self._display_hi) levels (finding 6 -- baked into the RGB values,
        since RGBA image data bypasses pyqtgraph's levels pipeline); alpha
        = 1.0 where a real tile has been blitted, 0.0 elsewhere (finding 1:
        unfilled regions must never occlude the overview underneath)."""
        span = max(self._display_hi - self._display_lo, 1e-6)
        norm = np.clip((data - self._display_lo) / span, 0.0, 1.0).astype(np.float32)
        rgba = np.empty(data.shape + (4,), dtype=np.float32)
        rgba[..., 0] = norm
        rgba[..., 1] = norm
        rgba[..., 2] = norm
        rgba[..., 3] = mask.astype(np.float32)
        return rgba

    def _on_blit_tick(self):
        """Coalesced (>= once/16ms) tick. In "float_full" this composes the
        FULL float32 RGBA canvas every dirty tick (original behavior, kept
        verbatim as the measurement baseline). In "uint8_full" it composes
        a full uint8 RGBA canvas every dirty tick. In "uint8_incremental"
        (default) the persistent uint8 canvases are already up to date
        (updated tile-by-tile on arrival) -- the tick ONLY submits them via
        setImage/setRect. Instrumented end-to-end as `blit_tick_ms` when
        probing (finding 3) -- this is the OTHER half of real per-frame prep
        cost, alongside `range_handler_ms`."""
        t0 = time.perf_counter() if self.probe else None
        did_work = self._raw_dirty or self._precise_dirty
        n_tiles = self._tiles_since_tick
        self._tiles_since_tick = 0

        if self.blit_mode == "float_full":
            if self._raw_dirty and self._raw_data is not None and self._raw_canvas_origin is not None:
                ds = self.provider.level_downsample(self.level)
                tx0, ty0 = self._raw_canvas_origin
                ts = self.grid.tile_size
                h, w = self._raw_data.shape
                rect = ExploreView.world_rect(ty0 * ts, tx0 * ts, h, w, ds)
                rgba = self._compose_rgba(self._raw_data, self._raw_mask)
                self.view.raw_item.setImage(rgba, autoLevels=False, levels=(0.0, 1.0))
                self.view.raw_item.setRect(rect)
                self._raw_dirty = False

            if self._precise_dirty and self._precise_data is not None and \
                    self._precise_canvas_origin is not None:
                ds = self.provider.level_downsample(self.level)
                tx0, ty0 = self._precise_canvas_origin
                ts = self.grid.tile_size
                h, w = self._precise_data.shape
                rect = ExploreView.world_rect(ty0 * ts, tx0 * ts, h, w, ds)
                rgba = self._compose_rgba(self._precise_data, self._precise_mask)
                self.view.precise_item.setImage(rgba, autoLevels=False, levels=(0.0, 1.0))
                self.view.precise_item.setRect(rect)
                self._precise_dirty = False
        else:
            self._blit_tick_uint8("raw")
            self._blit_tick_uint8("precise")

        # Record only ticks that actually composed/uploaded something —
        # idle 16 ms ticks are free and would drown the distribution in
        # meaningless ~0 ms samples.
        if self.probe and t0 is not None and did_work:
            dt_ms = (time.perf_counter() - t0) * 1000.0
            self.timings["blit_tick_ms"].append(dt_ms)
            self.timings["tiles_per_tick"].append(n_tiles)
            self.timings["frame_events"].append((time.perf_counter(), dt_ms))

    def _blit_tick_uint8(self, layer: str):
        """uint8_full / uint8_incremental tick body for one layer ("raw" or
        "precise"): (stage A only) compose the full uint8 canvas, then
        setImage + setRect -- the ONLY work stage B needs here, since its
        canvas was already updated tile-by-tile in the arrival handlers."""
        if layer == "raw":
            dirty = self._raw_dirty
            data, mask, origin = self._raw_data, self._raw_mask, self._raw_canvas_origin
            item = self.view.raw_item
        else:
            dirty = self._precise_dirty
            data, mask, origin = self._precise_data, self._precise_mask, self._precise_canvas_origin
            item = self.view.precise_item

        if not dirty or data is None or origin is None:
            return

        if self.blit_mode == "uint8_full":
            t_conv0 = time.perf_counter() if self.probe else None
            rgba = self._quantize_tile_rgba(data, mask)
            if layer == "raw":
                self._raw_rgba = rgba
            else:
                self._precise_rgba = rgba
            if self.probe and t_conv0 is not None:
                self.timings["tile_convert_ms"].append((time.perf_counter() - t_conv0) * 1000.0)
        else:
            rgba = self._raw_rgba if layer == "raw" else self._precise_rgba
            if rgba is None:
                return

        ds = self.provider.level_downsample(self.level)
        tx0, ty0 = origin
        ts = self.grid.tile_size
        h, w = data.shape
        rect = ExploreView.world_rect(ty0 * ts, tx0 * ts, h, w, ds)

        t_img0 = time.perf_counter() if self.probe else None
        item.setImage(rgba, autoLevels=False)
        item.setRect(rect)
        if self.probe and t_img0 is not None:
            self.timings["set_image_ms"].append((time.perf_counter() - t_img0) * 1000.0)

        if layer == "raw":
            self._raw_dirty = False
        else:
            self._precise_dirty = False

    # ── teardown ──────────────────────────────────────────────────────────

    def teardown(self, shutdown_backend: bool = True):
        """Stop timers, disconnect signals, then (if `shutdown_backend`)
        scheduler.shutdown(), then provider.close(). Order recorded in
        `_teardown_order`. `shutdown_backend=False` lets a caller (e.g. the
        g1_render_probe --compare-blit-modes harness) tear down a per-mode
        controller/view WITHOUT killing the shared scheduler/provider it
        will reuse for the next mode."""
        if self._torn_down:
            return
        self._torn_down = True

        self._settle_timer.stop()
        self._blit_timer.stop()
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
