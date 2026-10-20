"""Step2's zoomable DAPI picture (docs/f5_tma_display_plan.md part 3): a
filtered `Step1WholeSlideMount` that composes only the nucleus channel,
never on the CPU (it closes back to the old overview), never draws its
rectangle on the Tissue preview and has its own raw-texture budget; the
viewer around it is used only for the workspace's own input and is not
tried again after a failure; the tile grid is drawn and clicked in slide
pixels. On the takeover rig (synthetic pyramid, real mount and view)."""

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")
pytest.importorskip("pyqtgraph")

from PyQt5 import QtCore, QtWidgets  # noqa: E402

from test_step1_gpu_takeover import (  # noqa: E402
    _Window, _domain, _settle, _stack_factory, _state, app,  # noqa: F401
)

from block01.ui.step1_viewer_host import Step1ViewerHost  # noqa: E402
from block01.ui.step1_viewer_mount import BACKEND_GPU, Step1WholeSlideMount  # noqa: E402
from block01.ui.step2_viewer import STEP2_RAW_TEXTURE_BYTES, Step2DapiViewer, _GridClicks  # noqa: E402


class _Broken:
    def attach(self, _view):
        raise RuntimeError("no OpenGL here")

    def dispose(self):
        pass

    def setParent(self, _p):
        pass

    def deleteLater(self):
        pass


def _rig_fused():
    import json
    import tempfile
    d = tempfile.mkdtemp(prefix="f5s2_")
    with open(os.path.join(d, "roi_config.json"), "w") as f:
        json.dump([{"bbox_fullres": [256, 1280, 256, 1280], "polygon_fullres": None}], f)
    return os.path.join(d, "fused.zarr")


def _rig(app, *, broken=True, visible=("CD3", "CD8")):
    raws, schedulers = [], []
    window = _Window(_state(visible=visible), _domain())
    host = Step1ViewerHost(stack_factory=_stack_factory(raws, schedulers))
    host.setAttribute(QtCore.Qt.WA_DontShowOnScreen, True)
    host.resize(530, 530)
    host.show()
    holder = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(holder)
    legacy = QtWidgets.QLabel("old overview")
    lay.addWidget(legacy)
    page = SimpleNamespace(_zarr_path=_rig_fused(), _rois=[], _core_items=lambda: [],
                           btn_tma_cores=SimpleNamespace(isChecked=lambda: True),
                           overview_layout=lambda: lay, overview_widget=lambda: legacy,
                           tile_items=lambda: [], tile_at=lambda y, x: None,
                           _on_tile_clicked=lambda r, c: None)
    viewer = Step2DapiViewer(window, page)
    mount = Step1WholeSlideMount(window, host=host, display_filter=viewer.dapi_spec, navigator=False,
                                 raw_texture_bytes=STEP2_RAW_TEXTURE_BYTES,
                                 gpu_layer_factory=(lambda _s: _Broken()) if broken else None)
    mount.install(lay, legacy)
    viewer.mount = mount
    return SimpleNamespace(window=window, host=host, mount=mount, viewer=viewer, legacy=legacy,
                           raws=raws, holder=holder)


def _done(rig):
    for raw in rig.raws:
        raw.release_all()
    rig.mount.close()
    QtWidgets.QApplication.processEvents()


def test_the_filtered_spec_is_the_nucleus_alone_in_its_step1_colour_and_window(app):
    rig = _rig(app)
    try:
        spec = rig.mount._spec()
        assert spec["weights"] == {"DAPI": 1.0}                 # CD3 / CD8 ticked: not drawn
        assert set(spec["colors"]) == {"DAPI"} and spec["colors"]["DAPI"] == (0.0, 0.0, 1.0)
        assert set(spec["mappings"]) == {"DAPI"}
        snap = rig.mount._gpu_display_snapshot()
        assert set(snap.weights) == {"DAPI"} and snap.mode == "overlay"
    finally:
        _done(rig)


