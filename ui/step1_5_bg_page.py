"""
block01/ui/step1_5_bg_page.py — Step15BackgroundCorrectionPage.
"""

import os
import gc
import json
import traceback

import numpy as np
import tifffile
import zarr

from PyQt5 import QtWidgets, QtCore, QtGui
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QGroupBox, QSlider, QDoubleSpinBox, QProgressBar,
    QComboBox, QSplitter, QScrollArea, QSizePolicy,
    QMessageBox, QFileDialog, QButtonGroup, QCheckBox, QRadioButton,
)
import pyqtgraph as pg

from ..config import (
    OUTPUT_DIR, TOPHAT_RADIUS_DEFAULT, CUCIM_SIGMA_DEFAULT,
    TOPHAT_RADIUS_RANGE, CUCIM_SIGMA_RANGE,
    NUCLEUS_CONFIG, PATCH_COLORS,
)
from ..core.bg_correction import (
    CUCIM_AVAILABLE, CUCIM_IMPORT_ERROR,
    _normalize_correction_config, _load_correction_config,
    _apply_background_method_tiled, _compute_bg_metrics,
)
from ..core.io_loader import OMETIFFLoader
from .step0.search_ctrl import (
    BatchProcessWorker, WsiCorrectionWorker, _WsiCorrectionProgressDialog,
)

