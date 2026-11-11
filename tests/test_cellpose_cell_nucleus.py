"""Cellpose whole-cell + nuclei (docs/v17_cellpose_cell_nucleus_plan.md;
user rulings 2026-11-10): cells and nuclei from Step1's one image, a nucleus
kept only when all of it lies in one cell, several nuclei per cell allowed,
a cell with no nucleus dropped.

  * making cells and nuclei one (v1.3): each rule, ids, every piece in its
    cell, every cell with a piece;
  * the seam merge: a cell none of whose nuclei can be written is not
    written (before it takes an id); every written cell has a nucleus;
  * Step1: the pair on the window, cells owned by centroid, nuclei following
    their parent cell, the patch-edge case, the drop counts published;
  * the engine (real Cellpose, CPU): its cell mask is whole-cell's and its
    nucleus mask nuclei-DAPI's on the same input (wiring);
  * Step2 on a stand-in engine: no cell without a nucleus in the store, the
    counts recorded; Recovery Mode refused;
  * Step2's page: the nucleus values round-trip (None = auto), reset with
    the method.

Synthetic data in the test's temporary directory only.
"""
import json
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from block01.core import nuclei_pairing, preseg_run  # noqa: E402
from block01.core import seam_merge as sm  # noqa: E402
from block01.seg_runner import engines  # noqa: E402

METHOD = "cellpose_wholecell_nuclei"


def _square(a, label, y0, y1, x0, x1):
    a[y0:y1, x0:x1] = label


# ── making cells and nuclei one (plan §12-§13, v1.3) ────────────────────

def _combine(cells, nuclei, split=True):
    r = nuclei_pairing.combine_cells_and_nuclei(cells, nuclei, split=split)
    c, n, parent = r["cells"], r["nuclei"], r["nucleus_cell"]
    # invariants: ids 1..N / 1..K, every piece wholly in its parent cell,
    # every cell holds a piece
    assert set(np.unique(c[c > 0]).tolist()) == set(range(1, int(c.max()) + 1))
    assert set(np.unique(n[n > 0]).tolist()) == set(range(1, len(parent)))
    for k in range(1, len(parent)):
        assert (c[n == k] == parent[k]).all(), k
    assert set(parent[1:].tolist()) == set(range(1, int(c.max()) + 1))
    return r


def _two_cells():
    cells = np.zeros((40, 40), np.uint32)
    _square(cells, 1, 0, 10, 0, 10)          # A
    _square(cells, 2, 0, 10, 10, 20)         # B
    return cells


def test_a_cell_inside_a_nucleus_is_removed_and_the_nucleus_grows_a_cell():
    cells = np.zeros((40, 40), np.uint32)
    _square(cells, 1, 10, 14, 10, 14)
    nuclei = np.zeros_like(cells)
    _square(nuclei, 5, 8, 16, 8, 16)         # wholly covers cell 1
    r = _combine(cells, nuclei)
    assert r["counts"]["cells_inside_a_nucleus"] == 1
    assert r["counts"]["cells_from_nucleus"] == 1
    assert int(r["cells"].max()) == 1
    assert (r["cells"] > 0).sum() == 12 * 12              # 8 x 8 grown by 2 px
    assert (r["nuclei"] > 0).sum() == 64


def test_an_unshared_nucleus_mostly_in_its_cell_joins_it():
    cells = np.zeros((40, 40), np.uint32)
    _square(cells, 1, 0, 10, 0, 10)
    nuclei = np.zeros_like(cells)
    _square(nuclei, 5, 2, 6, 7, 12)          # 12 of 20 px in the cell, 8 on background
    r = _combine(cells, nuclei)
    assert r["counts"]["nuclei_joined"] == 1
    assert (r["cells"] == 1).sum() == 108                # the union
    assert (r["nuclei"] > 0).sum() == 20


