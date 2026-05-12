"""
block01/ui/step0/step0_page.py — Step0Page (main Step 0 QWidget).
"""

import os
import gc
import json
import shutil
import traceback
import multiprocessing as mp
from queue import Empty

import numpy as np
import zarr

from PyQt5 import QtWidgets, QtCore, QtGui
from PyQt5.QtCore import Qt, QTimer, QRectF, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QGroupBox, QSlider, QDoubleSpinBox,
    QInputDialog, QMessageBox, QFileDialog,
    QComboBox, QFrame, QProgressBar, QSizePolicy,
    QRadioButton, QButtonGroup, QSplitter,
)
import pyqtgraph as pg

from ...config import (
    OME_TIFF_FILE, OUTPUT_DIR, CHANNEL_NAME_MAP,
    INITIAL_GROUPS, NUCLEUS_CONFIG, PHASE1_DIAMETERS,
    PHASE2_FLOW, PHASE2_CELLPROB, DEFAULT_MODEL,
    PREVIEW_DOWNSAMPLE, OVERVIEW_DOWNSAMPLE,
    TOPHAT_RADIUS_DEFAULT, TOPHAT_RADIUS_RANGE,
    CUCIM_SIGMA_DEFAULT, CUCIM_SIGMA_RANGE,
    BG_CORR_MAX_TILE, PATCH_COLORS,
)
from ...core.bg_correction import (
    CUCIM_AVAILABLE, CUCIM_IMPORT_ERROR,
    _load_correction_config,
)
from ...core.io_loader import OMETIFFLoader
from ...core.fusion_engine import FusionEngine
from ...workers.cellpose_worker import (
    CellposeWorker, PreviewLoaderThread, run_cellpose_process,
)
from .overview_panel import OverviewPanel, TileSelectDialog, FullFusionWorker
from .config_panel import ConfigPanel
from .result_grid import ResultGridPanel
from .search_ctrl import (
    SearchCtrlPanel, BatchProcessWorker,
    WsiCorrectionWorker, BackgroundPreviewWorker,
    _WsiCorrectionProgressDialog,
)
from ...utils.roi_project import (
    create_roi_context,
    mark_roi_step,
    roi_shape_from_bbox,
)

