"""Block A9 §40: buffer swaps are asked not to wait for the display refresh
(swap interval 0), unless BLOCK01_VSYNC=1; the Step1 GPU layer asks for the
application's interval, since Qt5 hands a widget's interval up to its window."""

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


def test_swaps_do_not_wait_for_the_refresh_by_default(monkeypatch, default_format):
    monkeypatch.delenv("BLOCK01_VSYNC", raising=False)
    before = QtGui.QSurfaceFormat.defaultFormat()
    assert gpu_warmup.configure_surface_format() is True
    after = QtGui.QSurfaceFormat.defaultFormat()
    assert after.swapInterval() == 0
    # nothing else of the default format is touched
    assert (after.majorVersion(), after.minorVersion(), after.profile(), after.depthBufferSize()) == \
        (before.majorVersion(), before.minorVersion(), before.profile(), before.depthBufferSize())


def test_vsync_1_leaves_the_default_alone(monkeypatch, default_format):
    monkeypatch.setenv("BLOCK01_VSYNC", "1")
    fmt = QtGui.QSurfaceFormat.defaultFormat()
    fmt.setSwapInterval(1)
    QtGui.QSurfaceFormat.setDefaultFormat(fmt)
    assert gpu_warmup.configure_surface_format() is False
    assert QtGui.QSurfaceFormat.defaultFormat().swapInterval() == 1


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
