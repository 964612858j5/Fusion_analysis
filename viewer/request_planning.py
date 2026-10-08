"""Viewport → what-to-request planning: pure functions, no Qt, no I/O.

Same shape as `viewer.prefetch_policy`: plain data in, plain data out. No
Qt import, no provider / scheduler / view / tile-pool reference, no state
kept between calls, no timers, no threads, no logging, no generations, no
cancellation. `ExploreController` keeps every side effect -- writing
`self.level`, bumping generations, calling `scheduler.request`, touching
the pools -- and calls these to decide WHAT the answer is.

Where the "already covered / still missing" questions come in, the
controller computes those SETS from its pools and passes them here; these
functions never look at a pool. WHEN each set is snapshotted is part of the
controller's contract and stays there: each layer's missing set is
snapshotted from the pools immediately before that layer's batch is
issued, and every tile in it is requested. (An earlier revision had the
fallback layer's set filtered afterwards by a GUI-thread synthesis step;
that step is gone -- see `explore_view`'s "No synthesized coarse fallback
(removed)".)

This first round covers only the geometry-and-scale half of planning:
display-level choice with hysteresis, viewport clamping, level-0 → level
conversion, the visible tile set, and the pan/zoom classification. Request
ordering (centre-out, the fallback urgent/ring split, the directional
corridor) is not here yet.

Every function reproduces the controller's existing arithmetic exactly,
including its rounding and its odd corners -- see the notes on
`pick_display_level` and `clamp_viewport_to_level0`. This module is a
relocation of logic, not a chance to improve it.
"""

from typing import Optional, Sequence, Set, Tuple

from .tile_types import tiles_covering

Bbox = Tuple[int, int, int, int]          # (y0, x0, y1, x1)
TileCoord = Tuple[int, int]               # (tx, ty)

# A world-area change of less than this fraction is not motion: the same
# 0.5% band the controller has always used, so a drag's sub-pixel jitter
# does not read as a zoom.
ZOOM_AREA_TOLERANCE = 0.005


def pick_display_level(downsamples: Sequence[float],
                       screen_px_per_world_px: float) -> int:
    """The pyramid level to display at, by nearest-below downsample.

    For a given zoom (screen pixels per WORLD pixel) the ideal level has
    downsample ~= 1 / screen_px_per_world_px. This picks the largest
    downsample that does not exceed that ideal, falling back to level 0.

    Two corners are preserved deliberately, because changing either would
    change which level the viewer shows:

    * `screen_px_per_world_px <= 0` returns 0 rather than dividing.
    * the scan condition is `ds <= ideal_ds and ds >= best_ds`, i.e. it
      keeps the LAST qualifying level in list order when several share a
      downsample, and on a non-monotonic pyramid it can pass over a
      qualifying level that is smaller than one already seen. That is the
      existing behaviour; this is not the place to "fix" it.
    """
    if screen_px_per_world_px <= 0:
        return 0
    ideal_ds = 1.0 / screen_px_per_world_px
    best_level = 0
    best_ds = downsamples[0]
    for level in range(len(downsamples)):
        ds = downsamples[level]
        if ds <= ideal_ds and ds >= best_ds:
            best_level = level
            best_ds = ds
    return best_level


#: Block A9 S2a (Odon `imaging/tiling.rs`): the Step1/Step3 viewers pick the
#: level CLOSEST to 1:1 in log scale instead of the nearest-below one.
LEVEL_POLICY_NEAREST_BELOW = "nearest_below"
LEVEL_POLICY_NEAREST_LOG = "nearest_log"
#: How much better (natural-log units) the nearest level must be before a
#: displayed level is left: on a regular pyramid a +-0.075 band around the
#: geometric midpoint between two levels.
LOG_LEVEL_MARGIN = 0.15


def _log_error(downsample: float, screen_px_per_world_px: float) -> float:
    import math
    return abs(math.log(max(1e-12, float(downsample) * screen_px_per_world_px)))


def pick_display_level_nearest(downsamples: Sequence[float],
                               screen_px_per_world_px: float) -> int:
    """The level whose pixels come closest to one screen pixel each.

    Minimises ``|ln(screen_px_per_world_px * downsample)|``. On a tie the
    COARSER level wins (it needs fewer pixels); among levels with the same
    downsample the lowest index wins. A non-positive zoom returns 0, as
    `pick_display_level` does."""
    if screen_px_per_world_px <= 0 or not downsamples:
        return 0
    best_level, best_err, best_ds = 0, None, None
    for level, ds in enumerate(downsamples):
        err = _log_error(ds, screen_px_per_world_px)
        if (best_err is None or err < best_err - 1e-12
                or (abs(err - best_err) <= 1e-12 and ds > best_ds)):
            best_level, best_err, best_ds = level, err, ds
    return best_level


