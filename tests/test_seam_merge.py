"""Block N3a: seam reconciliation as pure functions (`core/seam_merge.py`).

Synthetic tiles over a small region:
  * candidates: a cell touching an INTERNAL read-window edge is not a
    candidate, one on the region's own border is; ownership margin sign;
  * a duplicate (two tiles, one cell) -> one version, the deeper one;
  * a cell BOTH tiles drop by the centroid rule (today's loss) -> recovered;
  * a cell only one tile saw, centroid in the neighbour's region -> kept;
  * split / merge (one tile M, the other P and Q) -> M, or P AND Q;
  * a chain A~B~C with A, C not directly overlapping -> one component;
  * a four-tile corner, one cell in four versions -> one kept;
  * nothing depends on the order of tiles or candidates;
  * painting: empty pixels only, ids 1..N consecutive, clipped pixels.
"""

import itertools
import random

import numpy as np

from block01.core import seam_merge as sm

H, W = 100, 100
OV = 20
# 2 x 2 tiles, own regions split at 50; read windows extend OV past the split
TILES = [sm.Tile(0, (0, 50 + OV, 0, 50 + OV), (0, 50, 0, 50)),
         sm.Tile(1, (0, 50 + OV, 50 - OV, 100), (0, 50, 50, 100)),
         sm.Tile(2, (50 - OV, 100, 0, 50 + OV), (50, 100, 0, 50)),
         sm.Tile(3, (50 - OV, 100, 50 - OV, 100), (50, 100, 50, 100))]


def disc(cy, cx, r):
    yy, xx = np.mgrid[0:H, 0:W]
    return (yy - cy) ** 2 + (xx - cx) ** 2 <= r * r


def rect(y0, y1, x0, x1):
    m = np.zeros((H, W), bool)
    m[y0:y1, x0:x1] = True
    return m


def local_of(tile, shapes):
    """The tile's local label image from region-coordinate masks (1, 2, ...)."""
    y0, y1, x0, x1 = tile.read
    loc = np.zeros((y1 - y0, x1 - x0), np.uint32)
    for lab, m in enumerate(shapes, start=1):
        loc[m[y0:y1, x0:x1]] = lab
    return loc


def candidates(per_tile):
    out = []
    for t in TILES:
        c, _n = sm.extract(t, local_of(t, per_tile.get(t.index, [])), (H, W))
        out += c
    return out


def kept_keys(cands, tau=0.5):
    d = sm.resolve(cands, TILES, tau)
    return sorted(cands[k].key for k in d.kept), d


def test_internal_edges_exclude_but_region_border_does_not():
    t = TILES[0]
    loc = local_of(t, [rect(0, 5, 10, 15),          # on the region's top border: kept
                       rect(30, 40, 60, 70)])       # touches the internal right edge (x=70)
    c, touching = sm.extract(t, loc, (H, W))
    assert [x.label for x in c] == [1] and touching == 1
    assert sm.margin(10, 10, t.own) > 0 and sm.margin(10, 60, t.own) < 0


def test_a_duplicate_keeps_the_deeper_version():
    # the same cell near the vertical seam, seen by tiles 0 and 1
    a, b = disc(20, 48, 5), disc(20, 49, 5)
    cands = candidates({0: [a], 1: [b]})
    keys, d = kept_keys(cands)
    assert len(keys) == 1 and d.stats["duplicate_groups"] == 1
    assert d.stats["today_duplicate_groups"] == 0      # centroids 48 / 49 both in tile 0
    # tile 0's version has centroid x=48 (margin 1); tile 1's x=49 is not in
    # tile 1 (margin < 0): tile 0 wins
    assert keys == [(0, 1)]


def test_todays_duplicate_is_resolved():
    a, b = disc(20, 48, 5), disc(20, 51, 5)     # tile 0 owns a, tile 1 owns b
    cands = candidates({0: [a], 1: [b]})
    assert all(c.owned for c in cands)
    keys, d = kept_keys(cands)
    assert len(keys) == 1 and d.stats["today_duplicate_groups"] == 1


def test_a_cell_both_tiles_drop_is_recovered():
    # tile 0's version leans right (centroid in tile 1), tile 1's leans left
    a = disc(20, 51, 5)
    b = disc(20, 48, 5)
    cands = candidates({0: [a], 1: [b]})
    assert not any(c.owned for c in cands)            # today: lost
    keys, d = kept_keys(cands)
    assert len(keys) == 1 and d.stats["today_missing_groups"] == 1


def test_a_cell_only_the_neighbour_missed_is_kept():
    cands = candidates({0: [disc(20, 55, 4)]})        # centroid in tile 1, tile 1 saw nothing
    keys, d = kept_keys(cands)
    assert keys == [(0, 1)] and d.stats["recovered_missing_cells"] == 1


