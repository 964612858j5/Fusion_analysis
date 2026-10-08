"""Block A9 S2c: raw planes uploaded as R8UI / R16UI integer textures draw
EXACTLY what the same values uploaded as R32F draw -- the integer becomes a
float in the shader and meets the same window and gamma -- and an integer
plane's validity is its rectangle: outside it nothing is drawn, as NaN is
not drawn on the float path.

Real GL only: set BLOCK01_REQUIRE_STEP1_GPU=1 (as test_step1_gpu_layer).
"""

import os

import numpy as np
import pytest

if os.environ.get("BLOCK01_REQUIRE_STEP1_GPU") != "1":
    pytest.skip("GPU tests require BLOCK01_REQUIRE_STEP1_GPU=1", allow_module_level=True)

import test_step1_gpu_layer as g  # noqa: E402  (its import shim and helpers)
from block01.ui.step1_gpu_layer import (  # noqa: E402
    MODE_FUSION, MODE_OVERLAY, DisplaySnapshot, RawPlane, ViewportSnapshot)

W, H = g.W, g.H
RECT = (0.0, float(W), 0.0, float(H))


@pytest.fixture(scope="module")
def app():
    from PyQt5 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _values(dtype):
    top = np.iinfo(dtype).max
    y, x = np.mgrid[:H, :W]
    v = (x * 37 + y * 11) % (top + 1)
    v[0, 0], v[1, 1] = top, 0
    return v.astype(dtype)


def _render(layer, planes, display, viewport=g.VIEWPORT):
    source = g._source(*[g._channel(name, coarse=(plane,)) for name, plane in planes])
    layer.submit(source, display, viewport)
    return layer.readback_rgba_for_test()


def _overlay(mappings, colors=None):
    names = list(mappings)
    return DisplaySnapshot(MODE_OVERLAY, mappings, {n: 1.0 for n in names},
                           colors or {n: (1.0, 0.6, 0.2) for n in names})


@pytest.mark.parametrize("dtype", [np.uint8, np.uint16])
@pytest.mark.parametrize("window", [(0.0, None, 1.0), (40.0, 41.0, 1.0), (3.0, None, 0.45)])
def test_an_integer_plane_draws_exactly_the_float_plane(app, dtype, window):
    top = float(np.iinfo(dtype).max)
    lo, hi, gamma = window
    mapping = (lo, top if hi is None else hi, gamma)
    values = _values(dtype)
    layer = g._layer(app)
    try:
        as_float = _render(layer, [("A", RawPlane(("f",), RECT, values.astype(np.float32)))],
                           _overlay({"A": mapping}))
        as_int = _render(layer, [("A", RawPlane(("i",), RECT, values))],
                         _overlay({"A": mapping}))
        assert np.array_equal(as_int, as_float)
        assert layer._cache.records[("i",)].integer is True
        assert layer._cache.records[("i",)].byte_count == values.nbytes
    finally:
        layer.deleteLater()


def test_an_integer_planes_rectangle_is_its_validity(app):
    values = _values(np.uint8)
    rect = (2, 6, 1, 7)                                   # y0, y1, x0, x1
    float_values = values.astype(np.float32)
    keep = np.zeros(values.shape, bool)
    keep[2:6, 1:7] = True
    float_values[~keep] = np.nan
    mapping = {"A": (0.0, 255.0, 1.0)}
    layer = g._layer(app)
    try:
        as_float = _render(layer, [("A", RawPlane(("f",), RECT, float_values))], _overlay(mapping))
        as_int = _render(layer, [("A", RawPlane(("i",), RECT, values, valid_rect=rect))],
                         _overlay(mapping))
        assert np.array_equal(as_int, as_float)
        assert as_int[..., 3].any() and not as_int[..., 3].all()
        empty = _render(layer, [("A", RawPlane(("e",), RECT, values, valid_rect=(0, 0, 0, 0)))],
                        _overlay(mapping))
        assert not empty[..., 3].any(), "an empty rectangle drew pixels"
    finally:
        layer.deleteLater()


