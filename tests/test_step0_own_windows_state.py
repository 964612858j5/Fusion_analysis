"""Step0 keeps its own Min/Max/Gamma: the display state's two window tables.

User ruling 2026-10-27 (docs/v17_step0_own_ranges_plan.md): Step0 has its
own windows (STEP0) and Step1, Step2, Step3 and Regions share one set
(SHARED). Every reader and writer names its table, SHARED by default; a seed
is one pixel answer that fills every table still empty and never an answer.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets  # noqa: E402

from block01.core import display_identity as di  # noqa: E402
from block01.core.display_identity import SHARED_TABLE, STEP0_TABLE  # noqa: E402
from block01.ui import block01_display as bd  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


A = di.DatasetIdentity("/tmp/own_windows_A.ome.tiff", "1:1")
B = di.DatasetIdentity("/tmp/own_windows_B.ome.tiff", "2:2")


def _state():
    state = bd.ChannelDisplayState()
    state.bind(A)
    heard = {"shared": [], "step0": []}
    state.mapping_changed.connect(heard["shared"].append)
    state.step0_mapping_changed.connect(heard["step0"].append)
    return state, heard


def test_the_tables_are_apart_and_shared_is_the_default(app):
    state, heard = _state()
    state.set_mapping("CD3", 1.0, 2.0, 1.0, origin="t")
    state.set_mapping("CD3", 5.0, 6.0, 0.8, origin="t", table=STEP0_TABLE)

    assert state.mapping("CD3") == (1.0, 2.0, 1.0)
    assert state.mapping("CD3", table=SHARED_TABLE) == (1.0, 2.0, 1.0)
    assert state.mapping("CD3", table=STEP0_TABLE) == (5.0, 6.0, 0.8)
    assert heard == {"shared": ["CD3"], "step0": ["CD3"]}

    # A write to one table is announced on that table's signal only.
    state.set_mapping("CD3", 1.0, 3.0, 1.0, origin="t")
    assert heard == {"shared": ["CD3", "CD3"], "step0": ["CD3"]}
    assert state.mapping("CD3", table=STEP0_TABLE) == (5.0, 6.0, 0.8)


def test_a_seed_fills_every_empty_table_and_never_an_answer(app):
    state, heard = _state()
    state.set_mapping("CD3", 5.0, 6.0, 1.0, origin="t", table=STEP0_TABLE)

    assert state.missing_tables("CD3") == (SHARED_TABLE,)
    assert state.fill_seed("CD3", 1.0, 9.0) == (SHARED_TABLE,)
    assert state.mapping("CD3") == (1.0, 9.0, 1.0)
    assert state.mapping("CD3", table=STEP0_TABLE) == (5.0, 6.0, 1.0)
    # once filled, a seed has nowhere left to go
    assert state.missing_tables("CD3") == ()
    assert state.fill_seed("CD3", 0.0, 1.0) == ()
    assert state.mapping("CD3") == (1.0, 9.0, 1.0)


def test_the_nucleus_role_takes_the_typed_dapi_window_of_its_own_table(app):
    """A number typed for DAPI is DAPI's nucleus window in THAT table; the
    other table's untyped role is seeded."""
    state, _heard = _state()
    records = state.window_records(STEP0_TABLE)
    records["params"]["DAPI"] = {"min": 3.0, "max": 30.0, "gamma": 0.9}
    records["user_adjusted"].add("DAPI")

    state.fill_seed("DAPI", 1.0, 100.0, nucleus=True)

    assert state.mapping("DAPI", nucleus=True, table=STEP0_TABLE) == (3.0, 30.0, 0.9)
    assert state.mapping("DAPI", nucleus=True) == (1.0, 100.0, 1.0)


def test_replace_mappings_installs_exactly_the_set_named(app):
    state, heard = _state()
    state.set_mapping("CD3", 1.0, 2.0, origin="t")
    state.set_mapping("CD8", 1.0, 4.0, origin="t")
    heard["shared"].clear()

    changed = state.replace_mappings({("CD3", False): (7.0, 8.0, 1.0)},
                                     origin="handoff")

    assert state.mappings() == {("CD3", False): (7.0, 8.0, 1.0)}
    assert sorted(changed) == [("CD3", False), ("CD8", False)]
    assert sorted(heard["shared"]) == ["CD3", "CD8"]
    assert heard["step0"] == []


