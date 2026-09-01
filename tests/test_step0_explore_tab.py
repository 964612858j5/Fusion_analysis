"""P0 mount tests for the Step0 Explore tab (docs/v15_step0_mount_plan.md §7).

Everything here runs against a FAKE stack: P0 is about lifecycle -- lazy
build, dataset switching, teardown order and idempotence -- not about
pixels, and a unit test must not need a real 31k x 29k slide to check that
a provider gets closed.
"""

import os
import time

import numpy as np
import pytest

pytest.importorskip("PyQt5")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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


class FakeController:
    """Mirrors `ExploreController.teardown`'s contract: scheduler first
    (which joins the workers), provider second."""

    def __init__(self, provider, scheduler, order):
        self.provider = provider
        self.scheduler = scheduler
        self._order = order
        self.teardown_calls = 0

    def teardown(self):
        self.teardown_calls += 1
        if self.provider.closed:
            return
        self.scheduler.shutdown()
        self._order.append("provider.close")
        self.provider.close()


class FakeView:
    """Stands in for the ExploreView QWidget."""

    def __init__(self, parent, provider):
        from PyQt5 import QtWidgets
        self._w = QtWidgets.QLabel("fake explore view", parent)
        self.provider = provider

    def __getattr__(self, name):
        return getattr(self._w, name)


def make_factory(order=None, fail=False):
    """Returns (factory, record) where `record` sees every stack built."""
    order = order if order is not None else []
    record = {"built": [], "order": order, "channels": []}

    def factory(path, channel, parent_widget=None):
        record["channels"].append(channel)
        record["parent"] = parent_widget
        provider = FakeProvider(path)
        if fail:
            # A factory that fails AFTER opening the provider is the case
            # that matters: the partial resources must not leak.
            provider.close()
            raise RuntimeError("boom: could not read pyramid")
        scheduler = FakeScheduler(order)
        controller = FakeController(provider, scheduler, order)
        from PyQt5 import QtWidgets
        # Parented to the tab, exactly as the real `ExploreView(parent)` is:
        # the bug this models is that a parented widget still has to be put
        # into the layout, or it gets no geometry and shows nothing.
        view = QtWidgets.QLabel("fake explore view", parent_widget)
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


def test_initial_channel_is_a_snapshot_of_the_pages_current_channel(app):
    factory, record = make_factory()
    page = FakePage("CD8")
    tab = Step0ExploreTab(page, stack_factory=factory)
    tab.set_dataset("/data/slide_a.ome.tif")
    tab.activate()

    assert record["channels"] == ["CD8"]

    # P0 has no two-way sync: a later page-side change does not reach the
    # stack, and must not silently rebuild it.
    page.current_channel = "DAPI"
    tab.activate()
    assert record["channels"] == ["CD8"]
    assert tab.build_attempts == 1
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


def test_the_real_factory_asks_for_no_correction_method(app):
    """P0 shows `Original`. The controller's `method` stays None, so no
    CorrectionKey is ever built and no slider value is read."""
    import inspect

    from block01.ui.step0 import step0_explore_tab as mod

    src = inspect.getsource(mod.build_default_stack)
    assert "set_selection" not in src, (
        "P0 must not select a correction method")
    assert "tophat" not in src and "cucim" not in src


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
