"""G2 test-only adapter from public Step1 tile supply to G1 GPU snapshots.

The binding consumes only injected public provider/scheduler/controller ports and
caller-built Display/Viewport snapshots.  It is deliberately not imported by a
production mount.  It owns no scheduler, raw cache, source authority, camera,
or display state: its bounded state is only the current source revision's
complete coarse transaction and one current fine viewport transaction.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Hashable, Mapping, Optional, Sequence, Set, Tuple

import numpy as np
from PyQt5 import QtCore

from ..viewer.tile_types import RawKey, TileAddress, TileRequest
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
        self._revision = 0
        self._source = None
        self._serial = 0
        self._latest_snapshot = None
        self._fine_epoch = 0
        self._coarse: Dict[str, _Plan] = {}
        self._fine: Dict[str, _Plan] = {}
        self._published_coarse: Dict[str, Tuple[RawPlane, ...]] = {}
        self._published_fine: Dict[str, Tuple[RawPlane, ...]] = {}
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
        self._connect_controller()

    # Public lifecycle ----------------------------------------------------

    def source_changed(self) -> None:
        """Explicit owner lifecycle fence after a public source replacement."""
        if self._disposed:
            return
        self._cancel_all()
        self._revision += 1
        self._fine_epoch = 0
        self._coarse.clear()
        self._fine.clear()
        self._published_coarse.clear()
        self._published_fine.clear()
        self._unavailable.clear()
        self._last_error = None
        self._source = self.provider.source_identity()
        self._latest_snapshot = self.controller.snapshot()
        self._clear_layer_output()
        self._start_coarse_for_active_channels()
        # Fine is intentionally held until a channel's complete coarse
        # transaction publishes; _publish_current() starts it then.

    def update_viewport(self, snapshot=None) -> None:
        """Immediately plan the public current viewport; never waits for quiet."""
        if self._disposed or self._source is None:
            return
        snapshot = self.controller.snapshot() if snapshot is None else snapshot
        if snapshot.source != self._source:
            # An owner must call source_changed() after rebinding.  Do not
            # infer old/new state or publish potentially wrong pixels.
            self._last_error = "viewport source differs from explicit binding source; source_changed() required"
            return
        self._latest_snapshot = snapshot
        self._begin_fine_epoch(snapshot)
        self._publish_current()

    def refresh_display(self) -> None:
        """Resubmit resident immutable planes; display-only changes issue no I/O."""
        if self._disposed or self._source is None:
            return
        active = set(self._active_channels(self._build_display_snapshot()))
        for channel in active:
            if channel not in self._coarse and channel not in self._unavailable:
                self._start_coarse_channel(channel)
        self._publish_current()

    def dispose(self) -> Dict[str, Any]:
        if self._disposed:
            return {"already_disposed": True, "published_channels": 0, "generations": 0}
        self._disposed = True
        self._motion_timer.stop()
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
        fine_bytes = sum(self._plane_bytes(plane) for planes in self._published_fine.values() for plane in planes)
        return {
            "source": self._source,
            "revision": self._revision,
            "coarse_channels": tuple(sorted(self._published_coarse)),
            "fine_channels": tuple(sorted(self._published_fine)),
            "coarse_plane_bytes": coarse_bytes,
            "fine_plane_bytes": fine_bytes,
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
        if self._disposed:
            return
        self._latest_snapshot = snapshot
        if kind == "NAVIGATOR_JUMP" or not self._motion_timer.isActive():
            self.update_viewport(snapshot)
        if kind != "NAVIGATOR_JUMP":
            self._motion_timer.start(self.budgets.motion_interval_ms)

    def _gesture_quiet(self, snapshot) -> None:
        if not self._disposed:
            self._latest_snapshot = snapshot
            self._flush_motion()

    def _flush_motion(self) -> None:
        if self._latest_snapshot is not None:
            self.update_viewport(self._latest_snapshot)

    # Coarse planning -----------------------------------------------------

    def _start_coarse_for_active_channels(self) -> None:
        for channel in self._active_channels(self._build_display_snapshot()):
            self._start_coarse_channel(channel)

    def _start_coarse_channel(self, channel: str) -> None:
        if self._disposed or channel in self._coarse or channel in self._unavailable:
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
                keys.add(RawKey(source=self._source, channel=channel, tile=address))
        return keys

    # Fine planning -------------------------------------------------------

    def _begin_fine_epoch(self, snapshot) -> None:
        active = self._active_channels(self._build_display_snapshot())
        if not active:
            return
        self._cancel_fine()
        self._fine_epoch += 1
        self._published_fine.clear()
        # One deterministic decision per viewport epoch: a complete-coarse
        # transaction that is still in flight puts this epoch's fine work
        # behind coarse in the existing scheduler's priority order.
        priority = (PRIORITY_FINE_DEFERRED if self.coarse_pending_channels()
                    else PRIORITY_FINE_FOREGROUND)
        self._last_fine_priority = priority
        for channel in active:
            if channel not in self._published_coarse or channel in self._unavailable:
                continue
            keys = self._visible_keys(channel, snapshot)
            expected_bytes = self._planned_bytes(keys)
            if (len(keys) > self.budgets.max_fine_tiles_per_viewport or
                    expected_bytes > self.budgets.max_fine_plane_bytes_per_channel):
                self._last_error = f"fine budget refused for {channel}"
                continue
            generation = self._generation("fine", channel, self._fine_epoch)
            plan = _Plan("fine", channel, self._source, self._revision, generation,
                         set(keys), viewport_epoch=self._fine_epoch, priority=priority)
            self._fine[channel] = plan
            for key in sorted(keys, key=self._key_order):
                self._request(plan, key, priority=plan.priority)

    def _visible_keys(self, channel: str, snapshot) -> Set[RawKey]:
        if snapshot.source != self._source:
            return set()
        level = int(snapshot.level)
        return {
            RawKey(source=self._source, channel=channel,
                   tile=TileAddress(grid=self.controller.grid, level=level, tx=int(tx), ty=int(ty)))
            for tx, ty in snapshot.visible_tiles
        }

    # Scheduler delivery --------------------------------------------------

    def _request(self, plan: _Plan, key: RawKey, *, priority: int) -> None:
        envelope = (plan.tier, plan.channel, plan.source, plan.revision,
                    plan.generation, plan.viewport_epoch, key)
        request = TileRequest(key=key, generation=plan.generation, priority=priority,
                              deadline_ms=None, notify_on_stale_completion=True)

        def callback(result, captured=envelope):
            # May be synchronous or worker-thread. Qt serializes the mutation.
            self._tile_result_received.emit((captured, result))

        self._request_count += 1
        if plan.tier == "fine":
            self._fine_requests_by_priority[priority] = (
                self._fine_requests_by_priority.get(priority, 0) + 1)
        self.scheduler.request(request, callback)

    @QtCore.pyqtSlot(object)
    def _accept_result(self, payload) -> None:
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
        array = np.asarray(result.pixels.handle)
        if array.ndim != 2:
            plan.failed = True
            self._last_error = f"{tier} tile has unsupported shape {array.shape!r}"
            return
        try:
            plane = RawPlane(identity=key, world_rect=self._world_rect(key, array.shape),
                             values=np.asarray(array, dtype=np.float32), valid=np.isfinite(array))
        except Exception as exc:
            plan.failed = True
            self._last_error = f"{tier} tile conversion failed: {exc}"
            return
        plan.planes[key] = plane
        self._accepted_count += 1
        if not plan.complete:
            return
        ordered = tuple(plan.planes[item] for item in sorted(plan.expected, key=self._key_order))
        if tier == "coarse":
            self._published_coarse[channel] = ordered
            self._coarse[channel] = plan
            if self._latest_snapshot is not None:
                self._begin_fine_epoch(self._latest_snapshot)
        else:
            self._published_fine[channel] = ordered
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

    def _publish_current(self) -> None:
        if self._disposed or self._source is None:
            return
        display = self._build_display_snapshot()
        viewport = self._build_viewport_snapshot()
        active = self._active_channels(display)
        channels = []
        for channel in active:
            coarse = self._published_coarse.get(channel)
            if not coarse:
                continue
            fine = self._published_fine.get(channel, ())
            channels.append(ChannelSource(channel, coarse=coarse, fine=tuple(fine),
                                          selected_level="fine" if fine else "coarse"))
        descriptor = SourceDescriptor(tuple(channels))
        try:
            stats = self.layer.submit(descriptor, display, viewport)
        except Step1GpuLayerError as exc:
            self._last_error = f"G1 submission failed: {exc}"
            return
        self._descriptor_history.append((descriptor, display, viewport, stats))

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
            total += tile_h * tile_w * np.dtype(np.float32).itemsize
        return int(total)

    @staticmethod
    def _plane_bytes(plane: RawPlane) -> int:
        return int(np.asarray(plane.values).size * np.dtype(np.float32).itemsize)

    @staticmethod
    def _key_order(key: RawKey):
        return key.tile.level, key.tile.ty, key.tile.tx

    def _missing_reason(self, channel: str) -> str:
        for missing in self.provider.missing_products():
            if missing.channel == channel:
                return missing.reason
        return "Step1 source unavailable"
