"""Regions geometry (docs/region_qc_module_plan.md §3): every shape is a
polygon ``[[x, y], ...]`` in whole level-0 pixels, clipped to the slide.

* `circle_polygon` -- the Circle tool's shape (a 64-gon), also what old
  circle data becomes.
* Edits: `translate`, `scale` (about the centroid), `move_vertex`.
* `region_outline` -- the region of a core made of one or more pieces:
  one piece is its polygon; several are merged at full resolution and
  their outer contours joined into ONE ring by there-and-back bridges,
  because A3's `region_id` and `core.preseg_input.RoiMask` take a single
  polygon (ported from F3's `core/tma/layout_geometry.py`).
* `fill` is RoiMask's rasterisation; `overlap` / `overlap_area` use it.
"""

from __future__ import annotations

import math
from typing import Dict, List, Sequence, Tuple

import numpy as np

CIRCLE_VERTICES = 64
Polygon = List[List[int]]


def _clip(x, y, shape) -> List[int]:
    h, w = shape
    return [min(max(int(round(x)), 0), w - 1), min(max(int(round(y)), 0), h - 1)]


def circle_polygon(cy: float, cx: float, r: float, shape: Tuple[int, int]) -> Polygon:
    """64-gon from due right, counter-clockwise on screen, whole pixels,
    clipped to the slide."""
    out = []
    for k in range(CIRCLE_VERTICES):
        a = 2 * math.pi * k / CIRCLE_VERTICES
        out.append(_clip(cx + r * math.cos(a), cy - r * math.sin(a), shape))
    return out


def ellipse_polygon(y0: float, x0: float, y1: float, x1: float, shape: Tuple[int, int]) -> Polygon:
    """The 64-gon inscribed in the box of two opposite corners (the Circle
    tool, drawn like Photoshop's elliptical marquee), clipped to the slide."""
    cy, cx, ry, rx = (y0 + y1) / 2, (x0 + x1) / 2, abs(y1 - y0) / 2, abs(x1 - x0) / 2
    out = []
    for k in range(CIRCLE_VERTICES):
        a = 2 * math.pi * k / CIRCLE_VERTICES
        out.append(_clip(cx + rx * math.cos(a), cy - ry * math.sin(a), shape))
    return out


UNION_MAX_PIXELS = 100_000_000


