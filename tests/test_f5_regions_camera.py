"""Step1 and Regions show the same place (§13.7, user 2026-10-20): one
camera, moved with REAL input -- wheel events and a left drag on the viewer's
viewport, a table jump and a Tissue Navigator click -- both ways, repeatedly.
(The camera owner was already shared; this locks it with real events.)"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")
pytest.importorskip("pyqtgraph")

from PyQt5 import QtCore, QtGui, QtTest, QtWidgets  # noqa: E402

from test_regions_r2 import _enter_regions, _pump  # noqa: E402
from test_step0_compare_tiles import app  # noqa: E402,F401
from test_step1_shared_camera import SLIDE_H, SLIDE_W, _close, _in, _same, _window  # noqa: E402


def _wheel(view, steps=3):
    vp = view.graphics.viewport()
    pos = QtCore.QPointF(vp.width() * 0.3, vp.height() * 0.4)
    for _ in range(steps):
        ev = QtGui.QWheelEvent(pos, QtCore.QPointF(vp.mapToGlobal(pos.toPoint())), QtCore.QPoint(0, 0),
                               QtCore.QPoint(0, 120), QtCore.Qt.NoButton, QtCore.Qt.NoModifier,
                               QtCore.Qt.NoScrollPhase, False)
        QtWidgets.QApplication.sendEvent(vp, ev)
        _pump(2)


def _drag(view, dx, dy):
    """A real left drag: press, moves that CARRY the held button, release
    (`QTest.mouseMove` carries none, so pyqtgraph would see no drag)."""
    vp = view.graphics.viewport()
    p0 = QtCore.QPointF(vp.width() / 2, vp.height() / 2)

    def send(kind, pos, button, buttons):
        ev = QtGui.QMouseEvent(kind, pos, button, buttons, QtCore.Qt.NoModifier)
        QtWidgets.QApplication.sendEvent(vp, ev)
    send(QtCore.QEvent.MouseButtonPress, p0, QtCore.Qt.LeftButton, QtCore.Qt.LeftButton)
    for i in range(1, 9):
        send(QtCore.QEvent.MouseMove, p0 + QtCore.QPointF(dx * i / 8, dy * i / 8),
             QtCore.Qt.NoButton, QtCore.Qt.LeftButton)
        _pump(1)
    send(QtCore.QEvent.MouseButtonRelease, p0 + QtCore.QPointF(dx, dy), QtCore.Qt.LeftButton,
         QtCore.Qt.NoButton)
    _pump(3)


def _to_step1(rig):
    rig.w._stack.setCurrentIndex(1)
    _in(rig, 1)
    _pump(10)


def test_step1_and_regions_keep_one_camera_under_real_input(app, monkeypatch, tmp_path):
    rig = _window(app, monkeypatch, tmp_path)
    try:
        w = rig.w
        w._step1_context_ready = True
        _to_step1(rig)
        s1 = w._step1_mount.host.stack.view
        before = w._step1_mount.current_camera()
        _wheel(s1)
        after_wheel = w._step1_mount.current_camera()
        assert after_wheel[2] != before[2]                                  # the wheel zoomed
        _drag(s1, 60, 30)
        cam1 = w._step1_mount.current_camera()
        assert (cam1[0], cam1[1]) != (after_wheel[0], after_wheel[1])       # the drag panned
        _enter_regions(rig)
        _pump(10)
        reg = w._regions_view.mount
        _same(reg.current_camera(), cam1)                                  # Step1 -> Regions
        assert reg.host.stack.view.view_box.width() == s1.view_box.width()
        _wheel(reg.host.stack.view, 2)
        after_wheel = reg.current_camera()
        assert after_wheel[2] != cam1[2]
        _drag(reg.host.stack.view, -80, 40)
        cam_r = reg.current_camera()
        assert (cam_r[0], cam_r[1]) != (after_wheel[0], after_wheel[1])
        _to_step1(rig)
        _same(w._step1_mount.current_camera(), cam_r)                      # Regions -> Step1
        _enter_regions(rig)
        _pump(10)
        _same(reg.current_camera(), cam_r)                                 # and back again
        w._regions_jump_box([100, 300, 200, 400])                          # a table / Prev / Next jump
        _pump(10)
        cam_j = reg.current_camera()
        _to_step1(rig)
        _same(w._step1_mount.current_camera(), cam_j)
        _enter_regions(rig)
        _pump(10)
        assert w._on_step1_tissue_navigate(SLIDE_H * 0.7, SLIDE_W * 0.3)   # a Tissue Navigator click
        _pump(10)
        cam_n = reg.current_camera()
        _to_step1(rig)
        _same(w._step1_mount.current_camera(), cam_n)
    finally:
        _close(rig)
