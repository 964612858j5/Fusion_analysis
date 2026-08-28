"""Selected-channel tool-area editors for the shared ChannelDock.

- MinMaxGammaEditor: label / slider / value three-column aligned rows.
- Step0Inspector: Min/Max/Gamma + background-method parameters + Compare entry.
- Step3Inspector: Min/Max/Gamma explicitly labeled display/QC only.
"""

from PyQt5 import QtWidgets
from PyQt5.QtCore import Qt, pyqtSignal

_LBL = "color:#879bb1;font-size:10px;"
_VAL = ("QDoubleSpinBox,QSpinBox{background:#182230;color:#dce5ef;"
        "border:1px solid #354a63;border-radius:3px;font-size:10px;}")


class MinMaxGammaEditor(QtWidgets.QWidget):
    """Aligned Min / Max / Gamma rows: 52px label | slider | 64px value."""

    params_changed = pyqtSignal(float, float, float)   # (min, max, gamma)

    def __init__(self, parent=None, value_range=(0.0, 65535.0)):
        super().__init__(parent)
        grid = QtWidgets.QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(3)
        grid.setColumnMinimumWidth(0, 52)
        grid.setColumnStretch(1, 1)

        self._busy = False
        self._range = value_range

        def _row(r, text, lo, hi, decimals, step):
            lbl = QtWidgets.QLabel(text)
            lbl.setStyleSheet(_LBL)
            sld = QtWidgets.QSlider(Qt.Horizontal)
            sld.setRange(0, 1000)
            sld.setFixedHeight(16)
            spin = QtWidgets.QDoubleSpinBox()
            spin.setRange(lo, hi)
            spin.setDecimals(decimals)
            spin.setSingleStep(step)
            spin.setFixedWidth(64)
            spin.setAlignment(Qt.AlignRight)
            spin.setStyleSheet(_VAL)
            grid.addWidget(lbl, r, 0)
            grid.addWidget(sld, r, 1)
            grid.addWidget(spin, r, 2)
            return sld, spin

        lo, hi = value_range
        self.min_slider, self.min_spin = _row(0, "Min", lo, hi, 1, 1.0)
        self.max_slider, self.max_spin = _row(1, "Max", lo, hi, 1, 1.0)
        self.gamma_slider, self.gamma_spin = _row(2, "Gamma", 0.05, 5.0, 2, 0.05)

        self.min_spin.setValue(lo)
        self.max_spin.setValue(hi)
        self.gamma_spin.setValue(1.0)
        self._sync_sliders()

        for sld, spin, conv in (
                (self.min_slider, self.min_spin, self._frac_to_val),
                (self.max_slider, self.max_spin, self._frac_to_val)):
            sld.valueChanged.connect(
                lambda v, s=spin, c=conv: self._from_slider(s, c(v)))
        self.gamma_slider.valueChanged.connect(
            lambda v: self._from_slider(self.gamma_spin, 0.05 + (5.0 - 0.05) * v / 1000.0))
        for spin in (self.min_spin, self.max_spin, self.gamma_spin):
            spin.valueChanged.connect(self._on_spin)

    def _frac_to_val(self, v):
        lo, hi = self._range
        return lo + (hi - lo) * v / 1000.0

    def _val_to_frac(self, x):
        lo, hi = self._range
        span = (hi - lo) or 1.0
        return int(round(1000 * (x - lo) / span))

    def _sync_sliders(self):
        self._busy = True
        self.min_slider.setValue(self._val_to_frac(self.min_spin.value()))
        self.max_slider.setValue(self._val_to_frac(self.max_spin.value()))
        self.gamma_slider.setValue(
            int(round(1000 * (self.gamma_spin.value() - 0.05) / (5.0 - 0.05))))
        self._busy = False

    def _from_slider(self, spin, value):
        if self._busy:
            return
        spin.setValue(value)

    def _on_spin(self, _):
        if self._busy:
            return
        self._sync_sliders()
        self.params_changed.emit(self.min_spin.value(),
                                 self.max_spin.value(),
                                 self.gamma_spin.value())

    # -- host API ----------------------------------------------------------
    def set_values(self, dmin, dmax, gamma):
        self._busy = True
        if dmin is not None:
            self.min_spin.setValue(float(dmin))
        if dmax is not None:
            self.max_spin.setValue(float(dmax))
        if gamma is not None:
            self.gamma_spin.setValue(float(gamma))
        self._busy = False
        self._sync_sliders()

    def values(self):
        return (self.min_spin.value(), self.max_spin.value(),
                self.gamma_spin.value())


