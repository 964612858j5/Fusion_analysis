"""
block01/core/seam_merge.py — reconciling Step2's tile seams (block N3).

Qt-free, pure functions. Step2 reads each tile with an overlap band and, today,
keeps the cells whose centroid lies in the tile's own region, writing them --
overlap pixels included -- over whatever an earlier tile wrote. Two tiles that
segment the same overlap band differently then keep two versions of one cell
(the later one overwrites the earlier), or neither (the cell is lost).

Here every cell a tile predicted that does not touch an INTERNAL read-window
edge (an edge with a neighbour; the region's own border is fine) is a
candidate. Candidates of different tiles whose overlap / smaller area reaches
`tau` are joined by an edge; each connected component is decided as a whole,
by TILE VERSION: one tile wins the component and keeps all of its candidates
there (so a tile that saw one cell M and a tile that saw two cells P, Q give
either M or both P and Q -- never P alone). The winner is the tile whose
version lies deepest inside its own region (the ownership margin of the
version's pixel centroid); the tile index only breaks ties. A candidate alone
in its component is kept -- owned as today, or recovered when its centroid
lies in a neighbour's region and the neighbour has no version of it.

Nothing here depends on the order tiles or candidates arrive in.

Block N3a uses this to PROBE real data (tau is a probe start, not a product
parameter); block N3b wires the locked rule into Step2.
"""

from __future__ import annotations

import dataclasses
from typing import Dict, List, Sequence, Tuple

import numpy as np
from scipy import ndimage as ndi


@dataclasses.dataclass
class Tile:
    index: int
    read: Tuple[int, int, int, int]      # y0, y1, x0, x1 in region pixels
    own: Tuple[int, int, int, int]


@dataclasses.dataclass
class Candidate:
    tile: int
    label: int                           # the tile's local label
    bbox: Tuple[int, int, int, int]      # y0, y1, x0, x1 in region pixels
    mask: np.ndarray                     # bool, the bbox crop
    area: int
    cy: float                            # region pixels
    cx: float
    margin: float                        # signed distance to the own region's edge
    owned: bool                          # centroid in the own region (today's rule)

    @property
    def key(self):
        return (self.tile, self.label)


def margin(cy, cx, own):
    """Signed distance of (cy, cx) to the own region's edge: > 0 inside
    (half-open [y0, y1) x [x0, x1)), < 0 outside."""
    y0, y1, x0, x1 = own
    inside = min(cy - y0, (y1 - 1) - cy, cx - x0, (x1 - 1) - cx)
    if y0 <= cy < y1 and x0 <= cx < x1:
        return float(inside)
    dy = max(y0 - cy, cy - (y1 - 1), 0.0)
    dx = max(x0 - cx, cx - (x1 - 1), 0.0)
    return -float(np.hypot(dy, dx))


def internal_edges(tile, region_shape):
    """Which read-window edges have a neighbour: (top, bottom, left, right)."""
    H, W = region_shape
    y0, y1, x0, x1 = tile.read
    return (y0 > 0, y1 < H, x0 > 0, x1 < W)


def extract(tile, local, region_shape):
    """(candidates, n_touching_internal_edge) of one tile's local labels."""
    local = np.asarray(local)
    top, bottom, left, right = internal_edges(tile, region_shape)
    ry0, _ry1, rx0, _rx1 = tile.read
    h, w = local.shape
    out, touching = [], 0
    for idx, sl in enumerate(ndi.find_objects(local)):
        if sl is None:
            continue
        lab = idx + 1
        ys, xs = sl
        if ((top and ys.start == 0) or (bottom and ys.stop == h)
                or (left and xs.start == 0) or (right and xs.stop == w)):
            touching += 1
            continue
        m = local[sl] == lab
        yy, xx = np.nonzero(m)
        area = int(yy.size)
        cy = float(yy.mean()) + ys.start + ry0
        cx = float(xx.mean()) + xs.start + rx0
        oy0, oy1, ox0, ox1 = tile.own
        out.append(Candidate(tile=tile.index, label=lab,
                             bbox=(ys.start + ry0, ys.stop + ry0, xs.start + rx0, xs.stop + rx0),
                             mask=m, area=area, cy=cy, cx=cx, margin=margin(cy, cx, tile.own),
                             owned=bool(oy0 <= cy < oy1 and ox0 <= cx < ox1)))
    return out, touching


def windows_meet(a, b):
    return a[0] < b[1] and b[0] < a[1] and a[2] < b[3] and b[2] < a[3]


def is_seam(c, tiles):
    """Does the candidate lie in another tile's read window (so another
    tile may have a version of it)?"""
    return any(t.index != c.tile and windows_meet(c.bbox, t.read) for t in tiles)


