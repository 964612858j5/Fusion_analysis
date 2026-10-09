"""Block A9 §38: the "Smooth" display setting (Odon's smooth_pixels).

On: a channel's raw value is the bilinear mix of the four texels around the
pixel, invalid ones (outside the valid rect, NaN) dropped and the weights
renormalised; the single pass reaches across tile edges, so four tiles draw
as one plane. Off: nearest texels, the bytes of every other gate.

Real GL only: set BLOCK01_REQUIRE_STEP1_GPU=1 (as test_step1_gpu_layer).
"""

import os

import numpy as np
import pytest

if os.environ.get("BLOCK01_REQUIRE_STEP1_GPU") != "1":
    pytest.skip("GPU tests require BLOCK01_REQUIRE_STEP1_GPU=1", allow_module_level=True)

import test_step1_gpu_layer as g  # noqa: E402  (its import shim and helpers)
from block01.ui import gpu_memory  # noqa: E402
from block01.ui.step1_gpu_layer import (  # noqa: E402
    MODE_FUSION, MODE_OVERLAY, DisplaySnapshot, RawPlane, ViewportSnapshot)

SIZE = (97, 61)
BUDGET = 512 * 1024 * 1024
T = 512
# about 4x magnified around the corner where four tiles meet
CORNER = ViewportSnapshot((500.0, 524.0, 500.0, 515.0), SIZE, 1.0)


@pytest.fixture(scope="module")
def app():
    from PyQt5 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _layer(app, smooth, arrays=True):
    layer = g._layer(app, budget=BUDGET)
    layer.use_tile_arrays = arrays
    layer.smooth = smooth
    return layer


def _ramp(n=1024):
    y, x = np.mgrid[0:n, 0:n]
    return ((x * 3 + y * 5) % 251).astype(np.uint8)


