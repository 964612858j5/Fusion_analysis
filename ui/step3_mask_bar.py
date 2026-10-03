"""Step3's mask row -- block 4c (user rulings, 2026-09-26/27).

Top of Step3's Viewer tab, left to right: the run drop-down, `Cell mask ▾`,
`Nucleus mask ▾`, the hint line; the page puts the Overlay / Fusion pair at
the right end. Each mask button opens a panel: Show, colour (preset swatches
and `Custom…`; the colour is the OUTLINE's -- Fill gives every cell its own
fixed colour), Opacity, Width (1..4 logical pixels, outline only) and
Outline / Fill.

Controls only: this module reads no file and decides nothing. The window
fills the run list and the hint, and turns `run_chosen` / `style_changed`
into the mount's calls.
"""

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt, pyqtSignal

from .step1_button_styles import MODE_BUTTON_QSS
from .step3_label_binding import CELL, DEFAULT_STYLES, NUCLEUS

LABELS = {CELL: "Cell mask", NUCLEUS: "Nucleus mask"}
NO_RUNS_TEXT = "No segmentation results for this ROI"
# Block RM (§5): a row's tag -- its upstream (the fuse run it was made
# from) -- is drawn right-aligned at its end; it may cover the right end of
# the run's name.
TAG_ROLE = Qt.UserRole + 1


#: A row that can be deleted shows a × at its start (block RM §9: Step3's
#: runs of this project).
CLOSE_ROLE = Qt.UserRole + 2
CLOSE_WIDTH = 22


def fit_popup(combo, max_closed=560):
    """Make a combo's list as wide as its longest row -- text, right-aligned
    tag and × (acceptance 2026-10-03: rows could not be told apart) -- and
    the closed combo as wide as that, up to `max_closed` pixels."""
    fm = combo.fontMetrics()
    widest = 0
    for row in range(combo.count()):
        tag = combo.itemData(row, TAG_ROLE) or ""
        closable = bool(combo.itemData(row, CLOSE_ROLE))
        width = (fm.horizontalAdvance(combo.itemText(row))
                 + (fm.horizontalAdvance(tag) + 24 if tag else 0)
                 + (CLOSE_WIDTH if closable else 0) + 40)
        widest = max(widest, width)
    combo.view().setMinimumWidth(widest)
    combo.setMinimumWidth(min(widest, max_closed))


class _TaggedItemDelegate(QtWidgets.QStyledItemDelegate):
    """A row with its tag (TAG_ROLE) drawn right-aligned over its end, and --
    when CLOSE_ROLE is set -- a × at its start; clicking the × emits
    `close_clicked(row)` and does not select the row."""

    close_clicked = pyqtSignal(int)

    @staticmethod
    def close_rect(option_rect):
        return QtCore.QRect(option_rect.left(), option_rect.top(),
                            CLOSE_WIDTH, option_rect.height())

    def paint(self, painter, option, index):
        if index.data(CLOSE_ROLE):
            shifted = QtWidgets.QStyleOptionViewItem(option)
            shifted.rect = option.rect.adjusted(CLOSE_WIDTH, 0, 0, 0)
            super().paint(painter, shifted, index)
            painter.save()
            painter.setPen(QtGui.QColor("#e06c75"))
            painter.drawText(self.close_rect(option.rect), Qt.AlignCenter, "\u00d7")
            painter.restore()
        else:
            super().paint(painter, option, index)
        tag = index.data(TAG_ROLE)
        if not tag:
            return
        painter.save()
        fm = option.fontMetrics
        width = fm.horizontalAdvance(tag) + 12
        rect = QtCore.QRect(option.rect.right() - width, option.rect.top(),
                            width, option.rect.height())
        selected = bool(option.state & QtWidgets.QStyle.State_Selected)
        painter.fillRect(rect, option.palette.highlight() if selected
                         else option.palette.base())
        painter.setPen(QtGui.QColor("#e5c07b"))
        painter.drawText(rect.adjusted(0, 0, -6, 0), Qt.AlignRight | Qt.AlignVCenter, tag)
        painter.restore()

    def editorEvent(self, event, model, option, index):
        if index.data(CLOSE_ROLE) and event.type() in (
                QtCore.QEvent.MouseButtonPress, QtCore.QEvent.MouseButtonRelease,
                QtCore.QEvent.MouseButtonDblClick) \
                and self.close_rect(option.rect).contains(event.pos()):
            if event.type() == QtCore.QEvent.MouseButtonRelease:
                self.close_clicked.emit(index.row())
            return True                     # the × is not a click on the row
        return super().editorEvent(event, model, option, index)
