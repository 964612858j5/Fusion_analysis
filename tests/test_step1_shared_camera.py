"""One slide, one place on it: Step0 and Step1 share the camera.

Block C4.5a of `docs/step1_rework_plan.md`, on the user's ruling. Pan or zoom
in Step0, walk into Step1, and Step1 is looking at the same point at the same
magnification; move in Step1 -- by hand, by a patch button, by a click on the
Tissue Preview -- and Step0 is there when the user comes back. What is shared
is the POSITION: `(cx, cy, scale)` in level-0 coordinates, carried with the
dataset it belongs to. The ticks, the current channel and the scientific state
stay as separate as C3 made them.

REAL GEOMETRY ON BOTH SIDES. Step0's full image and its compare panels are
real `ExploreView`s (the harness the compare suites already use); Step1 is its
real stack over a synthetic pyramid. A test that recorded a `jump_to` call and
declared the views synchronised would prove nothing about either aspect lock.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import json
import os
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")
pytest.importorskip("pyqtgraph")

from PyQt5 import QtTest, QtWidgets  # noqa: E402

from block01.ui import step1_viewer_mount as mount_module  # noqa: E402
from block01.ui.shared_camera import CameraSnapshot  # noqa: E402
from block01.ui.step1_viewer_host import (  # noqa: E402
    Step1TileProvider, Step1ViewerHost,
)
from block01.viewer.tile_types import TileGridSpec  # noqa: E402

from test_step0_compare_tiles import (  # noqa: E402
    SLIDE_H, SLIDE_W, _LowresLoader, _fake_compare_factory, app,  # noqa: F401
)
from test_step0_compare_toggle_drift import _RealTab  # noqa: E402

GRID = TileGridSpec(tile_size=512, source_chunk_shape=(), grid_version="v1")
CHANNELS = ("DAPI", "CD3", "CD8")
TOL = 12.0            # level-0 pixels: clamping and integer jump rounding
SCALE_TOL = 0.06      # 6%: the two widgets round their rectangles differently


# ── Step1's slide ─────────────────────────────────────────────────────

class _RawPyramid:
    def __init__(self):
        self._shapes = {0: (SLIDE_H, SLIDE_W),
                        1: (SLIDE_H // 4, SLIDE_W // 4)}
        self.reads = []

    def source_identity(self):
        from block01.viewer.tile_types import SourceIdentity
        return SourceIdentity(dataset_path="/fake/slide.ome.tif",
                              dataset_fingerprint="1:1", stage="raw")

    @property
    def num_levels(self):
        return 2

    @property
    def channel_names(self):
        return list(CHANNELS)

    def channel_index(self, channel):
        return CHANNELS.index(channel)

    def level_shape(self, level):
        return self._shapes[level]

    def level_downsample(self, level):
        return 1.0 if level == 0 else 4.0

    def level_downsample_yx(self, level):
        ds = self.level_downsample(level)
        return ds, ds

    def read_region(self, channel, level, y0, y1, x0, x1):
        self.reads.append((channel, level, y0, y1, x0, x1))
        base = (CHANNELS.index(channel) + 1) * 300.0
        yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
        return base + (yy % 7) * 10.0 + (xx % 5) * 3.0, (y0, x0)

    def read_tile(self, channel, tile):
        size = tile.grid.tile_size
        y0, x0 = tile.ty * size, tile.tx * size
        values, _off = self.read_region(channel, tile.level, y0, y0 + size,
                                        x0, x0 + size)
        return values, 0.0

    def close(self):
        pass


def _step1_factory(raws):
    def _factory(path, chan, table, parent):
        from block01.ui.step0.step0_explore_tab import ExploreStack
        from block01.viewer.caches import LRUByteCache
        from block01.viewer.correction_compute import CorrectionCompute
        from block01.viewer.explore_view import ExploreController, ExploreView
        from block01.viewer.scheduler import TileScheduler

        raw = _RawPyramid()
        raws.append(raw)
        provider = Step1TileProvider(raw, table)
        raw_cache = LRUByteCache(32 * 1024 * 1024)
        corrected_cache = LRUByteCache(32 * 1024 * 1024)
        compute = CorrectionCompute(provider, raw_cache)
        scheduler = TileScheduler(provider, compute, raw_cache,
                                  corrected_cache)
        view = ExploreView(parent)
        controller = ExploreController(provider, scheduler, compute, GRID,
                                       view, chan)
        controller.load_overview()
        view.view_box.setRange(xRange=(0, SLIDE_W), yRange=(0, SLIDE_H),
                               padding=0)
        return ExploreStack(provider, scheduler, controller, view,
                            (raw_cache, corrected_cache))
    return _factory


# ── the window, with both real viewers ────────────────────────────────

def _window(app, monkeypatch, tmp_path):
    from block01.ui.main_window import MainWindow

    raws = []

    class _Host(Step1ViewerHost):
        def __init__(self, *a, **k):
            super().__init__(stack_factory=_step1_factory(raws))

    original = mount_module.Step1WholeSlideMount.__init__

    def _init(self, window, host=None, parent=None):
        original(self, window, host=_Host(), parent=parent)

    monkeypatch.setattr(mount_module.Step1WholeSlideMount, "__init__", _init)
    monkeypatch.setattr(mount_module.Step1ViewerBinding, "_source_moved",
                        lambda self, **k: False)

    w = MainWindow()
    w._schedule_step1_session_save = lambda: None
    w._save_step1_session = lambda *a, **k: None
    w.loader = _LowresLoader()
    w.loader.filepath = "/fake/slide.ome.tif"
    w._step0.loader = w.loader
    w._step0.ome_path = "/fake/slide.ome.tif"
    w._step0.patches = []
    w._step0.nucleus_channel = "DAPI"
    w._step0._rebuild_channel_list()
    w._step0.current_channel = "CD3"
    tab = _RealTab()
    w._step0._explore_tab = tab
    w._step0._full_image_host.addWidget(tab, stretch=1)
    w._step0._compare_builds = []
    w._step0._compare_strip_widget._stack_factory = _fake_compare_factory(
        w._step0._compare_builds)
    w.config.set_channels(list(CHANNELS))
    w.config.load_panel({"markers": {"CD3": 1.0, "CD8": 0.5}}, "DAPI")
    w.config.set_nucleus("DAPI", 1.0)
    manifest = tmp_path / "step0_roi_result.json"
    manifest.write_text(json.dumps(
        {"handoff_schema_version": 2,
         "channel_remap_config_hash": "remap-1",
         "source_identity": {"dataset_path": w.loader.filepath,
                             "stage": "raw"}}), encoding="utf-8")
    w.step0_output = {"step1_dir": str(tmp_path), "output_dir": str(tmp_path),
                      "step0_manifest_path": str(manifest),
                      "channel_remap_config_hash": "remap-1"}
    w._corrected_decisions = {}
    w._corrected_zarr_path = ""
    w._active_roi = None
    w.resize(1200, 800)
    w.show()
    QtTest.QTest.qWait(50)
    w._step0._apply_full_image_view_rect((0.0, 0.0, float(SLIDE_W),
                                          float(SLIDE_H)))
    QtTest.QTest.qWait(20)
    return SimpleNamespace(w=w, raws=raws, tab=tab)


def _close(rig):
    rig.w._display.shutdown("test")
    rig.w.hide()
    rig.w.deleteLater()
    QtWidgets.QApplication.processEvents()


def _in(rig, step):
    rig.w._set_step_active(step)
    for _ in range(3):
        QtWidgets.QApplication.processEvents()
        QtTest.QTest.qWait(10)


def _step0_camera(rig):
    return rig.w._step0.current_camera_snapshot()


def _step1_camera(rig):
    return rig.w._step1_mount.current_camera()


def _same(a, b, tol=TOL, scale_tol=SCALE_TOL):
    assert a is not None and b is not None, (a, b)
    assert abs(a[0] - b[0]) <= tol, f"cx {a[0]} vs {b[0]}"
    assert abs(a[1] - b[1]) <= tol, f"cy {a[1]} vs {b[1]}"
    assert abs(a[2] - b[2]) <= scale_tol * max(a[2], b[2]), \
        f"scale {a[2]} vs {b[2]}"


# ── A. the camera goes both ways ──────────────────────────────────────

def test_step0_pan_and_zoom_is_where_step1_opens(app, monkeypatch, tmp_path):
    """Gate A1."""
    rig = _window(app, monkeypatch, tmp_path)
    try:
        _in(rig, 0)
        rig.w._step0._apply_full_image_view_rect(
            (4000.0, 3000.0, SLIDE_W / 4.0, SLIDE_H / 4.0))
        QtTest.QTest.qWait(20)
        step0 = _step0_camera(rig)

        _in(rig, 1)

        _same(step0, _step1_camera(rig))
    finally:
        _close(rig)


def test_step1_pan_and_zoom_is_where_step0_comes_back_to(app, monkeypatch,
                                                         tmp_path):
    """Gate A2."""
    rig = _window(app, monkeypatch, tmp_path)
    try:
        _in(rig, 0)
        _in(rig, 1)
        rig.w._step1_mount.apply_camera(9000.0, 6000.0,
                                        _step1_camera(rig)[2] * 2.0)
        QtTest.QTest.qWait(20)
        step1 = _step1_camera(rig)

        _in(rig, 0)

        _same(step1, _step0_camera(rig))
    finally:
        _close(rig)


def test_compare_is_the_camera_step1_takes(app, monkeypatch, tmp_path):
    """Gate A3: the panels are the view while they are up, so the hidden full
    image's older camera is not the one that travels."""
    rig = _window(app, monkeypatch, tmp_path)
    try:
        _in(rig, 0)
        rig.w._step0._apply_full_image_view_rect(
            (0.0, 0.0, float(SLIDE_W), float(SLIDE_H)))
        QtTest.QTest.qWait(20)
        full = _step0_camera(rig)
        rig.w._step0._enter_compare_mode()
        QtTest.QTest.qWait(30)
        rig.w._step0._apply_compare_camera(11000.0, 7000.0, full[2] * 3.0)
        QtTest.QTest.qWait(20)
        compare = rig.w._step0._compare_camera()
        assert compare is not None

        _in(rig, 1)

        _same(compare, _step1_camera(rig))
        assert abs(_step1_camera(rig)[0] - full[0]) > TOL, (
            "Step1 took the hidden full image's camera")
    finally:
        _close(rig)


