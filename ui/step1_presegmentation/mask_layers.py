"""Outlines of a result's masks, and how each combination draws them (block D).

A result mask is uint32, the size of its patch, at level 0 (plan 7.3). Its
outlines are extracted once, off the GUI thread (in the montage supply's two
authorised workers), and kept as polygons in the patch's own pixels; the
page turns them into one `QPainterPath` per (result, kind) placed on the
canvas, drawn with a cosmetic pen -- the width is in screen pixels whatever
the zoom (plan 4.6).
"""

import numpy as np

#: One colour per combination, in the plan's order (user ruling 2026-09-25:
#: a fixed, high-contrast palette; the colour can be changed per row).
PALETTE = ["#ffd166", "#4cc9f0", "#f15bb5", "#80ed99", "#ff9f1c", "#ff595e", "#9b8cff",
           "#ffffff"]
WIDTHS = (1.0, 1.5, 2.0, 3.0)
#: Outlines are left out when a typical cell is smaller than this on screen
#: (user ruling 2026-09-25): the frames stay, the outlines come back zoomed in.
MIN_CELL_SCREEN_PX = 3.0


def default_style(index):
    """Off, the palette's colour, 1 px, cells solid and nuclei dashed."""
    return {"cells": False, "nuclei": False, "color": PALETTE[index % len(PALETTE)],
            "width": 1.0, "cell_dashed": False, "nucleus_dashed": True}


def outlines(mask):
    """The outline of every label of `mask`: (polygons, count, median
    diameter). Polygons are (N, 2) float arrays of (x, y) through the
    centres of the boundary pixels, in the mask's own pixels, not closed
    (the drawer closes them)."""
    import cv2
    from scipy import ndimage
    mask = np.asarray(mask)
    polys, areas = [], []
    if mask.size == 0 or not mask.any():
        return polys, 0, 0.0
    for label, sl in enumerate(ndimage.find_objects(mask), start=1):
        if sl is None:
            continue
        crop = (mask[sl] == label).astype(np.uint8)
        areas.append(int(crop.sum()))
        found, _ = cv2.findContours(crop, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        oy, ox = sl[0].start, sl[1].start
        for c in found:
            pts = c.reshape(-1, 2).astype(np.float32)
            pts[:, 0] += ox + 0.5
            pts[:, 1] += oy + 0.5
            polys.append(pts)
    median_d = float(2.0 * np.sqrt(np.median(areas) / np.pi)) if areas else 0.0
    return polys, len(areas), median_d


def path_arrays(polys, ox, oy):
    """(x, y, connect) for `pyqtgraph.arrayToQPath`: every polygon closed,
    moved to canvas (ox, oy), one subpath each."""
    if not polys:
        return np.zeros(0), np.zeros(0), np.zeros(0, bool)
    xs, ys, conn = [], [], []
    for p in polys:
        closed = np.vstack([p, p[:1]])
        xs.append(closed[:, 0] + ox)
        ys.append(closed[:, 1] + oy)
        c = np.ones(len(closed), bool)
        c[-1] = False                              # the next polygon starts fresh
        conn.append(c)
    return np.concatenate(xs), np.concatenate(ys), np.concatenate(conn)


def lod_bucket(scale):
    """The simplification step for a zoom of `scale` screen px per canvas px:
    None (full detail) at 1:1 or closer, else k with 2**k <= 1 / scale, so a
    tolerance of 0.5 * 2**k canvas px is at most half a screen pixel."""
    import math
    if scale >= 1.0 or scale <= 0:
        return None
    return int(math.floor(math.log2(1.0 / scale)))


def simplify(polys, bucket):
    """`polys` within 0.5 * 2**bucket px (Douglas-Peucker); None: as they are."""
    if bucket is None:
        return polys
    import cv2
    eps = 0.5 * (2 ** bucket)
    out = []
    for p in polys:
        q = cv2.approxPolyDP(p.reshape(-1, 1, 2), eps, True).reshape(-1, 2)
        out.append(q.astype(np.float32) if len(q) >= 3 else p)
    return out


def qpath(polys, ox, oy):
    import pyqtgraph as pg
    x, y, connect = path_arrays(polys, ox, oy)
    if not len(x):
        from PyQt5 import QtGui
        return QtGui.QPainterPath()
    return pg.arrayToQPath(x, y, connect=connect.astype(np.int32))
