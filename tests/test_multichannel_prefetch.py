import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtCore, QtTest, QtWidgets  # noqa: E402

from viewer.multichannel_prefetch import (  # noqa: E402
    COVERAGE_BATCH_CHANNELS,
    COVERAGE_INFLIGHT,
    COVERAGE_PRIORITY_BASE,
    HOT_PRIORITY_BASE,
    HOT_INFLIGHT,
    SETTLE_CONFIRM_MS,
    MultiChannelPrefetchController,
)
from viewer.prefetch_policy import (  # noqa: E402
    ChannelCorrectionSpec,
    _coverage_order,
    _hot_order,
)
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
        """Physically finish a request.

        Models `TileScheduler._deliver`: if this request's generation has
        been cancelled, a waiter is called back ONLY when it opted in with
        `notify_on_stale_completion`, and then with a terminal
        `error="stale"` and no pixels. That terminal callback is how a
        consumer metering PHYSICAL concurrency learns the work is over --
        cancelling a generation never stopped work that had already started.
        """
        record = self.requests[index]
        assert not record["done"]
        record["done"] = True
        self.corrected_cache[record["request"].key] = object()
        req = record["request"]
        if req.generation in self.cancelled_generations:
            if getattr(req, "notify_on_stale_completion", False):
                record["callback"](SimpleNamespace(
                    request=req, error="stale", pixels=None))
            return
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
          cancel_callbacks=True, coverage=False):
    # `coverage=False` by default: every pre-existing test in this module
    # was written to exercise HOT in isolation (channel dedup, generation
    # counts, cancellation counts...), before COVERAGE existed. COVERAGE
    # defaults to on in the constructor itself (production behaviour is
    # unchanged), but letting it run by default in every one of those
    # HOT-only fixtures would put extra, HOT-unrelated requests into
    # `scheduler.requests` / extra `cancel_generation` calls that those
    # tests were never written to expect. The COVERAGE-specific tests below
    # opt back in explicitly via `_make_coverage` (coverage=True).
    controller = FakeController(channels)
    scheduler = FakeScheduler(cancel_callbacks=cancel_callbacks)
    specs = [ChannelCorrectionSpec(channel, 9, 13) for channel in channels]
    hot = MultiChannelPrefetchController(
        controller, scheduler, specs, TileGridSpec(tile_size=16),
        coverage=coverage,
    )
    return controller, scheduler, hot


def _ready_overviews(controller, channels=None, level=None):
    # Seed at the HOST's overview level, not the display level -- see
    # FakeController.OVERVIEW_LEVEL.
    level = controller.OVERVIEW_LEVEL if level is None else level
    for channel in channels or controller.channels:
        controller.overviews.add((controller.source, channel, level))


def _pump():
    """Let queued signals run. HOT's tile deliveries arrive on a compute
    worker thread and are marshalled to the GUI thread through a queued
    signal, exactly as every other delivery path in this viewer is, so a
    test that completes a request must pump before asserting."""
    QtWidgets.QApplication.instance().processEvents()


def _deliver(scheduler, index, error=None):
    """Physically finish a request AND let its queued delivery run."""
    scheduler.complete(index, error=error)
    _pump()


def _fire_confirm(hot):
    hot._confirm_timer.stop()
    hot._confirm_settle()


def _drain_requests(scheduler):
    # Flush anything already queued first. Deliveries are marshalled to the
    # GUI thread now, so a callback the fake has already fired (for instance
    # the "cancelled" it delivers when a generation is cancelled) has not
    # been HANDLED until the event loop runs -- and until it is handled, the
    # slot it occupies is still counted.
    _pump()
    index = 0
    while index < len(scheduler.requests):
        if not scheduler.requests[index]["done"]:
            _deliver(scheduler, index)
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

        _deliver(scheduler, 0)
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
        _deliver(scheduler, 0)
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
        # Let the queued cancellations from those switches be handled
        # before asserting on what the newest plan managed to issue.
        _pump()
        controller.gesture_quiet.emit(newest)
        _fire_confirm(hot)
        _drain_requests(scheduler)

        new_requests = scheduler.requests[old_request_count:]
        assert new_requests
        # Latest-wins means ONE surviving generation. The exact channel set
        # is not asserted: deliveries are queued now, so draining pumps the
        # event loop and HOT legitimately refills with further neighbours of
        # the newest centre as slots free up. What must hold is that every
        # request belongs to the same, newest generation, and that the
        # superseded plans contributed none.
        generations = {record["request"].generation for record in new_requests}
        assert len(generations) == 1, (
            f"more than one generation survived rapid switching: {generations}")
        centre = controller.channels.index("c3")
        allowed = {controller.channels[i]
                   for i in _hot_order(centre, len(controller.channels))}
        got = {record["request"].key.channel for record in new_requests}
        assert got <= allowed, (
            f"requests for channels outside the newest centre's HOT order: "
            f"{got - allowed}")
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


