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
    """What a completed run of `meta` stamps into its own store."""
    return {"complete": True,
            "artifact_kind": meta.get("artifact_kind"),
            "fusion_formula_version": meta.get("fusion_formula_version"),
            "config_hash": meta.get("config_hash")}


def test_a_meta_without_a_config_hash_is_not_reused(app, tmp_path):
    w = _window(app, tmp_path)
    try:
        _make_zarr(tmp_path)
        # Exactly what a DAPI-input run leaves behind: regions, no hash.
        with open(tmp_path / "fusion_meta.json", "w", encoding="utf-8") as f:
            json.dump({"mode": "full_wsi", "regions": [], "created_at": "x"}, f)

        expected = w._expected_fused_zarr_meta(_worker_cfg(), "cellpose_wholecell_fusion")
        assert w._try_reuse_fused_zarr(expected) is False
    finally:
        w.close()


def test_a_dapi_run_is_not_served_as_the_whole_cell_fusion(app, tmp_path):
    w = _window(app, tmp_path)
    try:
        _make_zarr(tmp_path)
        dapi_meta = w._expected_dapi_input_meta(_worker_cfg(), "cellpose_nuclei_dapi")
        # A DAPI meta, complete and self-consistent, written where the fused
        # reuse check will find it.
        with open(tmp_path / "fusion_meta.json", "w", encoding="utf-8") as f:
            json.dump(dapi_meta, f, default=str)

        fused_expected = w._expected_fused_zarr_meta(
            _worker_cfg(), "cellpose_wholecell_fusion")
        assert w._try_reuse_fused_zarr(fused_expected) is False
    finally:
        w.close()


def test_a_whole_cell_run_is_not_served_as_the_dapi_input(app, tmp_path, monkeypatch):
    w = _window(app, tmp_path)
    try:
        _make_zarr(tmp_path)
        fused_meta = w._expected_fused_zarr_meta(
            _worker_cfg(), "cellpose_wholecell_fusion")
        with open(tmp_path / "fusion_meta.json", "w", encoding="utf-8") as f:
            json.dump({"mode": "full_wsi",
                       "regions": [{"zarr_path": str(tmp_path / "fused.zarr")}]},
                      f, default=str)
        with open(w._dapi_input_meta_path(), "w", encoding="utf-8") as f:
            json.dump(fused_meta, f, default=str)

        asked = []
        monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                            staticmethod(lambda *a, **k: asked.append(a) or
                                         QtWidgets.QMessageBox.Yes))
        dapi_expected = w._expected_dapi_input_meta(
            _worker_cfg(), "cellpose_nuclei_dapi")
        assert w._try_reuse_dapi_input_zarr(dapi_expected) is False
    finally:
        w.close()


def test_a_meta_that_matches_but_for_the_hash_is_not_reused(app, tmp_path):
    """Isolates the hash gate: same kind, same formula, hash simply absent."""
    w = _window(app, tmp_path)
    try:
        _make_zarr(tmp_path)
        expected = w._expected_fused_zarr_meta(_worker_cfg(), "cellpose_wholecell_fusion")
        without_hash = {k: v for k, v in expected.items() if k != "config_hash"}
        with open(tmp_path / "fusion_meta.json", "w", encoding="utf-8") as f:
            json.dump(without_hash, f, default=str)

        assert w._try_reuse_fused_zarr(expected) is False
    finally:
        w.close()


def test_a_meta_that_matches_but_for_the_kind_is_not_reused(app, tmp_path):
    """Isolates the kind gate: everything else, including the hash, agrees."""
    w = _window(app, tmp_path)
    try:
        _make_zarr(tmp_path)
        expected = w._expected_fused_zarr_meta(_worker_cfg(), "cellpose_wholecell_fusion")
        wrong_kind = dict(expected)
        wrong_kind["artifact_kind"] = "step1_dapi_input_zarr"
        with open(tmp_path / "fusion_meta.json", "w", encoding="utf-8") as f:
            json.dump(wrong_kind, f, default=str)

        assert w._try_reuse_fused_zarr(expected) is False
    finally:
        w.close()


def test_a_result_from_another_formula_is_not_reused(app, tmp_path):
    w = _window(app, tmp_path)
    try:
        _make_zarr(tmp_path)
        expected = w._expected_fused_zarr_meta(_worker_cfg(), "cellpose_wholecell_fusion")
        stale = dict(expected)
        stale["fusion_formula_version"] = expected["fusion_formula_version"] + 1
        with open(tmp_path / "fusion_meta.json", "w", encoding="utf-8") as f:
            json.dump(stale, f, default=str)

        assert w._try_reuse_fused_zarr(expected) is False
    finally:
        w.close()


def test_a_matching_result_is_still_reused(app, tmp_path, monkeypatch):
    w = _window(app, tmp_path)
    try:
        expected = w._expected_fused_zarr_meta(_worker_cfg(), "cellpose_wholecell_fusion")
        path = _make_zarr(tmp_path, stamp=_stamp(expected))
        with open(tmp_path / "fusion_meta.json", "w", encoding="utf-8") as f:
            json.dump(expected, f, default=str)

        done = []
        monkeypatch.setattr(type(w), "_on_fusion_done",
                            lambda self, p: done.append(p))
        assert w._try_reuse_fused_zarr(expected) is True
        assert done == [path]
    finally:
        w.close()


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


def test_a_store_that_never_finished_is_not_reused(app, tmp_path):
    """A run cancelled or crashed part-way leaves a store of the right shape at
    the right path, with the PREVIOUS run's meta still beside it. The sidecar
    then matches and the pixels are half old, half new — so the store itself has
    to say it finished before anything may be reused."""
    w = _window(app, tmp_path)
    try:
        expected = w._expected_fused_zarr_meta(_worker_cfg(), "cellpose_wholecell_fusion")
        stamp = _stamp(expected)
        stamp["complete"] = False
        _make_zarr(tmp_path, stamp=stamp)
        with open(tmp_path / "fusion_meta.json", "w", encoding="utf-8") as f:
            json.dump(expected, f, default=str)

        assert w._try_reuse_fused_zarr(expected) is False
    finally:
        w.close()


def test_a_store_stamped_as_the_other_kind_is_not_reused(app, tmp_path):
    """The whole-cell and DAPI-input runs write the same filename. A sidecar
    can be replaced; what the store says about itself cannot."""
    w = _window(app, tmp_path)
    try:
        expected = w._expected_fused_zarr_meta(_worker_cfg(), "cellpose_wholecell_fusion")
        stamp = _stamp(expected)
        stamp["artifact_kind"] = "step1_dapi_input_zarr"
        _make_zarr(tmp_path, stamp=stamp)
        with open(tmp_path / "fusion_meta.json", "w", encoding="utf-8") as f:
            json.dump(expected, f, default=str)

        assert w._try_reuse_fused_zarr(expected) is False
    finally:
        w.close()


def test_a_store_made_by_an_older_formula_is_not_reused(app, tmp_path):
    """Even with a matching sidecar: the pixels were made by other arithmetic."""
    w = _window(app, tmp_path)
    try:
        expected = w._expected_fused_zarr_meta(_worker_cfg(), "cellpose_wholecell_fusion")
        stamp = _stamp(expected)
        stamp["fusion_formula_version"] = 1
        _make_zarr(tmp_path, stamp=stamp)
        with open(tmp_path / "fusion_meta.json", "w", encoding="utf-8") as f:
            json.dump(expected, f, default=str)

        assert w._try_reuse_fused_zarr(expected) is False
    finally:
        w.close()
