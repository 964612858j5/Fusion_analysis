"""Is the frame on screen covered? (block A9-M; plan v2.4 §20.3 A9-1/2/3).

A frame has a GAP when some part of the viewport -- clipped to the slide, the
analysis region's rectangle and its polygon -- is covered by no valid pixel of
a channel the frame should compose. Never judged by pixel colour (real tissue
can be black): by the planes / tiles actually submitted for that frame and
where their pixels are valid. A channel the frame should compose but has no
source for yet counts as uncovered everywhere.

SAMPLED, and reported as such (`sampled`): the viewport is SAMPLES x SAMPLES
points; a plane's validity is summarised once per plane as CELLS x CELLS
cells (a cell valid only if every pixel in it is); a viewport CELL (GRID x
GRID of them) is covered only if every sample point in it is. A hole
narrower than one sample (1/SAMPLES of the viewport) can be missed; the
report says so rather than claiming more.

Pure numpy, no Qt: the GPU layer and the Step0 view call `gpu_frame` /
`rect_frame` with what they drew and `publish` the result as a perf mark.
"""

import collections

import numpy as np

GRID = 64          # reported viewport cells per side
SAMPLES = 256      # sample points per side (SAMPLES // GRID per cell side)
CELLS = 16         # validity cells per plane side
_SUMMARY = collections.OrderedDict()      # plane identity -> CELLS x CELLS bool
_SUMMARY_MAX = 4096
#: The latest frame's result, and how many results have been published, for
#: the A9 driver's "settled" test.
LAST = None
PUBLISHED = 0


def publish(result, where, frame=None):
    """Remember `result` as the latest frame's and emit it as a perf mark."""
    global LAST, PUBLISHED
    LAST = dict(result, where=where, frame=frame)
    PUBLISHED += 1
    from ..utils import perf_trace
    perf_trace.mark("coverage", where=where, frame=frame, **result)


def _valid_cells(plane):
    """A plane's CELLS x CELLS validity, computed once per identity."""
    key = getattr(plane, "identity", None)
    if key is not None and key in _SUMMARY:
        _SUMMARY.move_to_end(key)
        return _SUMMARY[key]
    valid = getattr(plane, "valid", None)
    if valid is None:
        values = np.asarray(plane.values)
        valid = ~np.isnan(values) if values.dtype.kind == "f" else np.ones(values.shape, bool)
    valid = np.asarray(valid, bool)
    h, w = valid.shape[:2]
    out = np.zeros((CELLS, CELLS), bool)
    if h and w:
        ys = np.linspace(0, h, CELLS + 1).astype(int)
        xs = np.linspace(0, w, CELLS + 1).astype(int)
        for i in range(CELLS):
            for j in range(CELLS):
                block = valid[ys[i]:max(ys[i + 1], ys[i] + 1), xs[j]:max(xs[j + 1], xs[j] + 1)]
                out[i, j] = bool(block.size) and bool(block.all())
    if key is not None:
        _SUMMARY[key] = out
        while len(_SUMMARY) > _SUMMARY_MAX:
            _SUMMARY.popitem(last=False)
    return out


def _clip(world, other):
    x0, x1, y0, y1 = (float(v) for v in world)
    if other is not None:
        ox0, ox1, oy0, oy1 = (float(v) for v in other)
        x0, x1, y0, y1 = max(x0, ox0), min(x1, ox1), max(y0, oy0), min(y1, oy1)
    return x0, x1, y0, y1


def _points(lo, hi):
    step = (hi - lo) / SAMPLES
    return lo + step * (np.arange(SAMPLES) + 0.5)


def _inside_polygon(px, py, polygon):
    """SAMPLES x SAMPLES bool: sample points inside a simple polygon of
    (x, y) world points (even-odd rule)."""
    X, Y = np.meshgrid(px, py)
    inside = np.zeros(X.shape, bool)
    pts = [(float(x), float(y)) for x, y in polygon]
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        if y1 == y2:
            continue
        crosses = (Y >= min(y1, y2)) & (Y < max(y1, y2))
        xint = x1 + (Y - y1) * (x2 - x1) / (y2 - y1)
        inside ^= crosses & (X < xint)
    return inside