def apply_level_hysteresis_log(ideal_level: int, current_level: int,
                               downsamples: Sequence[float],
                               screen_px_per_world_px: float,
                               margin: float = LOG_LEVEL_MARGIN) -> int:
    """Keep `current_level` unless `ideal_level` is better by `margin`.

    Both are compared by their log error from 1:1; a jump of several levels
    goes straight to `ideal_level`. A current level outside the pyramid, or
    a non-positive zoom, yields `ideal_level`."""
    if ideal_level == current_level or screen_px_per_world_px <= 0:
        return ideal_level
    if not 0 <= int(current_level) < len(downsamples):
        return ideal_level
    current_err = _log_error(downsamples[current_level], screen_px_per_world_px)
    ideal_err = _log_error(downsamples[ideal_level], screen_px_per_world_px)
    if ideal_err + margin < current_err:
        return ideal_level
    return current_level


def apply_level_hysteresis(ideal_level: int, current_level: int,
                           current_downsample: float,
                           screen_px_per_world_px: float,
                           threshold: float) -> int:
    """Keep `current_level` unless the zoom has moved far enough.

    Without this, a zoom sitting right at a level boundary thrashes: every
    tiny wheel step flips the level, which re-orders z, re-requests tiles
    and re-blits. The comparison is on the RATIO of the ideal downsample to
    the current level's, and the level only changes when that ratio is more
    than `threshold` away from 1.0 -- so the band is asymmetric in absolute
    zoom, which is what the measured behaviour was tuned against.
    """
    if ideal_level == current_level:
        return current_level
    if screen_px_per_world_px <= 0:
        return ideal_level
    ideal_ds = 1.0 / screen_px_per_world_px
    ratio = ideal_ds / current_downsample if current_downsample else 1.0
    if abs(ratio - 1.0) > threshold:
        return ideal_level
    return current_level


def clamp_viewport_to_level0(y0: float, x0: float, y1: float, x1: float,
                             level0_h: int, level0_w: int) -> Bbox:
    """The visible world rect, clipped to the slide and truncated to ints.

    `int()` truncates toward zero, which after the clamp to [0, extent] is
    a floor. Both are the existing behaviour and both matter: truncating y1
    down means a viewport edge that lands mid-pixel does not pull in an
    extra tile row.
    """
    wy0 = max(0, min(y0, level0_h))
    wy1 = max(0, min(y1, level0_h))
    wx0 = max(0, min(x0, level0_w))
    wx1 = max(0, min(x1, level0_w))
    return (int(wy0), int(wx0), int(wy1), int(wx1))


def bbox_to_level(bbox_l0: Bbox, downsample: float) -> Bbox:
    """Level-0 pixel bbox → the same rect in level-N pixels.

    Plain division then `int()`, per corner, exactly as the controller has
    always done -- NOT a floor/ceil pair. A ceil on the far edges would
    silently widen every request set by up to one tile row and column.
    """
    return (
        int(bbox_l0[0] / downsample),
        int(bbox_l0[1] / downsample),
        int(bbox_l0[2] / downsample),
        int(bbox_l0[3] / downsample),
    )


def visible_tiles_for_viewport(bbox_l0: Bbox, downsample: float,
                               tile_size: int) -> Set[TileCoord]:
    """The tile set covering `bbox_l0` at a level with `downsample`.

    The one composition worth naming: the controller does this same
    two-step -- convert, then cover -- at four call sites, and a mismatch
    between any two of them is a class of bug that is invisible until a
    tile is requested at one level and looked up at another. `tiles_covering`
    itself stays in `tile_types`, where the grid convention lives.
    """
    return tiles_covering(bbox_to_level(bbox_l0, downsample), tile_size)


def classify_zoom(previous_world_area: Optional[float], world_area: float,
                  tolerance: float = ZOOM_AREA_TOLERANCE) -> Tuple[bool, bool]:
    """(shrinking, zooming) from the change in visible world area.

    `shrinking` means zoom-IN (the world rect got smaller) and suppresses
    the fallback look-ahead ring, which only pays off while panning.
    `zooming` means a change in EITHER direction, which disqualifies the
    tick from directional prefetch entirely.

    `previous_world_area` is None on the first frame, which is neither. The
    controller guards the zoom test with `prev_area > 0.0` and the shrink
    test not at all; that asymmetry is preserved, but it has no observable
    effect: a world area is never negative, so from a previous area of 0
    nothing can shrink either.
    """
    if previous_world_area is None:
        return (False, False)
    shrinking = world_area < previous_world_area * (1.0 - tolerance)
    zooming = (previous_world_area > 0.0
               and (world_area < previous_world_area * (1.0 - tolerance)
                    or world_area > previous_world_area * (1.0 + tolerance)))
    return (shrinking, zooming)
