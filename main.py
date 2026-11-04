"""
block01/main.py — Entry point for the Fusion GUI application.
"""

import sys
import multiprocessing as mp

from .qt import QtGui
import pyqtgraph as pg

pg.setConfigOptions(antialias=True, imageAxisOrder="row-major")

from .ui.main_window import MainWindow
from .utils.gui_watchdog import start_gui_watchdog


def main():
    # The code version results record, captured before anything runs
    # (docs/v17_review_fixes_plan.md §6c).
    from .core import provenance
    provenance.git_commit()
    # Block A9 §35: how long a thread may hold the GIL while the GUI thread
    # waits for it (CPython's default is 5 ms). Measurement switch for now.
    import os
    interval = os.environ.get("BLOCK01_SWITCH_INTERVAL_MS")
    if interval:
        sys.setswitchinterval(float(interval) / 1000.0)
    # Block A9: with BLOCK01_PERF_DISPATCH=1 (and BLOCK01_PERF=1) slow Qt
    # event deliveries are traced; otherwise this is a plain QApplication.
    # Block A9 §40: swaps that do not hold the GUI thread for the refresh
    from .ui.gpu_warmup import configure_surface_format
    configure_surface_format()
    from .utils import perf_dispatch
    app = perf_dispatch.make_application(sys.argv)
    app.setStyle("Fusion")
    # An unexpected error is logged and said, not the end of the program
    # (docs/v17_error_guard_plan.md §1); before any worker starts.
    from .utils import error_hook
    error_hook.install(app)

    # The theme (docs/v17_desktop_ui_plan.md): DeepSeek Harness's colours,
    # dark by default (BLOCK01_THEME / the `ui/theme` setting choose light).
    from .ui import theme
    theme.apply(app)

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
        sys.exit(app.exec())
    finally:
        if watchdog is not None:
            watchdog.stop()
        # R3 plan §2.5: reader threads told to stop (closed or fallen-back
        # viewers) get one shared second in all, then are left to the OS
        from .utils import closing_threads
        from .ui.step1_core_adapter import core_unavailable_reason
        left = closing_threads.wait_all(rust_ok=lambda: not core_unavailable_reason())
        if left["python_left"] or left["rust_left"]:
            print(f"[Block01] at exit, reader threads still running (left to the OS): "
                  f"{left['python_left']} + {left['rust_left']} Rust")


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    main()
