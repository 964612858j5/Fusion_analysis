"""Forced-hardware G1 tests for the isolated Step1 GPU layer.

Set ``BLOCK01_REQUIRE_STEP1_GPU=1`` to run these gates.  In that mode missing
PyOpenGL, a Qt-context failure, shader failure, or a software renderer is a
test failure—not a CPU fallback or an environmental skip.
"""

import importlib.util
import inspect
import os
import pathlib
import sys

import numpy as np
import pytest

if os.environ.get("BLOCK01_REQUIRE_STEP1_GPU") != "1":
    pytest.skip("G1 GPU tests require BLOCK01_REQUIRE_STEP1_GPU=1", allow_module_level=True)

try:
    import OpenGL  # noqa: F401
    import pyqtgraph  # noqa: F401
    from PyQt5 import QtCore, QtWidgets
except ImportError as exc:  # required mode must fail, not skip
    pytest.fail(f"required G1 GPU runtime dependency is unavailable: {exc}", pytrace=False)

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if "block01" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "block01", _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)]
    )
    assert _spec is not None and _spec.loader is not None
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["block01"] = _module
    _spec.loader.exec_module(_module)
sys.path.insert(0, str(_ROOT.parent))

from block01.ui import step1_gpu_layer as gpu_module
from block01.ui.step1_gpu_layer import (
    MODE_FUSION,
    MODE_OVERLAY,
    ChannelSource,
    DisplaySnapshot,
    RawPlane,
    SourceDescriptor,
    Step1GpuLayer,
    Step1GpuLayerError,
    ViewportSnapshot,
)
from block01.viewer.step1_compose import compose
from block01.viewer.explore_view import ExploreView

W = H = 8
VIEWPORT = ViewportSnapshot((0.0, float(W), 0.0, float(H)), (W, H), 1.0)


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _events(app, cycles=8):
    for _ in range(cycles):
        app.processEvents()


def _layer(app, *, budget=1024 * 1024, parent=None):
    layer = Step1GpuLayer(max_raw_texture_bytes=budget, require_hardware=True, parent=parent)
    layer.resize(W, H)
    if parent is None:
        layer.setAttribute(QtCore.Qt.WA_DontShowOnScreen, True)
    layer.show()
    _events(app)
    if not layer._initialized:
        pytest.fail(f"required G1 Qt/PyOpenGL layer did not initialize: {layer._init_error}")
    return layer


def _plane(identity, values, *, rect=(0.0, float(W), 0.0, float(H)), valid=None):
    return RawPlane(identity=identity, world_rect=rect,
                    values=np.asarray(values, dtype=np.float32), valid=valid)


def _source(*items):
    return SourceDescriptor(tuple(items))


def _channel(name, *, coarse=(), fine=(), selected="coarse"):
    return ChannelSource(name, tuple(coarse), tuple(fine), selected)


def _expected(mode, tiles, display):
    rgba, _valid, _missing = compose(
        mode, tiles, weights=display.weights, colors=display.colors,
        mappings=display.mappings, groups=display.groups,
        group_weights=display.group_weights, nucleus=display.nucleus,
    )
    return np.zeros((H, W, 4), dtype=np.uint8) if rgba is None else rgba


def _assert_c1(layer, source, display, tiles, viewport=VIEWPORT):
    result = layer.submit(source, display, viewport)
    actual = layer.readback_rgba_for_test()
    expected = _expected(display.mode, tiles, display)
    diff = np.abs(actual.astype(np.int16) - expected.astype(np.int16))
    assert int(diff.max()) <= 1, {"max": int(diff.max()), "result": result}
    assert np.array_equal(actual[..., 3], expected[..., 3])
    return actual, result


def _base_planes():
    y, x = np.mgrid[:H, :W].astype(np.float32)
    a = np.clip((x + y * 0.4) / 8.0, 0.0, 1.0)
    b = np.clip(((W - 1 - x) * 0.5 + y) / 8.0, 0.0, 1.0)
    return a, b


