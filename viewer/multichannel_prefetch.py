"""Measurement-free, cache-only HOT prefetch for neighbouring channels."""

from __future__ import annotations

from collections import deque

from PyQt5 import QtCore

# The ordering rules are called THROUGH the module, never bound at import
# time: `prefetch_rules.hot_order(...)`, not `hot_order(...)`. That is what
# lets a test monkeypatch `prefetch_policy.hot_order` and observe the
# runtime's request order change -- i.e. prove this controller consults the
# single rule source instead of carrying its own copy of the same logic. A
# bound `from ... import hot_order` would silently keep working against a
# stale function object and make that proof impossible.
from . import prefetch_policy as prefetch_rules
from .prefetch_policy import ChannelCorrectionSpec
from .tile_types import CorrectionKey, TileAddress, TileRequest, effective_param


SETTLE_CONFIRM_MS = 120
HOT_INFLIGHT = 2
HOT_PRIORITY_BASE = 5000
HOT_METHODS = ("tophat", "cucim")


class MultiChannelPrefetchController(QtCore.QObject):
    """Prepare neighbouring channels after the display has really settled.

    This object is deliberately a cache-only consumer.  It never touches an
    ``ExploreView`` or either of the view's pools: the scheduler writes
    completed correction results to ``corrected_cache``.

    HOT ONLY (P2, the i-1/i+1/i-2/i+2 neighbourhood given by
    ``prefetch_rules.hot_order``). COVERAGE (P3, every remaining channel)
    is not part of the production path: it lives in
    ``viewer.experimental.coverage_prefetch``, which subclasses this class
    and fills in the four lifecycle hooks at the bottom of this file.
    Instantiating THAT class is what enables it -- there is no flag here to
    get wrong, and nothing in ``viewer`` (outside ``experimental``), ``ui``
    or ``main`` may import it.
    """

    # `TileScheduler` fires callbacks on a COMPUTE WORKER thread. Every
    # other path in this viewer marshals such a callback to the GUI thread
    # through a queued signal before touching state, and this one must too:
    # `_on_tile_result` mutates the in-flight map, the stats and the queue,
    # and re-enters `scheduler.request`. The queued hop can only DELAY a
    # slot release, never advance it, so the physical cap stays conservative.
    _tile_delivered = QtCore.pyqtSignal(int, object, object)

    def __init__(self, controller, scheduler, specs, grid,
                 settle_confirm_ms=SETTLE_CONFIRM_MS,
                 hot_inflight=HOT_INFLIGHT,
                 parent=None):
        super().__init__(parent)
        self.controller = controller
        self.scheduler = scheduler
        self.specs = tuple(specs)
        self.grid = grid
        self.settle_confirm_ms = settle_confirm_ms
        self.hot_inflight = hot_inflight

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

        self._tile_delivered.connect(self._on_tile_result,
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
        """Disconnect from the host and cancel queued HOT work.

        Idempotent. A subclass extends this through `_on_stop()`, which runs
        after HOT's own generation has been cancelled and before the host
        signals are disconnected.
        """
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
        self._on_stop()

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
            order = prefetch_rules.hot_order(center, len(self.specs))
            self._overview_plan = [
                (self.specs[index].channel, hot_index)
                for hot_index, index in enumerate(order)
            ]
        self._overview_position = 0
        self._pump_overviews()
        self._after_settle(snapshot)

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

        # Both call sites (`_on_interaction`, `_on_selection_context_changed`)
        # reach this method, so it is the single place an experiment needs
        # to learn that HOT was abandoned.
        self._after_abort()

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
        # busy); an experiment gated on it gets a chance to react every time
        # HOT's pump runs.
        self._after_hot_activity()

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
                self._after_hot_activity()
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

    # ── lifecycle hooks (see `viewer.experimental.coverage_prefetch`) ────
    #
    # Deliberately four empty methods, not a plugin system: they are the
    # exact points where the COVERAGE experiment needs to act, and nothing
    # else is expected to subscribe. The base class knows no more than
    # that something MAY care about these four moments.

    def _on_stop(self):
        """`stop()` has cancelled HOT and is about to disconnect."""

    def _after_settle(self, snapshot):
        """A settle was confirmed and HOT's plan for it has been made."""

    def _after_abort(self):
        """HOT was aborted (interaction, or a selection-context change)."""

    def _after_hot_activity(self):
        """HOT's queue / in-flight state may just have changed."""

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
