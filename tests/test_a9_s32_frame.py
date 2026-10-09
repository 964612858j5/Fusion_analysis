"""Block A9 §32 (Odon frame loop): one quad per plane, planes outside the
view are not drawn, uploads per submission are bounded, the picture follows
the attached camera from resident textures, wheel planning leaves the input
event, and the publication history is bounded.

Real GL only: set BLOCK01_REQUIRE_STEP1_GPU=1 (as test_step1_gpu_layer).
"""

import os

import numpy as np
import pytest

if os.environ.get("BLOCK01_REQUIRE_STEP1_GPU") != "1":
    pytest.skip("GPU tests require BLOCK01_REQUIRE_STEP1_GPU=1", allow_module_level=True)

import test_step1_gpu_layer as g  # noqa: E402  (its import shim and helpers)
import test_step1_gpu_sources as s  # noqa: E402
from block01.ui import step1_gpu_layer as layer_module  # noqa: E402
from block01.ui import step1_gpu_binding as binding_module  # noqa: E402
from block01.ui.step1_gpu_layer import (  # noqa: E402
    MODE_FUSION, MODE_OVERLAY, DisplaySnapshot, RawPlane, ViewportSnapshot)

SIZE = (37, 29)                      # odd, so pixel centres fall off-grid


@pytest.fixture(scope="module")
def app():
    from PyQt5 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _tiles(level_px, n, *, dtype=np.uint8, seed=0):
    """An n x n grid of adjacent tiles, `level_px` world units each."""
    rng = np.random.default_rng(seed)
    planes = []
    for ty in range(n):
        for tx in range(n):
            values = rng.integers(0, 255, (4, 4)).astype(dtype)
            rect = (tx * level_px, (tx + 1) * level_px, ty * level_px, (ty + 1) * level_px)
            planes.append(RawPlane((level_px, tx, ty, seed), rect, values))
    return tuple(planes)


def _overlay(names):
    return DisplaySnapshot(MODE_OVERLAY, {n: (0.0, 255.0, 1.0) for n in names},
                           {n: 1.0 for n in names},
                           {n: (1.0, 0.5, 0.25) for n in names})


def _scene():
    coarse = _tiles(16.0, 2, seed=1)                  # 32 x 32 world, 2 x 2 tiles
    fine = _tiles(4.0, 8, seed=2)                     # same area, 8 x 8 tiles
    other = _tiles(8.0, 4, dtype=np.uint16, seed=3) * 1
    source = g._source(g._channel("A", coarse=coarse, fine=fine, selected="fine"),
                       g._channel("B", coarse=other))
    return source


VIEWS = [(0.0, 32.0, 0.0, 32.0), (0.3, 31.3, 0.2, 31.55), (2.13, 9.77, 1.1, 7.9),
         (-5.0, 20.0, 3.0, 40.0), (3.999, 12.001, 3.999, 12.0)]


@pytest.mark.parametrize("view", VIEWS)
def test_a_quad_per_plane_draws_exactly_what_the_fullscreen_pass_drew(app, monkeypatch, view):
    viewport = ViewportSnapshot(view, SIZE, 1.0)
    layer = g._layer(app)
    try:
        layer.submit(_scene(), _overlay(["A", "B"]), viewport)
        quads = layer.readback_rgba_for_test()
        # the same passes, every plane rasterised over the whole target
        monkeypatch.setattr(layer_module, "_plane_quad_ndc", lambda *_a: (-1.0, 1.0, -1.0, 1.0))
        layer.submit(_scene(), _overlay(["A", "B"]), viewport)
        fullscreen = layer.readback_rgba_for_test()
        assert np.count_nonzero(quads[..., 3]) > 0
        assert np.array_equal(quads, fullscreen)
    finally:
        layer.deleteLater()


def test_planes_outside_the_view_are_not_drawn(app):
    layer = g._layer(app)
    try:
        everything = ViewportSnapshot((0.0, 32.0, 0.0, 32.0), SIZE, 1.0)
        corner = ViewportSnapshot((0.0, 4.0, 0.0, 4.0), SIZE, 1.0)
        source = g._source(g._channel("A", coarse=_tiles(16.0, 2, seed=1),
                                      fine=_tiles(4.0, 8, seed=2), selected="fine"))
        wide = layer.submit(source, _overlay(["A"]), everything)["pass_count"]
        narrow = layer.submit(source, _overlay(["A"]), corner)["pass_count"]
        # one coarse + one fine tile meet the corner (+ the shared passes)
        assert narrow == wide - (4 - 1) - (64 - 1)
    finally:
        layer.deleteLater()


def test_an_upload_budget_keeps_the_coarse_and_defers_the_fine(app):
    viewport = ViewportSnapshot((0.0, 32.0, 0.0, 32.0), SIZE, 1.0)
    coarse, fine = _tiles(16.0, 2, seed=1), _tiles(4.0, 8, seed=2)
    layer = g._layer(app)
    try:
        layer.upload_budget_ms = 0.0
        result = layer.submit(g._source(g._channel("A", coarse=coarse, fine=fine,
                                                   selected="fine")),
                              _overlay(["A"]), viewport)
        assert result["deferred_uploads"] == len(fine)
        assert all(layer._cache.is_resident(p) for p in coarse)
        budgeted = layer.readback_rgba_for_test()
        layer.submit(g._source(g._channel("A", coarse=coarse)), _overlay(["A"]), viewport)
        assert np.array_equal(budgeted, layer.readback_rgba_for_test()), \
            "what is not uploaded yet is covered by the coarse under it"
        layer.upload_budget_ms = None
        result = layer.submit(g._source(g._channel("A", coarse=coarse, fine=fine,
                                                   selected="fine")),
                              _overlay(["A"]), viewport)
        assert result["deferred_uploads"] == 0
        assert all(layer._cache.is_resident(p) for p in fine)
    finally:
        layer.deleteLater()


