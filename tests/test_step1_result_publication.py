"""A Step1 result is published atomically and only believed when it says so.

Two ways a result that was not what it claimed used to reach Step2:

  * the worker wrote the real store in place, so a run that was cancelled or
    crashed left a half-written store of the right shape beside the previous
    run's still-matching meta file;
  * restoring a session took the recorded path on trust — no existence check,
    no kind, no formula version — which was a way around every gate the Save
    path applies.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

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


class _Loader:
    shape = (16, 16)

    def __init__(self):
        self.ch_map = {"DAPI": 0, "CD3": 1}

    def channel_names(self):
        return ["DAPI", "CD3"]

    def read_region(self, ch, y0, y1, x0, x1, downsample=1, normalize=True):
        return np.full((y1 - y0, x1 - x0), 100.0, np.float32)


def _worker(tmp_path, n_rows=1, n_cols=1, kind="step1_fused_zarr", hash_="h1"):
    from block01.ui.step0.overview_panel import FullFusionWorker

    cfg = {
        "ome_tiff": "/x/raw.ome.tif",
        "output_dir": str(tmp_path),
        "nucleus": {"channel": "DAPI", "weight": 1.0},
        "groups": {"g": {"group_weight": 1.0, "channels": {"CD3": 1.0}}},
        "channel_remap_params": {
            "DAPI": {"min": 0.0, "max": 255.0, "gamma": 1.0},
            "CD3": {"min": 0.0, "max": 255.0, "gamma": 1.0},
        },
        "artifact_kind": kind,
        "config_hash": hash_,
    }
    return FullFusionWorker(loader=_Loader(), fusion_cfg=cfg,
                            n_rows=n_rows, n_cols=n_cols, rois=None)


def test_a_finished_store_says_what_made_it(tmp_path):
    wk = _worker(tmp_path)
    wk.run()

    z = zarr.open(str(tmp_path / "fused.zarr"), mode="r")
    assert z.attrs["complete"] is True
    assert z.attrs["artifact_kind"] == "step1_fused_zarr"
    assert z.attrs["config_hash"] == "h1"
    assert z.attrs["fusion_formula_version"] == 2


def test_a_stopped_run_leaves_the_previous_result_alone(tmp_path):
    """The real path must still hold the previous, complete result: publishing
    is a rename after the last tile, not a write into the live store."""
    first = _worker(tmp_path, hash_="first")
    first.run()
    path = str(tmp_path / "fused.zarr")
    before = np.array(zarr.open(path, mode="r")[:])

    # Stop it AFTER the store exists and a tile has been written — stopping
    # before the first tile would never touch the previous result and would
    # prove nothing about where the tiles are written.
    second = _worker(tmp_path, n_rows=4, hash_="second")
    errors = []
    second.error.connect(errors.append)
    real_fuse = second._fuse_tile
    written = []

    def _fuse_then_stop(*a, **k):
        out = real_fuse(*a, **k)
        written.append(1)
        if len(written) >= 2:
            second._stop = True
        return out
    second._fuse_tile = _fuse_then_stop
    second.run()

    assert len(written) >= 2                        # it really did write tiles

    z = zarr.open(path, mode="r")
    assert errors                                   # it reported the stop
    assert z.attrs["config_hash"] == "first"        # untouched
    assert np.array_equal(np.array(z[:]), before)
    assert not os.path.isdir(path + ".inprogress")  # and cleaned up after itself


def test_a_session_pointing_at_a_missing_store_is_not_step1_done(app, tmp_path):
    from block01.ui.main_window import MainWindow

    w = MainWindow()
    try:
        assert w._restorable_fused_zarr(str(tmp_path / "gone.zarr")) == ""
    finally:
        w.close()


@pytest.mark.parametrize("attrs,why", [
    ({"complete": False, "artifact_kind": "step1_fused_zarr",
      "fusion_formula_version": 2, "config_hash": "h"}, "never finished"),
    ({"complete": True, "artifact_kind": "corrected_channels_zarr",
      "fusion_formula_version": 2, "config_hash": "h"}, "another kind"),
    ({"complete": True, "artifact_kind": "step1_fused_zarr",
      "fusion_formula_version": 1, "config_hash": "h"}, "the old formula"),
    ({"cellpose_channels": [1, 2]}, "nothing at all"),
])
def test_a_session_store_that_cannot_prove_itself_is_refused(app, tmp_path, attrs, why):
    from block01.ui.main_window import MainWindow

    path = str(tmp_path / "fused.zarr")
    z = zarr.open(path, mode="w", shape=(16, 16, 2), dtype="uint16")
    z.attrs["cellpose_channels"] = [1, 2]
    for k, v in attrs.items():
        z.attrs[k] = v

    w = MainWindow()
    try:
        assert w._restorable_fused_zarr(path) == "", why
    finally:
        w.close()


def test_a_session_store_that_proves_itself_is_restored(app, tmp_path):
    from block01.ui.main_window import MainWindow

    path = str(tmp_path / "fused.zarr")
    z = zarr.open(path, mode="w", shape=(16, 16, 2), dtype="uint16")
    z.attrs["cellpose_channels"] = [1, 2]
    z.attrs["complete"] = True
    z.attrs["artifact_kind"] = "step1_fused_zarr"
    z.attrs["fusion_formula_version"] = 2
    z.attrs["config_hash"] = "h"

    w = MainWindow()
    try:
        assert w._restorable_fused_zarr(path) == path
    finally:
        w.close()
