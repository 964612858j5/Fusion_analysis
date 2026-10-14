"""v16_5 S-Raw: the mount-facing binding of the Rust-drawn Step1 picture.

The same public face as `Step1GpuBinding` (`source_changed`,
`refresh_display`, `pause`, `resume`, `dispose`, `stats`,
`descriptor_history`, `on_status_changed`), but it plans nothing itself:
it turns the ONE controller's camera and the mount's display snapshot into
`ViewerCore.set_view` / `set_display` / `set_polygon` calls. R2 (docs/
v16_5_r2_plan.md): Overlay and Fusion, raw u8/u16/float and corrected
channels, rectangle and polygon regions. It hands the picture back to the
Python GPU path only when the core cannot draw it in one pass (sampler
capacity) or a channel is not in the slide.
"""

import collections
import os
from types import SimpleNamespace
from typing import Any, Callable, Dict, Optional, Tuple

from PyQt5 import QtCore

from ..core import resource_tiers as _tiers
from . import step1_core_adapter as adapter
from .step1_gpu_layer import MODE_FUSION, MODE_OVERLAY, sanitize_roi_polygon

#: the Python binding's timings (step1_gpu_binding: INPUT_HOT_MS,
#: HOT_UPLOAD_BUDGET_MS, GROWTH_QUIET_MS) and the whole-frame budget
INPUT_HOT_S = 0.060
#: R5 (docs/v16_5_r5_zoom_plan.md): the upload hot window, apart from
#: INPUT_HOT_S (which still keeps GPU blocks from being reserved ahead)
UPLOAD_HOT_S = 0.060
#: R5 (A9 §55): 4 ms, candidate D, after the confirmation and the cold
#: drag-right rerun; was 1.5 (the Python path's)
HOT_UPLOAD_MS = 4.0
FRAME_BUDGET_MS = 12.0
GROWTH_QUIET_S = 0.300
READER_THREADS = 6
#: R7b B1.1: decoded corrected chunks kept per viewer, carved out of the
#: corrected tile cache's allowance (`STEP1_VIEWER_CORRECTED_CACHE_BYTES`).
#: Off by default (R7b B1 results: no consistent gain yet); on with
#: `BLOCK01_STEP1_CHUNK_CACHE=1`. Off, the tile cache keeps it all.
CHUNK_CACHE_BYTES = 256 * 1024 * 1024


def _reader_threads():
    """READER_THREADS, or the R7b B1.3 measurement switch."""
    v = os.environ.get("BLOCK01_A9_READER_THREADS")
    return int(v) if v else READER_THREADS


def _chunk_cache_bytes():
    return CHUNK_CACHE_BYTES if os.environ.get("BLOCK01_STEP1_CHUNK_CACHE") == "1" else 0


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


def _pacing():
    """(upload_hot_s, hot_upload_ms): the defaults, or the two measurement
    switches (R5 plan §2; milliseconds, as the §43 switches)."""
    upload_hot_s, hot_upload_ms = UPLOAD_HOT_S, HOT_UPLOAD_MS
    if os.environ.get("BLOCK01_A9_RUST_UPLOAD_HOT_MS"):
        upload_hot_s = float(os.environ["BLOCK01_A9_RUST_UPLOAD_HOT_MS"]) / 1000.0
    if os.environ.get("BLOCK01_A9_RUST_HOT_UPLOAD_MS"):
        hot_upload_ms = float(os.environ["BLOCK01_A9_RUST_HOT_UPLOAD_MS"])
    return upload_hot_s, hot_upload_ms