def test_mixed_integer_and_float_channels_compose_as_all_float(app):
    a, b = _values(np.uint16), g._base_planes()[1] * 100.0
    mappings = {"A": (10.0, 900.0, 0.8), "B": (0.0, 100.0, 1.0)}
    colors = {"A": (1.0, 0.0, 0.0), "B": (0.0, 1.0, 0.0)}
    layer = g._layer(app)
    try:
        both_float = _render(layer, [("A", RawPlane(("af",), RECT, a.astype(np.float32))),
                                     ("B", RawPlane(("bf",), RECT, b.astype(np.float32)))],
                             _overlay(mappings, colors))
        mixed = _render(layer, [("A", RawPlane(("ai",), RECT, a)),
                                ("B", RawPlane(("bf2",), RECT, b.astype(np.float32)))],
                        _overlay(mappings, colors))
        assert np.array_equal(mixed, both_float)
        fusion = DisplaySnapshot(MODE_FUSION, mappings, {}, colors,
                                 groups={"g": {"A": 1.0}}, group_weights={"g": 1.0},
                                 nucleus=("B", 1.0))
        f_float = _render(layer, [("A", RawPlane(("af",), RECT, a.astype(np.float32))),
                                  ("B", RawPlane(("bf",), RECT, b.astype(np.float32)))], fusion)
        f_mixed = _render(layer, [("A", RawPlane(("ai",), RECT, a)),
                                  ("B", RawPlane(("bf2",), RECT, b.astype(np.float32)))], fusion)
        assert np.array_equal(f_mixed, f_float)
    finally:
        layer.deleteLater()


def test_the_roi_polygon_clips_an_integer_plane_like_a_float_one(app):
    values = _values(np.uint8)
    polygon = ((1.0, 1.0), (7.0, 2.0), (4.0, 7.0))
    viewport = ViewportSnapshot(RECT, (W, H), 1.0, roi_world_rect=(1.0, 7.0, 1.0, 7.0),
                                roi_polygon_world=polygon)
    mapping = {"A": (0.0, 255.0, 1.0)}
    layer = g._layer(app)
    try:
        as_float = _render(layer, [("A", RawPlane(("f",), RECT, values.astype(np.float32)))],
                           _overlay(mapping), viewport)
        as_int = _render(layer, [("A", RawPlane(("i",), RECT, values))], _overlay(mapping), viewport)
        assert np.array_equal(as_int, as_float)
    finally:
        layer.deleteLater()


def test_an_integer_plane_refuses_a_mask_and_a_rectangle_outside_it():
    from block01.ui.step1_gpu_layer import Step1GpuLayerError, _TextureLru
    cache = _TextureLru(1 << 20)
    values = _values(np.uint8)
    with pytest.raises(Step1GpuLayerError):
        cache._validate_identity(RawPlane(("m",), RECT, values, valid=np.ones(values.shape, bool)))
    with pytest.raises(Step1GpuLayerError):
        cache._validate_identity(RawPlane(("r",), RECT, values, valid_rect=(0, H + 1, 0, W)))
    assert cache.plane_bytes(RawPlane(("b",), RECT, values)) == H * W
    assert cache.plane_bytes(RawPlane(("w",), RECT, values.astype(np.uint16))) == 2 * H * W


@pytest.mark.parametrize("dtype", [np.uint8, np.uint16])
def test_anisotropic_scale_and_an_edge_tile_match_the_float_path(app, dtype):
    """A plane whose world scale differs in x and y (a non-integer
    downsample) and whose valid rectangle is a truncated edge, drawn as an
    integer and as the equivalent float plane with NaN outside."""
    values = _values(dtype)[:, :5]                        # 8 x 5: a cut tile
    rect_world = (0.0, 7.5, 0.0, 8.0)                     # sx 1.5, sy 1.0
    valid = (1, 7, 0, 4)
    float_values = values.astype(np.float32)
    keep = np.zeros(values.shape, bool)
    keep[1:7, 0:4] = True
    float_values[~keep] = np.nan
    top = float(np.iinfo(dtype).max)
    mapping = {"A": (top * 0.1, top * 0.9, 0.7)}
    layer = g._layer(app)
    try:
        as_float = _render(layer, [("A", RawPlane(("f",), rect_world, float_values))],
                           _overlay(mapping))
        as_int = _render(layer, [("A", RawPlane(("i",), rect_world, values, valid_rect=valid))],
                         _overlay(mapping))
        assert np.array_equal(as_int, as_float)
        assert as_int[..., 3].any()
    finally:
        layer.deleteLater()