def test_step1_reaches_all_three_compare_panels(app, monkeypatch, tmp_path):
    """Gate A4."""
    rig = _window(app, monkeypatch, tmp_path)
    try:
        _in(rig, 0)
        rig.w._step0._enter_compare_mode()
        QtTest.QTest.qWait(30)
        _in(rig, 1)
        rig.w._step1_mount.apply_camera(6000.0, 5000.0,
                                        _step1_camera(rig)[2] * 2.0)
        QtTest.QTest.qWait(20)
        step1 = _step1_camera(rig)

        _in(rig, 0)
        QtTest.QTest.qWait(30)

        strip = rig.w._step0._compare_strip_widget
        cameras = [strip.camera(i) for i in range(3)]
        assert all(c is not None for c in cameras), cameras
        for camera in cameras:
            _same(step1, camera)
    finally:
        _close(rig)


def test_round_trips_and_a_resize_do_not_walk_the_view(app, monkeypatch,
                                                       tmp_path):
    """Gate A5: a centre and a scale survive a trip that a RECTANGLE would
    not -- the two widgets have different aspects, and a resize changes one
    of them."""
    rig = _window(app, monkeypatch, tmp_path)
    try:
        _in(rig, 0)
        rig.w._step0._apply_full_image_view_rect(
            (5000.0, 4000.0, SLIDE_W / 3.0, SLIDE_H / 3.0))
        QtTest.QTest.qWait(20)
        start = _step0_camera(rig)

        for round_ in range(4):
            _in(rig, 1)
            if round_ == 1:
                rig.w.resize(1000, 900)
                QtTest.QTest.qWait(30)
            _in(rig, 0)

        _same(start, _step0_camera(rig), tol=40.0, scale_tol=0.12)
    finally:
        _close(rig)


