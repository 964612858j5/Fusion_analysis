"""
block01/ui/step0/search_ctrl.py — SearchCtrlPanel and background workers.
"""

import os
import gc
import json
import time
import shutil
import traceback

import numpy as np
import tifffile
import zarr

from PyQt5 import QtWidgets, QtCore
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QGroupBox, QDoubleSpinBox, QProgressBar, QComboBox,
    QMessageBox, QFileDialog, QDialog, QFrame, QCheckBox,
)
import pyqtgraph as pg

from ...config import (
    OUTPUT_DIR, TOPHAT_RADIUS_DEFAULT, CUCIM_SIGMA_DEFAULT,
    TOPHAT_RADIUS_RANGE, CUCIM_SIGMA_RANGE, BG_CORR_MAX_TILE,
    PHASE2_FLOW, PHASE2_CELLPROB,
)
from ...core.bg_correction import (
    CUCIM_AVAILABLE,
    CUCIM_IMPORT_ERROR,
    _normalize_correction_config,
    _apply_background_method_tiled,
    _apply_tophat_gpu_or_cpu,
    _apply_tophat_cpu,
    _apply_cucim_or_cpu,
    _compute_bg_metrics,
    _tile_slices,
)
from ...core.io_loader import OMETIFFLoader
from ...utils.segmentation_config import (
    CELLPOSE_NUCLEI_DAPI,
    CELLPOSE_NUCLEI_EXPANSION,
    CELLPOSE_NUCLEI_HQ,
    CELLPOSE_NUCLEI_HQ2,
    CELLPOSE_WHOLECELL_FUSION,
    STARDIST_NUCLEI_DAPI,
    STARDIST_NUCLEI_EXPANSION,
    available_segmentation_methods,
    get_segmentation_method_config,
    normalize_segmentation_config,
)
from ...workers.hq_marker_segmentation import (
    CONSENSUS_MODES,
    parse_channel_weights,
    parse_hq_channels,
)

