"""G2 test-only adapter from public Step1 tile supply to G1 GPU snapshots.

The binding consumes only injected public provider/scheduler/controller ports and
caller-built Display/Viewport snapshots.  It is deliberately not imported by a
production mount.  It owns no scheduler, raw cache, source authority, camera,
or display state: its bounded state is only the current source revision's
complete coarse transaction and the fine planes that still cover the
current viewport.

TWO TIERS, TWO DIFFERENT RULES (G3.2a).

* COMPLETE COARSE IS ATOMIC. A channel's coarsest-level transaction is
  published only when its last tile has landed, so a newly enabled channel
  never appears in the middle of the picture and grows outwards.
* FINE IS INCREMENTAL AND IS KEPT ACROSS CAMERA MOVES. A camera move does
  not throw away the fine planes that still cover where the camera now is:
  they are re-submitted straight away, only the tiles that are genuinely
  missing are asked for, and each one replaces its own world rectangle the
  moment it lands. Waiting for a whole viewport's transaction before
  showing any of it is what made every pan and zoom fall back to the
  blurriest level until the gesture stopped.

Retention is bounded, not a cache: a plane is dropped as soon as it stops
covering the viewport or its source moves, and what is left is trimmed to
the caller's own per-channel fine budget, target level first. There is no
second scheduler, raw cache, LRU authority or repository here.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Hashable, Mapping, Optional, Sequence, Set, Tuple

import numpy as np
from PyQt5 import QtCore

from ..viewer.tile_types import RawKey, TileAddress, TileRequest
from ..utils import perf_trace
from .step1_gpu_layer import (
    MODE_FUSION,
    MODE_OVERLAY,
    ChannelSource,
    DisplaySnapshot,
    RawPlane,
    SourceDescriptor,
    Step1GpuLayerError,
    ViewportSnapshot,
)

#: Block A9-O2: the frame slot landed results are published on (~60 Hz).
PUBLISH_FRAME_MS = 16.0


#: The whole G2.1 fairness policy, expressed only in the priority numbers
#: handed to the EXISTING public ``TileScheduler.request``.  That scheduler
#: serves its RawKey ready-queue as a min-heap on ``priority`` with FIFO
#: inside one priority tier, so a smaller number is served earlier.  Nothing
#: in ``viewer/scheduler.py`` is changed, no second queue or worker pool is
#: created, and no private scheduler state is read.
#:
#: In plain words: visible fine normally overtakes a channel's full coarse
#: preparation.  While ANY channel's complete-coarse transaction is still in
#: flight, fine requests issued FROM THEN ON are enqueued behind coarse
#: instead of in front of it, so continuous panning and zooming can no
#: longer keep pushing a newly enabled channel's coarse back forever.  Fine
#: work that was already queued before that keeps the priority it was given
#: and still finishes first.  The instant the last coarse transaction
#: completes, fine goes back to the foreground tier.
PRIORITY_FINE_FOREGROUND = 0
PRIORITY_COARSE = 100
PRIORITY_FINE_DEFERRED = 200


@dataclass
class _Plan:
    tier: str
    channel: str
    source: Hashable
    revision: int
    generation: Hashable
    expected: Set[RawKey]
    viewport_epoch: Optional[int] = None
    priority: int = PRIORITY_COARSE
    planes: Dict[RawKey, RawPlane] = field(default_factory=dict)
    failed: bool = False

    @property
    def complete(self) -> bool:
        return not self.failed and set(self.planes) == self.expected


@dataclass(frozen=True)
class BindingBudgets:
    """Explicit G2 retention/request limits; no production default is implied."""

    max_coarse_tiles_per_channel: int
    max_coarse_plane_bytes_per_channel: int
    max_fine_tiles_per_viewport: int
    max_fine_plane_bytes_per_channel: int
    motion_interval_ms: int = 33
    #: Block A9 S2b: the GPU layer's total raw-texture budget. When set,
    #: every plan is ADMITTED against it (complete coarse of every active
    #: channel + the admitted target + carried stand-ins), so a submission
    #: never exceeds it. None keeps the per-channel limits alone.
    max_raw_texture_bytes: Optional[int] = None


@dataclass(frozen=True)
class _Admission:
    """Block A9 S2b: one viewport's admitted fine target (immutable).

    `ideal` is the level the controller asked for; `level` the finest level
    from there towards coarse whose fine, for EVERY active channel, fits
    the per-channel limits and -- together with the complete coarse of
    every active channel -- the total raw-texture budget. `keys` is that
    level's visible tile set per channel (empty: coarse only), `carry` what
    each channel may keep of stand-ins carried from another zoom."""

    token: Hashable
    ideal: int
    level: int
    keys: Mapping[str, frozenset]
    carry: Mapping[str, int]
    #: no level's fine fits at all: the complete coarse alone is drawn
    coarse_only: bool = False

    @property
    def limited(self) -> bool:
        return self.level != self.ideal or self.coarse_only


class Step1GpuBinding(QtCore.QObject):
    """Public-port-only, test-reachable G2 source-supply adapter."""

    _tile_result_received = QtCore.pyqtSignal(object)

    def __init__(self, *, provider, scheduler, controller, layer,
                 build_display_snapshot: Callable[[], DisplaySnapshot],
                 build_viewport_snapshot: Callable[[], ViewportSnapshot],
                 budgets: BindingBudgets, dispose_layer: bool = False,
                 parent: Optional[QtCore.QObject] = None):
        super().__init__(parent)
        self.provider = provider
        self.scheduler = scheduler
        self.controller = controller
        self.layer = layer
        self._build_display_snapshot = build_display_snapshot
        self._build_viewport_snapshot = build_viewport_snapshot
        self.budgets = budgets
        self._dispose_layer = bool(dispose_layer)
        self._disposed = False
        #: Block 2a: a viewer that is not on screen asks for nothing. While
        #: paused no new read is issued; a read already under way may land
        #: and is kept, but it starts no next round. A source replacement
        #: that arrives meanwhile waits for `resume()`.
        self._paused = False
        self._source_pending = False
        self._revision = 0
        self._source = None
        #: Block A9 S2c: the identity tile KEYS carry -- the provider's
        #: native namespace when it has one (uint8/uint16 raw tiles), else
        #: the display source itself. Snapshots are still compared with
        #: `_source`; keys, retention and deliveries use this.
        self._key_source = None
        self._native_pixel_bytes = 4
        self._serial = 0
        self._latest_snapshot = None
        self._fine_epoch = 0
        self._coarse: Dict[str, _Plan] = {}
        self._fine: Dict[str, _Plan] = {}
        self._published_coarse: Dict[str, Tuple[RawPlane, ...]] = {}
        #: The fine planes currently on screen, per channel, keyed by their
        #: own RawKey. Keyed rather than a frozen tuple because a camera
        #: move keeps the ones that still cover the viewport and asks only
        #: for the rest -- and because planes of DIFFERENT levels may be
        #: resident at once while a zoom settles.
        self._published_fine: Dict[str, Dict[RawKey, RawPlane]] = {}
        self._fine_budget_refused: Set[str] = set()
        #: Block A9 S2b: the last admission (see `_Admission`) and an
        #: optional owner hook called when "resolution limited" changes.
        self._admission: Optional[_Admission] = None
        #: Block A9 S2b (delegated decision 2026-10-09): how many active
        #: channels' base layers alone exceed the total budget, 0 when they
        #: fit. Then nothing is submitted and the last frame stays.
        self._base_overflow = 0
        self.on_status_changed: Optional[Callable[[], None]] = None
        #: The channels the fine tier is currently planned for. One small
        #: tuple, so an untick can release that channel's planes at once
        #: instead of waiting for the next camera move -- and so a colour,
        #: a weight or an Intensity change, which leaves this set alone,
        #: still asks the scheduler for nothing.
        self._active_fine_channels: Tuple[str, ...] = ()
        #: `(source, interaction epoch)` of the viewport last PLANNED, kept
        #: only to skip planning the very same one twice. Immutable, local,
        #: read for one comparison and nothing else; it is not a token
        #: system, a queue or a second camera authority.
        self._consumed_viewport = None
        #: The channels the LAST published descriptor actually drew. One
        #: frozen set, read to answer a single question: is this channel
        #: appearing for the first time, and therefore owed its sharp
        #: picture in one step? It is not a lifecycle, a queue or a token.
        self._shown_channels: Set[str] = set()
        self._retained_fine_last_epoch = 0
        self._requested_fine_last_epoch = 0
        self._generations: Set[Hashable] = set()
        self._unavailable: Dict[str, str] = {}
        self._last_error: Optional[str] = None
        self._descriptor_history = []
        self._request_count = 0
        self._fine_requests_by_priority: Dict[int, int] = {}
        self._last_fine_priority: Optional[int] = None
        self._accepted_count = 0
        self._rejected_late_count = 0
        self._gui_thread = QtCore.QThread.currentThread()
        self._tile_result_received.connect(self._accept_result, QtCore.Qt.QueuedConnection)
        self._motion_timer = QtCore.QTimer(self)
        self._motion_timer.setSingleShot(True)
        self._motion_timer.timeout.connect(self._flush_motion)
        # Block A9-O2 (Odon's update loop): tiles that land are applied at
        # once but PUBLISHED at most once per frame -- a burst of 50 results
        # is one submit, not 50 back-to-back ones on the GUI thread.
        self._publish_due = False
        self._turn_published = False
        self._last_publish_at = -1.0
        self._publish_timer = QtCore.QTimer(self)
        self._publish_timer.setSingleShot(True)
        self._publish_timer.timeout.connect(self._flush_publish)
        self._connect_controller()

    # Public lifecycle ----------------------------------------------------

    def source_changed(self) -> None:
        """Explicit owner lifecycle fence after a public source replacement."""
        if self._disposed:
            return
        if self._paused:
            self._source_pending = True
            return
        self._cancel_all()
        self._revision += 1
        self._fine_epoch = 0
        self._coarse.clear()
        self._fine.clear()
        self._published_coarse.clear()
        self._published_fine.clear()
        self._fine_budget_refused.clear()
        self._admission = None
        self._base_overflow = 0
        self._active_fine_channels = ()
        self._shown_channels = set()
        self._unavailable.clear()
        self._last_error = None
        self._source = self.provider.source_identity()
        self._key_source = self._native_identity()
        stored = str(getattr(self.provider, "_dtype", "") or "")
        self._native_pixel_bytes = {"uint8": 1, "uint16": 2}.get(stored, 4)
        self._latest_snapshot = self.controller.snapshot()
        self._consumed_viewport = None
        self._clear_layer_output()
        self._start_coarse_for_active_channels()
        # Fine is intentionally held until a channel's complete coarse
        # transaction publishes; _publish_current() starts it then.

    def update_viewport(self, snapshot=None) -> None:
        """Immediately plan the public current viewport; never waits for quiet."""
        if self._disposed or self._source is None:
            return
        if self._paused:
            return
        snapshot = self.controller.snapshot() if snapshot is None else snapshot
        if snapshot.source != self._source:
            # An owner must call source_changed() after rebinding.  Do not
            # infer old/new state or publish potentially wrong pixels.
            self._last_error = "viewport source differs from explicit binding source; source_changed() required"
            return
        self._latest_snapshot = snapshot
        self._consumed_viewport = self._viewport_token(snapshot)
        self._begin_fine_epoch(snapshot)
        self._publish_current()

    @perf_trace.timed("gpu.refresh")
    def refresh_display(self) -> None:
        """Resubmit resident immutable planes; display-only changes issue no I/O.

        A channel that has just been UNTICKED is the one exception, and it
        is a release rather than work: its unfinished fine requests are
        cancelled and its fine planes are dropped here and now, instead of
        sitting in memory until the user happens to move the camera. A
        colour, a weight or an Intensity window leaves the active set alone
        and therefore still reads nothing and asks for nothing.
        """
        if self._disposed or self._source is None:
            return
        if self._paused:
            return
        active = self._active_channels(self._build_display_snapshot())
        previous = self._active_fine_channels
        # A REMOVAL IS ONLY A REMOVAL: the channels that are still drawn keep
        # their planes, keep their generation and keep whatever they have in
        # flight. Nothing of theirs is cancelled and nothing is asked for
        # again just because a neighbour was unticked.
        self._release_inactive_fine(active)
        for channel in active:
            if channel not in self._coarse and channel not in self._unavailable:
                self._start_coarse_channel(channel)
        if active != previous:
            self._active_fine_channels = active
            if self._latest_snapshot is not None:
                priority = self._fine_priority()
                before = self._admission
                admission = self._admit(self._latest_snapshot)
                changed = (before is None or before.level != admission.level
                           or any(admission.carry.get(c, 0) < before.carry.get(c, 0)
                                  for c in active))
                for channel in active:
                    if channel in previous and not changed:
                        continue
                    # The channel that just came back is planned (if its
                    # complete coarse is already published; a brand new one
                    # starts its fine when that coarse lands). Block A9 S2b:
                    # when the new active set changes the ADMISSION, every
                    # active channel is re-planned against it -- a plan that
                    # is still the same is kept, not re-requested.
                    self._plan_fine_for_channel(channel, self._latest_snapshot,
                                                priority)
        self._publish_once_per_turn()

    def _publish_once_per_turn(self) -> None:
        """Block A9-O3-2: the first display refresh of an event-loop turn
        draws AT ONCE; any further one in the same turn -- a session restore
        installs ~30 channels' settings back to back in one handler, and
        nobody can see the frames in between -- is folded into ONE publish
        right after the turn (codex: by turn, not by elapsed time, which a
        20 ms refresh would outrun)."""
        if self._turn_published:
            self._publish_due = True
            self._publish_timer.start(0)
            return
        self._publish_current()
        self._turn_published = True
        QtCore.QTimer.singleShot(0, self._end_turn)

    def _end_turn(self) -> None:
        self._turn_published = False

    def _release_inactive_fine(self, active) -> int:
        """Give back the fine of every channel that is no longer drawn."""
        wanted = set(active)
        released = 0
        for channel in tuple(self._published_fine):
            if channel not in wanted:
                released += len(self._published_fine.pop(channel))
        for channel in tuple(self._fine):
            if channel not in wanted:
                plan = self._fine.pop(channel)
                self.scheduler.cancel_generation(plan.generation)
                self._generations.discard(plan.generation)
        self._fine_budget_refused &= wanted
        return released

    def pause(self) -> bool:
        """Block 2a: stop asking. Idempotent.

        The controller's camera events are let go and the motion timer is
        stopped; every planning entry refuses while paused. Reads already
        issued may still land -- they are kept -- but none of them starts a
        next round (a coarse that completes does not plan its fine)."""
        if self._disposed or self._paused:
            return False
        self._paused = True
        self._motion_timer.stop()
        self._disconnect_controller()
        return True

    def resume(self) -> bool:
        """Block 2a: follow the camera again and catch up once. Idempotent.

        ONE refresh entry, from the state as it now reads: a source that
        moved while paused is taken up first; otherwise every drawn channel
        without coarse starts it, and the current viewport is planned
        afresh for every drawn channel -- whatever is missing is asked for,
        whatever is resident stays."""
        if self._disposed or not self._paused:
            return False
        self._paused = False
        self._connect_controller()
        if self._source_pending:
            self._source_pending = False
            self.source_changed()
            return True
        if self._source is None:
            return True
        active = self._active_channels(self._build_display_snapshot())
        self._release_inactive_fine(active)
        for channel in active:
            if channel not in self._coarse and channel not in self._unavailable:
                self._start_coarse_channel(channel)
        self._consumed_viewport = None
        self.update_viewport()
        return True

    @property
    def paused(self) -> bool:
        return self._paused

    def dispose(self) -> Dict[str, Any]:
        if self._disposed:
            return {"already_disposed": True, "published_channels": 0, "generations": 0}
        self._disposed = True
        self._motion_timer.stop()
        self._publish_timer.stop()
        self._publish_due = False
        self._disconnect_controller()
        self._cancel_all()
        self._coarse.clear()
        self._fine.clear()
        self._published_coarse.clear()
        self._published_fine.clear()
        if self._dispose_layer:
            self.layer.dispose()
        return {"already_disposed": False, "published_channels": 0, "generations": 0}

    def stats(self) -> Dict[str, Any]:
        coarse_bytes = sum(self._plane_bytes(plane) for planes in self._published_coarse.values() for plane in planes)
        fine_bytes = sum(self._plane_bytes(plane)
                         for planes in self._published_fine.values()
                         for plane in planes.values())
        return {
            "source": self._source,
            "revision": self._revision,
            "coarse_channels": tuple(sorted(self._published_coarse)),
            "fine_channels": tuple(sorted(channel for channel, planes
                                          in self._published_fine.items() if planes)),
            "coarse_plane_bytes": coarse_bytes,
            "fine_plane_bytes": fine_bytes,
            "fine_tiles": {channel: len(planes)
                           for channel, planes in sorted(self._published_fine.items())},
            "fine_levels": {channel: tuple(sorted({key.tile.level for key in planes}))
                            for channel, planes in sorted(self._published_fine.items())},
            "retained_fine_last_epoch": self._retained_fine_last_epoch,
            "requested_fine_last_epoch": self._requested_fine_last_epoch,
            "fine_budget_refused": tuple(sorted(self._fine_budget_refused)),
            # block A9 S2b
            "ideal_level": None if self._admission is None else self._admission.ideal,
            "admitted_level": None if self._admission is None else self._admission.level,
            "resolution_limited": bool(self._admission is not None and self._admission.limited),
            "base_overflow_channels": self._base_overflow,
            "requests": self._request_count,
            "coarse_pending": self.coarse_pending_channels(),
            "fine_requests_by_priority": dict(self._fine_requests_by_priority),
            "last_fine_priority": self._last_fine_priority,
            "accepted_results": self._accepted_count,
            "rejected_late_results": self._rejected_late_count,
            "unavailable": dict(self._unavailable),
            "last_error": self._last_error,
            "descriptor_publications": len(self._descriptor_history),
        }

    def coarse_pending_channels(self) -> Tuple[str, ...]:
        """Channels whose complete-coarse transaction has not finished yet.

        A failed transaction is not pending: it is never published and is
        never retried by this binding, so it must not hold fine back.
        """
        return tuple(sorted(name for name, plan in self._coarse.items()
                            if not plan.failed and not plan.complete))

    @property
    def descriptor_history(self):
        return tuple(self._descriptor_history)

    # Public controller signals -----------------------------------------

    def _connect_controller(self) -> None:
        if hasattr(self.controller, "interaction_event"):
            self.controller.interaction_event.connect(self._interaction_event)
        if hasattr(self.controller, "gesture_quiet"):
            self.controller.gesture_quiet.connect(self._gesture_quiet)

    def _disconnect_controller(self) -> None:
        for signal, slot in ((getattr(self.controller, "interaction_event", None), self._interaction_event),
                             (getattr(self.controller, "gesture_quiet", None), self._gesture_quiet)):
            if signal is not None:
                try:
                    signal.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass

    def _interaction_event(self, kind, snapshot) -> None:
        """A camera event from the ONE controller. Jumps now, motion throttled.

        THROTTLE, NOT DEBOUNCE. `QTimer.start()` on a running single-shot
        timer pushes its timeout further out, so restarting it on every
        event means it never fires at all while the camera keeps moving --
        the picture then waits for the mouse button to come up. That is the
        same bug the ViewBox's own motion timer already names and avoids
        (`viewer/explore_view.py`, "THROTTLE, not debounce"), and this
        leaves a timer that is already running alone so it fires every
        `motion_interval_ms` DURING the gesture with whatever the latest
        camera position is by then. The interval itself is unchanged and no
        second timer, thread or coalescer is introduced.
        """
        if self._disposed:
            return
        self._latest_snapshot = snapshot
        if kind == "NAVIGATOR_JUMP":
            # A LANDING IS THE LAST WORD on where the camera is, and it is
            # served now. A motion timer still running from the gesture
            # before it would fire afterwards and plan this very same
            # position all over again -- cancelling the generation that was
            # just issued and re-attaching every request for it.
            self._motion_timer.stop()
            self.update_viewport(snapshot)
            return
        if not self._motion_timer.isActive():
            self.update_viewport(snapshot)
            self._motion_timer.start(self.budgets.motion_interval_ms)

    def _gesture_quiet(self, snapshot) -> None:
        """The controller says the gesture is over. Catch up only if behind."""
        if not self._disposed:
            self._latest_snapshot = snapshot
            self._flush_motion()

    def _flush_motion(self) -> None:
        """Plan the latest camera position, unless it is already planned.

        WHY THE TIMER'S OWN STATE IS NOT ENOUGH. `isActive()` is true in two
        different situations that need opposite answers: a newer camera
        position is waiting for the throttle window to close (must plan),
        and the last event was consumed immediately with nothing since
        (must not). The controller already stamps every camera event with
        its own `epoch`, and the quiet snapshot carries the epoch of the
        last event rather than a new one, so comparing that is exactly the
        question "has anything happened since we last planned".
        """
        snapshot = self._latest_snapshot
        if snapshot is None:
            return
        if self._consumed_viewport == self._viewport_token(snapshot):
            return
        self.update_viewport(snapshot)

    @staticmethod
    def _viewport_token(snapshot):
        """What makes one planned viewport different from another."""
        return (getattr(snapshot, "source", None),
                int(getattr(snapshot, "epoch", -1)))

    # Coarse planning -----------------------------------------------------

    def _start_coarse_for_active_channels(self) -> None:
        for channel in self._active_channels(self._build_display_snapshot()):
            self._start_coarse_channel(channel)

    def _start_coarse_channel(self, channel: str) -> None:
        if self._disposed or self._paused or channel in self._coarse or channel in self._unavailable:
            return
        source_kind = self.provider.source_table.source_of(channel)
        if source_kind == "missing":
            self._unavailable[channel] = self._missing_reason(channel)
            return
        level = int(self.provider.num_levels) - 1
        keys = self._full_level_keys(channel, level)
        expected_bytes = self._planned_bytes(keys)
        if (len(keys) > self.budgets.max_coarse_tiles_per_channel or
                expected_bytes > self.budgets.max_coarse_plane_bytes_per_channel):
            self._unavailable[channel] = "coarse budget refused"
            return
        generation = self._generation("coarse", channel)
        plan = _Plan("coarse", channel, self._source, self._revision, generation, set(keys),
                     priority=PRIORITY_COARSE)
        self._coarse[channel] = plan
        for key in sorted(keys, key=self._key_order):
            self._request(plan, key, priority=plan.priority)

    def _full_level_keys(self, channel: str, level: int) -> Set[RawKey]:
        height, width = self.provider.level_shape(level)
        tile_size = self.controller.grid.tile_size
        keys = set()
        for ty in range(math.ceil(height / tile_size)):
            for tx in range(math.ceil(width / tile_size)):
                address = TileAddress(grid=self.controller.grid, level=level, tx=tx, ty=ty)
                keys.add(RawKey(source=self._key_source, channel=channel, tile=address))
        return keys

    # Fine planning -------------------------------------------------------

    def _begin_fine_epoch(self, snapshot) -> None:
        """Move the fine tier to this viewport WITHOUT emptying the screen.

        The camera moved; the pixels already on it did not stop being the
        right pixels for where they are. Everything that still covers the
        new viewport is kept and re-submitted at once, and only the tiles
        that are genuinely missing are asked for.
        """
        active = self._active_channels(self._build_display_snapshot())
        self._cancel_fine()
        self._fine_epoch += 1
        # RECORDED EVEN WHEN IT IS EMPTY. Unticking the last channel must
        # leave no memory of it, or ticking that same channel back would
        # look like "nothing changed" and its fine would never be planned.
        self._active_fine_channels = active
        self._release_inactive_fine(active)
        if not active:
            self._retained_fine_last_epoch = 0
            self._requested_fine_last_epoch = 0
            return
        # One deterministic decision per viewport epoch: a complete-coarse
        # transaction that is still in flight puts this epoch's fine work
        # behind coarse in the existing scheduler's priority order.
        priority = self._fine_priority()
        self._fine_budget_refused.clear()
        retained = requested = 0
        for channel in active:
            kept, asked = self._plan_fine_for_channel(channel, snapshot, priority)
            retained += kept
            requested += asked
        self._retained_fine_last_epoch = retained
        self._requested_fine_last_epoch = requested

    def _fine_priority(self) -> int:
        self._last_fine_priority = (PRIORITY_FINE_DEFERRED
                                    if self.coarse_pending_channels()
                                    else PRIORITY_FINE_FOREGROUND)
        return self._last_fine_priority

    def _plan_fine_for_channel(self, channel: str, snapshot, priority: int):
        """Plan ONE channel's fine for this viewport: `(retained, requested)`.

        It reads and writes only this channel's planes and this channel's
        request in flight.  Another channel's generation is never cancelled
        here and its tiles are never asked for again, so unticking a
        neighbour -- or a new channel's coarse landing -- cannot restart a
        load that is already half done.
        """
        if self._disposed or self._source is None or self._paused:
            return 0, 0
        if channel in self._unavailable:
            self._published_fine.pop(channel, None)
            return 0, 0
        if channel not in self._published_coarse and channel not in self._coarse:
            # No coarse of any kind yet: starting it is what plans this.
            self._published_fine.pop(channel, None)
            return 0, 0
        if getattr(snapshot, "source", None) != self._source:
            return 0, 0
        budget = self.budgets.max_fine_plane_bytes_per_channel
        # Block A9 S2b: the ADMITTED level and tiles, not the requested ones
        # -- a viewport that does not fit is drawn one level coarser (and
        # says so), never refused and left unresolved.
        admission = self._admit(snapshot, channel)
        target_level = admission.level
        keys = set(admission.keys.get(channel, frozenset()))
        viewport = self._keys_world_rect(keys) or self._keys_world_rect(
            self._visible_keys(channel, snapshot))
        # THE CURRENT TARGET LEVEL IS RESERVED FIRST. A layer carried over
        # from another zoom is a stand-in until the target arrives; it must
        # never be the reason the target itself does not fit, which would
        # leave the viewport on that stand-in for good.
        target_bytes = self._planned_bytes(keys)
        if (len(keys) > self.budgets.max_fine_tiles_per_viewport or
                target_bytes > budget):
            # FAIL CLOSED AND SAY SO: this viewport's OWN target level does
            # not fit. No partial precision, no extra budget, and what is
            # already on screen stays.
            self._fine_budget_refused.add(channel)
            perf_trace.mark("gpu.fine_refused", channel=channel, level=target_level,
                            tiles=len(keys), bytes=target_bytes, budget=budget)
            self._last_error = (
                f"fine budget refused for {channel}: the current viewport's "
                f"own target level needs {target_bytes} bytes, over {budget}")
            return self._retain_fine(channel, keys, viewport, target_level,
                                     budget), 0
        self._fine_budget_refused.discard(channel)
        retained = self._retain_fine(channel, keys, viewport, target_level,
                                     min(budget - target_bytes,
                                         admission.carry.get(channel, budget)))
        resident = self._published_fine.get(channel) or {}
        missing = [key for key in sorted(keys, key=self._key_order)
                   if key not in resident]
        if not missing:
            # The admitted target is all resident. A request still under way
            # for this channel belongs to another target (e.g. the finer
            # level before a re-admission moved it back): cancel it, so its
            # late tiles cannot land outside the carry allowance (codex).
            leftover = self._fine.pop(channel, None)
            if leftover is not None:
                self.scheduler.cancel_generation(leftover.generation)
                self._generations.discard(leftover.generation)
            return retained, 0
        current = self._fine.get(channel)
        if (current is not None and not current.failed
                and current.viewport_epoch == self._fine_epoch
                and set(current.expected) - set(current.planes) == set(missing)):
            # Block A9 S2b: re-planned against an unchanged admission -- the
            # same request is already under way; keep it.
            return retained, 0
        stale = self._fine.pop(channel, None)
        if stale is not None:
            # This channel's OWN previous plan, and nobody else's.
            self.scheduler.cancel_generation(stale.generation)
            self._generations.discard(stale.generation)
        generation = self._generation("fine", channel, self._fine_epoch)
        plan = _Plan("fine", channel, self._source, self._revision, generation,
                     set(missing), viewport_epoch=self._fine_epoch,
                     priority=priority)
        self._fine[channel] = plan
        # WHAT THE RAW CACHE CAN ANSWER AT ONCE BECOMES ONE PICTURE. Coming
        # back to a patch that is still cached used to arrive as one queued
        # signal per tile, so the viewer replayed coarse and then filled in
        # tile by tile for data it already had. These are applied here, in
        # this same call, and the caller publishes once -- so by the time Qt
        # gets to paint, the whole viewport is already there. A tile that is
        # genuinely missing still arrives later, on its own, one at a time.
        immediate = []
        for key in missing:
            self._request(plan, key, priority=plan.priority, collector=immediate)
        for payload in immediate:
            self._apply_result(payload, publish=False)
        return retained, len(missing)

    # Admission (block A9 S2b) ---------------------------------------------

    def _admit(self, snapshot, channel: Optional[str] = None) -> _Admission:
        """The admitted fine target for `snapshot` and the active channels.

        Memoised per (revision, viewport, active set, coarse state): the
        three planning paths (a camera move, a channel ticked, a complete
        coarse landing) all read the same answer for the same situation."""
        # the planned channels, plus the one being planned now (a complete
        # coarse can land before the first viewport epoch recorded them)
        named = set(self._active_fine_channels)
        if not named:
            # nothing planned yet (a complete coarse landing before the
            # first viewport epoch): the display's whole active set, so the
            # channels are admitted together, not one by one (codex)
            named = set(self._active_channels(self._build_display_snapshot()))
        if channel is not None:
            named.add(channel)
        active = tuple(sorted(c for c in named if c not in self._unavailable))
        coarse_state = tuple((c, id(self._coarse.get(c))) for c in active)
        token = (self._revision, self._viewport_token(snapshot), active, coarse_state)
        previous = self._admission
        if previous is not None and previous.token == token:
            return previous
        ideal = int(getattr(snapshot, "level", 0) or 0)
        coarsest = int(self.provider.num_levels) - 1
        fine_budget = self.budgets.max_fine_plane_bytes_per_channel
        max_tiles = self.budgets.max_fine_tiles_per_viewport
        total = self.budgets.max_raw_texture_bytes
        coarse_keys: Set[RawKey] = set()
        for channel in active:
            plan = self._coarse.get(channel)
            # the COMPLETE expected coarse, delivered or not (codex)
            coarse_keys |= (set(plan.expected) if plan is not None
                            else self._full_level_keys(channel, coarsest))
        chosen = None
        for level in range(max(0, ideal), coarsest + 1):
            fine = {c: frozenset(self._candidate_keys(c, snapshot, level)) for c in active}
            if any(len(k) > max_tiles or self._planned_bytes(k) > fine_budget
                   for k in fine.values()):
                continue
            union = set(coarse_keys)
            for k in fine.values():
                union |= k
            used = self._planned_bytes(union)
            if total is None or used <= total:
                chosen = (level, fine, used)
                break
        coarse_only = chosen is None
        if coarse_only:
            # nothing finer fits: the complete coarse alone is the picture
            chosen = (coarsest, {c: frozenset() for c in active},
                      self._planned_bytes(coarse_keys))
        level, fine, used = chosen
        share = (None if total is None
                 else max(0, total - used) // max(1, len(active)))
        carry = {c: max(0, fine_budget - self._planned_bytes(fine[c])) if share is None
                 else min(share, max(0, fine_budget - self._planned_bytes(fine[c])))
                 for c in active}
        admission = _Admission(token=token, ideal=ideal, level=level, keys=fine, carry=carry,
                               coarse_only=coarse_only)
        self._admission = admission
        if admission.limited:
            perf_trace.mark("gpu.fine_admitted", ideal=ideal, level=level,
                            channels=len(active), bytes=used, budget=total)
        def shown(a):
            return None if a is None or not a.limited else (a.ideal, a.level, a.coarse_only)
        if shown(previous) != shown(admission):
            hook = self.on_status_changed
            if hook is not None:
                QtCore.QTimer.singleShot(0, hook)
        return admission

    def _candidate_keys(self, channel: str, snapshot, level: int) -> Set[RawKey]:
        """The visible tiles at `level`: the requested level's own visible
        set, or the same area mapped onto a coarser level with the rounded
        downsamples the controller plans with."""
        ideal = int(getattr(snapshot, "level", 0) or 0)
        if level == ideal:
            return self._visible_keys(channel, snapshot)
        if snapshot.source != self._source:
            return set()
        tile = self.controller.grid.tile_size
        bbox_l0 = getattr(snapshot, "bbox_l0", None)
        if bbox_l0 is not None:
            # exactly the controller's own computation for that level
            # (`_on_range_changed`: clamp, convert, cover)
            from ..viewer import request_planning as planning
            coords = planning.visible_tiles_for_viewport(
                tuple(bbox_l0), self._level_ds(level), tile)
            return {RawKey(source=self._key_source, channel=channel,
                           tile=TileAddress(grid=self.controller.grid, level=level,
                                            tx=int(tx), ty=int(ty)))
                    for tx, ty in coords}
        ds_i, ds_l = self._level_ds(ideal), self._level_ds(level)
        height, width = self.provider.level_shape(level)
        nx, ny = math.ceil(width / tile), math.ceil(height / tile)
        coords = set()
        for tx, ty in snapshot.visible_tiles:
            x0, x1 = tx * tile * ds_i, (tx + 1) * tile * ds_i
            y0, y1 = ty * tile * ds_i, (ty + 1) * tile * ds_i
            for cx in range(int(x0 // (tile * ds_l)), min(nx - 1, int((x1 - 1) // (tile * ds_l))) + 1):
                for cy in range(int(y0 // (tile * ds_l)), min(ny - 1, int((y1 - 1) // (tile * ds_l))) + 1):
                    coords.add((cx, cy))
        return {RawKey(source=self._key_source, channel=channel,
                       tile=TileAddress(grid=self.controller.grid, level=level, tx=cx, ty=cy))
                for cx, cy in coords}

    def _level_ds(self, level: int) -> float:
        """The downsample the controller plans `level` with (its rounded
        `level_downsample`; the exact x one where a provider has no other)."""
        rounded = getattr(self.provider, "level_downsample", None)
        if rounded is not None:
            return float(rounded(level))
        return float(self.provider.level_downsample_yx(level)[1])

    def _target_keys(self, channel: str, snapshot) -> Set[RawKey]:
        """The admitted target tiles of `channel` for `snapshot`."""
        if getattr(snapshot, "source", None) != self._source:
            return set()
        return set(self._admit(snapshot, channel).keys.get(channel, frozenset()))

    def _retain_fine(self, channel: str, target_keys: Set[RawKey],
                     viewport: Optional[Tuple[float, float, float, float]],
                     target_level: int, carry_budget: int) -> int:
        """Keep the fine planes that still cover this viewport; drop the rest.

        A plane of the CURRENT target set is always kept: its bytes were
        reserved before this was called. Everything else is a stand-in
        carried from another zoom, and those share `carry_budget` -- what
        the target set left over -- nearest level first. A plane whose
        source has moved is gone, and so is one that no longer touches the
        viewport. Nothing here is a cache: there is no eviction policy
        beyond "does this still show where the camera is looking".
        """
        planes = self._published_fine.get(channel)
        if not planes:
            return 0
        kept, carried = {}, []
        for key, plane in planes.items():
            if key.source != self._key_source:
                continue
            if key in target_keys:
                kept[key] = plane
            elif self._overlaps(plane.world_rect, viewport):
                carried.append((key, plane))
        carried.sort(key=lambda item: (abs(int(item[0].tile.level) - target_level),
                                       self._key_order(item[0])))
        total = 0
        for key, plane in carried:
            size = self._plane_bytes(plane)
            if total + size > carry_budget:
                break
            kept[key] = plane
            total += size
        if kept:
            self._published_fine[channel] = kept
        else:
            self._published_fine.pop(channel, None)
        return len(kept)

    @staticmethod
    def _overlaps(rect, viewport) -> bool:
        if viewport is None:
            return False
        x0, x1, y0, y1 = rect
        vx0, vx1, vy0, vy1 = viewport
        return x0 < vx1 and x1 > vx0 and y0 < vy1 and y1 > vy0

    def _keys_world_rect(self, keys: Set[RawKey]):
        """The world rectangle a target-level tile set covers, or None."""
        rects = [self._tile_world_rect(key) for key in keys]
        rects = [rect for rect in rects if rect is not None]
        if not rects:
            return None
        return (min(r[0] for r in rects), max(r[1] for r in rects),
                min(r[2] for r in rects), max(r[3] for r in rects))

    def _tile_world_rect(self, key: RawKey):
        """One tile address as a world rectangle, from public geometry only."""
        level = int(key.tile.level)
        height, width = self.provider.level_shape(level)
        tile_size = key.tile.grid.tile_size
        ds_y, ds_x = self.provider.level_downsample_yx(level)
        tile_h = min(tile_size, height - key.tile.ty * tile_size)
        tile_w = min(tile_size, width - key.tile.tx * tile_size)
        if tile_h <= 0 or tile_w <= 0:
            return None
        x0 = key.tile.tx * tile_size * float(ds_x)
        y0 = key.tile.ty * tile_size * float(ds_y)
        return x0, x0 + tile_w * float(ds_x), y0, y0 + tile_h * float(ds_y)

    def _visible_keys(self, channel: str, snapshot) -> Set[RawKey]:
        if snapshot.source != self._source:
            return set()
        level = int(snapshot.level)
        return {
            RawKey(source=self._key_source, channel=channel,
                   tile=TileAddress(grid=self.controller.grid, level=level, tx=int(tx), ty=int(ty)))
            for tx, ty in snapshot.visible_tiles
        }

    # Scheduler delivery --------------------------------------------------

    def _request(self, plan: _Plan, key: RawKey, *, priority: int,
                 collector=None) -> None:
        """Ask the existing scheduler for one tile.

        `collector` is a LIST OWNED BY THE CALLING FRAME and nothing else.
        A result may come back two ways, and they are kept strictly apart:

        * SYNCHRONOUSLY, inside `scheduler.request(...)`, on this very GUI
          thread -- that is the raw cache answering at once. Those go into
          the caller's list so the whole batch becomes one picture.
        * ANY OTHER WAY -- a worker thread, or later -- keeps the queued
          signal it has always had. A worker thread never touches this
          object's state, the G1 layer or any widget.

        The window is exactly the duration of the `request()` call, and the
        thread is checked as well, so a worker that happens to finish
        during that call is still routed through the queued signal.
        """
        envelope = (plan.tier, plan.channel, plan.source, plan.revision,
                    plan.generation, plan.viewport_epoch, key)
        request = TileRequest(key=key, generation=plan.generation, priority=priority,
                              deadline_ms=None, notify_on_stale_completion=True)
        inside_this_call = [collector is not None]

        def callback(result, captured=envelope):
            if (inside_this_call[0] and collector is not None
                    and QtCore.QThread.currentThread() is self._gui_thread):
                collector.append((captured, result))
                return
            self._tile_result_received.emit((captured, result))

        self._request_count += 1
        if plan.tier == "fine":
            self._fine_requests_by_priority[priority] = (
                self._fine_requests_by_priority.get(priority, 0) + 1)
        try:
            self.scheduler.request(request, callback)
        finally:
            inside_this_call[0] = False

    @QtCore.pyqtSlot(object)
    @perf_trace.timed("gpu.accept")
    def _accept_result(self, payload) -> None:
        """The ONLY entry for a result that arrived through the queued signal.

        Applied now, drawn on the next frame slot (`_schedule_publish`)."""
        self._apply_result(payload, publish=True, deferred=True)

    def _schedule_publish(self) -> None:
        """One publish per FRAME_MS for results; a slot already armed takes
        every result that lands before it fires."""
        self._publish_due = True
        if self._publish_timer.isActive():
            return
        since = (time.monotonic() - self._last_publish_at) * 1000.0
        self._publish_timer.start(int(math.ceil(max(0.0, PUBLISH_FRAME_MS - since))))

    def _flush_publish(self) -> None:
        if self._publish_due and not self._disposed:
            perf_trace.mark("gpu.batch")
            self._publish_current()

    def _apply_result(self, payload, *, publish: bool, deferred: bool = False) -> None:
        if self._disposed:
            return
        captured, result = payload
        tier, channel, source, revision, generation, viewport_epoch, key = captured
        plan = self._coarse.get(channel) if tier == "coarse" else self._fine.get(channel)
        if not self._is_current_delivery(plan, source, revision, generation, viewport_epoch, key, result):
            self._rejected_late_count += 1
            return
        if result.error is not None or result.pixels is None or result.pixels.residency != "cpu":
            plan.failed = True
            self._last_error = f"{tier} tile failed for {channel}: {result.error or 'missing cpu pixels'}"
            if tier == "fine":
                self._fine.pop(channel, None)
            return
        handle = result.pixels.handle
        native = getattr(handle, "kind", None)
        array = np.asarray(handle.values if native is not None else handle)
        if array.ndim != 2:
            plan.failed = True
            self._last_error = f"{tier} tile has unsupported shape {array.shape!r}"
            return
        try:
            if native == "raw":
                # block A9 S2c: the pyramid's own integers; validity is the
                # tile's ROI rectangle (zero outside = absent, not black)
                rect = handle.valid_rect if handle.valid_rect is not None else (0, 0, 0, 0)
                plane = RawPlane(identity=key, world_rect=self._world_rect(key, array.shape),
                                 values=array, valid=None, valid_rect=tuple(rect))
            else:
                plane = RawPlane(identity=key, world_rect=self._world_rect(key, array.shape),
                                 values=np.asarray(array, dtype=np.float32),
                                 valid=np.isfinite(array))
        except Exception as exc:
            plan.failed = True
            self._last_error = f"{tier} tile conversion failed: {exc}"
            return
        plan.planes[key] = plane
        self._accepted_count += 1
        if tier == "coarse":
            # ATOMIC: a channel's complete coarse appears in one step or not
            # at all, so a new channel never grows out of the middle.
            if not plan.complete:
                return
            self._published_coarse[channel] = tuple(
                plan.planes[item] for item in sorted(plan.expected, key=self._key_order))
            self._coarse[channel] = plan
            if self._latest_snapshot is not None and not self._paused:
                # Only this channel's fine starts here. Another channel's
                # load is already under way and must not be restarted.
                kept, asked = self._plan_fine_for_channel(
                    channel, self._latest_snapshot, self._fine_priority())
                self._retained_fine_last_epoch += kept
                self._requested_fine_last_epoch += asked
        else:
            # INCREMENTAL: a fine tile sharpens its own world rectangle on
            # the complete coarse background the moment it lands. Waiting
            # for the whole viewport is what kept every gesture blurry.
            self._published_fine.setdefault(channel, {})[key] = plane
            if plan.complete:
                self._fine.pop(channel, None)
        if publish and deferred:
            self._schedule_publish()
        elif publish:
            self._publish_current()

    def _is_current_delivery(self, plan, source, revision, generation, viewport_epoch, key, result) -> bool:
        return bool(
            plan is not None and not plan.failed and
            self._source == source and self._revision == revision and
            plan.source == source and plan.revision == revision and
            plan.generation == generation and plan.viewport_epoch == viewport_epoch and
            key in plan.expected and
            result.request.key == key and result.request.generation == generation
        )

    # Publishing ----------------------------------------------------------

    @perf_trace.timed("gpu.publish")
    def _publish_current(self) -> None:
        if self._disposed or self._source is None:
            return
        # Whatever is due rides this publish: it reads every applied plane.
        self._publish_due = False
        self._publish_timer.stop()
        self._last_publish_at = time.monotonic()
        display = self._build_display_snapshot()
        viewport = self._build_viewport_snapshot()
        active = self._active_channels(display)
        channels = []
        shown = set()
        for channel in active:
            coarse = self._published_coarse.get(channel)
            if not coarse:
                continue
            # COARSE FIRST, THEN SHARPER (block A9-O3-5, user ruling
            # 2026-10-07: "follow Odon's design"). A channel appears the
            # moment its complete coarse is in hand and sharpens tile by
            # tile as its fine lands -- Odon draws coarse -> fine every
            # frame with a per-channel fallback. This replaces G3.2b's
            # hold-back until the viewport's fine was all in, which kept
            # the first Step1 picture off the screen for ~2 s.
            shown.add(channel)
            # COARSEST FIRST, FINEST LAST. G1 draws a channel's planes in
            # the order given with blending off, so a finer plane overwrites
            # a coarser one exactly where it has pixels -- which is how a
            # half-refreshed viewport carries the nearest level it already
            # has instead of falling back to the whole-slide coarse.
            resident = self._published_fine.get(channel) or {}
            fine = tuple(plane for _key, plane in sorted(
                resident.items(),
                key=lambda item: (-int(item[0].tile.level),
                                  int(item[0].tile.ty), int(item[0].tile.tx))))
            snapshot = self._latest_snapshot
            wants_fine = bool(snapshot is not None
                              and getattr(snapshot, "source", None) == self._source
                              and self._target_keys(channel, snapshot))
            channels.append(ChannelSource(channel, coarse=coarse, fine=fine,
                                          selected_level="fine" if fine else "coarse",
                                          target_level="fine" if wants_fine else "coarse"))
        fitted = self._fit_total_budget(channels)
        overflow = 0 if fitted is not None else len(channels)
        if overflow != self._base_overflow:
            self._base_overflow = overflow
            hook = self.on_status_changed
            if hook is not None:
                QtCore.QTimer.singleShot(0, hook)
        if fitted is None:
            # the base layers alone do not fit: never omit a channel, never
            # hand the layer a set it must refuse -- keep the last frame
            perf_trace.mark("gpu.base_overflow", channels=len(channels))
            return
        channels = fitted
        descriptor = SourceDescriptor(tuple(channels))
        tracing = perf_trace.enabled()
        if tracing:
            # A9 M0, measurement only: the numeric level this frame owes --
            # since S2b the ADMITTED one (the ideal is in gpu.frame)
            snapshot = self._latest_snapshot
            on_source = (snapshot is not None
                         and getattr(snapshot, "source", None) == self._source)
            ideal_index = int(snapshot.level) if on_source else None
            target_index = self._admit(snapshot).level if on_source else None
            self.layer._a9_target_index = target_index
        try:
            stats = self.layer.submit(descriptor, display, viewport)
        except Step1GpuLayerError as exc:
            self._last_error = f"G1 submission failed: {exc}"
            perf_trace.mark("gpu.submit_failed", error=str(exc)[:160].replace(" ", "_"))
            return
        if tracing:
            cache = stats.get("cache") or {}
            uploads = int(cache.get("uploads", 0) or 0)
            perf_trace.mark(
                "gpu.frame", frame=getattr(self.layer, "_a9_frame", 0), level=target_index,
                ideal=ideal_index,
                channels=len(channels),
                planes=sum(len(c.coarse) + len(c.fine) for c in channels),
                fine=sum(len(c.fine) for c in channels),
                fine_off_target=(-1 if target_index is None else sum(
                    1 for c in channels for p in c.fine
                    if int(p.identity.tile.level) != target_index)),
                passes=stats.get("pass_count"),
                uploads=uploads - getattr(self, "_a9_uploads_seen", 0),
                resident=cache.get("bytes"), refused=len(self._fine_budget_refused))
            self._a9_uploads_seen = uploads
        self._shown_channels = shown
        self._descriptor_history.append((descriptor, display, viewport, stats))

    def _fit_total_budget(self, channels):
        """Block A9 S2b safety net: the submitted planes, counted once per
        identity, must fit the total raw-texture budget. Admission already
        guarantees it for coarse + target; if carried stand-ins still push
        it over (a channel's admission changed while they were resident),
        the stand-ins go first -- never a target plane, never a coarse one."""
        total = self.budgets.max_raw_texture_bytes
        if total is None:
            return channels

        def used(items):
            seen = {}
            for source in items:
                for plane in tuple(source.coarse) + tuple(source.fine):
                    seen[plane.identity] = self._plane_bytes(plane)
            return sum(seen.values())
        if used(channels) <= total:
            return channels
        snapshot = self._latest_snapshot
        admitted = (self._admit(snapshot).keys if snapshot is not None
                    and getattr(snapshot, "source", None) == self._source else {})
        trimmed = []
        for source in channels:
            keep = admitted.get(source.channel, frozenset())
            fine = tuple(plane for plane in source.fine if plane.identity in keep)
            trimmed.append(ChannelSource(source.channel, coarse=source.coarse, fine=fine,
                                         selected_level="fine" if fine else "coarse",
                                         target_level=source.target_level))
        after = used(trimmed)
        perf_trace.mark("gpu.admission_trim", before=used(channels), after=after,
                        budget=total)
        if after > total:
            return None
        return trimmed

    def _viewport_fine_ready(self, channel: str) -> bool:
        """Is this channel's CURRENT viewport complete at its target level?

        ASKED OF THE PLANES ACTUALLY IN HAND, never of whether a plan
        happens to be in flight. A plan is also absent when the viewport was
        refused for budget and when one of its tiles failed, and neither of
        those means the picture is ready -- treating them as ready is how a
        first appearance would leak the blurry version it is supposed to
        wait past. With nothing in hand and nothing coming, the channel
        stays off the screen and the refusal stays on the badge.
        """
        snapshot = self._latest_snapshot
        if snapshot is None:
            return True
        if getattr(snapshot, "source", None) != self._source:
            return False
        keys = self._target_keys(channel, snapshot)
        if not keys:
            return True
        return keys <= set(self._published_fine.get(channel) or {})

    def _clear_layer_output(self) -> None:
        try:
            self.layer.submit(SourceDescriptor(()), self._build_display_snapshot(),
                              self._build_viewport_snapshot())
        except (Step1GpuLayerError, RuntimeError):
            # Layer may not be realized in a source-lifecycle unit test.
            pass

    # Helpers -------------------------------------------------------------

    def _active_channels(self, display: DisplaySnapshot) -> Tuple[str, ...]:
        active = set()
        if display.mode == MODE_OVERLAY:
            for channel, weight in display.weights.items():
                if float(weight or 0.0) > 0.0 and channel in display.mappings:
                    active.add(channel)
        elif display.mode == MODE_FUSION:
            for group, channel_weights in display.groups.items():
                if float(display.group_weights.get(group, 1.0) or 0.0) <= 0.0:
                    continue
                for channel, weight in channel_weights.items():
                    if float(weight or 0.0) > 0.0 and channel in display.mappings:
                        active.add(channel)
            nucleus, weight = display.nucleus
            if nucleus and float(weight or 0.0) > 0.0 and nucleus in display.mappings:
                active.add(nucleus)
        return tuple(sorted(active))

    def _generation(self, tier: str, channel: str, epoch: Optional[int] = None) -> Hashable:
        self._serial += 1
        token = ("step1-gpu-binding", id(self), tier, self._revision, channel, epoch, self._serial)
        self._generations.add(token)
        return token

    def _cancel_fine(self) -> None:
        for plan in self._fine.values():
            self.scheduler.cancel_generation(plan.generation)
            self._generations.discard(plan.generation)
        self._fine.clear()

    def _cancel_all(self) -> None:
        for generation in tuple(self._generations):
            self.scheduler.cancel_generation(generation)
        self._generations.clear()

    def _world_rect(self, key: RawKey, shape: Tuple[int, int]) -> Tuple[float, float, float, float]:
        ds_y, ds_x = self.provider.level_downsample_yx(key.tile.level)
        tile_size = key.tile.grid.tile_size
        height, width = int(shape[0]), int(shape[1])
        x0 = key.tile.tx * tile_size * float(ds_x)
        y0 = key.tile.ty * tile_size * float(ds_y)
        return x0, x0 + width * float(ds_x), y0, y0 + height * float(ds_y)

    def _planned_bytes(self, keys: Sequence[RawKey]) -> int:
        total = 0
        for key in keys:
            height, width = self.provider.level_shape(key.tile.level)
            tile_size = key.tile.grid.tile_size
            tile_h = max(0, min(tile_size, height - key.tile.ty * tile_size))
            tile_w = max(0, min(tile_size, width - key.tile.tx * tile_size))
            total += tile_h * tile_w * self._pixel_bytes(key)
        return int(total)

    def _native_identity(self):
        """Block A9 S2c: the provider's native namespace, or the source."""
        native = getattr(self.provider, "native_source_identity", None)
        return native() if callable(native) else self._source

    def _pixel_bytes(self, key: RawKey) -> int:
        """The texture bytes per pixel a key will cost: a raw channel read
        natively costs its stored integer size; everything else float32.
        An estimate for admission -- the submission itself is checked on
        the actual planes (`_fit_total_budget`)."""
        if key.source != self._key_source or self._key_source == self._source:
            return 4
        table = getattr(self.provider, "source_table", None)
        if table is None or table.source_of(key.channel) != "raw":
            return 4
        return self._native_pixel_bytes

    @staticmethod
    def _plane_bytes(plane: RawPlane) -> int:
        values = np.asarray(plane.values)
        itemsize = values.dtype.itemsize if values.dtype in (np.uint8, np.uint16) else 4
        return int(values.size * itemsize)

    @staticmethod
    def _key_order(key: RawKey):
        return key.tile.level, key.tile.ty, key.tile.tx

    def _missing_reason(self, channel: str) -> str:
        for missing in self.provider.missing_products():
            if missing.channel == channel:
                return missing.reason
        return "Step1 source unavailable"