def test_another_dataset_does_not_inherit_the_old_position(app, monkeypatch,
                                                           tmp_path):
    """Gate A6."""
    rig = _window(app, monkeypatch, tmp_path)
    try:
        _in(rig, 0)
        rig.w._step0._apply_full_image_view_rect(
            (7000.0, 6000.0, SLIDE_W / 5.0, SLIDE_H / 5.0))
        QtTest.QTest.qWait(20)
        _in(rig, 1)
        assert rig.w._shared_camera is not None

        rig.w.loader.filepath = "/fake/other.ome.tif"
        assert rig.w._shared_camera.valid_for("/fake/other.ome.tif") is False
        _in(rig, 0)

        assert rig.w._shared_camera.dataset == "/fake/other.ome.tif", (
            "the new slide did not establish its own position")
    finally:
        _close(rig)


# ── B. the hidden viewer is not driven ────────────────────────────────

def test_moving_in_step0_asks_step1_for_nothing(app, monkeypatch, tmp_path):
    """Gate B1."""
    rig = _window(app, monkeypatch, tmp_path)
    try:
        _in(rig, 0)
        _in(rig, 1)
        QtTest.QTest.qWait(50)
        _in(rig, 0)
        reads = len(rig.raws[0].reads)

        for i in range(3):
            rig.w._step0._apply_full_image_view_rect(
                (1000.0 * i, 900.0 * i, SLIDE_W / 4.0, SLIDE_H / 4.0))
            QtTest.QTest.qWait(20)

        assert len(rig.raws[0].reads) == reads, (
            "Step0's camera drove the hidden Step1 viewer")

        # AND THE TRANSITION ITSELF: entering Step0 applies the camera to
        # Step0 and to nothing else.
        _in(rig, 1)
        QtTest.QTest.qWait(30)
        reads = len(rig.raws[0].reads)
        jumps = len(rig.tab.stack.controller.jumps)
        rects = len(getattr(rig.tab.stack.controller, "view_rects", []))
        _in(rig, 0)
        QtTest.QTest.qWait(30)
        assert len(rig.tab.stack.controller.jumps) > jumps or \
            len(getattr(rig.tab.stack.controller, "view_rects", [])) > rects, (
                "entering Step0 did not move Step0's own camera")
    finally:
        _close(rig)


