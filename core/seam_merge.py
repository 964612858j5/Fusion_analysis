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


# ── block N3b: the region-level merger Step2 drives ──────────────────────

SEAM_MERGE = {"version": 1, "duplicate_overlap_threshold": 0.5, "minimum_writable_fraction": 0.5}


class SeamContractError(RuntimeError):
    """The merged labels break the LabelStore contract (a program defect)."""


class SeamMerger:
    """One region's merge. `add_tile` writes the tile's interior cells (they
    cannot meet another tile) at once and spills its seam candidates -- a
    bool crop and, with nuclei, the crop of their local nucleus ids -- to
    `spill_dir`; `finish` decides every seam candidate together (user ruling
    2026-09-27: at the end of the region, so the result does not depend on
    the tile order) and writes the kept ones deepest-margin first.

    Ids: cells 1..N and nuclei 1..M in write order, every id with pixels. A
    nucleus is written only when all its pixels lie in its cell's written
    pixels; otherwise it is dropped (`nuclei_dropped_seam_conflict`)."""

    def __init__(self, region_shape, tiles, spill_dir, with_nuclei=False,
                 tau=SEAM_MERGE["duplicate_overlap_threshold"],
                 min_free=SEAM_MERGE["minimum_writable_fraction"],
                 table_sink=None, table_chunk=1 << 20):
        import os
        self.shape = tuple(region_shape)
        self.tiles = list(tiles)
        self.spill_dir = spill_dir
        os.makedirs(spill_dir, exist_ok=True)
        self.with_nuclei = with_nuclei
        self.tau, self.min_free = float(tau), float(min_free)
        self.n_cells = 0
        self.n_nuclei = 0
        # nucleus -> cell, in id order: a numpy buffer handed to `table_sink`
        # (the zarr table's append) every `table_chunk` entries (block N2's
        # contract: appended block by block, no Python list of ids)
        self.table_sink = table_sink
        self._buf = np.zeros(int(table_chunk), np.uint32)
        self._nbuf = 0
        self.stats = {"candidates": 0, "interior_cells": 0, "touching_internal_edge": 0,
                      "duplicate_groups": 0, "duplicate_versions_removed": 0,
                      "split_merge_groups": 0, "recovered_missing_cells": 0,
                      "cells_with_clipped_pixels": 0, "clipped_pixels": 0,
                      "dropped_insufficient_free_pixels": 0,
                      "nuclei_dropped_seam_conflict": 0}

    # -- writing one kept candidate
    def _write(self, cand, nuc_crop, cells, nuclei):
        y0, y1, x0, x1 = cand.bbox
        view = cells[y0:y1, x0:x1]
        free = cand.mask & (view == 0)
        n_free = int(np.count_nonzero(free))
        if n_free == 0 or n_free < self.min_free * cand.area:
            self.stats["dropped_insufficient_free_pixels"] += 1
            return 0
        if n_free < cand.area:
            self.stats["cells_with_clipped_pixels"] += 1
            self.stats["clipped_pixels"] += int(cand.area - n_free)
        self.n_cells += 1
        cid = self.n_cells
        view[free] = cid
        if nuc_crop is not None and nuclei is not None:
            nview = nuclei[y0:y1, x0:x1]
            for n in np.unique(nuc_crop[nuc_crop > 0]):
                nm = nuc_crop == n
                if not free[nm].all():
                    self.stats["nuclei_dropped_seam_conflict"] += 1
                    continue
                self.n_nuclei += 1
                nview[nm] = self.n_nuclei
                self._buf[self._nbuf] = cid
                self._nbuf += 1
                if self._nbuf == self._buf.size:
                    self.flush_table()
        return cid

    def add_tile(self, tile, local, cells, nuclei=None, local_nuclei=None, local_nuclei_cell=None):
        """Returns the number of interior cells written now."""
        import os
        cands, touching = extract(tile, local, self.shape)
        self.stats["touching_internal_edge"] += touching
        ry0, _ry1, rx0, _rx1 = tile.read
        seam, crops, written = [], [], 0
        cell_of = None
        if self.with_nuclei and local_nuclei is not None and local_nuclei_cell is not None:
            cell_of = np.asarray(local_nuclei_cell, np.int64)
        for c in cands:
            nuc_crop = None
            if cell_of is not None:
                y0, y1, x0, x1 = c.bbox
                ln = np.asarray(local_nuclei[y0 - ry0:y1 - ry0, x0 - rx0:x1 - rx0], np.int64)
                ok = (ln > 0) & (ln < cell_of.size)
                mine = np.zeros(ln.shape, bool)
                mine[ok] = cell_of[ln[ok]] == c.label
                nuc_crop = np.where(mine & c.mask, ln, 0).astype(np.uint32)
            if is_seam(c, self.tiles):
                seam.append(c)
                crops.append(nuc_crop)
            elif c.owned:
                if self._write(c, nuc_crop, cells, nuclei):
                    written += 1
        self.stats["interior_cells"] += written
        self.stats["candidates"] += len(seam)
        if seam:
            meta = np.array([(c.tile, c.label, *c.bbox, c.area, c.cy, c.cx, c.margin, c.owned)
                             for c in seam], dtype=np.float64)
            masks = np.concatenate([c.mask.ravel() for c in seam])
            nucs = (np.concatenate([n.ravel() for n in crops]) if cell_of is not None
                    else np.zeros(0, np.uint32))
            np.savez(os.path.join(self.spill_dir, f"tile_{tile.index:05d}.npz"),
                     meta=meta, masks=masks, nucs=nucs)
        return written

    def _load(self):
        import glob
        import os
        cands, crops = [], []
        for path in sorted(glob.glob(os.path.join(self.spill_dir, "tile_*.npz"))):
            z = np.load(path)
            meta, masks, nucs = z["meta"], z["masks"], z["nucs"]
            pos = 0
            for row in meta:
                t, lab, y0, y1, x0, x1, area, cy, cx, mg, owned = row
                y0, y1, x0, x1 = int(y0), int(y1), int(x0), int(x1)
                n = (y1 - y0) * (x1 - x0)
                cands.append(Candidate(tile=int(t), label=int(lab), bbox=(y0, y1, x0, x1),
                                       mask=masks[pos:pos + n].reshape(y1 - y0, x1 - x0),
                                       area=int(area), cy=float(cy), cx=float(cx),
                                       margin=float(mg), owned=bool(owned)))
                crops.append(nucs[pos:pos + n].reshape(y1 - y0, x1 - x0) if nucs.size else None)
                pos += n
        return cands, crops

    def finish(self, cells, nuclei=None):
        """Decide and write the seam candidates; the region's counts."""
        import shutil
        cands, crops = self._load()
        d = resolve(cands, self.tiles, self.tau)
        for key in ("duplicate_groups", "duplicate_versions_removed", "split_merge_groups",
                    "recovered_missing_cells"):
            self.stats[key] = int(d.stats[key])
        for k in d.kept:
            self._write(cands[k], crops[k], cells, nuclei)
        self.flush_table()
        shutil.rmtree(self.spill_dir, ignore_errors=True)
        return self.record()

    def flush_table(self):
        if self._nbuf and self.table_sink is not None:
            self.table_sink(self._buf[:self._nbuf].copy())
        self._nbuf = 0

    def record(self):
        return {"seam_merge": dict(SEAM_MERGE,
                                   duplicate_overlap_threshold=self.tau,
                                   minimum_writable_fraction=self.min_free),
                "seam_reconciliation": dict(self.stats)}


