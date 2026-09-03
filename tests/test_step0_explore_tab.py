"""P0 mount tests for the Step0 Explore tab (docs/v15_step0_mount_plan.md §7).

Everything here runs against a FAKE stack: P0 is about lifecycle -- lazy
build, dataset switching, teardown order and idempotence -- not about
pixels, and a unit test must not need a real 31k x 29k slide to check that
a provider gets closed.
"""

import os
import re
import time

import numpy as np
import pytest

pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from block01.ui.step0 import step0_explore_tab as _et  # noqa: E402
from block01.ui.step0.step0_explore_tab import (  # noqa: E402
    ExploreStack, Step0ExploreTab, _cleanup_partial_stack,
    build_default_stack,
)


@pytest.fixture(scope="module")
def app():
    from PyQt5 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class FakeProvider:
    def __init__(self, path):
        self.path = path
        self.closed = False

    def read(self):
        if self.closed:
            raise RuntimeError("provider is closed")
        return f"pixels of {self.path}"

    def source_identity(self):
        return ("raw", self.path)

    def close(self):
        self.closed = True


class FakeCache:
    """Enough of LRUByteCache to prove teardown EMPTIES it. Dropping the
    tuple reference frees nothing -- the scheduler and the compute layer
    hold their own references to these same objects."""

    def __init__(self, name):
        self.name = name
        self._store = {"tile": np.zeros(16, dtype=np.uint8)}
        self._bytes = 16

    def stats(self):
        return {"items": len(self._store), "bytes": self._bytes}

    def clear(self):
        self._store.clear()
        self._bytes = 0


class FakeScheduler:
    def __init__(self, order):
        self._order = order
        self.workers_running = True

    def shutdown(self):
        self._order.append("scheduler.shutdown")
        self.workers_running = False


_UNSET = object()          # the fakes' stand-in for the controller's own


class FakeController:
    """Mirrors `ExploreController.teardown`'s contract: scheduler first
    (which joins the workers), provider second."""

    def __init__(self, provider, scheduler, order, channel=None,
                 method=None, params=()):
        self.provider = provider
        self.scheduler = scheduler
        self._order = order
        self.teardown_calls = 0
        self.teardown_waits = []
        # The selection surface the tab reads and writes.
        self.channel = channel
        self.method = method
        self.params = tuple(params)
        # What each call actually PASSED, as kwargs -- not the resulting
        # state. An omitted argument and an argument set to None are
        # different things here (see `set_selection`).
        self.selection_calls = []
        # Camera moves, in order, as (y0, x0, w, h) level-0 tuples. Shares
        # `_order` with the teardown steps so a test can assert that a
        # selection change lands BEFORE the camera move.
        self.jump_calls = []
        self.tints = []
        self.marker_visible = []

    def set_selection(self, channel=_UNSET, method=_UNSET, params=_UNSET):
        """Mirrors `ExploreController.set_selection`'s sentinel semantics.

        The real one defaults every argument to a private `_UNSET` and
        leaves what it is not given ALONE -- so `set_selection(channel=x)`
        keeps the current method and params. A fake defaulting them to None
        would silently clear the method instead, and a test built on it
        would "prove" a channel switch drops the correction.
        """
        passed = {}
        if channel is not _UNSET:
            passed["channel"] = channel
            self.channel = channel
        if method is not _UNSET:
            passed["method"] = method
            self.method = method
        if params is not _UNSET:
            passed["params"] = tuple(params) if params is not None else ()
            self.params = passed["params"]
        self.selection_calls.append(passed)

    def set_tint(self, rgb):
        """Mirrors `ExploreController.set_tint`: display-only, recorded in
        the shared order list so 'coloured before the camera moved' is
        checkable."""
        self.tints.append(rgb)
        self._order.append("controller.set_tint")

    def set_marker_visible(self, visible):
        self.marker_visible.append(bool(visible))

    def jump_to(self, y0, x0, w, h):
        """Mirrors `ExploreController.jump_to`: level-0 coordinates, and
        the ONLY camera entry the tab is allowed to use."""
        self.jump_calls.append((y0, x0, w, h))
        self._order.append("controller.jump_to")

    def teardown(self, *, wait_for_floor=False):
        self.teardown_calls += 1
        # Recorded, not acted on: the fake has no floor thread. What matters
        # is that the tab passes the hand-off flag through.
        self.teardown_waits.append(wait_for_floor)
        if self.provider.closed:
            return
        self.scheduler.shutdown()
        self._order.append("provider.close")
        self.provider.close()


class FakeView(QtWidgets.QLabel):
    """Stands in for the ExploreView QWidget.

    A REAL QWidget, because the tab puts it in a layout (`indexOf` refuses
    anything else). Carries the two surfaces a RELEASE touches -- the
    status badge and the camera's mouse enable -- since a release now keeps
    this widget on screen instead of deleting it.
    """

    def __init__(self, parent, provider):
        super().__init__("fake explore view", parent)
        self.provider = provider
        self.status_text = None
        self.mouse_enabled = (True, True)
        view = self

        class _Box:
            @staticmethod
            def setMouseEnabled(x, y):
                view.mouse_enabled = (x, y)

        self.view_box = _Box()

    def set_status_text(self, text):
        self.status_text = text


