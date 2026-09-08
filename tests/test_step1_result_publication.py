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


def test_a_failed_publish_still_leaves_the_previous_result(tmp_path, monkeypatch):
    """The dangerous instant is the publish itself.

    Deleting the old store and then renaming the new one in means a rename that
    fails — a full disk, a cross-device move, a killed process — destroys the
    previous result and puts nothing in its place. The old store is therefore
    moved aside, not deleted, and put back when the publish fails.
    """
    first = _worker(tmp_path, hash_="first")
    first.run()
    path = str(tmp_path / "fused.zarr")
    before = np.array(zarr.open(path, mode="r")[:])

    real_replace = os.replace

    def _fail_the_publish(src, dst, *a, **k):
        if str(dst) == path:
            raise OSError("no space left on device")
        return real_replace(src, dst, *a, **k)
    monkeypatch.setattr(os, "replace", _fail_the_publish)

    second = _worker(tmp_path, hash_="second")
    errors = []
    second.error.connect(errors.append)
    second.run()

    assert errors                                   # it reported the failure
    z = zarr.open(path, mode="r")
    assert z.attrs["config_hash"] == "first"        # previous result still there
    assert z.attrs["complete"] is True
    assert np.array_equal(np.array(z[:]), before)
    assert not os.path.isdir(path + ".inprogress")
    assert not os.path.isdir(path + ".previous")


def test_a_result_left_aside_by_a_killed_run_comes_back(tmp_path):
    """A process killed between moving the old store aside and moving the new
    one in leaves the previous result under `.previous`; the next run returns
    it rather than starting from no previous result at all."""
    first = _worker(tmp_path, hash_="first")
    first.run()
    path = str(tmp_path / "fused.zarr")
    os.rename(path, path + ".previous")             # what that kill leaves

    from block01.ui.step0.overview_panel import FullFusionWorker
    FullFusionWorker._recover_interrupted_publish(path)

    assert zarr.open(path, mode="r").attrs["config_hash"] == "first"
    assert not os.path.isdir(path + ".previous")


def _restore_window(app, tmp_path):
    """A window whose loaded configuration can describe its own identity."""
    from block01.ui import main_window as mw
    from block01.ui.main_window import MainWindow

    raw = tmp_path / "raw.ome.tif"
    raw.write_bytes(b"x" * 8)
    w = MainWindow()
    w.loader = _Loader()
    w.loader.filepath = str(raw)
    w._rois = []
    w._active_roi = None
    w.step0_output = {"roi_id": "roi-1", "handoff_schema_version": 2}
    w.config.set_channels(["DAPI", "CD3"])
    w.config.load_panel({"g": ["CD3"]}, "DAPI")
    w.config.set_nucleus("DAPI", 1.0)
    w.config.set_channel_weight("CD3", 0.5)
    w.config._edited_channels.add("CD3")
    w._active_segmentation_method = "cellpose_wholecell_fusion"
    mw.OUTPUT_DIR = str(tmp_path)
    return w


def _store(tmp_path, attrs, name="fused.zarr"):
    path = str(tmp_path / name)
    z = zarr.open(path, mode="w", shape=(16, 16, 2), dtype="uint16")
    z.attrs["cellpose_channels"] = [1, 2]
    for k, v in attrs.items():
        z.attrs[k] = v
    return path


def _stamp_of(w):
    """What a completed run of the window's CURRENT configuration would stamp."""
    meta = w._expected_meta_for_current_config()
    return {"complete": True,
            "artifact_kind": meta.get("artifact_kind"),
            "fusion_formula_version": meta.get("fusion_formula_version"),
            "config_hash": meta.get("config_hash")}


def test_a_session_pointing_at_a_missing_store_is_not_step1_done(app, tmp_path):
    w = _restore_window(app, tmp_path)
    try:
        assert w._restorable_fused_zarr(str(tmp_path / "gone.zarr")) == ""
    finally:
        w.close()


def test_a_session_store_that_proves_itself_is_restored(app, tmp_path):
    w = _restore_window(app, tmp_path)
    try:
        path = _store(tmp_path, _stamp_of(w))
        assert w._restorable_fused_zarr(path) == path
    finally:
        w.close()


@pytest.mark.parametrize("change,why", [
    ({"complete": False}, "never finished"),
    ({"artifact_kind": "step1_dapi_input_zarr"}, "the other kind of artifact"),
    ({"fusion_formula_version": 1}, "an older formula"),
    ({"config_hash": ""}, "no configuration recorded"),
    ({"config_hash": "unrelated-config"}, "another configuration entirely"),
])
def test_a_session_store_that_cannot_prove_itself_is_refused(app, tmp_path, change, why):
    w = _restore_window(app, tmp_path)
    try:
        stamp = _stamp_of(w)
        stamp.update(change)
        path = _store(tmp_path, stamp)
        assert w._restorable_fused_zarr(path) == "", why
    finally:
        w.close()


def test_a_session_store_from_other_weights_is_refused(app, tmp_path):
    """Formula version 2 is not enough. A result made at other weights is a
    different picture, and Step2 would be handed it as this session's."""
    w = _restore_window(app, tmp_path)
    try:
        path = _store(tmp_path, _stamp_of(w))
        assert w._restorable_fused_zarr(path) == path

        w.config.set_channel_weight("CD3", 0.9)
        w.config._edited_channels.add("CD3")
        assert w._restorable_fused_zarr(path) == ""
    finally:
        w.close()


def test_a_store_with_nothing_to_say_is_refused(app, tmp_path):
    w = _restore_window(app, tmp_path)
    try:
        path = _store(tmp_path, {})
        assert w._restorable_fused_zarr(path) == ""
    finally:
        w.close()
