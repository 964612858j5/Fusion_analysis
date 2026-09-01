"""COVERAGE (P3) background channel preparation — EXPERIMENTAL.

Every remaining channel's current viewport, walked from both ends of the
channel list toward the middle (`prefetch_rules.coverage_order`), planned in
batches of `COVERAGE_BATCH_CHANNELS`, cache-only, at strictly lower priority
than every HOT request.

WHY IT LIVES HERE, not in the production controller: measured on the real
57-channel slide, COVERAGE only pays off if what it prepares survives in the
corrected cache. At the 512MB budget the demo uses, a long dwell evicted
1305 of the 1817 tiles it produced and left 0 of 57 channels switch-ready;
even at 8GB, with zero evictions, only 2 of 57 counted as ready, because
`is_channel_ready` also needs an overview record and COVERAGE deliberately
never fetches one (HOT owns the one-at-a-time overview channel). See
docs/benchmarks/2026-09-01_57ch_coverage_long_dwell.md. So the production
Explore stack runs HOT only, and instantiating THIS class is what enables
COVERAGE -- there is no longer a `coverage=True/False` flag to get wrong.

Implemented as a subclass rather than a collaborator on purpose. COVERAGE's
strict-priority gate reads HOT's physical state (`_hot_idle()`: HOT's queue,
its in-flight map AND its one-at-a-time overview fetch), and it must be
pumped exactly when HOT's own pump runs. A collaborator would need that
private concurrency state published as public API; a subclass gets it
directly, and the production class keeps four empty lifecycle hooks instead
of any knowledge of COVERAGE.
"""

from collections import deque

from PyQt5 import QtCore

from .. import prefetch_policy as prefetch_rules
from ..multichannel_prefetch import HOT_METHODS, MultiChannelPrefetchController
from ..tile_types import TileRequest


# Above HOT_PRIORITY_BASE=5000, i.e. strictly LOWER priority than every HOT
# request (HOT's own priorities top out at HOT_PRIORITY_BASE + 3 for a
# 4-neighbour order).
COVERAGE_PRIORITY_BASE = 6000
# Physical cap, same meaning as HOT_INFLIGHT: a slot is released only when a
# terminal callback (ordinary or stale) arrives, never at cancellation time.
COVERAGE_INFLIGHT = 1
# Channels planned per batch, so a cancellation never has to discard a queue
# sized to the whole remaining channel list.
COVERAGE_BATCH_CHANNELS = 4


