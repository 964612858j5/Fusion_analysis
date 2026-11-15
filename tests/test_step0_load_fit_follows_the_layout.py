"""W2g (user report 2026-11-15): Step 0.5 opened zoomed out, and Step0 then
followed -- on the first visit of a new window only.

The first fit after a Load is written into the shared camera while the window
is still being laid out; the ViewBox's resize then fits the slide to its final
size, which nobody wrote (0.231 written, 0.256 on screen on the laptop). The
first entry elsewhere put the user at the stale fit. Now, while the load's fit
is still the last thing written, a resize writes it again; once the user has
moved, a resize writes nothing (A7, `test_v16_a7_camera_owner`).
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("block01.qt")
pytest.importorskip("pyqtgraph")

from camera_write_audit import as_user, camera_writes  # noqa: E402,F401
from test_step0_compare_tiles import SLIDE_H, SLIDE_W, app  # noqa: E402,F401
from test_step1_shared_camera import _close, _window  # noqa: E402
from test_v16_zero_drift import _enter, _pump  # noqa: E402


@pytest.fixture
def rig(app, monkeypatch, tmp_path):
    r = _window(app, monkeypatch, tmp_path)
    yield r
    _close(r)


def _owner_scale(rig):
    shot = rig.w._camera_owner.current(rig.w._camera_dataset(0))
    return None if shot is None else shot.scale


def _seeded(rig, camera_writes):
    _enter(rig, 0)
    page = rig.w._step0
    page._camera_seed_pending = True                         # set by a dataset commit
    page._show_full_image()
    _pump()
    assert camera_writes.count("jump") >= 1
    return page


def test_a_resize_after_the_load_fit_writes_the_fit_on_screen(rig, camera_writes):
    page = _seeded(rig, camera_writes)
    size = rig.w.size()
    rig.w.resize(size.width() + 300, size.height() + 200)    # the layout settles
    _pump()
    on_screen = page.current_camera_snapshot()[2]
    assert _owner_scale(rig) == pytest.approx(on_screen, rel=1e-6)
    # ...so entering another step and coming back keeps Step0 where it is
    _enter(rig, 1)
    _enter(rig, 0)
    _pump()
    assert page.current_camera_snapshot()[2] == pytest.approx(on_screen, rel=1e-3)
    rig.w.resize(size)
    _pump()


def test_once_the_user_moved_a_resize_writes_nothing(rig, camera_writes):
    page = _seeded(rig, camera_writes)
    vb = page._full_image_view_box()
    page._apply_full_image_camera(SLIDE_W * 0.4, SLIDE_H * 0.6, 2.5 * vb.width() / SLIDE_W)
    as_user(vb)
    _pump()
    camera_writes.clear()
    size = rig.w.size()
    rig.w.resize(size.width() + 300, size.height() + 200)
    _pump()
    rig.w.resize(size)
    _pump()
    assert camera_writes.count() == 0, camera_writes


def test_a_move_in_another_step_also_ends_the_load_fit(rig, camera_writes):
    """codex W2g: the user moved only in Step1 (or Step 0.5) and came back;
    a resize of Step0 must not write over that move."""
    _seeded(rig, camera_writes)
    _enter(rig, 1)
    vb = rig.w._step1_mount.host.stack.view.view_box
    x0, x1 = vb.viewRange()[0]
    y0, y1 = vb.viewRange()[1]
    vb.setRange(xRange=(x0 + 40, x1 - 40), yRange=(y0 + 40, y1 - 40), padding=0)
    as_user(vb)
    _pump()
    moved = rig.w._camera_owner.current(rig.w._camera_dataset(1)).camera
    _enter(rig, 0)
    camera_writes.clear()
    size = rig.w.size()
    rig.w.resize(size.width() + 300, size.height() + 200)
    _pump()
    rig.w.resize(size)
    _pump()
    assert camera_writes.count() == 0, camera_writes
    assert rig.w._camera_owner.current(rig.w._camera_dataset(0)).camera == moved
