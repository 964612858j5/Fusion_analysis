"""The full image opens on the region the compare panel is showing.

Three separate claims, tested separately:

  1. the CONVERSION -- a compare panel's `viewRange()` becomes a level-0
     `(y0, x0, w, h)` by a pure offset, with the near edge floored, the far
     edge CEILED (the answer must CONTAIN what the user sees) and the result
     clipped to the SLIDE, not to the patch: the blank margin an
     aspect-locked panel shows around the patch maps to real neighbouring
     tissue, which is what the full image is there to fill in;
  2. the PANEL -- the button that was clicked decides which of the three
     ViewBoxes is read, which only shows up with zoom lock off;
  3. the DELIVERY -- the rect reaches the viewer on both the cold path
     (inside the build, after `load_overview`) and the warm path (after any
     `set_selection`), and nothing is remembered when the build is refused.

Its own module: `test_step0_background_correction_tab.py` running FIRST in a
process segfaults deterministically at a fixed site (measured 8/8, and 8/8
at the pre-`bcc2693` baseline too), so page-heavy modules stay separate.

No slide, no wall-clock, no golden files: synthetic payloads and a
recording explore tab.
"""

import math
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

import pyqtgraph as pg  # noqa: E402

from block01.ui.step0 import step0_page as sp  # noqa: E402
from block01.ui.step0 import step0_explore_tab as et  # noqa: E402

from test_step0_background_correction_tab import app  # noqa: E402,F401

# Patch at a NON-ZERO origin, NON-SQUARE, with H != W and both != the
# origins: an x/y swap or a dropped offset cannot coincidentally agree.
PATCH = (1000, 1030, 4000, 4070)       # (y0, y1, x0, x1), half-open, level-0
PATCH_H, PATCH_W = 30, 70
# The slide the patch sits in. Deliberately much larger than the patch and
# not a multiple of it, so "clipped to the slide" and "clipped to the patch"
# can never agree by accident.
SLIDE_H, SLIDE_W = 29000, 31000


@pytest.fixture(scope="module", autouse=True)
def _row_major():
    """The compare ImageItems now pass `axisOrder="row-major"` themselves.

    This still pins the process-global to the OPPOSITE value: if the
    explicit argument were ever dropped, every mapping assertion below
    would fail instead of silently transposing.
    """
    pg.setConfigOptions(imageAxisOrder="col-major")


class _RecordingTab:
    def __init__(self):
        self.calls = []
        # The page consults this after a successful show_source, to push the
        # marker-toggle state into a freshly built stack.
        self.stack = None

    def show_source(self, channel, method, params=(), *, viewport_l0=None,
                    tint=None):
        self.calls.append({"channel": channel, "method": method,
                           "params": tuple(params), "viewport": viewport_l0,
                           "tint": tint})
        return True

    def set_dataset(self, _path):
        pass

    def teardown(self, **_kw):
        pass


class _SlideLoader:
    """Only the two things the conversion reads: the slide extent."""

    shape = (SLIDE_H, SLIDE_W)

    def channel_names(self):
        return ["DAPI", "CD3", "CD20"]

    @property
    def ch_map(self):
        return {c: i for i, c in enumerate(self.channel_names())}


def _page(app, payload=True, patch=PATCH):
    page = sp.Step0Page()
    page.loader = _SlideLoader()
    page.patches = [patch] if patch is not None else []
    page.current_patch_idx = 0
    page.current_channel = "CD3"
    page.nucleus_channel = "DAPI"
    if payload:
        h = patch[1] - patch[0]
        w = patch[3] - patch[2]
        d = np.linspace(0, 1, h * w, dtype=np.float32).reshape(h, w)
        m = {"snr": 4.0, "bg_cv": 0.25}
        pl = {"original_disp": d, "tophat_disp": d,
              "cucim_disp": d, "nucleus_disp": None,
              "original_metrics": m, "tophat_metrics": m,
              "cucim_metrics": m}
        # Displayed AND registered under (channel, patch): the conversion
        # requires the identity, not merely a non-empty payload.
        page._last_payload = pl
        page._preview_cache[(page.current_channel, page.current_patch_idx)] = pl
    page._explore_tab = _RecordingTab()
    return page