def test_required_hardware_context_and_one_source_sampler_design(app):
    layer = _layer(app)
    try:
        environment = layer.environment_report()
        assert environment["software_renderer"] is False
        assert "NVIDIA" in environment["gl_renderer"]
        assert environment["max_texture_image_units"] >= 1
        assert environment["raw_texture_format"] == "GL_R32F / GL_RED / GL_FLOAT"
        shader = (gpu_module._SHADER_DIR / "step1_gpu.frag").read_text(encoding="utf-8")
        assert "sampler2D u_raw" in shader
        assert "channels[" not in shader
        assert "step1_compose" not in inspect.getsource(gpu_module)
    finally:
        report = layer.dispose()
        assert report["raw_textures_remaining"] == 0


def test_overlay_c1_gamma_weights_colors_roi_and_hot_parameter_update(app):
    a, b = _base_planes()
    valid_a = np.ones((H, W), dtype=bool)
    valid_a[:2, :2] = False
    valid_b = np.ones((H, W), dtype=bool)
    valid_b[5:, 5:] = False
    source = _source(
        _channel("A", coarse=(_plane(("src", "A", "coarse"), a, valid=valid_a),)),
        _channel("B", coarse=(_plane(("src", "B", "coarse"), b, valid=valid_b),)),
        # This zero-weight plane must never be uploaded.
        _channel("ZERO", coarse=(_plane(("src", "zero"), np.ones((H, W))),)),
    )
    first = DisplaySnapshot(
        mode=MODE_OVERLAY,
        mappings={"A": (0.05, 0.95, 1.5), "B": (0.10, 0.85, 0.75), "ZERO": (0, 1, 1)},
        weights={"A": 1.0, "B": 0.55, "ZERO": 0.0},
        colors={"A": (1.0, 0.2, 0.1), "B": (1.0, 0.2, 0.1), "ZERO": (1, 1, 1)},
    )
    tiles = {"A": (a, valid_a), "B": (b, valid_b), "ZERO": (np.ones((H, W), np.float32), np.ones((H, W), bool))}
    layer = _layer(app)
    try:
        initial, initial_stats = _assert_c1(layer, source, first, tiles)
        assert initial_stats["cache"]["uploads"] == 2
        before = layer.cache_stats().copy()
        changed = dataclasses_replace(first, mappings={"A": (0.2, 0.8, 1e-9), "B": (0.0, 0.65, 1.0), "ZERO": (0, 1, 1)},
                                    weights={"A": 0.5, "B": 1.0, "ZERO": 0.0},
                                    colors={"A": (0.2, 1.0, 0.1), "B": (0.1, 0.1, 1.0), "ZERO": (1, 1, 1)})
        changed_actual, _changed_stats = _assert_c1(layer, source, changed, tiles)
        after = layer.cache_stats()
        assert np.any(initial != changed_actual)
        assert after["uploads"] == before["uploads"]
        assert after["misses"] == before["misses"]
        # Valid dark data is opaque; union of masks makes these areas exact.
        assert changed_actual[3, 3, 3] == 255
        assert changed_actual[0, 0, 3] == 255  # B remains valid there.
        assert changed_actual[7, 7, 3] == 255  # A remains valid there.
    finally:
        layer.dispose()


def test_fusion_c1_groups_max_nucleus_zeros_and_absent_channel(app):
    a, b = _base_planes()
    nucleus = np.full((H, W), 0.75, dtype=np.float32)
    valid = np.ones((H, W), dtype=bool)
    source = _source(
        _channel("A", coarse=(_plane(("f", "A"), a),)),
        _channel("B", coarse=(_plane(("f", "B"), b),)),
        _channel("N", coarse=(_plane(("f", "N"), nucleus),)),
    )
    display = DisplaySnapshot(
        mode=MODE_FUSION,
        mappings={"A": (0, 1, 1), "B": (0, 1, 1), "N": (0, 1, 1)},
        groups={"low": {"A": 0.25, "B": 0.75, "MISSING": 1.0}, "high": {"A": 1.0}, "zero": {"B": 1.0}},
        group_weights={"low": 0.6, "high": 0.4, "zero": 0.0},
        nucleus=("N", 0.5),
    )
    tiles = {"A": (a, valid), "B": (b, valid), "N": (nucleus, valid)}
    layer = _layer(app)
    try:
        actual, stats = _assert_c1(layer, source, display, tiles)
        assert np.any(actual[..., 0] != 0)
        assert np.any(actual[..., 2] != 0)
        assert np.all(actual[..., 1] == 0)
        assert stats["cache"]["uploads"] == 3
        zero = dataclasses_replace(display, nucleus=("N", 0.0), group_weights={"low": 0.0, "high": 0.0, "zero": 0.0})
        zero_actual, _ = _assert_c1(layer, source, zero, tiles)
        assert np.array_equal(zero_actual, np.zeros_like(zero_actual))
    finally:
        layer.dispose()


