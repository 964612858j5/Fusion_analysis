"""v16_5 S-Raw: the mount-facing binding of the Rust-drawn Step1 picture.

The same public face as `Step1GpuBinding` (`source_changed`,
`refresh_display`, `pause`, `resume`, `dispose`, `stats`,
`descriptor_history`, `on_status_changed`), but it plans nothing itself:
it turns the ONE controller's camera and the mount's display snapshot into
`ViewerCore.set_view` / `set_display` calls, and says when the state has
left what the thin slice draws (Overlay, raw uint8 channels, a
whole-slide region) so the mount can hand the picture back to the Python GPU
path (docs/v16_5_sraw_plan.md §1, §6).
"""

import collections
from types import SimpleNamespace
from typing import Any, Callable, Dict, Optional, Tuple

from PyQt5 import QtCore

from ..core import resource_tiers as _tiers
from . import step1_core_adapter as adapter
from .step1_gpu_layer import MODE_OVERLAY

#: the Python binding's timings (step1_gpu_binding: INPUT_HOT_MS,
#: HOT_UPLOAD_BUDGET_MS, GROWTH_QUIET_MS) and the whole-frame budget
INPUT_HOT_S = 0.060
HOT_UPLOAD_MS = 1.5
FRAME_BUDGET_MS = 12.0
GROWTH_QUIET_S = 0.300
READER_THREADS = 6


def raw_provider_of(provider):
    """The `RawTileProvider` under Step1's provider wrappers, or None."""
    from ..viewer.raw_tile_provider import RawTileProvider
    seen = 0
    while provider is not None and seen < 5:
        if isinstance(provider, RawTileProvider):
            return provider
        provider = provider.__dict__.get("_raw")
        seen += 1
    return None


def core_config(budgets, *, legacy_order=False) -> Dict[str, Any]:
    """`ViewerCore`'s config from the mount's `demo_budgets()`."""
    return {
        "max_raw_texture_bytes": int(budgets.max_raw_texture_bytes),
        "max_fine_tiles": int(budgets.max_fine_tiles_per_viewport),
        "max_fine_bytes_per_channel": int(budgets.max_fine_plane_bytes_per_channel),
        "max_coarse_tiles": int(budgets.max_coarse_tiles_per_channel),
        "cpu_cache_bytes": int(_tiers.STEP1_VIEWER_RAW_CACHE_BYTES),
        "threads": READER_THREADS,
        "input_hot_s": INPUT_HOT_S,
        "hot_upload_ms": HOT_UPLOAD_MS,
        "frame_budget_ms": FRAME_BUDGET_MS,
        "growth_quiet_s": GROWTH_QUIET_S,
        "legacy_order": bool(legacy_order),
    }


def polygon_is_rect(polygon, rect) -> bool:
    """Is the region's drawn shape just its rectangle (4 corners)?"""
    if polygon is None:
        return True
    try:
        points = {(float(x), float(y)) for x, y in polygon}
    except (TypeError, ValueError):
        return False
    x0, x1, y0, y1 = rect
    return points <= {(x0, y0), (x1, y0), (x1, y1), (x0, y1)} and len(points) == 4