def make_factory(order=None, fail=False):
    """Returns (factory, record) where `record` sees every stack built."""
    order = order if order is not None else []
    record = {"built": [], "order": order, "channels": [], "built_with": [],
              "viewports": [], "tints": []}

    def factory(path, channel, parent_widget=None, *, method=None, params=(),
                initial_viewport_l0=None, tint=None, nucleus_channel=None,
                nucleus_tint=None, nucleus_enabled=False):
        record["channels"].append(channel)
        record["parent"] = parent_widget
        # Where the stack was asked to OPEN. None = whole slide.
        record["viewports"].append(initial_viewport_l0)
        record["tints"].append(tint)
        # The selection the stack is asked to come up WITH -- the drill-down
        # must not build Original and switch afterwards.
        record["built_with"].append((channel, method, tuple(params)))
        provider = FakeProvider(path)
        if fail:
            # A factory that fails AFTER opening the provider is the case
            # that matters: the partial resources must not leak.
            provider.close()
            raise RuntimeError("boom: could not read pyramid")
        scheduler = FakeScheduler(order)
        controller = FakeController(provider, scheduler, order,
                                    channel=channel, method=method,
                                    params=tuple(params))
        # Parented to the tab, exactly as the real `ExploreView(parent)` is:
        # the bug this models is that a parented widget still has to be put
        # into the layout, or it gets no geometry and shows nothing. It also
        # carries the badge and camera surfaces a release freezes.
        view = FakeView(parent_widget, provider)
        caches = (FakeCache("raw"), FakeCache("corrected"))
        # A scheduler holds the same cache objects, exactly as the real one
        # does -- so a test that only checked `stack.caches is None` would
        # prove nothing about the 2GB actually being released.
        scheduler.caches = caches
        stack = ExploreStack(provider, scheduler, controller, view, caches)
        record["built"].append(stack)
        return stack

    return factory, record


class FakePage:
    def __init__(self, channel=None):
        self.current_channel = channel


def test_placeholder_until_a_dataset_is_loaded(app):
    factory, record = make_factory()
    tab = Step0ExploreTab(FakePage(), stack_factory=factory)

    # Activating with no dataset must not build anything.
    tab.activate()
    assert tab.stack is None
    assert record["built"] == []
    assert tab.build_attempts == 0
    tab.teardown()


def test_stack_is_built_lazily_exactly_once(app):
    factory, record = make_factory()
    tab = Step0ExploreTab(FakePage("CD3"), stack_factory=factory)
    tab.set_dataset("/data/slide_a.ome.tif")

    assert tab.stack is None, "binding a dataset must not build the stack"

    tab.activate()
    assert tab.stack is not None
    assert tab.build_attempts == 1

    # Re-entering the tab several times must not build a second provider or
    # a second scheduler -- 8 I/O + 4 compute workers per build.
    for _ in range(5):
        tab.activate()
    assert tab.build_attempts == 1
    assert len(record["built"]) == 1
    tab.teardown()


def test_the_stack_is_built_with_the_pages_channel_at_that_moment(app):
    factory, record = make_factory()
    page = FakePage("CD8")
    tab = Step0ExploreTab(page, stack_factory=factory)
    tab.set_dataset("/data/slide_a.ome.tif")
    tab.activate()

    assert record["channels"] == ["CD8"]
    assert tab.stack.controller.channel == "CD8"
    # The build is the switch: nothing extra is asked of the controller.
    assert tab.stack.controller.selection_calls == []
    tab.teardown()


def test_re_activation_follows_the_pages_channel_once(app):
    """The user picks channels on the Background Correction tab and comes
    back: Explore follows the FINAL choice, with one switch, not one per
    intermediate selection."""
    factory, _record = make_factory()
    page = FakePage("CD8")
    tab = Step0ExploreTab(page, stack_factory=factory)
    tab.set_dataset("/data/slide_a.ome.tif")
    tab.activate()
    controller = tab.stack.controller

    # Away from the tab, the page's selection moves several times.
    for channel in ("DAPI", "CD3", "Ki67"):
        page.current_channel = channel
    assert controller.selection_calls == [], (
        "the tab must not follow while it is not being entered")

    tab.activate()

    assert controller.selection_calls == [{"channel": "Ki67"}]
    assert controller.channel == "Ki67"
    assert tab.build_attempts == 1, "following a channel must not rebuild"
    tab.teardown()


def test_re_activation_on_the_same_channel_switches_nothing(app):
    """`set_selection` is not free even for the channel already shown: it
    cancels the directional prefetch and re-enters the provisional state.
    Re-entering the tab repeatedly must therefore cost nothing."""
    factory, _record = make_factory()
    page = FakePage("CD8")
    tab = Step0ExploreTab(page, stack_factory=factory)
    tab.set_dataset("/data/slide_a.ome.tif")
    tab.activate()
    controller = tab.stack.controller

    for _ in range(5):
        tab.activate()

    assert controller.selection_calls == []
    assert controller.channel == "CD8"
    tab.teardown()


@pytest.mark.parametrize("empty", [None, ""])
def test_an_empty_page_selection_leaves_the_view_alone(app, empty):
    """The page holds None between datasets and while nothing is selected.
    Following that would switch the view to nothing."""
    factory, _record = make_factory()
    page = FakePage("CD8")
    tab = Step0ExploreTab(page, stack_factory=factory)
    tab.set_dataset("/data/slide_a.ome.tif")
    tab.activate()
    controller = tab.stack.controller

    page.current_channel = empty
    tab.activate()

    assert controller.selection_calls == []
    assert controller.channel == "CD8"
    tab.teardown()