def test_overlay_raw_like_nan_missing_mapping_and_transparent_roi(app):
    raw = (np.arange(H * W, dtype=np.uint8).reshape(H, W) * 3).astype(np.uint8)
    floating = np.full((H, W), 0.4, dtype=np.float32)
    floating[1, 1] = np.nan
    floating[2, 2] = np.inf
    valid = np.ones((H, W), dtype=bool)
    valid[0, 0] = False
    source = _source(
        _channel("RAW", coarse=(_plane(("edge", "raw"), raw),)),
        _channel("FLOAT", coarse=(_plane(("edge", "float"), floating, valid=valid),)),
        _channel("NO_WINDOW", coarse=(_plane(("edge", "window"), np.ones((H, W))),)),
    )
    display = DisplaySnapshot(MODE_OVERLAY,
                              {"RAW": (0, 255, 1), "FLOAT": (0, 1, 1)},
                              {"RAW": 1, "FLOAT": 1, "NO_WINDOW": 1},
                              {"RAW": (1, 0, 0), "FLOAT": (0, 1, 0), "NO_WINDOW": (0, 0, 1)})
    tiles = {"RAW": (raw, np.ones((H, W), bool)), "FLOAT": (floating, valid),
             "NO_WINDOW": (np.ones((H, W), np.float32), np.ones((H, W), bool))}
    layer = _layer(app)
    try:
        actual, stats = _assert_c1(layer, source, display, tiles)
        assert stats["missing_windows"] == ("NO_WINDOW",)
        assert stats["cache"]["uploads"] == 2
        assert actual[1, 1, 3] == 255  # RAW remains valid where FLOAT is NaN.
        assert actual[0, 0, 3] == 255  # RAW remains valid where FLOAT mask is false.
    finally:
        layer.dispose()


def test_fusion_missing_mapping_and_active_working_set_budget_fail_closed(app):
    a, b = _base_planes()
    source = _source(
        _channel("A", coarse=(_plane(("budget", "A"), a),)),
        _channel("B", coarse=(_plane(("budget", "B"), b),)),
    )
    display = DisplaySnapshot(MODE_FUSION, {"A": (0, 1, 1)}, groups={"g": {"A": 1, "B": 1}},
                              group_weights={"g": 1}, nucleus=("", 0))
    tiles = {"A": (a, np.ones((H, W), bool)), "B": (b, np.ones((H, W), bool))}
    layer = _layer(app, budget=H * W * 4)
    try:
        # B lacks a mapping and is omitted before cache planning, so only A fits.
        _actual, stats = _assert_c1(layer, source, display, tiles)
        assert stats["missing_windows"] == ("B",)
        assert stats["cache"]["uploads"] == 1
        too_many = dataclasses_replace(display, mappings={"A": (0, 1, 1), "B": (0, 1, 1)})
        with pytest.raises(Step1GpuLayerError, match="working set"):
            layer.submit(source, too_many, VIEWPORT)
        assert layer.cache_stats()["bytes"] == H * W * 4
    finally:
        layer.dispose()

    coarse_a = np.full((H, W), 0.2, dtype=np.float32)
    fine_a = np.full((4, 4), 0.9, dtype=np.float32)
    coarse_b = np.full((H, W), 0.4, dtype=np.float32)
    merged_a = coarse_a.copy()
    merged_a[2:6, 2:6] = fine_a
    source = _source(
        _channel("A", coarse=(_plane(("lod", "A", "coarse"), coarse_a),),
                 fine=(_plane(("lod", "A", "fine"), fine_a, rect=(2, 6, 2, 6)),), selected="fine"),
        _channel("B", coarse=(_plane(("lod", "B", "coarse"), coarse_b),), selected="coarse"),
    )
    display = DisplaySnapshot(MODE_OVERLAY, {"A": (0, 1, 1), "B": (0, 1, 1)},
                              {"A": 1, "B": 1}, {"A": (1, 0, 0), "B": (0, 1, 0)})
    tiles = {"A": (merged_a, np.ones((H, W), bool)), "B": (coarse_b, np.ones((H, W), bool))}
    layer = _layer(app)
    try:
        actual, _ = _assert_c1(layer, source, display, tiles)
        assert actual[3, 3, 0] > actual[0, 0, 0]
        assert np.all(actual[..., 1] > 0)
    finally:
        layer.dispose()


