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
from .step_frame import FRAME_TAB_QSS, StepFrame, StepFrameMetrics, free_tab_bar
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

    def __init__(self, parent=None, metrics=None, title_bar=None):
        super().__init__(parent)
        # The page frame (block A1b S4): the window hands over the one
        # `StepFrameMetrics` and a title bar built like Step3's; a page
        # standing on its own (the page-level tests) measures its own.
        self._frame_metrics_in = metrics
        self._title_bar_in = title_bar
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
        self._apply_job_to_scope(job)

    def _update_run_button(self):
        self._btn_run.setEnabled(self._job is not None and not self._running
                                 and not self.__dict__.get("_scope_blocked", False))

    # ── output scope (block S4-2) ─────────────────────────────────────

    NO_NUCLEI = 'this result has no nuclei beside its cells'

    def _apply_job_to_scope(self, job):
        """What the chosen result can give: nucleus / cytoplasm and the
        nuclear summary need a cell run that kept its nuclei (checked by
        default then); a nuclei-only run's primary object is the nucleus."""
        has_nuc = bool(job is not None and job.has_nuclei)
        nuclei_only = bool(job is not None and job.compartment == qs.NUCLEUS)
        key = None if job is None else (job.run_dir, job.roi_name, has_nuc, job.compartment)
        if key is not None and key == self.__dict__.get("_scope_key"):
            self._update_scope()             # the same result again: keep the user's choice
            return
        self._scope_key = key
        self._marker_list.blockSignals(True)
        self._marker_list.clear()
        for c in (job.channels if job is not None else ()):
            item = QtWidgets.QListWidgetItem(c.name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self._marker_list.addItem(item)
        self._marker_list.blockSignals(False)
        self._region_checks['cell'].setText('Nucleus' if nuclei_only else 'Whole cell')
        for cb in (self._region_checks['nucleus'], self._region_checks['cytoplasm'],
                   self._feature_checks['nuclear_summary']):
            cb.blockSignals(True)
            cb.setEnabled(has_nuc)
            cb.setChecked(has_nuc)
            cb.setToolTip('' if has_nuc else
                          ('the primary objects are already nuclei' if nuclei_only
                           else self.NO_NUCLEI))
            cb.blockSignals(False)
        self._update_scope()

    def scope(self):
        """(statistics, regions, features, write_csv) as checked."""
        stats = [k for k, cb in self._stat_checks.items() if cb.isChecked()]
        regions = [k for k in ('nucleus', 'cytoplasm')
                   if self._region_checks[k].isEnabled() and self._region_checks[k].isChecked()]
        features = [k for k in ('morphology', 'nuclear_summary')
                    if self._feature_checks[k].isEnabled() and self._feature_checks[k].isChecked()]
        return stats, regions, features, self._output_checks['csv'].isChecked()

    def distribution_scope(self):
        """(distribution statistics, markers in slide order) as checked."""
        dist = [k for k, cb in self._dist_checks.items() if cb.isChecked()]
        markers = [self._marker_list.item(i).text() for i in range(self._marker_list.count())
                   if self._marker_list.item(i).checkState() == Qt.Checked]
        return dist, markers

    def _update_scope(self):
        stats, regions, features, csv = self.scope()
        dist, markers = self.distribution_scope()
        self._marker_list.setVisible(bool(dist))
        self._scope_blocked = bool(dist and not markers)
        base = output_base(self._prefix_edit.text().strip())
        files = [f'{base}.h5ad', f'{base}_provenance.json'] + ([f'{base}.csv'] if csv else [])
        job = self._job
        primary = 'nucleus' if (job is not None and job.compartment == qs.NUCLEUS) else 'cell'
        x = f'X = {primary} {stats[0]}' if stats else '⚠  choose at least one statistic'
        if dist and not markers:
            x += '     ·     ⚠  choose the markers for the distribution statistics'
        elif dist:
            x += f'     ·     distribution: {len(markers)} marker' + ('s' if len(markers) > 1 else '')
        self._prefix_info.setText('Outputs:  ' + '   '.join(files) + '     ·     ' + x)
        risky = bool(job is not None and job.has_nuclei and job.seam_merge is None
                     and (regions or 'nuclear_summary' in features))
        self._risk_lbl.setText(
            'Risk: this result was made before Step 2\'s tile-seam fix — a few nuclei on tile '
            'seams may lie partly outside their cell, so the nucleus / cytoplasm values of '
            'those cells carry that error (the provenance file counts them). Re-run Step 2 '
            'to remove it.' if risky else '')
        self._risk_lbl.setVisible(risky)
        self._update_run_button()

    # ── UI ────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ONE `StepFrame` in its wide mode (block A1b S4, S0 ruling 4): the
        # page's single column is the frame's one merged slot. `root` is that
        # column's layout, put into the slot at assembly.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        root = QVBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

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

        # ── Output scope questionnaire (block S4-2) ─────────────────────
        # Groups open by default, each folded by the arrow on its title line.
        # Only the checked combinations are computed.
        scope = _box('What to quantify', '#e5c07b')
        sl = QVBoxLayout(scope)

        def _group(title):
            head = QHBoxLayout()
            arrow = QtWidgets.QToolButton()
            arrow.setArrowType(Qt.DownArrow)
            arrow.setAutoRaise(True)
            arrow.setCheckable(True)
            arrow.setChecked(True)
            name = QLabel(title)
            name.setStyleSheet('font-size:11px;font-weight:bold;color:#ddd;')
            head.addWidget(arrow)
            head.addWidget(name)
            head.addStretch()
            sl.addLayout(head)
            body = QWidget()
            row = QHBoxLayout(body)
            row.setContentsMargins(24, 0, 0, 4)
            sl.addWidget(body)

            def fold(opened, arrow=arrow, body=body):
                arrow.setArrowType(Qt.DownArrow if opened else Qt.RightArrow)
                body.setVisible(opened)
            arrow.toggled.connect(fold)
            return row, arrow, body

        def _checks(row, defs):
            out = {}
            for key, label, default in defs:
                cb = QtWidgets.QCheckBox(label)
                cb.setChecked(default)
                cb.setStyleSheet('font-size:11px;')
                cb.toggled.connect(lambda _on: self._update_scope())
                row.addWidget(cb)
                out[key] = cb
            row.addStretch()
            return out

        self._groups = {}
        row, arrow, body = _group('Statistics')
        self._groups['statistics'] = (arrow, body)
        self._stat_checks = _checks(row, [
            ('mean', 'Mean', True), ('sum', 'Sum (total int.)', True),
            ('std', 'Std dev', True), ('min', 'Min', True), ('max', 'Max', True)])
        # block S4-3: distribution statistics, slow, for the chosen markers
        dist_w = QWidget()
        dl = QVBoxLayout(dist_w)
        dl.setContentsMargins(24, 0, 0, 4)
        drow = QHBoxLayout()
        dlab = QLabel('Distribution (slow):')
        dlab.setStyleSheet('font-size:11px;color:#ccc;')
        drow.addWidget(dlab)
        self._dist_checks = _checks(drow, [
            ('median', 'Median', False), ('p90', 'P90', False), ('p95', 'P95', False),
            ('gini', 'Gini', False)])
        dl.addLayout(drow)
        self._marker_list = QtWidgets.QListWidget()
        self._marker_list.setMaximumHeight(110)
        self._marker_list.setStyleSheet('font-size:11px;')
        self._marker_list.setToolTip('Markers for the distribution statistics')
        self._marker_list.itemChanged.connect(lambda _item: self._update_scope())
        self._marker_list.setVisible(False)
        dl.addWidget(self._marker_list)
        sl.addWidget(dist_w)
        arrow.toggled.connect(dist_w.setVisible)
        self._dist_widget = dist_w
        row, arrow, body = _group('Expression regions  (for the expression values only)')
        self._groups['regions'] = (arrow, body)
        self._region_checks = _checks(row, [
            ('cell', 'Whole cell', True), ('nucleus', 'Nucleus', False),
            ('cytoplasm', 'Cytoplasm', False)])
        row, arrow, body = _group('Features')
        self._groups['features'] = (arrow, body)
        self._feature_checks = _checks(row, [
            ('expression', 'Expression', True), ('morphology', 'Morphology', True),
            ('nuclear_summary', 'Nuclear summary', False)])
        row, arrow, body = _group('Outputs')
        self._groups['outputs'] = (arrow, body)
        self._output_checks = _checks(row, [('h5ad', 'h5ad', True), ('csv', 'CSV', False)])
        for cb in (self._region_checks['cell'], self._feature_checks['expression'],
                   self._output_checks['h5ad']):
            cb.setEnabled(False)                     # always there
            cb.setToolTip('Always computed / written')
        self._risk_lbl = QLabel('')
        self._risk_lbl.setWordWrap(True)
        self._risk_lbl.setStyleSheet('color:#e5a050;font-size:11px;')
        self._risk_lbl.setVisible(False)
        sl.addWidget(self._risk_lbl)
        root.addWidget(scope)
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
        self._prefix_edit.setPlaceholderText('Leave blank for the default: cell_features.h5ad')
        self._prefix_edit.setStyleSheet('font-size:11px;')
        prefix_row.addWidget(self._prefix_edit, stretch=1)
        ol.addLayout(prefix_row)

        self._prefix_info = QLabel('')
        self._prefix_info.setStyleSheet('color:#888;font-size:10px;')
        ol.addWidget(self._prefix_info)
        self._prefix_edit.textChanged.connect(lambda _t: self._update_scope())
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

        self._assemble_frame(outer, root, nav)
        self._apply_job_to_scope(None)

    def _assemble_frame(self, outer, column, nav):
        """Put the page into its wide `StepFrame` (block A1b S4).

            title slot   `Step 4 — Cell Feature Extraction`
            tab row      blank
            merged slot  the page's one column, as before
            bottom slot  ← Back to Step 3 | stretch | Batch... | Stop | Extract
        """
        title_bar = self._title_bar_in
        if title_bar is None:
            title_bar = QLabel('Step 4 — Cell Feature Extraction')
            title_bar.setStyleSheet('font-size:12px;font-weight:bold;color:#eee;'
                                    'padding:4px 8px;')
        metrics = self._frame_metrics_in
        if metrics is None:
            title_bar.ensurePolished()
            metrics = StepFrameMetrics(
                title_height=title_bar.sizeHint().height(),
                bottom_height=max(nav.sizeHint().height(), 38),
                tab_qss=FRAME_TAB_QSS)
        frame = StepFrame(metrics, free_tab_bar, wide=True)
        self._frame = frame
        frame.set_title(title_bar)
        frame.wide_layout.addLayout(column)
        frame.bottom_layout.addLayout(nav)
        outer.addWidget(frame)

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

        stats, regions, features, write_csv = self.scope()
        distribution, markers = self.distribution_scope()
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
            regions     = regions,
            features    = features,
            write_csv   = write_csv,
            distribution = distribution,
            markers     = markers,
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
        outs = (self._worker.outputs or {}) if self._worker is not None else {}
        files = [outs.get('h5ad') or os.path.join(out_dir, f'{base_name}.h5ad')]
        if outs.get('csv'):
            files.append(outs['csv'])
        files.append(outs.get('provenance') or os.path.join(out_dir,
                                                            f'{base_name}_provenance.json'))
        note = ''
        outside = (outs.get('nucleus_outside') or {}).get('pixels', 0)
        if outside:
            note = (f'\n\nRisk: {outside:,} nucleus pixels lie outside their cell (a result '
                    f'made before Step 2\'s tile-seam fix); re-run Step 2 to remove them.')
        self._announce('Feature extraction complete',
                       'Outputs:\n\n' + '\n'.join(f'  {f}' for f in files) +
                       '\n\nThe provenance file says which channels were read raw and which '
                       'from Step 0\'s correction.' + note)

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