class Step15BackgroundCorrectionPage(QWidget):
    go_back = pyqtSignal()
    saved_and_continue = pyqtSignal(dict, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.loader = None
        self.output_dir = OUTPUT_DIR
        self.patches = []
        self.nucleus_channel = NUCLEUS_CONFIG["channel"]
        self.current_patch_idx = 0
        self.current_channel = None
        self._channel_rows = {}
        self._channel_order = []
        self._channel_decisions = {}
        self._loaded_config = None
        self._bg_state = "pre_choice"
        self._channel_state = {}
        self._preview_cache = {}
        self._batch_worker = None
        self._lazy_worker = None
        self._failed_channels = set()
        self._selection_enabled = False
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        title = QLabel('Step 1.5 — Background Correction')
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            'font-size:16px;font-weight:bold;color:#eee;'
            'background:#1a1a1a;padding:6px;border-radius:4px;'
        )
        root.addWidget(title)

        choice_box = QGroupBox('Start')
        choice_box.setStyleSheet(self._box_style('#d19a66'))
        cl = QVBoxLayout(choice_box)
        prompt = QLabel('Do background correction?')
        prompt.setStyleSheet('color:#eee;font-size:13px;font-weight:bold;')
        cl.addWidget(prompt)
        choice_row = QHBoxLayout()
        self._choice_yes = QPushButton('Yes')
        self._choice_no = QPushButton('No')
        for btn, color in ((self._choice_yes, '#4fa36d'), (self._choice_no, '#a35f5f')):
            btn.setCheckable(True)
            btn.setStyleSheet(
                f'QPushButton{{background:#1a1a1a;color:{color};border:1px solid {color};'
                'border-radius:4px;padding:6px 16px;font-weight:bold;}}'
                f'QPushButton:checked{{background:{color};color:#111;}}'
            )
            choice_row.addWidget(btn)
        choice_row.addStretch()
        cl.addLayout(choice_row)
        self._choice_yes.clicked.connect(lambda checked: self._on_bg_choice('yes') if checked else None)
        self._choice_no.clicked.connect(lambda checked: self._on_bg_choice('no') if checked else None)
        self._choice_status = QLabel('Choose Yes or No to unlock the rest of this step.')
        self._choice_status.setStyleSheet('color:#aaa;font-size:10px;')
        self._choice_status.setWordWrap(True)
        cl.addWidget(self._choice_status)
        root.addWidget(choice_box)

        split = QSplitter(Qt.Horizontal)
        root.addWidget(split, stretch=1)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(6)

        self._selection_box = QGroupBox('Channel Selection')
        self._selection_box.setStyleSheet(self._box_style('#61afef'))
        chl = QVBoxLayout(self._selection_box)
        ch_note = QLabel(
            'Select non-DAPI channels to process. Each row controls its own compute method. '
            'The nucleus/DAPI channel stays locked and always saves as original.'
        )
        ch_note.setWordWrap(True)
        ch_note.setStyleSheet('color:#aaa;font-size:10px;')
        chl.addWidget(ch_note)
        self._cb_all = QCheckBox('All non-DAPI')
        self._cb_all.stateChanged.connect(self._on_select_all_changed)
        chl.addWidget(self._cb_all)
        self._channel_list = QtWidgets.QListWidget()
        self._channel_list.setStyleSheet(
            'QListWidget{background:#111;border:1px solid #333;border-radius:4px;}'
            'QListWidget::item:selected{background:#1f2f3f;}'
        )
        self._channel_list.currentRowChanged.connect(self._on_channel_row_changed)
        chl.addWidget(self._channel_list, stretch=1)
        ll.addWidget(self._selection_box, stretch=3)

        self._param_box = QGroupBox('Method Parameters')
        self._param_box.setStyleSheet(self._box_style('#e5c07b'))
        ml = QVBoxLayout(self._param_box)
        self._tophat_value = QLabel()
        self._tophat_slider = QSlider(Qt.Horizontal)
        self._tophat_slider.setRange(*TOPHAT_RADIUS_RANGE)
        self._tophat_slider.setValue(TOPHAT_RADIUS_DEFAULT)
        self._tophat_slider.valueChanged.connect(self._on_params_changed)
        ml.addWidget(self._tophat_value)
        ml.addWidget(self._tophat_slider)
        ml.addWidget(self._hint_label('TopHat radius applies to any row using TopHat or Both.'))
        self._cucim_value = QLabel()
        self._cucim_slider = QSlider(Qt.Horizontal)
        self._cucim_slider.setRange(*CUCIM_SIGMA_RANGE)
        self._cucim_slider.setValue(CUCIM_SIGMA_DEFAULT)
        self._cucim_slider.valueChanged.connect(self._on_params_changed)
        ml.addWidget(self._cucim_value)
        ml.addWidget(self._cucim_slider)
        ml.addWidget(self._hint_label('cucim sigma applies to any row using cucim or Both.'))
        warn_text = (
            'cucim not available. Any cucim request falls back to the CPU gaussian implementation.'
        )
        if CUCIM_IMPORT_ERROR:
            warn_text += f' ({CUCIM_IMPORT_ERROR})'
        self._cucim_warn = QLabel(warn_text)
        self._cucim_warn.setWordWrap(True)
        self._cucim_warn.setVisible(not CUCIM_AVAILABLE)
        self._cucim_warn.setStyleSheet(
            'color:#ffb86c;font-size:10px;background:#2a1f14;'
            'border:1px solid #704b1f;border-radius:4px;padding:6px;'
        )
        ml.addWidget(self._cucim_warn)
        ll.addWidget(self._param_box)

        self._process_box = QGroupBox('Process Controls')
        self._process_box.setStyleSheet(self._box_style('#98c379'))
        pl = QVBoxLayout(self._process_box)
        proc_row = QHBoxLayout()
        self._btn_process = QPushButton('Process')
        self._btn_stop = QPushButton('Stop')
        self._btn_reset = QPushButton('Reset')
        self._btn_process.clicked.connect(self._on_process_clicked)
        self._btn_stop.clicked.connect(self._on_stop_clicked)
        self._btn_reset.clicked.connect(self._on_reset_clicked)
        proc_row.addWidget(self._btn_process)
        proc_row.addWidget(self._btn_stop)
        proc_row.addWidget(self._btn_reset)
        proc_row.addStretch()
        pl.addLayout(proc_row)
        self._bg_pbar = QProgressBar()
        self._bg_pbar.setRange(0, 100)
        self._bg_pbar.setValue(0)
        self._bg_pbar.setStyleSheet(
            'QProgressBar{border:1px solid #4a9;border-radius:3px;background:#111;}'
            'QProgressBar::chunk{background:#4a9;border-radius:2px;}'
        )
        pl.addWidget(self._bg_pbar)
        self._bg_start_status = QLabel('Waiting for your choice above.')
        self._bg_start_status.setStyleSheet('color:#aaa;font-size:10px;')
        self._bg_start_status.setWordWrap(True)
        pl.addWidget(self._bg_start_status)
        ll.addWidget(self._process_box)

        patch_box = QGroupBox('Patch ROI')
        patch_box.setStyleSheet(self._box_style('#56b6c2'))
        pbl = QVBoxLayout(patch_box)
        self._patch_buttons_row = QHBoxLayout()
        self._patch_buttons_row.setSpacing(4)
        pbl.addLayout(self._patch_buttons_row)
        self._patch_info = QLabel('No patch ROI available yet. Draw a patch in Step 1 first.')
        self._patch_info.setWordWrap(True)
        self._patch_info.setStyleSheet('color:#888;font-size:10px;')
        pbl.addWidget(self._patch_info)
        ll.addWidget(patch_box)

        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(6)

        self._comparison_box = QGroupBox('Comparison')
        self._comparison_box.setStyleSheet(self._box_style('#c678dd'))
        pvl = QVBoxLayout(self._comparison_box)
        top_row = QHBoxLayout()
        self._preview_vbs = []
        self._preview_imgs = []
        self._preview_texts = []
        for title_text in ('Original', 'TopHat', 'cucim'):
            col = QVBoxLayout()
            lbl = QLabel(title_text)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet('color:#ddd;font-size:11px;font-weight:bold;')
            gv = pg.GraphicsLayoutWidget()
            gv.setBackground('#111')
            vb = gv.addViewBox()
            vb.setAspectLocked(True)
            vb.invertY(True)
            vb.setMenuEnabled(False)
            item = pg.ImageItem()
            text = pg.TextItem('', color='#aaa', anchor=(0.5, 0.5))
            vb.addItem(item)
            vb.addItem(text)
            col.addWidget(lbl)
            col.addWidget(gv, stretch=1)
            top_row.addLayout(col, stretch=1)
            self._preview_vbs.append(vb)
            self._preview_imgs.append(item)
            self._preview_texts.append(text)
        self._orig_vb, self._top_vb, self._cu_vb = self._preview_vbs
        self._orig_img, self._top_img, self._cu_img = self._preview_imgs
        pvl.addLayout(top_row, stretch=1)
        self._preview_status = QLabel('Comparison is locked until the first batch run finishes or is stopped.')
        self._preview_status.setAlignment(Qt.AlignCenter)
        self._preview_status.setWordWrap(True)
        self._preview_status.setStyleSheet('color:#aaa;font-size:10px;')
        pvl.addWidget(self._preview_status)

        # ── 遮罩层：叠在comparison_box上，locked时显示 ─────────────────
        # 用一个包装容器来实现遮罩效果
        comparison_container = QWidget()
        comparison_container.setLayout(QVBoxLayout())
        comparison_container.layout().setContentsMargins(0, 0, 0, 0)
        comparison_container.layout().addWidget(self._comparison_box)
        self._comparison_overlay = QLabel(
            "🔒  Comparison Locked\n\nComplete Process first to unlock comparison.")
        self._comparison_overlay.setAlignment(Qt.AlignCenter)
        self._comparison_overlay.setWordWrap(True)
        self._comparison_overlay.setStyleSheet(
            "background:rgba(0,0,0,180);"
            "color:#e5c07b;"
            "font-size:16px;"
            "font-weight:bold;"
            "border-radius:6px;"
            "padding:20px;"
        )
        self._comparison_overlay.setParent(comparison_container)
        self._comparison_overlay.setVisible(True)
        self._comparison_overlay.raise_()
        # 遮罩跟随父容器大小
        comparison_container.resizeEvent = lambda e, c=comparison_container: (
            self._comparison_overlay.setGeometry(c.rect()),
            super(type(c), c).resizeEvent(e)
        )[1] if False else self._comparison_overlay.setGeometry(c.rect()) or super(type(c), c).resizeEvent(e)

        rl.addWidget(comparison_container, stretch=3)

        self._metrics_box = QGroupBox('Quantitative Metrics')
        self._metrics_box.setStyleSheet(self._box_style('#56b6c2'))
        metl = QVBoxLayout(self._metrics_box)
        self._metrics_original = QLabel('Original  → SNR: —  BG-CV: —')
        self._metrics_tophat = QLabel('TopHat    → SNR: —  BG-CV: —')
        self._metrics_cucim = QLabel('cucim     → SNR: —  BG-CV: —')
        for lbl in (self._metrics_original, self._metrics_tophat, self._metrics_cucim):
            lbl.setStyleSheet('color:#ddd;font-size:11px;background:#111;padding:4px;border-radius:3px;')
            metl.addWidget(lbl)
        rl.addWidget(self._metrics_box)

        self._decision_box = QGroupBox('Decision')
        self._decision_box.setStyleSheet(self._box_style('#e06c75'))
        dl = QVBoxLayout(self._decision_box)
        self._decision_group = QButtonGroup(self)
        self._dec_top = QRadioButton('Use TopHat result')
        self._dec_cu = QRadioButton('Use cucim result')
        self._dec_orig = QRadioButton('Keep original')
        self._dec_orig.setChecked(True)
        for rb in (self._dec_top, self._dec_cu, self._dec_orig):
            self._decision_group.addButton(rb)
            dl.addWidget(rb)
        self._apply_btn = QPushButton('Apply to this channel')
        self._apply_btn.setStyleSheet(
            'QPushButton{background:#255;color:white;border-radius:4px;padding:6px 12px;font-weight:bold;}'
            'QPushButton:hover{background:#377;}'
            'QPushButton:disabled{background:#333;color:#555;}'
        )
        self._apply_btn.clicked.connect(self._apply_current_channel_decision)
        dl.addWidget(self._apply_btn)
        self._decision_status = QLabel('Decisions are available after review unlocks.')
        self._decision_status.setWordWrap(True)
        self._decision_status.setStyleSheet('color:#aaa;font-size:10px;')
        dl.addWidget(self._decision_status)
        rl.addWidget(self._decision_box)

        split.addWidget(right)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 2)

        nav = QHBoxLayout()
        self._btn_back = QPushButton('← Back to Step 1')
        self._btn_back.setStyleSheet(
            'QPushButton{color:#fa8;border:1px solid #fa8;border-radius:4px;padding:6px 16px;}'
            'QPushButton:hover{background:#321;}'
        )
        self._btn_back.clicked.connect(self.go_back.emit)
        nav.addWidget(self._btn_back)
        nav.addStretch()

        self._btn_continue = QPushButton('Save & Continue →')
        self._btn_continue.setStyleSheet(
            'QPushButton{background:#2a5;color:white;border-radius:4px;'
            'padding:7px 22px;font-size:13px;font-weight:bold;}'
            'QPushButton:hover{background:#3b6;}'
        )
        self._btn_continue.clicked.connect(self._save_and_continue)
        nav.addWidget(self._btn_continue)
        root.addLayout(nav)

        self._refresh_slider_labels()
        self._clear_preview()
        self._set_state('pre_choice')

    @staticmethod
    def _box_style(color):
        return (
            f'QGroupBox{{border:1px solid {color};border-radius:5px;'
            f'margin-top:4px;font-weight:bold;color:{color};font-size:11px;}}'
        )

    @staticmethod
    def _hint_label(text):
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet('color:#888;font-size:10px;')
        return lbl

    def set_context(self, loader, output_dir, patches, nucleus_channel):
        self.loader = loader
        self.output_dir = output_dir or OUTPUT_DIR
        self.patches = list(patches or [])
        self.nucleus_channel = nucleus_channel or NUCLEUS_CONFIG["channel"]
        self._load_existing_config()
        self._init_channel_state()
        self._rebuild_channel_list()
        self._rebuild_patch_buttons()
        self._clear_preview()
        self._set_state('pre_choice')

    def _load_existing_config(self):
        path = os.path.join(self.output_dir, 'correction_config.json')
        self._loaded_config = _load_correction_config(path)
        raw_decisions = dict((self._loaded_config or {}).get("channel_decisions") or {})
        self._channel_decisions = {k: ("original" if v == "both" else v) for k, v in raw_decisions.items()}
        params = (self._loaded_config or {}).get("method_params") or {}
        self._tophat_slider.blockSignals(True)
        self._tophat_slider.setValue(int(params.get("tophat_radius", TOPHAT_RADIUS_DEFAULT)))
        self._tophat_slider.blockSignals(False)
        self._cucim_slider.blockSignals(True)
        self._cucim_slider.setValue(int(params.get("cucim_sigma", CUCIM_SIGMA_DEFAULT)))
        self._cucim_slider.blockSignals(False)
        self._refresh_slider_labels()

    def _init_channel_state(self):
        self._channel_state = {}
        if not self.loader:
            return
        for ch in self.loader.channel_names():
            decision = self._channel_decisions.get(ch, 'original')
            if ch == self.nucleus_channel:
                checked = False
                method = 'original'
            else:
                checked = decision in {'tophat', 'cucim'}
                method = decision if decision in {'tophat', 'cucim', 'both', 'original'} else 'original'
            self._channel_state[ch] = {
                'checked': checked,
                'method': method,
                'status': 'excluded' if ch == self.nucleus_channel else 'idle',
            }

    def _rebuild_channel_list(self):
        current = self.current_channel
        self._channel_rows.clear()
        self._channel_order = []
        self._channel_list.clear()
        if not self.loader:
            return

        for ch in self.loader.channel_names():
            item = QtWidgets.QListWidgetItem(self._channel_list)
            item.setSizeHint(QtCore.QSize(320, 34))
            row = QWidget()
            lay = QHBoxLayout(row)
            lay.setContentsMargins(8, 2, 6, 2)
            lay.setSpacing(6)

            is_nucleus = (ch == self.nucleus_channel)
            cb = QCheckBox()
            cb.setEnabled(not is_nucleus)
            cb.stateChanged.connect(lambda state, name=ch: self._on_channel_checked(name, state))
            lay.addWidget(cb)
            label = QLabel(ch if not is_nucleus else f'{ch}  (locked)')
            label.setStyleSheet('color:#ddd;font-size:11px;')
            lay.addWidget(label, stretch=1)
            combo = QComboBox()
            combo.addItems(['TopHat', 'cucim', 'Both', 'Original'])
            combo.setFixedWidth(72)
            combo.setEnabled(not is_nucleus)
            combo.setStyleSheet(
                'QComboBox{background:#1a1a1a;color:#ddd;border:1px solid #444;'
                'border-radius:3px;padding:1px 2px;font-size:10px;}'
                'QComboBox::drop-down{border:none;}'
                'QComboBox:disabled{color:#555;}'
            )
            combo.currentTextChanged.connect(lambda txt, name=ch: self._on_channel_method_changed(name, txt))
            lay.addWidget(combo)
            badge = QLabel()
            badge.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            badge.setMinimumWidth(86)
            lay.addWidget(badge)

            self._channel_list.setItemWidget(item, row)
            self._channel_rows[ch] = {
                "checkbox": cb,
                "label": label,
                "method_cb": combo,
                "badge": badge,
                "item": item,
                "row_widget": row,
            }
            self._channel_order.append(ch)
            self._refresh_channel_row(ch)

        if current in self._channel_rows:
            self.current_channel = current
            self._channel_list.blockSignals(True)
            self._channel_list.setCurrentItem(self._channel_rows[current]["item"])
            self._channel_list.blockSignals(False)
        else:
            first = next((ch for ch in self._channel_order if ch != self.nucleus_channel), None)
            self.current_channel = first
            if first:
                self._channel_list.blockSignals(True)
                self._channel_list.setCurrentItem(self._channel_rows[first]["item"])
                self._channel_list.blockSignals(False)
        self._update_select_all_state()

    def _refresh_channel_row(self, ch):
        row = self._channel_rows.get(ch)
        if not row:
            return
        st = self._channel_state.get(ch, {})
        cb = row['checkbox']
        combo = row['method_cb']
        cb.blockSignals(True)
        combo.blockSignals(True)
        cb.setChecked(bool(st.get('checked')))
        method = st.get('method', 'original')
        combo.setCurrentIndex({'tophat': 0, 'cucim': 1, 'both': 2, 'original': 3}.get(method, 3))
        cb.blockSignals(False)
        combo.blockSignals(False)

        if ch == self.nucleus_channel:
            txt, color, bg = 'excluded', '#666', ''
        else:
            status = st.get('status', 'idle')
            if status == 'running':
                txt, color, bg = '⟳ running', '#e5c07b', 'background:#2a2416;border-radius:3px;'
            elif status == 'done':
                txt, color, bg = '✓ done', '#6bffa0', 'background:#2a2a2a;border-radius:3px;'
            elif status == 'stopped':
                txt, color, bg = 'stopped', '#ffb86c', 'background:#2a2016;border-radius:3px;'
            elif status == 'failed':
                txt, color, bg = 'failed', '#ff6b6b', 'background:#2a1616;border-radius:3px;'
            elif st.get('checked'):
                txt, color, bg = 'queued', '#8ecae6', ''
            else:
                txt, color, bg = 'idle', '#777', ''
        row["badge"].setText(txt)
        row["badge"].setStyleSheet(f'color:{color};font-size:10px;font-weight:bold;')
        row["row_widget"].setStyleSheet(bg)

    def _rebuild_patch_buttons(self):
        while self._patch_buttons_row.count():
            item = self._patch_buttons_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        if not self.patches:
            self.current_patch_idx = 0
            self._patch_info.setText('No patch ROI available yet. Draw a patch in Step 1 first.')
            return

        self.current_patch_idx = min(self.current_patch_idx, len(self.patches) - 1)
        for i in range(len(self.patches)):
            btn = QPushButton(f'P{i+1}')
            btn.setCheckable(True)
            btn.setFixedSize(44, 22)
            color = PATCH_COLORS[i % len(PATCH_COLORS)]
            btn.setStyleSheet(
                f'QPushButton{{color:{color};border:1px solid {color};border-radius:3px;background:#1a1a1a;font-size:10px;font-weight:bold;}}'
                f'QPushButton:checked{{background:{color};color:#111;}}'
            )
            btn.clicked.connect(lambda _checked, idx=i: self._select_patch(idx))
            btn.setChecked(i == self.current_patch_idx)
            self._patch_buttons_row.addWidget(btn)
        self._patch_buttons_row.addStretch()
        self._update_patch_info()

    def _select_patch(self, idx):
        self.current_patch_idx = idx
        for i in range(self._patch_buttons_row.count()):
            widget = self._patch_buttons_row.itemAt(i).widget()
            if isinstance(widget, QPushButton):
                widget.setChecked(widget.text() == f'P{idx+1}')
        self._update_patch_info()
        if self._bg_state == 'review_ready' and self.current_channel:
            self._show_current_channel()

    def _update_patch_info(self):
        if not self.patches:
            return
        y0, y1, x0, x1 = self.patches[self.current_patch_idx]
        self._patch_info.setText(
            f'Current patch: P{self.current_patch_idx+1}  '
            f'[{y0}:{y1}, {x0}:{x1}]  '
            f'{(y1-y0):,}×{(x1-x0):,} px'
        )

    def _on_channel_row_changed(self, row):
        if row < 0 or row >= len(self._channel_order):
            self.current_channel = None
            self._update_decision_ui()
            return
        self.current_channel = self._channel_order[row]
        self._update_decision_ui()
        if self._bg_state == 'review_ready':
            self._show_current_channel()
        elif self._bg_state == 'selection_ready':
            self._preview_status.setText('Process selected channels first. Comparison is still locked.')
            self._preview_status.setStyleSheet('color:#aaa;font-size:10px;')

    def _update_decision_ui(self):
        ch = self.current_channel
        enabled = bool(
            self._bg_state == 'review_ready'
            and ch and ch != self.nucleus_channel and ch in self._channel_rows
        )
        self._apply_btn.setEnabled(enabled)
        if not enabled:
            if ch == self.nucleus_channel:
                self._decision_status.setText('The locked nucleus channel is always excluded from correction.')
            elif self._bg_state != 'review_ready':
                self._decision_status.setText('Decisions unlock after batch processing reaches review.')
            else:
                self._decision_status.setText('Select a non-DAPI channel to apply a decision.')
            self._dec_orig.setChecked(True)
            return
        decision = self._channel_decisions.get(ch, 'original')
        if decision == 'both':
            decision = 'original'
        if decision == 'tophat':
            self._dec_top.setChecked(True)
        elif decision == 'cucim':
            self._dec_cu.setChecked(True)
        else:
            self._dec_orig.setChecked(True)
        if decision != 'original':
            self._decision_status.setText(f'Saved decision for {ch}: {decision}')
        else:
            self._decision_status.setText(f'No correction assigned for {ch}. Select a method and click Apply.')

    def _refresh_slider_labels(self):
        self._tophat_value.setText(f'disk_radius: {self._tophat_slider.value()}')
        self._tophat_value.setStyleSheet('color:#ddd;font-size:11px;')
        self._cucim_value.setText(f'sigma: {self._cucim_slider.value()}')
        self._cucim_value.setStyleSheet('color:#ddd;font-size:11px;')

    def _on_params_changed(self):
        self._refresh_slider_labels()
        self._update_process_controls()

    @staticmethod
    def _metric_text(name, metrics):
        return (
            f'{name:<10} → SNR: {metrics["snr"]:.2f}  '
            f'BG-CV: {metrics["bg_cv"]:.2f}'
        )

    def _apply_current_channel_decision(self):
        ch = self.current_channel
        if not ch or ch == self.nucleus_channel or self._bg_state != 'review_ready':
            return
        if self._dec_top.isChecked():
            decision = 'tophat'
        elif self._dec_cu.isChecked():
            decision = 'cucim'
        else:
            decision = 'original'
        self._channel_decisions[ch] = decision
        self._refresh_channel_row(ch)
        self._decision_status.setText(f'Saved decision for {ch}: {decision}')

    def _build_config(self):
        decisions = {}
        for ch in self._channel_order:
            if ch == self.nucleus_channel:
                decisions[ch] = 'original'
            else:
                d = self._channel_decisions.get(ch, 'original')
                decisions[ch] = 'original' if d == 'both' else d
        return {
            "method_params": {
                "tophat_radius": int(self._tophat_slider.value()),
                "cucim_sigma": int(self._cucim_slider.value()),
            },
            "channel_decisions": decisions,
        }

    def _save_and_continue(self):
        os.makedirs(self.output_dir, exist_ok=True)
        config = self._build_config()
        out_path = os.path.join(self.output_dir, 'correction_config.json')
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        self.saved_and_continue.emit(config, out_path)

    def _set_state(self, state):
        self._bg_state = state
        selection_enabled = (state == 'selection_ready')
        review_enabled = (state == 'review_ready')
        processing = (state == 'batch_processing')
        skipped = (state == 'skipped')
        self._choice_yes.setEnabled(not processing)
        self._choice_no.setEnabled(not processing)

        self._selection_box.setEnabled(selection_enabled or review_enabled or processing)
        self._param_box.setEnabled(selection_enabled)
        self._process_box.setEnabled(selection_enabled or review_enabled or processing)
        self._comparison_box.setEnabled(review_enabled)
        self._metrics_box.setEnabled(review_enabled)
        self._decision_box.setEnabled(review_enabled)
        # 遮罩：只有review_ready时才隐藏
        if hasattr(self, '_comparison_overlay'):
            self._comparison_overlay.setVisible(not review_enabled)
        self._channel_list.setEnabled(selection_enabled or review_enabled)
        self._cb_all.setEnabled(selection_enabled or review_enabled)

        for ch, row in self._channel_rows.items():
            is_nucleus = (ch == self.nucleus_channel)
            row['checkbox'].setEnabled((selection_enabled or review_enabled) and not is_nucleus)
            row['method_cb'].setEnabled((selection_enabled or review_enabled) and not is_nucleus)

        for i in range(self._patch_buttons_row.count()):
            widget = self._patch_buttons_row.itemAt(i).widget()
            if isinstance(widget, QPushButton):
                widget.setEnabled(review_enabled)

        if state == 'pre_choice':
            self._choice_status.setText('Choose Yes or No to unlock the rest of this step.')
            self._bg_start_status.setText('Waiting for your choice above.')
        elif state == 'selection_ready':
            self._choice_status.setText('Channel selection is unlocked. Comparison stays locked until Process finishes or stops.')
            self._bg_start_status.setText('Select channels and methods, then click Process.')
        elif state == 'batch_processing':
            self._choice_status.setText('Background correction is running.')
        elif state == 'review_ready':
            self._choice_status.setText('Review is unlocked. Switching to cached channels is instant; unprocessed channels compute lazily on click.')
        elif skipped:
            self._choice_status.setText('Background correction is skipped. All channels will remain original.')
            self._preview_status.setText('Background correction skipped. Comparison is intentionally disabled.')
            self._preview_status.setStyleSheet('color:#aaa;font-size:10px;')

        self._btn_continue.setEnabled(review_enabled or skipped)
        self._update_process_controls()
        self._update_decision_ui()

    def _update_process_controls(self):
        eligible = bool(self._selected_methods_for_batch())
        has_processing = self._batch_worker is not None and self._batch_worker.isRunning()
        lazy_running = self._lazy_worker is not None and self._lazy_worker.isRunning()
        self._btn_process.setText('Re-Process' if self._bg_state == 'review_ready' else 'Process')
        self._btn_process.setEnabled(
            not has_processing and not lazy_running
            and self._bg_state in {'selection_ready', 'review_ready'} and eligible and bool(self.patches)
        )
        self._btn_stop.setEnabled(has_processing)
        self._btn_reset.setEnabled(
            self._bg_state in {'selection_ready', 'review_ready'} and not has_processing and not lazy_running
        )
        if not self.patches:
            self._btn_process.setEnabled(False)
            self._bg_start_status.setText('Draw at least one patch ROI in Step 1 before processing.')

    def _on_bg_choice(self, choice):
        self._choice_yes.blockSignals(True)
        self._choice_no.blockSignals(True)
        self._choice_yes.setChecked(choice == 'yes')
        self._choice_no.setChecked(choice == 'no')
        self._choice_yes.blockSignals(False)
        self._choice_no.blockSignals(False)
        if choice == 'no':
            self._reset_processing_state()
            for ch in self._channel_order:
                if ch == self.nucleus_channel:
                    continue
                st = self._channel_state[ch]
                st['checked'] = False
                st['status'] = 'idle'
                self._channel_decisions[ch] = 'original'
                self._refresh_channel_row(ch)
            self._update_select_all_state()
            self._set_state('skipped')
            return
        self._set_state('selection_ready')

    def _on_select_all_changed(self, state):
        checked = (state == Qt.Checked)
        for ch in self._channel_order:
            if ch == self.nucleus_channel:
                continue
            row = self._channel_rows.get(ch)
            st = self._channel_state.get(ch)
            if not row or not st:
                continue
            if self._bg_state == 'review_ready':
                self._invalidate_channel_cache(ch)
            row['checkbox'].blockSignals(True)
            row['checkbox'].setChecked(checked)
            row['checkbox'].blockSignals(False)
            st['checked'] = checked
            if not checked and self._bg_state in {'pre_choice', 'selection_ready', 'skipped'}:
                self._channel_decisions[ch] = 'original'
            self._refresh_channel_row(ch)
        self._update_process_controls()

    def _update_select_all_state(self):
        if not self._channel_order:
            return
        states = [
            self._channel_state.get(ch, {}).get('checked', False)
            for ch in self._channel_order if ch != self.nucleus_channel
        ]
        self._cb_all.blockSignals(True)
        if states and all(states):
            self._cb_all.setCheckState(Qt.Checked)
        elif any(states):
            self._cb_all.setCheckState(Qt.PartiallyChecked)
        else:
            self._cb_all.setCheckState(Qt.Unchecked)
        self._cb_all.blockSignals(False)

    def _on_channel_checked(self, ch, state):
        if ch == self.nucleus_channel or ch not in self._channel_state:
            return
        if self._bg_state == 'review_ready':
            self._invalidate_channel_cache(ch)
        self._channel_state[ch]['checked'] = (state == Qt.Checked)
        if state != Qt.Checked and self._bg_state in {'pre_choice', 'selection_ready', 'skipped'}:
            self._channel_decisions[ch] = 'original'
        self._refresh_channel_row(ch)
        self._update_select_all_state()
        self._update_process_controls()

    def _on_channel_method_changed(self, ch, txt):
        if ch not in self._channel_state:
            return
        method = txt.lower()
        if method not in {'tophat', 'cucim', 'both', 'original'}:
            method = 'original'
        if self._bg_state == 'review_ready':
            self._invalidate_channel_cache(ch)
        self._channel_state[ch]['method'] = method
        self._refresh_channel_row(ch)
        self._update_process_controls()

    def _selected_methods_for_batch(self):
        """只看勾选状态，勾选即跑，method只有tophat/cucim/both。"""
        selected = {}
        for ch in self._channel_order:
            if ch == self.nucleus_channel:
                continue
            st = self._channel_state.get(ch, {})
            if st.get('checked'):
                method = st.get('method', 'original')
                if method not in {'tophat', 'cucim', 'both', 'original'}:
                    method = 'original'
                selected[ch] = method
        return selected

    def _clear_preview(self):
        self._set_preview_image(0, None, 'Waiting')
        self._set_preview_image(1, None, 'Not computed')
        self._set_preview_image(2, None, 'Not computed')
        self._metrics_original.setText('Original  → SNR: —  BG-CV: —')
        self._metrics_tophat.setText('TopHat    → SNR: —  BG-CV: —')
        self._metrics_cucim.setText('cucim     → SNR: —  BG-CV: —')

    def _set_preview_image(self, idx, arr, overlay_text=None):
        if arr is None:
            arr = np.zeros((64, 64), dtype=np.float32)
        self._preview_imgs[idx].setImage(arr, autoLevels=False, levels=[0.0, 1.0])
        h, w = arr.shape[:2]
        text = self._preview_texts[idx]
        text.setText(overlay_text or '')
        text.setPos(w / 2.0, h / 2.0)
        self._preview_vbs[idx].autoRange()

    def _show_payload(self, payload, ch):
        if payload is None:
            self._clear_preview()
            return
        self._set_preview_image(0, payload.get('original_disp'), None)
        top_not = 'Not computed' if payload.get('tophat_disp') is None else None
        cu_not = 'Not computed' if payload.get('cucim_disp') is None else None
        self._set_preview_image(1, payload.get('tophat_disp'), top_not)
        self._set_preview_image(2, payload.get('cucim_disp'), cu_not)
        self._metrics_original.setText(self._metric_text('Original', payload.get('original_metrics') or {"snr": 0.0, "bg_cv": 0.0}))
        if payload.get('tophat_disp') is None:
            self._metrics_tophat.setText('TopHat    → Not computed')
        else:
            self._metrics_tophat.setText(self._metric_text('TopHat', payload.get('tophat_metrics') or {"snr": 0.0, "bg_cv": 0.0}))
        if payload.get('cucim_disp') is None:
            self._metrics_cucim.setText('cucim     → Not computed')
        else:
            self._metrics_cucim.setText(self._metric_text('cucim', payload.get('cucim_metrics') or {"snr": 0.0, "bg_cv": 0.0}))
        self._preview_status.setText(f'{ch}  P{self.current_patch_idx+1}')
        self._preview_status.setStyleSheet('color:#aaa;font-size:10px;')

    def _cache_payload(self, ch, patch_idx, payload):
        self._preview_cache[(ch, patch_idx)] = payload

    def _invalidate_channel_cache(self, ch):
        keys = [key for key in self._preview_cache if key[0] == ch]
        for key in keys:
            del self._preview_cache[key]
        if ch in self._channel_state and ch != self.nucleus_channel:
            self._channel_state[ch]['status'] = 'idle'
            self._refresh_channel_row(ch)

    def _channel_has_cache(self, ch):
        return any(k[0] == ch for k in self._preview_cache)

    def _show_current_channel(self):
        ch = self.current_channel
        if not ch:
            return
        if ch == self.nucleus_channel:
            self._preview_status.setText('The nucleus channel is excluded from correction review.')
            self._clear_preview()
            return
        payload = self._preview_cache.get((ch, self.current_patch_idx))
        if payload is not None:
            self._show_payload(payload, ch)
            return
        if self._channel_has_cache(ch):
            for idx in range(len(self.patches)):
                payload = self._preview_cache.get((ch, idx))
                if payload is not None:
                    self.current_patch_idx = idx
                    self._sync_patch_buttons()
                    self._update_patch_info()
                    self._show_payload(payload, ch)
                    return
        self._start_lazy_compute(ch)

    def _sync_patch_buttons(self):
        for i in range(self._patch_buttons_row.count()):
            widget = self._patch_buttons_row.itemAt(i).widget()
            if isinstance(widget, QPushButton):
                widget.setChecked(widget.text() == f'P{self.current_patch_idx+1}')

    def _start_lazy_compute(self, ch):
        if self._bg_state != 'review_ready' or ch == self.nucleus_channel:
            return
        method = self._channel_state.get(ch, {}).get('method', 'original')
        self._channel_state[ch]['status'] = 'running'
        self._refresh_channel_row(ch)
        self._preview_status.setText(f'Computing {ch} across all patches on demand…')
        self._preview_status.setStyleSheet('color:#e5c07b;font-size:10px;')
        self._channel_list.setEnabled(False)
        self._lazy_worker = BatchProcessWorker(
            self.loader,
            self.patches,
            {ch: method},
            self.nucleus_channel,
            self._tophat_slider.value(),
            self._cucim_slider.value(),
            max_gpu_workers=2,
        )
        self._lazy_worker.channel_patch_done.connect(self._on_lazy_patch_done)
        self._lazy_worker.channel_done.connect(self._on_lazy_channel_done)
        self._lazy_worker.error_signal.connect(self._on_lazy_error)
        self._lazy_worker.canceled.connect(self._on_lazy_canceled)
        self._lazy_worker.all_done.connect(self._on_lazy_finished)
        self._lazy_worker.start()
        self._update_process_controls()

    def _on_lazy_patch_done(self, ch, patch_idx, payload):
        self._cache_payload(ch, patch_idx, payload)
        if ch == self.current_channel and patch_idx == self.current_patch_idx:
            self._show_payload(payload, ch)

    def _on_lazy_channel_done(self, ch):
        self._channel_state[ch]['status'] = 'done'
        self._refresh_channel_row(ch)

    def _on_lazy_error(self, ch, _patch_idx, msg):
        if ch != '__global__':
            self._channel_state[ch]['status'] = 'failed'
            self._refresh_channel_row(ch)
        print(f'[Step1.5 Lazy Error]\n{msg}')

    def _on_lazy_canceled(self):
        if self.current_channel in self._channel_state:
            self._channel_state[self.current_channel]['status'] = 'stopped'
            self._refresh_channel_row(self.current_channel)
        self._channel_list.setEnabled(True)
        self._lazy_worker = None
        self._update_process_controls()

    def _on_lazy_finished(self):
        self._channel_list.setEnabled(True)
        self._lazy_worker = None
        self._update_process_controls()
        if self.current_channel:
            payload = self._preview_cache.get((self.current_channel, self.current_patch_idx))
            if payload is not None:
                self._show_payload(payload, self.current_channel)

    def _on_process_clicked(self):
        if self._bg_state == 'review_ready':
            self._reset_processing_state()
        methods = self._selected_methods_for_batch()
        if not methods:
            QMessageBox.information(self, 'No channels selected', 'Select at least one checked non-DAPI channel with a method other than Original.')
            return
        if not self.patches:
            QMessageBox.information(self, 'No patch ROI', 'Draw at least one patch ROI in Step 1 before processing.')
            return
        self._failed_channels.clear()
        for ch in self._channel_order:
            if ch == self.nucleus_channel:
                continue
            st = self._channel_state[ch]
            if ch in methods:
                st['status'] = 'queued'
            elif st['status'] not in {'done'}:
                st['status'] = 'idle'
            self._refresh_channel_row(ch)
        self._set_state('batch_processing')
        self._bg_pbar.setValue(0)
        self._bg_start_status.setText(f'Processing {len(methods)} channel(s) across {len(self.patches)} patch(es)…')
        self._preview_status.setText('Batch processing started. Comparison will unlock after the run completes or is stopped.')
        self._preview_status.setStyleSheet('color:#e5c07b;font-size:10px;')
        self._batch_worker = BatchProcessWorker(
            self.loader,
            self.patches,
            methods,
            self.nucleus_channel,
            self._tophat_slider.value(),
            self._cucim_slider.value(),
            max_gpu_workers=4,
        )
        self._batch_worker.channel_patch_done.connect(self._on_batch_patch_done)
        self._batch_worker.channel_done.connect(self._on_batch_channel_done)
        self._batch_worker.progress.connect(self._on_batch_progress)
        self._batch_worker.error_signal.connect(self._on_batch_error)
        self._batch_worker.canceled.connect(self._on_batch_canceled)
        self._batch_worker.all_done.connect(self._on_batch_all_done)
        self._batch_worker.start()

    def _on_stop_clicked(self):
        if self._batch_worker and self._batch_worker.isRunning():
            self._batch_worker.stop()

    def _on_reset_clicked(self):
        self._reset_processing_state()
        self._set_state('selection_ready')

    def _reset_processing_state(self):
        self._preview_cache.clear()
        self._clear_preview()
        self._failed_channels.clear()
        self._bg_pbar.setValue(0)
        for ch in self._channel_order:
            if ch == self.nucleus_channel:
                continue
            self._channel_state[ch]['status'] = 'idle'
            self._refresh_channel_row(ch)
        self._bg_start_status.setText('Selection reset. Adjust channels or parameters, then click Process.')

    def _on_batch_patch_done(self, ch, patch_idx, payload):
        self._cache_payload(ch, patch_idx, payload)
        if self.current_channel == ch and self.current_patch_idx == patch_idx:
            self._show_payload(payload, ch)

    def _on_batch_channel_done(self, ch):
        if ch not in self._failed_channels:
            self._channel_state[ch]['status'] = 'done'
        self._refresh_channel_row(ch)
        done_count = sum(1 for name in self._selected_methods_for_batch() if self._channel_state.get(name, {}).get('status') == 'done')
        total = max(1, len(self._selected_methods_for_batch()))
        self._bg_pbar.setValue(int(done_count / total * 100))
        self._bg_start_status.setText(f'Finished {done_count}/{total} selected channel(s).')

    def _on_batch_progress(self, _done, _total, msg):
        self._bg_start_status.setText(msg)
        ch = None
        if msg.startswith('Reading ') or msg.startswith('Done '):
            ch = msg.split()[1]
        if ch in self._channel_state and self._channel_state[ch]['status'] == 'queued':
            self._channel_state[ch]['status'] = 'running'
            self._refresh_channel_row(ch)

    def _on_batch_error(self, ch, _patch_idx, msg):
        if ch != '__global__':
            self._failed_channels.add(ch)
            self._channel_state[ch]['status'] = 'failed'
            self._refresh_channel_row(ch)
        else:
            print(f'[Step1.5 Batch Error]\n{msg}')
            self._bg_start_status.setText('Batch processing failed. Review is unlocked for any cached results.')
            self._batch_worker = None
            self._set_state('review_ready')

    def _on_batch_canceled(self):
        for ch in self._selected_methods_for_batch():
            if self._channel_state.get(ch, {}).get('status') in {'queued', 'running'}:
                self._channel_state[ch]['status'] = 'stopped'
                self._refresh_channel_row(ch)
        self._bg_start_status.setText('Stopped. Completed channel results were kept in cache.')
        self._batch_worker = None
        self._set_state('review_ready')

    def _on_batch_all_done(self):
        self._batch_worker = None
        self._bg_pbar.setValue(100)
        self._bg_start_status.setText('Batch processing finished. Review is unlocked.')
        self._set_state('review_ready')
        if self.current_channel:
            self._show_current_channel()