def _tiles(values, name="A", ds=1.0, **kw):
    """`values` cut into <=512 tiles (the last row / column partial)."""
    h, w = values.shape
    planes = []
    for ty in range(-(-h // T)):
        for tx in range(-(-w // T)):
            tile = values[ty * T:(ty + 1) * T, tx * T:(tx + 1) * T]
            th, tw = tile.shape
            rect = (tx * T * ds, (tx * T + tw) * ds, ty * T * ds, (ty * T + th) * ds)
            planes.append(RawPlane((name, ds, tx, ty), rect, tile, **kw))
    return tuple(planes)


def _source(*channels):
    return g._source(*[g._channel(name, coarse=planes) for name, planes in channels])


def _overlay(names=("A",), mapping=(0.0, 255.0, 1.0)):
    return DisplaySnapshot(MODE_OVERLAY, {n: mapping for n in names}, {n: 1.0 for n in names},
                           {n: (1, 1, 1) for n in names})


def _draw(app, source, display, viewport=CORNER, smooth=True, arrays=True, vt=None):
    layer = _layer(app, smooth, arrays)
    try:
        layer.submit(source, display, viewport)
        if vt is not None:
            frames = layer._vt.frames if layer._vt is not None else 0
            assert (frames > 0) is vt, f"single pass used: {frames}"
        return layer.readback_rgba_for_test()
    finally:
        layer.deleteLater()


@pytest.mark.parametrize("n, gamma", [(1024, 1.0), (1000, 0.5)],
                         ids=["whole-tiles", "partial-tiles-gamma"])
def test_four_tiles_draw_smoothed_as_one_plane(app, n, gamma):
    """The single pass mixes across tile edges: no seam where four tiles
    meet (one plane, drawn per plane, has no edge there to begin with)."""
    values = _ramp(n)
    display = _overlay(mapping=(0.0, 255.0, gamma))
    tiled = _draw(app, _source(("A", _tiles(values))), display, vt=True)
    whole = _draw(app, _source(("A", (RawPlane(("A", "whole"), (0.0, float(n), 0.0, float(n)), values),))),
                  display, vt=False)
    assert np.count_nonzero(tiled[..., 3]) == tiled.shape[0] * tiled.shape[1]
    assert int(np.abs(tiled.astype(int) - whole.astype(int)).max()) <= 1


def test_smooth_is_not_nearest_and_off_is(app):
    values = _ramp()
    source = _source(("A", _tiles(values)))
    on = _draw(app, source, _overlay(), smooth=True)
    off = _draw(app, source, _overlay(), smooth=False)
    nearest_multipass = _draw(app, source, _overlay(), smooth=False, arrays=False)
    assert np.array_equal(off, nearest_multipass)
    assert not np.array_equal(on, off)
    # the ramp is magnified ~4x: nearest repeats each value ~4 times along a
    # row, smoothed rows change at almost every pixel
    row = SIZE[1] // 2
    assert len(np.unique(on[row, :, 0])) > 2 * len(np.unique(off[row, :, 0]))


@pytest.mark.parametrize("arrays", [True, False], ids=["single-pass", "per-plane"])
@pytest.mark.parametrize("view", [(0.0, 97.0, 0.0, 61.0), (100.3, 300.3, 50.2, 176.0)],
                         ids=["one-to-one", "zoomed-out"])
def test_nothing_changes_where_a_texel_is_no_wider_than_a_pixel(app, arrays, view):
    source = _source(("A", _tiles(_ramp())))
    viewport = ViewportSnapshot(view, SIZE, 1.0)
    on = _draw(app, source, _overlay(), viewport=viewport, smooth=True, arrays=arrays)
    off = _draw(app, source, _overlay(), viewport=viewport, smooth=False, arrays=arrays)
    assert np.array_equal(on, off)


@pytest.mark.parametrize("arrays", [True, False], ids=["single-pass", "per-plane"])
def test_one_axis_magnified_is_smoothed(app, arrays):
    """x at 1:1, y magnified 4x: smoothing is on (either axis decides)."""
    source = _source(("A", _tiles(_ramp())))
    viewport = ViewportSnapshot((100.0, 197.0, 50.2, 65.45), SIZE, 1.0)
    on = _draw(app, source, _overlay(), viewport=viewport, smooth=True, arrays=arrays)
    off = _draw(app, source, _overlay(), viewport=viewport, smooth=False, arrays=arrays)
    assert not np.array_equal(on, off)


@pytest.mark.parametrize("arrays", [True, False], ids=["single-pass", "per-plane"])
def test_texels_outside_the_valid_rect_never_mix_in(app, arrays):
    """A constant inside the valid rect, garbage outside: smoothing over the
    edge stays exactly the constant."""
    values = np.full((1024, 1024), 255, np.uint8)
    values[:, :] = 7                                      # garbage everywhere ...
    planes = []
    for plane in _tiles(values):
        tile = np.array(plane.values)
        tile[: 509, : 509] = 200                          # ... but the valid part
        planes.append(RawPlane(plane.identity, plane.world_rect, tile, valid_rect=(0, 509, 0, 509)))
    view = ViewportSnapshot((1000.0, 1024.0, 500.0, 515.0), SIZE, 1.0)
    out = _draw(app, _source(("A", tuple(planes))), _overlay(), viewport=view, arrays=arrays)
    drawn = out[..., 3] > 0
    assert drawn.any() and not drawn.all()               # the valid edge is in view
    assert set(np.unique(out[..., 0][drawn])) == {200}


@pytest.mark.parametrize("arrays", [True, False], ids=["single-pass", "per-plane"])
def test_nan_texels_never_mix_in(app, arrays):
    rng = np.random.default_rng(3)
    values = np.full((1024, 1024), 600.0, np.float32)
    values[rng.random(values.shape) < 0.2] = np.nan
    out = _draw(app, _source(("A", _tiles(values))), _overlay(mapping=(0.0, 1000.0, 1.0)),
                arrays=arrays)
    drawn = out[..., 3] > 0
    assert drawn.any()
    assert set(np.unique(out[..., 0][drawn])) == {round(0.6 * 255)}


def test_fusion_is_smoothed_too(app):
    values = _ramp()
    source = _source(("A", _tiles(values)), ("D", _tiles(values[::-1].copy(), "D")))
    display = DisplaySnapshot(MODE_FUSION, {"A": (0.0, 255.0, 1.0), "D": (0.0, 255.0, 1.0)},
                              groups={"g1": {"A": 1.0}}, group_weights={"g1": 1.0},
                              nucleus=("D", 1.0))
    on = _draw(app, source, display, smooth=True, vt=True)
    off = _draw(app, source, display, smooth=False, vt=True)
    assert np.array_equal(on[..., 3], off[..., 3])
    assert not np.array_equal(on, off)


def test_a_missing_neighbour_tile_never_mixes_in_the_coarse_level(app):
    """Fine tiles of 200 with one tile missing, a coarse level of 50 under
    them: across that edge the fine side stays exactly 200, the hole shows
    the coarse 50 -- a tap is never taken from another level."""
    fine = tuple(p for p in _tiles(np.full((1024, 1024), 200, np.uint8))
                 if p.identity[2:] != (1, 0))
    coarse = _tiles(np.full((256, 256), 50, np.uint8), ds=4.0)
    source = g._source(g._channel("A", coarse=coarse, fine=fine, selected="fine"))
    view = ViewportSnapshot((500.0, 524.0, 200.0, 215.0), SIZE, 1.0)
    out = _draw(app, source, _overlay(), viewport=view, vt=True)
    assert set(np.unique(out[..., 0])) == {200, 50}


def test_the_setting_redraws_every_layer_and_back_to_the_same_bytes(app):
    layer = _layer(app, gpu_memory.smooth_pixels())
    assert layer.smooth is False                          # the gates' default
    try:
        layer.submit(_source(("A", _tiles(_ramp()))), _overlay(), CORNER)
        before = layer.readback_rgba_for_test()

        def redraw():
            layer.makeCurrent()
            try:
                if layer._camera_dirty:
                    layer._recompose_for_camera()
            finally:
                layer.doneCurrent()
            return layer.readback_rgba_for_test()

        gpu_memory.set_smooth(True)
        assert layer.smooth is True
        smoothed = redraw()
        assert not np.array_equal(before, smoothed)
        assert _layer(app, gpu_memory.smooth_pixels()).smooth is True   # new layers too
        gpu_memory.set_smooth(False)
        assert np.array_equal(before, redraw())
    finally:
        gpu_memory.set_smooth(False)
        layer.deleteLater()


def test_the_setting_redraws_shown_layers_through_qt(app):
    """Two shown layers (Viewer-like and montage-like); the public setter
    alone, then Qt's own paint, gives each its smoothed picture."""
    from PyQt5 import QtWidgets
    hosts, layers = [], []
    try:
        for _ in range(2):
            host = QtWidgets.QWidget()
            host.resize(*SIZE)
            host.show()
            layer = g._layer(app, budget=BUDGET, parent=host)
            layer.use_tile_arrays = True
            layer.resize(*SIZE)
            g._events(app)
            layer.submit(_source(("A", _tiles(_ramp()))), _overlay(), CORNER)
            hosts.append(host)
            layers.append(layer)
        before = [layer.readback_rgba_for_test() for layer in layers]
        gpu_memory.set_smooth(True)
        for _ in range(20):
            g._events(app)
            if not any(layer._camera_dirty for layer in layers):
                break
        assert not any(layer._camera_dirty for layer in layers), "Qt never painted"
        for layer, old in zip(layers, before):
            assert not np.array_equal(old, layer.readback_rgba_for_test())
        gpu_memory.set_smooth(False)
        for _ in range(20):
            g._events(app)
            if not any(layer._camera_dirty for layer in layers):
                break
        for layer, old in zip(layers, before):
            assert np.array_equal(old, layer.readback_rgba_for_test())
    finally:
        gpu_memory.set_smooth(False)
        for host in hosts:
            host.deleteLater()
