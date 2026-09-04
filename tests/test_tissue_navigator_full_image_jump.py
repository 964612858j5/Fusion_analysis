"""A click on the Tissue Preview jumps the full image there.

Reported from manual testing: in full-image mode there was no way to get to
a far region except panning. Two halves:

  1. `OverviewPanel` emits `navigate_requested(y, x)` (full-image pixels) for
     a plain click in patch mode -- press and release without a drag, which
     used to be discarded as "too small for a patch" -- and for Ctrl+click in
     ROI mode, which used to add no vertex. A drag still draws a patch, a
     plain click in ROI mode still adds a vertex.
  2. The page centres the full image's camera on that point, keeping the
     viewport size (the zoom) and clamping to the slide; only while the full
     image is on screen and not suspended. The full image's viewport is
     drawn on the navigator as it moves, and the compare viewer's rectangle
     comes back when the user returns to the compare page.

Own module: page-heavy Step0 suites crash pyqtgraph offscreen when combined
with the background-correction module in one process.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtCore, QtGui, QtTest, QtWidgets  # noqa: E402

from block01.ui.step0 import overview_panel as ovp  # noqa: E402
from block01.ui.step0 import step0_page as sp  # noqa: E402

from test_step0_background_correction_tab import app  # noqa: E402,F401


# ── 1. the overview emits the request ─────────────────────────────────────

class _Loader:
    shape = (8000, 6000)

    def channel_names(self):
        return ["DAPI"]


def _panel(mode="patch"):
    panel = ovp.OverviewPanel(_Loader(), "DAPI", lazy=True)
    panel.ds = 10
    panel.ov_h, panel.ov_w = 800, 600
    panel._set_mode(mode)
    return panel


def _mouse(panel, kind, r, c, button=QtCore.Qt.LeftButton, mods=QtCore.Qt.NoModifier):
    """Feed one mouse event through the panel's own event filter, with the
    scene->overview mapping pinned to (r, c)."""
    panel._ov_pos = lambda _sp: (r, c)
    ev = QtGui.QMouseEvent(kind, QtCore.QPointF(5, 5), button,
                           button if kind != QtCore.QEvent.MouseButtonRelease else QtCore.Qt.NoButton,
                           mods)
    return panel.eventFilter(panel.gview.viewport(), ev)


def test_a_click_in_patch_mode_requests_navigation_in_full_image_pixels(app):
    panel = _panel("patch")
    seen = []
    panel.navigate_requested.connect(lambda y, x: seen.append((y, x)))
    patches = []
    panel.patches_changed.connect(patches.append)

    _mouse(panel, QtCore.QEvent.MouseButtonPress, 120, 45)
    _mouse(panel, QtCore.QEvent.MouseButtonRelease, 121, 46)   # < 3 px: a click

    assert seen == [(1210, 460)]                # overview (r, c) x ds
    assert patches == [], "a click must not draw a patch"


def test_a_drag_in_patch_mode_still_draws_a_patch_and_does_not_navigate(app):
    panel = _panel("patch")
    seen = []
    panel.navigate_requested.connect(lambda y, x: seen.append((y, x)))
    added = []
    panel._add_patch = lambda *a, **k: added.append(a)

    _mouse(panel, QtCore.QEvent.MouseButtonPress, 100, 100)
    _mouse(panel, QtCore.QEvent.MouseButtonRelease, 140, 160)

    assert seen == []
    assert len(added) == 1


def test_ctrl_click_in_roi_mode_navigates_and_adds_no_vertex(app):
    panel = _panel("roi")
    seen = []
    panel.navigate_requested.connect(lambda y, x: seen.append((y, x)))

    _mouse(panel, QtCore.QEvent.MouseButtonPress, 30, 40,
           mods=QtCore.Qt.ControlModifier)

    assert seen == [(300, 400)]
    assert panel._cur_pts == []


def test_a_plain_click_in_roi_mode_still_adds_a_vertex(app):
    panel = _panel("roi")
    seen = []
    panel.navigate_requested.connect(lambda y, x: seen.append((y, x)))

    _mouse(panel, QtCore.QEvent.MouseButtonPress, 30, 40)

    assert seen == []
    assert panel._cur_pts == [(40, 30)]


def test_the_request_is_clamped_to_the_slide(app):
    panel = _panel("patch")
    seen = []
    panel.navigate_requested.connect(lambda y, x: seen.append((y, x)))

    _mouse(panel, QtCore.QEvent.MouseButtonPress, 5000, -3)
    _mouse(panel, QtCore.QEvent.MouseButtonRelease, 5000, -3)

    assert seen == [(7999, 0)]


# ── 2. the page moves the full image ──────────────────────────────────────

class _Ctl:
    def __init__(self, bbox=(1000, 2000, 1500, 2800), suspended=False):
        self._current_bbox = bbox
        self.suspended = suspended
        self.jumps = []
        self.channel, self.method, self.params = "CD3", None, ()

    def jump_to(self, y0, x0, w, h):
        self.jumps.append((y0, x0, w, h))
        # A real controller's range handler updates the bbox.
        self._current_bbox = (y0, x0, y0 + h, x0 + w)


class _Provider:
    def level_shape(self, _level):
        return (59040, 35520)


class _Stack:
    def __init__(self, ctl):
        self.controller = ctl
        self.provider = _Provider()
        self.view = None


class _Tab:
    def __init__(self, stack):
        self.stack = stack
        self.calls = []

    def show_source(self, *a, **k):
        self.calls.append((a, k))
        return True

    def set_dataset(self, _p):
        pass

    def teardown(self, **_k):
        pass


def _page(app, ctl):
    page = sp.Step0Page()
    page.current_channel = "CD3"
    page.nucleus_channel = "DAPI"
    page._explore_tab = _Tab(_Stack(ctl)) if ctl is not None else _Tab(None)
    return page


def test_a_navigation_centres_the_current_viewport_on_the_point(app):
    ctl = _Ctl(bbox=(1000, 2000, 1500, 2800))       # 500 high, 800 wide
    page = _page(app, ctl)

    page._on_tissue_navigate(20000, 10000)

    assert ctl.jumps == [(20000 - 250, 10000 - 400, 800, 500)]


def test_a_navigation_is_clamped_to_the_slide(app):
    ctl = _Ctl(bbox=(0, 0, 500, 800))
    page = _page(app, ctl)

    page._on_tissue_navigate(10, 35510)               # top edge, right edge

    assert ctl.jumps == [(0, 35520 - 800, 800, 500)]


def test_without_a_viewport_yet_a_default_size_is_used(app):
    ctl = _Ctl(bbox=None)
    page = _page(app, ctl)

    page._on_tissue_navigate(5000, 5000)

    d = sp.FULL_IMAGE_JUMP_DEFAULT_SIZE
    assert ctl.jumps == [(5000 - d // 2, 5000 - d // 2, d, d)]


def test_a_jump_from_the_whole_slide_view_zooms_in(app):
    """Keeping a whole-slide viewport would move nothing; a click on the
    whole slide means 'take me there, zoomed in'."""
    ctl = _Ctl(bbox=(0, 0, 59040, 35520))
    page = _page(app, ctl)

    page._on_tissue_navigate(20000, 10000)

    d = sp.FULL_IMAGE_JUMP_DEFAULT_SIZE
    assert ctl.jumps == [(20000 - d // 2, 10000 - d // 2, d, d)]
    # The second jump keeps that zoom.
    page._on_tissue_navigate(30000, 12000)
    assert ctl.jumps[-1] == (30000 - d // 2, 12000 - d // 2, d, d)


def test_ignored_while_there_is_no_full_image(app):
    """The full image is not a PAGE any more -- it is there whenever a
    viewer stack is -- so the only state a jump has to be ignored in is
    having no stack at all."""
    page = _page(app, None)

    page._on_tissue_navigate(20000, 10000)

    assert page._explore_tab.calls == [], "must not build or switch anything"


def test_ignored_while_a_production_run_holds_the_camera(app):
    ctl = _Ctl(suspended=True)
    page = _page(app, ctl)

    page._on_tissue_navigate(20000, 10000)

    assert ctl.jumps == []


def test_harmless_with_no_stack(app):
    page = _page(app, None)
    page._on_tissue_navigate(20000, 10000)            # must not raise


def test_the_popup_is_wired_to_the_page(app):
    ctl = _Ctl(bbox=(0, 0, 500, 800))
    page = _page(app, ctl)
    popup = page._ensure_tissue_navigator()

    popup.overview.navigate_requested.emit(3000, 4000)

    assert ctl.jumps == [(3000 - 250, 4000 - 400, 800, 500)]


# ── 3. the navigator shows the full image's viewport ─────────────────────

def test_the_full_image_viewport_is_drawn_on_the_navigator(app):
    ctl = _Ctl(bbox=(1000, 2000, 1500, 2800))
    page = _page(app, ctl)
    popup = page._ensure_tissue_navigator()

    page._update_full_image_view_rect()

    # (y0, y1, x0, x1) in full-image pixels, the overview's own convention.
    assert popup.overview.current_view_rect() == (1000.0, 1500.0, 2000.0, 2800.0)

    page._on_tissue_navigate(20000, 10000)
    assert popup.overview.current_view_rect() == (19750.0, 20250.0, 9600.0, 10400.0)


def test_the_compare_rect_path_defers_to_the_full_image_while_it_is_shown(app):
    ctl = _Ctl(bbox=(1000, 2000, 1500, 2800))
    page = _page(app, ctl)
    popup = page._ensure_tissue_navigator()

    page._update_tissue_view_rect()          # the legacy entry point

    assert popup.overview.current_view_rect() == (1000.0, 1500.0, 2000.0, 2800.0)


def test_entering_compare_mode_without_a_snapshot_leaves_the_rectangle(app):
    """The panels DO have a camera now, and while they are the view the
    navigator draws theirs -- but only once they are looking at something.
    Compare mode with no snapshot in it has a ViewBox range and not a view,
    and the true rectangle is the full image's, which is where the user is
    about to come back to."""
    ctl = _Ctl(bbox=(1000, 2000, 1500, 2800))
    page = _page(app, ctl)
    popup = page._ensure_tissue_navigator()
    page._update_full_image_view_rect()
    before = popup.overview.current_view_rect()
    assert before is not None

    page._set_compare_mode(True)

    assert popup.overview.current_view_rect() == before


# ── 4. no drawing mode: click jumps, drag pans; ROI mode: inside an ROI jumps ─

def _viewrange(panel):
    return [tuple(round(v) for v in r) for r in panel.vb.viewRange()]


def test_with_no_drawing_mode_a_click_navigates(app):
    panel = _panel(None)
    seen = []
    panel.navigate_requested.connect(lambda y, x: seen.append((y, x)))

    _mouse(panel, QtCore.QEvent.MouseButtonPress, 50, 60)
    _mouse(panel, QtCore.QEvent.MouseButtonRelease, 50, 60)

    assert seen == [(500, 600)]


def test_with_no_drawing_mode_a_left_drag_neither_pans_nor_navigates(app):
    """No left-drag pan (by request): a drag is simply not a click."""
    panel = _panel(None)
    panel.resize(400, 400)
    panel.show()
    panel.vb.setRange(xRange=(0, 600), yRange=(0, 800), padding=0)
    QtTest.QTest.qWait(30)
    before = _viewrange(panel)
    seen = []
    panel.navigate_requested.connect(lambda y, x: seen.append((y, x)))

    press = QtGui.QMouseEvent(QtCore.QEvent.MouseButtonPress, QtCore.QPointF(50, 50),
                              QtCore.Qt.LeftButton, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)
    move = QtGui.QMouseEvent(QtCore.QEvent.MouseMove, QtCore.QPointF(90, 70),
                             QtCore.Qt.NoButton, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)
    release = QtGui.QMouseEvent(QtCore.QEvent.MouseButtonRelease, QtCore.QPointF(90, 70),
                                QtCore.Qt.LeftButton, QtCore.Qt.NoButton, QtCore.Qt.NoModifier)
    panel._ov_pos = lambda _sp: (50, 50)
    vp = panel.gview.viewport()
    assert panel.eventFilter(vp, press) is True
    assert panel.eventFilter(vp, move) is True
    assert panel.eventFilter(vp, release) is True

    assert _viewrange(panel) == before, "a left drag must not pan"
    assert seen == []
    panel.close()


def test_with_no_drawing_mode_the_middle_button_pans(app):
    panel = _panel(None)
    panel.resize(400, 400)
    panel.show()
    panel.vb.setRange(xRange=(0, 600), yRange=(0, 800), padding=0)
    QtTest.QTest.qWait(30)
    before = _viewrange(panel)
    panel._ov_pos = lambda _sp: (50, 50)
    vp = panel.gview.viewport()
    press = QtGui.QMouseEvent(QtCore.QEvent.MouseButtonPress, QtCore.QPointF(50, 50),
                              QtCore.Qt.MiddleButton, QtCore.Qt.MiddleButton, QtCore.Qt.NoModifier)
    move = QtGui.QMouseEvent(QtCore.QEvent.MouseMove, QtCore.QPointF(90, 70),
                             QtCore.Qt.NoButton, QtCore.Qt.MiddleButton, QtCore.Qt.NoModifier)
    panel.eventFilter(vp, press)
    panel.eventFilter(vp, move)
    assert _viewrange(panel) != before
    panel.close()


def test_no_drawing_mode_hides_both_tool_panels_and_says_so(app):
    panel = _panel(None)
    assert panel._roi_ctrl.isHidden() and panel._patch_ctrl.isHidden()
    assert "Jump" in panel.hint.text() and "Left-drag" not in panel.hint.text()


def test_in_roi_mode_a_click_inside_an_existing_roi_navigates(app):
    panel = _panel("roi")
    panel._rois = [{"name": "ROI_1", "polygon_display": [(10, 10), (100, 10), (100, 100), (10, 100)]}]
    seen = []
    panel.navigate_requested.connect(lambda y, x: seen.append((y, x)))

    _mouse(panel, QtCore.QEvent.MouseButtonPress, 50, 50)          # inside

    assert seen == [(500, 500)]
    assert panel._cur_pts == []

    _mouse(panel, QtCore.QEvent.MouseButtonPress, 300, 300)        # outside
    assert seen == [(500, 500)]
    assert panel._cur_pts == [(300, 300)]

    # A polygon in progress keeps taking vertices, even inside an ROI.
    _mouse(panel, QtCore.QEvent.MouseButtonPress, 50, 50)
    assert panel._cur_pts == [(300, 300), (50, 50)]
    assert seen == [(500, 500)]


def test_the_mode_buttons_toggle_off_to_no_drawing_mode(app):
    page = sp.Step0Page()
    page._ensure_tissue_navigator()
    ov = page._drawing_overview()

    page._set_draw_mode("roi")
    assert page._btn_mode_roi.isChecked() and not page._btn_mode_patch.isChecked()
    assert ov._mode == "roi"

    status_before = ov.status.text()
    page._btn_mode_roi.click()                    # the active one: off
    assert not page._btn_mode_roi.isChecked() and not page._btn_mode_patch.isChecked()
    assert ov._mode is None
    # No mode sentence in the status line any more (it took a row a click
    # meant for the tissue landed on); the hint line carries the mouse help.
    assert ov.status.text() == status_before
    assert "Jump" in ov.hint.text()

    page._btn_mode_patch.click()                  # on
    assert page._btn_mode_patch.isChecked() and ov._mode == "patch"
    page._btn_mode_roi.click()                    # switches, never both
    assert page._btn_mode_roi.isChecked() and not page._btn_mode_patch.isChecked()
    assert ov._mode == "roi"
    page._btn_mode_roi.click()
    assert ov._mode is None