def test_physical_in_flight_never_exceeds_the_cap_across_generations(app):
    """The cap must meter PHYSICAL work, not the current plan's work.

    Cancelling a generation stops only what has not started; anything
    already running keeps consuming the same I/O and GPU. An earlier
    revision released those slots at abort time, so an abandoned task and a
    freshly issued one could run together and the real concurrency could
    reach 2 * hot_inflight -- the opposite of what the cap is for. (That
    revision existed because `TileScheduler` did not call back a stale
    waiter at all, which leaks the slot forever; the fix is the opt-in
    terminal callback, not early release.)
    """
    controller, scheduler, hot = _make(cancel_callbacks=False)
    try:
        _ready_overviews(controller)
        snapshot = _snapshot(controller)
        controller.gesture_quiet.emit(snapshot)
        _fire_confirm(hot)
        assert len(hot._active_requests) == HOT_INFLIGHT, "test setup"
        assert all(r["request"].notify_on_stale_completion
                   for r in scheduler.requests), (
            "HOT must opt in to the terminal stale callback or it can never "
            "learn that abandoned work finished")
        issued_before = len(scheduler.requests)

        # Abort: the two running tasks are abandoned but NOT stopped.
        controller.interaction_event.emit("PAN", snapshot)
        assert len(hot._active_requests) == HOT_INFLIGHT, (
            "slots were released while the physical work was still running")

        # A new plan settles, and must issue NOTHING: the capacity is still
        # genuinely occupied.
        controller.gesture_quiet.emit(snapshot)
        _fire_confirm(hot)
        assert len(scheduler.requests) == issued_before, (
            f"{len(scheduler.requests) - issued_before} tasks were issued "
            f"while {HOT_INFLIGHT} abandoned ones were still running")

        # One abandoned task physically finishes -> exactly one new task.
        _deliver(scheduler, 0)
        assert hot.stats["hot_abandoned_finished"] == 1
        assert len(scheduler.requests) == issued_before + 1, (
            "finishing one abandoned task should admit exactly one new task")
        assert len(hot._active_requests) <= HOT_INFLIGHT

        # And again for the second.
        _deliver(scheduler, 1)
        assert len(scheduler.requests) == issued_before + 2
        assert len(hot._active_requests) <= HOT_INFLIGHT
    finally:
        hot.stop()


def test_late_stale_callback_is_harmless(app):
    """A callback that does arrive for a superseded request must neither
    double-release capacity nor touch the current generation's counters."""
    controller, scheduler, hot = _make(cancel_callbacks=False)
    try:
        _ready_overviews(controller)
        snapshot = _snapshot(controller)
        controller.gesture_quiet.emit(snapshot)
        _fire_confirm(hot)
        controller.interaction_event.emit("PAN", snapshot)
        controller.gesture_quiet.emit(snapshot)
        _fire_confirm(hot)

        done_before = hot.stats["hot_tiles_completed"]
        active_before = len(hot._active_requests)
        _deliver(scheduler, 0)          # from the abandoned generation

        assert hot.stats["hot_tiles_completed"] == done_before
        assert len(hot._active_requests) == active_before
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
        _deliver(scheduler, 0, error="boom")

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


# ── COVERAGE (P3) ────────────────────────────────────────────────────────────
#
# A 10-channel list, centred on "c4", is used throughout so HOT's 4-neighbour
# order (_hot_order(4, 10) == [3, 5, 2, 6]) and COVERAGE's remainder
# (_coverage_order(10, 4) minus that neighbourhood == [c0, c9, c1, c8, c7])
# are both non-trivial and disjoint -- a 5-channel list (as `_make`'s default)
# leaves HOT's neighbourhood covering everyone else, so COVERAGE would never
# have anything to do.
_COVERAGE_CHANNELS = tuple(f"c{i}" for i in range(10))
_COVERAGE_CENTER = "c4"
_COVERAGE_HOT_CHANNELS = {"c2", "c3", "c5", "c6"}
_COVERAGE_REMAINING_ORDER = ["c0", "c9", "c1", "c8", "c7"]


