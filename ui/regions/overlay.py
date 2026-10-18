"""The Regions overlay (docs/region_qc_module_plan.md §2): ONE transparent
widget above the viewer's GL picture that draws every shape, label, handle,
numbering line and click sequence, and is the ONE input path.

Placed like Step1's `MontageOverlay` (ui/step1_presegmentation/
montage_view.py:75-118): a child of the viewer's viewport, transparent for
the mouse, raised after the GL layer (which raises itself on every
resize), mapping level-0 to its pixels through the viewer's ViewBox.

Input: an event filter on that viewport. A left press the tool takes
(`ToolController.press`) is consumed with its moves and release; anything
else goes on to pyqtgraph, which pans / zooms / right-clicks as in Step1.
A left press + release without movement is a click for the tool. The
wheel goes to the tool only while it wants it (the numbering band).
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt

from .tools import handle_points

LABEL_MIN_PX = 14            # a shape smaller than this on screen gets no label
CLICK_SLOP_PX = 4


class RegionsOverlay(QtWidgets.QWidget):
    def __init__(self, graphics, view_box, controller):
        viewport = graphics.viewport()
        super().__init__(viewport)
        self.setObjectName("regions_overlay")
        self.graphics, self.vb, self.controller = graphics, view_box, controller
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.items: List[Dict] = []
        self.selected: set = set()
        self.handles_for: Optional[Sequence] = None
        self.order_labels: Dict = {}
        self.line = None
        self.sequence: List = []
        self.circle = None
        self._press = None            # (pos, claimed)
        viewport.installEventFilter(self)
        view_box.sigRangeChanged.connect(self._range_changed)
        self.setGeometry(viewport.rect())
        self.show()
        self.raise_()

    def viewport(self):
        return self.parentWidget()

    def detach(self):
        try:
            self.viewport().removeEventFilter(self)
            self.vb.sigRangeChanged.disconnect(self._range_changed)
        except (RuntimeError, TypeError):
            pass
        self.setParent(None)
        self.deleteLater()

    def _range_changed(self, *_a):
        self.update()

    # ── content (the page's view interface) ──
    def set_items(self, items, selected=(), handles_for=None, order_labels=None):
        self.items = list(items)
        self.selected = set(selected)
        self.handles_for = handles_for
        self.order_labels = dict(order_labels or {})
        self.update()

    def set_line(self, line):
        self.line = line
        self.update()

    def set_sequence(self, points):
        self.sequence = list(points)
        self.update()

    def set_circle(self, circle):
        self.circle = circle
        self.update()

    def screen_px(self) -> float:
        """Level-0 pixels per screen pixel."""
        tr = self.canvas_transform()
        s = abs(tr.m11())
        return 1.0 / s if s > 0 else 1.0

    # ── coordinates ──
    def to_widget(self, x, y):
        scene = self.vb.mapViewToScene(QtCore.QPointF(x, y))
        return self.graphics.viewportTransform().map(scene)

    def to_level0(self, pos):
        scene = self.graphics.mapToScene(pos)
        q = self.vb.mapSceneToView(scene)
        return float(q.y()), float(q.x())

    def canvas_transform(self):
        (x0, x1), (y0, y1) = self.vb.viewRange()
        a, b = self.to_widget(x0, y0), self.to_widget(x1, y1)
        sx = (b.x() - a.x()) / (x1 - x0) if x1 != x0 else 1.0
        sy = (b.y() - a.y()) / (y1 - y0) if y1 != y0 else 1.0
        return QtGui.QTransform(sx, 0, 0, sy, a.x() - sx * x0, a.y() - sy * y0)

    # ── input ──
    def eventFilter(self, watched, event):  # noqa: N802
        t = event.type()
        if t in (QtCore.QEvent.Resize, QtCore.QEvent.Move, QtCore.QEvent.Show):
            self.setGeometry(watched.rect())
            QtCore.QTimer.singleShot(0, self.raise_)      # after the GL layer's own raise
            return False
        if t == QtCore.QEvent.ChildAdded:
            # a new picture layer (Rust -> Python GPU hand-over) is a new
            # sibling: stay above it
            QtCore.QTimer.singleShot(0, self.raise_)
            return False
        c = self.controller
        if c is None:
            return False
        if t == QtCore.QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            y, x = self.to_level0(event.pos())
            claimed = bool(c.press(y, x))
            self._press = (QtCore.QPoint(event.pos()), claimed)
            return claimed
        if t == QtCore.QEvent.MouseMove and self._press is not None and self._press[1]:
            y, x = self.to_level0(event.pos())
            c.drag(y, x)
            return True
        if t == QtCore.QEvent.MouseButtonRelease and event.button() == Qt.LeftButton and self._press is not None:
            start, claimed = self._press
            self._press = None
            moved = (event.pos() - start).manhattanLength() > CLICK_SLOP_PX
            y, x = self.to_level0(event.pos())
            if claimed:
                c.drag(y, x, finish=True)
                if not moved:
                    c.click(y, x, event.modifiers())
                return True
            if not moved:
                c.click(y, x, event.modifiers())
            return False
        if t == QtCore.QEvent.Wheel:
            return bool(c.wheel(event.angleDelta().y()))
        return False

    # ── drawing ──
    def paintEvent(self, event):  # noqa: N802
        if not (self.items or self.line or self.sequence or self.circle):
            return
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        tr = self.canvas_transform()
        scale = abs(tr.m11())
        area = QtCore.QRectF(self.rect())
        fm = QtGui.QFontMetricsF(p.font())
        for it in self.items:
            poly = QtGui.QPolygonF([tr.map(QtCore.QPointF(x, y)) for x, y in it["polygon"]])
            br = poly.boundingRect()
            if not br.intersects(area):
                continue
            pen = QtGui.QPen(QtGui.QColor(it["color"]))
            pen.setWidthF(3.0 if it["id"] in self.selected else 1.5)
            if it.get("dash"):
                pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawPolygon(poly)
            text = self.order_labels.get(it["id"]) or it.get("label")
            if text and max(br.width(), br.height()) >= LABEL_MIN_PX:
                box = QtCore.QRectF(br.right() - 4, br.top() - fm.height(), fm.width(text) + 8, fm.height() + 2)
                p.setPen(Qt.NoPen)
                p.setBrush(QtGui.QColor(0, 0, 0, 170))
                p.drawRoundedRect(box, 3, 3)
                p.setPen(QtGui.QColor(235, 235, 235))
                p.drawText(box, Qt.AlignCenter, text)
        if self.handles_for is not None:
            p.setPen(QtGui.QPen(QtGui.QColor("white")))
            p.setBrush(QtGui.QColor(40, 40, 40))
            for hy, hx in handle_points(self.handles_for):
                q = tr.map(QtCore.QPointF(hx, hy))
                p.drawRect(QtCore.QRectF(q.x() - 5, q.y() - 5, 10, 10))
        if self.line is not None:
            (y0, x0), (y1, x1), hw = self.line
            a, b = tr.map(QtCore.QPointF(x0, y0)), tr.map(QtCore.QPointF(x1, y1))
            dx, dy = b.x() - a.x(), b.y() - a.y()
            n = (dx * dx + dy * dy) ** 0.5 or 1.0
            w = (hw or 0) * scale
            ox, oy = -dy / n * w, dx / n * w
            band = QtGui.QPolygonF([QtCore.QPointF(a.x() + ox, a.y() + oy), QtCore.QPointF(b.x() + ox, b.y() + oy),
                                    QtCore.QPointF(b.x() - ox, b.y() - oy), QtCore.QPointF(a.x() - ox, a.y() - oy)])
            p.setPen(Qt.NoPen)
            p.setBrush(QtGui.QColor(80, 160, 255, 60))
            p.drawPolygon(band)
            p.setPen(QtGui.QPen(QtGui.QColor(80, 160, 255), 2))
            p.drawLine(a, b)
        if len(self.sequence) > 1:
            p.setPen(QtGui.QPen(QtGui.QColor(255, 210, 60), 2))
            p.drawPolyline(QtGui.QPolygonF([tr.map(QtCore.QPointF(x, y)) for y, x in self.sequence]))
        if self.circle is not None:
            cy, cx, r = self.circle
            c = tr.map(QtCore.QPointF(cx, cy))
            pen = QtGui.QPen(QtGui.QColor("#4fc3f7"), 1.5)
            pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(c, r * scale, r * scale)
        p.end()
