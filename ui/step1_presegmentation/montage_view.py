"""The montage: every patch of a run on one canvas, one camera (block D).

After step5_v8's montage viewer in idea only (plan 7.6): one canvas, one
camera, a layout table, separators and labels as layers of their own, hit
testing by the table. Unlike it, patches differ in size (packed in rows),
the base images are composed live (`montage_supply`), and the P names are
their own layer.

Canvas units are LEVEL-0 pixels: a patch of 512 x 512 level-0 pixels takes
512 x 512 canvas units whatever pyramid level its image comes from, so the
outlines (level-0 masks) and the images line up by construction.
"""

import math

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtGui, QtWidgets

from ...viewer import request_planning as planning

GAP_FRACTION = 0.06            # the gap between patches, of the median side
MIN_GAP = 16


def pack_rows(sizes, gap=None, target_aspect=1.6):
    """Place boxes of `sizes` [(h, w)] in rows, in order.

    Rows wrap at a width that makes the whole roughly `target_aspect` wide
    for tall; each row is as tall as its tallest box. Returns
    [(y, x, h, w)] in canvas units, and the canvas (height, width).
    """
    if not sizes:
        return [], (0, 0)
    sides = sorted(max(h, w) for h, w in sizes)
    if gap is None:
        gap = max(MIN_GAP, int(sides[len(sides) // 2] * GAP_FRACTION))
    area = sum((h + gap) * (w + gap) for h, w in sizes)
    row_width = max(max(w for _, w in sizes), math.sqrt(area * target_aspect))
    out, x, y, row_h, width = [], 0, 0, 0, 0
    for h, w in sizes:
        if x > 0 and x + w > row_width:
            y += row_h + gap
            x, row_h = 0, 0
        out.append((y, x, h, w))
        x += w + gap
        row_h = max(row_h, h)
        width = max(width, x - gap)
    return out, (y + row_h, width)


class MontageLayout:
    """The layout table: which patch is where on the canvas."""

    def __init__(self, patches):
        self.patches = list(patches)                  # [{id, name, bbox}]
        sizes = [(p["bbox"][1] - p["bbox"][0], p["bbox"][3] - p["bbox"][2])
                 for p in self.patches]
        self.rects, self.size = pack_rows(sizes)

    def rect(self, i):
        return self.rects[i]

    def hit(self, cy, cx):
        """The index of the patch under canvas point (cy, cx), or None --
        a gap between patches is nobody's."""
        for i, (y, x, h, w) in enumerate(self.rects):
            if y <= cy < y + h and x <= cx < x + w:
                return i
        return None


class MontageOverlay(QtWidgets.QWidget):
    """Frames, names and the selection, drawn ABOVE the picture.

    The GPU layer is a GL widget laid over the canvas's viewport (as in the
    viewer), so anything drawn in the canvas's scene would be under it. This
    widget sits on top of it, lets every mouse event through to the canvas,
    and maps canvas coordinates to its own pixels through the same ViewBox
    -- the same numbers the GPU layer is given. Block D step 2 draws the
    outlines here too.
    """

    def __init__(self, view):
        super().__init__(view.gv.viewport())
        self.view = view
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(QtCore.Qt.WA_NoSystemBackground, True)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        view.gv.viewport().installEventFilter(self)
        view.vb.sigRangeChanged.connect(lambda *a: self.update())
        self.setGeometry(view.gv.viewport().rect())
        self.raise_()

    def eventFilter(self, watched, event):  # noqa: N802
        if event.type() in (QtCore.QEvent.Resize, QtCore.QEvent.Move, QtCore.QEvent.Show):
            self.setGeometry(watched.rect())
            # after the GL layer's own raise for the same event
            QtCore.QTimer.singleShot(0, self.raise_)
        return False

    def to_widget(self, x, y):
        scene = self.view.vb.mapViewToScene(QtCore.QPointF(x, y))
        return QtCore.QPointF(self.view.gv.mapFromScene(scene))

    def widget_rect(self, rect):
        y, x, h, w = rect
        a, b = self.to_widget(x, y), self.to_widget(x + w, y + h)
        return QtCore.QRectF(a, b).normalized()

    def paintEvent(self, event):  # noqa: N802
        view = self.view
        if not view.layout_table.patches:
            return
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, False)
        font = p.font()
        metrics = QtGui.QFontMetrics(font)
        for i, patch in enumerate(view.layout_table.patches):
            r = self.widget_rect(view.layout_table.rect(i))
            selected = i == view.selected
            p.setPen(MontageView._pen(MontageView.SELECTED if selected else MontageView.SEPARATOR,
                                      2 if selected else 1))
            p.setBrush(QtCore.Qt.NoBrush)
            p.drawRect(r)
            name = str(patch.get("name") or "")
            if name:
                box = QtCore.QRectF(r.left() + 1, r.top() + 1,
                                    metrics.horizontalAdvance(name) + 8, metrics.height() + 4)
                p.fillRect(box, QtGui.QColor(0, 0, 0, 150))
                p.setPen(QtGui.QColor(MontageView.LABEL_COLOR))
                p.drawText(box, QtCore.Qt.AlignCenter, name)
        p.end()


class MontageView(QtWidgets.QWidget):
    """The canvas. Feed it patches with `set_patches`, and base images with
    `set_image`; it tells the page which level it wants (`level_wanted`)
    and which patch was clicked (`patch_clicked`)."""

    level_wanted = QtCore.pyqtSignal(int, int)       # level, stride
    patch_clicked = QtCore.pyqtSignal(object)        # patch id, or None
    mode_requested = QtCore.pyqtSignal(str)          # "overlay" | "fusion"

    LABEL_COLOR = "#ffffff"
    SEPARATOR = QtGui.QColor(110, 110, 110)
    SELECTED = QtGui.QColor(255, 209, 102)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        # Overlay / Fusion here too (user ruling, 2026-09-24): the same one
        # command as the Viewer's two buttons, kept in step with them.
        bar = QtWidgets.QHBoxLayout()
        bar.setContentsMargins(4, 2, 4, 0)
        self.btn_overlay = QtWidgets.QPushButton("Overlay", self)
        self.btn_fusion = QtWidgets.QPushButton("Fusion", self)
        group = QtWidgets.QButtonGroup(self)
        for b, mode in ((self.btn_overlay, "overlay"), (self.btn_fusion, "fusion")):
            b.setCheckable(True)
            group.addButton(b)
            bar.addWidget(b)
            b.clicked.connect(lambda _c, m=mode: self.mode_requested.emit(m))
        self.btn_overlay.setChecked(True)
        bar.addStretch(1)
        lay.addLayout(bar)
        self.gv = pg.GraphicsView(self)
        self.gv.setBackground("#202020")
        self.vb = pg.ViewBox(lockAspect=True, invertY=True, enableMenu=False)
        self.vb.setDefaultPadding(0.02)
        # The camera is the user's and `fit` / `focus`'s alone: an image of a
        # new size arriving must not move it (it did -- each moved range
        # asked for another full-resolution frame in the middle of a drag).
        self.vb.disableAutoRange()
        self.gv.setCentralItem(self.vb)
        lay.addWidget(self.gv)
        self.empty = QtWidgets.QLabel("No patches to show.", self)
        self.empty.setAlignment(QtCore.Qt.AlignCenter)
        self.empty.setStyleSheet("color:#888;")
        lay.addWidget(self.empty)
        self.layout_table = MontageLayout([])
        self._images = []
        self._images_shown = True               # False while the GPU layer draws the picture
        self._downsamples = [1.0]
        self.level = 0
        self.stride = 1
        self.selected = None
        self.focused = None                            # a patch shown alone, or None
        self._level_timer = QtCore.QTimer(self)
        self._level_timer.setSingleShot(True)
        self._level_timer.setInterval(120)
        self._level_timer.timeout.connect(self._check_level)
        self.vb.sigRangeChanged.connect(lambda *a: self._level_timer.start())
        self.vb.scene().sigMouseClicked.connect(self._on_click)
        self.fit_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence("F"), self)
        self.fit_shortcut.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        self.fit_shortcut.activated.connect(self.fit)
        self.overlay = MontageOverlay(self)
        self._show_empty(True)

    # ── contents ─────────────────────────────────────────────────────
    def set_downsamples(self, downsamples):
        self._downsamples = [float(d) for d in (downsamples or [1.0])]

    def set_patches(self, patches):
        """A new set of patches: a new layout, fitted to the view."""
        for item in self._images:
            self.vb.removeItem(item)
        self._images = []
        self.layout_table = MontageLayout(patches)
        self.selected = None
        self.focused = None
        self._on_screen = None
        for i, p in enumerate(self.layout_table.patches):
            y, x, h, w = self.layout_table.rect(i)
            img = pg.ImageItem(axisOrder="row-major")
            img.setZValue(0)
            img.setVisible(self._images_shown)
            self.vb.addItem(img, ignoreBounds=True)
            self._images.append(img)
        self._show_empty(not self.layout_table.patches)
        self.overlay.update()
        self.fit()

    def set_image(self, bbox, rgba):
        """The base image of the patch whose level-0 box is `bbox`."""
        i = self._index_of(bbox)
        if i is None:
            return False
        y, x, h, w = self.layout_table.rect(i)
        img = self._images[i]
        img.setImage(np.asarray(rgba), autoLevels=False, levels=(0, 255))
        img.setRect(QtCore.QRectF(x, y, w, h))
        return True

    def bboxes(self):
        return [tuple(int(v) for v in p["bbox"]) for p in self.layout_table.patches]

    def patch_ids(self):
        return [p.get("id") for p in self.layout_table.patches]

    def canvas_rect(self, pid):
        for i, p in enumerate(self.layout_table.patches):
            if p.get("id") == pid:
                return self.layout_table.rect(i)
        return None

    def set_mode(self, mode):
        """Follow the page's mode (the Viewer's buttons may have moved it)."""
        (self.btn_fusion if mode == "fusion" else self.btn_overlay).setChecked(True)

    # ── camera ───────────────────────────────────────────────────────
    def fit(self):
        """Every patch in view (F, or a double-click on a patch shown alone
        or on the background)."""
        self.focused = None
        h, w = self.layout_table.size
        if h and w:
            self.vb.setRange(QtCore.QRectF(0, 0, w, h), padding=0.02)
        self._level_timer.start()

    def focus(self, i):
        """One patch filling the view (a double-click on it)."""
        y, x, h, w = self.layout_table.rect(i)
        self.focused = i
        self.select_index(i)
        self.vb.setRange(QtCore.QRectF(x, y, w, h), padding=0.01)
        self._level_timer.start()

    def double_click_at(self, cy, cx):
        """On a patch: show it alone; on the one shown alone, or between
        patches: show them all again."""
        i = self.layout_table.hit(cy, cx)
        if i is None or i == self.focused:
            self.fit()
        else:
            self.focus(i)
        return self.focused

    def screen_px_per_canvas_px(self):
        (x0, x1), _ = self.vb.viewRange()
        width = self.vb.width() or self.gv.width() or 1
        return float(width) / max(1e-9, (x1 - x0))

    def wanted_level(self):
        return planning.pick_display_level(self._downsamples, self.screen_px_per_canvas_px())

    def wanted(self):
        """(level, stride): the pyramid level to read, and every how-many-th
        of its pixels the screen actually shows -- composed at that, never
        finer than the screen."""
        scale = self.screen_px_per_canvas_px()
        level = planning.pick_display_level(self._downsamples, scale)
        ideal = 1.0 / scale if scale > 0 else 1.0
        stride = max(1, int(ideal / self._downsamples[level]))
        return level, stride

    def bboxes_by_need(self):
        """The patches on screen, nearest the centre first. The others are
        composed when they come on screen: zoomed in on one patch, composing
        all the rest at that resolution would be work nobody sees."""
        (x0, x1), (y0, y1) = self.vb.viewRange()
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        seen, rest = [], []
        for i, p in enumerate(self.layout_table.patches):
            y, x, h, w = self.layout_table.rect(i)
            box = tuple(int(v) for v in p["bbox"])
            if x < x1 and x + w > x0 and y < y1 and y + h > y0:
                seen.append(((x + w / 2.0 - cx) ** 2 + (y + h / 2.0 - cy) ** 2, i, box))
            else:
                rest.append(box)
        return [b for _, _, b in sorted(seen)]

    def _check_level(self):
        """After the camera settles: ask again when the resolution or the set
        of patches on screen changed."""
        level, stride = self.wanted()
        on_screen = frozenset(self.bboxes_by_need())
        if (level, stride, on_screen) != (self.level, self.stride,
                                          getattr(self, "_on_screen", None)):
            self.level, self.stride, self._on_screen = level, stride, on_screen
            self.level_wanted.emit(level, stride)


    # ── clicks ───────────────────────────────────────────────────────
    def _on_click(self, event):
        pos = self.vb.mapSceneToView(event.scenePos())
        if event.double():
            self.double_click_at(pos.y(), pos.x())
            return
        self.click_at(pos.y(), pos.x())

    def click_at(self, cy, cx):
        """Select the patch at a canvas point; the view does not move."""
        i = self.layout_table.hit(cy, cx)
        self.select_index(i)
        pid = None if i is None else self.layout_table.patches[i].get("id")
        self.patch_clicked.emit(pid)
        return pid

    def select_index(self, i):
        self.selected = i
        self.overlay.update()

    def show_images(self, shown):
        """The CPU picture on (no GPU layer) or off (the GPU layer draws it)."""
        self._images_shown = bool(shown)
        for img in self._images:
            img.setVisible(self._images_shown)

    def keep_overlay_on_top(self):
        self.overlay.raise_()

    # ── helpers ──────────────────────────────────────────────────────
    @staticmethod
    def _pen(color, width):
        pen = QtGui.QPen(color)
        pen.setCosmetic(True)                     # screen pixels, whatever the zoom
        pen.setWidthF(width)
        return pen

    def _index_of(self, bbox):
        bbox = tuple(int(v) for v in bbox)
        for i, p in enumerate(self.layout_table.patches):
            if tuple(int(v) for v in p["bbox"]) == bbox:
                return i
        return None

    def _show_empty(self, empty):
        self.empty.setVisible(empty)
        self.gv.setVisible(not empty)
