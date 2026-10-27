"""v16_5 S-Raw: the Step1 picture drawn by the Rust core (`fusion_viewer_core`).

A QOpenGLWidget laid over the SAME ViewBox as `Step1GpuLayer` (mouse passes
through; it follows the ViewBox's geometry) whose paintGL hands Qt's
framebuffer to `ViewerCore.paint`. Python passes snapshots only; planning,
reading, caching, uploads and drawing are the core's
(docs/v16_5_sraw_plan.md). Only the Rust-mode Step1 mount builds it.

GL contract (plan v2.1 §3): the core is loaded through Qt's own
`getProcAddress`, draws into `defaultFramebufferObject()`, and releases its
names in `aboutToBeDestroyed` with the context current. Calls into the core
keep the GIL (R0: re-taking it behind busy Python threads cost up to
seconds). Redraw notices arrive on a socket pair: drain, ack, then update.
"""

import socket
from typing import Any, Dict, Optional

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets

from ..utils import perf_trace
from ..utils import tile_trace
from ..viewer import coverage_probe
from .core_gl_session import FRAME_MS, CoreGlSession  # noqa: F401 -- FRAME_MS kept importable here
from .step1_core_labels import CoreLabelSurface


class Step1CoreLayerError(RuntimeError):
    pass


class Step1CoreLayer(QtWidgets.QOpenGLWidget, CoreLabelSurface):
    """The Rust-drawn Step1 picture; the mount's `gpu_layer` in Rust mode.
    With `labels`, Step3's masks too (`CoreLabelSurface`, R3)."""

    def __init__(self, core, notice_socket=None, parent=None, labels=False):
        super().__init__(parent)
        self.core = core
        self._disposed = False
        self._initialized = False
        self._init_labels(labels)
        self._initialized = False
        self._init_error: Optional[Exception] = None
        self._disposed = False
        self._capabilities: Dict[str, Any] = {}
        self._attached_view = None
        self._attached_viewport = None
        #: the mount's / gpu_memory's switches, as on Step1GpuLayer
        self.hidden = False
        self.hold_growth = False
        self.upload_budget_ms = None
        self.use_tile_arrays = True
        from . import gpu_memory
        self.smooth = gpu_memory.smooth_pixels()
        #: set by the binding: called when Smooth changes
        self.on_smooth_changed = None
        #: the binding's fallback when a picture needs more blocks than one pass binds
        self.on_capacity = None
        #: the binding's fallback at run time (R3 plan §2.5): failed reads
        #: given up, a paint error, a GL re-initialisation that failed
        self.on_failure = None
        self._failed = ""
        self._ever_initialized = False
        #: set by the binding: the number of channels the frame should draw
        self.expected_channels = lambda: 0
        self.last_report: Dict[str, Any] = {}
        self._a9_pending = None
        # the core's notices, GL calls, paint reports and drawing records
        # (docs/v17_step0_engine_plan.md §10.2); the socket pair is its own
        self._session = CoreGlSession(core, notice_socket, owner=self, on_notice=self._on_notice,
                                      tag=self.a9_tag, channel_name=self._channel_name, parent=self)
        self._again = QtCore.QTimer(self)
        self._again.setSingleShot(True)
        self._again.timeout.connect(self.update)
        self.frameSwapped.connect(self._presented)
        gpu_memory.register(self)
        fmt = QtGui.QSurfaceFormat()
        fmt.setRenderableType(QtGui.QSurfaceFormat.OpenGL)
        fmt.setVersion(3, 3)
        fmt.setProfile(QtGui.QSurfaceFormat.CoreProfile)
        fmt.setSwapInterval(QtGui.QSurfaceFormat.defaultFormat().swapInterval())
        self.setFormat(fmt)

    # ── what the mount asks (Step1GpuLayer's public surface) ──────────
    @property
    def initialized(self) -> bool:
        return bool(self._initialized)

    @property
    def init_error(self) -> Optional[Exception]:
        return self._init_error

    @property
    def tile_arrays_active(self) -> bool:
        return True

    def attach(self, view_adapter) -> None:
        """Sibling overlay on the existing view (as `Step1GpuLayer.attach`)."""
        if self._initialized:
            raise Step1CoreLayerError("attach must happen before the widget is realised")
        self._attached_view = view_adapter
        self._attached_viewport = view_adapter.graphics.viewport()
        if self.parent() is not self._attached_viewport:
            self.setParent(self._attached_viewport)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self._attached_viewport.installEventFilter(self)
        view_adapter.view_box.sigRangeChanged.connect(self._range_changed)
        view_adapter.view_box.sigResized.connect(self._sync_geometry)
        self._sync_geometry()

    def eventFilter(self, watched, event):  # noqa: N802
        if watched is self._attached_viewport and event.type() in (
                QtCore.QEvent.Resize, QtCore.QEvent.Move, QtCore.QEvent.Show):
            self._sync_geometry()
        return False

    def _sync_geometry(self, *_args) -> None:
        """Cover exactly the ViewBox (block A1, C1), as the Python layer."""
        if self._attached_viewport is None:
            return
        rect = self._attached_viewport.rect()
        view = self._attached_view
        view_box = getattr(view, "view_box", None)
        graphics = getattr(view, "graphics", None)
        if view_box is not None and graphics is not None:
            scene_rect = view_box.mapRectToScene(view_box.rect())
            top_left = graphics.mapFromScene(scene_rect.topLeft())
            width, height = int(round(scene_rect.width())), int(round(scene_rect.height()))
            if width > 0 and height > 0:
                rect = QtCore.QRect(top_left.x(), top_left.y(), width, height)
        self.setGeometry(rect)
        self.raise_()

    def _range_changed(self, *_args) -> None:
        if not self.hidden:
            self.update()

    def submit(self, *_args, **_kwargs):
        raise Step1CoreLayerError("the Rust layer takes snapshots, not descriptors")

    def is_resident(self, _plane) -> bool:
        return False

    def environment_report(self) -> Dict[str, Any]:
        return dict(self._capabilities)

    def cache_stats(self) -> Dict[str, Any]:
        if self._disposed:
            return {}
        stats = dict(self.core.stats())
        stats["bytes"] = self.gpu_bytes()
        stats["textures"] = int(stats.get("gpu_tiles", 0))
        return stats

    def gpu_bytes(self) -> int:
        return 0 if self._disposed else int(self.core.gpu_bytes())

    def release_to_coarse(self) -> int:
        if self._disposed or not self._initialized:
            return 0
        self.makeCurrent()
        try:
            return int(self.core.release_to_coarse())
        finally:
            self.doneCurrent()

    def prepare_headroom(self) -> bool:
        """The core reserves its GPU blocks ahead in its own frames: the
        first ones, and again when more channels are drawn, the canvas grows
        or a release dropped blocks -- one per frame, never while input is
        hot (§48, the approved exception to plan §1; no idle timer)."""
        return False

    def array_slack_bytes(self, _itemsizes=(1,)) -> int:
        return 0

    def set_smooth(self, smooth: bool) -> None:
        if bool(smooth) == self.smooth:
            return
        self.smooth = bool(smooth)
        hook = self.on_smooth_changed
        if hook is not None:
            hook()
        self.update()

    # ── Qt lifecycle ──────────────────────────────────────────────────
    @perf_trace.timed("gpu.init")
    def initializeGL(self) -> None:  # noqa: N802
        context = self.context()
        if context is None or not context.isValid():
            self._init_error = Step1CoreLayerError("Qt did not create a valid OpenGL context")
            return
        try:
            context.aboutToBeDestroyed.connect(self._context_going)
            self._session.attach_gl(context)
            self._ever_initialized = True
            fmt = context.format()
            self._capabilities = {"backend": "rust", "gl_version": f"{fmt.majorVersion()}.{fmt.minorVersion()}",
                                  "core": dict(self.core.stats())}
            self._initialized = True
        except Exception as exc:                            # noqa: BLE001 -- reported, fallback
            self._init_error = exc
            if self._ever_initialized:                      # a reparent's new context failed
                self._fail(f"GL re-initialisation failed: {exc}")

    def _context_going(self) -> None:
        """`aboutToBeDestroyed`: release the core's names in that context;
        the next initializeGL (a reparent) builds them again."""
        if self._disposed or not self._initialized:
            return
        self.makeCurrent()
        try:
            self._session.release_gl()
        finally:
            self.doneCurrent()
        self._initialized = False

    def resizeGL(self, width: int, height: int) -> None:  # noqa: N802
        del width, height                                   # the paint passes the size

    def _view_rect(self):
        view_box = getattr(self._attached_view, "view_box", None)
        if view_box is None:
            return None
        (x0, x1), (y0, y1) = view_box.viewRange()
        return (float(x0), float(x1), float(y0), float(y1))

    @perf_trace.timed("gpu.paint")
    def paintGL(self) -> None:  # noqa: N802
        if tile_trace.ON:
            tile_trace.stamp("paint")
        if not self._initialized or self._disposed or self.hidden or self._failed:
            return
        view = self._view_rect()
        if view is None:
            return
        ratio = float(self.devicePixelRatioF()) or 1.0
        width = int(round(self.width() * ratio))
        height = int(round(self.height() * ratio))
        self._labels_follow_ratio()
        session = self._session
        report = session.paint(self.defaultFramebufferObject(), width, height, view)
        self.last_report = report
        failure = session.failure_of(report)
        if failure:
            self._fail(failure)                          # this frame was not shown
            return
        if report.get("capacity_exceeded") and self.on_capacity is not None:
            QtCore.QTimer.singleShot(0, self.on_capacity)
        again = session.again_ms(report)
        if again is not None and not self._again.isActive():
            self._again.start(again)
        session.note_reads()
        self._a9_pending = session.note_frame(report, int(self.expected_channels() or 0),
                                              getattr(self, "_a9_input_seq", None))

    def _fail(self, why: str) -> None:
        """Stop drawing now (the last complete picture stays on screen);
        the binding swaps to the Python path on the next event-loop turn,
        never inside this paint (R3 plan §2.5)."""
        if self._failed:
            return
        self._failed = why
        self.hidden = True
        perf_trace.mark("gpu.core_failed", why=why[:160].replace(" ", "_"))
        hook = self.on_failure
        if hook is not None:
            QtCore.QTimer.singleShot(0, lambda: hook(why))

    @property
    def failed(self) -> str:
        return self._failed

    # ── notices from the core (drained and acknowledged by the session) ──
    def _on_notice(self) -> None:
        if not self.hidden:
            self.update()

    # ── measurement (A9 tools read these; default off) ────────────────
    @property
    def _a9_frame(self) -> int:
        return self._session.frame

    def _presented(self) -> None:
        if tile_trace.ON:
            tile_trace.stamp("present", frame=self._a9_frame, viewer=self.a9_tag())
            self._merge_trace()
        pending, self._a9_pending = self._a9_pending, None
        if pending is None:
            return
        frame, result, seq, _report = pending
        perf_trace.mark("gpu.present", frame=frame, seq=seq, viewer=self.a9_tag())
        coverage_probe.publish(result, "gpu", frame)

    def _merge_trace(self) -> None:
        """The core's stage records into `tile_trace`, with their own times."""
        if self._disposed:
            return
        self._session.merge_trace()

    def a9_tag(self) -> str:
        """Measurement only: this layer's name in exported records."""
        return f"{id(self):x}"

    def _channel_name(self, index):
        names = getattr(self, "channel_names", None)
        return names[index] if names and 0 <= index < len(names) else str(index)

    # ── tests ─────────────────────────────────────────────────────────
    def readback_rgba_for_test(self) -> np.ndarray:
        self.makeCurrent()
        try:
            got = self.core.readback_final()
        finally:
            self.doneCurrent()
        if got is None:
            return np.zeros((0, 0, 4), np.uint8)
        data, width, height = got
        return np.frombuffer(data, np.uint8).reshape(height, width, 4).copy()

    # ── end of life ───────────────────────────────────────────────────
    def dispose(self) -> Dict[str, Any]:
        """Stop notices and reads, release GL in the current context, close
        the socket pair -- the plan's order. Safe twice."""
        if self._disposed:
            return {"already_disposed": True, "raw_textures_remaining": 0,
                    "transient_targets_remaining": 0, "cache_bytes": 0, "threads_created": 0}
        if tile_trace.ON:
            self._merge_trace()
        self._session.close()                            # no notice, no read after this
        self._again.stop()
        if self._attached_view is not None:
            for signal, slot in ((self._attached_view.view_box.sigRangeChanged, self._range_changed),
                                 (self._attached_view.view_box.sigResized, self._sync_geometry)):
                try:
                    signal.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
        if self._initialized and self.context() is not None and self.context().isValid():
            self.makeCurrent()
            try:
                self._session.release_gl()
            finally:
                self.doneCurrent()
        else:
            self._session.forget_gl()
        self._initialized = False
        if tile_trace.ON:
            self._merge_trace()       # R7a: what the GL release itself recorded (lost GPU timers)
        # what is left after the release (Step1GpuLayer's keys: 0 when clean)
        left = dict(self.core.stats())
        remaining = {"raw_textures_remaining": int(left.get("gpu_tiles", 0)),
                     "transient_targets_remaining": int(self.core.gpu_bytes() > 0),
                     "cache_bytes": int(self.core.gpu_bytes())}
        self._disposed = True
        self._session.close_sockets()
        return {"already_disposed": False, "threads_created": 0, **remaining}


def notice_pair():
    """The redraw-notice socket pair: (read end, write end), both owned by
    the layer; the core borrows the write end's handle."""
    read_end, write_end = socket.socketpair()
    read_end.setblocking(False)
    return read_end, write_end