def validate(cells, nuclei=None, parents=None, n_cells=None, block=1024):
    """Stream the merged region by row blocks: cell ids 1..N each with pixels;
    with nuclei, ids 1..M each with pixels and every nucleus pixel in the
    cell `parents[id - 1]`. Raises SeamContractError."""
    H = cells.shape[0]
    N = int(n_cells) if n_cells is not None else 0
    ccount = np.zeros(N + 1, np.int64)
    M = 0 if parents is None else int(parents.size)
    ncount = np.zeros(M + 1, np.int64)
    table = None if parents is None else np.concatenate([[0], parents]).astype(np.uint32)
    for y in range(0, H, block):
        c = np.asarray(cells[y:y + block])
        top = int(c.max()) if c.size else 0
        if top > N:
            raise SeamContractError(f"cell id {top} above the {N} written")
        ccount += np.bincount(c.ravel(), minlength=N + 1)
        if nuclei is not None and table is not None:
            n = np.asarray(nuclei[y:y + block])
            ntop = int(n.max()) if n.size else 0
            if ntop > M:
                raise SeamContractError(f"nucleus id {ntop} above the {M} written")
            ncount += np.bincount(n.ravel(), minlength=M + 1)
            m = n > 0
            if (table[n[m]] != c[m]).any():
                raise SeamContractError("a nucleus pixel lies outside its cell")
    empty = np.nonzero(ccount[1:] == 0)[0]
    if empty.size:
        raise SeamContractError(f"{empty.size} cell ids without pixels (first {int(empty[0]) + 1})")
    if M:
        nempty = np.nonzero(ncount[1:] == 0)[0]
        if nempty.size:
            raise SeamContractError(f"{nempty.size} nucleus ids without pixels")