def test_without_a_gpu_it_closes_to_the_old_picture_and_never_composes_on_the_cpu(app):
    rig = _rig(app, broken=True)
    try:
        assert rig.mount.open("DAPI") is None
        _settle(app, rounds=10)
        assert rig.mount.coordinator is None and rig.mount.compose is None and rig.mount.layer is None
        assert rig.mount.host.stack is None                       # nothing of it reads any more
        assert not rig.legacy.isHidden()
        assert rig.mount.source_changed("handoff") is None        # a later source move opens nothing
        assert rig.mount.coordinator is None
    finally:
        _done(rig)


def test_an_unfiltered_mount_still_falls_back_to_the_cpu(app):
    raws, schedulers = [], []
    window = _Window(_state(visible=("CD3",)), _domain())
    host = Step1ViewerHost(stack_factory=_stack_factory(raws, schedulers))
    host.setAttribute(QtCore.Qt.WA_DontShowOnScreen, True)
    host.resize(530, 530)
    host.show()
    mount = Step1WholeSlideMount(window, host=host, gpu_layer_factory=lambda _s: _Broken())
    try:
        assert mount.open("CD3") is not None and mount.coordinator is not None
    finally:
        for raw in raws:
            raw.release_all()
        mount.close()


def test_it_never_draws_its_rectangle_and_has_its_own_budget(app):
    rig = _rig(app, broken=False)
    try:
        rig.mount.open("DAPI")
        _settle(app, rounds=20)
        if rig.mount.backend != BACKEND_GPU:
            pytest.skip(f"no GPU here: {rig.mount.gpu_status()['reason']}")
        assert rig.mount.active
        assert rig.mount.publish_view_rect() is False
        assert rig.mount.gpu_status()["raw_texture_budget_bytes"] == STEP2_RAW_TEXTURE_BYTES
        assert rig.mount._budgets().max_raw_texture_bytes == STEP2_RAW_TEXTURE_BYTES
    finally:
        _done(rig)


def test_the_budget_reaches_both_gpu_paths():
    m = Step1WholeSlideMount.__new__(Step1WholeSlideMount)
    m._raw_texture_bytes = STEP2_RAW_TEXTURE_BYTES
    assert m._budgets().max_raw_texture_bytes == STEP2_RAW_TEXTURE_BYTES


def _viewer_for(window, page, monkeypatch, region=((256, 1280, 256, 1280), "/x/g3.ome.tif", "DAPI")):
    from block01.core import tile_tissue
    monkeypatch.setattr(tile_tissue, "fused_region", lambda p: region)
    return Step2DapiViewer(window, page)


def _fuse_run(tmp_path, polygon=None, bbox=(256, 1280, 256, 1280)):
    import json
    run = tmp_path / "fuse_run"
    run.mkdir(exist_ok=True)
    (run / "roi_config.json").write_text(json.dumps([{"bbox_fullres": list(bbox), "polygon_fullres": polygon}]))
    return str(run / "fused.zarr")


def test_only_the_workspaces_own_input_is_drawn_zoomable(app, monkeypatch, tmp_path):
    window = _Window(_state(), _domain())                      # ROI (256, 1280, 256, 1280), nucleus DAPI
    page = SimpleNamespace(_zarr_path=_fuse_run(tmp_path), _rois=[], _core_items=lambda: [],
                           btn_tma_cores=SimpleNamespace(isChecked=lambda: True))
    assert _viewer_for(window, page, monkeypatch).compatible()
    assert not _viewer_for(window, page, monkeypatch, ((0, 1024, 256, 1280), "/x/g3.ome.tif", "DAPI")).compatible()
    assert not _viewer_for(window, page, monkeypatch, ((256, 1280, 256, 1280), "/y/other.tif", "DAPI")).compatible()
    assert not _viewer_for(window, page, monkeypatch, ((256, 1280, 256, 1280), "/x/g3.ome.tif", "CD3")).compatible()
    poly = [[300, 300], [900, 300], [900, 900]]
    page._zarr_path = _fuse_run(tmp_path, polygon=poly)         # same box, another polygon (as Browse)
    assert not _viewer_for(window, page, monkeypatch).compatible()
    window._active_roi["polygon_fullres"] = poly
    assert _viewer_for(window, page, monkeypatch).compatible()
    page._rois = [{"polygon_fullres": None}]                    # the page's list is not what decides
    assert _viewer_for(window, page, monkeypatch).compatible()
    os.remove(os.path.join(os.path.dirname(page._zarr_path), "roi_config.json"))
    assert not _viewer_for(window, page, monkeypatch).compatible()   # the input does not say: no
    page._zarr_path = None
    assert not _viewer_for(window, page, monkeypatch).compatible()