def union_outline(polys: Sequence[Polygon], shape):
    """(outline, None) -- the one outer outline of polygons that touch or
    overlap (Merge ▸ Union; holes inside are filled, vertices simplified
    within one pixel) -- or (None, reason): "apart" when they make more than
    one region (4-connected pixels), "too_far" when their box exceeds
    `UNION_MAX_PIXELS`. Rasterised at full resolution like `fill`."""
    import cv2
    polys = [p for p in polys if len(p) >= 3]
    if not polys:
        return None, "apart"
    y0, y1, x0, x1 = bbox_of([q for p in polys for q in p], shape)
    if (y1 - y0 + 2) * (x1 - x0 + 2) > UNION_MAX_PIXELS:
        return None, "too_far"
    canvas = np.zeros((y1 - y0 + 2, x1 - x0 + 2), dtype=np.uint8)       # a free border: outlines close
    for p in polys:
        cv2.fillPoly(canvas, [np.array([[x - x0 + 1, y - y0 + 1] for x, y in p], dtype=np.int32)], color=1)
    n, _labels = cv2.connectedComponents(canvas, connectivity=4)
    if n != 2:                                                           # background + one region
        return None, "apart"
    found, _ = cv2.findContours(canvas, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    ring = cv2.approxPolyDP(max(found, key=cv2.contourArea), 1.0, True)
    if len(ring) < 3:
        return None, "apart"
    return [_clip(int(q[0][0]) + x0 - 1, int(q[0][1]) + y0 - 1, shape) for q in ring], None


def area(poly: Sequence[Sequence[float]]) -> float:
    if len(poly) < 3:
        return 0.0
    xs = np.asarray([p[0] for p in poly], float)
    ys = np.asarray([p[1] for p in poly], float)
    return float(abs(np.dot(xs, np.roll(ys, -1)) - np.dot(ys, np.roll(xs, -1))) / 2)


def centroid(poly: Sequence[Sequence[float]]) -> Tuple[float, float]:
    """(y, x) area centroid; the vertex mean for a degenerate polygon."""
    xs = np.asarray([p[0] for p in poly], float)
    ys = np.asarray([p[1] for p in poly], float)
    cross = xs * np.roll(ys, -1) - np.roll(xs, -1) * ys
    a = cross.sum() / 2
    if abs(a) < 1e-9:
        return float(ys.mean()), float(xs.mean())
    cx = ((xs + np.roll(xs, -1)) * cross).sum() / (6 * a)
    cy = ((ys + np.roll(ys, -1)) * cross).sum() / (6 * a)
    return float(cy), float(cx)


def contains(poly: Sequence[Sequence[float]], y: float, x: float) -> bool:
    import cv2
    if len(poly) < 3:
        return False
    return cv2.pointPolygonTest(np.asarray(poly, np.float32).reshape(-1, 1, 2), (float(x), float(y)), False) >= 0


def translate(poly: Polygon, dy: float, dx: float, shape) -> Polygon:
    return [_clip(p[0] + dx, p[1] + dy, shape) for p in poly]


def scale(poly: Polygon, factor: float, shape, about=None) -> Polygon:
    if not (factor > 0 and math.isfinite(factor)):
        raise ValueError("a scale needs a positive finite factor")
    cy, cx = about if about is not None else centroid(poly)
    return [_clip(cx + (p[0] - cx) * factor, cy + (p[1] - cy) * factor, shape) for p in poly]


def move_vertex(poly: Polygon, index: int, y: float, x: float, shape) -> Polygon:
    out = [list(p) for p in poly]
    out[index] = _clip(x, y, shape)
    return out


def bbox_of(polygon, shape) -> List[int]:
    """``[y0, y1, x0, x1]``, half-open: max + 1, clipped to the slide."""
    h, w = shape
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    return [max(min(ys), 0), min(max(ys) + 1, h), max(min(xs), 0), min(max(xs) + 1, w)]


def fill(polygon, bbox) -> np.ndarray:
    """The polygon filled over `bbox` exactly as `core.preseg_input.RoiMask`
    does (vertices truncated relative to the bbox, `cv2.fillPoly`)."""
    import cv2
    y0, y1, x0, x1 = (int(v) for v in bbox)
    canvas = np.zeros((y1 - y0, x1 - x0), dtype=np.uint8)
    pts = np.array([[int(x - x0), int(y - y0)] for x, y in polygon], dtype=np.int32)
    cv2.fillPoly(canvas, [pts], color=1)
    return canvas.astype(bool)


def region_outline(polys: Sequence[Polygon], shape) -> Dict:
    """``polygon_fullres`` and ``bbox_fullres`` of one region made of
    `polys` (pieces)."""
    polys = [p for p in polys if len(p) >= 3]
    if not polys:
        raise ValueError("a region needs at least one polygon")
    poly = [list(q) for q in polys[0]] if len(polys) == 1 else _union_ring(polys, shape)
    return {"polygon_fullres": poly, "bbox_fullres": bbox_of(poly, shape)}


def _union_ring(polys, shape) -> Polygon:
    import cv2
    box = bbox_of([q for p in polys for q in p], shape)
    y0, y1, x0, x1 = box
    canvas = np.zeros((y1 - y0, x1 - x0), dtype=np.uint8)
    for p in polys:
        cv2.fillPoly(canvas, [np.array([[x - x0, y - y0] for x, y in p], dtype=np.int32)], color=1)
    found, _ = cv2.findContours(canvas, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours = [[[int(q[0][0]) + x0, int(q[0][1]) + y0] for q in c] for c in found]
    contours.sort(key=lambda c: (min(q[1] for q in c), min(q[0] for q in c)))
    return contours[0] if len(contours) == 1 else keyhole(contours)


def keyhole(contours: List[Polygon]) -> Polygon:
    """Join contours into one ring: a minimum spanning tree over the nearest
    vertex pairs (Prim from contour 0; ties by index), each tree edge walked
    there and back. Deterministic for the same input."""
    n = len(contours)
    arrs = [np.asarray(c, dtype=np.float64) for c in contours]

    def nearest(i, j):
        dist = ((arrs[i][:, None, :] - arrs[j][None, :, :]) ** 2).sum(-1)
        k = int(np.argmin(dist))
        return float(dist.flat[k]), k // dist.shape[1], k % dist.shape[1]

    pair = {(i, j): nearest(i, j) for i in range(n) for j in range(i + 1, n)}
    inside, children = {0}, {i: [] for i in range(n)}
    while len(inside) < n:
        best = None
        for i in sorted(inside):
            for j in range(n):
                if j in inside:
                    continue
                dist, a, b = pair[(i, j)] if i < j else (pair[(j, i)][0], pair[(j, i)][2], pair[(j, i)][1])
                if best is None or (dist, i, j) < best[0]:
                    best = ((dist, i, j), i, j, a, b)
        _, i, j, a, b = best
        children[i].append((a, j, b))
        inside.add(j)

    def walk(i, entry):
        c, m = contours[i], len(contours[i])
        out = []
        for k in range(m + 1):
            idx = (entry + k) % m
            out.append(list(c[idx]))
            if k < m:
                for a, j, b in sorted(children[i]):
                    if a == idx:
                        out.extend(walk(j, b))
                        out.append(list(c[idx]))
        return out

    return walk(0, 0)[:-1]                 # the closing vertex is implicit


def has_area(g: Dict) -> bool:
    from ..project_identity import normalize_polygon
    y0, y1, x0, x1 = g["bbox_fullres"]
    if normalize_polygon(g["polygon_fullres"]) is None or y1 - y0 < 2 or x1 - x0 < 2:
        return False
    return area(g["polygon_fullres"]) > 0


def overlap_area(ga: Dict, gb: Dict) -> int:
    """Pixels the two regions' fills share (bridges included)."""
    ay0, ay1, ax0, ax1 = ga["bbox_fullres"]
    by0, by1, bx0, bx1 = gb["bbox_fullres"]
    y0, y1, x0, x1 = max(ay0, by0), min(ay1, by1), max(ax0, bx0), min(ax1, bx1)
    if y0 >= y1 or x0 >= x1:
        return 0
    fa = fill(ga["polygon_fullres"], ga["bbox_fullres"])[y0 - ay0:y1 - ay0, x0 - ax0:x1 - ax0]
    fb = fill(gb["polygon_fullres"], gb["bbox_fullres"])[y0 - by0:y1 - by0, x0 - bx0:x1 - bx0]
    return int((fa & fb).sum())


def overlap(ga: Dict, gb: Dict) -> bool:
    return overlap_area(ga, gb) > 0
