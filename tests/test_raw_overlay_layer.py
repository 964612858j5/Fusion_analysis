"""The additive nucleus overlay on top of the marker layers.

`RawOverlayLayer` is deliberately narrow: one tile pool, one channel, its
own display range and its own generation namespace, sharing the main
stack's provider, scheduler, raw cache and ViewBox -- and owning no part of
their shutdown. The marker `ExploreController` stays the only planner of
viewport, level and visible tiles.

Three things are easy to get wrong and are therefore pinned hardest here:

  * the GENERATION namespace. `TileScheduler.cancel_generation` matches
    tokens by value in ONE global stale-set, so a bare int -- or the
    marker's own ("raw", n) -- would let either side's cancel drop the
    other side's in-flight work.
  * ADDITIVE composition. It is what makes this an overlay rather than an
    occluder, and it is measured on a real ExploreView here, not assumed.
    It also forces `coarser_visible=False`: two additive levels of the same
    channel would double their own brightness where they overlap.
  * the DELIVERY guard. A queued Qt signal already in the event loop is
    delivered after a `disconnect`, so teardown and the off switch have to
    be tested at the handler, not assumed away upstream.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtGui  # noqa: E402

from block01.viewer.explore_view import (  # noqa: E402
    ExploreView, OVERLAY_RAW_BASE_PRIORITY, RawOverlayLayer, TileItemPool,
)
from block01.viewer.tile_types import (  # noqa: E402
    PixelBuffer, QualityLevel, RawKey, TileAddress, TileGridSpec, TileRequest,
    TileResult,
)

from test_explore_controller import (  # noqa: E402
    FakeProvider, FakeScheduler, _pump, app, make_controller,
    set_view_and_pump,
)

GRID = TileGridSpec(tile_size=512, source_chunk_shape=(), grid_version="v1")


def _overlay(ctrl, provider, scheduler, view, channel="CD3", *,
             enabled=True, calibrate=True):
    layer = RawOverlayLayer(provider, scheduler, GRID, view, channel)
    ctrl.attach_overlay(layer)
    if calibrate:
        # Skip the async read: calibration timing has its own tests below.
        layer._display_lo, layer._display_hi = 0.0, 1000.0
    if enabled:
        layer.set_enabled(True, host=ctrl)
    return layer


def _deliver(scheduler, layer, req, value=500.0, error=None):
    """Deliver one request through the overlay's real queued path."""
    arr = np.full((512, 512), value, np.float32)
    pixels = None if error else PixelBuffer(
        residency="cpu", dtype="float32", shape=arr.shape, handle=arr)
    layer._on_result(TileResult(request=req, pixels=pixels,
                                quality=QualityLevel.NATIVE,
                                provisional=False, timing={}, error=error))
    _pump(30)


def _overlay_requests(scheduler):
    return [r for r, _cb in scheduler.requests
            if isinstance(r.key, RawKey)
            and str(r.generation[0]) == "dapi_raw"]


def _live_overlay_requests(scheduler, layer):
    """Only the CURRENT generation's requests.

    A single `set_view_and_pump` produces several range events and so
    several syncs; picking `[0]` would hand back a superseded request, and a
    test meant to prove the off-switch or the teardown guard would then pass
    on the generation check instead.
    """
    return [r for r in _overlay_requests(scheduler)
            if r.generation == layer.generation]


# ── 1-3. generation namespace and priority ───────────────────────────────

def test_the_generation_namespace_cannot_collide_with_the_markers(app):
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()
    layer = _overlay(ctrl, provider, scheduler, view)
    set_view_and_pump(view, 0, 0, 1024, 1024)

    gens = {r.generation for r, _ in scheduler.requests
            if isinstance(r.key, RawKey)}
    marker_gens = {g for g in gens if g[0] == "raw"}
    overlay_gens = {g for g in gens if g[0] == "dapi_raw"}

    assert marker_gens and overlay_gens
    assert marker_gens.isdisjoint(overlay_gens)
    # The counters are independent, so they DO reach the same integer --
    # which is exactly why the namespace, not the number, has to separate
    # them.
    assert layer.generation[0] == "dapi_raw"
    ctrl.teardown()


