"""StarDist runs a whole tile at once and crashed (std::bad_alloc) on a
~150 MP tile (2026-10-25): with StarDist the grid is split until no tile's
read window exceeds the cap -- when the method is chosen, and again at Run."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")
from PyQt5 import QtWidgets  # noqa: E402

from block01.utils.segmentation_config import STARDIST_NUCLEI_EXPANSION, CELLPOSE_WHOLECELL_FUSION  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _page(method):
    from block01.ui.step2_page import Step2Page
    p = Step2Page()
    p._full_h, p._full_w = 58320, 28800              # Slice1
    p._overlap_spin.setValue(200)
    p._method_combo.setCurrentIndex(p._method_combo.findData(method))
    return p


def test_the_crashing_grid_is_split_below_the_cap(app):
    p = _page(STARDIST_NUCLEI_EXPANSION)
    assert p._largest_tile_px(3, 4) > 140_000_000              # the 2026-10-25 grid
    nr, nc = p._stardist_safe_grid(3, 4)
    assert p._largest_tile_px(nr, nc) <= p.STARDIST_MAX_TILE_PX
    assert (nr, nc) != (3, 4) and nr >= 3 and nc >= 4


def test_choosing_stardist_splits_an_unsafe_grid_at_once(app):
    p = _page(CELLPOSE_WHOLECELL_FUSION)
    p._set_grid(3, 4)
    assert (p._rows_spin.value(), p._cols_spin.value()) == (3, 4)   # typing / setting: no adjustment
    p._method_combo.setCurrentIndex(p._method_combo.findData(STARDIST_NUCLEI_EXPANSION))  # StarDist chosen
    assert p._largest_tile_px(p._rows_spin.value(), p._cols_spin.value()) <= p.STARDIST_MAX_TILE_PX
    assert "StarDist" in p._tile_suggestion_lbl.text()
    assert p._keep_stardist_tiles_safe() is False               # already safe: left alone


def test_other_methods_keep_their_grid(app):
    p = _page(CELLPOSE_WHOLECELL_FUSION)
    p._set_grid(3, 4)
    assert p._keep_stardist_tiles_safe() is False
    assert (p._rows_spin.value(), p._cols_spin.value()) == (3, 4)


def test_a_typed_grid_is_checked_when_the_typing_is_done(app):
    p = _page(STARDIST_NUCLEI_EXPANSION)
    p._set_grid(3, 4)                                    # as digits arrive: left as typed
    assert (p._rows_spin.value(), p._cols_spin.value()) == (3, 4)
    p._rows_spin.editingFinished.emit()                  # leaving the box
    assert p._largest_tile_px(p._rows_spin.value(), p._cols_spin.value()) <= p.STARDIST_MAX_TILE_PX


def test_a_grid_past_the_boxes_maximum_is_applied_not_clamped(app):
    p = _page(STARDIST_NUCLEI_EXPANSION)
    p._full_h = p._full_w = 200_000                      # a very large input
    nr, nc = p._stardist_safe_grid(4, 4)
    assert nr > 20 or nc > 20
    p._set_grid(nr, nc)
    assert (p._rows_spin.value(), p._cols_spin.value()) == (nr, nc)
    assert p._largest_tile_px(nr, nc) <= p.STARDIST_MAX_TILE_PX


def test_every_region_of_a_multi_roi_run_is_checked(app, tmp_path):
    zarr = pytest.importorskip("zarr")
    p = _page(STARDIST_NUCLEI_EXPANSION)
    small = str(tmp_path / "a.zarr")
    big = str(tmp_path / "b.zarr")
    zarr.open(small, mode="w", shape=(2000, 2000, 2), chunks=(1000, 1000, 2), dtype="u2")
    zarr.open(big, mode="w", shape=(58320, 28800, 2), chunks=(4096, 4096, 2), dtype="u2")
    p._full_h, p._full_w = 2000, 2000                    # the shown (first) region is small
    p.set_region_inputs({"A": small, "B": big})
    assert p._largest_tile_px(3, 4) > 140_000_000        # region B decides
    nr, nc = p._stardist_safe_grid(3, 4)
    assert p._largest_tile_px(nr, nc) <= p.STARDIST_MAX_TILE_PX


def test_the_run_guard_refuses_on_no_and_applies_on_yes_before_the_config(app, monkeypatch):
    from PyQt5.QtWidgets import QMessageBox
    p = _page(STARDIST_NUCLEI_EXPANSION)
    p._set_grid(3, 4)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.No))
    assert p._stardist_grid_ok_for_run() is False
    assert (p._rows_spin.value(), p._cols_spin.value()) == (3, 4)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    assert p._stardist_grid_ok_for_run() is True
    assert p._largest_tile_px(p._rows_spin.value(), p._cols_spin.value()) <= p.STARDIST_MAX_TILE_PX


def test_a_safe_grid_keeps_the_skip_choices_with_auto_on(app, monkeypatch):
    """Auto on, grid already safe: the run guard changes nothing, so the skip
    plan (with the user's choices) is not rebuilt (codex round 2)."""
    p = _page(STARDIST_NUCLEI_EXPANSION)
    p._auto_tile_strategy.setChecked(True)
    nr, nc = p._stardist_safe_grid(*(p._rows_spin.value(), p._cols_spin.value()))
    p._set_grid(nr, nc)
    calls = []
    monkeypatch.setattr(p, "_refresh_skip_plan", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(p, "_refresh_tile_suggestion", lambda *a, **k: calls.append("auto"))
    assert p._stardist_grid_ok_for_run() is True
    assert calls == [] and (p._rows_spin.value(), p._cols_spin.value()) == (nr, nc)


def test_auto_follows_a_new_inputs_size(app, tmp_path):
    zarr = pytest.importorskip("zarr")
    p = _page(STARDIST_NUCLEI_EXPANSION)
    p._auto_tile_strategy.setChecked(True)
    big = (p._rows_spin.value(), p._cols_spin.value())
    small = str(tmp_path / "small.zarr")
    z = zarr.open(small, mode="w", shape=(2000, 2000, 2), chunks=(1000, 1000, 2), dtype="u2")
    z.attrs["channel_0"], z.attrs["channel_1"] = "fusion", "DAPI"
    p._zarr_edit.setText(small)
    p._load_zarr_info()
    assert (p._rows_spin.value(), p._cols_spin.value()) != big
    assert p._rows_spin.value() * p._cols_spin.value() <= 2


def test_switching_input_sizes_the_grid_with_the_new_inputs_regions(app, tmp_path):
    zarr = pytest.importorskip("zarr")
    p = _page(STARDIST_NUCLEI_EXPANSION)
    p._auto_tile_strategy.setChecked(True)
    big = str(tmp_path / "big.zarr")
    small = str(tmp_path / "small.zarr")
    zarr.open(big, mode="w", shape=(58320, 28800, 2), chunks=(4096, 4096, 2), dtype="u2")
    zarr.open(small, mode="w", shape=(2000, 2000, 2), chunks=(1000, 1000, 2), dtype="u2")
    p._fuse_items = [{"zarr": big, "run": "r1", "rois": [], "regions": {"A": big}},
                     {"zarr": small, "run": "r2", "rois": [], "regions": {"A": small}}]
    p._use_fuse_item(0)
    p._use_fuse_item(1)
    assert p._rows_spin.value() * p._cols_spin.value() <= 2      # not held up by the old big region