def test_an_unshared_nucleus_mostly_on_background_becomes_a_cell():
    cells = np.zeros((40, 40), np.uint32)
    _square(cells, 1, 0, 10, 0, 10)
    nuclei = np.zeros_like(cells)
    _square(nuclei, 5, 2, 6, 8, 13)          # 8 of 20 px in the cell
    r = _combine(cells, nuclei)
    assert r["counts"]["cells_from_nucleus"] == 1
    assert r["counts"]["cells_without_nucleus"] == 1     # the old cell lost its only nucleus
    c = r["cells"]
    assert int(c.max()) == 1
    assert (c[nuclei == 5] == 1).all()
    old = np.zeros_like(cells, bool)
    old[0:10, 0:10] = True
    old[nuclei == 5] = False
    assert not c[old].any()                  # it never grew into the cell


def test_a_shared_nucleus_is_shared_out():
    cells = _two_cells()
    nuclei = np.zeros_like(cells)
    _square(nuclei, 5, 8, 12, 4, 14)         # A 12 px, B 8 px, background 20 px (50 %)
    r = _combine(cells, nuclei)
    c, n = r["cells"], r["nuclei"]
    assert r["counts"]["nuclei_shared"] == 1
    assert len(r["nucleus_cell"]) == 3                    # two pieces
    a, b = c[0, 0], c[0, 19]
    assert (n[c == a] > 0).sum() == 12 + 20              # A's piece + the background
    assert (n[c == b] > 0).sum() == 8
    assert (c == a).sum() == 120 and (c == b).sum() == 100


def test_the_background_of_a_shared_nucleus_mostly_on_background_is_discarded():
    cells = _two_cells()
    nuclei = np.zeros_like(cells)
    _square(nuclei, 5, 8, 13, 4, 14)         # A 12, B 8, background 30 (60 %)
    r = _combine(cells, nuclei)
    c, n = r["cells"], r["nuclei"]
    assert r["counts"]["background_discarded"] == 1
    assert (c == c[0, 0]).sum() == 100 and (c == c[0, 19]).sum() == 100
    assert (n > 0).sum() == 20 and not c[10:13, 4:14].any()


def test_a_tie_for_the_largest_piece_leaves_the_background():
    cells = _two_cells()
    nuclei = np.zeros_like(cells)
    _square(nuclei, 5, 8, 12, 5, 15)         # A 10, B 10, background 20
    r = _combine(cells, nuclei)
    assert not r["cells"][10:12, 5:15].any()
    assert (r["nuclei"] > 0).sum() == 20


def test_a_sliver_is_no_nucleus_and_its_cell_goes():
    cells = _two_cells()
    nuclei = np.zeros_like(cells)
    _square(nuclei, 5, 8, 12, 4, 11)         # A 12, B 2 (7 %), background 14
    r = _combine(cells, nuclei)
    c, n = r["cells"], r["nuclei"]
    assert r["counts"]["slivers"] == 1 and r["counts"]["cells_without_nucleus"] == 1
    assert int(c.max()) == 1                                # B is gone
    assert (n > 0).sum() == 12 + 14


def test_growth_stops_at_cells_and_a_contested_pixel_stays_background():
    cells = np.zeros((30, 40), np.uint32)
    _square(cells, 1, 10, 12, 19, 31)        # X, with its own nucleus inside
    nuclei = np.zeros_like(cells)
    _square(nuclei, 7, 10, 12, 22, 24)
    _square(nuclei, 5, 10, 12, 10, 12)       # two nuclei on background, 3 px apart
    _square(nuclei, 6, 10, 12, 15, 17)
    r = _combine(cells, nuclei)
    c = r["cells"]
    assert int(c.max()) == 3
    assert not c[10:12, 13].any()            # both reach it in the same step
    x = c[10, 25]
    assert (c[10:12, 19:31] == x).all() and (c == x).sum() == 24   # X untouched


def test_a_shared_nucleus_without_an_eligible_piece_is_lost_whole():
    """§14 (approved rules, kept on purpose): 85 % background, two pieces of
    8 % and 7 % -> no piece is a nucleus, the background is discarded."""
    cells = _two_cells()
    nuclei = np.zeros((40, 40), np.uint32)
    nuclei[9, 6:10] = 5                      # 4 px in A
    nuclei[9, 10:14] = 5                     # 4 px in B  (each 4 / 50 = 8 %)
    nuclei[10:14, 2:12] = 5                  # 40 px of background
    nuclei[14, 2:4] = 5
    r = nuclei_pairing.combine_cells_and_nuclei(cells, nuclei)
    assert r["counts"]["nuclei_shared_without_piece"] == 1
    assert not r["nuclei"].any() and not r["cells"].any()      # both cells had only slivers


