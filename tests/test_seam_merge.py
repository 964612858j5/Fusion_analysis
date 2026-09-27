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


# ── shared by the Step2 tests (block N3b): the reference merge ───────────

def reference_merge(tiles, locals_, shape, nuclei=None, nuclei_cell=None, tau=0.5, min_free=0.5):
    """The merge by the pure functions, independent of Step2's loops: every
    tile's candidates, interior (owned) ones as they are, seam ones decided
    together and painted deepest-margin first onto empty pixels. With
    nuclei: (cells, nuclei, parents) where a nucleus is written only when all
    its pixels lie in its cell's written pixels."""
    allc, crops = [], []
    seam_idx = []
    for t, loc in zip(tiles, locals_):
        cs, _n = sm.extract(t, loc, shape)
        ry0, _r1, rx0, _x1 = t.read
        for c in cs:
            crop = None
            if nuclei is not None:
                y0, y1, x0, x1 = c.bbox
                ln = np.asarray(nuclei[t.index][y0 - ry0:y1 - ry0, x0 - rx0:x1 - rx0], np.int64)
                cell_of = np.asarray(nuclei_cell[t.index], np.int64)
                ok = (ln > 0) & (ln < cell_of.size)
                mine = np.zeros(ln.shape, bool)
                mine[ok] = cell_of[ln[ok]] == c.label
                crop = np.where(mine & c.mask, ln, 0)
            if sm.is_seam(c, tiles):
                seam_idx.append(len(allc))
            allc.append(c)
            crops.append(crop)
    seam = [allc[k] for k in seam_idx]
    d = sm.resolve(seam, tiles, tau)
    # Step2's id order: interior cells tile by tile, label by label (they
    # never meet another tile's cells), then the seam cells deepest-margin
    # first -- the pixels equal a margin-order paint of everything
    seam_set = set(seam_idx)
    keep = [k for k in range(len(allc)) if k not in seam_set and allc[k].owned] + \
        [seam_idx[k] for k in d.kept]
    cells = np.zeros(shape, np.uint32)
    nuc = np.zeros(shape, np.uint32) if nuclei is not None else None
    parents = []
    nid = cid = 0
    for k in keep:
        c = allc[k]
        y0, y1, x0, x1 = c.bbox
        view = cells[y0:y1, x0:x1]
        free = c.mask & (view == 0)
        nf = int(free.sum())
        if nf == 0 or nf < min_free * c.area:
            continue
        cid += 1
        view[free] = cid
        if nuc is not None:
            for n in np.unique(crops[k][crops[k] > 0]):
                nm = crops[k] == n
                if free[nm].all():
                    nid += 1
                    nuc[y0:y1, x0:x1][nm] = nid
                    parents.append(cid)
    if nuclei is None:
        return cells
    return cells, nuc, np.asarray(parents, np.uint32)


def same_partition(a, b):
    """Do two label images split the pixels the same way (ids may differ)?
    Returns the id map a -> b, or None."""
    a, b = np.asarray(a).ravel(), np.asarray(b).ravel()
    if not np.array_equal(a > 0, b > 0):
        return None
    m = a > 0
    pairs = np.unique(np.stack([a[m], b[m]]), axis=1)
    if len(np.unique(pairs[0])) != pairs.shape[1] or len(np.unique(pairs[1])) != pairs.shape[1]:
        return None
    return dict(zip(pairs[0].tolist(), pairs[1].tolist()))


# ── SeamMerger (block N3b) ───────────────────────────────────────────────

def _merge_run(per_tile, nuclei=None, cells_of=None, tmp=None, chunk=1 << 20):
    sink_calls = []
    m = sm.SeamMerger((H, W), TILES, str(tmp), with_nuclei=nuclei is not None,
                      table_sink=lambda b: sink_calls.append(b.copy()), table_chunk=chunk)
    cells = np.zeros((H, W), np.uint32)
    nuc = np.zeros((H, W), np.uint32) if nuclei is not None else None
    interior = []
    for t in TILES:
        loc = local_of(t, per_tile.get(t.index, []))
        ln = None if nuclei is None else local_of(t, nuclei.get(t.index, []))
        interior.append(m.add_tile(t, loc, cells, nuc, ln,
                                   None if cells_of is None else cells_of.get(t.index)))
    rec = m.finish(cells, nuc)
    return m, cells, nuc, rec, interior, sink_calls


