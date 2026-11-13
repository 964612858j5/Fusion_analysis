"""Warm the GPU display path while the user is still in Step0 (block A9-O3-1).

User ruling 2026-10-07: "start the GPU at program start, during the time
the user fills in the paths and clicks Load". The first Step1 entry paid
647 ms on the GUI thread for its first OpenGL widget (measured): 333 ms
importing PyOpenGL, ~270 ms for Qt to create a context and switch the
window to GL composition, 38 ms of shaders.

What is warmed is the PyOpenGL import, on a background thread, while
Step0 is up -- it was 333 of those 647 ms.

A 1x1 QOpenGLWidget shown at start to create the context early was tried
and REMOVED (measured, 2026-10-07): it bought nothing the import had not
(gl_show 653 -> 341 ms either way), and it put the whole window on GL
composition from the start, so every Step0 repaint went through the GL
path -- Step0's CPU view wheel went from 6 ms to 403 ms (p50) on WSL.
A failure leaves everything as it was: the Step1 layer then imports itself.
BLOCK01_GPU_WARMUP=0 turns it off.
"""

import os
import sys
import threading

from ..qt import QtCore, QtGui

from ..utils import perf_trace


def configure_surface_format():
    """Set the application's buffer swap interval; called before the
    QApplication exists.

    Default 1 (swaps wait for the display refresh). Block A9 §40 had asked
    for 0, to keep the GUI thread from waiting up to a refresh period in the
    whole-window swap (a single notch on screen 28-33 ms -> 17 ms). On the
    user's machine (NVIDIA 535, GNOME/mutter X11) interval 0 left the Step0
    compare panels' swaps off the screen -- frames drawn, nothing shown, drag
    dead -- and interval 1 fixed it (user acceptance 2026-10-29, S0-C).
    `BLOCK01_VSYNC=0` asks for 0 again, for diagnosis. A REQUEST, not a
    guarantee: a driver may ignore it. Returns the interval asked for.

    macOS only: the default format is also OpenGL 3.3 core, the version
    and profile every GL widget here asks for (Step0 host, Step1 layers).
    macOS cannot share a core-profile context with the window's default
    (legacy 2.1) one -- Qt logs "Could not create NSOpenGLContext with
    shared context, falling back to unshared context" -- and the window
    then composites the widget's texture from the wrong context: after
    Load, Step0's viewer showed a stale, upside-down copy of the window
    and never the slide, until a resize (reproduced 2026-11-09, Mac mini).
    The fix is for macOS only; other systems keep their earlier setting."""
    interval = 0 if os.environ.get("BLOCK01_VSYNC") == "0" else 1
    fmt = QtGui.QSurfaceFormat.defaultFormat()
    fmt.setSwapInterval(interval)
    if sys.platform == "darwin":
        fmt.setRenderableType(QtGui.QSurfaceFormat.OpenGL)
        fmt.setVersion(3, 3)
        fmt.setProfile(QtGui.QSurfaceFormat.CoreProfile)
    QtGui.QSurfaceFormat.setDefaultFormat(fmt)
    return interval


class GpuWarmup(QtCore.QObject):
    """Import `OpenGL.GL` on a thread; `imported` says how it went."""

    _imported = QtCore.Signal(bool)

    def __init__(self, window):
        super().__init__(window)
        self.imported = None
        self._imported.connect(self._on_imported, QtCore.Qt.QueuedConnection)

    def start(self):
        threading.Thread(target=self._import, name="gpu-warmup",
                         daemon=True).start()

    def _import(self):
        ok = True
        with perf_trace.span("gpu.warm.import"):
            try:
                from .step1_gpu_layer import configure_pyopengl
                configure_pyopengl()        # before the first OpenGL.GL import
                from OpenGL import GL  # noqa: F401
            except Exception:                               # noqa: BLE001
                ok = False
        self._imported.emit(ok)

    def _on_imported(self, ok):
        self.imported = bool(ok)
        perf_trace.mark("gpu.warm.ready", ok=self.imported)


def compose_with_gl_before_show(window):
    """Put `window` on GL composition before it is first shown (backlog 17,
    docs/v17_acceptance_notes_plan.md B17). Step0's first GL host
    (`step0/engine_mount.py` `start`: show, `grabFramebuffer`, hide) made
    Qt switch the shown window from raster to GL composition: its native
    window destroyed and recreated -- the blink on the first Load of a
    session (measured on the Mac mini, 2026-11-13). Done here, off screen,
    the window is created GL-composed and no Load recreates it.

    Only when Step0 will use its engine (`engine_refusal() == ""`): without
    it every view is drawn on the CPU, and GL composition from the start
    made that slow (the 2026-10-07 note above). A 1x1 `QOpenGLWidget`
    child is shown, initialized and hidden; a widget only shown, or hidden,
    does not switch the window. Returns the widget (kept by the window), or
    None when not done; a failure leaves the window as it was."""
    if os.environ.get("BLOCK01_GL_FIRST", "1") == "0":
        return None
    try:
        from .step0.engine_mount import engine_refusal
        if engine_refusal():
            return None
        from ..qt import QtWidgets
        with perf_trace.span("gpu.compose_before_show"):
            gl = QtWidgets.QOpenGLWidget(window)
            gl.setObjectName("gl_composition")
            gl.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
            gl.resize(1, 1)
            gl.show()
            gl.grabFramebuffer()
            gl.hide()
    except Exception as exc:                                # noqa: BLE001 -- a look only
        print(f"[GPU] window not put on GL composition before showing it: {exc}")
        return None
    return gl


def start(window):
    """Begin the warm-up for `window` (a shown MainWindow). Returns the
    GpuWarmup, or None when it is turned off."""
    if os.environ.get("BLOCK01_GPU_WARMUP", "1") == "0":
        return None
    warmup = GpuWarmup(window)
    warmup.start()
    return warmup


__all__ = ["start", "GpuWarmup", "compose_with_gl_before_show"]