def _make_coverage(**kwargs):
    kwargs.setdefault("coverage", True)
    return _make(_COVERAGE_CHANNELS, **kwargs)


def _finish_all_hot_overviews(controller, hot):
    """Resolve HOT's one-at-a-time overview fetches until none is pending."""
    guard = 0
    while hot._overview_inflight is not None:
        guard += 1
        assert guard <= 20, "overview fetch never settled"
        target = hot._overview_inflight[2]
        controller.finish_overview(target)


def _drain_hot_only(scheduler, hot):
    """Deliver ONLY HOT's requests (never touching a COVERAGE request),
    until HOT is genuinely idle. Used to get COVERAGE running without
    accidentally draining any COVERAGE request a test wants to inspect
    in flight."""
    index = 0
    while hot._tile_queue or hot._active_requests:
        if index >= len(scheduler.requests):
            _pump()
            continue
        record = scheduler.requests[index]
        if (not record["done"]
                and record["request"].key.channel in _COVERAGE_HOT_CHANNELS):
            _deliver(scheduler, index)
        index += 1


def test_coverage_order_is_policy_order_minus_hot_minus_completed(app):
    controller, scheduler, hot = _make_coverage()
    try:
        snapshot = _snapshot(controller, channel=_COVERAGE_CENTER,
                             visible=((0, 0),))
        # Pre-cache "c9"'s tiles so it counts as already complete and must
        # be excluded from the plan.
        for method, base in (("tophat", 9), ("cucim", 13)):
            key = hot._make_key(snapshot, "c9", 0, 0, method, base)
            scheduler.corrected_cache[key] = object()

        controller.gesture_quiet.emit(snapshot)
        _fire_confirm(hot)

        expected = [c for c in _COVERAGE_REMAINING_ORDER if c != "c9"]
        assert hot._coverage_full_order == expected
    finally:
        hot.stop()


def test_coverage_is_planned_in_batches_and_waits_for_the_previous_one(app):
    controller, scheduler, hot = _make_coverage()
    try:
        _ready_overviews(controller)
        snapshot = _snapshot(controller, channel=_COVERAGE_CENTER,
                             visible=((0, 0),))
        controller.gesture_quiet.emit(snapshot)
        _fire_confirm(hot)

        # First batch of COVERAGE_BATCH_CHANNELS=4 channels is planned
        # immediately (planning does not wait on HOT), the 5th is not.
        assert hot.stats["coverage_batches"] == 1
        assert hot._coverage_order_position == COVERAGE_BATCH_CHANNELS
        assert len(hot._coverage_queue) == COVERAGE_BATCH_CHANNELS * 2  # 2 methods

        _drain_requests(scheduler)

        # Once every tile from both batches has completed, both batches
        # have been planned and the whole remaining order was consumed.
        assert hot.stats["coverage_batches"] == 2
        assert hot._coverage_order_position == len(_COVERAGE_REMAINING_ORDER)
        assert hot.stats["coverage_tiles_completed"] == len(_COVERAGE_REMAINING_ORDER) * 2
        assert hot.stats["coverage_tiles_failed"] == 0
    finally:
        hot.stop()


def test_coverage_issues_nothing_while_hot_has_queued_or_inflight_work(app):
    controller, scheduler, hot = _make_coverage()
    try:
        _ready_overviews(controller)
        snapshot = _snapshot(controller, channel=_COVERAGE_CENTER,
                             visible=((0, 0),))
        controller.gesture_quiet.emit(snapshot)
        _fire_confirm(hot)

        # HOT still has queued/in-flight work (8 tiles, HOT_INFLIGHT=2 cap):
        # nothing coverage-owned has reached the scheduler yet.
        assert scheduler.requests
        assert all(r["request"].key.channel in _COVERAGE_HOT_CHANNELS
                   for r in scheduler.requests)

        _drain_hot_only(scheduler, hot)
        assert not hot._tile_queue and not hot._active_requests

        # HOT is now fully drained -- COVERAGE has started.
        assert any(r["request"].key.channel not in _COVERAGE_HOT_CHANNELS
                   for r in scheduler.requests)
    finally:
        hot.stop()


