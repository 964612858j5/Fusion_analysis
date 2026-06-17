"""
block01/ui/widgets/channel_viewer_canvas.py — v13.1 center viewer canvas.

Center column of the channel-first viewer (see
docs/v13_1_channel_conditioning/04_UI_REDESIGN_SPEC.md).

A large PyQtGraph image canvas. For the Phase 2 prototype it supports:
  - displaying a single float32 [0,1] image (the remapped active channel, or
    a fused / DAPI underlay supplied by the host),
  - a raw-vs-remapped split view (left = raw, right = remapped),
  - basic opacity-blended overlay of a second image (e.g. mask / DAPI).

No raw/corrected source text is shown here (spec). No napari. Host-agnostic:
the workbench feeds it ready-to-display arrays.
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtWidgets


def _to_display(img):
    """Coerce any array to a contiguous float32 in [0,1] for display."""
    arr = np.asarray(img).astype(np.float32)
    arr = np.nan_to_num(arr, nan=0.0, posinf=1.0, neginf=0.0)
    return np.clip(arr, 0.0, 1.0)


class ChannelViewerCanvas(QtWidgets.QWidget):
    """Large image canvas with optional raw-vs-remapped split."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._split = False
        self._raw = None
        self._remapped = None

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        self._glw = pg.GraphicsLayoutWidget()
        self._glw.setBackground("#0a0a0a")
        self._vb = self._glw.addViewBox(row=0, col=0)
        self._vb.setAspectLocked(True)
        self._vb.invertY(True)
        self._img_item = pg.ImageItem(axisOrder="row-major")
        self._vb.addItem(self._img_item)
        lay.addWidget(self._glw, stretch=1)

        self._status = QtWidgets.QLabel("No image loaded")
        self._status.setStyleSheet("color:#888;font-size:10px;")
        lay.addWidget(self._status)

    def set_split(self, enabled):
        self._split = bool(enabled)
        self._refresh()

    def set_images(self, raw=None, remapped=None):
        """Provide raw and/or remapped versions of the active channel."""
        self._raw = None if raw is None else _to_display(raw)
        self._remapped = None if remapped is None else _to_display(remapped)
        self._refresh()

    def clear(self):
        self._raw = None
        self._remapped = None
        self._img_item.clear()
        self._status.setText("No image loaded")

    def _refresh(self):
        primary = self._remapped if self._remapped is not None else self._raw
        if primary is None:
            self.clear()
            return

        if self._split and self._raw is not None and self._remapped is not None:
            composed = self._compose_split(self._raw, self._remapped)
            self._img_item.setImage(composed, levels=(0.0, 1.0))
            self._status.setText("Split view — left: raw   right: remapped")
        else:
            self._img_item.setImage(primary, levels=(0.0, 1.0))
            label = "remapped" if self._remapped is not None else "raw"
            self._status.setText(f"Active channel — {label}")
        self._vb.autoRange()

    @staticmethod
    def _compose_split(raw, remapped):
        """Left half = raw, right half = remapped, with a thin divider."""
        if raw.shape != remapped.shape:
            # Fall back to remapped only if shapes disagree.
            return remapped
        h, w = raw.shape[:2]
        mid = w // 2
        out = remapped.copy()
        out[:, :mid] = raw[:, :mid]
        if 0 <= mid < w:
            out[:, mid:mid + 1] = 1.0  # divider line
        return out