def _set_range(page, idx, xr, yr):
    """Set one panel's range without the zoom lock fanning it out.

    `setRange` on an aspect-locked ViewBox expands the other axis to fit, so
    the range read back is >= what was asked for. Every assertion below is
    written against what the panel actually reports, not against the request.
    """
    page._btn_lock_zoom.setChecked(False)
    page._preview_vbs[idx].setRange(xRange=xr, yRange=yr, padding=0)


def _expected(page, idx):
    """The conversion, recomputed here from the panel's REPORTED range.

    Offset first, clip to the SLIDE second -- the patch is not a boundary.
    """
    (vx0, vx1), (vy0, vy1) = page._preview_vbs[idx].viewRange()
    lx0 = max(0, min(SLIDE_W, PATCH[2] + math.floor(vx0)))
    lx1 = max(0, min(SLIDE_W, PATCH[2] + math.ceil(vx1)))
    ly0 = max(0, min(SLIDE_H, PATCH[0] + math.floor(vy0)))
    ly1 = max(0, min(SLIDE_H, PATCH[0] + math.ceil(vy1)))
    return (ly0, lx0, lx1 - lx0, ly1 - ly0)


# ── 1. the conversion ────────────────────────────────────────────────────

def test_the_compare_items_set_row_major_themselves(app):
    """The mapping treats world x as the patch COLUMN. pyqtgraph captures
    `axisOrder` once in `ImageItem.__init__` and its library default is
    col-major, so this must not come from the process-global."""
    page = _page(app)
    assert [i.axisOrder for i in page._preview_imgs] == ["row-major"] * 3
    assert pg.getConfigOption("imageAxisOrder") == "col-major"


def test_a_zoomed_panel_maps_by_a_pure_offset(app):
    page = _page(app)
    _set_range(page, 0, (10, 30), (5, 15))

    got = page._compare_viewport_l0("original")

    y0, x0, w, h = got
    assert got == _expected(page, 0)
    # The offset lands on the RIGHT axis: x carries the patch's x origin.
    assert x0 == PATCH[2] + 10
    assert w == 20
    # y carries the patch's y origin. The aspect lock widens y past the
    # patch, and that margin is KEPT -- it is real slide, just not patch --
    # so the bound to check is the slide's, not the patch's.
    assert 0 <= y0 < SLIDE_H and 0 <= x0 < SLIDE_W
    assert abs(y0 - PATCH[0]) < PATCH_H


def test_the_far_edge_ceils_so_the_answer_contains_the_view(app):
    """y=[-0.25, 20.25] over a 30-row patch covers rows 0..20 inclusive.
    Flooring both ends would return h=20 and clip the bottom row off."""
    page = _page(app)
    page._preview_vbs[0].enableAutoRange(enable=False)
    # Set the range directly on the ViewBox state so the aspect lock does
    # not widen the axis under test.
    page._btn_lock_zoom.setChecked(False)
    page._preview_vbs[0].setAspectLocked(False)
    # Near edge on an exact integer, so this case isolates the FAR edge.
    page._preview_vbs[0].setRange(xRange=(0, PATCH_W), yRange=(0.0, 20.25),
                                  padding=0)
    (_, _), (vy0, vy1) = page._preview_vbs[0].viewRange()
    assert (vy0, vy1) == (0.0, 20.25), "test setup: exact range needed"

    y0, x0, w, h = page._compare_viewport_l0("original")

    assert (y0, h) == (PATCH[0] + 0, 21)         # NOT 20


def test_the_far_edge_ceils_on_x_too(app):
    """Same rule, x axis, stated separately: the sub-pixel case below lands
    on w=1 either way, so it does not pin this."""
    page = _page(app)
    page._btn_lock_zoom.setChecked(False)
    page._preview_vbs[0].setAspectLocked(False)
    page._preview_vbs[0].setRange(xRange=(0.0, 20.25), yRange=(0, PATCH_H),
                                  padding=0)
    (vx0, vx1), _ = page._preview_vbs[0].viewRange()
    assert (vx0, vx1) == (0.0, 20.25), "test setup: exact range needed"

    y0, x0, w, h = page._compare_viewport_l0("original")

    assert (x0, w) == (PATCH[2] + 0, 21)         # NOT 20