def test_moving_in_step1_asks_step0_for_nothing(app, monkeypatch, tmp_path):
    """Gate B2."""
    rig = _window(app, monkeypatch, tmp_path)
    try:
        _in(rig, 0)
        _in(rig, 1)
        controller = rig.tab.stack.controller
        jumps = len(controller.jumps)
        rects = len(getattr(controller, "view_rects", []))

        rig.w._step1_mount.apply_camera(8000.0, 5000.0,
                                        _step1_camera(rig)[2] * 2.0)
        QtTest.QTest.qWait(30)

        assert len(controller.jumps) == jumps
        assert len(getattr(controller, "view_rects", [])) == rects, (
            "Step1's camera drove the hidden Step0 viewer")

        # AND THE TRANSITION: entering Step1 asks Step1 for its tiles and
        # leaves the hidden Step0 viewer alone.
        _in(rig, 0)
        QtTest.QTest.qWait(30)
        jumps = len(controller.jumps)
        rects = len(getattr(controller, "view_rects", []))
        _in(rig, 1)
        QtTest.QTest.qWait(30)
        assert len(controller.jumps) == jumps
        assert len(getattr(controller, "view_rects", [])) == rects, (
            "entering Step1 drove the hidden Step0 viewer")
    finally:
        _close(rig)


