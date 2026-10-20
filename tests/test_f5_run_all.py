"""Regions' core table: Run all / Run none tick or untick every position the
list shows (the Show / Find filter narrows it), in one edit (user ruling
2026-10-20)."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")
from PyQt5 import QtWidgets  # noqa: E402

from test_regions_r1 import _finished  # noqa: E402

from block01.core.regions import document as D  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _rows():
    def r(i, run, donor):
        return {"kind": "position", "id": f"k{i}", "run": run, "position": f"X1-Y{i:02d}",
                "donor": donor, "sample": "", "status": "已对应", "pieces": 1, "tissue": ""}
    return [r(1, False, "K2024"), r(2, True, "K2024"), r(3, False, "Liver"),
            {"kind": "core", "id": "c9", "run": None, "position": "", "donor": "", "sample": "",
             "status": "未对应", "pieces": 1, "tissue": ""}]


def test_run_all_and_none_act_on_the_shown_positions_only(app):
    from block01.ui.tma.core_table import CoreTable
    t = CoreTable()
    t.set_rows(_rows(), "", read_only=False)
    got = []
    t.runs_set.connect(lambda keys, on: got.append((keys, on)))
    t.btn_run_all.click()
    assert got == [(["k1", "k3"], True)]                 # only the unticked; never a core row
    got.clear()
    t.find.setText("liver")                              # narrow the list first
    t.btn_run_none.click()
    assert got == []                                     # k3 is unticked already
    t.find.setText("k2024")
    t.btn_run_none.click()
    assert got == [(["k2"], False)]


def test_a_read_only_table_offers_neither(app):
    from block01.ui.tma.core_table import CoreTable
    t = CoreTable()
    t.set_rows(_rows(), "", read_only=True)
    assert not t.btn_run_all.isEnabled() and not t.btn_run_none.isEnabled()
    got = []
    t.runs_set.connect(lambda keys, on: got.append(keys))
    t._set_shown_runs(True)
    assert got == []


def test_set_runs_is_one_edit_and_one_undo_step():
    from block01.ui.tma.draft import DraftHolder
    s, d = _finished()
    keys = sorted(d["positions"])[:3]
    h = DraftHolder(d)
    n = len(h.d.get("operations") or [])
    h.do(D.set_runs, keys, True)
    assert all(h.d["positions"][k]["run"] for k in keys)
    assert len(h.d["operations"]) == n + 1
    h.undo()
    assert not any(h.d["positions"][k]["run"] for k in keys)
    h.redo()
    assert all(h.d["positions"][k]["run"] for k in keys)
