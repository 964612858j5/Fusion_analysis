"""Step1 G1 test-only GPU composition layer.

This is an isolated validation renderer, not a production display path.  It
owns only Qt-context-local GL names and a bounded GPU texture cache.  Callers
supply immutable source/display/viewport snapshots; this module never imports
or queries the Step1 coordinator, provider, scheduler, ViewBox controller,
shared display state, MainWindow, or the C1 CPU composer.

Qt owns the widget/context/event lifecycle.  PyOpenGL is imported lazily only
from ``initializeGL`` while this widget's exact Qt context is current.
"""

from __future__ import annotations

import collections
import dataclasses
import pathlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Hashable, Mapping, Optional, Sequence, Tuple

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets


MODE_OVERLAY = "overlay"
MODE_FUSION = "fusion"
_SHADER_DIR = pathlib.Path(__file__).with_name("shaders")


class Step1GpuLayerError(RuntimeError):
    """A diagnostic G1 renderer failure; callers must not silently fall back."""


@dataclass(frozen=True)
class RawPlane:
    """One caller-owned raw plane and its existing identity/geometry."""

    identity: Hashable
    world_rect: Tuple[float, float, float, float]
    values: np.ndarray
    valid: Optional[np.ndarray] = None


@dataclass(frozen=True)
class ChannelSource:
    """Caller-selected per-channel coarse/fine data; no source lookup occurs here."""

    channel: str
    coarse: Tuple[RawPlane, ...] = ()
    fine: Tuple[RawPlane, ...] = ()
    selected_level: str = "coarse"

    def selected_planes(self) -> Tuple[RawPlane, ...]:
        if self.selected_level == "coarse":
            return self.coarse
        if self.selected_level == "fine":
            # Fine draws after coarse so valid fine coverage replaces that
            # channel's coarse signal only; another channel is independent.
            return self.coarse + self.fine
        raise Step1GpuLayerError(f"unknown selected level {self.selected_level!r}")


@dataclass(frozen=True)
class SourceDescriptor:
    channels: Tuple[ChannelSource, ...]

    def by_channel(self) -> Dict[str, ChannelSource]:
        result = {item.channel: item for item in self.channels}
        if len(result) != len(self.channels):
            raise Step1GpuLayerError("source descriptor contains duplicate channel names")
        return result


@dataclass(frozen=True)
class DisplaySnapshot:
    """A complete caller-owned C1 display input, not mutable GPU state."""

    mode: str
    mappings: Mapping[str, Tuple[float, float, float]]
    weights: Mapping[str, float] = field(default_factory=dict)
    colors: Mapping[str, Tuple[float, float, float]] = field(default_factory=dict)
    groups: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    group_weights: Mapping[str, float] = field(default_factory=dict)
    nucleus: Tuple[str, float] = ("", 0.0)


@dataclass(frozen=True)
class ViewportSnapshot:
    """Read-only world and physical-output requirements from the current owner."""

    world_rect: Tuple[float, float, float, float]
    logical_size: Tuple[int, int]
    device_pixel_ratio: float = 1.0
    repaint_request: Optional[Any] = None

    @property
    def physical_size(self) -> Tuple[int, int]:
        width = max(1, int(round(self.logical_size[0] * self.device_pixel_ratio)))
        height = max(1, int(round(self.logical_size[1] * self.device_pixel_ratio)))
        return width, height


@dataclass
class _TextureRecord:
    texture: int
    width: int
    height: int
    byte_count: int
    world_rect: Tuple[float, float, float, float]