def test_an_install_names_both_tables_and_a_cancelled_restore_takes_them_back(app):
    state, heard = _state()
    state.set_mapping("CD3", 1.0, 2.0, origin="t")
    state.set_mapping("CD3", 3.0, 4.0, origin="t", table=STEP0_TABLE)

    state.prepare_restore(A, {"mappings": {("CD3", False): (9.0, 10.0, 1.0)},
                              "step0_mappings": {("CD3", False): (11.0, 12.0, 1.0)}})
    assert state.mapping("CD3") == (9.0, 10.0, 1.0)
    assert state.mapping("CD3", table=STEP0_TABLE) == (11.0, 12.0, 1.0)
    state.cancel_restore("test")

    assert state.mapping("CD3") == (1.0, 2.0, 1.0)
    assert state.mapping("CD3", table=STEP0_TABLE) == (3.0, 4.0, 1.0)

    heard["shared"].clear()
    heard["step0"].clear()
    assert state.install({"step0_mappings": {("CD3", False): (5.0, 6.0, 1.0)}})
    assert heard == {"shared": [], "step0": ["CD3"]}


def test_both_tables_and_their_records_follow_the_slide(app):
    state, _heard = _state()
    state.set_mapping("CD3", 3.0, 4.0, origin="t", table=STEP0_TABLE)
    state.window_records(STEP0_TABLE)["params"]["CD3"] = {"min": 3.0, "max": 4.0}

    state.bind(B)
    assert state.mapping("CD3", table=STEP0_TABLE) is None
    assert state.window_records(STEP0_TABLE)["params"] == {}

    state.bind(A)
    assert state.mapping("CD3", table=STEP0_TABLE) == (3.0, 4.0, 1.0)
    assert state.window_records(STEP0_TABLE)["params"]["CD3"]["max"] == 4.0


class _Worker:
    def __init__(self):
        self.submitted = []

    def submit(self, binding, channel, array, nucleus=False):
        self.submitted.append((channel, nucleus))
        return True


def _services(monkeypatch):
    services = bd.Block01DisplayServices()
    services.state.bind(A)
    worker = _Worker()
    monkeypatch.setattr(services, "_seed_worker_ready", lambda: worker)
    monkeypatch.setattr(services, "lowres_array",
                        lambda ch: np.linspace(1, 50, 64, dtype=np.float32))
    return services, worker


@pytest.mark.parametrize("held", [SHARED_TABLE, STEP0_TABLE])
def test_a_seed_is_asked_for_while_any_table_lacks_it(app, monkeypatch, held):
    """One table populated, the other empty -- in both directions. Asking
    only SHARED let a populated Step1 window starve Step0's own table."""
    services, worker = _services(monkeypatch)
    state = services.state
    state.set_mapping("CD3", 2.0, 20.0, origin="t", table=held)

    assert services.request_mapping_seed("CD3") is True
    assert worker.submitted == [("CD3", False)]

    services._on_mapping_seeded({"channel": "CD3", "binding": state.binding(),
                                 "min": 1.0, "max": 50.0, "gamma": 1.0})

    other = STEP0_TABLE if held == SHARED_TABLE else SHARED_TABLE
    assert state.mapping("CD3", table=held) == (2.0, 20.0, 1.0)
    assert state.mapping("CD3", table=other) == (1.0, 50.0, 1.0)
    # ...and with both answered, nothing more is asked for.
    worker.submitted.clear()
    assert services.request_mapping_seed("CD3") is False
    assert worker.submitted == []


def test_a_seed_that_lands_after_both_tables_were_answered_is_refused(app, monkeypatch):
    services, _worker = _services(monkeypatch)
    state = services.state
    binding = state.binding()
    state.set_mapping("CD3", 2.0, 20.0, origin="t")
    state.set_mapping("CD3", 3.0, 30.0, origin="t", table=STEP0_TABLE)

    services._on_mapping_seeded({"channel": "CD3", "binding": binding,
                                 "min": 1.0, "max": 50.0, "gamma": 1.0})

    assert state.mapping("CD3") == (2.0, 20.0, 1.0)
    assert state.mapping("CD3", table=STEP0_TABLE) == (3.0, 30.0, 1.0)