class Step0Page(QWidget):
    step0_complete = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.loader = None
        self.output_dir = OUTPUT_DIR
        self.ome_path = OME_TIFF_FILE
        self.panel_csv_path = ""
        self.panel_groups = {}
        self.nucleus_channel = NUCLEUS_CONFIG["channel"]
        self.patches = []
        self.rois = []
        self.current_patch_idx = 0
        self.current_channel = None
        self._preview_worker = None
        self._preview_req_id = 0
        self._preview_debounce = QTimer(self)
        self._preview_debounce.setSingleShot(True)
        self._preview_debounce.timeout.connect(self._start_preview_compute)
        self._wsi_worker = None
        self._wsi_dialog = None
        self._channel_rows = {}
        self._channel_order = []
        self._channel_decisions = {}
        self._loaded_config = None
        self._roi_selected_idx = -1
        self._roi_context = None
        self._project_output_dir = OUTPUT_DIR
        self._patch_selected_idx = -1
        self._roi_selected_indices = []
        self._patch_selected_indices = []
        self._bg_queue = []
        self._bg_queue_idx = 0
        self._bg_n_tophat = 0
        self._bg_n_cucim = 0
        self._bg_n_orig = 0
        self._bg_n_total = 0
        self._bg_workers = []
        # 预览结果缓存（供toggle复用）和zoom联动防循环flag
        self._last_payload = None
        self._zoom_lock_active = False
        # 预览结果缓存：key=(channel, patch_idx) → payload dict
        self._preview_cache: dict = {}
        # 通道颜色：key=channel_name → (R,G,B) float 0-1
        self._channel_colors: dict = {}
        # 通道方法选择：key=channel_name → "tophat"|"cucim"|"both"
        self._channel_methods: dict = {}
        # 批量处理worker
        self._batch_worker: BatchProcessWorker = None
        # 计算完成的通道集合
        self._computed_channels: set = set()
        # 参数是否被修改（提示需要重新Process）
        self._params_dirty: bool = False
        # Process是否已完成（只有完成后才允许按需计算）
        self._process_completed: bool = False
        # 按需计算worker（点击未计算通道时）
        self._ondemand_worker = None
        self._ondemand_workers: list = []
        self._build_ui()

    def _build_ui(self):
        # ── 顶层：垂直布局，不用 ScrollArea，充满窗口 ──────────────────
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(4)

        # ══ Section A — 单行横排 file_bar ══════════════════════════════
        file_bar = QWidget()
        file_bar.setStyleSheet("background:#1a1a2a;border-radius:4px;")
        file_bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        fb = QHBoxLayout(file_bar)
        fb.setContentsMargins(8, 4, 8, 4)
        fb.setSpacing(6)

        _edit_style = (
            "font-size:11px;background:#111;color:#ddd;"
            "border:1px solid #444;border-radius:3px;padding:2px 4px;"
        )
        _btn_style = (
            "QPushButton{font-size:10px;color:#8cf;border:1px solid #8cf;"
            "border-radius:3px;padding:2px 6px;}"
            "QPushButton:hover{background:#1a2a4a;}"
        )

        # OME-TIFF
        fb.addWidget(QLabel("OME-TIFF:"))
        self._ome_path_edit = QtWidgets.QLineEdit(OME_TIFF_FILE)
        self._ome_path_edit.setStyleSheet(_edit_style)
        self._ome_path_edit.setMinimumWidth(260)
        fb.addWidget(self._ome_path_edit, stretch=3)
        _btn_ome = QPushButton("Browse")
        _btn_ome.setFixedWidth(58)
        _btn_ome.setStyleSheet(_btn_style)
        _btn_ome.clicked.connect(self._browse_ome)
        fb.addWidget(_btn_ome)

        # Output dir
        fb.addWidget(QLabel("Output dir:"))
        self._out_path_edit = QtWidgets.QLineEdit(OUTPUT_DIR)
        self._out_path_edit.setStyleSheet(_edit_style)
        self._out_path_edit.setMinimumWidth(180)
        fb.addWidget(self._out_path_edit, stretch=2)
        _btn_out = QPushButton("Browse")
        _btn_out.setFixedWidth(58)
        _btn_out.setStyleSheet(_btn_style)
        _btn_out.clicked.connect(self._browse_out_dir)
        fb.addWidget(_btn_out)

        # Panel CSV
        fb.addWidget(QLabel("Panel CSV:"))
        self._panel_csv_edit = QtWidgets.QLineEdit()
        self._panel_csv_edit.setPlaceholderText("panel.csv  (optional)")
        self._panel_csv_edit.setStyleSheet(_edit_style)
        self._panel_csv_edit.setMinimumWidth(160)
        fb.addWidget(self._panel_csv_edit, stretch=2)
        _btn_panel = QPushButton("Browse")
        _btn_panel.setFixedWidth(58)
        _btn_panel.setStyleSheet(_btn_style)
        _btn_panel.clicked.connect(self._browse_panel_csv)
        fb.addWidget(_btn_panel)

        # Load button + status
        self._btn_load = QPushButton("▶  Load")
        self._btn_load.setFixedWidth(72)
        self._btn_load.setStyleSheet(
            "QPushButton{background:#2a5;color:white;font-weight:bold;"
            "font-size:11px;border-radius:3px;padding:3px 8px;}"
            "QPushButton:hover{background:#3b6;}"
        )
        self._btn_load.clicked.connect(self._reload_from_paths)
        fb.addWidget(self._btn_load)

        self._load_status = QLabel("No project loaded.")
        self._load_status.setStyleSheet("color:#aaa;font-size:11px;")
        fb.addWidget(self._load_status)

        outer.addWidget(file_bar)   # Section A 固定高度，不拉伸

        # ══ Section B + C — 左右分栏，撑满剩余空间 ════════════════════
        main_split = QSplitter(Qt.Horizontal)
        main_split.setStyleSheet("QSplitter::handle{background:#333;width:3px;}")
        main_split.setChildrenCollapsible(False)
        self._main_split = main_split   # 保存引用，showEvent里固定比例
        outer.addWidget(main_split, stretch=1)   # 占用所有剩余高度

        # ── Section B（左 25%）— ROI & Patch Definition ───────────────
        sec_b = QWidget()
        sec_b.setStyleSheet("background:#1c1c1c;")
        bl = QVBoxLayout(sec_b)
        bl.setContentsMargins(4, 4, 4, 4)
        bl.setSpacing(4)

        b_title = QLabel("B — ROI & Patch")
        b_title.setAlignment(Qt.AlignCenter)
        b_title.setStyleSheet(
            "font-size:11px;font-weight:bold;color:#98c379;"
            "border:1px solid #98c379;border-radius:3px;padding:2px;"
        )
        bl.addWidget(b_title)

        # ROI/Patch 统一工具栏：模式切换 + 一键删除 + 重命名
        _ts = (
            "QPushButton{{color:{c};border:1px solid {c};border-radius:3px;"
            "padding:3px 7px;font-size:10px;background:#161616;}}"
            "QPushButton:hover{{background:#222;}}"
            "QPushButton:checked{{background:{c};color:#111;font-weight:bold;}}"
        )
        tool_row = QHBoxLayout()
        tool_row.setSpacing(3)

        self._btn_mode_roi = QPushButton("🔲 ROI")
        self._btn_mode_roi.setCheckable(True)
        self._btn_mode_roi.setToolTip(
            "Switch to ROI mode — click vertices on overview, Enter/right-click to close")
        self._btn_mode_roi.setStyleSheet(_ts.format(c="#6bcb77"))

        self._btn_mode_patch = QPushButton("📍 Patch")
        self._btn_mode_patch.setCheckable(True)
        self._btn_mode_patch.setChecked(True)
        self._btn_mode_patch.setToolTip(
            "Switch to Patch mode — drag rectangle inside a ROI")
        self._btn_mode_patch.setStyleSheet(_ts.format(c="#4d96ff"))

        self._btn_delete_sel = QPushButton("✕ Del")
        self._btn_delete_sel.setToolTip(
            "Delete selected item:\n"
            "  • Patch selected → delete that patch\n"
            "  • ROI selected   → delete ROI + all its patches")
        self._btn_delete_sel.setStyleSheet(_ts.format(c="#e06c75"))

        self._btn_rename_roi = QPushButton("✎")
        self._btn_rename_roi.setToolTip("Rename selected ROI")
        self._btn_rename_roi.setStyleSheet(_ts.format(c="#e5c07b"))

        tool_row.addWidget(self._btn_mode_roi)
        tool_row.addWidget(self._btn_mode_patch)
        tool_row.addSpacing(6)
        tool_row.addWidget(self._btn_rename_roi)
        tool_row.addStretch()

        self._btn_mode_roi.clicked.connect(lambda: self._set_draw_mode("roi"))
        self._btn_mode_patch.clicked.connect(lambda: self._set_draw_mode("patch"))
        self._btn_delete_sel.clicked.connect(self._delete_selected_item)
        self._btn_rename_roi.clicked.connect(self._rename_selected_roi)
        bl.addLayout(tool_row)

        # Overview（DAPI thumbnail + patch 绘制）
        _dummy_loader = type("_DummyLoader", (), {
            "shape": (0, 0), "ch_map": {}, "channel_names": lambda s: []
        })()
        self.overview = OverviewPanel(_dummy_loader, self.nucleus_channel, lazy=True)
        self.overview.patches_changed.connect(self._on_patches_changed)
        self.overview.rois_changed.connect(self._on_rois_changed)
        self._wrap_overview_patch_limit()
        bl.addWidget(self.overview, stretch=3)   # overview 占大部分高度

        # ROI 列表区（标题行 + Del按钮 + 列表）
        roi_hdr = QHBoxLayout()
        roi_hdr.setSpacing(4)
        roi_lbl = QLabel("ROIs")
        roi_lbl.setStyleSheet("color:#98c379;font-size:10px;font-weight:bold;")
        roi_hdr.addWidget(roi_lbl)
        roi_hdr.addStretch()
        self._btn_del_roi = QPushButton("✕ Del")
        self._btn_del_roi.setToolTip(
            "Delete selected ROI(s) and all their patches\n"
            "(Ctrl/Shift+click to multi-select)")
        self._btn_del_roi.setStyleSheet(
            "QPushButton{color:#e06c75;border:1px solid #e06c75;border-radius:3px;"
            "padding:1px 6px;font-size:10px;background:#161616;}"
            "QPushButton:hover{background:#2a1111;}"
        )
        self._btn_del_roi.clicked.connect(self._delete_selected_rois)
        roi_hdr.addWidget(self._btn_del_roi)
        bl.addLayout(roi_hdr)

        self._roi_list = QtWidgets.QListWidget()
        self._roi_list.setSelectionMode(
            QtWidgets.QAbstractItemView.ExtendedSelection)
        self._roi_list.setStyleSheet(
            "QListWidget{background:#111;border:1px solid #333;border-radius:3px;font-size:10px;}"
            "QListWidget::item:selected{background:#1f3a2a;}"
        )
        self._roi_list.setMaximumHeight(80)
        self._roi_list.itemSelectionChanged.connect(self._on_roi_selection_changed)
        bl.addWidget(self._roi_list)

        # Patch 列表区（标题行 + Del按钮 + 列表）
        patch_hdr = QHBoxLayout()
        patch_hdr.setSpacing(4)
        patch_lbl = QLabel("Patches")
        patch_lbl.setStyleSheet("color:#98c379;font-size:10px;font-weight:bold;")
        patch_hdr.addWidget(patch_lbl)
        patch_hdr.addStretch()
        self._btn_del_patch = QPushButton("✕ Del")
        self._btn_del_patch.setToolTip(
            "Delete selected patch(es)\n"
            "(Ctrl/Shift+click to multi-select)")
        self._btn_del_patch.setStyleSheet(
            "QPushButton{color:#e06c75;border:1px solid #e06c75;border-radius:3px;"
            "padding:1px 6px;font-size:10px;background:#161616;}"
            "QPushButton:hover{background:#2a1111;}"
        )
        self._btn_del_patch.clicked.connect(self._delete_selected_patches)
        patch_hdr.addWidget(self._btn_del_patch)
        bl.addLayout(patch_hdr)

        self._patch_list = QtWidgets.QListWidget()
        self._patch_list.setSelectionMode(
            QtWidgets.QAbstractItemView.ExtendedSelection)
        self._patch_list.setStyleSheet(
            "QListWidget{background:#111;border:1px solid #333;border-radius:3px;font-size:10px;}"
            "QListWidget::item:selected{background:#2b1f2f;}"
        )
        self._patch_list.setMaximumHeight(80)
        self._patch_list.itemSelectionChanged.connect(self._on_patch_selection_changed)
        bl.addWidget(self._patch_list)

        self._patch_warning = QLabel("")
        self._patch_warning.setStyleSheet("color:#ffb86c;font-size:10px;font-weight:bold;")
        self._patch_warning.setVisible(False)
        bl.addWidget(self._patch_warning)

        main_split.addWidget(sec_b)

        # ── Section C（右 75%）— Background Correction ────────────────
        sec_c = QWidget()
        sec_c.setStyleSheet("background:#1c1c1c;")
        cl = QVBoxLayout(sec_c)
        cl.setContentsMargins(4, 4, 4, 4)
        cl.setSpacing(4)

        c_title = QLabel("C — Background Correction")
        c_title.setAlignment(Qt.AlignCenter)
        c_title.setStyleSheet(
            "font-size:11px;font-weight:bold;color:#c678dd;"
            "border:1px solid #c678dd;border-radius:3px;padding:2px;"
        )
        cl.addWidget(c_title)

        # Section C 内部：左（通道列表+参数+patch选择） / 右（三联预览+metrics+决策）
        c_split = QSplitter(Qt.Horizontal)
        c_split.setStyleSheet("QSplitter::handle{background:#333;width:3px;}")
        cl.addWidget(c_split, stretch=1)

        # C-左：通道列表 + 参数滑块 + patch 选择
        c_left = QWidget()
        cll = QVBoxLayout(c_left)
        cll.setContentsMargins(0, 0, 0, 0)
        cll.setSpacing(4)

        # ── 通道列表（勾选 + 方法下拉 + 状态图标）─────────────────────
        ch_box = QGroupBox("Channels")
        ch_box.setStyleSheet(self._box_style("#61afef"))
        chl = QVBoxLayout(ch_box)

        # All选项行
        all_row = QHBoxLayout()
        self._cb_all = QtWidgets.QCheckBox("All channels")
        self._cb_all.setStyleSheet("color:#ddd;font-size:11px;")
        self._cb_all.setToolTip("Select all non-nucleus channels")
        self._cb_all.stateChanged.connect(self._on_select_all_changed)
        self._method_all = QtWidgets.QComboBox()
        self._method_all.addItems(["TopHat", "cucim", "Both"])
        self._method_all.setCurrentIndex(2)  # default Both
        self._method_all.setStyleSheet(
            "QComboBox{background:#1a1a1a;color:#ddd;border:1px solid #444;"
            "border-radius:3px;padding:1px 4px;font-size:10px;}"
            "QComboBox::drop-down{border:none;}"
        )
        self._method_all.setFixedWidth(64)
        self._method_all.currentTextChanged.connect(self._on_method_all_changed)
        all_row.addWidget(self._cb_all)
        all_row.addStretch()
        all_row.addWidget(QLabel("Method:"))
        all_row.addWidget(self._method_all)
        chl.addLayout(all_row)

        # 分隔线
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color:#333;")
        chl.addWidget(sep)

        self._channel_list = QtWidgets.QListWidget()
        self._channel_list.setStyleSheet(
            "QListWidget{background:#111;border:1px solid #333;border-radius:3px;}"
            "QListWidget::item{padding:1px;}"
            "QListWidget::item:selected{background:#1f2f3f;}"
        )
        self._channel_list.currentRowChanged.connect(self._on_channel_row_changed)
        chl.addWidget(self._channel_list, stretch=1)
        cll.addWidget(ch_box, stretch=2)

        # ── Method Parameters ─────────────────────────────────────────
        method_box = QGroupBox("Method Parameters")
        method_box.setStyleSheet(self._box_style("#e5c07b"))
        ml = QVBoxLayout(method_box)
        self._tophat_value = QLabel()
        self._tophat_slider = QSlider(Qt.Horizontal)
        self._tophat_slider.setRange(*TOPHAT_RADIUS_RANGE)
        self._tophat_slider.setValue(TOPHAT_RADIUS_DEFAULT)
        self._tophat_slider.valueChanged.connect(self._on_slider_changed)
        self._cucim_value = QLabel()
        self._cucim_slider = QSlider(Qt.Horizontal)
        self._cucim_slider.setRange(*CUCIM_SIGMA_RANGE)
        self._cucim_slider.setValue(CUCIM_SIGMA_DEFAULT)
        self._cucim_slider.valueChanged.connect(self._on_slider_changed)
        for lbl in (self._tophat_value, self._cucim_value):
            lbl.setStyleSheet("color:#ddd;font-size:11px;")
        ml.addWidget(self._tophat_value)
        ml.addWidget(self._tophat_slider)
        ml.addWidget(self._hint_label("~0.5–1.5× cell diameter"))
        ml.addSpacing(4)
        ml.addWidget(self._cucim_value)
        ml.addWidget(self._cucim_slider)
        ml.addWidget(self._hint_label("Large sigma → broad background"))
        self._cucim_warn = QLabel(
            "cucim not available — CPU fallback."
            + (f" ({CUCIM_IMPORT_ERROR})" if CUCIM_IMPORT_ERROR else "")
        )
        self._cucim_warn.setVisible(not CUCIM_AVAILABLE)
        self._cucim_warn.setWordWrap(True)
        self._cucim_warn.setStyleSheet(
            "color:#ffb86c;font-size:10px;background:#2a1f14;"
            "border:1px solid #704b1f;border-radius:3px;padding:4px;"
        )
        ml.addWidget(self._cucim_warn)
        cll.addWidget(method_box)

        # ── Process 按钮 + 进度 ───────────────────────────────────────
        proc_box = QGroupBox("Process")
        proc_box.setStyleSheet(self._box_style("#98c379"))
        prl = QVBoxLayout(proc_box)
        prl.setSpacing(4)

        proc_btn_row = QHBoxLayout()
        self._btn_process = QPushButton("▶ Process")
        self._btn_process.setStyleSheet(
            "QPushButton{background:#1a5c2a;color:#6bffa0;border:1px solid #4a9;"
            "border-radius:4px;padding:6px 14px;font-size:12px;font-weight:bold;}"
            "QPushButton:hover{background:#2a7c3a;}"
            "QPushButton:disabled{background:#222;color:#555;border-color:#333;}"
        )
        self._btn_process.clicked.connect(self._on_process_clicked)

        self._btn_stop_process = QPushButton("⏹ Stop")
        self._btn_stop_process.setEnabled(False)
        self._btn_stop_process.setStyleSheet(
            "QPushButton{background:#722;color:white;border-radius:4px;padding:6px 10px;}"
            "QPushButton:hover{background:#944;}"
            "QPushButton:disabled{background:#333;color:#555;}"
        )
        self._btn_stop_process.clicked.connect(self._on_stop_process)

        proc_btn_row.addWidget(self._btn_process, stretch=1)
        proc_btn_row.addWidget(self._btn_stop_process)
        prl.addLayout(proc_btn_row)

        self._proc_pbar = QProgressBar()
        self._proc_pbar.setRange(0, 100)
        self._proc_pbar.setValue(0)
        self._proc_pbar.setVisible(False)
        self._proc_pbar.setFixedHeight(14)
        self._proc_pbar.setStyleSheet(
            "QProgressBar{border:1px solid #4a9;border-radius:3px;background:#111;}"
            "QProgressBar::chunk{background:#4a9;border-radius:2px;}"
        )
        prl.addWidget(self._proc_pbar)

        self._proc_status = QLabel("Select channels and click Process.")
        self._proc_status.setWordWrap(True)
        self._proc_status.setStyleSheet("color:#aaa;font-size:10px;")
        prl.addWidget(self._proc_status)

        cll.addWidget(proc_box)

        # ── Preview Patch 选择 ────────────────────────────────────────
        patch_box = QGroupBox("Preview Patch")
        patch_box.setStyleSheet(self._box_style("#98c379"))
        pl2 = QVBoxLayout(patch_box)
        self._patch_buttons_row = QHBoxLayout()
        self._patch_buttons_row.setSpacing(4)
        pl2.addLayout(self._patch_buttons_row)
        self._patch_info = QLabel("Draw a patch in Section B first.")
        self._patch_info.setWordWrap(True)
        self._patch_info.setStyleSheet("color:#888;font-size:10px;")
        pl2.addWidget(self._patch_info)
        cll.addWidget(patch_box)

        c_split.addWidget(c_left)

        # C-右：三联预览 + metrics + 决策
        c_right = QWidget()
        crl = QVBoxLayout(c_right)
        crl.setContentsMargins(0, 0, 0, 0)
        crl.setSpacing(4)

        prev_box = QGroupBox("Patch Preview  —  Original | TopHat | cucim")
        prev_box.setStyleSheet(self._box_style("#c678dd"))
        pvl = QVBoxLayout(prev_box)

        # ── 控制行（Display toggle + 对比度 + 颜色 + zoom复位）─────────
        _tg = (
            "QPushButton{{color:{c};border:1px solid {c};border-radius:3px;"
            "padding:2px 6px;font-size:10px;background:#1a1a1a;}}"
            "QPushButton:checked{{background:{c};color:#111;font-weight:bold;}}"
        )
        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(4)

        # Nucleus / Marker 颜色选择色块
        self._nuc_color = (0.0, 0.5, 1.0)      # 默认蓝色
        self._marker_color = (0.0, 1.0, 0.3)   # 默认绿色

        nuc_lbl = QLabel("Nuc:")
        nuc_lbl.setStyleSheet("color:#aaa;font-size:10px;")
        self._nuc_color_btn = QPushButton()
        self._nuc_color_btn.setFixedSize(18, 18)
        self._nuc_color_btn.setToolTip("Click to change nucleus (DAPI) display color")
        self._nuc_color_btn.setStyleSheet(
            "QPushButton{background:#0080ff;border:1px solid #555;border-radius:2px;}"
            "QPushButton:hover{border:1px solid #aaa;}"
        )
        self._nuc_color_btn.clicked.connect(self._pick_nucleus_color)

        self._btn_show_nucleus = QPushButton("Nucleus")
        self._btn_show_nucleus.setCheckable(True)
        self._btn_show_nucleus.setChecked(True)
        self._btn_show_nucleus.setToolTip("Show/hide nucleus channel")
        self._btn_show_nucleus.setStyleSheet(_tg.format(c="#56b6c2"))

        mk_lbl = QLabel("Marker:")
        mk_lbl.setStyleSheet("color:#aaa;font-size:10px;")
        self._marker_color_btn = QPushButton()
        self._marker_color_btn.setFixedSize(18, 18)
        self._marker_color_btn.setToolTip("Click to change marker channel display color")
        self._marker_color_btn.setStyleSheet(
            "QPushButton{background:#00ff4d;border:1px solid #555;border-radius:2px;}"
            "QPushButton:hover{border:1px solid #aaa;}"
        )
        self._marker_color_btn.clicked.connect(self._pick_marker_color)

        self._btn_show_marker = QPushButton("Marker")
        self._btn_show_marker.setCheckable(True)
        self._btn_show_marker.setChecked(True)
        self._btn_show_marker.setToolTip("Show/hide marker channel")
        self._btn_show_marker.setStyleSheet(_tg.format(c="#98c379"))

        self._btn_lock_zoom = QPushButton("🔗 Lock")
        self._btn_lock_zoom.setCheckable(True)
        self._btn_lock_zoom.setChecked(True)
        self._btn_lock_zoom.setToolTip("Lock zoom/pan across all three panels")
        self._btn_lock_zoom.setStyleSheet(_tg.format(c="#e5c07b"))

        btn_reset_all = QPushButton("⊡ Reset All")
        btn_reset_all.setToolTip("Reset all three panels to full view")
        btn_reset_all.setStyleSheet(
            "QPushButton{color:#aaa;border:1px solid #555;border-radius:3px;"
            "padding:2px 6px;font-size:10px;background:#1a1a1a;}"
            "QPushButton:hover{background:#333;color:#fff;}"
        )
        btn_reset_all.clicked.connect(self._reset_all_views)

        ctrl_row.addWidget(nuc_lbl)
        ctrl_row.addWidget(self._nuc_color_btn)
        ctrl_row.addWidget(self._btn_show_nucleus)
        ctrl_row.addSpacing(6)
        ctrl_row.addWidget(mk_lbl)
        ctrl_row.addWidget(self._marker_color_btn)
        ctrl_row.addWidget(self._btn_show_marker)
        ctrl_row.addSpacing(10)
        ctrl_row.addWidget(self._btn_lock_zoom)
        ctrl_row.addSpacing(4)
        ctrl_row.addWidget(btn_reset_all)
        ctrl_row.addStretch()

        self._btn_show_nucleus.toggled.connect(lambda _: self._refresh_preview_display(keep_zoom=True))
        self._btn_show_marker.toggled.connect(lambda _: self._refresh_preview_display(keep_zoom=True))
        pvl.addLayout(ctrl_row)

        # ── 对比度滑块行（Marker 和 Nucleus 分开）──────────────────────
        def _make_contrast_row(label, attr_slider, attr_lbl, color):
            row = QHBoxLayout()
            row.setSpacing(4)
            lbl = QLabel(label)
            lbl.setStyleSheet(f"color:{color};font-size:10px;min-width:48px;")
            slider = QSlider(Qt.Horizontal)
            slider.setRange(1, 100)
            slider.setValue(100)
            slider.setToolTip(
                f"Adjust {label} display upper level. Lower = brighter.\n"
                "Does not affect actual correction data.")
            slider.valueChanged.connect(lambda v: self._on_contrast_changed())
            val_lbl = QLabel("100%")
            val_lbl.setStyleSheet("color:#ddd;font-size:10px;min-width:34px;")
            slider.valueChanged.connect(lambda v, vl=val_lbl: vl.setText(f"{v}%"))
            btn_r = QPushButton("↺")
            btn_r.setFixedSize(18, 18)
            btn_r.setStyleSheet(
                "QPushButton{color:#aaa;border:1px solid #555;border-radius:3px;"
                "font-size:10px;background:#1a1a1a;}"
                "QPushButton:hover{background:#333;}"
            )
            btn_r.clicked.connect(lambda: slider.setValue(100))
            row.addWidget(lbl)
            row.addWidget(slider, stretch=1)
            row.addWidget(val_lbl)
            row.addWidget(btn_r)
            setattr(self, attr_slider, slider)
            setattr(self, attr_lbl, val_lbl)
            return row

        pvl.addLayout(_make_contrast_row(
            "Marker:", "_marker_contrast_slider", "_marker_contrast_lbl", "#98c379"))
        pvl.addLayout(_make_contrast_row(
            "Nucleus:", "_nuc_contrast_slider", "_nuc_contrast_lbl", "#56b6c2"))

        # ── 三联图（同一GraphicsLayoutWidget，保证同步repaint）────────
        self._preview_vbs  = []
        self._preview_imgs = []
        self._preview_gv = pg.GraphicsLayoutWidget()   # 单一widget
        self._preview_gv.setBackground("#111")
        TITLES = ("Original", "TopHat", "cucim")
        for i, title_text in enumerate(TITLES):
            lbl = self._preview_gv.addLabel(title_text, row=0, col=i)
            lbl.setText(f'<span style="color:#ddd;font-size:11px;font-weight:bold;">{title_text}</span>')
            vb = self._preview_gv.addViewBox(row=1, col=i)
            vb.setAspectLocked(True)
            vb.invertY(True)
            vb.setMenuEnabled(False)
            item = pg.ImageItem()
            vb.addItem(item)
            self._preview_vbs.append(vb)
            self._preview_imgs.append(item)
            def _on_manual_range(vb_ref, src=i):
                if self._btn_lock_zoom.isChecked() and not self._zoom_lock_active:
                    self._sync_zoom(src)
            vb.sigRangeChangedManually.connect(_on_manual_range)

        pvl.addWidget(self._preview_gv, stretch=1)

        # 复位按钮行（每图一个 + 状态栏）
        reset_row = QHBoxLayout()
        for i, lbl_text in enumerate(TITLES):
            btn_r = QPushButton(f"↺ {lbl_text}")
            btn_r.setFixedHeight(20)
            btn_r.setStyleSheet(
                "QPushButton{color:#888;border:1px solid #444;border-radius:3px;"
                "font-size:10px;background:#1a1a1a;}"
                "QPushButton:hover{color:#ddd;border-color:#888;}"
            )
            btn_r.clicked.connect(lambda _, idx=i: self._reset_single_view(idx))
            reset_row.addWidget(btn_r, stretch=1)
        pvl.addLayout(reset_row)

        # 别名兼容
        self._orig_vb,  self._orig_img  = self._preview_vbs[0], self._preview_imgs[0]
        self._top_vb,   self._top_img   = self._preview_vbs[1], self._preview_imgs[1]
        self._cu_vb,    self._cu_img    = self._preview_vbs[2], self._preview_imgs[2]

        self._preview_status = QLabel(
            "Select a channel and patch ROI to preview background correction."
        )
        self._preview_status.setAlignment(Qt.AlignCenter)
        self._preview_status.setWordWrap(True)
        self._preview_status.setStyleSheet("color:#aaa;font-size:10px;")
        pvl.addWidget(self._preview_status)
        crl.addWidget(prev_box, stretch=3)

        # Metrics + Decision 横排（都在右侧底部）
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(6)

        metrics_box = QGroupBox("Quantitative Metrics")
        metrics_box.setStyleSheet(self._box_style("#56b6c2"))
        metl = QVBoxLayout(metrics_box)
        self._metrics_original = QLabel("Original  → SNR: —  BG-CV: —")
        self._metrics_tophat   = QLabel("TopHat    → SNR: —  BG-CV: —")
        self._metrics_cucim    = QLabel("cucim     → SNR: —  BG-CV: —")
        for lbl in (self._metrics_original, self._metrics_tophat, self._metrics_cucim):
            lbl.setStyleSheet(
                "color:#ddd;font-size:11px;background:#111;padding:3px;border-radius:3px;"
            )
            metl.addWidget(lbl)
        bottom_row.addWidget(metrics_box, stretch=1)

        decision_box = QGroupBox("Per-Channel Decision")
        decision_box.setStyleSheet(self._box_style("#e06c75"))
        dl = QVBoxLayout(decision_box)
        self._decision_group = QButtonGroup(self)
        self._dec_top  = QRadioButton("Use TopHat result")
        self._dec_cu   = QRadioButton("Use cucim result")
        self._dec_orig = QRadioButton("Keep original")
        self._dec_orig.setChecked(True)
        rb_row = QHBoxLayout()
        for rb in (self._dec_top, self._dec_cu, self._dec_orig):
            self._decision_group.addButton(rb)
            rb.setStyleSheet("font-size:11px;")
            rb_row.addWidget(rb)
        dl.addLayout(rb_row)
        self._apply_btn = QPushButton("Apply to this channel")
        self._apply_btn.setStyleSheet(
            "QPushButton{background:#255;color:white;border-radius:4px;"
            "padding:5px 12px;font-weight:bold;}"
            "QPushButton:hover{background:#377;}"
            "QPushButton:disabled{background:#333;color:#555;}"
        )
        self._apply_btn.clicked.connect(self._apply_current_channel_decision)
        dl.addWidget(self._apply_btn)
        self._decision_status = QLabel("No decision saved yet.")
        self._decision_status.setWordWrap(True)
        self._decision_status.setStyleSheet("color:#aaa;font-size:10px;")
        dl.addWidget(self._decision_status)
        bottom_row.addWidget(decision_box, stretch=1)

        crl.addLayout(bottom_row)
        c_split.addWidget(c_right)

        # C内部 左:右 = 1:2
        c_split.setStretchFactor(0, 1)
        c_split.setStretchFactor(1, 2)

        # Run BG correction 行 + Save & Continue（Section C 底部）
        run_row = QHBoxLayout()
        self._bg_start_status = QLabel("Configure channels above, then click ▶ Run.")
        self._bg_start_status.setStyleSheet("color:#aaa;font-size:11px;")
        run_row.addWidget(self._bg_start_status, stretch=1)
        self._bg_pbar = QProgressBar()
        self._bg_pbar.setRange(0, 100)
        self._bg_pbar.setValue(0)
        self._bg_pbar.setVisible(False)
        self._bg_pbar.setFixedSize(160, 14)
        self._bg_pbar.setStyleSheet(
            "QProgressBar{border:1px solid #4a9;border-radius:3px;background:#111;}"
            "QProgressBar::chunk{background:#4a9;border-radius:2px;}"
        )
        run_row.addWidget(self._bg_pbar)
        self._btn_start_bg = QPushButton("▶ Run BG correction on all assigned channels")
        self._btn_start_bg.setStyleSheet(
            "QPushButton{background:#1a5c2a;color:#6bffa0;border:1px solid #4a9;"
            "border-radius:4px;padding:6px 14px;font-size:12px;font-weight:bold;}"
            "QPushButton:hover{background:#2a7c3a;}"
            "QPushButton:disabled{background:#222;color:#555;border-color:#333;}"
        )
        self._btn_start_bg.clicked.connect(self._on_start_bg_correction)
        run_row.addWidget(self._btn_start_bg)
        cl.addLayout(run_row)

        main_split.addWidget(sec_c)

        # 主分栏比例：B=25%, C=75%
        main_split.setStretchFactor(0, 1)
        main_split.setStretchFactor(1, 3)

        # ══ 底部导航 ═══════════════════════════════════════════════════
        nav = QHBoxLayout()
        nav.addStretch()
        self._btn_continue = QPushButton("Save & Continue → Step 1")
        self._btn_continue.setStyleSheet(
            "QPushButton{background:#2a5;color:white;border-radius:4px;"
            "padding:8px 22px;font-size:13px;font-weight:bold;}"
            "QPushButton:hover{background:#3b6;}"
        )
        self._btn_continue.clicked.connect(self._save_and_continue)
        nav.addWidget(self._btn_continue)
        outer.addLayout(nav)

        self._refresh_slider_labels()

    def showEvent(self, event):
        super().showEvent(event)
        self._fix_split_ratio()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fix_split_ratio()

    def _fix_split_ratio(self):
        """强制维持 B:C = 1:3 的分栏比例，不受内容影响。"""
        if not hasattr(self, '_main_split'):
            return
        total = self._main_split.width()
        if total < 10:
            return
        b_w = max(80, total // 4)
        c_w = total - b_w - self._main_split.handleWidth()
        self._main_split.setSizes([b_w, c_w])

    @staticmethod
    def _box_style(color):
        return (
            f"QGroupBox{{border:1px solid {color};border-radius:5px;margin-top:4px;"
            f"font-weight:bold;color:{color};font-size:11px;}}"
        )

    @staticmethod
    def _hint_label(text):
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color:#888;font-size:10px;")
        return lbl

    def _file_row(self, label, edit, slot, is_dir=False):
        row = QHBoxLayout()
        row.addWidget(QLabel(label))
        row.addWidget(edit, stretch=1)
        btn = QPushButton("Browse")
        btn.setFixedWidth(70)
        btn.setStyleSheet(
            "QPushButton{font-size:10px;color:#8cf;border:1px solid #8cf;border-radius:3px;padding:2px 6px;}"
            "QPushButton:hover{background:#1a2a4a;}"
        )
        if is_dir:
            btn.clicked.connect(lambda: slot())
        else:
            btn.clicked.connect(slot)
        row.addWidget(btn)
        return row

    @staticmethod
    def _parse_panel_csv(path):
        import csv
        groups = {}
        nucleus_rows = []
        dapi_fallback = None

        def _norm_key(v):
            return (v or "").strip().lower()

        def _is_dapi(*values):
            return any(_norm_key(v) == "dapi" for v in values)

        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return {}, None
            for row in reader:
                row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
                ch_name = (
                    row.get("channel_name")
                    or row.get("channel")
                    or row.get("name")
                    or row.get("marker")
                    or ""
                ).strip()
                marker = (row.get("marker") or "").strip()
                role = _norm_key(row.get("role"))
                group = (
                    row.get("group")
                    or row.get("category")
                    or row.get("class")
                    or ""
                ).strip()
                if not ch_name:
                    continue
                group_norm = _norm_key(group)
                is_nucleus = role == "nucleus" or group_norm in ("nucleus", "dapi", "nuclear")
                if is_nucleus:
                    nucleus_rows.append((ch_name, marker))
                if dapi_fallback is None and _is_dapi(ch_name, marker):
                    dapi_fallback = ch_name
                if group and not is_nucleus:
                    groups.setdefault(group, {})[ch_name] = 1.0

        selected_nuc = None
        dapi_nucleus = [ch for ch, marker in nucleus_rows if _is_dapi(ch, marker)]
        if dapi_nucleus:
            selected_nuc = dapi_nucleus[0]
        elif nucleus_rows:
            selected_nuc = nucleus_rows[0][0]
        elif dapi_fallback:
            selected_nuc = dapi_fallback
        return groups, selected_nuc

    def _browse_ome(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select OME-TIFF",
            os.path.dirname(self._ome_path_edit.text()) or os.getcwd(),
            "OME-TIFF (*.tif *.tiff)",
        )
        if path:
            self._ome_path_edit.setText(path)

    def _browse_panel_csv(self):
        csv_dir = os.path.dirname(self._panel_csv_edit.text()) if self._panel_csv_edit.text().strip() else os.path.dirname(self._ome_path_edit.text())
        path, _ = QFileDialog.getOpenFileName(self, "Select Panel CSV", csv_dir or os.getcwd(), "CSV (*.csv)")
        if path:
            self._panel_csv_edit.setText(path)

    def _browse_out_dir(self):
        path = QFileDialog.getExistingDirectory(self, "Select Output Directory", self._out_path_edit.text() or os.getcwd())
        if path:
            self._out_path_edit.setText(path)

    def _reload_from_paths(self):
        global OME_TIFF_FILE, OUTPUT_DIR

        ome = self._ome_path_edit.text().strip()
        outd = self._out_path_edit.text().strip()
        panel_csv = self._panel_csv_edit.text().strip()

        if not ome or not os.path.exists(ome):
            QMessageBox.warning(self, "File not found", f"OME-TIFF not found:\n{ome}")
            return

        OME_TIFF_FILE = ome
        OUTPUT_DIR = outd if outd else os.path.dirname(ome)
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        self._out_path_edit.setText(OUTPUT_DIR)

        try:
            self.loader = OMETIFFLoader(OME_TIFF_FILE, CHANNEL_NAME_MAP)
        except Exception as e:
            QMessageBox.critical(self, "Load error", str(e))
            return

        self.ome_path = OME_TIFF_FILE
        self.output_dir = OUTPUT_DIR
        self.panel_csv_path = panel_csv
        self.panel_groups = {}
        self.nucleus_channel = NUCLEUS_CONFIG["channel"]

        if panel_csv and os.path.exists(panel_csv):
            try:
                self.panel_groups, parsed_nuc = self._parse_panel_csv(panel_csv)
                if parsed_nuc:
                    self.nucleus_channel = parsed_nuc
            except Exception as e:
                QMessageBox.warning(self, "Panel CSV error", f"Failed to parse panel CSV:\n{e}")
        else:
            template_path = os.path.join(OUTPUT_DIR, "panel.csv")
            try:
                import csv as _csv
                with open(template_path, "w", newline="", encoding="utf-8") as f:
                    w = _csv.writer(f)
                    w.writerow(["channel_name", "marker", "role", "group"])
                    for ch in self.loader.channel_names():
                        w.writerow([ch, ch, "", ""])
                self._panel_csv_edit.setText(template_path)
                self.panel_csv_path = template_path
            except Exception:
                pass

        if self.nucleus_channel not in self.loader.ch_map:
            if "DAPI" in self.loader.ch_map:
                self.nucleus_channel = "DAPI"
            else:
                self.nucleus_channel = next(iter(self.loader.ch_map.keys()), NUCLEUS_CONFIG["channel"])

        self.loader.set_correction_config(
            _load_correction_config(os.path.join(OUTPUT_DIR, "correction_config.json"))
        )
        self.loader.set_corrected_zarr_store(None, {})

        self._stop_bg_workers()
        self.current_patch_idx = 0
        self.current_channel = None
        self._preview_req_id = 0
        self._channel_decisions.clear()
        self._bg_pbar.setVisible(False)
        self._bg_pbar.setValue(0)
        self._bg_start_status.setText("Configure channels above, then click ▶ Run.")
        self._patch_warning.setVisible(False)

        self.overview.loader = self.loader
        self.overview.nuc_ch = self.nucleus_channel
        self.overview.full_h = self.loader.shape[0]
        self.overview.full_w = self.loader.shape[1]
        for arts in self.overview._roi_artists:
            for item in arts:
                self.overview.vb.removeItem(item)
        for rect, lbl in self.overview._patch_artists:
            self.overview.vb.removeItem(rect)
            self.overview.vb.removeItem(lbl)
        self.overview._rois.clear()
        self.overview._patches.clear()
        self.overview._roi_artists.clear()
        self.overview._patch_artists.clear()
        self.overview.img_item.clear()
        self.overview._update_info()
        self.overview._load_overview()
        self._on_rois_changed([])
        self._on_patches_changed([])

        self._load_existing_config()
        self._rebuild_channel_list()
        self._rebuild_patch_buttons()
        self._load_status.setText(
            f"Loaded: {self.loader.shape[0]:,}x{self.loader.shape[1]:,} px  |  {len(self.loader.ch_map)} channels"
        )

    def _wrap_overview_patch_limit(self):
        original = self.overview._add_patch

        def wrapped(fy0, fy1, fx0, fx1, rmin, rmax, cmin, cmax, roi_idx):
            if self.overview._patches_in_roi(roi_idx) >= self.overview._max_patches_for_roi(roi_idx):
                self._patch_warning.setText("Max 4 patches per ROI")
                self._patch_warning.setVisible(True)
                return
            self._patch_warning.setVisible(False)
            return original(fy0, fy1, fx0, fx1, rmin, rmax, cmin, cmax, roi_idx)

        self.overview._add_patch = wrapped

    def _reindex_roi_patch_links(self):
        for roi in self.overview._rois:
            roi["patch_indices"] = []
        for idx, patch in enumerate(self.overview._patches):
            roi_idx = patch.get("roi_idx")
            if roi_idx is not None and 0 <= roi_idx < len(self.overview._rois):
                self.overview._rois[roi_idx]["patch_indices"].append(idx)

    def _on_rois_changed(self, rois):
        self.rois = list(rois or [])
        sel = self._roi_selected_idx
        self._roi_list.clear()
        for idx, roi in enumerate(self.rois):
            n_p = len(roi.get("patch_indices", []))
            self._roi_list.addItem(f'{roi["name"]} [{n_p}/4]')
        if self.rois:
            self._roi_selected_idx = min(max(sel, 0), len(self.rois) - 1)
            self._roi_list.setCurrentRow(self._roi_selected_idx)
        else:
            self._roi_selected_idx = -1
        self._rebuild_patch_list()

    def _on_patches_changed(self, patches):
        self.patches = list(patches or [])
        self._reindex_roi_patch_links()
        self.rois = list(self.overview.get_rois())
        self._on_rois_changed(self.rois)
        self._rebuild_patch_buttons()
        if self.patches:
            self.current_patch_idx = min(self.current_patch_idx, len(self.patches) - 1)
            self._update_patch_info()
            # 新架构：patches变化时只更新UI，不自动触发计算
            # 如果有缓存结果且有选中通道，刷新显示
            if self.current_channel and self._has_any_cache(self.current_channel):
                self._show_channel_from_cache(self.current_channel)
        else:
            self.current_patch_idx = 0
            self._patch_info.setText("No patch ROI available yet. Draw a patch in Section B first.")
            self._preview_status.setText("Select a channel and patch ROI to preview background correction.")

    def _rebuild_patch_list(self):
        sel = self._patch_selected_idx
        self._patch_list.clear()
        for idx, patch in enumerate(self.overview._patches):
            y0, y1, x0, x1 = patch["coords"]
            roi_idx = patch.get("roi_idx")
            roi_name = self.overview._rois[roi_idx]["name"] if roi_idx is not None and roi_idx < len(self.overview._rois) else "No ROI"
            self._patch_list.addItem(f"P{idx+1}  {roi_name}  [{y1-y0}x{x1-x0}px]")
        if self.overview._patches:
            self._patch_selected_idx = min(max(sel, 0), len(self.overview._patches) - 1)
            self._patch_list.setCurrentRow(self._patch_selected_idx)
        else:
            self._patch_selected_idx = -1

    def _on_roi_selection_changed(self):
        """ROI列表选择变化——记录所有选中行的索引"""
        rows = [self._roi_list.row(i)
                for i in self._roi_list.selectedItems()]
        self._roi_selected_idx = rows[-1] if rows else -1
        self._roi_selected_indices = rows

    def _on_patch_selection_changed(self):
        """Patch列表选择变化——记录所有选中行，跳转预览到最后一个"""
        rows = [self._patch_list.row(i)
                for i in self._patch_list.selectedItems()]
        self._patch_selected_idx = rows[-1] if rows else -1
        self._patch_selected_indices = rows
        if rows and rows[-1] < len(self.patches):
            self.current_patch_idx = rows[-1]
            self._sync_patch_buttons()
            self._update_patch_info()
            # 有缓存则立刻显示新patch的结果
            if self.current_channel and self.current_channel != self.nucleus_channel:
                if self._has_any_cache(self.current_channel):
                    self._show_channel_from_cache(self.current_channel)

    def _on_roi_selected(self, row):
        """兼容旧代码的单选回调"""
        self._roi_selected_idx = row
        self._roi_selected_indices = [row] if row >= 0 else []

    def _on_patch_selected(self, row):
        """兼容旧代码的单选回调"""
        self._patch_selected_idx = row
        self._patch_selected_indices = [row] if row >= 0 else []
        if 0 <= row < len(self.patches):
            self.current_patch_idx = row
            self._sync_patch_buttons()
            self._update_patch_info()
            if self.current_channel and self.current_channel != self.nucleus_channel:
                if self._has_any_cache(self.current_channel):
                    self._show_channel_from_cache(self.current_channel)

    def _set_draw_mode(self, mode):
        """切换绘制模式，同步按钮状态"""
        self._btn_mode_roi.setChecked(mode == "roi")
        self._btn_mode_patch.setChecked(mode == "patch")
        if mode == "roi":
            # 自动生成下一个不重名的默认ROI名，写入输入框，不弹对话框
            existing = {r["name"] for r in self.overview._rois}
            n = len(self.overview._rois) + 1
            next_name = f"ROI_{n}"
            while next_name in existing:
                n += 1
                next_name = f"ROI_{n}"
            self.overview._roi_name_edit.setText(next_name)
            self.overview._set_mode("roi")
            self.overview.status.setText(
                "Draw ROI vertices on the overview, then press Enter or right-click to close.")
        else:
            self.overview._set_mode("patch")
            self.overview.status.setText("Drag to draw a patch rectangle inside a ROI.")

    def _delete_selected_item(self):
        """优先删除选中patch，其次删除选中ROI（兼容旧工具栏调用）"""
        p_idxs = getattr(self, '_patch_selected_indices', [])
        r_idxs = getattr(self, '_roi_selected_indices', [])
        if p_idxs:
            self._delete_selected_patches()
        elif r_idxs:
            self._delete_selected_rois()
        else:
            QMessageBox.information(
                self, "Nothing selected",
                "Select a ROI or Patch in the lists first.")

    def _delete_selected_rois(self):
        """批量删除所有选中的ROI（及其patch），从大到小索引顺序删除避免偏移"""
        idxs = sorted(
            getattr(self, '_roi_selected_indices', []),
            reverse=True)
        if not idxs:
            return
        for idx in idxs:
            if idx < 0 or idx >= len(self.overview._rois):
                continue
            # 移除canvas上的ROI图形
            for a in self.overview._roi_artists[idx]:
                self.overview.vb.removeItem(a)
            del self.overview._roi_artists[idx]
            del self.overview._rois[idx]
            # 删除属于该ROI的patch，重映射其余patch的roi_idx
            new_patches = []
            for p in self.overview._patches:
                ri = p.get("roi_idx")
                if ri == idx:
                    continue
                if ri is not None and ri > idx:
                    p = dict(p)
                    p["roi_idx"] = ri - 1
                new_patches.append(p)
            self.overview._patches = new_patches
            # 后续循环里idx已减小，不需要额外偏移，因为我们从大到小删
        self.overview._rebuild_patch_artists()
        self._reindex_roi_patch_links()
        self.overview._update_info()
        self.overview.rois_changed.emit(list(self.overview._rois))
        self.overview.patches_changed.emit(self.overview._patch_coords())
        self._patch_warning.setVisible(False)

    def _delete_selected_patches(self):
        """批量删除所有选中的patch，从大到小索引顺序删除避免偏移"""
        idxs = sorted(
            getattr(self, '_patch_selected_indices', []),
            reverse=True)
        if not idxs:
            return
        for idx in idxs:
            if 0 <= idx < len(self.overview._patches):
                del self.overview._patches[idx]
        self.overview._rebuild_patch_artists()
        self._reindex_roi_patch_links()
        self.overview._update_info()
        self.overview.patches_changed.emit(self.overview._patch_coords())
        self.overview.rois_changed.emit(list(self.overview._rois))

    def _begin_add_roi(self):
        existing = {r["name"] for r in self.overview._rois}
        n = len(self.overview._rois) + 1
        next_name = f"ROI_{n}"
        while next_name in existing:
            n += 1
            next_name = f"ROI_{n}"
        self.overview._roi_name_edit.setText(next_name)
        self.overview._set_mode("roi")
        self.overview.status.setText(
            "Draw ROI vertices on the overview, then press Enter or right-click to close.")

    def _delete_selected_roi(self):
        idx = self._roi_selected_idx
        if idx < 0 or idx >= len(self.overview._rois):
            return
        dead_patch_indices = set(self.overview._rois[idx].get("patch_indices", []))
        for arts in self.overview._roi_artists[idx:idx+1]:
            for a in arts:
                self.overview.vb.removeItem(a)
        del self.overview._roi_artists[idx]
        del self.overview._rois[idx]

        new_patches = []
        for p in self.overview._patches:
            ri = p.get("roi_idx")
            if ri == idx:
                continue
            if ri is not None and ri > idx:
                p = dict(p)
                p["roi_idx"] = ri - 1
            new_patches.append(p)
        self.overview._patches = new_patches
        self.overview._rebuild_patch_artists()
        self._reindex_roi_patch_links()
        self.overview._update_info()
        self.overview.rois_changed.emit(list(self.overview._rois))
        self.overview.patches_changed.emit(self.overview._patch_coords())
        if dead_patch_indices:
            self._patch_warning.setVisible(False)

    def _rename_selected_roi(self):
        idx = self._roi_selected_idx
        if idx < 0 or idx >= len(self.overview._rois):
            QMessageBox.information(self, "No ROI selected",
                                    "Select a ROI in the list first.")
            return
        roi = self.overview._rois[idx]
        name, ok = QInputDialog.getText(self, "Rename ROI", "ROI name:", text=roi["name"])
        if not ok or not name.strip():
            return
        new_name = name.strip()
        # 重名检测（排除自身）
        existing = {r["name"] for i, r in enumerate(self.overview._rois) if i != idx}
        if new_name in existing:
            QMessageBox.warning(self, "Duplicate name",
                                f'ROI name "{new_name}" already exists.\nPlease choose a different name.')
            return
        roi["name"] = new_name
        label_item = self.overview._roi_artists[idx][1]
        try:
            label_item.setText(new_name)
        except Exception:
            pass
        self.overview._update_info()
        self.overview.rois_changed.emit(list(self.overview._rois))

    def _delete_selected_patch(self):
        idx = self._patch_selected_idx
        if idx < 0 or idx >= len(self.overview._patches):
            return
        del self.overview._patches[idx]
        self.overview._rebuild_patch_artists()
        self._reindex_roi_patch_links()
        self.overview._update_info()
        self.overview.patches_changed.emit(self.overview._patch_coords())
        self.overview.rois_changed.emit(list(self.overview._rois))

    def _load_existing_config(self):
        path = os.path.join(self.output_dir, "correction_config.json")
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

    def _rebuild_channel_list(self):
        current = self.current_channel
        self._channel_rows.clear()
        self._channel_order = []
        self._channel_list.clear()
        if not self.loader:
            return

        for ch in self.loader.channel_names():
            item = QtWidgets.QListWidgetItem(self._channel_list)
            item.setSizeHint(QtCore.QSize(300, 36))
            row = QWidget()
            lay = QHBoxLayout(row)
            lay.setContentsMargins(4, 2, 4, 2)
            lay.setSpacing(4)

            is_nucleus = (ch == self.nucleus_channel)

            # 勾选框
            cb = QtWidgets.QCheckBox()
            cb.setChecked(False)
            cb.setEnabled(not is_nucleus)
            cb.stateChanged.connect(lambda state, name=ch: self._on_channel_checkbox_toggled(name, state))
            lay.addWidget(cb)

            # 通道名
            label = QLabel(ch if not is_nucleus else f"{ch} ★")
            label.setStyleSheet("color:#ddd;font-size:11px;")
            label.setMinimumWidth(60)
            lay.addWidget(label, stretch=1)


            # 方法下拉（nucleus锁定）
            method_cb = QtWidgets.QComboBox()
            method_cb.addItems(["TopHat", "cucim", "Both"])
            method_cb.setEnabled(not is_nucleus)
            method_cb.setFixedWidth(60)
            method_cb.setStyleSheet(
                "QComboBox{background:#1a1a1a;color:#ddd;border:1px solid #444;"
                "border-radius:3px;padding:1px 2px;font-size:10px;}"
                "QComboBox::drop-down{border:none;}"
                "QComboBox:disabled{color:#555;}"
            )
            saved = self._channel_methods.get(ch, "both")
            idx_map = {"tophat": 0, "cucim": 1, "both": 2}
            method_cb.setCurrentIndex(idx_map.get(saved, 2))  # default Both
            method_cb.currentTextChanged.connect(
                lambda txt, name=ch: self._on_channel_method_changed(name, txt))
            lay.addWidget(method_cb)

            # 状态图标（空/转圈/绿勾）
            status_lbl = QLabel("—")
            status_lbl.setAlignment(Qt.AlignCenter)
            status_lbl.setFixedWidth(20)
            status_lbl.setStyleSheet("color:#666;font-size:12px;")
            lay.addWidget(status_lbl)

            self._channel_list.setItemWidget(item, row)
            self._channel_rows[ch] = {
                "checkbox": cb, "label": label, "badge": status_lbl,
                "item": item,
                "method_cb": method_cb, "status_lbl": status_lbl,
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

    def _refresh_channel_row(self, ch):
        row = self._channel_rows.get(ch)
        if not row:
            return
        cb = row["checkbox"]
        status_lbl = row["status_lbl"]
        row_widget  = row["row_widget"]
        cb.blockSignals(True)

        if ch == self.nucleus_channel:
            cb.setChecked(False)
            cb.setEnabled(False)
            cb.setStyleSheet("")
            status_lbl.setText("★")
            status_lbl.setStyleSheet("color:#56b6c2;font-size:12px;")
            row_widget.setStyleSheet("")
        elif ch in self._computed_channels:
            # 计算完成：checkbox变绿锁定，不可取消
            cb.setChecked(True)
            cb.setEnabled(False)
            cb.setStyleSheet(
                "QCheckBox::indicator{border:1px solid #6bffa0;border-radius:2px;"
                "background:#6bffa0;}"
                "QCheckBox::indicator:checked{background:#6bffa0;border:1px solid #6bffa0;}"
            )
            status_lbl.setText("")   # 不再显示独立绿勾
            row_widget.setStyleSheet("background:#1a2e1a;border-radius:3px;")
        else:
            cb.setEnabled(True)
            cb.setStyleSheet("")
            checked = ch in self._channel_methods
            cb.setChecked(checked)
            status_lbl.setText("—")
            status_lbl.setStyleSheet("color:#666;font-size:12px;")
            row_widget.setStyleSheet("")

        cb.blockSignals(False)

    def _set_channel_computing(self, ch):
        """将通道状态设为计算中。"""
        row = self._channel_rows.get(ch)
        if not row:
            return
        row["status_lbl"].setText("⟳")
        row["status_lbl"].setStyleSheet("color:#e5c07b;font-size:13px;")
        row["row_widget"].setStyleSheet("background:#2a2a1a;border-radius:3px;")

    def _set_channel_done(self, ch):
        """计算完成：checkbox绿色锁定，不可取消。"""
        self._computed_channels.add(ch)
        row = self._channel_rows.get(ch)
        if not row:
            return
        cb = row["checkbox"]
        cb.blockSignals(True)
        cb.setChecked(True)
        cb.setEnabled(False)
        cb.setStyleSheet(
            "QCheckBox::indicator{border:1px solid #6bffa0;border-radius:2px;"
            "background:#6bffa0;}"
            "QCheckBox::indicator:checked{background:#6bffa0;border:1px solid #6bffa0;}"
        )
        cb.blockSignals(False)
        row["status_lbl"].setText("")
        row["row_widget"].setStyleSheet("background:#1a2e1a;border-radius:3px;")

    def _pick_channel_color(self, ch, btn):
        """弹颜色对话框，让用户选择通道显示颜色。"""
        from PyQt5.QtWidgets import QColorDialog
        rgb = self._channel_colors.get(ch, (0.2, 1.0, 0.2))
        init_color = QtGui.QColor(int(rgb[0]*255), int(rgb[1]*255), int(rgb[2]*255))
        color = QColorDialog.getColor(init_color, self, f"Color for {ch}")
        if not color.isValid():
            return
        new_rgb = (color.red()/255.0, color.green()/255.0, color.blue()/255.0)
        self._channel_colors[ch] = new_rgb
        hex_color = color.name()
        btn.setStyleSheet(
            f"QPushButton{{background:{hex_color};border:1px solid #555;border-radius:2px;}}"
            f"QPushButton:hover{{border:1px solid #aaa;}}"
        )
        # 使缓存中该通道的结果失效，下次重新合成RGB
        keys_to_del = [k for k in self._preview_cache if k[0] == ch]
        for k in keys_to_del:
            del self._preview_cache[k]
        # 如果当前正在显示这个通道，重新渲染
        if ch == self.current_channel and self._last_payload is not None:
            # 用新颜色重新合成RGB overlay
            self._rebuild_payload_rgb(ch)
            self._refresh_preview_display(keep_zoom=True)

    def _pick_nucleus_color(self):
        """弹颜色对话框，让用户选择 nucleus 叠加显示颜色。"""
        from PyQt5.QtWidgets import QColorDialog
        rgb = getattr(self, '_nuc_color', (0.0, 0.5, 1.0))
        init_color = QtGui.QColor(int(rgb[0] * 255), int(rgb[1] * 255), int(rgb[2] * 255))
        color = QColorDialog.getColor(init_color, self, "Color for nucleus")
        if not color.isValid():
            return
        self._nuc_color = (color.red() / 255.0, color.green() / 255.0, color.blue() / 255.0)
        self._nuc_color_btn.setStyleSheet(
            f"QPushButton{{background:{color.name()};border:1px solid #555;border-radius:2px;}}"
            f"QPushButton:hover{{border:1px solid #aaa;}}"
        )
        if self._last_payload is not None and self.current_channel:
            self._rebuild_payload_rgb(self.current_channel)
            self._refresh_preview_display(keep_zoom=True)

    def _pick_marker_color(self):
        """弹颜色对话框，让用户选择当前 marker 通道叠加显示颜色。"""
        from PyQt5.QtWidgets import QColorDialog
        current_ch = self.current_channel
        default_rgb = getattr(self, '_marker_color', (0.0, 1.0, 0.3))
        rgb = self._channel_colors.get(current_ch, default_rgb) if current_ch else default_rgb
        init_color = QtGui.QColor(int(rgb[0] * 255), int(rgb[1] * 255), int(rgb[2] * 255))
        title = f"Color for {current_ch}" if current_ch else "Color for marker"
        color = QColorDialog.getColor(init_color, self, title)
        if not color.isValid():
            return
        new_rgb = (color.red() / 255.0, color.green() / 255.0, color.blue() / 255.0)
        self._marker_color = new_rgb
        if current_ch:
            self._channel_colors[current_ch] = new_rgb
        self._marker_color_btn.setStyleSheet(
            f"QPushButton{{background:{color.name()};border:1px solid #555;border-radius:2px;}}"
            f"QPushButton:hover{{border:1px solid #aaa;}}"
        )
        if self._last_payload is not None and current_ch:
            self._rebuild_payload_rgb(current_ch)
            self._refresh_preview_display(keep_zoom=True)

    def _rebuild_payload_rgb_from(self, payload, ch, nucleus_rgb, marker_rgb):
        """按当前 nucleus / marker 颜色重建 payload 中三路 RGB 预览。"""
        if payload is None:
            return
        self._channel_colors[ch] = marker_rgb
        for mono_key, rgb_key in (
            ("original_disp", "original_rgb"),
            ("tophat_disp",   "tophat_rgb"),
            ("cucim_disp",    "cucim_rgb"),
        ):
            marker = payload.get(mono_key)
            if marker is None:
                continue
            payload[rgb_key] = self._make_colored_rgb(
                marker,
                payload.get("nucleus_disp"),
                marker_rgb=marker_rgb,
                nucleus_rgb=nucleus_rgb,
            )

    def _rebuild_payload_rgb(self, ch):
        """用当前通道颜色重新合成_last_payload中的RGB图。"""
        payload = self._last_payload
        if payload is None:
            return
        self._rebuild_payload_rgb_from(
            payload,
            ch,
            getattr(self, '_nuc_color', (0.0, 0.5, 1.0)),
            self._channel_colors.get(ch, getattr(self, '_marker_color', (0.0, 1.0, 0.3))),
        )

    @staticmethod
    def _make_colored_rgb(
        marker_norm,
        nucleus_norm,
        marker_rgb=(0.2, 1.0, 0.2),
        nucleus_rgb=(0.0, 0.5, 1.0),
    ):
        """按 marker / nucleus 各自颜色合成 float32 RGB (H, W, 3)。"""
        marker_f = marker_norm.astype(np.float32, copy=False)
        r = marker_f * marker_rgb[0]
        g = marker_f * marker_rgb[1]
        b = marker_f * marker_rgb[2]
        if nucleus_norm is not None:
            nucleus_f = nucleus_norm.astype(np.float32, copy=False)
            r = np.clip(r + nucleus_f * nucleus_rgb[0], 0, 1)
            g = np.clip(g + nucleus_f * nucleus_rgb[1], 0, 1)
            b = np.clip(b + nucleus_f * nucleus_rgb[2], 0, 1)
        return np.stack([r, g, b], axis=-1)

    def _sync_zoom(self, src_idx):
        """将一个预览窗的缩放/平移同步到另外两个窗。"""
        if not hasattr(self, "_preview_vbs") or not (0 <= src_idx < len(self._preview_vbs)):
            return
        src_vb = self._preview_vbs[src_idx]
        x_range, y_range = src_vb.viewRange()
        self._zoom_lock_active = True
        try:
            for idx, vb in enumerate(self._preview_vbs):
                if idx == src_idx:
                    continue
                vb.setRange(xRange=x_range, yRange=y_range, padding=0)
        finally:
            self._zoom_lock_active = False

    def _reset_single_view(self, idx):
        """复位指定预览窗；如果锁定开启则同步复位全部。"""
        if not hasattr(self, "_preview_vbs") or not (0 <= idx < len(self._preview_vbs)):
            return
        if self._btn_lock_zoom.isChecked():
            self._reset_all_views()
            return
        self._preview_vbs[idx].autoRange()

    def _reset_all_views(self):
        """复位三联预览到完整视图。"""
        if not hasattr(self, "_preview_vbs"):
            return
        self._zoom_lock_active = True
        try:
            for vb in self._preview_vbs:
                vb.autoRange()
        finally:
            self._zoom_lock_active = False

    def _refresh_preview_display(self, keep_zoom=False):
        """按当前显示开关、颜色和对比度刷新三联预览。"""
        payload = self._last_payload
        if payload is None:
            return

        prev_ranges = [vb.viewRange() for vb in self._preview_vbs] if keep_zoom else None
        self._rebuild_payload_rgb(
            self.current_channel or next(iter(self._channel_colors.keys()), "")
        )

        marker_on = self._btn_show_marker.isChecked()
        nucleus_on = self._btn_show_nucleus.isChecked()
        marker_scale = max(float(self._marker_contrast_slider.value()) / 100.0, 1e-3)
        nucleus_scale = max(float(self._nuc_contrast_slider.value()) / 100.0, 1e-3)

        def _compose(rgb_key, mono_key):
            rgb = payload.get(rgb_key)
            marker = payload.get(mono_key)
            nucleus = payload.get("nucleus_disp")

            if rgb is None and marker is not None:
                rgb = self._make_colored_rgb(
                    marker,
                    nucleus,
                    marker_rgb=self._channel_colors.get(
                        self.current_channel, getattr(self, "_marker_color", (0.0, 1.0, 0.3))
                    ),
                    nucleus_rgb=getattr(self, "_nuc_color", (0.0, 0.5, 1.0)),
                )
            if rgb is None:
                return None

            out = np.zeros_like(rgb, dtype=np.float32)
            if marker_on:
                out += rgb.astype(np.float32, copy=False)
            if not marker_on and nucleus_on and nucleus is not None:
                nuc_rgb = getattr(self, "_nuc_color", (0.0, 0.5, 1.0))
                nucleus_f = np.clip(nucleus.astype(np.float32, copy=False) / nucleus_scale, 0, 1)
                out[..., 0] += nucleus_f * nuc_rgb[0]
                out[..., 1] += nucleus_f * nuc_rgb[1]
                out[..., 2] += nucleus_f * nuc_rgb[2]
            elif marker_on and not nucleus_on and nucleus is not None:
                nuc_rgb = getattr(self, "_nuc_color", (0.0, 0.5, 1.0))
                nucleus_f = nucleus.astype(np.float32, copy=False)
                out[..., 0] -= nucleus_f * nuc_rgb[0]
                out[..., 1] -= nucleus_f * nuc_rgb[1]
                out[..., 2] -= nucleus_f * nuc_rgb[2]

            if marker_on and marker is not None:
                base_marker = np.clip(marker.astype(np.float32, copy=False) / marker_scale, 0, 1)
                marker_rgb = self._channel_colors.get(
                    self.current_channel, getattr(self, "_marker_color", (0.0, 1.0, 0.3))
                )
                if nucleus_on and nucleus is not None:
                    nucleus_f = np.clip(nucleus.astype(np.float32, copy=False) / nucleus_scale, 0, 1)
                else:
                    nucleus_f = None
                out = self._make_colored_rgb(
                    base_marker,
                    nucleus_f,
                    marker_rgb=marker_rgb,
                    nucleus_rgb=getattr(self, "_nuc_color", (0.0, 0.5, 1.0)),
                )

            return np.clip(out, 0, 1)

        imgs = (
            _compose("original_rgb", "original_disp"),
            _compose("tophat_rgb", "tophat_disp"),
            _compose("cucim_rgb", "cucim_disp"),
        )
        _lv = [0.0, 1.0]

        # 确定黑图尺寸（用第一个非None图的尺寸，或默认64x64）
        _blank_shape = next((a.shape[:2] for a in imgs if a is not None), (64, 64))
        _blank = np.zeros((*_blank_shape, 3), dtype=np.float32)

        self._zoom_lock_active = True
        try:
            for idx, arr in enumerate(imgs):
                # arr=None表示该方法未计算，显示黑色清空，不保留上一通道的图
                self._preview_imgs[idx].setImage(
                    arr if arr is not None else _blank,
                    autoLevels=False, levels=_lv)
            if keep_zoom and prev_ranges is not None:
                for idx, vb in enumerate(self._preview_vbs):
                    xr, yr = prev_ranges[idx]
                    vb.setRange(xRange=xr, yRange=yr, padding=0)
            else:
                for vb in self._preview_vbs:
                    vb.autoRange()
        finally:
            self._zoom_lock_active = False

    def _on_contrast_changed(self):
        """调整显示对比度时刷新预览，不触发重算。"""
        self._refresh_preview_display(keep_zoom=True)

    def _update_decision_ui(self):
        ch = self.current_channel
        enabled = bool(ch and ch != self.nucleus_channel and ch in self._channel_rows)
        self._apply_btn.setEnabled(enabled)
        if not enabled:
            self._decision_status.setText("The locked nucleus channel is always excluded from correction.")
            self._dec_orig.setChecked(True)
            return
        decision = self._channel_decisions.get(ch, "original")
        if decision == "both":
            decision = "original"
        if decision == "tophat":
            self._dec_top.setChecked(True)
        elif decision == "cucim":
            self._dec_cu.setChecked(True)
        else:
            self._dec_orig.setChecked(True)
        if decision != "original":
            self._decision_status.setText(f"Saved decision for {ch}: {decision}")
        else:
            self._decision_status.setText(
                f"No correction assigned for {ch}. Select a method and click Apply."
            )

    def _refresh_channel_row(self, ch):
        row = self._channel_rows.get(ch)
        if not row:
            return
        cb = row["checkbox"]
        cb.blockSignals(True)
        if ch == self.nucleus_channel:
            txt, color = "excluded", "#666"
            cb.setChecked(False)
        else:
            decision = self._channel_decisions.get(ch, "original")
            txt = decision
            color = {"tophat": "#e5c07b", "cucim": "#56b6c2", "original": "#777"}.get(decision, "#888")
            cb.setChecked(decision != "original")
        cb.blockSignals(False)
        row["badge"].setText(txt)
        row["badge"].setStyleSheet(f"color:{color};font-size:10px;font-weight:bold;")

    def _on_channel_checkbox_toggled(self, ch, state):
        if ch == self.nucleus_channel:
            return
        if state == Qt.Checked and self._channel_decisions.get(ch, "original") == "original":
            if self._dec_cu.isChecked():
                self._channel_decisions[ch] = "cucim"
            else:
                self._channel_decisions[ch] = "tophat"
        elif state != Qt.Checked:
            self._channel_decisions[ch] = "original"
        self._refresh_channel_row(ch)
        if ch == self.current_channel:
            self._update_decision_ui()

    def _rebuild_patch_buttons(self):
        while self._patch_buttons_row.count():
            item = self._patch_buttons_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        if not self.patches:
            self.current_patch_idx = 0
            self._patch_info.setText("No patch ROI available yet. Draw a patch in Section B first.")
            return
        self.current_patch_idx = min(self.current_patch_idx, len(self.patches) - 1)
        for i in range(len(self.patches)):
            btn = QPushButton(f"P{i+1}")
            btn.setCheckable(True)
            btn.setFixedSize(44, 22)
            color = PATCH_COLORS[i % len(PATCH_COLORS)]
            btn.setStyleSheet(
                f"QPushButton{{color:{color};border:1px solid {color};border-radius:3px;background:#1a1a1a;font-size:10px;font-weight:bold;}}"
                f"QPushButton:checked{{background:{color};color:#111;}}"
            )
            btn.clicked.connect(lambda _checked, idx=i: self._select_patch(idx))
            btn.setChecked(i == self.current_patch_idx)
            self._patch_buttons_row.addWidget(btn)
        self._patch_buttons_row.addStretch()
        self._update_patch_info()

    def _sync_patch_buttons(self):
        for i in range(self._patch_buttons_row.count()):
            widget = self._patch_buttons_row.itemAt(i).widget()
            if isinstance(widget, QPushButton):
                widget.setChecked(widget.text() == f"P{self.current_patch_idx+1}")

    def _select_patch(self, idx):
        self.current_patch_idx = idx
        self._patch_selected_idx = idx
        if self._patch_list.count() > idx:
            self._patch_list.setCurrentRow(idx)
        self._sync_patch_buttons()
        self._update_patch_info()

    def _update_patch_info(self):
        if not self.patches:
            return
        y0, y1, x0, x1 = self.patches[self.current_patch_idx]
        self._patch_info.setText(
            f"Current patch: P{self.current_patch_idx+1}  [{y0}:{y1}, {x0}:{x1}]  {(y1-y0):,}x{(x1-x0):,} px"
        )

    # ══ 通道/方法 选择事件 ═══════════════════════════════════════════

    def _on_select_all_changed(self, state):
        """All channels checkbox change. Uses each channel's own method_cb value."""
        checked = (state == Qt.Checked)
        for ch in self._channel_order:
            if ch == self.nucleus_channel:
                continue
            row = self._channel_rows.get(ch)
            if row:
                row["checkbox"].blockSignals(True)
                row["checkbox"].setChecked(checked)
                row["checkbox"].blockSignals(False)
                if checked:
                    method_txt = row["method_cb"].currentText().lower()
                    if method_txt not in {"tophat", "cucim", "both"}:
                        method_txt = "both"
                    self._channel_methods[ch] = method_txt
                    self._channel_decisions[ch] = method_txt
                else:
                    self._channel_methods.pop(ch, None)
                    self._channel_decisions[ch] = "original"

    def _on_method_all_changed(self, txt):
        """All channels 方法下拉变化，同步到所有勾选通道。"""
        method = txt.lower()
        for ch in self._channel_order:
            if ch == self.nucleus_channel:
                continue
            row = self._channel_rows.get(ch)
            if row and row["checkbox"].isChecked():
                self._channel_methods[ch] = method
                idx_map = {"tophat": 0, "cucim": 1, "both": 2}
                row["method_cb"].blockSignals(True)
                row["method_cb"].setCurrentIndex(idx_map.get(method, 0))
                row["method_cb"].blockSignals(False)

    def _on_channel_method_changed(self, ch, txt):
        """Single channel method dropdown change."""
        self._channel_methods[ch] = txt.lower()
        self._channel_decisions[ch] = txt.lower()

    def _on_channel_checkbox_toggled(self, ch, state):
        if ch == self.nucleus_channel:
            return
        if state == Qt.Checked:
            method_txt = self._channel_rows[ch]["method_cb"].currentText().lower()
            self._channel_methods[ch] = method_txt
            self._channel_decisions[ch] = method_txt
        else:
            self._channel_methods.pop(ch, None)
            self._channel_decisions[ch] = "original"

    def _on_channel_row_changed(self, row):
        if row < 0 or row >= len(self._channel_order):
            self.current_channel = None
            self._apply_btn.setEnabled(False)
            return
        self.current_channel = self._channel_order[row]
        self._update_decision_ui()

        if not self.patches:
            self._preview_status.setText(
                "⚠  Draw patches in Section B first.")
            self._preview_status.setStyleSheet("color:#ffb86c;font-size:11px;")
            return

        ch = self.current_channel
        if ch == self.nucleus_channel:
            self._preview_status.setText("Nucleus channel is excluded from correction.")
            return

        # 检查是否有缓存结果可以直接显示
        if self._has_any_cache(ch):
            self._show_channel_from_cache(ch)
        elif ch in self._computed_channels:
            self._preview_status.setText(f"No result for {ch}. Try re-processing.")
        else:
            # 按需计算：只有process已完成（_process_completed=True）才允许
            if getattr(self, '_process_completed', False):
                self._preview_status.setText(
                    f"Computing {ch} on demand…")
                self._preview_status.setStyleSheet("color:#aaa;font-size:10px;")
                self._start_ondemand(ch)
            else:
                self._preview_status.setText(
                    "Run Process first. On-demand computing is locked until Process completes.")
                self._preview_status.setStyleSheet("color:#ffb86c;font-size:10px;")

    # ══ Process 按钮逻辑 ══════════════════════════════════════════════

    def _on_process_clicked(self):
        """▶ Process 按钮。只要勾选就跑，method只有tophat/cucim/both。"""
        selected = {}
        for ch, row_data in self._channel_rows.items():
            if ch == self.nucleus_channel:
                continue
            if row_data["checkbox"].isChecked():
                method = row_data["method_cb"].currentText().lower()
                if method not in {"tophat", "cucim", "both"}:
                    method = self._channel_methods.get(ch, "both")
                if method == "original":
                    continue  # skip channels with no correction selected
                selected[ch] = method
        if not selected:
            QMessageBox.information(self, "No channels selected",
                                    "Please check at least one channel and select a method.")
            return
        if not self.patches:
            QMessageBox.information(self, "No patches",
                                    "Please draw at least one patch in Section B.")
            return

        # 清掉受影响通道的缓存（params dirty时重算）
        if self._params_dirty:
            for ch in selected:
                self._preview_cache = {k: v for k, v in self._preview_cache.items()
                                       if k[0] != ch}
            self._computed_channels -= set(selected.keys())
            self._params_dirty = False
        self._process_completed = False   # 锁住按需计算，直到本次process完成

        self._btn_process.setEnabled(False)
        self._btn_stop_process.setEnabled(True)
        self._proc_pbar.setVisible(True)
        self._proc_pbar.setValue(0)
        self._proc_status.setText("Starting…")

        # 将选中通道标记为"计算中"
        for ch in selected:
            self._set_channel_computing(ch)

        self._batch_worker = BatchProcessWorker(
            self.loader, self.patches, selected,
            self.nucleus_channel,
            self._tophat_slider.value(),
            self._cucim_slider.value(),
            max_gpu_workers=4,
        )
        self._batch_worker.channel_patch_done.connect(self._on_batch_patch_done)
        self._batch_worker.channel_done.connect(self._on_batch_channel_done)
        self._batch_worker.all_done.connect(self._on_batch_all_done)
        self._batch_worker.progress.connect(self._on_batch_progress)
        self._batch_worker.error_signal.connect(self._on_batch_error)
        self._batch_worker.canceled.connect(self._on_batch_canceled)
        self._batch_worker.start()

    def _on_stop_process(self):
        if self._batch_worker and self._batch_worker.isRunning():
            self._batch_worker.stop()

    def _on_batch_progress(self, done, total, msg):
        pct = int(done / total * 100) if total > 0 else 0
        self._proc_pbar.setValue(pct)
        self._proc_status.setText(msg)

    def _on_batch_patch_done(self, ch, p_idx, payload):
        """一个patch计算完成，存入缓存。"""
        self._preview_cache[(ch, p_idx)] = payload
        # 如果当前正在查看这个通道的这个patch，立刻刷新
        if ch == self.current_channel and p_idx == self.current_patch_idx:
            self._last_payload = payload
            self._rebuild_payload_rgb_from(
                payload, ch,
                getattr(self, '_nuc_color', (0.0, 0.5, 1.0)),
                self._channel_colors.get(ch, getattr(self, '_marker_color', (0.0, 1.0, 0.3))))
            self._refresh_preview_display(keep_zoom=False)
            self._metrics_original.setText(self._metric_text("Original", payload["original_metrics"]))
            if payload.get("tophat_disp") is not None:
                self._metrics_tophat.setText(self._metric_text("TopHat", payload["tophat_metrics"]))
            else:
                self._metrics_tophat.setText("TopHat    → Not computed")
            if payload.get("cucim_disp") is not None:
                self._metrics_cucim.setText(self._metric_text("cucim", payload["cucim_metrics"]))
            else:
                self._metrics_cucim.setText("cucim     → Not computed")
            self._preview_status.setText(
                f"Preview ready: {ch}  P{p_idx+1}")
            self._preview_status.setStyleSheet("color:#aaa;font-size:10px;")

    def _on_batch_channel_done(self, ch):
        """一个通道的所有patches全部计算完成。"""
        self._set_channel_done(ch)

    def _on_batch_all_done(self):
        self._proc_pbar.setValue(100)
        self._proc_status.setText("✓ All done. Click a channel to view results.")
        self._proc_status.setStyleSheet("color:#6bffa0;font-size:10px;font-weight:bold;")
        self._btn_process.setEnabled(True)
        self._btn_process.setText("↺ Re-process")
        self._btn_stop_process.setEnabled(False)
        self._process_completed = True   # 解锁按需计算

    def _on_batch_canceled(self):
        self._proc_status.setText("Stopped.")
        self._proc_status.setStyleSheet("color:#ffb86c;font-size:10px;")
        self._btn_process.setEnabled(True)
        self._btn_stop_process.setEnabled(False)

    def _on_batch_error(self, ch, p_idx, msg):
        print(f"[Batch Error] ch={ch} p_idx={p_idx}\n{msg}")
        if ch == "__global__":
            self._proc_status.setText(f"Error (see terminal): {msg[:60]}")
            self._proc_status.setStyleSheet("color:#ff6b6b;font-size:10px;")
            self._btn_process.setEnabled(True)
            self._btn_stop_process.setEnabled(False)

    # ══ 按需计算（点击未计算通道）════════════════════════════════════

    def _start_ondemand(self, ch):
        """为未计算的通道启动按需计算（所有patches）。"""
        if not self.loader or not self.patches:
            return
        # 从method_cb直接读（最可靠），_channel_methods作为备用，默认both
        row_data = self._channel_rows.get(ch)
        if row_data and "method_cb" in row_data:
            method = row_data["method_cb"].currentText().lower()
        else:
            method = self._channel_methods.get(ch, "both")
        if method not in {"tophat", "cucim", "both"}:
            method = "both"
        self._set_channel_computing(ch)

        worker = BatchProcessWorker(
            self.loader, self.patches,
            {ch: method},
            self.nucleus_channel,
            self._tophat_slider.value(),
            self._cucim_slider.value(),
            max_gpu_workers=2,
        )
        worker.channel_patch_done.connect(self._on_batch_patch_done)
        worker.channel_done.connect(self._on_batch_channel_done)
        worker.all_done.connect(lambda: None)
        worker.error_signal.connect(self._on_batch_error)
        worker.canceled.connect(lambda: None)
        self._ondemand_workers.append(worker)
        worker.start()

    # ══ 从缓存显示结果 ════════════════════════════════════════════════

    def _has_any_cache(self, ch):
        return any(k[0] == ch for k in self._preview_cache)

    def _show_channel_from_cache(self, ch):
        """从缓存里取当前patch的结果并显示。"""
        p_idx = self.current_patch_idx
        payload = self._preview_cache.get((ch, p_idx))
        if payload is None:
            # 找该通道任意一个patch的结果
            for pi in range(len(self.patches)):
                payload = self._preview_cache.get((ch, pi))
                if payload is not None:
                    self.current_patch_idx = pi
                    self._sync_patch_buttons()
                    break
        if payload is None:
            self._preview_status.setText(f"No cached result for {ch}.")
            return
        nc = getattr(self, '_nuc_color', (0.0, 0.5, 1.0))
        mc = self._channel_colors.get(ch, getattr(self, '_marker_color', (0.0, 1.0, 0.3)))
        self._rebuild_payload_rgb_from(payload, ch, nc, mc)
        self._last_payload = payload
        self._refresh_preview_display(keep_zoom=True)
        self._metrics_original.setText(self._metric_text("Original", payload["original_metrics"]))
        # 直接检查_disp是否None，不依赖method字段
        if payload.get("tophat_disp") is not None:
            self._metrics_tophat.setText(self._metric_text("TopHat", payload["tophat_metrics"]))
        else:
            self._metrics_tophat.setText("TopHat    → Not computed")
        if payload.get("cucim_disp") is not None:
            self._metrics_cucim.setText(self._metric_text("cucim", payload["cucim_metrics"]))
        else:
            self._metrics_cucim.setText("cucim     → Not computed")
        not_computed = []
        if payload.get("tophat_disp") is None: not_computed.append("TopHat")
        if payload.get("cucim_disp")  is None: not_computed.append("cucim")
        status = f"[cache] {ch}  P{self.current_patch_idx+1}"
        if not_computed:
            status += f"  ({', '.join(not_computed)} not computed)"
        self._preview_status.setText(status)
        self._preview_status.setStyleSheet("color:#6bffa0;font-size:10px;")
        self._update_decision_ui()

    # ══ 切换patch时直接从缓存取 ═══════════════════════════════════════

    def _select_patch(self, idx):
        self.current_patch_idx = idx
        self._patch_selected_idx = idx
        if self._patch_list.count() > idx:
            self._patch_list.setCurrentRow(idx)
        self._sync_patch_buttons()
        self._update_patch_info()
        if self.current_channel and self._has_any_cache(self.current_channel):
            self._show_channel_from_cache(self.current_channel)

    # ══ Params dirty tracking ════════════════════════════════════════

    def _on_slider_changed(self):
        self._refresh_slider_labels()
        if not self._params_dirty:
            self._params_dirty = True
            self._btn_process.setText("↺ Re-process (params changed)")
            self._btn_process.setStyleSheet(
                "QPushButton{background:#5c3a1a;color:#ffb86c;border:1px solid #c87;"
                "border-radius:4px;padding:6px 14px;font-size:12px;font-weight:bold;}"
                "QPushButton:hover{background:#7c5a2a;}"
            )

    def _refresh_slider_labels(self):
        self._tophat_value.setText(f"disk_radius: {self._tophat_slider.value()}")
        self._tophat_value.setStyleSheet("color:#ddd;font-size:11px;")
        self._cucim_value.setText(f"sigma: {self._cucim_slider.value()}")
        self._cucim_value.setStyleSheet("color:#ddd;font-size:11px;")

    def _queue_preview(self):
        self._preview_debounce.start(150)

    def _start_preview_compute(self):
        if not self.loader or not self.patches or not self.current_channel:
            return
        if self.current_channel == self.nucleus_channel:
            self._preview_status.setText("The nucleus/DAPI channel is excluded from background correction preview.")
            return
        roi = self.patches[self.current_patch_idx]
        if self._preview_worker is not None and self._preview_worker.isRunning():
            self._preview_worker.stop()
        self._preview_req_id += 1
        req_id = self._preview_req_id
        self._preview_status.setText(
            f"Computing preview for {self.current_channel} on P{self.current_patch_idx+1}…"
        )
        self._preview_worker = BackgroundPreviewWorker(
            req_id,
            self.loader,
            self.current_channel,
            roi,
            self._tophat_slider.value(),
            self._cucim_slider.value(),
            nucleus_channel=self.nucleus_channel,
        )
        self._preview_worker.finished.connect(self._on_preview_ready)
        self._preview_worker.error.connect(self._on_preview_error)
        self._preview_worker.start()

    def _on_preview_ready(self, req_id, payload):
        if req_id != self._preview_req_id:
            return
        self._preview_cache[(self.current_channel, self.current_patch_idx)] = payload
        self._last_payload = payload
        self._rebuild_payload_rgb_from(
            payload,
            self.current_channel,
            getattr(self, "_nuc_color", (0.0, 0.5, 1.0)),
            self._channel_colors.get(
                self.current_channel, getattr(self, "_marker_color", (0.0, 1.0, 0.3))
            ),
        )
        self._refresh_preview_display(keep_zoom=False)
        self._metrics_original.setText(self._metric_text("Original", payload["original_metrics"]))
        self._metrics_tophat.setText(self._metric_text("TopHat", payload["tophat_metrics"]))
        self._metrics_cucim.setText(self._metric_text("cucim", payload["cucim_metrics"]))
        self._preview_status.setText(
            f"Preview ready for {self.current_channel} on P{self.current_patch_idx+1}."
        )
        self._preview_status.setStyleSheet("color:#aaa;font-size:10px;")

    # ══ Patch变化时重新触发（如果通道已有结果）═══════════════════════

    def _on_preview_error(self, req_id, msg):
        if req_id != self._preview_req_id:
            return
        self._preview_status.setText("Preview failed. See terminal for details.")
        print(f"[Step0 Preview Error]\n{msg}")

    @staticmethod
    def _metric_text(name, metrics):
        return f'{name:<10} → SNR: {metrics["snr"]:.2f}  BG-CV: {metrics["bg_cv"]:.2f}'

    def _apply_current_channel_decision(self):
        ch = self.current_channel
        if not ch or ch == self.nucleus_channel:
            return
        if self._dec_top.isChecked():
            decision = "tophat"
        elif self._dec_cu.isChecked():
            decision = "cucim"
        else:
            decision = "original"
        self._channel_decisions[ch] = decision
        self._refresh_channel_row(ch)
        self._decision_status.setText(f"Saved decision for {ch}: {decision}")

    def _build_config(self):
        decisions = {}
        for ch in self._channel_order:
            if ch == self.nucleus_channel:
                continue
            d = self._channel_decisions.get(ch, "original")
            decisions[ch] = "original" if d == "both" else d
        return {
            "method_params": {
                "tophat_radius": int(self._tophat_slider.value()),
                "cucim_sigma": int(self._cucim_slider.value()),
            },
            "channel_decisions": decisions,
        }

    def _stop_bg_workers(self):
        for worker in self._bg_workers:
            if worker.isRunning():
                worker.stop()
        self._bg_workers = []

    def _on_start_bg_correction(self):
        n_tophat = sum(
            1 for ch in self._channel_order
            if ch != self.nucleus_channel and self._channel_decisions.get(ch) == "tophat"
        )
        n_cucim = sum(
            1 for ch in self._channel_order
            if ch != self.nucleus_channel and self._channel_decisions.get(ch) == "cucim"
        )
        n_all = len([ch for ch in self._channel_order if ch != self.nucleus_channel])
        n_orig = n_all - n_tophat - n_cucim

        os.makedirs(self.output_dir, exist_ok=True)
        config = self._build_config()
        out_path = os.path.join(self.output_dir, "correction_config.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)

        channels_to_run = [
            ch for ch in self._channel_order
            if ch != self.nucleus_channel and self._channel_decisions.get(ch, "original") in ("tophat", "cucim")
        ]

        self._bg_n_tophat = n_tophat
        self._bg_n_cucim = n_cucim
        self._bg_n_orig = n_orig
        self._bg_workers = []

        if not channels_to_run or not self.patches:
            self._finish_bg_start(n_run=0)
            return

        self._bg_queue = list(channels_to_run)
        self._bg_queue_idx = 0
        self._bg_n_total = len(channels_to_run)
        self._btn_start_bg.setEnabled(False)
        self._btn_start_bg.setText("Running…")
        self._bg_pbar.setVisible(True)
        self._bg_pbar.setValue(0)
        self._bg_start_status.setStyleSheet("color:#aaa;font-size:11px;")
        self._bg_run_next()

    def _bg_run_next(self):
        if self._bg_queue_idx >= len(self._bg_queue):
            self._finish_bg_start(n_run=self._bg_n_total)
            return

        ch = self._bg_queue[self._bg_queue_idx]
        roi = self.patches[self.current_patch_idx]
        pct = int(100 * self._bg_queue_idx / max(1, self._bg_n_total))
        method = self._channel_decisions.get(ch, "original")
        self._bg_pbar.setValue(pct)
        self._bg_start_status.setText(f"▶ Channel {self._bg_queue_idx + 1} / {self._bg_n_total}: {ch} [{method}]")
        self._preview_status.setText(f"[BG Run] Processing channel {self._bg_queue_idx + 1}/{self._bg_n_total}: {ch} ({method})")
        self._preview_status.setStyleSheet("color:#e5c07b;font-size:11px;font-weight:bold;")

        if ch in self._channel_rows:
            self._channel_list.blockSignals(True)
            self._channel_list.setCurrentItem(self._channel_rows[ch]["item"])
            self._channel_list.blockSignals(False)
            self.current_channel = ch

        self._preview_req_id += 1
        req_id = self._preview_req_id
        worker = BackgroundPreviewWorker(
            req_id, self.loader, ch, roi,
            self._tophat_slider.value(), self._cucim_slider.value(),
            nucleus_channel=self.nucleus_channel,
        )

        def on_done(rid, payload, expected=req_id, ch_name=ch):
            if rid != expected:
                return
            self._last_payload = payload
            self._refresh_preview_display()
            self._metrics_original.setText(self._metric_text("Original", payload["original_metrics"]))
            self._metrics_tophat.setText(self._metric_text("TopHat",    payload["tophat_metrics"]))
            self._metrics_cucim.setText(self._metric_text("cucim",      payload["cucim_metrics"]))
            self._preview_status.setText(
                f'[BG Run] Done: {ch_name} (SNR {payload["original_metrics"]["snr"]:.1f} → {payload["tophat_metrics"]["snr"]:.1f})'
            )
            self._bg_queue_idx += 1
            self._bg_run_next()

        def on_err(rid, _msg, expected=req_id):
            if rid == expected:
                self._bg_queue_idx += 1
                self._bg_run_next()

        worker.finished.connect(on_done)
        worker.error.connect(on_err)
        self._bg_workers.append(worker)
        worker.start()

    def _finish_bg_start(self, n_run):
        self._bg_pbar.setValue(100)
        parts = []
        if self._bg_n_tophat:
            parts.append(f"{self._bg_n_tophat} TopHat")
        if self._bg_n_cucim:
            parts.append(f"{self._bg_n_cucim} cucim")
        if self._bg_n_orig:
            parts.append(f"{self._bg_n_orig} original")
        summary = ", ".join(parts) if parts else "all original"
        if n_run > 0:
            nav_text = f"✓ {summary} — {n_run} channel(s) verified on patch"
            prev_text = f"[BG Run complete] {summary}"
        else:
            nav_text = f"✓ Config saved — {summary} (draw a patch ROI first to preview)"
            prev_text = "BG correction configured. Draw a patch ROI and re-run to preview."
        self._bg_start_status.setText(nav_text)
        self._bg_start_status.setStyleSheet("color:#6bffa0;font-size:11px;font-weight:bold;")
        self._preview_status.setText(prev_text)
        self._preview_status.setStyleSheet("color:#6bffa0;font-size:11px;")
        self._btn_start_bg.setText("▶ Run BG correction on all assigned channels")
        self._btn_start_bg.setEnabled(True)

    def _save_and_continue(self):
        if self.loader is None:
            QMessageBox.warning(self, "Validation", "Please load an OME-TIFF first.")
            return
        if not self.patches:
            QMessageBox.warning(self, "Validation", "Please define at least 1 patch before continuing.")
            return

        rois = list(self.overview.get_rois() if self.overview else self.rois)
        if not rois:
            QMessageBox.warning(self, "Validation", "No ROI found. Draw ROI first.")
            return
        self.rois = rois
        self._project_output_dir = self.output_dir
        self._roi_context = create_roi_context(self._project_output_dir, rois[0], self.ome_path)
        step0_dir = self._roi_context["step_dirs"]["step0"]
        os.makedirs(step0_dir, exist_ok=True)
        print("[Step0] writing ROI-specific outputs")
        print(f"[Step0] roi_id={self._roi_context['roi_id']}")
        print(f"[Step0] step0_dir={step0_dir}")

        config = self._build_config()
        config_path = os.path.join(step0_dir, "correction_config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        self.loader.set_correction_config(config)

        corrected = {
            ch: method
            for ch, method in (config.get("channel_decisions") or {}).items()
            if method in {"tophat", "cucim"}
        }
        zarr_path = os.path.join(step0_dir, "corrected_channels.zarr")

        if not corrected:
            self._ensure_empty_corrected_zarr(zarr_path, rois)
            self.loader.set_corrected_zarr_store(None, {})
            self._emit_complete(config, zarr_path, {})
            return

        self._btn_continue.setEnabled(False)
        self._btn_load.setEnabled(False)
        self._wsi_dialog = _WsiCorrectionProgressDialog(self)
        self._wsi_worker = WsiCorrectionWorker(
            self.loader, step0_dir, config, rois=rois, parent=self
        )
        self._wsi_worker.progress.connect(self._on_wsi_progress)
        self._wsi_worker.finished.connect(lambda path, decisions: self._on_wsi_finished(config, path, decisions))
        self._wsi_worker.canceled.connect(self._on_wsi_canceled)
        self._wsi_worker.error.connect(self._on_wsi_error)
        self._wsi_dialog.cancel_requested.connect(self._wsi_worker.stop_after_current_channel)
        self._wsi_worker.start()
        self._wsi_dialog.exec_()

    def _on_wsi_progress(self, channel_idx, channel_total, tile_idx, tile_total, ch_name, method, eta_s):
        pct = int(((channel_idx - 1) + tile_idx / max(1, tile_total)) / max(1, channel_total) * 100)
        self._wsi_dialog.set_progress(
            pct,
            f"Processing channel {channel_idx}/{channel_total}: {ch_name}  [{method}]",
            eta_s,
        )

    def _on_wsi_finished(self, config, zarr_path, decisions):
        if self._wsi_dialog is not None:
            self._wsi_dialog.allow_close()
            self._wsi_dialog.accept()
        self.loader.set_corrected_zarr_store(zarr_path, decisions)
        self._emit_complete(config, zarr_path, decisions)

    def _on_wsi_canceled(self, zarr_path):
        if os.path.exists(zarr_path):
            shutil.rmtree(zarr_path, ignore_errors=True)
        if self._wsi_dialog is not None:
            self._wsi_dialog.allow_close()
            self._wsi_dialog.reject()
        self._btn_continue.setEnabled(True)
        self._btn_load.setEnabled(True)
        QMessageBox.information(self, "Canceled", "Background correction was canceled. Partial corrected zarr output was removed.")

    def _on_wsi_error(self, msg):
        if self._wsi_dialog is not None:
            self._wsi_dialog.allow_close()
            self._wsi_dialog.reject()
        self._btn_continue.setEnabled(True)
        self._btn_load.setEnabled(True)
        QMessageBox.critical(self, "Background Correction Error", msg)
        print(f"[Step0 WSI Error]\n{msg}")

    @staticmethod
    def _clean_correction_config(config):
        cfg = dict(config or {})
        params = dict(cfg.get("method_params") or {})
        decisions = {}
        for ch, method in (cfg.get("channel_decisions") or {}).items():
            m = str(method).strip().lower()
            if m == "both":
                m = "original"
            if m not in {"tophat", "cucim", "original"}:
                m = "original"
            decisions[str(ch)] = m
        return {
            "method_params": {
                "tophat_radius": int(params.get("tophat_radius", TOPHAT_RADIUS_DEFAULT)),
                "cucim_sigma": int(params.get("cucim_sigma", CUCIM_SIGMA_DEFAULT)),
            },
            "channel_decisions": decisions,
        }

    @staticmethod
    def _roi_shape_from_bbox(bbox):
        if not bbox or len(bbox) != 4:
            return [0, 0]
        y0, y1, x0, x1 = [int(v) for v in bbox]
        return [max(0, y1 - y0), max(0, x1 - x0)]

    def _standard_rois(self):
        src = list(self.overview._rois if self.overview else self.rois)
        rois = []
        for idx, roi in enumerate(src, start=1):
            bbox = list(roi.get("bbox_fullres") or [])
            item = {
                "name": str(roi.get("name") or f"ROI_{idx}"),
                "display_name": str(roi.get("name") or f"ROI_{idx}"),
                "bbox_fullres": [int(v) for v in bbox] if len(bbox) == 4 else [],
                "polygon_fullres": roi.get("polygon_fullres") or [],
                "shape": self._roi_shape_from_bbox(bbox),
            }
            if idx == 1 and self._roi_context:
                item["roi_id"] = self._roi_context.get("roi_id", "")
                item["roi_dir"] = self._roi_context.get("roi_dir", "")
            if "color" in roi:
                item["color"] = roi.get("color")
            if "polygon_display" in roi:
                item["polygon_display"] = roi.get("polygon_display")
            rois.append(item)
        return rois

    @staticmethod
    def _patch_roi_name(patch, rois):
        y0, y1, x0, x1 = [int(v) for v in patch]
        cy = (y0 + y1) / 2.0
        cx = (x0 + x1) / 2.0
        for roi in rois:
            bbox = roi.get("bbox_fullres") or []
            if len(bbox) != 4:
                continue
            ry0, ry1, rx0, rx1 = [int(v) for v in bbox]
            if ry0 <= cy <= ry1 and rx0 <= cx <= rx1:
                return roi.get("name", "ROI_1"), [ry0, ry1, rx0, rx1]
        if rois:
            bbox = rois[0].get("bbox_fullres") or [0, 0, 0, 0]
            return rois[0].get("name", "ROI_1"), [int(v) for v in bbox]
        return "", [0, 0, 0, 0]

    def _standard_patches(self, rois):
        patches = []
        raw_patches = list(self.overview._patches if self.overview else [])
        if not raw_patches:
            raw_patches = [{"coords": p} for p in self.patches]
        for idx, patch_obj in enumerate(raw_patches, start=1):
            coords = patch_obj.get("coords") if isinstance(patch_obj, dict) else patch_obj
            if not coords or len(coords) != 4:
                continue
            y0, y1, x0, x1 = [int(v) for v in coords]
            roi_name, roi_bbox = self._patch_roi_name((y0, y1, x0, x1), rois)
            ry0, _, rx0, _ = roi_bbox
            patches.append({
                "name": f"P{idx}",
                "roi_name": roi_name,
                "bbox_fullres": [y0, y1, x0, x1],
                "bbox_local": [y0 - ry0, y1 - ry0, x0 - rx0, x1 - rx0],
                "coords": [y0, y1, x0, x1],
            })
        return patches

    def _ensure_empty_corrected_zarr(self, zarr_path, rois):
        if os.path.exists(zarr_path):
            shutil.rmtree(zarr_path, ignore_errors=True)
        out_dir = os.path.dirname(zarr_path) or self.output_dir
        os.makedirs(out_dir, exist_ok=True)
        root = zarr.open_group(zarr_path, mode="w")
        root.attrs["mode"] = "roi_only"
        root.attrs["source_ome"] = os.path.abspath(self.ome_path)
        root.attrs["output_dir"] = os.path.abspath(out_dir)
        if self._roi_context:
            root.attrs["roi_id"] = self._roi_context.get("roi_id", "")
            root.attrs["roi_dir"] = os.path.abspath(self._roi_context.get("roi_dir", ""))
        root.attrs["roi_names"] = [r.get("name", f"ROI_{i}") for i, r in enumerate(rois, start=1)]
        root.attrs["created_by"] = "Step0"
        for idx, roi in enumerate(rois, start=1):
            name = str(roi.get("name") or f"ROI_{idx}")
            group = root.create_group(name, overwrite=True)
            group.attrs["roi_name"] = name
            group.attrs["bbox_fullres"] = roi.get("bbox_fullres") or []
            group.attrs["polygon_fullres"] = roi.get("polygon_fullres") or []
            group.attrs["shape"] = roi.get("shape") or self._roi_shape_from_bbox(roi.get("bbox_fullres"))

    def _write_step0_handoff(self, config, zarr_path):
        step0_dir = os.path.dirname(zarr_path) if zarr_path else (
            self._roi_context["step_dirs"]["step0"] if self._roi_context else self.output_dir
        )
        os.makedirs(step0_dir, exist_ok=True)
        config = self._clean_correction_config(config)
        rois = self._standard_rois()
        patches = self._standard_patches(rois)
        corr_path = os.path.join(step0_dir, "correction_config.json")
        roi_path = os.path.join(step0_dir, "roi_config.json")
        patch_path = os.path.join(step0_dir, "patch_config.json")
        corrected_path = zarr_path or os.path.join(step0_dir, "corrected_channels.zarr")
        manifest_path = os.path.join(step0_dir, "step0_roi_result.json")
        roi_id = self._roi_context.get("roi_id", "") if self._roi_context else ""
        roi_dir = self._roi_context.get("roi_dir", "") if self._roi_context else ""
        project_dir = self._roi_context.get("project_dir", self.output_dir) if self._roi_context else self.output_dir

        print("[Step0] writing ROI-specific outputs")
        print(f"[Step0] roi_id={roi_id}")
        print(f"[Step0] step0_dir={step0_dir}")
        with open(corr_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        with open(roi_path, "w", encoding="utf-8") as f:
            json.dump(rois, f, indent=2, ensure_ascii=False)
        with open(patch_path, "w", encoding="utf-8") as f:
            json.dump(patches, f, indent=2, ensure_ascii=False)

        if not os.path.exists(corrected_path):
            self._ensure_empty_corrected_zarr(corrected_path, rois)

        if os.path.exists(corrected_path):
            try:
                root = zarr.open_group(corrected_path, mode="a")
                root.attrs["mode"] = "roi_only"
                root.attrs["source_ome"] = os.path.abspath(self.ome_path)
                root.attrs["output_dir"] = os.path.abspath(step0_dir)
                root.attrs["project_output_dir"] = os.path.abspath(project_dir)
                root.attrs["roi_id"] = roi_id
                root.attrs["roi_dir"] = os.path.abspath(roi_dir) if roi_dir else ""
                root.attrs["roi_names"] = [r.get("name", f"ROI_{i}") for i, r in enumerate(rois, start=1)]
                root.attrs["created_by"] = "Step0"
                for roi in rois:
                    name = str(roi.get("name") or "")
                    if name and name in root:
                        group = root[name]
                        group.attrs["roi_name"] = name
                        group.attrs["bbox_fullres"] = roi.get("bbox_fullres") or []
                        group.attrs["polygon_fullres"] = roi.get("polygon_fullres") or []
                        group.attrs["shape"] = roi.get("shape") or self._roi_shape_from_bbox(roi.get("bbox_fullres"))
            except Exception as e:
                print(f"[Step0] failed to update corrected zarr attrs: {e}")

        manifest = {
            "version": "v5_roi_handoff_1",
            "roi_id": roi_id,
            "display_name": rois[0]["name"] if rois else "",
            "mode": "roi_only",
            "project_output_dir": os.path.abspath(project_dir),
            "roi_dir": os.path.abspath(roi_dir) if roi_dir else "",
            "step0_dir": os.path.abspath(step0_dir),
            "output_dir": os.path.abspath(step0_dir),
            "raw_ome_path": os.path.abspath(self.ome_path),
            "nucleus_channel": self.nucleus_channel,
            "corrected_zarr_path": os.path.abspath(corrected_path),
            "correction_config_path": os.path.abspath(corr_path),
            "roi_config_path": os.path.abspath(roi_path),
            "patch_config_path": os.path.abspath(patch_path),
            "active_roi": rois[0]["name"] if rois else "",
            "bbox_fullres": rois[0].get("bbox_fullres", []) if rois else [],
            "shape": rois[0].get("shape", []) if rois else [],
            "n_rois": len(rois),
            "n_patches": len(patches),
        }
        manifest["step0_roi_result_path"] = os.path.abspath(manifest_path)
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        print(f"[Step0] correction_config={corr_path}")
        print(f"[Step0] roi_config={roi_path}")
        print(f"[Step0] patch_config={patch_path}")
        print(f"[Step0] corrected_zarr={corrected_path}")
        print(f"[Step0] step0_roi_result={manifest_path}")
        if roi_id and project_dir:
            try:
                mark_roi_step(project_dir, roi_id, "step0", "done")
            except Exception as e:
                print(f"[Step0] failed to update ROI index: {e}")
        return config, rois, patches, manifest

    def _emit_complete(self, config, zarr_path, decisions):
        self._btn_continue.setEnabled(True)
        self._btn_load.setEnabled(True)
        try:
            config, rois, patches, manifest = self._write_step0_handoff(config, zarr_path)
        except Exception as e:
            rois = list(self.rois)
            patches = [{"coords": p} for p in self.patches]
            manifest = {}
            print(f"[Step0] Auto-save ROI failed: {e}")
        payload = {
            "loader": self.loader,
            "patches": list(self.patches),
            "rois": list(rois),
            "correction_config": config,
            "corrected_zarr_path": manifest.get("corrected_zarr_path", zarr_path),
            "output_dir": manifest.get("step0_dir", self.output_dir),
            "project_output_dir": manifest.get("project_output_dir", self.output_dir),
            "roi_id": manifest.get("roi_id", ""),
            "roi_dir": manifest.get("roi_dir", ""),
            "step0_dir": manifest.get("step0_dir", ""),
            "step1_dir": (
                self._roi_context["step_dirs"]["step1"]
                if self._roi_context else ""
            ),
            "ome_tiff_path": self.ome_path,
            "panel_csv_path": self.panel_csv_path,
            "panel_groups": dict(self.panel_groups),
            "panel_nucleus": self.nucleus_channel,
            "corrected_decisions": dict(decisions),
            "step0_manifest_path": manifest.get("step0_roi_result_path", os.path.join(self.output_dir, "step0_roi_result.json")),
        }
        self.step0_complete.emit(payload)