def test_after_a_dataset_switch_the_new_stack_uses_the_new_channel(app):
    factory, record = make_factory()
    page = FakePage("CD8")
    tab = Step0ExploreTab(page, stack_factory=factory)
    tab.set_dataset("/data/slide_a.ome.tif")
    tab.activate()
    first = tab.stack

    # A dataset load resets the page's selection and then picks a channel
    # of the NEW dataset; the old stack is gone before either happens.
    page.current_channel = None
    tab.set_dataset("/data/slide_b.ome.tif")
    assert tab.stack is None and first.provider.closed is True
    page.current_channel = "CD20"

    tab.activate()

    assert record["channels"] == ["CD8", "CD20"]
    assert tab.stack.controller.channel == "CD20"
    assert tab.stack.controller.selection_calls == []
    tab.teardown()


def test_dataset_switch_discards_the_previous_stack_before_binding(app):
    order = []
    factory, record = make_factory(order)
    tab = Step0ExploreTab(FakePage("CD3"), stack_factory=factory)
    tab.set_dataset("/data/slide_a.ome.tif")
    tab.activate()
    first = tab.stack
    assert first.provider.read() == "pixels of /data/slide_a.ome.tif"

    tab.set_dataset("/data/slide_b.ome.tif")

    # The old stack is gone -- not merely hidden -- before anything is bound
    # to the new dataset, and its provider is closed, so not one pixel and
    # not one source identity of slide A can survive.
    assert tab.stack is None
    assert first.provider.closed is True
    with pytest.raises(RuntimeError):
        first.provider.read()
    assert first.provider.source_identity() != ("raw", "/data/slide_b.ome.tif")

    tab.activate()
    assert tab.stack is not None and tab.stack is not first
    assert tab.stack.provider.source_identity() == (
        "raw", "/data/slide_b.ome.tif")
    tab.teardown()


def test_teardown_order_is_scheduler_then_provider_then_caches(app):
    order = []
    factory, _record = make_factory(order)
    tab = Step0ExploreTab(FakePage("CD3"), stack_factory=factory)
    tab.set_dataset("/data/slide_a.ome.tif")
    tab.activate()
    stack = tab.stack
    scheduler_caches = stack.scheduler.caches

    tab.teardown()

    assert order == ["scheduler.shutdown", "provider.close"], (
        "workers must be joined before the handles they read through are "
        f"closed; got {order}")
    assert stack.caches is None, "caches are dropped last, after teardown"
    for cache in scheduler_caches:
        assert cache.stats() == {"items": 0, "bytes": 0}, (
            f"{cache.name} cache still holds pixels after teardown")
    assert stack.provider.closed is True
    assert stack.scheduler.workers_running is False


def test_teardown_is_idempotent(app):
    order = []
    factory, _record = make_factory(order)
    tab = Step0ExploreTab(FakePage("CD3"), stack_factory=factory)
    tab.set_dataset("/data/slide_a.ome.tif")
    tab.activate()
    stack = tab.stack

    tab.teardown()
    tab.teardown()
    tab.teardown()

    assert order == ["scheduler.shutdown", "provider.close"]
    assert stack.controller.teardown_calls == 1
    assert tab.stack is None


def test_a_failed_build_shows_an_error_and_leaves_nothing_running(app):
    factory, record = make_factory(fail=True)
    tab = Step0ExploreTab(FakePage("CD3"), stack_factory=factory)
    tab.set_dataset("/data/broken.ome.tif")

    tab.activate()          # must not raise into the GUI

    assert tab.stack is None
    assert record["built"] == []
    assert tab.build_attempts == 1

    # And it is not retried on every click of the tab, which would spawn a
    # worker pool per click.
    tab.activate()
    tab.activate()
    assert tab.build_attempts == 1

    # A reload clears the error state.
    tab.set_dataset("/data/broken.ome.tif")
    tab.activate()
    assert tab.build_attempts == 2
    tab.teardown()


def test_p0_touches_no_save_config_or_correction_state(app):
    """P0 is display-only: the page object it is handed must come back
    untouched apart from what it was already carrying."""
    factory, _record = make_factory()
    page = FakePage("CD3")
    before = dict(page.__dict__)

    tab = Step0ExploreTab(page, stack_factory=factory)
    tab.set_dataset("/data/slide_a.ome.tif")
    tab.activate()
    tab.teardown()

    assert page.__dict__ == before


def test_partial_build_failure_after_the_controller_exists_is_unwound(app):
    """`load_overview` and the initial `setRange` run AFTER the controller
    is constructed, so the failure path must unwind a CONTROLLER -- which
    also stops timers, disconnects signals, joins the floor threads and
    shuts the overview pool down -- not just the two backend objects."""
    from PyQt5 import QtWidgets

    order = []
    provider = FakeProvider("/data/slide_a.ome.tif")
    scheduler = FakeScheduler(order)
    controller = FakeController(provider, scheduler, order)
    view = QtWidgets.QLabel("half-built view")
    holder = QtWidgets.QWidget()
    view.setParent(holder)

    _cleanup_partial_stack(controller, scheduler, provider, view)

    assert controller.teardown_calls == 1, (
        "the controller must own the unwind, or its timers/threads survive")
    assert order == ["scheduler.shutdown", "provider.close"]
    assert provider.closed is True
    assert view.parent() is None, "a half-built view must not stay mounted"


