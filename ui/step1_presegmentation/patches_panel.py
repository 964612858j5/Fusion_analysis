"""The Patches strip at the top of Step1's `Method & Parameters` tab (block A1).

Which of the existing patches take part in a pre-segmentation run: one
tickable tile per patch, all ticked by default. The panel does not own the
patches -- Step0's single model does -- so a tile's `×` and a double-click
rename only ASK (`delete_requested`, `rename_requested`); the host sends the
request through the same path a navigator edit takes, and the result comes
back as a new `set_patches` call.

What is ticked is remembered by patch id (ids are stable, plan block P), so
adding, deleting or moving other patches never changes it; a new patch comes
in ticked. The ticks are not persisted and feed nothing yet (block C).

User rulings 2026-09-24: no hover text; a ticked tile has a `×` in its top
right corner that deletes the patch; double-clicking a tile renames it.
"""

import time

from PyQt5 import QtWidgets
from PyQt5.QtCore import QPoint, QRect, QSize, Qt, pyqtSignal

from ...config import PATCH_COLORS
from ..step0.roi_context_model import patch_id, patch_name

TILE_H = 24
TILE_MIN_W = 44
TILE_SPACING = 4
MAX_ROWS = 3
EMPTY_HINT = "No patches yet — draw them in Step0 or the Tissue Navigator"


class FlowLayout(QtWidgets.QLayout):
    """Left-to-right rows that wrap at the available width.

    Its minimum width is ONE item's, never the row's: the strip sits in
    Step1's channel column, whose width floor must stay what a channel row
    needs (`MainWindow._hold_step1_channel_floor`).
    """

    def __init__(self, parent=None, spacing=TILE_SPACING):
        super().__init__(parent)
        self._items = []
        self.setSpacing(spacing)
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):  # noqa: N802 -- Qt API
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientations(0)

    def hasHeightForWidth(self):  # noqa: N802
        return True

    def heightForWidth(self, width):  # noqa: N802
        return self._arrange(QRect(0, 0, width, 0), move=False)

    def setGeometry(self, rect):  # noqa: N802
        super().setGeometry(rect)
        self._arrange(rect, move=True)

    def sizeHint(self):  # noqa: N802
        return self.minimumSize()

    def minimumSize(self):  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _arrange(self, rect, move):
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, row_h = area.x(), area.y(), 0
        gap = self.spacing()
        for item in self._items:
            # The widget's own hint: a QWidgetItem reports an empty size for
            # a tile that was just created and is not shown yet, which would
            # measure a freshly rebuilt strip as zero rows high.
            widget = item.widget()
            hint = widget.sizeHint() if widget is not None else item.sizeHint()
            if x + hint.width() > area.right() + 1 and row_h > 0:
                x, y, row_h = area.x(), y + row_h + gap, 0
            if move:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + gap
            row_h = max(row_h, hint.height())
        return y + row_h - rect.y() + m.bottom()


class PatchTile(QtWidgets.QFrame):
    """One patch: its name, a tick (the whole tile), and a `×` while ticked."""

    toggled = pyqtSignal(int, bool)           # patch id, ticked
    delete_clicked = pyqtSignal(int)          # patch id
    rename_asked = pyqtSignal(int)            # patch id

    def __init__(self, pid, name, color, checked=True, parent=None):
        super().__init__(parent)
        self.pid = int(pid)
        self._color = color
        self._checked = bool(checked)
        self._pre_click = self._checked
        self._last_press = -1.0
        self.setObjectName("patchTile")
        self.setFixedHeight(TILE_H)
        self.setCursor(Qt.PointingHandCursor)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(6, 0, 16, 0)
        self._label = QtWidgets.QLabel(name, self)
        self._label.setAttribute(Qt.WA_TransparentForMouseEvents)
        lay.addWidget(self._label)
        self._close = QtWidgets.QToolButton(self)
        self._close.setText("×")
        self._close.setFixedSize(12, 12)
        self._close.setAutoRaise(True)
        self._close.clicked.connect(lambda: self.delete_clicked.emit(self.pid))
        self._restyle()

    # ── state ────────────────────────────────────────────────────────
    def name(self):
        return self._label.text()

    def is_checked(self):
        return self._checked

    def set_checked(self, on, emit=True):
        on = bool(on)
        if on == self._checked:
            return
        self._checked = on
        self._restyle()
        if emit:
            self.toggled.emit(self.pid, on)

    def close_button(self):
        return self._close

    # ── geometry / look ──────────────────────────────────────────────
    def sizeHint(self):  # noqa: N802
        w = self._label.sizeHint().width() + 6 + 16
        return QSize(max(TILE_MIN_W, w), TILE_H)

    def minimumSizeHint(self):  # noqa: N802
        return QSize(TILE_MIN_W, TILE_H)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._close.move(self.width() - self._close.width() - 2, 2)

    def _restyle(self):
        c = self._color
        if self._checked:
            self.setStyleSheet(
                f"QFrame#patchTile{{background:{c};border:1px solid {c};border-radius:3px;}}"
                f"QLabel{{color:#111;font-size:10px;font-weight:bold;}}"
                f"QToolButton{{color:#111;border:none;font-size:11px;font-weight:bold;padding:0;}}")
        else:
            self.setStyleSheet(
                f"QFrame#patchTile{{background:#1a1a1a;border:1px solid {c};border-radius:3px;}}"
                f"QLabel{{color:{c};font-size:10px;font-weight:bold;}}")
        # The `×` belongs to ticked tiles only (user ruling).
        self._close.setVisible(self._checked)

    # ── gestures ─────────────────────────────────────────────────────
    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton:
            now = time.monotonic()
            interval = QtWidgets.QApplication.doubleClickInterval() / 1000.0
            if now - self._last_press > interval:
                # The first press of what may become a double-click: remember
                # the tick as it was, whatever presses follow.
                self._pre_click = self._checked
            self._last_press = now
            self.set_checked(not self._checked)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton:
            # A rename is not a tick: whatever the presses of this
            # double-click did to it, put the tick back as it was.
            self.set_checked(self._pre_click)
            self.rename_asked.emit(self.pid)
        event.accept()