class _TextureLru:
    """A context-local raw texture LRU keyed only by supplied plane identity."""

    def __init__(self, max_bytes: int):
        if max_bytes <= 0:
            raise ValueError("max_raw_texture_bytes must be positive")
        self.max_bytes = int(max_bytes)
        self.records: "collections.OrderedDict[Hashable, _TextureRecord]" = collections.OrderedDict()
        self.bytes = 0
        self.peak_bytes = 0
        self.peak_textures = 0
        self.hits = 0
        self.misses = 0
        self.uploads = 0
        self.evictions = 0
        self.upload_submit_ms = []

    @staticmethod
    def plane_bytes(plane: RawPlane) -> int:
        values = np.asarray(plane.values)
        if values.ndim != 2:
            raise Step1GpuLayerError("raw plane values must be two dimensional")
        return int(values.shape[0] * values.shape[1] * np.dtype(np.float32).itemsize)

    def _validate_identity(self, plane: RawPlane) -> None:
        try:
            hash(plane.identity)
        except TypeError as exc:
            raise Step1GpuLayerError("raw plane identity must be caller-supplied and hashable") from exc
        x0, x1, y0, y1 = plane.world_rect
        if not x1 > x0 or not y1 > y0:
            raise Step1GpuLayerError("raw plane world rect must have positive extent")
        values = np.asarray(plane.values)
        if values.ndim != 2 or values.shape[0] == 0 or values.shape[1] == 0:
            raise Step1GpuLayerError("raw plane values must be a nonempty HxW array")
        if plane.valid is not None and np.asarray(plane.valid).shape != values.shape:
            raise Step1GpuLayerError("raw plane valid mask shape must match values")

    def prepare(self, gl, planes: Sequence[RawPlane]) -> None:
        """Make a submitted active set resident or fail before issuing passes."""
        required: Dict[Hashable, RawPlane] = {}
        for plane in planes:
            self._validate_identity(plane)
            previous = required.get(plane.identity)
            if previous is not None:
                if (np.asarray(previous.values).shape != np.asarray(plane.values).shape or
                        previous.world_rect != plane.world_rect):
                    raise Step1GpuLayerError("one supplied identity has incompatible raw geometry")
            required[plane.identity] = plane
        total_required = sum(self.plane_bytes(plane) for plane in required.values())
        if total_required > self.max_bytes:
            raise Step1GpuLayerError(
                f"active raw working set {total_required} bytes exceeds cache budget {self.max_bytes}"
            )
        for identity, plane in required.items():
            record = self.records.get(identity)
            if record is not None:
                expected = self.plane_bytes(plane)
                shape = np.asarray(plane.values).shape
                if (record.byte_count != expected or record.height != shape[0] or
                        record.width != shape[1] or record.world_rect != plane.world_rect):
                    raise Step1GpuLayerError("resubmitted identity has incompatible raw texture metadata")
        missing_bytes = sum(self.plane_bytes(plane) for identity, plane in required.items()
                            if identity not in self.records)
        while self.bytes + missing_bytes > self.max_bytes:
            victim = next((identity for identity in self.records if identity not in required), None)
            if victim is None:
                raise Step1GpuLayerError("cache cannot fit active raw working set without deleting active texture")
            record = self.records.pop(victim)
            gl.glDeleteTextures([record.texture])
            self.bytes -= record.byte_count
            self.evictions += 1
        for identity, plane in required.items():
            if identity in self.records:
                self.records.move_to_end(identity)
                self.hits += 1
                continue
            self.misses += 1
            values = np.ascontiguousarray(np.asarray(plane.values), dtype=np.float32).copy()
            if plane.valid is not None:
                values[~np.asarray(plane.valid, dtype=bool)] = np.nan
            upload_started = time.perf_counter()
            texture = _as_name(gl.glGenTextures(1))
            gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
            gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_R32F, values.shape[1], values.shape[0],
                            0, gl.GL_RED, gl.GL_FLOAT, values)
            self.upload_submit_ms.append((time.perf_counter() - upload_started) * 1000.0)
            byte_count = self.plane_bytes(plane)
            self.records[identity] = _TextureRecord(
                texture=texture, width=values.shape[1], height=values.shape[0],
                byte_count=byte_count, world_rect=plane.world_rect,
            )
            self.bytes += byte_count
            self.peak_bytes = max(self.peak_bytes, self.bytes)
            self.peak_textures = max(self.peak_textures, len(self.records))
            self.uploads += 1
        _check_gl(gl, "raw texture upload")

    def texture_for(self, plane: RawPlane) -> int:
        record = self.records.get(plane.identity)
        if record is None:
            raise Step1GpuLayerError("submitted raw plane is not resident")
        self.records.move_to_end(plane.identity)
        return record.texture

    def clear(self, gl) -> int:
        count = len(self.records)
        if self.records:
            gl.glDeleteTextures([record.texture for record in self.records.values()])
        self.records.clear()
        self.bytes = 0
        return count

    def stats(self) -> Dict[str, int]:
        return {
            "budget_bytes": self.max_bytes,
            "bytes": self.bytes,
            "peak_bytes": self.peak_bytes,
            "textures": len(self.records),
            "peak_textures": self.peak_textures,
            "hits": self.hits,
            "misses": self.misses,
            "uploads": self.uploads,
            "upload_submit_ms": tuple(self.upload_submit_ms),
            "evictions": self.evictions,
        }