def test_partial_build_failure_before_the_controller_exists_is_unwound(app):
    order = []
    provider = FakeProvider("/data/slide_a.ome.tif")
    scheduler = FakeScheduler(order)

    _cleanup_partial_stack(None, scheduler, provider, None)

    assert order == ["scheduler.shutdown"]
    assert provider.closed is True


def test_real_factory_unwinds_when_load_overview_raises(app, tmp_path):
    """The same failure, through the REAL factory: a real provider is
    opened, a real scheduler starts its 8+4 worker threads, and then the
    build fails. Nothing may be left running or open.

    A fake factory that closes its own provider cannot prove this.
    """
    import threading

    tifffile = pytest.importorskip("tifffile")
    from block01.viewer.explore_view import ExploreController

    path = str(tmp_path / "small.ome.tif")
    data = np.zeros((2, 256, 256), dtype=np.uint16)
    data[0] = np.arange(256, dtype=np.uint16).reshape(-1, 1)
    tifffile.imwrite(path, data, ome=True,
                     metadata={"Channel": {"Name": ["ch0", "ch1"]}})

    captured = {}
    real_load = ExploreController.load_overview

    def exploding_load(self, *a, **k):
        captured["controller"] = self
        raise RuntimeError("boom: overview level unreadable")

    threads_before = threading.active_count()
    ExploreController.load_overview = exploding_load
    try:
        with pytest.raises(RuntimeError, match="boom"):
            build_default_stack(path, "ch0", None)
    finally:
        ExploreController.load_overview = real_load

    ctrl = captured["controller"]
    assert ctrl._torn_down is True, "the controller was not torn down"
    assert ctrl._teardown_order == ["scheduler.shutdown", "provider.close"]
    assert ctrl.provider._closed is True
    with pytest.raises(RuntimeError):
        ctrl.provider.read_region(0, 0, 0, 64, 0, 64)

    deadline = time.time() + 5.0
    while threading.active_count() > threads_before and time.time() < deadline:
        time.sleep(0.02)
    assert threading.active_count() <= threads_before, (
        "a failed build left worker threads running")


def test_page_destruction_tears_the_stack_down_exactly_once(app):
    """Destroying the Step0 page must release the stack deterministically,
    not leave 12 worker threads and two open handles to Python GC."""
    from PyQt5 import QtCore, QtWidgets

    order = []
    factory, _record = make_factory(order)

    page = QtWidgets.QWidget()          # stands in for Step0Page
    tab = Step0ExploreTab(page, stack_factory=factory, parent=page)
    tab.set_dataset("/data/slide_a.ome.tif")
    tab.activate()
    stack = tab.stack
    assert stack is not None

    page.deleteLater()
    # A deferred delete is only acted on by the event loop that owns it, so
    # a bare processEvents() here would silently destroy nothing and the
    # test would pass for the wrong reason.
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.DeferredDelete)
    QtWidgets.QApplication.processEvents()

    assert order == ["scheduler.shutdown", "provider.close"]
    assert stack.provider.closed is True
    assert stack.controller.teardown_calls == 1, "torn down more than once"
    assert stack.caches is None


def test_page_teardown_entry_point_calls_the_tab(app):
    """`Step0Page.teardown()` is the deterministic path a host should use."""
    import inspect

    from block01.ui.step0 import step0_page

    src = inspect.getsource(step0_page.Step0Page.teardown)
    assert "_explore_tab" in src and "teardown()" in src


def test_the_view_is_actually_laid_out_and_visible_after_activation(app):
    """Regression: the view was created with the tab as its Qt PARENT, and
    `_show_widget` tested parenthood before calling `addWidget` -- so the
    view was never added to the layout, got no geometry, and the tab came up
    BLANK while the stack behind it was healthy. A manual test saw nothing
    for two minutes; every existing test still passed, because none of them
    looked at the widget.
    """
    from PyQt5 import QtWidgets

    factory, record = make_factory()
    tab = Step0ExploreTab(FakePage("CD3"), stack_factory=factory)
    tab.resize(640, 480)
    tab.show()
    tab.set_dataset("/data/slide_a.ome.tif")
    tab.activate()
    QtWidgets.QApplication.processEvents()

    view = tab.stack.view
    assert record["parent"] is tab, "test setup: the view must be parented"
    assert tab._layout.indexOf(view) >= 0, (
        "the view is not in the tab's layout, so it can never be shown")
    assert view.isVisible() is True
    assert view.width() > 0 and view.height() > 0, (
        f"the view has no geometry: {view.size()}")
    assert tab._placeholder.isVisible() is False

    # And the placeholder comes back, laid out, once the dataset goes away.
    tab.set_dataset(None)
    QtWidgets.QApplication.processEvents()
    assert tab._placeholder.isVisible() is True
    assert tab._placeholder.width() > 0
    tab.teardown()


# ── show_source: the drill-down entry point ──────────────────────────────────
#
# Each compare panel's "full image" button names one result. These pin the two
# rules that keep it from doing work twice: build WITH the selection rather
# than build-then-switch, and compare the whole triple before switching.

def test_show_source_builds_with_the_requested_selection(app):
    """Never Original-then-switch: building with the selection means the
    first viewport request already carries the right method/params, and no
    extra `set_selection` (with its provisional/generation cancel cycle)
    happens."""
    factory, record = make_factory()
    tab = Step0ExploreTab(FakePage("CD8"), stack_factory=factory)
    tab.set_dataset("/data/slide_a.ome.tif")

    assert tab.show_source("CD8", "tophat", (15,)) is True

    assert record["built_with"] == [("CD8", "tophat", (15,))]
    assert tab.build_attempts == 1
    controller = tab.stack.controller
    assert (controller.channel, controller.method, controller.params) == (
        "CD8", "tophat", (15,))
    assert controller.selection_calls == [], (
        "the build already carried the selection; nothing to switch")
    tab.teardown()