def test_a_sub_pixel_view_still_names_one_pixel(app):
    """Zoomed inside a single pixel is a real place in the slide. Falling
    back to the whole slide there would throw the position away exactly when
    the user is most zoomed in."""
    page = _page(app)
    page._btn_lock_zoom.setChecked(False)
    page._preview_vbs[0].setAspectLocked(False)
    page._preview_vbs[0].setRange(xRange=(12.2, 12.5), yRange=(7.1, 7.4),
                                  padding=0)

    y0, x0, w, h = page._compare_viewport_l0("original")

    assert (w, h) == (1, 1)
    assert (y0, x0) == (PATCH[0] + 7, PATCH[2] + 12)


def test_a_view_beyond_the_patch_edge_maps_to_the_neighbouring_slide(app):
    """Past the patch edge is not out of bounds -- it is the tissue next to
    the patch, and it is exactly what the full image should fill in."""
    page = _page(app)
    page._btn_lock_zoom.setChecked(False)
    page._preview_vbs[0].setAspectLocked(False)
    page._preview_vbs[0].setRange(xRange=(PATCH_W + 5, PATCH_W + 25),
                                  yRange=(PATCH_H + 5, PATCH_H + 15),
                                  padding=0)

    y0, x0, w, h = page._compare_viewport_l0("original")

    # Entirely OUTSIDE the patch, entirely INSIDE the slide.
    assert (y0, x0) == (PATCH[1] + 5, PATCH[3] + 5)
    assert (w, h) == (20, 10)
    assert y0 >= PATCH[1] and x0 >= PATCH[3]


def test_the_margin_around_the_patch_becomes_neighbouring_slide(app):
    """The reviewer's case. An aspect-locked panel reports far outside the
    patch -- measured y=[-70, 100] for a patch at y=1000..1030. Clipping to
    the patch would open on the patch alone; the same offset maps that
    margin to slide rows 930..1100."""
    page = _page(app)
    page._btn_lock_zoom.setChecked(False)
    page._preview_vbs[0].setAspectLocked(False)
    page._preview_vbs[0].setRange(xRange=(-70, 100), yRange=(-70, 100),
                                  padding=0)

    y0, x0, w, h = page._compare_viewport_l0("original")

    assert (y0, y0 + h) == (930, 1100)                # NOT (1000, 1030)
    assert (x0, x0 + w) == (PATCH[2] - 70, PATCH[2] + 100)
    assert h == 170 and w == 170


def test_only_the_slide_edge_clips(app):
    """A patch near the slide's corner: the margin is kept where the slide
    continues and cut only where the slide ends."""
    corner = (5, 35, SLIDE_W - 40, SLIDE_W - 10)      # 30 x 30, near the right
    page = _page(app, patch=corner)
    page._btn_lock_zoom.setChecked(False)
    page._preview_vbs[0].setAspectLocked(False)
    page._preview_vbs[0].setRange(xRange=(-20, 100), yRange=(-20, 100),
                                  padding=0)

    y0, x0, w, h = page._compare_viewport_l0("original")

    # Top: 5 - 20 = -15 -> clipped to 0. Bottom: 5 + 100 = 105, well inside.
    assert (y0, y0 + h) == (0, 105)
    # Left: kept in full (corner[2] - 20). Right: runs past the slide -> W.
    assert x0 == corner[2] - 20
    assert x0 + w == SLIDE_W
    assert w < 120, "the right margin must be cut at the slide edge"


@pytest.mark.parametrize(
    ("why", "mutate"),
    [("no payload on screen",
      lambda p: setattr(p, "_last_payload", None)),
     ("no patch", lambda p: setattr(p, "patches", [])),
     ("patch index out of range",
      lambda p: setattr(p, "current_patch_idx", 7)),
     ("zero-area patch",
      lambda p: setattr(p, "patches", [(1000, 1000, 4000, 4000)])),
     ("no loader, so no slide extent", lambda p: setattr(p, "loader", None)),
     # A payload from ANOTHER patch would otherwise be given this patch's
     # origin -- a wrong position, not a missing one.
     ("payload belongs to another patch",
      lambda p: p._preview_cache.__setitem__(
          (p.current_channel, p.current_patch_idx), {"other": True})),
     ("view wholly outside the slide",
      lambda p: p._preview_vbs[0].setRange(
          xRange=(-PATCH[2] - 500, -PATCH[2] - 100),
          yRange=(0, 10), padding=0)),
     # A ViewBox will not accept inf through setRange, so the value is
     # injected where the conversion actually reads it.
     ("non-finite range",
      lambda p: setattr(p._preview_vbs[0], "viewRange",
                        lambda: [[0.0, float("nan")], [0.0, 1.0]])),
     ("unknown source", lambda p: None)],
)
def test_there_is_no_viewport_when_it_cannot_be_trusted(app, why, mutate):
    page = _page(app)
    mutate(page)
    source = "nope" if why == "unknown source" else "original"

    assert page._compare_viewport_l0(source) is None


