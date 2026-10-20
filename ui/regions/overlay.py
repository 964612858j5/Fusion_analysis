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

Drawing (§10.5): the outlines and labels are painted into a cached
picture, redrawn only when they or the view change; what follows the
mouse (the polygon being clicked, a marquee, the ellipse, the transform
box) is painted over it on every move.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt

from ...core.regions import transform as T

LABEL_MIN_PX = 14            # a shape smaller than this on screen gets no label
CLICK_SLOP_PX = 4


class RegionsOverlay(QtWidgets.QWidget):
    def __init__(self, graphics, view_box, controller, label_min_px=LABEL_MIN_PX):
        viewport = graphics.viewport()
        super().__init__(viewport)
        self.setObjectName("regions_overlay")
        self.graphics, self.vb, self.controller = graphics, view_box, controller
        self.label_min_px = label_min_px          # a shape smaller than this on screen gets no label
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.items: List[Dict] = []
        self.selected: set = set()
        self.handles_for: Optional[Sequence] = None
        self.vertices_for: Optional[Sequence] = None
        self.draft: List = []
        self.order_labels: Dict = {}
        self.line = None
        self.sequence: List = []
        self.circle = None
        self._press = None            # (pos, claimed, space-pan)
        self._static = None           # (key, QPixmap): the outlines and labels as last painted
        self._items_ver = 0
        self._cursors = {}
        self._space = False
        self.marquee = None
        self.live = None              # the shape under a pending transform, drawn over the cache
        viewport.setMouseTracking(True)       # hover previews (polygons)
        viewport.installEventFilter(self)
        graphics.installEventFilter(self)     # the Space key reaches the view, not its viewport
        view_box.sigRangeChanged.connect(self._range_changed)
        self.setGeometry(viewport.rect())
        self.show()
        self.raise_()

    def viewport(self):
        return self.parentWidget()

    def detach(self):
        try:
            self.viewport().removeEventFilter(self)
            self.graphics.removeEventFilter(self)
            self.vb.sigRangeChanged.disconnect(self._range_changed)
        except (RuntimeError, TypeError):
            pass
        self.setParent(None)
        self.deleteLater()

    def _range_changed(self, *_a):
        # after a pan / zoom the pointer sits on another image point: the
        # previews (line end, circle radius, polygon rubber band) follow it
        c = self.controller
        if c is not None and self._press is None:
            pos = self.viewport().mapFromGlobal(QtGui.QCursor.pos())
            if self.viewport().rect().contains(pos):
                y, x = self.to_level0(pos)
                c.hover(y, x)
        self.update()

    # ── content (the page's view interface) ──
    def set_items(self, items, selected=(), handles_for=None, order_labels=None, vertices_for=None):
        self.items = list(items)
        self.selected = set(selected)
        self.handles_for = handles_for
        self.vertices_for = vertices_for
        self.order_labels = dict(order_labels or {})
        self._items_ver += 1
        self.update()

    def set_live(self, item, box):
        """The shape being transformed and its box ((x, y) quad), or None."""
        self.live = item
        self.handles_for = box
        self.update()

    def set_marquee(self, marquee):
        """("rect" | "ellipse", (y0, x0), (y1, x1)) being dragged, or None."""
        self.marquee = marquee
        self.update()

    def set_draft_polygon(self, points):
        """The polygon being drawn, [(y, x)]."""
        self.draft = list(points)
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
        if watched is not self.viewport():               # the view itself: only the Space key
            if t in (QtCore.QEvent.KeyPress, QtCore.QEvent.KeyRelease) and event.key() == Qt.Key_Space \
                    and not event.isAutoRepeat():
                self._space = t == QtCore.QEvent.KeyPress
            elif t == QtCore.QEvent.FocusOut:
                self._space = False                      # its release may land elsewhere
            return False
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
        if t == QtCore.QEvent.MouseButtonDblClick and event.button() == Qt.LeftButton \
                and not self._space and self._press is None:
            # Qt sends this in place of the second press: a polygon closes
            # (its release then does nothing), otherwise it is a press
            y, x = self.to_level0(event.pos())
            if c.double_click(y, x):
                self._press = (QtCore.QPoint(event.pos()), True, False)
                return True
        if t in (QtCore.QEvent.MouseButtonPress, QtCore.QEvent.MouseButtonDblClick) \
                and event.button() == Qt.LeftButton:
            self.graphics.setFocus(Qt.MouseFocusReason)     # the viewer's keys (Ctrl+Z, Delete, Space)
            if self._space:
                self._press = (QtCore.QPoint(event.pos()), False, True)
                return False                                 # a space drag pans, whatever the tool
            y, x = self.to_level0(event.pos())
            ctrl = bool(event.modifiers() & Qt.ControlModifier)
            claimed = bool(c.press(y, x, ctrl))
            self._press = (QtCore.QPoint(event.pos()), claimed, False)
            return claimed
        if t == QtCore.QEvent.MouseMove and self._press is not None and self._press[1]:
            y, x = self.to_level0(event.pos())
            c.drag(y, x, mods=int(event.modifiers()))
            return True
        if t == QtCore.QEvent.MouseMove and self._press is None:
            y, x = self.to_level0(event.pos())
            c.hover(y, x)
            self._set_cursor(None if self._space else getattr(c, "cursor_at", lambda *_: None)(y, x))
            return False
        if t == QtCore.QEvent.MouseButtonRelease and event.button() == Qt.LeftButton and self._press is not None:
            start, claimed, panned = self._press
            self._press = None
            moved = (event.pos() - start).manhattanLength() > CLICK_SLOP_PX
            y, x = self.to_level0(event.pos())
            ctrl = bool(event.modifiers() & Qt.ControlModifier)
            if claimed:
                c.drag(y, x, finish=True, mods=int(event.modifiers()))
                if not moved:
                    c.click(y, x, ctrl)
                c.released()
                return True
            if not moved and not panned:
                c.click(y, x, ctrl)
            c.released()
            return False
        if t == QtCore.QEvent.Wheel:
            dragging = self._press is not None and self._press[1]
            return bool(c.wheel(event.angleDelta().y(), dragging))   # otherwise the view zooms
        return False

    # ── the pointer ──
    def _set_cursor(self, kind):
        vp = self.viewport()
        if kind is None:
            vp.unsetCursor()
            return
        shape = {"move": Qt.SizeAllCursor, "hor": Qt.SizeHorCursor, "ver": Qt.SizeVerCursor,
                 "fdiag": Qt.SizeFDiagCursor, "bdiag": Qt.SizeBDiagCursor}.get(kind)
        vp.setCursor(QtGui.QCursor(shape) if shape is not None else self._rotate_cursor())

    def _rotate_cursor(self):
        """A curved double arrow (Qt has no rotate cursor)."""
        if "rotate" not in self._cursors:
            pm = QtGui.QPixmap(24, 24)
            pm.fill(Qt.transparent)
            p = QtGui.QPainter(pm)
            p.setRenderHint(QtGui.QPainter.Antialiasing, True)
            for colour, width in ((QtGui.QColor("white"), 4.0), (QtGui.QColor("black"), 1.6)):
                p.setPen(QtGui.QPen(colour, width, Qt.SolidLine, Qt.RoundCap))
                p.drawArc(QtCore.QRectF(4, 4, 16, 16), 30 * 16, 210 * 16)
                for tip, a, b in (((18.9, 7.9), (14.5, 7.5), (19.5, 3.5)), ((4.6, 16.0), (4.0, 11.5), (8.5, 15.5))):
                    p.drawLine(QtCore.QPointF(*tip), QtCore.QPointF(*a))
                    p.drawLine(QtCore.QPointF(*tip), QtCore.QPointF(*b))
            p.end()
            self._cursors["rotate"] = QtGui.QCursor(pm, 12, 12)
        return self._cursors["rotate"]

    # ── drawing ──
    def paintEvent(self, event):  # noqa: N802
        if not (self.items or self.line or self.sequence or self.circle or self.draft or self.marquee
                or self.handles_for is not None or self.live is not None):
            return
        tr = self.canvas_transform()
        key = (self._items_ver, tr.m11(), tr.m22(), tr.dx(), tr.dy(), self.width(), self.height(),
               self.devicePixelRatioF())
        if self._static is None or self._static[0] != key:
            dpr = self.devicePixelRatioF()
            pm = QtGui.QPixmap(int(self.width() * dpr), int(self.height() * dpr))
            pm.setDevicePixelRatio(dpr)
            pm.fill(Qt.transparent)
            sp = QtGui.QPainter(pm)
            sp.setRenderHint(QtGui.QPainter.Antialiasing, True)
            self._paint_static(sp, tr)
            sp.end()
            self._static = (key, pm)
        p = QtGui.QPainter(self)
        p.drawPixmap(0, 0, self._static[1])
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        self._paint_dynamic(p, tr)
        p.end()

    def _paint_static(self, p, tr):
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
            if it.get("hatch"):                           # an exclusion: hatched, as a mark
                p.setBrush(QtGui.QBrush(QtGui.QColor(it["color"]), Qt.BDiagPattern))
            else:
                p.setBrush(Qt.NoBrush)
            p.drawPolygon(poly)
            self._label(p, it, br, fm)

    def _label(self, p, it, br, fm):
        text = self.order_labels.get(it["id"]) or it.get("label")
        if text and max(br.width(), br.height()) >= self.label_min_px:
            box = QtCore.QRectF(br.right() - 4, br.top() - fm.height(), fm.width(text) + 8, fm.height() + 2)
            p.setPen(Qt.NoPen)
            p.setBrush(QtGui.QColor(0, 0, 0, 170))
            p.drawRoundedRect(box, 3, 3)
            p.setPen(QtGui.QColor(235, 235, 235))
            p.drawText(box, Qt.AlignCenter, text)

    def _paint_dynamic(self, p, tr):
        scale = abs(tr.m11())
        if self.live is not None:
            pen = QtGui.QPen(QtGui.QColor(self.live["color"]), 3.0)
            pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            p.setBrush(QtGui.QBrush(QtGui.QColor(self.live["color"]), Qt.BDiagPattern) if self.live.get("hatch")
                       else Qt.NoBrush)
            poly = QtGui.QPolygonF([tr.map(QtCore.QPointF(x, y)) for x, y in self.live["polygon"]])
            p.drawPolygon(poly)
            self._label(p, self.live, poly.boundingRect(), QtGui.QFontMetricsF(p.font()))
        if self.handles_for is not None:                  # the transform box: outline + 8 handles
            quad = [tr.map(QtCore.QPointF(x, y)) for x, y in self.handles_for]
            pen = QtGui.QPen(QtGui.QColor("white"), 1.0)
            pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawPolygon(QtGui.QPolygonF(quad))
            p.setPen(QtGui.QPen(QtGui.QColor("white")))
            p.setBrush(QtGui.QColor(40, 40, 40))
            for hx, hy in T.handle_points(self.handles_for):
                q = tr.map(QtCore.QPointF(hx, hy))
                p.drawRect(QtCore.QRectF(q.x() - 5, q.y() - 5, 10, 10))
        if self.vertices_for is not None:
            p.setPen(QtGui.QPen(QtGui.QColor("white")))
            p.setBrush(QtGui.QColor(30, 136, 229))
            for vx, vy in self.vertices_for:
                q = tr.map(QtCore.QPointF(vx, vy))
                p.drawEllipse(q, 4, 4)
        if self.draft:
            pts = [tr.map(QtCore.QPointF(x, y)) for y, x in self.draft]
            pen = QtGui.QPen(QtGui.QColor("#4fc3f7"), 1.5)
            pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawPolyline(QtGui.QPolygonF(pts))
            p.setBrush(QtGui.QColor("#4fc3f7"))
            for q in pts:
                p.drawEllipse(q, 3, 3)
        if self.marquee is not None:
            kind, (y0, x0), (y1, x1) = self.marquee
            r = QtCore.QRectF(tr.map(QtCore.QPointF(x0, y0)), tr.map(QtCore.QPointF(x1, y1))).normalized()
            pen = QtGui.QPen(QtGui.QColor(80, 160, 255), 1.5)
            pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            p.setBrush(QtGui.QColor(80, 160, 255, 30))
            if kind == "rect":
                p.drawRect(r)
            else:
                p.drawEllipse(r)
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
        if self.circle is not None:                       # the ellipse in the dragged box
            y0, x0, y1, x1 = self.circle
            r = QtCore.QRectF(tr.map(QtCore.QPointF(x0, y0)), tr.map(QtCore.QPointF(x1, y1))).normalized()
            pen = QtGui.QPen(QtGui.QColor("#4fc3f7"), 1.5)
            pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(r)
