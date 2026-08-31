"""Measurement-free, cache-only HOT prefetch for neighbouring channels."""

from __future__ import annotations

from collections import deque

from PyQt5 import QtCore

from .prefetch_policy import ChannelCorrectionSpec, _coverage_order, _hot_order
from .tile_types import CorrectionKey, TileAddress, TileRequest, effective_param


SETTLE_CONFIRM_MS = 120
HOT_INFLIGHT = 2
HOT_PRIORITY_BASE = 5000
HOT_METHODS = ("tophat", "cucim")

# ── COVERAGE (P3) ────────────────────────────────────────────────────────────
#
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


class MultiChannelPrefetchController(QtCore.QObject):
    """Prepare neighbouring channels after the display has really settled.

    This object is deliberately a cache-only consumer.  It never touches an
    ``ExploreView`` or either of the view's pools: the scheduler writes
    completed correction results to ``corrected_cache``.

    In addition to HOT (P2, the i-1/i+1/i-2/i+2 neighbourhood), this object
    also runs COVERAGE (P3): every remaining channel's current viewport,
    walked from both ends of the channel list toward the middle via
    ``_coverage_order`` (reused from ``prefetch_policy``, not reimplemented
    here), planned in batches of ``COVERAGE_BATCH_CHANNELS`` channels.

    NOTE ON THE INTERLEAVE RATIO: ``prefetch_policy``'s design document
    describes a deterministic 3:1 HOT:COVERAGE interleave
    (``HOT_PER_COVERAGE``). That is deliberately NOT implemented here. The
    operative instruction for this round is strict HOT priority with a
    physical COVERAGE cap of 1 -- COVERAGE issues a request only when HOT
    has nothing queued and nothing in flight (including its one-at-a-time
    overview fetch) -- which is simpler and strictly safer than interleaving
    the two. The ratio can be revisited once this is measured.
    """

    # `TileScheduler` fires callbacks on a COMPUTE WORKER thread. Every
    # other path in this viewer marshals such a callback to the GUI thread
    # through a queued signal before touching state, and this one must too:
    # `_on_tile_result` mutates the in-flight map, the stats and the queue,
    # and re-enters `scheduler.request`. The queued hop can only DELAY a
    # slot release, never advance it, so the physical cap stays conservative.
    _tile_delivered = QtCore.pyqtSignal(int, object, object)

    # COVERAGE gets its own signal rather than reusing `_tile_delivered`:
    # the two consumers keep entirely separate in-flight maps, generation
    # counters and stats, and sharing one signal would force every handler
    # to first disambiguate HOT vs COVERAGE deliveries from the same
    # (token, generation) namespace. Two signals keep that separation at the
    # Qt layer instead of re-deriving it in a shared slot.
    _coverage_tile_delivered = QtCore.pyqtSignal(int, object, object)

    def __init__(self, controller, scheduler, specs, grid,
                 settle_confirm_ms=SETTLE_CONFIRM_MS,
                 hot_inflight=HOT_INFLIGHT,
                 # OFF by default. COVERAGE only pays off if what it
                 # prepares survives in the corrected cache, and at the
                 # 512MB the demo still uses, a long dwell on the real
                 # 57-channel slide evicted 1305 of the 1817 tiles it
                 # produced -- most of the background work is thrown away
                 # again. It is enabled explicitly (`--coverage`) until a
                 # cache budget is chosen for the host that mounts it.
                 coverage: bool = False,
                 coverage_inflight=COVERAGE_INFLIGHT,
                 parent=None):
        super().__init__(parent)
        self.controller = controller
        self.scheduler = scheduler
        self.specs = tuple(specs)
        self.grid = grid
        self.settle_confirm_ms = settle_confirm_ms
        self.hot_inflight = hot_inflight
        self.coverage_enabled = coverage
        self.coverage_inflight = coverage_inflight

        self.stats = {
            "hot_batches": 0,
            "hot_tiles_requested": 0,
            "hot_tiles_completed": 0,
            "hot_cancelled": 0,
            "overviews_requested": 0,
            "overviews_failed": 0,
            "hot_tiles_failed": 0,
            "settle_confirmations": 0,
            "settle_aborted": 0,
            "hot_abandoned_finished": 0,
            "coverage_batches": 0,
            "coverage_tiles_requested": 0,
            "coverage_tiles_completed": 0,
            "coverage_tiles_failed": 0,
            "coverage_cancelled": 0,
            "coverage_abandoned_finished": 0,
        }

        self._spec_by_channel = {spec.channel: spec for spec in self.specs}
        self._index_by_channel = {
            spec.channel: index for index, spec in enumerate(self.specs)
        }
        self._latest_snapshot = None
        self._center_channel = None

        self._confirm_timer = QtCore.QTimer(self)
        self._confirm_timer.setSingleShot(True)
        self._confirm_timer.setInterval(int(settle_confirm_ms))
        self._confirm_timer.timeout.connect(self._confirm_settle)
        self._confirm_pending = False
        self._confirm_snapshot = None
        self._quiet_serial = 0
        self._confirm_serial = None

        self._settled = False
        self._hot_generation = 0
        self._hot_snapshot = None
        self._overview_plan = []
        self._overview_position = 0
        # (generation, source, channel).  This remains set after an
        # interaction until the already-started overview read reports back;
        # that prevents a new generation from starting a second read.
        self._overview_inflight = None
        self._tile_queue = deque()
        self._active_requests = {}
        self._request_serial = 0
        self._pumping_tiles = False
        self._stopped = False

        # COVERAGE state -- entirely separate bookkeeping from HOT's above,
        # on purpose (see class docstring).
        self._coverage_generation = 0
        self._coverage_full_order = []
        self._coverage_order_position = 0
        self._coverage_queue = deque()
        self._coverage_active_requests = {}
        self._coverage_request_serial = 0
        self._coverage_pumping = False
        # Tiles queued or in flight for the CURRENT batch, in the CURRENT
        # generation only. Reaching zero is what triggers planning the next
        # batch -- it deliberately does not span generations the way the
        # physical in-flight cap does (see `_coverage_active_requests`),
        # because an abandoned generation's still-running task must not
        # block planning fresh work for a new one.
        self._coverage_batch_remaining = 0

        self._tile_delivered.connect(self._on_tile_result,
                                     QtCore.Qt.QueuedConnection)
        self._coverage_tile_delivered.connect(self._on_coverage_tile_result,
                                              QtCore.Qt.QueuedConnection)
        self.controller.interaction_event.connect(self._on_interaction)
        self.controller.gesture_quiet.connect(self._on_gesture_quiet)
        self.controller.selection_context_changed.connect(
            self._on_selection_context_changed)
        self.controller.overview_prepared.connect(self._on_overview_prepared)

    # ── public API ──────────────────────────────────────────────────────

    def is_channel_ready(self, channel, snapshot) -> bool:
        """Return whether ``channel`` has its exact HOT identity cached."""
        spec = self._spec_by_channel.get(channel)
        if spec is None:
            return False

        # NOT `snapshot.level`. That is the DISPLAY level (L0 while zoomed
        # in); the overview record lives at the host's own overview level
        # (L2 on the real slides). Asking for the display level made this
        # never match, so a channel could never be reported ready -- and it
        # made the caller below call `prepare_overview_async` for a record
        # that was already cached, which returns without emitting, leaving
        # the one-at-a-time overview gate stuck for good. Passing None lets
        # the host resolve its own level.
        #
        # This overview requirement is why a COVERAGE-only channel (tiles
        # cached, but COVERAGE never touches overviews -- HOT owns the
        # one-at-a-time overview channel) is correctly never reported ready
        # here.
        if not self.controller.has_overview_record(
                channel, source=snapshot.source):
            return False

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

    def stop(self):
        """Disconnect from the host and cancel queued HOT/COVERAGE work."""
        if self._stopped:
            return
        self._stopped = True
        self._confirm_timer.stop()
        self._confirm_pending = False
        self._quiet_serial += 1
        self.stats["hot_cancelled"] += len(self._tile_queue)
        self._tile_queue.clear()
        self._settled = False
        self._overview_plan.clear()
        self._overview_position = 0
        self.scheduler.cancel_generation(self._hot_generation)

        if self.coverage_enabled:
            self.stats["coverage_cancelled"] += len(self._coverage_queue)
            self._coverage_queue.clear()
            self._coverage_full_order = []
            self._coverage_order_position = 0
            self._coverage_batch_remaining = 0
            self.scheduler.cancel_generation(self._coverage_generation)

        connections = (
            (self.controller.interaction_event, self._on_interaction),
            (self.controller.gesture_quiet, self._on_gesture_quiet),
            (self.controller.selection_context_changed,
             self._on_selection_context_changed),
            (self.controller.overview_prepared, self._on_overview_prepared),
        )
        for signal, slot in connections:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        try:
            self._tile_delivered.disconnect(self._on_tile_result)
        except (TypeError, RuntimeError):
            pass
        try:
            self._coverage_tile_delivered.disconnect(
                self._on_coverage_tile_result)
        except (TypeError, RuntimeError):
            pass

    # ── signal handlers ─────────────────────────────────────────────────

    def _on_gesture_quiet(self, snapshot):
        if self._stopped:
            return
        self._latest_snapshot = snapshot
        self._center_channel = snapshot.channel
        self._quiet_serial += 1
        self._confirm_serial = self._quiet_serial
        self._confirm_snapshot = snapshot
        self._confirm_pending = True
        self._confirm_timer.start(int(self.settle_confirm_ms))

    def _on_interaction(self, _kind, snapshot):
        if self._stopped:
            return
        self._latest_snapshot = snapshot
        self._center_channel = snapshot.channel
        self._abort_hot()

    def _on_selection_context_changed(self, snapshot):
        if self._stopped:
            return
        previous = self._latest_snapshot
        self._latest_snapshot = snapshot
        self._center_channel = snapshot.channel
        if previous is None:
            return
        if (previous.channel != snapshot.channel
                or not self._same_correction_context(previous, snapshot)):
            self._abort_hot()

    def _confirm_settle(self):
        if (self._stopped or not self._confirm_pending
                or self._confirm_serial != self._quiet_serial):
            return
        snapshot = self._confirm_snapshot
        self._confirm_pending = False
        self._confirm_serial = None
        self._confirm_snapshot = None
        self._settled = True
        self._hot_snapshot = snapshot
        self.stats["settle_confirmations"] += 1
        self.stats["hot_batches"] += 1

        center = self._index_by_channel.get(snapshot.channel)
        if center is None:
            self._overview_plan = []
        else:
            order = _hot_order(center, len(self.specs))
            self._overview_plan = [
                (self.specs[index].channel, hot_index)
                for hot_index, index in enumerate(order)
            ]
        self._overview_position = 0
        self._pump_overviews()
        self._start_coverage_plan(snapshot)

    # ── generation / overview sequencing ────────────────────────────────

    def _abort_hot(self):
        if self._confirm_pending:
            self.stats["settle_aborted"] += 1
        self._confirm_pending = False
        self._confirm_snapshot = None
        self._confirm_serial = None
        self._confirm_timer.stop()
        self._quiet_serial += 1

        self._settled = False
        self._hot_snapshot = None
        self._overview_plan.clear()
        self._overview_position = 0
        if self._tile_queue:
            self.stats["hot_cancelled"] += len(self._tile_queue)
            self._tile_queue.clear()

        # `_active_requests` is deliberately NOT cleared here. Cancelling a
        # generation stops only work that has not STARTED; anything already
        # running keeps consuming the same I/O and GPU. Releasing its slot
        # now would let the abandoned task and a freshly issued one run at
        # the same time, so the physical concurrency could reach
        # 2 * hot_inflight instead of hot_inflight -- the opposite of what
        # the cap is for.
        #
        # An earlier revision did exactly that, to work around the fact that
        # `TileScheduler._deliver` does not call back a stale waiter at all
        # (which otherwise leaks the slot forever). The real fix is the
        # scheduler contract: HOT's requests set
        # `notify_on_stale_completion`, so an abandoned task still delivers
        # ONE terminal callback when it physically finishes, and the slot is
        # released there. The count therefore spans generations on purpose --
        # it meters physical work, not the current plan's work.
        old_generation = self._hot_generation
        self._hot_generation += 1
        self.scheduler.cancel_generation(old_generation)

        # Any interaction that aborts HOT aborts COVERAGE the same way --
        # both existing call sites (`_on_interaction`,
        # `_on_selection_context_changed`) reach this method already, so
        # this is the one place that needs to know about COVERAGE too.
        if self.coverage_enabled:
            self._abort_coverage()

    def _pump_overviews(self):
        if (self._stopped or not self._settled
                or not self._overview_plan or self._overview_inflight is not None):
            return
        snapshot = self._hot_snapshot
        generation = self._hot_generation
        while self._overview_position < len(self._overview_plan):
            channel, hot_index = self._overview_plan[self._overview_position]
            # Host's overview level, not the display level -- see
            # `is_channel_ready`.
            if self.controller.has_overview_record(
                    channel, source=snapshot.source):
                self._overview_position += 1
                self._queue_channel_tiles(snapshot, channel, hot_index,
                                          generation)
                continue

            self._overview_inflight = (generation, snapshot.source, channel)
            self.stats["overviews_requested"] += 1
            self.controller.prepare_overview_async(channel)
            return
        self._pump_tiles()

    def _on_overview_prepared(self, source, channel, _level, ok):
        waiting = self._overview_inflight
        if waiting is None or source != waiting[1] or channel != waiting[2]:
            return
        self._overview_inflight = None

        if not ok:
            # A channel whose overview could not be read is not ready and
            # never will be from this plan. Queueing its corrected tiles
            # anyway would spend the budget on a channel that cannot be
            # switched to, and would let `is_channel_ready` be asked about
            # a channel whose display range is unknown.
            self.stats["overviews_failed"] += 1
            if not self._stopped and self._settled and waiting[0] == self._hot_generation:
                self._overview_position += 1
            self._pump_overviews()
            return

        if (self._stopped or not self._settled
                or waiting[0] != self._hot_generation):
            # A stale read is still useful to the host's overview cache.  It
            # must not advance the superseded HOT plan, but it does release
            # the one-overview-at-a-time gate for the current plan.
            self._pump_overviews()
            return

        self._overview_position += 1
        snapshot = self._hot_snapshot
        channel_index = self._index_by_channel.get(channel)
        if channel_index is not None:
            hot_index = self._overview_plan[self._overview_position - 1][1]
            self._queue_channel_tiles(snapshot, channel, hot_index,
                                      waiting[0])
        self._pump_tiles()
        self._pump_overviews()

    # ── corrected tile requests ──────────────────────────────────────────

    def _queue_channel_tiles(self, snapshot, channel, hot_index, generation):
        spec = self._spec_by_channel[channel]
        for tx, ty in self._tiles(snapshot):
            for method, base_param in self._method_params(spec):
                key = self._make_key(snapshot, channel, tx, ty, method,
                                     base_param)
                self._tile_queue.append((generation,
                                         HOT_PRIORITY_BASE + hot_index, key))

    def _pump_tiles(self):
        if self._stopped or not self._settled or self._pumping_tiles:
            return
        self._pumping_tiles = True
        try:
            while (self._tile_queue
                   and len(self._active_requests) < self.hot_inflight):
                generation, priority, key = self._tile_queue.popleft()
                if generation != self._hot_generation:
                    continue
                token = self._request_serial
                self._request_serial += 1
                self._active_requests[token] = generation
                # `notify_on_stale_completion` is what makes the in-flight
                # accounting PHYSICAL rather than notional: cancelling a
                # generation does not stop work that has already started, so
                # without a terminal callback this object can never learn
                # that the work is over.
                request = TileRequest(key=key, generation=generation,
                                      priority=priority,
                                      notify_on_stale_completion=True)
                self.stats["hot_tiles_requested"] += 1
                # Worker thread: emit only. The real handling runs on the
                # GUI thread (see `_tile_delivered`).
                callback = lambda result, token=token, generation=generation: \
                    self._tile_delivered.emit(token, generation, result)
                try:
                    self.scheduler.request(request, callback)
                except Exception:
                    self._active_requests.pop(token, None)
                    self.stats["hot_cancelled"] += 1
        finally:
            self._pumping_tiles = False
        # HOT's queue/in-flight state may just have gone idle (or become
        # busy) -- either way COVERAGE's strict-priority gate depends on it,
        # so give it a chance to react every time HOT's pump runs.
        self._pump_coverage()

    def _on_tile_result(self, token, generation, result):
        if self._active_requests.pop(token, None) is None:
            return
        if self._stopped or generation != self._hot_generation:
            # A terminal callback for an ABANDONED generation. Its result (if
            # any) is already in the scheduler's cache; HOT never consumes
            # the payload. What matters here is that the physical work is now
            # over, so its slot is genuinely free -- the pop above did that --
            # and a newer plan may be waiting on exactly that capacity.
            self.stats["hot_abandoned_finished"] += 1
            if not self._stopped and self._settled:
                self._pump_tiles()
            else:
                self._pump_coverage()
            return

        error = getattr(result, "error", None)
        if error == "cancelled":
            self.stats["hot_cancelled"] += 1
        elif error is not None or getattr(result, "pixels", None) is None:
            # Any other error is a FAILURE, not a completion. Counting it as
            # done made a benchmark's "120 of 120 completed" meaningless.
            self.stats["hot_tiles_failed"] += 1
        else:
            self.stats["hot_tiles_completed"] += 1
        self._pump_tiles()

    # ── identity helpers ─────────────────────────────────────────────────

    @staticmethod
    def _tiles(snapshot):
        return tuple(sorted(snapshot.visible_tiles))

    @staticmethod
    def _same_correction_context(left, right):
        return (
            left.source == right.source
            and left.channel == right.channel
            and left.method == right.method
            and left.params == right.params
            and left.level == right.level
            and left.quality == right.quality
            and left.algorithm_version == right.algorithm_version
        )

    @staticmethod
    def _method_params(spec):
        values = {
            "tophat": spec.tophat_radius,
            "cucim": spec.cucim_sigma,
        }
        return tuple((method, values[method]) for method in HOT_METHODS)

    def _level_downsample(self, level):
        # ExploreController's provider and level_downsample are public.  The
        # provider path is the same scalar path used by the foreground key
        # builder; no controller-private state is consulted here.
        return self.controller.provider.level_downsample(level)

    def _make_key(self, snapshot, channel, tx, ty, method, base_param):
        downsample = self._level_downsample(snapshot.level)
        param = effective_param(base_param, snapshot.level, downsample)
        return CorrectionKey(
            source=snapshot.source,
            channel=channel,
            tile=TileAddress(grid=self.grid, level=snapshot.level,
                             tx=tx, ty=ty),
            method=method,
            params=(param,),
            algorithm_version=snapshot.algorithm_version,
            quality=snapshot.quality,
        )

    # ── COVERAGE (P3): every remaining channel's current viewport ────────

    def _hot_idle(self):
        """True iff HOT has nothing queued and nothing in flight.

        Includes the one-at-a-time overview fetch (`_overview_inflight`):
        that fetch is part of HOT's own settle sequence, so a COVERAGE
        request issued while it is outstanding would still be competing
        with HOT for the same I/O, which is exactly what strict priority is
        meant to prevent.
        """
        return (not self._tile_queue and not self._active_requests
                and self._overview_inflight is None)

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
        """`_coverage_order` minus the HOT neighbourhood minus completed
        channels -- reusing both policy functions verbatim, never
        reimplementing either."""
        center = self._index_by_channel.get(snapshot.channel)
        n = len(self.specs)
        if center is None:
            return []
        hot_idx = set(_hot_order(center, n))
        order = []
        for idx in _coverage_order(n, center):
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
        if not self.coverage_enabled or self._stopped or not self._settled:
            return
        self._coverage_full_order = self._compute_coverage_order(snapshot)
        self._coverage_order_position = 0
        self._coverage_queue.clear()
        self._coverage_batch_remaining = 0
        self._plan_next_coverage_batch()
        self._pump_coverage()

    def _plan_next_coverage_batch(self):
        if (not self.coverage_enabled or self._stopped or not self._settled):
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
        if (not self.coverage_enabled or self._stopped or not self._settled
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
        """Account for one tile of the current batch being finished with,
        however it ended -- delivered, failed, abandoned, or never accepted
        by the scheduler at all. Plans the next batch when the current one
        is fully accounted for."""
        self._coverage_batch_remaining -= 1
        if self._coverage_batch_remaining <= 0:
            self._coverage_batch_remaining = 0
            self._plan_next_coverage_batch()
