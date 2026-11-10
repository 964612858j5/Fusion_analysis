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


#: docs/v17_cellpose_cell_nucleus_plan.md v1.3 (user rulings 2026-11-10)
MAJORITY = 0.5           # an unshared nucleus joins its cell from 50 % on
MIN_PIECE = 0.1          # a piece of a shared nucleus is a nucleus from 10 % on
GROW_PX = 2              # a nucleus-born cell grows 1 px per step into background


def combine_cells_and_nuclei(cells, nuclei):
    """Cellpose whole-cell + nuclei: cells and nuclei made one, per window
    (plan §12-§13, user rulings 2026-11-10). In this order, every decision
    taken from the masks after step 1:

    1. a cell wholly inside one nucleus is removed;
    2. a nucleus on no cell -> a new cell (the nucleus, grown below);
    3. a nucleus on ONE cell: >= 50 % in it -> the cell becomes cell ∪
       nucleus; else -> a new cell (the nucleus's pixels, from wherever);
    4. a nucleus on TWO OR MORE cells is shared out: each cell keeps its
       piece as a nucleus of its own when the piece is >= 10 % of the nucleus
       (measured before 5.), a smaller piece stays an ordinary pixel of its
       cell; the nucleus's background part goes to the unique largest
       eligible piece when it is <= 50 % of the nucleus, else (or on a tie,
       or with no eligible piece) it stays background;
       new cells grow 1 px per step (3 x 3), 2 steps, into background only
       (a pixel two new cells reach in the same step stays background);
    5. a cell with no nucleus is dropped; nothing grows afterwards.

    Returns a dict: `cells` (uint32, 1..N), `nuclei` (uint32, one id per kept
    (nucleus, cell) piece, 1..K), `nucleus_cell` (uint32, K + 1; every piece
    lies wholly in its cell), `counts` (window totals) and `objects`
    ({reason: (label image, ids)}, for counting by centroid with
    `owned_drop_counts`)."""
    from scipy import ndimage as ndi
    cells = np.asarray(cells).astype(np.uint32, copy=False)
    nuclei = np.asarray(nuclei).astype(np.uint32, copy=False)
    if cells.shape != nuclei.shape:
        raise ValueError(f"cells {cells.shape} and nuclei {nuclei.shape} differ in shape")
    counts = {}
    objects = {}
    ctop = int(cells.max()) if cells.size else 0
    ntop = int(nuclei.max()) if nuclei.size else 0

    # 1. cells wholly inside one nucleus
    cm = cells > 0
    if cm.any():
        p = np.unique((cells[cm].astype(np.uint64) << np.uint64(32)) | nuclei[cm])
        pc, pn = p >> np.uint64(32), p & np.uint64(0xFFFFFFFF)
        ids, start, n_pairs = np.unique(pc, return_index=True, return_counts=True)
        inside = ids[(n_pairs == 1) & (pn[start] > 0)].astype(np.int64)
    else:
        inside = np.zeros(0, np.int64)
    del cm
    objects["cells_inside_a_nucleus"] = (cells, inside)
    alive = np.ones(ctop + 1, bool)
    alive[0] = False
    alive[inside] = False
    c1 = np.where(alive[cells], cells, 0).astype(np.uint32)

    # the pieces of every nucleus on that mask
    nm = nuclei > 0
    key, cnt = np.unique((nuclei[nm].astype(np.uint64) << np.uint64(32)) | c1[nm],
                         return_counts=True)
    kn = (key >> np.uint64(32)).astype(np.int64)
    kc = (key & np.uint64(0xFFFFFFFF)).astype(np.int64)
    del key
    area = np.bincount(kn, weights=cnt, minlength=ntop + 1)
    bg = np.bincount(kn[kc == 0], weights=cnt[kc == 0], minlength=ntop + 1)
    fg = kc > 0
    ncell = np.bincount(kn[fg], minlength=ntop + 1)
    order = np.lexsort((-cnt[fg], kn[fg]))          # per nucleus, largest piece first
    fn, fc, fcnt = kn[fg][order], kc[fg][order], cnt[fg][order]
    first = np.unique(fn, return_index=True)[1]
    best_cell = np.zeros(ntop + 1, np.int64)
    best_px = np.zeros(ntop + 1, np.int64)
    second_px = np.zeros(ntop + 1, np.int64)
    best_cell[fn[first]] = fc[first]
    best_px[fn[first]] = fcnt[first]
    nxt = first + 1
    has2 = (nxt < fn.size)
    has2[has2] = fn[nxt[has2]] == fn[first[has2]]
    second_px[fn[first[has2]]] = fcnt[nxt[has2]]
    present = area > 0
    union = present & (ncell == 1) & (best_px >= MAJORITY * area)
    born = present & ((ncell == 0) | ((ncell == 1) & ~union))
    shared = present & (ncell >= 2)
    # eligible pieces of shared nuclei (measured before the background moves)
    elig = fg.copy()
    elig[fg] = shared[kn[fg]] & (cnt[fg] >= MIN_PIECE * area[kn[fg]])
    n_elig = np.bincount(kn[elig], minlength=ntop + 1)
    bg_to = np.zeros(ntop + 1, np.int64)
    give = (shared & (bg > 0) & (bg <= MAJORITY * area) & (best_px > second_px)
            & (best_px >= MIN_PIECE * area))
    bg_to[give] = best_cell[give]
    counts.update(
        nuclei_joined=int(union.sum()), cells_from_nucleus=int(born.sum()),
        nuclei_shared=int(shared.sum()),
        nuclei_shared_without_piece=int((shared & (n_elig == 0)).sum()),
        slivers=int((fg & ~elig & shared[kn]).sum()),
        background_discarded=int((shared & (bg > 0) & ~give).sum()),
        cells_inside_a_nucleus=int(inside.size))
    objects["nuclei_shared"] = (nuclei, np.nonzero(shared)[0])
    objects["nuclei_shared_without_piece"] = (nuclei, np.nonzero(shared & (n_elig == 0))[0])
    # (codex review: persisted too) a shared nucleus with at least one piece
    # under 10 % -- counted by the nucleus, not per sliver -- and one whose
    # background part was discarded
    sliver = fg & ~elig
    sliver[fg] &= shared[kn[fg]]
    objects["nuclei_with_slivers"] = (nuclei, np.unique(kn[sliver]))
    objects["nuclei_background_discarded"] = (nuclei, np.nonzero(shared & (bg > 0) & ~give)[0])

    # pixels: union / nucleus-born whole nuclei, the background of shared ones
    whole = np.zeros(ntop + 1, np.int64)
    whole[union] = best_cell[union]
    born_ids = np.nonzero(born)[0]
    if ctop + born_ids.size >= 2 ** 32:
        raise RuntimeError("more than 2^32 - 1 cells in one window")
    whole[born_ids] = ctop + 1 + np.arange(born_ids.size)
    t = whole[nuclei]
    out = np.where(t > 0, t, c1).astype(np.uint32)
    b = bg_to[nuclei]
    move = (t == 0) & (c1 == 0) & (b > 0)
    out[move] = b[move]
    del t, b, move, c1
    # nucleus-born cells grow into background, 1 px per step
    if born_ids.size:
        is_new = np.zeros(ctop + born_ids.size + 1, bool)
        is_new[ctop + 1:] = True
        big = np.uint32(0xFFFFFFFF)
        for _ in range(GROW_PX):
            lab = np.where(is_new[out], out, 0).astype(np.uint32)
            hi = ndi.maximum_filter(lab, size=3)
            lo = ndi.minimum_filter(np.where(lab > 0, lab, big), size=3)
            claim = (out == 0) & (hi > 0) & (lo == hi)
            out[claim] = hi[claim]
            del lab, hi, lo, claim

    # the kept pieces: (nucleus, its cell now) -- a union / nucleus-born
    # nucleus whole; a shared one by its eligible pieces (+ the background
    # it was given, now inside the same cell)
    key2, inv = np.unique((nuclei[nm].astype(np.uint64) << np.uint64(32)) | out[nm],
                          return_inverse=True)
    n2 = (key2 >> np.uint64(32)).astype(np.int64)
    c2 = (key2 & np.uint64(0xFFFFFFFF)).astype(np.int64)
    ok = np.zeros(key2.size, bool)
    target = whole[n2]
    ok[target > 0] = c2[target > 0] == target[target > 0]
    sh = shared[n2] & (c2 > 0)
    if sh.any():
        ekeys = (kn[elig].astype(np.uint64) << np.uint64(32)) | kc[elig].astype(np.uint64)
        ok[sh] = np.isin(key2[sh], ekeys)
    piece_id = np.zeros(key2.size, np.uint32)
    piece_id[ok] = np.arange(1, int(ok.sum()) + 1, dtype=np.uint32)
    parent = c2[ok]

    # 5. cells without a nucleus
    have = np.zeros(int(out.max()) + 1 if out.size else 1, bool)
    have[parent] = True
    lost = np.unique(out[(out > 0) & ~have[out]]).astype(np.int64)
    objects["cells_without_nucleus"] = (out.copy(), lost)
    counts["cells_without_nucleus"] = int(lost.size)
    out[~have[out]] = 0
    # relabel cells 1..N, pieces keep 1..K with their parent
    cid = np.nonzero(have)[0]
    clut = np.zeros(have.size, np.uint32)
    clut[cid] = np.arange(1, cid.size + 1, dtype=np.uint32)
    cells_out = clut[out]
    nuclei_out = np.zeros(nuclei.shape, np.uint32)
    nuclei_out[nm] = piece_id[inv.reshape(-1)]
    nucleus_cell = np.zeros(int(ok.sum()) + 1, np.uint32)
    nucleus_cell[1:] = clut[parent]
    born_kept = clut[ctop + 1:ctop + 1 + born_ids.size] if born_ids.size else np.zeros(0, np.uint32)
    objects["cells_from_nucleus"] = (cells_out, born_kept[born_kept > 0].astype(np.int64))
    counts.update(cells=int(cid.size), nucleus_pieces=int(ok.sum()))
    return {"cells": cells_out, "nuclei": nuclei_out, "nucleus_cell": nucleus_cell,
            "counts": counts, "objects": objects}


def owned_object_counts(objects, own_local):
    """{reason: how many of `objects`' ids this window owns} -- by each
    object's own centroid, as `owned_drop_counts` does for dropped nuclei."""
    out = {}
    for reason, (labels, ids) in (objects or {}).items():
        out[reason] = owned_drop_counts(labels, {reason: ids}, own_local).get(reason, 0)
    return out
