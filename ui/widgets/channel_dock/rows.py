"""Per-page channel row widgets for the shared ChannelDock.

All rows share the base structure (visibility checkbox, color swatch, name);
page-specific extras stay in subclasses:

- Step0ChannelRow: + background-method combo (preview/decision) + status badge.
- WeightChannelRow: + weight slider + aligned numeric input (Step1).
- DisplayChannelRow: base only (Step3 rows: visibility/color/name only).

Rows write user interaction into the ChannelSetModel and follow model signals
for their channel; they never touch config files.
"""

from PyQt5 import QtCore, QtWidgets
from PyQt5.QtCore import Qt, pyqtSignal

from .model import ChannelSetModel

_NAME_STYLE = "color:#dce5ef;font-size:11px;"


class ChannelRowBase(QtWidgets.QWidget):
    """Color swatch + visibility + name. Click anywhere selects the channel."""

    color_clicked = pyqtSignal(str)

    def __init__(self, model: ChannelSetModel, cid: str, parent=None,
                 show_visibility=True):
        super().__init__(parent)
        self._model = model
        self._cid = cid
        st = model.get(cid)

        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(6, 2, 6, 2)
        lay.setSpacing(5)

        self.checkbox = QtWidgets.QCheckBox()
        self.checkbox.setChecked(bool(st and st.visible))
        self.checkbox.setEnabled(not (st and st.locked))
        self.checkbox.toggled.connect(self._on_visibility_toggled)
        self.checkbox.setVisible(show_visibility)
        lay.addWidget(self.checkbox)

        self.swatch = QtWidgets.QLabel()
        self.swatch.setFixedSize(13, 13)
        self.swatch.setCursor(Qt.PointingHandCursor)
        self.swatch.setToolTip("Click to change display color")
        lay.addWidget(self.swatch)

        self.name_label = QtWidgets.QLabel(st.name if st else cid)
        self.name_label.setStyleSheet(_NAME_STYLE)
        self.name_label.setToolTip(st.name if st else cid)
        self.name_label.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                      QtWidgets.QSizePolicy.Preferred)
        lay.addWidget(self.name_label, stretch=1)

        self._extras_layout = lay          # subclasses append here
        # Rows and their children stay transparent so the list's hover/selected
        # highlight paints through; host pages (e.g. Step0's #1c1c1c section
        # stylesheet) would otherwise cascade opaque boxes onto each child.
        # Explicit indicator style so the checkbox is drawn visibly ON TOP of
        # the hover/selected highlight instead of blending into it.
        self.setStyleSheet(
            "*{background:transparent;}"
            "QCheckBox::indicator{width:13px;height:13px;border-radius:2px;"
            "border:1px solid #6d8196;background:#182230;}"
            "QCheckBox::indicator:checked{background:#9bd0ff;border:1px solid #9bd0ff;}"
            "QCheckBox::indicator:disabled{border:1px solid #3a4a5c;background:#141b26;}")
        self._apply_color(st.color if st else "#888888")

        model.color_changed.connect(self._on_model_color)
        model.visibility_changed.connect(self._on_model_visibility)

    # -- properties ------------------------------------------------------
    @property
    def channel_id(self) -> str:
        return self._cid

    # -- model -> widget ---------------------------------------------------
    def _on_model_color(self, cid, color):
        if cid == self._cid:
            self._apply_color(color)

    def _on_model_visibility(self, cid, visible):
        if cid == self._cid and self.checkbox.isChecked() != visible:
            self.checkbox.blockSignals(True)
            self.checkbox.setChecked(visible)
            self.checkbox.blockSignals(False)

    def _apply_color(self, color):
        self.swatch.setStyleSheet(
            f"background:{color};border:1px solid #354a63;border-radius:2px;")

    # -- widget -> model ---------------------------------------------------
    def _on_visibility_toggled(self, checked):
        self._model.set_visible(self._cid, bool(checked))

    def mousePressEvent(self, ev):
        self._model.select(self._cid)
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):
        if self.swatch.geometry().contains(ev.pos()):
            self.color_clicked.emit(self._cid)
        super().mouseReleaseEvent(ev)


