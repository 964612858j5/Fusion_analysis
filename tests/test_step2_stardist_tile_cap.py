"""StarDist runs a whole tile at once and crashed (std::bad_alloc) on a
~150 MP tile (2026-10-25): with StarDist the grid is split until no tile's
read window exceeds the cap -- when the method is chosen, and again at Run."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("block01.qt")
from block01.qt import QtWidgets  # noqa: E402

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
    from block01.qt.QtWidgets import QMessageBox
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


# ── the CUDA card's memory (docs/v17_stardist_vram_tile_plan.md) ──

from block01.utils import step2_units as U  # noqa: E402
from block01.utils.segmentation_config import HYBRID_CELLPOSE_STARDIST  # noqa: E402


@pytest.fixture
def vram(monkeypatch):
    """Pretend the engine's card has `set(gib)` GiB (None: no CUDA card)."""
    from block01.ui.step2_page import Step2Page

    def put(gib):
        monkeypatch.setattr(Step2Page, "_engine_vram_cache", [gib])
        monkeypatch.setattr(Step2Page, "_engine_vram_how", ["test"])
    put(None)
    return put


def test_the_cap_follows_the_cards_memory():
    mp = lambda m, g, gpu=True: U.stardist_cap_px(m, g, gpu) / 1e6
    assert mp("stardist_nuclei_expansion", None) == 60           # unknown / not CUDA: the host cap
    assert mp("stardist_nuclei_expansion", 6.0, gpu=False) == 60 # CPU run
    assert 18 < mp("stardist_nuclei_expansion", 6.0) < 19        # (6 x .75 - 1) GiB / 200 B
    assert 10 < mp(HYBRID_CELLPOSE_STARDIST, 6.0) < 11           # + Cellpose's 1.5 GiB
    assert 38 < mp("stardist_nuclei_expansion", 11.0) < 40
    assert mp("stardist_nuclei_expansion", 48.0) == 60
    assert mp(HYBRID_CELLPOSE_STARDIST, 2.0) == U.STARDIST_MIN_CAP_PX / 1e6
    assert U.engine_cap_px("stardist_nuclei_dapi", 5_000_000) == 5_000_000
    assert U.engine_cap_px("cellpose_wholecell_fusion", 5_000_000) == U.CAP_PX["cellpose"]


def test_a_6_gib_card_splits_the_laptops_wsi_grid_by_read_windows(app, vram):
    vram(6.0)
    p = _page(HYBRID_CELLPOSE_STARDIST)
    p._full_h, p._full_w = 15437, 16215                  # the laptop's Full WSI, 2026-11-18
    cap = p._stardist_cap_px()
    assert cap < 11_000_000
    assert p._largest_tile_px(4, 4) > cap               # the grid that ran out of memory
    nr, nc = p._stardist_safe_grid(4, 4)
    assert p._largest_tile_px(nr, nc) <= cap and nr * nc > 16
    p._cp_gpu.setChecked(False)                          # CPU: the host cap again
    assert p._stardist_cap_px() == p.STARDIST_MAX_TILE_PX


def test_ticking_the_gpu_rechecks_the_grid(app, vram):
    vram(6.0)
    p = _page(STARDIST_NUCLEI_EXPANSION)
    p._full_h, p._full_w = 15437, 16215
    p._cp_gpu.setChecked(False)
    p._set_grid(4, 4)                                    # fits the host cap
    p._cp_gpu.setChecked(True)
    assert p._largest_tile_px(p._rows_spin.value(), p._cols_spin.value()) <= p._stardist_cap_px()
    assert (p._rows_spin.value(), p._cols_spin.value()) != (4, 4)


def test_auto_suggests_a_grid_whose_read_windows_fit(app, vram):
    """codex r1: the suggestion sizes own areas (no overlap, 20 % over)."""
    vram(6.0)
    p = _page(HYBRID_CELLPOSE_STARDIST)
    p._full_h, p._full_w = 12000, 12000
    p._auto_tile_strategy.setChecked(True)
    p._refresh_tile_suggestion(apply=True)
    s = p._suggested_tile_strategy
    assert (p._rows_spin.value(), p._cols_spin.value()) == (s["n_rows"], s["n_cols"])
    assert p._largest_tile_px(s["n_rows"], s["n_cols"], overlap=s["overlap"]) <= p._stardist_cap_px()