def test_the_merger_writes_interior_cells_at_once_and_seam_cells_at_the_end(tmp_path):
    inner, seam_a, seam_b = disc(15, 15, 4), disc(20, 48, 5), disc(20, 49, 5)
    m, cells, _n, rec, interior, _s = _merge_run({0: [inner, seam_a], 1: [seam_b]},
                                                 tmp=tmp_path / "spill")
    assert interior == [1, 0, 0, 0]
    assert m.n_cells == 2 and sorted(np.unique(cells)) == [0, 1, 2]
    assert cells[15, 15] == 1                         # the interior cell took id 1
    r = rec["seam_reconciliation"]
    assert r["candidates"] == 3 - 1 and r["duplicate_groups"] == 1
    assert rec["seam_merge"] == sm.SEAM_MERGE
    assert not (tmp_path / "spill").exists()
    sm.validate(cells, n_cells=m.n_cells)


def test_a_nucleus_that_cannot_be_written_whole_is_dropped(tmp_path):
    # two different cells across the seam (overlap < tau); tile 1's is deeper
    # and is written first; tile 0's loses the shared pixels -- where its
    # nucleus lies
    a = rect(10, 30, 38, 52)                          # tile 0, centroid x = 44.5
    b = rect(10, 30, 49, 80)                          # tile 1, centroid x = 64
    na = rect(15, 20, 49, 52)                         # a's nucleus, in the shared strip
    nb = rect(15, 20, 60, 64)
    m, cells, nuc, rec, _i, sink = _merge_run(
        {0: [a], 1: [b]}, nuclei={0: [na], 1: [nb]},
        cells_of={0: np.array([0, 1], np.uint32), 1: np.array([0, 1], np.uint32)},
        tmp=tmp_path / "spill")
    assert m.n_cells == 2
    assert rec["seam_reconciliation"]["nuclei_dropped_seam_conflict"] == 1
    assert m.n_nuclei == 1 and not nuc[na].any() and nuc[nb].all()
    table = np.concatenate(sink)
    assert table.tolist() == [cells[17, 62]]
    sm.validate(cells, nuc, table, n_cells=m.n_cells)


def test_the_table_goes_to_the_sink_in_chunks(tmp_path):
    shapes = [rect(5 + 6 * k, 9 + 6 * k, 5, 9) for k in range(5)]   # interior, tile 0
    nucs = [rect(6 + 6 * k, 8 + 6 * k, 6, 8) for k in range(5)]
    m, cells, nuc, _r, _i, sink = _merge_run(
        {0: shapes}, nuclei={0: nucs}, cells_of={0: np.arange(6, dtype=np.uint32)},
        tmp=tmp_path / "spill", chunk=2)
    assert [len(b) for b in sink] == [2, 2, 1]
    sm.validate(cells, nuc, np.concatenate(sink), n_cells=m.n_cells)


def test_validation_refuses_each_defect():
    import pytest
    cells = np.zeros((10, 10), np.uint32)
    cells[0:3, 0:3] = 1
    cells[5:8, 5:8] = 2
    nuc = np.zeros_like(cells)
    nuc[1, 1] = 1
    sm.validate(cells, nuc, np.array([1], np.uint32), n_cells=2)
    with pytest.raises(sm.SeamContractError, match="without pixels"):
        sm.validate(cells, n_cells=3)
    with pytest.raises(sm.SeamContractError, match="above"):
        sm.validate(cells, n_cells=1)
    with pytest.raises(sm.SeamContractError, match="outside its cell"):
        sm.validate(cells, nuc, np.array([2], np.uint32), n_cells=2)
    with pytest.raises(sm.SeamContractError, match="nucleus ids without pixels"):
        sm.validate(cells, nuc, np.array([1, 1], np.uint32), n_cells=2)


# ── Step2 end to end (stand-in engine) ───────────────────────────────────

def _seam_truths():
    """Two global cell truths that differ on one cell across the vertical
    seam of the 2 x 2 Step2 rig: tile 0's version leans right, the others'
    left, so today's centroid rule keeps neither."""
    import test_step2_keeps_nuclei as kn
    cells, nuclei, _e = kn._truth()
    y0, y1 = 20, 36
    split = kn.W // 2
    a = cells.copy()
    b = cells.copy()
    for arr in (a, b):
        arr[y0:y1, split - 20:split + 20] = 0
    a[y0:y1, split - 6:split + 10] = 9999            # centroid x = split + 1.5: tile 1's
    b[y0:y1, split - 10:split + 6] = 9999            # centroid x = split - 2.5: tile 0's
    nuclei = nuclei.copy()
    nuclei[y0:y1, split - 20:split + 20] = 0         # no nucleus in the test strip
    return a, b, nuclei.astype(np.uint32)


