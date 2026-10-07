"""Block A1b S1-S4: every step page is built on one page frame, so every part
of the pages occupies the SAME window rectangle -- to the pixel -- at any
window size, after a drag of any page's handle and at the narrowest column.

Step0, Step1 and Step3 carry the channel panel and a viewer, and all of
their parts are compared. Step2 (S3) has the frame's columns, tabs, tool row
and slots, and those are compared. Step4 (S4) is the frame's wide mode: its
one slot is the other pages' two columns and the handle between them.

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

PAGE = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4}
STEPS = (0, 1, 3)            # the pages with the channel panel and a viewer
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
    if step == 0:
        frame, view = w._step0._frame, w._step0._explore_tab.stack.view
        channels = w._step0._channels_box
    elif step == 1:
        frame, view = w._step1_page_widget, w._step1_mount.host.stack.view
        channels = w._step1_channels_box
    else:
        frame, view = w._step3._frame, w._step3_mount.host.stack.view
        channels = [b for b in w._step3.findChildren(QtWidgets.QGroupBox)
                    if b.title() == "Channels"][0]
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
    parts = {}
    for step in STEPS:
        _enter(rig, step)
        parts[step] = _parts(rig, step)
    first = parts[STEPS[0]]
    for step in STEPS[1:]:
        diff = {k: (first[k], parts[step][k]) for k in first
                if first[k] != parts[step][k]}
        assert diff == {}, (step, diff)
    _enter(rig, STEPS[0])
    assert _parts(rig, STEPS[0]) == first   # and back: nothing moved
    return first


@pytest.mark.parametrize("size", SIZES)
def test_the_framed_steps_share_every_rectangle(rig, size):
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


def _split(rig, step):
    return {0: rig.w._step0._bg_c_split, 1: rig.w._step1_main_split,
            2: rig.w._step2.channel_column_splitter(),
            3: rig.w._step3.channel_column_splitter()}[step]


def _panels(rig):
    return (rig.w._step0.left_panel(), rig.w._step1_left_panel,
            rig.w._step2.left_panel(), rig.w._step3.left_panel())


def _frame(rig, step):
    w = rig.w
    return {0: w._step0._frame, 1: w._step1_page_widget, 2: w._step2._frame,
            3: w._step3._frame, 4: w._step4._frame}[step]


def _frame_parts(rig, step):
    """The frame's own parts, the ones every columned page has."""
    w, frame = rig.w, _frame(rig, step)
    tool = frame.right_tabs.currentWidget().layout().itemAt(0).widget()
    return {
        "title": _rect(w, frame.title_slot),
        "left tab bar row": _rect(w, frame.left_tabs.tabBar())[1::2],
        "right tab bar row": _rect(w, frame.right_tabs.tabBar())[1::2],
        "left column": _rect(w, frame.left_tabs),
        "right column": _rect(w, frame.right_tabs),
        "left pane": _rect(w, frame.left_tabs.currentWidget()),
        "tool row": _rect(w, tool),
        "bottom": _rect(w, frame.bottom_slot),
    }


@pytest.mark.parametrize("size", SIZES)
def test_step2_shares_the_frame(rig, size):
    rig.w.resize(*size)
    _pump()
    _enter(rig, 1)
    one = _frame_parts(rig, 1)
    _enter(rig, 2)
    two = _frame_parts(rig, 2)
    assert {k: (one[k], two[k]) for k in one if one[k] != two[k]} == {}


@pytest.mark.parametrize("size", SIZES)
def test_step4_is_the_frame_s_wide_mode(rig, size):
    """S0 ruling 4: the title slot, the (blank) tab row and the bottom slot
    are every page's; the one slot is left column + handle + right column,
    and its pane has the tab panes' top and bottom edges."""
    rig.w.resize(*size)
    _pump()
    _enter(rig, 1)
    one = _frame_parts(rig, 1)
    left, right = one["left column"], one["right column"]
    _enter(rig, 4)
    frame, w = _frame(rig, 4), rig.w
    assert frame.wide and frame.splitter is None
    assert _rect(w, frame.title_slot) == one["title"]
    assert _rect(w, frame.bottom_slot) == one["bottom"]
    assert _rect(w, frame.wide_slot.tabBar())[1::2] == one["left tab bar row"]
    assert _rect(w, frame.wide_slot) == (left[0], left[1],
                                         right[0] + right[2] - left[0], left[3])
    pane = _rect(w, frame.wide_slot.currentWidget())
    assert (pane[1], pane[3]) == (one["left pane"][1], one["left pane"][3])
    # the blank row's one tab is there only for its height
    bar = frame.wide_slot.tabBar()
    assert bar.count() == 1 and bar.tabText(0) == "" and not bar.isTabEnabled(0)


