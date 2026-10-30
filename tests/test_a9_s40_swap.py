"""Buffer swaps wait for the display refresh (swap interval 1) by default;
BLOCK01_VSYNC=0 asks for the Block A9 §40 interval 0 again; the Step1 GPU
layer asks for the application's interval, since Qt5 hands a widget's
interval up to its window."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")

from PyQt5 import QtGui, QtWidgets  # noqa: E402

from block01.ui import gpu_warmup  # noqa: E402


@pytest.fixture
def default_format():
    saved = QtGui.QSurfaceFormat.defaultFormat()
    yield
    QtGui.QSurfaceFormat.setDefaultFormat(saved)


def test_swaps_wait_for_the_refresh_by_default(monkeypatch, default_format):
    monkeypatch.delenv("BLOCK01_VSYNC", raising=False)
    before = QtGui.QSurfaceFormat.defaultFormat()
    fmt = QtGui.QSurfaceFormat.defaultFormat()
    fmt.setSwapInterval(0)
    QtGui.QSurfaceFormat.setDefaultFormat(fmt)
    assert gpu_warmup.configure_surface_format() == 1
    after = QtGui.QSurfaceFormat.defaultFormat()
    assert after.swapInterval() == 1
    # nothing else of the default format is touched
    assert (after.majorVersion(), after.minorVersion(), after.profile(), after.depthBufferSize()) == \
        (before.majorVersion(), before.minorVersion(), before.profile(), before.depthBufferSize())


def test_vsync_0_asks_for_no_wait(monkeypatch, default_format):
    monkeypatch.setenv("BLOCK01_VSYNC", "0")
    assert gpu_warmup.configure_surface_format() == 0
    assert QtGui.QSurfaceFormat.defaultFormat().swapInterval() == 0


@pytest.mark.parametrize("interval", [0, 1])
def test_the_gpu_layer_asks_for_the_applications_interval(monkeypatch, default_format, interval):
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    fmt = QtGui.QSurfaceFormat.defaultFormat()
    fmt.setSwapInterval(interval)
    QtGui.QSurfaceFormat.setDefaultFormat(fmt)
    from block01.ui.step1_gpu_layer import Step1GpuLayer
    layer = Step1GpuLayer(max_raw_texture_bytes=1 << 20)
    try:
        assert layer.format().swapInterval() == interval
        assert (layer.format().majorVersion(), layer.format().minorVersion()) == (3, 3)
    finally:
        layer.deleteLater()