def test_marker_requests_are_submitted_before_the_overlays(app):
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()
    _overlay(ctrl, provider, scheduler, view)
    scheduler.requests.clear()
    set_view_and_pump(view, 0, 0, 1024, 1024)

    order = [r.generation[0] for r, _ in scheduler.requests
             if isinstance(r.key, RawKey)]
    assert "raw" in order and "dapi_raw" in order
    assert order.index("raw") < order.index("dapi_raw")
    ctrl.teardown()


def test_overlay_priorities_start_at_the_overlay_base(app):
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()
    _overlay(ctrl, provider, scheduler, view)
    set_view_and_pump(view, 0, 0, 1024, 1024)

    prios = sorted(r.priority for r in _overlay_requests(scheduler))
    assert prios and prios[0] == OVERLAY_RAW_BASE_PRIORITY
    assert prios == list(range(OVERLAY_RAW_BASE_PRIORITY,
                               OVERLAY_RAW_BASE_PRIORITY + len(prios)))
    ctrl.teardown()


# ── 4. the overlay does not plan ─────────────────────────────────────────

def test_the_overlay_requests_exactly_the_hosts_visible_tiles(app):
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()
    layer = _overlay(ctrl, provider, scheduler, view)
    set_view_and_pump(view, 0, 0, 1024, 1024)

    wanted = set(ctrl._visible_tiles)
    got = {(r.key.tile.tx, r.key.tile.ty) for r in _overlay_requests(scheduler)}
    assert got == wanted
    # Same level as the host, decided by the host.
    assert {r.key.tile.level for r in _overlay_requests(scheduler)} == {ctrl.level}
    ctrl.teardown()


def test_the_overlay_has_no_planner_state_of_its_own(app):
    """It must not grow a second viewport state machine: no timers, no
    level picker, no interaction epoch, no floor/precise/gain/directional."""
    ctrl, provider, scheduler, view = make_controller(app)
    layer = _overlay(ctrl, provider, scheduler, view, calibrate=True)

    for forbidden in ("_motion_timer", "_settle_timer", "_interaction_epoch",
                      "_floor_threads", "_precise_pool", "_level_gain",
                      "_pick_display_level_with_hysteresis",
                      "_issue_directional_prefetch", "overview_item"):
        assert not hasattr(layer, forbidden), forbidden
    ctrl.teardown()


# ── 5-6. per-level visibility under additive composition ─────────────────

def test_only_the_current_level_is_visible(app):
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()
    layer = _overlay(ctrl, provider, scheduler, view)
    set_view_and_pump(view, 0, 0, 1024, 1024)
    for req in _live_overlay_requests(scheduler, layer):
        _deliver(scheduler, layer, req)

    coarse = TileAddress(grid=GRID, level=ctrl.level + 1, tx=0, ty=0)
    layer._pool.put(coarse.level, 0, 0, view.view_box.viewRect(),
                    np.zeros((4, 4), np.uint8), key="coarse")
    layer.apply_visibility(ctrl.level)

    for entry in layer._pool.entries.values():
        if entry.level == ctrl.level:
            assert entry.item.isVisible() is True
        else:
            assert entry.item.isVisible() is False, (
                "a coarser overlay tile left visible ADDS to the current "
                "level instead of being covered by it")
    ctrl.teardown()


def test_a_level_change_hides_the_old_level_before_new_tiles_arrive(app):
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()
    layer = _overlay(ctrl, provider, scheduler, view)
    set_view_and_pump(view, 0, 0, 1024, 1024)
    for req in _live_overlay_requests(scheduler, layer):
        _deliver(scheduler, layer, req)
    old_level = ctrl.level
    shown = [e for e in layer._pool.entries.values() if e.item.isVisible()]
    assert shown, "test setup: nothing was visible to begin with"

    scheduler.requests.clear()
    set_view_and_pump(view, 0, 0, 4096, 4096)     # zoom out -> coarser level
    assert ctrl.level != old_level, "test setup: the level did not change"

    # No new tile has been DELIVERED yet -- only requested.
    for entry in layer._pool.entries.values():
        if entry.level == old_level:
            assert entry.item.isVisible() is False
    ctrl.teardown()


# ── 7. additive composition, measured on a real ExploreView ──────────────

