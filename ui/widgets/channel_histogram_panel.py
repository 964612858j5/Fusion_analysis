"""
block01/ui/widgets/channel_histogram_panel.py — v13.1 active-channel histogram.

Top of the right-hand active-channel inspector (see
docs/v13_1_channel_conditioning/04_UI_REDESIGN_SPEC.md).

Shows the intensity histogram of the active channel with draggable min/max
window lines. Dragging a line emits window_changed(min, max) so the inspector
spin-boxes and the live preview stay in sync.

Built on PyQtGraph (no napari). Host-agnostic.
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtWidgets
from PyQt5.QtCore import pyqtSignal


class ChannelHistogramPanel(QtWidgets.QWidget):
    """Histogram + draggable window lines for one channel.

    Signals
    -------
    window_changed(float, float) : emitted while the user drags a window line
    """

    window_changed = pyqtSignal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._updating = False

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        self._plot = pg.PlotWidget()
        self._plot.setBackground("#101010")
        self._plot.setMaximumHeight(160)
        self._plot.showGrid(x=False, y=False)
        self._plot.getPlotItem().setMouseEnabled(x=True, y=False)
        self._plot.setLabel("bottom", "intensity")
        lay.addWidget(self._plot)

        # stepMode is supplied per-setData call (histogram edges = counts+1);
        # constructing with stepMode and no data raises, so defer it.
        self._curve = pg.PlotCurveItem(
            fillLevel=0, brush=(80, 140, 200, 120),
            pen=pg.mkPen("#61afef", width=1),
        )
        self._plot.addItem(self._curve)

        self._min_line = pg.InfiniteLine(
            angle=90, movable=True, pen=pg.mkPen("#f0a030", width=2))
        self._max_line = pg.InfiniteLine(
            angle=90, movable=True, pen=pg.mkPen("#e06060", width=2))
        self._plot.addItem(self._min_line)
        self._plot.addItem(self._max_line)
        self._min_line.sigPositionChanged.connect(self._on_line_moved)
        self._max_line.sigPositionChanged.connect(self._on_line_moved)

    def set_data(self, image, min_value=None, max_value=None, bins=256):
        """Set the histogram from a channel image and place the window lines."""
        self._updating = True
        try:
            arr = np.asarray(image)
            arr = arr[np.isfinite(arr)]
            if arr.size == 0:
                self._curve.setData(np.array([0.0, 1.0]), np.array([0.0]),
                                    stepMode="center")
            else:
                lo = float(arr.min())
                hi = float(arr.max())
                if hi <= lo:
                    hi = lo + 1.0
                counts, edges = np.histogram(arr, bins=bins, range=(lo, hi))
                self._curve.setData(edges, counts.astype(float), stepMode="center")

            if min_value is not None:
                self._min_line.setValue(float(min_value))
            if max_value is not None:
                self._max_line.setValue(float(max_value))
        finally:
            self._updating = False

    def set_window(self, min_value, max_value):
        """Move the window lines without emitting (programmatic sync)."""
        self._updating = True
        try:
            self._min_line.setValue(float(min_value))
            self._max_line.setValue(float(max_value))
        finally:
            self._updating = False

    def window(self):
        return float(self._min_line.value()), float(self._max_line.value())

    def _on_line_moved(self):
        if self._updating:
            return
        lo = float(self._min_line.value())
        hi = float(self._max_line.value())
        self.window_changed.emit(lo, hi)