# ── C. the Tissue Preview's rectangle ─────────────────────────────────

def _overview(rig):
    popup = rig.w._display.ensure_navigator()
    QtWidgets.QApplication.processEvents()
    return popup.overview


def test_step1_publishes_its_viewport_and_keeps_it_current(app, monkeypatch,
                                                           tmp_path):
    """Gates C1, C2 and C5: the popup is created AFTER Step1's viewer, and
    the rectangle follows a pan and a zoom."""
    rig = _window(app, monkeypatch, tmp_path)
    try:
        _in(rig, 0)
        _in(rig, 1)
        overview = _overview(rig)
        rig.w._step1_mount.publish_view_rect()
        first = overview.current_view_rect()
        assert first is not None, "the lazily created popup got no rectangle"

        # THE VIEWBOX ITSELF, as a hand pan and a wheel zoom are: nothing
        # calls a mount method, so what keeps the rectangle current is the
        # connection to the real range signal.
        view_box = rig.w._step1_mount.host.stack.view.view_box
        view_box.setRange(xRange=(6000, 10000), yRange=(5000, 9000),
                          padding=0)
        QtTest.QTest.qWait(20)
        panned = overview.current_view_rect()
        assert panned is not None and panned != first, "the rectangle did not move"

        view_box.setRange(xRange=(7000, 9000), yRange=(6000, 8000), padding=0)
        QtTest.QTest.qWait(20)
        zoomed = overview.current_view_rect()
        assert (zoomed[1] - zoomed[0]) < (panned[1] - panned[0]) * 0.9, (
            "the rectangle did not shrink when the view zoomed in")
    finally:
        _close(rig)


@pytest.mark.parametrize("gesture", ["patch", "preview"])
def test_a_jump_puts_the_rectangle_where_it_landed(app, monkeypatch, tmp_path,
                                                   gesture):
    """Gates C3 and C4."""
    rig = _window(app, monkeypatch, tmp_path)
    try:
        _in(rig, 0)
        _in(rig, 1)
        overview = _overview(rig)
        target = (6000, 5000)

        if gesture == "patch":
            rig.w._all_patches = [(target[0], target[0] + 1024,
                                   target[1], target[1] + 1024)]
            rig.w._select_preview_patch(0)
            want = (target[0] + 512, target[1] + 512)
        else:
            rig.w._on_step1_tissue_navigate(target[0], target[1])
            want = target
        QtTest.QTest.qWait(30)

        rect = overview.current_view_rect()
        assert rect is not None
        cy = (rect[0] + rect[1]) / 2.0
        cx = (rect[2] + rect[3]) / 2.0
        assert abs(cy - want[0]) <= 600 and abs(cx - want[1]) <= 600, (
            f"the rectangle is at {(cy, cx)}, not {want}")
    finally:
        _close(rig)


def test_a_rebuilt_stack_keeps_publishing(app, monkeypatch, tmp_path):
    """Gate C6: a new source is a new ViewBox."""
    rig = _window(app, monkeypatch, tmp_path)
    try:
        _in(rig, 0)
        _in(rig, 1)
        overview = _overview(rig)
        rig.w._step1_mount.publish_view_rect()
        before_box = rig.w._step1_mount.host.stack.view.view_box

        # A MOVED SOURCE, as a republished handoff would be: the viewer
        # binding tears the stack down and builds another one.
        monkeypatch.setattr(mount_module.Step1ViewerBinding, "_source_moved",
                            lambda self, **k: True)
        rig.w._step1_mount.source_changed("test-rebuild")
        monkeypatch.setattr(mount_module.Step1ViewerBinding, "_source_moved",
                            lambda self, **k: False)
        QtTest.QTest.qWait(30)
        after_box = rig.w._step1_mount.host.stack.view.view_box
        assert after_box is not before_box, "the stack was not rebuilt"

        after_box.setRange(xRange=(3000, 5000), yRange=(3000, 5000),
                           padding=0)
        QtTest.QTest.qWait(20)
        rect = overview.current_view_rect()
        assert rect is not None
        cy = (rect[0] + rect[1]) / 2.0
        assert abs(cy - 4000.0) <= 600, (
            "the rebuilt ViewBox stopped updating the rectangle")
    finally:
        _close(rig)