FILL_NOTE = "Fill uses one colour per cell"
#: The preset swatches, RGB 0..1: green, cyan, yellow, magenta, orange, red, white.
PRESET_COLOURS = ((0.0, 1.0, 0.0), (0.0, 0.8, 1.0), (1.0, 1.0, 0.0), (1.0, 0.0, 1.0),
                  (1.0, 0.6, 0.0), (1.0, 0.3, 0.3), (1.0, 1.0, 1.0))
_PANEL_QSS = ("QFrame{background:#1c1c1c;color:#ddd;}"
              "QLabel{color:#bbb;font-size:10px;}"
              "QCheckBox,QRadioButton{color:#ddd;font-size:10px;}")


def _qcolor(rgb):
    return QtGui.QColor.fromRgbF(*[float(v) for v in rgb])


def _swatch_icon(rgb, size=10):
    pix = QtGui.QPixmap(size, size)
    pix.fill(_qcolor(rgb))
    return QtGui.QIcon(pix)


class MaskPanel(QtWidgets.QFrame):
    """One mask's settings. Emits `changed(dict)` with only what changed."""

    changed = pyqtSignal(dict)

    def __init__(self, kind, parent=None):
        super().__init__(parent)
        self.kind = kind
        style = DEFAULT_STYLES[kind]
        self._color = tuple(style.color)
        #: How `Custom…` asks for a colour; returns a QColor (invalid for
        #: cancel). A seam: the product uses the system dialog, a test
        #: answers directly instead of opening a modal box.
        self.pick_custom_colour = lambda initial: QtWidgets.QColorDialog.getColor(
            initial, self, f"{LABELS[kind]} colour")
        self.setStyleSheet(_PANEL_QSS)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        self.show_box = QtWidgets.QCheckBox("Show")
        self.show_box.setChecked(style.visible)
        self.show_box.toggled.connect(lambda on: self.changed.emit({"visible": bool(on)}))
        lay.addWidget(self.show_box)

        colours = QtWidgets.QHBoxLayout()
        colours.setSpacing(3)
        self.swatches = []
        for rgb in PRESET_COLOURS:
            btn = QtWidgets.QToolButton()
            btn.setFixedSize(16, 16)
            btn.setStyleSheet(f"QToolButton{{background:{_qcolor(rgb).name()};"
                              "border:1px solid #555;border-radius:2px;}")
            btn.clicked.connect(lambda _c=False, c=rgb: self.set_colour(c))
            colours.addWidget(btn)
            self.swatches.append((rgb, btn))
        self.custom = QtWidgets.QPushButton("Custom…")
        self.custom.setStyleSheet(MODE_BUTTON_QSS)
        self.custom.clicked.connect(self._custom_colour)
        colours.addWidget(self.custom)
        colours.addStretch()
        lay.addLayout(colours)

        opacity = QtWidgets.QHBoxLayout()
        opacity.addWidget(QtWidgets.QLabel("Opacity"))
        self.opacity = QtWidgets.QSlider(Qt.Horizontal)
        self.opacity.setRange(0, 100)
        self.opacity.setValue(int(round(style.alpha * 100)))
        self.opacity_value = QtWidgets.QLabel(f"{self.opacity.value()} %")
        self.opacity.valueChanged.connect(self._opacity_moved)
        opacity.addWidget(self.opacity, 1)
        opacity.addWidget(self.opacity_value)
        lay.addLayout(opacity)

        width = QtWidgets.QHBoxLayout()
        width.addWidget(QtWidgets.QLabel("Width"))
        self.width = QtWidgets.QSpinBox()
        self.width.setRange(1, 4)
        self.width.setValue(int(style.width))
        self.width.setSuffix(" px")
        self.width.valueChanged.connect(lambda v: self.changed.emit({"width": float(v)}))
        width.addWidget(self.width)
        width.addStretch()
        lay.addLayout(width)

        mode = QtWidgets.QHBoxLayout()
        self.outline = QtWidgets.QRadioButton("Outline")
        self.fill = QtWidgets.QRadioButton("Fill")
        self.outline.setChecked(style.mode == "outline")
        self.fill.setChecked(style.mode == "fill")
        self.fill.toggled.connect(
            lambda on: self.changed.emit({"mode": "fill" if on else "outline"}))
        mode.addWidget(self.outline)
        mode.addWidget(self.fill)
        mode.addStretch()
        lay.addLayout(mode)
        note = QtWidgets.QLabel(FILL_NOTE)
        lay.addWidget(note)
        self.fill_note = note

    @property
    def colour(self):
        return self._color

    def set_colour(self, rgb):
        rgb = tuple(float(v) for v in rgb)
        if rgb == self._color:
            return
        self._color = rgb
        self.changed.emit({"color": rgb})

    def _custom_colour(self):
        chosen = self.pick_custom_colour(_qcolor(self._color))
        if chosen is not None and chosen.isValid():
            self.set_colour((chosen.redF(), chosen.greenF(), chosen.blueF()))

    def _opacity_moved(self, value):
        self.opacity_value.setText(f"{value} %")
        self.changed.emit({"alpha": value / 100.0})


