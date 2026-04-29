"""
block01/ui/step4_page.py — Step4Page (Cell Feature Extraction).
"""

import os

from PyQt5 import QtWidgets
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QGroupBox, QProgressBar, QMessageBox, QFileDialog,
)

from ..config import OUTPUT_DIR, OME_TIFF_FILE
from ..core.bg_correction import _load_correction_config
from ..workers.feature_extract_worker import FeatureExtractWorker

# ══════════════════════════════════════════════════════════════════════
#  Step 4 Page  — Cell Feature Extraction
# ══════════════════════════════════════════════════════════════════════

class Step4Page(QWidget):

    go_back = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker = None
        self._build_ui()

    # ── public API ────────────────────────────────────────────────────

    def set_paths(self, mask_path, ome_tiff_path, output_dir):
        """Called from MainWindow after Step 2 finishes."""
        if mask_path:
            self._mask_edit.setText(mask_path)
        if ome_tiff_path:
            self._ome_edit.setText(ome_tiff_path)
        if output_dir:
            self._out_edit.setText(output_dir)

    # ── UI ────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        title = QLabel('Step 4 — Cell Feature Extraction')
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            'font-size:16px;font-weight:bold;color:#eee;'
            'background:#1a1a1a;padding:6px;border-radius:4px;'
        )
        root.addWidget(title)

        def _box(label, color):
            b = QGroupBox(label)
            b.setStyleSheet(
                f'QGroupBox{{border:1px solid {color};border-radius:5px;'
                f'margin-top:4px;font-weight:bold;color:{color};font-size:11px;}}'
            )
            return b

        def _file_row(parent_lay, label, placeholder, btn_slot):
            r   = QHBoxLayout()
            r.addWidget(QLabel(label))
            ed  = QtWidgets.QLineEdit()
            ed.setPlaceholderText(placeholder)
            ed.setStyleSheet('font-size:11px;')
            r.addWidget(ed, stretch=1)
            btn = QPushButton('Browse')
            btn.setFixedWidth(64)
            btn.clicked.connect(btn_slot)
            r.addWidget(btn)
            parent_lay.addLayout(r)
            return ed

        # ── Input files ───────────────────────────────────────────────
        inp = _box('Input Files', '#61afef')
        il  = QVBoxLayout(inp)

        self._mask_edit = _file_row(
            il, 'Mask (.dat/.ome.tiff):',
            'global_mask.dat  or  global_mask.ome.tiff',
            lambda: self._browse_file(self._mask_edit,
                                      'Mask file (*.dat *.tiff *.tif)')
        )
        self._ome_edit = _file_row(
            il, 'OME-TIFF (original):',
            'original multichannel OME-TIFF',
            lambda: self._browse_file(self._ome_edit,
                                      'OME-TIFF (*.tif *.tiff)')
        )
        self._ome_edit.setText(OME_TIFF_FILE)
        root.addWidget(inp)

        # ── Statistics selection ───────────────────────────────────────
        stat_box = _box('Intensity Statistics  (multi-select)', '#e5c07b')
        stl = QVBoxLayout(stat_box)

        # Note label
        note = QLabel(
            'The first checked item becomes the X matrix in h5ad (scanpy default).\n'
            'All checked items are saved in CSV.  p90 is slowest.'
        )
        note.setStyleSheet('color:#888;font-size:10px;')
        stl.addWidget(note)

        # Checkboxes — (key, display_label, default_checked)
        _stat_defs = [
            ('mean',   'Mean',            True),
            ('median', 'Median',          False),
            ('sum',    'Sum (total int.)', False),
            ('std',    'Std dev',         False),
            ('min',    'Min',             False),
            ('max',    'Max',             False),
            ('p90',    '90th percentile', False),
        ]
        self._stat_checks = {}
        chk_row = QHBoxLayout()
        for key, label, default in _stat_defs:
            cb = QtWidgets.QCheckBox(label)
            cb.setChecked(default)
            cb.setStyleSheet('font-size:11px;')
            chk_row.addWidget(cb)
            self._stat_checks[key] = cb
        chk_row.addStretch()
        stl.addLayout(chk_row)

        # "X matrix =" info label, updates when checkboxes change
        self._x_lbl = QLabel('X matrix in h5ad:  mean')
        self._x_lbl.setStyleSheet('color:#e5c07b;font-size:10px;')
        stl.addWidget(self._x_lbl)

        def _update_x_lbl():
            first = next(
                (k for k, cb in self._stat_checks.items() if cb.isChecked()),
                None
            )
            self._x_lbl.setText(
                f'X matrix in h5ad:  {first}' if first
                else '⚠  Select at least one statistic'
            )
        for cb in self._stat_checks.values():
            cb.stateChanged.connect(_update_x_lbl)

        root.addWidget(stat_box)
        out = _box('Output', '#98c379')
        ol  = QVBoxLayout(out)
        self._out_edit = _file_row(
            ol, 'Output dir:',
            OUTPUT_DIR,
            lambda: self._out_edit.setText(
                QFileDialog.getExistingDirectory(self, 'Select output dir')
            )
        )
        self._out_edit.setText(OUTPUT_DIR)

        # Filename prefix row
        prefix_row = QHBoxLayout()
        prefix_row.addWidget(QLabel('Filename prefix (optional):'))
        self._prefix_edit = QtWidgets.QLineEdit()
        self._prefix_edit.setPlaceholderText('Leave blank for default: cell_features.csv / .h5ad')
        self._prefix_edit.setStyleSheet('font-size:11px;')
        prefix_row.addWidget(self._prefix_edit, stretch=1)
        ol.addLayout(prefix_row)

        self._prefix_info = QLabel('Outputs:  cell_features.csv   cell_features.h5ad')
        self._prefix_info.setStyleSheet('color:#888;font-size:10px;')
        ol.addWidget(self._prefix_info)

        def _update_prefix_info():
            p = self._prefix_edit.text().strip()
            base = f'{p}_cell_features' if p else 'cell_features'
            self._prefix_info.setText(f'Outputs:  {base}.csv   {base}.h5ad')

        self._prefix_edit.textChanged.connect(_update_prefix_info)
        root.addWidget(out)

        # ── Progress ──────────────────────────────────────────────────
        self._prog_bar = QProgressBar()
        self._prog_bar.setRange(0, 100)
        self._prog_bar.setStyleSheet(
            'QProgressBar{border:1px solid #444;border-radius:3px;'
            'text-align:center;color:#fff;height:18px;}'
            'QProgressBar::chunk{background:#2a5;border-radius:3px;}'
        )
        root.addWidget(self._prog_bar)

        self._prog_lbl = QLabel('—')
        self._prog_lbl.setStyleSheet('color:#ccc;font-size:11px;padding:2px;')
        self._prog_lbl.setWordWrap(True)
        root.addWidget(self._prog_lbl)

        root.addStretch()

        # ── Navigation ────────────────────────────────────────────────
        nav = QHBoxLayout()

        btn_back = QPushButton('← Back to Step 3')
        btn_back.setStyleSheet(
            'QPushButton{color:#fa8;border:1px solid #fa8;'
            'border-radius:4px;padding:6px 16px;}'
            'QPushButton:hover{background:#321;}'
        )
        btn_back.clicked.connect(self.go_back.emit)
        nav.addWidget(btn_back)
        nav.addStretch()

        self._btn_stop = QPushButton('⏹ Stop')
        self._btn_stop.setEnabled(False)
        self._btn_stop.setStyleSheet(
            'QPushButton{background:#722;color:white;border-radius:4px;padding:7px 14px;}'
            'QPushButton:hover{background:#944;}'
            'QPushButton:disabled{background:#333;color:#555;}'
        )
        self._btn_stop.clicked.connect(self._stop)
        nav.addWidget(self._btn_stop)

        self._btn_run = QPushButton('▶  Extract Features')
        self._btn_run.setStyleSheet(
            'QPushButton{background:#2a5;color:white;border-radius:4px;'
            'padding:7px 22px;font-size:13px;font-weight:bold;}'
            'QPushButton:hover{background:#3b6;}'
            'QPushButton:disabled{background:#333;color:#555;}'
        )
        self._btn_run.clicked.connect(self._run)
        nav.addWidget(self._btn_run)

        root.addLayout(nav)

    # ── helpers ───────────────────────────────────────────────────────

    def _browse_file(self, edit, filt):
        p, _ = QFileDialog.getOpenFileName(self, 'Select file',
                                            OUTPUT_DIR, filt)
        if p:
            edit.setText(p)

    def _run(self):
        mask_path = self._mask_edit.text().strip()
        ome_path  = self._ome_edit.text().strip()
        out_dir   = self._out_edit.text().strip() or OUTPUT_DIR

        if not mask_path or not os.path.exists(mask_path):
            QMessageBox.warning(self, 'Missing input', 'Please select a valid mask file.')
            return
        if not ome_path or not os.path.exists(ome_path):
            QMessageBox.warning(self, 'Missing input', 'Please select the original OME-TIFF.')
            return

        stats = [k for k, cb in self._stat_checks.items() if cb.isChecked()]
        if not stats:
            QMessageBox.warning(self, 'No statistics selected',
                                'Please select at least one intensity statistic.')
            return

        prefix = self._prefix_edit.text().strip()
        correction_config = _load_correction_config(
            os.path.join(out_dir, 'correction_config.json')
        )

        self._worker = FeatureExtractWorker(
            mask_path     = mask_path,
            ome_tiff_path = ome_path,
            output_dir    = out_dir,
            statistics    = stats,
            file_prefix   = prefix,
            correction_config = correction_config,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)

        self._btn_run.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._prog_bar.setValue(0)
        self._worker.start()

    def _stop(self):
        if self._worker:
            self._worker.stop()

    def _on_progress(self, done, total, msg):
        pct = int(done / total * 100) if total > 0 else 0
        self._prog_bar.setValue(pct)
        self._prog_lbl.setText(msg)

    def _on_finished(self, out_dir, base_name):
        self._prog_bar.setValue(100)
        self._btn_run.setEnabled(True)
        self._btn_stop.setEnabled(False)
        csv_p = os.path.join(out_dir, f'{base_name}.csv')
        h5_p  = os.path.join(out_dir, f'{base_name}.h5ad')
        QMessageBox.information(
            self, 'Feature extraction complete',
            f'Outputs:\n\n'
            f'  {csv_p}\n'
            f'  {h5_p}\n\n'
            f'Load {base_name}.h5ad in scanpy for downstream analysis.'
        )

    def _on_error(self, msg):
        self._prog_bar.setValue(0)
        self._btn_run.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._prog_lbl.setText('✗ Error — see terminal')
        QMessageBox.critical(self, 'Error', msg)
        print(f'[Step4 Error]\n{msg}')


# ══════════════════════════════════════════════════════════════════════
#  Entry Point
# ══════════════════════════════════════════════════════════════════════

