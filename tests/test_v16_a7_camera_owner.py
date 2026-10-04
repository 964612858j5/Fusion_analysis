"""Block A7 (E4 / gate 8): the camera owner holds the user's intent.

docs/v16_A7_application.md §6. The window is the real one with both real
viewers (the rig of `test_step1_shared_camera`); steps are entered the way
`_go_to_stepN` enters them (`test_v16_zero_drift._enter`).

  * C4 -- ZERO write-back: switching steps, a resize, a same-source refresh,
    a channel switched on, leaving compare and the mirrored compare panels
    write nothing into the owner. This test also runs on the code from before
    A7 (the fixture falls back to `MainWindow._remember_camera`) and must fail
    there: entering a step read the viewer back and wrote it.
  * §3.1 -- every user entry writes: a drag / wheel once per notification,
    every explicit command exactly once.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")
pytest.importorskip("pyqtgraph")

from PyQt5 import QtWidgets  # noqa: E402

from camera_write_audit import as_user, camera_writes  # noqa: E402,F401
from test_step0_compare_tiles import SLIDE_H, SLIDE_W, app  # noqa: E402,F401
from test_step1_shared_camera import _close, _window  # noqa: E402
from test_v16_zero_drift import _enter, _pump  # noqa: E402


@pytest.fixture
def rig(app, monkeypatch, tmp_path):
    r = _window(app, monkeypatch, tmp_path)
    yield r
    _close(r)


def _user_start(rig):
    """The user zooms into Step0's full image somewhere off-centre."""
    _enter(rig, 0)
    page = rig.w._step0
    vb = page._full_image_view_box()
    scale = 2.7 * vb.width() / SLIDE_W
    page._apply_full_image_camera(SLIDE_W * 0.41, SLIDE_H * 0.57, scale)
    as_user(vb)
    _pump()


# ── C4: nothing but the user writes ───────────────────────────────────

def test_switching_resizing_and_refreshing_write_nothing(rig, camera_writes):
    _user_start(rig)
    camera_writes.clear()
    for step in (1, 3, 1, 0, 3, 0, 1):
        _enter(rig, step)
    size = rig.w.size()
    rig.w.resize(size.width() - 150, size.height() - 80)
    _pump()
    rig.w.resize(size)
    _pump()
    rig.w._step1_mount.sync_source("a7-test")             # same source
    _pump()
    state = rig.w._display.state
    with state.using_scope("step1"):
        state.set_display_visible("CD8", True, origin="a7-test")
    _pump()
    for step in (3, 0):
        _enter(rig, step)
    assert camera_writes.count() == 0, camera_writes


def test_compare_entry_is_one_jump_leaving_and_mirroring_write_nothing(
        rig, camera_writes):
    _user_start(rig)
    page = rig.w._step0
    camera_writes.clear()
    page._enter_compare_mode()
    _pump()
    assert camera_writes.count("jump") == 1 and camera_writes.count() == 1, camera_writes
    camera_writes.clear()
    strip = page._compare_strip_widget
    cx, cy, scale = strip.camera(0)
    strip.set_camera(cx + 300.0, cy + 200.0, scale)          # the strip's own linking
    _pump()
    assert camera_writes.count() == 0, camera_writes
    page._exit_compare_mode()
    _pump()
    assert camera_writes.count() == 0, camera_writes


# ── §3.1: every user entry writes ─────────────────────────────────────

def test_a_drag_or_wheel_writes_once_per_notification(rig, camera_writes):
    _user_start(rig)
    for step, box in ((0, lambda: rig.w._step0._full_image_view_box()),
                      (1, lambda: rig.w._step1_mount.host.stack.view.view_box),
                      (3, lambda: rig.w._step3_mount.host.stack.view.view_box)):
        _enter(rig, step)
        camera_writes.clear()
        view_box = box()
        view_box.translateBy(x=120.0, y=80.0)
        as_user(view_box)
        view_box.scaleBy((0.9, 0.9))
        as_user(view_box)
        _pump()
        assert camera_writes.count("user_navigated") == 2, (step, camera_writes)
        assert camera_writes.count() == 2, (step, camera_writes)