def test_every_coverage_request_priority_is_above_every_hot_priority(app):
    controller, scheduler, hot = _make_coverage()
    try:
        _ready_overviews(controller)
        snapshot = _snapshot(controller, channel=_COVERAGE_CENTER,
                             visible=((0, 0),))
        controller.gesture_quiet.emit(snapshot)
        _fire_confirm(hot)
        _drain_requests(scheduler)

        hot_priorities = [r["request"].priority for r in scheduler.requests
                          if r["request"].key.channel in _COVERAGE_HOT_CHANNELS]
        coverage_priorities = [
            r["request"].priority for r in scheduler.requests
            if r["request"].key.channel not in _COVERAGE_HOT_CHANNELS]

        assert hot_priorities and coverage_priorities
        assert all(p >= COVERAGE_PRIORITY_BASE for p in coverage_priorities)
        assert max(hot_priorities) < min(coverage_priorities)
    finally:
        hot.stop()


def test_coverage_requests_set_notify_on_stale_completion(app):
    controller, scheduler, hot = _make_coverage()
    try:
        _ready_overviews(controller)
        snapshot = _snapshot(controller, channel=_COVERAGE_CENTER,
                             visible=((0, 0),))
        controller.gesture_quiet.emit(snapshot)
        _fire_confirm(hot)
        _drain_requests(scheduler)

        coverage_records = [r for r in scheduler.requests
                            if r["request"].key.channel not in _COVERAGE_HOT_CHANNELS]
        assert coverage_records
        assert all(r["request"].notify_on_stale_completion
                   for r in coverage_records)
    finally:
        hot.stop()


def test_coverage_physical_in_flight_never_exceeds_the_cap_across_generations(app):
    """Mirrors `test_physical_in_flight_never_exceeds_the_cap_across_generations`
    for COVERAGE: an abandoned generation's still-running task keeps its
    slot until its terminal callback arrives, never released early."""
    controller, scheduler, hot = _make_coverage(cancel_callbacks=False)
    try:
        _ready_overviews(controller)
        snapshot = _snapshot(controller, channel=_COVERAGE_CENTER,
                             visible=((0, 0),))
        controller.gesture_quiet.emit(snapshot)
        _fire_confirm(hot)
        _drain_hot_only(scheduler, hot)
        assert not hot._tile_queue and not hot._active_requests, "test setup"

        assert len(hot._coverage_active_requests) == COVERAGE_INFLIGHT, "test setup"

        def _coverage_records():
            return [r for r in scheduler.requests
                    if r["request"].key.channel not in _COVERAGE_HOT_CHANNELS]

        coverage_issued_before = len(_coverage_records())
        # The one active COVERAGE request is the most recently issued one.
        active_index = scheduler.requests.index(_coverage_records()[-1])

        # Abort: the running COVERAGE task is abandoned but NOT stopped.
        controller.interaction_event.emit("PAN", snapshot)
        assert len(hot._coverage_active_requests) == COVERAGE_INFLIGHT, (
            "slot released while the physical work was still running")

        # A new plan settles. HOT must drain again first (this fake never
        # simulates a scheduler cache hit), and only once it does may
        # COVERAGE even attempt anything -- which it must not, because the
        # single physical slot is still genuinely occupied.
        controller.gesture_quiet.emit(snapshot)
        _fire_confirm(hot)
        _drain_hot_only(scheduler, hot)
        assert not hot._tile_queue and not hot._active_requests

        assert len(_coverage_records()) == coverage_issued_before, (
            f"{len(_coverage_records()) - coverage_issued_before} COVERAGE "
            f"tasks were issued while the physical slot was still occupied")

        # The abandoned task physically finishes -> exactly one new task.
        _deliver(scheduler, active_index)
        assert hot.stats["coverage_abandoned_finished"] == 1
        assert len(_coverage_records()) == coverage_issued_before + 1, (
            "finishing the abandoned task should admit exactly one new task")
        assert len(hot._coverage_active_requests) <= COVERAGE_INFLIGHT
    finally:
        hot.stop()