def test_show_source_original_asks_for_no_method(app):
    factory, record = make_factory()
    tab = Step0ExploreTab(FakePage("CD8"), stack_factory=factory)
    tab.set_dataset("/data/slide_a.ome.tif")

    tab.show_source("CD8", None, ())

    assert record["built_with"] == [("CD8", None, ())]
    assert tab.stack.controller.method is None
    tab.teardown()


@pytest.mark.parametrize(
    ("second", "expect_switch"),
    [(("CD8", "cucim", (50,)), True),      # different method
     (("CD8", "tophat", (25,)), True),     # same method, different param
     (("CD3", "tophat", (15,)), True),     # different channel
     (("CD8", None, ()), True),            # back to Original
     (("CD8", "tophat", (15,)), False)],   # identical triple
)
def test_show_source_switches_only_on_a_real_difference(app, second,
                                                        expect_switch):
    """`set_selection` is not free even for an identical selection: it
    cancels the directional prefetch and re-enters the provisional state.
    So the WHOLE triple is compared, and every part of it counts."""
    factory, _record = make_factory()
    tab = Step0ExploreTab(FakePage("CD8"), stack_factory=factory)
    tab.set_dataset("/data/slide_a.ome.tif")
    tab.show_source("CD8", "tophat", (15,))
    controller = tab.stack.controller

    assert tab.show_source(*second) is True

    if expect_switch:
        assert controller.selection_calls == [
            {"channel": second[0], "method": second[1], "params": second[2]}]
        assert (controller.channel, controller.method,
                controller.params) == second
    else:
        assert controller.selection_calls == []
    assert tab.build_attempts == 1, "switching a source must not rebuild"
    tab.teardown()


def test_show_source_repeated_identical_calls_do_nothing(app):
    factory, _record = make_factory()
    tab = Step0ExploreTab(FakePage("CD8"), stack_factory=factory)
    tab.set_dataset("/data/slide_a.ome.tif")
    tab.show_source("CD8", "cucim", (50,))
    controller = tab.stack.controller

    for _ in range(5):
        assert tab.show_source("CD8", "cucim", (50,)) is True

    assert controller.selection_calls == []
    assert tab.build_attempts == 1
    tab.teardown()


def test_show_source_without_a_dataset_does_nothing(app):
    factory, record = make_factory()
    tab = Step0ExploreTab(FakePage("CD8"), stack_factory=factory)

    assert tab.show_source("CD8", "tophat", (15,)) is False

    assert tab.stack is None
    assert record["built_with"] == []
    assert tab.build_attempts == 0
    tab.teardown()


def test_show_source_after_a_failed_build_stays_failed(app):
    """A failed build is not retried on every button press -- that would
    spawn a worker pool per click."""
    factory, record = make_factory(fail=True)
    tab = Step0ExploreTab(FakePage("CD8"), stack_factory=factory)
    tab.set_dataset("/data/broken.ome.tif")

    assert tab.show_source("CD8", "tophat", (15,)) is False
    assert tab.build_attempts == 1

    assert tab.show_source("CD8", "cucim", (50,)) is False
    assert tab.build_attempts == 1
    assert record["built"] == []
    tab.teardown()


# ── the real factory's orchestration ─────────────────────────────────────────
#
# Behaviour, not source shape: the viewer classes `build_default_stack`
# reaches for are replaced with recorders, and the ORDER and ARGUMENTS of
# what it does to them are asserted. No real slide, no Qt view.

# ONE timeline shared by the controller and the view. Separate per-object
# lists could only show the order WITHIN each object -- they could never
# show that `setRange` came after `load_overview`, which is half of what
# this is meant to pin.
_TIMELINE = []


class _RecordingController:
    instances = []

    def __init__(self, provider, scheduler, compute, grid, view, channel):
        self.channel = channel
        self.method = None
        self.params = ()
        _RecordingController.instances.append(self)

    def set_selection(self, channel=_UNSET, method=_UNSET, params=_UNSET):
        record = {}
        if channel is not _UNSET:
            record["channel"] = channel
            self.channel = channel
        if method is not _UNSET:
            record["method"] = method
            self.method = method
        if params is not _UNSET:
            record["params"] = tuple(params)
            self.params = tuple(params)
        _TIMELINE.append(("set_selection", record))

    def load_overview(self, ensure_floor=True):
        _TIMELINE.append(("load_overview", {"ensure_floor": ensure_floor}))

    def teardown(self, *, wait_for_floor=False):
        _TIMELINE.append(("teardown", {"wait_for_floor": wait_for_floor}))


class _RecordingViewBox:
    def setRange(self, **kwargs):
        _TIMELINE.append(("setRange", sorted(kwargs)))


class _RecordingView:
    def __init__(self, parent=None):
        self.view_box = _RecordingViewBox()

    def setParent(self, _p):
        pass

    def deleteLater(self):
        pass


class _StubProvider:
    channel_names = ["CD8", "CD3"]

    def __init__(self, path):
        self.path = path

    def level_shape(self, _level):
        return (4096, 4096)

    def close(self):
        pass


