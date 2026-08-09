"""Step1 -> Step2 Input Data auto-sync (regression).

Bug: entering Step2 after finishing Step1 did NOT auto-populate the Input Data
fields (fused.zarr + Segmentation Index). Root cause: `_go_to_step2` performed
the sync only inside `if self.is_sequential_flow and self.step1_output:`, but
`is_sequential_flow` is never set True (the sequential "Next" button was removed),
so the sync block was permanently dead — for EVERY segmentation method.

Fix: sync whenever `step1_output` exists (not gated on is_sequential_flow), and
fill the two Input Data fields only when still empty (never clobber a manual
override / re-entry). Qt offscreen.
"""

import os

import pytest

pytest.importorskip("PyQt5")
zarr = pytest.importorskip("zarr")
import numpy as np  # noqa: E402


@pytest.fixture(scope="module")
def app():
    from PyQt5 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture()
def window(app, monkeypatch):
    from block01.ui.main_window import MainWindow
    w = MainWindow()
    monkeypatch.setattr(type(w), "_load_step0_roi_result",
                        lambda self, *a, **k: None)
    w._current_step = 1
    yield w
    w.close()


def _fused_zarr(tmp_path):
    """A minimal fused.zarr array so set_zarr_path -> _load_zarr_info succeeds."""
    path = str(tmp_path / "fused.zarr")
    z = zarr.open(path, mode="w", shape=(2, 32, 32), chunks=(1, 32, 32),
                  dtype="float32")
    z[:] = np.ones((2, 32, 32), dtype="float32")
    return path


def _seg_index(tmp_path, method):
    """Write a real segmentation_params_index.json under output_dir."""
    from block01.utils.segmentation_params import (
        save_segmentation_params, params_index_path)
    out_dir = str(tmp_path)
    save_segmentation_params(out_dir, {"method": method})
    assert os.path.exists(params_index_path(out_dir))
    return out_dir


def _step1_output(tmp_path, method):
    return {
        "zarr_path": _fused_zarr(tmp_path),
        "output_dir": _seg_index(tmp_path, method),
        "step2_dir": str(tmp_path / "step2"),
        "fusion_config_path": "",
        "roi_info": [],
        "roi_id": "", "roi_dir": "",
    }


# ── the fix: both Input Data fields auto-sync on entering Step2 ───────────────
@pytest.mark.parametrize("method", [
    "cellpose_wholecell_fusion",   # the reported case
    "cellpose_nuclei_hq2",         # method-independent: others too
    "cellpose_nuclei_csd",
])
def test_entering_step2_autosyncs_input_data(window, tmp_path, method):
    # the flag the old gate depended on is (and stays) False in a normal run
    assert window.is_sequential_flow is False
    window.step1_output = _step1_output(tmp_path, method)
    # fields start empty
    assert window._step2._zarr_edit.text().strip() == ""
    assert window._step2._seg_params_edit.text().strip() == ""

    window._go_to_step2()

    # navigated to Step2
    assert window._stack.currentIndex() == 2
    # fused.zarr auto-populated
    assert window._step2._zarr_edit.text().strip() == window.step1_output["zarr_path"]
    # Segmentation Index auto-populated (resolves to the nested index file)
    seg = window._step2._seg_params_edit.text().strip()
    assert seg.endswith("segmentation_params_index.json")
    assert os.path.exists(seg)


# ── non-clobber: a manual override is preserved across re-entry ──────────────
def test_manual_override_not_clobbered(window, tmp_path):
    window.step1_output = _step1_output(tmp_path, "cellpose_wholecell_fusion")
    manual = "/some/manual/other.zarr"
    window._step2._zarr_edit.setText(manual)
    window._step2._seg_params_edit.setText("/manual/index.json")

    window._go_to_step2()

    assert window._step2._zarr_edit.text().strip() == manual
    assert window._step2._seg_params_edit.text().strip() == "/manual/index.json"


# ── no Step1 result -> no crash, fields stay empty ───────────────────────────
def test_no_step1_output_no_sync(window):
    window.step1_output = None
    window._go_to_step2()
    assert window._stack.currentIndex() == 2
    assert window._step2._zarr_edit.text().strip() == ""