def test_multi_pass_33_channels_lru_eviction_and_reupload(app):
    channels = []
    tiles = {}
    mappings = {}
    weights = {}
    colors = {}
    for index in range(33):
        name = f"C{index}"
        values = np.full((H, W), (index % 3 + 1) / 100.0, dtype=np.float32)
        channels.append(_channel(name, coarse=(_plane(("many", index), values),)))
        tiles[name] = (values, np.ones((H, W), bool))
        mappings[name] = (0, 1, 1)
        weights[name] = 1.0
        colors[name] = (1.0, 0.0, 0.0)
    display = DisplaySnapshot(MODE_OVERLAY, mappings, weights, colors)
    # 33 planes * 8 * 8 * 4 exactly fits this active descriptor.
    layer = _layer(app, budget=33 * H * W * 4)
    try:
        _actual, stats = _assert_c1(layer, _source(*channels), display, tiles)
        assert stats["pass_count"] >= 33 * 2 + 1
        assert stats["cache"]["textures"] == 33
        # Submit a different two-plane set under the same fixed budget, then
        # restore C0/C1. This exercises LRU GL deletion and re-upload.
        small = _source(
            _channel("X", coarse=(_plane(("evict", "X"), np.full((H, W), 0.1)),)),
            _channel("Y", coarse=(_plane(("evict", "Y"), np.full((H, W), 0.2)),)),
        )
        small_display = DisplaySnapshot(MODE_OVERLAY, {"X": (0, 1, 1), "Y": (0, 1, 1)},
                                        {"X": 1, "Y": 1}, {"X": (1, 0, 0), "Y": (0, 1, 0)})
        small_tiles = {"X": (np.full((H, W), 0.1, np.float32), np.ones((H, W), bool)),
                       "Y": (np.full((H, W), 0.2, np.float32), np.ones((H, W), bool))}
        _assert_c1(layer, small, small_display, small_tiles)
        assert layer.cache_stats()["evictions"] > 0
        _assert_c1(layer, _source(*channels[:2]),
                   DisplaySnapshot(MODE_OVERLAY, {name: mappings[name] for name in ("C0", "C1")},
                                   {name: 1 for name in ("C0", "C1")}, {name: colors[name] for name in ("C0", "C1")}),
                   {name: tiles[name] for name in ("C0", "C1")})
        assert layer.cache_stats()["uploads"] > 35
    finally:
        layer.dispose()


def test_viewbox_attach_and_idempotent_dispose(app):
    view = ExploreView()
    view.resize(160, 120)
    view.show()
    _events(app)
    layer = Step1GpuLayer(max_raw_texture_bytes=4096, require_hardware=True)
    layer.attach(view)
    layer.show()
    _events(app)
    try:
        assert layer.parent() is view.graphics.viewport()
        assert layer.testAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        owner = view.view_box
        owner.setRange(xRange=(1, 7), yRange=(1, 7), padding=0)
        _events(app)
        assert view.view_box is owner
        assert layer._attached_range is not None
        first = layer.dispose()
        second = layer.dispose()
        assert first["raw_textures_remaining"] == 0
        assert first["transient_targets_remaining"] == 0
        assert second["already_disposed"]
    finally:
        layer.close()
        view.close()


def dataclasses_replace(instance, **changes):
    """Keep snapshot mutation out of the tests and caller contract."""
    import dataclasses
    return dataclasses.replace(instance, **changes)
