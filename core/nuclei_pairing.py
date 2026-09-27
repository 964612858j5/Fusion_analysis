"""Nuclei and the cells they belong to -- block N2 (user rulings 2026-09-27).

No Qt. One tile at a time, BEFORE ownership and relabelling: a nucleus is
kept when every one of its pixels lies in ONE cell (the cell label under it
is a single non-zero value); otherwise it is dropped, counted by why:

  * `multiple_cells`     -- it touches two or more cells;
  * `partial_background` -- one cell and background;
  * `outside_cells`      -- background only.

Several nuclei in one cell are all kept (a normal biological case). The
kept nuclei are renumbered 1..K in the order of their original ids, and
`nucleus_cell[k]` is the (local) cell of kept nucleus k -- so a nucleus
maps to exactly one cell while a cell may own 0, 1 or many nuclei.

Only the nucleus pixels are visited: (nucleus, cell) pairs are packed into
one uint64 and made unique, which costs about 30 ms on a 4096 x 4096 tile
with ~12 000 nuclei (measured 2026-09-27).
"""

import numpy as np

COUNT_KEYS = ("predicted", "kept", "multiple_cells", "partial_background", "outside_cells")


def empty_counts():
    return {key: 0 for key in COUNT_KEYS}


def add_counts(total, more):
    for key in COUNT_KEYS:
        total[key] = int(total.get(key, 0)) + int(more.get(key, 0))
    return total


def pair_nuclei(cells, nuclei):
    """`(kept, nucleus_cell, counts, dropped_ids)` for one tile.

    `cells`, `nuclei`: 2-D label arrays of the same shape (0 = background,
    ids are the tile's own). `kept` is uint32 of that shape with the kept
    nuclei numbered 1..K; `nucleus_cell` is uint32 of length K + 1 with
    `nucleus_cell[0] == 0`; `counts` holds `COUNT_KEYS` (over the whole
    window); `dropped_ids` maps each drop reason to the original ids dropped
    for it (for `owned_drop_counts`)."""
    cells = np.asarray(cells)
    nuclei = np.asarray(nuclei)
    if cells.shape != nuclei.shape:
        raise ValueError(f"cells {cells.shape} and nuclei {nuclei.shape} differ in shape")
    counts = empty_counts()
    where = nuclei > 0
    if not where.any():
        return (np.zeros(nuclei.shape, np.uint32), np.zeros(1, np.uint32), counts,
                {"multiple_cells": np.zeros(0, np.uint64), "partial_background": np.zeros(0, np.uint64),
                 "outside_cells": np.zeros(0, np.uint64)})
    nv = nuclei[where].astype(np.uint64)
    cv = cells[where].astype(np.uint64)
    pairs = np.unique((nv << np.uint64(32)) | cv)           # sorted: by nucleus, then cell
    pair_nucleus = pairs >> np.uint64(32)
    pair_cell = pairs & np.uint64(0xFFFFFFFF)
    ids, start, n_pairs = np.unique(pair_nucleus, return_index=True, return_counts=True)
    has_background = pair_cell[start] == 0                 # cell 0 sorts first
    n_cells = n_pairs - has_background.astype(np.int64)
    keep = (n_cells == 1) & ~has_background
    counts["predicted"] = int(ids.size)
    counts["kept"] = int(keep.sum())
    counts["multiple_cells"] = int((n_cells >= 2).sum())
    counts["partial_background"] = int(((n_cells == 1) & has_background).sum())
    counts["outside_cells"] = int((n_cells == 0).sum())

    dropped = {"multiple_cells": ids[n_cells >= 2],
               "partial_background": ids[(n_cells == 1) & has_background],
               "outside_cells": ids[n_cells == 0]}
    kept_ids = ids[keep]
    lut = np.zeros(int(ids[-1]) + 1, np.uint32)
    lut[kept_ids.astype(np.int64)] = np.arange(1, kept_ids.size + 1, dtype=np.uint32)
    kept = lut[nuclei]
    nucleus_cell = np.zeros(kept_ids.size + 1, np.uint32)
    nucleus_cell[1:] = pair_cell[start[keep]].astype(np.uint32)
    return kept, nucleus_cell, counts, dropped


def owned_drop_counts(nuclei, dropped_ids, own_local):
    """How many of a tile's DROPPED nuclei this tile owns -- by the nucleus's
    own centroid in `own_local` = (oy0, oy1, ox0, ox1), the rule cells are
    owned by -- so a nucleus dropped in two overlapping windows is counted
    once. (A KEPT nucleus is counted once through its cell instead.)"""
    from .label_ownership import centroids
    out = {"multiple_cells": 0, "partial_background": 0, "outside_cells": 0}
    ids_all = [np.asarray(v, np.int64) for v in (dropped_ids or {}).values()]
    if not any(v.size for v in ids_all):
        return out
    cy, cx = centroids(np.asarray(nuclei))
    oy0, oy1, ox0, ox1 = own_local
    for reason, ids in (dropped_ids or {}).items():
        ids = np.asarray(ids, np.int64)
        if ids.size:
            y, x = cy[ids - 1], cx[ids - 1]
            out[reason] = int(((y >= oy0) & (y < oy1) & (x >= ox0) & (x < ox1)).sum())
    return out
