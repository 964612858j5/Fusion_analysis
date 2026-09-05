"""A Preview Patch is a NAVIGATION SHORTCUT for the compare panels.

The user drew three patches far apart on the slide, entered compare mode,
and clicked P1 / P2 / P3. Nothing moved: the panels stayed exactly where
the right-click had put them, and the only thing the click changed was
which button looked pressed. That is what this module pins down.

The cause was a class with `_select_patch` defined TWICE. Python keeps the
later definition, so the earlier one -- which the reader met first -- was
unreachable, and neither of them knew the compare panels existed. There is
now exactly one definition, both ways in (the Pn buttons and the patch
list) go through it, and in compare mode it moves the one camera.

What "go to the patch" means here:

* the CENTRE of the three panels is the centre of the patch;
* ONE scale for all three, solved from their REAL widths, small enough
  that the patch is entirely inside the tightest of them -- three panels at
  three magnifications would make a correction look like it changed the
  size of a structure;
* through `CompareStrip.set_camera`, so the panels ask for the tiles of
  where they land in the same turn and sharpen block by block;
* no Process, no preview worker, no backend rebuild;
* and the same, in its own way, on the FULL IMAGE -- see
  `test_step0_patch_full_image_navigation`. Pn is the shortcut to a place
  on the slide, and the earlier rule "the full image keeps its camera" is
  revoked: a button that works in one of the two viewers and silently does
  nothing in the other has a meaning that depends on a mode the user is
  not looking at. The dispatch between the two lives in ONE method,
  `_navigate_active_view_to_patch`.

Own module, like the other page-heavy Step0 suites: combined runs segfault
in offscreen pyqtgraph.
"""

import ast
import inspect
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtTest  # noqa: E402

from block01.ui.step0 import step0_page as sp  # noqa: E402

from test_step0_compare_tiles import (  # noqa: E402
    SLIDE_H,
    SLIDE_W,
    _enter,
    _page,
    app,            # noqa: F401  (pytest fixture)
)


# Three patches, far apart, different sizes -- the case the user reported.
P1 = (100, 356, 100, 356)              # 256 x 256, top-left
P2 = (1800, 2312, 1500, 2524)          # 512 x 1024, middle, wide
P3 = (3000, 3128, 3500, 3628)          # 128 x 128, bottom-right, small


def _with_patches(app, patches=(P1, P2, P3)):
    page = _page(app)
    page.patches = [tuple(p) for p in patches]
    page._rebuild_patch_buttons()
    # The patch LIST is the other way in, and it is what the page's own
    # `_rebuild_patch_list` would have filled from the navigator's patches.
    page._patch_list.clear()
    for i, (y0, y1, x0, x1) in enumerate(page.patches):
        page._patch_list.addItem(f"P{i+1}  [{y1-y0}x{x1-x0}px]")
    return page


def _centre_of(patch):
    y0, y1, x0, x1 = patch
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


def _cams(strip):
    return [strip.camera(i) for i in range(3)]


# ── 1. one definition, one entry ─────────────────────────────────────────

def test_the_class_defines_select_patch_exactly_once():
    """Parsed, not grepped: a string search cannot tell a definition from a
    call, and it was a SECOND DEFINITION -- silently shadowed -- that made
    the visible half of this behaviour unreachable."""
    source = inspect.getsource(sp)
    tree = ast.parse(source)
    classes = [n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "Step0Page"]
    assert len(classes) == 1
    defs = [n for n in classes[0].body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "_select_patch"]
    assert len(defs) == 1


def test_the_patch_buttons_and_the_patch_list_are_the_same_entry(app):
    """Two ways in, one behaviour. They used to be two implementations."""
    page = _with_patches(app)
    strip = _enter(page, 400.0, 400.0)

    page._select_patch(1)
    QtTest.QTest.qWait(20)
    by_button = _cams(strip)

    page._select_patch(0)
    QtTest.QTest.qWait(20)
    page._patch_list.setCurrentRow(1)
    QtTest.QTest.qWait(20)
    by_list = _cams(strip)

    for a, b in zip(by_button, by_list):
        assert a == pytest.approx(b, rel=1e-9, abs=1e-6)
    assert page.current_patch_idx == 1


# ── 2. the camera actually goes there ────────────────────────────────────