def test_the_window_events_are_counted_by_owner():
    cells = _two_cells()
    nuclei = np.zeros((40, 40), np.uint32)
    _square(nuclei, 5, 8, 12, 4, 11)         # A 12, B a sliver of 2, background 14
    _square(nuclei, 6, 20, 24, 30, 34)       # on background -> a cell of its own
    r = nuclei_pairing.combine_cells_and_nuclei(cells, nuclei)
    got = nuclei_pairing.owned_object_counts(r["objects"], (0, 40, 0, 40))
    assert got == {"cells_inside_a_nucleus": 0, "nuclei_shared": 1,
                   "nuclei_shared_without_piece": 0, "nuclei_with_slivers": 1,
                   "nuclei_background_discarded": 0, "cells_without_nucleus": 1,
                   "cells_from_nucleus": 1, "nuclei_moved": 0, "moves_refused": 0,
                   "cells_split": 0}
    assert nuclei_pairing.owned_object_counts(r["objects"], (25, 40, 0, 40)) == \
        dict.fromkeys(got, 0)                # nothing of it is owned there


# ── v1.4 (plan §15): a straddling nucleus moves its cell's border ────────

def test_a_straddling_piece_moves_to_the_nucleus_s_cell():
    cells = _two_cells()                     # A cols 0-9, B cols 10-19
    nuclei = np.zeros_like(cells)
    _square(nuclei, 6, 2, 6, 2, 6)           # A's own nucleus
    _square(nuclei, 5, 3, 8, 8, 18)          # 50 px: 10 in A (20 %), 40 in B
    r = _combine(cells, nuclei)
    c, n = r["cells"], r["nuclei"]
    a, b = c[0, 0], c[0, 19]
    assert r["counts"]["nuclei_moved"] == 1 and r["counts"]["moves_refused"] == 0
    assert (c[3:8, 8:10] == b).all()         # A's border follows the nucleus
    assert (c == a).sum() == 90 and (c == b).sum() == 110
    assert (n[c == b] > 0).sum() == 50 and (n[c == a] > 0).sum() == 16
    assert len(r["nucleus_cell"]) == 3       # one nucleus each, none shared


def test_a_move_that_would_cut_the_cell_apart_is_trimmed_instead():
    cells = np.zeros((30, 30), np.uint32)
    _square(cells, 1, 0, 8, 0, 10)           # A: a body ...
    _square(cells, 1, 8, 14, 6, 10)          # ... and a tail below
    _square(cells, 2, 6, 12, 10, 20)         # B
    nuclei = np.zeros_like(cells)
    _square(nuclei, 6, 1, 5, 1, 5)           # A's own nucleus
    _square(nuclei, 5, 8, 10, 6, 16)         # 8 px in A (cuts the tail off), 12 in B
    r = _combine(cells, nuclei)
    c, n = r["cells"], r["nuclei"]
    a = c[0, 0]
    assert r["counts"]["moves_refused"] == 1 and r["counts"]["nuclei_moved"] == 0
    assert (c[8:10, 6:10] == a).all()        # A keeps the pixels ...
    assert not n[8:10, 6:10].any()           # ... as plain cell pixels, not a nucleus
    assert (n[c == a] > 0).sum() == 16


def test_no_move_when_the_cell_has_no_nucleus_of_its_own():
    cells = _two_cells()
    nuclei = np.zeros_like(cells)
    _square(nuclei, 5, 3, 8, 8, 18)          # 20 % in A, A has nothing else
    r = _combine(cells, nuclei)
    c, n = r["cells"], r["nuclei"]
    assert r["counts"]["nuclei_moved"] == 0
    assert (n[c == c[0, 0]] > 0).sum() == 10  # today's rule: A keeps its piece


def test_a_tie_for_the_nucleus_s_cell_moves_nothing():
    cells = _two_cells()
    nuclei = np.zeros_like(cells)
    _square(nuclei, 6, 1, 4, 1, 4)
    _square(nuclei, 7, 1, 4, 15, 18)
    _square(nuclei, 5, 5, 9, 5, 15)          # 20 in A, 20 in B
    r = _combine(cells, nuclei)
    assert r["counts"]["nuclei_moved"] == 0 and r["counts"]["moves_refused"] == 0
    assert (r["cells"] == r["cells"][0, 0]).sum() == 100