def test_split_merge_keeps_a_whole_version():
    m = rect(10, 30, 40, 60)                          # tile 0: one cell across the seam
    p, q = rect(10, 20, 40, 60), rect(20, 30, 40, 60) # tile 1: two cells
    for version_of_0, version_of_1 in [([m], [p, q])]:
        cands = candidates({0: version_of_0, 1: version_of_1})
        keys, d = kept_keys(cands)
        tiles = {k[0] for k in keys}
        assert len(tiles) == 1
        assert keys in ([(0, 1)], [(1, 1), (1, 2)])
        assert d.stats["split_merge_groups"] == 1


def test_a_chain_is_one_component():
    # A (tile 0) ~ B (tile 1) ~ C (tile 3); A and C do not overlap
    a = rect(40, 50, 44, 52)
    b = rect(44, 56, 46, 56)
    c = rect(52, 60, 50, 58)
    cands = candidates({0: [a], 1: [b], 3: [c]})
    assert len(cands) == 3
    assert sm.overlap(cands[0], cands[2]) == 0
    keys, d = kept_keys(cands, tau=0.3)
    assert d.stats["components"] == 1 and len(keys) == 1


def test_a_four_tile_corner_keeps_one_version():
    shapes = {i: [disc(50 + dy, 50 + dx, 6)] for i, (dy, dx) in
              enumerate([(-1, -1), (-1, 1), (1, -1), (1, 1)])}
    cands = candidates(shapes)
    assert len(cands) == 4
    keys, d = kept_keys(cands)
    assert len(keys) == 1 and d.stats["duplicate_versions_removed"] == 3


def test_nothing_depends_on_order():
    rng = np.random.default_rng(0)
    shapes = {i: [] for i in range(4)}
    for _ in range(40):
        cy, cx = rng.uniform(8, 92, size=2)
        r = rng.uniform(3, 6)
        for i, t in enumerate(TILES):
            y0, y1, x0, x1 = t.read
            if y0 + r < cy < y1 - r and x0 + r < cx < x1 - r:
                jy, jx = rng.integers(-1, 2, size=2)
                shapes[i].append(disc(cy + jy, cx + jx, r))
    for i in shapes:                                   # a tile's own labels must not overlap
        acc = np.zeros((H, W), bool)
        keep = []
        for m in shapes[i]:
            if not (m & acc).any():
                keep.append(m)
                acc |= m
        shapes[i] = keep
    base = candidates(shapes)
    ref, dref = kept_keys(base)
    ref_img, _ = sm.paint(base, dref.kept, (H, W))
    for perm in itertools.islice(itertools.permutations(range(4)), 8):
        cands = [c for t in perm for c in base if c.tile == t]
        random.Random(sum(perm)).shuffle(cands)
        keys, d = kept_keys(cands)
        assert keys == ref
        img, _ = sm.paint(cands, d.kept, (H, W))
        np.testing.assert_array_equal(img, ref_img)


def test_paint_fills_empty_pixels_only_with_consecutive_ids():
    a, b = rect(10, 20, 40, 52), rect(12, 22, 48, 60)   # small overlap: two cells
    cands = candidates({0: [a], 1: [b]})
    d = sm.resolve(cands, TILES, 0.5)
    assert len(d.kept) == 2
    img, clipped = sm.paint(cands, d.kept, (H, W), offset=10)
    assert sorted(np.unique(img)) == [0, 11, 12]
    first, second = d.kept
    assert clipped[first] == 0 and clipped[second] == sm.overlap(cands[0], cands[1])
    assert (img[cands[first].bbox[0]:cands[first].bbox[1],
                cands[first].bbox[2]:cands[first].bbox[3]][cands[first].mask] == 11).all()


def test_a_candidate_left_with_less_than_the_minimum_is_dropped_whole():
    a = rect(10, 20, 40, 52)
    b = rect(10, 20, 44, 56)          # 8 of 12 columns shared: two cells at tau 0.7
    cands = candidates({0: [a], 1: [b]})
    d = sm.resolve(cands, TILES, 0.7)
    assert len(d.kept) == 2
    img, clipped = sm.paint(cands, d.kept, (H, W), min_free_fraction=0.5)
    second = d.kept[1]
    assert clipped[second] == cands[second].area        # 4/12 free < 0.5: dropped whole
    assert sorted(np.unique(img)) == [0, 1]
    img2, _ = sm.paint(cands, d.kept, (H, W), min_free_fraction=0.3)
    assert sorted(np.unique(img2)) == [0, 1, 2]


def test_the_deeper_version_wins_even_from_the_later_tile():
    # tile 0's version leans into tile 1 (margin < 0), tile 1's is inside tile 1
    a, b = disc(20, 51, 5), disc(20, 53, 5)
    cands = candidates({0: [a], 1: [b]})
    keys, _d = kept_keys(cands)
    assert keys == [(1, 1)]


def test_split_merge_keeps_both_cells_when_the_split_tile_wins():
    m = rect(10, 30, 50, 66)                          # tile 0: one cell, centroid in tile 1
    p, q = rect(10, 20, 50, 66), rect(20, 30, 50, 66) # tile 1: two cells, inside tile 1
    cands = candidates({0: [m], 1: [p, q]})
    keys, d = kept_keys(cands)
    assert keys == [(1, 1), (1, 2)] and d.stats["split_merge_groups"] == 1