def test_step2_recovers_a_cell_both_tiles_drop_and_keeps_its_contract(tmp_path, monkeypatch):
    import json as _json
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.dirname(__file__))
    import test_step2_keeps_nuclei as kn
    import test_step2_runner_path as rp
    from PyQt5 import QtWidgets
    from block01.workers import segment_merge_worker as smw
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    a, b, nuclei = _seam_truths()

    class PerTile(kn._FakeEngine):
        def run(self, tasks, on_settle=None):
            out = {}
            for task in tasks:
                idx = int(__import__("re").search(r"(\d+)$", task["task_id"]).group(1))
                self.cells = a if idx == 0 else b
                out.update(super().run([task], on_settle))
            return out
    worker = rp._worker(tmp_path, rp._fused_zarr(tmp_path, rp._image()),
                        dict(rp.PARAMS[kn.GUIDED], method=kn.GUIDED), rois=None)
    engine = PerTile((a, nuclei), worker=worker)
    monkeypatch.setattr(smw, "EngineProcess", engine)
    monkeypatch.setattr(smw.SegmentMergeWorker, "_validate_mesmer_config", lambda s, *x, **k: None)
    got = rp._collect(worker)
    worker.run()
    assert got["error"] == [] and len(got["finished"]) == 1
    import zarr
    cell = np.asarray(zarr.open(_os.path.join(worker.output_dir, "global_mask.zarr"), "r"))
    ys, xs = np.nonzero(cell == cell[28, kn.W // 2])
    assert cell[28, kn.W // 2] > 0 and ys.size == 16 * 16          # recovered, whole
    assert set(np.unique(cell)) == set(range(int(cell.max()) + 1))  # no empty id
    meta = _json.load(open(_os.path.join(worker.output_dir, "segmentation_meta.json")))
    store = meta["label_store"]
    assert store["seam_merge"] == sm.SEAM_MERGE
    assert store["seam_reconciliation"]["recovered_missing_cells"] + \
        store["seam_reconciliation"]["duplicate_groups"] >= 1
    assert store["cell"]["n_objects"] == int(cell.max()) == got["finished"][0]
    names = _os.listdir(worker.output_dir)
    assert not [n for n in names if n.startswith(".seam_candidates")]


def test_a_broken_contract_fails_the_run_unregistered(tmp_path, monkeypatch):
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.dirname(__file__))
    import test_step2_keeps_nuclei as kn
    import test_step2_runner_path as rp

    def broken(*a, **k):
        raise sm.SeamContractError("a nucleus pixel lies outside its cell")
    monkeypatch.setattr(sm, "validate", broken)
    worker, _truth = kn._guided_worker(tmp_path, monkeypatch, None)
    got = rp._collect(worker)
    worker.run()
    assert got["finished"] == [] and "outside its cell" in got["error"][0]
    assert not rp._registered(worker)
    names = _os.listdir(worker.output_dir)
    assert not [n for n in names if n.endswith(".partial") or n.startswith(".seam_candidates")]
    assert not _os.path.exists(_os.path.join(worker.output_dir, "segmentation_meta.json"))


def test_a_corrupted_merge_is_caught_by_the_real_final_check(tmp_path, monkeypatch):
    """Not a stand-in check: the merge's output is broken after `finish` (a
    nucleus pixel moved into another cell) and Step2's own validation must
    fail the run."""
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.dirname(__file__))
    import test_step2_keeps_nuclei as kn
    import test_step2_runner_path as rp
    real_finish = sm.SeamMerger.finish

    def corrupting(self, cells, nuclei=None):
        rec = real_finish(self, cells, nuclei)
        if nuclei is not None:
            ys, xs = np.nonzero(np.asarray(nuclei) == 1)
            other = np.nonzero((np.asarray(cells) > 0) & (np.asarray(cells) != cells[ys[0], xs[0]]))
            nuclei[other[0][0], other[1][0]] = 1
        return rec
    monkeypatch.setattr(sm.SeamMerger, "finish", corrupting)
    worker, _truth = kn._guided_worker(tmp_path, monkeypatch, None)
    got = rp._collect(worker)
    worker.run()
    assert got["finished"] == [] and "outside its cell" in got["error"][0]
    assert not rp._registered(worker)
