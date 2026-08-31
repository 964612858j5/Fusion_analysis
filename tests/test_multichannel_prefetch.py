import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtCore, QtTest  # noqa: E402

from viewer.multichannel_prefetch import (  # noqa: E402
    HOT_PRIORITY_BASE,
    SETTLE_CONFIRM_MS,
    MultiChannelPrefetchController,
)
from viewer.prefetch_policy import ChannelCorrectionSpec  # noqa: E402
from viewer.tile_types import (  # noqa: E402
    CorrectionKey,
    QualityLevel,
    SourceIdentity,
    TileAddress,
    TileGridSpec,
    effective_param,
)


class FakeProvider:
    def level_downsample(self, level):
        return {0: 1.0, 1: 4.0}[level]


class FakeController(QtCore.QObject):
    interaction_event = QtCore.pyqtSignal(str, object)
    gesture_quiet = QtCore.pyqtSignal(object)
    selection_context_changed = QtCore.pyqtSignal(object)
    overview_prepared = QtCore.pyqtSignal(object, str, int, bool)

    def __init__(self, channels):
        super().__init__()
        self.provider = FakeProvider()
        self.source = SourceIdentity("fake", "fingerprint", "raw")
        self.channels = tuple(channels)
        self.overviews = set()
        self.prepare_calls = []
        self.overview_inflight = []
        self.raw_pool = []
        self.precise_pool = []
        self.display_state = {"channel": channels[0], "changed": False}

    def snapshot(self):
        return SimpleNamespace(
            epoch=-1,
            source=self.source,
            channel=self.channels[0],
            method="tophat",
            params=(99,),
            level=1,
            quality=QualityLevel.INTERACTIVE,
            algorithm_version="algo",
            bbox_l0=None,
            visible_tiles=frozenset(),
            overview_ready=False,
            display_lo=None,
            display_hi=None,
        )

    # The host keeps overview records at ITS OWN overview level, which is
    # NOT the display level -- on the real slides the display is L0 while
    # the overview lives at L2. `level=None` means "the host's level", and
    # that is how a consumer must ask. Modelling both as the same level is
    # what let a bug through where HOT queried the display level: the
    # lookup never matched, the channel could never be ready, and
    # `prepare_overview_async` then returned without emitting for a record
    # that was already cached, wedging the one-at-a-time gate for good.
    OVERVIEW_LEVEL = 2

    def has_overview_record(self, channel, level=None, source=None):
        lvl = self.OVERVIEW_LEVEL if level is None else level
        return (source, channel, lvl) in self.overviews

    def prepare_overview_async(self, channel):
        # Matches the host: already-cached is a silent no-op, NO signal.
        if (self.source, channel, self.OVERVIEW_LEVEL) in self.overviews:
            return
        self.prepare_calls.append(channel)
        self.overview_inflight.append(channel)

    def finish_overview(self, channel, ok=True, level=None):
        lvl = self.OVERVIEW_LEVEL if level is None else level
        if channel in self.overview_inflight:
            self.overview_inflight.remove(channel)
        if ok:
            self.overviews.add((self.source, channel, lvl))
        self.overview_prepared.emit(self.source, channel, lvl, ok)


class FakeScheduler:
    def __init__(self, cancel_callbacks=True):
        self.corrected_cache = {}
        self.requests = []
        self.cancelled_generations = []
        self.cancel_callbacks = cancel_callbacks

    def request(self, request, callback):
        self.requests.append({
            "request": request,
            "callback": callback,
            "done": False,
        })

    def cancel_generation(self, generation):
        self.cancelled_generations.append(generation)
        if not self.cancel_callbacks:
            return
        # Model TileScheduler's queued cancellation callback. Started work
        # is represented by tests that complete a record explicitly.
        for record in list(self.requests):
            if record["done"] or record["request"].generation != generation:
                continue
            record["done"] = True
            record["callback"](SimpleNamespace(
                request=record["request"], error="cancelled"))

    def complete(self, index, error=None):
        record = self.requests[index]
        assert not record["done"]
        record["done"] = True
        self.corrected_cache[record["request"].key] = object()
        # A SUCCESS carries pixels. The real scheduler delivers a TileResult
        # whose `pixels` is None only on failure, and HOT now distinguishes
        # the two -- a fake that omitted pixels made every "success" in this
        # suite count as a failure, i.e. the model would have drifted from
        # the real scheduler exactly where the new logic reads it.
        record["callback"](SimpleNamespace(
            request=record["request"], error=error,
            pixels=None if error is not None else object()))