def _patch_real_factory_dependencies(monkeypatch):
    """Swap the viewer classes the factory imports for recorders.

    The factory imports them INSIDE the function, so they resolve from the
    module at call time and patching the module attribute is enough.
    """
    from block01.viewer import caches, correction_compute, explore_view
    from block01.viewer import raw_tile_provider, scheduler

    _RecordingController.instances = []
    del _TIMELINE[:]
    monkeypatch.setattr(raw_tile_provider, "RawTileProvider", _StubProvider)
    monkeypatch.setattr(caches, "LRUByteCache", lambda _n: object())
    monkeypatch.setattr(correction_compute, "CorrectionCompute",
                        lambda *a, **k: object())
    monkeypatch.setattr(scheduler, "TileScheduler", lambda *a, **k: object())
    monkeypatch.setattr(explore_view, "ExploreView", _RecordingView)
    monkeypatch.setattr(explore_view, "ExploreController",
                        _RecordingController)


def test_real_factory_orchestration_for_a_corrected_method(app, monkeypatch):
    """Selection FIRST (so the overview install is the thing that starts the
    floor, once, against the right method), then the overview with
    `ensure_floor=True`, then the opening range."""
    from block01.ui.step0.step0_explore_tab import build_default_stack

    _patch_real_factory_dependencies(monkeypatch)
    build_default_stack("/data/slide.ome.tif", "CD8",
                        method="tophat", params=(15,))

    assert [name for name, _payload in _TIMELINE] == [
        "set_selection", "load_overview", "setRange"]
    assert _TIMELINE[0][1] == {"method": "tophat", "params": (15,)}
    assert _TIMELINE[1][1] == {"ensure_floor": True}
    assert _TIMELINE[2][1] == ["padding", "xRange", "yRange"]


def test_real_factory_orchestration_for_original(app, monkeypatch):
    """Original selects nothing at all and asks for no floor."""
    from block01.ui.step0.step0_explore_tab import build_default_stack

    _patch_real_factory_dependencies(monkeypatch)
    build_default_stack("/data/slide.ome.tif", "CD8")

    assert [name for name, _p in _TIMELINE] == ["load_overview", "setRange"]
    assert _TIMELINE[0][1] == {"ensure_floor": False}
    controller = _RecordingController.instances[-1]
    assert controller.method is None and controller.params == ()


def test_real_factory_ignores_params_given_without_a_method(app, monkeypatch):
    """`method=None` means `params=()`; a stray parameter must not leave the
    controller's state disagreeing with what the caller asked for."""
    from block01.ui.step0.step0_explore_tab import build_default_stack

    _patch_real_factory_dependencies(monkeypatch)
    build_default_stack("/data/slide.ome.tif", "CD8", method=None,
                        params=(15,))

    assert [name for name, _p in _TIMELINE] == ["load_overview", "setRange"]
    controller = _RecordingController.instances[-1]
    assert controller.params == ()


def test_a_channel_change_keeps_the_current_method_and_params(app):
    """`activate`'s channel pull must not disturb the correction.

    The real `set_selection` leaves what it is not given alone, so passing
    only `channel=` keeps the method and its parameter. That is exactly
    what a user expects: they were looking at Top-hat, they pick another
    channel on the correction tab, they come back to Top-hat of the new
    channel -- not to Original.
    """
    factory, _record = make_factory()
    page = FakePage("CD8")
    tab = Step0ExploreTab(page, stack_factory=factory)
    tab.set_dataset("/data/slide_a.ome.tif")
    tab.show_source("CD8", "tophat", (15,))
    controller = tab.stack.controller

    page.current_channel = "CD3"
    tab.activate()

    assert controller.channel == "CD3"
    assert controller.method == "tophat", (
        "the channel pull cleared the correction method")
    assert controller.params == (15,)
    assert controller.selection_calls == [{"channel": "CD3"}], (
        "the pull must pass only the channel")
    tab.teardown()


@pytest.mark.parametrize("stray", [(15,), (0,), (99, 1)])
def test_show_source_normalises_original_params(app, stray):
    """Original ignores parameters, so accepting them as part of the
    requested triple would let `show_source` report success for a state the
    controller does not hold -- and make an identical request look
    different next time."""
    factory, record = make_factory()
    tab = Step0ExploreTab(FakePage("CD8"), stack_factory=factory)
    tab.set_dataset("/data/slide_a.ome.tif")

    assert tab.show_source("CD8", None, stray) is True

    assert record["built_with"] == [("CD8", None, ())]
    controller = tab.stack.controller
    assert controller.params == ()

    # And the normalisation makes the comparison stable: a second Original
    # request with different stray params is still the same request.
    assert tab.show_source("CD8", None, (1234,)) is True
    assert controller.selection_calls == []
    tab.teardown()


# ── the production-correction gate ───────────────────────────────────────────
#
# Explore and Step0's correction workers both drive the GPU, and cupy is not
# safe to use from two places at once. The gate is two-directional: a
# production run releases Explore before starting, and Explore refuses to
# build while one is running.

def test_a_busy_probe_blocks_the_build_and_says_why(app):
    factory, record = make_factory()
    tab = Step0ExploreTab(FakePage("CD8"), stack_factory=factory,
                          busy_probe=lambda: "whole-slide correction (Save)")
    tab.resize(640, 480)
    tab.show()
    tab.set_dataset("/data/slide_a.ome.tif")

    tab.activate()

    assert tab.stack is None
    assert record["built"] == []
    assert tab.build_attempts == 0, (
        "a refused build must not count as an attempt -- it is retryable")
    assert tab._placeholder.isVisible()
    text = tab._placeholder.text()
    assert "whole-slide correction (Save)" in text and "GPU" in text
    tab.teardown()