# ── 2. the panel that was clicked ────────────────────────────────────────

def test_each_button_reads_its_own_view_box(app):
    """With zoom lock OFF the three panels hold three ranges. Reading
    `_preview_vbs[0]` for every button would pass with the lock on."""
    page = _page(app)
    page._btn_lock_zoom.setChecked(False)
    for idx, xr in ((0, (0, 10)), (1, (20, 40)), (2, (50, 70))):
        page._preview_vbs[idx].setRange(xRange=xr, yRange=(0, 10), padding=0)

    got = [page._compare_viewport_l0(src) for src in sp.FULL_IMAGE_SOURCES]

    assert got == [_expected(page, 0), _expected(page, 1), _expected(page, 2)]
    xs = [g[1] for g in got]
    assert len(set(xs)) == 3, f"all three buttons read the same panel: {xs}"
    assert xs == sorted(xs) and xs[0] == PATCH[2] + 0


def test_the_button_click_passes_the_viewport_down(app):
    page = _page(app)
    _set_range(page, 2, (50, 70), (0, 10))

    page._enter_full_image("cucim")

    call = page._explore_tab.calls[-1]
    assert call["viewport"] == _expected(page, 2)
    assert page._preview_stack.currentIndex() == sp.PREVIEW_PAGE_FULL_IMAGE


def test_a_channel_change_and_a_reopen_do_not_reposition(app):
    """The user may have panned inside the full image; a channel change or a
    post-run rebuild must not yank them back to the compare patch."""
    page = _page(app)
    _set_range(page, 0, (10, 30), (5, 15))
    page._preview_stack.setCurrentIndex(sp.PREVIEW_PAGE_FULL_IMAGE)

    page._sync_full_image_to_channel()
    page._reopen_full_image()

    assert [c["viewport"] for c in page._explore_tab.calls] == [None, None]


# ── 3. delivery to the viewer ────────────────────────────────────────────

class _FakeController:
    def __init__(self):
        self.order = []
        self.jump_calls = []
        self.selection_calls = []
        self.tints = []
        self.marker_visible = []
        self.channel, self.method, self.params = "CD3", None, ()

    def set_selection(self, channel=None, method=None, params=None):
        self.selection_calls.append((channel, method, tuple(params or ())))
        self.order.append("set_selection")
        if channel is not None:
            self.channel = channel
        self.method = method
        self.params = tuple(params or ())

    def set_tint(self, rgb):
        self.tints.append(rgb)
        self.order.append("set_tint")

    def set_marker_visible(self, visible):
        self.marker_visible.append(bool(visible))

    def jump_to(self, y0, x0, w, h):
        self.jump_calls.append((y0, x0, w, h))
        self.order.append("jump_to")


def _tab_with_factory(app, order):
    """A tab whose factory records the build ORDER against a shared list, so
    'the camera moved after the overview was installed' is checkable."""
    from PyQt5 import QtWidgets

    class _Stack:
        def __init__(self, controller):
            self.controller = controller
            self.provider = self
            self.scheduler = self
            self.view = QtWidgets.QLabel("fake")
            self.caches = ()

        def level_shape(self, _lvl):
            return (29000, 31000)

        def shutdown(self):
            pass

        def close(self):
            pass

        def teardown(self, *, wait_for_floor=False):
            pass

    made = {}

    def factory(path, channel, parent_widget=None, *, method=None, params=(),
                initial_viewport_l0=None, tint=None):
        ctl = _FakeController()
        ctl.tints.append(tint)
        ctl.order = order
        ctl.channel, ctl.method, ctl.params = channel, method, tuple(params)
        order.append("load_overview")
        if initial_viewport_l0 is not None:
            ctl.jump_to(*initial_viewport_l0)
        made["controller"] = ctl
        stack = _Stack(ctl)
        stack.view.setParent(parent_widget)
        return stack

    tab = et.Step0ExploreTab(_FakePage(), stack_factory=factory)
    return tab, made


