"""Block N2: `core.nuclei_pairing.pair_nuclei` on hand-made labels.

  * a nucleus wholly inside one cell is kept, pointing at that cell;
  * one touching two cells, one straddling a cell and background, one on
    background only: dropped, each counted under its own reason;
  * two nuclei in one cell: both kept, each with its own id, both -> that cell;
  * independent numbering (nucleus ids larger than the cell count, unrelated
    to cell ids): paired correctly; kept ids are 1..K in original order;
  * empty input; shapes must match.
"""

import numpy as np
import pytest

from block01.core import nuclei_pairing as npair


def _grid():
    cells = np.zeros((12, 20), np.uint32)
    cells[1:6, 1:9] = 7          # cell 7
    cells[1:6, 9:17] = 3         # cell 3, touching cell 7
    cells[7:11, 1:9] = 50        # cell 50
    return cells


def test_the_four_cases_and_the_counts():
    cells = _grid()
    nuc = np.zeros_like(cells)
    nuc[2:4, 2:4] = 900          # inside cell 7                  -> kept
    nuc[2:4, 8:10] = 41          # across cells 7 and 3           -> multiple_cells
    nuc[5:7, 3:5] = 12           # cell 7 and background (row 6)  -> partial_background
    nuc[8:10, 18:20] = 5         # background only                -> outside_cells
    nuc[8:10, 2:4] = 300         # inside cell 50                 -> kept
    kept, table, counts, dropped = npair.pair_nuclei(cells, nuc)
    assert counts == {"predicted": 5, "kept": 2, "multiple_cells": 1,
                      "partial_background": 1, "outside_cells": 1}
    # kept ids 1..K in the order of the original ids: 300 -> 1, 900 -> 2
    assert list(table) == [0, 50, 7]
    assert set(np.unique(kept)) == {0, 1, 2}
    assert (kept[nuc == 300] == 1).all() and (kept[nuc == 900] == 2).all()
    assert not kept[(nuc == 41) | (nuc == 12) | (nuc == 5)].any()
    assert {k: list(v) for k, v in dropped.items()} == {
        "multiple_cells": [41], "partial_background": [12], "outside_cells": [5]}
    assert kept.dtype == np.uint32 and table.dtype == np.uint32


def test_several_nuclei_in_one_cell_are_all_kept():
    cells = _grid()
    nuc = np.zeros_like(cells)
    nuc[2:4, 2:4] = 1
    nuc[2:4, 5:7] = 2
    nuc[4:5, 3:6] = 3            # three nuclei, all in cell 7
    kept, table, counts, dropped = npair.pair_nuclei(cells, nuc)
    assert counts["kept"] == 3 and list(table) == [0, 7, 7, 7]
    assert len(set(np.unique(kept)) - {0}) == 3


def test_every_kept_nucleus_lies_in_its_cell():
    rng = np.random.default_rng(3)
    cells = np.kron(rng.integers(0, 40, (8, 8)), np.ones((16, 16), int)).astype(np.uint32)
    nuc = np.zeros_like(cells)
    ids = rng.permutation(np.arange(1000, 1100))
    for n, (y, x) in zip(ids, rng.integers(0, 124, (100, 2))):
        nuc[y:y + 4, x:x + 4] = n
    kept, table, counts, dropped = npair.pair_nuclei(cells, nuc)
    k = kept > 0
    assert (cells[k] == table[kept[k]]).all()                   # in its own cell
    assert counts["predicted"] == len(np.unique(nuc)) - 1
    assert counts["kept"] + counts["multiple_cells"] + counts["partial_background"] \
        + counts["outside_cells"] == counts["predicted"]
    assert counts["kept"] == table.size - 1 == len(np.unique(kept)) - 1


def test_empty_and_shapes():
    cells = _grid()
    kept, table, counts, dropped = npair.pair_nuclei(cells, np.zeros_like(cells))
    assert not kept.any() and list(table) == [0] and counts["predicted"] == 0
    with pytest.raises(ValueError):
        npair.pair_nuclei(cells, np.zeros((3, 3), np.uint32))


def test_a_dropped_nucleus_is_counted_by_the_window_that_owns_it():
    cells = _grid()
    nuc = np.zeros_like(cells)
    nuc[2:4, 8:10] = 41          # centroid x 8.5
    nuc[8:10, 18:20] = 5         # centroid x 18.5
    _kept, _t, _c, dropped = npair.pair_nuclei(cells, nuc)
    left = npair.owned_drop_counts(nuc, dropped, (0, 12, 0, 10))
    right = npair.owned_drop_counts(nuc, dropped, (0, 12, 10, 20))
    assert left == {"multiple_cells": 1, "partial_background": 0, "outside_cells": 0}
    assert right == {"multiple_cells": 0, "partial_background": 0, "outside_cells": 1}