# ── v1.4 (plan §15): a cell with two nuclei is split ─────────────────────

def _one_cell(w=30):
    cells = np.zeros((20, 40), np.uint32)
    _square(cells, 1, 0, 10, 0, w)
    return cells


def test_two_similar_nuclei_split_their_cell():
    cells = _one_cell()
    nuclei = np.zeros_like(cells)
    _square(nuclei, 5, 2, 8, 2, 10)          # 48 px
    _square(nuclei, 6, 2, 8, 20, 28)         # 48 px
    r = _combine(cells, nuclei)
    c, n = r["cells"], r["nuclei"]
    assert r["counts"]["cells_split"] == 1 and int(c.max()) == 2
    assert (c > 0).sum() == 300              # no pixel lost
    assert c[0, 0] != c[0, 29]
    assert (c[nuclei == 5] == c[0, 0]).all() and (c[nuclei == 6] == c[0, 29]).all()


def test_the_switch_off_keeps_one_cell():
    cells = _one_cell()
    nuclei = np.zeros_like(cells)
    _square(nuclei, 5, 2, 8, 2, 10)
    _square(nuclei, 6, 2, 8, 20, 28)
    r = _combine(cells, nuclei, split=False)
    assert r["counts"]["cells_split"] == 0 and int(r["cells"].max()) == 1
    assert len(r["nucleus_cell"]) == 3


@pytest.mark.parametrize("second", [(2, 6, 20, 24),      # 16 px: under 40 px
                                    (2, 7, 20, 28)])     # 40 px vs 100: 0.4, not a seed
def test_a_fragment_or_an_uncertain_nucleus_does_not_split(second):
    cells = _one_cell()
    nuclei = np.zeros_like(cells)
    _square(nuclei, 5, 0, 10, 0, 10)         # 100 px
    _square(nuclei, 6, *second)
    r = _combine(cells, nuclei)
    assert r["counts"]["cells_split"] == 0 and int(r["cells"].max()) == 1
    assert len(r["nucleus_cell"]) == 3


def test_three_seeds_make_three_cells():
    cells = _one_cell(40)
    nuclei = np.zeros_like(cells)
    for k, x in ((5, 1), (6, 16), (7, 31)):
        _square(nuclei, k, 2, 8, x, x + 8)
    r = _combine(cells, nuclei)
    assert int(r["cells"].max()) == 3 and (r["cells"] > 0).sum() == 400


def test_a_non_seed_piece_goes_whole_to_one_sub_cell():
    cells = _one_cell()
    nuclei = np.zeros_like(cells)
    _square(nuclei, 5, 2, 8, 2, 10)
    _square(nuclei, 6, 2, 8, 20, 28)
    _square(nuclei, 7, 4, 7, 13, 17)         # 12 px, across the middle
    r = _combine(cells, nuclei)              # `_combine` checks it lies in one cell
    c = r["cells"]
    assert int(c.max()) == 2 and len(np.unique(c[nuclei == 7])) == 1
    assert (c > 0).sum() == 300


def test_a_part_cut_off_from_every_seed_keeps_its_pixels():
    out = np.zeros((20, 40), np.uint32)
    _square(out, 1, 0, 10, 0, 20)            # two seeds here ...
    _square(out, 1, 0, 10, 25, 30)           # ... and a part with none
    nuclei = np.zeros_like(out)
    _square(nuclei, 5, 2, 8, 1, 8)
    _square(nuclei, 6, 2, 8, 12, 19)
    parent, split = nuclei_pairing.split_by_seeds(out, nuclei, [5, 6], [1, 1], [42, 42])
    assert split.tolist() == [1] and sorted(parent.tolist()) == [1, 2]
    assert (out > 0).sum() == 250            # nothing of the cell lost
    assert (out[0:10, 25:30] == out[0, 19]).all()   # the nearest sub-cell


def test_without_nuclei_every_cell_goes():
    r = nuclei_pairing.combine_cells_and_nuclei(_two_cells(), np.zeros((40, 40), np.uint32))
    assert not r["cells"].any() and len(r["nucleus_cell"]) == 1