def test_a_failed_open_is_not_tried_again_for_the_same_input(app, monkeypatch):
    rig = _rig(app, broken=True)
    from block01.core import tile_tissue
    monkeypatch.setattr(tile_tissue, "fused_region", lambda p: ((256, 1280, 256, 1280), "/x/g3.ome.tif", "DAPI"))
    opened = []
    real_open = rig.mount.open
    rig.mount.open = lambda channel="": opened.append(channel) or real_open(channel)
    try:
        assert rig.viewer.follow(True) is False and opened == ["DAPI"]
        assert rig.viewer.refused_key is not None and not rig.legacy.isHidden()
        assert rig.viewer.follow(True) is False and opened == ["DAPI"]   # leave and come back: no retry
        rig.viewer.follow(False)
        rig.viewer.reset()                                               # another dataset: tried again
        assert rig.viewer.refused_key is None
    finally:
        _done(rig)


def test_an_input_from_elsewhere_closes_the_viewer_on_entry(app, monkeypatch):
    rig = _rig(app, broken=True)
    from block01.core import tile_tissue
    monkeypatch.setattr(tile_tissue, "fused_region", lambda p: ((0, 10, 0, 10), "/x/g3.ome.tif", "DAPI"))
    closed = []
    real_close = rig.mount.close
    rig.mount.close = lambda: closed.append(1) or real_close()
    try:
        assert rig.viewer.follow(True) is False and closed and not rig.legacy.isHidden()
    finally:
        _done(rig)


def test_the_grid_is_drawn_and_clicked_in_slide_pixels(app):
    from block01.ui.step2_page import Step2Page
    page = Step2Page()
    page._full_h, page._full_w = 400, 600
    page._ov_h, page._ov_w, page._ov_ds = 100, 150, 4
    page._ov_origin = (1000, 2000)
    page._rows_spin.setValue(2)
    page._cols_spin.setValue(3)
    page._draw_tile_grid()
    items = page.tile_items()
    assert len(items) == 6
    first = next(i for i in items if i["id"] == "tile_0_0")
    assert first["polygon"] == [[2000, 1000], [2200, 1000], [2200, 1200], [2000, 1200]]
    assert page.tile_at(1250, 2450) == (1, 2) and page.tile_at(999, 2000) is None
    hits = []
    clicks = _GridClicks(page.tile_at, lambda r, c: hits.append((r, c)))
    assert clicks.press(1100, 2100) is False and clicks.wheel(120, False) is False
    clicks.click(1100, 2100)
    assert hits == [(0, 0)] and clicks.cursor_at(0, 0) is None and clicks.double_click(0, 0) is False
    page._set_tile_colour(0, 0, "done")
    assert next(i for i in page.tile_items() if i["id"] == "tile_0_0")["color"] == page.TILE_COLOURS["done"]


def test_clearing_the_input_tells_the_zoomable_picture(app):
    from block01.ui.step2_page import Step2Page
    page = Step2Page()
    told = []
    page.input_shown = lambda: told.append(1)
    page._use_fuse_item(-1)                                   # the last input deleted: none selected
    assert told and page._zarr_path is None