@pytest.fixture(scope="module")
def app():
    return QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])


def _snapshot(controller, channel="c2", level=1, visible=((0, 0), (1, 0)),
              epoch=0, algorithm_version="algo"):
    return SimpleNamespace(
        epoch=epoch,
        source=controller.source,
        channel=channel,
        method="tophat",
        params=(99,),
        level=level,
        quality=QualityLevel.INTERACTIVE,
        algorithm_version=algorithm_version,
        bbox_l0=(0, 0, 32, 16),
        visible_tiles=frozenset(visible),
        overview_ready=True,
        display_lo=0.0,
        display_hi=1.0,
    )


def _make(channels=("c0", "c1", "c2", "c3", "c4"),
          cancel_callbacks=True):
    controller = FakeController(channels)
    scheduler = FakeScheduler(cancel_callbacks=cancel_callbacks)
    specs = [ChannelCorrectionSpec(channel, 9, 13) for channel in channels]
    hot = MultiChannelPrefetchController(
        controller, scheduler, specs, TileGridSpec(tile_size=16),
    )
    return controller, scheduler, hot


def _ready_overviews(controller, channels=None, level=None):
    # Seed at the HOST's overview level, not the display level -- see
    # FakeController.OVERVIEW_LEVEL.
    level = controller.OVERVIEW_LEVEL if level is None else level
    for channel in channels or controller.channels:
        controller.overviews.add((controller.source, channel, level))


def _fire_confirm(hot):
    hot._confirm_timer.stop()
    hot._confirm_settle()


def _drain_requests(scheduler):
    index = 0
    while index < len(scheduler.requests):
        if not scheduler.requests[index]["done"]:
            scheduler.complete(index)
        index += 1


