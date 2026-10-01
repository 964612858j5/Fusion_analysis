"""Step2 tile-status display (block S2T, segment 1; docs/v16_Step2_tiles_application.md).

The run's progress is recorded independently of the drawn grid. It is drawn
only while the drawn grid is the run's grid: changing the tile spin boxes
during a run shows the new grid with no state at all, and changing back
shows every state recorded so far. The worker's signals are emitted by hand
here (no engine).
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")
zarr = pytest.importorskip("zarr")
from PyQt5 import QtWidgets  # noqa: E402

GREY, YELLOW, GREEN = "#808080", "#ffc832", "#3cc850"


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def page(app, tmp_path):
    from block01.ui.step2_page import Step2Page
    zp = str(tmp_path / "fused_Full WSI.zarr")
    z = zarr.open(zp, mode="w", shape=(600, 800, 2), chunks=(256, 256, 2), dtype=np.uint16)
    z[...] = np.random.default_rng(0).integers(0, 4000, (600, 800, 2), dtype=np.uint16)
    p = Step2Page()
    p._zarr_edit.setText(zp)
    p._load_zarr_info()
    p._rows_spin.setValue(3)
    p._cols_spin.setValue(4)
    assert len(p._tile_rects) == 12
    yield p
    p.deleteLater()


def _colours(p):
    return {k: r.pen.color().name() for k, r in p._tile_rects.items()}


def _start(p):
    """What `_run` does for the display when it starts a 3x4 run."""
    p._n_rows, p._n_cols = 3, 4
    p._begin_run_progress(3, 4)
    p._record_tile(0, "running")


def test_states_drawn_on_the_run_grid(page):
    _start(page)
    page._on_tile_done(0, 12, 5)
    page._on_progress(1, 12, "tile 2")
    c = _colours(page)
    assert c[(0, 0)] == GREEN and c[(0, 1)] == YELLOW
    assert all(v == GREY for k, v in c.items() if k not in ((0, 0), (0, 1)))
    assert page._tile_status[(0, 0)] == "done" and page._tile_status[(0, 1)] == "running"


def test_another_grid_shows_no_state_and_the_run_grid_restores_all(page):
    """The reported bug: 3x4 running, 6x4 shown, back to 3x4."""
    _start(page)
    page._on_tile_done(0, 12, 5)
    page._rows_spin.setValue(6)                       # the user looks at 6x4
    assert len(page._tile_rects) == 24
    page._on_progress(1, 12, "tile 2")
    page._on_tile_done(1, 12, 3)
    page._on_progress(2, 12, "tile 3")
    c = _colours(page)
    assert set(c.values()) == {GREY}                   # nothing of the 3x4 run on 6x4
    assert set(page._tile_status.values()) == {"idle"}
    page._rows_spin.setValue(3)                        # back to the run's grid
    c = _colours(page)
    assert c[(0, 0)] == GREEN and c[(0, 1)] == GREEN and c[(0, 2)] == YELLOW
    assert sum(v != GREY for v in c.values()) == 3
    assert page._tile_status[(0, 1)] == "done" and page._tile_status[(0, 2)] == "running"


def test_the_final_state_survives_a_regrid_after_the_run(page):
    _start(page)
    for i in range(12):
        page._on_tile_done(i, 12, 1)
    page._cols_spin.setValue(6)
    page._cols_spin.setValue(4)
    assert set(_colours(page).values()) == {GREEN}


def test_a_new_run_starts_from_a_clean_record(page):
    _start(page)
    for i in range(3):
        page._on_tile_done(i, 12, 1)
    _start(page)
    page._rows_spin.setValue(6)
    page._rows_spin.setValue(3)                 # redraw from the record
    c = _colours(page)
    assert c[(0, 0)] == YELLOW and sum(v != GREY for v in c.values()) == 1
    assert page._run_progress["state"] == {0: "running"}


def test_the_running_grid_is_named_while_running(page):
    _start(page)
    assert not page._run_grid_lbl.isHidden() and "3×4" in page._run_grid_lbl.text()


def test_regridding_during_a_run_does_not_change_the_runs_grid(page):
    """The worker keeps its own grid; the page keeps the run's grid."""
    _start(page)
    page._rows_spin.setValue(5)
    page._cols_spin.setValue(7)
    assert page._run_progress["grid"] == (3, 4) and (page._n_rows, page._n_cols) == (3, 4)
