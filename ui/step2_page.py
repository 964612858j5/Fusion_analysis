"""
block01/ui/step2_page.py — Step2Page (Segmentation & Merge).
"""

import os
import json

import numpy as np
import zarr

from PyQt5 import QtWidgets, QtCore
from PyQt5.QtCore import Qt, QRectF, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QGroupBox, QSplitter, QProgressBar, QMessageBox, QFileDialog,
    QDoubleSpinBox, QScrollArea, QComboBox, QCheckBox,
)
import pyqtgraph as pg

from ..config import OUTPUT_DIR
from ..utils.segmentation_config import (
    CELLPOSE_NUCLEI_DAPI,
    CELLPOSE_NUCLEI_EXPANSION,
    CELLPOSE_NUCLEI_HQ,
    CELLPOSE_NUCLEI_HQ2,
    CELLPOSE_WHOLECELL_FUSION,
    MESMER_WHOLE_CELL,
    MESMER_NUCLEI,
    MESMER_NUCLEAR_GUIDED,
    STARDIST_NUCLEI_DAPI,
    STARDIST_NUCLEI_EXPANSION,
    available_segmentation_methods,
    get_segmentation_method_config,
    normalize_segmentation_config,
)
from ..utils.segmentation_params import (
    PARAM_INDEX,
    params_index_path,
)
from ..utils.tile_strategy import suggest_tile_strategy
from ..utils.roi_project import mark_roi_step
from ..workers.hq_marker_segmentation import (
    CONSENSUS_MODES,
    parse_channel_weights,
    parse_hq_channels,
    resolve_hq_channels,
    validate_hq_channels,
)
from ..workers.segment_merge_worker import SegmentMergeWorker
from ..core.io_loader import OMETIFFLoader

# ══════════════════════════════════════════════════════════════════════
#  Step 2 Page  (Segmentation & Merge)
# ══════════════════════════════════════════════════════════════════════

