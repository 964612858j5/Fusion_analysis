"""Step3's mask controls (block 4c; multi-result plan §3.1, 2026-10-30).

One mask's settings panel (`MaskPanel`: colour presets and `Custom…`,
Opacity, Width 1..4 logical pixels, Solid / Dashed -- outline only), the
swatch that drops it down (`SwatchButton`, used in each row of the results
window), and the list helpers Step0 shares (`TaggedItemDelegate`,
`fit_popup`, `ElidedLabel`).

Controls only: this module reads no file and decides nothing.
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
            combo = self.parent()
            if isinstance(combo, QtWidgets.QComboBox):
                # (acceptance 2026-10-04) A combo's popup takes the release
                # for itself, so the × acts on the press: the popup closes,
                # then the deletion asks -- outside the popup's mouse handler.
                if event.type() == QtCore.QEvent.MouseButtonPress:
                    row = index.row()
                    combo.hidePopup()
                    QtCore.QTimer.singleShot(0, lambda: self.close_clicked.emit(row))
            elif event.type() == QtCore.QEvent.MouseButtonRelease:
                self.close_clicked.emit(index.row())
            return True                     # the × is not a click on the row
        return super().editorEvent(event, model, option, index)
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
    """One mask's settings (Step3 plan §3.1: the tick in the results row
    shows it; outline only, so no Fill): colour (presets and `Custom…`),
    Opacity, Width and Solid / Dashed. Emits `changed(dict)` with only what
    changed."""

    changed = pyqtSignal(dict)

    def __init__(self, kind, parent=None, style=None):
        super().__init__(parent)
        self.kind = kind
        style = style or DEFAULT_STYLES[kind]
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

        line = QtWidgets.QHBoxLayout()
        self.solid = QtWidgets.QRadioButton("Solid")
        self.dashed = QtWidgets.QRadioButton("Dashed")
        self.solid.setChecked(not style.dash)
        self.dashed.setChecked(bool(style.dash))
        self.dashed.toggled.connect(lambda on: self.changed.emit({"dash": bool(on)}))
        line.addWidget(self.solid)
        line.addWidget(self.dashed)
        line.addStretch()
        lay.addLayout(line)

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


class SwatchButton(QtWidgets.QToolButton):
    """A mask's colour; its `MaskPanel` drops down under it."""

    def __init__(self, kind, style=None, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.setFixedSize(26, 16)
        self.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.setStyleSheet("QToolButton{border:1px solid #555;border-radius:2px;}"
                           "QToolButton::menu-indicator{image:none;width:0;}")
        self.setToolTip(f"{LABELS[kind]}: colour, opacity, width, solid or dashed")
        self.panel = MaskPanel(kind, style=style)
        menu = QtWidgets.QMenu(self)
        action = QtWidgets.QWidgetAction(menu)
        action.setDefaultWidget(self.panel)
        menu.addAction(action)
        self.setMenu(menu)
        self.panel.changed.connect(self._repaint_swatch)
        self._repaint_swatch()

    def _repaint_swatch(self, *_):
        self.setIcon(_swatch_icon(self.panel.colour, size=12))


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


# Block DV: the same row style for Step0's "Open a workspace" list.
TaggedItemDelegate = _TaggedItemDelegate
