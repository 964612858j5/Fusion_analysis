"""Block PA-5b fix 2b (user ruling 2026-10-06): Generate samples Step2's
nucleus overview from the pixels it writes and saves it beside the store;
Step2 uses it, off the GUI thread, without reading the store again."""

import os
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")
zarr = pytest.importorskip("zarr")

from PyQt5 import QtWidgets  # noqa: E402

from block01.core import nucleus_overview as nov  # noqa: E402
from test_v16_pa_fusion_parity import POLY, _run  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _store(tmp_path, name, rois=None):
    sub = "fused.zarr" if rois is None else f"fused_{rois[0]['name']}.zarr"
    return str(tmp_path / name / sub)


@pytest.mark.parametrize("long_side", [16, 20, 7, 4096])
@pytest.mark.parametrize("chunk,wide", [(8, 1), (16, 3), (5, 2)])
@pytest.mark.parametrize("poly", [False, True])
def test_the_saved_overview_is_the_old_strided_read(tmp_path, monkeypatch, long_side,
                                                   chunk, wide, poly):
    monkeypatch.setattr(nov, "OVERVIEW_LONG_SIDE", long_side)
    rois = ([{"name": "R", "bbox_fullres": [2, 50, 1, 69], "polygon_fullres": POLY}]
            if poly else None)
    _run(tmp_path, "g", chunk, wide, rois=rois)
    z = zarr.open(_store(tmp_path, "g", rois), mode="r")
    ds = nov.stride(z.shape)
    saved = np.load(nov.path_for(_store(tmp_path, "g", rois)))
    assert saved.dtype == np.uint16
    assert np.array_equal(saved, np.asarray(z[::ds, ::ds, 1]))
    assert np.array_equal(nov.load(_store(tmp_path, "g", rois), z.shape), saved)


def _wait(app, pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end and not pred():
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    return pred()


def test_step2_draws_the_saved_overview_without_reading_the_store(app, tmp_path, monkeypatch):
    from block01.ui.step2_page import Step2Page
    _run(tmp_path, "g", 8, 1)
    path = _store(tmp_path, "g")
    nov._MEMORY.clear()                                  # as after a restart
    monkeypatch.setattr(nov, "read_from_store",
                        lambda z: (_ for _ in ()).throw(AssertionError("store read")))
    p = Step2Page()
    try:
        p._zarr_edit.setText(path)
        p._load_zarr_info()
        assert p._ov_ds == nov.stride((53, 71)) and p._tile_rects   # grid at once
        assert _wait(app, lambda: p._ov_status.text().startswith("Overview "))
        assert p._ov_img.image is not None and p._ov_img.image.shape == (p._ov_h, p._ov_w)
    finally:
        p.deleteLater()


def test_an_older_store_is_read_off_the_gui_thread_and_not_written_to(app, tmp_path):
    from block01.ui.step2_page import Step2Page
    _run(tmp_path, "g", 8, 1)
    path = _store(tmp_path, "g")
    os.remove(nov.path_for(path))                        # saved before this existed
    nov._MEMORY.clear()
    p = Step2Page()
    try:
        p._zarr_edit.setText(path)
        p._load_zarr_info()
        assert _wait(app, lambda: p._ov_status.text().startswith("Overview "))
        assert not os.path.exists(nov.path_for(path))    # the published run is untouched
    finally:
        p.deleteLater()


def test_a_late_overview_of_another_store_is_dropped(app, tmp_path):
    from block01.ui.step2_page import Step2Page
    p = Step2Page()
    try:
        p._ov_token = object()
        p._on_overview_ready((object(), np.ones((3, 3), np.float32), None))
        assert p._ov_img.image is None
    finally:
        p.deleteLater()


def test_a_cleared_input_drops_the_pending_overview(app):
    from block01.ui.step2_page import Step2Page
    p = Step2Page()
    try:
        token = p._ov_token = object()
        p._use_fuse_item(-1)                             # e.g. its run was deleted
        p._on_overview_ready((token, np.ones((3, 3), np.float32), None))
        assert p._ov_img.image is None
    finally:
        p.deleteLater()


def test_a_replaced_store_never_shows_its_old_overview(tmp_path, monkeypatch):
    _run(tmp_path, "g", 8, 1)
    path = _store(tmp_path, "g")
    assert os.path.isfile(nov.path_for(path))
    monkeypatch.setattr(nov, "save", lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
    _run(tmp_path, "g", 8, 1)                            # the same path, save fails
    assert not os.path.exists(nov.path_for(path))
    assert nov.load(path, (53, 71, 2)) is None           # -> Step2 reads the store