# ── the seam merge ───────────────────────────────────────────────────────

def _cand(label, y0, y1, x0, x1):
    m = np.ones((y1 - y0, x1 - x0), bool)
    return sm.Candidate(tile=0, label=label, bbox=(y0, y1, x0, x1), mask=m,
                        area=int(m.sum()), cy=(y0 + y1) / 2, cx=(x0 + x1) / 2,
                        margin=1.0, owned=True)


def _merger(tmp_path, require):
    table = []
    tile = sm.Tile(0, (0, 40, 0, 40), (0, 40, 0, 40))
    merger = sm.SeamMerger((40, 40), [tile], str(tmp_path / f"spill{require}"),
                           with_nuclei=True, table_sink=table.append, require_nucleus=require)
    return merger, table


def test_a_cell_whose_nuclei_cannot_be_written_is_not_written(tmp_path):
    cells = np.zeros((40, 40), np.uint32)
    nuclei = np.zeros((40, 40), np.uint32)
    merger, table = _merger(tmp_path, True)
    first = _cand(1, 0, 10, 0, 10)
    nuc = np.zeros((10, 10), np.uint32)
    nuc[2:5, 2:5] = 1
    assert merger._write(first, nuc, cells, nuclei) == 1
    # a second version over the same place: its only nucleus lies in pixels
    # the first already took -> no id, nothing written
    second = _cand(2, 0, 20, 0, 20)        # mostly free (300 of 400 px)
    nuc2 = np.zeros((20, 20), np.uint32)
    nuc2[3:6, 3:6] = 1
    assert merger._write(second, nuc2, cells, nuclei) == 0
    assert merger.stats["cells_dropped_no_writable_nucleus"] == 1
    # a cell with one writable and one blocked nucleus is written, with one
    third = _cand(3, 8, 20, 8, 20)          # 4 px of it are cell 1's
    nuc3 = np.zeros((12, 12), np.uint32)
    nuc3[0:3, 0:3] = 1                       # (8..11, 8..11): partly in cell 1
    nuc3[6:9, 6:9] = 2                       # free
    assert merger._write(third, nuc3, cells, nuclei) == 2
    # a cell without any nucleus crop at all
    assert merger._write(_cand(4, 30, 35, 30, 35), None, cells, nuclei) == 0
    merger.flush_table()
    parents = np.concatenate(table)
    assert merger.n_cells == 2 and merger.n_nuclei == 2
    assert set(parents.tolist()) == set(range(1, merger.n_cells + 1))   # every cell has one
    sm.validate(cells, nuclei, parents, merger.n_cells)                 # containment, ids


def test_without_the_rule_the_merge_is_unchanged(tmp_path):
    cells = np.zeros((40, 40), np.uint32)
    nuclei = np.zeros((40, 40), np.uint32)
    merger, _ = _merger(tmp_path, False)
    assert merger._write(_cand(4, 30, 35, 30, 35), None, cells, nuclei) == 1
    assert "cells_dropped_no_writable_nucleus" not in merger.stats


# ── Step1 ────────────────────────────────────────────────────────────────

def test_step1_combines_on_the_window_and_nuclei_follow_their_cell():
    from block01.ui.step1_presegmentation.run_job import (combine_window,
                                                         own_cell_nucleus_masks)
    cells = np.zeros((60, 60), np.uint32)
    nuclei = np.zeros((60, 60), np.uint32)
    own = (10, 50, 10, 50)                   # the patch inside a 10 px halo
    _square(cells, 1, 20, 30, 20, 30)        # owned, nucleus inside
    _square(nuclei, 7, 23, 26, 23, 26)
    _square(cells, 2, 40, 58, 40, 58)        # centroid (49, 49): owned; its nucleus
    _square(nuclei, 8, 53, 56, 53, 56)       # lies outside the patch (the edge case)
    _square(cells, 3, 30, 40, 30, 40)        # owned, no nucleus -> dropped, counted
    _square(cells, 4, 0, 8, 0, 8)            # centroid outside: not owned
    _square(nuclei, 9, 2, 5, 2, 5)
    _square(nuclei, 6, 12, 15, 40, 43)       # on background, owned -> a cell of its own
    combined = combine_window({"cell": cells, "nucleus": nuclei})
    raw = {"cell": combined["cells"], "nucleus": combined["nuclei"]}
    masks, drops = own_cell_nucleus_masks(raw, combined, own)
    c, n = masks["cell"], masks["nucleus"]
    assert c.shape == n.shape == (40, 40)
    assert len(np.unique(c[c > 0])) == 3                     # 1, 2 and the nucleus-born one
    assert len(np.unique(n[n > 0])) == 2                     # cell 2's nucleus is cropped away
    assert (c[n > 0] > 0).all()
    assert drops["cells_without_nucleus"] == 1 and drops["cells_from_nucleus"] == 1


