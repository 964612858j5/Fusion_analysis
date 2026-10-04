"""A Step1 output is only reused when it can prove it matches.

Three ways a stale result used to be served as a current one:
  * `fusion_meta.json` with no `config_hash` was reused "because the zarr is
    valid" — and a DAPI-input run leaves exactly such a meta;
  * the whole-cell and DAPI-input runs write the same `fused_<roi>.zarr`, so
    one could be picked up as the other;
  * nothing recorded which fusion arithmetic made the pixels, so a formula
    change could not invalidate anything.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import json
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")
zarr = pytest.importorskip("zarr")

from PyQt5 import QtWidgets  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    """A reuse decision that wrongly says yes must fail the test, not hang it.

    Saying yes ends in `_on_fusion_done`, which pops a modal summary box; under
    offscreen Qt that blocks for ever and a red test looks like a hung one.
    """
    for name, answer in (("information", None), ("critical", None),
                         ("warning", None),
                         ("question", QtWidgets.QMessageBox.Yes)):
        monkeypatch.setattr(QtWidgets.QMessageBox, name,
                            staticmethod(lambda *a, _r=answer, **k: _r))


class _Loader:
    shape = (64, 64)

    def __init__(self, path):
        self.filepath = path
        self._names = ["DAPI", "CD3"]
        self.ch_map = {c: i for i, c in enumerate(self._names)}

    def channel_names(self):
        return list(self._names)

    def read_region(self, ch, y0, y1, x0, x1, downsample=1, normalize=True):
        return np.zeros((y1 - y0, x1 - x0), np.float32)


def _worker_cfg():
    return {
        "nucleus": {"channel": "DAPI", "weight": 1.0},
        "groups": {"g": {"group_weight": 1.0, "channels": {"CD3": 0.5}}},
        "norm_low": 1.0, "norm_high": 99.5,
        "channel_remap_params": {},
        "resolution": None,
    }


def _window(app, tmp_path):
    from block01.ui import main_window as mw
    from block01.ui.main_window import MainWindow

    raw = tmp_path / "raw.ome.tif"
    raw.write_bytes(b"x" * 8)
    w = MainWindow()
    w.loader = _Loader(str(raw))
    w._rois = []
    w._active_roi = None
    w.step0_output = {"roi_id": "roi-1", "handoff_schema_version": 2}
    mw.OUTPUT_DIR = str(tmp_path)
    return w


def _make_zarr(tmp_path, name="fused.zarr", stamp=None):
    """A fused store on disk. `stamp` is what the store says about itself: the
    worker writes those attrs last, and a reader may only believe a store that
    carries them."""
    path = str(tmp_path / name)
    z = zarr.open(path, mode="w", shape=(64, 64, 2), dtype="uint16")
    z[:] = 1
    z.attrs["cellpose_channels"] = [1, 2]
    for key, value in (stamp or {}).items():
        z.attrs[key] = value
    return path


def _stamp(meta):
    """What a completed run of `meta` stamps into its own store: the identity of
    the run, and which region these pixels are."""
    region = (meta.get("regions") or [{}])[0]
    return {"complete": True,
            "artifact_kind": meta.get("artifact_kind"),
            "fusion_formula_version": meta.get("fusion_formula_version"),
            "config_hash": meta.get("config_hash"),
            "roi_name": region.get("roi_name") or "",
            "bbox_fullres": list(region.get("roi_bbox") or [])}


def test_changing_the_weights_invalidates_the_dapi_input_too(app, tmp_path):
    """Channel 0 of that file is a full marker fusion, so weights matter."""
    w = _window(app, tmp_path)
    try:
        base = w._expected_dapi_input_meta(_worker_cfg(), "cellpose_nuclei_dapi")
        other_cfg = _worker_cfg()
        other_cfg["groups"]["g"]["channels"]["CD3"] = 0.9
        other = w._expected_dapi_input_meta(other_cfg, "cellpose_nuclei_dapi")

        assert w._dapi_meta_compare_view(base) != w._dapi_meta_compare_view(other)
    finally:
        w.close()


def test_changing_the_display_mapping_invalidates_both(app, tmp_path, monkeypatch):
    w = _window(app, tmp_path)
    try:
        monkeypatch.setattr(type(w), "_load_step0_remap_params",
                            lambda self: ({"CD3": {"min": 0.0, "max": 1.0}}, "p"))
        a_fused = w._expected_fused_zarr_meta(_worker_cfg(), "cellpose_wholecell_fusion")
        a_dapi = w._expected_dapi_input_meta(_worker_cfg(), "cellpose_nuclei_dapi")

        monkeypatch.setattr(type(w), "_load_step0_remap_params",
                            lambda self: ({"CD3": {"min": 0.0, "max": 0.4}}, "p"))
        b_fused = w._expected_fused_zarr_meta(_worker_cfg(), "cellpose_wholecell_fusion")
        b_dapi = w._expected_dapi_input_meta(_worker_cfg(), "cellpose_nuclei_dapi")

        assert a_fused["config_hash"] != b_fused["config_hash"]
        assert w._dapi_meta_compare_view(a_dapi) != w._dapi_meta_compare_view(b_dapi)
    finally:
        w.close()


def _two_roi_window(app, tmp_path):
    w = _window(app, tmp_path)
    w._rois = [{"name": "ROI_1", "bbox_fullres": [0, 64, 0, 64]},
               {"name": "ROI_2", "bbox_fullres": [0, 64, 0, 64]}]
    w._active_roi = w._rois[0]
    return w


def _two_roi_meta(w, tmp_path):
    expected = w._expected_fused_zarr_meta(_worker_cfg(), "cellpose_wholecell_fusion")
    on_disk = dict(expected)
    on_disk["regions"] = [
        {"roi_name": "ROI_1", "zarr_path": str(tmp_path / "fused_ROI_1.zarr"),
         "zarr_shape": [64, 64, 2]},
        {"roi_name": "ROI_2", "zarr_path": str(tmp_path / "fused_ROI_2.zarr"),
         "zarr_shape": [64, 64, 2]},
    ]
    with open(tmp_path / "fusion_meta.json", "w", encoding="utf-8") as f:
        json.dump(on_disk, f, default=str)
    return expected


def _region_stamp(expected, roi_name, bbox=(0, 64, 0, 64)):
    """What the worker writes into one region's store: the identity of the run
    plus which region these pixels are."""
    stamp = _stamp(expected)
    stamp["roi_name"] = roi_name
    stamp["bbox_fullres"] = list(bbox)
    return stamp


