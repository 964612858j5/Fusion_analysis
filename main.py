"""
block01/main.py — Entry point for the Fusion GUI application.
"""

import sys
import multiprocessing as mp

from PyQt5 import QtGui
from PyQt5.QtWidgets import QApplication
import pyqtgraph as pg

pg.setConfigOptions(antialias=True, imageAxisOrder="row-major")

from .ui.main_window import MainWindow
from .utils.gui_watchdog import start_gui_watchdog


def main():
    app = QApplication(sys.argv)
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
    # for 2 s or more -- see utils/gui_watchdog.py. Kept for the freeze on
    # the first Save that manual testing reported and offscreen runs could
    # not reproduce. Set BLOCK01_GUI_WATCHDOG=0 to disable.
    watchdog = start_gui_watchdog()

    win = MainWindow()
    win.show()
    try:
        sys.exit(app.exec_())
    finally:
        if watchdog is not None:
            watchdog.stop()


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    main()
