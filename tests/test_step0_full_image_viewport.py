"""The viewer opens where its caller asked it to.

`viewport_l0` -- `(y0, x0, w, h)` in level-0 pixels -- is the explore tab's
"open HERE" argument, and this module pins what happens to it: it reaches
the controller on both the cold path (inside the build, after
`load_overview` has installed the record, because before that a camera move
issues nothing) and the warm path (after any `set_selection`), and nothing
is remembered when the build is refused.

It no longer starts from a compare panel. The three panels used to be a
small viewer with a camera of its own, and a ⤢ button converted that
camera into this rectangle; A2 made them a static SNAPSHOT of the full
image instead, so the page passes no viewport at all and the conversion,
its per-panel reading and the "compare is untouched" section went with the
gesture. What is tested here is the argument, driven directly.

Its own module: `test_step0_background_correction_tab.py` running FIRST in
a process segfaults deterministically at a fixed site, so page-heavy
modules stay separate.

No slide, no wall-clock, no golden files: a recording explore tab and a
fake stack factory.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

import pyqtgraph as pg  # noqa: E402

from block01.ui.step0 import step0_page as sp  # noqa: E402
from block01.ui.step0 import step0_explore_tab as et  # noqa: E402

from test_step0_background_correction_tab import app  # noqa: E402,F401


@pytest.fixture(scope="module", autouse=True)
def _row_major():
    """The compare ImageItems pass `axisOrder="row-major"` themselves.

    This pins the process-global to the OPPOSITE value: if the explicit
    argument were ever dropped, the item test below would fail instead of
    the panels silently transposing.
    """
    pg.setConfigOptions(imageAxisOrder="col-major")


# ── 1. the compare items still say which way round they are ──────────────

def test_the_compare_items_set_row_major_themselves(app):
    """A snapshot places its arrays on a LEVEL-0 rectangle, so a
    transposition would put the panels on a different part of the slide
    from the full image they were cut out of. pyqtgraph captures
    `axisOrder` once in `ImageItem.__init__` and its library default is
    col-major, so this must not come from the process-global."""
    page = sp.Step0Page()
    assert [i.axisOrder for i in page._preview_imgs] == ["row-major"] * 3
    assert pg.getConfigOption("imageAxisOrder") == "col-major"



# ── 2. delivery to the viewer ────────────────────────────────────────────

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
    """Asking for the same source again is the caller asking to see this
    region, so the rectangle is applied even when the triple did not
    change."""
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


# ── 3. the real cold path ────────────────────────────────────────────────
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