def core_config(budgets, *, legacy_order=False) -> Dict[str, Any]:
    """`ViewerCore`'s config from the mount's `demo_budgets()`."""
    upload_hot_s, hot_upload_ms = _pacing()
    return {
        "max_raw_texture_bytes": int(budgets.max_raw_texture_bytes),
        "max_fine_tiles": int(budgets.max_fine_tiles_per_viewport),
        "max_fine_bytes_per_channel": int(budgets.max_fine_plane_bytes_per_channel),
        "max_coarse_tiles": int(budgets.max_coarse_tiles_per_channel),
        "max_coarse_bytes_per_channel": (None if budgets.max_coarse_plane_bytes_per_channel is None
                                         else int(budgets.max_coarse_plane_bytes_per_channel)),
        "cpu_cache_bytes": int(_tiers.STEP1_VIEWER_RAW_CACHE_BYTES),
        "corrected_cache_bytes": int(_tiers.STEP1_VIEWER_CORRECTED_CACHE_BYTES),
        "threads": _reader_threads(),
        "chunk_cache_bytes": _chunk_cache_bytes(),
        "input_hot_s": INPUT_HOT_S,
        "upload_hot_s": upload_hot_s,
        "hot_upload_ms": hot_upload_ms,
        "frame_budget_ms": FRAME_BUDGET_MS,
        "growth_quiet_s": GROWTH_QUIET_S,
        "legacy_order": bool(legacy_order),
        # R6 measurement only (A9 §56): with the tile trace on, the core
        # records per paint what it drew and what it held (`frame::drawlog`)
        "draw_log": os.environ.get("BLOCK01_A9_DRAW_LOG") == "1",
        # test only (R2 lifecycle: the sampler-capacity fallback)
        "test_block_units": (int(os.environ["BLOCK01_RUST_TEST_BLOCK_UNITS"])
                             if os.environ.get("BLOCK01_RUST_TEST_BLOCK_UNITS") else None),
    }


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
        self._missing_windows: Tuple[str, ...] = ()
        self._polygon_error = ""
        self._limited = None
        self._views = 0
        self._descriptor_history = collections.deque(maxlen=8)
        layer.channel_names = self._names
        layer.expected_channels = lambda: len(self._active)
        layer.on_smooth_changed = self.refresh_display
        layer.on_capacity = lambda: self._fallback(
            "the drawn channels need more GPU blocks of one format than one pass can bind")
        layer.on_failure = self._fallback
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
        rows, active, missing = self._rows(display)
        self._active = active
        self._display = display
        self._missing_windows = missing
        self.core.set_display(rows, bool(self.layer.smooth), display.mode == MODE_FUSION)
        self._apply_polygon()
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
        table = getattr(self.stack.provider, "source_table", None)
        missing = {m.channel: m.reason for m in (table.missing() if table is not None else ())}
        unavailable = {self._names[i]: missing.get(self._names[i], "coarse budget refused")
                       for i in core.get("unavailable", ()) if 0 <= i < len(self._names)}
        return {
            "backend": "rust",
            "source": None,
            # per channel from the core's planner (fine: holds delivered fine tiles kept for the view)
            "coarse_channels": self._channel_names(core.get("coarse_complete", ())),
            "fine_channels": self._channel_names(core.get("fine_held", ())),
            "coarse_pending": self._channel_names(core.get("coarse_pending", ())),
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
            "missing_windows": self._missing_windows,
            "roi_polygon_error": self._polygon_error,
            "core": core,
        }

    def _channel_names(self, indices) -> Tuple[str, ...]:
        return tuple(sorted(self._names[i] for i in indices if 0 <= i < len(self._names)))

    def idle(self) -> bool:
        return self._disposed or bool(self.core.idle())

    # ── the rows the core draws ───────────────────────────────────────
    def _drawable(self, channel) -> bool:
        """In the slide and with a source (a missing product draws nothing,
        as the Python path publishes no plane for it)."""
        if channel not in self._index:
            return False
        table = getattr(self.stack.provider, "source_table", None)
        return table is None or table.source_of(channel) != "missing"

    def _rows(self, display):
        """`(rows, active channels, missing windows)` as the Python layer
        composes them (`Step1GpuLayer._compose`): Overlay one row per
        channel with a weight and a window, in name order; Fusion the
        groups in their configured order (a group of weight 0 skipped),
        each group's channels in their configured order, then the nucleus
        (group -1). A channel may be in several groups (several rows)."""
        rows, active, missing = [], [], []

        def row(channel, weight, color, group, group_weight):
            lo, hi, gamma = (float(v) for v in display.mappings[channel])
            r, g, b = (float(v) for v in color)
            rows.append((self._index[channel], lo, hi, gamma, r, g, b, float(weight),
                         float(group), float(group_weight)))
            if channel not in active:
                active.append(channel)

        if display.mode == MODE_FUSION:
            index = 0
            for group, weights in display.groups.items():
                group_weight = min(1.0, max(0.0, float(display.group_weights.get(group, 1.0) or 0.0)))
                if group_weight <= 0.0:
                    continue
                members = 0
                for channel, weight in weights.items():
                    if float(weight or 0.0) <= 0.0 or not self._drawable(channel):
                        continue
                    if channel not in display.mappings:
                        missing.append(channel)
                        continue
                    row(channel, min(1.0, max(0.0, float(weight))), (1.0, 0.0, 0.0), index, group_weight)
                    members += 1
                index += 1 if members else 0
            nucleus, nucleus_weight = display.nucleus
            if nucleus and float(nucleus_weight or 0.0) > 0.0 and self._drawable(nucleus):
                if nucleus not in display.mappings:
                    missing.append(nucleus)
                else:
                    row(nucleus, min(1.0, max(0.0, float(nucleus_weight))), (0.0, 0.0, 1.0), -1, 0.0)
        else:
            for channel in sorted(display.weights):
                weight = float(display.weights.get(channel, 0.0) or 0.0)
                if weight <= 0.0 or not self._drawable(channel):
                    continue
                if channel not in display.mappings:
                    missing.append(channel)
                    continue
                row(channel, min(1.0, weight), display.colors.get(channel, (1.0, 1.0, 1.0)), 0, 0.0)
        return rows, tuple(active), tuple(missing)

    def _apply_polygon(self) -> None:
        """The region polygon as `_finalize` takes it: checked against the
        rectangle (`sanitize_roi_polygon`); refused -> the rectangle alone
        clips and the reason is reported."""
        viewport = self._viewport_source()
        polygon, error = sanitize_roi_polygon(viewport.roi_polygon_world, viewport.roi_world_rect)
        self._polygon_error = error
        self.core.set_polygon(None if polygon is None else [(float(x), float(y)) for x, y in polygon])

    def _unsupported_reason(self, display) -> str:
        names = list(display.weights) if display.mode != MODE_FUSION else \
            [c for weights in display.groups.values() for c in weights] + [display.nucleus[0]]
        for channel in names:
            if channel and channel not in self._index:
                return f"channel {channel!r} is not in the slide"
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
    import fusion_viewer_core as fvc
    from .step1_core_layer import Step1CoreLayer, notice_pair

    if legacy_order is None:
        # measurement only (plan §3.5): request fine tiles in the old
        # channel-by-channel order, to tell the engine's gain from the order's
        legacy_order = os.environ.get("BLOCK01_A9_RUST_LEGACY_ORDER") == "1"

    raw = raw_provider_of(stack.provider)
    if raw is None:
        raise RuntimeError("Step1's provider is not a raw OME-TIFF pyramid")
    manifest = adapter.build_raw_manifest(raw)
    table = getattr(stack.provider, "source_table", None)
    roi = table.roi_bbox() if table is not None else None
    sources = None
    if table is not None:
        strides = [max(1, int(round(raw.level_downsample(level)))) for level in range(raw.num_levels)]
        sources = adapter.build_corrected_manifest(table, raw, strides)
    sockets = notice_pair()
    try:
        from ..utils import perf_trace, tile_trace
        config = core_config(budgets, legacy_order=legacy_order)
        if perf_trace.enabled():      # which pacing was measured (R5 plan §2)
            perf_trace.mark("rust.config", **{k: config[k] for k in (
                "input_hot_s", "upload_hot_s", "hot_upload_ms", "frame_budget_ms", "threads",
                "chunk_cache_bytes")})
        core = fvc.ViewerCore(manifest, config,
                              tuple(int(v) for v in roi) if roi else None,
                              sockets[1].fileno(), bool(tile_trace.ON), sources)
    except Exception:
        for end in sockets:
            end.close()
        raise
    layer = Step1CoreLayer(core, sockets, labels=bool(getattr(mount, "_labels_wanted", False)))
    binding_args = dict(stack=stack, layer=layer, core=core, channel_names=raw.channel_names,
                        build_display_snapshot=mount._gpu_display_snapshot,
                        build_viewport_snapshot=mount._gpu_viewport_snapshot,
                        on_unsupported=mount._rust_unsupported)
    return layer, binding_args