class MaskButton(QtWidgets.QToolButton):
    """`Cell mask ▾` / `Nucleus mask ▾`: the panel drops down under it."""

    def __init__(self, kind, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.setText(f"{LABELS[kind]} ▾")
        self.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.setStyleSheet(MODE_BUTTON_QSS.replace("QPushButton", "QToolButton")
                           + "QToolButton::menu-indicator{image:none;width:0;}")
        self.panel = MaskPanel(kind)
        menu = QtWidgets.QMenu(self)
        action = QtWidgets.QWidgetAction(menu)
        action.setDefaultWidget(self.panel)
        menu.addAction(action)
        self.setMenu(menu)
        self.panel.changed.connect(self._repaint_swatch)
        self._repaint_swatch()

    def _repaint_swatch(self, *_):
        self.setIcon(_swatch_icon(self.panel.colour))


class ElidedLabel(QtWidgets.QLabel):
    """One line, cut with … when it does not fit; the whole text on hover."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._full = ""
        self.setStyleSheet("color:#e5c07b;font-size:10px;")
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)

    def set_full_text(self, text):
        self._full = str(text or "")
        self.setToolTip(self._full)
        self._elide()

    def full_text(self):
        return self._full

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._elide()

    def _elide(self):
        width = max(0, self.width() - 4)
        self.setText(self.fontMetrics().elidedText(self._full, Qt.ElideRight, width))


class Step3MaskBar(QtCore.QObject):
    """The row's four controls and their signals."""

    run_chosen = pyqtSignal(str)            # an entry's key (a run folder and a region)
    delete_requested = pyqtSignal(str)      # block RM §9: the × of a run's row
    load_requested = pyqtSignal()           # `Load…` (block B3)
    style_changed = pyqtSignal(str, dict)   # kind, the fields that changed

    def __init__(self, parent=None):
        super().__init__(parent)
        self.run_combo = QtWidgets.QComboBox()
        self.run_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToContents)
        self.run_combo.setStyleSheet(
            "QComboBox{color:#ddd;background:#182230;border:1px solid #354a63;"
            "border-radius:4px;padding:1px 6px;font-size:10px;}")
        self.run_combo.activated.connect(self._run_activated)
        delegate = _TaggedItemDelegate(self.run_combo)
        delegate.close_clicked.connect(self._close_clicked)
        self.run_combo.setItemDelegate(delegate)
        # Block B3: any Step2 result of the open slide, from anywhere.
        self.load_button = QtWidgets.QPushButton("Load…")
        self.load_button.setStyleSheet(MODE_BUTTON_QSS)
        self.load_button.setToolTip("Load a Step2 result made on this slide "
                                    "(its run folder), from any project")
        self.load_button.clicked.connect(self.load_requested.emit)
        self._corner = None
        self.buttons = {kind: MaskButton(kind) for kind in (CELL, NUCLEUS)}
        for kind, button in self.buttons.items():
            button.panel.changed.connect(lambda change, k=kind: self.style_changed.emit(k, change))
        self.hint = ElidedLabel()
        self.set_runs([])

    def row_widgets(self):
        """The three controls before the hint, in order."""
        return (self.run_combo, self.buttons[CELL], self.buttons[NUCLEUS])

    def corner(self):
        """The run drop-down and `Load…`, for the tab bar's corner."""
        if self._corner is None:
            corner = QtWidgets.QWidget()
            lay = QtWidgets.QHBoxLayout(corner)
            lay.setContentsMargins(0, 0, 4, 0)
            lay.setSpacing(4)
            lay.addWidget(self.run_combo)
            lay.addWidget(self.load_button)
            self._corner = corner
        return self._corner

    def set_runs(self, items, current_dir=None):
        """`items` = [(label, run_dir)], [(label, run_dir, tag)] or
        [(label, run_dir, tag, deletable)], newest first; `current_dir`
        selected. A tag (the upstream's name) is drawn right-aligned at the
        row's end and is in the row's tooltip; a deletable row has a ×."""
        combo = self.run_combo
        combo.blockSignals(True)
        combo.clear()
        if not items:
            combo.addItem(NO_RUNS_TEXT, "")
            combo.setEnabled(False)
        else:
            for item in items:
                label, run_dir = item[0], item[1]
                tag = item[2] if len(item) > 2 else ""
                combo.addItem(label, run_dir)
                row = combo.count() - 1
                if tag:
                    combo.setItemData(row, tag, TAG_ROLE)
                    combo.setItemData(row, f"{label}  —  from {tag}", Qt.ToolTipRole)
                if len(item) > 3 and item[3]:
                    combo.setItemData(row, True, CLOSE_ROLE)
            combo.setEnabled(True)
            index = combo.findData(current_dir) if current_dir else -1
            combo.setCurrentIndex(max(0, index))
        fit_popup(combo)
        combo.blockSignals(False)

    def current_run_dir(self):
        """The chosen run's folder (an entry's key is `folder \\x1f region`)."""
        return str(self.run_combo.currentData() or "").partition("\x1f")[0]

    def current_key(self):
        return str(self.run_combo.currentData() or "")

    def set_mask_available(self, kind, available):
        self.buttons[kind].setEnabled(bool(available))

    def set_hint(self, text):
        self.hint.set_full_text(text)

    def _close_clicked(self, row):
        key = self.run_combo.itemData(row) or ""
        if key:
            self.run_combo.hidePopup()
            self.delete_requested.emit(str(key).partition("\x1f")[0])

    def _run_activated(self, index):
        run_dir = self.run_combo.itemData(index) or ""
        if run_dir:
            self.run_chosen.emit(run_dir)


# Block DV: the same row style for Step0's "Open a workspace" list.
TaggedItemDelegate = _TaggedItemDelegate