def test_step1_records_the_drops(tmp_path):
    run = {"run_id": "r", "engines": {}, "fusion": {"hash": "h"}, "halo_px": 32}
    task = {"task_id": "t", "combo_id": "c", "method": METHOD, "params": {},
            "patch_bbox": [0, 4, 0, 4]}
    os.makedirs(tmp_path / "masks", exist_ok=True)
    os.makedirs(tmp_path / "records", exist_ok=True)
    rec = preseg_run.publish_result(str(tmp_path), run, task, preseg_run.OK,
                                    masks={"cell": np.ones((4, 4)), "nucleus": np.ones((4, 4))},
                                    paired=True, drops={"cells_without_nucleus": 3,
                                                        "multiple_cells": 1})
    assert rec["drops"] == {"cells_without_nucleus": 3, "multiple_cells": 1}
    again = preseg_run.load_records(str(tmp_path))
    assert next(iter(again.values()))["drops"]["cells_without_nucleus"] == 3


# ── the engine ───────────────────────────────────────────────────────────

def test_the_engine_runs_whole_cell_and_nuclei_dapi_on_one_input(monkeypatch):
    pytest.importorskip("cellpose")
    import test_step2_runner_path as rp
    from block01.core import preseg_input
    eng = engines.CellposeEngine()          # this machine's device (CPU is very slow)
    image = preseg_input.model_input(preseg_input.INPUT_KIND[METHOD], rp._image())
    params = {"diameter": 16.0, "flow_threshold": 0.4, "cellprob_threshold": 0.0,
              "min_size": 15, "nucleus_diameter": 12.0, "nucleus_flow_threshold": 0.5,
              "nucleus_cellprob_threshold": -0.5, "nucleus_min_size": 10}
    got = engines.run(eng, METHOD, image, params)
    whole = engines.run(eng, "cellpose_wholecell_fusion", image, params)["cell"]
    nuc_in = preseg_input.model_input("nucleus", rp._image())
    # the nucleus pass's values, written out (not through the mapping under test)
    nuclei = engines.run(eng, "cellpose_nuclei_dapi", nuc_in,
                         {"diameter": 12.0, "flow_threshold": 0.5,
                          "cellprob_threshold": -0.5, "min_size": 10})["nucleus"]
    np.testing.assert_array_equal(got["cell"], whole)
    np.testing.assert_array_equal(got["nucleus"], nuclei)
    assert got["cell"].max() > 0 and got["nucleus"].max() > 0


# ── Step2 ────────────────────────────────────────────────────────────────

def _raw_ome(path, image):
    import tifffile
    data = np.moveaxis(np.asarray(image), -1, 0)
    with tifffile.TiffWriter(str(path), ome=True) as tw:
        tw.write(data, metadata={"axes": "CYX"}, subifds=2)
        tw.write(data[:, ::2, ::2], subfiletype=1)
        tw.write(data[:, ::4, ::4], subfiletype=1)
    return str(path)


def _step2(tmp_path, monkeypatch, method=METHOD, **kw):
    pytest.importorskip("block01.qt")
    from block01.qt import QtWidgets
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    import test_step2_keeps_nuclei as kn
    import test_step2_runner_path as rp
    from block01.workers import segment_merge_worker as smw
    truth = kn._truth()
    image = rp._image()
    cfg = dict(rp.PARAMS["cellpose_wholecell_fusion"], method=method)
    cfg["raw_ome_path"] = _raw_ome(tmp_path / "slide.ome.tif", image)
    worker = rp._worker(tmp_path, rp._fused_zarr(tmp_path, image), cfg)
    for k, v in kw.items():
        setattr(worker, k, v)
    monkeypatch.setattr(smw, "EngineProcess", kn._FakeEngine(truth[:2], worker=worker))
    return worker, rp._collect(worker), truth


