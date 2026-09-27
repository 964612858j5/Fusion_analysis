"""
block01/ui/step4_page.py — Step4Page (Cell Feature Extraction).
"""

import os

from PyQt5 import QtWidgets
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QGroupBox, QProgressBar, QMessageBox, QFileDialog, QComboBox,
)

from ..config import OUTPUT_DIR
from ..core import quant_engine as qe
from ..core import quant_sources as qs
from ..workers.feature_extract_worker import (
    FeatureExtractWorker, default_output_dir, output_base,
)

# ══════════════════════════════════════════════════════════════════════
#  Step 4 Page  — Cell Feature Extraction
# ══════════════════════════════════════════════════════════════════════

class Step4Page(QWidget):

    go_back = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker = None
        self._running = False
        self._open_slide = ""
        self._job = None            # the resolved QuantJob, or None
        self._build_ui()

    # ── public API ────────────────────────────────────────────────────

    def set_run(self, run_dir, roi_name=None, open_slide=None):
        """The (run, region) Step4 quantifies -- MainWindow hands over
        Step3's choice. The slide is the run's own; `open_slide` is the one
        open in the program (a run of another slide is refused)."""
        if open_slide is not None:
            self._open_slide = open_slide or ""
        self._run_edit.blockSignals(True)
        self._run_edit.setText(run_dir or "")
        self._run_edit.blockSignals(False)
        self._load_run(roi_name)

    def refusal(self):
        """Why `Extract Features` is unavailable ('' when it is available)."""
        return self._reason_lbl.text() if self._job is None else ""

    def _load_run(self, roi_name=None):
        """Fill the region list for the run in the field, then resolve."""
        path = self._run_edit.text().strip()
        self._roi_combo.blockSignals(True)
        self._roi_combo.clear()
        regions = []
        if path:
            try:
                regions = qs.run_regions(qs.open_run(path))
            except qs.QuantSourceError as exc:
                self._set_job(None, str(exc))
                self._roi_combo.blockSignals(False)
                return
        for name, _bbox in regions:
            self._roi_combo.addItem(name, name)
        if roi_name is not None and self._roi_combo.findData(roi_name) >= 0:
            self._roi_combo.setCurrentIndex(self._roi_combo.findData(roi_name))
        self._roi_combo.setEnabled(len(regions) > 1)
        self._roi_combo.blockSignals(False)
        self._resolve()

    def _resolve(self, set_output=True):
        path = self._run_edit.text().strip()
        if not path:
            self._set_job(None, "Choose a Step 2 result (its run folder).")
            return
        roi = self._roi_combo.currentData()
        try:
            job = qs.resolve_quant_job(path, roi, self._open_slide or None)
        except qs.QuantSourceError as exc:
            self._set_job(None, str(exc))
            return
        self._set_job(job, "")
        if set_output:
            self._out_edit.setText(default_output_dir(job))

    def _set_job(self, job, reason):
        self._job = job
        if job is None:
            self._slide_lbl.setText("—")
            self._source_lbl.setText("")
            self._reason_lbl.setText(reason)
            self._reason_lbl.setVisible(bool(reason))
        else:
            n_corr = sum(1 for c in job.channels if c.kind == "corrected")
            self._slide_lbl.setText(job.slide)
            what = "nuclei (this result has no cell mask)" if job.compartment == qs.NUCLEUS \
                else "cells"
            self._source_lbl.setText(
                f"{job.n_objects:,} labelled {what} · {len(job.channels)} channels: "
                f"{len(job.channels) - n_corr} raw, {n_corr} from Step 0's correction")
            self._reason_lbl.setText("")
            self._reason_lbl.setVisible(False)
        self._update_run_button()

    def _update_run_button(self):
        self._btn_run.setEnabled(self._job is not None and not self._running)

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

        self._run_edit = _file_row(
            il, 'Run:',
            'a Step 2 result (its run folder)',
            self._browse_run,
        )
        self._run_edit.editingFinished.connect(lambda: self._load_run())
        roi_row = QHBoxLayout()
        roi_row.addWidget(QLabel('Region:'))
        self._roi_combo = QComboBox()
        self._roi_combo.setStyleSheet('font-size:11px;')
        self._roi_combo.currentIndexChanged.connect(lambda _i: self._resolve())
        roi_row.addWidget(self._roi_combo, stretch=1)
        il.addLayout(roi_row)
        slide_row = QHBoxLayout()
        slide_row.addWidget(QLabel('Slide:'))
        self._slide_lbl = QLabel('—')
        self._slide_lbl.setStyleSheet('color:#ccc;font-size:11px;')
        self._slide_lbl.setToolTip("The run's own slide: intensities are read from it, and "
                                   "from Step 0's saved correction for corrected channels")
        slide_row.addWidget(self._slide_lbl, stretch=1)
        il.addLayout(slide_row)
        self._source_lbl = QLabel('')
        self._source_lbl.setStyleSheet('color:#888;font-size:10px;')
        il.addWidget(self._source_lbl)
        self._reason_lbl = QLabel('Choose a Step 2 result (its run folder).')
        self._reason_lbl.setWordWrap(True)
        self._reason_lbl.setStyleSheet('color:#e06c75;font-size:11px;')
        il.addWidget(self._reason_lbl)
        root.addWidget(inp)

        # ── Statistics selection ───────────────────────────────────────
        stat_box = _box('Intensity Statistics  (multi-select)', '#e5c07b')
        stl = QVBoxLayout(stat_box)

        # Note label
        note = QLabel('Each checked statistic is computed for every channel, all in one pass, '
                      'and saved in the CSV.')
        note.setStyleSheet('color:#888;font-size:10px;')
        stl.addWidget(note)

        # Checkboxes — (key, display_label, default_checked)
        _stat_defs = [
            ('mean',   'Mean',            True),
            ('sum',    'Sum (total int.)', True),
            ('std',    'Std dev',         True),
            ('min',    'Min',             True),
            ('max',    'Max',             True),
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

        # Filename prefix row
        prefix_row = QHBoxLayout()
        prefix_row.addWidget(QLabel('Filename prefix (optional):'))
        self._prefix_edit = QtWidgets.QLineEdit()
        self._prefix_edit.setPlaceholderText('Leave blank for the default: cell_features.csv')
        self._prefix_edit.setStyleSheet('font-size:11px;')
        prefix_row.addWidget(self._prefix_edit, stretch=1)
        ol.addLayout(prefix_row)

        self._prefix_info = QLabel('Outputs:  cell_features.csv   cell_features_provenance.json')
        self._prefix_info.setStyleSheet('color:#888;font-size:10px;')
        ol.addWidget(self._prefix_info)

        def _update_prefix_info():
            p = self._prefix_edit.text().strip()
            base = output_base(p)
            self._prefix_info.setText(f'Outputs:  {base}.csv   {base}_provenance.json')

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

        self._batch_btn = QPushButton('Batch...')
        self._batch_btn.setStyleSheet(
            'QPushButton{color:#61afef;border:1px solid #61afef;'
            'border-radius:4px;padding:6px 16px;}'
            'QPushButton:hover{background:#123;}'
        )
        self._batch_btn.clicked.connect(self._open_batch)
        nav.addWidget(self._batch_btn)

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
        self._btn_run.setEnabled(False)
        nav.addWidget(self._btn_run)

        root.addLayout(nav)

    # ── helpers ───────────────────────────────────────────────────────

    def _pick_run_folder(self):
        """The system dialog (a seam: a test answers directly)."""
        return QFileDialog.getExistingDirectory(
            self, 'A Step 2 result (its run folder)',
            self._run_edit.text().strip() or OUTPUT_DIR)

    def _browse_run(self):
        p = self._pick_run_folder()
        if p:
            self._run_edit.setText(p)
            self._load_run()

    def _open_batch(self):
        from .batch_step4_dialog import BatchStep4Dialog
        dlg = BatchStep4Dialog(parent=self)
        dlg.exec_()

    def _run(self):
        self._resolve(set_output=False)     # the files may have changed since
        if self._job is None:
            return
        out_dir = self._out_edit.text().strip() or default_output_dir(self._job)

        stats = [k for k, cb in self._stat_checks.items() if cb.isChecked()]
        if not stats:
            QMessageBox.warning(self, 'No statistics selected',
                                'Please select at least one intensity statistic.')
            return

        prefix = self._prefix_edit.text().strip()
        self._worker = FeatureExtractWorker(
            run_path    = self._job.run_dir,
            roi_name    = self._job.roi_name,
            output_dir  = out_dir,
            statistics  = stats,
            file_prefix = prefix,
            open_slide  = self._open_slide or None,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.extraction_done.connect(self._on_finished)
        self._worker.error.connect(self._on_error)

        self._running = True
        self._update_run_button()
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
        self._running = False
        self._btn_stop.setEnabled(False)
        self._update_run_button()
        csv_p = os.path.join(out_dir, f'{base_name}.csv')
        prov_p = os.path.join(out_dir, f'{base_name}_provenance.json')
        self._announce('Feature extraction complete',
                       f'Outputs:\n\n  {csv_p}\n  {prov_p}\n\n'
                       f'The provenance file says which channels were read raw and which '
                       f'from Step 0\'s correction.')

    def _announce(self, title, text):
        """The finished box (a seam: offscreen tests do not open it)."""
        QMessageBox.information(self, title, text)

    def _on_error(self, msg):
        self._prog_bar.setValue(0)
        self._running = False
        self._btn_stop.setEnabled(False)
        self._update_run_button()
        self._prog_lbl.setText(f'✗ {msg.splitlines()[-1] if msg else "Error"}')
        print(f'[Step4 Error]\n{msg}')
        self._report_error(msg)

    def _report_error(self, msg):
        """The error box (a seam: offscreen tests do not open it)."""
        QMessageBox.critical(self, 'Step 4', msg)


# ══════════════════════════════════════════════════════════════════════
#  Entry Point
# ══════════════════════════════════════════════════════════════════════

