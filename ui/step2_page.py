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
    QDoubleSpinBox, QScrollArea, QComboBox,
)
import pyqtgraph as pg

from ..config import OUTPUT_DIR
from ..utils.segmentation_config import (
    CELLPOSE_NUCLEI_DAPI,
    CELLPOSE_WHOLECELL_FUSION,
    STARDIST_NUCLEI_DAPI,
    STARDIST_NUCLEI_EXPANSION,
    available_segmentation_methods,
    get_segmentation_method_config,
    normalize_segmentation_config,
)
from ..workers.segment_merge_worker import SegmentMergeWorker

# ══════════════════════════════════════════════════════════════════════
#  Step 2 Page  (Segmentation & Merge)
# ══════════════════════════════════════════════════════════════════════

class Step2Page(QWidget):
    """Full Step 2 UI: zarr input, tile grid, segmentation params, progress."""

    go_back           = pyqtSignal()
    segmentation_done = pyqtSignal(str)   # emits output_dir when done

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

        self._build_ui()

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

        btn_load = QPushButton('Load zarr info & overview')
        btn_load.setStyleSheet(
            'QPushButton{background:#255;color:white;border-radius:3px;padding:4px;}'
            'QPushButton:hover{background:#377;}'
        )
        btn_load.clicked.connect(self._load_zarr_info)
        inl.addLayout(zr)
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

        self._method_combo = QComboBox()
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

        self._method_hint = QLabel('')
        self._method_hint.setStyleSheet('color:#999;font-size:10px;')
        self._method_hint.setWordWrap(True)
        cpl.addWidget(self._method_hint)

        btn_load_cp = QPushButton('↓ Load segmentation_config.json / cellpose_params.json')
        btn_load_cp.setStyleSheet(
            'QPushButton{color:#8cf;font-size:10px;border:1px solid #8cf;'
            'border-radius:3px;padding:3px;}'
        )
        btn_load_cp.clicked.connect(self._load_cp_params)
        cpl.addWidget(btn_load_cp)
        rl.addWidget(cp_box)
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
            self._load_zarr_info()
            self._load_default_seg_config(silent=True)

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
            self._zarr_info.setText(
                f'Shape: {self._full_h:,} × {self._full_w:,} px  '
                f'| dtype: {z.dtype}  '
                f'| ch0={attrs.get("channel_0","?")}  '
                f'ch1={attrs.get("channel_1","?")}'
            )
            self._update_tile_info()
            self._load_overview_from_zarr(z)
            self._load_default_seg_config(silent=True)
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

    def _load_cp_params(self):
        self._load_default_seg_config(silent=False)

    def _load_default_seg_config(self, silent=True):
        cp_path = os.path.join(OUTPUT_DIR, 'segmentation_config.json')
        if not os.path.exists(cp_path):
            cp_path = os.path.join(OUTPUT_DIR, 'cellpose_params.json')
        if not os.path.exists(cp_path):
            if silent:
                return
            cp_path, _ = QFileDialog.getOpenFileName(
                self, 'Select segmentation_config.json', OUTPUT_DIR, 'JSON (*.json)'
            )
        if not cp_path or not os.path.exists(cp_path):
            return
        try:
            with open(cp_path) as f:
                p = normalize_segmentation_config(json.load(f))
            self._apply_seg_config_to_ui(p)
            self._seg_config = p
            if not silent:
                QMessageBox.information(self, 'Loaded',
                                        f'Segmentation config loaded from:\n{cp_path}')
        except Exception as e:
            if not silent:
                QMessageBox.warning(self, 'Error', str(e))
            else:
                print(f'[Step2] failed to auto-load segmentation config: {e}')

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
        self._sd_model.setText(str(p.get('model_name') or '2D_versatile_fluo'))
        self._sd_prob.setValue(-1.0 if p.get('prob_thresh') is None else float(p.get('prob_thresh')))
        self._sd_nms.setValue(-1.0 if p.get('nms_thresh') is None else float(p.get('nms_thresh')))
        self._sd_expand.setValue(float(p.get('expand_distance', 8) or 0))
        self._on_method_changed()

    def get_cp_params(self):
        return self.get_seg_config()

    def get_seg_config(self):
        method = self._method_combo.currentData() or CELLPOSE_WHOLECELL_FUSION
        diam = self._cp_diam.value()
        data = {
            'method':             method,
            'params': {
                'model_name':         self._sd_model.text().strip() or '2D_versatile_fluo',
                'prob_thresh':        None if self._sd_prob.value() < 0 else self._sd_prob.value(),
                'nms_thresh':         None if self._sd_nms.value() < 0 else self._sd_nms.value(),
                'expand_distance':    self._sd_expand.value(),
            },
            'model_type':         'cpsam',  # Cellpose 4.0.1+: always cpsam
            'diameter':           None if diam == 0 else diam,
            'flow_threshold':     self._cp_flow.value(),
            'cellprob_threshold': self._cp_prob.value(),
            'min_size':           self._cp_minsize.value(),
        }
        return normalize_segmentation_config(data)

    def _on_method_changed(self):
        method = self._method_combo.currentData() or CELLPOSE_WHOLECELL_FUSION
        is_cellpose = method in (CELLPOSE_WHOLECELL_FUSION, CELLPOSE_NUCLEI_DAPI)
        is_stardist = method in (STARDIST_NUCLEI_DAPI, STARDIST_NUCLEI_EXPANSION)
        is_expansion = method == STARDIST_NUCLEI_EXPANSION
        for w in (
            self._cp_model_label, self._cp_model_lbl,
            self._cp_diam_label, self._cp_diam,
            self._cp_flow_label, self._cp_flow,
            self._cp_prob_label, self._cp_prob,
            self._cp_minsize_label, self._cp_minsize,
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
        cfg = get_segmentation_method_config(method)
        self._method_hint.setText(
            f'{method} | input={cfg.get("input_type")} | output={cfg.get("output_type")}'
        )

    # ── run / stop ────────────────────────────────────────────────────

    def _run(self):
        if not self._zarr_path or not os.path.exists(self._zarr_path):
            QMessageBox.warning(self, 'No data',
                                'Please load a fused.zarr first.')
            return

        recovery_dir = None
        if self._rec_box.isChecked():
            recovery_dir = self._rec_edit.text().strip() or None

        nr  = self._rows_spin.value()
        nc  = self._cols_spin.value()
        self._n_rows = nr
        self._n_cols = nc

        # Reset tile colours
        for key in self._tile_status:
            self._tile_status[key] = 'idle'
            self._set_tile_colour(key[0], key[1], 'idle')
        self._total_cells = 0
        self._cells_lbl.setText('Total cells detected: 0')

        self._worker = SegmentMergeWorker(
            zarr_path        = self._zarr_path,
            seg_config       = self.get_seg_config(),
            n_rows           = nr,
            n_cols           = nc,
            overlap_px       = self._overlap_spin.value(),
            output_dir       = self._out_edit.text().strip() or OUTPUT_DIR,
            recovery_npy_dir = recovery_dir,
            rois             = self._rois if self._rois else None,
        )
        seg_cfg = self.get_seg_config()
        print(f"[Step2] segmentation method={seg_cfg.get('method')}")
        print(f"[Step2] input_type={seg_cfg.get('input_type')}")
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
        self._prog_bar.setValue(100)
        self._prog_lbl.setText(
            f'✓ Done!  {total_cells:,} cells  →  {output_dir}'
        )
        self._btn_run.setEnabled(True)
        self._btn_back.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._last_output_dir = output_dir

        msg = QMessageBox(self)
        msg.setWindowTitle('Segmentation Complete')
        msg.setText(
            f'Total cells: {total_cells:,}\n\n'
            f'Output directory:\n  {output_dir}\n\n'
            f'Global outputs:\n'
            f'  global_mask.zarr\n'
            f'  global_mask.ome.tiff   (merged mask, float32)\n'
            f'  global_dapi.ome.tiff   (DAPI, uint16)\n'
            f'  segmentation_config.json\n'
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
            self.segmentation_done.emit(output_dir)
        elif msg.clickedButton() is btn_feat:
            self.segmentation_done.emit(output_dir)   # step3 auto-loads too

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