@pytest.mark.parametrize("idx,patch", [(0, P1), (1, P2), (2, P3)])
def test_selecting_a_patch_centres_the_three_panels_on_it(app, idx, patch):
    page = _with_patches(app)
    strip = _enter(page, 4000.0, 200.0)      # deliberately somewhere else
    page._select_patch(idx)
    QtTest.QTest.qWait(20)
    want_x, want_y = _centre_of(patch)
    for cam in _cams(strip):
        assert cam is not None
        assert cam[0] == pytest.approx(want_x, abs=1.0)
        assert cam[1] == pytest.approx(want_y, abs=1.0)


@pytest.mark.parametrize("idx", [0, 1, 2])
def test_the_three_panels_land_on_one_scale(app, idx):
    """Not three fits. A structure that is bigger in the TopHat column than
    in the Original column is a correction artefact that is not there."""
    page = _with_patches(app)
    strip = _enter(page)
    page._select_patch(idx)
    QtTest.QTest.qWait(20)
    scales = [cam[2] for cam in _cams(strip)]
    assert all(s is not None for s in scales)
    for other in scales[1:]:
        assert other == pytest.approx(scales[0], rel=1e-6)


@pytest.mark.parametrize("idx,patch", [(0, P1), (1, P2), (2, P3)])
def test_the_whole_patch_is_inside_every_panel(app, idx, patch):
    page = _with_patches(app)
    strip = _enter(page)
    page._select_patch(idx)
    QtTest.QTest.qWait(20)
    y0, y1, x0, x1 = patch
    for i in range(3):
        vx, vy, vw, vh = strip.view_rect_l0(i)
        assert vx <= x0 + 1e-6 and vx + vw >= x1 - 1e-6, f"panel {i} x"
        assert vy <= y0 + 1e-6 and vy + vh >= y1 - 1e-6, f"panel {i} y"


def test_the_fit_uses_the_real_panel_sizes(app):
    """The scale is the tightest panel's own fit, times the border margin --
    a formula over measured widths, not a constant."""
    page = _with_patches(app)
    strip = _enter(page)
    page._select_patch(1)
    QtTest.QTest.qWait(20)
    y0, y1, x0, x1 = P2
    pw, ph = float(x1 - x0), float(y1 - y0)
    want = min(min(w / pw, h / ph)
               for w, h in page._compare_panel_sizes()) * page._PATCH_FIT_MARGIN
    assert strip.camera(0)[2] == pytest.approx(want, rel=1e-6)


def test_a_far_away_zoomed_in_view_still_jumps_to_the_patch(app):
    """Zoomed right in on the opposite corner, P1 still goes to P1."""
    page = _with_patches(app)
    strip = _enter(page)
    strip.set_camera(SLIDE_W - 100.0, SLIDE_H - 100.0, 8.0)
    QtTest.QTest.qWait(20)
    page._select_patch(0)
    QtTest.QTest.qWait(20)
    want_x, want_y = _centre_of(P1)
    cam = strip.camera(0)
    assert cam[0] == pytest.approx(want_x, abs=1.0)
    assert cam[1] == pytest.approx(want_y, abs=1.0)
    assert cam[2] != pytest.approx(8.0, rel=1e-6)


def test_walking_the_three_patches_lands_on_each_in_turn(app):
    page = _with_patches(app)
    strip = _enter(page)
    for idx, patch in ((0, P1), (2, P3), (1, P2), (0, P1)):
        page._select_patch(idx)
        QtTest.QTest.qWait(20)
        want_x, want_y = _centre_of(patch)
        cam = strip.camera(0)
        assert cam[0] == pytest.approx(want_x, abs=1.0)
        assert cam[1] == pytest.approx(want_y, abs=1.0)


# ── 3. it is a camera move and NOTHING else ──────────────────────────────

def test_selecting_a_patch_does_not_rebuild_the_backend(app):
    page = _with_patches(app)
    strip = _enter(page)
    before = len(page._compare_builds)
    controllers = list(strip.controllers)
    for idx in (0, 1, 2, 0):
        page._select_patch(idx)
        QtTest.QTest.qWait(20)
    assert len(page._compare_builds) == before
    assert list(strip.controllers) == controllers


def test_selecting_a_patch_starts_no_worker(app):
    page = _with_patches(app)
    _enter(page)
    for idx in (0, 1, 2):
        page._select_patch(idx)
        QtTest.QTest.qWait(20)
    assert page._batch_worker is None
    assert page._preview_worker is None
    assert page.production_correction_busy() is None


def test_selecting_a_patch_reselects_no_channel(app):
    """A camera move is not a selection change: the panels keep the channel
    and the method they had."""
    page = _with_patches(app)
    strip = _enter(page)
    before = [len(c.selections) for c in strip.controllers]
    for idx in (0, 1, 2):
        page._select_patch(idx)
        QtTest.QTest.qWait(20)
    assert [len(c.selections) for c in strip.controllers] == before


