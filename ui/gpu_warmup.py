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
import threading

from PyQt5 import QtCore, QtGui

from ..utils import perf_trace


def configure_surface_format():
    """Block A9 §40: ask for buffer swaps that do not wait for the display's
    refresh (swap interval 0). With an OpenGL widget in the window, Qt
    composes and swaps the whole window on the GUI thread; the default
    interval 1 made that swap wait up to a refresh period, during which no
    input was handled (measured: a single notch on screen 28-33 ms -> 17 ms).
    The desktop compositor (GNOME, Windows DWM) still presents in step with
    the display. A REQUEST, not a guarantee: a driver may ignore it.
    `BLOCK01_VSYNC=1` leaves Qt's default untouched (a machine that tears).
    Called before the QApplication exists."""
    if os.environ.get("BLOCK01_VSYNC") == "1":
        return False
    fmt = QtGui.QSurfaceFormat.defaultFormat()
    fmt.setSwapInterval(0)
    QtGui.QSurfaceFormat.setDefaultFormat(fmt)
    return True


class GpuWarmup(QtCore.QObject):
    """Import `OpenGL.GL` on a thread; `imported` says how it went."""

    _imported = QtCore.pyqtSignal(bool)

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


def start(window):
    """Begin the warm-up for `window` (a shown MainWindow). Returns the
    GpuWarmup, or None when it is turned off."""
    if os.environ.get("BLOCK01_GPU_WARMUP", "1") == "0":
        return None
    warmup = GpuWarmup(window)
    warmup.start()
    return warmup


__all__ = ["start", "GpuWarmup"]