def test_the_run_records_the_cap_it_was_decided_with(app, vram):
    vram(6.0)
    p = _page(STARDIST_NUCLEI_EXPANSION)
    rec = p._stardist_cap_record()
    assert rec["stardist_cap_px"] == p._stardist_cap_px()
    assert rec["stardist_cap_source"] == {"use_gpu": True, "vram_gib": 6.0, "how": "test"}


def test_no_nvidia_smi_means_no_card(monkeypatch):
    import subprocess
    import sys
    from block01.ui.step2_page import Step2Page
    monkeypatch.setattr(Step2Page, "_engine_vram_cache", [])
    monkeypatch.setattr(Step2Page, "_engine_vram_how", [])
    monkeypatch.setitem(sys.modules, "torch", None)
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)

    def run(cmd, **kw):
        raise FileNotFoundError("nvidia-smi")
    monkeypatch.setattr(subprocess, "run", run)
    assert Step2Page._engine_vram_gib() is None


@pytest.mark.parametrize("visible, smi, want", [
    (None, "6144\n24576\n", 6.0),                        # two cards, numbering unsure: the smaller
    ("1", "6144\n24576\n", 6.0),                         # an index is CUDA's numbering: still the smaller
    ("", "6144\n", None),                                # no card visible
    ("GPU-abc", "11264\n", 11.0),                        # a UUID: asked for by name
    ("-1", "6144\n", None),                              # CUDA hidden
])
def test_the_engines_card_is_found_without_torch(monkeypatch, visible, smi, want):
    import subprocess
    import sys
    from block01.ui.step2_page import Step2Page
    monkeypatch.setattr(Step2Page, "_engine_vram_cache", [])
    monkeypatch.setattr(Step2Page, "_engine_vram_how", [])
    monkeypatch.setitem(sys.modules, "torch", None)
    if visible is None:
        monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    else:
        monkeypatch.setenv("CUDA_VISIBLE_DEVICES", visible)
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=smi, stderr="")
    monkeypatch.setattr(subprocess, "run", run)
    assert Step2Page._engine_vram_gib() == want
    if visible in ("", "-1"):
        assert seen == []                                # CUDA hidden: nvidia-smi not asked
    if visible == "GPU-abc":
        assert seen and seen[0][1:3] == ["-i", "GPU-abc"]


def test_an_overlap_that_alone_fills_the_limit_is_refused_not_looped(app, vram, monkeypatch):
    """codex r2: a 4 GiB card, the hybrid, a 1000 px overlap -- no grid fits;
    the search stops and the Run says so instead of splitting for ever."""
    from block01.qt.QtWidgets import QMessageBox
    vram(4.0)
    p = _page(HYBRID_CELLPOSE_STARDIST)
    p._full_h, p._full_w = 12000, 12000
    p._overlap_spin.setValue(1000)
    assert p._stardist_cap_px() < 3_000_000
    assert p._stardist_safe_grid(4, 4) is None
    said = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: said.append(a[2])))
    p._set_grid(4, 4)
    assert p._stardist_grid_ok_for_run() is False
    assert said and "Lower the overlap" in said[0]
    assert p._keep_stardist_tiles_safe() is False
    assert "no tile grid fits" in p._tile_suggestion_lbl.text()


def test_a_grid_that_fits_is_still_found_with_a_wide_overlap(app, vram):
    """codex r3: 4 GiB, hybrid, 12000^2, overlap 600 -- 28x28 fits; not refused."""
    vram(4.0)
    p = _page(HYBRID_CELLPOSE_STARDIST)
    p._full_h, p._full_w = 12000, 12000
    p._overlap_spin.setValue(600)
    nr, nc = p._stardist_safe_grid(4, 4)
    assert p._largest_tile_px(nr, nc) <= p._stardist_cap_px()