class CoverageMultiChannelPrefetchController(MultiChannelPrefetchController):
    """HOT (inherited, unchanged) plus COVERAGE."""

    # COVERAGE gets its own signal rather than reusing the inherited
    # `_tile_delivered`: the two consumers keep entirely separate in-flight
    # maps, generation counters and stats, and sharing one signal would
    # force every handler to first disambiguate HOT vs COVERAGE deliveries
    # from the same (token, generation) namespace.
    _coverage_tile_delivered = QtCore.pyqtSignal(int, object, object)

    def __init__(self, controller, scheduler, specs, grid,
                 coverage_inflight=COVERAGE_INFLIGHT, **kwargs):
        super().__init__(controller, scheduler, specs, grid, **kwargs)
        self.coverage_inflight = coverage_inflight

        self.stats.update({
            "coverage_batches": 0,
            "coverage_tiles_requested": 0,
            "coverage_tiles_completed": 0,
            "coverage_tiles_failed": 0,
            "coverage_cancelled": 0,
            "coverage_abandoned_finished": 0,
        })

        # COVERAGE state -- entirely separate bookkeeping from HOT's, which
        # stays in the base class.
        self._coverage_generation = 0
        self._coverage_full_order = []
        self._coverage_order_position = 0
        self._coverage_queue = deque()
        self._coverage_active_requests = {}
        self._coverage_request_serial = 0
        self._coverage_pumping = False
        # Tiles of the CURRENT batch still to be accounted for. Not a
        # capacity limit -- that is what the physical in-flight cap does
        # (see `_coverage_active_requests`) -- but the gate that keeps a
        # cancellation from having to discard a queue sized to the whole
        # remaining channel list.
        self._coverage_batch_remaining = 0

        self._coverage_tile_delivered.connect(self._on_coverage_tile_result,
                                              QtCore.Qt.QueuedConnection)

    # ── lifecycle hooks the base class calls ────────────────────────────
    #
    # `_stopped` remains the final line of defence in every handler below:
    # disconnecting a signal cannot un-queue a delivery that is already
    # posted, so a late callback must be harmless rather than impossible.

    def _on_stop(self):
        self.stats["coverage_cancelled"] += len(self._coverage_queue)
        self._coverage_queue.clear()
        self._coverage_full_order = []
        self._coverage_order_position = 0
        self._coverage_batch_remaining = 0
        self.scheduler.cancel_generation(self._coverage_generation)
        try:
            self._coverage_tile_delivered.disconnect(
                self._on_coverage_tile_result)
        except (TypeError, RuntimeError):
            pass

    def _after_settle(self, snapshot):
        self._start_coverage_plan(snapshot)

    def _after_abort(self):
        self._abort_coverage()

    def _after_hot_activity(self):
        self._pump_coverage()

    # ── COVERAGE (P3): every remaining channel's current viewport ────────

    def _tiles_cached_for_channel(self, channel, snapshot):
        """Whether every HOT-identity tile for `channel` is already cached.

        Unlike `is_channel_ready`, this deliberately does NOT require an
        overview record -- COVERAGE must not request overviews (HOT owns
        the one-at-a-time overview channel), so a COVERAGE channel can only
        ever be judged "already complete" (and so skipped from a fresh
        plan) by its tiles, never by an overview it was never asked to
        fetch.
        """
        spec = self._spec_by_channel.get(channel)
        if spec is None:
            return True
        cache = getattr(self.scheduler, "corrected_cache", None)
        if cache is None:
            return False
        for tx, ty in self._tiles(snapshot):
            for method, base_param in self._method_params(spec):
                key = self._make_key(snapshot, channel, tx, ty, method,
                                     base_param)
                if cache.get(key) is None:
                    return False
        return True

    def _compute_coverage_order(self, snapshot):
        """`prefetch_rules.coverage_order` minus the HOT neighbourhood minus completed
        channels -- reusing both policy functions verbatim, never
        reimplementing either."""
        center = self._index_by_channel.get(snapshot.channel)
        n = len(self.specs)
        if center is None:
            return []
        hot_idx = set(prefetch_rules.hot_order(center, n))
        order = []
        for idx in prefetch_rules.coverage_order(n, center):
            if idx in hot_idx:
                continue
            channel = self.specs[idx].channel
            if self._tiles_cached_for_channel(channel, snapshot):
                continue
            order.append(channel)
        return order

    def _abort_coverage(self):
        if self._coverage_queue:
            self.stats["coverage_cancelled"] += len(self._coverage_queue)
            self._coverage_queue.clear()
        self._coverage_full_order = []
        self._coverage_order_position = 0
        self._coverage_batch_remaining = 0

        # Physical in-flight accounting mirrors HOT exactly (see
        # `_abort_hot`'s comment): `_coverage_active_requests` is NOT
        # cleared here. A slot is released only by a terminal callback --
        # ordinary or the opt-in stale one -- never by cancellation, or the
        # physical concurrency could exceed `coverage_inflight`.
        old_generation = self._coverage_generation
        self._coverage_generation += 1
        self.scheduler.cancel_generation(old_generation)

    def _start_coverage_plan(self, snapshot):
        if self._stopped or not self._settled:
            return
        self._coverage_full_order = self._compute_coverage_order(snapshot)
        self._coverage_order_position = 0
        self._coverage_queue.clear()
        self._coverage_batch_remaining = 0
        self._plan_next_coverage_batch()
        self._pump_coverage()

    def _plan_next_coverage_batch(self):
        if (self._stopped or not self._settled):
            return
        # Never plan a new batch while the previous one still has queued or
        # in-flight (current-generation) work -- see `_coverage_batch_remaining`.
        if self._coverage_queue or self._coverage_batch_remaining > 0:
            return
        if self._coverage_order_position >= len(self._coverage_full_order):
            return

        start = self._coverage_order_position
        batch = self._coverage_full_order[start:start + COVERAGE_BATCH_CHANNELS]
        self._coverage_order_position = start + len(batch)
        if not batch:
            return
        self.stats["coverage_batches"] += 1

        snapshot = self._hot_snapshot
        generation = self._coverage_generation
        for channel in batch:
            spec = self._spec_by_channel[channel]
            for tx, ty in self._tiles(snapshot):
                for method, base_param in self._method_params(spec):
                    key = self._make_key(snapshot, channel, tx, ty, method,
                                         base_param)
                    self._coverage_queue.append(
                        (generation, COVERAGE_PRIORITY_BASE, key))
                    self._coverage_batch_remaining += 1

    def _pump_coverage(self):
        if (self._stopped or not self._settled
                or self._coverage_pumping):
            return
        # Strict HOT priority: COVERAGE issues a request only when HOT has
        # nothing queued AND nothing in flight (see class docstring for why
        # the 3:1 interleave is not implemented here).
        if not self._hot_idle():
            return
        self._coverage_pumping = True
        try:
            while (self._coverage_queue
                   and len(self._coverage_active_requests) < self.coverage_inflight):
                generation, priority, key = self._coverage_queue.popleft()
                if generation != self._coverage_generation:
                    continue
                token = self._coverage_request_serial
                self._coverage_request_serial += 1
                self._coverage_active_requests[token] = generation
                # Same reasoning as HOT's `notify_on_stale_completion`: the
                # physical cap can only be metered with a terminal callback.
                request = TileRequest(key=key, generation=generation,
                                      priority=priority,
                                      notify_on_stale_completion=True)
                self.stats["coverage_tiles_requested"] += 1
                callback = lambda result, token=token, generation=generation: \
                    self._coverage_tile_delivered.emit(token, generation, result)
                try:
                    self.scheduler.request(request, callback)
                except Exception:
                    # The slot is not the only thing to release. A request
                    # that never reached the scheduler will never deliver a
                    # callback, so its share of the batch has to be settled
                    # HERE -- otherwise the batch counter never reaches zero,
                    # the next batch is never planned, and COVERAGE stops for
                    # good while looking idle: queue empty, nothing in
                    # flight, batch_remaining stuck above zero. A benchmark
                    # would report that state as "drained".
                    self._coverage_active_requests.pop(token, None)
                    self.stats["coverage_cancelled"] += 1
                    self._settle_coverage_batch_slot()
        finally:
            self._coverage_pumping = False

    def _on_coverage_tile_result(self, token, generation, result):
        if self._coverage_active_requests.pop(token, None) is None:
            return
        if self._stopped or generation != self._coverage_generation:
            # A terminal callback for an ABANDONED generation -- see
            # `_on_tile_result`'s identical reasoning. The slot is free (the
            # pop above did that); a newer plan may be waiting on exactly
            # that capacity.
            self.stats["coverage_abandoned_finished"] += 1
            if not self._stopped and self._settled:
                self._pump_coverage()
            return

        error = getattr(result, "error", None)
        if error == "cancelled":
            self.stats["coverage_cancelled"] += 1
        elif error is not None or getattr(result, "pixels", None) is None:
            self.stats["coverage_tiles_failed"] += 1
        else:
            self.stats["coverage_tiles_completed"] += 1

        self._settle_coverage_batch_slot()
        self._pump_coverage()

    def _settle_coverage_batch_slot(self):
        """Account for one tile of the CURRENT batch being finished with,
        however it ended -- delivered, failed, or never accepted by the
        scheduler at all. Plans the next batch once the current one is fully
        accounted for.

        Deliberately NOT called for a terminal callback belonging to an
        abandoned generation: that tile was part of an older batch, and the
        abort already zeroed that batch's accounting, so decrementing here
        would steal a tile from the batch now in flight and plan the next
        one early. `_on_coverage_tile_result` returns before reaching this
        point in that case; it still frees the in-flight slot, which is
        physical and generation-independent.
        """
        self._coverage_batch_remaining -= 1
        if self._coverage_batch_remaining <= 0:
            self._coverage_batch_remaining = 0
            self._plan_next_coverage_batch()
