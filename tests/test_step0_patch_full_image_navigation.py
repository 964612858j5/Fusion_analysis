"""P1/P2/P3 take the FULL IMAGE to the patch too.

The compare panels have flown to the picked patch since the `_select_patch`
duplicate was removed; the full image did not, on the reasoning that the
whole slide is the landing view and picking a patch is not a request to
leave it. That rule is revoked. The Pn buttons are the shortcut to a PLACE
ON THE SLIDE, and a shortcut that works in one of the two viewers and
silently does nothing in the other has a meaning that depends on a mode the
user is not looking at.

So there are two branches and exactly ONE place that chooses between them,
`_navigate_active_view_to_patch`, reached from the single `_select_patch`
that every user entry already converges on -- the Pn buttons, the patch
list, and the Tissue Preview's selection sync. Adding a fourth entry later
gets the same answer for free; adding a fourth COPY of the choice is what
this module exists to prevent.

The full-image branch is a camera move through the existing public entry,
`controller.jump_to(y0, x0, w, h)`. That hands the ViewBox a level-0 rect,
which an aspect-locked box fits, so the patch is shown WHOLE at the full
image's own aspect ratio. Nothing else about the viewer changes: not the
channel, not Original/TopHat/cuCIM, not the parameters, not the DAPI or
marker layers, not the stack, and no Process or preview worker starts. The
viewer asks for the tiles of where it lands the moment it lands, exactly as
a pan does.

`navigate=False` still moves NOTHING, in either viewer: that is the flag
for the callers that are bookkeeping rather than a user's choice --
`_rebuild_patch_list` putting a row back after a `clear()`, which arrives
through the same selection signal. A patch renumber must not fly the
camera away.

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


# The same three far-apart patches the compare module uses, so "the same
# three patches behave the same way in both viewers" is a claim these two
# modules make about one set of numbers.
P1 = (100, 356, 100, 356)              # 256 x 256, top-left
P2 = (1800, 2312, 1500, 2524)          # 512 x 1024, middle, wide
P3 = (3000, 3128, 3500, 3628)          # 128 x 128, bottom-right, small


def _with_patches(app, patches=(P1, P2, P3)):
    page = _page(app)
    page.patches = [tuple(p) for p in patches]
    page._rebuild_patch_buttons()
    page._patch_list.clear()
    for i, (y0, y1, x0, x1) in enumerate(page.patches):
        page._patch_list.addItem(f"P{i+1}  [{y1-y0}x{x1-x0}px]")
    return page


def _controller(page):
    return page._explore_tab.stack.controller


def _viewport(page):
    """The full image's viewport as level-0 `(x0, x1, y0, y1)`."""
    (x0, x1), (y0, y1) = _controller(page).view_box.viewRange()
    return (x0, x1, y0, y1)


def _far_away(page):
    """Put the full image somewhere none of the three patches is."""
    _controller(page).jump_to(SLIDE_H - 600, SLIDE_W - 600, 500, 500)
    _controller(page).jumps.clear()
    QtTest.QTest.qWait(10)


def _contains(viewport, patch):
    x0, x1, y0, y1 = viewport
    py0, py1, px0, px1 = patch
    return (x0 <= px0 + 1e-6 and x1 >= px1 - 1e-6
            and y0 <= py0 + 1e-6 and y1 >= py1 - 1e-6)


# ── 1. the camera goes to the patch ──────────────────────────────────────

def test_p1_from_far_away_jumps_the_full_image_to_p1(app):
    """The reported case: the viewer is nowhere near P1, the user clicks
    P1, the full image is looking at P1 with the whole patch visible."""
    page = _with_patches(app)
    assert page._compare_mode() is False
    _far_away(page)
    before = _viewport(page)
    assert not _contains(before, P1)

    page._select_patch(0)
    QtTest.QTest.qWait(20)

    assert _controller(page).jumps == [(100, 100, 256, 256)]
    assert _viewport(page) != before
    assert _contains(_viewport(page), P1), "the patch is not entirely visible"


@pytest.mark.parametrize("idx,patch", [(0, P1), (1, P2), (2, P3)])
def test_each_button_goes_to_its_own_patch(app, idx, patch):
    """P2 is not P1. The bbox handed to `jump_to` is the picked patch's, in
    level-0 `(y0, x0, w, h)` -- never `patches[0]` for all three."""
    page = _with_patches(app)
    _far_away(page)

    page._select_patch(idx)
    QtTest.QTest.qWait(20)

    y0, y1, x0, x1 = patch
    assert _controller(page).jumps == [(y0, x0, x1 - x0, y1 - y0)]
    assert _contains(_viewport(page), patch)


def test_walking_the_three_patches_lands_on_each_in_turn(app):
    page = _with_patches(app)
    _far_away(page)
    for idx, patch in ((0, P1), (2, P3), (1, P2), (0, P1)):
        page._select_patch(idx)
        QtTest.QTest.qWait(20)
        assert _contains(_viewport(page), patch), f"P{idx+1}"


