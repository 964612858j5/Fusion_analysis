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

    run_chosen = pyqtSignal(str)            # a run directory
    style_changed = pyqtSignal(str, dict)   # kind, the fields that changed

    def __init__(self, parent=None):
        super().__init__(parent)
        self.run_combo = QtWidgets.QComboBox()
        self.run_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToContents)
        self.run_combo.setStyleSheet(
            "QComboBox{color:#ddd;background:#182230;border:1px solid #354a63;"
            "border-radius:4px;padding:1px 6px;font-size:10px;}")
        self.run_combo.activated.connect(self._run_activated)
        self.buttons = {kind: MaskButton(kind) for kind in (CELL, NUCLEUS)}
        for kind, button in self.buttons.items():
            button.panel.changed.connect(lambda change, k=kind: self.style_changed.emit(k, change))
        self.hint = ElidedLabel()
        self.set_runs([])

    def row_widgets(self):
        """The three controls before the hint, in order."""
        return (self.run_combo, self.buttons[CELL], self.buttons[NUCLEUS])

    def set_runs(self, items, current_dir=None):
        """`items` = [(label, run_dir)], newest first; `current_dir` selected."""
        combo = self.run_combo
        combo.blockSignals(True)
        combo.clear()
        if not items:
            combo.addItem(NO_RUNS_TEXT, "")
            combo.setEnabled(False)
        else:
            for label, run_dir in items:
                combo.addItem(label, run_dir)
            combo.setEnabled(True)
            index = combo.findData(current_dir) if current_dir else -1
            combo.setCurrentIndex(max(0, index))
        combo.blockSignals(False)

    def current_run_dir(self):
        return self.run_combo.currentData() or ""

    def set_mask_available(self, kind, available):
        self.buttons[kind].setEnabled(bool(available))

    def set_hint(self, text):
        self.hint.set_full_text(text)

    def _run_activated(self, index):
        run_dir = self.run_combo.itemData(index) or ""
        if run_dir:
            self.run_chosen.emit(run_dir)