def test_the_picture_follows_the_attached_camera_from_resident_textures(app):
    layer = g._layer(app)
    try:
        layer.resize(*SIZE)
        g._events(app)
        start = ViewportSnapshot((0.0, 32.0, 0.0, 32.0), SIZE, 1.0)
        moved = (2.13, 9.77, 1.1, 7.9)
        layer.submit(_scene(), _overlay(["A", "B"]), start)
        uploads = layer._cache.uploads
        layer._attached_range = moved
        layer._camera_dirty = True
        layer.makeCurrent()
        try:
            layer._recompose_for_camera()
        finally:
            layer.doneCurrent()
        followed = layer.readback_rgba_for_test()
        assert layer._cache.uploads == uploads, "a camera frame uploads nothing"
        layer.submit(_scene(), _overlay(["A", "B"]), ViewportSnapshot(moved, SIZE, 1.0))
        assert np.array_equal(followed, layer.readback_rgba_for_test())
    finally:
        layer.deleteLater()


def test_a_fusion_scene_follows_the_camera_too(app):
    layer = g._layer(app)
    try:
        layer.resize(*SIZE)
        g._events(app)
        display = DisplaySnapshot(MODE_FUSION, {"A": (0.0, 255.0, 1.0), "B": (0.0, 65535.0, 1.0)},
                                  groups={"g": {"A": 1.0}}, group_weights={"g": 1.0},
                                  nucleus=("B", 1.0))
        moved = (0.3, 31.3, 0.2, 31.55)
        layer.submit(_scene(), display, ViewportSnapshot((0.0, 32.0, 0.0, 32.0), SIZE, 1.0))
        layer._attached_range = moved
        layer.makeCurrent()
        try:
            layer._recompose_for_camera()
        finally:
            layer.doneCurrent()
        followed = layer.readback_rgba_for_test()
        layer.submit(_scene(), display, ViewportSnapshot(moved, SIZE, 1.0))
        assert np.array_equal(followed, layer.readback_rgba_for_test())
    finally:
        layer.deleteLater()


def test_a_wheel_event_plans_on_the_next_turn_not_inside_the_event(app):
    provider = s._Provider(level_shape=(8, 8), levels=2)
    controller = s._Controller(provider, visible_tiles=((0, 0), (1, 0)), level=0)
    scheduler = s._Scheduler()
    layer = s._RecordingLayer()
    binding, _holder = s._binding(provider, scheduler, controller, layer,
                                  display=s._display(("A",)))
    try:
        binding.source_changed()
        s._deliver_all(app, scheduler, list(scheduler.requests),
                       value=np.full((4, 4), 0.2, np.float32))
        submitted = len(layer.calls)
        moved = controller.set_snapshot(visible_tiles=((0, 0), (1, 0), (0, 1)), epoch=2)
        controller.interaction_event.emit("ZOOM", moved)
        assert len(layer.calls) == submitted, "nothing is planned inside the input event"
        s._events(app)
        assert len(layer.calls) > submitted, "...and it is planned right after"
    finally:
        binding.dispose()


def test_the_publication_history_is_bounded_but_counted(app, monkeypatch):
    monkeypatch.delenv("BLOCK01_GPU_HISTORY_ALL", raising=False)
    provider = s._Provider(level_shape=(8, 8), levels=2)
    controller = s._Controller(provider, visible_tiles=((0, 0),), level=0)
    scheduler = s._Scheduler()
    layer = s._RecordingLayer()
    binding, _holder = s._binding(provider, scheduler, controller, layer,
                                  display=s._display(("A",)))
    try:
        binding.source_changed()
        s._deliver_all(app, scheduler, list(scheduler.requests),
                       value=np.full((4, 4), 0.2, np.float32))
        for _ in range(binding_module.HISTORY_LIMIT + 10):
            binding._publish_current()
        assert len(binding.descriptor_history) == binding_module.HISTORY_LIMIT
        assert binding.publication_count >= binding_module.HISTORY_LIMIT + 10
        assert binding.stats()["descriptor_publications"] == binding.publication_count
    finally:
        binding.dispose()


def test_pyopengl_error_checking_is_off_unless_asked_for(monkeypatch):
    import OpenGL
    before = OpenGL.ERROR_CHECKING
    try:
        monkeypatch.delenv("BLOCK01_GL_CHECK", raising=False)
        layer_module.configure_pyopengl()
        assert OpenGL.ERROR_CHECKING is False
        OpenGL.ERROR_CHECKING = True
        monkeypatch.setenv("BLOCK01_GL_CHECK", "1")
        layer_module.configure_pyopengl()
        assert OpenGL.ERROR_CHECKING is True
    finally:
        OpenGL.ERROR_CHECKING = before
