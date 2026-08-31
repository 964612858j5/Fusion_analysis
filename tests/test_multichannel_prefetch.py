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

    def has_overview_record(self, channel, level=None, source=None):
        return (source, channel, level) in self.overviews

    def prepare_overview_async(self, channel):
        self.prepare_calls.append(channel)
        self.overview_inflight.append(channel)

    def finish_overview(self, channel, ok=True, level=1):
        if channel in self.overview_inflight:
            self.overview_inflight.remove(channel)
        if ok:
            self.overviews.add((self.source, channel, level))
        self.overview_prepared.emit(self.source, channel, level, ok)


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
        record["callback"](SimpleNamespace(
            request=record["request"], error=error))


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


def _ready_overviews(controller, channels=None, level=1):
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

        controller.overviews.add((controller.source, "c0", snapshot.level))
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