class PatchesPanel(QtWidgets.QWidget):
    """The Patches strip: tiles, `Select all` / `Select none`, `k/n selected`."""

    selection_changed = pyqtSignal(list)      # ticked patch ids, in patch order
    delete_requested = pyqtSignal(int)        # patch id
    rename_requested = pyqtSignal(int, str)   # patch id, new name

    def __init__(self, parent=None):
        super().__init__(parent)
        # As tall as its content and no taller: the tab's spare height
        # belongs to the method controls under it.
        self.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Maximum)
        self._tiles = []                      # in patch order
        self._unticked = set()                # ids the user has unticked

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 4)
        outer.setSpacing(4)

        self._scroll = QtWidgets.QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.viewport().setAutoFillBackground(False)
        self._tile_host = QtWidgets.QWidget()
        self._flow = FlowLayout(self._tile_host)
        self._scroll.setWidget(self._tile_host)
        self._scroll.setMinimumWidth(TILE_MIN_W)
        outer.addWidget(self._scroll)

        self._empty = QtWidgets.QLabel(EMPTY_HINT, self)
        self._empty.setWordWrap(True)
        self._empty.setStyleSheet("color:#888;font-size:11px;")
        outer.addWidget(self._empty)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(6)
        self._btn_all = QtWidgets.QPushButton("Select all", self)
        self._btn_none = QtWidgets.QPushButton("Select none", self)
        for b in (self._btn_all, self._btn_none):
            b.setStyleSheet("font-size:11px;")
            row.addWidget(b)
        self._count = QtWidgets.QLabel("", self)
        self._count.setStyleSheet("color:#bbb;font-size:11px;")
        self._count.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        # The count takes the rest of the row, right-aligned, and gives way
        # rather than widening the channel column: its width does not count
        # towards the strip's minimum.
        self._count.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                  QtWidgets.QSizePolicy.Preferred)
        row.addWidget(self._count, 1)
        outer.addLayout(row)

        self._btn_all.clicked.connect(lambda: self._set_all(True))
        self._btn_none.clicked.connect(lambda: self._set_all(False))
        self._refresh_chrome()

    # ── model in ─────────────────────────────────────────────────────
    def set_patches(self, patches):
        """Show `patches` (Step1's patch list). Ticks are kept by patch id;
        a patch not seen before comes in ticked."""
        while self._flow.count():
            item = self._flow.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._tiles = []
        live = set()
        for i, p in enumerate(patches or []):
            pid = patch_id(p)
            if pid is None:
                pid = i + 1
            live.add(pid)
            color = PATCH_COLORS[i % len(PATCH_COLORS)]
            tile = PatchTile(pid, patch_name(p, i), color,
                             checked=pid not in self._unticked, parent=self._tile_host)
            tile.toggled.connect(self._on_tile_toggled)
            tile.delete_clicked.connect(self.delete_requested.emit)
            tile.rename_asked.connect(self._ask_rename)
            self._flow.addWidget(tile)
            self._tiles.append(tile)
        # Forget unticks of patches that are gone, so an id can never carry a
        # stale answer (ids are never reused, but the set stays honest).
        self._unticked &= live
        self._tile_host.updateGeometry()
        self._refresh_chrome()
        self._fit_height()
        self.selection_changed.emit(self.selected_ids())

    # ── state out ────────────────────────────────────────────────────
    def selected_ids(self):
        return [t.pid for t in self._tiles if t.is_checked()]

    def tiles(self):
        return list(self._tiles)

    # ── internals ────────────────────────────────────────────────────
    def _on_tile_toggled(self, pid, on):
        if on:
            self._unticked.discard(pid)
        else:
            self._unticked.add(pid)
        self._refresh_chrome()
        self.selection_changed.emit(self.selected_ids())

    def _set_all(self, on):
        for t in self._tiles:
            t.set_checked(on, emit=False)
        self._unticked = set() if on else {t.pid for t in self._tiles}
        self._refresh_chrome()
        self.selection_changed.emit(self.selected_ids())

    def _ask_rename(self, pid):
        tile = next((t for t in self._tiles if t.pid == pid), None)
        if tile is None:
            return
        name, ok = QtWidgets.QInputDialog.getText(
            self, "Rename patch", "Patch name:", text=tile.name())
        if not ok:
            return
        new = str(name).strip()
        if not new or new == tile.name():
            return
        if any(t.name() == new for t in self._tiles if t is not tile):
            QtWidgets.QMessageBox.warning(
                self, "Duplicate name",
                f'Patch name "{new}" already exists.\nPlease choose a different name.')
            return
        self.rename_requested.emit(pid, new)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._fit_height()

    def _fit_height(self):
        """As tall as the rows need, up to MAX_ROWS; beyond that it scrolls."""
        cap = MAX_ROWS * TILE_H + (MAX_ROWS - 1) * TILE_SPACING
        width = max(TILE_MIN_W, self._scroll.viewport().width())
        need = self._flow.heightForWidth(width) if self._tiles else 0
        self._scroll.setFixedHeight(min(max(need, TILE_H), cap) + 2)

    def _refresh_chrome(self):
        n = len(self._tiles)
        has = n > 0
        self._scroll.setVisible(has)
        self._empty.setVisible(not has)
        self._btn_all.setEnabled(has)
        self._btn_none.setEnabled(has)
        self._count.setText(f"{len(self.selected_ids())}/{n} selected" if has else "")
