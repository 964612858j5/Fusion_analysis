"""
block01/ui/step0/channel_weight_row.py — Single channel weight slider + spinbox row.
"""

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QPushButton,
    QSlider, QDoubleSpinBox,
)


class ChannelWeightRow(QWidget):
    changed          = pyqtSignal()
    remove_requested = pyqtSignal(str)

    def __init__(self, ch_name, weight=1.0,
                 show_label=True, show_delete=True):
        super().__init__()
        self.ch_name = ch_name
        self._busy   = False

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 1, 0, 1)
        lay.setSpacing(4)

        if show_label:
            lbl = QLabel(ch_name)
            lbl.setFixedWidth(82)
            lbl.setStyleSheet("font-size:11px;")
            lay.addWidget(lbl)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setValue(int(weight * 100))
        self.slider.setFixedHeight(16)
        lay.addWidget(self.slider, stretch=1)

        self.spin = QDoubleSpinBox()
        self.spin.setRange(0.0, 1.0)
        self.spin.setSingleStep(0.05)
        self.spin.setDecimals(2)
        self.spin.setValue(weight)
        self.spin.setFixedWidth(56)
        self.spin.setStyleSheet("font-size:11px;")
        lay.addWidget(self.spin)

        if show_delete:
            btn = QPushButton("✕")
            btn.setFixedSize(20, 20)
            btn.setStyleSheet(
                "QPushButton{color:#c44;border:none;font-size:12px;}"
                "QPushButton:hover{color:#f66;}"
            )
            btn.clicked.connect(lambda: self.remove_requested.emit(self.ch_name))
            lay.addWidget(btn)

        self.slider.valueChanged.connect(self._sl)
        self.spin.valueChanged.connect(self._sp)

    def _sl(self, v):
        if self._busy:
            return
        self._busy = True
        self.spin.setValue(v / 100.0)
        self._busy = False
        self.changed.emit()

    def _sp(self, v):
        if self._busy:
            return
        self._busy = True
        self.slider.setValue(int(v * 100))
        self._busy = False
        self.changed.emit()

    def weight(self):
        return self.spin.value()
