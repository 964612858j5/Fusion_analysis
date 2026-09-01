"""COVERAGE (P3) tests — the EXPERIMENTAL controller.

Moved out of test_multichannel_prefetch.py when COVERAGE left the
production path (`viewer/experimental/coverage_prefetch.py`). The
assertions are unchanged; only the controller being instantiated is: these
build `CoverageMultiChannelPrefetchController`, and instantiating it IS
the opt-in — there is no `coverage=True` flag any more.

The HOT fixtures and fakes are imported from the production suite rather
than duplicated, so both suites keep exercising the same fake host.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtTest  # noqa: E402

from viewer.experimental.coverage_prefetch import (  # noqa: E402
    COVERAGE_BATCH_CHANNELS,
    COVERAGE_INFLIGHT,
    COVERAGE_PRIORITY_BASE,
    CoverageMultiChannelPrefetchController,
)
from viewer.prefetch_policy import coverage_order, hot_order  # noqa: E402

from test_multichannel_prefetch import (  # noqa: E402
    HOT_PRIORITY_BASE,
    SETTLE_CONFIRM_MS,
    _deliver,
    _drain_requests,
    _expected_key,
    _fire_confirm,
    _make,
    _pump,
    _ready_overviews,
    _snapshot,
    app,           # noqa: F401  (pytest fixture)
)


# ── COVERAGE (P3) ────────────────────────────────────────────────────────────
#
# A 10-channel list, centred on "c4", is used throughout so HOT's 4-neighbour
# order (hot_order(4, 10) == [3, 5, 2, 6]) and COVERAGE's remainder
# (coverage_order(10, 4) minus that neighbourhood == [c0, c9, c1, c8, c7])
# are both non-trivial and disjoint -- a 5-channel list (as `_make`'s default)
# leaves HOT's neighbourhood covering everyone else, so COVERAGE would never
# have anything to do.
_COVERAGE_CHANNELS = tuple(f"c{i}" for i in range(10))
_COVERAGE_CENTER = "c4"
_COVERAGE_HOT_CHANNELS = {"c2", "c3", "c5", "c6"}
_COVERAGE_REMAINING_ORDER = ["c0", "c9", "c1", "c8", "c7"]


def _make_coverage(**kwargs):
    """The EXPERIMENTAL controller on the 10-channel fixture. Building this
    class is what enables COVERAGE."""
    return _make(_COVERAGE_CHANNELS,
                 controller_cls=CoverageMultiChannelPrefetchController,
                 **kwargs)


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


def test_coverage_survives_a_request_that_the_scheduler_rejects(app):
    """A request that never reaches the scheduler will never deliver a
    callback, so its share of the batch has to be settled where it failed.

    Without that, `_coverage_batch_remaining` never reaches zero, the next
    batch is never planned, and COVERAGE stops for good while looking idle
    from the outside -- empty queue, nothing in flight -- which a benchmark
    would happily report as "drained".
    """
    controller, scheduler, hot = _make_coverage()
    try:
        _ready_overviews(controller)
        snapshot = _snapshot(controller, channel=_COVERAGE_CENTER,
                             visible=((0, 0),))
        controller.gesture_quiet.emit(snapshot)
        _fire_confirm(hot)
        assert hot.stats["coverage_batches"] == 1

        # The FIRST tile of batch 1 is never accepted by the scheduler.
        real_request = scheduler.request
        blown = {"n": 0}

        def exploding_request(request, callback):
            if (request.priority >= COVERAGE_PRIORITY_BASE
                    and blown["n"] == 0):
                blown["n"] += 1
                raise RuntimeError("scheduler refused")
            return real_request(request, callback)

        scheduler.request = exploding_request
        try:
            _drain_requests(scheduler)
        finally:
            scheduler.request = real_request
        _drain_requests(scheduler)

        assert blown["n"] == 1, "test setup: the failure never happened"
        assert hot.stats["coverage_cancelled"] >= 1

        # The rejected tile must not wedge the plan: batch 2 is still
        # planned, the whole remaining order is consumed, and the batch
        # counter settles back to zero with nothing left in flight.
        assert hot.stats["coverage_batches"] == 2
        assert hot._coverage_order_position == len(_COVERAGE_REMAINING_ORDER)
        assert hot._coverage_batch_remaining == 0
        assert not hot._coverage_queue
        assert not hot._coverage_active_requests
        # Everything except the one rejected tile still completed.
        assert (hot.stats["coverage_tiles_completed"]
                == len(_COVERAGE_REMAINING_ORDER) * 2 - 1)
    finally:
        hot.stop()


# ── experimental-controller lifecycle ────────────────────────────────────────

def test_coverage_stats_and_state_live_on_the_experimental_controller(app):
    """Everything the production class shed is present here."""
    controller, scheduler, hot = _make_coverage()
    try:
        for key in ("coverage_batches", "coverage_tiles_requested",
                    "coverage_tiles_completed", "coverage_tiles_failed",
                    "coverage_cancelled", "coverage_abandoned_finished"):
            assert key in hot.stats, key
        assert hot.coverage_inflight == COVERAGE_INFLIGHT
        for attr in ("_coverage_generation", "_coverage_full_order",
                     "_coverage_order_position", "_coverage_queue",
                     "_coverage_active_requests", "_coverage_request_serial",
                     "_coverage_pumping", "_coverage_batch_remaining"):
            assert hasattr(hot, attr), attr
        # HOT's own surface is inherited untouched.
        assert hot.stats["hot_batches"] == 0
        assert hot.hot_inflight > 0
    finally:
        hot.stop()


def test_coverage_inflight_is_configurable_and_defaults_to_one(app):
    controller, scheduler, hot = _make_coverage(coverage_inflight=3)
    try:
        assert hot.coverage_inflight == 3
    finally:
        hot.stop()
    controller, scheduler, hot = _make_coverage()
    try:
        assert hot.coverage_inflight == 1
    finally:
        hot.stop()


def test_both_signals_deliver_on_the_subclass(app):
    """A `pyqtSignal` declared on the subclass and one inherited from the
    base must BOTH connect and deliver -- if the metaclass dropped either,
    HOT or COVERAGE would silently never report a completion."""
    controller, scheduler, hot = _make_coverage()
    try:
        _ready_overviews(controller)
        snapshot = _snapshot(controller, channel=_COVERAGE_CENTER,
                             visible=((0, 0),))
        controller.gesture_quiet.emit(snapshot)
        _fire_confirm(hot)
        _drain_requests(scheduler)

        assert hot.stats["hot_tiles_completed"] > 0, (
            "the inherited HOT delivery signal never arrived")
        assert hot.stats["coverage_tiles_completed"] > 0, (
            "the subclass's COVERAGE delivery signal never arrived")
    finally:
        hot.stop()


def test_stop_is_idempotent_and_late_callbacks_cannot_resume_coverage(app):
    """`stop()` disconnects, but a queued Qt delivery already posted cannot
    be un-queued -- so `_stopped` is the final line of defence: a late
    callback must not issue a request, advance the batch or revive the plan.
    """
    controller, scheduler, hot = _make_coverage()
    _ready_overviews(controller)
    snapshot = _snapshot(controller, channel=_COVERAGE_CENTER,
                         visible=((0, 0),))
    controller.gesture_quiet.emit(snapshot)
    _fire_confirm(hot)

    # Leave COVERAGE work in flight, then stop.
    assert hot.stats["coverage_tiles_requested"] > 0 or hot._coverage_queue
    in_flight = dict(hot._coverage_active_requests)
    hot.stop()
    hot.stop()          # idempotent
    hot.stop()

    requests_after_stop = len(scheduler.requests)
    position_after_stop = hot._coverage_order_position
    batch_after_stop = hot._coverage_batch_remaining

    # Simulate the deliveries that were already posted when stop() ran.
    for token, generation in in_flight.items():
        hot._on_coverage_tile_result(token, generation, _fake_ok_result())
    hot._on_coverage_tile_result(999, hot._coverage_generation,
                                 _fake_ok_result())
    _pump()

    assert len(scheduler.requests) == requests_after_stop, (
        "a late callback issued new COVERAGE work after stop()")
    assert hot._coverage_order_position == position_after_stop
    assert hot._coverage_batch_remaining == batch_after_stop
    assert not hot._coverage_queue


def _fake_ok_result():
    from types import SimpleNamespace
    return SimpleNamespace(error=None, pixels=object())