def test_the_patch_list_is_the_same_entry_as_the_buttons(app):
    """Two ways in, one behaviour -- through the one `_select_patch`."""
    page = _with_patches(app)
    _far_away(page)
    page._select_patch(1)
    QtTest.QTest.qWait(20)
    by_button = _viewport(page)

    page._select_patch(0)
    QtTest.QTest.qWait(20)
    _far_away(page)
    page._patch_list.setCurrentRow(1)
    QtTest.QTest.qWait(20)
    by_list = _viewport(page)

    assert by_list == pytest.approx(by_button, rel=1e-9, abs=1e-6)
    assert page.current_patch_idx == 1


def test_the_class_chooses_the_view_in_exactly_one_place(app):
    """Parsed, not grepped. Three UI callbacks can ask for a patch; if the
    choice between the two viewers is written out in each of them, the next
    change fixes two of the three."""
    tree = ast.parse(inspect.getsource(sp))
    classes = [n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "Step0Page"]
    assert len(classes) == 1
    names = [n.name for n in classes[0].body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert names.count("_select_patch") == 1
    assert names.count("_navigate_active_view_to_patch") == 1
    # Nobody but the dispatcher decides which viewer to move: the two
    # branch methods are called from it and from tests, never from a
    # second copy of the choice.
    callers = {}
    for fn in classes[0].body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) \
                    and isinstance(node.func, ast.Attribute) \
                    and node.func.attr in ("_navigate_compare_to_patch",
                                           "_navigate_full_image_to_patch"):
                callers.setdefault(node.func.attr, set()).add(fn.name)
    assert callers.get("_navigate_compare_to_patch") == \
        {"_navigate_active_view_to_patch"}
    assert callers.get("_navigate_full_image_to_patch") == \
        {"_navigate_active_view_to_patch"}


# ── 2. it is a camera move and NOTHING else ──────────────────────────────

def test_the_method_and_its_parameters_survive_the_jump(app):
    """On TopHat or cuCIM, going to a patch changes the camera and not the
    correction the user is looking through."""
    page = _with_patches(app)
    controller = _controller(page)
    controller.method = "tophat"
    controller.params = (50, 3)
    channel = controller.channel
    level = controller.level
    _far_away(page)

    for idx in (0, 1, 2):
        page._select_patch(idx)
        QtTest.QTest.qWait(20)

    assert controller.method == "tophat"
    assert controller.params == (50, 3)
    assert controller.channel == channel
    assert controller.level == level


def test_the_dapi_and_marker_layers_survive_the_jump(app):
    """The layer toggles are the user's reading of the picture, not part of
    where the picture is."""
    page = _with_patches(app)
    _far_away(page)
    dapi = page._btn_full_nucleus.isChecked()
    marker = page._btn_full_marker.isChecked()
    marker_calls = list(getattr(_controller(page),
                                "marker_visible_calls", []))

    for idx in (0, 1, 2):
        page._select_patch(idx)
        QtTest.QTest.qWait(20)

    assert page._btn_full_nucleus.isChecked() == dapi
    assert page._btn_full_marker.isChecked() == marker
    assert list(getattr(_controller(page), "marker_visible_calls", [])) \
        == marker_calls, "the jump touched a layer"


def test_the_stack_and_the_controller_are_the_same_objects_afterwards(app):
    """Identity, not equality: a rebuilt backend would throw away the tile
    pools and the overview record and re-read the slide."""
    page = _with_patches(app)
    stack = page._explore_tab.stack
    controller = stack.controller
    provider = stack.provider
    _far_away(page)

    for idx in (0, 1, 2, 0):
        page._select_patch(idx)
        QtTest.QTest.qWait(20)

    assert page._explore_tab.stack is stack
    assert page._explore_tab.stack.controller is controller
    assert page._explore_tab.stack.provider is provider


def test_no_worker_starts(app):
    """Not a Process, not a preview, not a batch. The viewer asks for the
    tiles of where it lands, which is what a pan does too."""
    page = _with_patches(app)
    _far_away(page)

    for idx in (0, 1, 2):
        page._select_patch(idx)
        QtTest.QTest.qWait(20)

    assert page._batch_worker is None
    assert page._preview_worker is None
    assert page.production_correction_busy() is None


def test_the_tissue_preview_rectangle_follows_the_new_viewport(app):
    """The dashed rectangle on the Tissue Preview IS the full image's
    viewport, so a jump has to redraw it -- otherwise the map still points
    at where the user used to be."""
    page = _with_patches(app)
    popup = page._ensure_tissue_navigator()
    _far_away(page)
    page._update_full_image_view_rect()
    before = popup.overview._current_view_full

    page._select_patch(1)
    QtTest.QTest.qWait(20)

    after = popup.overview._current_view_full
    assert after is not None and after != before
    y0, y1, x0, x1 = after
    py0, py1, px0, px1 = P2
    assert x0 <= px0 + 1e-6 and x1 >= px1 - 1e-6
    assert y0 <= py0 + 1e-6 and y1 >= py1 - 1e-6


