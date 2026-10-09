"""
block01/main.py — Entry point for the Fusion GUI application.
"""

import sys
import multiprocessing as mp

from PyQt5 import QtGui
import pyqtgraph as pg

pg.setConfigOptions(antialias=True, imageAxisOrder="row-major")

from .ui.main_window import MainWindow
from .utils.gui_watchdog import start_gui_watchdog


def main():
    # Block A9 §35: how long a thread may hold the GIL while the GUI thread
    # waits for it (CPython's default is 5 ms). Measurement switch for now.
    import os
    interval = os.environ.get("BLOCK01_SWITCH_INTERVAL_MS")
    if interval:
        sys.setswitchinterval(float(interval) / 1000.0)
    # Block A9: with BLOCK01_PERF_DISPATCH=1 (and BLOCK01_PERF=1) slow Qt
    # event deliveries are traced; otherwise this is a plain QApplication.
    from .utils import perf_dispatch
    app = perf_dispatch.make_application(sys.argv)
    app.setStyle("Fusion")

    pal = QtGui.QPalette()
    pal.setColor(QtGui.QPalette.Window,          QtGui.QColor(28, 28, 28))
    pal.setColor(QtGui.QPalette.WindowText,      QtGui.QColor(220, 220, 220))
    pal.setColor(QtGui.QPalette.Base,            QtGui.QColor(18, 18, 18))
    pal.setColor(QtGui.QPalette.AlternateBase,   QtGui.QColor(38, 38, 38))
    pal.setColor(QtGui.QPalette.Text,            QtGui.QColor(220, 220, 220))
    pal.setColor(QtGui.QPalette.Button,          QtGui.QColor(48, 48, 48))
    pal.setColor(QtGui.QPalette.ButtonText,      QtGui.QColor(220, 220, 220))
    pal.setColor(QtGui.QPalette.Highlight,       QtGui.QColor(42, 130, 218))
    pal.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor(0, 0, 0))
    app.setPalette(pal)

    # Reports, with the GUI thread's stack, whenever the event loop stops
    # for 2 s or more -- see utils/gui_watchdog.py. A diagnostic for freezes
    # that offscreen runs cannot reproduce; off unless BLOCK01_GUI_WATCHDOG=1.
    watchdog = start_gui_watchdog()
    # Block A9 §32: full garbage collections wait for an idle moment
    # instead of pausing a wheel gesture (utils/gc_pacer.py)
    from .utils import gc_pacer
    app._gc_pacer = gc_pacer.start(app)

    win = MainWindow()
    win.show()
    # Block A9-O3-1: the GPU display path is warmed while Step0 is up
    from .ui import gpu_warmup
    win._gpu_warmup = gpu_warmup.start(win)
    try:
        sys.exit(app.exec_())
    finally:
        if watchdog is not None:
            watchdog.stop()


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    main()