def _render_centre(view, w=64, h=64):
    from PyQt5 import QtGui as _G
    img = _G.QImage(w, h, _G.QImage.Format_RGB32)
    img.fill(0)
    painter = _G.QPainter(img)
    view.graphics.scene().render(painter)
    painter.end()
    c = img.pixelColor(w // 2, h // 2)
    return (c.red(), c.green(), c.blue())


def _two_layer_view(app, marker_val, nucleus_val):
    """A real ExploreView with one marker tile and one overlay tile at the
    same place, coloured the way Step0 colours them."""
    import pyqtgraph as pg
    from PyQt5 import QtCore
    from block01.viewer.explore_view import ExploreController

    view = ExploreView()
    view.resize(64, 64)
    vb = view.view_box
    vb.disableAutoRange()

    def _item(val, rgb, z, plus):
        it = pg.ImageItem(axisOrder="row-major")
        it.setZValue(z)
        it.setImage(np.full((8, 8), val, np.uint8), autoLevels=False,
                    levels=(0, 255))
        it.setLookupTable(ExploreController.build_tint_lut(rgb))
        it.setRect(QtCore.QRectF(0, 0, 8, 8))
        vb.addItem(it)
        if plus:
            it.setCompositionMode(QtGui.QPainter.CompositionMode_Plus)
        return it

    marker = _item(marker_val, (0.0, 1.0, 0.0), 100, False)
    nucleus = _item(nucleus_val, (0.0, 0.5, 1.0), 300, True)
    vb.setRange(xRange=(0, 8), yRange=(0, 8), padding=0)
    _pump(20)
    return view, marker, nucleus


def test_the_overlay_adds_to_the_marker_per_channel(app):
    """The compare panels compute `marker_colour*i + nucleus_colour*j`.
    This is that, saturating per channel."""
    view, marker, nucleus = _two_layer_view(app, 200, 160)

    got = _render_centre(view)

    expected = tuple(min(255, a + b) for a, b in
                     zip((0, 200, 0), (0, int(160 * 0.5), 160)))
    assert got == expected == (0, 255, 160)
    view.deleteLater()


def test_each_layer_alone_is_itself(app):
    view, marker, nucleus = _two_layer_view(app, 200, 160)

    nucleus.setOpacity(0.0)
    _pump(10)
    assert _render_centre(view) == (0, 200, 0)          # marker alone

    nucleus.setOpacity(1.0)
    marker.setOpacity(0.0)
    _pump(10)
    assert _render_centre(view) == (0, 80, 160)         # nucleus alone
    view.deleteLater()


def test_a_dark_overlay_pixel_adds_nothing(app):
    """Zero must ADD zero -- not darken and not occlude, which is what an
    ordinary alpha-over overlay would do."""
    view, marker, nucleus = _two_layer_view(app, 200, 0)

    assert _render_centre(view) == (0, 200, 0)
    view.deleteLater()


# ── 8. the pool owns the composition mode ────────────────────────────────

def test_the_pool_applies_the_composition_mode_to_late_items(app):
    from PyQt5.QtCore import QRectF

    class _Box:
        def __init__(self):
            self.items = []

        def addItem(self, item):
            self.items.append(item)

        def removeItem(self, item):
            self.items.remove(item)

    box = _Box()
    pool = TileItemPool(box, base_z=300, num_levels=2, budget=8)
    early = pool.put(0, 0, 0, QRectF(0, 0, 4, 4), np.zeros((4, 4), np.uint8),
                     key="a")

    pool.set_composition_mode(QtGui.QPainter.CompositionMode_Plus)
    late = pool.put(0, 1, 0, QRectF(4, 0, 4, 4), np.zeros((4, 4), np.uint8),
                    key="b")

    assert early.item.paintMode == QtGui.QPainter.CompositionMode_Plus
    assert late.item.paintMode == QtGui.QPainter.CompositionMode_Plus


def test_the_default_composition_mode_is_unchanged_for_existing_callers(app):
    from PyQt5.QtCore import QRectF

    class _Box:
        def addItem(self, item):
            pass

        def removeItem(self, item):
            pass

    pool = TileItemPool(_Box(), base_z=100, num_levels=2, budget=8)
    entry = pool.put(0, 0, 0, QRectF(0, 0, 4, 4), np.zeros((4, 4), np.uint8),
                     key="a")

    assert entry.item.paintMode is None      # pyqtgraph default: SourceOver


# ── 9-11. calibration ────────────────────────────────────────────────────

def test_no_tile_is_requested_before_calibration(app):
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()
    layer = _overlay(ctrl, provider, scheduler, view, calibrate=False,
                     enabled=False)
    layer._enabled = True                    # on, but not yet calibrated
    scheduler.requests.clear()

    set_view_and_pump(view, 0, 0, 1024, 1024)

    assert _overlay_requests(scheduler) == []
    assert layer.calibrated is False
    ctrl.teardown()


def test_calibration_requests_the_current_viewport_immediately(app):
    """No pan needed: the layer resyncs from the host's CURRENT viewport as
    soon as its display range lands."""
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()
    layer = _overlay(ctrl, provider, scheduler, view, calibrate=False,
                     enabled=False)
    set_view_and_pump(view, 0, 0, 1024, 1024)
    layer._enabled = True
    scheduler.requests.clear()

    layer.start_calibration(ctrl)
    _pump(200)

    assert layer.calibrated is True
    assert layer._display_lo is not None and layer._display_hi is not None
    got = {(r.key.tile.tx, r.key.tile.ty) for r in _overlay_requests(scheduler)}
    assert got == set(ctrl._visible_tiles)
    ctrl.teardown()


def test_a_failed_calibration_leaves_the_marker_alone(app):
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()
    marker_lo, marker_hi = ctrl._display_lo, ctrl._display_hi
    layer = _overlay(ctrl, provider, scheduler, view, calibrate=False,
                     enabled=False)
    layer._enabled = True

    layer._calibrating = True
    layer._calibrated.emit(RuntimeError("no such level"))
    _pump(60)

    assert layer.calibration_failed is True
    assert layer.calibrated is False
    assert layer.stats["calibration_failures"] == 1
    # It must NOT borrow the marker's range: that is another channel's
    # histogram presented as this one's.
    assert layer._display_lo is None and layer._display_hi is None
    assert (ctrl._display_lo, ctrl._display_hi) == (marker_lo, marker_hi)
    assert _overlay_requests(scheduler) == []
    ctrl.teardown()


def test_calibration_is_single_flight(app):
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()
    layer = _overlay(ctrl, provider, scheduler, view, calibrate=False,
                     enabled=False)
    submitted = []
    ctrl._overview_pool = type("P", (), {
        "submit": lambda _s, fn: submitted.append(fn),
        "shutdown": lambda _s, wait=True: None})()

    layer.start_calibration(ctrl)
    layer.start_calibration(ctrl)

    assert len(submitted) == 1
    ctrl.teardown()


# ── 12-13. the switch ────────────────────────────────────────────────────

def test_turning_the_overlay_off_cancels_and_stops_requesting(app):
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()
    layer = _overlay(ctrl, provider, scheduler, view)
    set_view_and_pump(view, 0, 0, 1024, 1024)
    gen_before = layer.generation
    scheduler.cancelled_generations.clear()
    scheduler.requests.clear()

    layer.set_enabled(False, host=ctrl)
    set_view_and_pump(view, 0, 0, 2048, 2048)

    assert gen_before in scheduler.cancelled_generations
    assert _overlay_requests(scheduler) == []
    ctrl.teardown()


def test_a_queued_delivery_after_the_switch_is_dropped(app):
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()
    layer = _overlay(ctrl, provider, scheduler, view)
    set_view_and_pump(view, 0, 0, 1024, 1024)
    req = _live_overlay_requests(scheduler, layer)[0]
    before = len(layer._pool.entries)

    layer.set_enabled(False, host=ctrl)
    _deliver(scheduler, layer, req)

    assert len(layer._pool.entries) == before
    ctrl.teardown()


def test_turning_it_back_on_resumes_without_a_pan(app):
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()
    layer = _overlay(ctrl, provider, scheduler, view)
    set_view_and_pump(view, 0, 0, 1024, 1024)
    layer.set_enabled(False, host=ctrl)
    scheduler.requests.clear()

    layer.set_enabled(True, host=ctrl)       # no camera movement at all

    got = {(r.key.tile.tx, r.key.tile.ty) for r in _overlay_requests(scheduler)}
    assert got == set(ctrl._visible_tiles)
    ctrl.teardown()


# ── 14-15. marker channel == nucleus channel ─────────────────────────────

def test_the_overlay_is_suppressed_when_the_marker_shows_that_channel(app):
    ctrl, provider, scheduler, view = make_controller(app, channel="DAPI")
    ctrl.load_overview()
    layer = _overlay(ctrl, provider, scheduler, view, channel="DAPI")
    layer.set_suppressed(True, host=ctrl)
    scheduler.requests.clear()

    set_view_and_pump(view, 0, 0, 1024, 1024)

    assert _overlay_requests(scheduler) == []
    assert layer.effective_enabled is False
    # The USER's switch is untouched, so there is no per-channel map to keep.
    assert layer.enabled is True
    ctrl.teardown()


def test_leaving_that_channel_restores_the_overlay(app):
    ctrl, provider, scheduler, view = make_controller(app, channel="DAPI")
    ctrl.load_overview()
    layer = _overlay(ctrl, provider, scheduler, view, channel="DAPI")
    layer.set_suppressed(True, host=ctrl)
    set_view_and_pump(view, 0, 0, 1024, 1024)
    scheduler.requests.clear()

    layer.set_suppressed(False, host=ctrl)   # marker moved to another channel

    assert layer.enabled is True
    assert layer.effective_enabled is True
    got = {(r.key.tile.tx, r.key.tile.ty) for r in _overlay_requests(scheduler)}
    assert got == set(ctrl._visible_tiles)
    ctrl.teardown()


# ── 16. tint ─────────────────────────────────────────────────────────────

def test_changing_the_tint_only_swaps_the_lookup_table(app):
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()
    layer = _overlay(ctrl, provider, scheduler, view)
    set_view_and_pump(view, 0, 0, 1024, 1024)
    for req in _live_overlay_requests(scheduler, layer):
        _deliver(scheduler, layer, req)
    entry = next(iter(layer._pool.entries.values()))
    pixels_before = np.array(entry.item.image, copy=True)
    scheduler.requests.clear()

    layer.set_tint((1.0, 0.0, 0.0))

    assert np.array_equal(entry.item.image, pixels_before)   # not requantised
    assert scheduler.requests == []                          # nothing asked for
    assert tuple(layer._pool._lut[255]) == (255, 0, 0)
    ctrl.teardown()


# ── 17-19. teardown and shutdown ownership ───────────────────────────────

def test_overlay_teardown_does_not_close_the_shared_backend(app):
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()
    layer = _overlay(ctrl, provider, scheduler, view)
    set_view_and_pump(view, 0, 0, 1024, 1024)

    layer.teardown()

    assert scheduler.shutdown_called is False
    assert provider.close_called is False
    assert layer._pool.entries == {}
    ctrl.teardown()


def test_the_controller_tears_the_overlay_down_first_then_the_backend(app):
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()
    layer = _overlay(ctrl, provider, scheduler, view)

    shutdowns = []
    real_shutdown = scheduler.shutdown
    scheduler.shutdown = lambda: (shutdowns.append(1), real_shutdown())

    ctrl.teardown()

    # Counted, not merely "was called": the overlay borrows this scheduler,
    # so a shutdown from BOTH sides would join the workers twice.
    assert shutdowns == [1]
    order = ctrl._teardown_order
    assert order.index("overlay.teardown") < order.index("scheduler.shutdown")
    assert order.index("scheduler.shutdown") < order.index("provider.close")
    assert order.count("scheduler.shutdown") == 1
    assert order.count("provider.close") == 1
    assert layer._torn_down is True


def test_a_queued_delivery_after_teardown_cannot_resurrect_a_tile(app):
    """`disconnect` is not a defence: a signal already in Qt's queue is
    still delivered afterwards."""
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()
    layer = _overlay(ctrl, provider, scheduler, view)
    set_view_and_pump(view, 0, 0, 1024, 1024)
    req = _live_overlay_requests(scheduler, layer)[0]
    arr = np.full((512, 512), 500.0, np.float32)
    result = TileResult(
        request=req,
        pixels=PixelBuffer(residency="cpu", dtype="float32",
                           shape=arr.shape, handle=arr),
        quality=QualityLevel.NATIVE, provisional=False, timing={}, error=None)

    layer._on_result(result)         # queued, NOT yet delivered
    ctrl.teardown()                  # tears the overlay down first
    _pump(60)                        # now the queued signal lands

    assert layer._pool.entries == {}


def test_teardown_is_idempotent(app):
    ctrl, provider, scheduler, view = make_controller(app)
    layer = _overlay(ctrl, provider, scheduler, view)

    layer.teardown()
    layer.teardown()

    assert scheduler.shutdown_called is False
    ctrl.teardown()


# ── delivery guard, per check ────────────────────────────────────────────

def test_a_result_for_another_channel_is_dropped(app):
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()
    layer = _overlay(ctrl, provider, scheduler, view, channel="CD3")
    set_view_and_pump(view, 0, 0, 1024, 1024)
    addr = TileAddress(grid=GRID, level=ctrl.level, tx=0, ty=0)
    alien = TileRequest(
        key=RawKey(source=provider.source_identity(), channel="CD8",
                   tile=addr),
        generation=layer.generation, priority=OVERLAY_RAW_BASE_PRIORITY)

    before = len(layer._pool.entries)
    _deliver(scheduler, layer, alien)

    assert len(layer._pool.entries) == before
    assert layer.stats["mismatched_dropped"] >= 1
    ctrl.teardown()


def test_a_stale_generation_result_is_dropped(app):
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()
    layer = _overlay(ctrl, provider, scheduler, view)
    set_view_and_pump(view, 0, 0, 1024, 1024)
    req = _live_overlay_requests(scheduler, layer)[0]
    layer._bump_generation()                 # what a resync does
    before = len(layer._pool.entries)

    _deliver(scheduler, layer, req)

    assert len(layer._pool.entries) == before
    assert layer.stats["late_rejected"] >= 1
    ctrl.teardown()


def test_a_delivered_tile_is_quantised_against_the_overlays_own_range(app):
    """Borrowing the marker's range would quantise nucleus pixels against a
    different channel's histogram."""
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()
    # Enabled only AFTER the range is set: enabling an uncalibrated layer
    # starts the real calibration, which would land later and overwrite it.
    layer = _overlay(ctrl, provider, scheduler, view, calibrate=False,
                     enabled=False)
    layer._display_lo, layer._display_hi = 0.0, 1000.0
    ctrl._display_lo, ctrl._display_hi = 0.0, 10.0      # deliberately unlike
    layer.set_enabled(True, host=ctrl)
    set_view_and_pump(view, 0, 0, 1024, 1024)
    req = _live_overlay_requests(scheduler, layer)[0]

    _deliver(scheduler, layer, req, value=500.0)

    entry = layer._pool.get(req.key.tile.level, req.key.tile.tx,
                            req.key.tile.ty)
    assert entry is not None
    # 500 of 0..1000 -> ~128, NOT saturated at 255 (which the marker's
    # 0..10 range would have produced).
    assert 120 <= int(entry.item.image.flat[0]) <= 135
    ctrl.teardown()


# ── the handler's own guards, isolated from the generation check ─────────
#
# Turning the layer off and tearing it down BOTH bump the generation, so
# the generation check alone would hide a missing `_torn_down` or
# `effective_enabled` guard. These two set the flag WITHOUT bumping, which
# is the state a delivery already inside Qt's queue can arrive in: the
# generation still matches, and only the flag stands between it and the
# pool.

def test_the_handler_refuses_a_matching_result_once_torn_down(app):
    """NOTE, honestly: removing the explicit `_torn_down` check from the
    handler does NOT fail this -- `effective_enabled` is defined as
    `_enabled and not _suppressed and not _torn_down`, so it catches the
    same state. The explicit check is kept for readability; this case pins
    the BEHAVIOUR (a torn-down layer accepts nothing), not that particular
    line."""
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()
    layer = _overlay(ctrl, provider, scheduler, view)
    set_view_and_pump(view, 0, 0, 1024, 1024)
    req = _live_overlay_requests(scheduler, layer)[0]
    before = len(layer._pool.entries)

    layer._torn_down = True          # generation deliberately unchanged
    assert req.generation == layer.generation, "test setup: must still match"
    _deliver(scheduler, layer, req)

    assert len(layer._pool.entries) == before
    ctrl.teardown()


def test_the_handler_refuses_a_matching_result_once_disabled(app):
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()
    layer = _overlay(ctrl, provider, scheduler, view)
    set_view_and_pump(view, 0, 0, 1024, 1024)
    req = _live_overlay_requests(scheduler, layer)[0]
    before = len(layer._pool.entries)

    layer._enabled = False           # generation deliberately unchanged
    assert req.generation == layer.generation, "test setup: must still match"
    _deliver(scheduler, layer, req)

    assert len(layer._pool.entries) == before
    ctrl.teardown()


# ── one centre-out ordering, owned by the marker controller ──────────────
#
# The overlay used to sort the visible tiles itself, with a copy of the
# host's distance formula. Two copies is how the two channels end up
# fetching the same viewport in different orders.

def _marker_request_order(scheduler):
    return [(r.key.tile.tx, r.key.tile.ty) for r, _cb in scheduler.requests
            if isinstance(r.key, RawKey) and r.generation[0] == "raw"]


def test_the_marker_request_order_is_the_controllers_ordering(app):
    """The order the marker issues in IS `_visible_tiles_center_out`
    filtered by its own pool -- item for item, ties included."""
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()
    scheduler.requests.clear()
    set_view_and_pump(view, 0, 0, 1024, 1024)

    ordered = list(ctrl._visible_tiles_center_out())
    issued = _marker_request_order(scheduler)
    expected = [c for c in ordered if c in set(issued)]
    assert issued[-len(expected):] == expected
    assert set(ordered) == set(ctrl._visible_tiles)
    ctrl.teardown()


def test_the_ordering_entry_point_sorts_but_never_recomputes_the_set(app):
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()
    set_view_and_pump(view, 0, 0, 1024, 1024)

    ctrl._visible_tiles = {(5, 5), (0, 0), (9, 9)}
    ordered = ctrl._visible_tiles_center_out()

    assert set(ordered) == {(5, 5), (0, 0), (9, 9)}
    assert len(ordered) == 3
    ctrl.teardown()


def test_the_ordering_entry_point_is_empty_without_a_viewport(app):
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl._current_bbox = None

    assert tuple(ctrl._visible_tiles_center_out()) == ()
    ctrl.teardown()


def test_the_overlay_follows_the_controllers_order(app):
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()
    layer = _overlay(ctrl, provider, scheduler, view)
    scheduler.requests.clear()
    set_view_and_pump(view, 0, 0, 1024, 1024)

    ordered = list(ctrl._visible_tiles_center_out())
    got = [(r.key.tile.tx, r.key.tile.ty)
           for r in _live_overlay_requests(scheduler, layer)]
    assert got == [c for c in ordered if c in set(got)]
    # Priorities follow that same sequence.
    prios = [r.priority for r in _live_overlay_requests(scheduler, layer)]
    assert prios == list(range(OVERLAY_RAW_BASE_PRIORITY,
                               OVERLAY_RAW_BASE_PRIORITY + len(prios)))
    ctrl.teardown()


def test_the_overlay_has_no_ordering_or_viewport_state_of_its_own(app):
    """`_order_tiles` and `_last_sync` are both gone: the overlay stores no
    bbox, no viewport and no tile set."""
    ctrl, provider, scheduler, view = make_controller(app)
    layer = _overlay(ctrl, provider, scheduler, view)
    set_view_and_pump(view, 0, 0, 1024, 1024)

    assert not hasattr(layer, "_order_tiles")
    assert not hasattr(layer, "_last_sync")
    ctrl.teardown()


@pytest.mark.parametrize("path", ["enable", "calibration"])
def test_both_resync_paths_go_through_the_controllers_ordering(app, path):
    """Enabling the layer and finishing its calibration both resync from
    the host's CURRENT viewport, and both must ask the host for the order
    rather than deriving one."""
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()
    layer = _overlay(ctrl, provider, scheduler, view, calibrate=False,
                     enabled=False)
    set_view_and_pump(view, 0, 0, 1024, 1024)

    calls = []
    real = ctrl._visible_tiles_center_out
    ctrl._visible_tiles_center_out = lambda: (calls.append(1), real())[1]
    scheduler.requests.clear()

    if path == "enable":
        layer._display_lo, layer._display_hi = 0.0, 1000.0
        layer.set_enabled(True, host=ctrl)
    else:
        layer._enabled = True
        layer.start_calibration(ctrl)
        _pump(200)

    assert calls, "the resync did not go through the controller's ordering"
    got = {(r.key.tile.tx, r.key.tile.ty)
           for r in _live_overlay_requests(scheduler, layer)}
    assert got == set(ctrl._visible_tiles)
    ctrl.teardown()
