"""Independent G0.1 Qt/PyOpenGL feasibility probe for Step1.

Qt owns the window, event loop and OpenGL context.  PyOpenGL is used only for
GL calls while that Qt context is current.  This module is not imported by
production code and is not a provider, cache, scheduler or production viewer.

Run from an isolated probe environment, for example::

    cd /tmp
    PYTHONDONTWRITEBYTECODE=1 /tmp/block01-g0-1-pyopengl-XXXXXX/bin/python \
      /sda1/Fusion/analysis_pipline/block01_v14/scripts/benchmark_step1_gpu_demo.py \
      --platform offscreen \
      --out /sda1/Fusion/analysis_pipline/block01_v14/docs/benchmarks/step1_gpu_demo/2026-09-20_g0_1_offscreen

Offscreen FBO output establishes shader/readback correctness only.  It is not
an actual-desktop-presentation, compositor, vsync or input-to-photon measure.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import os
import pathlib
import platform
import socket
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterable, Mapping, Optional, Tuple

import numpy as np

# Platform selection must precede Qt imports.  Tests control it themselves.
if "--platform" in sys.argv:
    _platform_index = sys.argv.index("--platform")
    if _platform_index + 1 < len(sys.argv) and sys.argv[_platform_index + 1] == "offscreen":
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: E402


ROOT = pathlib.Path(__file__).resolve().parent.parent
BENCHMARK_ROOT = ROOT / "docs" / "benchmarks" / "step1_gpu_demo"
CHANNEL_A = "A"
CHANNEL_B = "B"
CHANNELS = (CHANNEL_A, CHANNEL_B)
SIZE = 32
WORLD_RECT = (0.0, float(SIZE), 0.0, float(SIZE))  # x0, x1, y0, y1

VERTEX_SHADER = """#version 330 core
out vec2 v_screen_uv;
void main() {
    const vec2 positions[3] = vec2[3](vec2(-1.0, -1.0),
                                      vec2( 3.0, -1.0),
                                      vec2(-1.0,  3.0));
    vec2 position = positions[gl_VertexID];
    v_screen_uv = 0.5 * (position + 1.0);
    gl_Position = vec4(position, 0.0, 1.0);
}
"""

FRAGMENT_SHADER = """#version 330 core
in vec2 v_screen_uv;
out vec4 out_rgba;
uniform sampler2D a_coarse;
uniform sampler2D a_fine;
uniform sampler2D b_coarse;
uniform sampler2D b_fine;
uniform bool a_use_fine;
uniform bool b_use_fine;
uniform vec3 a_mapping;
uniform vec3 b_mapping;
uniform vec3 a_color;
uniform vec3 b_color;
uniform float a_weight;
uniform float b_weight;
uniform vec4 view_world_rect;
uniform vec4 image_world_rect;

float signal_for(float value, vec3 mapping) {
    if (isnan(value) || isinf(value)) return 0.0;
    float denominator = mapping.y - mapping.x;
    if (denominator <= 0.0 || mapping.z <= 0.0) return 0.0;
    float normalized = clamp((value - mapping.x) / denominator, 0.0, 1.0);
    return pow(normalized, mapping.z);
}

void main() {
    vec2 view_extent = vec2(view_world_rect.y - view_world_rect.x,
                            view_world_rect.w - view_world_rect.z);
    vec2 world = vec2(view_world_rect.x + v_screen_uv.x * view_extent.x,
                      view_world_rect.w - v_screen_uv.y * view_extent.y);
    vec2 image_extent = vec2(image_world_rect.y - image_world_rect.x,
                             image_world_rect.w - image_world_rect.z);
    vec2 sample_uv = (world - vec2(image_world_rect.x, image_world_rect.z)) / image_extent;
    bool in_image = all(greaterThanEqual(sample_uv, vec2(0.0))) &&
                    all(lessThan(sample_uv, vec2(1.0)));
    if (!in_image) {
        out_rgba = vec4(0.0);
        return;
    }
    float a_value = texture(a_use_fine ? a_fine : a_coarse, sample_uv).r;
    float b_value = texture(b_use_fine ? b_fine : b_coarse, sample_uv).r;
    bool a_valid = !isnan(a_value) && !isinf(a_value);
    bool b_valid = !isnan(b_value) && !isinf(b_value);
    vec3 rgb = vec3(0.0);
    if (a_weight > 0.0 && a_valid)
        rgb += signal_for(a_value, a_mapping) * a_weight * a_color;
    if (b_weight > 0.0 && b_valid)
        rgb += signal_for(b_value, b_mapping) * b_weight * b_color;
    bool valid = (a_weight > 0.0 && a_valid) || (b_weight > 0.0 && b_valid);
    out_rgba = vec4(clamp(rgb, 0.0, 1.0), valid ? 1.0 : 0.0);
}
"""


class ProbeBlocked(RuntimeError):
    """An explicit G0.1 stop condition, never a fallback invitation."""


def _gl():
    """Import the approved call layer only while a Qt context is current."""
    try:
        from OpenGL import GL
        import OpenGL
    except ImportError as exc:
        raise ProbeBlocked("PyOpenGL is unavailable in this isolated probe interpreter") from exc
    return GL, OpenGL


def _decode_gl_string(value) -> str:
    if value is None:
        return "unavailable"
    return bytes(value).decode("ascii", "replace")


def _as_name(value) -> int:
    """Normalize PyOpenGL's scalar/array GL name return types."""
    if isinstance(value, (tuple, list, np.ndarray)):
        return int(value[0])
    return int(value)