# ── 3. bookkeeping is not a user's choice ────────────────────────────────

def test_a_programmatic_selection_moves_the_full_image_nowhere(app):
    """`navigate=False` is the flag for the callers that are putting a list
    row back, not the ones that are a user's click."""
    page = _with_patches(app)
    _far_away(page)
    before = _viewport(page)

    page._select_patch(2, navigate=False)
    QtTest.QTest.qWait(20)

    assert page.current_patch_idx == 2, "the selection state must still move"
    assert _viewport(page) == pytest.approx(before)
    assert _controller(page).jumps == []


def test_a_list_rebuild_does_not_fly_the_camera_away(app):
    """The real bookkeeping path: `_rebuild_patch_list` clears the list and
    puts the row back, which comes through the same selection signal."""
    page = _with_patches(app)
    _far_away(page)
    page._select_patch(1)
    QtTest.QTest.qWait(20)
    settled = _viewport(page)
    _controller(page).jumps.clear()

    page._rebuild_patch_list()
    QtTest.QTest.qWait(20)

    assert _viewport(page) == pytest.approx(settled)
    assert _controller(page).jumps == []


def test_a_programmatic_selection_moves_the_compare_panels_nowhere(app):
    """The same flag, the other viewer."""
    page = _with_patches(app)
    strip = _enter(page, 4000.0, 200.0)
    before = [strip.camera(i) for i in range(3)]

    page._select_patch(2, navigate=False)
    QtTest.QTest.qWait(20)

    for a, b in zip(before, [strip.camera(i) for i in range(3)]):
        assert a == pytest.approx(b, rel=1e-9, abs=1e-6)


# ── 4. safe no-ops ───────────────────────────────────────────────────────

def test_an_index_that_is_not_a_patch_is_a_no_op(app):
    page = _with_patches(app)
    _far_away(page)
    before = _viewport(page)
    for idx in (7, -1, None, 3.5):
        assert page._navigate_full_image_to_patch(idx) is False
    assert _viewport(page) == pytest.approx(before)
    assert _controller(page).jumps == []


def test_a_zero_area_patch_is_a_no_op(app):
    page = _with_patches(app, patches=((500, 500, 500, 900),
                                       (500, 900, 500, 500)))
    _far_away(page)
    before = _viewport(page)
    for idx in (0, 1):
        assert page._navigate_full_image_to_patch(idx) is False
        page._select_patch(idx)
        QtTest.QTest.qWait(20)
    assert _viewport(page) == pytest.approx(before)
    assert _controller(page).jumps == []


def test_a_patch_off_the_slide_is_a_no_op(app):
    page = _with_patches(app, patches=((SLIDE_H - 10, SLIDE_H + 500, 10, 500),
                                       (-50, 100, 10, 500),
                                       (100, 500, SLIDE_W - 10, SLIDE_W + 90)))
    _far_away(page)
    before = _viewport(page)
    for idx in (0, 1, 2):
        assert page._navigate_full_image_to_patch(idx) is False
    assert _viewport(page) == pytest.approx(before)
    assert _controller(page).jumps == []


def test_a_suspended_camera_is_not_moved(app):
    """A production run holds the camera. Moving the viewer under it would
    fight the run for tiles and leave its badge describing a place the user
    is no longer at -- and the badge is left exactly as it was."""
    page = _with_patches(app)
    _far_away(page)
    before = _viewport(page)
    controller = _controller(page)
    controller.suspend_for_production("test", badge="Correcting…")
    assert controller.suspended is True
    badge = list(controller.suspend_calls)

    assert page._navigate_full_image_to_patch(1) is False
    page._select_patch(1)
    QtTest.QTest.qWait(20)

    assert _viewport(page) == pytest.approx(before)
    assert controller.jumps == []
    assert controller.suspend_calls == badge, "the badge was disturbed"
    assert controller.resume_calls == 0, "the jump resumed a held camera"
    # ...and when the run gives it back, the button works again.
    controller.resume_from_production()
    assert page._navigate_full_image_to_patch(1) is True
    assert _contains(_viewport(page), P2)


def test_no_viewer_at_all_is_a_no_op(app):
    """Torn down for a dataset switch, or never opened."""
    page = _with_patches(app)
    page._explore_tab.stack = None
    assert page._full_image_visible() is False
    assert page._navigate_full_image_to_patch(0) is False
    assert page._navigate_active_view_to_patch(0) is False
    page._select_patch(0)
    assert page.current_patch_idx == 0, "the selection state must still move"
