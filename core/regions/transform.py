"""The free transform of one shape, as Photoshop's Ctrl+T
(docs/regions_tools_redesign.md §10.1, §10.5) -- pure geometry, no Qt.

The state is a box of four corners ``quad`` (x, y floats, clockwise on
screen from the top left) that starts as the shape's bounding box, and a
direction ``theta`` (radians) that only a rotation changes. The shape is
always recomputed from its original vertices by the perspective map from
the original box to the current quad, so nothing drifts or gets rounded
between drags.

Handles: 0-3 the corners, 4-7 the edge midpoints (top, right, bottom,
left). Each function takes the quad as it was when the drag started and
returns the new one, or None when the result would be unusable (folded,
crossed, flattened): the caller then keeps the last good one.
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

import numpy as np

OPPOSITE = {0: 2, 1: 3, 2: 0, 3: 1, 4: 6, 5: 7, 6: 4, 7: 5}
EDGES = {4: (0, 1), 5: (1, 2), 6: (2, 3), 7: (3, 0)}
SNAP = math.radians(15.0)
MIN_FACTOR = 1e-3


def box_quad(points: Sequence[Sequence[float]]) -> np.ndarray:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    return np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=np.float64)


def handle_points(quad: np.ndarray) -> np.ndarray:
    q = np.asarray(quad, dtype=np.float64)
    mids = [(q[a] + q[b]) / 2 for a, b in (EDGES[4], EDGES[5], EDGES[6], EDGES[7])]
    return np.vstack([q, np.array(mids)])


def centre(quad: np.ndarray) -> np.ndarray:
    return np.asarray(quad, dtype=np.float64).mean(axis=0)


def _rot(v: np.ndarray, a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([c * v[..., 0] - s * v[..., 1], s * v[..., 0] + c * v[..., 1]]).T


def valid(quad: np.ndarray) -> bool:
    """Convex and not crossed: every turn has the same sign, none flat."""
    q = np.asarray(quad, dtype=np.float64)
    if not np.all(np.isfinite(q)):
        return False
    cross = []
    for i in range(4):
        a, b, c = q[i], q[(i + 1) % 4], q[(i + 2) % 4]
        cross.append((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]))
    scale = max(1.0, float(np.ptp(q[:, 0]) * np.ptp(q[:, 1])))
    return all(c > 1e-9 * scale for c in cross) or all(c < -1e-9 * scale for c in cross)


def translate(quad0: np.ndarray, dx: float, dy: float) -> np.ndarray:
    return np.asarray(quad0, dtype=np.float64) + np.array([dx, dy])


def scale(quad0: np.ndarray, theta: float, handle: int, pointer: Tuple[float, float],
          keep_ratio: bool = False, about_centre: bool = False) -> Optional[np.ndarray]:
    """Drag handle `handle` to `pointer`: an affine scale of all four corners
    in the box's own direction `theta`, about the opposite handle (Alt: the
    centre). Corners scale both ways (Shift: the same factor), edge
    midpoints one way (Shift: both, by that factor)."""
    q = np.asarray(quad0, dtype=np.float64)
    hp = handle_points(q)
    a = centre(q) if about_centre else hp[OPPOSITE[handle]]
    l0 = _rot(hp[handle] - a, -theta)
    l1 = _rot(np.asarray(pointer, dtype=np.float64) - a, -theta)
    sx = l1[0] / l0[0] if abs(l0[0]) > 1e-9 else 1.0
    sy = l1[1] / l0[1] if abs(l0[1]) > 1e-9 else 1.0
    if handle < 4:
        if keep_ratio:
            sx = sy = float(np.dot(l1, l0) / max(np.dot(l0, l0), 1e-12))
    elif handle in (4, 6):
        sx = sy if keep_ratio else 1.0
    else:
        sy = sx if keep_ratio else 1.0
    if abs(sx) < MIN_FACTOR or abs(sy) < MIN_FACTOR:
        return None
    local = _rot(q - a, -theta) * np.array([sx, sy])
    out = a + _rot(local, theta)
    return out if valid(out) else None


def rotate(quad0: np.ndarray, theta0: float, start: Tuple[float, float], pointer: Tuple[float, float],
           snap: bool = False) -> Tuple[np.ndarray, float]:
    """Turn about the centre by the pointer's angle change since `start`;
    Shift snaps the box direction to 15 degrees."""
    q = np.asarray(quad0, dtype=np.float64)
    c = centre(q)
    a0 = math.atan2(start[1] - c[1], start[0] - c[0])
    a1 = math.atan2(pointer[1] - c[1], pointer[0] - c[0])
    theta = theta0 + (a1 - a0)
    if snap:
        theta = round(theta / SNAP) * SNAP
    return c + _rot(q - c, theta - theta0), theta


def distort(quad0: np.ndarray, corner: int, pointer: Tuple[float, float]) -> Optional[np.ndarray]:
    """Ctrl + a corner: only that corner moves."""
    out = np.asarray(quad0, dtype=np.float64).copy()
    out[corner] = pointer
    return out if valid(out) else None


def apply(points: Sequence[Sequence[float]], src: np.ndarray, dst: np.ndarray) -> List[List[float]]:
    """The original vertices mapped from box `src` to quad `dst`."""
    import cv2
    m = cv2.getPerspectiveTransform(np.asarray(src, np.float32), np.asarray(dst, np.float32))
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 1, 2)
    return cv2.perspectiveTransform(pts, m.astype(np.float64)).reshape(-1, 2).tolist()


def hit(quad: np.ndarray, x: float, y: float, tol: float) -> Optional[Tuple[str, int]]:
    """What a press at (x, y) grabs: ("handle", i), ("rotate", corner) just
    outside a corner, ("move", -1) inside the box, or None."""
    import cv2
    hp = handle_points(quad)
    d = np.hypot(hp[:, 0] - x, hp[:, 1] - y)
    i = int(np.argmin(d))
    if d[i] <= tol:
        return ("handle", i)
    inside = cv2.pointPolygonTest(np.asarray(quad, np.float32).reshape(-1, 1, 2), (float(x), float(y)), False) >= 0
    if inside:
        return ("move", -1)
    k = int(np.argmin(d[:4]))
    if d[k] <= 3 * tol:
        return ("rotate", k)
    return None


class Session:
    """One pending transform (§10.5): never written into the document until
    Enter. `history` holds the (quad, theta) before each drag, so Ctrl+Z
    steps back one drag; an empty history with the box untouched is no
    change at all."""

    def __init__(self, sid: str, polygon: Sequence[Sequence[float]]):
        self.sid = sid
        self.orig = [[float(p[0]), float(p[1])] for p in polygon]
        self.src = box_quad(self.orig)
        self.quad = self.src.copy()
        self.theta = 0.0
        self.history: List[Tuple[np.ndarray, float]] = []
        self.drag = None
        self.grab = np.zeros(2)       # handle minus pointer at the press: a click on a handle moves nothing

    @property
    def changed(self) -> bool:
        return not np.allclose(self.quad, self.src, rtol=0.0, atol=1e-6)   # absolute: slides are large

    def preview(self) -> List[List[float]]:
        return apply(self.orig, self.src, self.quad) if self.changed else [list(p) for p in self.orig]

    def begin(self, mode: str, handle: int, pointer: Tuple[float, float]) -> None:
        self.history.append((self.quad.copy(), self.theta))
        self.drag = (mode, handle, tuple(pointer), self.quad.copy(), self.theta)
        self.grab = (handle_points(self.quad)[handle] - np.asarray(pointer, dtype=np.float64)
                     if mode in ("scale", "distort") else np.zeros(2))

    def move(self, pointer: Tuple[float, float], shift=False, alt=False) -> None:
        if self.drag is None:
            return
        mode, handle, start, q0, t0 = self.drag
        pointer = tuple(np.asarray(pointer, dtype=np.float64) + self.grab)
        if mode == "move":
            self.quad = translate(q0, pointer[0] - start[0], pointer[1] - start[1])
        elif mode == "rotate":
            self.quad, self.theta = rotate(q0, t0, start, pointer, snap=shift)
        elif mode == "distort":
            out = distort(q0, handle, pointer)
            if out is not None:
                self.quad = out
        else:
            out = scale(q0, t0, handle, pointer, keep_ratio=shift, about_centre=alt)
            if out is not None:
                self.quad = out

    def end(self) -> None:
        """The button came up: a drag that changed nothing leaves no step."""
        if self.drag is not None and self.history:
            q, t = self.history[-1]
            if np.allclose(q, self.quad, rtol=0.0, atol=1e-6) and abs(t - self.theta) < 1e-12:
                self.history.pop()
        self.drag = None

    def abort_drag(self) -> None:
        if self.drag is not None:
            self.quad, self.theta = self.history.pop()
            self.drag = None

    def step_back(self) -> bool:
        """Ctrl+Z after a drag: the box as before it. False when nothing is
        left to step back (the session then ends)."""
        if self.history:
            self.quad, self.theta = self.history.pop()
        return bool(self.history) or self.changed