class Step0ChannelRow(ChannelRowBase):
    """Step0 row: color, name, background-method combo, status badge.

    The combo is the single control for the assigned/final method (as in v14);
    the badge shows preview/compute status (e.g. "", computing, done, unsaved).
    Method parameters and Compare live in the selected-channel inspector, not
    in the row.
    """

    method_changed = pyqtSignal(str, str)      # (channel_id, method text)

    METHODS = ["TopHat", "cucim", "Both", "Original"]

    def __init__(self, model, cid, parent=None):
        super().__init__(model, cid, parent)
        st = model.get(cid)

        # Fixed geometry so rows never shift: the checkbox keeps a constant
        # footprint even when the page restyles its indicator (green "done"
        # state), and the name column is non-stretching so the method combo
        # sits directly after the name. The host (adapter) sets one uniform
        # name width across rows — the longest name — so combos align.
        self.checkbox.setFixedSize(22, 18)
        self.name_label.setSizePolicy(QtWidgets.QSizePolicy.Fixed,
                                      QtWidgets.QSizePolicy.Preferred)
        self.name_label.setFixedWidth(self.name_label.sizeHint().width() + 2)
        self._extras_layout.setStretchFactor(self.name_label, 0)

        self.method_cb = QtWidgets.QComboBox()
        self.method_cb.addItems(self.METHODS)
        self.method_cb.setFixedWidth(64)
        self.method_cb.setEnabled(not (st and st.locked))
        self.method_cb.setStyleSheet(
            "QComboBox{background:#182230;color:#dce5ef;border:1px solid #354a63;"
            "border-radius:3px;padding:1px 2px;font-size:10px;}"
            "QComboBox::drop-down{border:none;}"
            "QComboBox:disabled{color:#555;}")
        if st and st.bg_final_method:
            idx = self._method_index(st.bg_final_method)
            if idx >= 0:
                self.method_cb.setCurrentIndex(idx)
        self.method_cb.currentTextChanged.connect(
            lambda txt: self.method_changed.emit(self._cid, txt))
        self._extras_layout.addWidget(self.method_cb)
        self._extras_layout.addStretch(1)   # push only the status badge right

        self.status_lbl = QtWidgets.QLabel("—")
        self.status_lbl.setAlignment(Qt.AlignCenter)
        self.status_lbl.setFixedWidth(20)
        self.status_lbl.setStyleSheet("color:#6d8196;font-size:12px;")
        self._extras_layout.addWidget(self.status_lbl)

        model.status_changed.connect(self._on_model_status)
        model.bg_final_changed.connect(self._on_model_final)

    @classmethod
    def _method_index(cls, method: str) -> int:
        lut = {"tophat": 0, "cucim": 1, "both": 2, "original": 3}
        return lut.get(str(method).lower(), -1)

    def _on_model_final(self, cid, method):
        if cid != self._cid:
            return
        idx = self._method_index(method)
        if idx >= 0 and idx != self.method_cb.currentIndex():
            self.method_cb.blockSignals(True)
            self.method_cb.setCurrentIndex(idx)
            self.method_cb.blockSignals(False)

    def _on_model_status(self, cid, status):
        if cid != self._cid:
            return
        style = {"computing": ("⟳", "color:#f4c45e;font-size:13px;"),
                 "done": ("✓", "color:#56d990;font-size:12px;"),
                 "unsaved": ("●", "color:#f4c45e;font-size:11px;"),
                 "nucleus": ("★", "color:#56b6c2;font-size:12px;")}.get(
            status, ("—", "color:#6d8196;font-size:12px;"))
        self.status_lbl.setText(style[0])
        self.status_lbl.setStyleSheet(style[1])


class WeightChannelRow(ChannelRowBase):
    """Step1 row: color, name, weight slider + aligned numeric input.

    Slider and spinbox are two-way synced; no background-correction method and
    no Min/Max/Gamma editors here (those are Step0 semantics).
    """

    def __init__(self, model, cid, parent=None):
        super().__init__(model, cid, parent)
        st = model.get(cid)
        w = st.weight if (st and st.weight is not None) else 1.0
        self._busy = False

        self.slider = QtWidgets.QSlider(Qt.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setValue(int(round(w * 100)))
        self.slider.setFixedHeight(16)
        self.slider.setMinimumWidth(70)
        self._extras_layout.addWidget(self.slider, stretch=2)

        self.spin = QtWidgets.QDoubleSpinBox()
        self.spin.setRange(0.0, 1.0)
        self.spin.setSingleStep(0.05)
        self.spin.setDecimals(2)
        self.spin.setValue(w)
        self.spin.setFixedWidth(52)
        self.spin.setAlignment(Qt.AlignRight)
        self.spin.setStyleSheet(
            "QDoubleSpinBox{background:#182230;color:#dce5ef;"
            "border:1px solid #354a63;border-radius:3px;font-size:10px;}")
        self._extras_layout.addWidget(self.spin)

        self.slider.valueChanged.connect(self._on_slider)
        self.spin.valueChanged.connect(self._on_spin)
        model.weight_changed.connect(self._on_model_weight)

    def weight(self) -> float:
        return float(self.spin.value())

    def _on_slider(self, v):
        if self._busy:
            return
        self._busy = True
        self.spin.setValue(v / 100.0)
        self._busy = False
        self._model.set_weight(self._cid, v / 100.0)

    def _on_spin(self, v):
        if self._busy:
            return
        self._busy = True
        self.slider.setValue(int(round(v * 100)))
        self._busy = False
        self._model.set_weight(self._cid, float(v))

    def _on_model_weight(self, cid, w):
        if cid != self._cid or self._busy:
            return
        self._busy = True
        self.slider.setValue(int(round(w * 100)))
        self.spin.setValue(w)
        self._busy = False


class DisplayChannelRow(ChannelRowBase):
    """Step3 row: visibility, color and name only (display/QC)."""
    pass