def _paint(covered, px, py, rect, cells=None):
    """Mark the sample points inside `rect` (x0, x1, y0, y1) and, with
    `cells`, inside a valid cell of it."""
    rx0, rx1, ry0, ry1 = (float(v) for v in rect)
    if rx1 <= rx0 or ry1 <= ry0:
        return
    ix = np.nonzero((px >= rx0) & (px < rx1))[0]
    iy = np.nonzero((py >= ry0) & (py < ry1))[0]
    if not ix.size or not iy.size:
        return
    if cells is None:
        covered[np.ix_(iy, ix)] = True
        return
    vx = np.minimum(((px[ix] - rx0) / (rx1 - rx0) * CELLS).astype(int), CELLS - 1)
    vy = np.minimum(((py[iy] - ry0) / (ry1 - ry0) * CELLS).astype(int), CELLS - 1)
    covered[np.ix_(iy, ix)] |= cells[np.ix_(vy, vx)]


def _cells(points_ok):
    """GRID x GRID: a cell is covered only if all its sample points are."""
    k = SAMPLES // GRID
    return points_ok.reshape(GRID, k, GRID, k).all(axis=(1, 3))


def _frame_geometry(viewport_world, roi_world, slide_world, polygon):
    x0, x1, y0, y1 = _clip(_clip(viewport_world, roi_world), slide_world)
    if x1 <= x0 or y1 <= y0:
        return None
    px, py = _points(x0, x1), _points(y0, y1)
    excluded = (~_inside_polygon(px, py, polygon) if polygon and len(polygon) >= 3
                else np.zeros((SAMPLES, SAMPLES), bool))
    return px, py, excluded


def _empty_result(n):
    return {"channels": n, "coarse_complete": True, "target_fraction": 1.0,
            "gap_cells": 0, "sampled": SAMPLES}


def gpu_frame(sources, viewport_world, roi_world=None, slide_world=None,
              polygon=None, expected=()):
    """Coverage of one GPU submission.

    `sources`: {channel: ChannelSource} actually composed (their `coarse` /
    `fine` planes and `selected_level`); `expected`: every channel the frame
    should compose -- one without a source is uncovered everywhere. Rects
    are (x0, x1, y0, y1) in level-0 world coordinates, like `world_rect`.
    The TARGET level is the channel's selected one (coarse when zoomed out).
    """
    channels = dict(sources or {})
    for name in expected or ():
        channels.setdefault(name, None)
    geom = _frame_geometry(viewport_world, roi_world, slide_world, polygon)
    if geom is None or not channels:
        return _empty_result(len(channels))
    px, py, excluded = geom
    gap = 0
    target_cells = 0
    coarse_ok = True
    for source in channels.values():
        coarse = excluded.copy()
        fine = excluded.copy()
        if source is not None:
            for plane in getattr(source, "coarse", ()) or ():
                _paint(coarse, px, py, plane.world_rect, _valid_cells(plane))
            level = getattr(source, "selected_level", "coarse")
            if level == "fine":
                for plane in getattr(source, "fine", ()) or ():
                    _paint(fine, px, py, plane.world_rect, _valid_cells(plane))
            target = fine if level == "fine" else coarse
        else:
            level, target = "coarse", coarse
        covered = _cells(coarse | fine)
        gap += int((~covered).sum())
        coarse_ok = coarse_ok and bool(_cells(coarse).all())
        target_cells += int(_cells(target).sum())
    return {"channels": len(channels), "coarse_complete": coarse_ok,
            "target_fraction": round(target_cells / (len(channels) * GRID * GRID), 4),
            "gap_cells": gap, "sampled": SAMPLES}


def rect_frame(rects_by_level, current_level, viewport_world, floor_rects=(),
               roi_world=None, slide_world=None, polygon=None):
    """Coverage of one CPU (Step0) frame.

    `rects_by_level`: {level: [(x0, x1, y0, y1), ...]} of the tiles VISIBLE
    in the frame; `floor_rects`: the whole-slide overview / floor items that
    are visible under them. Same keys as `gpu_frame`."""
    geom = _frame_geometry(viewport_world, roi_world, slide_world, polygon)
    if geom is None:
        return _empty_result(1)
    px, py, excluded = geom
    floor = excluded.copy()
    for rect in floor_rects or ():
        _paint(floor, px, py, rect)
    anything = floor.copy()
    target = excluded.copy()
    for level, rects in (rects_by_level or {}).items():
        for rect in rects:
            _paint(anything, px, py, rect)
            if int(level) == int(current_level):
                _paint(target, px, py, rect)
    covered = _cells(anything)
    return {"channels": 1, "coarse_complete": bool(_cells(floor).all()) or bool(covered.all()),
            "target_fraction": round(float(_cells(target).mean()), 4),
            "gap_cells": int((~covered).sum()), "sampled": SAMPLES}


__all__ = ["gpu_frame", "rect_frame", "publish", "GRID", "SAMPLES", "CELLS"]
