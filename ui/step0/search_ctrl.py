"""
block01/ui/step0/search_ctrl.py — SearchCtrlPanel and background workers.
"""

import os
import gc
import json
import time
import traceback
import uuid

import numpy as np
import tifffile
import zarr

from ...qt import QtWidgets, QtCore
from ...qt.QtCore import Qt, QThread, Signal
from ...qt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QGroupBox, QDoubleSpinBox, QProgressBar, QComboBox,
    QMessageBox, QFileDialog, QDialog, QCheckBox,
)
import pyqtgraph as pg

from ...config import (
    OUTPUT_DIR, TOPHAT_RADIUS_DEFAULT, CUCIM_SIGMA_DEFAULT,
    TOPHAT_RADIUS_RANGE, CUCIM_SIGMA_RANGE, BG_CORR_MAX_TILE,
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
    method_overlap,
    BG_CORRECTION_ALGO_VERSION,
    GpuBackendFailed,
    compute_path,
    compute_impl,
    legacy_compute_impl,
    CpuImplUnavailable,
    GAUSSIAN_IMPL_SCIPY,
    GAUSSIAN_IMPL_OPENCV,
    correct_tile,
    tophat_footprint,
    resolve_effective_correction_params,
    stamp_corrected_channel_identity,
)
from ...core.bg_parallel import (MEM_PER_TILE_BYTES, MEM_PER_TILE_FAST_BYTES, choose_workers,
                                 mem_available_bytes, ordered_results)
from ..theme import widgets as theme_widgets
from ...core import product_swap
from ...core.io_loader import OMETIFFLoader
from ...viewer import step1_source as sources
from ...viewer import display_pyramid
from ...utils.segmentation_config import (
    CELLPOSE_NUCLEI_DAPI,
    CELLPOSE_NUCLEI_EXPANSION,
    CELLPOSE_WHOLECELL_FUSION,
    STARDIST_NUCLEI_DAPI,
    STARDIST_NUCLEI_EXPANSION,
    MESMER_WHOLE_CELL,
    MESMER_NUCLEI,
    MESMER_NUCLEAR_GUIDED,
    available_segmentation_methods,
    get_segmentation_method_config,
    normalize_segmentation_config,
)
from ...utils.mesmer_utils import parse_channel_list

