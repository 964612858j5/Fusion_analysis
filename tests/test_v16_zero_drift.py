"""Block A1 of v16: switching Step0 / Step1 / Step3 leaves the camera where it
was -- exactly, not within a tolerance.

The window is the real one with both real viewers (the rig of
`test_step1_shared_camera`), and a step is entered the way `_go_to_stepN`
enters it: the page is put on screen FIRST (`setCurrentIndex`, which lays it
out), then `_set_step_active` -- the order A0 found the drift in. The old
suite called `_set_step_active` alone, which skipped the layout that
re-fitted Step1's camera.

A0's findings this pins (docs/v16_A0_viewer_shift_report.md):
  C2  Step1 / Step3 applied an INTEGER rectangle and the aspect lock re-fitted
      it: +-0.5 level-0 px per entry, always the same way, so it accumulated.
  C3  a layout change right after the apply shrank Step1's ViewBox and the
      re-fit changed the magnification (-2.5 % on the first entry).
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")
pytest.importorskip("pyqtgraph")

from PyQt5 import QtTest, QtWidgets  # noqa: E402

from test_step0_compare_tiles import SLIDE_H, SLIDE_W, app  # noqa: E402,F401
from test_step1_shared_camera import _close, _window  # noqa: E402
from camera_write_audit import as_user  # noqa: E402  (block A7)

#: `_go_to_step0/1/3` put these stack pages on screen.
PAGE = {0: 0, 1: 1, 3: 3}
CENTRE_TOL = 1e-6      # level-0 px: float arithmetic only
SCALE_TOL = 1e-9       # relative


def _pump(cycles=6):
    for _ in range(cycles):
        QtWidgets.QApplication.processEvents()
        QtTest.QTest.qWait(10)


def _enter(rig, step):
    rig.w._stack.setCurrentIndex(PAGE[step])
    rig.w._set_step_active(step)
    _pump()


def _on_screen(rig):
    step = rig.w._current_step
    if step == 0:
        return rig.w._step0.current_camera_snapshot()
    mount = rig.w._step1_mount if step == 1 else rig.w._step3_mount
    return mount.current_camera()


def _same(a, b):
    assert a is not None and b is not None, (a, b)
    assert abs(a[0] - b[0]) <= CENTRE_TOL, f"cx {a[0]!r} vs {b[0]!r}"
    assert abs(a[1] - b[1]) <= CENTRE_TOL, f"cy {a[1]!r} vs {b[1]!r}"
    assert abs(a[2] / b[2] - 1.0) <= SCALE_TOL, f"scale {a[2]!r} vs {b[2]!r}"


def _start(rig):
    """A camera no viewer would pick by itself: off-centre, zoomed in, and at
    a centre whose half-extent is not a whole number of pixels."""
    _enter(rig, 0)
    vb = rig.w._step0._full_image_view_box()
    scale = 3.37 * vb.width() / SLIDE_W
    assert rig.w._step0._apply_full_image_camera(SLIDE_W * 0.371, SLIDE_H * 0.613, scale)
    as_user(vb)                 # block A7: this start is the user's own move
    _pump()
    return _on_screen(rig)


@pytest.fixture
def rig(app, monkeypatch, tmp_path):
    r = _window(app, monkeypatch, tmp_path)
    yield r
    _close(r)


def test_every_transition_is_exact(rig):
    start = _start(rig)
    for step in (1, 3, 1, 0, 3, 0):
        _enter(rig, step)
        _same(_on_screen(rig), start)


def test_fifty_round_trips_do_not_drift(rig):
    start = _start(rig)
    for _ in range(50):
        for step in (1, 3, 1, 0):
            _enter(rig, step)
    _same(_on_screen(rig), start)
    _same(rig.w._camera_owner.current(rig.w._camera_dataset()).camera, start)


def test_a_new_drawable_size_keeps_step1s_camera(rig):
    """C3's rule, directly: Step1's ViewBox may change size (a dock, a bar,
    the window); its centre and its magnification do not."""
    _start(rig)
    _enter(rig, 1)
    before = _on_screen(rig)
    size = rig.w.size()
    rig.w.resize(size.width() - 170, size.height() - 90)
    _pump()
    view_box = rig.w._step1_mount.host.stack.view.view_box
    assert view_box.width() != 0
    _same(_on_screen(rig), before)
    rig.w.resize(size)
    _pump()
    _same(_on_screen(rig), before)


def test_a_zoom_after_a_resize_is_the_users(rig):
    """The kept camera follows the user: a zoom is a zoom, and the next
    resize keeps THAT one."""
    _start(rig)
    _enter(rig, 1)
    view_box = rig.w._step1_mount.host.stack.view.view_box
    view_box.scaleBy((0.8, 0.8))
    _pump()
    zoomed = _on_screen(rig)
    size = rig.w.size()
    rig.w.resize(size.width() - 120, size.height())
    _pump()
    _same(_on_screen(rig), zoomed)
