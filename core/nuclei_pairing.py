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


#: docs/v17_cellpose_cell_nucleus_plan.md v1.3 / v1.4 (user rulings 2026-11-10)
MAJORITY = 0.5           # an unshared nucleus joins its cell from 50 % on
MIN_PIECE = 0.1          # a piece of a shared nucleus is a nucleus from 10 % on
GROW_PX = 2              # a nucleus-born cell grows 1 px per step into background
SEED_RATIO = 0.5         # v1.4: a nucleus splits its cell from 0.5 x the cell's largest
SEED_MIN_PX = 40         # v1.4: ... and from 40 px
#: The version of these rules a Step1 result records and Step2 checks
#: (plan §15.5): 2 = v1.4 (move, split).
PAIRING_RULES = 2


def combine_cells_and_nuclei(cells, nuclei, split=True):
    """Cellpose whole-cell + nuclei: cells and nuclei made one, per window
    (plan §12-§13, §15; user rulings 2026-11-10). In this order:

    1. a cell wholly inside one nucleus is removed;
    1b. (v1.4) a nucleus on two or more cells whose largest piece is in ONE
       cell B: its piece (>= 10 % of it) in another cell A that has a
       nucleus of its own (a nucleus with its unique largest piece, >= 50 %,
       in A) moves to B -- A's border follows the nucleus -- unless A minus
       all the pieces it would lose has more 8-connected parts than A had;
       then those pieces stay in A as plain cell pixels (trimmed);
    2. a nucleus on no cell -> a new cell (the nucleus, grown below);
    3. a nucleus on ONE cell: >= 50 % in it -> the cell becomes cell ∪
       nucleus; else -> a new cell (the nucleus's pixels, from wherever);
    4. a nucleus on TWO OR MORE cells is shared out: each cell keeps its
       piece as a nucleus of its own when the piece is >= 10 % of the nucleus
       (measured before 5.) and was not trimmed in 1b, a smaller piece stays
       an ordinary pixel of its cell; the nucleus's background part goes to
       the unique largest eligible piece when it is <= 50 % of the nucleus,
       else (or on a tie, or with no eligible piece) it stays background;
       new cells grow 1 px per step (3 x 3), 2 steps, into background only
       (a pixel two new cells reach in the same step stays background);
    4b. (v1.4, when `split`) a cell holding two or more seeds -- nucleus
       pieces >= 0.5 x its largest piece and >= 40 px -- is split among them
       (`split_by_seeds`);
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

    # 1b. straddling pieces move to the nucleus's cell (v1.4)
    nm = nuclei > 0
    c1, trimmed, moved_ids, refused_ids = _move_straddling_pieces(c1, nuclei, nm, ntop)
    counts.update(nuclei_moved=int(moved_ids.size), moves_refused=int(refused_ids.size))
    objects["nuclei_moved"] = (nuclei, moved_ids)
    objects["moves_refused"] = (nuclei, refused_ids)

    # the pieces of every nucleus on that mask
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
    if trimmed.size:                                 # 1b refused: plain cell pixels
        tk = ((kn.astype(np.uint64) << np.uint64(32)) | kc.astype(np.uint64))
        elig &= ~np.isin(tk, trimmed)
        del tk
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
    key2, inv, cnt2 = np.unique((nuclei[nm].astype(np.uint64) << np.uint64(32)) | out[nm],
                                return_inverse=True, return_counts=True)
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

    # 4b. a cell with two or more seed nuclei is split among them (v1.4)
    split_ids = np.zeros(0, np.int64)
    before_split = None
    if split and parent.size:
        before_split = out.copy()
        parent, split_ids = split_by_seeds(out, nuclei, n2[ok], parent, cnt2[ok])
    objects["cells_split"] = (before_split if before_split is not None else out, split_ids)
    counts["cells_split"] = int(split_ids.size)

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


def _move_straddling_pieces(c1, nuclei, nm, ntop):
    """Rule 1b (plan §15.3): `(cells, trimmed keys, moved nucleus ids,
    refused nucleus ids)`. Every measure is taken on `c1` before any move;
    each losing cell is judged on all its losses together, without gains, so
    the order of nuclei does not matter. `trimmed` = packed (nucleus << 32 |
    cell) keys of the pieces refused (they stay plain pixels of their cell)."""
    from scipy import ndimage as ndi
    none = np.zeros(0, np.uint64), np.zeros(0, np.int64), np.zeros(0, np.int64)
    if not nm.any():
        return (c1,) + none
    pk = (nuclei[nm].astype(np.uint64) << np.uint64(32)) | c1[nm]
    key, cnt = np.unique(pk, return_counts=True)
    kn = (key >> np.uint64(32)).astype(np.int64)
    kc = (key & np.uint64(0xFFFFFFFF)).astype(np.int64)
    area = np.bincount(kn, weights=cnt, minlength=ntop + 1)
    fg = kc > 0
    if not fg.any():
        return (c1,) + none
    ncell = np.bincount(kn[fg], minlength=ntop + 1)
    order = np.lexsort((-cnt[fg], kn[fg]))
    fn, fc, fcnt = kn[fg][order], kc[fg][order], cnt[fg][order]
    first = np.unique(fn, return_index=True)[1]
    best_cell = np.zeros(ntop + 1, np.int64)
    best_px = np.zeros(ntop + 1, np.int64)
    second_px = np.zeros(ntop + 1, np.int64)
    best_cell[fn[first]] = fc[first]
    best_px[fn[first]] = fcnt[first]
    nxt = first + 1
    has2 = nxt < fn.size
    has2[has2] = fn[nxt[has2]] == fn[first[has2]]
    second_px[fn[first[has2]]] = fcnt[nxt[has2]]
    unique_best = best_px > second_px
    # a cell's own nucleus: its unique largest piece, >= 50 %, is in that cell
    own = unique_best & (best_px >= MAJORITY * area) & (best_px > 0)
    has_own = np.zeros(int(c1.max()) + 1, bool)
    has_own[best_cell[own]] = True
    cand = (fg & (ncell[kn] >= 2) & unique_best[kn] & (kc != best_cell[kn])
            & (cnt >= MIN_PIECE * area[kn]) & has_own[kc])
    if not cand.any():
        return (c1,) + none
    ckeys = key[cand]
    losers = np.unique(kc[cand])
    move_px = np.zeros(c1.shape, bool)
    move_px[nm] = np.isin(pk, ckeys)
    eight = np.ones((3, 3), bool)
    slices = ndi.find_objects(c1)
    refused_cells = []
    for a in losers.tolist():
        sl = slices[a - 1]
        am = c1[sl] == a
        _, before = ndi.label(am, structure=eight)
        _, after = ndi.label(am & ~move_px[sl], structure=eight)
        if after > before:
            refused_cells.append(a)
    refused = np.isin(kc[cand], np.asarray(refused_cells, np.int64))
    go_keys, trimmed = ckeys[~refused], ckeys[refused]
    out = c1.copy()
    if go_keys.size:
        go = np.zeros(c1.shape, bool)
        go[nm] = np.isin(pk, go_keys)
        out[go] = best_cell[nuclei[go].astype(np.int64)].astype(np.uint32)
    moved_ids = np.unique((go_keys >> np.uint64(32)).astype(np.int64))
    refused_ids = np.unique((trimmed >> np.uint64(32)).astype(np.int64))
    return out, trimmed, moved_ids, refused_ids


def split_by_seeds(out, nuclei, piece_nucleus, parent, piece_area):
    """Rule 4b (plan §15.3): split, in place in `out`, every cell holding two
    or more seeds -- pieces >= SEED_RATIO x the cell's largest piece and
    >= SEED_MIN_PX. Each split cell's crop is flooded from its seed pieces
    (geodesic nearest seed, 8-connected); a part the flood cannot reach takes
    the nearest flooded pixel's sub-cell; a non-seed piece goes whole to the
    sub-cell holding most of it (tie: the larger seed, then the seed whose
    first pixel comes first) and the cell under it follows. The first
    sub-cell keeps the cell's id, the others get new ids above `out.max()`.
    Returns `(parent, split cell ids)` -- `parent` updated per piece."""
    from scipy import ndimage as ndi
    from skimage.segmentation import watershed
    parent = np.asarray(parent, np.int64).copy()
    piece_nucleus = np.asarray(piece_nucleus, np.int64)
    piece_area = np.asarray(piece_area, np.int64)
    top = np.zeros(int(parent.max()) + 1, np.int64)
    np.maximum.at(top, parent, piece_area)
    seed = (piece_area >= SEED_RATIO * top[parent]) & (piece_area >= SEED_MIN_PX)
    nseed = np.bincount(parent[seed], minlength=top.size)
    cells = np.nonzero(nseed >= 2)[0]
    if not cells.size:
        return parent, np.zeros(0, np.int64)
    slices = ndi.find_objects(out)
    next_id = int(out.max()) + 1
    by_cell = {}
    for i in np.nonzero(np.isin(parent, cells))[0].tolist():
        by_cell.setdefault(int(parent[i]), []).append(i)
    for c in cells.tolist():
        sl = slices[c - 1]
        cm = out[sl] == c
        nuc = nuclei[sl]
        idx = by_cell[c]
        seeds = [i for i in idx if seed[i]]
        masks = {i: cm & (nuc == piece_nucleus[i]) for i in idx}
        # a window-invariant order: raster order of each seed's first pixel
        first = {i: int(np.flatnonzero(masks[i])[0]) for i in seeds}
        seeds.sort(key=lambda i: first[i])
        markers = np.zeros(cm.shape, np.int32)
        for k, i in enumerate(seeds, 1):
            markers[masks[i]] = k
        lab = watershed(np.zeros(cm.shape, np.uint8), markers, mask=cm, connectivity=2)
        lost = cm & (lab == 0)
        if lost.any():
            _, (iy, ix) = ndi.distance_transform_edt(lab == 0, return_indices=True)
            lab[lost] = lab[iy[lost], ix[lost]]
        rank = {i: (-int(piece_area[i]), first[i]) for i in seeds}
        for i in idx:
            if seed[i]:
                continue
            votes = np.bincount(lab[masks[i]], minlength=len(seeds) + 1)[1:]
            best = max(range(len(seeds)),
                       key=lambda k: (votes[k], tuple(-v for v in rank[seeds[k]])))
            lab[masks[i]] = best + 1
        ids = np.array([c] + list(range(next_id, next_id + len(seeds) - 1)), np.uint32)
        next_id += len(seeds) - 1
        region = out[sl]
        region[cm] = ids[lab[cm] - 1]
        for k, i in enumerate(seeds):
            parent[i] = int(ids[k])
        for i in idx:
            if not seed[i]:
                parent[i] = int(region[masks[i]][0])
    return parent, cells.astype(np.int64)


def owned_object_counts(objects, own_local):
    """{reason: how many of `objects`' ids this window owns} -- by each
    object's own centroid, as `owned_drop_counts` does for dropped nuclei."""
    out = {}
    for reason, (labels, ids) in (objects or {}).items():
        out[reason] = owned_drop_counts(labels, {reason: ids}, own_local).get(reason, 0)
    return out