def overlap(a, b):
    """Pixels shared by two candidates."""
    y0, y1 = max(a.bbox[0], b.bbox[0]), min(a.bbox[1], b.bbox[1])
    x0, x1 = max(a.bbox[2], b.bbox[2]), min(a.bbox[3], b.bbox[3])
    if y0 >= y1 or x0 >= x1:
        return 0
    ma = a.mask[y0 - a.bbox[0]:y1 - a.bbox[0], x0 - a.bbox[2]:x1 - a.bbox[2]]
    mb = b.mask[y0 - b.bbox[0]:y1 - b.bbox[0], x0 - b.bbox[2]:x1 - b.bbox[2]]
    return int(np.count_nonzero(ma & mb))


def pairs(cands):
    """Every pair of candidates of DIFFERENT tiles sharing a pixel:
    [(i, j, inter, inter / min area, IoU, centroid distance)], i < j,
    sorted -- independent of the input order up to the indices."""
    order = sorted(range(len(cands)), key=lambda k: cands[k].bbox[0])
    out = []
    for n, i in enumerate(order):
        a = cands[i]
        for j in order[n + 1:]:
            b = cands[j]
            if b.bbox[0] >= a.bbox[1]:
                break
            if a.tile == b.tile or not windows_meet(a.bbox, b.bbox):
                continue
            inter = overlap(a, b)
            if inter == 0:
                continue
            union = a.area + b.area - inter
            lo, hi = (i, j) if i < j else (j, i)
            out.append((lo, hi, inter, inter / min(a.area, b.area), inter / union,
                        float(np.hypot(a.cy - b.cy, a.cx - b.cx))))
    out.sort()
    return out


def components(n, edges):
    """Connected components of nodes 0..n-1 (union-find); each a sorted list,
    the list sorted by its first node."""
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for i, j in edges:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)
    groups: Dict[int, List[int]] = {}
    for k in range(n):
        groups.setdefault(find(k), []).append(k)
    return sorted((sorted(g) for g in groups.values()), key=lambda g: g[0])


def version_margin(cands, members, own):
    """The ownership margin of a tile version's pixel centroid."""
    area = sum(cands[k].area for k in members)
    cy = sum(cands[k].cy * cands[k].area for k in members) / area
    cx = sum(cands[k].cx * cands[k].area for k in members) / area
    return margin(cy, cx, own)


@dataclasses.dataclass
class Decision:
    kept: List[int]                      # candidate indices, in write order
    stats: Dict[str, int]
    component_of: Dict[int, int]


def resolve(cands, tiles, tau):
    """Decide every candidate. Returns the kept candidates in write order
    (deepest margin first; ties by tile, label) and the counts."""
    own = {t.index: t.own for t in tiles}
    edges = [(i, j) for i, j, _inter, frac, _iou, _d in pairs(cands) if frac >= tau]
    comps = components(len(cands), edges)
    kept = []
    stats = {"candidates": len(cands), "components": len(comps), "duplicate_groups": 0,
             "duplicate_versions_removed": 0, "split_merge_groups": 0,
             "recovered_missing_cells": 0, "today_duplicate_groups": 0,
             "today_missing_groups": 0}
    comp_of = {}
    for ci, comp in enumerate(comps):
        for k in comp:
            comp_of[k] = ci
        by_tile: Dict[int, List[int]] = {}
        for k in comp:
            by_tile.setdefault(cands[k].tile, []).append(k)
        owned_today = sum(1 for k in comp if cands[k].owned)
        if len(by_tile) == 1:
            k = comp[0]
            kept.append(k)
            if not cands[k].owned:
                stats["recovered_missing_cells"] += 1
            continue
        stats["duplicate_groups"] += 1
        if any(len(v) > 1 for v in by_tile.values()):
            stats["split_merge_groups"] += 1
        if owned_today == 0:
            stats["today_missing_groups"] += 1
        elif len({cands[k].tile for k in comp if cands[k].owned}) > 1:
            stats["today_duplicate_groups"] += 1
        winner = max(by_tile, key=lambda t: (version_margin(cands, by_tile[t], own[t]), -t))
        kept += by_tile[winner]
        stats["duplicate_versions_removed"] += sum(len(v) for t, v in by_tile.items()
                                                   if t != winner)
    kept.sort(key=lambda k: (-cands[k].margin, cands[k].tile, cands[k].label))
    return Decision(kept=kept, stats=stats, component_of=comp_of)


def paint(cands, kept, region_shape, offset=0, min_free_fraction=0.0):
    """Write the kept candidates in order onto empty pixels only. Returns
    (labels uint32, clipped pixels per kept candidate). Ids offset + 1 ... in
    write order, every id with at least one pixel. A candidate whose free
    pixels are fewer than `min_free_fraction` of its area (or none) is
    dropped whole -- no sliver -- and reported with clipped == area."""
    H, W = region_shape
    out = np.zeros((H, W), np.uint32)
    clipped = {}
    nid = offset
    for k in kept:
        c = cands[k]
        y0, y1, x0, x1 = c.bbox
        view = out[y0:y1, x0:x1]
        free = c.mask & (view == 0)
        n_free = int(np.count_nonzero(free))
        if n_free == 0 or n_free < min_free_fraction * c.area:
            clipped[k] = int(c.area)
            continue
        clipped[k] = int(c.area - n_free)
        nid += 1
        view[free] = nid
    return out, clipped
