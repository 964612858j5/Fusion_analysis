"""Block A1b S1: Step1 and Step3 are built on one page frame, so every part of
the two pages occupies the SAME window rectangle -- to the pixel -- at any
window size, after a drag of either handle and at the narrowest column.

S1 guarantees Step1 == Step3 only; Step0 and Step2 join the frame in S2 / S3.

The window is the real one with both real viewers (the rig of
`test_step1_shared_camera`), entered the way `_go_to_stepN` enters a page.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")
pytest.importorskip("pyqtgraph")

from PyQt5 import QtCore, QtTest, QtWidgets  # noqa: E402

from test_step0_compare_tiles import app  # noqa: E402,F401
from test_step1_shared_camera import _close, _window  # noqa: E402

PAGE = {1: 1, 3: 3}
SIZES = [(1500, 950), (1600, 1000), (1920, 1080), (2050, 1330)]


def _pump(cycles=6):
    for _ in range(cycles):
        QtWidgets.QApplication.processEvents()
        QtTest.QTest.qWait(10)


def _enter(rig, step):
    rig.w._stack.setCurrentIndex(PAGE[step])
    rig.w._set_step_active(step)
    _pump()


def _rect(w, widget):
    top_left = widget.mapTo(w, QtCore.QPoint(0, 0))
    return (top_left.x(), top_left.y(), widget.width(), widget.height())


def _parts(rig, step):
    """Every frame part of the page on screen, in window coordinates."""
    w = rig.w
    if step == 1:
        frame, mount = w._step1_page_widget, w._step1_mount
        channels = w._step1_channels_box
    else:
        frame, mount = w._step3._frame, w._step3_mount
        channels = [b for b in w._step3.findChildren(QtWidgets.QGroupBox)
                    if b.title() == "Channels"][0]
    view = mount.host.stack.view
    vb = view.view_box
    scene = vb.mapRectToScene(vb.rect())
    corner = view.graphics.mapFromScene(scene.topLeft())
    corner = view.graphics.viewport().mapTo(w, corner)
    tool = frame.right_tabs.currentWidget().layout().itemAt(0).widget()
    dock = w._channel_dock
    return {
        "title": _rect(w, frame.title_slot),
        "left tab bar row": _rect(w, frame.left_tabs.tabBar())[1::2],
        "right tab bar row": _rect(w, frame.right_tabs.tabBar())[1::2],
        "left column": _rect(w, frame.left_tabs),
        "right column": _rect(w, frame.right_tabs),
        "tool row": _rect(w, tool),
        "graphics viewport": _rect(w, view.graphics.viewport()),
        "viewer": (corner.x(), corner.y(), round(scene.width()), round(scene.height())),
        "bottom": _rect(w, frame.bottom_slot),
        "Channels frame": _rect(w, channels),
        "dock x / width": _rect(w, dock)[0::2],
    }


@pytest.fixture
def rig(app, monkeypatch, tmp_path):
    r = _window(app, monkeypatch, tmp_path)
    yield r
    _close(r)


def _assert_same(rig):
    _enter(rig, 1)
    one = _parts(rig, 1)
    _enter(rig, 3)
    three = _parts(rig, 3)
    diff = {k: (one[k], three[k]) for k in one if one[k] != three[k]}
    assert diff == {}, diff
    _enter(rig, 1)
    assert _parts(rig, 1) == one            # and back: nothing moved
    return one


@pytest.mark.parametrize("size", SIZES)
def test_step1_and_step3_share_every_rectangle(rig, size):
    rig.w.resize(*size)
    _pump()
    parts = _assert_same(rig)
    # the frame's fixed rows are what the metrics say
    metrics = rig.w._frame_metrics
    assert parts["title"][3] == metrics.title_height
    assert parts["tool row"][3] == metrics.tool_height
    assert parts["bottom"][3] == metrics.bottom_height


def _drag(rig, split, left):
    total = sum(split.sizes())
    split.setSizes([left, total - left])
    rig.w._on_channel_column_dragged(split)
    _pump()


@pytest.mark.parametrize("step", [1, 3])
def test_a_drag_on_either_page_moves_both(rig, step):
    _enter(rig, step)
    split = (rig.w._step1_main_split if step == 1
             else rig.w._step3.channel_column_splitter())
    _drag(rig, split, 420)
    parts = _assert_same(rig)
    assert abs(parts["left column"][2] - 420) <= 2


@pytest.mark.parametrize("step", [1, 3])
def test_the_narrowest_column_is_the_same_and_covers_no_weight_box(rig, step):
    """Dragged to nothing on EITHER page: both pages hold the same floor."""
    _enter(rig, step)
    split = (rig.w._step1_main_split if step == 1
             else rig.w._step3.channel_column_splitter())
    _drag(rig, split, 40)
    parts = _assert_same(rig)
    assert parts["left column"][2] > 40             # the floor held
    for shown in (1, 3):
        _enter(rig, shown)
        dock = rig.w._channel_dock
        viewport = dock.list_widget.viewport()
        right = viewport.mapTo(rig.w, QtCore.QPoint(viewport.width(), 0)).x()
        for cid in dock.visible_row_ids():
            spin = dock.row(cid).spin
            if spin.isVisible():
                edge = spin.mapTo(rig.w, QtCore.QPoint(spin.width(), 0)).x()
                assert edge <= right, (shown, cid, edge, right)


def test_both_pages_hold_one_floor(rig):
    """The row floor is measured where the dock is and held on BOTH pages."""
    for step in (1, 3):
        _enter(rig, step)
        floors = {rig.w._step1_left_panel.minimumWidth(),
                  rig.w._step3.left_panel().minimumWidth()}
        assert len(floors) == 1 and floors.pop() > 0, step


def test_a_floor_that_drops_does_not_narrow_the_column(rig, monkeypatch):
    """W = max(W_user, floor), and a floor that widened the column becomes
    W_user -- so a lower floor later leaves the width where it is."""
    _enter(rig, 1)
    split = rig.w._step1_main_split
    _drag(rig, split, 250)
    usable = split.width() - split.handleWidth()
    panels = (rig.w._step1_left_panel, rig.w._step3.left_panel())
    monkeypatch.setattr(type(rig.w), "_hold_step1_channel_floor", lambda self: None)
    for panel in panels:                       # a floor that widens the column
        panel.setMinimumWidth(380)
    rig.w._apply_channel_column_fraction()
    _pump()
    widened = split.sizes()[0]
    assert widened > 250
    assert abs(rig.w.channel_column_fraction() * usable - widened) <= 1   # written back
    for panel in panels:                       # ...and the floor drops again
        panel.setMinimumWidth(200)
    rig.w._apply_channel_column_fraction()
    _pump()
    # Within 1 px, not exactly: until S2 Step0 still takes the share as a
    # FRACTION and its own settled width is written back, which rounds by up
    # to a pixel (S1 ruling 3). What must not happen is the drop to 200.
    assert abs(split.sizes()[0] - widened) <= 1