def test_a_compare_panel_drag_is_the_users_and_step1_follows_it(rig, camera_writes):
    """Ruling 15: wherever the user looks in compare is where Step1 opens."""
    _user_start(rig)
    page = rig.w._step0
    page._enter_compare_mode()
    _pump()
    camera_writes.clear()
    view_box = page._compare_strip_widget.view_boxes[2]      # any panel
    view_box.translateBy(x=-400.0, y=250.0)
    as_user(view_box)
    _pump()
    assert camera_writes.count("user_navigated") == 1, camera_writes
    compare = page._compare_camera()
    _enter(rig, 1)
    step1 = rig.w._step1_mount.current_camera()
    assert abs(step1[0] - compare[0]) < 1e-6 and abs(step1[1] - compare[1]) < 1e-6


@pytest.mark.parametrize("command", [
    "step0-navigate", "step0-patch", "step0-fit", "step0-compare-navigate",
    "step0-compare-patch", "step1-navigate", "step1-patch", "step3-patch"])
def test_every_explicit_command_is_exactly_one_jump(rig, camera_writes, command):
    _user_start(rig)
    page = rig.w._step0
    patch = (1000, 1512, 1500, 2012)                         # inside the 4096 px slide
    rig.w._all_patches = [patch]
    page.patches = [patch]
    step = 3 if command.startswith("step3") else 1 if command.startswith("step1") else 0
    if command.startswith("step0-compare"):
        page._enter_compare_mode()
        _pump()
    _enter(rig, step)
    camera_writes.clear()
    if command == "step0-navigate":
        page._on_tissue_navigate(2500, 3000)
    elif command == "step0-patch":
        page._navigate_full_image_to_patch(0)
    elif command == "step0-fit":
        page._fit_full_image()
    elif command == "step0-compare-navigate":
        page._navigate_compare_to(2500, 3000)
    elif command == "step0-compare-patch":
        page._navigate_compare_to_patch(0)
    elif command == "step1-navigate":
        rig.w._on_step1_tissue_navigate(2500, 3000)
    elif command == "step1-patch":
        rig.w._select_preview_patch(0)
    elif command == "step3-patch":
        rig.w._select_step3_patch(0)
    _pump()
    assert camera_writes.count("jump") == 1, (command, camera_writes)
    assert camera_writes.count() == 1, (command, camera_writes)


def test_the_first_full_image_after_a_load_is_one_jump(rig, camera_writes):
    _enter(rig, 0)
    page = rig.w._step0
    camera_writes.clear()
    page._camera_seed_pending = True                         # set by a dataset commit
    page._show_full_image()
    _pump()
    assert camera_writes.count("jump") == 1, camera_writes
    camera_writes.clear()
    page._show_full_image()                                  # a later rebuild is not
    _pump()
    assert camera_writes.count() == 0, camera_writes


def test_another_slide_resets_the_owner(rig, camera_writes):
    _user_start(rig)
    camera_writes.clear()
    rig.w.loader.filepath = "/fake/other.ome.tif"
    _enter(rig, 1)
    assert camera_writes.count("reset_for_dataset") == 1, camera_writes
    assert rig.w._camera_owner.current("/fake/other.ome.tif") is None


def test_step0_moves_before_the_first_save_reach_step1(rig, camera_writes):
    """codex A7: before the first Save the window has no loader of its own;
    Step0's navigation is labelled with Step0's slide and is not lost."""
    real = rig.w.loader
    _enter(rig, 0)
    rig.w.loader = None                                       # no handoff yet
    page = rig.w._step0
    vb = page._full_image_view_box()
    page._apply_full_image_camera(SLIDE_W * 0.3, SLIDE_H * 0.7, 2.2 * vb.width() / SLIDE_W)
    as_user(vb)
    _pump()
    assert camera_writes.count("user_navigated") == 1, camera_writes
    wanted = page.current_camera_snapshot()
    rig.w.loader = real                                       # the Save's handoff
    _enter(rig, 1)
    got = rig.w._step1_mount.current_camera()
    assert abs(got[0] - wanted[0]) < 1e-6 and abs(got[1] - wanted[1]) < 1e-6


def test_a_dataset_commit_resets_the_owner(rig, camera_writes):
    _user_start(rig)
    camera_writes.clear()
    rig.w._on_step0_dataset_committed({"gen": 99, "ome_path": "/fake/next.ome.tif"})
    assert camera_writes.count("reset_for_dataset") == 1, camera_writes


def test_the_owner_has_no_general_setter():
    from block01.ui.camera_owner import CameraOwner
    public = {n for n in dir(CameraOwner) if not n.startswith("_")}
    assert public == {"current", "dataset", "jump", "reset_for_dataset",
                      "user_navigated"}