def _register_block01_alias() -> None:
    """Ensure the CPU oracle is imported from this checkout, not another one."""
    existing = sys.modules.get("block01")
    if existing is not None:
        existing_file = pathlib.Path(getattr(existing, "__file__", "")).resolve()
        if existing_file.parent == ROOT:
            return
        raise RuntimeError(f"block01 already loaded from {existing_file.parent}, not {ROOT}")
    spec = importlib.util.spec_from_file_location(
        "block01", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not create this-checkout block01 module alias")
    module = importlib.util.module_from_spec(spec)
    sys.modules["block01"] = module
    spec.loader.exec_module(module)
    sys.path.insert(0, str(ROOT.parent))


def _step1_compose():
    """Defer application imports until a real shader draw has already worked."""
    _register_block01_alias()
    from block01.viewer.step1_compose import MODE_OVERLAY, compose
    return MODE_OVERLAY, compose


def _explore_view_class():
    """Defer the local camera compatibility probe until shader correctness passes."""
    _register_block01_alias()
    from block01.viewer.explore_view import ExploreView
    return ExploreView


@dataclass(frozen=True)
class DisplayParameters:
    mappings: Mapping[str, Tuple[float, float, float]]
    colors: Mapping[str, Tuple[float, float, float]]
    weights: Mapping[str, float]


@dataclass
class ProbeCounters:
    source_selections: int = 0
    texture_uploads: int = 0
    uniform_updates: int = 0


@dataclass
class ProbeSource:
    """Deterministic in-memory raw planes with observable source-selection cost."""

    planes: Mapping[str, Mapping[str, np.ndarray]]
    counters: ProbeCounters = field(default_factory=ProbeCounters)

    def select(self, channel: str, level: str) -> np.ndarray:
        self.counters.source_selections += 1
        return self.planes[channel][level]


def synthetic_source(size: int = SIZE) -> ProbeSource:
    """Create asymmetric float32 data, distinct LODs and transparent NaN regions."""
    y, x = np.mgrid[:size, :size].astype(np.float32)
    a_fine = np.clip((x * 0.71 + y * 0.23) / (size * 0.94), 0.0, 1.0)
    b_fine = np.clip(((size - 1 - x) * 0.19 + y * 0.88) / (size * 0.91), 0.0, 1.0)
    a_coarse = np.floor(a_fine * 5.0) / 5.0
    b_coarse = np.floor(b_fine * 4.0) / 4.0
    a_fine[2, 3], a_fine[-4, 6] = 0.96, 0.08
    b_fine[5, -5], b_fine[-3, 2] = 0.91, 0.04
    for plane in (a_fine, a_coarse):
        plane[0:3, 0:4] = np.nan
    for plane in (b_fine, b_coarse):
        plane[-3:, -4:] = np.nan
    return ProbeSource({
        CHANNEL_A: {"coarse": a_coarse.astype(np.float32), "fine": a_fine.astype(np.float32)},
        CHANNEL_B: {"coarse": b_coarse.astype(np.float32), "fine": b_fine.astype(np.float32)},
    })


def default_parameters() -> DisplayParameters:
    return DisplayParameters(
        mappings={CHANNEL_A: (0.08, 0.92, 1.3), CHANNEL_B: (0.11, 0.87, 0.75)},
        colors={CHANNEL_A: (1.0, 0.25, 0.10), CHANNEL_B: (0.15, 0.70, 1.0)},
        weights={CHANNEL_A: 0.85, CHANNEL_B: 0.65},
    )


def cpu_reference(source: ProbeSource, selected: Mapping[str, str], parameters: DisplayParameters) -> np.ndarray:
    """The unmodified existing C1 Overlay formula, used only as the oracle."""
    tiles = {
        channel: (source.planes[channel][selected[channel]],
                  np.isfinite(source.planes[channel][selected[channel]]))
        for channel in CHANNELS
    }
    mode_overlay, compose_fn = _step1_compose()
    rgba, _valid, missing = compose_fn(
        mode_overlay, tiles, weights=parameters.weights,
        colors=parameters.colors, mappings=parameters.mappings,
    )
    if missing or rgba is None:
        raise AssertionError(f"probe CPU reference unexpectedly missing {missing}")
    return np.asarray(rgba, np.uint8)


def comparison(actual: np.ndarray, expected: np.ndarray) -> Dict[str, object]:
    diff = np.abs(actual.astype(np.int16) - expected.astype(np.int16))
    index = tuple(int(item) for item in np.unravel_index(np.argmax(diff), diff.shape))
    return {
        "max_abs_lsb": int(diff.max()),
        "max_error_index_yxrgba": list(index),
        "nonzero_components": int(np.count_nonzero(diff)),
        "over_one_lsb_components": int(np.count_nonzero(diff > 1)),
        "alpha_mismatch_components": int(np.count_nonzero(actual[..., 3] != expected[..., 3])),
    }


def _require_no_gl_error(gl, operation: str) -> None:
    error = gl.glGetError()
    if error != gl.GL_NO_ERROR:
        raise ProbeBlocked(f"OpenGL error after {operation}: 0x{int(error):04x}")


class GpuProbeWidget(QtWidgets.QOpenGLWidget):
    """Qt-owned context with G0.1's minimal PyOpenGL resource lifecycle."""

    initialized = QtCore.pyqtSignal()

    def __init__(self, source: ProbeSource, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.source = source
        self.parameters = default_parameters()
        self.selected = {CHANNEL_A: "fine", CHANNEL_B: "coarse"}
        self.counters = source.counters
        self.capabilities: Dict[str, object] = {}
        self.timings: Dict[str, list] = {name: [] for name in (
            "source_select_ms", "upload_submit_ms", "uniform_update_ms",
            "draw_submit_ms", "gpu_draw_query_ns_low32", "fbo_draw_readback_ms",
        )}
        self._gl = None
        self._program = 0
        self._vao = 0
        self._fbo = 0
        self._color_texture = 0
        self._timer_query = 0
        self._textures: Dict[Tuple[str, str], int] = {}
        self._uniforms: Dict[str, int] = {}
        self._disposed = False
        self._initialized = False
        self._init_error: Optional[ProbeBlocked] = None
        self._view_world_rect = WORLD_RECT
        self.resize(SIZE, SIZE)
        fmt = QtGui.QSurfaceFormat()
        fmt.setRenderableType(QtGui.QSurfaceFormat.OpenGL)
        fmt.setVersion(3, 3)
        fmt.setProfile(QtGui.QSurfaceFormat.CoreProfile)
        self.setFormat(fmt)

    def initializeGL(self) -> None:  # noqa: N802 - Qt API contract
        context = self.context()
        if context is None or not context.isValid():
            self._init_error = ProbeBlocked("Qt did not create a valid OpenGL context")
            return
        try:
            gl, opengl = _gl()
            self._gl = gl
            actual = context.format()
            vendor = _decode_gl_string(gl.glGetString(gl.GL_VENDOR))
            renderer = _decode_gl_string(gl.glGetString(gl.GL_RENDERER))
            version = _decode_gl_string(gl.glGetString(gl.GL_VERSION))
            glsl = _decode_gl_string(gl.glGetString(gl.GL_SHADING_LANGUAGE_VERSION))
            renderer_lower = f"{vendor} {renderer}".lower()
            self.capabilities = {
                "requested_format": {"major": 3, "minor": 3, "profile": "core"},
                "actual_format": {"major": actual.majorVersion(), "minor": actual.minorVersion(),
                                  "profile": int(actual.profile())},
                "gl_vendor": vendor,
                "gl_renderer": renderer,
                "gl_version": version,
                "glsl_version": glsl,
                "software_renderer": any(token in renderer_lower for token in ("llvmpipe", "softpipe", "swiftshader", "software")),
                "max_texture_image_units": int(gl.glGetIntegerv(gl.GL_MAX_TEXTURE_IMAGE_UNITS)),
                "max_texture_size": int(gl.glGetIntegerv(gl.GL_MAX_TEXTURE_SIZE)),
                "texture_format": "GL_R32F / GL_RED / GL_FLOAT",
                "pyopengl_version": opengl.__version__,
                "pyopengl_file": opengl.__file__,
            }
            if actual.majorVersion() < 3 or self.capabilities["max_texture_image_units"] < 4:
                raise ProbeBlocked("Qt context does not meet G0.1 core shader texture-unit requirements")
            self._create_program()
            self._create_targets()
            self.upload_sources()
            self._initialized = True
            self.initialized.emit()
        except (ProbeBlocked, RuntimeError) as exc:
            self._init_error = exc if isinstance(exc, ProbeBlocked) else ProbeBlocked(str(exc))
            self._destroy_gl_names()

    def _create_program(self) -> None:
        gl = self._require_gl()
        vertex = self._compile_shader(gl.GL_VERTEX_SHADER, VERTEX_SHADER)
        fragment = self._compile_shader(gl.GL_FRAGMENT_SHADER, FRAGMENT_SHADER)
        program = gl.glCreateProgram()
        gl.glAttachShader(program, vertex)
        gl.glAttachShader(program, fragment)
        gl.glLinkProgram(program)
        if not gl.glGetProgramiv(program, gl.GL_LINK_STATUS):
            log = gl.glGetProgramInfoLog(program).decode("utf-8", "replace")
            gl.glDeleteProgram(program)
            gl.glDeleteShader(vertex)
            gl.glDeleteShader(fragment)
            raise ProbeBlocked(f"shader link failed: {log}")
        gl.glDeleteShader(vertex)
        gl.glDeleteShader(fragment)
        self._program = int(program)
        self._vao = _as_name(gl.glGenVertexArrays(1))
        self._timer_query = _as_name(gl.glGenQueries(1))
        names = (
            "a_coarse", "a_fine", "b_coarse", "b_fine", "a_use_fine", "b_use_fine",
            "a_mapping", "b_mapping", "a_color", "b_color", "a_weight", "b_weight",
            "view_world_rect", "image_world_rect",
        )
        self._uniforms = {name: int(gl.glGetUniformLocation(self._program, name)) for name in names}
        missing = [name for name, location in self._uniforms.items() if location < 0]
        if missing:
            raise ProbeBlocked(f"shader optimized away required uniforms: {', '.join(missing)}")
        _require_no_gl_error(gl, "shader and VAO setup")

    def _compile_shader(self, shader_type: int, source: str) -> int:
        gl = self._require_gl()
        shader = gl.glCreateShader(shader_type)
        gl.glShaderSource(shader, source)
        gl.glCompileShader(shader)
        if not gl.glGetShaderiv(shader, gl.GL_COMPILE_STATUS):
            log = gl.glGetShaderInfoLog(shader).decode("utf-8", "replace")
            gl.glDeleteShader(shader)
            kind = "vertex" if shader_type == gl.GL_VERTEX_SHADER else "fragment"
            raise ProbeBlocked(f"{kind} shader compilation failed: {log}")
        return int(shader)

    def _create_targets(self) -> None:
        gl = self._require_gl()
        self._color_texture = _as_name(gl.glGenTextures(1))
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._color_texture)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, SIZE, SIZE, 0,
                        gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
        self._fbo = _as_name(gl.glGenFramebuffers(1))
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self._fbo)
        gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                  gl.GL_TEXTURE_2D, self._color_texture, 0)
        status = gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        if status != gl.GL_FRAMEBUFFER_COMPLETE:
            raise ProbeBlocked(f"RGBA8 framebuffer incomplete: 0x{int(status):04x}")
        _require_no_gl_error(gl, "FBO setup")

    def upload_sources(self) -> None:
        """The only path that selects raw source or uploads raw pixels."""
        self._require_current()
        gl = self._require_gl()
        for texture in self._textures.values():
            gl.glDeleteTextures([texture])
        self._textures.clear()
        started = time.perf_counter()
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
        for channel in CHANNELS:
            for level in ("coarse", "fine"):
                source_started = time.perf_counter()
                plane = np.ascontiguousarray(self.source.select(channel, level), dtype=np.float32)
                self.timings["source_select_ms"].append((time.perf_counter() - source_started) * 1000.0)
                texture = _as_name(gl.glGenTextures(1))
                gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
                gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
                gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
                gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
                gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
                gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_R32F, plane.shape[1], plane.shape[0], 0,
                                gl.GL_RED, gl.GL_FLOAT, plane)
                self._textures[(channel, level)] = texture
                self.counters.texture_uploads += 1
        self.timings["upload_submit_ms"].append((time.perf_counter() - started) * 1000.0)
        _require_no_gl_error(gl, "R32F source upload")

    def set_display_parameters(self, parameters: DisplayParameters) -> None:
        """Store display uniforms only; source selection/reupload are forbidden."""
        self.parameters = parameters
        self.counters.uniform_updates += 1
        started = time.perf_counter()
        self.timings["uniform_update_ms"].append((time.perf_counter() - started) * 1000.0)
        self.update()

    def set_selected_levels(self, selected: Mapping[str, str]) -> None:
        if set(selected) != set(CHANNELS) or any(level not in {"coarse", "fine"} for level in selected.values()):
            raise ValueError("selected levels must specify fine/coarse for both channels")
        self.selected = dict(selected)
        self.update()

    def set_view_world_rect(self, world_rect: Tuple[float, float, float, float]) -> None:
        x0, x1, y0, y1 = world_rect
        if not x1 > x0 or not y1 > y0:
            raise ValueError("ViewBox world rect must have positive extent")
        self._view_world_rect = tuple(float(value) for value in world_rect)
        self.update()

    def paintGL(self) -> None:  # noqa: N802 - Qt API contract
        if self._initialized:
            self._draw_to(self.defaultFramebufferObject(), max(1, self.width()), max(1, self.height()))

    def _draw_to(self, framebuffer: int, width: int, height: int) -> None:
        self._require_current()
        gl = self._require_gl()
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, framebuffer)
        gl.glViewport(0, 0, width, height)
        gl.glClearColor(0.0, 0.0, 0.0, 0.0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        started = time.perf_counter()
        gl.glUseProgram(self._program)
        for unit, key in enumerate(((CHANNEL_A, "coarse"), (CHANNEL_A, "fine"),
                                    (CHANNEL_B, "coarse"), (CHANNEL_B, "fine"))):
            gl.glActiveTexture(gl.GL_TEXTURE0 + unit)
            gl.glBindTexture(gl.GL_TEXTURE_2D, self._textures[key])
        self._uniform_i("a_coarse", 0)
        self._uniform_i("a_fine", 1)
        self._uniform_i("b_coarse", 2)
        self._uniform_i("b_fine", 3)
        self._uniform_i("a_use_fine", int(self.selected[CHANNEL_A] == "fine"))
        self._uniform_i("b_use_fine", int(self.selected[CHANNEL_B] == "fine"))
        self._set_channel_uniforms(CHANNEL_A, "a")
        self._set_channel_uniforms(CHANNEL_B, "b")
        self._uniform_4f("view_world_rect", self._view_world_rect)
        self._uniform_4f("image_world_rect", WORLD_RECT)
        gl.glBindVertexArray(self._vao)
        gl.glBeginQuery(gl.GL_TIME_ELAPSED, self._timer_query)
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
        gl.glEndQuery(gl.GL_TIME_ELAPSED)
        gl.glBindVertexArray(0)
        gl.glUseProgram(0)
        self.timings["draw_submit_ms"].append((time.perf_counter() - started) * 1000.0)
        _require_no_gl_error(gl, "shader draw submission")

    def _collect_gpu_query(self) -> None:
        gl = self._require_gl()
        if not self._timer_query:
            return
        available = gl.glGetQueryObjectiv(self._timer_query, gl.GL_QUERY_RESULT_AVAILABLE)
        if available:
            self.timings["gpu_draw_query_ns_low32"].append(
                float(gl.glGetQueryObjectuiv(self._timer_query, gl.GL_QUERY_RESULT))
            )

    def _uniform_i(self, name: str, value: int) -> None:
        self._require_gl().glUniform1i(self._uniforms[name], int(value))

    def _uniform_4f(self, name: str, values: Tuple[float, float, float, float]) -> None:
        self._require_gl().glUniform4f(self._uniforms[name], *values)

    def _set_channel_uniforms(self, channel: str, prefix: str) -> None:
        gl = self._require_gl()
        mapping = self.parameters.mappings[channel]
        color = self.parameters.colors[channel]
        gl.glUniform3f(self._uniforms[f"{prefix}_mapping"], *mapping)
        gl.glUniform3f(self._uniforms[f"{prefix}_color"], *color)
        gl.glUniform1f(self._uniforms[f"{prefix}_weight"], float(self.parameters.weights[channel]))

    def readback_rgba(self) -> np.ndarray:
        """Exact-size FBO result, normalized once from GL lower-left to top-left."""
        own_current = QtGui.QOpenGLContext.currentContext() is not self.context()
        if own_current:
            self.makeCurrent()
        try:
            self._require_current()
            gl = self._require_gl()
            started = time.perf_counter()
            self._draw_to(self._fbo, SIZE, SIZE)
            gl.glPixelStorei(gl.GL_PACK_ALIGNMENT, 1)
            pixels = gl.glReadPixels(0, 0, SIZE, SIZE, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE)
            actual = np.frombuffer(pixels, dtype=np.uint8).reshape(SIZE, SIZE, 4).copy()
            actual = np.flipud(actual)
            self.timings["fbo_draw_readback_ms"].append((time.perf_counter() - started) * 1000.0)
            self._collect_gpu_query()
            _require_no_gl_error(gl, "FBO readback")
            return actual
        finally:
            if own_current:
                self.doneCurrent()

    def _require_gl(self):
        if self._gl is None:
            raise ProbeBlocked("PyOpenGL was not initialized in the Qt context")
        return self._gl

    def _require_current(self) -> None:
        if QtGui.QOpenGLContext.currentContext() is not self.context():
            raise RuntimeError("probe GL operation requires this widget's current Qt context")

    def dispose(self) -> Dict[str, object]:
        """Idempotently delete this probe's GL names on the Qt GUI thread/context."""
        if self._disposed:
            return {"already_disposed": True, "textures_remaining": 0, "threads_created": 0}
        self.makeCurrent()
        before = len(self._textures)
        self._destroy_gl_names()
        self.doneCurrent()
        self._disposed = True
        return {"already_disposed": False, "textures_destroyed": before,
                "textures_remaining": len(self._textures), "threads_created": 0}

    def _destroy_gl_names(self) -> None:
        if self._gl is None:
            return
        gl = self._gl
        if self._textures:
            gl.glDeleteTextures(list(self._textures.values()))
            self._textures.clear()
        if self._color_texture:
            gl.glDeleteTextures([self._color_texture])
            self._color_texture = 0
        if self._fbo:
            gl.glDeleteFramebuffers(1, [self._fbo])
            self._fbo = 0
        if self._timer_query:
            gl.glDeleteQueries(1, [self._timer_query])
            self._timer_query = 0
        if self._vao:
            gl.glDeleteVertexArrays(1, [self._vao])
            self._vao = 0
        if self._program:
            gl.glDeleteProgram(self._program)
            self._program = 0
        self._uniforms.clear()


class ViewBoxOverlayAdapter(QtCore.QObject):
    """Local-only sibling overlay adapter; it does not own or replace camera state."""

    def __init__(self, view, gl_widget: GpuProbeWidget):
        super().__init__(view)
        self.view = view
        self.gl_widget = gl_widget
        self.range_events = 0
        self.resize_events = 0
        self.last_world_rect: Optional[Tuple[float, float, float, float]] = None
        self.viewport = view.graphics.viewport()
        if gl_widget.parent() is not self.viewport:
            gl_widget.setParent(self.viewport)
        gl_widget.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self.viewport.installEventFilter(self)
        view.view_box.sigRangeChanged.connect(self._range_changed)
        self.sync_geometry()
        self._range_changed(view.view_box, view.view_box.viewRange())

    def eventFilter(self, watched, event):  # noqa: N802 - Qt API contract
        if watched is self.viewport and event.type() in (QtCore.QEvent.Resize, QtCore.QEvent.Move, QtCore.QEvent.Show):
            self.resize_events += 1
            self.sync_geometry()
        return False

    def sync_geometry(self) -> None:
        self.gl_widget.setGeometry(self.viewport.rect())
        self.gl_widget.raise_()

    def _range_changed(self, _view_box, ranges) -> None:
        self.range_events += 1
        self.last_world_rect = (float(ranges[0][0]), float(ranges[0][1]),
                                float(ranges[1][0]), float(ranges[1][1]))
        self.gl_widget.set_view_world_rect(self.last_world_rect)


def process_events(app: QtWidgets.QApplication, cycles: int = 8) -> None:
    for _ in range(cycles):
        app.processEvents()
        time.sleep(0.003)


def create_probe(source: Optional[ProbeSource] = None, *, visible: bool = False) -> Tuple[QtWidgets.QApplication, GpuProbeWidget]:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    widget = GpuProbeWidget(source or synthetic_source())
    if not visible:
        widget.setAttribute(QtCore.Qt.WA_DontShowOnScreen, True)
    widget.show()
    process_events(app)
    if not widget._initialized:
        raise widget._init_error or ProbeBlocked("Qt widget did not reach initializeGL")
    return app, widget


def timing_summary(samples: Iterable[float]) -> Dict[str, Optional[float]]:
    values = sorted(float(value) for value in samples)
    if not values:
        return {"count": 0, "p50": None, "p95": None, "max": None}
    p95 = min(len(values) - 1, round((len(values) - 1) * 0.95))
    return {"count": len(values), "p50": values[len(values) // 2], "p95": values[p95], "max": values[-1]}


def _passes(stats: Mapping[str, object]) -> bool:
    return int(stats["max_abs_lsb"]) <= 1 and int(stats["alpha_mismatch_components"]) == 0


def run_probe(*, visible: bool = False) -> Dict[str, object]:
    """Run G0.1 synthetic correctness, hot-update, LOD and camera-surface checks."""
    source = synthetic_source()
    app, widget = create_probe(source, visible=visible)
    initial = {CHANNEL_A: "fine", CHANNEL_B: "coarse"}
    widget.set_selected_levels(initial)
    process_events(app)
    initial_actual = widget.readback_rgba()
    initial_expected = cpu_reference(source, initial, widget.parameters)
    initial_comparison = comparison(initial_actual, initial_expected)

    before = (source.counters.source_selections, source.counters.texture_uploads)
    changed = DisplayParameters(
        mappings={CHANNEL_A: (0.17, 0.82, 0.80), CHANNEL_B: (0.06, 0.78, 1.50)},
        colors={CHANNEL_A: (0.20, 1.0, 0.18), CHANNEL_B: (0.95, 0.08, 0.72)},
        weights={CHANNEL_A: 0.51, CHANNEL_B: 0.92},
    )
    widget.set_display_parameters(changed)
    process_events(app)
    changed_actual = widget.readback_rgba()
    changed_comparison = comparison(changed_actual, cpu_reference(source, initial, changed))
    after = (source.counters.source_selections, source.counters.texture_uploads)

    reverse = {CHANNEL_A: "coarse", CHANNEL_B: "fine"}
    widget.set_selected_levels(reverse)
    process_events(app)
    reverse_actual = widget.readback_rgba()
    reverse_comparison = comparison(reverse_actual, cpu_reference(source, reverse, changed))

    # Build the local sibling overlay as a child before its first GL context is
    # made. Reparenting a live QOpenGLWidget can invalidate its context/names.
    view = _explore_view_class()()
    view.resize(180, 140)
    view.show()
    process_events(app)
    camera_source = synthetic_source()
    camera_widget = GpuProbeWidget(camera_source, view.graphics.viewport())
    camera_widget.show()
    process_events(app)
    if not camera_widget._initialized:
        raise camera_widget._init_error or ProbeBlocked("ViewBox overlay widget did not initialize")
    owner = view.view_box
    adapter = ViewBoxOverlayAdapter(view, camera_widget)
    view.view_box.setRange(xRange=(2, 22), yRange=(4, 28), padding=0)
    process_events(app)
    camera_actual = camera_widget.readback_rgba()
    camera = {
        "same_viewbox_owner": view.view_box is owner,
        "range_events": adapter.range_events,
        "resize_events": adapter.resize_events,
        "world_rect": list(adapter.last_world_rect or ()),
        "uniform_world_rect": list(camera_widget._view_world_rect),
        "overlay_transparent_for_mouse": bool(camera_widget.testAttribute(QtCore.Qt.WA_TransparentForMouseEvents)),
        "overlay_matches_viewport": camera_widget.geometry() == view.graphics.viewport().rect(),
        "camera_uniform_changes_pixels": bool(np.any(camera_actual != np.zeros_like(camera_actual))),
    }
    camera_cleanup = camera_widget.dispose()
    camera_widget.close()
    view.close()

    exposed = bool(visible and widget.isVisible() and widget.windowHandle() and widget.windowHandle().isExposed())
    cleanup = widget.dispose()
    widget.close()
    process_events(app)

    result = {
        "status": "measured",
        "synthetic_only": True,
        "desktop_window_exposed": exposed,
        "presentation_measurement": "not measured: no compositor/vsync or input-to-display timestamp API in this isolated probe",
        "capabilities": widget.capabilities,
        "source": {"kind": "synthetic float32", "channels": 2, "levels_per_channel": ["coarse", "fine"],
                   "tile_size": [SIZE, SIZE], "cold_definition": "new Qt widget before four R32F uploads",
                   "hot_definition": "same Qt widget with four raw textures resident"},
        "scenarios": {
            "A_cold_first_prepare": "synthetic source selection and R32F upload submission measured",
            "B_hot_same_channel_viewport": "resident-texture FBO draw/readback measured",
            "C_resident_intensity": "uniform-only update counters and C1 readback measured",
            "D_camera_new_region": "existing ViewBox range forwarded to world-to-UV shader uniform; no real tile I/O",
            "E_mixed_precision": "A fine/B coarse then A coarse/B fine measured",
        },
        "initial_fine_a_coarse_b": initial_comparison,
        "parameter_update": {
            "comparison": changed_comparison,
            "pixels_changed": bool(np.any(changed_actual != initial_actual)),
            "additional_source_selections": after[0] - before[0],
            "additional_texture_uploads": after[1] - before[1],
        },
        "reverse_coarse_a_fine_b": reverse_comparison,
        "mixed_lod_pixels_differ": bool(np.any(reverse_actual != changed_actual)),
        "viewbox_adapter": camera,
        "counters": asdict(source.counters),
        "timings": {name: timing_summary(samples) for name, samples in widget.timings.items()},
        "timing_limits": {
            "source_read_decode": "not measured: synthetic in-memory source has no disk queue/read/decode",
            "upload_submit_ms": "CPU submission wall time; it is not GPU completion",
            "draw_submit_ms": "CPU OpenGL submission wall time; it is not GPU completion",
            "gpu_draw_query_ns_low32": "GL_TIME_ELAPSED query low 32 bits for this short draw; not presentation",
            "fbo_draw_readback_ms": "draw plus blocking FBO readback; numeric correctness only, not desktop presentation",
            "desktop_presentation": "not measured: exposed Qt window does not provide compositor/vsync/input-to-display timing",
            "ram_vram": "not measured: no reliable cross-driver accounting API in this probe",
        },
        "cleanup": {"main": cleanup, "viewbox_overlay": camera_cleanup},
    }
    result["pass_gates"] = {
        "two_real_raw_texture_channels": len(widget.capabilities) > 0 and source.counters.texture_uploads == 4,
        "hardware_renderer": not bool(widget.capabilities.get("software_renderer")),
        "initial_c1_match_le_1_lsb": _passes(initial_comparison),
        "parameter_c1_match_le_1_lsb": _passes(changed_comparison),
        "parameter_pixels_changed": result["parameter_update"]["pixels_changed"],
        "parameter_zero_source_read_and_upload": result["parameter_update"]["additional_source_selections"] == 0 and result["parameter_update"]["additional_texture_uploads"] == 0,
        "independent_mixed_lod": _passes(reverse_comparison) and result["mixed_lod_pixels_differ"],
        "viewbox_camera_adapter": camera["same_viewbox_owner"] and camera["range_events"] > 0 and camera["uniform_world_rect"] == camera["world_rect"] and camera["overlay_transparent_for_mouse"] and camera["overlay_matches_viewport"] and camera["camera_uniform_changes_pixels"],
        "context_current_gl_cleanup": all(item["textures_remaining"] == 0 and item["threads_created"] == 0 for item in (cleanup, camera_cleanup)),
        "desktop_presentation": exposed,
    }
    return result


def probe_real_wsi(path: pathlib.Path, channels: Tuple[int, int], level: int) -> Dict[str, object]:
    """Read two coarsest/specified TIFF planes only, then prove two raw uploads.

    This is deliberately a small direct `tifffile` read from the documented
    demonstration WSI.  It does not construct a project provider, scheduler,
    cache, MainWindow or saving workflow.  A 32x32 crop is only a G0 texture
    proof; it is not a production tile/LOD implementation.
    """
    try:
        import tifffile
    except ImportError as exc:
        return {"status": "not_measured", "reason": f"tifffile unavailable: {exc}"}
    if not path.is_file() or not os.access(path, os.R_OK):
        return {"status": "not_measured", "reason": f"WSI unreadable: {path}"}
    if len(channels) != 2 or channels[0] == channels[1]:
        return {"status": "not_measured", "reason": "two distinct real channel indices are required"}
    try:
        open_started = time.perf_counter()
        with tifffile.TiffFile(path) as tif:
            open_ms = (time.perf_counter() - open_started) * 1000.0
            series = tif.series[0]
            if level < 0 or level >= len(series.levels):
                return {"status": "not_measured", "reason": f"level {level} absent; levels={len(series.levels)}"}
            selected_level = series.levels[level]
            if selected_level.axes != "CYX":
                return {"status": "not_measured", "reason": f"unsupported axes {selected_level.axes!r}"}
            if max(channels) >= selected_level.shape[0]:
                return {"status": "not_measured", "reason": f"channel outside 0..{selected_level.shape[0] - 1}"}

            def decode_once(channel: int):
                started = time.perf_counter()
                array = selected_level.pages[channel].asarray()
                elapsed = (time.perf_counter() - started) * 1000.0
                return np.ascontiguousarray(array[:SIZE, :SIZE], dtype=np.float32) / 255.0, elapsed

            first = [decode_once(channel) for channel in channels]
            second = [decode_once(channel) for channel in channels]
            source = ProbeSource({
                CHANNEL_A: {"coarse": first[0][0], "fine": first[0][0]},
                CHANNEL_B: {"coarse": first[1][0], "fine": first[1][0]},
            })
            app, widget = create_probe(source)
            selected = {CHANNEL_A: "fine", CHANNEL_B: "fine"}
            widget.set_selected_levels(selected)
            process_events(app)
            actual = widget.readback_rgba()
            expected = cpu_reference(source, selected, widget.parameters)
            numeric = comparison(actual, expected)
            cleanup = widget.dispose()
            widget.close()
            process_events(app)
            return {
                "status": "measured",
                "path": str(path),
                "read_only": True,
                "tifffile_version": tifffile.__version__,
                "channels": list(channels),
                "level": level,
                "level_shape_cyx": list(selected_level.shape),
                "crop_yx": [SIZE, SIZE],
                "dtype": str(selected_level.dtype),
                "cold_open_ms": open_ms,
                "first_decode_ms_per_channel": [item[1] for item in first],
                "same_handle_repeat_decode_ms_per_channel": [item[1] for item in second],
                "texture_upload_submit": timing_summary(widget.timings["upload_submit_ms"]),
                "fbo_draw_readback": timing_summary(widget.timings["fbo_draw_readback_ms"]),
                "cpu_reference_comparison": numeric,
                "cpu_match_le_1_lsb": _passes(numeric),
                "cleanup": cleanup,
                "limits": "same-handle repeat is an observed warm definition, not a general TIFF cache guarantee",
            }
    except Exception as exc:
        return {"status": "not_measured", "reason": f"read-only WSI probe failed: {type(exc).__name__}: {exc}"}


def markdown_report(result: Mapping[str, object], json_name: str) -> str:
    gates = dict(result.get("pass_gates", {}))
    cap = dict(result.get("capabilities", {}))
    blocker = result.get("blocker")
    measured = result.get("status") == "measured"
    core_gates = [name for name in gates if name != "desktop_presentation"]
    core_passed = measured and all(bool(gates[name]) for name in core_gates)
    result_text = "G0.1 core feasibility passed; desktop presentation remains separately limited" if core_passed else "blocked or failed; inspect raw evidence"
    return f"""# Step1 G0.1 PyOpenGL Probe

- Raw data: [{json_name}]({json_name})
- Result: **{result_text}**
- Blocker: **{blocker or 'none'}**
- Mode: synthetic float32 raw planes; this is not a provider/scheduler/real-project I/O benchmark.
- PyOpenGL: `{cap.get('pyopengl_version', 'unavailable')}` from `{cap.get('pyopengl_file', 'unavailable')}`
- Qt platform: `{os.environ.get('QT_QPA_PLATFORM', 'desktop/default')}`
- GL_VENDOR / GL_RENDERER: `{cap.get('gl_vendor', 'unavailable')}` / `{cap.get('gl_renderer', 'unavailable')}`
- GL_VERSION / GLSL: `{cap.get('gl_version', 'unavailable')}` / `{cap.get('glsl_version', 'unavailable')}`
- Software renderer detected: `{cap.get('software_renderer', 'unavailable')}`

## Evidence limits

Qt owns the sole window/context/event lifecycle. PyOpenGL makes texture,
shader, draw, FBO/readback and deletion calls only while that Qt context is
current. FBO readback is a CPU/GPU pixel-correctness boundary; it is neither
actual desktop presentation nor compositor/vsync/input-to-display evidence.
Upload and draw submission durations are explicitly not GPU-completion claims.

## Exit gates

""" + "\n".join(f"- `{name}`: **{'pass' if value else 'not passed'}**" for name, value in gates.items()) + """

## G1 interface recommendation (not implemented)

If a future separately approved G1 starts, its `Step1GpuLayer` should accept
immutable source descriptors (channel/source/generation/tile/world geometry,
independently available coarse/fine planes and valid mask), existing display
snapshots, and an existing ViewBox viewport snapshot. It may own only Qt GL
resources and a bounded local texture cache. It must not own the camera,
provider, scheduler, raw cache, shared UI, source authority or a new state
system. Future product mounting remains G3-only.

G0.1 已停止；G1 未启动；生产界面未接管；未提交、未 push。
"""


def _environment(args) -> Dict[str, object]:
    opengl_version = None
    opengl_file = None
    try:
        import OpenGL
        opengl_version = OpenGL.__version__
        opengl_file = OpenGL.__file__
    except ImportError:
        pass
    try:
        distribution_version = importlib.metadata.version("PyOpenGL")
    except importlib.metadata.PackageNotFoundError:
        distribution_version = None
    return {
        "hostname": socket.gethostname(),
        "os": platform.platform(),
        "executable": sys.executable,
        "python": sys.version.split()[0],
        "sys_path": sys.path,
        "pyqt": QtCore.PYQT_VERSION_STR,
        "qt": QtCore.QT_VERSION_STR,
        "pyopengl_version": opengl_version,
        "pyopengl_file": opengl_file,
        "pyopengl_distribution_version": distribution_version,
        "requested_platform": args.platform,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("offscreen", "desktop", "auto"), default="auto")
    parser.add_argument("--out", type=pathlib.Path, required=True,
                        help="output prefix below docs/benchmarks/step1_gpu_demo")
    parser.add_argument("--real-wsi", type=pathlib.Path,
                        help="optional read-only OME-TIFF demonstration data path")
    parser.add_argument("--real-level", type=int, default=3,
                        help="pyramid level for the optional read-only TIFF probe")
    parser.add_argument("--real-channels", type=int, nargs=2, default=(0, 1), metavar=("CHANNEL_A", "CHANNEL_B"),
                        help="two channel indices for the optional read-only TIFF probe")
    args = parser.parse_args()
    if args.platform == "desktop" and os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        parser.error("desktop requested but QT_QPA_PLATFORM is already offscreen")
    out = args.out.resolve()
    if BENCHMARK_ROOT.resolve() not in out.parents:
        parser.error(f"--out must be below {BENCHMARK_ROOT}")
    visible = args.platform == "desktop" or (args.platform == "auto" and bool(os.environ.get("DISPLAY")))
    try:
        result = run_probe(visible=visible)
    except (ProbeBlocked, RuntimeError) as exc:
        result = {"status": "blocked", "blocker": str(exc), "qt_qpa_platform": os.environ.get("QT_QPA_PLATFORM")}
    if args.real_wsi is not None and result.get("status") == "measured":
        result["real_data_probe"] = probe_real_wsi(args.real_wsi.resolve(), tuple(args.real_channels), args.real_level)
    result["environment"] = _environment(args)
    out.parent.mkdir(parents=True, exist_ok=True)
    json_path = out.with_suffix(".json")
    markdown_path = out.with_suffix(".md")
    json_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(markdown_report(result, json_path.name), encoding="utf-8")
    print(json_path)
    print(markdown_path)
    return 0 if result.get("status") == "measured" else 2


if __name__ == "__main__":
    raise SystemExit(main())