class SearchCtrlPanel(QWidget):
    """
    Step1's segmentation-method parameters (off the screen since block E,
    2026-09-25): the method box, a loaded params file and the manual values
    ("Use These Params"). It runs nothing -- Phase 1 / Phase 2 and the
    in-process patch preview were removed (Windows plan W2); segmentation
    runs in the seg_runner engine (Step1 pre-segmentation, Step2).
    """

    params_ready = Signal(dict)
    method_changed = Signal(str)

    def __init__(self):
        super().__init__()
        self.setMinimumSize(300, 390)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
        self._setup_ui()

    def sizeHint(self):
        hint = self.layout().sizeHint() if self.layout() is not None else QtCore.QSize(420, 460)
        return QtCore.QSize(max(420, hint.width()), max(460, hint.height()))

    def minimumSizeHint(self):
        hint = self.layout().minimumSize() if self.layout() is not None else QtCore.QSize(300, 390)
        return QtCore.QSize(max(300, hint.width()), max(390, hint.height()))

    def _setup_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(5)
        lay.setSizeConstraint(QtWidgets.QLayout.SetMinimumSize)

        method_box = QGroupBox("Segmentation Method")
        method_box.setMinimumHeight(86)
        method_box.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        from ..step1_button_styles import SECTION_BOX_QSS
        method_box.setStyleSheet(SECTION_BOX_QSS)       # the theme's neutral box
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

        # ── Load existing params ───────────────────────────────────────
        load_box = QGroupBox("Load existing cellpose_params.json")
        load_box.setStyleSheet(
            "QGroupBox{border:1px solid #666;border-radius:4px;margin-top:16px;"
            "font-weight:bold;color:#ccc;font-size:11px;}"
            "QGroupBox::title{subcontrol-origin:margin;subcontrol-position:"
            "top left;left:8px;padding:0 4px;}"
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
            "QGroupBox{border:1px solid #666;border-radius:4px;margin-top:16px;"
            "font-weight:bold;color:#ccc;font-size:11px;}"
            "QGroupBox::title{subcontrol-origin:margin;subcontrol-position:"
            "top left;left:8px;padding:0 4px;}"
        )
        ml = QVBoxLayout(man_box)
        ml.setContentsMargins(8, 4, 8, 4)    # compact: ~2/3 the old height
        ml.setSpacing(2)

        def _spin_row(label, lo, hi, step, dec, val):
            r = QHBoxLayout()
            l = QLabel(label)
            l.setFixedWidth(130)
            l.setMinimumHeight(18)
            r.addWidget(l)
            sp = QDoubleSpinBox()
            sp.setRange(lo, hi); sp.setSingleStep(step)
            sp.setDecimals(dec); sp.setValue(val)
            sp.setStyleSheet("font-size:11px;")
            sp.setMinimumHeight(18)
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
        sd_model_lbl.setMinimumHeight(18)
        sd_model_row.addWidget(sd_model_lbl)
        self._sd_model = QtWidgets.QLineEdit("2D_versatile_fluo")
        self._sd_model.setStyleSheet("font-size:11px;")
        self._sd_model.setMinimumHeight(18)
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

        self._build_mesmer_params_panel()
        lay.insertWidget(1, self.mesmer_params_panel)

        self.btn_use_manual = QPushButton("✓ Use These Params")
        self.btn_use_manual.setStyleSheet(
            "QPushButton{background:#444;color:#ccc;"
            "border-radius:4px;padding:4px;font-size:11px;}"
            "QPushButton:hover{background:#258;color:white;}"
        )
        self.btn_use_manual.clicked.connect(self._use_manual_params)
        ml.addWidget(self.btn_use_manual)

        self._manual_params_scroll = man_box
        self._manual_params_scroll.setMinimumHeight(0)
        self._manual_params_scroll.setMaximumHeight(16777215)
        # Hug content vertically (was MinimumExpanding, which grabbed leftover
        # space and opened a big gap above 'Use These Params').
        self._manual_params_scroll.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Preferred,
        )
        lay.addWidget(man_box)
        lay.addStretch(1)   # absorb leftover space below the box, not inside it
        self._on_method_changed()

    def _build_mesmer_params_panel(self):
        self.mesmer_params_panel = QGroupBox("Mesmer parameters")
        self.mesmer_params_panel.setMinimumHeight(210)
        self.mesmer_params_panel.setMaximumHeight(390)
        self.mesmer_params_panel.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Preferred,
        )
        self.mesmer_params_panel.setStyleSheet(
            "QGroupBox{border:1px solid #56b6c2;border-radius:4px;margin-top:16px;"
            "font-weight:bold;color:#56b6c2;font-size:11px;}"
            "QGroupBox::title{subcontrol-origin:margin;subcontrol-position:top left;"
            "left:8px;padding:0 4px;}"
        )
        outer = QVBoxLayout(self.mesmer_params_panel)
        grid = QtWidgets.QGridLayout()
        outer.addLayout(grid)

        def add(row, label, widget):
            lbl = QLabel(label)
            lbl.setMinimumWidth(130)
            lbl.setStyleSheet("font-size:10px;color:#bbb;")
            widget.setMinimumHeight(18)
            grid.addWidget(lbl, row, 0)
            grid.addWidget(widget, row, 1)
            return widget

        self._mesmer_nuclear_channel = add(0, "nuclear_channel:", QtWidgets.QLineEdit("DAPI"))
        self._mesmer_membrane_channels = add(1, "membrane_channels:", QtWidgets.QLineEdit())
        self._mesmer_membrane_channels.setPlaceholderText("PanCK;CD45;CD68;HLA-DR")
        self._mesmer_input_mode = add(2, "input_mode:", QComboBox())
        for label, value in (
            ("DAPI only", "DAPI only"),
            ("DAPI + Fusion channel", "step1_weighted_fusion"),
            ("DAPI + membrane channels", "selected_channels"),
            ("selected channels", "selected_channels"),
        ):
            self._mesmer_input_mode.addItem(label, value)
        self._mesmer_use_gpu = add(3, "use_gpu:", QComboBox())
        for label, value in (("Auto", "auto"), ("GPU", "gpu"), ("CPU", "cpu")):
            self._mesmer_use_gpu.addItem(label, value)
        self._mesmer_tile_size = add(4, "tile_size:", QDoubleSpinBox())
        self._mesmer_tile_size.setRange(128, 8192)
        self._mesmer_tile_size.setSingleStep(128)
        self._mesmer_tile_size.setDecimals(0)
        self._mesmer_tile_size.setValue(2048)
        self._mesmer_overlap = add(5, "overlap:", QDoubleSpinBox())
        self._mesmer_overlap.setRange(0, 1024)
        self._mesmer_overlap.setSingleStep(32)
        self._mesmer_overlap.setDecimals(0)
        self._mesmer_overlap.setValue(128)
        self._mesmer_batch_size = add(6, "batch_size:", QDoubleSpinBox())
        self._mesmer_batch_size.setRange(1, 32)
        self._mesmer_batch_size.setDecimals(0)
        self._mesmer_batch_size.setValue(1)
        self._mesmer_mpp = add(7, "image_mpp:", QDoubleSpinBox())
        self._mesmer_mpp.setRange(0.01, 10)
        self._mesmer_mpp.setSingleStep(0.05)
        self._mesmer_mpp.setDecimals(3)
        self._mesmer_mpp.setValue(0.5)
        self._mesmer_norm = add(8, "normalize_input:", QCheckBox("True"))
        self._mesmer_norm.setChecked(True)
        self._mesmer_low = add(9, "percentile_low:", QDoubleSpinBox())
        self._mesmer_low.setRange(0, 50)
        self._mesmer_low.setValue(1.0)
        self._mesmer_high = add(10, "percentile_high:", QDoubleSpinBox())
        self._mesmer_high.setRange(50, 100)
        self._mesmer_high.setValue(99.8)
        self._mesmer_min_size = add(11, "postprocess_min_size:", QDoubleSpinBox())
        self._mesmer_min_size.setRange(0, 100000)
        self._mesmer_min_size.setDecimals(0)
        self._mesmer_min_size.setValue(0)
        self._mesmer_save_intermediate = add(12, "save_intermediate:", QCheckBox("False"))
        self.mesmer_params_panel.setVisible(False)

    def collect_mesmer_params(self):
        method = self._selected_method()
        mode = {
            MESMER_WHOLE_CELL: "whole_cell",
            MESMER_NUCLEI: "nuclei",
            MESMER_NUCLEAR_GUIDED: "nuclear_guided",
        }.get(method, "whole_cell")
        return {
            "method": method,
            "mesmer_mode": mode,
            "nuclear_channel": self._mesmer_nuclear_channel.text().strip() or "DAPI",
            "membrane_channels": parse_channel_list(self._mesmer_membrane_channels.text()),
            "cytoplasm_channel": "",
            "input_mode": self._mesmer_input_mode.currentData() or "selected_channels",
            "compartment": "nuclear" if method == MESMER_NUCLEI else "whole-cell",
            "image_mpp": self._mesmer_mpp.value(),
            "pixel_size": self._mesmer_mpp.value(),
            "batch_size": int(self._mesmer_batch_size.value()),
            "tile_size": int(self._mesmer_tile_size.value()),
            "overlap": int(self._mesmer_overlap.value()),
            "use_gpu": self._mesmer_use_gpu.currentData() or "auto",
            "normalize_input": self._mesmer_norm.isChecked(),
            "percentile_low": self._mesmer_low.value(),
            "percentile_high": self._mesmer_high.value(),
            "postprocess_min_size": int(self._mesmer_min_size.value()),
            "save_intermediate": self._mesmer_save_intermediate.isChecked(),
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

    def check_method_offered(self, method):
        """Raise ValueError unless the method box offers `method`: an unlisted
        one is refused, never silently replaced by the method on screen."""
        if self._method_combo.findData(method) < 0:
            raise ValueError(f"Unknown segmentation method: {method}")

    def apply_seg_config_to_ui(self, cfg):
        """Restore method-specific parameter widgets from a saved config."""
        p = normalize_segmentation_config(cfg or {})
        method = p.get("method") or CELLPOSE_WHOLECELL_FUSION
        self.check_method_offered(method)           # before any widget changes
        idx = self._method_combo.findData(method)
        if idx >= 0:
            self._method_combo.setCurrentIndex(idx)
        self._man_diam.setValue(float(p.get("diameter") or 0))
        self._man_flow.setValue(float(p.get("flow_threshold", 0.4)))
        self._man_prob.setValue(float(p.get("cellprob_threshold", 0.0)))
        if p.get("expand_distance") is not None:
            self._sd_expand_manual.setValue(float(p.get("expand_distance")))
        if method in (MESMER_WHOLE_CELL, MESMER_NUCLEI, MESMER_NUCLEAR_GUIDED):
            self._mesmer_nuclear_channel.setText(str(p.get("nuclear_channel", "DAPI") or "DAPI"))
            self._mesmer_membrane_channels.setText(";".join(parse_channel_list(p.get("membrane_channels") or [])))
            idx = self._mesmer_input_mode.findData(p.get("input_mode", "selected_channels"))
            self._mesmer_input_mode.setCurrentIndex(max(0, idx))
            idx = self._mesmer_use_gpu.findData(str(p.get("use_gpu", "auto")).lower())
            self._mesmer_use_gpu.setCurrentIndex(max(0, idx))
            self._mesmer_tile_size.setValue(float(p.get("tile_size", 2048) or 2048))
            self._mesmer_overlap.setValue(float(p.get("overlap", 128) or 128))
            self._mesmer_batch_size.setValue(float(p.get("batch_size", 1) or 1))
            self._mesmer_mpp.setValue(float(p.get("image_mpp", p.get("pixel_size", 0.5)) or 0.5))
            self._mesmer_norm.setChecked(bool(p.get("normalize_input", True)))
            self._mesmer_low.setValue(float(p.get("percentile_low", 1.0)))
            self._mesmer_high.setValue(float(p.get("percentile_high", 99.8)))
            self._mesmer_min_size.setValue(float(p.get("postprocess_min_size", 0) or 0))
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

    def get_current_params(self):
        """Return spinbox values regardless of source."""
        method = self._selected_method()
        if method in (MESMER_WHOLE_CELL, MESMER_NUCLEI, MESMER_NUCLEAR_GUIDED):
            params = self.collect_mesmer_params()
            return normalize_segmentation_config({"method": method, "params": params, **params})
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
        if method in (MESMER_WHOLE_CELL, MESMER_NUCLEI, MESMER_NUCLEAR_GUIDED):
            params.update(self.collect_mesmer_params())
        return {"method": method, "params": params}

    def _selected_method(self):
        method = self._method_combo.currentData() or CELLPOSE_WHOLECELL_FUSION
        return method

    def _on_method_changed(self):
        method = self._selected_method()
        cfg = get_segmentation_method_config(method)
        is_cellpose = method in (CELLPOSE_WHOLECELL_FUSION, CELLPOSE_NUCLEI_DAPI, CELLPOSE_NUCLEI_EXPANSION)
        is_stardist = method in (STARDIST_NUCLEI_DAPI, STARDIST_NUCLEI_EXPANSION)
        is_expansion = method in (CELLPOSE_NUCLEI_EXPANSION, STARDIST_NUCLEI_EXPANSION)
        is_mesmer = method in (MESMER_WHOLE_CELL, MESMER_NUCLEI, MESMER_NUCLEAR_GUIDED)
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
        if getattr(self, "mesmer_params_panel", None) is not None:
            self.mesmer_params_panel.setVisible(is_mesmer)
        self._expand_dist.setEnabled(is_expansion)
        self._expand_dist.setVisible(False)
        self._expand_dist_label.setVisible(False)
        self._method_hint.setText(
            f"{method}  |  input={cfg.get('input_type')}  output={cfg.get('output_type')}"
        )
        self._dapi_only_note.setVisible(method != CELLPOSE_WHOLECELL_FUSION)
        workflow = {
            CELLPOSE_WHOLECELL_FUSION: "wholecell_phase1_phase2",
            CELLPOSE_NUCLEI_DAPI: "nuclei_cellpose",
            CELLPOSE_NUCLEI_EXPANSION: "nuclei_cellpose_expansion",
            STARDIST_NUCLEI_DAPI: "stardist",
            STARDIST_NUCLEI_EXPANSION: "stardist_expansion",
            MESMER_WHOLE_CELL: "mesmer_whole_cell_patch_preview",
            MESMER_NUCLEI: "mesmer_nuclei_patch_preview",
            MESMER_NUCLEAR_GUIDED: "mesmer_nuclear_guided_patch_preview",
        }.get(method, "unknown")
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
    job_finished = Signal(str, int, object)   # (channel, patch_idx, raw_array)
    error    = Signal(str, int, str)      # (channel, patch_idx, msg)

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
            self.job_finished.emit(self.channel_name, self.patch_idx, raw)
        except Exception:
            if not self._stop:
                self.error.emit(self.channel_name, self.patch_idx, traceback.format_exc())


class BgComputeWorker(QThread):
    """纯GPU计算worker：接受已读好的raw数组，不做任何IO。
    tophat + cucim + norm + metrics，全部在GPU上完成。
    """
    job_finished = Signal(int, dict)
    error    = Signal(int, str)

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
                "nucleus_raw":      (self.nuc_raw.astype(np.float32, copy=False)
                                     if self.nuc_raw is not None else None),
                "original_metrics": _compute_bg_metrics(raw),
                "tophat_metrics":   _compute_bg_metrics(tophat),
                "cucim_metrics":    _compute_bg_metrics(cucim),
            }
            self.job_finished.emit(self.request_id, payload)
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
    channel_patch_done = Signal(str, int, dict)   # (channel, patch_idx, payload)
    channel_done       = Signal(str)               # channel全部patches完成
    all_done           = Signal()
    progress           = Signal(int, int, str)     # (done, total, msg)
    error_signal       = Signal(str, int, str)     # (channel, patch_idx, msg)
    canceled           = Signal()

    def __init__(self, loader, patches, methods, nucleus_channel,
                 tophat_radius, cucim_sigma, channel_params=None,
                 max_gpu_workers=4, parent=None):
        super().__init__(parent)
        self.loader           = loader
        self.patches          = list(patches)
        self.methods          = dict(methods)          # {ch: "original"|"tophat"|"cucim"|"both"}
        self.nucleus_channel  = nucleus_channel
        self.tophat_radius    = int(tophat_radius)
        self.cucim_sigma      = int(cucim_sigma)
        # Optional per-channel param overrides {ch: {"tophat_radius", "cucim_sigma"}}.
        # A channel absent here falls back to the global tophat_radius/cucim_sigma.
        self.channel_params   = dict(channel_params or {})
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

                # Per-channel param override (falls back to the global values).
                _tr, _cs = resolve_effective_correction_params(
                    {
                        "tophat_radius": self.tophat_radius,
                        "cucim_sigma": self.cucim_sigma,
                    },
                    self.channel_params,
                    ch,
                )

                # GPU计算（并行，max_gpu_workers个同时跑）
                # 用默认参数固定捕获method和raws，防止闭包引用变化
                def _compute_one(p_idx, _method=method, _raws=raws, _tr=_tr, _cs=_cs):
                    if self._stop:
                        return p_idx, None
                    raw = _raws.get(p_idx)
                    if raw is None:
                        return p_idx, None
                    nuc_raw  = nuc_raws.get(p_idx)
                    nuc_norm = OMETIFFLoader._norm(nuc_raw) if nuc_raw is not None else None
                    orig_norm = OMETIFFLoader._norm(raw)

                    tophat_norm = cucim_norm = None
                    if _method in ("tophat", "both"):
                        th = _apply_tophat_gpu_or_cpu(raw, _tr)
                        tophat_norm = OMETIFFLoader._norm(th)
                        tophat_raw  = th
                    else:
                        tophat_raw = None
                    if _method in ("cucim", "both"):
                        cu = _apply_cucim_or_cpu(raw, _cs, prefer_gpu=CUCIM_AVAILABLE)
                        cucim_norm = OMETIFFLoader._norm(cu)
                        cucim_raw  = cu
                    else:
                        cucim_raw = None

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
                        "nucleus_raw":      nuc_raw.astype(np.float32, copy=False) if nuc_raw is not None else None,
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
    job_finished = Signal(int, dict)
    error = Signal(int, str)

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
            self.job_finished.emit(self.request_id, payload)
        except Exception:
            if not self._stop:
                self.error.emit(self.request_id, traceback.format_exc())