class Step1CoreBinding(QtCore.QObject):
    """Snapshots in, nothing planned here (see the module docstring)."""

    def __init__(self, *, stack, layer, core, channel_names,
                 build_display_snapshot: Callable[[], Any],
                 build_viewport_snapshot: Callable[[], Any],
                 on_unsupported: Callable[[str], None], parent=None):
        super().__init__(parent)
        self.stack = stack
        self.controller = stack.controller
        self.layer = layer
        self.core = core
        self._names = tuple(channel_names)
        raw = raw_provider_of(stack.provider)
        self._shape0 = raw.level_shape(0) if raw is not None else stack.provider.level_shape(0)
        self._index = {name: i for i, name in enumerate(self._names)}
        self._display_source = build_display_snapshot
        self._viewport_source = build_viewport_snapshot
        self._on_unsupported = on_unsupported
        self.on_status_changed: Optional[Callable[[], None]] = None
        self._paused = False
        self._disposed = False
        self._unsupported = ""
        self._active: Tuple[str, ...] = ()
        self._display = None
        self._limited = None
        self._views = 0
        self._descriptor_history = collections.deque(maxlen=8)
        layer.channel_names = self._names
        layer.expected_channels = lambda: len(self._active)
        layer.on_smooth_changed = self.refresh_display
        self._connect_controller()

    # ── the mount's lifecycle calls ───────────────────────────────────
    def source_changed(self) -> None:
        """First picture of this source: display, then the current view."""
        if self._disposed or self._paused:
            return
        if self.refresh_display():
            self._set_view(self.controller.snapshot(), input_=False)

    def refresh_display(self) -> bool:
        """Push the display as it now reads; False (and a fallback request)
        when it is outside what the thin slice draws."""
        if self._disposed or self._paused:
            return False
        display = self._display_source()
        why = self._unsupported_reason(display)
        if why:
            self._fallback(why)
            return False
        active = tuple(sorted(c for c, w in display.weights.items()
                              if float(w or 0.0) > 0.0 and c in display.mappings))
        rows = []
        for channel in active:
            lo, hi, gamma = (float(v) for v in display.mappings[channel])
            r, g, b = (float(v) for v in display.colors.get(channel, (1.0, 1.0, 1.0)))
            weight = min(1.0, float(display.weights.get(channel, 0.0) or 0.0))
            rows.append((self._index[channel], lo, hi, gamma, r, g, b, weight))
        self._active = active
        self._display = display
        self.core.set_display(rows, bool(self.layer.smooth))
        self._descriptor_history.append(
            (SimpleNamespace(channels=tuple(SimpleNamespace(channel=c) for c in active)), display, None, {}))
        self._status_check()
        self.layer.update()
        return True

    def pause(self) -> bool:
        if self._disposed or self._paused:
            return False
        self._paused = True
        self._disconnect_controller()
        self.core.pause()
        self.layer.hidden = True
        return True

    def resume(self) -> bool:
        if self._disposed or not self._paused:
            return False
        self._paused = False
        self.layer.hidden = False
        from . import gpu_memory
        gpu_memory.on_layer_active(self.layer)
        self._connect_controller()
        self.core.resume()
        if self.refresh_display():
            self._set_view(self.controller.snapshot(), input_=False)
        return True

    @property
    def paused(self) -> bool:
        return self._paused

    def dispose(self) -> Dict[str, Any]:
        if self._disposed:
            return {"already_disposed": True, "published_channels": 0, "generations": 0}
        self._disposed = True
        self._disconnect_controller()
        return {"already_disposed": False, "published_channels": 0, "generations": 0}

    # ── what the mount, the main window and the A9 tools read ─────────
    @property
    def descriptor_history(self):
        return tuple(self._descriptor_history)

    @property
    def publication_count(self) -> int:
        return int(self.layer.last_report.get("frame", 0) or 0)

    def stats(self) -> Dict[str, Any]:
        core = {} if self._disposed else dict(self.core.stats())
        report = self.layer.last_report or {}
        unavailable = {self._names[i]: "coarse budget refused"
                       for i in core.get("unavailable", ()) if 0 <= i < len(self._names)}
        return {
            "backend": "rust",
            "source": None,
            "coarse_channels": self._active if report.get("channels") else (),
            "fine_channels": self._active if report.get("wanted") else (),
            "coarse_pending": (),
            "requests": int(core.get("reads", 0)),
            "accepted_results": int(core.get("held_tiles", 0)),
            "rejected_late_results": 0,
            "ideal_level": core.get("ideal_level"),
            "admitted_level": core.get("admitted_level"),
            "resolution_limited": bool(core.get("resolution_limited", False)),
            "base_overflow_channels": int(core.get("base_overflow_channels", 0) or 0),
            "unavailable": unavailable,
            "last_error": core.get("last_error") or None,
            "descriptor_publications": self.publication_count,
            "deferred_uploads": int(report.get("deferred", 0) or 0),
            "unsupported": self._unsupported,
            "core": core,
        }

    def idle(self) -> bool:
        return self._disposed or bool(self.core.idle())

    # ── the thin slice's range ────────────────────────────────────────
    def _unsupported_reason(self, display) -> str:
        if display.mode != MODE_OVERLAY:
            return f"display mode {display.mode!r} (the Rust layer draws Overlay only)"
        table = getattr(self.stack.provider, "source_table", None)
        for channel, weight in display.weights.items():
            if float(weight or 0.0) <= 0.0 or channel not in display.mappings:
                continue
            if channel not in self._index:
                return f"channel {channel!r} is not in the slide"
            source = table.source_of(channel) if table is not None else "raw"
            if source != "raw":
                return f"channel {channel!r} is {source}, not raw"
        viewport = self._viewport_source()
        roi = viewport.roi_world_rect
        if roi is not None:
            h0, w0 = self._shape0
            if tuple(float(v) for v in roi) != (0.0, float(w0), 0.0, float(h0)):
                return "the region is not the whole slide (the thin slice draws whole-slide regions only)"
            if not polygon_is_rect(viewport.roi_polygon_world, roi):
                return "the region is a polygon (the thin slice draws whole-slide regions only)"
        return ""

    def _fallback(self, why: str) -> None:
        if self._unsupported:
            return
        self._unsupported = why
        self._disconnect_controller()
        QtCore.QTimer.singleShot(0, lambda: self._on_unsupported(why))

    # ── the controller's camera ───────────────────────────────────────
    def _connect_controller(self) -> None:
        self.controller.interaction_event.connect(self._interaction_event)
        self.controller.gesture_quiet.connect(self._gesture_quiet)

    def _disconnect_controller(self) -> None:
        for signal, slot in ((self.controller.interaction_event, self._interaction_event),
                             (self.controller.gesture_quiet, self._gesture_quiet)):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass

    def _interaction_event(self, _kind, snapshot) -> None:
        self._set_view(snapshot, input_=True)

    def _gesture_quiet(self, snapshot) -> None:
        self._set_view(snapshot, input_=False)

    def _set_view(self, snapshot, *, input_: bool) -> None:
        if self._disposed or self._paused or self._unsupported:
            return
        bbox = getattr(snapshot, "bbox_l0", None)
        if bbox is None:
            return
        self.core.set_view(int(snapshot.level), tuple(int(v) for v in bbox),
                           int(snapshot.epoch), bool(input_))
        self._views += 1
        self._status_check()
        self.layer.update()

    def _status_check(self) -> None:
        limited = bool(self.core.stats().get("resolution_limited", False))
        if limited != self._limited:
            self._limited = limited
            hook = self.on_status_changed
            if hook is not None:
                QtCore.QTimer.singleShot(0, hook)