class _FakePage:
    current_channel = "CD3"


def test_the_cold_path_moves_the_camera_after_the_overview(app):
    order = []
    tab, made = _tab_with_factory(app, order)
    tab.set_dataset("/data/slide_a.ome.tif")

    assert tab.show_source("CD3", "tophat", (15,),
                           viewport_l0=(1005, 4010, 20, 10)) is True

    assert made["controller"].jump_calls == [(1005, 4010, 20, 10)]
    assert order.index("load_overview") < order.index("jump_to")
    tab.teardown()


def test_the_cold_path_without_a_viewport_opens_on_the_whole_slide(app):
    order = []
    tab, made = _tab_with_factory(app, order)
    tab.set_dataset("/data/slide_a.ome.tif")

    tab.show_source("CD3", None, ())

    assert made["controller"].jump_calls == []
    assert "jump_to" not in order
    tab.teardown()


def test_the_warm_path_repositions_even_for_the_same_source(app):
    """Clicking the same ⤢ again is the user asking to see this region, and
    the region is whatever the compare panel shows NOW."""
    order = []
    tab, made = _tab_with_factory(app, order)
    tab.set_dataset("/data/slide_a.ome.tif")
    tab.show_source("CD3", "tophat", (15,), viewport_l0=(1000, 4000, 70, 30))
    ctl = made["controller"]

    tab.show_source("CD3", "tophat", (15,), viewport_l0=(1005, 4010, 20, 10))

    assert ctl.selection_calls == []              # triple unchanged
    assert ctl.jump_calls[-1] == (1005, 4010, 20, 10)
    tab.teardown()


def test_the_warm_path_selects_first_then_repositions(app):
    order = []
    tab, made = _tab_with_factory(app, order)
    tab.set_dataset("/data/slide_a.ome.tif")
    tab.show_source("CD3", None, ())
    ctl = made["controller"]
    order.clear()

    tab.show_source("CD3", "cucim", (7,), viewport_l0=(1005, 4010, 20, 10))

    assert ctl.selection_calls[-1] == ("CD3", "cucim", (7,))
    assert ctl.jump_calls[-1] == (1005, 4010, 20, 10)
    assert order.index("set_selection") < order.index("jump_to")
    tab.teardown()


def test_a_busy_refusal_leaves_no_target_behind(app):
    order = []
    busy = {"reason": "patch background correction"}
    tab, made = _tab_with_factory(app, order)
    tab._busy_probe = lambda: busy["reason"]
    tab.set_dataset("/data/slide_a.ome.tif")

    tab.show_source("CD3", "tophat", (15,), viewport_l0=(1005, 4010, 20, 10))
    assert tab.stack is None
    assert "controller" not in made

    # The run finishes and the user clicks again: the NEW region is used,
    # and nothing from the refused attempt survived.
    busy["reason"] = None
    tab.show_source("CD3", "tophat", (15,), viewport_l0=(2000, 5000, 40, 40))

    assert made["controller"].jump_calls == [(2000, 5000, 40, 40)]
    tab.teardown()


def test_a_dataset_switch_does_not_reuse_the_previous_bbox(app):
    """Nothing is stored anywhere, so the only way a stale bbox could return
    is if the page kept one. Prove the click after a switch recomputes."""
    page = _page(app)
    _set_range(page, 0, (10, 30), (5, 15))
    page._enter_full_image("original")
    first = page._explore_tab.calls[-1]["viewport"]
    assert first is not None

    # What a committed switch does to the display state.
    page._reset_dataset_view_state()
    page._enter_full_image("original")

    assert page._explore_tab.calls[-1]["viewport"] is None
    assert page._preview_stack.currentIndex() == sp.PREVIEW_PAGE_FULL_IMAGE


# ── 4. compare is untouched ──────────────────────────────────────────────

def test_going_to_the_full_image_and_back_leaves_the_panels_alone(app):
    page = _page(app)
    page.resize(1200, 800)
    page.show()
    _set_range(page, 0, (10, 30), (5, 15))
    from PyQt5 import QtWidgets
    QtWidgets.QApplication.processEvents()
    before = [[list(ax) for ax in vb.viewRange()] for vb in page._preview_vbs]

    page._enter_full_image("original")
    QtWidgets.QApplication.processEvents()
    page._return_to_compare()
    QtWidgets.QApplication.processEvents()

    after = [[list(ax) for ax in vb.viewRange()] for vb in page._preview_vbs]
    assert after == before


