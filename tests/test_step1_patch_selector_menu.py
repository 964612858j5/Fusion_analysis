"""Step1's patch selector reaches P8 and beyond without growing the row.

The row used to hold one button per patch. Measured offscreen before this
block: twenty patches made the strip 916 px wide and put the last button's
right edge at 1309 px -- outside a 1000 px window entirely. So the strip is
capped at `STEP1_INLINE_PATCH_BUTTONS` and a "Patch" menu, which is the
row's title and its dropdown at once, lists every patch there is.

What these gates protect:

  * the strip's width is the SAME at 8 and at 20 patches as at 7, and the
    dropdown is as wide as a full strip -- Qt's default for twenty "P12"
    items is 250 px, measured, which next to a 318 px strip reads as a
    different control;
  * the menu is derived from `_all_patches` every rebuild -- there is no
    second patch model and no second selection authority: every menu item
    goes through `_select_preview_patch()`, the entry point the inline
    buttons already used;
  * a patch the strip cannot show is still selectable BY A REAL CLICK, and
    selecting it navigates the viewer exactly as an inline button does;
  * loading / ready / error read the same in the menu as in the strip;
  * rebuilding the selector never navigates on its own.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtCore, QtTest, QtWidgets  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


#: Seven buttons and their six gaps -- the width a full inline strip has,
#: written out rather than imported so a change to the production constant
#: has to be argued for here too.
_FULL_STRIP_W = 7 * 42 + 6 * 4   # 318


def _patches(n):
    return [(i * 100, i * 100 + 50, i * 100, i * 100 + 50) for i in range(n)]


class _Stack:
    """Stands in for a live whole-slide stack (the GPU path's mount)."""


class _Host:
    def __init__(self, stack):
        self.stack = stack

    def isVisibleTo(self, _other):
        # `_step1_whole_slide_active()` asks this; a stack that exists in
        # this fixture is the picture Step1 is showing.
        return self.stack is not None


class _Mount:
    """Records the camera navigation `_select_preview_patch` asks for."""

    def __init__(self, stack=None):
        self.host = _Host(stack)
        self.shown = []

    def show_patch(self, patch):
        self.shown.append(tuple(patch))

    def close(self):
        return None


@pytest.fixture
def win(app):
    from block01.ui.main_window import MainWindow

    w = MainWindow()
    w._current_step = 1
    yield w
    w.close()


def _mount(w, stack=_Stack()):
    mount = _Mount(stack)
    w._step1_mount = mount
    return mount


def _menu_labels(w):
    return [a.text() for a in w._patch_menu_actions]


def _row_width(w):
    """The width the inline strip asks for, patch buttons only."""
    return w._patch_sel_container.sizeHint().width()


def _click_menu_item(w, idx):
    """A real mouse click on the menu entry for patch `idx`.

    The menu is popped up rather than opened by clicking the tool button,
    because `QToolButton`'s InstantPopup runs the menu in a nested event
    loop that never returns under a test. Everything after the popup is a
    genuine Qt mouse press/release on the item's own rectangle.
    """
    menu = w._patch_menu
    menu.popup(QtCore.QPoint(0, 0))
    QtWidgets.QApplication.processEvents()
    act = w._patch_menu_actions[idx]
    rect = menu.actionGeometry(act)
    assert rect.isValid() and not rect.isEmpty(), (
        f"menu entry for P{idx+1} has no rectangle to click")
    QtTest.QTest.mouseClick(menu, QtCore.Qt.LeftButton,
                            QtCore.Qt.NoModifier, rect.center())
    QtWidgets.QApplication.processEvents()
    menu.close()
    QtWidgets.QApplication.processEvents()


# ── the strip is capped, the menu is complete ───────────────────────

@pytest.mark.parametrize("n", [0, 1, 7, 8, 20])
def test_the_strip_is_capped_and_the_menu_lists_every_patch(win, n):
    from block01.ui.main_window import STEP1_INLINE_PATCH_BUTTONS

    # Seven is written out here on purpose: a gate that reads the cap from
    # the constant it is guarding would pass at any cap, including none.
    assert STEP1_INLINE_PATCH_BUTTONS == 7
    win._on_patches(_patches(n))
    assert len(win._patch_sel_btns) == min(n, 7)
    assert len(win._patch_menu_actions) == n
    assert _menu_labels(win)[:3] == [f"P{i+1}" for i in range(min(n, 3))]
    if n:
        assert win._patch_menu_actions[-1].text().startswith(f"P{n}")


def test_the_strip_does_not_get_wider_past_seven_patches(win):
    win._on_patches(_patches(7))
    at7 = _row_width(win)
    win._on_patches(_patches(8))
    assert _row_width(win) == at7
    win._on_patches(_patches(20))
    assert _row_width(win) == at7


def test_the_menu_entry_has_a_width_of_its_own_that_patches_do_not_change(win):
    win._on_patches(_patches(7))
    at7 = win._patch_menu_btn.sizeHint().width()
    win._on_patches(_patches(20))
    assert win._patch_menu_btn.sizeHint().width() == at7
    assert win._patch_menu_btn.text() == "Patch"


def test_with_no_patches_the_menu_entry_is_present_but_disabled(win):
    win._on_patches([])
    assert win._patch_menu_btn.isEnabled() is False
    assert win._patch_menu_actions == []
    win._on_patches(_patches(3))
    assert win._patch_menu_btn.isEnabled() is True


# ── the dropdown is as wide as a full strip ─────────────────────────

def test_the_full_strip_width_is_what_the_module_computes(win):
    from block01.ui.main_window import step1_patch_menu_width

    assert step1_patch_menu_width() == _FULL_STRIP_W
    win._on_patches(_patches(7))
    assert _row_width(win) == _FULL_STRIP_W


def _popup(win):
    btn = win._patch_menu_btn
    menu = win._patch_menu
    menu.popup(btn.mapToGlobal(QtCore.QPoint(0, btn.height())))
    QtWidgets.QApplication.processEvents()
    return menu


@pytest.mark.parametrize("n", [1, 8])
def test_a_popped_up_menu_that_fits_is_exactly_a_full_strip_wide(win, n):
    win.resize(1200, 800)
    win.show()
    QtWidgets.QApplication.processEvents()
    win._on_patches(_patches(n))

    menu = _popup(win)
    try:
        assert menu.isVisible() is True
        # The real popped-up rectangle, not the size hint.
        assert menu.width() == _FULL_STRIP_W
        # and the items fill it, so the list is what is wide, not a margin
        assert menu.actionGeometry(
            win._patch_menu_actions[-1]).width() == _FULL_STRIP_W
    finally:
        menu.close()
        QtWidgets.QApplication.processEvents()


def test_every_item_is_a_full_strip_wide_even_when_qt_needs_columns(win):
    """A list taller than the screen is laid out in columns by Qt.

    Measured on this 800x600 offscreen screen: twenty 32 px items do not
    fit in 600 px, so Qt lays them out in two columns and P19/P20 sit at
    x=318. The width is therefore a MINIMUM, not a fixed size -- pinning it
    to 318 px clipped the second column and made exactly those two patches
    unreachable. What is guaranteed is that no column is ever narrower than
    a full inline strip.
    """
    win.resize(1200, 800)
    win.show()
    QtWidgets.QApplication.processEvents()
    win._on_patches(_patches(20))

    menu = _popup(win)
    try:
        assert menu.width() >= _FULL_STRIP_W
        assert menu.width() % _FULL_STRIP_W == 0, (
            f"{menu.width()} is not a whole number of {_FULL_STRIP_W} px columns")
        widths = {menu.actionGeometry(a).width()
                  for a in win._patch_menu_actions}
        assert widths == {_FULL_STRIP_W}
        # every item is inside the rectangle that is actually on screen
        for act in win._patch_menu_actions:
            assert menu.rect().contains(menu.actionGeometry(act)), (
                f"{act.text()} is outside the popped-up menu")
    finally:
        menu.close()
        QtWidgets.QApplication.processEvents()


@pytest.mark.parametrize("win_w", [1400, 1000, 800])
def test_the_dropdown_stays_a_full_strip_wide_and_on_screen_in_narrow_windows(
        win, win_w):
    win.resize(win_w, 800)
    win.show()
    QtWidgets.QApplication.processEvents()
    win._on_patches(_patches(20))
    QtWidgets.QApplication.processEvents()

    btn = win._patch_menu_btn
    menu = _popup(win)
    try:
        assert menu.width() >= _FULL_STRIP_W
        assert menu.width() % _FULL_STRIP_W == 0
        screen = QtWidgets.QApplication.primaryScreen().availableGeometry()
        geom = menu.frameGeometry()
        assert screen.contains(geom), (
            f"the dropdown fell off the screen: {geom} not inside {screen}")
        # It drops out of the entry it belongs to, horizontally. Vertically
        # Qt is free to move a list taller than the screen above the entry,
        # and on this 600 px offscreen screen it does.
        anchor = btn.mapToGlobal(QtCore.QPoint(0, 0))
        assert abs(geom.left() - anchor.x()) <= menu.width()
    finally:
        menu.close()
        QtWidgets.QApplication.processEvents()


def test_the_dropdown_does_not_shrink_when_the_patch_list_changes(win):
    for n in (20, 4, 1, 9):
        win._on_patches(_patches(n))
        assert win._patch_menu.minimumWidth() == _FULL_STRIP_W
        assert win._patch_menu.sizeHint().width() >= _FULL_STRIP_W


# ── a real click reaches the patches the strip cannot show ──────────

def test_clicking_the_patch_entry_really_opens_the_menu(win):
    """The dropdown opens from a genuine mouse click on "Patch".

    `QToolButton`'s InstantPopup runs the menu in a nested event loop, so
    the click only returns once the menu closes: a watchdog records what
    was on screen and closes it.
    """
    win.resize(1200, 800)
    win.show()
    QtWidgets.QApplication.processEvents()
    win._on_patches(_patches(20))
    QtWidgets.QApplication.processEvents()

    seen = {}

    def look():
        menu = win._patch_menu
        seen["visible"] = menu.isVisible()
        seen["items"] = len(menu.actions())
        seen["width"] = menu.width()
        menu.close()

    QtCore.QTimer.singleShot(150, look)
    QtTest.QTest.mouseClick(win._patch_menu_btn, QtCore.Qt.LeftButton)
    QtWidgets.QApplication.processEvents()

    assert seen.get("visible") is True, "the Patch entry did not open a menu"
    assert seen["items"] == 20
    assert seen["width"] >= _FULL_STRIP_W
    assert seen["width"] % _FULL_STRIP_W == 0

def test_a_real_click_selects_p8(win):
    mount = _mount(win)
    win._on_patches(_patches(20))
    win._select_preview_patch(0)
    mount.shown.clear()

    _click_menu_item(win, 7)

    assert win._preview_patch_idx == 7
    assert win._selected_step1_patch_idx == 7
    assert mount.shown == [_patches(20)[7]], "P8 did not navigate the viewer"


def test_a_real_click_selects_p20(win):
    mount = _mount(win)
    win._on_patches(_patches(20))
    win._select_preview_patch(0)
    mount.shown.clear()

    _click_menu_item(win, 19)

    assert win._preview_patch_idx == 19
    assert mount.shown == [_patches(20)[19]]


def test_selecting_p8_lights_no_inline_button(win):
    _mount(win)
    win._on_patches(_patches(20))
    _click_menu_item(win, 7)

    assert [b.isChecked() for b in win._patch_sel_btns] == [False] * 7
    assert [a.isChecked() for a in win._patch_menu_actions].index(True) == 7
    assert sum(a.isChecked() for a in win._patch_menu_actions) == 1


def test_p8_then_p2_and_back(win):
    mount = _mount(win)
    win._on_patches(_patches(20))

    _click_menu_item(win, 7)
    assert win._preview_patch_idx == 7

    QtTest.QTest.mouseClick(win._patch_sel_btns[1], QtCore.Qt.LeftButton)
    QtWidgets.QApplication.processEvents()
    assert win._preview_patch_idx == 1
    assert win._patch_sel_btns[1].isChecked() is True
    assert win._patch_menu_actions[1].isChecked() is True
    assert win._patch_menu_actions[7].isChecked() is False

    _click_menu_item(win, 7)
    assert win._preview_patch_idx == 7
    assert win._patch_sel_btns[1].isChecked() is False
    assert mount.shown[-3:] == [_patches(20)[7], _patches(20)[1],
                                _patches(20)[7]]


# ── one model, one selection authority ──────────────────────────────

def test_every_menu_item_goes_through_select_preview_patch(win):
    win._on_patches(_patches(20))
    seen = []
    win._select_preview_patch = lambda idx: seen.append(idx)
    for act in win._patch_menu_actions:
        act.trigger()
    assert seen == list(range(20))


def test_the_menu_is_rebuilt_from_all_patches_not_kept_alongside_it(win):
    win._on_patches(_patches(20))
    win._on_patches(_patches(4))
    assert len(win._patch_menu_actions) == 4
    assert win._all_patches == _patches(4)
    win._on_patches([])
    assert win._patch_menu_actions == []
    win._on_patches(_patches(9))
    assert len(win._patch_menu_actions) == 9
    assert len(win._patch_sel_btns) == 7


def test_rebuilding_the_selector_does_not_navigate(win):
    mount = _mount(win)
    win._on_patches(_patches(20))
    win._select_preview_patch(9)
    mount.shown.clear()

    win._rebuild_patch_buttons(_patches(20))
    QtWidgets.QApplication.processEvents()

    assert mount.shown == [], "a programmatic rebuild moved the camera"
    assert win._preview_patch_idx == 9
    assert win._patch_menu_actions[9].isChecked() is True


def test_an_out_of_range_menu_index_selects_nothing(win):
    mount = _mount(win)
    win._on_patches(_patches(8))
    win._select_preview_patch(3)
    mount.shown.clear()
    win._select_preview_patch(8)
    assert win._preview_patch_idx == 3
    assert mount.shown == []


# ── load state reads the same in both places ────────────────────────

@pytest.mark.parametrize("state,glyph", [("idle", ""), ("loading", " ⟳"),
                                         ("ready", " ✓"), ("error", " ✗")])
def test_load_state_shows_the_same_in_the_strip_and_in_the_menu(win, state, glyph):
    win._on_patches(_patches(20))
    win._set_patch_btn_state(2, state)
    win._set_patch_btn_state(11, state)

    assert win._patch_sel_btns[2].text() == f"P3{glyph}"
    assert win._patch_menu_actions[2].text() == f"P3{glyph}"
    # P12 has no inline button at all; the menu is where its state is read.
    assert win._patch_menu_actions[11].text() == f"P12{glyph}"


def test_a_rebuild_restores_the_state_of_menu_only_patches(win):
    win._on_patches(_patches(20))
    win._patch_load_ready.add(11)
    win._patch_loaders[12] = object()
    try:
        win._rebuild_patch_buttons(_patches(20))
        assert win._patch_menu_actions[11].text() == "P12 ✓"
        assert win._patch_menu_actions[12].text() == "P13 ⟳"
        assert win._patch_menu_actions[13].text() == "P14"
    finally:
        win._patch_loaders.pop(12, None)


# ── nothing else in Step1 moves ─────────────────────────────────────

def test_the_viewer_viewport_and_splitter_survive_seven_to_twenty_to_seven(win):
    win.resize(1200, 800)
    win.show()
    QtWidgets.QApplication.processEvents()
    win._on_patches(_patches(7))
    QtWidgets.QApplication.processEvents()

    def shape():
        return (win.prev_gv.viewport().size(),
                win.viewer_tab.size(),
                tuple(win._step1_main_split.sizes()))

    before = shape()
    win._on_patches(_patches(20))
    QtWidgets.QApplication.processEvents()
    assert shape() == before
    win._on_patches(_patches(7))
    QtWidgets.QApplication.processEvents()
    assert shape() == before


def test_selection_works_with_and_without_a_live_whole_slide_stack(win):
    # GPU/whole-slide path: the mount has a stack, so the camera follows.
    gpu = _mount(win, _Stack())
    win._on_patches(_patches(20))
    _click_menu_item(win, 15)
    assert win._preview_patch_idx == 15
    assert gpu.shown[-1] == _patches(20)[15]

    # Fallback path: no stack behind the mount. The selection still moves
    # and nothing is asked to navigate.
    cpu = _mount(win, None)
    _click_menu_item(win, 16)
    assert win._preview_patch_idx == 16
    assert win._selected_step1_patch_idx == 16
    assert cpu.shown == []
