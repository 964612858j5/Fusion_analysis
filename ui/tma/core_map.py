"""The TMA page's slide map (F3, docs/tma_f3_page_application.md §2): the
whole-slide DAPI thumbnail with one circle per piece, its label, the
selection's 8 handles, the numbering line with its band, and the clicked
sequence. A view only: it draws what the page gives it (`set_items`) and
reports the mouse to the page (`controller`); no layout logic here.

Coordinates are level-0 pixels throughout: the thumbnail is placed on the
level-0 rectangle, so every overlay uses the draft's own numbers.
Odon's design is reused (docs §2.6): pick by hit test, drag = one step,
labels in screen space on a rounded dark box, only the visible ones.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtGui
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QVBoxLayout, QWidget

HANDLE_PX = 6          # half size of a resize handle, screen pixels
LABEL_MIN_R_PX = 6     # a circle smaller than this on screen gets no label


class _Overlay(pg.GraphicsObject):
    """Every circle, label, handle and the numbering line in one item."""

    def __init__(self):
        super().__init__()
        self.items: List[Dict] = []
        self.selected: set = set()
        self.handles_for: Optional[Dict] = None
        self.line = None                 # ((y0, x0), (y1, x1), half_width)
        self.sequence: List = []         # [(y, x)] clicked order
        self.order_labels: Dict = {}     # piece/core id -> "k"
        self._rect = QtCore.QRectF(0, 0, 1, 1)

    def set_bounds(self, w, h):
        self.prepareGeometryChange()
        self._rect = QtCore.QRectF(0, 0, float(w), float(h))

    def boundingRect(self):
        return self._rect

    def paint(self, p, *_args):
        scale = math.hypot(p.transform().m11(), p.transform().m12()) or 1.0
        for it in self.items:
            pen = QtGui.QPen(QtGui.QColor(it["color"]))
            pen.setCosmetic(True)
            pen.setWidthF(3.0 if it["id"] in self.selected else 1.5)
            if it.get("dash"):
                pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            r = it["r"]
            p.drawEllipse(QtCore.QPointF(it["cx"], it["cy"]), r, r)
        if self.line is not None:
            (y0, x0), (y1, x1), hw = self.line
            dy, dx = y1 - y0, x1 - x0
            n = math.hypot(dy, dx) or 1.0
            oy, ox = dx / n * hw, -dy / n * hw
            poly = QtGui.QPolygonF([QtCore.QPointF(x0 + ox, y0 + oy), QtCore.QPointF(x1 + ox, y1 + oy),
                                    QtCore.QPointF(x1 - ox, y1 - oy), QtCore.QPointF(x0 - ox, y0 - oy)])
            p.setPen(Qt.NoPen)
            p.setBrush(QtGui.QColor(80, 160, 255, 60))
            p.drawPolygon(poly)
            pen = QtGui.QPen(QtGui.QColor(80, 160, 255))
            pen.setCosmetic(True)
            pen.setWidthF(2)
            p.setPen(pen)
            p.drawLine(QtCore.QPointF(x0, y0), QtCore.QPointF(x1, y1))
        if len(self.sequence) > 1:
            pen = QtGui.QPen(QtGui.QColor(255, 210, 60))
            pen.setCosmetic(True)
            pen.setWidthF(2)
            p.setPen(pen)
            p.drawPolyline(QtGui.QPolygonF([QtCore.QPointF(x, y) for y, x in self.sequence]))
        # screen-space parts (handles, labels): drawn at an item point with
        # the view's scale undone, so they keep their pixel size and stay upright
        t = p.transform()
        sx = math.hypot(t.m11(), t.m12()) or 1.0
        sy = math.hypot(t.m21(), t.m22()) or 1.0
        flip = -1.0 if t.m22() < 0 else 1.0
        font = p.font()
        font.setPointSizeF(8)
        p.setFont(font)
        fm = QtGui.QFontMetricsF(font)

        def at(x, y):
            p.save()
            p.translate(x, y)
            p.scale(1.0 / sx, flip / sy)

        if self.handles_for is not None:
            p.setPen(QtGui.QPen(QtGui.QColor("white")))
            p.setBrush(QtGui.QColor(40, 40, 40))
            for hy, hx in handle_points(self.handles_for):
                at(hx, hy)
                p.drawRect(QtCore.QRectF(-HANDLE_PX, -HANDLE_PX, 2 * HANDLE_PX, 2 * HANDLE_PX))
                p.restore()
        vb = self.getViewBox()
        visible = vb.viewRect() if vb is not None else None
        for it in self.items:
            text = self.order_labels.get(it["id"]) or it.get("label")
            if not text or it["r"] * scale < LABEL_MIN_R_PX:
                continue
            if visible is not None and not visible.contains(QtCore.QPointF(it["cx"], it["cy"])):
                continue                                  # only the visible ones (Odon mosaic/mod.rs:6225)
            at(it["cx"] + it["r"] * 0.71, it["cy"] - it["r"] * 0.71)
            box = QtCore.QRectF(2, -fm.height() - 2, fm.width(text) + 8, fm.height() + 2)
            p.setPen(Qt.NoPen)
            p.setBrush(QtGui.QColor(0, 0, 0, 170))
            p.drawRoundedRect(box, 3, 3)
            p.setPen(QtGui.QColor(235, 235, 235))
            p.drawText(box, Qt.AlignCenter, text)
            p.restore()


def handle_points(it) -> List:
    """The 8 handles of a circle: its box corners and edge midpoints (y, x)."""
    cy, cx, r = it["cy"], it["cx"], it["r"]
    return [(cy + dy * r, cx + dx * r) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if (dy, dx) != (0, 0)]


class _View(pg.ViewBox):
    """Left button goes to the controller; right / middle drag pans, the
    wheel zooms -- except while a numbering line is shown, when the wheel
    sets the band width."""

    def __init__(self, owner):
        super().__init__(lockAspect=True, invertY=True, enableMenu=False)
        self._owner = owner
        self._left_pans = True
        self.setMouseMode(pg.ViewBox.PanMode)

    def _pt(self, ev):
        q = self.mapSceneToView(ev.scenePos())
        return float(q.y()), float(q.x())

    def mouseClickEvent(self, ev):
        c = self._owner.controller
        if c is None:
            return super().mouseClickEvent(ev)
        if ev.button() == Qt.LeftButton:
            ev.accept()
            c.map_click(*self._pt(ev), ev.modifiers())
        elif ev.button() == Qt.RightButton:
            ev.accept()
            c.map_context(*self._pt(ev), ev.screenPos())
        else:
            super().mouseClickEvent(ev)

    def mouseDragEvent(self, ev, axis=None):
        c = self._owner.controller
        if ev.button() == Qt.LeftButton and c is not None:
            ev.accept()
            if ev.isStart():
                y, x = self._pt(ev)
                q = self.mapSceneToView(ev.buttonDownScenePos())
                self._left_pans = not c.map_drag("start", (float(q.y()), float(q.x())), (y, x))
            if not self._left_pans:
                y, x = self._pt(ev)
                q = self.mapSceneToView(ev.buttonDownScenePos())
                if not ev.isStart():
                    c.map_drag("finish" if ev.isFinish() else "move", (float(q.y()), float(q.x())), (y, x))
                return
        if ev.button() in (Qt.LeftButton, Qt.RightButton, Qt.MiddleButton):
            ev.accept()
            tr = self.childGroup.transform()
            tr = pg.functions.invertQTransform(tr)
            d = tr.map(ev.lastPos()) - tr.map(ev.pos())
            self.translateBy(x=d.x(), y=d.y())
            return
        super().mouseDragEvent(ev, axis)

    def wheelEvent(self, ev, axis=None):
        c = self._owner.controller
        if c is not None and c.map_wheel(ev.delta()):
            ev.accept()
            return
        super().wheelEvent(ev, axis)


class CoreMap(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.controller = None
        self.setObjectName("tma_core_map")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.gview = pg.GraphicsLayoutWidget()
        self.vb = _View(self)
        self.gview.addItem(self.vb)
        self.img = pg.ImageItem(axisOrder="row-major")   # explicit: the app's global order is not assumed
        self.vb.addItem(self.img)
        self.overlay = _Overlay()
        self.vb.addItem(self.overlay)
        lay.addWidget(self.gview)
        self.shape = None

    # ── content ──
    def set_slide(self, shape, thumb: Optional[np.ndarray]):
        """`shape` is the level-0 (h, w); `thumb` any downsampled DAPI."""
        self.shape = tuple(int(v) for v in shape)
        h, w = self.shape
        self.overlay.set_bounds(w, h)
        if thumb is None:
            self.img.clear()
        else:
            a = np.asarray(thumb, dtype=np.float32)
            lo, hi = np.percentile(a, 1), np.percentile(a, 99.7)
            self.img.setImage(np.clip((a - lo) / max(hi - lo, 1e-6), 0, 1), autoLevels=False, levels=(0, 1))
            self.img.setRect(QtCore.QRectF(0, 0, w, h))
        self.vb.setRange(QtCore.QRectF(0, 0, w, h), padding=0.02)

    def clear(self):
        self.img.clear()
        self.set_items([])
        self.set_line(None)
        self.set_sequence([])

    def set_items(self, items: Sequence[Dict], selected=(), handles_for=None, order_labels=None):
        self.overlay.items = list(items)
        self.overlay.selected = set(selected)
        self.overlay.handles_for = handles_for
        self.overlay.order_labels = dict(order_labels or {})
        self.overlay.update()

    def set_line(self, line):
        self.overlay.line = line
        self.overlay.update()

    def set_sequence(self, points):
        self.overlay.sequence = list(points)
        self.overlay.update()

    # ── camera ──
    def centre_on(self, cy, cx, span):
        """Show a `span`-wide level-0 square around (cy, cx)."""
        self.vb.setRange(QtCore.QRectF(cx - span / 2, cy - span / 2, span, span), padding=0)

    def screen_px(self) -> float:
        """Level-0 pixels per screen pixel (for hit tolerances)."""
        (px, _py) = self.vb.viewPixelSize()
        return float(px) or 1.0
