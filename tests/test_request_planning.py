"""Unit tests for the pure viewport-planning functions.

Two kinds of case here, and the second is the one that matters:

* direct tests of each function's contract, including the corners the
  controller's arithmetic has always had (truncation, the `<= 0` guards,
  the hysteresis band);
* DIFFERENTIAL tests against `ExploreController`'s own private methods,
  swept over many inputs. Those are what license the migration: they prove
  the pure function answers identically to the code still in the
  controller, before a single call site moves.

Nothing here is wired into the controller yet.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from viewer.request_planning import (  # noqa: E402
    ZOOM_AREA_TOLERANCE,
    apply_level_hysteresis,
    bbox_to_level,
    clamp_viewport_to_level0,
    classify_zoom,
    pick_display_level,
    visible_tiles_for_viewport,
)
from viewer.tile_types import tiles_covering  # noqa: E402


# ── display level ────────────────────────────────────────────────────────────

DS_TWO_LEVEL = [1.0, 4.0]
DS_FOUR_LEVEL = [1.0, 2.0, 4.0, 8.0]


@pytest.mark.parametrize(
    ("downsamples", "spp", "expected"),
    [
        # Zoomed in past 1:1 -- nothing coarser qualifies, so level 0.
        (DS_FOUR_LEVEL, 2.0, 0),
        (DS_FOUR_LEVEL, 1.0, 0),
        # ideal_ds = 2.0 exactly: level 1 qualifies (ds <= ideal).
        (DS_FOUR_LEVEL, 0.5, 1),
        (DS_FOUR_LEVEL, 0.4, 1),
        (DS_FOUR_LEVEL, 0.25, 2),
        (DS_FOUR_LEVEL, 0.125, 3),
        # Zoomed out beyond the coarsest level: stays at the coarsest.
        (DS_FOUR_LEVEL, 0.01, 3),
        (DS_TWO_LEVEL, 0.25, 1),
        # Guard: a non-positive ratio never divides.
        (DS_FOUR_LEVEL, 0.0, 0),
        (DS_FOUR_LEVEL, -3.0, 0),
        # Single-level pyramid.
        ([1.0], 0.001, 0),
    ],
)
def test_pick_display_level(downsamples, spp, expected):
    assert pick_display_level(downsamples, spp) == expected


def test_pick_display_level_keeps_the_last_of_equal_downsamples():
    """`ds >= best_ds` (not `>`) means the LAST qualifying level wins when
    two share a downsample. Preserved from the controller deliberately --
    a duplicated level in a pyramid must not change which one is shown."""
    assert pick_display_level([1.0, 4.0, 4.0], 0.25) == 2


# ── hysteresis ───────────────────────────────────────────────────────────────

def test_hysteresis_keeps_the_current_level_inside_the_band():
    # ideal_ds = 1/0.24 ~= 4.17 against the current level's ds 4.0 ->
    # ratio ~= 1.04, inside a 0.2 band: no switch.
    assert apply_level_hysteresis(ideal_level=2, current_level=1,
                                  current_downsample=4.0,
                                  screen_px_per_world_px=0.24,
                                  threshold=0.2) == 1


def test_hysteresis_switches_once_the_band_is_exceeded():
    # ideal_ds = 1/0.125 = 8.0 against ds 4.0 -> ratio 2.0, well outside.
    assert apply_level_hysteresis(ideal_level=3, current_level=1,
                                  current_downsample=4.0,
                                  screen_px_per_world_px=0.125,
                                  threshold=0.2) == 3


def test_hysteresis_is_a_no_op_when_the_ideal_is_already_current():
    assert apply_level_hysteresis(2, 2, 4.0, 0.25, 0.2) == 2


def test_hysteresis_boundary_is_strict():
    """Exactly AT the threshold keeps the current level -- the comparison
    is `> threshold`, not `>=`.

    The numbers are chosen to be exact in binary so the assertion is about
    the comparison and not about float error: ideal_ds = 1/0.8 = 1.25
    against current ds 1.0 gives ratio 1.25, i.e. abs(ratio - 1) == 0.25,
    equal to the threshold to the bit.
    """
    assert apply_level_hysteresis(ideal_level=2, current_level=1,
                                  current_downsample=1.0,
                                  screen_px_per_world_px=0.8,
                                  threshold=0.25) == 1
    # A hair past it does switch.
    assert apply_level_hysteresis(ideal_level=2, current_level=1,
                                  current_downsample=1.0,
                                  screen_px_per_world_px=0.8,
                                  threshold=0.2499) == 2


@pytest.mark.parametrize("spp", [0.0, -1.0])
def test_hysteresis_takes_the_ideal_when_the_zoom_is_degenerate(spp):
    assert apply_level_hysteresis(3, 1, 4.0, spp, 0.2) == 3


def test_hysteresis_survives_a_zero_downsample():
    """A provider reporting ds 0 must not divide by zero; the controller's
    `if current_downsample else 1.0` maps it to ratio 1.0 -- inside every
    band, so the level stays put."""
    assert apply_level_hysteresis(2, 1, 0.0, 0.25, 0.2) == 1


# ── viewport clamping and level conversion ───────────────────────────────────

def test_clamp_keeps_an_interior_viewport_and_truncates():
    assert clamp_viewport_to_level0(10.9, 20.9, 100.9, 200.9,
                                    4096, 4096) == (10, 20, 100, 200)


def test_clamp_pulls_a_viewport_back_inside_the_slide():
    assert clamp_viewport_to_level0(-50.0, -70.0, 5000.0, 6000.0,
                                    4096, 2048) == (0, 0, 4096, 2048)


def test_clamp_of_a_fully_outside_viewport_is_empty_not_negative():
    y0, x0, y1, x1 = clamp_viewport_to_level0(-500.0, -500.0, -100.0, -100.0,
                                              4096, 4096)
    assert (y0, x0, y1, x1) == (0, 0, 0, 0)


@pytest.mark.parametrize(
    ("bbox", "ds", "expected"),
    [((0, 0, 1024, 1024), 1.0, (0, 0, 1024, 1024)),
     ((0, 0, 1024, 1024), 4.0, (0, 0, 256, 256)),
     # Truncation, NOT rounding: 1023/4 = 255.75 -> 255.
     ((4, 4, 1023, 1023), 4.0, (1, 1, 255, 255)),
     ((10, 20, 30, 40), 3.0, (3, 6, 10, 13))],
)
def test_bbox_to_level(bbox, ds, expected):
    assert bbox_to_level(bbox, ds) == expected


def test_visible_tiles_is_the_conversion_then_the_cover():
    bbox_l0 = (0, 0, 2048, 2048)
    ds, tile = 4.0, 256
    assert visible_tiles_for_viewport(bbox_l0, ds, tile) == tiles_covering(
        bbox_to_level(bbox_l0, ds), tile)


def test_visible_tiles_of_an_empty_bbox_is_empty():
    assert visible_tiles_for_viewport((0, 0, 0, 0), 1.0, 512) == set()


def test_visible_tiles_covers_a_partial_tile():
    # One pixel into the second tile column still needs that column.
    assert visible_tiles_for_viewport((0, 0, 1, 513), 1.0, 512) == {(0, 0), (1, 0)}


# ── pan / zoom classification ────────────────────────────────────────────────

def test_first_frame_is_neither_shrinking_nor_zooming():
    assert classify_zoom(None, 1000.0) == (False, False)


def test_pure_pan_is_neither():
    assert classify_zoom(1000.0, 1000.0) == (False, False)


def test_zoom_in_is_shrinking_and_zooming():
    assert classify_zoom(1000.0, 500.0) == (True, True)


def test_zoom_out_is_zooming_but_not_shrinking():
    assert classify_zoom(1000.0, 2000.0) == (False, True)


@pytest.mark.parametrize("area", [998.0, 1002.0])
def test_sub_tolerance_jitter_is_not_a_zoom(area):
    """Absolute numbers, deliberately: writing these in terms of
    ZOOM_AREA_TOLERANCE would make the test move with the constant and
    stop pinning the 0.5% band at all."""
    assert classify_zoom(1000.0, area) == (False, False)


@pytest.mark.parametrize(
    ("area", "expected"),
    [(994.0, (True, True)),      # -0.6%: just past the band, zoom in
     (1006.0, (False, True)),    # +0.6%: just past it, zoom out
     (996.0, (False, False)),    # -0.4%: still jitter
     (1004.0, (False, False))],  # +0.4%: still jitter
)
def test_the_band_is_half_a_percent(area, expected):
    assert ZOOM_AREA_TOLERANCE == 0.005
    assert classify_zoom(1000.0, area) == expected


def test_the_tolerance_is_a_parameter():
    assert classify_zoom(1000.0, 994.0, tolerance=0.05) == (False, False)
    assert classify_zoom(1000.0, 900.0, tolerance=0.05) == (True, True)


def test_a_zero_previous_area_is_neither():
    """The controller guards the zoom test with `prev_area > 0.0` and the
    shrink test not at all. The asymmetry is preserved verbatim, but it
    cannot be observed: a world area is never negative, so from 0 nothing
    shrinks either."""
    assert classify_zoom(0.0, 0.0) == (False, False)
    assert classify_zoom(0.0, 1000.0) == (False, False)


# ── differential: the pure functions vs the controller still in place ────────
#
# These are the evidence that migrating a call site is safe. They sweep the
# same inputs through `ExploreController`'s own private methods and through
# the pure functions and require identical answers -- so a divergence shows
# up here, BEFORE any call site moves, rather than as a rendering change.

from test_explore_controller import (  # noqa: E402
    _pump,
    app,            # noqa: F401  (pytest fixture)
    make_controller,
)


SPP_SWEEP = [4.0, 2.0, 1.5, 1.0, 0.9, 0.75, 0.5, 0.4, 0.3, 0.26, 0.25, 0.24,
             0.2, 0.15, 0.125, 0.1, 0.05, 0.01, 0.001, 0.0, -1.0]


def _downsamples(provider):
    return [provider.level_downsample(i) for i in range(provider.num_levels)]


def test_pure_level_pick_matches_the_controller_over_a_zoom_sweep(app):
    ctrl, provider, _scheduler, _view = make_controller(app)
    try:
        downsamples = _downsamples(provider)
        for spp in SPP_SWEEP:
            assert (pick_display_level(downsamples, spp)
                    == ctrl._pick_display_level(spp)), spp
    finally:
        ctrl.teardown()


def test_pure_hysteresis_matches_the_controller_from_every_level(app):
    ctrl, provider, _scheduler, _view = make_controller(app)
    try:
        downsamples = _downsamples(provider)
        for current in range(provider.num_levels):
            ctrl.level = current
            for spp in SPP_SWEEP:
                ideal = ctrl._pick_display_level(spp)
                expected = ctrl._pick_display_level_with_hysteresis(spp)
                got = apply_level_hysteresis(
                    ideal_level=ideal, current_level=current,
                    current_downsample=provider.level_downsample(current),
                    screen_px_per_world_px=spp,
                    threshold=ctrl.LEVEL_HYSTERESIS)
                assert got == expected, (current, spp)
    finally:
        ctrl.teardown()


VIEWPORT_SWEEP = [
    (0.0, 0.0, 4096.0, 4096.0),          # whole slide
    (-100.0, -100.0, 500.0, 500.0),      # off the top-left corner
    (3900.0, 3900.0, 4500.0, 4500.0),    # off the bottom-right corner
    (1000.4, 2000.6, 1512.9, 2512.1),    # interior, fractional edges
    (-500.0, -500.0, -100.0, -100.0),    # entirely outside
    (2048.0, 2048.0, 2048.0, 2048.0),    # degenerate, zero area
]


def test_pure_clamp_and_visible_tiles_match_the_controller(app):
    """Drives the controller's real range handler and compares its
    `_current_bbox` / `_visible_tiles` against the pure functions."""
    ctrl, provider, _scheduler, view = make_controller(app)
    try:
        level0_h, level0_w = provider.level_shape(0)
        for (y0, x0, y1, x1) in VIEWPORT_SWEEP:
            view.view_box.setRange(xRange=(x0, x1), yRange=(y0, y1),
                                   padding=0)
            _pump(30)

            # Read back what the ViewBox actually settled on -- it applies
            # its own limits, so the requested range is not necessarily the
            # live one.
            (vx0, vx1), (vy0, vy1) = view.view_box.viewRange()
            expected_bbox = clamp_viewport_to_level0(
                vy0, vx0, vy1, vx1, level0_h, level0_w)
            assert ctrl._current_bbox == expected_bbox, (y0, x0, y1, x1)

            ds = provider.level_downsample(ctrl.level)
            assert ctrl._visible_tiles == visible_tiles_for_viewport(
                ctrl._current_bbox, ds, ctrl.grid.tile_size)
    finally:
        ctrl.teardown()


def test_pure_zoom_classification_matches_the_controller(app):
    """Replays a pan-then-zoom sequence and compares the controller's
    `_viewport_shrinking` / `_viewport_zooming` with `classify_zoom`."""
    ctrl, provider, _scheduler, view = make_controller(app)
    try:
        sequence = [
            (0.0, 0.0, 1024.0, 1024.0),      # first frame: neither
            (100.0, 0.0, 1124.0, 1024.0),    # pure pan, same area
            (0.0, 0.0, 512.0, 512.0),        # zoom in
            (0.0, 0.0, 2048.0, 2048.0),      # zoom out
            (10.0, 10.0, 2058.0, 2058.0),    # pan at the new scale
        ]
        prev_area = None
        for (y0, x0, y1, x1) in sequence:
            view.view_box.setRange(xRange=(x0, x1), yRange=(y0, y1),
                                   padding=0)
            _pump(30)
            (vx0, vx1), (vy0, vy1) = view.view_box.viewRange()
            area = max(0.0, vx1 - vx0) * max(0.0, vy1 - vy0)

            expected = classify_zoom(prev_area, area)
            assert (ctrl._viewport_shrinking, ctrl._viewport_zooming) == expected, (
                (y0, x0, y1, x1), prev_area, area)
            prev_area = area
    finally:
        ctrl.teardown()