class Step0Inspector(QtWidgets.QWidget):
    """Selected-channel tools for Step0: Min/Max/Gamma, background-method
    parameters, and the Compare entry point."""

    bg_params_changed = pyqtSignal(dict)     # {"tophat_radius":int,"cucim_sigma":int}
    compare_requested = pyqtSignal()

    def __init__(self, parent=None, tophat_range=(1, 200), cucim_range=(1, 500)):
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(4)

        self.remap = MinMaxGammaEditor(self)
        lay.addWidget(self.remap)

        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setColumnMinimumWidth(0, 92)

        def _param(r, text, rng, tip):
            lbl = QtWidgets.QLabel(text)
            lbl.setStyleSheet(_LBL)
            lbl.setToolTip(tip)
            sb = QtWidgets.QSpinBox()
            sb.setRange(*rng)
            sb.setAlignment(Qt.AlignRight)
            sb.setFixedWidth(64)
            sb.setToolTip(tip)
            sb.setStyleSheet(_VAL)
            grid.addWidget(lbl, r, 0)
            grid.addWidget(sb, r, 1, alignment=Qt.AlignRight)
            return sb

        self.tophat_radius = _param(0, "TopHat radius", tophat_range,
                                    "TopHat disk radius (px)")
        self.cucim_sigma = _param(1, "cucim sigma", cucim_range,
                                  "cucim Gaussian sigma (px)")
        for sb in (self.tophat_radius, self.cucim_sigma):
            sb.valueChanged.connect(self._emit_bg_params)
        lay.addLayout(grid)

        self.compare_btn = QtWidgets.QPushButton("Compare methods…")
        self.compare_btn.setToolTip(
            "Open Compare mode: Original / Top-hat / cuCIM / Final selected "
            "on the same viewport")
        self.compare_btn.setStyleSheet(
            "QPushButton{color:#9bd0ff;border:1px solid #354a63;border-radius:3px;"
            "padding:3px 8px;font-size:10px;background:#182230;}"
            "QPushButton:hover{background:#23354a;}")
        self.compare_btn.clicked.connect(self.compare_requested.emit)
        lay.addWidget(self.compare_btn)

    def _emit_bg_params(self, _):
        self.bg_params_changed.emit({
            "tophat_radius": int(self.tophat_radius.value()),
            "cucim_sigma": int(self.cucim_sigma.value()),
        })

    def set_bg_params(self, tophat_radius=None, cucim_sigma=None):
        for sb, v in ((self.tophat_radius, tophat_radius),
                      (self.cucim_sigma, cucim_sigma)):
            if v is None:
                continue
            sb.blockSignals(True)
            sb.setValue(int(v))
            sb.blockSignals(False)


class Step3Inspector(QtWidgets.QWidget):
    """Selected-channel tools for Step3: Min/Max/Gamma, display/QC only.

    display_only is a hard semantic marker: values edited here must never be
    written into processing or segmentation configs.
    """

    display_only = True

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(4)

        badge = QtWidgets.QLabel("Display / QC only — does not modify "
                                 "segmentation or quantification results")
        badge.setWordWrap(True)
        badge.setStyleSheet(
            "color:#f4c45e;font-size:9px;background:#2a2415;"
            "border:1px solid #5a4a1f;border-radius:3px;padding:3px;")
        lay.addWidget(badge)

        self.remap = MinMaxGammaEditor(self)
        lay.addWidget(self.remap)