class Step2Page(QWidget):
    """Full Step 2 UI: zarr input, tile grid, segmentation params, progress."""

    go_back           = pyqtSignal()
    segmentation_done = pyqtSignal(str)   # emits output_dir when done
    open_qc_requested = pyqtSignal(str)   # user explicitly chose Step3/QC

    # Tile status colours
    _COL_IDLE    = (80,  80,  80)
    _COL_RUNNING = (255, 200,  50)
    _COL_DONE    = ( 60, 200,  80)
    _COL_ERROR   = (220,  60,  60)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._zarr_path      = None
        self._seg_config     = {}
        self._worker         = None
        self._tile_rects     = {}
        self._tile_status    = {}
        self._n_rows         = 3
        self._n_cols         = 4
        self._full_h         = 0
        self._full_w         = 0
        self._total_cells    = 0
        self._ov_thread      = None
        self._rois           = []
        self._last_output_dir = None   # set when segmentation finishes
        self._seg_param_file = ""
        self._seg_params_index = {}
        self._seg_params_dir = ""
        self._loading_index_selection = False
        self._parameter_source = "manual"
        self._applied_param_file = ""
        self._roi_id = ""
        self._roi_dir = ""
        self._step2_dir = ""
        self._suggested_tile_strategy = {}

        self._build_ui()

    def set_roi_context(self, roi_id="", roi_dir="", step2_dir=""):
        self._roi_id = roi_id or ""
        self._roi_dir = roi_dir or ""
        self._step2_dir = step2_dir or ""
        if self._step2_dir:
            os.makedirs(self._step2_dir, exist_ok=True)
            self._out_edit.setText(self._step2_dir)
        print(f"[Step2] roi_id={self._roi_id}")
        print(f"[Step2] output_base={self._step2_dir or self._out_edit.text().strip()}")

    def _sync_output_dir_from_zarr_path(self, zarr_path):
        """Use the ROI step2 directory when the input zarr comes from ROI step1."""
        path = os.path.abspath(zarr_path or "")
        parts = path.split(os.sep)
        if "rois" not in parts or "step1" not in parts:
            return
        step1_idx = parts.index("step1")
        if step1_idx < 2 or parts[step1_idx - 2] != "rois":
            return
        roi_dir = os.sep + os.path.join(*parts[:step1_idx])
        roi_id = parts[step1_idx - 1]
        step2_dir = os.path.join(roi_dir, "step2")
        current = os.path.abspath(self._step2_dir or self._out_edit.text().strip() or OUTPUT_DIR)
        if current != os.path.abspath(step2_dir):
            self.set_roi_context(roi_id=roi_id, roi_dir=roi_dir, step2_dir=step2_dir)

    # ── UI construction ───────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # Title
        title = QLabel('Step 2 — Segmentation & Merge')
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            'font-size:16px;font-weight:bold;color:#eee;'
            'background:#1a1a1a;padding:6px;border-radius:4px;'
        )
        root.addWidget(title)

        # Main split: left overview | right controls
        split = QSplitter(Qt.Horizontal)

        # ── LEFT: overview + progress ─────────────────────────────────
        left = QWidget()
        ll   = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(4)

        ll.addWidget(self._lbl('Tile Status Overview', bold=True))

        self._ov_gv  = pg.GraphicsLayoutWidget()
        self._ov_gv.setBackground('#111')
        self._ov_vb  = self._ov_gv.addViewBox()
        self._ov_vb.setAspectLocked(True)
        self._ov_vb.invertY(True)
        self._ov_vb.setMenuEnabled(False)
        self._ov_img = pg.ImageItem()
        self._ov_vb.addItem(self._ov_img)
        ll.addWidget(self._ov_gv, stretch=1)

        self._ov_status = QLabel('No zarr loaded')
        self._ov_status.setStyleSheet('color:#888;font-size:10px;')
        self._ov_status.setAlignment(Qt.AlignCenter)
        self._ov_status.setWordWrap(True)
        ll.addWidget(self._ov_status)

        # Progress text
        ll.addWidget(self._lbl('Progress', bold=True))
        self._prog_lbl = QLabel('—')
        self._prog_lbl.setStyleSheet(
            'color:#ccc;font-size:11px;padding:4px;'
            'background:#111;border-radius:3px;'
        )
        self._prog_lbl.setWordWrap(True)
        ll.addWidget(self._prog_lbl)

        self._prog_bar = QProgressBar()
        self._prog_bar.setRange(0, 100)
        self._prog_bar.setStyleSheet(
            'QProgressBar{border:1px solid #444;border-radius:3px;'
            'text-align:center;color:#fff;height:16px;}'
            'QProgressBar::chunk{background:#2a5;border-radius:3px;}'
        )
        ll.addWidget(self._prog_bar)

        self._cells_lbl = QLabel('Total cells detected: —')
        self._cells_lbl.setStyleSheet('color:#aaa;font-size:11px;')
        ll.addWidget(self._cells_lbl)

        split.addWidget(left)

        # ── RIGHT: all controls ───────────────────────────────────────
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setStyleSheet('QScrollArea{border:none;}')
        right_w = QWidget()
        rl = QVBoxLayout(right_w)
        rl.setSpacing(8)

        # Data input
        inp_box = QGroupBox('Input Data')
        inp_box.setStyleSheet(self._box_style('#61afef'))
        inl = QVBoxLayout(inp_box)

        zr = QHBoxLayout()
        zr.addWidget(QLabel('fused.zarr:'))
        self._zarr_edit = QtWidgets.QLineEdit()
        self._zarr_edit.setPlaceholderText('Path to fused.zarr …')
        self._zarr_edit.setStyleSheet('font-size:11px;')
        zr.addWidget(self._zarr_edit, stretch=1)
        btn_browse = QPushButton('Browse')
        btn_browse.setFixedWidth(64)
        btn_browse.clicked.connect(self._browse_zarr)
        zr.addWidget(btn_browse)
        inl.addLayout(zr)

        pr = QHBoxLayout()
        pr.addWidget(QLabel('Segmentation Index:'))
        self._seg_params_edit = QtWidgets.QLineEdit()
        self._seg_params_edit.setPlaceholderText('segmentation_params/ or segmentation_params_index.json')
        self._seg_params_edit.setStyleSheet('font-size:11px;')
        pr.addWidget(self._seg_params_edit, stretch=1)
        btn_params = QPushButton('Browse')
        btn_params.setFixedWidth(64)
        btn_params.clicked.connect(self._browse_seg_params)
        pr.addWidget(btn_params)
        inl.addLayout(pr)

        btn_load = QPushButton('Load zarr info & overview')
        btn_load.setStyleSheet(
            'QPushButton{background:#255;color:white;border-radius:3px;padding:4px;}'
            'QPushButton:hover{background:#377;}'
        )
        btn_load.clicked.connect(self._load_zarr_info)
        inl.addWidget(btn_load)

        self._zarr_info = QLabel('—')
        self._zarr_info.setStyleSheet('color:#aaa;font-size:10px;')
        self._zarr_info.setWordWrap(True)
        inl.addWidget(self._zarr_info)
        rl.addWidget(inp_box)

        # Tile grid
        tile_box = QGroupBox('Tile Grid (for segmentation)')
        tile_box.setStyleSheet(self._box_style('#e5c07b'))
        til = QVBoxLayout(tile_box)

        self._auto_tile_strategy = QCheckBox('Auto tile strategy')
        self._auto_tile_strategy.setChecked(False)
        self._auto_tile_strategy.stateChanged.connect(self._on_auto_tile_strategy_changed)
        til.addWidget(self._auto_tile_strategy)

        self._tile_suggestion_lbl = QLabel('Suggested: —')
        self._tile_suggestion_lbl.setStyleSheet('color:#aaa;font-size:10px;')
        self._tile_suggestion_lbl.setWordWrap(True)
        til.addWidget(self._tile_suggestion_lbl)

        grid_row = QHBoxLayout()
        grid_row.addWidget(QLabel('Rows:'))
        self._rows_spin = QtWidgets.QSpinBox()
        self._rows_spin.setRange(1, 20)
        self._rows_spin.setValue(3)
        grid_row.addWidget(self._rows_spin)
        grid_row.addWidget(QLabel('Cols:'))
        self._cols_spin = QtWidgets.QSpinBox()
        self._cols_spin.setRange(1, 20)
        self._cols_spin.setValue(4)
        grid_row.addWidget(self._cols_spin)
        grid_row.addStretch()
        til.addLayout(grid_row)

        ovlp_row = QHBoxLayout()
        ovlp_row.addWidget(QLabel('Overlap (px):'))
        self._overlap_spin = QtWidgets.QSpinBox()
        self._overlap_spin.setRange(50, 1000)
        self._overlap_spin.setValue(200)
        self._overlap_spin.setSingleStep(50)
        ovlp_row.addWidget(self._overlap_spin)
        ovlp_row.addStretch()
        til.addLayout(ovlp_row)

        self._tile_ram_lbl = QLabel('')
        self._tile_ram_lbl.setStyleSheet('color:#aaa;font-size:10px;')
        til.addWidget(self._tile_ram_lbl)

        self._rows_spin.valueChanged.connect(self._update_tile_info)
        self._cols_spin.valueChanged.connect(self._update_tile_info)
        self._overlap_spin.valueChanged.connect(self._update_tile_info)
        rl.addWidget(tile_box)

        # Segmentation params
        cp_box = QGroupBox('Segmentation Parameters')
        cp_box.setStyleSheet(self._box_style('#c678dd'))
        cpl = QVBoxLayout(cp_box)

        def _param_row(label, widget):
            r = QHBoxLayout()
            l = QLabel(label)
            l.setFixedWidth(160)
            r.addWidget(l)
            r.addWidget(widget)
            return r, l, widget

        self._param_source_combo = QComboBox()
        self._param_source_combo.addItem('None / Manual default', 'manual')
        self._param_source_combo.addItem('From segmentation_params index', 'index')
        self._param_source_combo.currentIndexChanged.connect(self._on_param_source_changed)
        r, self._param_source_label, _ = _param_row('Parameter Source:', self._param_source_combo)
        cpl.addLayout(r)

        self._index_method_combo = QComboBox()
        self._index_method_combo.currentIndexChanged.connect(self._on_index_method_changed)
        r, self._index_method_label, _ = _param_row('Index method:', self._index_method_combo)
        cpl.addLayout(r)

        self._history_combo = QComboBox()
        self._history_combo.currentIndexChanged.connect(self._on_history_changed)
        r, self._history_label, _ = _param_row('Parameter version:', self._history_combo)
        cpl.addLayout(r)

        self._apply_index_btn = QPushButton('Apply Selected Params')
        self._apply_index_btn.clicked.connect(self._apply_selected_index_params)
        self._apply_index_btn.setStyleSheet(
            'QPushButton{background:#3a5;color:white;border-radius:3px;padding:4px;}'
            'QPushButton:hover{background:#4b6;}'
        )
        cpl.addWidget(self._apply_index_btn)

        self._resolved_param_edit = QtWidgets.QLineEdit()
        self._resolved_param_edit.setReadOnly(True)
        self._resolved_param_edit.setPlaceholderText('Resolved parameter JSON path')
        self._resolved_param_edit.setStyleSheet('font-size:10px;color:#aaa;')
        r, self._resolved_param_label, _ = _param_row('Resolved path:', self._resolved_param_edit)
        cpl.addLayout(r)

        self._method_combo = QComboBox()
        self._method_combo.setMinimumWidth(260)
        self._method_combo.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        for method in available_segmentation_methods():
            cfg = get_segmentation_method_config(method)
            self._method_combo.addItem(cfg["display_name"], method)
        self._method_combo.setCurrentIndex(
            max(0, self._method_combo.findData(CELLPOSE_WHOLECELL_FUSION))
        )
        self._method_combo.currentIndexChanged.connect(self._on_method_changed)
        r, self._method_label, _ = _param_row('Method:', self._method_combo)
        cpl.addLayout(r)

        # Cellpose 4.0.1+: model_type is ignored — only cpsam is used
        self._cp_model_lbl = QLabel('cpsam  (Cellpose 4.0.1+: only model, model_type ignored)')
        self._cp_model_lbl.setStyleSheet(
            'color:#fa8;font-size:11px;padding:2px 4px;'
            'background:#221;border-radius:3px;'
        )
        r, self._cp_model_label, _ = _param_row('Model:', self._cp_model_lbl)
        cpl.addLayout(r)

        self._cp_diam = QDoubleSpinBox()
        self._cp_diam.setRange(0, 300)
        self._cp_diam.setValue(30)
        self._cp_diam.setSpecialValueText('auto')
        r, self._cp_diam_label, _ = _param_row('diameter (0=auto):', self._cp_diam)
        cpl.addLayout(r)

        self._cp_flow = QDoubleSpinBox()
        self._cp_flow.setRange(0.0, 3.0)
        self._cp_flow.setSingleStep(0.05)
        self._cp_flow.setValue(0.4)
        r, self._cp_flow_label, _ = _param_row('flow_threshold:', self._cp_flow)
        cpl.addLayout(r)

        self._cp_prob = QDoubleSpinBox()
        self._cp_prob.setRange(-6.0, 6.0)
        self._cp_prob.setSingleStep(0.1)
        self._cp_prob.setValue(0.0)
        r, self._cp_prob_label, _ = _param_row('cellprob_threshold:', self._cp_prob)
        cpl.addLayout(r)

        self._cp_minsize = QtWidgets.QSpinBox()
        self._cp_minsize.setRange(1, 10000)
        self._cp_minsize.setValue(15)
        r, self._cp_minsize_label, _ = _param_row('min_size (px²):', self._cp_minsize)
        cpl.addLayout(r)

        self._cp_gpu = QCheckBox('Use GPU if available')
        self._cp_gpu.setChecked(True)
        r, self._cp_gpu_label, _ = _param_row('GPU:', self._cp_gpu)
        cpl.addLayout(r)

        self._cp_tile_size = QtWidgets.QSpinBox()
        self._cp_tile_size.setRange(128, 4096)
        self._cp_tile_size.setSingleStep(128)
        self._cp_tile_size.setValue(1024)
        r, self._cp_tile_size_label, _ = _param_row('tile size:', self._cp_tile_size)
        cpl.addLayout(r)

        self._cp_batch_size = QtWidgets.QSpinBox()
        self._cp_batch_size.setRange(1, 128)
        self._cp_batch_size.setValue(8)
        r, self._cp_batch_size_label, _ = _param_row('batch size:', self._cp_batch_size)
        cpl.addLayout(r)

        self._sd_model = QtWidgets.QLineEdit('2D_versatile_fluo')
        self._sd_model.setStyleSheet('font-size:11px;')
        r, self._sd_model_label, _ = _param_row('StarDist model:', self._sd_model)
        cpl.addLayout(r)

        self._sd_prob = QDoubleSpinBox()
        self._sd_prob.setRange(-1.0, 1.0)
        self._sd_prob.setSingleStep(0.05)
        self._sd_prob.setValue(-1.0)
        self._sd_prob.setSpecialValueText('auto')
        r, self._sd_prob_label, _ = _param_row('prob_thresh:', self._sd_prob)
        cpl.addLayout(r)

        self._sd_nms = QDoubleSpinBox()
        self._sd_nms.setRange(-1.0, 1.0)
        self._sd_nms.setSingleStep(0.05)
        self._sd_nms.setValue(-1.0)
        self._sd_nms.setSpecialValueText('auto')
        r, self._sd_nms_label, _ = _param_row('nms_thresh:', self._sd_nms)
        cpl.addLayout(r)

        self._sd_expand = QDoubleSpinBox()
        self._sd_expand.setRange(0, 200)
        self._sd_expand.setSingleStep(1)
        self._sd_expand.setValue(8)
        r, self._sd_expand_label, _ = _param_row('expansion_distance:', self._sd_expand)
        cpl.addLayout(r)

        self._hq_channels = QtWidgets.QLineEdit()
        self._hq_channels.setPlaceholderText('PanCK;CD45;CD68')
        self._hq_channels.setStyleSheet('font-size:11px;')
        r, self._hq_channels_label, _ = _param_row('hq_channels:', self._hq_channels)
        cpl.addLayout(r)

        self._hq_input_mode = QComboBox()
        self._hq_input_mode.addItem('selected_channels_from_source', 'selected_channels_from_source')
        self._hq_input_mode.addItem('step1_weighted_fusion', 'step1_weighted_fusion')
        self._hq_input_mode.addItem('hybrid', 'hybrid')
        r, self._hq_input_mode_label, _ = _param_row('hq input mode:', self._hq_input_mode)
        cpl.addLayout(r)

        self._hq_radius = QDoubleSpinBox()
        self._hq_radius.setRange(1, 200)
        self._hq_radius.setSingleStep(1)
        self._hq_radius.setValue(12)
        r, self._hq_radius_label, _ = _param_row('max_cell_radius:', self._hq_radius)
        cpl.addLayout(r)

        self._hq_norm_low = QDoubleSpinBox()
        self._hq_norm_low.setRange(0.0, 50.0)
        self._hq_norm_low.setSingleStep(0.5)
        self._hq_norm_low.setValue(1.0)
        r, self._hq_norm_low_label, _ = _param_row('norm percentile low:', self._hq_norm_low)
        cpl.addLayout(r)

        self._hq_norm_high = QDoubleSpinBox()
        self._hq_norm_high.setRange(50.0, 100.0)
        self._hq_norm_high.setSingleStep(0.5)
        self._hq_norm_high.setValue(99.5)
        r, self._hq_norm_high_label, _ = _param_row('norm percentile high:', self._hq_norm_high)
        cpl.addLayout(r)

        self._hq_consensus = QComboBox()
        for mode in CONSENSUS_MODES:
            self._hq_consensus.addItem(mode, mode)
        r, self._hq_consensus_label, _ = _param_row('consensus mode:', self._hq_consensus)
        cpl.addLayout(r)

        self._hq_weights = QtWidgets.QLineEdit()
        self._hq_weights.setPlaceholderText('optional: PanCK=1;CD45=0.8;CD68=1')
        self._hq_weights.setStyleSheet('font-size:11px;')
        r, self._hq_weights_label, _ = _param_row('channel weights:', self._hq_weights)
        cpl.addLayout(r)

        self._hq_min_signal = QDoubleSpinBox()
        self._hq_min_signal.setRange(0.0, 1.0)
        self._hq_min_signal.setSingleStep(0.01)
        self._hq_min_signal.setValue(0.08)
        r, self._hq_min_signal_label, _ = _param_row('min signal threshold:', self._hq_min_signal)
        cpl.addLayout(r)

        self._hq_widgets = [
            self._hq_channels_label, self._hq_channels,
            self._hq_input_mode_label, self._hq_input_mode,
            self._hq_radius_label, self._hq_radius,
            self._hq_norm_low_label, self._hq_norm_low,
            self._hq_norm_high_label, self._hq_norm_high,
            self._hq_consensus_label, self._hq_consensus,
            self._hq_weights_label, self._hq_weights,
            self._hq_min_signal_label, self._hq_min_signal,
        ]
        self._hq2_widgets = []

        def _hq2_row(label, widget):
            r, l, w = _param_row(label, widget)
            cpl.addLayout(r)
            self._hq2_widgets.extend([l, w])
            return w

        self._hq2_base_section = QLabel('HQ2: HQ proposal inputs')
        self._hq2_base_section.setStyleSheet('color:#7dd3fc;font-size:11px;font-weight:bold;padding-top:4px;')
        cpl.addWidget(self._hq2_base_section)
        self._hq2_widgets.append(self._hq2_base_section)

        self._hq2_channels = _hq2_row('hq2 hq_channels:', QtWidgets.QLineEdit())
        self._hq2_channels.setPlaceholderText('CD68;CD206;CD45')
        self._hq2_channels.setStyleSheet('font-size:11px;')

        self._hq2_input_mode = _hq2_row('hq2 input mode:', QComboBox())
        self._hq2_input_mode.addItem('selected_channels_from_source', 'selected_channels_from_source')
        self._hq2_input_mode.addItem('step1_weighted_fusion', 'step1_weighted_fusion')
        self._hq2_input_mode.addItem('hybrid', 'hybrid')

        self._hq2_radius = _hq2_row('hq2 max cell radius:', QDoubleSpinBox())
        self._hq2_radius.setRange(1, 300)
        self._hq2_radius.setSingleStep(1)
        self._hq2_radius.setValue(18)

        self._hq2_norm_low = _hq2_row('hq2 norm pct low:', QDoubleSpinBox())
        self._hq2_norm_low.setRange(0.0, 50.0)
        self._hq2_norm_low.setSingleStep(0.5)
        self._hq2_norm_low.setValue(1.0)

        self._hq2_norm_high = _hq2_row('hq2 norm pct high:', QDoubleSpinBox())
        self._hq2_norm_high.setRange(50.0, 100.0)
        self._hq2_norm_high.setSingleStep(0.5)
        self._hq2_norm_high.setValue(99.5)

        self._hq2_consensus = _hq2_row('hq2 level1 consensus:', QComboBox())
        for mode in CONSENSUS_MODES:
            self._hq2_consensus.addItem(mode, mode)

        self._hq2_weights = _hq2_row('hq2 channel weights:', QtWidgets.QLineEdit())
        self._hq2_weights.setPlaceholderText('optional: CD68=1;CD206=1;CD45=0.8')
        self._hq2_weights.setStyleSheet('font-size:11px;')

        self._hq2_min_signal = _hq2_row('hq2 min signal:', QDoubleSpinBox())
        self._hq2_min_signal.setRange(0.0, 1.0)
        self._hq2_min_signal.setSingleStep(0.01)
        self._hq2_min_signal.setValue(0.08)

        self._hq2_section = QLabel('HQ2: ImageJ-style proposal')
        self._hq2_section.setStyleSheet('color:#7dd3fc;font-size:11px;font-weight:bold;padding-top:4px;')
        cpl.addWidget(self._hq2_section)
        self._hq2_widgets.append(self._hq2_section)

        self._hq2_imagej_blur = _hq2_row('imagej blur sigma:', QDoubleSpinBox())
        self._hq2_imagej_blur.setRange(0.0, 10.0)
        self._hq2_imagej_blur.setSingleStep(0.1)
        self._hq2_imagej_blur.setValue(1.0)

        self._hq2_bg_radius = _hq2_row('imagej background radius:', QtWidgets.QSpinBox())
        self._hq2_bg_radius.setRange(0, 300)
        self._hq2_bg_radius.setValue(20)

        self._hq2_threshold_method = _hq2_row('imagej threshold:', QComboBox())
        for mode in ('adaptive', 'otsu', 'percentile'):
            self._hq2_threshold_method.addItem(mode, mode)

        self._hq2_threshold_percentile = _hq2_row('threshold percentile:', QDoubleSpinBox())
        self._hq2_threshold_percentile.setRange(0.0, 100.0)
        self._hq2_threshold_percentile.setValue(75.0)

        self._hq2_min_object = _hq2_row('imagej min object:', QtWidgets.QSpinBox())
        self._hq2_min_object.setRange(0, 100000)
        self._hq2_min_object.setValue(20)

        self._hq2_closing = _hq2_row('imagej closing radius:', QtWidgets.QSpinBox())
        self._hq2_closing.setRange(0, 50)
        self._hq2_closing.setValue(2)

        self._hq2_opening = _hq2_row('imagej opening radius:', QtWidgets.QSpinBox())
        self._hq2_opening.setRange(0, 50)
        self._hq2_opening.setValue(1)

        self._hq2_core_mode = _hq2_row('core mode:', QComboBox())
        for mode in ('weighted_support', 'intersection', 'majority_support'):
            self._hq2_core_mode.addItem(mode, mode)

        self._hq2_min_core = _hq2_row('min core area:', QtWidgets.QSpinBox())
        self._hq2_min_core.setRange(0, 100000)
        self._hq2_min_core.setValue(8)

        self._hq2_signal_mode = _hq2_row('signal map mode:', QComboBox())
        for mode in ('per_cell_best_channel', 'max', 'weighted_max'):
            self._hq2_signal_mode.addItem(mode, mode)

        self._hq2_min_cont_signal = _hq2_row('min continuous signal:', QDoubleSpinBox())
        self._hq2_min_cont_signal.setRange(0.0, 1.0)
        self._hq2_min_cont_signal.setSingleStep(0.01)
        self._hq2_min_cont_signal.setValue(0.08)

        self._hq2_max_expansion = _hq2_row('max expansion radius:', QDoubleSpinBox())
        self._hq2_max_expansion.setRange(0, 300)
        self._hq2_max_expansion.setValue(25)

        self._hq2_boundary_weight = _hq2_row('boundary gradient weight:', QDoubleSpinBox())
        self._hq2_boundary_weight.setRange(0.0, 10.0)
        self._hq2_boundary_weight.setSingleStep(0.05)
        self._hq2_boundary_weight.setValue(0.25)

        self._hq2_distance_weight = _hq2_row('distance penalty weight:', QDoubleSpinBox())
        self._hq2_distance_weight.setRange(0.0, 10.0)
        self._hq2_distance_weight.setSingleStep(0.01)
        self._hq2_distance_weight.setValue(0.02)

        self._hq2_neighbor_weight = _hq2_row('neighbor nucleus penalty:', QDoubleSpinBox())
        self._hq2_neighbor_weight.setRange(0.0, 10.0)
        self._hq2_neighbor_weight.setSingleStep(0.05)
        self._hq2_neighbor_weight.setValue(0.15)

        self._hq2_irregular = _hq2_row('allow irregular shape:', QCheckBox('True'))
        self._hq2_irregular.setChecked(True)

        self._hq2_macrophage_channels = _hq2_row('macrophage channels:', QtWidgets.QLineEdit('CD68;CD206'))
        self._hq2_macrophage_channels.setStyleSheet('font-size:11px;')

        self._hq2_macrophage_radius = _hq2_row('macrophage max radius:', QDoubleSpinBox())
        self._hq2_macrophage_radius.setRange(0, 500)
        self._hq2_macrophage_radius.setValue(35)

        self._hq2_macrophage_signal = _hq2_row('macrophage min signal:', QDoubleSpinBox())
        self._hq2_macrophage_signal.setRange(0.0, 1.0)
        self._hq2_macrophage_signal.setSingleStep(0.01)
        self._hq2_macrophage_signal.setValue(0.08)

        self._mesmer_widgets = []
        def _mesmer_row(label, widget):
            r, l, w = _param_row(label, widget)
            cpl.addLayout(r)
            self._mesmer_widgets.extend([l, w])
            return w

        self._mesmer_section = QLabel('Mesmer parameters')
        self._mesmer_section.setStyleSheet('color:#56b6c2;font-size:11px;font-weight:bold;padding-top:4px;')
        cpl.addWidget(self._mesmer_section)
        self._mesmer_widgets.append(self._mesmer_section)
        self._mesmer_nuclear_channel = _mesmer_row('nuclear_channel:', QtWidgets.QLineEdit('DAPI'))
        self._mesmer_membrane_channels = _mesmer_row('membrane_channels:', QtWidgets.QLineEdit())
        self._mesmer_membrane_channels.setPlaceholderText('PanCK;CD45;CD68;HLA-DR')
        self._mesmer_input_mode = _mesmer_row('input_mode:', QComboBox())
        for label, value in (
            ('DAPI only', 'DAPI only'),
            ('DAPI + Fusion channel', 'step1_weighted_fusion'),
            ('DAPI + membrane channels', 'selected_channels'),
            ('selected channels', 'selected_channels'),
        ):
            self._mesmer_input_mode.addItem(label, value)
        self._mesmer_use_gpu = _mesmer_row('use_gpu:', QComboBox())
        for label, value in (('Auto', 'auto'), ('GPU', 'gpu'), ('CPU', 'cpu')):
            self._mesmer_use_gpu.addItem(label, value)
        self._mesmer_tile_size = _mesmer_row('tile_size:', QtWidgets.QSpinBox())
        self._mesmer_tile_size.setRange(0, 8192)
        self._mesmer_tile_size.setSingleStep(128)
        self._mesmer_tile_size.setSpecialValueText('from Step2 Tile Grid')
        self._mesmer_tile_size.setToolTip('Step2 uses Rows × Cols and Tile Grid overlap. This optional Mesmer tile_size is not used unless future internal Mesmer tiling is enabled.')
        self._mesmer_tile_size.setValue(0)
        self._mesmer_tile_size.setEnabled(False)
        self._mesmer_overlap = _mesmer_row('overlap:', QtWidgets.QSpinBox())
        self._mesmer_overlap.setRange(0, 1024)
        self._mesmer_overlap.setSingleStep(32)
        self._mesmer_overlap.setSpecialValueText('from Step2 Tile Grid')
        self._mesmer_overlap.setToolTip('Step2 uses the Tile Grid overlap above for reading padded tiles. This optional Mesmer overlap is not used in current Step2.')
        self._mesmer_overlap.setValue(0)
        self._mesmer_overlap.setEnabled(False)
        self._mesmer_batch_size = _mesmer_row('batch_size:', QtWidgets.QSpinBox())
        self._mesmer_batch_size.setRange(1, 32)
        self._mesmer_batch_size.setValue(1)
        self._mesmer_mpp = _mesmer_row('image_mpp:', QDoubleSpinBox())
        self._mesmer_mpp.setRange(0.01, 10)
        self._mesmer_mpp.setSingleStep(0.05)
        self._mesmer_mpp.setValue(0.5)
        self._mesmer_norm = _mesmer_row('normalize_input:', QCheckBox('True'))
        self._mesmer_norm.setChecked(True)
        self._mesmer_low = _mesmer_row('percentile_low:', QDoubleSpinBox())
        self._mesmer_low.setRange(0, 50)
        self._mesmer_low.setValue(1.0)
        self._mesmer_high = _mesmer_row('percentile_high:', QDoubleSpinBox())
        self._mesmer_high.setRange(50, 100)
        self._mesmer_high.setValue(99.8)
        self._mesmer_min_size = _mesmer_row('postprocess_min_size:', QtWidgets.QSpinBox())
        self._mesmer_min_size.setRange(0, 100000)
        self._mesmer_min_size.setValue(0)
        self._mesmer_step2_tiling_hint = QLabel(
            'Step2 tiling is controlled by Tile Grid: Rows × Cols plus Overlap (px). '
            'Mesmer tile_size/overlap are optional and left empty here.'
        )
        self._mesmer_step2_tiling_hint.setStyleSheet('color:#999;font-size:10px;')
        self._mesmer_step2_tiling_hint.setWordWrap(True)
        cpl.addWidget(self._mesmer_step2_tiling_hint)
        self._mesmer_widgets.append(self._mesmer_step2_tiling_hint)

        self._method_hint = QLabel('')
        self._method_hint.setStyleSheet('color:#999;font-size:10px;')
        self._method_hint.setWordWrap(True)
        cpl.addWidget(self._method_hint)

        rl.addWidget(cp_box)
        self._update_param_source_ui()
        self._on_method_changed()

        # Output settings
        out_box = QGroupBox('Output')
        out_box.setStyleSheet(self._box_style('#98c379'))
        outl = QVBoxLayout(out_box)

        outr = QHBoxLayout()
        outr.addWidget(QLabel('Output dir:'))
        self._out_edit = QtWidgets.QLineEdit(OUTPUT_DIR)
        self._out_edit.setStyleSheet('font-size:11px;')
        outr.addWidget(self._out_edit, stretch=1)
        btn_out = QPushButton('Browse')
        btn_out.setFixedWidth(64)
        btn_out.clicked.connect(self._browse_out)
        outr.addWidget(btn_out)
        outl.addLayout(outr)

        out_info = QLabel(
            'Each run writes: segmentation_results/<timestamp>_<method>/\n'
            'Global: global_mask.zarr | global_mask.ome.tiff | global_dapi.ome.tiff\n'
            'Per-tile: tile_masks/tile_r*_c*_dapi.ome.tiff | tile_r*_c*_raw_mask.ome.tiff'
        )
        out_info.setStyleSheet('color:#888;font-size:10px;')
        outl.addWidget(out_info)
        rl.addWidget(out_box)

        # Recovery mode (collapsed by default)
        rec_box = QGroupBox('Recovery Mode (merge from saved .npy files)')
        rec_box.setStyleSheet(self._box_style('#e06c75'))
        rec_box.setCheckable(True)
        rec_box.setChecked(False)
        recl = QVBoxLayout(rec_box)

        rec_info = QLabel(
            'Use this if a previous run saved tile masks to disk.\n'
            'Select the directory containing tile_r_c.npy files.'
        )
        rec_info.setStyleSheet('color:#aaa;font-size:10px;')
        recl.addWidget(rec_info)

        rr = QHBoxLayout()
        self._rec_edit = QtWidgets.QLineEdit()
        self._rec_edit.setPlaceholderText('Directory with .npy tile masks…')
        rr.addWidget(self._rec_edit, stretch=1)
        btn_rec = QPushButton('Browse')
        btn_rec.setFixedWidth(64)
        btn_rec.clicked.connect(
            lambda: self._rec_edit.setText(
                QFileDialog.getExistingDirectory(self, 'Select .npy directory')
            )
        )
        rr.addWidget(btn_rec)
        recl.addLayout(rr)
        self._rec_box = rec_box
        rl.addWidget(rec_box)

        rl.addStretch()
        right_scroll.setWidget(right_w)
        split.addWidget(right_scroll)

        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 1)
        root.addWidget(split, stretch=1)

        # ── Bottom navigation ─────────────────────────────────────────
        nav = QHBoxLayout()

        self._btn_back = QPushButton('← Back to Step 1')
        self._btn_back.setStyleSheet(
            'QPushButton{color:#fa8;border:1px solid #fa8;'
            'border-radius:4px;padding:6px 16px;}'
            'QPushButton:hover{background:#321;}'
        )
        self._btn_back.clicked.connect(self.go_back.emit)
        nav.addWidget(self._btn_back)
        nav.addStretch()
        # REMOVE: redundant navigation button (use top-right Next only)

        self._btn_run = QPushButton('▶  Run Segmentation & Merge')
        self._btn_run.setStyleSheet(
            'QPushButton{background:#2a5;color:white;border-radius:4px;'
            'padding:7px 22px;font-size:13px;font-weight:bold;}'
            'QPushButton:hover{background:#3b6;}'
            'QPushButton:disabled{background:#333;color:#555;}'
        )
        self._btn_run.clicked.connect(self._run)
        nav.addWidget(self._btn_run)

        self._btn_stop = QPushButton('⏹ Stop')
        self._btn_stop.setEnabled(False)
        self._btn_stop.setStyleSheet(
            'QPushButton{background:#722;color:white;border-radius:4px;padding:7px 14px;}'
            'QPushButton:hover{background:#944;}'
            'QPushButton:disabled{background:#333;color:#555;}'
        )
        self._btn_stop.clicked.connect(self._stop)
        nav.addWidget(self._btn_stop)

        root.addLayout(nav)

    # ── utilities ─────────────────────────────────────────────────────

    @staticmethod
    def _lbl(text, bold=False):
        l = QLabel(text)
        l.setAlignment(Qt.AlignCenter)
        s = 'font-size:12px;color:#ddd;background:#1a1a1a;padding:3px;'
        if bold:
            s += 'font-weight:bold;'
        l.setStyleSheet(s)
        return l

    @staticmethod
    def _box_style(color):
        return (
            f'QGroupBox{{border:1px solid {color};border-radius:5px;'
            f'margin-top:4px;font-weight:bold;color:{color};font-size:11px;}}'
        )

    # ── zarr loading ──────────────────────────────────────────────────

    def set_zarr_path(self, path):
        """Called from MainWindow when Step 1 finishes generating fused.zarr."""
        if path and os.path.exists(path):
            self._zarr_edit.setText(path)
            self._sync_output_dir_from_zarr_path(path)
            self._load_zarr_info()

    def set_rois(self, rois):
        """Receive ROI list from Step 1 for per-ROI segmentation."""
        self._rois = rois if rois else []
        if self._rois:
            names = [r["name"] for r in self._rois]
            self._zarr_info.setText(
                (self._zarr_info.text() or "") +
                f"\nROIs: {names}  (will segment each independently)"
            )

    def _browse_zarr(self):
        path = QFileDialog.getExistingDirectory(self, 'Select fused.zarr directory')
        if path:
            self._zarr_edit.setText(path)
            self._sync_output_dir_from_zarr_path(path)
            self._load_zarr_info()

    def _browse_seg_params(self):
        dlg = QFileDialog(self, 'Select segmentation_params directory or index JSON', OUTPUT_DIR)
        dlg.setOption(QFileDialog.DontUseNativeDialog, True)
        dlg.setFileMode(QFileDialog.AnyFile)
        dlg.setNameFilter('Segmentation params index (segmentation_params_index.json);;JSON (*.json);;All files (*)')
        if dlg.exec_():
            selected = dlg.selectedFiles()
            if selected:
                self.set_segmentation_params_path(selected[0], load=True)

    def set_segmentation_params_path(self, path, load=False):
        display_path = os.path.abspath(path) if path else ""
        self._seg_params_edit.setText(display_path)
        if not load:
            self._seg_param_file = display_path
            return
        if display_path and self._load_seg_params_index(display_path, silent=False, apply_active=False):
            self._set_param_source("index")
            return
        if display_path:
            QMessageBox.warning(self, 'Error', 'Select segmentation_params/ or segmentation_params_index.json.')

    def _resolve_seg_params_index_path(self, path):
        path = os.path.abspath(path or "")
        if not path:
            return ""
        if os.path.isfile(path):
            return path if os.path.basename(path) == PARAM_INDEX else ""
        if os.path.isdir(path):
            direct = os.path.join(path, PARAM_INDEX)
            if os.path.exists(direct):
                return direct
            nested = params_index_path(path)
            if os.path.exists(nested):
                return nested
        return ""

    def _param_path_from_index(self, rel_or_abs):
        rel_or_abs = rel_or_abs or ""
        if not rel_or_abs:
            return ""
        return os.path.abspath(rel_or_abs) if os.path.isabs(rel_or_abs) else os.path.abspath(os.path.join(self._seg_params_dir, rel_or_abs))

    def _method_display_name(self, method):
        try:
            return get_segmentation_method_config(method).get("display_name") or method
        except Exception:
            return method

    def _load_seg_params_index(self, path, silent=True, apply_active=False):
        index_path = self._resolve_seg_params_index_path(path)
        if not index_path:
            if not silent:
                QMessageBox.warning(self, 'Error', 'Select segmentation_params/ or segmentation_params_index.json.')
            return False
        try:
            with open(index_path, 'r', encoding='utf-8') as f:
                index = json.load(f)
            if not isinstance(index, dict):
                raise ValueError('segmentation_params_index.json is not a JSON object.')
            methods = index.get("methods") or {}
            if not methods:
                raise ValueError('segmentation_params_index.json has no methods.')
            self._seg_params_index = index
            self._seg_params_dir = os.path.dirname(index_path)
            self._seg_params_edit.setText(index_path)
            active_method = index.get("active_method") or next(iter(methods))
            if active_method not in methods:
                active_method = next(iter(methods))
            active_file = index.get("active_param_file") if active_method == index.get("active_method") else ""
            active_file = active_file or (methods.get(active_method) or {}).get("latest") or ""

            self._loading_index_selection = True
            try:
                self._index_method_combo.clear()
                for method in methods.keys():
                    self._index_method_combo.addItem(self._method_display_name(method), method)
                idx = self._index_method_combo.findData(active_method)
                self._index_method_combo.setCurrentIndex(idx if idx >= 0 else 0)
                self._populate_history_for_method(active_method, active_file)
                self._update_resolved_param_path()
            finally:
                self._loading_index_selection = False

            ok = True
            if apply_active:
                ok = self._apply_index_method(active_method, active_file, silent=silent)
            if ok:
                print("[Step2] loaded segmentation params index")
                print(f"[Step2] index={index_path}")
            return ok
        except Exception as e:
            if not silent:
                QMessageBox.warning(self, 'Error', str(e))
            else:
                print(f'[Step2] failed to load segmentation params index: {e}')
            return False

    def _populate_history_for_method(self, method, selected_file=""):
        info = (self._seg_params_index.get("methods") or {}).get(method) or {}
        latest = info.get("latest") or ""
        history = []
        if latest:
            history.append(latest)
        for item in list(info.get("history") or []):
            if item and item not in history:
                history.append(item)
        selected_file = selected_file or latest
        self._history_combo.blockSignals(True)
        try:
            self._history_combo.clear()
            for filename in history:
                label = f"latest: {filename}" if filename == latest else filename
                self._history_combo.addItem(label, filename)
            idx = self._history_combo.findData(selected_file)
            self._history_combo.setCurrentIndex(idx if idx >= 0 else max(0, self._history_combo.findData(latest)))
            has_history = self._history_combo.count() > 0
            self._history_combo.setVisible(has_history)
            self._history_label.setVisible(has_history)
        finally:
            self._history_combo.blockSignals(False)

    def _load_index_method(self, method, filename="", silent=True):
        return self._apply_index_method(method, filename, silent=silent)

    def _apply_index_method(self, method, filename="", silent=True):
        info = (self._seg_params_index.get("methods") or {}).get(method) or {}
        filename = filename or info.get("latest") or ""
        path = self._param_path_from_index(filename)
        if not path or not os.path.exists(path):
            if not silent:
                QMessageBox.warning(self, 'Error', f'Segmentation params file not found:\n{path}')
            return False
        self._populate_history_for_method(method, filename)
        old = self._loading_index_selection
        self._loading_index_selection = True
        try:
            return self._load_seg_params_file(path, silent=silent)
        finally:
            self._loading_index_selection = old

    def _load_seg_params_file(self, path, silent=True):
        if not path or not os.path.exists(path):
            if not silent:
                QMessageBox.warning(self, 'Error', 'Segmentation params file not found.')
            return False
        try:
            with open(path, 'r', encoding='utf-8') as f:
                cfg = normalize_segmentation_config(json.load(f))
            self._apply_seg_config_to_ui(cfg)
            self._seg_config = cfg
            self._seg_param_file = os.path.abspath(path)
            self._resolved_param_edit.setText(self._seg_param_file)
            print("[Step2] loaded active segmentation params")
            print(f"[Step2] method={cfg.get('method')}")
            print(f"[Step2] param_file={path}")
            return True
        except Exception as e:
            if not silent:
                QMessageBox.warning(self, 'Error', str(e))
            else:
                print(f'[Step2] failed to load segmentation params: {e}')
            return False

    def _on_history_changed(self):
        if self._loading_index_selection:
            return
        self._update_resolved_param_path()

    def _on_index_method_changed(self):
        if self._loading_index_selection:
            return
        method = self._index_method_combo.currentData()
        if method:
            self._populate_history_for_method(method)
            self._update_resolved_param_path()

    def _set_param_source(self, source):
        idx = self._param_source_combo.findData(source)
        if idx >= 0:
            self._param_source_combo.setCurrentIndex(idx)
        self._parameter_source = source
        self._update_param_source_ui()

    def _on_param_source_changed(self):
        source = self._param_source_combo.currentData() or "manual"
        self._parameter_source = source
        if source == "manual":
            self._applied_param_file = ""
            self._seg_param_file = ""
            self._resolved_param_edit.setText("")
        elif source == "index" and not self._seg_params_index:
            path = self._seg_params_edit.text().strip()
            if path:
                self._load_seg_params_index(path, silent=True, apply_active=False)
        self._update_param_source_ui()

    def _update_param_source_ui(self):
        is_index = (self._param_source_combo.currentData() or "manual") == "index"
        for w in (
            self._index_method_label, self._index_method_combo,
            self._history_label, self._history_combo,
            self._apply_index_btn,
            self._resolved_param_label, self._resolved_param_edit,
        ):
            w.setVisible(is_index)

    def _update_resolved_param_path(self):
        method = self._index_method_combo.currentData()
        filename = self._history_combo.currentData()
        path = self._param_path_from_index(filename) if method and filename else ""
        self._resolved_param_edit.setText(path)
        return path

    def _apply_selected_index_params(self):
        if not self._seg_params_index:
            path = self._seg_params_edit.text().strip()
            if not self._load_seg_params_index(path, silent=False, apply_active=False):
                return False
        method = self._index_method_combo.currentData()
        filename = self._history_combo.currentData()
        if not method or not filename:
            QMessageBox.warning(self, 'Error', 'No indexed parameter version selected.')
            return False
        ok = self._apply_index_method(method, filename, silent=False)
        if ok:
            self._set_param_source("index")
            self._applied_param_file = self._seg_param_file
            self._resolved_param_edit.setText(self._seg_param_file)
        return ok

    def _browse_out(self):
        path = QFileDialog.getExistingDirectory(self, 'Select output directory')
        if path:
            self._out_edit.setText(path)

    def _load_zarr_info(self):
        path = self._zarr_edit.text().strip()
        if not path or not os.path.exists(path):
            self._zarr_info.setText('⚠ Path not found')
            return
        try:
            z = zarr.open(path, mode='r')
            self._full_h, self._full_w = z.shape[0], z.shape[1]
            attrs = dict(z.attrs)
            self._zarr_path = path
            self._sync_output_dir_from_zarr_path(path)
            self._zarr_info.setText(
                f'Shape: {self._full_h:,} × {self._full_w:,} px  '
                f'| dtype: {z.dtype}  '
                f'| ch0={attrs.get("channel_0","?")}  '
                f'ch1={attrs.get("channel_1","?")}'
            )
            self._update_tile_info()
            self._load_overview_from_zarr(z)
        except Exception as e:
            self._zarr_info.setText(f'⚠ Failed to open zarr: {e}')

    def _load_overview_from_zarr(self, z):
        """Load nucleus channel (index 1) downsampled as overview."""
        self._ov_status.setText('Loading overview…')
        try:
            ds  = 32
            # Read as uint16 then normalise — avoids float setImage issue
            arr_raw = z[::ds, ::ds, 1]
            arr     = arr_raw.astype(np.float32)
            nz      = arr[arr > 0]
            if nz.size > 100:
                lo, hi = np.percentile(nz, [1, 99.5])
                if hi > lo:
                    arr = np.clip((arr - lo) / (hi - lo), 0.0, 1.0)
                else:
                    arr = np.zeros_like(arr)
            else:
                arr = np.zeros_like(arr)

            self._ov_h  = arr.shape[0]
            self._ov_w  = arr.shape[1]
            self._ov_ds = ds

            # pyqtgraph requires levels=[min,max] when dtype is float
            self._ov_img.setImage(arr, autoLevels=False, levels=[0.0, 1.0])
            self._ov_vb.setRange(
                QRectF(0, 0, self._ov_w, self._ov_h), padding=0.01
            )
            self._ov_status.setText(
                f'Overview {self._ov_h}×{self._ov_w} px (1/{ds})'
            )
            self._draw_tile_grid()
        except Exception as e:
            import traceback as _tb
            self._ov_status.setText(f'Overview failed: {e}')
            print(f'[Step2 overview error]\n{_tb.format_exc()}')

    # ── tile grid overlay ─────────────────────────────────────────────

    def _update_tile_info(self):
        if self._full_h == 0:
            return
        self._refresh_tile_suggestion(apply=False)
        nr  = self._rows_spin.value()
        nc  = self._cols_spin.value()
        th  = -(-self._full_h // nr)
        tw  = -(-self._full_w // nc)
        # RAM estimate: tile_h × tile_w × 2 channels × 4 bytes float32 × 2 (accum)
        ram = th * tw * 2 * 4 * 2 / 1e9
        self._tile_ram_lbl.setText(
            f'{nr}×{nc} = {nr*nc} tiles  |  '
            f'tile size: {th:,}×{tw:,} px  |  '
            f'est. VRAM/tile: {ram:.1f} GB'
        )
        self._draw_tile_grid()

    def _on_auto_tile_strategy_changed(self):
        self._refresh_tile_suggestion(apply=self._auto_tile_strategy.isChecked())

    def _refresh_tile_suggestion(self, apply=False):
        if self._full_h <= 0 or self._full_w <= 0:
            return
        method = self._method_combo.currentData() if hasattr(self, "_method_combo") else CELLPOSE_WHOLECELL_FUSION
        cfg = self.get_seg_config() if hasattr(self, "_method_combo") else {}
        channel_count = 2
        if method in (CELLPOSE_NUCLEI_HQ, CELLPOSE_NUCLEI_HQ2):
            channel_count = max(2, len(parse_hq_channels(cfg.get("hq_channels") or [])) + 1)
        elif method in (MESMER_WHOLE_CELL, MESMER_NUCLEI, MESMER_NUCLEAR_GUIDED):
            channel_count = max(2, len(parse_hq_channels(cfg.get("membrane_channels") or [])) + 1)
        suggestion = suggest_tile_strategy(
            self._full_h,
            self._full_w,
            method,
            vram_gb=self._detect_vram_gb(),
            channel_count=channel_count,
        )
        self._suggested_tile_strategy = suggestion
        self._tile_suggestion_lbl.setText(
            f"Suggested: {suggestion['n_rows']}×{suggestion['n_cols']} grid "
            f"(~{suggestion['estimated_tile_mpx']:.0f} MP/tile)"
        )
        if apply:
            self._rows_spin.blockSignals(True)
            self._cols_spin.blockSignals(True)
            self._overlap_spin.blockSignals(True)
            try:
                self._rows_spin.setValue(int(suggestion["n_rows"]))
                self._cols_spin.setValue(int(suggestion["n_cols"]))
                self._overlap_spin.setValue(int(suggestion["overlap"]))
            finally:
                self._rows_spin.blockSignals(False)
                self._cols_spin.blockSignals(False)
                self._overlap_spin.blockSignals(False)
            self._update_tile_info()

    @staticmethod
    def _detect_vram_gb():
        try:
            import torch
            if torch.cuda.is_available():
                return torch.cuda.get_device_properties(0).total_memory / (1024.0 ** 3)
        except Exception:
            pass
        return None

    def _draw_tile_grid(self):
        """Overlay tile rectangles on the overview."""
        if not hasattr(self, '_ov_h') or self._full_h == 0:
            return
        nr = self._rows_spin.value()
        nc = self._cols_spin.value()

        # Remove old rects
        for item in list(self._tile_rects.values()):
            self._ov_vb.removeItem(item)
        self._tile_rects.clear()
        self._tile_status.clear()

        th  = -(-self._full_h // nr)
        tw  = -(-self._full_w // nc)
        ds  = self._ov_ds

        for r in range(nr):
            for c in range(nc):
                oy0 = r * th
                oy1 = min(oy0 + th, self._full_h)
                ox0 = c * tw
                ox1 = min(ox0 + tw, self._full_w)
                # convert to overview coords
                rect = pg.RectROI(
                    [ox0 / ds, oy0 / ds],
                    [(ox1 - ox0) / ds, (oy1 - oy0) / ds],
                    pen=pg.mkPen('#808080', width=1),
                    movable=False, resizable=False,
                )
                self._ov_vb.addItem(rect)
                self._tile_rects[(r, c)] = rect
                self._tile_status[(r, c)] = 'idle'

    def _set_tile_colour(self, row, col, state):
        rect = self._tile_rects.get((row, col))
        if rect is None:
            return
        colours = {
            'idle':    '#808080',
            'running': '#ffc832',
            'done':    '#3cc850',
            'error':   '#dc3c3c',
        }
        rect.setPen(pg.mkPen(colours.get(state, '#808080'), width=2))

    # ── Segmentation params loading ───────────────────────────────────

    def load_step1_active_params(self, output_dir):
        print("[Step2] entered from Step1")
        if not self._load_seg_params_index(output_dir, silent=True, apply_active=True):
            print("[Step2] failed to auto-load Step1 active segmentation params")
            return False
        self._set_param_source("index")
        self._applied_param_file = self._seg_param_file
        cfg = self._seg_config or {}
        print(f"[Step2] auto-loaded active method={cfg.get('method')}")
        print(f"[Step2] active_param_file={self._seg_param_file}")
        return True

    def _apply_seg_config_to_ui(self, p):
        method = p.get('method', CELLPOSE_WHOLECELL_FUSION)
        idx = self._method_combo.findData(method)
        if idx >= 0:
            self._method_combo.setCurrentIndex(idx)
            # model_type ignored in Cellpose 4.0.1+; skip UI update
            self._cp_diam.setValue(p.get('diameter') or 0)
            self._cp_flow.setValue(p.get('flow_threshold', 0.4))
            self._cp_prob.setValue(p.get('cellprob_threshold', 0.0))
            self._cp_minsize.setValue(p.get('min_size', 15))
            self._cp_gpu.setChecked(bool(p.get('use_gpu', True)))
            self._cp_tile_size.setValue(int(p.get('tile_size', 1024) or 1024))
            self._cp_batch_size.setValue(int(p.get('batch_size', 8) or 8))
        self._sd_model.setText(str(p.get('model_name') or '2D_versatile_fluo'))
        self._sd_prob.setValue(-1.0 if p.get('prob_thresh') is None else float(p.get('prob_thresh')))
        self._sd_nms.setValue(-1.0 if p.get('nms_thresh') is None else float(p.get('nms_thresh')))
        self._sd_expand.setValue(float(p.get('expand_distance', 8) or 0))
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
        self._hq2_channels.setText(";".join(parse_hq_channels(p.get("hq_channels") or [])))
        idx = self._hq2_input_mode.findData(p.get("hq_input_mode", "selected_channels_from_source"))
        self._hq2_input_mode.setCurrentIndex(max(0, idx))
        self._hq2_radius.setValue(float(p.get("max_cell_radius", 18) or 18))
        self._hq2_norm_low.setValue(float(p.get("normalization_percentile_low", 1.0)))
        self._hq2_norm_high.setValue(float(p.get("normalization_percentile_high", 99.5)))
        idx = self._hq2_consensus.findData(p.get("consensus_mode", "adaptive_best_channel"))
        self._hq2_consensus.setCurrentIndex(max(0, idx))
        self._hq2_weights.setText(";".join(f"{k}={v}" for k, v in weights.items()))
        self._hq2_min_signal.setValue(float(p.get("min_signal_threshold", 0.08)))
        self._hq2_imagej_blur.setValue(float(p.get("imagej_blur_sigma", 1.0)))
        self._hq2_bg_radius.setValue(int(p.get("imagej_background_radius", 20) or 0))
        idx = self._hq2_threshold_method.findData(p.get("imagej_threshold_method", "adaptive"))
        self._hq2_threshold_method.setCurrentIndex(max(0, idx))
        self._hq2_threshold_percentile.setValue(float(p.get("imagej_threshold_percentile", 75.0)))
        self._hq2_min_object.setValue(int(p.get("imagej_min_object_size", 20) or 0))
        self._hq2_closing.setValue(int(p.get("imagej_closing_radius", 2) or 0))
        self._hq2_opening.setValue(int(p.get("imagej_opening_radius", 1) or 0))
        idx = self._hq2_core_mode.findData(p.get("core_mode", "weighted_support"))
        self._hq2_core_mode.setCurrentIndex(max(0, idx))
        self._hq2_min_core.setValue(int(p.get("min_core_area", 8) or 0))
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
        if method in (MESMER_WHOLE_CELL, MESMER_NUCLEI, MESMER_NUCLEAR_GUIDED):
            self._mesmer_nuclear_channel.setText(str(p.get("nuclear_channel", "DAPI") or "DAPI"))
            self._mesmer_membrane_channels.setText(";".join(parse_hq_channels(p.get("membrane_channels") or [])))
            idx = self._mesmer_input_mode.findData(p.get("input_mode", "selected_channels"))
            self._mesmer_input_mode.setCurrentIndex(max(0, idx))
            idx = self._mesmer_use_gpu.findData(str(p.get("use_gpu", "auto")).lower())
            self._mesmer_use_gpu.setCurrentIndex(max(0, idx))
            self._mesmer_tile_size.setValue(0)
            self._mesmer_overlap.setValue(0)
            self._mesmer_batch_size.setValue(int(p.get("batch_size", 1) or 1))
            self._mesmer_mpp.setValue(float(p.get("image_mpp", p.get("pixel_size", 0.5)) or 0.5))
            self._mesmer_norm.setChecked(bool(p.get("normalize_input", True)))
            self._mesmer_low.setValue(float(p.get("percentile_low", 1.0)))
            self._mesmer_high.setValue(float(p.get("percentile_high", 99.8)))
            self._mesmer_min_size.setValue(int(p.get("postprocess_min_size", 0) or 0))
        self._on_method_changed()

    def _apply_method_defaults_to_ui(self, method):
        cfg = normalize_segmentation_config({"method": method})
        self._cp_diam.setValue(cfg.get('diameter') or 0)
        self._cp_flow.setValue(cfg.get('flow_threshold', 0.4))
        self._cp_prob.setValue(cfg.get('cellprob_threshold', 0.0))
        self._cp_minsize.setValue(cfg.get('min_size', 15))
        self._cp_gpu.setChecked(bool(cfg.get('use_gpu', True)))
        self._cp_tile_size.setValue(int(cfg.get('tile_size', 1024) or 1024))
        self._cp_batch_size.setValue(int(cfg.get('batch_size', 8) or 8))
        self._sd_model.setText(str(cfg.get('model_name') or '2D_versatile_fluo'))
        self._sd_prob.setValue(-1.0 if cfg.get('prob_thresh') is None else float(cfg.get('prob_thresh')))
        self._sd_nms.setValue(-1.0 if cfg.get('nms_thresh') is None else float(cfg.get('nms_thresh')))
        self._sd_expand.setValue(float(cfg.get('expand_distance', 8) or 0))
        self._hq_channels.setText(";".join(parse_hq_channels(cfg.get("hq_channels") or [])))
        idx = self._hq_input_mode.findData(cfg.get("hq_input_mode", "selected_channels_from_source"))
        self._hq_input_mode.setCurrentIndex(max(0, idx))
        self._hq_radius.setValue(float(cfg.get("max_cell_radius", 12) or 12))
        self._hq_norm_low.setValue(float(cfg.get("normalization_percentile_low", 1.0)))
        self._hq_norm_high.setValue(float(cfg.get("normalization_percentile_high", 99.5)))
        idx = self._hq_consensus.findData(cfg.get("consensus_mode", "adaptive_best_channel"))
        self._hq_consensus.setCurrentIndex(max(0, idx))
        self._hq_weights.setText("")
        self._hq_min_signal.setValue(float(cfg.get("min_signal_threshold", 0.08)))
        self._hq2_channels.setText(";".join(parse_hq_channels(cfg.get("hq_channels") or [])))
        idx = self._hq2_input_mode.findData(cfg.get("hq_input_mode", "selected_channels_from_source"))
        self._hq2_input_mode.setCurrentIndex(max(0, idx))
        self._hq2_radius.setValue(float(cfg.get("max_cell_radius", 18) or 18))
        self._hq2_norm_low.setValue(float(cfg.get("normalization_percentile_low", 1.0)))
        self._hq2_norm_high.setValue(float(cfg.get("normalization_percentile_high", 99.5)))
        idx = self._hq2_consensus.findData(cfg.get("consensus_mode", "adaptive_best_channel"))
        self._hq2_consensus.setCurrentIndex(max(0, idx))
        self._hq2_weights.setText("")
        self._hq2_min_signal.setValue(float(cfg.get("min_signal_threshold", 0.08)))
        self._hq2_imagej_blur.setValue(float(cfg.get("imagej_blur_sigma", 1.0)))
        self._hq2_bg_radius.setValue(int(cfg.get("imagej_background_radius", 20) or 0))
        idx = self._hq2_threshold_method.findData(cfg.get("imagej_threshold_method", "adaptive"))
        self._hq2_threshold_method.setCurrentIndex(max(0, idx))
        self._hq2_threshold_percentile.setValue(float(cfg.get("imagej_threshold_percentile", 75.0)))
        self._hq2_min_object.setValue(int(cfg.get("imagej_min_object_size", 20) or 0))
        self._hq2_closing.setValue(int(cfg.get("imagej_closing_radius", 2) or 0))
        self._hq2_opening.setValue(int(cfg.get("imagej_opening_radius", 1) or 0))
        idx = self._hq2_core_mode.findData(cfg.get("core_mode", "weighted_support"))
        self._hq2_core_mode.setCurrentIndex(max(0, idx))
        self._hq2_min_core.setValue(int(cfg.get("min_core_area", 8) or 0))
        idx = self._hq2_signal_mode.findData(cfg.get("signal_map_mode", "per_cell_best_channel"))
        self._hq2_signal_mode.setCurrentIndex(max(0, idx))
        self._hq2_min_cont_signal.setValue(float(cfg.get("min_continuous_signal", 0.08)))
        self._hq2_max_expansion.setValue(float(cfg.get("max_expansion_radius", 25)))
        self._hq2_boundary_weight.setValue(float(cfg.get("boundary_gradient_weight", 0.25)))
        self._hq2_distance_weight.setValue(float(cfg.get("distance_penalty_weight", 0.02)))
        self._hq2_neighbor_weight.setValue(float(cfg.get("neighbor_nucleus_penalty_weight", 0.15)))
        self._hq2_irregular.setChecked(bool(cfg.get("allow_irregular_shape", True)))
        self._hq2_macrophage_channels.setText(str(cfg.get("macrophage_channels", "CD68;CD206") or ""))
        self._hq2_macrophage_radius.setValue(float(cfg.get("macrophage_max_radius", 35)))
        self._hq2_macrophage_signal.setValue(float(cfg.get("macrophage_min_signal", 0.08)))
        if method in (MESMER_WHOLE_CELL, MESMER_NUCLEI, MESMER_NUCLEAR_GUIDED):
            self._mesmer_nuclear_channel.setText(str(cfg.get("nuclear_channel", "DAPI") or "DAPI"))
            self._mesmer_membrane_channels.setText(";".join(parse_hq_channels(cfg.get("membrane_channels") or [])))
            idx = self._mesmer_input_mode.findData(cfg.get("input_mode", "selected_channels"))
            self._mesmer_input_mode.setCurrentIndex(max(0, idx))
            idx = self._mesmer_use_gpu.findData(str(cfg.get("use_gpu", "auto")).lower())
            self._mesmer_use_gpu.setCurrentIndex(max(0, idx))
            self._mesmer_tile_size.setValue(0)
            self._mesmer_overlap.setValue(0)
            self._mesmer_batch_size.setValue(int(cfg.get("batch_size", 1) or 1))
            self._mesmer_mpp.setValue(float(cfg.get("image_mpp", cfg.get("pixel_size", 0.5)) or 0.5))
            self._mesmer_norm.setChecked(bool(cfg.get("normalize_input", True)))
            self._mesmer_low.setValue(float(cfg.get("percentile_low", 1.0)))
            self._mesmer_high.setValue(float(cfg.get("percentile_high", 99.8)))
            self._mesmer_min_size.setValue(int(cfg.get("postprocess_min_size", 0) or 0))

    def get_cp_params(self):
        return self.get_seg_config()

    def get_seg_config(self):
        method = self._method_combo.currentData() or CELLPOSE_WHOLECELL_FUSION
        diam = self._cp_diam.value()
        data = dict(self._seg_config or {})
        params = dict(get_segmentation_method_config(method).get("params") or {})
        if method in (MESMER_WHOLE_CELL, MESMER_NUCLEI, MESMER_NUCLEAR_GUIDED):
            mode = {
                MESMER_WHOLE_CELL: "whole_cell",
                MESMER_NUCLEI: "nuclei",
                MESMER_NUCLEAR_GUIDED: "nuclear_guided",
            }.get(method, "whole_cell")
            params.update({
                "mesmer_mode": mode,
                "nuclear_channel": self._mesmer_nuclear_channel.text().strip() or "DAPI",
                "membrane_channels": parse_hq_channels(self._mesmer_membrane_channels.text()),
                "input_mode": self._mesmer_input_mode.currentData() or "selected_channels",
                "compartment": "nuclear" if method == MESMER_NUCLEI else "whole-cell",
                "use_gpu": self._mesmer_use_gpu.currentData() or "auto",
                "tile_size": None if self._mesmer_tile_size.value() <= 0 else self._mesmer_tile_size.value(),
                "overlap": None if self._mesmer_overlap.value() <= 0 else self._mesmer_overlap.value(),
                "batch_size": self._mesmer_batch_size.value(),
                "image_mpp": self._mesmer_mpp.value(),
                "pixel_size": self._mesmer_mpp.value(),
                "normalize_input": self._mesmer_norm.isChecked(),
                "percentile_low": self._mesmer_low.value(),
                "percentile_high": self._mesmer_high.value(),
                "postprocess_min_size": self._mesmer_min_size.value(),
            })
        if method == CELLPOSE_NUCLEI_HQ2:
            hq_channels = parse_hq_channels(self._hq2_channels.text())
            hq_input_mode = self._hq2_input_mode.currentData() or 'selected_channels_from_source'
            max_cell_radius = self._hq2_radius.value()
            norm_low = self._hq2_norm_low.value()
            norm_high = self._hq2_norm_high.value()
            consensus_mode = self._hq2_consensus.currentData() or 'adaptive_best_channel'
            channel_weights = parse_channel_weights(self._hq2_weights.text(), hq_channels)
            min_signal = self._hq2_min_signal.value()
        elif method == CELLPOSE_NUCLEI_HQ:
            hq_channels = parse_hq_channels(self._hq_channels.text())
            hq_input_mode = self._hq_input_mode.currentData() or 'selected_channels_from_source'
            max_cell_radius = self._hq_radius.value()
            norm_low = self._hq_norm_low.value()
            norm_high = self._hq_norm_high.value()
            consensus_mode = self._hq_consensus.currentData() or 'adaptive_best_channel'
            channel_weights = parse_channel_weights(self._hq_weights.text(), hq_channels)
            min_signal = self._hq_min_signal.value()
        else:
            hq_channels = []
            hq_input_mode = 'selected_channels_from_source'
            max_cell_radius = params.get('max_cell_radius', 12)
            norm_low = params.get('normalization_percentile_low', 1.0)
            norm_high = params.get('normalization_percentile_high', 99.5)
            consensus_mode = params.get('consensus_mode', 'adaptive_best_channel')
            channel_weights = {}
            min_signal = params.get('min_signal_threshold', 0.08)
        params.update({
            'model_name':         self._sd_model.text().strip() or '2D_versatile_fluo',
            'prob_thresh':        None if self._sd_prob.value() < 0 else self._sd_prob.value(),
            'nms_thresh':         None if self._sd_nms.value() < 0 else self._sd_nms.value(),
            'expand_distance':    self._sd_expand.value(),
            'hq_channels':        hq_channels,
            'hq_input_mode':      hq_input_mode,
            'max_cell_radius':    max_cell_radius,
            'normalization_percentile_low': norm_low,
            'normalization_percentile_high': norm_high,
            'consensus_mode':     consensus_mode,
            'channel_weights':    channel_weights,
            'min_signal_threshold': min_signal,
            'imagej_blur_sigma':  self._hq2_imagej_blur.value(),
            'imagej_background_radius': self._hq2_bg_radius.value(),
            'imagej_threshold_method': self._hq2_threshold_method.currentData() or 'adaptive',
            'imagej_threshold_percentile': self._hq2_threshold_percentile.value(),
            'imagej_min_object_size': self._hq2_min_object.value(),
            'imagej_closing_radius': self._hq2_closing.value(),
            'imagej_opening_radius': self._hq2_opening.value(),
            'core_mode': self._hq2_core_mode.currentData() or 'weighted_support',
            'min_core_area': self._hq2_min_core.value(),
            'signal_map_mode': self._hq2_signal_mode.currentData() or 'per_cell_best_channel',
            'min_continuous_signal': self._hq2_min_cont_signal.value(),
            'max_expansion_radius': self._hq2_max_expansion.value(),
            'boundary_gradient_weight': self._hq2_boundary_weight.value(),
            'distance_penalty_weight': self._hq2_distance_weight.value(),
            'neighbor_nucleus_penalty_weight': self._hq2_neighbor_weight.value(),
            'allow_irregular_shape': self._hq2_irregular.isChecked(),
            'macrophage_channels': self._hq2_macrophage_channels.text().strip(),
            'macrophage_max_radius': self._hq2_macrophage_radius.value(),
            'macrophage_min_signal': self._hq2_macrophage_signal.value(),
        })
        if method != CELLPOSE_NUCLEI_HQ2:
            for key in (
                'imagej_blur_sigma', 'imagej_background_radius', 'imagej_threshold_method',
                'imagej_threshold_percentile', 'imagej_min_object_size',
                'imagej_closing_radius', 'imagej_opening_radius', 'core_mode',
                'min_core_area', 'signal_map_mode', 'min_continuous_signal',
                'max_expansion_radius', 'boundary_gradient_weight',
                'distance_penalty_weight', 'neighbor_nucleus_penalty_weight',
                'allow_irregular_shape', 'macrophage_channels',
                'macrophage_max_radius', 'macrophage_min_signal',
            ):
                params.pop(key, None)
        data.update({
            'method':             method,
            'params':             params,
            'model_type':         'cpsam',  # Cellpose 4.0.1+: always cpsam
            'diameter':           None if diam == 0 else diam,
            'flow_threshold':     self._cp_flow.value(),
            'cellprob_threshold': self._cp_prob.value(),
            'min_size':           self._cp_minsize.value(),
            'use_gpu':            self._cp_gpu.isChecked(),
            'tile_size':          self._cp_tile_size.value(),
            'batch_size':         self._cp_batch_size.value(),
            'tile_strategy_mode': 'auto' if self._auto_tile_strategy.isChecked() else 'manual',
            'suggested_tile_strategy': dict(self._suggested_tile_strategy or {}),
            'enable_tile_prefetch': True,
            'prefetch_queue_size': 2,
            'channel_cache_items': 32,
        })
        if method in (MESMER_WHOLE_CELL, MESMER_NUCLEI, MESMER_NUCLEAR_GUIDED):
            data.update(params)
        cfg = normalize_segmentation_config(data)
        if method == CELLPOSE_NUCLEI_HQ2:
            print(f"[HQ2-UI] collected params={cfg.get('params')}")
            print(f"[HQ2-UI] worker config keys={sorted(cfg.keys())}")
        return cfg

    def _on_method_changed(self):
        method = self._method_combo.currentData() or CELLPOSE_WHOLECELL_FUSION
        if (
            not self._loading_index_selection
            and (self._param_source_combo.currentData() or "manual") == "manual"
        ):
            self._apply_method_defaults_to_ui(method)
        is_cellpose = method in (CELLPOSE_WHOLECELL_FUSION, CELLPOSE_NUCLEI_DAPI, CELLPOSE_NUCLEI_EXPANSION, CELLPOSE_NUCLEI_HQ, CELLPOSE_NUCLEI_HQ2)
        is_stardist = method in (STARDIST_NUCLEI_DAPI, STARDIST_NUCLEI_EXPANSION)
        is_expansion = method in (CELLPOSE_NUCLEI_EXPANSION, STARDIST_NUCLEI_EXPANSION)
        is_hq = method == CELLPOSE_NUCLEI_HQ
        is_hq2 = method == CELLPOSE_NUCLEI_HQ2
        is_mesmer = method in (MESMER_WHOLE_CELL, MESMER_NUCLEI, MESMER_NUCLEAR_GUIDED)
        for w in (
            self._cp_model_label, self._cp_model_lbl,
            self._cp_diam_label, self._cp_diam,
            self._cp_flow_label, self._cp_flow,
            self._cp_prob_label, self._cp_prob,
            self._cp_minsize_label, self._cp_minsize,
            self._cp_gpu_label, self._cp_gpu,
            self._cp_tile_size_label, self._cp_tile_size,
            self._cp_batch_size_label, self._cp_batch_size,
        ):
            w.setVisible(is_cellpose)
        for w in (
            self._sd_model_label, self._sd_model,
            self._sd_prob_label, self._sd_prob,
            self._sd_nms_label, self._sd_nms,
        ):
            w.setVisible(is_stardist)
        self._sd_expand_label.setVisible(is_expansion)
        self._sd_expand.setVisible(is_expansion)
        for w in self._hq_widgets:
            w.setVisible(is_hq)
        for w in self._hq2_widgets:
            w.setVisible(is_hq2)
        for w in self._mesmer_widgets:
            w.setVisible(is_mesmer)
        cfg = get_segmentation_method_config(method)
        self._method_hint.setText(
            f'{method} | input={cfg.get("input_type")} | output={cfg.get("output_type")}'
        )
        preview_btn = getattr(self, "_btn_patch_preview", None)
        print("[Step2] method changed:", method)
        print("[Step2] is_hq:", is_hq)
        print("[Step2] hq widgets visible:", self._hq_channels.isVisible())
        print("[HQ2-UI] method selected=", method)
        print("[HQ2-UI] parameter widgets created=", bool(getattr(self, "_hq2_widgets", None)))
        print("[HQ2-UI] parameter widgets visible=", self._hq2_channels.isVisible() if hasattr(self, "_hq2_channels") else False)
        print("[HQ2-UI] patch preview enabled=", preview_btn.isEnabled() if preview_btn is not None else None)
        if getattr(self, "_full_h", 0):
            self._refresh_tile_suggestion(apply=getattr(self, "_auto_tile_strategy", None) is not None and self._auto_tile_strategy.isChecked())

    def _available_hq_channels(self):
        candidates = []
        cfg = self._seg_config or {}
        requested = parse_hq_channels(cfg.get("hq_channels") or [])
        first_available = []
        for key in ("hq_source_zarr", "multichannel_source_path", "corrected_channels_zarr"):
            if cfg.get(key):
                candidates.append(cfg.get(key))
        if self._roi_dir:
            candidates.append(os.path.join(self._roi_dir, "step0", "corrected_channels.zarr"))
        out_dir = self._step2_dir or self._out_edit.text().strip() or OUTPUT_DIR
        candidates.extend([
            os.path.join(out_dir, "corrected_channels.zarr"),
            os.path.join(os.path.dirname(out_dir), "corrected_channels.zarr"),
        ])
        seen = set()
        for path in candidates:
            if not path or not os.path.exists(path):
                continue
            path = os.path.abspath(path)
            if path in seen:
                continue
            seen.add(path)
            root = zarr.open(path, mode="r")
            mode = str(root.attrs.get("mode", "")).strip().lower()
            if mode == "roi_only":
                groups = list(getattr(root, "group_keys", lambda: [])())
                if not groups and hasattr(root, "groups"):
                    groups = [name for name, _group in root.groups()]
                target = None
                for group_name in groups:
                    group = root[group_name]
                    requested_roi_id = str(cfg.get("roi_id") or self._roi_id or "")
                    requested_names = {
                        str(v) for v in (
                            cfg.get("roi_name"),
                            cfg.get("roi_display_name"),
                            self._roi_id,
                        )
                        if str(v or "").strip()
                    }
                    if requested_roi_id and str(group.attrs.get("roi_id") or "") == requested_roi_id:
                        target = group
                        break
                    group_names = {
                        str(group_name),
                        str(group.attrs.get("roi_name") or ""),
                        str(group.attrs.get("display_name") or ""),
                        str(group.attrs.get("roi_display_name") or ""),
                    }
                    if requested_names and requested_names & {name for name in group_names if name}:
                        target = group
                        break
                if target is not None:
                    available = list(target.array_keys())
                    if not first_available:
                        first_available = available
                    _resolved, missing, _warnings = resolve_hq_channels(requested, available)
                    if not requested or not missing:
                        return available
                    print(
                        "[Step2-HQ] corrected zarr missing requested channels; trying next source\n"
                        f"  path={path}\n"
                        f"  missing={missing}\n"
                        f"  available={available}",
                        flush=True,
                    )
                    continue
                print(
                    "[Step2] HQ channel source ROI group not matched\n"
                    f"  path={path}\n"
                    f"  requested_roi_id={cfg.get('roi_id') or self._roi_id or ''}\n"
                    f"  requested_names={sorted(requested_names) if 'requested_names' in locals() else []}\n"
                    f"  available_groups={groups}",
                    flush=True,
                )
                continue
            available = list(root.array_keys())
            if not first_available:
                first_available = available
            _resolved, missing, _warnings = resolve_hq_channels(requested, available)
            if not requested or not missing:
                return available
            print(
                "[Step2-HQ] corrected zarr missing requested channels; trying next source\n"
                f"  path={path}\n"
                f"  missing={missing}\n"
                f"  available={available}",
                flush=True,
            )

        for path in (
            cfg.get("raw_channel_source_path"),
            cfg.get("raw_ome_path"),
        ):
            if not path or not os.path.exists(path):
                continue
            try:
                available = OMETIFFLoader(path).channel_names()
            except Exception as exc:
                print(f"[Step2-HQ] failed to inspect raw OME channel source {path}: {exc}", flush=True)
                continue
            _resolved, missing, _warnings = resolve_hq_channels(requested, available)
            if not requested or not missing:
                print(f"[Step2-HQ] using raw OME channel source for HQ channels: {path}", flush=True)
                return available
            if not first_available:
                first_available = available
        return first_available

    # ── run / stop ────────────────────────────────────────────────────

    def _run(self):
        if not self._zarr_path or not os.path.exists(self._zarr_path):
            QMessageBox.warning(self, 'No data',
                                'Please load a fused.zarr first.')
            return
        source = self._param_source_combo.currentData() or "manual"
        if source == "index":
            if not self._seg_params_index:
                path = self._seg_params_edit.text().strip()
                if path and not self._load_seg_params_index(path, silent=False, apply_active=False):
                    return
            resolved = self._update_resolved_param_path()
            if not resolved or not os.path.exists(resolved):
                QMessageBox.warning(self, 'Error', 'No indexed segmentation parameter file selected.')
                return
            current_method = self._method_combo.currentData() or CELLPOSE_WHOLECELL_FUSION
            selected_method = self._index_method_combo.currentData()
            if (
                not self._applied_param_file
                or os.path.abspath(self._applied_param_file) != os.path.abspath(resolved)
                or (selected_method and selected_method != current_method)
            ):
                if not self._apply_selected_index_params():
                    return
        seg_config = self.get_seg_config()
        if seg_config.get("method") in (CELLPOSE_NUCLEI_HQ, CELLPOSE_NUCLEI_HQ2):
            channels = seg_config.get("hq_channels") or []
            try:
                available = self._available_hq_channels()
                validate_hq_channels(channels, available)
            except Exception as e:
                QMessageBox.warning(self, 'Missing channels', str(e))
                return
            if len(channels) > 3:
                QMessageBox.information(
                    self,
                    'HQ channel recommendation',
                    'Cellpose nuclei + HQ/HQ2 recommends 2-5 high-quality structural channels; '
                    'using more than 3 channels can be slower and may dilute consensus.'
                )

        recovery_dir = None
        if self._rec_box.isChecked():
            recovery_dir = self._rec_edit.text().strip() or None

        if self._auto_tile_strategy.isChecked():
            self._refresh_tile_suggestion(apply=True)

        nr  = self._rows_spin.value()
        nc  = self._cols_spin.value()
        self._n_rows = nr
        self._n_cols = nc
        param_file = ""
        if source == "index" and self._applied_param_file:
            param_file = self._applied_param_file

        # Reset tile colours
        for key in self._tile_status:
            self._tile_status[key] = 'idle'
            self._set_tile_colour(key[0], key[1], 'idle')
        self._total_cells = 0
        self._cells_lbl.setText('Total cells detected: 0')

        self._worker = SegmentMergeWorker(
            zarr_path        = self._zarr_path,
            seg_config       = seg_config,
            n_rows           = nr,
            n_cols           = nc,
            overlap_px       = self._overlap_spin.value(),
            output_dir       = self._step2_dir or self._out_edit.text().strip() or OUTPUT_DIR,
            recovery_npy_dir = recovery_dir,
            rois             = self._rois if self._rois else None,
            param_file        = param_file,
            parameter_source  = "index" if param_file else "manual",
        )
        seg_cfg = seg_config
        print(f"[Step2] segmentation method={seg_cfg.get('method')}")
        print(f"[Step2] input_type={seg_cfg.get('input_type')}")
        if seg_cfg.get("method") in (CELLPOSE_NUCLEI_HQ, CELLPOSE_NUCLEI_HQ2):
            print(f"[Step2][HQ] loaded param_file path={param_file or '(manual)'}")
            print(f"[Step2-HQ] hq_input_mode: {seg_cfg.get('hq_input_mode')}")
            print(f"[Step2-HQ] requested hq_channels: {seg_cfg.get('hq_channels')}")
            print(f"[Step2][HQ] seg_config hq_channels={seg_cfg.get('hq_channels')}")
            print(
                "[Step2][HQ] selected hq_source_zarr="
                f"{seg_cfg.get('hq_source_zarr') or seg_cfg.get('multichannel_source_path') or '(auto)'}"
            )
            print(
                "[Step2][HQ] requested roi="
                f"id={seg_cfg.get('roi_id') or self._roi_id or ''} "
                f"name={seg_cfg.get('roi_name') or seg_cfg.get('roi_display_name') or ''}"
            )
        if self._roi_id:
            print(f"[Step2] roi_id={self._roi_id}")
            print(f"[Step2] output_base={self._step2_dir or self._out_edit.text().strip()}")
        self._worker.progress.connect(self._on_progress)
        self._worker.tile_done.connect(self._on_tile_done)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)

        self._btn_run.setEnabled(False)
        self._btn_back.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._prog_bar.setValue(0)
        self._worker.start()

        # Mark first tile as running
        if self._tile_rects:
            self._set_tile_colour(0, 0, 'running')
            self._tile_status[(0, 0)] = 'running'

    def _stop(self):
        if self._worker:
            self._worker.stop()

    def _on_progress(self, done, total, msg):
        pct = int(done / total * 100) if total > 0 else 0
        self._prog_bar.setValue(pct)
        self._prog_lbl.setText(msg)

        # Mark next tile as running
        nr = self._n_rows
        nc = self._n_cols
        if done < total:
            r = done // nc
            c = done  % nc
            self._set_tile_colour(r, c, 'running')

    def _on_tile_done(self, tile_idx, n_tiles, n_cells):
        nr = self._n_rows
        nc = self._n_cols
        r  = tile_idx // nc
        c  = tile_idx  % nc
        self._set_tile_colour(r, c, 'done')
        self._tile_status[(r, c)] = 'done'
        self._total_cells += n_cells
        self._cells_lbl.setText(
            f'Total cells detected: {self._total_cells:,}'
        )

    def _on_finished(self, output_dir, total_cells):
        runtime = {}
        try:
            if self._worker is not None and hasattr(self._worker, "runtime_summary"):
                runtime = self._worker.runtime_summary()
        except Exception:
            runtime = {}
        runtime_text = ""
        if runtime:
            runtime_text = (
                f"Elapsed: {runtime.get('elapsed') or 'N/A'}  |  "
                f"Peak RAM: {runtime.get('peak_ram') or 'N/A'}  |  "
                f"Peak VRAM: {runtime.get('peak_vram') or 'N/A'}"
            )
        self._prog_bar.setValue(100)
        self._prog_lbl.setText(
            f'✓ Done!  {total_cells:,} cells'
            + (f'  |  {runtime_text}' if runtime_text else '')
            + f'  →  {output_dir}'
        )
        self._btn_run.setEnabled(True)
        self._btn_back.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._last_output_dir = output_dir
        self.segmentation_done.emit(output_dir)
        if self._roi_id and self._roi_dir:
            try:
                project_dir = os.path.dirname(os.path.dirname(self._roi_dir))
                mark_roi_step(project_dir, self._roi_id, "step2", "done")
            except Exception as e:
                print(f"[Step2] failed to update ROI step2 status: {e}")

        msg = QMessageBox(self)
        msg.setWindowTitle('Segmentation Complete')
        runtime_block = ""
        if runtime_text:
            runtime_block = (
                f'Runtime summary:\n'
                f'  {runtime_text}\n\n'
            )
        msg.setText(
            f'Total cells: {total_cells:,}\n\n'
            f'{runtime_block}'
            f'Output directory:\n  {output_dir}\n\n'
            f'Global outputs:\n'
            f'  global_mask.zarr\n'
            f'  global_mask.ome.tiff   (merged mask, float32)\n'
            f'  global_dapi.ome.tiff   (DAPI, uint16)\n'
            f'  run_segmentation_params.json\n'
            f'  segmentation_meta.json\n\n'
            f'Per-tile outputs:\n'
            f'  tile_masks/\n'
            f'    tile_r*_c*_dapi.ome.tiff\n'
            f'    tile_r*_c*_raw_mask.ome.tiff'
        )
        btn_qc = msg.addButton('→ Open QC Viewer (Step 3)',
                               QMessageBox.AcceptRole)
        btn_feat = msg.addButton('→ Feature Extraction (Step 4)',
                                 QMessageBox.AcceptRole)
        msg.addButton('OK', QMessageBox.RejectRole)
        msg.exec_()
        if msg.clickedButton() is btn_qc:
            self.open_qc_requested.emit(output_dir)
        elif msg.clickedButton() is btn_feat:
            self.open_qc_requested.emit(output_dir)   # step3 auto-loads too

    def _on_error(self, msg):
        self._prog_lbl.setText('✗ Error — see terminal')
        self._btn_run.setEnabled(True)
        self._btn_back.setEnabled(True)
        self._btn_stop.setEnabled(False)
        QMessageBox.critical(self, 'Error', msg)
        print(f'[Step2 Error]\n{msg}')


# ══════════════════════════════════════════════════════════════════════
#  Step 3 Page  — Segmentation QC Viewer
# ══════════════════════════════════════════════════════════════════════
