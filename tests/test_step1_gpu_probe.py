"""G0.1 Qt/PyOpenGL probe checks.

The approved PyOpenGL route uses the Qt-created current context.  When the
isolated interpreter lacks PyOpenGL or cannot create a compatible Qt context,
these tests skip with that exact environmental reason; they never substitute a
CPU or a different OpenGL backend.  An offscreen pass remains FBO evidence,
not desktop-present timing evidence.
"""

import importlib.util
import os
import pathlib
import sys

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

pytest.importorskip("PyQt5")
pytest.importorskip("pyqtgraph")
pytest.importorskip("OpenGL")

from PyQt5 import QtCore, QtWidgets  # noqa: E402

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "benchmark_step1_gpu_demo.py"
_SPEC = importlib.util.spec_from_file_location("step1_gpu_g0_1_probe", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_GPU_PROBE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _GPU_PROBE
_SPEC.loader.exec_module(_GPU_PROBE)

CHANNEL_A = _GPU_PROBE.CHANNEL_A
CHANNEL_B = _GPU_PROBE.CHANNEL_B
DisplayParameters = _GPU_PROBE.DisplayParameters
GpuProbeWidget = _GPU_PROBE.GpuProbeWidget
ProbeBlocked = _GPU_PROBE.ProbeBlocked
ViewBoxOverlayAdapter = _GPU_PROBE.ViewBoxOverlayAdapter
comparison = _GPU_PROBE.comparison
cpu_reference = _GPU_PROBE.cpu_reference
create_probe = _GPU_PROBE.create_probe
process_events = _GPU_PROBE.process_events
synthetic_source = _GPU_PROBE.synthetic_source
explore_view_class = _GPU_PROBE._explore_view_class


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def ready_probe(app):
    source = synthetic_source()
    try:
        _app, widget = create_probe(source)
    except ProbeBlocked as exc:
        pytest.skip(f"G0.1 Qt/PyOpenGL draw path unavailable: {exc}")
    yield source, widget
    widget.dispose()
    widget.close()
    process_events(app)


def _assert_oracle(actual, expected):
    stats = comparison(actual, expected)
    assert stats["max_abs_lsb"] <= 1, stats
    assert stats["alpha_mismatch_components"] == 0, stats


def test_qt_current_context_has_real_pyopengl_two_channel_resources(ready_probe):
    source, widget = ready_probe
    assert widget._initialized
    assert len(widget._textures) == 4
    assert widget.capabilities["pyopengl_version"]
    assert widget.capabilities["texture_format"] == "GL_R32F / GL_RED / GL_FLOAT"
    assert source.counters.texture_uploads == 4


def test_fbo_readback_matches_existing_step1_cpu_overlay_for_gamma_above_and_below_one(ready_probe):
    source, widget = ready_probe
    selected = {CHANNEL_A: "fine", CHANNEL_B: "coarse"}
    widget.set_selected_levels(selected)
    actual = widget.readback_rgba()
    _assert_oracle(actual, cpu_reference(source, selected, widget.parameters))


def test_intensity_color_weight_uniform_updates_change_pixels_without_source_or_upload(ready_probe):
    source, widget = ready_probe
    selected = {CHANNEL_A: "fine", CHANNEL_B: "coarse"}
    widget.set_selected_levels(selected)
    before = widget.readback_rgba()
    counters_before = (source.counters.source_selections, source.counters.texture_uploads)
    changed = DisplayParameters(
        mappings={CHANNEL_A: (0.17, 0.82, 0.80), CHANNEL_B: (0.06, 0.78, 1.50)},
        colors={CHANNEL_A: (0.20, 1.0, 0.18), CHANNEL_B: (0.95, 0.08, 0.72)},
        weights={CHANNEL_A: 0.51, CHANNEL_B: 0.92},
    )
    widget.set_display_parameters(changed)
    after = widget.readback_rgba()
    _assert_oracle(after, cpu_reference(source, selected, changed))
    assert np.any(after != before)
    assert (source.counters.source_selections, source.counters.texture_uploads) == counters_before


def test_each_channel_keeps_its_own_coarse_or_fine_fallback(ready_probe):
    source, widget = ready_probe
    a_fine_b_coarse = {CHANNEL_A: "fine", CHANNEL_B: "coarse"}
    a_coarse_b_fine = {CHANNEL_A: "coarse", CHANNEL_B: "fine"}
    widget.set_selected_levels(a_fine_b_coarse)
    first = widget.readback_rgba()
    _assert_oracle(first, cpu_reference(source, a_fine_b_coarse, widget.parameters))
    widget.set_selected_levels(a_coarse_b_fine)
    second = widget.readback_rgba()
    _assert_oracle(second, cpu_reference(source, a_coarse_b_fine, widget.parameters))
    assert np.any(first != second)


def test_existing_viewbox_remains_camera_owner_and_overlay_receives_world_transform(app):
    view = explore_view_class()()
    view.resize(180, 140)
    view.show()
    process_events(app)
    overlay_source = synthetic_source()
    overlay = GpuProbeWidget(overlay_source, view.graphics.viewport())
    overlay.show()
    process_events(app)
    if not overlay._initialized:
        pytest.skip(f"G0.1 ViewBox overlay unavailable: {overlay._init_error}")
    owner = view.view_box
    adapter = ViewBoxOverlayAdapter(view, overlay)
    owner.setRange(xRange=(3, 21), yRange=(6, 26), padding=0)
    process_events(app)
    assert view.view_box is owner
    assert adapter.range_events >= 1
    assert tuple(adapter.last_world_rect) == overlay._view_world_rect
    assert overlay.testAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
    assert overlay.geometry() == view.graphics.viewport().rect()
    assert np.any(overlay.readback_rgba() != 0)
    first = overlay.dispose()
    second = overlay.dispose()
    overlay.close()
    view.close()
    assert first["textures_remaining"] == 0
    assert first["threads_created"] == 0
    assert second["already_disposed"]