def read_corrected_zarr_state(zarr_path):
    """Inspect an existing corrected_channels.zarr for incremental save.

    Returns (signatures, roi_bboxes):
      signatures : {channel_name: (method, param_value, algo_version,
                   bg_compute_path, tophat_footprint)} for
                   channels present in EVERY ROI group with a single consistent
                   signature — the v14.5a correction_method attr plus the
                   additive correction_param_value (None when an older zarr did
                   not stamp it; a None param compares unequal to the current
                   int param, so such a channel is safely reprocessed) plus
                   bg_correction_algo_version (missing -> "1", which never
                   matches the current version, forcing a reprocess of legacy
                   gaussian outputs) plus, since block S0P, the backend that
                   computed it (bg_compute_path "cpu" | "gpu" | "mps", tophat_footprint
                   "disk" | "square" | None for cucim; both None in a product
                   made before S0P, which therefore never matches and is
                   reprocessed -- its backend cannot be shown to equal this
                   machine's). A channel present in only some groups, or
                   with a mixed signature, is omitted.
      roi_bboxes : sorted list of each ROI group's bbox_fullres tuple — the ROI
                   signature used to decide whether the cached zarr still matches
                   the current ROI set.
    Missing / unreadable zarr -> ({}, []).
    """
    if not zarr_path or not os.path.isdir(zarr_path):
        return {}, []
    try:
        root = zarr.open_group(zarr_path, mode="r")
    except Exception:
        return {}, []
    groups = list(root.group_keys())
    if not groups:
        return {}, []
    roi_bboxes = []
    per_group = []
    for g in groups:
        grp = root[g]
        bbox = list(grp.attrs.get("bbox_fullres") or [])
        if len(bbox) == 4:
            roi_bboxes.append(tuple(int(v) for v in bbox))
        gm = {}
        for ch in grp.array_keys():
            a = grp[ch].attrs
            m = a.get("correction_method")
            if not m:
                continue
            pv = a.get("correction_param_value")
            # Legacy zarrs without the stamp were produced by version "1"
            # (2*sigma gaussian halo) — their signature never matches the
            # current version, so they are reprocessed on the next Save.
            ver = str(a.get("bg_correction_algo_version") or "1")
            path = a.get("bg_compute_path")
            foot = a.get("tophat_footprint")
            if path is None:
                foot = "<unrecorded>"     # a pre-S0P product: never equal
            # Block CG: the implementation; a product made before it used
            # the scipy / cupyx Gaussian (TopHat: None).
            impl = (a.get("bg_compute_impl") if "bg_compute_impl" in a
                    else legacy_compute_impl(m, path))
            gm[str(ch)] = (str(m), None if pv is None else int(pv), ver,
                           None if path is None else str(path),
                           None if foot is None else str(foot),
                           None if impl is None else str(impl))
        per_group.append(gm)
    common = set(per_group[0])
    for gm in per_group[1:]:
        common &= set(gm)
    signatures = {}
    for ch in common:
        sigs = {gm[ch] for gm in per_group}
        if len(sigs) == 1:
            signatures[ch] = next(iter(sigs))
    return signatures, sorted(roi_bboxes)