def test_the_rectangle_belongs_to_the_step_on_screen(app, monkeypatch,
                                                     tmp_path):
    """Gates C7 and C8."""
    rig = _window(app, monkeypatch, tmp_path)
    try:
        _in(rig, 0)
        _in(rig, 1)
        overview = _overview(rig)
        rig.w._step1_mount.apply_camera(9000.0, 7000.0,
                                        _step1_camera(rig)[2] * 2.0)
        QtTest.QTest.qWait(20)
        step1_rect = overview.current_view_rect()
        assert step1_rect is not None

        # A hidden Step0 range signal, exactly as a late one would arrive.
        rig.w._step0._on_full_image_camera_moved()
        QtWidgets.QApplication.processEvents()
        assert overview.current_view_rect() == step1_rect, (
            "hidden Step0 overwrote Step1's rectangle")

        _in(rig, 0)
        rig.w._step0._update_full_image_view_rect()
        QtWidgets.QApplication.processEvents()
        assert overview.current_view_rect() != step1_rect, (
            "Step0 did not take the rectangle back")
    finally:
        _close(rig)


# ── D. the science does not travel with the camera ────────────────────

def test_sharing_the_camera_changes_no_scientific_state(app, monkeypatch,
                                                        tmp_path):
    """Gate D."""
    rig = _window(app, monkeypatch, tmp_path)
    try:
        _in(rig, 0)
        _in(rig, 1)
        state = rig.w._display.state
        fusion = rig.w._display.fusion
        with state.using_scope("step0"):
            before_step0 = dict(state.display_visibility())
            step0_channel = state.selected_channel()
        with state.using_scope("step1"):
            before_step1 = dict(state.display_visibility())
            step1_channel = state.selected_channel()
        draft = fusion.draft_snapshot()
        committed = fusion.committed_snapshot()
        colors = state.colors(list(CHANNELS))
        mode = rig.w._step1_preview_mode
        methods = dict(getattr(rig.w._step0, "_channel_methods", {}) or {})
        decisions = dict(getattr(rig.w._step0, "_channel_decisions", {}) or {})

        for _ in range(2):
            _in(rig, 0)
            rig.w._step0._apply_full_image_view_rect(
                (3000.0, 2000.0, SLIDE_W / 3.0, SLIDE_H / 3.0))
            QtTest.QTest.qWait(20)
            _in(rig, 1)
            rig.w._step1_mount.apply_camera(7000.0, 6000.0,
                                            _step1_camera(rig)[2] * 1.5)
            QtTest.QTest.qWait(20)

        with state.using_scope("step0"):
            assert dict(state.display_visibility()) == before_step0
            assert state.selected_channel() == step0_channel
        with state.using_scope("step1"):
            assert dict(state.display_visibility()) == before_step1
            assert state.selected_channel() == step1_channel
        assert fusion.draft_snapshot() == draft
        assert fusion.committed_snapshot() == committed
        assert state.colors(list(CHANNELS)) == colors
        assert rig.w._step1_preview_mode == mode
        assert dict(getattr(rig.w._step0, "_channel_methods", {}) or {}) == methods
        assert dict(getattr(rig.w._step0, "_channel_decisions", {}) or {}) == decisions
    finally:
        _close(rig)


def test_a_snapshot_is_refused_for_another_slide(app):
    shot = CameraSnapshot("/a.ome.tif", 10.0, 20.0, 0.5, "test")
    assert shot.valid_for("/a.ome.tif") is True
    assert shot.valid_for("/b.ome.tif") is False
    assert CameraSnapshot("/a.ome.tif", 0.0, 0.0, 0.0).usable() is False