def build(mount, stack, budgets, *, legacy_order=None):
    """(layer, binding) for `stack`, or raise with the reason the Rust
    backend cannot start (the mount then takes the Python GPU path)."""
    import os
    import fusion_viewer_core as fvc
    from .step1_core_layer import Step1CoreLayer, notice_pair

    if legacy_order is None:
        # measurement only (plan §3.5): request fine tiles in the old
        # channel-by-channel order, to tell the engine's gain from the order's
        legacy_order = os.environ.get("BLOCK01_A9_RUST_LEGACY_ORDER") == "1"

    raw = raw_provider_of(stack.provider)
    if raw is None:
        raise RuntimeError("Step1's provider is not a raw OME-TIFF pyramid")
    if str(getattr(raw, "_dtype", "")) != "uint8":
        raise RuntimeError(f"the slide is {raw._dtype}, the Rust layer draws uint8 only")
    manifest = adapter.build_raw_manifest(raw)
    table = getattr(stack.provider, "source_table", None)
    roi = table.roi_bbox() if table is not None else None
    sockets = notice_pair()
    try:
        from ..utils import tile_trace
        core = fvc.ViewerCore(manifest, core_config(budgets, legacy_order=legacy_order),
                              tuple(int(v) for v in roi) if roi else None,
                              sockets[1].fileno(), bool(tile_trace.ON))
    except Exception:
        for end in sockets:
            end.close()
        raise
    layer = Step1CoreLayer(core, sockets)
    binding_args = dict(stack=stack, layer=layer, core=core, channel_names=raw.channel_names,
                        build_display_snapshot=mount._gpu_display_snapshot,
                        build_viewport_snapshot=mount._gpu_viewport_snapshot,
                        on_unsupported=mount._rust_unsupported)
    return layer, binding_args