class _CoarsePlaneAccumulator:
    """The Step1 coarse plane, accumulated while level 0 is being written.

    Step1 draws a corrected channel's coarsest level by averaging the whole
    corrected region -- the product has no pyramid -- and that cost lands on
    the first tick of the channel: 9.2-11.6 s measured on a real
    59040x35520 product (G3.2b.4C/4E). The pixels it averages pass through
    this worker anyway, one 4096 tile at a time, so the same plane is
    accumulated here for free instead of being recomputed on demand later.

    It is the SAME plane, by construction and by gate
    (`tests/test_step1_coarse_plane.py`, `tests/test_step0_coarse_plane_write.py`):
    blocks anchored at the LEVEL-0 origin, each divided by the number of
    samples that actually exist inside the region, float64 sums, float32
    out. Nothing here changes level 0 or what any other consumer reads.

    Bounded: one float64 sum and one int64 count per BLOCK, so a whole-slide
    region at stride 64 is 923 x 555 x 16 B = 8.2 MiB, whatever the slide.
    """

    def __init__(self, bbox, stride):
        self.bbox = tuple(int(v) for v in bbox)
        self.stride = max(1, int(stride))
        (self.by0, self.bx0), (bh, bw) = sources.coarse_block_range(
            self.bbox, self.stride)
        self._sums = np.zeros((bh, bw), np.float64)
        self._counts = np.zeros((bh, bw), np.int64)

    def add(self, values, roi_y0, roi_x0):
        """One written tile, at its ROI-LOCAL top-left corner."""
        values = np.asarray(values, np.float32)
        if values.ndim != 2 or not values.size:
            return
        stride = self.stride
        gy = self.bbox[0] + int(roi_y0)
        gx = self.bbox[2] + int(roi_x0)
        height, width = values.shape
        front_y, front_x = gy % stride, gx % stride
        pad_y = (-(front_y + height)) % stride
        pad_x = (-(front_x + width)) % stride
        if front_y or front_x or pad_y or pad_x:
            values = np.pad(values, ((front_y, pad_y), (front_x, pad_x)))
        rows = values.shape[0] // stride
        cols = values.shape[1] // stride
        if not rows or not cols:
            return
        block_sums = values.reshape(rows, stride, cols, stride).sum(
            axis=(1, 3), dtype=np.float64)
        # The pad contributed zeros to the sums, which add nothing; it must
        # NOT contribute to the counts, or a block at the region's edge
        # would be divided by samples that do not exist and go dark.
        starts = np.arange(rows, dtype=np.int64) * stride
        row_span = np.maximum(
            np.minimum(starts + stride, front_y + height)
            - np.maximum(starts, front_y), 0)
        starts = np.arange(cols, dtype=np.int64) * stride
        col_span = np.maximum(
            np.minimum(starts + stride, front_x + width)
            - np.maximum(starts, front_x), 0)
        oy = gy // stride - self.by0
        ox = gx // stride - self.bx0
        self._sums[oy:oy + rows, ox:ox + cols] += block_sums
        self._counts[oy:oy + rows, ox:ox + cols] += (row_span[:, None]
                                                     * col_span[None, :])

    def plane(self):
        """`(mean, complete)` -- float32 means, and whether every block of
        the region received at least one sample."""
        valid = self._counts > 0
        mean = np.zeros(self._sums.shape, np.float32)
        np.divide(self._sums, np.maximum(self._counts, 1), out=mean,
                  where=valid, casting="unsafe")
        return mean, bool(valid.all())

    def nbytes(self):
        return int(self._sums.nbytes + self._counts.nbytes)