def _as_name(value) -> int:
    if isinstance(value, (tuple, list, np.ndarray)):
        return int(value[0])
    return int(value)


def _check_gl(gl, operation: str) -> None:
    error = gl.glGetError()
    if error != gl.GL_NO_ERROR:
        raise Step1GpuLayerError(f"OpenGL error after {operation}: 0x{int(error):04x}")


def _decode(value) -> str:
    return "unavailable" if value is None else bytes(value).decode("ascii", "replace")


class Step1GpuLayer(QtWidgets.QOpenGLWidget):
    """A test-only G1 multi-pass GPU compositor with no production import path."""

    def __init__(self, *, max_raw_texture_bytes: int, require_hardware: bool = True,
                 parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self._cache = _TextureLru(max_raw_texture_bytes)
        self._require_hardware = bool(require_hardware)
        self._gl = None
        self._programs: Dict[str, int] = {}
        self._uniforms: Dict[str, Dict[str, int]] = {}
        self._vao = 0
        self._targets: Dict[str, Tuple[int, int]] = {}
        self._target_size: Optional[Tuple[int, int]] = None
        self._initialized = False
        self._init_error: Optional[Exception] = None
        self._disposed = False
        self._capabilities: Dict[str, Any] = {}
        self._submission: Dict[str, Any] = {}
        self._attached_view = None
        self._attached_viewport = None
        self._attached_range = None
        fmt = QtGui.QSurfaceFormat()
        fmt.setRenderableType(QtGui.QSurfaceFormat.OpenGL)
        fmt.setVersion(3, 3)
        fmt.setProfile(QtGui.QSurfaceFormat.CoreProfile)
        self.setFormat(fmt)

    # Public G1 boundary -------------------------------------------------

    @property
    def initialized(self) -> bool:
        """Did Qt realize this widget's context and did G1 set itself up?

        Read-only. A caller that must decide between the GPU backend and the
        existing CPU path asks this instead of reaching for private state,
        and it is never allowed to report success on a failed setup.
        """
        return bool(self._initialized)

    @property
    def init_error(self) -> Optional[Exception]:
        """Why realization failed, exactly as raised. None while it has not."""
        return self._init_error

    def attach(self, view_adapter) -> None:
        """Sibling overlay attachment on an existing view; it never changes camera/input state."""
        if self._initialized:
            raise Step1GpuLayerError("attach must happen before QOpenGLWidget realization")
        if not hasattr(view_adapter, "graphics") or not hasattr(view_adapter, "view_box"):
            raise Step1GpuLayerError("attach expects an existing ExploreView-like adapter")
        self._attached_view = view_adapter
        self._attached_viewport = view_adapter.graphics.viewport()
        if self.parent() is not self._attached_viewport:
            self.setParent(self._attached_viewport)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self._attached_viewport.installEventFilter(self)
        view_adapter.view_box.sigRangeChanged.connect(self._attached_range_changed)
        self._sync_attached_geometry()

    def submit(self, source_descriptor: SourceDescriptor, display_snapshot: DisplaySnapshot,
               viewport_snapshot: ViewportSnapshot) -> Dict[str, Any]:
        """Synchronously render caller-provided snapshots; no I/O or CPU fallback."""
        self._validate_snapshots(source_descriptor, display_snapshot, viewport_snapshot)
        own_current = QtGui.QOpenGLContext.currentContext() is not self.context()
        if own_current:
            self.makeCurrent()
        try:
            self._require_ready()
            submit_started = time.perf_counter()
            gl = self._gl
            self._ensure_targets(viewport_snapshot.physical_size)
            self._submission = {"pass_count": 0}
            by_channel = source_descriptor.by_channel()
            mode = display_snapshot.mode
            if mode == MODE_OVERLAY:
                active, missing = self._overlay_active(by_channel, display_snapshot)
                self._cache.prepare(gl, [plane for source in active.values() for plane in source.selected_planes()])
                self._render_overlay(active, display_snapshot, viewport_snapshot)
            elif mode == MODE_FUSION:
                active_groups, active_nucleus, missing = self._fusion_active(by_channel, display_snapshot)
                planes = []
                for sources in active_groups.values():
                    for source in sources.values():
                        planes.extend(source.selected_planes())
                if active_nucleus is not None:
                    planes.extend(active_nucleus.selected_planes())
                self._cache.prepare(gl, planes)
                self._render_fusion(active_groups, active_nucleus, display_snapshot, viewport_snapshot)
            else:
                raise Step1GpuLayerError(f"unknown display mode {mode!r}")
            self._submission = {
                "mode": mode,
                "missing_windows": tuple(missing),
                "pass_count": int(self._submission.get("pass_count", 0)),
                "cpu_submit_ms": (time.perf_counter() - submit_started) * 1000.0,
                "cache": self._cache.stats(),
                "physical_size": viewport_snapshot.physical_size,
            }
            if callable(viewport_snapshot.repaint_request):
                viewport_snapshot.repaint_request()
            self.update()
            _check_gl(gl, "G1 submission")
            return dict(self._submission)
        finally:
            if own_current:
                self.doneCurrent()

    def readback_rgba_for_test(self) -> np.ndarray:
        """Return the final RGBA8 FBO, flipped once to C1 top-left rows."""
        own_current = QtGui.QOpenGLContext.currentContext() is not self.context()
        if own_current:
            self.makeCurrent()
        try:
            self._require_ready()
            if self._target_size is None:
                raise Step1GpuLayerError("submit before requesting test readback")
            gl = self._gl
            final_fbo, _final_texture = self._targets["final"]
            width, height = self._target_size
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, final_fbo)
            gl.glPixelStorei(gl.GL_PACK_ALIGNMENT, 1)
            pixels = gl.glReadPixels(0, 0, width, height, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE)
            result = np.frombuffer(pixels, dtype=np.uint8).reshape(height, width, 4).copy()
            _check_gl(gl, "G1 framebuffer readback")
            return np.flipud(result)
        finally:
            if own_current:
                self.doneCurrent()

    def dispose(self) -> Dict[str, Any]:
        """Delete every G1 GL name under the active Qt context; safe twice."""
        if self._disposed:
            return {"already_disposed": True, "raw_textures_remaining": 0,
                    "transient_targets_remaining": 0, "cache_bytes": 0, "threads_created": 0}
        self._disconnect_attachment()
        if self.context() is not None and self.context().isValid():
            self.makeCurrent()
            try:
                self._destroy_gl_names()
            finally:
                self.doneCurrent()
        self._disposed = True
        return {"already_disposed": False, "raw_textures_remaining": len(self._cache.records),
                "transient_targets_remaining": len(self._targets), "cache_bytes": self._cache.bytes,
                "threads_created": 0}

    def cache_stats(self) -> Dict[str, int]:
        return self._cache.stats()

    def environment_report(self) -> Dict[str, Any]:
        return dict(self._capabilities)

    # Qt lifecycle -------------------------------------------------------

    def initializeGL(self) -> None:  # noqa: N802
        context = self.context()
        if context is None or not context.isValid():
            self._init_error = Step1GpuLayerError("Qt did not create a valid G1 OpenGL context")
            return
        try:
            from OpenGL import GL
            import OpenGL
            self._gl = GL
            actual = context.format()
            renderer = _decode(GL.glGetString(GL.GL_RENDERER))
            vendor = _decode(GL.glGetString(GL.GL_VENDOR))
            renderer_lower = f"{vendor} {renderer}".lower()
            self._capabilities = {
                "pyopengl_version": OpenGL.__version__,
                "pyopengl_file": OpenGL.__file__,
                "gl_vendor": vendor,
                "gl_renderer": renderer,
                "gl_version": _decode(GL.glGetString(GL.GL_VERSION)),
                "glsl_version": _decode(GL.glGetString(GL.GL_SHADING_LANGUAGE_VERSION)),
                "actual_format": {"major": actual.majorVersion(), "minor": actual.minorVersion(),
                                  "profile": int(actual.profile())},
                "max_texture_image_units": int(GL.glGetIntegerv(GL.GL_MAX_TEXTURE_IMAGE_UNITS)),
                "max_texture_size": int(GL.glGetIntegerv(GL.GL_MAX_TEXTURE_SIZE)),
                "software_renderer": any(item in renderer_lower for item in ("llvmpipe", "softpipe", "swiftshader", "software")),
                "raw_texture_format": "GL_R32F / GL_RED / GL_FLOAT",
                "transient_format": "GL_RGBA32F",
            }
            if actual.majorVersion() < 3:
                raise Step1GpuLayerError("G1 requires an OpenGL 3.3 Core-capable Qt context")
            if self._require_hardware and self._capabilities["software_renderer"]:
                raise Step1GpuLayerError(f"G1 requires hardware renderer, got {renderer!r}")
            self._compile_programs()
            self._vao = _as_name(GL.glGenVertexArrays(1))
            _check_gl(GL, "G1 shader setup")
            self._initialized = True
        except (ImportError, RuntimeError, Step1GpuLayerError) as exc:
            self._init_error = exc if isinstance(exc, Step1GpuLayerError) else Step1GpuLayerError(str(exc))
            self._destroy_gl_names()

    def resizeGL(self, width: int, height: int) -> None:  # noqa: N802
        # FBO allocation follows explicit viewport snapshots in submit().
        # Qt logical resize alone is deliberately not a source/camera owner.
        del width, height

    def paintGL(self) -> None:  # noqa: N802
        if not self._initialized or self._target_size is None:
            return
        gl = self._gl
        final_fbo, final_texture = self._targets["final"]
        gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, final_fbo)
        gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, self.defaultFramebufferObject())
        width, height = self.width(), self.height()
        source_width, source_height = self._target_size
        gl.glBlitFramebuffer(0, 0, source_width, source_height, 0, 0, width, height,
                             gl.GL_COLOR_BUFFER_BIT, gl.GL_NEAREST)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        del final_texture

    def eventFilter(self, watched, event):  # noqa: N802
        if watched is self._attached_viewport and event.type() in (QtCore.QEvent.Resize, QtCore.QEvent.Move, QtCore.QEvent.Show):
            self._sync_attached_geometry()
        return False

    # Snapshot validation and active selection -------------------------

    def _validate_snapshots(self, source: SourceDescriptor, display: DisplaySnapshot,
                            viewport: ViewportSnapshot) -> None:
        if self._disposed:
            raise Step1GpuLayerError("G1 layer is disposed")
        if display.mode not in (MODE_OVERLAY, MODE_FUSION):
            raise Step1GpuLayerError(f"unsupported display mode {display.mode!r}")
        x0, x1, y0, y1 = viewport.world_rect
        if not x1 > x0 or not y1 > y0:
            raise Step1GpuLayerError("viewport world rect must have positive extent")
        if viewport.logical_size[0] <= 0 or viewport.logical_size[1] <= 0 or viewport.device_pixel_ratio <= 0:
            raise Step1GpuLayerError("viewport dimensions and DPR must be positive")
        source.by_channel()

    def _overlay_active(self, sources: Mapping[str, ChannelSource], display: DisplaySnapshot):
        active, missing = {}, []
        for channel, source in sources.items():
            weight = float(display.weights.get(channel, 0.0) or 0.0)
            if weight <= 0.0:
                continue
            if channel not in display.mappings:
                missing.append(channel)
                continue
            if not source.selected_planes():
                continue
            active[channel] = source
        return active, missing

    def _fusion_active(self, sources: Mapping[str, ChannelSource], display: DisplaySnapshot):
        active_groups: Dict[str, Dict[str, ChannelSource]] = {}
        missing = []
        for group, weights in display.groups.items():
            group_weight = float(display.group_weights.get(group, 1.0) or 0.0)
            if group_weight <= 0.0:
                continue
            group_sources = {}
            for channel, channel_weight in weights.items():
                if float(channel_weight or 0.0) <= 0.0 or channel not in sources:
                    continue
                if channel not in display.mappings:
                    missing.append(channel)
                    continue
                if sources[channel].selected_planes():
                    group_sources[channel] = sources[channel]
            if group_sources:
                active_groups[group] = group_sources
        nucleus_source = None
        nucleus_channel, nucleus_weight = display.nucleus
        if nucleus_channel and float(nucleus_weight or 0.0) > 0.0 and nucleus_channel in sources:
            if nucleus_channel not in display.mappings:
                missing.append(nucleus_channel)
            elif sources[nucleus_channel].selected_planes():
                nucleus_source = sources[nucleus_channel]
        return active_groups, nucleus_source, missing

    # Rendering ----------------------------------------------------------

    def _render_overlay(self, active: Mapping[str, ChannelSource], display: DisplaySnapshot,
                        viewport: ViewportSnapshot) -> None:
        gl = self._gl
        self._clear_target("accum")
        for channel, source in active.items():
            self._render_signal(source, display.mappings[channel], viewport)
            weight = min(1.0, float(display.weights.get(channel, 0.0) or 0.0))
            self._contribute("accum", weight, display.colors.get(channel, (1.0, 1.0, 1.0)), 0,
                             blend_equation=(gl.GL_FUNC_ADD, gl.GL_MAX))
        self._finalize("accum", "final", "final_overlay")

    def _render_fusion(self, groups: Mapping[str, Mapping[str, ChannelSource]], nucleus_source: Optional[ChannelSource],
                       display: DisplaySnapshot, viewport: ViewportSnapshot) -> None:
        gl = self._gl
        self._clear_target("fusion")
        for group, sources in groups.items():
            self._clear_target("group")
            for channel, source in sources.items():
                self._render_signal(source, display.mappings[channel], viewport)
                channel_weight = min(1.0, max(0.0, float(display.groups[group].get(channel, 0.0) or 0.0)))
                self._contribute("group", channel_weight, (1.0, 0.0, 0.0), 1,
                                 blend_equation=(gl.GL_FUNC_ADD, gl.GL_MAX))
            group_weight = min(1.0, max(0.0, float(display.group_weights.get(group, 1.0) or 0.0)))
            self._group_resolve(group_weight)
        if nucleus_source is not None:
            nucleus_channel, nucleus_weight = display.nucleus
            self._render_signal(nucleus_source, display.mappings[nucleus_channel], viewport)
            self._contribute("fusion", min(1.0, max(0.0, float(nucleus_weight))), (0.0, 0.0, 1.0), 2,
                             blend_equation=(gl.GL_MAX, gl.GL_MAX))
        self._finalize("fusion", "final", "final_fusion")

    def _render_signal(self, source: ChannelSource, mapping: Tuple[float, float, float],
                       viewport: ViewportSnapshot) -> None:
        gl = self._gl
        self._clear_target("signal")
        self._bind_target("signal")
        gl.glDisable(gl.GL_BLEND)
        gl.glUseProgram(self._programs["source"])
        self._uniform4("source", "u_view_rect", viewport.world_rect)
        self._uniform3("source", "u_mapping", mapping)
        for plane in source.selected_planes():
            gl.glActiveTexture(gl.GL_TEXTURE0)
            gl.glBindTexture(gl.GL_TEXTURE_2D, self._cache.texture_for(plane))
            self._uniform1i("source", "u_raw", 0)
            self._uniform4("source", "u_plane_rect", plane.world_rect)
            self._draw()
        gl.glUseProgram(0)

    def _contribute(self, target: str, weight: float, color: Tuple[float, float, float], component: int,
                    blend_equation: Tuple[int, int]) -> None:
        gl = self._gl
        self._bind_target(target)
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendEquationSeparate(*blend_equation)
        gl.glBlendFuncSeparate(gl.GL_ONE, gl.GL_ONE, gl.GL_ONE, gl.GL_ONE)
        gl.glUseProgram(self._programs["contribution"])
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._targets["signal"][1])
        self._uniform1i("contribution", "u_input", 0)
        self._uniform3("contribution", "u_color", color)
        self._uniform1f("contribution", "u_weight", weight)
        self._uniform1i("contribution", "u_component", component)
        self._draw()
        gl.glUseProgram(0)
        gl.glDisable(gl.GL_BLEND)

    def _group_resolve(self, group_weight: float) -> None:
        gl = self._gl
        self._bind_target("fusion")
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendEquation(gl.GL_MAX)
        gl.glUseProgram(self._programs["group_resolve"])
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._targets["group"][1])
        self._uniform1i("group_resolve", "u_input", 0)
        self._uniform1f("group_resolve", "u_weight", group_weight)
        self._draw()
        gl.glUseProgram(0)
        gl.glDisable(gl.GL_BLEND)

    def _finalize(self, source: str, target: str, program: str) -> None:
        gl = self._gl
        self._bind_target(target)
        gl.glDisable(gl.GL_BLEND)
        gl.glUseProgram(self._programs[program])
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._targets[source][1])
        self._uniform1i(program, "u_input", 0)
        self._draw()
        gl.glUseProgram(0)
        _check_gl(gl, f"G1 {program}")

    def _draw(self) -> None:
        self._gl.glBindVertexArray(self._vao)
        self._gl.glDrawArrays(self._gl.GL_TRIANGLES, 0, 3)
        self._gl.glBindVertexArray(0)
        self._submission["pass_count"] = int(self._submission.get("pass_count", 0)) + 1

    # GL resource management --------------------------------------------

    def _require_ready(self) -> None:
        if self._disposed:
            raise Step1GpuLayerError("G1 layer is disposed")
        if not self._initialized:
            raise self._init_error or Step1GpuLayerError("G1 Qt OpenGL widget is not initialized")
        if QtGui.QOpenGLContext.currentContext() is not self.context():
            raise Step1GpuLayerError("G1 GL operation requires its exact Qt context current")

    def _compile_programs(self) -> None:
        vertex = (_SHADER_DIR / "step1_gpu.vert").read_text(encoding="utf-8")
        fragment = (_SHADER_DIR / "step1_gpu.frag").read_text(encoding="utf-8")
        for name, define in {
            "source": "PASS_SOURCE",
            "contribution": "PASS_CONTRIBUTION",
            "group_resolve": "PASS_GROUP_RESOLVE",
            "final_overlay": "PASS_FINAL_OVERLAY",
            "final_fusion": "PASS_FINAL_FUSION",
        }.items():
            source = fragment.replace("\n", f"\n#define {define}\n", 1)
            self._programs[name] = self._link_program(vertex, source)
            self._uniforms[name] = {}

    def _link_program(self, vertex_source: str, fragment_source: str) -> int:
        gl = self._gl
        def compile_one(kind, source):
            shader = gl.glCreateShader(kind)
            gl.glShaderSource(shader, source)
            gl.glCompileShader(shader)
            if not gl.glGetShaderiv(shader, gl.GL_COMPILE_STATUS):
                message = gl.glGetShaderInfoLog(shader).decode("utf-8", "replace")
                gl.glDeleteShader(shader)
                raise Step1GpuLayerError(f"G1 shader compile failure: {message}")
            return shader
        vertex = compile_one(gl.GL_VERTEX_SHADER, vertex_source)
        fragment = compile_one(gl.GL_FRAGMENT_SHADER, fragment_source)
        program = gl.glCreateProgram()
        gl.glAttachShader(program, vertex)
        gl.glAttachShader(program, fragment)
        gl.glLinkProgram(program)
        gl.glDeleteShader(vertex)
        gl.glDeleteShader(fragment)
        if not gl.glGetProgramiv(program, gl.GL_LINK_STATUS):
            message = gl.glGetProgramInfoLog(program).decode("utf-8", "replace")
            gl.glDeleteProgram(program)
            raise Step1GpuLayerError(f"G1 shader link failure: {message}")
        return int(program)

    def _ensure_targets(self, size: Tuple[int, int]) -> None:
        if size == self._target_size:
            return
        self._destroy_targets()
        gl = self._gl
        for name, internal, fmt, typ in (
            ("signal", gl.GL_RGBA32F, gl.GL_RGBA, gl.GL_FLOAT),
            ("accum", gl.GL_RGBA32F, gl.GL_RGBA, gl.GL_FLOAT),
            ("group", gl.GL_RGBA32F, gl.GL_RGBA, gl.GL_FLOAT),
            ("fusion", gl.GL_RGBA32F, gl.GL_RGBA, gl.GL_FLOAT),
            ("final", gl.GL_RGBA8, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE),
        ):
            texture = _as_name(gl.glGenTextures(1))
            gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, internal, size[0], size[1], 0, fmt, typ, None)
            fbo = _as_name(gl.glGenFramebuffers(1))
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
            gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0, gl.GL_TEXTURE_2D, texture, 0)
            if gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER) != gl.GL_FRAMEBUFFER_COMPLETE:
                raise Step1GpuLayerError(f"G1 {name} framebuffer incomplete")
            self._targets[name] = (fbo, texture)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        self._target_size = size
        _check_gl(gl, "G1 transient target allocation")

    def _bind_target(self, name: str) -> None:
        fbo, _texture = self._targets[name]
        width, height = self._target_size
        self._gl.glBindFramebuffer(self._gl.GL_FRAMEBUFFER, fbo)
        self._gl.glViewport(0, 0, width, height)

    def _clear_target(self, name: str) -> None:
        self._bind_target(name)
        self._gl.glDisable(self._gl.GL_BLEND)
        self._gl.glClearColor(0.0, 0.0, 0.0, 0.0)
        self._gl.glClear(self._gl.GL_COLOR_BUFFER_BIT)

    def _uniform_location(self, program: str, name: str) -> int:
        locations = self._uniforms[program]
        if name not in locations:
            location = int(self._gl.glGetUniformLocation(self._programs[program], name))
            if location < 0:
                raise Step1GpuLayerError(f"G1 shader missing required uniform {program}.{name}")
            locations[name] = location
        return locations[name]

    def _uniform1i(self, program: str, name: str, value: int) -> None:
        self._gl.glUniform1i(self._uniform_location(program, name), int(value))

    def _uniform1f(self, program: str, name: str, value: float) -> None:
        self._gl.glUniform1f(self._uniform_location(program, name), float(value))

    def _uniform3(self, program: str, name: str, values) -> None:
        self._gl.glUniform3f(self._uniform_location(program, name), *[float(item) for item in values])

    def _uniform4(self, program: str, name: str, values) -> None:
        self._gl.glUniform4f(self._uniform_location(program, name), *[float(item) for item in values])

    def _destroy_targets(self) -> None:
        if self._gl is None:
            self._targets.clear()
            self._target_size = None
            return
        for fbo, texture in self._targets.values():
            self._gl.glDeleteFramebuffers(1, [fbo])
            self._gl.glDeleteTextures([texture])
        self._targets.clear()
        self._target_size = None

    def _destroy_gl_names(self) -> None:
        if self._gl is None:
            return
        self._cache.clear(self._gl)
        self._destroy_targets()
        if self._vao:
            self._gl.glDeleteVertexArrays(1, [self._vao])
            self._vao = 0
        for program in self._programs.values():
            self._gl.glDeleteProgram(program)
        self._programs.clear()
        self._uniforms.clear()

    # Attachment ---------------------------------------------------------

    def _attached_range_changed(self, _view_box, ranges) -> None:
        self._attached_range = (float(ranges[0][0]), float(ranges[0][1]),
                                float(ranges[1][0]), float(ranges[1][1]))

    def _sync_attached_geometry(self) -> None:
        if self._attached_viewport is not None:
            self.setGeometry(self._attached_viewport.rect())
            self.raise_()

    def _disconnect_attachment(self) -> None:
        if self._attached_view is not None:
            try:
                self._attached_view.view_box.sigRangeChanged.disconnect(self._attached_range_changed)
            except (TypeError, RuntimeError):
                pass
        if self._attached_viewport is not None:
            self._attached_viewport.removeEventFilter(self)
        self._attached_view = None
        self._attached_viewport = None