def test_the_move_goes_through_the_strips_camera_seam(app):
    """`set_camera`, not three ViewBoxes -- that is what solves each panel's
    own rectangle and issues its tile requests."""
    page = _with_patches(app)
    strip = _enter(page)
    seen = []
    real = strip.set_camera

    def spy(cx, cy, scale):
        seen.append((cx, cy, scale))
        return real(cx, cy, scale)

    strip.set_camera = spy
    try:
        page._select_patch(2)
        QtTest.QTest.qWait(20)
    finally:
        strip.set_camera = real
    assert len(seen) == 1
    want_x, want_y = _centre_of(P3)
    assert seen[0][0] == pytest.approx(want_x)
    assert seen[0][1] == pytest.approx(want_y)


def test_the_panels_ask_for_the_tiles_of_where_they_land(app):
    page = _with_patches(app)
    strip = _enter(page)
    before = [c._current_bbox for c in strip.controllers]
    page._select_patch(1)
    QtTest.QTest.qWait(20)
    after = [c._current_bbox for c in strip.controllers]
    assert after != before
    y0, y1, x0, x1 = P2
    for by0, bx0, by1, bx1 in after:
        assert bx0 <= x0 and bx1 >= x1
        assert by0 <= y0 and by1 >= y1


# ── 4. the compare branch is the one the compare mode takes ─────────────

def test_the_dispatch_sends_the_compare_mode_to_the_compare_panels(app):
    """`_select_patch` does not know which viewer is up; one method does.
    In compare mode that method must reach the panels and leave the full
    image's own camera exactly where it was."""
    page = _with_patches(app)
    strip = _enter(page, 4000.0, 200.0)
    assert page._compare_mode() is True
    full_before = page._full_image_camera()
    jumps = len(page._explore_tab.stack.controller.jumps)

    assert page._navigate_active_view_to_patch(2) is True

    QtTest.QTest.qWait(20)
    want_x, want_y = _centre_of(P3)
    cam = strip.camera(0)
    assert cam[0] == pytest.approx(want_x, abs=1.0)
    assert cam[1] == pytest.approx(want_y, abs=1.0)
    # The full image is not on screen: it was not moved.
    assert page._full_image_camera() == pytest.approx(full_before)
    assert len(page._explore_tab.stack.controller.jumps) == jumps


# ── 5. safe no-ops ───────────────────────────────────────────────────────

def _unchanged(page, strip, call):
    before = _cams(strip)
    call()
    QtTest.QTest.qWait(20)
    for a, b in zip(before, _cams(strip)):
        assert a == pytest.approx(b, rel=1e-9, abs=1e-6)


def test_no_patches_at_all_is_a_no_op(app):
    page = _with_patches(app, patches=())
    strip = _enter(page)
    _unchanged(page, strip, lambda: page._select_patch(0))


def test_an_index_past_the_end_is_a_no_op(app):
    page = _with_patches(app)
    strip = _enter(page)
    _unchanged(page, strip, lambda: page._navigate_compare_to_patch(7))
    _unchanged(page, strip, lambda: page._navigate_compare_to_patch(-1))


def test_a_zero_area_patch_is_a_no_op(app):
    page = _with_patches(app, patches=((500, 500, 500, 900),))
    strip = _enter(page)
    _unchanged(page, strip, lambda: page._select_patch(0))
    assert page._patch_fit_camera((500, 500, 500, 900)) is None


def test_a_patch_off_the_slide_is_a_no_op(app):
    page = _with_patches(app, patches=((SLIDE_H - 10, SLIDE_H + 500,
                                        10, 500),))
    strip = _enter(page)
    _unchanged(page, strip, lambda: page._select_patch(0))
    assert page._patch_fit_camera((-50, 100, 10, 500)) is None


def test_an_unbuilt_strip_is_a_no_op(app):
    """No compare mode has ever been entered: nothing to move, no crash."""
    page = _with_patches(app)
    assert page._compare_strip_widget.built is False
    assert page._navigate_compare_to_patch(0) is False
    page._select_patch(1)
    assert page.current_patch_idx == 1


def test_a_garbage_index_is_a_no_op(app):
    page = _with_patches(app)
    _enter(page)
    assert page._navigate_compare_to_patch(None) is False
    assert page._navigate_compare_to_patch("2") is True     # int("2") is 2