def test_show_source_is_gated_too_and_recovers_when_free(app):
    """The drill-down buttons go through the same gate, and it is not
    sticky: once the run finishes, the next attempt builds."""
    factory, _record = make_factory()
    busy = {"reason": "patch background correction"}
    tab = Step0ExploreTab(FakePage("CD8"), stack_factory=factory,
                          busy_probe=lambda: busy["reason"])
    tab.set_dataset("/data/slide_a.ome.tif")

    assert tab.show_source("CD8", "tophat", (15,)) is False
    assert tab.stack is None and tab.build_attempts == 0

    busy["reason"] = None
    assert tab.show_source("CD8", "tophat", (15,)) is True
    assert tab.stack is not None
    assert tab.stack.controller.method == "tophat"
    tab.teardown()


def test_no_probe_means_never_busy(app):
    """Every existing caller passes no probe and must be unaffected."""
    factory, _record = make_factory()
    tab = Step0ExploreTab(FakePage("CD8"), stack_factory=factory)
    tab.set_dataset("/data/slide_a.ome.tif")

    tab.activate()

    assert tab.stack is not None
    tab.teardown()


def test_release_for_production_waits_for_the_floor_and_says_why(app):
    factory, _record = make_factory()
    tab = Step0ExploreTab(FakePage("CD8"), stack_factory=factory)
    tab.resize(640, 480)
    tab.show()
    tab.set_dataset("/data/slide_a.ome.tif")
    tab.activate()
    stack = tab.stack

    tab.release_for_production("patch background correction")

    assert tab.stack is None
    assert stack.provider.closed is True
    assert stack.controller.teardown_waits == [True], (
        "the release must be the hand-off form, or a floor job keeps the GPU")
    # The BACKEND went away; the picture did not. The user pressing Save
    # should not have to watch their image vanish, and the pixels are
    # already-blitted uint8 -- they need neither GPU nor provider.
    assert tab.frozen_view is stack.view
    assert tab._layout.indexOf(stack.view) >= 0
    assert stack.view.isVisible() is True
    assert tab._placeholder.isVisible() is False
    # Frozen, and it says so: nothing can fetch a newly exposed tile any
    # more, so the camera is stopped rather than left to pan into blank.
    assert stack.view.status_text is not None
    assert "patch background correction" in stack.view.status_text
    assert "frozen" in stack.view.status_text.lower()
    assert stack.view.mouse_enabled == (False, False)


def test_release_is_idempotent_and_a_no_op_without_a_stack(app):
    factory, _record = make_factory()
    tab = Step0ExploreTab(FakePage("CD8"), stack_factory=factory)
    tab.set_dataset("/data/slide_a.ome.tif")

    # No stack yet: the release must not overwrite the "load a dataset"
    # style placeholder with a "released" message for something that was
    # never there.
    before = tab._placeholder.text()
    tab.release_for_production("patch background correction")
    assert tab._placeholder.text() == before

    tab.activate()
    tab.release_for_production("patch background correction")
    released = tab._placeholder.text()
    tab.release_for_production("patch background correction")
    assert tab._placeholder.text() == released
    assert tab.stack is None


def test_a_released_stack_can_be_rebuilt(app):
    """Releasing is recoverable -- that is what distinguishes it from a
    teardown. The rebuild uses whatever is current at that point."""
    factory, record = make_factory()
    page = FakePage("CD8")
    tab = Step0ExploreTab(page, stack_factory=factory)
    tab.set_dataset("/data/slide_a.ome.tif")
    tab.show_source("CD8", "tophat", (15,))
    tab.release_for_production("whole-slide correction (Save)")

    page.current_channel = "CD3"
    assert tab.show_source("CD3", "cucim", (50,)) is True

    assert record["built_with"] == [("CD8", "tophat", (15,)),
                                    ("CD3", "cucim", (50,))]
    assert tab.build_attempts == 2
    tab.teardown()


# ── placeholder states ───────────────────────────────────────────────────────
#
# The full image is not on screen for four different reasons, and each has
# to say something true. They used to share one constant, so a dataset
# that WAS loaded was still told to load one -- and that constant still
# called itself "Explore (v15 trial)" and pointed at a tab that no longer
# exists.

def test_no_dataset_says_load_one(app):
    factory, _record = make_factory()
    tab = Step0ExploreTab(FakePage(), stack_factory=factory)

    text = tab._placeholder.text()

    assert "Load an OME-TIFF" in text
    tab.teardown()


def test_a_bound_dataset_is_not_told_to_load_one(app):
    """The state that was wrong: a dataset IS loaded, the view just has not
    been opened, and the placeholder has to say how to open it."""
    factory, _record = make_factory()
    tab = Step0ExploreTab(FakePage("CD8"), stack_factory=factory)

    tab.set_dataset("/data/slide_a.ome.tif")
    text = tab._placeholder.text()

    assert "Load an OME-TIFF" not in text
    assert "⤢" in text                       # how it is actually opened
    assert tab.stack is None                 # still not built -- just a message
    tab.teardown()


