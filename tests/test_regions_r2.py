"""Regions R2 (docs/region_qc_module_plan.md §2, §5 R2) with the real window
and real viewers (the rig of `test_step1_shared_camera`): the third
resident viewer opens on entry, pauses when another step is on screen and
reopens after a close; the overlay is attached to the viewer on screen and
visible; the channel scope is Step1's; the camera goes both ways between
Step0, Regions and Step1; the entry check refuses without a handoff.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")
pytest.importorskip("pyqtgraph")

from PyQt5 import QtTest, QtWidgets  # noqa: E402

from test_step0_compare_tiles import app  # noqa: E402,F401
from test_step1_shared_camera import (SLIDE_H, SLIDE_W, _close, _in, _same,  # noqa: E402
                                      _step0_camera, _user_step0, _window, as_user)


def _pump(n=6):
    for _ in range(n):
        QtWidgets.QApplication.processEvents()
        QtTest.QTest.qWait(10)


def _enter_regions(rig):
    rig.w._step1_context_ready = True
    rig.w._stack.setCurrentWidget(rig.w._regions)
    _in(rig, rig.w.REGIONS_STEP)
    _pump()


def test_the_third_viewer_opens_carries_the_overlay_and_pauses_when_left(app, monkeypatch, tmp_path):
    rig = _window(app, monkeypatch, tmp_path)
    try:
        _enter_regions(rig)
        host = rig.w._regions_view
        assert host.mount is not None and host.mount.host.stack is not None
        assert host.overlay is not None and host.overlay.isVisible()
        assert host.overlay.viewport() is host.mount.host.stack.view.graphics.viewport()
        assert rig.w._display.state.scope() == "step1"
        assert rig.w._channel_dock.parentWidget() is rig.w._regions.channels_host().parentWidget()
        rig.w._stack.setCurrentIndex(1)
        _in(rig, 1)
        assert host.mount.paused
        # the dataset moved: the viewer closes, the overlay goes; the next entry reopens both
        host.close()
        assert host.overlay is None and host.mount.host.stack is None
        _enter_regions(rig)
        assert host.mount.host.stack is not None and host.overlay is not None and host.overlay.isVisible()
    finally:
        _close(rig)


def test_the_camera_goes_both_ways_between_step0_regions_and_step1(app, monkeypatch, tmp_path):
    rig = _window(app, monkeypatch, tmp_path)
    try:
        _in(rig, 0)
        rig.w._step0._apply_full_image_view_rect((SLIDE_W * 0.2, SLIDE_H * 0.2, SLIDE_W * 0.3, SLIDE_H * 0.3))
        _user_step0(rig)
        _pump()
        cam0 = _step0_camera(rig)
        _enter_regions(rig)
        mount = rig.w._regions_view.mount
        _same(mount.current_camera(), cam0)                           # Regions opens where Step0 was
        vb = mount.host.stack.view.view_box
        vb.setRange(xRange=(SLIDE_W * 0.5, SLIDE_W * 0.6), yRange=(SLIDE_H * 0.5, SLIDE_H * 0.6), padding=0)
        as_user(vb)
        _pump()
        cam_r = mount.current_camera()
        rig.w._step1_context_ready = True
        rig.w._stack.setCurrentIndex(1)
        _in(rig, 1)
        _pump()
        _same(rig.w._step1_mount.current_camera(), cam_r)             # ...and Step1 opens where Regions was
    finally:
        _close(rig)


def test_regions_is_refused_without_a_handoff_and_jumps_go_to_the_shared_camera(app, monkeypatch, tmp_path):
    rig = _window(app, monkeypatch, tmp_path)
    try:
        said = []
        monkeypatch.setattr(QtWidgets.QMessageBox, "information", staticmethod(lambda *a, **k: said.append(a[2])))
        rig.w._step1_context_ready = False
        rig.w.step0_output = {}
        rig.w._go_to_regions()
        assert said and rig.w._current_step != rig.w.REGIONS_STEP
        _enter_regions(rig)
        rig.w._regions_jump_box([100, 300, 200, 400])
        _pump()
        shot = rig.w._camera_owner.current(rig.w._camera_dataset(rig.w.REGIONS_STEP))
        assert shot is not None
        cx, cy, _s = shot.camera
        assert abs(cx - 300) < 60 and abs(cy - 200) < 60
    finally:
        _close(rig)


def test_the_tissue_navigator_lands_the_regions_viewer_whatever_tool_is_in_use(app, monkeypatch, tmp_path):
    """User 2026-10-18: Regions keeps the Tissue Preview; a click lands the
    viewer there even in the middle of drawing a line."""
    rig = _window(app, monkeypatch, tmp_path)
    try:
        _enter_regions(rig)
        assert rig.w._btn_regions_tissue_nav.text() == "Tissue Navigator"
        page = rig.w._regions
        page._set_tool("line")
        page.ctl.press(10.0, 10.0)                                     # a line being drawn
        shown = []
        monkeypatch.setattr(rig.w._display, "show_navigator", lambda *a, **k: shown.append((a, k)))
        rig.w._btn_regions_tissue_nav.click()                         # the real button
        assert shown and shown[0][1] == {"roi_policy": "read_only", "patch_editable": False}
        assert rig.w._on_step1_tissue_navigate(SLIDE_H * 0.7, SLIDE_W * 0.3)
        _pump()
        cx, cy, _s = rig.w._regions_view.mount.current_camera()
        assert abs(cx - SLIDE_W * 0.3) < SLIDE_W * 0.05 and abs(cy - SLIDE_H * 0.7) < SLIDE_H * 0.05
    finally:
        _close(rig)