class _PlaneFeeds:
    """R7b B2.3: what each written tile also feeds -- the coarse plane and
    the display layers (`viewer/display_pyramid.py`), ROI-local coords."""

    def __init__(self, coarse, display):
        self.coarse, self.display = coarse, display

    def add(self, values, roi_y0, roi_x0):
        if self.coarse is not None:
            self.coarse.add(values, roi_y0, roi_x0)
        if self.display is not None:
            self.display.add_local(values, roi_y0, roi_x0)


def coarsest_level_stride(slide_path):
    """`(level, stride)` Step1 will ask a corrected channel for, or None.

    Read from the slide's OWN pyramid, exactly the way the viewer's provider
    reads it (`viewer/raw_tile_provider.py`: `series[0].levels`, and
    `level_downsample` rounds `h0 / hL`). A slide whose pyramid cannot be
    read this way gets NO plane -- the runtime reduction is still correct,
    and a guessed stride would be a wrong picture.
    """
    try:
        import tifffile
        with tifffile.TiffFile(str(slide_path)) as handle:
            shapes = [tuple(level.shape) for level in handle.series[0].levels]
    except Exception:                                       # noqa: BLE001
        return None
    if len(shapes) < 2:
        return None
    try:
        h0 = int(shapes[0][-2])
        hn = int(shapes[-1][-2])
    except (IndexError, TypeError, ValueError):
        return None
    if h0 <= 0 or hn <= 0:
        return None
    stride = int(round(h0 / float(hn)))
    if stride < 2:
        return None
    return len(shapes) - 1, stride


def correction_tile_size(method, impl=None, path=None):
    """The Save tile edge (block CG G1b). The Gaussian's halo covers its whole
    truncated kernel, so its result does not depend on the tile size --
    verified bitwise on a whole real channel, seams included, for both
    implementations. 1024 suits the OpenCV Gaussian (cache: 14.6 -> 10.0 s on
    8 workers); scipy is faster at 4096 (15.3 vs 22.8 s), and TopHat stays
    at 4096. On the Mac's GPU ("mps") 2048: the Save holds the device for
    one tile at a time, and a smaller tile leaves the preview gaps
    (docs/v17_step0_mac_gpu_correction_plan.md §3.2)."""
    if path == "mps":
        return 2048
    return 1024 if impl == GAUSSIAN_IMPL_OPENCV else 4096


