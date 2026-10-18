"""TMA layout geometry (F3, docs/tma_f3_page_application.md §3.3): the
region outline of one core. One piece -> its 64-gon (from due right,
counter-clockwise on screen, whole pixels, clipped to the slide); several
-> the raster union's outer contours (overlaps and holes merged), joined
into ONE ring by there-and-back bridges, because A3's `region_id` and
`core.preseg_input.RoiMask` take a single polygon. `fill` is RoiMask's
rasterisation, used for the lock's overlap check, bridges included.
"""

from __future__ import annotations

import math
from typing import Dict, List, Sequence, Tuple

import numpy as np

CIRCLE_VERTICES = 64


def circle_polygon(cy: float, cx: float, r: float, shape: Tuple[int, int]) -> List[List[int]]:
    """64-gon from due right, counter-clockwise on screen, whole pixels,
    clipped to the slide."""
    h, w = shape
    out = []
    for k in range(CIRCLE_VERTICES):
        a = 2 * math.pi * k / CIRCLE_VERTICES
        x = int(round(cx + r * math.cos(a)))
        y = int(round(cy - r * math.sin(a)))
        out.append([min(max(x, 0), w - 1), min(max(y, 0), h - 1)])
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


def core_geometry(pieces: Sequence[Dict], shape: Tuple[int, int]) -> Dict:
    """``bbox_fullres``, ``polygon_fullres`` and the pieces of one core."""
    polys = [circle_polygon(p["cy"], p["cx"], p["r"], shape) for p in pieces]
    if len(polys) == 1:
        poly = polys[0]
    else:
        poly = _union_ring(polys, shape)
    return {"pieces": [{"cy": round(p["cy"], 2), "cx": round(p["cx"], 2), "r": round(p["r"], 2)} for p in pieces],
            "polygon_fullres": poly, "bbox_fullres": bbox_of(poly, shape)}


def _union_ring(polys, shape) -> List[List[int]]:
    import cv2
    allpts = [q for p in polys for q in p]
    box = bbox_of(allpts, shape)
    y0, y1, x0, x1 = box
    canvas = np.zeros((y1 - y0, x1 - x0), dtype=np.uint8)
    for p in polys:
        cv2.fillPoly(canvas, [np.array([[x - x0, y - y0] for x, y in p], dtype=np.int32)], color=1)
    found, _ = cv2.findContours(canvas, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours = [[[int(q[0][0]) + x0, int(q[0][1]) + y0] for q in c] for c in found]
    contours = [c for c in contours if len(c) >= 1]
    contours.sort(key=lambda c: (min(q[1] for q in c), min(q[0] for q in c)))
    if len(contours) == 1:
        return contours[0]
    return keyhole(contours)


def keyhole(contours: List[List[List[int]]]) -> List[List[int]]:
    """Join contours into one ring: a minimum spanning tree over the nearest
    vertex pairs (Prim from contour 0; ties by index), each tree edge walked
    there and back. Deterministic for the same input."""
    n = len(contours)
    arrs = [np.asarray(c, dtype=np.float64) for c in contours]

    def nearest(i, j):
        dist = ((arrs[i][:, None, :] - arrs[j][None, :, :]) ** 2).sum(-1)
        k = int(np.argmin(dist))
        return float(dist.flat[k]), k // dist.shape[1], k % dist.shape[1]

    pair = {}
    for i in range(n):
        for j in range(i + 1, n):
            pair[(i, j)] = nearest(i, j)
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

    ring = walk(0, 0)
    return ring[:-1]                       # the closing vertex is implicit