class SearchCtrlPanel(QWidget):
    """
    Cellpose grid-search control panel.

    Phase 1: Auto-diameter preview (diameter=None, cpsam estimates internally).
             Optional override spinbox for manual diameter.
    Phase 2: Fixed diameter, search flow × cellprob.

    Three ways to obtain parameters (any one unlocks the Save button):
      1. Run Phase 1 → Phase 2 and select from result grid
      2. Load an existing cellpose_params.json
      3. Manually fill in the spinboxes and click "Use These Params"
    """

    run_p1       = pyqtSignal(list)   # [diameter_or_None] — single-element list
    run_p2       = pyqtSignal(dict)   # {diameter, flow, prob}
    run_preview  = pyqtSignal(dict)   # direct patch preview for non-whole-cell methods
    stop         = pyqtSignal()
    params_ready = pyqtSignal(dict)
    method_changed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setMinimumSize(300, 390)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
        self._p2_diam     = None
        self._p2_diam_set = False   # True after Phase 1 completes
        self._setup_ui()

    def sizeHint(self):
        return QtCore.QSize(420, 460)

    def minimumSizeHint(self):
        return QtCore.QSize(300, 390)

    def _setup_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(5)

        method_box = QGroupBox("Segmentation Method")
        method_box.setMinimumHeight(86)
        method_box.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        method_box.setStyleSheet(
            "QGroupBox{border:1px solid #666;border-radius:4px;"
            "font-weight:bold;color:#ccc;font-size:11px;}"
        )
        method_lay = QVBoxLayout(method_box)
        method_lay.setContentsMargins(8, 8, 8, 6)
        method_lay.setSpacing(4)
        method_row = QHBoxLayout()
        method_label = QLabel("Method:")
        method_label.setFixedWidth(54)
        method_row.addWidget(method_label)
        self._method_combo = QComboBox()
        self._method_combo.setMinimumWidth(280)
        self._method_combo.setMinimumHeight(28)
        self._method_combo.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        for method in available_segmentation_methods():
            cfg = get_segmentation_method_config(method)
            self._method_combo.addItem(cfg["display_name"], method)
        self._method_combo.setCurrentIndex(
            max(0, self._method_combo.findData(CELLPOSE_WHOLECELL_FUSION))
        )
        self._method_combo.currentIndexChanged.connect(self._on_method_changed)
        method_row.addWidget(self._method_combo, stretch=1)
        method_lay.addLayout(method_row)

        exp_row = QHBoxLayout()
        self._expand_dist_label = QLabel("expansion_distance:")
        exp_row.addWidget(self._expand_dist_label)
        self._expand_dist = QDoubleSpinBox()
        self._expand_dist.setRange(0, 200)
        self._expand_dist.setDecimals(1)
        self._expand_dist.setSingleStep(1)
        self._expand_dist.setValue(8)
        self._expand_dist.setFixedWidth(80)
        exp_row.addWidget(self._expand_dist)
        exp_row.addStretch()
        method_lay.addLayout(exp_row)
        self._method_hint = QLabel("")
        self._method_hint.setStyleSheet("color:#999;font-size:10px;")
        self._method_hint.setWordWrap(True)
        self._method_hint.setMaximumHeight(36)
        method_lay.addWidget(self._method_hint)
        self._dapi_only_note = QLabel(
            "Segmentation uses DAPI only. Fusion panel remains available for preview/QC."
        )
        self._dapi_only_note.setStyleSheet("color:#8fb8ff;font-size:10px;")
        self._dapi_only_note.setWordWrap(True)
        self._dapi_only_note.setMaximumHeight(36)
        method_lay.addWidget(self._dapi_only_note)
        lay.addWidget(method_box)
        self._method_box = method_box

        # ── Phase 1 ───────────────────────────────────────────────────
        p1 = QGroupBox("Phase 1 — Auto-diameter preview  (cpsam)")
        self._p1_box = p1
        p1.setStyleSheet(
            "QGroupBox{border:1px solid #666;border-radius:4px;"
            "font-weight:bold;color:#ccc;font-size:11px;}"
        )
        pl1 = QVBoxLayout(p1)

        info_lbl = QLabel(
            "cpsam (Cellpose 4) estimates cell size automatically.\n"
            "Leave override = 0 to use auto-diameter (recommended)."
        )
        info_lbl.setStyleSheet("color:#aaa;font-size:10px;")
        info_lbl.setWordWrap(True)
        pl1.addWidget(info_lbl)

        ov_row = QHBoxLayout()
        ov_row.addWidget(QLabel("Override diameter (0 = auto):"))
        self._p1_override = QDoubleSpinBox()
        self._p1_override.setRange(0, 500)
        self._p1_override.setValue(0)
        self._p1_override.setSingleStep(5)
        self._p1_override.setDecimals(1)
        self._p1_override.setSpecialValueText("auto")
        self._p1_override.setFixedWidth(80)
        self._p1_override.setStyleSheet("font-size:11px;")
        self._p1_override.setToolTip(
            "0 = let cpsam estimate diameter automatically (recommended)\n"
            "Set a positive value only if auto results look wrong."
        )
        ov_row.addWidget(self._p1_override)
        ov_row.addStretch()
        pl1.addLayout(ov_row)

        self.btn_p1 = QPushButton("▶ Run Phase 1  (auto-diameter preview)")
        self.btn_p1.setStyleSheet(
            "QPushButton{background:#2a5;color:white;"
            "border-radius:4px;padding:5px;font-weight:bold;}"
            "QPushButton:hover{background:#3b6;}"
            "QPushButton:disabled{background:#333;color:#555;}"
        )
        self.btn_p1.clicked.connect(self._emit_p1)
        pl1.addWidget(self.btn_p1)
        lay.addWidget(p1)

        # ── Phase 2 ───────────────────────────────────────────────────
        p2 = QGroupBox("Phase 2 — Fine search: flow × cellprob")
        self._p2_box = p2
        p2.setStyleSheet(
            "QGroupBox{border:1px solid #666;border-radius:4px;"
            "font-weight:bold;color:#ccc;font-size:11px;}"
        )
        pl2 = QVBoxLayout(p2)
        self.p2_diam_lbl = QLabel("diameter: run Phase 1 first")
        self.p2_diam_lbl.setStyleSheet("color:#888;font-size:10px;")
        pl2.addWidget(self.p2_diam_lbl)
        fr = QHBoxLayout()
        fr.addWidget(QLabel("flow:"))
        self.flow_edit = QtWidgets.QLineEdit(
            ", ".join(str(f) for f in PHASE2_FLOW)
        )
        fr.addWidget(self.flow_edit)
        pl2.addLayout(fr)
        pr = QHBoxLayout()
        pr.addWidget(QLabel("cellprob:"))
        self.prob_edit = QtWidgets.QLineEdit(
            ", ".join(str(p) for p in PHASE2_CELLPROB)
        )
        pr.addWidget(self.prob_edit)
        pl2.addLayout(pr)
        self.btn_p2 = QPushButton("▶ Run Phase 2")
        self.btn_p2.setEnabled(False)
        self.btn_p2.setStyleSheet(
            "QPushButton{background:#2a5;color:white;"
            "border-radius:4px;padding:5px;font-weight:bold;}"
            "QPushButton:hover{background:#3b6;}"
            "QPushButton:disabled{background:#333;color:#555;}"
        )
        self.btn_p2.clicked.connect(self._emit_p2)
        pl2.addWidget(self.btn_p2)
        lay.addWidget(p2)

        # ── Divider ───────────────────────────────────────────────────
        div = QFrame()
        div.setFrameShape(QFrame.HLine)
        div.setStyleSheet("color:#444;")
        lay.addWidget(div)
        or_lbl = QLabel("— or skip grid search —")
        or_lbl.setAlignment(Qt.AlignCenter)
        or_lbl.setStyleSheet("color:#555;font-size:10px;")
        lay.addWidget(or_lbl)

        # ── Load existing params ───────────────────────────────────────
        load_box = QGroupBox("Load existing cellpose_params.json")
        load_box.setStyleSheet(
            "QGroupBox{border:1px solid #888;border-radius:4px;"
            "font-weight:bold;color:#aaa;font-size:11px;}"
        )
        ll = QVBoxLayout(load_box)
        load_row = QHBoxLayout()
        self.btn_load_params = QPushButton("📂 Browse…")
        self.btn_load_params.setStyleSheet(
            "QPushButton{color:#4d96ff;font-size:11px;"
            "border:1px solid #4d96ff;border-radius:3px;padding:3px 8px;}"
            "QPushButton:hover{background:#1a2a4a;}"
        )
        self.btn_load_params.clicked.connect(self._load_params_file)
        load_row.addWidget(self.btn_load_params)
        self._loaded_lbl = QLabel("No file loaded")
        self._loaded_lbl.setStyleSheet("color:#888;font-size:10px;")
        self._loaded_lbl.setWordWrap(True)
        load_row.addWidget(self._loaded_lbl, stretch=1)
        ll.addLayout(load_row)
        lay.addWidget(load_box)

        # ── Manual entry ───────────────────────────────────────────────
        man_box = QGroupBox("Manual / direct method parameters")
        self._manual_box = man_box
        man_box.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
        man_box.setStyleSheet(
            "QGroupBox{border:1px solid #888;border-radius:4px;"
            "font-weight:bold;color:#aaa;font-size:11px;}"
        )
        ml = QVBoxLayout(man_box)

        def _spin_row(label, lo, hi, step, dec, val):
            r = QHBoxLayout()
            l = QLabel(label)
            l.setFixedWidth(130)
            l.setMinimumHeight(24)
            r.addWidget(l)
            sp = QDoubleSpinBox()
            sp.setRange(lo, hi); sp.setSingleStep(step)
            sp.setDecimals(dec); sp.setValue(val)
            sp.setStyleSheet("font-size:11px;")
            sp.setMinimumHeight(24)
            sp.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
            r.addWidget(sp)
            return r, sp

        r1, self._man_diam  = _spin_row("diameter (px):",  0, 500, 5,   1, 30)
        r2, self._man_flow  = _spin_row("flow_threshold:", 0,   3, 0.05, 2, 0.4)
        r3, self._man_prob  = _spin_row("cellprob_threshold:", -6, 6, 0.1, 2, 0.0)
        self._cellpose_param_rows = [r1, r2, r3]
        self._man_diam.setSpecialValueText("auto")
        for r in (r1, r2, r3):
            ml.addLayout(r)

        sd_model_row = QHBoxLayout()
        sd_model_lbl = QLabel("model_name:")
        sd_model_lbl.setFixedWidth(130)
        sd_model_lbl.setMinimumHeight(24)
        sd_model_row.addWidget(sd_model_lbl)
        self._sd_model = QtWidgets.QLineEdit("2D_versatile_fluo")
        self._sd_model.setStyleSheet("font-size:11px;")
        self._sd_model.setMinimumHeight(24)
        sd_model_row.addWidget(self._sd_model)
        ml.addLayout(sd_model_row)

        r4, self._sd_prob = _spin_row("prob_thresh:", -1, 1, 0.05, 2, -1)
        r5, self._sd_nms = _spin_row("nms_thresh:", -1, 1, 0.05, 2, -1)
        r6, self._sd_expand_manual = _spin_row("expand_distance:", 0, 200, 1, 1, 8)
        self._sd_prob.setSpecialValueText("auto")
        self._sd_nms.setSpecialValueText("auto")
        self._stardist_param_rows = [sd_model_row, r4, r5, r6]
        for r in (r4, r5, r6):
            ml.addLayout(r)

        hq_channels_row = QHBoxLayout()
        self._hq_channels_label = QLabel("hq_channels:")
        self._hq_channels_label.setFixedWidth(130)
        self._hq_channels_label.setMinimumHeight(24)
        hq_channels_row.addWidget(self._hq_channels_label)
        self._hq_channels = QtWidgets.QLineEdit()
        self._hq_channels.setPlaceholderText("PanCK;CD45;CD68")
        self._hq_channels.setStyleSheet("font-size:11px;")
        self._hq_channels.setMinimumHeight(24)
        self._hq_channels.textChanged.connect(lambda _txt: self._refresh_patch_preview_state())
        hq_channels_row.addWidget(self._hq_channels)

        hq_mode_row = QHBoxLayout()
        self._hq_mode_label = QLabel("hq_input_mode:")
        self._hq_mode_label.setFixedWidth(130)
        self._hq_mode_label.setMinimumHeight(24)
        hq_mode_row.addWidget(self._hq_mode_label)
        self._hq_input_mode = QComboBox()
        self._hq_input_mode.setMinimumHeight(24)
        self._hq_input_mode.addItem("selected_channels_from_source", "selected_channels_from_source")
        self._hq_input_mode.addItem("step1_weighted_fusion", "step1_weighted_fusion")
        self._hq_input_mode.addItem("hybrid", "hybrid")
        hq_mode_row.addWidget(self._hq_input_mode)

        hq_radius_row, self._hq_radius = _spin_row("max_cell_radius:", 1, 200, 1, 1, 12)
        hq_low_row, self._hq_norm_low = _spin_row("norm pct low:", 0, 50, 0.5, 1, 1)
        hq_high_row, self._hq_norm_high = _spin_row("norm pct high:", 50, 100, 0.5, 1, 99.5)

        hq_consensus_row = QHBoxLayout()
        self._hq_consensus_label = QLabel("consensus_mode:")
        self._hq_consensus_label.setFixedWidth(130)
        self._hq_consensus_label.setMinimumHeight(24)
        hq_consensus_row.addWidget(self._hq_consensus_label)
        self._hq_consensus = QComboBox()
        self._hq_consensus.setMinimumHeight(24)
        for mode in CONSENSUS_MODES:
            self._hq_consensus.addItem(mode, mode)
        hq_consensus_row.addWidget(self._hq_consensus)

        hq_weights_row = QHBoxLayout()
        self._hq_weights_label = QLabel("channel_weights:")
        self._hq_weights_label.setFixedWidth(130)
        self._hq_weights_label.setMinimumHeight(24)
        hq_weights_row.addWidget(self._hq_weights_label)
        self._hq_weights = QtWidgets.QLineEdit()
        self._hq_weights.setPlaceholderText("optional: PanCK=1;CD45=0.8;CD68=1")
        self._hq_weights.setStyleSheet("font-size:11px;")
        self._hq_weights.setMinimumHeight(24)
        hq_weights_row.addWidget(self._hq_weights)

        hq_signal_row, self._hq_min_signal = _spin_row("min signal:", 0, 1, 0.01, 2, 0.08)
        self._hq_param_rows = [
            hq_channels_row,
            hq_mode_row,
            hq_radius_row,
            hq_low_row,
            hq_high_row,
            hq_consensus_row,
            hq_weights_row,
            hq_signal_row,
        ]
        for r in self._hq_param_rows:
            ml.addLayout(r)

        def _text_row(label, placeholder="", text=""):
            r = QHBoxLayout()
            l = QLabel(label)
            l.setFixedWidth(130)
            l.setMinimumHeight(24)
            r.addWidget(l)
            edit = QtWidgets.QLineEdit(text)
            edit.setPlaceholderText(placeholder)
            edit.setStyleSheet("font-size:11px;")
            edit.setMinimumHeight(24)
            r.addWidget(edit)
            return r, edit

        def _combo_row(label, values):
            r = QHBoxLayout()
            l = QLabel(label)
            l.setFixedWidth(130)
            l.setMinimumHeight(24)
            r.addWidget(l)
            combo = QComboBox()
            combo.setMinimumHeight(24)
            for value in values:
                combo.addItem(value, value)
            r.addWidget(combo)
            return r, combo

        hq2_title = QHBoxLayout()
        hq2_title_label = QLabel("HQ2 parameters")
        hq2_title_label.setStyleSheet("color:#7dd3fc;font-size:11px;font-weight:bold;")
        hq2_title.addWidget(hq2_title_label)
        hq2_title.addStretch()

        hq2_channels_row, self._hq2_channels = _text_row("hq2 channels:", "CD68;CD206;CD45")
        self._hq2_channels.textChanged.connect(lambda _txt: self._refresh_patch_preview_state())
        hq2_mode_row, self._hq2_input_mode = _combo_row(
            "hq2 input mode:",
            ["selected_channels_from_source", "step1_weighted_fusion", "hybrid"],
        )
        hq2_radius_row, self._hq2_radius = _spin_row("hq2 max radius:", 1, 300, 1, 1, 18)
        hq2_low_row, self._hq2_norm_low = _spin_row("hq2 norm low:", 0, 50, 0.5, 1, 1)
        hq2_high_row, self._hq2_norm_high = _spin_row("hq2 norm high:", 50, 100, 0.5, 1, 99.5)
        hq2_signal_row, self._hq2_min_signal = _spin_row("hq2 min signal:", 0, 1, 0.01, 2, 0.08)
        hq2_blur_row, self._hq2_imagej_blur = _spin_row("imagej blur:", 0, 10, 0.1, 1, 1.0)
        hq2_bg_row, self._hq2_bg_radius = _spin_row("imagej bg radius:", 0, 300, 1, 0, 20)
        hq2_thr_row, self._hq2_threshold_method = _combo_row("imagej threshold:", ["adaptive", "otsu", "percentile"])
        hq2_pct_row, self._hq2_threshold_percentile = _spin_row("threshold pct:", 0, 100, 1, 1, 75)
        hq2_min_obj_row, self._hq2_min_object = _spin_row("imagej min obj:", 0, 100000, 1, 0, 20)
        hq2_close_row, self._hq2_closing = _spin_row("imagej closing:", 0, 50, 1, 0, 2)
        hq2_open_row, self._hq2_opening = _spin_row("imagej opening:", 0, 50, 1, 0, 1)
        hq2_core_row, self._hq2_core_mode = _combo_row("core mode:", ["weighted_support", "intersection", "majority_support"])
        hq2_core_area_row, self._hq2_min_core = _spin_row("min core area:", 0, 100000, 1, 0, 8)
        hq2_signal_mode_row, self._hq2_signal_mode = _combo_row("signal map:", ["per_cell_best_channel", "max", "weighted_max"])
        hq2_min_cont_row, self._hq2_min_cont_signal = _spin_row("continuous signal:", 0, 1, 0.01, 2, 0.08)
        hq2_exp_row, self._hq2_max_expansion = _spin_row("expansion radius:", 0, 300, 1, 1, 25)
        hq2_boundary_row, self._hq2_boundary_weight = _spin_row("boundary weight:", 0, 10, 0.05, 2, 0.25)
        hq2_dist_row, self._hq2_distance_weight = _spin_row("distance penalty:", 0, 10, 0.01, 2, 0.02)
        hq2_neighbor_row, self._hq2_neighbor_weight = _spin_row("neighbor penalty:", 0, 10, 0.05, 2, 0.15)
        hq2_irregular_row = QHBoxLayout()
        hq2_irregular_label = QLabel("irregular shape:")
        hq2_irregular_label.setFixedWidth(130)
        hq2_irregular_label.setMinimumHeight(24)
        hq2_irregular_row.addWidget(hq2_irregular_label)
        self._hq2_irregular = QtWidgets.QCheckBox("allow")
        self._hq2_irregular.setChecked(True)
        hq2_irregular_row.addWidget(self._hq2_irregular)
        hq2_irregular_row.addStretch()
        hq2_macro_ch_row, self._hq2_macrophage_channels = _text_row("macrophage ch:", "CD68;CD206", "CD68;CD206")
        hq2_macro_r_row, self._hq2_macrophage_radius = _spin_row("macrophage radius:", 0, 500, 1, 1, 35)
        hq2_macro_s_row, self._hq2_macrophage_signal = _spin_row("macrophage signal:", 0, 1, 0.01, 2, 0.08)
        self._hq2_param_rows = [
            hq2_title, hq2_channels_row, hq2_mode_row, hq2_radius_row,
            hq2_low_row, hq2_high_row, hq2_signal_row,
            hq2_blur_row, hq2_bg_row, hq2_thr_row, hq2_pct_row,
            hq2_min_obj_row, hq2_close_row, hq2_open_row,
            hq2_core_row, hq2_core_area_row, hq2_signal_mode_row,
            hq2_min_cont_row, hq2_exp_row, hq2_boundary_row,
            hq2_dist_row, hq2_neighbor_row, hq2_irregular_row,
            hq2_macro_ch_row, hq2_macro_r_row, hq2_macro_s_row,
        ]
        for r in self._hq2_param_rows:
            ml.addLayout(r)
        self._legacy_hq2_param_rows = list(self._hq2_param_rows)
        self._build_hq2_params_panel()
        lay.insertWidget(1, self.hq2_params_panel)

        self._patch_preview_hint = QLabel("")
        self._patch_preview_hint.setStyleSheet("color:#ffb86c;font-size:10px;")
        self._patch_preview_hint.setWordWrap(True)
        ml.addWidget(self._patch_preview_hint)

        self.btn_use_manual = QPushButton("✓ Use These Params")
        self.btn_use_manual.setStyleSheet(
            "QPushButton{background:#444;color:#ccc;"
            "border-radius:4px;padding:4px;font-size:11px;}"
            "QPushButton:hover{background:#258;color:white;}"
        )
        self.btn_use_manual.clicked.connect(self._use_manual_params)
        ml.addWidget(self.btn_use_manual)

        self.btn_patch_preview = QPushButton("▶ Run Patch Preview")
        self.btn_patch_preview.setStyleSheet(
            "QPushButton{background:#246;color:white;"
            "border-radius:4px;padding:5px;font-weight:bold;}"
            "QPushButton:hover{background:#357;}"
            "QPushButton:disabled{background:#333;color:#555;}"
        )
        self.btn_patch_preview.clicked.connect(self._emit_patch_preview)
        ml.addWidget(self.btn_patch_preview)
        self._manual_params_scroll = QtWidgets.QScrollArea()
        self._manual_params_scroll.setWidgetResizable(True)
        self._manual_params_scroll.setFrameShape(QFrame.NoFrame)
        self._manual_params_scroll.setMinimumHeight(180)
        self._manual_params_scroll.setMaximumHeight(300)
        self._manual_params_scroll.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Preferred,
        )
        self._manual_params_scroll.setWidget(man_box)
        lay.addWidget(self._manual_params_scroll)
        self._on_method_changed()

        # ── Progress ──────────────────────────────────────────────────
        self.pbar = QProgressBar()
        self.pbar.setRange(0, 100)
        self.pbar.setStyleSheet(
            "QProgressBar{border:1px solid #444;border-radius:3px;"
            "text-align:center;color:#fff;height:16px;}"
            "QProgressBar::chunk{background:#2a5;}"
        )
        lay.addWidget(self.pbar)

        self.plbl = QLabel("")
        self.plbl.setStyleSheet("color:#aaa;font-size:10px;")
        self.plbl.setWordWrap(True)
        lay.addWidget(self.plbl)

        self.btn_stop = QPushButton("⏹ Stop")
        self.btn_stop.setEnabled(False)
        self.btn_stop.setStyleSheet(
            "QPushButton{background:#722;color:white;"
            "border-radius:4px;padding:4px;}"
            "QPushButton:hover{background:#944;}"
            "QPushButton:disabled{background:#333;color:#555;}"
        )
        self.btn_stop.clicked.connect(self.stop.emit)
        lay.addWidget(self.btn_stop)

    # ── Helpers ───────────────────────────────────────────────────────

    def _parse(self, txt):
        try:
            return [float(x.strip()) for x in txt.split(",") if x.strip()]
        except ValueError:
            return []

    def _emit_p1(self):
        if self._selected_method() not in (CELLPOSE_WHOLECELL_FUSION, CELLPOSE_NUCLEI_DAPI, CELLPOSE_NUCLEI_EXPANSION):
            QMessageBox.information(self, "Segmentation mode", "Phase 1 is only used for Cellpose modes.")
            return
        override = self._p1_override.value()
        # None = auto (diameter=0 in spinbox), positive = manual override
        diam = None if override <= 0 else override
        self.run_p1.emit([diam])

    def _emit_p2(self):
        if self._selected_method() not in (CELLPOSE_WHOLECELL_FUSION, CELLPOSE_NUCLEI_DAPI, CELLPOSE_NUCLEI_EXPANSION):
            QMessageBox.information(self, "Segmentation mode", "Phase 2 is only used for Cellpose modes.")
            return
        if not self._p2_diam_set:
            QMessageBox.warning(
                self, "Info",
                "Please run Phase 1 first to confirm the diameter."
            )
            return
        flows = self._parse(self.flow_edit.text())
        probs = self._parse(self.prob_edit.text())
        if not flows or not probs:
            QMessageBox.warning(self, "Error", "Invalid flow/cellprob format")
            return
        payload = {
            "diameter": self._p2_diam,   # None = auto, float = override
            "flow": flows, "prob": probs,
        }
        payload.update(self.get_selected_method_config())
        self.run_p2.emit(payload)

    def _emit_patch_preview(self):
        method = self._selected_method()
        if method in (CELLPOSE_WHOLECELL_FUSION, CELLPOSE_NUCLEI_DAPI, CELLPOSE_NUCLEI_EXPANSION):
            QMessageBox.information(
                self, "Segmentation mode",
                "Use Phase 1 / Phase 2 for Cellpose patch preview."
            )
            return
        hq_text = self._hq2_channels.text() if method == CELLPOSE_NUCLEI_HQ2 else self._hq_channels.text()
        if method in (CELLPOSE_NUCLEI_HQ, CELLPOSE_NUCLEI_HQ2) and not parse_hq_channels(hq_text):
            QMessageBox.warning(
                self,
                "HQ channels required",
                "Please enter HQ channels, e.g. PanCK;CD45;CD68",
            )
            self._refresh_patch_preview_state()
            return
        self.run_preview.emit(self.get_current_params())

    def _build_hq2_params_panel(self):
        self.hq2_params_panel = QGroupBox("Cellpose nuclei + HQ2 parameters")
        self.hq2_params_panel.setMinimumHeight(210)
        self.hq2_params_panel.setMaximumHeight(350)
        self.hq2_params_panel.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Preferred,
        )
        self.hq2_params_panel.setStyleSheet(
            "QGroupBox{border:1px solid #7dd3fc;border-radius:4px;"
            "font-weight:bold;color:#7dd3fc;font-size:11px;}"
        )
        outer = QVBoxLayout(self.hq2_params_panel)
        outer.setContentsMargins(6, 8, 6, 6)
        outer.setSpacing(5)

        scroll = QtWidgets.QScrollArea()
        self._hq2_params_scroll = scroll
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setMinimumHeight(180)
        scroll.setMaximumHeight(320)
        scroll.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Preferred,
        )
        content = QWidget()
        content.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
        form = QVBoxLayout(content)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(4)

        def spin(lo, hi, step, dec, val):
            w = QDoubleSpinBox()
            w.setRange(lo, hi)
            w.setSingleStep(step)
            w.setDecimals(dec)
            w.setValue(val)
            w.setStyleSheet("font-size:11px;")
            w.setMinimumHeight(24)
            w.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
            return w

        def section(title):
            box = QGroupBox(title)
            box.setMinimumHeight(24)
            box.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
            box.setStyleSheet(
                "QGroupBox{border:1px solid #444;border-radius:3px;"
                "margin-top:5px;color:#ccc;font-size:10px;font-weight:bold;}"
            )
            grid = QtWidgets.QGridLayout(box)
            grid.setContentsMargins(6, 8, 6, 5)
            grid.setHorizontalSpacing(6)
            grid.setVerticalSpacing(2)
            form.addWidget(box)
            return box, grid

        def add_row(grid, row, label, widget):
            lbl = QLabel(label)
            lbl.setMinimumWidth(132)
            lbl.setMinimumHeight(24)
            lbl.setStyleSheet("font-size:10px;color:#bbb;")
            widget.setMinimumHeight(24)
            widget.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
            grid.addWidget(lbl, row, 0)
            grid.addWidget(widget, row, 1)
            return widget

        def collapse_section(box, collapsed=True):
            for child in box.findChildren(QWidget):
                child.setVisible(not collapsed)
            box.setMaximumHeight(24 if collapsed else 16777215)

        def make_collapsible(box, collapsed=True):
            box.setCheckable(True)
            box.toggled.connect(lambda checked, b=box: collapse_section(b, collapsed=not checked))
            box.setChecked(not collapsed)
            collapse_section(box, collapsed=collapsed)

        _, g = section("Cellpose nuclei parameters")
        self._hq2_model_label = QLabel("cpsam nuclei")
        self._hq2_model_label.setStyleSheet("font-size:11px;color:#ddd;")
        add_row(g, 0, "model_type:", self._hq2_model_label)
        self._hq2_cp_diam = add_row(g, 1, "diameter:", spin(0, 500, 5, 1, 0))
        self._hq2_cp_diam.setSpecialValueText("auto")
        self._hq2_cp_flow = add_row(g, 2, "flow_threshold:", spin(0, 3, 0.05, 2, 0.4))
        self._hq2_cp_prob = add_row(g, 3, "cellprob_threshold:", spin(-6, 6, 0.1, 2, 0.0))
        self._hq2_cp_gpu = QCheckBox("Use GPU if available")
        self._hq2_cp_gpu.setChecked(True)
        add_row(g, 4, "GPU:", self._hq2_cp_gpu)
        self._hq2_cp_tile = add_row(g, 5, "tile_size:", spin(128, 4096, 128, 0, 1024))
        self._hq2_cp_batch = add_row(g, 6, "batch_size:", spin(1, 128, 1, 0, 8))

        _, g = section("HQ2 basic parameters")
        self._hq2_channels = QtWidgets.QLineEdit()
        self._hq2_channels.setMinimumHeight(24)
        self._hq2_channels.setPlaceholderText("CD68;CD206;CD45")
        self._hq2_channels.textChanged.connect(lambda _txt: self._refresh_patch_preview_state())
        add_row(g, 0, "hq_channels:", self._hq2_channels)
        self._hq2_input_mode = QComboBox()
        self._hq2_input_mode.setMinimumHeight(24)
        for value in ("selected_channels_from_source", "step1_weighted_fusion", "hybrid"):
            self._hq2_input_mode.addItem(value, value)
        add_row(g, 1, "hq_input_mode:", self._hq2_input_mode)
        self._hq2_radius = add_row(g, 2, "max_cell_radius:", spin(1, 300, 1, 1, 18))
        self._hq2_norm_low = add_row(g, 3, "norm pct low:", spin(0, 50, 0.5, 1, 1))
        self._hq2_norm_high = add_row(g, 4, "norm pct high:", spin(50, 100, 0.5, 1, 99.5))
        self._hq2_min_signal = add_row(g, 5, "min_signal_threshold:", spin(0, 1, 0.01, 2, 0.08))

        imagej_box, g = section("ImageJ-style proposal")
        self._hq2_imagej_blur = add_row(g, 0, "blur_sigma:", spin(0, 10, 0.1, 1, 1.0))
        self._hq2_bg_radius = add_row(g, 1, "background_radius:", spin(0, 300, 1, 0, 20))
        self._hq2_threshold_method = QComboBox()
        self._hq2_threshold_method.setMinimumHeight(24)
        for value in ("adaptive", "otsu", "percentile"):
            self._hq2_threshold_method.addItem(value, value)
        add_row(g, 2, "threshold_method:", self._hq2_threshold_method)
        self._hq2_threshold_percentile = add_row(g, 3, "threshold_percentile:", spin(0, 100, 1, 1, 75))
        self._hq2_min_object = add_row(g, 4, "min_object_size:", spin(0, 100000, 1, 0, 20))
        self._hq2_closing = add_row(g, 5, "closing_radius:", spin(0, 50, 1, 0, 2))
        self._hq2_opening = add_row(g, 6, "opening_radius:", spin(0, 50, 1, 0, 1))

        expansion_box, g = section("Continuous signal expansion")
        self._hq2_core_mode = QComboBox()
        self._hq2_core_mode.setMinimumHeight(24)
        for value in ("weighted_support", "intersection", "majority_support"):
            self._hq2_core_mode.addItem(value, value)
        add_row(g, 0, "core_mode:", self._hq2_core_mode)
        self._hq2_min_core = add_row(g, 1, "min_core_area:", spin(0, 100000, 1, 0, 8))
        self._hq2_signal_mode = QComboBox()
        self._hq2_signal_mode.setMinimumHeight(24)
        for value in ("per_cell_best_channel", "max", "weighted_max"):
            self._hq2_signal_mode.addItem(value, value)
        add_row(g, 2, "signal_map_mode:", self._hq2_signal_mode)
        self._hq2_min_cont_signal = add_row(g, 3, "min_continuous_signal:", spin(0, 1, 0.01, 2, 0.08))
        self._hq2_max_expansion = add_row(g, 4, "max_expansion_radius:", spin(0, 300, 1, 1, 25))
        self._hq2_boundary_weight = add_row(g, 5, "boundary_gradient_weight:", spin(0, 10, 0.05, 2, 0.25))
        self._hq2_distance_weight = add_row(g, 6, "distance_penalty_weight:", spin(0, 10, 0.01, 2, 0.02))
        self._hq2_neighbor_weight = add_row(g, 7, "neighbor_nucleus_penalty:", spin(0, 10, 0.05, 2, 0.15))
        self._hq2_irregular = QCheckBox("allow irregular shape")
        self._hq2_irregular.setMinimumHeight(24)
        self._hq2_irregular.setChecked(True)
        add_row(g, 8, "allow_irregular_shape:", self._hq2_irregular)

        macro_box, g = section("Macrophage refinement")
        self._hq2_macrophage_channels = QtWidgets.QLineEdit("CD68;CD206")
        self._hq2_macrophage_channels.setMinimumHeight(24)
        self._hq2_macrophage_channels.setPlaceholderText("CD68;CD206")
        add_row(g, 0, "macrophage_channels:", self._hq2_macrophage_channels)
        self._hq2_macrophage_radius = add_row(g, 1, "macrophage_max_radius:", spin(0, 500, 1, 1, 35))
        self._hq2_macrophage_signal = add_row(g, 2, "macrophage_min_signal:", spin(0, 1, 0.01, 2, 0.08))
        for optional_box in (imagej_box, expansion_box, macro_box):
            make_collapsible(optional_box, collapsed=True)

        scroll.setWidget(content)
        outer.addWidget(scroll)
        self.hq2_params_panel.setVisible(False)
        print("[HQ2-UI] parameter widgets created=True")

    def _show_hq2_params_panel(self):
        if getattr(self, "hq2_params_panel", None) is not None:
            self.hq2_params_panel.setVisible(True)

    def _hide_hq2_params_panel(self):
        if getattr(self, "hq2_params_panel", None) is not None:
            self.hq2_params_panel.setVisible(False)

    def collect_hq2_params(self):
        hq_channels = parse_hq_channels(self._hq2_channels.text())
        return {
            "method": CELLPOSE_NUCLEI_HQ2,
            "model_type": "cpsam",
            "diameter": None if self._hq2_cp_diam.value() == 0 else self._hq2_cp_diam.value(),
            "flow_threshold": self._hq2_cp_flow.value(),
            "cellprob_threshold": self._hq2_cp_prob.value(),
            "use_gpu": self._hq2_cp_gpu.isChecked(),
            "tile_size": int(self._hq2_cp_tile.value()),
            "batch_size": int(self._hq2_cp_batch.value()),
            "hq_channels": hq_channels,
            "hq_input_mode": self._hq2_input_mode.currentData() or "selected_channels_from_source",
            "max_cell_radius": self._hq2_radius.value(),
            "normalization_percentile_low": self._hq2_norm_low.value(),
            "normalization_percentile_high": self._hq2_norm_high.value(),
            "consensus_mode": "adaptive_best_channel",
            "channel_weights": {},
            "min_signal_threshold": self._hq2_min_signal.value(),
            "imagej_blur_sigma": self._hq2_imagej_blur.value(),
            "imagej_background_radius": int(self._hq2_bg_radius.value()),
            "imagej_threshold_method": self._hq2_threshold_method.currentData() or "adaptive",
            "imagej_threshold_percentile": self._hq2_threshold_percentile.value(),
            "imagej_min_object_size": int(self._hq2_min_object.value()),
            "imagej_closing_radius": int(self._hq2_closing.value()),
            "imagej_opening_radius": int(self._hq2_opening.value()),
            "core_mode": self._hq2_core_mode.currentData() or "weighted_support",
            "min_core_area": int(self._hq2_min_core.value()),
            "signal_map_mode": self._hq2_signal_mode.currentData() or "per_cell_best_channel",
            "min_continuous_signal": self._hq2_min_cont_signal.value(),
            "max_expansion_radius": self._hq2_max_expansion.value(),
            "boundary_gradient_weight": self._hq2_boundary_weight.value(),
            "distance_penalty_weight": self._hq2_distance_weight.value(),
            "neighbor_nucleus_penalty_weight": self._hq2_neighbor_weight.value(),
            "allow_irregular_shape": self._hq2_irregular.isChecked(),
            "macrophage_channels": self._hq2_macrophage_channels.text().strip(),
            "macrophage_max_radius": self._hq2_macrophage_radius.value(),
            "macrophage_min_signal": self._hq2_macrophage_signal.value(),
        }

    def _load_params_file(self):
        """Browse for cellpose_params.json and load it."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Load cellpose_params.json",
            os.path.join(OUTPUT_DIR, "cellpose_params.json"),
            "JSON (*.json)"
        )
        if not path or not os.path.exists(path):
            return
        try:
            with open(path) as f:
                p = normalize_segmentation_config(json.load(f))
            self.apply_seg_config_to_ui(p)
            d = p.get("diameter") or 0
            fl = p.get("flow_threshold", 0.4)
            cp = p.get("cellprob_threshold", 0.0)
            self._loaded_lbl.setText(
                f"✓ {os.path.basename(path)}  "
                f"diam={d}  flow={fl}  prob={cp}"
            )
            self._loaded_lbl.setStyleSheet("color:#4c4;font-size:10px;")
            data = {
                "params":             dict(p.get("params") or {}),
                "diameter":           d if d > 0 else None,
                "flow_threshold":     fl,
                "cellprob_threshold": cp,
                "_source":            "loaded",
            }
            if p.get("method"):
                data["method"] = p.get("method")
                idx = self._method_combo.findData(p.get("method"))
                if idx >= 0:
                    self._method_combo.setCurrentIndex(idx)
            if p.get("expand_distance") is not None:
                self._sd_expand_manual.setValue(float(p.get("expand_distance")))
                data["params"]["expand_distance"] = float(p.get("expand_distance"))
            params = normalize_segmentation_config(data)
            self.params_ready.emit(params)
        except Exception as e:
            QMessageBox.warning(self, "Error", "Failed to load params:\n" + str(e))

    def apply_seg_config_to_ui(self, cfg):
        """Restore method-specific parameter widgets from a saved config."""
        p = normalize_segmentation_config(cfg or {})
        method = p.get("method") or CELLPOSE_WHOLECELL_FUSION
        idx = self._method_combo.findData(method)
        if idx >= 0:
            self._method_combo.setCurrentIndex(idx)
        self._man_diam.setValue(float(p.get("diameter") or 0))
        self._man_flow.setValue(float(p.get("flow_threshold", 0.4)))
        self._man_prob.setValue(float(p.get("cellprob_threshold", 0.0)))
        if p.get("expand_distance") is not None:
            self._sd_expand_manual.setValue(float(p.get("expand_distance")))
        if method == CELLPOSE_NUCLEI_HQ:
            self._hq_channels.setText(";".join(parse_hq_channels(p.get("hq_channels") or [])))
            idx = self._hq_input_mode.findData(p.get("hq_input_mode", "selected_channels_from_source"))
            self._hq_input_mode.setCurrentIndex(max(0, idx))
            self._hq_radius.setValue(float(p.get("max_cell_radius", 12) or 12))
            self._hq_norm_low.setValue(float(p.get("normalization_percentile_low", 1.0)))
            self._hq_norm_high.setValue(float(p.get("normalization_percentile_high", 99.5)))
            idx = self._hq_consensus.findData(p.get("consensus_mode", "adaptive_best_channel"))
            self._hq_consensus.setCurrentIndex(max(0, idx))
            weights = p.get("channel_weights") or {}
            self._hq_weights.setText(";".join(f"{k}={v}" for k, v in weights.items()))
            self._hq_min_signal.setValue(float(p.get("min_signal_threshold", 0.08)))
        if method == CELLPOSE_NUCLEI_HQ2:
            self._hq2_channels.setText(";".join(parse_hq_channels(p.get("hq_channels") or [])))
            idx = self._hq2_input_mode.findData(p.get("hq_input_mode", "selected_channels_from_source"))
            self._hq2_input_mode.setCurrentIndex(max(0, idx))
            self._hq2_radius.setValue(float(p.get("max_cell_radius", 18) or 18))
            self._hq2_norm_low.setValue(float(p.get("normalization_percentile_low", 1.0)))
            self._hq2_norm_high.setValue(float(p.get("normalization_percentile_high", 99.5)))
            self._hq2_min_signal.setValue(float(p.get("min_signal_threshold", 0.08)))
            self._hq2_imagej_blur.setValue(float(p.get("imagej_blur_sigma", 1.0)))
            self._hq2_bg_radius.setValue(float(p.get("imagej_background_radius", 20)))
            idx = self._hq2_threshold_method.findData(p.get("imagej_threshold_method", "adaptive"))
            self._hq2_threshold_method.setCurrentIndex(max(0, idx))
            self._hq2_threshold_percentile.setValue(float(p.get("imagej_threshold_percentile", 75.0)))
            self._hq2_min_object.setValue(float(p.get("imagej_min_object_size", 20)))
            self._hq2_closing.setValue(float(p.get("imagej_closing_radius", 2)))
            self._hq2_opening.setValue(float(p.get("imagej_opening_radius", 1)))
            idx = self._hq2_core_mode.findData(p.get("core_mode", "weighted_support"))
            self._hq2_core_mode.setCurrentIndex(max(0, idx))
            self._hq2_min_core.setValue(float(p.get("min_core_area", 8)))
            idx = self._hq2_signal_mode.findData(p.get("signal_map_mode", "per_cell_best_channel"))
            self._hq2_signal_mode.setCurrentIndex(max(0, idx))
            self._hq2_min_cont_signal.setValue(float(p.get("min_continuous_signal", 0.08)))
            self._hq2_max_expansion.setValue(float(p.get("max_expansion_radius", 25)))
            self._hq2_boundary_weight.setValue(float(p.get("boundary_gradient_weight", 0.25)))
            self._hq2_distance_weight.setValue(float(p.get("distance_penalty_weight", 0.02)))
            self._hq2_neighbor_weight.setValue(float(p.get("neighbor_nucleus_penalty_weight", 0.15)))
            self._hq2_irregular.setChecked(bool(p.get("allow_irregular_shape", True)))
            self._hq2_macrophage_channels.setText(str(p.get("macrophage_channels", "CD68;CD206") or ""))
            self._hq2_macrophage_radius.setValue(float(p.get("macrophage_max_radius", 35)))
            self._hq2_macrophage_signal.setValue(float(p.get("macrophage_min_signal", 0.08)))
            print(f"[HQ2-UI] restored params={p.get('params')}")
        self._on_method_changed()

    def _use_manual_params(self):
        """Use the manually entered spinbox values."""
        d  = self._man_diam.value()
        fl = self._man_flow.value()
        cp = self._man_prob.value()
        data = {
            "diameter":           d if d > 0 else None,
            "flow_threshold":     fl,
            "cellprob_threshold": cp,
            "_source":            "manual",
        }
        data.update(self.get_selected_method_config())
        params = normalize_segmentation_config(data)
        self._loaded_lbl.setText(
            f"✓ Manual  method={params.get('method')}  diam={d}  flow={fl}  prob={cp}"
        )
        self._loaded_lbl.setStyleSheet("color:#fa8;font-size:10px;")
        self.params_ready.emit(params)

    # ── Called by MainWindow ──────────────────────────────────────────

    def set_p2_diam(self, d):
        self._p2_diam     = d
        self._p2_diam_set = True
        label = "auto (cpsam)" if d is None else str(d)
        self.p2_diam_lbl.setText(f"diameter = {label}  (from Phase 1)")
        self.btn_p2.setEnabled(self._selected_method() in (CELLPOSE_WHOLECELL_FUSION, CELLPOSE_NUCLEI_DAPI, CELLPOSE_NUCLEI_EXPANSION))

    def set_running(self, running):
        method = self._selected_method()
        is_cellpose = method in (CELLPOSE_WHOLECELL_FUSION, CELLPOSE_NUCLEI_DAPI, CELLPOSE_NUCLEI_EXPANSION)
        self.btn_p1.setEnabled(not running and is_cellpose)
        self.btn_p2.setEnabled(not running and self._p2_diam_set and is_cellpose)
        self._refresh_patch_preview_state(running=running)
        self.btn_stop.setEnabled(running)

    def update_progress(self, done, total, msg):
        if total > 0:
            self.pbar.setValue(int(done / total * 100))
        self.plbl.setText(f"[{done}/{total}]  {msg}")

    def get_current_params(self):
        """Return spinbox values regardless of source."""
        method = self._selected_method()
        if method == CELLPOSE_NUCLEI_HQ2:
            params = self.collect_hq2_params()
            print(f"[HQ2-UI] collected params={params}")
            return normalize_segmentation_config({"method": CELLPOSE_NUCLEI_HQ2, "params": params, **params})
        d  = self._man_diam.value()
        fl = self._man_flow.value()
        cp = self._man_prob.value()
        params = {
            "diameter":           d if d > 0 else None,
            "flow_threshold":     fl,
            "cellprob_threshold": cp,
        }
        params.update(self.get_selected_method_config())
        return normalize_segmentation_config(params)

    def get_selected_method_config(self):
        method = self._selected_method()
        cfg = get_segmentation_method_config(method)
        params = dict(cfg.get("params") or {})
        if method in (STARDIST_NUCLEI_DAPI, STARDIST_NUCLEI_EXPANSION):
            params["model_name"] = self._sd_model.text().strip() or "2D_versatile_fluo"
            params["prob_thresh"] = None if self._sd_prob.value() < 0 else self._sd_prob.value()
            params["nms_thresh"] = None if self._sd_nms.value() < 0 else self._sd_nms.value()
            params["device_preference"] = "gpu_first"
        if method in (CELLPOSE_NUCLEI_EXPANSION, STARDIST_NUCLEI_EXPANSION):
            params["expand_distance"] = self._sd_expand_manual.value()
        if method == CELLPOSE_NUCLEI_HQ:
            hq_channels = parse_hq_channels(self._hq_channels.text())
            params["hq_channels"] = hq_channels
            params["hq_input_mode"] = self._hq_input_mode.currentData() or "selected_channels_from_source"
            params["max_cell_radius"] = self._hq_radius.value()
            params["normalization_percentile_low"] = self._hq_norm_low.value()
            params["normalization_percentile_high"] = self._hq_norm_high.value()
            params["consensus_mode"] = self._hq_consensus.currentData() or "adaptive_best_channel"
            params["channel_weights"] = parse_channel_weights(self._hq_weights.text(), hq_channels)
            params["min_signal_threshold"] = self._hq_min_signal.value()
        if method == CELLPOSE_NUCLEI_HQ2:
            params.update(self.collect_hq2_params())
            print(f"[HQ2-UI] collected params={params}")
        return {"method": method, "params": params}

    def _selected_method(self):
        method = self._method_combo.currentData() or CELLPOSE_WHOLECELL_FUSION
        return method

    def _refresh_patch_preview_state(self, running=False):
        method = self._selected_method()
        is_hq = method in (CELLPOSE_NUCLEI_HQ, CELLPOSE_NUCLEI_HQ2)
        is_stardist = method in (STARDIST_NUCLEI_DAPI, STARDIST_NUCLEI_EXPANSION)
        hq_text = self._hq2_channels.text() if method == CELLPOSE_NUCLEI_HQ2 else self._hq_channels.text()
        enabled = (is_stardist or (is_hq and bool(parse_hq_channels(hq_text)))) and not running
        self.btn_patch_preview.setEnabled(enabled)
        if is_hq and not parse_hq_channels(hq_text):
            self._patch_preview_hint.setText("Please enter HQ channels, e.g. PanCK;CD45;CD68")
        else:
            self._patch_preview_hint.setText("")
        if method == CELLPOSE_NUCLEI_HQ2:
            print(f"[HQ2-UI] patch preview enabled={enabled}")

    def _on_method_changed(self):
        method = self._selected_method()
        cfg = get_segmentation_method_config(method)
        is_cellpose = method in (CELLPOSE_WHOLECELL_FUSION, CELLPOSE_NUCLEI_DAPI, CELLPOSE_NUCLEI_EXPANSION)
        is_stardist = method in (STARDIST_NUCLEI_DAPI, STARDIST_NUCLEI_EXPANSION)
        is_expansion = method in (CELLPOSE_NUCLEI_EXPANSION, STARDIST_NUCLEI_EXPANSION)
        is_hq = method == CELLPOSE_NUCLEI_HQ
        is_hq2 = method == CELLPOSE_NUCLEI_HQ2
        self._p1_box.setVisible(is_cellpose)
        self._p2_box.setVisible(is_cellpose)
        self._p1_box.setEnabled(is_cellpose)
        self._p2_box.setEnabled(is_cellpose)
        self._p1_box.setTitle(
            "Phase 1 — Auto-diameter preview  (cpsam)"
            if is_cellpose else "Phase 1 — StarDist direct params (not required)"
        )
        self._p2_box.setTitle(
            "Phase 2 — Fine search: flow × cellprob"
            if is_cellpose else "Phase 2 — StarDist direct params (not required)"
        )
        self.btn_p1.setEnabled(is_cellpose)
        self.btn_p2.setEnabled(is_cellpose and self._p2_diam_set)
        for row in self._cellpose_param_rows:
            for i in range(row.count()):
                w = row.itemAt(i).widget()
                if w:
                    w.setVisible(is_cellpose)
        for row in self._stardist_param_rows:
            visible = is_stardist
            if row is self._stardist_param_rows[-1]:
                visible = is_expansion
            for i in range(row.count()):
                w = row.itemAt(i).widget()
                if w:
                    w.setVisible(visible)
        for row in self._hq_param_rows:
            for i in range(row.count()):
                w = row.itemAt(i).widget()
                if w:
                    w.setVisible(is_hq)
        for row in getattr(self, "_legacy_hq2_param_rows", []):
            for i in range(row.count()):
                w = row.itemAt(i).widget()
                if w:
                    w.setVisible(False)
        if is_hq2:
            self._show_hq2_params_panel()
            self._manual_params_scroll.setMinimumHeight(96)
            self._manual_params_scroll.setMaximumHeight(150)
        else:
            self._hide_hq2_params_panel()
            self._manual_params_scroll.setMinimumHeight(180)
            self._manual_params_scroll.setMaximumHeight(300)
        self._expand_dist.setEnabled(is_expansion)
        self._expand_dist.setVisible(False)
        self._expand_dist_label.setVisible(False)
        self._method_hint.setText(
            f"{method}  |  input={cfg.get('input_type')}  output={cfg.get('output_type')}"
        )
        self._dapi_only_note.setVisible(method != CELLPOSE_WHOLECELL_FUSION)
        self._refresh_patch_preview_state()
        self.btn_patch_preview.setVisible(True)
        workflow = {
            CELLPOSE_WHOLECELL_FUSION: "wholecell_phase1_phase2",
            CELLPOSE_NUCLEI_DAPI: "nuclei_cellpose",
            CELLPOSE_NUCLEI_EXPANSION: "nuclei_cellpose_expansion",
            CELLPOSE_NUCLEI_HQ: "cellpose_nuclei_hq_patch_preview",
            CELLPOSE_NUCLEI_HQ2: "cellpose_nuclei_hq2_patch_preview",
            STARDIST_NUCLEI_DAPI: "stardist",
            STARDIST_NUCLEI_EXPANSION: "stardist_expansion",
        }.get(method, "unknown")
        print("[Step2] method changed:", method)
        print("[Step2] is_hq:", is_hq)
        print("[Step2] hq widgets visible:", self._hq_channels.isVisible())
        print("[Step2] preview enabled:", self.btn_patch_preview.isEnabled())
        print("[HQ2-UI] method selected=", method)
        print("[HQ2-UI] parameter widgets created=", bool(getattr(self, "_hq2_param_rows", None)))
        print("[HQ2-UI] hq2_panel exists =", self.hq2_params_panel is not None)
        print("[HQ2-UI] hq2_panel visible =", self.hq2_params_panel.isVisible())
        print("[HQ2-UI] parameter widgets visible=", self._hq2_channels.isVisible())
        print("[HQ2-UI] patch preview enabled=", self.btn_patch_preview.isEnabled())
        print(f"[Step1] segmentation mode selected={method}")
        print(f"[Step1] workflow={workflow}")
        print(f"[Step1] phase1_required={is_cellpose}")
        print(f"[Step1] mode switched={method}")
        print("[Step1] layout stable=True")
        self.method_changed.emit(method)


# ══════════════════════════════════════════════════════════════════════
#  Main Window
# ══════════════════════════════════════════════════════════════════════

class RawDataPrefetchWorker(QThread):
    """后台IO worker：批量读取指定通道的所有patch原始数据，写入raw_cache。
    不做任何计算，只负责磁盘→内存的IO，尽快完成。
    """
    finished = pyqtSignal(str, int, object)   # (channel, patch_idx, raw_array)
    error    = pyqtSignal(str, int, str)      # (channel, patch_idx, msg)

    def __init__(self, loader, channel_name, patch_idx, roi,
                 nucleus_channel=None, parent=None):
        super().__init__(parent)
        self.loader          = loader
        self.channel_name    = channel_name
        self.patch_idx       = patch_idx
        self.roi             = roi
        self.nucleus_channel = nucleus_channel
        self._stop           = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            y0, y1, x0, x1 = self.roi
            raw = self.loader.read_region(
                self.channel_name, y0, y1, x0, x1,
                downsample=1, correction_config={}, normalize=False,
            )
            if self._stop:
                return
            self.finished.emit(self.channel_name, self.patch_idx, raw)
        except Exception:
            if not self._stop:
                self.error.emit(self.channel_name, self.patch_idx, traceback.format_exc())


class BgComputeWorker(QThread):
    """纯GPU计算worker：接受已读好的raw数组，不做任何IO。
    tophat + cucim + norm + metrics，全部在GPU上完成。
    """
    finished = pyqtSignal(int, dict)
    error    = pyqtSignal(int, str)

    def __init__(self, request_id, channel_name, raw, nuc_raw,
                 tophat_radius, cucim_sigma, parent=None):
        super().__init__(parent)
        self.request_id    = request_id
        self.channel_name  = channel_name
        self.raw           = raw           # float32 ndarray，已在内存
        self.nuc_raw       = nuc_raw       # float32 ndarray 或 None
        self.tophat_radius = int(tophat_radius)
        self.cucim_sigma   = int(cucim_sigma)
        self._stop         = False

    def stop(self):
        self._stop = True

    @staticmethod
    def _make_rgb(marker_norm, nucleus_norm):
        r = np.zeros_like(marker_norm, dtype=np.float32)
        g = marker_norm.astype(np.float32, copy=False)
        b = nucleus_norm.astype(np.float32, copy=False) if nucleus_norm is not None             else np.zeros_like(marker_norm, dtype=np.float32)
        return np.stack([r, g, b], axis=-1)

    def run(self):
        try:
            raw = self.raw
            if self._stop:
                return

            # 全部GPU计算（patch小于tile_size时直接整张计算，不tile）
            tophat = _apply_tophat_gpu_or_cpu(raw, self.tophat_radius)
            if self._stop:
                return
            cucim = _apply_cucim_or_cpu(raw, self.cucim_sigma, prefer_gpu=CUCIM_AVAILABLE)
            if self._stop:
                return

            nuc_norm    = OMETIFFLoader._norm(self.nuc_raw) if self.nuc_raw is not None else None
            orig_norm   = OMETIFFLoader._norm(raw)
            tophat_norm = OMETIFFLoader._norm(tophat)
            cucim_norm  = OMETIFFLoader._norm(cucim)

            payload = {
                "original_disp":    orig_norm,
                "tophat_disp":      tophat_norm,
                "cucim_disp":       cucim_norm,
                "nucleus_disp":     nuc_norm,
                "original_rgb":     self._make_rgb(orig_norm,   nuc_norm),
                "tophat_rgb":       self._make_rgb(tophat_norm,  nuc_norm),
                "cucim_rgb":        self._make_rgb(cucim_norm,   nuc_norm),
                "original_raw":     raw.astype(np.float32, copy=False),
                "tophat_raw":       tophat.astype(np.float32, copy=False),
                "cucim_raw":        cucim.astype(np.float32, copy=False),
                "original_metrics": _compute_bg_metrics(raw),
                "tophat_metrics":   _compute_bg_metrics(tophat),
                "cucim_metrics":    _compute_bg_metrics(cucim),
            }
            self.finished.emit(self.request_id, payload)
        except Exception:
            if not self._stop:
                self.error.emit(self.request_id, traceback.format_exc())



class BatchProcessWorker(QThread):
    """批量背景校正计算引擎。
    
    策略：串行IO（逐patch读磁盘）+ 串行GPU计算（cupy/cupyx非线程安全）
    - IO串行避免机械硬盘随机读的性能损失
    - GPU计算串行：cupy多线程并发会导致结果混乱，GPU本身有内部并行性
    - 每个(channel, patch)计算完后emit channel_patch_done信号
    - 一个channel的所有patches都完成后emit channel_done信号
    
    methods参数：{channel: "original"|"tophat"|"cucim"|"both"}
    """
    channel_patch_done = pyqtSignal(str, int, dict)   # (channel, patch_idx, payload)
    channel_done       = pyqtSignal(str)               # channel全部patches完成
    all_done           = pyqtSignal()
    progress           = pyqtSignal(int, int, str)     # (done, total, msg)
    error_signal       = pyqtSignal(str, int, str)     # (channel, patch_idx, msg)
    canceled           = pyqtSignal()

    def __init__(self, loader, patches, methods, nucleus_channel,
                 tophat_radius, cucim_sigma, max_gpu_workers=4, parent=None):
        super().__init__(parent)
        self.loader           = loader
        self.patches          = list(patches)
        self.methods          = dict(methods)          # {ch: "original"|"tophat"|"cucim"|"both"}
        self.nucleus_channel  = nucleus_channel
        self.tophat_radius    = int(tophat_radius)
        self.cucim_sigma      = int(cucim_sigma)
        self.max_gpu_workers  = max_gpu_workers
        self._stop            = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            channels = list(self.methods.keys())
            n_patches = len(self.patches)
            print(f"[BatchProcessWorker] start: channels={channels} n_patches={n_patches}")
            total_units = sum(
                n_patches * (2 if m == "both" else 1)
                for m in self.methods.values()
            )
            done_units = 0

            # 先一次性读取nucleus（所有patches）
            nuc_raws = {}
            if self.nucleus_channel and self.nucleus_channel in self.loader.ch_map:
                for p_idx, roi in enumerate(self.patches):
                    if self._stop:
                        self.canceled.emit(); return
                    y0, y1, x0, x1 = roi
                    try:
                        nuc_raws[p_idx] = self.loader.read_region(
                            self.nucleus_channel, y0, y1, x0, x1,
                            downsample=1, correction_config={}, normalize=False)
                    except Exception:
                        nuc_raws[p_idx] = None

            # 逐通道处理
            for ch in channels:
                if self._stop:
                    self.canceled.emit(); return
                method = self.methods[ch]

                # 读取该通道所有patches的raw数据（串行IO）
                raws = {}
                for p_idx, roi in enumerate(self.patches):
                    if self._stop:
                        self.canceled.emit(); return
                    y0, y1, x0, x1 = roi
                    self.progress.emit(
                        done_units, total_units,
                        f"Reading {ch}  P{p_idx+1}/{n_patches}…")
                    try:
                        raws[p_idx] = self.loader.read_region(
                            ch, y0, y1, x0, x1,
                            downsample=1, correction_config={}, normalize=False)
                    except Exception as e:
                        self.error_signal.emit(ch, p_idx, str(e))
                        raws[p_idx] = None

                if self._stop:
                    self.canceled.emit(); return

                # GPU计算（并行，max_gpu_workers个同时跑）
                # 用默认参数固定捕获method和raws，防止闭包引用变化
                def _compute_one(p_idx, _method=method, _raws=raws):
                    if self._stop:
                        return p_idx, None
                    raw = _raws.get(p_idx)
                    if raw is None:
                        return p_idx, None
                    nuc_raw  = nuc_raws.get(p_idx)
                    nuc_norm = OMETIFFLoader._norm(nuc_raw) if nuc_raw is not None else None
                    orig_norm = OMETIFFLoader._norm(raw)

                    tophat_norm = cucim_norm = None
                    print(f"[DEBUG _compute_one] p_idx={p_idx} _method={_method!r}")
                    if _method in ("tophat", "both"):
                        th = _apply_tophat_gpu_or_cpu(raw, self.tophat_radius)
                        tophat_norm = OMETIFFLoader._norm(th)
                        tophat_raw  = th
                        print(f"[DEBUG] tophat done, norm shape={tophat_norm.shape}")
                    else:
                        tophat_raw = None
                    if _method in ("cucim", "both"):
                        print(f"[DEBUG] starting cucim...")
                        cu = _apply_cucim_or_cpu(raw, self.cucim_sigma, prefer_gpu=CUCIM_AVAILABLE)
                        cucim_norm = OMETIFFLoader._norm(cu)
                        cucim_raw  = cu
                        print(f"[DEBUG] cucim done, norm shape={cucim_norm.shape}")
                    else:
                        cucim_raw = None
                        print(f"[DEBUG] cucim skipped")
                    print(f"[DEBUG] payload cucim_norm is None: {cucim_norm is None}")

                    def _mk_rgb(mono, nuc):
                        if mono is None:
                            return None
                        r = np.zeros_like(mono, dtype=np.float32)
                        g = mono.astype(np.float32, copy=False)
                        b = nuc.astype(np.float32, copy=False) if nuc is not None else np.zeros_like(mono, dtype=np.float32)
                        return np.stack([r, g, b], axis=-1)

                    payload = {
                        "method":           _method,
                        "original_disp":    orig_norm,
                        "tophat_disp":      tophat_norm,
                        "cucim_disp":       cucim_norm,
                        "nucleus_disp":     nuc_norm,
                        "original_rgb":     _mk_rgb(orig_norm,   nuc_norm),
                        "tophat_rgb":       _mk_rgb(tophat_norm, nuc_norm),
                        "cucim_rgb":        _mk_rgb(cucim_norm,  nuc_norm),
                        "original_raw":     raw.astype(np.float32, copy=False),
                        "tophat_raw":       tophat_raw.astype(np.float32, copy=False) if tophat_raw is not None else None,
                        "cucim_raw":        cucim_raw.astype(np.float32, copy=False)  if cucim_raw  is not None else None,
                        "original_metrics": _compute_bg_metrics(raw),
                        "tophat_metrics":   _compute_bg_metrics(tophat_raw) if tophat_raw is not None else {"snr":0.0,"bg_cv":0.0},
                        "cucim_metrics":    _compute_bg_metrics(cucim_raw)  if cucim_raw  is not None else {"snr":0.0,"bg_cv":0.0},
                    }
                    return p_idx, payload

                patch_indices = list(range(n_patches))
                for pi in patch_indices:
                    if self._stop:
                        self.canceled.emit(); return
                    p_idx, payload = _compute_one(pi)
                    if payload is not None:
                        self.channel_patch_done.emit(ch, p_idx, payload)
                    done_units += 1
                    self.progress.emit(
                        done_units, total_units,
                        f"Done {ch}  P{p_idx+1}/{n_patches}")

                self.channel_done.emit(ch)

            self.all_done.emit()

        except Exception:
            import traceback as _tb
            self.error_signal.emit("__global__", -1, _tb.format_exc())

class BackgroundPreviewWorker(QThread):
    finished = pyqtSignal(int, dict)
    error = pyqtSignal(int, str)

    def __init__(self, request_id, loader, channel_name, roi,
                 tophat_radius, cucim_sigma, nucleus_channel=None,
                 raw_cache=None, nuc_raw_cache=None):
        super().__init__()
        self.request_id      = request_id
        self.loader          = loader
        self.channel_name    = channel_name
        self.nucleus_channel = nucleus_channel
        self.roi             = roi
        self.tophat_radius   = int(tophat_radius)
        self.cucim_sigma     = int(cucim_sigma)
        self.raw_cache       = raw_cache or {}
        self.nuc_raw_cache   = nuc_raw_cache or {}
        self._stop           = False

    def stop(self):
        self._stop = True

    @staticmethod
    def _make_rgb(marker_norm, nucleus_norm):
        """marker → 绿色通道，nucleus → 蓝色通道，RGB float32 [0,1]"""
        r = np.zeros_like(marker_norm, dtype=np.float32)
        g = marker_norm.astype(np.float32, copy=False)
        b = nucleus_norm.astype(np.float32, copy=False) if nucleus_norm is not None \
            else np.zeros_like(marker_norm, dtype=np.float32)
        return np.stack([r, g, b], axis=-1)

    def run(self):
        try:
            if self.channel_name not in self.loader.ch_map:
                raise KeyError(f"Channel '{self.channel_name}' not found")
            y0, y1, x0, x1 = self.roi
            patch_key = (self.channel_name, id(self.roi))

            # 优先从raw缓存读取，避免重复IO
            raw = self.raw_cache.get(self.channel_name)
            if raw is None:
                raw = self.loader.read_region(
                    self.channel_name, y0, y1, x0, x1,
                    downsample=1, correction_config={}, normalize=False,
                )
                self.raw_cache[self.channel_name] = raw
            if self._stop:
                return

            # 核通道：优先从缓存，缺失时读磁盘
            nuc_norm = None
            if self.nucleus_channel and self.nucleus_channel in self.loader.ch_map:
                nuc_key = "nuc"
                nuc_raw = self.nuc_raw_cache.get(nuc_key)
                if nuc_raw is None:
                    nuc_raw = self.loader.read_region(
                        self.nucleus_channel, y0, y1, x0, x1,
                        downsample=1, correction_config={}, normalize=False,
                    )
                    self.nuc_raw_cache[nuc_key] = nuc_raw
                nuc_norm = OMETIFFLoader._norm(nuc_raw)
            if self._stop:
                return

            # GPU tophat（prefer_gpu=True，走_apply_tophat_gpu_or_cpu）
            tophat = _apply_background_method_tiled(
                raw, "tophat", radius=self.tophat_radius, prefer_gpu=True)
            if self._stop:
                return
            cucim = _apply_background_method_tiled(
                raw, "cucim", sigma=self.cucim_sigma, prefer_gpu=CUCIM_AVAILABLE)
            if self._stop:
                return

            orig_norm   = OMETIFFLoader._norm(raw)
            tophat_norm = OMETIFFLoader._norm(tophat)
            cucim_norm  = OMETIFFLoader._norm(cucim)

            payload = {
                # 灰度（单通道显示用）
                "original_disp": orig_norm,
                "tophat_disp":   tophat_norm,
                "cucim_disp":    cucim_norm,
                "nucleus_disp":  nuc_norm,
                # RGB overlay（marker绿+nucleus蓝）
                "original_rgb":  self._make_rgb(orig_norm,   nuc_norm),
                "tophat_rgb":    self._make_rgb(tophat_norm,  nuc_norm),
                "cucim_rgb":     self._make_rgb(cucim_norm,   nuc_norm),
                # raw 用于 metrics
                "original_raw":  raw.astype(np.float32, copy=False),
                "tophat_raw":    tophat.astype(np.float32, copy=False),
                "cucim_raw":     cucim.astype(np.float32, copy=False),
                "original_metrics": _compute_bg_metrics(raw),
                "tophat_metrics":   _compute_bg_metrics(tophat),
                "cucim_metrics":    _compute_bg_metrics(cucim),
            }
            self.finished.emit(self.request_id, payload)
        except Exception:
            if not self._stop:
                self.error.emit(self.request_id, traceback.format_exc())


class WsiCorrectionWorker(QThread):
    progress = pyqtSignal(int, int, int, int, str, str, int)
    finished = pyqtSignal(str, dict)
    canceled = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, loader, output_dir, correction_config, rois=None, parent=None):
        super().__init__(parent)
        self.loader = loader
        self.output_dir = output_dir
        self.correction_config = _normalize_correction_config(correction_config) or {}
        self.rois = list(rois or [])
        self._cancel_requested = False

    def stop_after_current_channel(self):
        self._cancel_requested = True

    @staticmethod
    def _safe_group_name(name, idx):
        name = str(name or f"ROI_{idx}").strip() or f"ROI_{idx}"
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)
        return safe or f"ROI_{idx}"

    @staticmethod
    def _poly_mask(polygon_fullres, bbox_y0, bbox_x0, h, w):
        import cv2 as _cv2
        mask = np.zeros((h, w), dtype=np.uint8)
        pts = np.array(
            [[int(x - bbox_x0), int(y - bbox_y0)] for x, y in polygon_fullres],
            dtype=np.int32,
        )
        _cv2.fillPoly(mask, [pts], color=1)
        return mask.astype(bool)

    def run(self):
        try:
            rois = [
                roi for roi in self.rois
                if roi.get("bbox_fullres") and len(roi.get("bbox_fullres")) == 4
            ]
            print("[WsiCorrectionWorker] mode=roi_only")
            print(f"[WsiCorrectionWorker] n_rois={len(rois)}")
            if not rois:
                self.error.emit("No ROI found. Draw ROI first.")
                return

            decisions = dict((self.correction_config.get("channel_decisions") or {}))
            params = dict((self.correction_config.get("method_params") or {}))
            channels = [
                (
                    ch,
                    method,
                    int(
                        params.get(
                            "tophat_radius" if method == "tophat" else "cucim_sigma",
                            TOPHAT_RADIUS_DEFAULT if method == "tophat" else CUCIM_SIGMA_DEFAULT,
                        )
                    ),
                )
                for ch, method in decisions.items()
                if method in {"tophat", "cucim"} and ch in self.loader.ch_map
            ]
            zarr_path = os.path.join(self.output_dir, "corrected_channels.zarr")

            if os.path.exists(zarr_path):
                shutil.rmtree(zarr_path, ignore_errors=True)

            if not channels:
                self.finished.emit("", {})
                return

            os.makedirs(self.output_dir, exist_ok=True)
            root = zarr.open_group(zarr_path, mode="w")
            root.attrs["mode"] = "roi_only"
            root.attrs["source_ome"] = os.path.abspath(getattr(self.loader, "filepath", "") or "")
            root.attrs["output_dir"] = os.path.abspath(self.output_dir)
            root.attrs["created_by"] = "Step0"
            root.attrs["roi_names"] = [str(r.get("name") or f"ROI_{i}") for i, r in enumerate(rois, start=1)]
            root.attrs["correction_config"] = self.correction_config
            corrected_decisions = {}
            full_h, full_w = self.loader.shape
            roi_infos = []
            used_group_names = set()
            for idx, roi in enumerate(rois, start=1):
                y0, y1, x0, x1 = [int(v) for v in roi["bbox_fullres"]]
                y0 = max(0, min(full_h, y0)); y1 = max(0, min(full_h, y1))
                x0 = max(0, min(full_w, x0)); x1 = max(0, min(full_w, x1))
                if y1 <= y0 or x1 <= x0:
                    continue
                base_group_name = self._safe_group_name(roi.get("name"), idx)
                group_name = base_group_name
                suffix = 2
                while group_name in used_group_names:
                    group_name = f"{base_group_name}_{suffix}"
                    suffix += 1
                used_group_names.add(group_name)
                roi_h, roi_w = y1 - y0, x1 - x0
                print(f"[WsiCorrectionWorker] {group_name} bbox={[y0, y1, x0, x1]} shape={(roi_h, roi_w)}")
                roi_infos.append({
                    "idx": idx,
                    "group_name": group_name,
                    "roi_name": str(roi.get("name") or group_name),
                    "bbox": [y0, y1, x0, x1],
                    "shape": (roi_h, roi_w),
                    "polygon_fullres": roi.get("polygon_fullres"),
                })

            if not roi_infos:
                self.error.emit("No ROI found. Draw ROI first.")
                return
            root.attrs["roi_names"] = [info["roi_name"] for info in roi_infos]

            channel_total = len(channels) * len(roi_infos)
            tile_counts = []
            for info in roi_infos:
                roi_h, roi_w = info["shape"]
                for _, _, param in channels:
                    tile_counts.append(len(list(_tile_slices(roi_h, roi_w, 4096, max(1, 2 * param)))))
            total_units = sum(tile_counts)
            started = time.time()
            completed_units = 0
            progress_idx = 0

            for info in roi_infos:
                group = root.create_group(info["group_name"], overwrite=True)
                group.attrs["roi_name"] = info["roi_name"]
                group.attrs["bbox_fullres"] = info["bbox"]
                group.attrs["shape"] = [int(roi_h), int(roi_w)]
                if info["polygon_fullres"] is not None:
                    group.attrs["polygon_fullres"] = info["polygon_fullres"]
                roi_y0, roi_y1, roi_x0, roi_x1 = info["bbox"]
                roi_h, roi_w = info["shape"]
                poly_mask = None
                if info["polygon_fullres"]:
                    poly_mask = self._poly_mask(info["polygon_fullres"], roi_y0, roi_x0, roi_h, roi_w)

                for ch_name, method, param in channels:
                    progress_idx += 1
                    print(f"[WsiCorrectionWorker] processing channel={ch_name} method={method}")
                    overlap = max(1, 2 * param)
                    tiles = list(_tile_slices(roi_h, roi_w, 4096, overlap))
                    ds = group.create_dataset(
                        ch_name,
                        shape=(roi_h, roi_w),
                        chunks=(min(1024, roi_h), min(1024, roi_w)),
                        dtype=np.float32,
                        overwrite=True,
                    )

                    for tile_idx, (core, padded, crop) in enumerate(tiles, start=1):
                        y0, y1, x0, x1 = core
                        py0, py1, px0, px1 = padded
                        cy0, cy1, cx0, cx1 = crop
                        raw = self.loader._read_roi_zarr(
                            self.loader.ch_map[ch_name],
                            roi_y0 + py0, roi_y0 + py1,
                            roi_x0 + px0, roi_x0 + px1,
                        ).astype(np.float32, copy=False)
                        if method == "tophat":
                            corr = _apply_tophat_cpu(raw, param)
                        else:
                            corr = _apply_cucim_or_cpu(raw, param, prefer_gpu=CUCIM_AVAILABLE)
                        out = corr[cy0:cy1, cx0:cx1].astype(np.float32, copy=False)
                        if poly_mask is not None:
                            out = out.copy()
                            out[~poly_mask[y0:y1, x0:x1]] = 0
                        ds[y0:y1, x0:x1] = out
                        elapsed = max(0.001, time.time() - started)
                        done_units = completed_units + tile_idx
                        remain = int((total_units - done_units) * (elapsed / done_units))
                        self.progress.emit(
                            progress_idx,
                            channel_total,
                            tile_idx,
                            len(tiles),
                            f"{info['group_name']}/{ch_name}",
                            method,
                            remain,
                        )

                    corrected_decisions[ch_name] = method
                    completed_units += len(tiles)
                    if self._cancel_requested:
                        shutil.rmtree(zarr_path, ignore_errors=True)
                        self.canceled.emit(zarr_path)
                        return

            self.finished.emit(zarr_path, corrected_decisions)
        except Exception:
            self.error.emit(traceback.format_exc())


class _WsiCorrectionProgressDialog(QDialog):
    cancel_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._allow_close = False
        self.setModal(True)
        self.setWindowTitle("Background Correction")
        self.setWindowFlags(Qt.Dialog | Qt.CustomizeWindowHint | Qt.WindowTitleHint)
        self.setMinimumWidth(520)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        self._label = QLabel("Preparing full-WSI correction…")
        self._label.setWordWrap(True)
        self._label.setStyleSheet("color:#ddd;font-size:12px;")
        lay.addWidget(self._label)

        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setStyleSheet(
            "QProgressBar{border:1px solid #444;border-radius:4px;background:#111;color:#ddd;}"
            "QProgressBar::chunk{background:#2a82da;border-radius:3px;}"
        )
        lay.addWidget(self._bar)

        self._eta = QLabel("Estimated time remaining: —")
        self._eta.setStyleSheet("color:#aaa;font-size:11px;")
        lay.addWidget(self._eta)

        self._cancel = QPushButton("Cancel")
        self._cancel.setStyleSheet(
            "QPushButton{background:#722;color:white;border-radius:4px;padding:6px 18px;}"
            "QPushButton:hover{background:#944;}"
        )
        self._cancel.clicked.connect(self._on_cancel)
        lay.addWidget(self._cancel, alignment=Qt.AlignRight)

    def _on_cancel(self):
        self._cancel.setEnabled(False)
        self._label.setText("Cancellation requested. The current channel will finish before cleanup.")
        self.cancel_requested.emit()

    def set_progress(self, pct, label, eta_s):
        self._bar.setValue(int(np.clip(pct, 0, 100)))
        self._label.setText(label)
        if eta_s is None or eta_s < 0:
            self._eta.setText("Estimated time remaining: —")
        else:
            mm, ss = divmod(int(eta_s), 60)
            hh, mm = divmod(mm, 60)
            if hh > 0:
                eta_txt = f"{hh:d}h {mm:02d}m {ss:02d}s"
            else:
                eta_txt = f"{mm:d}m {ss:02d}s"
            self._eta.setText(f"Estimated time remaining: {eta_txt}")

    def allow_close(self):
        self._allow_close = True
        self._cancel.setEnabled(False)

    def reject(self):
        if self._allow_close:
            super().reject()

    def closeEvent(self, event):
        if self._allow_close:
            super().closeEvent(event)
        else:
            event.ignore()
