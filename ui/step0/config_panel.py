"""
block01/ui/step0/config_panel.py — ConfigPanel for channel fusion group configuration.
"""

import os
import json

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QGroupBox, QComboBox, QInputDialog, QMessageBox,
    QFileDialog,
)
from PyQt5.QtCore import pyqtSignal

from ...config import NUCLEUS_CONFIG
from .channel_weight_row import ChannelWeightRow
from .group_panel import GroupPanel


class ConfigPanel(QWidget):
    config_changed = pyqtSignal()

    def __init__(self, all_channels):
        super().__init__()
        self.all_channels = all_channels
        self._panels      = {}
        self._setup_ui()

    def _setup_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)

        btn_row = QHBoxLayout()

        btn_reset = QPushButton("Reset")
        btn_reset.setStyleSheet(
            "QPushButton{background:#553;color:white;"
            "border-radius:4px;padding:4px;}"
            "QPushButton:hover{background:#775;}"
        )
        btn_reset.clicked.connect(self._reset_all_channel_weights)
        btn_row.addWidget(btn_reset)

        btn_load_weights = QPushButton("Load Weights")
        btn_load_weights.setStyleSheet(
            "QPushButton{background:#255;color:white;"
            "border-radius:4px;padding:4px;}"
            "QPushButton:hover{background:#377;}"
        )
        btn_load_weights.clicked.connect(self._load_weights_from_file)
        btn_row.addWidget(btn_load_weights)

        lay.addLayout(btn_row)

        # Nucleus
        nuc_box = QGroupBox("Nucleus Channel")
        nuc_box.setStyleSheet(
            "QGroupBox{border:1px solid #666;border-radius:4px;"
            "font-weight:bold;font-size:11px;}"
        )
        nl = QHBoxLayout(nuc_box)
        nl.addWidget(QLabel("Channel:"))
        self.nuc_combo = QComboBox()
        self.nuc_combo.addItems(self.all_channels)
        idx = self.nuc_combo.findText(NUCLEUS_CONFIG["channel"])
        if idx >= 0:
            self.nuc_combo.setCurrentIndex(idx)
        self.nuc_combo.currentTextChanged.connect(self.config_changed.emit)
        nl.addWidget(self.nuc_combo)
        nl.addWidget(QLabel("Weight:"))
        self.nuc_row = ChannelWeightRow("", NUCLEUS_CONFIG["weight"],
                                        show_label=False, show_delete=False)
        self.nuc_row.changed.connect(self.config_changed.emit)
        nl.addWidget(self.nuc_row, stretch=1)
        lay.addWidget(nuc_box)

        # Groups (scrollable)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea{border:none;}")
        self.grp_cont = QWidget()
        self.grp_lay  = QVBoxLayout(self.grp_cont)
        self.grp_lay.setContentsMargins(0, 0, 0, 0)
        self.grp_lay.setSpacing(5)
        self.grp_lay.addStretch()
        scroll.setWidget(self.grp_cont)
        lay.addWidget(scroll, stretch=1)

        btn_new = QPushButton("＋ New Group")
        btn_new.setStyleSheet(
            "QPushButton{background:#255;color:white;"
            "border-radius:4px;padding:4px;}"
            "QPushButton:hover{background:#377;}"
        )
        btn_new.clicked.connect(self._new_group)
        lay.addWidget(btn_new)

    def _add_group(self, name, cw=None):
        if name in self._panels:
            return
        p = GroupPanel(name, cw or {}, self.all_channels)
        p.config_changed.connect(self.config_changed.emit)
        p.remove_group.connect(self._del_group)
        self._panels[name] = p
        self.grp_lay.insertWidget(self.grp_lay.count() - 1, p)

    def _del_group(self, name):
        if name not in self._panels:
            return
        p = self._panels.pop(name)
        self.grp_lay.removeWidget(p)
        p.deleteLater()
        self.config_changed.emit()

    def _new_group(self):
        name, ok = QInputDialog.getText(self, "New Group", "Group name:")
        if ok and name.strip():
            self._add_group(name.strip())
            self.config_changed.emit()

    def _reset_all_channel_weights(self):
        nuc_ch = self.nuc_combo.currentText().strip()
        for panel in self._panels.values():
            for row in panel._rows.values():
                if row.ch_name != nuc_ch:
                    row.spin.setValue(0.0)
        self.config_changed.emit()

    def _load_weights_from_file(self):
        mw = self.window()
        out_dir = ""
        if hasattr(mw, "current_gui_work_dir"):
            out_dir = mw.current_gui_work_dir()
        if not out_dir and hasattr(mw, "_out_path_edit"):
            out_dir = mw._out_path_edit.text().strip()
        start_dir = out_dir if out_dir and os.path.exists(out_dir) else os.getcwd()
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Channel Weights", start_dir, "JSON Files (*.json)"
        )
        if not path:
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            QMessageBox.warning(self, "Load Weights", f"Invalid JSON file:\n{e}")
            return

        try:
            nucleus_cfg = cfg.get("nucleus") or {}
            groups_cfg = cfg.get("groups") or {}

            nuc_ch = nucleus_cfg.get("channel")
            if nuc_ch:
                idx = self.nuc_combo.findText(str(nuc_ch))
                if idx >= 0:
                    self.nuc_combo.setCurrentIndex(idx)
            self.nuc_row.spin.setValue(float(nucleus_cfg.get("weight", 0.0)))

            file_groups = {}
            for gname, gdata in groups_cfg.items():
                if not isinstance(gdata, dict):
                    continue
                ch_cfg = gdata.get("channels") or {}
                file_groups[gname] = {
                    "group_weight": float(gdata.get("group_weight", 0.0)),
                    "channels": {
                        str(ch): float(w)
                        for ch, w in ch_cfg.items()
                    },
                }

            for gname in file_groups:
                if gname not in self._panels:
                    self._add_group(gname, {})

            for gname, panel in self._panels.items():
                gdata = file_groups.get(gname, {})
                panel.gw_row.spin.setValue(float(gdata.get("group_weight", 0.0)))
                channels_cfg = gdata.get("channels", {})

                for ch in channels_cfg:
                    if ch in self.all_channels and ch not in panel._rows:
                        panel._add_row(ch, 0.0)

                for ch, row in panel._rows.items():
                    row.spin.setValue(float(channels_cfg.get(ch, 0.0)))

            self.config_changed.emit()

        except Exception as e:
            QMessageBox.warning(
                self, "Load Weights",
                f"Failed to apply weight configuration:\n{e}"
            )

    def get_groups(self):
        return {n: p.channel_weights() for n, p in self._panels.items()}

    def get_group_weights(self):
        return {n: p.group_weight() for n, p in self._panels.items()}

    def get_nucleus(self):
        return self.nuc_combo.currentText(), self.nuc_row.weight()

    def load_panel(self, groups, nuc_ch):
        """
        Load panel from parsed CSV.
        groups: {'group_name': {'CH': 1.0, ...}, ...}
        nuc_ch: str or None
        """
        for name in list(self._panels.keys()):
            self._del_group(name)

        if nuc_ch:
            idx = self.nuc_combo.findText(nuc_ch)
            if idx >= 0:
                self.nuc_combo.setCurrentIndex(idx)
                self.nuc_row.spin.setValue(1.0)
            else:
                self.nuc_combo.setCurrentIndex(-1)
                self.nuc_row.spin.setValue(0.0)
        else:
            self.nuc_combo.setCurrentIndex(-1)
            self.nuc_row.spin.setValue(0.0)

        for gname, channels in groups.items():
            zeroed = {ch: 0.0 for ch in channels.keys()}
            self._add_group(gname, zeroed)

        self.config_changed.emit()

    def get_full_config(self):
        nuc_ch, nuc_w = self.get_nucleus()
        return {
            "nucleus": {"channel": nuc_ch, "weight": nuc_w},
            "groups":  {
                n: {"group_weight": p.group_weight(),
                    "channels":     p.channel_weights()}
                for n, p in self._panels.items()
            },
        }