# ── 5. the real cold path ────────────────────────────────────────────────
#
# The cases above use a fake factory, which proves the tab PASSES the rect
# but not that `build_default_stack` applies it -- and where it applies it
# is the whole point: before `load_overview` the controller is blocked on
# the overview record and a camera move would issue nothing. This drives
# the real function with the viewer classes replaced.

def test_build_default_stack_moves_the_camera_after_load_overview(monkeypatch):
    import importlib
    from PyQt5 import QtWidgets

    order = []

    class _Provider:
        channel_names = ["CD3", "CD20"]

        def __init__(self, path):
            self.path = path

        def level_shape(self, _lvl):
            return (29000, 31000)

        def close(self):
            pass

    class _Controller:
        def __init__(self, *_a, **_kw):
            self.calls = []

        def set_selection(self, **kw):
            order.append("set_selection")
            self.calls.append(kw)

        def load_overview(self, ensure_floor=True):
            order.append("load_overview")

        def jump_to(self, y0, x0, w, h):
            order.append(("jump_to", y0, x0, w, h))

    class _View:
        def __init__(self, parent=None):
            self.view_box = _Box()
            self._w = QtWidgets.QLabel("fake", parent)

        def __getattr__(self, name):
            return getattr(self._w, name)

    class _Box:
        def __init__(self):
            self.ranges = []

        def setRange(self, **kw):
            order.append("setRange")
            self.ranges.append(kw)

    mods = {
        "block01.viewer.raw_tile_provider": {"RawTileProvider": _Provider},
        "block01.viewer.explore_view": {"ExploreController": _Controller,
                                        "ExploreView": _View},
        "block01.viewer.scheduler": {"TileScheduler": lambda *a, **k: object()},
        "block01.viewer.correction_compute": {
            "CorrectionCompute": lambda *a, **k: object()},
    }
    for name, attrs in mods.items():
        mod = importlib.import_module(name)
        for attr, value in attrs.items():
            monkeypatch.setattr(mod, attr, value)

    et.build_default_stack("/data/a.ome.tif", "CD3", None,
                           method="tophat", params=(15,),
                           initial_viewport_l0=(1005, 4010, 20, 10))

    assert ("jump_to", 1005, 4010, 20, 10) in order
    assert order.index("load_overview") < order.index(
        ("jump_to", 1005, 4010, 20, 10))
    assert "setRange" not in order, (
        "the whole-slide range must NOT also be applied -- it would replace "
        "the requested viewport")


def test_build_default_stack_falls_back_to_the_whole_slide(monkeypatch):
    """Same drive, no viewport: the pre-existing whole-slide open."""
    import importlib
    from PyQt5 import QtWidgets

    order = []
    boxes = []

    class _Provider:
        channel_names = ["CD3"]

        def __init__(self, path):
            pass

        def level_shape(self, _lvl):
            return (29000, 31000)

        def close(self):
            pass

    class _Controller:
        def __init__(self, *_a, **_kw):
            pass

        def set_selection(self, **kw):
            order.append("set_selection")

        def load_overview(self, ensure_floor=True):
            order.append("load_overview")

        def jump_to(self, *a):
            order.append("jump_to")

    class _Box:
        def setRange(self, **kw):
            order.append("setRange")
            boxes.append(kw)

    class _View:
        def __init__(self, parent=None):
            self.view_box = _Box()
            self._w = QtWidgets.QLabel("fake", parent)

        def __getattr__(self, name):
            return getattr(self._w, name)

    mods = {
        "block01.viewer.raw_tile_provider": {"RawTileProvider": _Provider},
        "block01.viewer.explore_view": {"ExploreController": _Controller,
                                        "ExploreView": _View},
        "block01.viewer.scheduler": {"TileScheduler": lambda *a, **k: object()},
        "block01.viewer.correction_compute": {
            "CorrectionCompute": lambda *a, **k: object()},
    }
    for name, attrs in mods.items():
        mod = importlib.import_module(name)
        for attr, value in attrs.items():
            monkeypatch.setattr(mod, attr, value)

    et.build_default_stack("/data/a.ome.tif", "CD3", None)

    assert "jump_to" not in order
    assert boxes == [{"xRange": (0, 31000), "yRange": (0, 29000),
                      "padding": 0}]
