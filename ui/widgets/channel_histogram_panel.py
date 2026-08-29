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
        # No "intensity" x-axis label — the tick values already say what the axis is,
        # and dropping the label gives the freed height to the histogram curve.
        self._plot.getPlotItem().getAxis("bottom").showLabel(False)
        lay.addWidget(self._plot)

        # Smoothed density curve; the area under it is filled with the active
        # channel's color (set_color / set_data(color=...)).
        self._curve = pg.PlotCurveItem(
            fillLevel=0, brush=(80, 140, 200, 120),
            pen=pg.mkPen("#61afef", width=1.5),
        )
        self._plot.addItem(self._curve)

        self._min_line = pg.InfiniteLine(
            angle=90, movable=True, pen=pg.mkPen("#f0a030", width=2))
        # Intensity Min is never negative: bound the Min handle's lower drag limit
        # at x=0 (intensity-zero on this axis). pyqtgraph clamps BOTH the drag and
        # setValue() to this bound, so the handle physically stops at 0 — it cannot
        # slide into the negative region (the spinbox clamp from ca4e7d9 only fixed
        # the OTHER input path). Max is NOT lower-bounded.
        self._min_line.setBounds([0, None])
        self._max_line = pg.InfiniteLine(
            angle=90, movable=True, pen=pg.mkPen("#e06060", width=2))
        self._plot.addItem(self._min_line)
        self._plot.addItem(self._max_line)
        self._min_line.sigPositionChanged.connect(self._on_line_moved)
        self._max_line.sigPositionChanged.connect(self._on_line_moved)

    def set_color(self, color):
        """Curve/fill follow the channel's display color."""
        c = pg.mkColor(color)
        self._curve.setPen(pg.mkPen(c, width=1.5))
        fill = pg.mkColor(c)
        fill.setAlpha(110)
        self._curve.setBrush(fill)

    def set_data(self, image, min_value=None, max_value=None, bins=256,
                 color=None):
        """Set the histogram from a channel image and place the window lines.

        Draws a smoothed density curve (not raw bars) filled with the channel
        color, and bounds the view: zoom-out stops at the initial full range,
        zoom-in is capped, and panning cannot leave the data range.
        """
        self._updating = True
        try:
            if color is not None:
                self.set_color(color)
            arr = np.asarray(image)
            arr = arr[np.isfinite(arr)]
            vb = self._plot.getPlotItem().getViewBox()
            if arr.size == 0:
                self._curve.setData(np.array([0.0, 1.0]), np.array([0.0, 0.0]))
                vb.setLimits(xMin=0.0, xMax=1.0, yMin=0.0,
                             maxXRange=1.0, minXRange=0.01)
                vb.setXRange(0.0, 1.0, padding=0)
            else:
                lo = float(arr.min())
                hi = float(arr.max())
                if hi <= lo:
                    hi = lo + 1.0
                counts, edges = np.histogram(arr, bins=bins, range=(lo, hi))
                centers = 0.5 * (edges[:-1] + edges[1:])
                # Gaussian smoothing (sigma ~2.5 bins) turns the bar staircase
                # into a curve without shifting the distribution.
                k = np.exp(-0.5 * (np.arange(-8, 9) / 2.5) ** 2)
                k /= k.sum()
                smooth = np.convolve(counts.astype(float), k, mode="same")
                self._curve.setData(centers, smooth)

                # View bounds: include the window lines if they sit outside
                # the data range, plus a hair of padding.
                xmin = min(lo, float(min_value) if min_value is not None else lo)
                xmax = max(hi, float(max_value) if max_value is not None else hi)
                pad = 0.02 * (xmax - xmin)
                span = (xmax - xmin) + 2 * pad
                vb.setLimits(xMin=xmin - pad, xMax=xmax + pad, yMin=0.0,
                             maxXRange=span,            # no zoom-out past initial
                             minXRange=max(span / 100.0, 1e-9))  # zoom-in cap
                vb.setXRange(xmin - pad, xmax + pad, padding=0)
                ymax = float(smooth.max())
                vb.setYRange(0.0, ymax * 1.05 if ymax > 0 else 1.0, padding=0)

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