class WsiCorrectionWorker(QThread):
    progress = Signal(int, int, int, int, str, str, int)
    job_finished = Signal(str, dict)
    canceled = Signal(str)
    error = Signal(str)

    def __init__(self, loader, output_dir, correction_config, rois=None, parent=None,
                 process_channels=None, incremental=False):
        super().__init__(parent)
        self.loader = loader
        self.output_dir = output_dir
        self.correction_config = _normalize_correction_config(correction_config) or {}
        self.rois = list(rois or [])
        self._cancel_requested = False
        # Incremental save: process only `process_channels` (None = all assigned);
        # when `incremental`, the computed channels replace theirs in the
        # existing product and the rest stay; otherwise the product is
        # replaced as a whole. Either way the channels are computed into a
        # staging product, and the live product is NOT touched here: the
        # page moves them in on the GUI thread (`swap_plan`, read when
        # `finished` arrives; `core.product_swap`, docs/v17_save_inplace_
        # step1_ahead_plan.md §8).
        self._process_channels = (None if process_channels is None
                                  else set(process_channels))
        self._incremental = bool(incremental)
        #: what the page moves into the live product when `finished` arrives:
        #: ("in", [(group, channel)], [(group, withdrawn channel)]) or
        #: ("whole",); None when there is nothing to move
        self.swap_plan = None

    def stop_after_current_channel(self):
        """Request cancellation. Honoured at the next TILE boundary (see
        `run`); the name is kept for its callers."""
        self._cancel_requested = True

    @staticmethod
    def _sidecar_path(output_dir):
        return os.path.join(output_dir, sources.COARSE_SIDECAR_DIRNAME)

    def _correct_channel(self, ds, accumulator, tiles, ch_name, method, param, path,
                         info, roi_y0, roi_x0, poly_mask, progress, impl=None):
        """Every tile of one channel on ONE backend, written and accumulated in
        tile order. Returns "done" or "canceled"; a GPU failure raises
        `GpuBackendFailed` (the caller recomputes the channel on the CPU).

        Block S0P: on the CPU backend the tiles are read one after another
        and corrected on up to `choose_workers(...)` threads
        (`core.bg_parallel`); the GPU backend stays one tile at a time. The
        results are written here, in tile order, so the product and its
        coarse plane are bitwise the serial ones. A cancel starts no new tile
        and writes nothing more."""
        progress_idx, channel_total, completed_units, total_units, started = progress
        page = self.loader.ch_map[ch_name]
        if path in ("gpu", "mps"):
            n = 1
        else:
            # Block CS P3: the per-tile memory of the kernel that will run --
            # the skimage TopHat fallback needs far more than OpenCV / scipy.
            # (codex CS P3: only an integer source is sure to reach OpenCV --
            # a float one may hold NaN/Inf, which go to skimage)
            from ...core import bg_correction as _bg
            fast = method == "cucim" or (_bg._cv2 is not None
                                         and self._source_is_integer())
            avail = mem_available_bytes()
            n = choose_workers(os.cpu_count(), len(tiles), avail,
                               MEM_PER_TILE_FAST_BYTES if fast else MEM_PER_TILE_BYTES)
            print(f"[WsiCorrectionWorker] {ch_name}: {n} worker(s) (cpu={os.cpu_count()}, "
                  f"free={'?' if avail is None else f'{avail / 1e9:.1f} GB'}, "
                  f"tiles={len(tiles)}, {'fast' if fast else 'skimage'} kernel)", flush=True)

        def task(tile, read_done):
            _core, padded, crop = tile
            py0, py1, px0, px1 = padded
            raw = self.loader._read_roi_zarr(
                page, roi_y0 + py0, roi_y0 + py1, roi_x0 + px0, roi_x0 + px1,
            ).astype(np.float32, copy=False)
            read_done()
            corr = correct_tile(raw, method, param, path, impl)
            cy0, cy1, cx0, cx1 = crop
            return corr[cy0:cy1, cx0:cx1].astype(np.float32, copy=True)

        self._tile_stats = {}
        results = ordered_results(task, tiles, n,
                                  should_stop=lambda: self._cancel_requested,
                                  stats=self._tile_stats)
        written = 0
        try:
            for tile_idx, out in enumerate(results, start=1):
                if self._cancel_requested:
                    return "canceled"
                self._write_tile(ds, accumulator, out, tiles[tile_idx - 1][0], poly_mask)
                written = tile_idx
                elapsed = max(0.001, time.time() - started)
                done_units = completed_units + tile_idx
                remain = int((total_units - done_units) * (elapsed / done_units))
                self.progress.emit(progress_idx, channel_total, tile_idx, len(tiles),
                                   f"{info['group_name']}/{ch_name}", method, remain)
        finally:
            results.close()
        if written < len(tiles):
            return "canceled"
        return "done"

    def _source_is_integer(self):
        """Is the slide stored as integers (so its tiles are finite)? False
        -- the conservative answer -- when it cannot be told."""
        known = self.__dict__.get("_source_integer")
        if known is None:
            known = False
            try:
                import tifffile
                with tifffile.TiffFile(self.loader.filepath) as tif:
                    known = np.dtype(tif.series[0].dtype).kind in "ui"
            except Exception:                               # noqa: BLE001
                known = False
            self._source_integer = known
        return known

    @staticmethod
    def _write_tile(ds, accumulator, out, core, poly_mask):
        """One corrected core into level 0, and into the coarse plane while
        the pixels are still in hand (level 0 is never re-read)."""
        y0, y1, x0, x1 = core
        if poly_mask is not None:
            out = out.copy()
            out[~poly_mask[y0:y1, x0:x1]] = 0
        ds[y0:y1, x0:x1] = out
        if accumulator is not None:
            accumulator.add(out, y0, x0)

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

    @staticmethod
    def _publish_plane(sidecar_path, group_name, ch_name, accumulator, ds,
                       level_stride, write_token):
        """Write the plane, then say it is there. Never the other way round.

        A plane whose `complete` mark is missing is refused by Step1
        (`viewer/step1_source.py`), so a run that dies between the pixels
        and the mark costs a runtime reduction -- not a wrong picture. A
        region with a block that received no sample at all is not published
        at all: that would be a plane that disagrees with the reduction.
        """
        level, stride = level_stride
        plane, complete = accumulator.plane()
        if not complete:
            return False
        try:
            root = zarr.open_group(sidecar_path, mode="a")
            group = root.require_group(group_name)
            out = group.create_dataset(
                ch_name, shape=plane.shape, dtype=np.float32,
                chunks=(min(512, plane.shape[0]), min(512, plane.shape[1])),
                overwrite=True)
            out[:, :] = plane
            product_attrs = dict(ds.attrs)
            product_attrs["source_identity"] = write_token
            identity = sources.coarse_plane_identity(product_attrs, stride,
                                                     level=level)
            for key, value in identity.items():
                out.attrs[key] = value
            origin, _shape = sources.coarse_block_range(
                product_attrs.get("roi_bbox_fullres") or [], stride)
            out.attrs["block_origin"] = [int(origin[0]), int(origin[1])]
            out.attrs["written_by"] = "WsiCorrectionWorker"
            out.attrs["written_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            out.attrs["complete"] = True          # LAST
        except Exception:                                   # noqa: BLE001
            # The plane is an optimisation for Step1; level 0 is the
            # product. A plane that could not be written is not an error.
            return False
        return True

    def run(self):
        # the Mac's GPU: its one-time check runs here, on this thread, before
        # any channel picks its backend (plan §3.1; a flag read otherwise)
        from ...core import mps_kernels
        mps_kernels.ensure_ready()
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
            # Per-channel param overrides {ch:{tophat_radius,cucim_sigma}}; a channel
            # absent falls back to the global method_params.
            ch_params = dict((self.correction_config.get("channel_params") or {}))

            def _param_for(ch, method):
                radius, sigma = resolve_effective_correction_params(
                    params, ch_params, ch)
                return radius if method == "tophat" else sigma
            # All channels desired-corrected (the merged end-state of the zarr).
            desired = {
                ch: method for ch, method in decisions.items()
                if method in {"tophat", "cucim"} and ch in self.loader.ch_map
            }
            # Incremental: process only the requested subset (new/changed); the
            # rest are already in the zarr with the same method and are skipped.
            if self._process_channels is None:
                process = dict(desired)
            else:
                process = {ch: m for ch, m in desired.items()
                           if ch in self._process_channels}
            for ch in desired:
                if ch not in process:
                    print(f"[WsiCorrectionWorker] skipping channel={ch} "
                          f"(already corrected with {desired[ch]})")
            channels = [
                (ch, method, _param_for(ch, method))
                for ch, method in process.items()
            ]
            live_path = os.path.join(self.output_dir, "corrected_channels.zarr")
            incremental = self._incremental and os.path.isdir(live_path)
            # What the product holds now, and which of it this Save withdraws.
            withdrawn = []
            if incremental:
                live_root = zarr.open_group(live_path, mode="r")
                for g in live_root.group_keys():
                    for ch in live_root[g].array_keys():
                        if ch not in desired:
                            withdrawn.append((g, ch))
            # Which level Step1 will ask a corrected channel for, read from
            # the slide's own pyramid. `None` means "do not write a plane" --
            # never a guessed stride.
            level_stride = coarsest_level_stride(
                getattr(self.loader, "filepath", "") or "")
            # R7b B2.3: every level's stride, for the display layers
            display_strides = display_pyramid.level_strides(
                getattr(self.loader, "filepath", "") or "")

            if not channels:
                # Nothing to (re)compute. In incremental mode the zarr already
                # holds every desired channel -> report the merged state; in fresh
                # mode there is genuinely nothing.
                if incremental and withdrawn:
                    self.swap_plan = ("in", [], withdrawn)
                self.job_finished.emit(live_path if incremental else "",
                                   dict(desired) if incremental else {})
                return

            os.makedirs(self.output_dir, exist_ok=True)
            staging = product_swap.new_staging(self.output_dir)
            zarr_path = os.path.join(staging, "corrected_channels.zarr")
            sidecar_path = self._sidecar_path(staging)
            root = zarr.open_group(zarr_path, mode="w")
            root.attrs["mode"] = "roi_only"
            root.attrs["source_ome"] = os.path.abspath(getattr(self.loader, "filepath", "") or "")
            root.attrs["output_dir"] = os.path.abspath(self.output_dir)   # the live run
            root.attrs["created_by"] = "Step0"
            # Version of the correction numerics that LAST wrote this zarr.
            # Per-channel identity (bg_correction_algo_version on each array)
            # is what incremental save compares; this root attr is provenance.
            root.attrs["bg_correction_algo_version"] = BG_CORRECTION_ALGO_VERSION
            root.attrs["roi_names"] = [str(r.get("name") or f"ROI_{i}") for i, r in enumerate(rois, start=1)]
            root.attrs["correction_config"] = self.correction_config
            # Seed with the channels we are KEEPING (already in the zarr, same
            # method) so the emitted decisions describe the full merged zarr.
            corrected_decisions = {ch: m for ch, m in desired.items()
                                   if ch not in process}
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
                for _, method, param in channels:
                    _path = compute_path(method)
                    tile_counts.append(len(list(_tile_slices(
                        roi_h, roi_w,
                        correction_tile_size(method, compute_impl(method, _path), _path),
                        method_overlap(method, param)))))
            total_units = sum(tile_counts)
            started = time.time()
            completed_units = 0
            progress_idx = 0
            display = None                 # the display builder under way, if any

            for info in roi_infos:
                # the staging product holds only what this run computes
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
                    overlap = method_overlap(method, param)
                    # Block S0P: ONE backend for the whole channel, chosen
                    # here; a GPU failure part-way discards the channel and
                    # computes it again on the CPU from tile 0 -- never a
                    # channel half square-footprint, half disk.
                    path = compute_path(method)
                    # Block CG: ONE implementation for the whole channel too.
                    impl = compute_impl(method, path)
                    while True:
                        # (the tiles follow the implementation: a channel
                        # recomputed with scipy goes back to 4096)
                        tiles = list(_tile_slices(roi_h, roi_w,
                                                  correction_tile_size(method, impl, path),
                                                  overlap))
                        # (staged: the live channel, its plane and its display
                        # layers stand until the swap at the end of the run)
                        accumulator = None
                        if level_stride is not None:
                            accumulator = _CoarsePlaneAccumulator(
                                info["bbox"], level_stride[1])
                        # (a display builder that cannot run -- no level, no
                        # disk, a sample it cannot keep -- leaves none, and
                        # Step1 reads as before)
                        display_pyramid.drop(display_pyramid.display_root(zarr_path),
                                             info["group_name"], ch_name)
                        ds = group.create_dataset(
                            ch_name,
                            shape=(roi_h, roi_w),
                            chunks=(min(1024, roi_h), min(1024, roi_w)),
                            dtype=np.float32,
                            overwrite=True,
                        )
                        display = None
                        if display_strides:
                            display = display_pyramid.DisplayBuilder(
                                display_pyramid.display_root(zarr_path), info["group_name"],
                                ch_name, info["bbox"], display_strides, threaded=True,
                                fill=float(ds.fill_value or 0.0))
                        # v14.5a: per-channel source identity (metadata only — never
                        # touches array data/shape). channel_index from the loader's
                        # channel map; None if unavailable (name/key/shape/dtype still
                        # recorded).
                        stamp_corrected_channel_identity(
                            ds,
                            channel_name=ch_name,
                            channel_index=self.loader.ch_map.get(ch_name),
                            correction_method=method,
                            roi_name=info["roi_name"],
                            roi_bbox_fullres=info["bbox"],
                            correction_param_name=(
                                "tophat_radius" if method == "tophat" else "cucim_sigma"),
                            correction_param_value=int(param),
                        )
                        try:
                            outcome = self._correct_channel(
                                ds, _PlaneFeeds(accumulator, display), tiles, ch_name,
                                method, param, path,
                                info, roi_y0, roi_x0, poly_mask,
                                (progress_idx, channel_total, completed_units,
                                 total_units, started), impl)
                        except GpuBackendFailed as exc:
                            print(f"[WsiCorrectionWorker] {ch_name}: {exc}; the channel "
                                  f"is discarded and computed again on the CPU from tile 0")
                            if ch_name in group:
                                del group[ch_name]
                            if display is not None:
                                display.abort()        # its thread ends before the redo
                            path = "cpu"
                            impl = compute_impl(method, path)
                            continue
                        except CpuImplUnavailable as exc:
                            # Block CG: never a channel half OpenCV, half scipy.
                            print(f"[WsiCorrectionWorker] {ch_name}: {exc}; the channel "
                                  f"is discarded and computed again with scipy from tile 0")
                            if ch_name in group:
                                del group[ch_name]
                            if display is not None:
                                display.abort()        # its thread ends before the redo
                            impl = GAUSSIAN_IMPL_SCIPY
                            continue
                        break
                    if outcome == "canceled":
                        if display is not None:
                            display.abort()
                        # Checked per TILE, not per channel: in Full-WSI
                        # mode one channel is the whole slide, and a
                        # cancel that waited for it was a cancel that did
                        # nothing for minutes. The channel in progress is
                        # partial and is dropped; a fresh save drops the
                        # staging goes; the live product was never touched.
                        product_swap.discard_staging(self.output_dir)
                        self.canceled.emit(live_path)
                        return
                    # Block S0P: which backend made these pixels -- part of
                    # the incremental-reuse signature.
                    ds.attrs["bg_compute_path"] = path
                    ds.attrs["tophat_footprint"] = tophat_footprint(method, path)
                    ds.attrs["bg_compute_impl"] = impl          # block CG

                    # WHICH WRITE THIS IS. Minted once per channel per real
                    # recomputation, after its level 0 is whole: the static
                    # identity above says what the settings were, this says
                    # that these are the pixels those settings produced THIS
                    # time. Step1 compares it against the plane's copy, so a
                    # plane from an earlier write of the same channel with
                    # the same settings can never be mistaken for this one.
                    # An incremental Save that SKIPS a channel never reaches
                    # here, so that channel's token -- and its plane -- stand.
                    write_token = uuid.uuid4().hex
                    ds.attrs["source_identity"] = write_token
                    ds.attrs["written_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                    if accumulator is not None:
                        self._publish_plane(
                            sidecar_path, info["group_name"], ch_name,
                            accumulator, ds, level_stride, write_token)
                    if display is not None:
                        display.finish(dict(ds.attrs), ds)  # the new token; s1 checked against ds
                    corrected_decisions[ch_name] = method
                    completed_units += len(tiles)
                    if self._cancel_requested:
                        # Requested after the last tile of this channel: the
                        # Save is canceled as a whole all the same.
                        product_swap.discard_staging(self.output_dir)
                        self.canceled.emit(live_path)
                        return

            # Every channel is complete: the page moves them in.
            self.swap_plan = (
                ("in", [(info["group_name"], ch) for info in roi_infos
                        for ch, _m, _p in channels], withdrawn)
                if incremental else ("whole",))
            self.job_finished.emit(live_path, corrected_decisions)
        except Exception:
            # a display builder still running ends with the save (R7b B2)
            builder = locals().get("display")
            if builder is not None:
                builder.abort()
            product_swap.discard_staging(self.output_dir)
            self.error.emit(traceback.format_exc())


class _WsiCorrectionProgressDialog(QDialog):
    cancel_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._allow_close = False
        # NOT modal. An application-modal dialog blocks every other window
        # of the application, including the floating Tissue Preview / ROI
        # Navigator, whose close button then does nothing for the length of
        # a whole-slide run. The page disables what must not run during a
        # save (Load, Continue) and gates the GPU entry points on
        # `production_correction_busy()`; the dialog itself only has to stay
        # visible and refuse to be closed until the run has ended.
        self.setModal(False)
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
        # stops the run: danger (button restyle plan §2); its hairline is
        # paid for by one pixel of padding, so the size stays
        theme_widgets.button(self._cancel, "danger", "padding:5px 17px;")
        self._cancel.clicked.connect(self._on_cancel)
        lay.addWidget(self._cancel, alignment=Qt.AlignRight)

    def _on_cancel(self):
        self._cancel.setEnabled(False)
        self._label.setText("Cancellation requested. Stopping after the current tile…")
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