def test_step2_keeps_no_cell_without_a_nucleus(tmp_path, monkeypatch):
    import zarr
    worker, got, (cells_truth, nuclei_truth, expect) = _step2(tmp_path, monkeypatch)
    worker.run()
    out = worker.output_dir
    assert got["error"] == []
    assert os.path.isfile(os.path.join(out, "segmentation_meta.json"))   # written last
    cells = np.asarray(zarr.open(os.path.join(out, "global_mask.zarr"), "r"))
    nuclei = np.asarray(zarr.open(os.path.join(out, "global_nuclei_mask.zarr"), "r"))
    table = np.asarray(zarr.open(os.path.join(out, "global_nuclei_cell.zarr"), "r"))
    n_cells = int(cells.max())
    assert n_cells > 0
    assert set(table[1:].tolist()) == set(range(1, n_cells + 1))   # every cell has a nucleus
    sm.validate(cells, nuclei, table[1:], n_cells)
    # the truth's cells without a wholly-inside nucleus are not there
    with open(os.path.join(out, "segmentation_meta.json"), encoding="utf-8") as f:
        store = json.load(f)["label_store"]
    pairing = store["nuclei"]["pairing"]
    assert pairing["cells_from_nucleus"] > 0              # the truth's nuclei on background
    assert pairing["nuclei_shared"] > 0                   # its straddlers are shared out
    assert "cells_no_writable_nucleus" in pairing
    assert store["relation"] == {"nucleus_to_cell": "many_to_one"}


def test_step2_refuses_recovery_mode(tmp_path, monkeypatch):
    os.makedirs(tmp_path / "rec", exist_ok=True)
    worker, got, _ = _step2(tmp_path, monkeypatch, recovery_npy_dir=str(tmp_path / "rec"))
    worker.run()
    assert got["finished"] == []
    assert "Recovery Mode cannot run this method" in got["error"][0]


# ── Step2's page ─────────────────────────────────────────────────────────

def test_the_page_round_trips_the_nucleus_values():
    pytest.importorskip("block01.qt")
    from block01.qt import QtWidgets
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from block01.ui.step2_page import Step2Page
    from block01.utils.segmentation_config import normalize_segmentation_config
    page = Step2Page()
    try:
        cfg = normalize_segmentation_config({
            "method": METHOD, "diameter": 30.0, "flow_threshold": 0.5,
            "cellprob_threshold": -1.0, "min_size": 20, "nucleus_diameter": None,
            "nucleus_flow_threshold": 0.7, "nucleus_cellprob_threshold": -2.0,
            "nucleus_min_size": 9})
        page._loading_index_selection = True       # as every real caller loads a file
        page._apply_seg_config_to_ui(cfg)
        page._loading_index_selection = False
        back = page.get_seg_config()
        for k in ("nucleus_diameter", "nucleus_flow_threshold",
                  "nucleus_cellprob_threshold", "nucleus_min_size"):
            assert back[k] == cfg[k], k
            assert back["params"][k] == cfg[k], k
        assert back["nucleus_diameter"] is None                  # auto stays auto
        page._apply_method_defaults_to_ui(METHOD)
        back = page.get_seg_config()
        assert back["nucleus_flow_threshold"] == 0.4 and back["nucleus_min_size"] == 15
        # loaded as a file (`_seg_config` holds it), then the user switches to
        # another method by hand: that method carries no nucleus keys
        page._loading_index_selection = True
        page._apply_seg_config_to_ui(cfg)
        page._loading_index_selection = False
        page._seg_config = dict(cfg)
        page._set_param_source("manual")
        page._method_combo.setCurrentIndex(page._method_combo.findData("cellpose_wholecell_fusion"))
        other = page.get_seg_config()
        assert other["method"] == "cellpose_wholecell_fusion"
        assert not [k for k in list(other) + list(other["params"]) if k.startswith("nucleus_")]
    finally:
        page.close()