def test_the_load_prompt_comes_back_when_the_dataset_goes_away(app):
    factory, _record = make_factory()
    tab = Step0ExploreTab(FakePage("CD8"), stack_factory=factory)
    tab.set_dataset("/data/slide_a.ome.tif")

    tab.set_dataset(None)
    text = tab._placeholder.text()

    assert "Load an OME-TIFF" in text
    assert "⤢" not in text
    tab.teardown()


def test_a_failed_build_names_the_error_and_does_not_say_load(app):
    factory, _record = make_factory(fail=True)
    tab = Step0ExploreTab(FakePage("CD8"), stack_factory=factory)
    tab.set_dataset("/data/slide_a.ome.tif")

    tab.activate()
    text = tab._placeholder.text()

    assert "boom: could not read pyramid" in text
    assert "Load an OME-TIFF" not in text
    # It must also not blame the part of the workspace that still works.
    assert "compare panels are unaffected" in text
    tab.teardown()


def test_no_placeholder_still_calls_this_a_trial_tab(app):
    """The view lives inside Background Correction now. No text may promise a
    tab, and none may carry the v15 trial label."""
    texts = [_et.PLACEHOLDER_NO_DATASET, _et.PLACEHOLDER_NOT_OPEN,
             _et.PLACEHOLDER_BUILD_FAILED, _et.PLACEHOLDER_BUSY,
             ]
    for text in texts:
        assert "trial" not in text.lower(), text
        # Whole word: "stable"/"table" are not claims about a tab.
        assert not re.search(r"\btabs?\b", text, re.I), text


# ── a release keeps the picture ──────────────────────────────────────────────
#
# Pressing Save used to blank the full image. The GPU has to go, but the
# pixels do not: they are already-blitted uint8 arrays in ImageItems and
# need neither the provider nor the scheduler.

def test_a_release_shuts_the_backend_down_but_keeps_the_frame(app):
    factory, _record = make_factory()
    tab = Step0ExploreTab(FakePage("CD8"), stack_factory=factory)
    tab.resize(640, 480)
    tab.show()
    tab.set_dataset("/data/slide_a.ome.tif")
    tab.activate()
    stack = tab.stack

    tab.release_for_production("whole-slide correction (Save)")

    # Backend: gone, and in the hand-off form.
    assert stack.provider.closed is True
    assert stack.scheduler.workers_running is False
    assert stack.caches is None
    assert stack.controller.teardown_waits == [True]
    # Picture: still there.
    assert tab.stack is None, "there is no live stack -- only a frame"
    assert tab.frozen_view is stack.view
    assert tab._layout.indexOf(stack.view) >= 0
    assert stack.view.isVisible() is True
    tab.teardown()


def test_a_frozen_frame_cannot_be_panned(app):
    """With no scheduler nothing can fetch a newly exposed tile, so the
    camera is stopped instead of letting the user pan into blank space."""
    factory, _record = make_factory()
    tab = Step0ExploreTab(FakePage("CD8"), stack_factory=factory)
    tab.set_dataset("/data/slide_a.ome.tif")
    tab.activate()
    view = tab.stack.view
    assert view.mouse_enabled == (True, True)

    tab.release_for_production("patch background correction")

    assert view.mouse_enabled == (False, False)
    tab.teardown()


def test_a_rebuild_destroys_the_frozen_frame(app):
    """Two views in the layout would leave the old pixels painted over the
    new stack -- and the frozen one holds a pool of tile arrays."""
    factory, record = make_factory()
    tab = Step0ExploreTab(FakePage("CD8"), stack_factory=factory)
    tab.resize(640, 480)
    tab.show()
    tab.set_dataset("/data/slide_a.ome.tif")
    tab.activate()
    old_view = tab.stack.view
    tab.release_for_production("patch background correction")
    assert tab.frozen_view is old_view

    tab.activate()                      # rebuild

    assert tab.stack is not None
    assert tab.stack.view is not old_view
    assert tab.frozen_view is None
    assert tab._layout.indexOf(old_view) < 0
    assert len(record["built"]) == 2
    tab.teardown()


def test_a_dataset_change_destroys_the_frozen_frame(app):
    factory, _record = make_factory()
    tab = Step0ExploreTab(FakePage("CD8"), stack_factory=factory)
    tab.set_dataset("/data/slide_a.ome.tif")
    tab.activate()
    old_view = tab.stack.view
    tab.release_for_production("patch background correction")

    tab.set_dataset("/data/slide_b.ome.tif")

    assert tab.frozen_view is None
    assert tab._layout.indexOf(old_view) < 0
    tab.teardown()


def test_a_teardown_destroys_the_frozen_frame(app):
    factory, _record = make_factory()
    tab = Step0ExploreTab(FakePage("CD8"), stack_factory=factory)
    tab.set_dataset("/data/slide_a.ome.tif")
    tab.activate()
    old_view = tab.stack.view
    tab.release_for_production("patch background correction")

    tab.teardown()

    assert tab.frozen_view is None
    assert tab._layout.indexOf(old_view) < 0


def test_releasing_twice_keeps_one_frame(app):
    """Two production runs in a row: the second release has no stack, so it
    must leave the frame from the first alone rather than dropping it."""
    factory, _record = make_factory()
    tab = Step0ExploreTab(FakePage("CD8"), stack_factory=factory)
    tab.set_dataset("/data/slide_a.ome.tif")
    tab.activate()
    view = tab.stack.view

    tab.release_for_production("patch background correction")
    tab.release_for_production("on-demand background correction")

    assert tab.frozen_view is view
    assert tab._layout.indexOf(view) >= 0
    tab.teardown()