def test_settled_requires_extra_confirm(app):
    controller, scheduler, hot = _make()
    try:
        _ready_overviews(controller)
        snapshot = _snapshot(controller)
        controller.gesture_quiet.emit(snapshot)
        QtTest.QTest.qWait(SETTLE_CONFIRM_MS // 2)
        assert scheduler.requests == []
        assert hot.stats["settle_confirmations"] == 0

        QtTest.QTest.qWait(SETTLE_CONFIRM_MS)
        assert hot.stats["settle_confirmations"] == 1
        assert scheduler.requests
    finally:
        hot.stop()


def test_interaction_during_confirm_aborts_without_hot_work(app):
    controller, scheduler, hot = _make()
    try:
        snapshot = _snapshot(controller)
        controller.gesture_quiet.emit(snapshot)
        controller.interaction_event.emit("PAN", snapshot)
        QtTest.QTest.qWait(SETTLE_CONFIRM_MS + SETTLE_CONFIRM_MS // 2)
        assert scheduler.requests == []
        assert hot.stats["settle_aborted"] == 1
    finally:
        hot.stop()


@pytest.mark.parametrize(
    ("center", "expected"),
    [("c2", ["c1", "c3", "c0", "c4"]),
     ("c0", ["c1", "c2"]),
     ("c4", ["c3", "c2"])],
)
def test_hot_order_is_policy_order_at_both_ends(app, center, expected):
    controller, scheduler, hot = _make()
    try:
        _ready_overviews(controller)
        controller.gesture_quiet.emit(
            _snapshot(controller, channel=center, visible=((0, 0),)))
        _fire_confirm(hot)
        _drain_requests(scheduler)
        actual = []
        for record in scheduler.requests:
            channel = record["request"].key.channel
            if not actual or actual[-1] != channel:
                actual.append(channel)
        assert actual == expected
    finally:
        hot.stop()


def test_only_one_overview_is_in_flight_and_next_waits_for_signal(app):
    controller, scheduler, hot = _make(("c0", "c1", "c2", "c3"))
    try:
        controller.gesture_quiet.emit(_snapshot(controller, channel="c2"))
        _fire_confirm(hot)
        assert controller.prepare_calls == ["c1"]
        assert controller.overview_inflight == ["c1"]

        controller.finish_overview("c1")
        assert controller.prepare_calls == ["c1", "c3"]
        assert controller.overview_inflight == ["c3"]
        controller.finish_overview("c3")
        assert controller.prepare_calls == ["c1", "c3", "c0"]
        assert controller.overview_inflight == ["c0"]
    finally:
        hot.stop()


def test_interaction_stops_overview_submission(app):
    controller, scheduler, hot = _make(("c0", "c1", "c2", "c3"))
    try:
        snapshot = _snapshot(controller, channel="c2")
        controller.gesture_quiet.emit(snapshot)
        _fire_confirm(hot)
        assert controller.prepare_calls == ["c1"]
        controller.interaction_event.emit("ZOOM", snapshot)
        controller.finish_overview("c1")
        assert controller.prepare_calls == ["c1"]
    finally:
        hot.stop()


def test_corrected_inflight_cap_refills_one(app):
    controller, scheduler, hot = _make(("c0", "c1", "c2"))
    try:
        _ready_overviews(controller)
        controller.gesture_quiet.emit(_snapshot(controller, channel="c1"))
        _fire_confirm(hot)
        assert len(scheduler.requests) == 2
        assert len([r for r in scheduler.requests if not r["done"]]) == 2

        scheduler.complete(0)
        assert len(scheduler.requests) == 3
        assert len([r for r in scheduler.requests if not r["done"]]) == 2
    finally:
        hot.stop()


def test_every_hot_request_is_in_hot_priority_band(app):
    controller, scheduler, hot = _make(("c0", "c1", "c2"))
    try:
        _ready_overviews(controller)
        controller.gesture_quiet.emit(_snapshot(controller, channel="c1"))
        _fire_confirm(hot)
        _drain_requests(scheduler)
        assert scheduler.requests
        assert all(record["request"].priority >= HOT_PRIORITY_BASE
                   for record in scheduler.requests)
    finally:
        hot.stop()


def test_hot_results_are_never_blitted_or_displayed(app):
    controller, scheduler, hot = _make(("c0", "c1", "c2"))
    try:
        _ready_overviews(controller)
        before_pools = (list(controller.raw_pool), list(controller.precise_pool),
                        dict(controller.display_state))
        controller.gesture_quiet.emit(_snapshot(controller, channel="c1"))
        _fire_confirm(hot)
        _drain_requests(scheduler)
        assert controller.raw_pool == before_pools[0]
        assert controller.precise_pool == before_pools[1]
        assert controller.display_state == before_pools[2]
    finally:
        hot.stop()


def _expected_key(controller, grid, snapshot, channel, method, base):
    param = effective_param(base, snapshot.level,
                            controller.provider.level_downsample(snapshot.level))
    return CorrectionKey(
        source=snapshot.source,
        channel=channel,
        tile=TileAddress(grid=grid, level=snapshot.level, tx=0, ty=0),
        method=method,
        params=(param,),
        algorithm_version=snapshot.algorithm_version,
        quality=snapshot.quality,
    )


def test_readiness_requires_overview_and_exact_both_method_keys(app):
    controller, scheduler, hot = _make(("c0", "c1", "c2"))
    try:
        snapshot = _snapshot(controller, channel="c1", visible=((0, 0),))
        assert not hot.is_channel_ready("c0", snapshot)

        controller.overviews.add(
            (controller.source, "c0", controller.OVERVIEW_LEVEL))
        assert not hot.is_channel_ready("c0", snapshot)

        grid = hot.grid
        tophat = _expected_key(controller, grid, snapshot, "c0", "tophat", 9)
        cucim = _expected_key(controller, grid, snapshot, "c0", "cucim", 13)
        scheduler.corrected_cache[tophat] = object()
        assert not hot.is_channel_ready("c0", snapshot)
        scheduler.corrected_cache[cucim] = object()
        assert hot.is_channel_ready("c0", snapshot)

        scheduler.corrected_cache.pop(cucim)
        wrong = CorrectionKey(
            source=cucim.source, channel=cucim.channel, tile=cucim.tile,
            method=cucim.method,
            params=(effective_param(9, snapshot.level,
                                    controller.provider.level_downsample(
                                        snapshot.level)),),
            algorithm_version=cucim.algorithm_version, quality=cucim.quality,
        )
        scheduler.corrected_cache[wrong] = object()
        assert not hot.is_channel_ready("c0", snapshot)
    finally:
        hot.stop()


def test_stale_callback_does_not_change_current_hot_counters(app):
    controller, scheduler, hot = _make(("c0", "c1", "c2"),
                                        cancel_callbacks=False)
    try:
        _ready_overviews(controller)
        snapshot = _snapshot(controller, channel="c1", visible=((0, 0),))
        controller.gesture_quiet.emit(snapshot)
        _fire_confirm(hot)
        assert scheduler.requests
        requested = hot.stats["hot_tiles_requested"]
        completed = hot.stats["hot_tiles_completed"]
        controller.interaction_event.emit("PAN", snapshot)
        scheduler.complete(0)
        assert scheduler.requests[0]["request"].key in scheduler.corrected_cache
        assert hot.stats["hot_tiles_requested"] == requested
        assert hot.stats["hot_tiles_completed"] == completed
    finally:
        hot.stop()


def test_rapid_switches_are_latest_wins(app):
    controller, scheduler, hot = _make(("c0", "c1", "c2", "c3"))
    try:
        _ready_overviews(controller)
        first = _snapshot(controller, channel="c1", visible=((0, 0),))
        controller.gesture_quiet.emit(first)
        _fire_confirm(hot)
        old_request_count = len(scheduler.requests)

        middle = _snapshot(controller, channel="c2", visible=((0, 0),), epoch=1)
        newest = _snapshot(controller, channel="c3", visible=((0, 0),), epoch=2)
        controller.selection_context_changed.emit(middle)
        controller.interaction_event.emit("CHANNEL_SWITCH", middle)
        controller.selection_context_changed.emit(newest)
        controller.interaction_event.emit("CHANNEL_SWITCH", newest)
        controller.gesture_quiet.emit(newest)
        _fire_confirm(hot)
        _drain_requests(scheduler)

        new_requests = scheduler.requests[old_request_count:]
        assert new_requests
        assert {record["request"].key.channel for record in new_requests} == {
            "c2", "c1",
        }
        assert all(record["request"].generation == new_requests[0]["request"].generation
                   for record in new_requests)
        # The intermediate centre c2 never reached its own settle confirm.
    finally:
        hot.stop()


def test_stop_twice_is_safe_and_leaves_no_local_work(app):
    controller, scheduler, hot = _make(("c0", "c1", "c2"))
    _ready_overviews(controller)
    controller.gesture_quiet.emit(_snapshot(controller, channel="c1"))
    _fire_confirm(hot)
    hot.stop()
    hot.stop()
    before = len(scheduler.requests)
    controller.gesture_quiet.emit(_snapshot(controller, channel="c1"))
    QtTest.QTest.qWait(SETTLE_CONFIRM_MS + SETTLE_CONFIRM_MS // 2)
    assert len(scheduler.requests) == before
    assert not hot._tile_queue
    assert len(scheduler.cancelled_generations) == 1


def test_overview_is_queried_at_the_hosts_level_not_the_display_level(app):
    """Regression for a defect that made HOT unusable on real data.

    The display level is L0 while zoomed in; the host keeps its overview
    record at its OWN level (L2 on the real slides). Querying the DISPLAY
    level meant the lookup never matched, so no channel could ever be
    reported ready -- and HOT then called `prepare_overview_async` for a
    record that was already cached, which is a silent no-op emitting
    nothing, wedging the one-overview-at-a-time gate permanently. The
    earlier tests hid this by modelling both levels as the same number.
    """
    controller, scheduler, hot = _make()
    try:
        snapshot = _snapshot(controller, level=0)      # displaying L0
        assert snapshot.level != controller.OVERVIEW_LEVEL, "test setup"

        _ready_overviews(controller)                    # stored at L2 only
        for tx, ty in snapshot.visible_tiles:
            for method, base in (("tophat", 9), ("cucim", 13)):
                key = hot._make_key(snapshot, "c1", tx, ty, method, base)
                scheduler.corrected_cache[key] = object()

        assert hot.is_channel_ready("c1", snapshot) is True, (
            "readiness asked for the display level instead of the host's")

        controller.prepare_calls.clear()
        controller.gesture_quiet.emit(snapshot)
        _fire_confirm(hot)
        assert controller.prepare_calls == [], (
            "HOT asked to prepare overviews it already has")
        assert hot._overview_inflight is None, (
            "the one-at-a-time overview gate wedged on an already-cached record")
    finally:
        hot.stop()


def test_stale_tile_callback_refills_the_freed_slot(app):
    """A callback from a superseded generation frees its in-flight slot. If
    it returns without pumping, a newer plan sits stalled with capacity
    available and nobody to use it."""
    controller, scheduler, hot = _make(cancel_callbacks=False)
    try:
        _ready_overviews(controller)
        snapshot = _snapshot(controller)
        controller.gesture_quiet.emit(snapshot)
        _fire_confirm(hot)
        assert scheduler.requests, "test setup: nothing was requested"

        controller.interaction_event.emit("PAN", snapshot)
        controller.gesture_quiet.emit(snapshot)
        _fire_confirm(hot)
        issued_before = len(scheduler.requests)

        # A callback from the OLD generation now arrives.
        scheduler.complete(0)

        assert len(scheduler.requests) > issued_before, (
            "a stale callback freed a slot but nothing refilled it")
    finally:
        hot.stop()


def test_failures_are_not_counted_as_completions(app):
    """A benchmark reporting "120 of 120 completed" has to mean it."""
    controller, scheduler, hot = _make(cancel_callbacks=False)
    try:
        _ready_overviews(controller)
        snapshot = _snapshot(controller)
        controller.gesture_quiet.emit(snapshot)
        _fire_confirm(hot)
        scheduler.complete(0, error="boom")

        assert hot.stats["hot_tiles_completed"] == 0
        assert hot.stats["hot_tiles_failed"] == 1
    finally:
        hot.stop()


def test_failed_overview_does_not_make_a_channel_ready(app):
    """A channel whose overview could not be read has an unknown display
    range; queueing its corrected tiles would spend the budget on a channel
    that cannot be switched to."""
    controller, scheduler, hot = _make()
    try:
        snapshot = _snapshot(controller)
        controller.gesture_quiet.emit(snapshot)
        _fire_confirm(hot)
        target = controller.prepare_calls[0]
        before = len(scheduler.requests)
        controller.finish_overview(target, ok=False)

        assert hot.stats["overviews_failed"] == 1
        assert hot.is_channel_ready(target, snapshot) is False
        queued = [r["request"] for r in scheduler.requests[before:]
                  if r["request"].key.channel == target]
        assert not queued, (
            "tiles were queued for a channel whose overview failed")
    finally:
        hot.stop()