def test_interaction_cancels_coverage_and_it_resumes_only_after_settle(app):
    controller, scheduler, hot = _make_coverage()
    try:
        _ready_overviews(controller)
        snapshot = _snapshot(controller, channel=_COVERAGE_CENTER,
                             visible=((0, 0),))
        controller.gesture_quiet.emit(snapshot)
        _fire_confirm(hot)

        assert hot._coverage_full_order
        assert hot._coverage_queue or hot._coverage_active_requests
        queued_before_abort = len(hot._coverage_queue)

        controller.interaction_event.emit("PAN", snapshot)
        assert hot._coverage_full_order == []
        assert not hot._coverage_queue
        assert hot.stats["coverage_cancelled"] >= queued_before_abort

        before = len(scheduler.requests)
        # Still no new COVERAGE plan without a fresh settle confirmation.
        QtTest.QTest.qWait(SETTLE_CONFIRM_MS // 2)
        assert not hot._coverage_full_order

        controller.gesture_quiet.emit(snapshot)
        _fire_confirm(hot)
        assert hot._coverage_full_order  # resumed
    finally:
        hot.stop()


def test_coverage_never_requests_overviews(app):
    controller, scheduler, hot = _make_coverage()
    try:
        # Deliberately leave every overview UNready (not even HOT's) so that
        # `prepare_overview_async` calls are actually exercised, and the
        # test can show COVERAGE channels never appear among them.
        snapshot = _snapshot(controller, channel=_COVERAGE_CENTER,
                             visible=((0, 0),))
        controller.gesture_quiet.emit(snapshot)
        _fire_confirm(hot)
        _finish_all_hot_overviews(controller, hot)
        _drain_requests(scheduler)

        assert controller.prepare_calls, "test setup: HOT should have fetched overviews"
        assert set(controller.prepare_calls) <= _COVERAGE_HOT_CHANNELS
        coverage_channels = set(_COVERAGE_REMAINING_ORDER)
        assert not (coverage_channels & set(controller.prepare_calls)), (
            "COVERAGE must never call prepare_overview_async")
    finally:
        hot.stop()


def test_coverage_results_are_never_blitted_or_displayed(app):
    controller, scheduler, hot = _make_coverage()
    try:
        _ready_overviews(controller)
        before_pools = (list(controller.raw_pool), list(controller.precise_pool),
                        dict(controller.display_state))
        snapshot = _snapshot(controller, channel=_COVERAGE_CENTER,
                             visible=((0, 0),))
        controller.gesture_quiet.emit(snapshot)
        _fire_confirm(hot)
        _drain_requests(scheduler)

        assert controller.raw_pool == before_pools[0]
        assert controller.precise_pool == before_pools[1]
        assert controller.display_state == before_pools[2]
    finally:
        hot.stop()


def test_coverage_channel_ready_stays_false_without_an_overview_record(app):
    controller, scheduler, hot = _make_coverage()
    try:
        _ready_overviews(controller)  # every channel EXCEPT none excluded here
        snapshot = _snapshot(controller, channel=_COVERAGE_CENTER,
                             visible=((0, 0),))
        controller.gesture_quiet.emit(snapshot)
        _fire_confirm(hot)
        _drain_requests(scheduler)

        coverage_channel = _COVERAGE_REMAINING_ORDER[0]
        # Tiles are fully cached (COVERAGE finished it)...
        assert hot.is_channel_ready(coverage_channel, snapshot) is True

        # ...but with the overview record removed, readiness must go back
        # to False: COVERAGE never fetches overviews, so a real COVERAGE
        # channel (whose overview HOT has not reached yet) must never be
        # reported ready.
        controller.overviews.discard(
            (controller.source, coverage_channel, controller.OVERVIEW_LEVEL))
        assert hot.is_channel_ready(coverage_channel, snapshot) is False
    finally:
        hot.stop()


def test_stop_twice_is_safe_with_coverage_running(app):
    controller, scheduler, hot = _make_coverage()
    _ready_overviews(controller)
    snapshot = _snapshot(controller, channel=_COVERAGE_CENTER, visible=((0, 0),))
    controller.gesture_quiet.emit(snapshot)
    _fire_confirm(hot)
    _drain_hot_only(scheduler, hot)
    assert hot._coverage_queue or hot._coverage_active_requests, "test setup"

    hot.stop()
    hot.stop()

    before = len(scheduler.requests)
    controller.gesture_quiet.emit(snapshot)
    QtTest.QTest.qWait(SETTLE_CONFIRM_MS + SETTLE_CONFIRM_MS // 2)
    assert len(scheduler.requests) == before
    assert not hot._tile_queue
    assert not hot._coverage_queue
    assert hot._hot_generation in scheduler.cancelled_generations
    assert hot._coverage_generation - 1 in scheduler.cancelled_generations or \
        hot._coverage_generation in scheduler.cancelled_generations