def test_a_drag_on_step2_moves_every_column_page(rig):
    _enter(rig, 2)
    _drag(rig, _split(rig, 2), 420)
    for step in (0, 1, 3, 2):
        _enter(rig, step)
        assert abs(_split(rig, step).sizes()[0] - 420) <= 2, step
    widths = set()
    for step in (0, 1, 2, 3):
        _enter(rig, step)
        widths.add(_split(rig, step).sizes()[0])
    assert len(widths) == 1


def test_step2_holds_the_one_floor(rig):
    """User ruling 2026-10-08 (block A9-W1): the one floor is the hard floor,
    and Step2's column takes it like every other page's."""
    from block01.ui.step_frame import CHANNEL_COLUMN_HARD_FLOOR as floor
    _enter(rig, 2)
    _drag(rig, _split(rig, 2), 40)
    widths = set()
    for step in (0, 1, 2, 3):
        _enter(rig, step)
        widths.add(_split(rig, step).sizes()[0])
    assert len(widths) == 1 and floor <= widths.pop() <= floor + 4
    assert _split(rig, 2).widget(0).minimumWidth() == floor


@pytest.mark.parametrize("step", STEPS)
def test_a_drag_on_any_page_moves_all(rig, step):
    _enter(rig, step)
    split = _split(rig, step)
    _drag(rig, split, 420)
    parts = _assert_same(rig)
    assert abs(parts["left column"][2] - 420) <= 2


@pytest.mark.parametrize("step", STEPS)
def test_the_narrowest_column_is_the_same_on_every_page(rig, step):
    """Dragged to nothing on ANY page: every page holds the same floor --
    the hard floor, not any page's content (user ruling 2026-10-08, block
    A9-W1, replacing ruling 2 of 2026-09-30) -- and content wider than the
    column is covered from its right edge rather than widening it."""
    from block01.ui.step_frame import CHANNEL_COLUMN_HARD_FLOOR as floor
    _enter(rig, step)
    _drag(rig, _split(rig, step), 40)
    parts = _assert_same(rig)
    assert floor <= parts["left column"][2] <= floor + 4
    content = max(p.minimumSizeHint().width() for p in _panels(rig))
    assert content > parts["left column"][2], "nothing was left to cover"


def test_every_page_holds_one_floor(rig):
    """One floor on every framed column, whichever page shows the dock: the
    hard floor (user ruling 2026-10-08, block A9-W1)."""
    from block01.ui.step_frame import CHANNEL_COLUMN_HARD_FLOOR as floor
    for step in STEPS:
        _enter(rig, step)
        floors = {_split(rig, s).widget(0).minimumWidth() for s in (0, 1, 2, 3)}
        assert floors == {floor}, step


def test_a_page_not_visited_yet_does_not_widen_the_column(rig):
    """The case ruling 2 removes: straight to the narrowest on Step0, before
    Step1 or Step3 has ever been shown, then enter them -- the column keeps
    its width to the pixel."""
    _enter(rig, 0)
    _drag(rig, _split(rig, 0), 40)
    before = _parts(rig, 0)["left column"]
    for step in (1, 3, 0):
        _enter(rig, step)
        assert _parts(rig, step)["left column"] == before, step


def test_every_page_opens_at_the_one_fixed_share(rig):
    """User ruling 2026-10-08 (block A9-W1): every framed page opens at one
    fixed share of its width, not one measured from any page's content."""
    from block01.ui.step_frame import CHANNEL_COLUMN_OPENING_FRACTION as share
    for step in STEPS:
        _enter(rig, step)
        split = _split(rig, step)
        usable = split.width() - split.handleWidth()
        assert abs(split.sizes()[0] - round(usable * share)) <= 2, step


def test_step0_compare_mode_moves_neither_tool_row_nor_view_area(rig):
    _enter(rig, 0)
    page = rig.w._step0
    tool = page._frame.right_tabs.currentWidget().layout().itemAt(0).widget()
    before = (_rect(rig.w, tool), _rect(rig.w, page._view_area))
    page._view_area.setCurrentIndex(page._VIEW_COMPARE)
    _pump()
    assert (_rect(rig.w, tool), _rect(rig.w, page._view_area)) == before
    page._view_area.setCurrentIndex(page._VIEW_FULL)
    _pump()
    assert (_rect(rig.w, tool), _rect(rig.w, page._view_area)) == before


def test_content_wider_than_the_column_never_widens_it(rig):
    """User ruling 2026-10-08 (block A9-W1): content that needs more than the
    user's width is covered from the column's right edge; neither the
    column nor the shared share moves."""
    _enter(rig, 1)
    split = rig.w._step1_main_split
    _drag(rig, split, 250)
    share = rig.w.channel_column_fraction()
    for panel in _panels(rig):                 # content that needs 380 px
        panel.setMinimumWidth(380)
    rig.w._apply_channel_column_fraction()
    _pump()
    assert abs(split.sizes()[0] - 250) <= 2
    assert rig.w.channel_column_fraction() == share
    for step in (0, 2, 3):
        _enter(rig, step)
        assert abs(_split(rig, step).sizes()[0] - 250) <= 2, step
