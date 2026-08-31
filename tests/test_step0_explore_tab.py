"""P0 mount tests for the Step0 Explore tab (docs/v15_step0_mount_plan.md §7).

Everything here runs against a FAKE stack: P0 is about lifecycle -- lazy
build, dataset switching, teardown order and idempotence -- not about
pixels, and a unit test must not need a real 31k x 29k slide to check that
a provider gets closed.
"""

import os

import pytest

pytest.importorskip("PyQt5")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from block01.ui.step0.step0_explore_tab import (  # noqa: E402
    ExploreStack, Step0ExploreTab,
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
        provider = FakeProvider(path)
        if fail:
            # A factory that fails AFTER opening the provider is the case
            # that matters: the partial resources must not leak.
            provider.close()
            raise RuntimeError("boom: could not read pyramid")
        scheduler = FakeScheduler(order)
        controller = FakeController(provider, scheduler, order)
        from PyQt5 import QtWidgets
        view = QtWidgets.QLabel("fake explore view")
        stack = ExploreStack(provider, scheduler, controller, view,
                             ("raw_cache", "corrected_cache"))
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

    tab.teardown()

    assert order == ["scheduler.shutdown", "provider.close"], (
        "workers must be joined before the handles they read through are "
        f"closed; got {order}")
    assert stack.caches is None, "caches are dropped last, after teardown"
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
