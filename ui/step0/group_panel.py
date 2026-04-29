"""
block01/ui/step0/group_panel.py — GroupPanel widget for channel-fusion group management.
"""

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import (
    QGroupBox, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QInputDialog, QMessageBox,
)

from .channel_weight_row import ChannelWeightRow

_GROUP_COLORS = ["#e06c75", "#98c379", "#61afef", "#e5c07b", "#c678dd"]
_grp_color_idx = 0


class GroupPanel(QGroupBox):
    config_changed = pyqtSignal()
    remove_group   = pyqtSignal(str)

    def __init__(self, group_name, ch_weights, all_channels):
        global _grp_color_idx
        super().__init__()
        self.group_name   = group_name
        self.all_channels = all_channels
        self._rows        = {}

        color = _GROUP_COLORS[_grp_color_idx % len(_GROUP_COLORS)]
        _grp_color_idx += 1
        self.setStyleSheet(
            f"QGroupBox{{border:1px solid {color};"
            f"border-radius:5px;margin-top:2px;}}"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 4, 6, 4)
        outer.setSpacing(2)

        # Header row
        hdr = QHBoxLayout()
        dot = QLabel("●")
        dot.setStyleSheet(f"color:{color};font-size:13px;")
        hdr.addWidget(dot)
        hdr.addWidget(QLabel(group_name))
        hdr.addStretch()
        hdr.addWidget(QLabel("Group weight:"))
        self.gw_row = ChannelWeightRow("", 1.0, show_label=False, show_delete=False)
        self.gw_row.changed.connect(self.config_changed.emit)
        self.gw_row.setFixedWidth(155)
        hdr.addWidget(self.gw_row)
        btn_del = QPushButton("Delete")
        btn_del.setFixedSize(44, 20)
        btn_del.setStyleSheet(
            "QPushButton{color:#c44;font-size:10px;"
            "border:1px solid #c44;border-radius:3px;}"
        )
        btn_del.clicked.connect(lambda: self.remove_group.emit(self.group_name))
        hdr.addWidget(btn_del)
        outer.addLayout(hdr)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet(f"color:{color};")
        outer.addWidget(line)

        self.ch_lay = QVBoxLayout()
        self.ch_lay.setContentsMargins(0, 0, 0, 0)
        self.ch_lay.setSpacing(0)
        outer.addLayout(self.ch_lay)

        for ch, w in ch_weights.items():
            self._add_row(ch, w)

        btn_add = QPushButton("＋ Add Channel")
        btn_add.setStyleSheet(
            "QPushButton{color:#7a7;font-size:10px;border:none;text-align:left;}"
            "QPushButton:hover{color:#afa;}"
        )
        btn_add.clicked.connect(self._add_dialog)
        outer.addWidget(btn_add)

    def _add_row(self, ch, w=1.0):
        if ch in self._rows:
            return
        row = ChannelWeightRow(ch, w)
        row.changed.connect(self.config_changed.emit)
        row.remove_requested.connect(self._del_row)
        self._rows[ch] = row
        self.ch_lay.addWidget(row)

    def _del_row(self, ch):
        if ch not in self._rows:
            return
        w = self._rows.pop(ch)
        self.ch_lay.removeWidget(w)
        w.deleteLater()
        self.config_changed.emit()

    def _add_dialog(self):
        avail = [c for c in self.all_channels if c not in self._rows]
        if not avail:
            QMessageBox.information(self, "Info", "All channels are already in this group")
            return
        ch, ok = QInputDialog.getItem(
            self, f"Add Channel → {self.group_name}", "Select:", avail, 0, False
        )
        if ok and ch:
            self._add_row(ch, 1.0)
            self.config_changed.emit()

    def channel_weights(self):
        return {ch: r.weight() for ch, r in self._rows.items()}

    def group_weight(self):
        return self.gw_row.weight()
