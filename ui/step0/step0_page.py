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
    stamp_corrected_zarr_provenance,
    corrected_zarr_report,
    CORRECTED_ZARR_OUTPUT_KIND,
    CREATED_FROM_STEP0_BACKGROUND_CORRECTION,
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
    read_corrected_zarr_state,
)
from ...utils.roi_project import (
    create_roi_context,
    create_full_wsi_context,
    mark_roi_step,
    roi_shape_from_bbox,
)
# v14.1b: Step0 hosts the shared ChannelWorkbench as its Channel Conditioning /
# Remap tab (the third host alongside Step1.5 creator + Step3 reviewer). GUI-only
# — these are the same UI-local schema/widget modules Step1.5 used; no promotion /
# resolver / Step2-runtime import is introduced here.
from ..widgets.channel_workbench import ChannelWorkbench
from ..widgets.tissue_navigator_popup import TissueNavigatorPopup
from .roi_context_model import RoiContextModel
from ...utils.channel_remap_config import (
    save_channel_remap_config,
    CREATED_FROM_STEP0_CONDITIONING,
)
# v14.5b: source-aware preview-config primitives (schema + preview-time identity
# reader). Pure/Qt-free; NOT the Step2 resolver or promotion.
from ...utils.source_identity import (
    REQUESTED_SOURCE_RAW_OME,
    REQUESTED_SOURCE_CORRECTED_ZARR,
    DEFAULT_CAMP_SOURCE_POLICY,
    validate_calibration_source_identity,
)
from ...utils.calibration_source import (
    resolve_channel_calibration,
    source_mixture_mode_from_identities,
    open_corrected_channel_array,
    SourceAwareIdentityError,
)

# (#6) Channel Conditioning keeps marker channels + DAPI only. Mask / fusion
# product channels are non-conditioning and excluded structurally — by known
# non-marker keyword in the channel name, NOT by a hardcoded marker whitelist —
# so any present/future product layer is dropped while every real marker stays.
_NON_MARKER_CHANNEL_KEYWORDS = ("mask", "fusion")


def _is_non_marker_channel(name):
    """True if a channel name denotes a non-conditioning product (mask/fusion)."""
    low = str(name).lower()
    return any(kw in low for kw in _NON_MARKER_CHANNEL_KEYWORDS)


class PreloadWorker(QThread):
    """Background reader: loads every (patch × channel) tile into the host's
    conditioning preload cache so patch-switch / All-toggle are zero-IO.

    Emits one channel_loaded(gen, patch_idx, name, array) per tile and
    finished_gen(gen) at the end. Cancellable between reads. `gen` lets the host
    discard a stale (cancelled) worker's late signals after patches change.
    Arrays cross threads via the signal payload (queued, thread-safe) — the
    worker never writes the host cache directly.
    """

    channel_loaded = pyqtSignal(int, int, str, object)   # gen, patch_idx, name, arr
    finished_gen = pyqtSignal(int)                        # gen

    def __init__(self, loader, patches, channels, gen, parent=None):
        super().__init__(parent)
        self._loader = loader
        self._patches = list(patches)
        self._channels = list(channels)
        self._gen = int(gen)
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        for pidx, bbox in enumerate(self._patches):
            if self._cancelled:
                return
            try:
                y0, y1, x0, x1 = bbox
            except Exception:
                continue
            for ch in self._channels:
                if self._cancelled:
                    return
                try:
                    arr = self._loader.read_region(ch, y0, y1, x0, x1,
                                                   normalize=False)
                    arr = np.asarray(arr, dtype=np.float32)
                    if arr.ndim == 3 and arr.shape[2] == 1:
                        arr = arr[:, :, 0]
                except Exception:
                    continue                 # a bad read never kills the preload
                self.channel_loaded.emit(self._gen, pidx, ch, arr)
        if not self._cancelled:
            self.finished_gen.emit(self._gen)


class Step0Page(QWidget):
    step0_complete = pyqtSignal(dict)

    # Per-channel BG method / decision -> combo index (TopHat/cucim/Both/Original).
    _METHOD_IDX = {"tophat": 0, "cucim": 1, "both": 2, "original": 3}

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
        # Conditioning preload: background QThread caches ALL patches × ALL
        # channels so patch-switch / All-toggle are zero-IO. _preload_gen tags the
        # active worker so a cancelled (stale) worker's late signals are ignored.
        self._preload_cache = {}      # {patch_idx: {channel_name: 2D float32}}
        self._preload_worker = None
        self._preload_gen = 0
        # (#4) Patch-LOCAL conditioning viewport (zoom/pan), keyed by patch bbox.
        # Remap params stay channel-global; only the viewer view is per patch.
        self._conditioning_patch_viewports = {}
        # v14.2b: single authoritative ROI/context model. Both the Step0 overview
        # and the TissueNavigatorPopup overview are views/editors over this one
        # model; panel _rois/_patches are render caches derived from it.
        self._roi_model = RoiContextModel()
        self._roi_sync_guard = False  # re-entrancy guard for cross-panel render
        # v14.5b: per-channel SourceRequest for Channel Conditioning. Default all
        # channels to raw OME; corrected is opt-in per channel. Visible per-channel
        # selector UI is deferred — this map is the internal/test-hook entry point
        # (see set_channel_source_request). CalibrationSourceIdentity is never read
        # from this map; it is derived from the actual opened pixel source at save.
        self._channel_source_requests = {}
        self._tissue_navigator_popup = None  # v14.2a: lazily created on first toggle
        # Per-load guard for auto-opening the Tissue Navigator on data load:
        # re-armed at the start of each load, fired once at load-completion.
        self._navigator_auto_opened = False
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
        self._roi_context_sig = None     # (#1) analysis-region identity for reuse
        self._project_output_dir = OUTPUT_DIR
        self._analysis_region_mode = "roi"
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
        # Per-channel param overrides: {ch: {"tophat_radius": int, "cucim_sigma": int}}.
        # Absent -> the channel uses the global Method Parameters values. Lets each
        # channel be re-tuned independently (Per-Channel Decision box).
        self._channel_params: dict = {}
        # Guard: True while _update_decision_ui programmatically loads the Decision
        # widgets (so their signals don't fire preview/dirty during a load).
        self._loading_decision: bool = False
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

        fb.addStretch()
        # v14.2a: toggle the floating Tissue Preview / ROI Navigator popup.
        self._btn_tissue_nav = QPushButton("🗺 Tissue Navigator")
        self._btn_tissue_nav.setToolTip(
            "Show/hide the floating Tissue Preview / ROI Navigator popup.")
        self._btn_tissue_nav.setStyleSheet(
            "QPushButton{font-size:10px;color:#8cf;border:1px solid #8cf;"
            "border-radius:3px;padding:2px 8px;}"
            "QPushButton:hover{background:#1a2a4a;}")
        self._btn_tissue_nav.clicked.connect(self.toggle_tissue_navigator)
        fb.addWidget(self._btn_tissue_nav)

        outer.addWidget(file_bar)   # Section A 固定高度，不拉伸

        # ══ Section B + C — 左右分栏，撑满剩余空间 ════════════════════
        main_split = QSplitter(Qt.Horizontal)
        main_split.setStyleSheet("QSplitter::handle{background:#333;width:3px;}")
        main_split.setChildrenCollapsible(False)
        self._main_split = main_split   # 保存引用，showEvent里固定比例

        # v14.1b: Step0 main workarea = two tabs.
        #   Tab 1 "Background Correction"        — the existing Step0 correction UI
        #   Tab 2 "Channel Conditioning / Remap" — the migrated Step1.5 conditioning
        #                                          surface, reusing ChannelWorkbench.
        self._step0_tabs = QtWidgets.QTabWidget()
        self._step0_tabs.addTab(main_split, "Background Correction")
        self._cond_tab = self._build_step0_conditioning_tab()
        self._cond_tab_index = self._step0_tabs.addTab(
            self._cond_tab, "Channel Remap")
        self._step0_tabs.currentChanged.connect(self._on_step0_tab_changed)
        outer.addWidget(self._step0_tabs, stretch=1)   # 占用所有剩余高度
        # Keep the BG-tab left column (channel list + params) and the Remap-tab left
        # column (Channels + Intensity) the SAME width + position, and draggable: a
        # drag on either syncs the other. Deferred: Section C (which builds _bg_c_split)
        # is constructed after this point, so wire it once the whole page exists.
        QtCore.QTimer.singleShot(0, self._wire_left_column_sync)

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

        # Analysis-region selector (ROI vs Full WSI). Built here but NOT added to
        # sec_b: sec_b is hidden (#10 relocation), and choosing ROI-vs-full-WSI is
        # logically tied to ROI drawing, which now lives in the Tissue Navigator
        # popup. The selector is wrapped in its own container and handed to the
        # popup in _ensure_tissue_navigator. Its handler/signals are unchanged.
        self._region_selector = QWidget()
        rs_lay = QVBoxLayout(self._region_selector)
        rs_lay.setContentsMargins(0, 0, 0, 0)
        rs_lay.setSpacing(4)
        region_row = QHBoxLayout()
        region_row.addWidget(QLabel("Analysis region:"))
        self._analysis_region_combo = QComboBox()
        self._analysis_region_combo.addItems(["ROI selection", "Full WSI"])
        self._analysis_region_combo.currentIndexChanged.connect(self._on_analysis_region_changed)
        region_row.addWidget(self._analysis_region_combo, stretch=1)
        self._btn_use_full_wsi = QPushButton("Use full image")
        self._btn_use_full_wsi.setToolTip("Switch to Full WSI mode. ROI drawing is not required.")
        self._btn_use_full_wsi.clicked.connect(lambda: self._analysis_region_combo.setCurrentIndex(1))
        region_row.addWidget(self._btn_use_full_wsi)
        rs_lay.addLayout(region_row)
        # (navigator-layout) The "Full WSI mode: the entire image will be
        # processed." banner was removed as redundant clutter — the region
        # dropdown already conveys the mode (and the overview status line echoes
        # it). _on_analysis_region_changed no longer references a message label.

        # ROI/Patch drawing toolbar + ROI/Patch lists. Like the region selector,
        # these are built here but NOT added to the hidden sec_b: they belong with
        # the ROI drawing surface, which now lives in the Tissue Navigator popup.
        # Wrapped in a container handed to the popup via set_roi_toolbar. Handlers
        # stay on self.overview; the v14.2b bridge mirrors edits to the popup
        # overview. _set_draw_mode is routed to the visible (popup) overview.
        self._roi_patch_toolbar = QWidget()
        tb_lay = QVBoxLayout(self._roi_patch_toolbar)
        tb_lay.setContentsMargins(0, 0, 0, 0)
        tb_lay.setSpacing(4)

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
        tb_lay.addLayout(tool_row)

        # Overview（DAPI thumbnail + patch 绘制）
        _dummy_loader = type("_DummyLoader", (), {
            "shape": (0, 0), "ch_map": {}, "channel_names": lambda s: []
        })()
        self.overview = OverviewPanel(_dummy_loader, self.nucleus_channel, lazy=True)
        self.overview.full_wsi_mode = False
        self.overview.patches_changed.connect(self._on_patches_changed)
        self.overview.rois_changed.connect(self._on_rois_changed)
        # v14.2b: also adopt Step0-overview edits into the single ROI model and
        # mirror them to the popup overview (kept as a separate slot so existing
        # Step0-local UI handlers above stay unchanged).
        self.overview.patches_changed.connect(
            lambda *_: self._reconcile_roi_edit(self.overview))
        self.overview.rois_changed.connect(
            lambda *_: self._reconcile_roi_edit(self.overview))
        self._wrap_overview_patch_limit()
        bl.addWidget(self.overview, stretch=3)   # overview 占大部分高度

        # (navigator-layout) ROI/Patch LISTS live in their own container, hosted
        # BELOW the overview in the popup (overview 3/5, ROI list 1/5, Patch list
        # 1/5). The mode-switch toolbar (_roi_patch_toolbar) stays above the
        # overview. Both are handed to the popup; sec_b stays hidden.
        self._roi_patch_lists = QWidget()
        ll_lay = QVBoxLayout(self._roi_patch_lists)
        ll_lay.setContentsMargins(0, 0, 0, 0)
        ll_lay.setSpacing(4)

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
        ll_lay.addLayout(roi_hdr)

        self._roi_list = QtWidgets.QListWidget()
        self._roi_list.setSelectionMode(
            QtWidgets.QAbstractItemView.ExtendedSelection)
        self._roi_list.setStyleSheet(
            "QListWidget{background:#111;border:1px solid #333;border-radius:3px;font-size:10px;}"
            "QListWidget::item:selected{background:#1f3a2a;}"
        )
        self._roi_list.itemSelectionChanged.connect(self._on_roi_selection_changed)
        ll_lay.addWidget(self._roi_list, stretch=1)   # ROI list ~1/5 of popup

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
        ll_lay.addLayout(patch_hdr)

        self._patch_list = QtWidgets.QListWidget()
        self._patch_list.setSelectionMode(
            QtWidgets.QAbstractItemView.ExtendedSelection)
        self._patch_list.setStyleSheet(
            "QListWidget{background:#111;border:1px solid #333;border-radius:3px;font-size:10px;}"
            "QListWidget::item:selected{background:#2b1f2f;}"
        )
        self._patch_list.itemSelectionChanged.connect(self._on_patch_selection_changed)
        ll_lay.addWidget(self._patch_list, stretch=1)   # Patch list ~1/5 of popup

        self._patch_warning = QLabel("")
        self._patch_warning.setStyleSheet("color:#ffb86c;font-size:10px;font-weight:bold;")
        self._patch_warning.setVisible(False)
        ll_lay.addWidget(self._patch_warning)

        # (#10) Section B "ROI & Patch" — the tissue preview + ROI/patch drawing
        # (self.overview) — is the SAME component the Tissue Navigator was derived
        # from. It is NO LONGER rendered inside the Background Correction tab; it
        # lives in the Tissue Navigator popup, whose OverviewPanel is a view over
        # the SAME RoiContextModel (v14.2b). sec_b's widgets (self.overview,
        # _roi_list, _patch_list, region combo) are KEPT as the Step0-side model
        # views the v14.2b bridge reconciles — just not shown in the BG layout.
        # main_split therefore holds only Section C (correction + Preview Patch).
        self._roi_patch_section = sec_b   # keep a ref so the orphan isn't GC'd
        sec_b.setVisible(False)

        # ── Section C（右 75%）— Background Correction ────────────────
        sec_c = QWidget()
        sec_c.setStyleSheet("background:#1c1c1c;")
        cl = QVBoxLayout(sec_c)
        cl.setContentsMargins(4, 4, 4, 4)
        cl.setSpacing(4)

        # (redundant "C — Background Correction" title removed: the tab is already
        # named "Background Correction"; the freed vertical space goes entirely to
        # c_split below — Channels/params/patch on the left, the Original|Tophat|
        # cucim Patch Preview on the right.)
        # Section C 内部：左（通道列表+参数+patch选择） / 右（三联预览+metrics+决策）
        c_split = QSplitter(Qt.Horizontal)
        c_split.setStyleSheet("QSplitter::handle{background:#333;width:3px;}")
        self._bg_c_split = c_split   # synced with the Remap tab's left column
        cl.addWidget(c_split, stretch=1)

        # C-左：通道列表 + 参数滑块 + patch 选择. Capped narrow so the Channels
        # list matches the Channel Remap tab's left-column width; the freed width
        # goes to the triple Patch Preview on the right.
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
        self._cb_all = QtWidgets.QCheckBox("All")
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
        all_row.addWidget(self._method_all)   # "Method:" label dropped (combo is clear)
        chl.addLayout(all_row)

        # 分隔线
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color:#333;")
        chl.addWidget(sep)

        # v15: shared ChannelDock mounted through an adapter. The dock's inner
        # QListWidget is exposed as self._channel_list so every legacy code
        # path (setCurrentItem / currentRowChanged / item registry) is intact.
        from .step0_dock_adapter import Step0ChannelDockAdapter
        self._dock_adapter = Step0ChannelDockAdapter(self)
        self._channel_list = self._dock_adapter.dock.list_widget
        self._channel_list.currentRowChanged.connect(self._on_channel_row_changed)
        chl.addWidget(self._dock_adapter.dock, stretch=1)
        cll.addWidget(ch_box, stretch=2)

        # ── Method Parameters ─────────────────────────────────────────
        # Compact: one numeric INPUT box per method (no sliders, no separate
        # value/hint labels) — hints live in tooltips. Halves the vertical space.
        method_box = QGroupBox("Method Parameters")
        method_box.setStyleSheet(self._box_style("#e5c07b"))
        ml = QVBoxLayout(method_box)
        ml.setContentsMargins(6, 4, 6, 4)
        ml.setSpacing(3)

        def _param_input(rng, default, tip):
            sb = QtWidgets.QSpinBox()
            sb.setRange(int(rng[0]), int(rng[1]))
            sb.setValue(int(default))
            sb.setButtonSymbols(QtWidgets.QAbstractSpinBox.NoButtons)  # pure input box
            sb.setAlignment(Qt.AlignRight)
            sb.setFixedWidth(72)
            sb.setToolTip(tip)
            sb.setStyleSheet(
                "QSpinBox{background:#1a1a1a;color:#ddd;border:1px solid #444;"
                "border-radius:3px;padding:1px 5px;font-size:11px;}"
            )
            return sb

        def _param_row(text, widget, tip):
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            lbl = QLabel(text)
            lbl.setToolTip(tip)
            lbl.setStyleSheet("color:#ddd;font-size:11px;")
            row.addWidget(lbl)
            row.addStretch()
            row.addWidget(widget)
            ml.addLayout(row)

        # Names kept as *_slider for API/back-compat (QSpinBox is a drop-in:
        # value()/setValue()/valueChanged/blockSignals all match QSlider).
        self._tophat_slider = _param_input(
            TOPHAT_RADIUS_RANGE, TOPHAT_RADIUS_DEFAULT,
            "TopHat disk radius (px) — roughly 0.5–1.5× cell diameter")
        self._tophat_slider.valueChanged.connect(self._on_slider_changed)
        self._cucim_slider = _param_input(
            CUCIM_SIGMA_RANGE, CUCIM_SIGMA_DEFAULT,
            "cucim Gaussian sigma (px) — larger sigma estimates broader background")
        self._cucim_slider.valueChanged.connect(self._on_slider_changed)
        _param_row("TopHat radius:", self._tophat_slider,
                   "TopHat disk radius (px) — roughly 0.5–1.5× cell diameter")
        _param_row("cucim sigma:", self._cucim_slider,
                   "cucim Gaussian sigma (px) — larger sigma estimates broader background")

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

        # Run controls folded INTO Method Parameters (the params ARE the run's
        # inputs). Process = first run; becomes Re-process only after a completed
        # run when params change (see _on_slider_changed / _on_batch_all_done).
        _sep = QFrame()
        _sep.setFrameShape(QFrame.HLine)
        _sep.setStyleSheet("color:#333;")
        ml.addWidget(_sep)

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
        ml.addLayout(proc_btn_row)

        self._proc_pbar = QProgressBar()
        self._proc_pbar.setRange(0, 100)
        self._proc_pbar.setValue(0)
        self._proc_pbar.setVisible(False)
        self._proc_pbar.setFixedHeight(14)
        self._proc_pbar.setStyleSheet(
            "QProgressBar{border:1px solid #4a9;border-radius:3px;background:#111;}"
            "QProgressBar::chunk{background:#4a9;border-radius:2px;}"
        )
        ml.addWidget(self._proc_pbar)

        self._proc_status = QLabel("Select channels and click Process.")
        self._proc_status.setWordWrap(True)
        self._proc_status.setStyleSheet("color:#aaa;font-size:10px;")
        ml.addWidget(self._proc_status)

        cll.addWidget(method_box)

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
        # (#4) patch_box (Preview Patch) is NOT added to c_left — it moves to
        # c_right's bottom_row (next to the shrunk Quantitative Metrics). c_left's
        # Channels panel (stretch=2) absorbs the freed vertical space.

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
        # (#4) Metrics shrinks from 1/2 to 1/3 of bottom_row: it shares the row
        # equally with the relocated Preview Patch and the Decision panel.
        bottom_row.addWidget(metrics_box, stretch=1)
        # (#4) Preview Patch relocated here (was in c_left) — into the space freed
        # by shrinking Metrics. Its P-buttons + _patch_info + wiring are intact.
        bottom_row.addWidget(patch_box, stretch=1)

        decision_box = QGroupBox("Per-Channel Decision")
        decision_box.setStyleSheet(self._box_style("#e06c75"))
        dl = QVBoxLayout(decision_box)
        dl.setContentsMargins(6, 4, 6, 4)
        dl.setSpacing(3)

        # Per-channel params (override the global Method Parameters for THIS
        # channel). Only the field matching the chosen method is enabled.
        param_row = QHBoxLayout()
        param_row.setContentsMargins(0, 0, 0, 0)
        self._dec_radius = QtWidgets.QSpinBox()
        self._dec_radius.setRange(int(TOPHAT_RADIUS_RANGE[0]), int(TOPHAT_RADIUS_RANGE[1]))
        self._dec_radius.setValue(TOPHAT_RADIUS_DEFAULT)
        self._dec_sigma = QtWidgets.QSpinBox()
        self._dec_sigma.setRange(int(CUCIM_SIGMA_RANGE[0]), int(CUCIM_SIGMA_RANGE[1]))
        self._dec_sigma.setValue(CUCIM_SIGMA_DEFAULT)
        for sb, tip in ((self._dec_radius, "TopHat disk radius (px) for this channel"),
                        (self._dec_sigma, "cucim Gaussian sigma (px) for this channel")):
            sb.setButtonSymbols(QtWidgets.QAbstractSpinBox.NoButtons)
            sb.setAlignment(Qt.AlignRight)
            sb.setFixedWidth(56)
            sb.setToolTip(tip)
            sb.setStyleSheet(
                "QSpinBox{background:#1a1a1a;color:#ddd;border:1px solid #444;"
                "border-radius:3px;padding:1px 4px;font-size:11px;}"
                "QSpinBox:disabled{color:#555;border-color:#2a2a2a;}"
            )
            sb.setKeyboardTracking(False)   # valueChanged once per committed edit
            sb.valueChanged.connect(self._on_dec_param_changed)   # persist only
            sb.lineEdit().returnPressed.connect(self._on_dec_param_entered)  # Enter -> run
        _rl = QLabel("radius:"); _rl.setStyleSheet("color:#ddd;font-size:11px;")
        _sl = QLabel("sigma:");  _sl.setStyleSheet("color:#ddd;font-size:11px;")
        param_row.addWidget(_rl); param_row.addWidget(self._dec_radius)
        param_row.addSpacing(8)
        param_row.addWidget(_sl); param_row.addWidget(self._dec_sigma)
        param_row.addStretch()
        dl.addLayout(param_row)

        self._decision_group = QButtonGroup(self)
        self._dec_top  = QRadioButton("TopHat")
        self._dec_cu   = QRadioButton("cucim")
        self._dec_orig = QRadioButton("Original")
        self._dec_orig.setChecked(True)
        rb_row = QHBoxLayout()
        for rb in (self._dec_top, self._dec_cu, self._dec_orig):
            self._decision_group.addButton(rb)
            rb.setStyleSheet("font-size:11px;")
            rb.toggled.connect(self._on_dec_method_toggled)
            rb_row.addWidget(rb)
        dl.addLayout(rb_row)

        btn_row = QHBoxLayout()
        self._dec_process_btn = QPushButton("Process")
        self._dec_process_btn.setToolTip(
            "Run this channel's params over ALL its patches now (incremental).")
        self._dec_process_btn.setStyleSheet(
            "QPushButton{background:#1a5c2a;color:#6bffa0;border-radius:4px;"
            "padding:5px 10px;font-weight:bold;}"
            "QPushButton:hover{background:#2a7c3a;}"
            "QPushButton:disabled{background:#333;color:#555;}"
        )
        self._dec_process_btn.clicked.connect(self._process_current_channel)
        self._apply_btn = QPushButton("Apply")
        self._apply_btn.setToolTip("Save this channel's method + params (no run).")
        self._apply_btn.setStyleSheet(
            "QPushButton{background:#255;color:white;border-radius:4px;"
            "padding:5px 12px;font-weight:bold;}"
            "QPushButton:hover{background:#377;}"
            "QPushButton:disabled{background:#333;color:#555;}"
        )
        self._apply_btn.clicked.connect(self._apply_current_channel_decision)
        btn_row.addWidget(self._dec_process_btn, stretch=1)
        btn_row.addWidget(self._apply_btn, stretch=1)
        dl.addLayout(btn_row)

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

        # (#5) ONE BG-tab Save button — replaces BOTH the old "Run BG correction"
        # preview-batch button AND the page-level "Save Step0" footer. The handler
        # _save_and_continue already does the FULL pipeline: run WsiCorrectionWorker
        # on assigned channels -> write corrected_channels.zarr -> write
        # correction/roi/patch configs + step0_roi_result.json -> emit
        # step0_complete (Step0->Step1 handoff). The per-patch preview-batch button
        # was dropped (its preview duty is not part of the save pipeline).
        save_row = QHBoxLayout()
        save_row.addStretch()
        self._btn_continue = QPushButton("Save")
        self._btn_continue.setToolTip(
            "Run background correction on assigned channels, write "
            "corrected_channels.zarr + the Step0->Step1 handoff, and mark Step0 "
            "complete. Navigate via the step names.")
        self._btn_continue.setStyleSheet(
            "QPushButton{background:#2a5;color:white;border-radius:4px;"
            "padding:8px 22px;font-size:13px;font-weight:bold;}"
            "QPushButton:hover{background:#3b6;}"
        )
        self._btn_continue.setFixedHeight(38)   # unify with the Remap-tab Save
        self._btn_continue.clicked.connect(self._save_and_continue)
        save_row.addWidget(self._btn_continue)
        cl.addLayout(save_row)

        # v14.4: explicit corrected-output status — honest about whether the last
        # Save wrote a VALID non-empty corrected_channels.zarr.
        self._bg_corrected_status = QLabel(
            "corrected_channels.zarr: not written yet.")
        self._bg_corrected_status.setStyleSheet("color:#888;font-size:11px;")
        cl.addWidget(self._bg_corrected_status)

        main_split.addWidget(sec_c)
        # (#10) Section C is the sole child of the BG splitter (Section B relocated
        # to the Tissue Navigator). No page-level Save footer anymore (#5).

        self._refresh_slider_labels()

    # ── v14.1b Channel Conditioning / Remap (migrated from Step1.5) ───────────
    #  Step0 is the v14 host for pre-segmentation channel conditioning. It reuses
    #  the shared ChannelWorkbench and Step0's OWN context (self.loader + current
    #  patch + self._channel_order + self.nucleus_channel) — the same context
    #  pieces the old Step1.5 page received via set_context. Configs stay
    #  preview_only (step2_ready=false); promotion to Step2-ready is a v14.5 phase.

    def _build_step0_conditioning_tab(self):
        w = QWidget()
        # Match the Background Correction tab's darker background (#1c1c1c) instead
        # of the default gray.
        w.setStyleSheet("background:#1c1c1c;")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)   # match the BG tab so the left edges align

        # Preview-patch selector for the conditioning view. The BG tab's P1/P2/…
        # buttons are not visible from this tab, so mirror them here. Both rows are
        # rebuilt by _rebuild_patch_buttons and drive the same _select_patch (which
        # refreshes the conditioning data for the chosen patch).
        patch_sel_row = QHBoxLayout()
        patch_sel_row.setSpacing(4)
        psl = QLabel("Preview Patch:")
        psl.setStyleSheet("color:#98c379;font-size:10px;font-weight:bold;")
        patch_sel_row.addWidget(psl)
        self._cond_patch_buttons_row = QHBoxLayout()
        self._cond_patch_buttons_row.setSpacing(4)
        patch_sel_row.addLayout(self._cond_patch_buttons_row)
        patch_sel_row.addStretch()
        # Fit view + Validate config live here (top row, right of the patch buttons),
        # not inside the workbench — frees the workbench height for the panes. They
        # drive the workbench's public actions (workbench built just below).
        _btn_fit = QPushButton("Fit view")
        _btn_fit.setToolTip("Reset zoom/pan to fit the patch.")
        _btn_fit.clicked.connect(lambda: self._cond_workbench.fit_view())
        patch_sel_row.addWidget(_btn_fit)
        _btn_val = QPushButton("Validate config")
        _btn_val.setToolTip("Validate the current per-channel remap config.")
        _btn_val.clicked.connect(lambda: self._cond_workbench.validate_config())
        patch_sel_row.addWidget(_btn_val)
        # NOT added to the tab column: this bar goes into the workbench's center
        # (above the image) so the left Channels column rises to the top and its
        # border aligns with the BG tab's Channels border.
        patch_sel_row.setContentsMargins(0, 0, 0, 2)
        _patch_bar = QWidget()
        _patch_bar.setLayout(patch_sel_row)

        # (#6/#8) Step0 conditioning: DAPI is a normal channel (no reference
        # overlay) and fusion participation is Step1's call (no per-channel
        # Enabled checkbox). Both shared-widget surfaces are turned off here;
        # Step1.5 / Step3 keep them.
        self._cond_workbench = ChannelWorkbench(
            show_reference_bar=False, show_enabled_checkbox=False,
            multichannel_overlay=True, show_banner=False,
            step0_intensity_panel=True)
        # Match the Background Correction tab's darker background.
        self._cond_workbench.setStyleSheet("background:#1c1c1c;")
        # Host-agnostic: it asks for data via refresh_requested and we feed it from
        # Step0's own loader/patch. Hide the generic internal save — Step0's
        # "Save remap config (Step0)" below is the only official save path (it
        # stamps the honest preview provenance + registered created_from_step).
        # (#2-cleanup) Hide the manual data-load buttons: Step0 auto-syncs the
        # current patch (+ lazy-load), so host-refresh / demo / file are redundant.
        self._cond_workbench.configure_host_actions(
            show_internal_save=False, show_load_buttons=False,
            show_fit_button=False, show_bottom_bar=False)
        # Put the Preview Patch + Fit/Validate bar over the image (center top), so the
        # Channels column top is not pushed down by it and aligns with the BG tab.
        self._cond_workbench.set_center_top_bar(_patch_bar)
        self._cond_workbench.refresh_requested.connect(self._sync_step0_to_workbench)
        # Lazy-load (#2 perf): patch switch pre-loads ONLY the active channel; the
        # workbench fetches the rest on-demand (when the user selects them) via
        # this provider, which always reads the CURRENT patch.
        self._cond_workbench.set_pixel_provider(self._provide_channel_pixels)
        # v14.2c: when the viewer's viewport settles (debounced), update the
        # Tissue Navigator current-view rectangle.
        self._cond_workbench.viewer.viewport_changed.connect(
            self._update_tissue_view_rect)
        lay.addWidget(self._cond_workbench, stretch=1)
        lay.addSpacing(8)   # small gap so the panes don't sit flush against Save

        bar = QHBoxLayout()
        # (#2-cleanup) The redundant "Load current patch channels" button was
        # removed; data auto-syncs from Step0's current patch (_sync_step0_to_
        # workbench via refresh_requested + on patch load) with lazy-load.
        # (#5b) The Channel Conditioning tab's Save. Same handler / same written
        # preview remap config as before — only the label + styling are formalized
        # to match the BG tab's "Save" (one tab, one Save).
        btn_save = QPushButton('Save')
        btn_save.setToolTip(
            "Save the per-channel preview remap config (preview_only; "
            "step2_ready=false) for this tab.")
        btn_save.setStyleSheet(
            "QPushButton{background:#2a5;color:white;border-radius:4px;"
            "padding:8px 22px;font-size:13px;font-weight:bold;}"
            "QPushButton:hover{background:#3b6;}")
        btn_save.setFixedHeight(38)   # unify with the BG-tab Save
        btn_save.clicked.connect(self._save_step0_remap_config)
        # Right-align Save to match the BG tab's save_row (stretch -> button).
        bar.addStretch()
        bar.addWidget(btn_save)
        lay.addLayout(bar)
        return w

    def _on_step0_tab_changed(self, idx):
        if not hasattr(self, "_step0_tabs"):
            return
        # Key on the tab INDEX, not its title (title was renamed to "Channel
        # Remap"). Entering the remap tab always (re)feeds the workbench from the
        # current patch so it works even when NO background correction was run.
        if idx == getattr(self, "_cond_tab_index", -1) \
                and hasattr(self, "_cond_workbench") \
                and not self._cond_workbench.has_channel_data():
            self._sync_step0_to_workbench()
        # Re-apply the shared channels-column width to the now-visible tab so the
        # left column looks unmoved across the switch.
        now = (self._bg_c_split if idx == 0
               else getattr(getattr(self, "_cond_workbench", None), "_h_split", None))
        if now is not None and getattr(self, "_left_col_width", None):
            QtCore.QTimer.singleShot(0, lambda s=now: self._apply_left_col_width(s))

    def _wire_left_column_sync(self):
        """Bidirectionally sync the BG tab's left column (channel list + params) and the
        Remap tab's left column (Channels + Intensity): a drag on either updates the
        shared width and the other splitter, and a tab switch re-applies it. Draggable,
        no fixed cap — so switching tabs never appears to move the channels column."""
        a = getattr(self, "_bg_c_split", None)
        b = getattr(getattr(self, "_cond_workbench", None), "_h_split", None)
        if a is None or b is None:
            return
        self._left_split_a = a
        self._left_split_b = b
        self._syncing_left_cols = False
        # Start both at the LARGER of the two left-pane minimums (+ a little), so the
        # right borders line up and neither is clamped below its own minimum.
        wa = a.widget(0).minimumSizeHint().width() if a.widget(0) else 0
        wb = b.widget(0).minimumSizeHint().width() if b.widget(0) else 0
        # v15 user feedback: the BG Channels column starts at 2x the previous
        # default width (still draggable and synced with the Remap tab).
        self._left_col_width = 2 * (max(wa, wb, 120) + 4)
        a.splitterMoved.connect(lambda _p, _i: self._on_left_split_dragged(a))
        b.splitterMoved.connect(lambda _p, _i: self._on_left_split_dragged(b))
        self._apply_left_col_width(a)
        self._apply_left_col_width(b)

    def _on_left_split_dragged(self, src):
        if getattr(self, "_syncing_left_cols", False):
            return
        s = src.sizes()
        if len(s) < 2 or s[0] <= 0:
            return
        a = getattr(self, "_left_split_a", None)
        b = getattr(self, "_left_split_b", None)
        self._left_col_width = s[0]
        self._apply_left_col_width(a)
        self._apply_left_col_width(b)
        # Reconcile: if the drag went below the other column's minimum it clamped and
        # they diverged — pin BOTH to the larger actual width so they always match.
        if a is not None and b is not None:
            actual = max(a.sizes()[0], b.sizes()[0])
            if actual != self._left_col_width:
                self._left_col_width = actual
                self._apply_left_col_width(a)
                self._apply_left_col_width(b)

    def _apply_left_col_width(self, split):
        w = getattr(self, "_left_col_width", None)
        if not w or split is None:
            return
        d = split.sizes()
        if len(d) < 2:
            return
        total = sum(d)
        if total <= 0:
            return
        self._syncing_left_cols = True
        try:
            split.setSizes([int(w), max(1, total - int(w))])
        finally:
            self._syncing_left_cols = False

    def _maybe_refresh_conditioning(self):
        """Re-feed the workbench from the current patch, once conditioning is in
        use. Uses a sticky _conditioning_in_use flag rather than has_channel_data
        so that deleting all patches (which clears the workbench) and creating
        new ones still re-populates the conditioning view."""
        wb = getattr(self, "_cond_workbench", None)
        if wb is None:
            return
        if getattr(self, "_conditioning_in_use", False) or wb.has_channel_data():
            self._sync_step0_to_workbench()

    def _read_cond_patch_channel(self, ch, normalize=False):
        """Read one channel's current-patch array via Step0's own loader."""
        if not self.loader or not self.patches:
            return None
        y0, y1, x0, x1 = self.patches[self.current_patch_idx]
        arr = self.loader.read_region(ch, y0, y1, x0, x1, normalize=normalize)
        arr = np.asarray(arr, dtype=np.float32)
        if arr.ndim == 3 and arr.shape[2] == 1:
            arr = arr[:, :, 0]
        return arr

    # ── conditioning preload cache ───────────────────────────────────────────
    def _conditioning_channels(self):
        """Marker channels + DAPI (the set the conditioning workbench shows)."""
        return [ch for ch in self._channel_order if not _is_non_marker_channel(ch)]

    def _provide_channel_pixels(self, name):
        """Pixel provider for the workbench: serve from the preload cache (zero
        IO) when warm, else fall back to a single live read for the current
        patch (lazy-load). Always reads the CURRENT patch."""
        cached = self._preload_cache.get(self.current_patch_idx, {}).get(name)
        if cached is not None:
            return cached
        return self._read_cond_patch_channel(name, normalize=False)

    def _cancel_preload(self):
        w = getattr(self, "_preload_worker", None)
        if w is not None:
            w.cancel()                       # flag; stale signals ignored by gen
            self._preload_worker = None

    def _start_preload(self):
        """(Re)start the background preload of all patches × channels. Cancels any
        running preload and invalidates the cache first (patches changed)."""
        self._cancel_preload()
        self._preload_cache = {}
        if not self.loader or not self.patches:
            return
        channels = self._conditioning_channels()
        if not channels:
            return
        self._preload_gen += 1
        gen = self._preload_gen
        worker = PreloadWorker(self.loader, list(self.patches), channels, gen,
                               parent=self)
        worker.channel_loaded.connect(self._on_preload_channel)
        worker.finished_gen.connect(self._on_preload_finished)
        self._preload_worker = worker
        worker.start()

    def _on_preload_channel(self, gen, patch_idx, name, arr):
        if gen != self._preload_gen:
            return                           # stale worker (patches changed)
        self._preload_cache.setdefault(patch_idx, {})[name] = arr

    def _on_preload_finished(self, gen):
        if gen != self._preload_gen:
            return
        self._preload_worker = None

    def _sync_step0_to_workbench(self):
        """Feed the workbench from Step0's loader + current patch + channel order.

        Marker channels mirror self._channel_order (minus the nucleus/DAPI
        channel). DAPI is supplied as a reference layer only — never a marker.
        Mirrors the old Step1.5 _sync_step15_to_workbench exactly; only the host
        (Step0) and provenance labels differ.
        """
        if not hasattr(self, "_cond_workbench"):
            return
        # Conditioning is engaged: keep refreshing it on patch changes even after
        # a clear (delete-all). Sticky flag read by _maybe_refresh_conditioning.
        self._conditioning_in_use = True
        if not self.loader or not self.patches:
            self._cond_workbench.clear_channel_images()
            return

        # Lazy-load (#2 perf): read ONLY the active marker channel here (one
        # read_region) instead of looping over all ~27. The other channels are
        # passed as None placeholders and fetched on-demand by the workbench's
        # pixel provider the first time the user selects them. Per-patch this
        # rebuild resets _raw, so a stale channel is re-read against the new
        # patch the next time it is activated.
        # (#6) Conditioning list = marker channels + DAPI ONLY. DAPI (the nucleus
        # channel) is now a NORMAL conditionable channel — kept in the list, gets
        # Min/Max/Gamma + a default blue swatch, and enters build_config like any
        # marker. Mask / fusion product channels are non-conditioning and are
        # filtered out structurally (by known non-marker keyword, not a marker
        # whitelist) so they never leak into the list.
        channels = [ch for ch in self._channel_order if not _is_non_marker_channel(ch)]
        if not channels:
            self._cond_workbench.clear_channel_images()
            return
        # Preserve the workbench's current selection (active + which channels are
        # checked/visible) across patch switches. On the FIRST load the overlay
        # opens with ONLY DAPI checked + active (QuPath default state).
        if self._cond_workbench.has_channel_data():
            visible = [c for c in self._cond_workbench.visible_channels()
                       if c in channels] or [self.nucleus_channel]
            active = self._cond_workbench.active_channel()
            if active not in channels:
                active = visible[0]
        else:
            visible = [self.nucleus_channel]
            active = self.nucleus_channel
        # The eager (pre-loaded) channel is the active one; other visible channels
        # are lazy-loaded by the overlay recomposite. Make sure `active` is one we
        # actually read eagerly below.
        if active not in channels:
            active = channels[0]
        # DAPI is traditionally blue in fluorescence — give the nucleus channel a
        # fixed blue swatch; other channels fall back to the workbench palette.
        colors = {self.nucleus_channel: "#3366ff"}
        # Preload integration: serve every channel from the warm cache (zero IO →
        # All-toggle / patch-switch instant). Cold channels stay None (lazy); only
        # the active one is read eagerly so first paint is never blank.
        patch_cache = self._preload_cache.get(self.current_patch_idx, {})
        images, meta = {}, {}
        for ch in channels:
            arr = patch_cache.get(ch)       # warm: real array (no IO)
            if arr is None and ch == active:
                try:
                    a = self._read_cond_patch_channel(ch, normalize=False)
                    if a is not None and a.ndim == 2 and a.size:
                        arr = a
                except Exception as exc:
                    print(f"[Step0] conditioning: skip channel {ch}: {exc}")
            images[ch] = arr            # None => lazy (read on demand)
            # Honest per-channel provenance, independent of pixel data: calibrated
            # from the Step0 patch, NOT proven to match the source Step2 will read.
            meta[ch] = {
                "source": "step0_loader",
                "intensity_space": "raw_ome_native_float",
                "normalization": "none",
                "step2_compatible": False,
                "step2_pre_remap_source": "unknown",
                "calibration_source_matches_step2": False,
                "fallback_reason": "step0_preview_source_unverified",
            }

        # Honest top-level alignment policy: preview_only + step2_ready=false plus
        # calibration_source_matches_step2=false and source_alignment_mode=
        # partial_or_preview_fallback all force Step2 validation to reject this
        # config even with allow_preview_remap=True. Promotion to Step2-ready is a
        # later v14.5 phase (real source path / shape / intensity-space check).
        source_policy = {
            "source": "step0_loader",
            "intensity_space": "raw_ome_native_float",
            "normalization": "none",
            "scope": "step0_pre_segmentation",
            "preview_only": True,
            "step2_ready": False,
            "step2_pre_remap_source": "unknown",
            "calibration_source_matches_step2": False,
            "source_alignment_mode": "partial_or_preview_fallback",
            "alignment_note": (
                "Step0 preview config calibrated from the current patch via "
                "OMETIFFLoader. Source alignment with Step2 has not yet been "
                "verified. This config must not be promoted to Step2-ready until "
                "v14.5 source path, shape, and intensity-space validation succeeds."),
        }
        source_policy.update(self._calibration_source_identity())
        self._cond_workbench.set_channel_images(
            images,
            context={"patch": self.current_patch_idx + 1, "step": "step0"},
            source="manual", source_policy=source_policy, channel_metadata=meta,
            colors=colors, active=active, visible=visible)
        # No separate DAPI reference read: DAPI is a normal channel in `images`
        # above and is lazy-loaded on demand like any other (#2-new).
        print(f"[Step0] conditioning workbench synced: {len(images)} channels "
              f"(markers + DAPI) patch={self.current_patch_idx + 1}")

    def _calibration_source_identity(self):
        """Identity of the source the Step0 workbench actually calibrated on.

        Record-only. The full source geometry is the loader's whole-image shape
        (the image the patch was cropped from), NOT the patch shape. If the loader
        cannot provide path/shape, the value is null and the config simply cannot
        be promoted later — never guessed from ROI defaults.
        """
        path = getattr(self.loader, "filepath", None)
        path = os.path.abspath(path) if path else None
        shp = getattr(self.loader, "shape", None)
        source_shape = None
        if shp and len(tuple(shp)) >= 2 and int(shp[0]) > 0 and int(shp[1]) > 0:
            source_shape = [int(shp[0]), int(shp[1])]  # [H, W]
        bbox = None
        if self.patches and 0 <= self.current_patch_idx < len(self.patches):
            y0, y1, x0, x1 = self.patches[self.current_patch_idx]
            bbox = [int(y0), int(y1), int(x0), int(x1)]
        return {
            "calibration_source_path": path,
            "calibration_source_kind": "raw_ome",
            "calibration_source_shape": source_shape,
            "calibration_intensity_space": "raw_ome_native_float",
            "calibration_patch_bbox": bbox,
            "calibration_patch_index": int(self.current_patch_idx),
        }

    def _step0_conditioning_out_dir(self):
        """Physical storage dir for the Step0 preview remap config.

        Unified with the ROI's Step0 outputs: when a Step0 ROI context exists,
        write the remap config next to corrected_channels.zarr at
        <roi_dir>/step0/ (self._roi_context["step_dirs"]["step0"]). Only when no
        ROI context has been created yet (a bare preview before any Step0
        Save-and-continue) does it fall back to the legacy
        <output_dir>/step1_5/channel_remap_configs/ location.
        """
        ctx = getattr(self, "_roi_context", None)
        if ctx:
            step0_dir = (ctx.get("step_dirs") or {}).get("step0")
            if step0_dir:
                return step0_dir
        base = self.output_dir or OUTPUT_DIR
        return os.path.join(base, "step1_5", "channel_remap_configs")

    def set_channel_source_request(self, channel_name, requested_source):
        """Set a channel's SourceRequest (raw_ome | corrected_zarr). Default raw.

        Internal/test-hook entry point — the visible per-channel selector UI is
        deferred. Records intent only; the actual source identity is derived from
        pixels at save time."""
        self._channel_source_requests[str(channel_name)] = str(requested_source)

    def _corrected_zarr_path(self):
        """Corrected zarr path the loader currently knows about, or None."""
        return getattr(self.loader, "_corrected_zarr_path", None)

    def _corrected_available_channels(self, channel_names):
        """Channels that have REAL corrected pixel arrays in corrected_channels.zarr.

        Grounded in actual array availability, not a stale UI decision: a channel
        counts only if the resolver's own opener can open its corrected array
        (roi_name=None -> scans every ROI group). Uses open_corrected_channel_array
        so availability == resolvability (a channel that would fall back to raw at
        resolve time is NOT reported available -> never defaults to fake corrected).
        """
        corrected_zarr = self._corrected_zarr_path()
        if not corrected_zarr:
            return set()
        avail = set()
        for ch in channel_names:
            if open_corrected_channel_array(corrected_zarr, ch, None) is not None:
                avail.add(ch)
        return avail

    def _apply_source_aware_identity(self, cfg):
        """v14.5b: stamp per-channel SourceRequest + CalibrationSourceIdentity and
        a top-level source_mixture_mode + camp_source_policy into a PREVIEW config.

        SourceRequest = what was asked for (per-channel map, default raw_ome).
        CalibrationSourceIdentity = derived from the ACTUAL opened pixel source
        (Strategy B fallback recorded honestly). source_mixture_mode is derived
        from the actual identities, never from the requests. Never sets step2_ready.

        v14.5b.1 Strategy A — NO partial source-aware config: if ANY conditioned
        channel fails identity resolution (e.g. neither corrected nor raw can be
        read, or the resolved identity is invalid), raise SourceAwareIdentityError
        and stamp NOTHING. source_mixture_mode is computed only after every channel
        resolved successfully — never from a partial subset.
        """
        channels = cfg.get("channels") or {}
        if not channels:
            return cfg
        patch_bbox = None
        if self.patches and 0 <= self.current_patch_idx < len(self.patches):
            y0, y1, x0, x1 = self.patches[self.current_patch_idx]
            patch_bbox = [int(y0), int(y1), int(x0), int(x1)]
        raw_path = getattr(self.loader, "filepath", None)
        ch_map = getattr(self.loader, "ch_map", {}) or {}
        corrected_zarr = self._corrected_zarr_path()

        def _read_raw(ch):
            return self._read_cond_patch_channel(ch, normalize=False)

        # Auto-source default: a channel with REAL corrected data defaults to
        # corrected_zarr; one without stays raw_ome. Availability is grounded in
        # the actual corrected arrays on disk (not a stale UI decision), so a
        # channel is never defaulted to a corrected source that does not exist.
        corrected_available = self._corrected_available_channels(list(channels.keys()))

        # Resolve ALL channels first; only commit to cfg if every one succeeds.
        resolved = []
        for ch in channels:
            # Precedence: an EXPLICIT user source choice always wins (manual raw
            # override of a corrected channel survives every later save/sync).
            # Only when the user has NOT chosen does auto-detection pick the
            # default: corrected_zarr iff real corrected data exists, else raw_ome.
            if ch in self._channel_source_requests:
                requested = self._channel_source_requests[ch]
                user_selected = True
            elif ch in corrected_available:
                requested = REQUESTED_SOURCE_CORRECTED_ZARR
                user_selected = False
            else:
                requested = REQUESTED_SOURCE_RAW_OME
                user_selected = False
            try:
                req, csi = resolve_channel_calibration(
                    ch, requested,
                    read_raw=_read_raw, raw_path=raw_path,
                    channel_index=ch_map.get(ch), patch_bbox=patch_bbox,
                    corrected_zarr_path=corrected_zarr, roi_name=None,
                    user_selected=user_selected)
            except SourceAwareIdentityError:
                raise
            except Exception as exc:
                # Whole-save failure: do not silently skip the channel.
                raise SourceAwareIdentityError(
                    channel=ch, requested_source=requested, cause=exc)
            errs = validate_calibration_source_identity(csi)
            if errs:
                raise SourceAwareIdentityError(
                    channel=ch, requested_source=requested,
                    message="invalid calibration_source_identity: " + "; ".join(errs))
            resolved.append((ch, req, csi))

        # All channels succeeded — commit identities and derive mixture mode.
        identities = []
        for ch, req, csi in resolved:
            channels[ch]["source_request"] = req
            channels[ch]["calibration_source_identity"] = csi
            identities.append(csi)
        mode = source_mixture_mode_from_identities(identities)
        if mode is not None:
            cfg["source_mixture_mode"] = mode
        # Default safe camp policy (corrected nonlinear -> never silently in camp/Gi).
        cfg.setdefault("camp_source_policy", DEFAULT_CAMP_SOURCE_POLICY)
        return cfg

    def _save_step0_remap_config(self):
        wb = getattr(self, "_cond_workbench", None)
        if wb is None or not wb.has_channel_data():
            QMessageBox.information(
                self, "Nothing to save",
                "Load current patch channels and condition them first.")
            return
        cfg = wb.build_config()
        # v14.5b: stamp per-channel source-aware identity (preview only). Stays
        # preview_only / step2_ready=false; never promoted here.
        # v14.5b.1 Strategy A: if any channel's identity cannot be resolved, abort
        # the WHOLE save — no path chosen, no dir created, no partial config.
        try:
            self._apply_source_aware_identity(cfg)
        except SourceAwareIdentityError as exc:
            QMessageBox.warning(
                self, "Source identity failed",
                f"Cannot save source-aware preview config: channel "
                f"'{exc.channel}' source identity could not be resolved "
                f"(requested {exc.requested_source}).\n{exc.message}")
            return
        # Provenance: created in the v14 Step0 Setup & Preprocessing workbench.
        # created_from_step is a REGISTERED constant (utils.channel_remap_config),
        # not an ad-hoc string, so v14.5 promotion can recognize Step0 configs.
        # Stays preview_only / step2_ready=false (set via the workbench source_policy).
        out_dir = self._step0_conditioning_out_dir()
        cfg["created_from_step"] = CREATED_FROM_STEP0_CONDITIONING
        cfg["ui_context"] = "Step0: Setup & Preprocessing / Channel Conditioning"
        # Record the actual physical storage dir honestly. With a ROI context this
        # is the unified <roi_dir>/step0/ location (next to corrected_channels.zarr);
        # without one it is the legacy step1_5/channel_remap_configs fallback.
        cfg["storage_dir"] = out_dir
        cfg["legacy_storage_path"] = out_dir      # kept for schema back-compat
        os.makedirs(out_dir, exist_ok=True)
        # Normal Save AUTO-writes to the canonical Step0 remap-config path for the
        # current run/ROI (stable filename so a later Save overwrites it). No file
        # dialog — picking a location is reserved for an explicit Save As/Export.
        path = self._step0_conditioning_config_path()
        try:
            save_channel_remap_config(cfg, path)
        except ValueError as exc:
            QMessageBox.warning(self, "Save failed", str(exc))
            return
        # Remember the exact path just written so Step1 fusion can pick up the
        # manual remap regardless of any later ROI-context change.
        self._last_saved_remap_path = path
        print(f"[Step0] saved channel remap config -> {path}")
        QMessageBox.information(
            self, "Saved",
            f"Saved channel remap:\n{path}")

    def _step0_conditioning_config_path(self):
        """Canonical Step0 remap-config file for the current run/ROI. Stable name
        so the normal Save overwrites it on every save (no per-save timestamped
        files, no user-chosen location)."""
        return os.path.join(self._step0_conditioning_out_dir(),
                            "step0_channel_remap.json")

    # ── v14.2a Tissue Preview / ROI Navigator popup ──────────────────────────
    #  Step0 owns one floating TissueNavigatorPopup. Creation/show/hide is
    #  side-effect free (no files/configs/outputs). Without loaded data the popup
    #  shows a missing-context hint instead of crashing.
    def _ensure_tissue_navigator(self):
        if self._tissue_navigator_popup is None:
            self._tissue_navigator_popup = TissueNavigatorPopup(
                loader=self.loader, nuc_ch=self.nucleus_channel, parent=self)
            popup = self._tissue_navigator_popup
            # restore-region-selector: host the analysis-region selector (ROI vs
            # Full WSI) in the popup, alongside the ROI drawing it controls. The
            # widget + handler are owned by Step0; reparenting preserves signals.
            popup.set_region_selector(self._region_selector)
            # restore-roi-patch-toolbar: host the ROI/patch drawing toolbar (mode
            # switches + ROI/patch lists) in the popup, with the overview it draws
            # on. _set_draw_mode targets the popup overview via _drawing_overview.
            popup.set_roi_toolbar(self._roi_patch_toolbar)
            # (navigator-layout) ROI/Patch lists below the overview (3/5 overview,
            # 1/5 each list). Mode buttons stay above via set_roi_toolbar.
            popup.set_roi_lists(self._roi_patch_lists)
            # Feed the popup overview from the SINGLE model (no file IO).
            self._feed_popup_from_model()
            # Adopt popup-overview edits into the same model and mirror them back
            # to the Step0 overview. The popup overview is a view/editor over the
            # one model — never an independent ROI store.
            popup.overview.patches_changed.connect(
                lambda *_: self._reconcile_roi_edit(popup.overview))
            popup.overview.rois_changed.connect(
                lambda *_: self._reconcile_roi_edit(popup.overview))
        return self._tissue_navigator_popup

    # ── v14.2b single-model ROI bridge ───────────────────────────────────────
    def _registered_roi_overviews(self):
        """Every OverviewPanel that is a view/editor over the single ROI model."""
        panels = [self.overview]
        if self._tissue_navigator_popup is not None:
            panels.append(self._tissue_navigator_popup.overview)
        return panels

    def _feed_popup_from_model(self):
        """Render the popup overview from the single model (loader/nuc/rois/patches)."""
        popup = self._tissue_navigator_popup
        if popup is None:
            return
        m = self._roi_model
        popup.set_overview_context(
            loader=m.loader, nuc_ch=m.nucleus_channel,
            rois=list(m.rois), patches=list(m.patches),
            full_wsi_mode=m.full_wsi_mode)

    def _reconcile_roi_edit(self, source_panel):
        """Single-model write-back: adopt the edited panel's authoritative state
        into the ONE model, then re-render every OTHER overview from the model.

        Correctness comes from one source of truth + re-render after every edit,
        not from pushing state between two panel stores."""
        if self._roi_sync_guard:
            return
        self._roi_sync_guard = True
        try:
            self._roi_model.adopt(
                rois=source_panel.get_rois(),
                patches=source_panel._patch_coords(),
                full_wsi_mode=getattr(source_panel, "full_wsi_mode", False),
            )
            for panel in self._registered_roi_overviews():
                if panel is source_panel:
                    continue
                panel.set_rois_and_patches(
                    list(self._roi_model.rois), list(self._roi_model.patches),
                    self._roi_model.full_wsi_mode)
            if self._tissue_navigator_popup is not None:
                self._tissue_navigator_popup._refresh_bar_text()
            # Refresh Step0's own ROI widgets when the edit came from elsewhere
            # (the Step0 overview's own signal already refreshed them).
            if source_panel is not self.overview:
                self._on_patches_changed(list(self._roi_model.patches))
        finally:
            self._roi_sync_guard = False

    def show_tissue_navigator(self):
        popup = self._ensure_tissue_navigator()
        popup.show()
        popup.raise_()
        self._update_tissue_view_rect()

    def _auto_open_tissue_navigator(self):
        """Auto-open the Tissue Navigator ONCE after a successful Step0 data load.

        ROI/patch drawing now lives in the navigator (#10), so opening it on load
        lets the user start drawing immediately. Reuses the existing open path
        (show_tissue_navigator); fires once per load via _navigator_auto_opened
        (re-armed at the start of each load). No-op without a usable loader — so a
        failed/empty load never pops an empty navigator, and a mere ROI/overview
        refresh (which does not re-arm) never re-pops it."""
        if self.loader is None or self._navigator_auto_opened:
            return
        self._navigator_auto_opened = True
        self.show_tissue_navigator()

    def toggle_tissue_navigator(self):
        popup = self._ensure_tissue_navigator()
        if popup.isVisible():
            popup.hide()
        else:
            popup.show()
            popup.raise_()
            self._update_tissue_view_rect()

    # ── v14.2c viewer → Tissue Navigator current-view rectangle sync ──────────
    def _map_viewport_to_full(self, vp):
        """Map a viewer image-local viewport rect to FULL-IMAGE pixels.

        AUDITED mapping (Case A): the viewer displays the raw current-patch crop
        with geometry preserved (intensity-only display pipeline), so the viewer's
        image-local pixels are PATCH-LOCAL. Add the current patch origin to get
        full-image pixels. Patch convention is (y0, y1, x0, x1).

        Returns (y0, y1, x0, x1) full-image px, or None when no mapping is valid
        (no viewport, wrong coordinate_space, or no current patch — full-WSI mode
        has no patch crop to anchor the rect, so it returns None).
        """
        if not vp:
            return None
        if vp.get("coordinate_space") != "image_local_pixels":
            return None
        if not self.patches or not (0 <= self.current_patch_idx < len(self.patches)):
            return None
        y0p, y1p, x0p, x1p = (int(v) for v in self.patches[self.current_patch_idx])
        fx0 = x0p + float(vp["x0"]); fx1 = x0p + float(vp["x1"])
        fy0 = y0p + float(vp["y0"]); fy1 = y0p + float(vp["y1"])
        return (fy0, fy1, fx0, fx1)

    def _update_tissue_view_rect(self):
        """Refresh the popup's current-view rectangle from the active viewer.

        Clears the rectangle when: no popup, split view (mixed geometry), no
        viewer image yet, or no current patch. Never creates ROIs/files."""
        popup = self._tissue_navigator_popup
        if popup is None:
            return
        wb = getattr(self, "_cond_workbench", None)
        # Split view mixes raw|remapped per column → mapping ambiguous → clear.
        if wb is None or wb.is_split_view():
            popup.clear_viewport_rect()
            popup.overview.clear_current_view_rect()
            return
        vp = wb.viewer_viewport_rect()
        full = self._map_viewport_to_full(vp)
        if full is None:
            popup.clear_viewport_rect()
            popup.overview.clear_current_view_rect()
            return
        popup.set_viewport_rect(vp)                    # store image-local (validated)
        popup.overview.set_current_view_rect(full)     # draw in overview coords

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
        # (#10) Section B was relocated to the Tissue Navigator; with a single
        # child (Section C) there is no B:C ratio to fix — let it fill the tab.
        if self._main_split.count() < 2:
            return
        total = self._main_split.width()
        if total < 10:
            return
        b_w = max(80, total // 4)
        c_w = total - b_w - self._main_split.handleWidth()
        self._main_split.setSizes([b_w, c_w])

    @staticmethod
    def _box_style(color):
        # Reserve vertical room for the title (margin-top) AND explicitly position
        # the title sub-control in that margin so it sits ABOVE the border/body —
        # without the ::title rule + enough margin, the first body child rides up
        # and occludes the title (the styled-QGroupBox "eats its title" bug).
        return (
            f"QGroupBox{{border:1px solid {color};border-radius:5px;margin-top:16px;"
            f"font-weight:bold;color:{color};font-size:11px;}}"
            f"QGroupBox::title{{subcontrol-origin:margin;subcontrol-position:top left;"
            f"left:8px;padding:0 4px;}}"
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

        # Re-arm the per-load auto-open guard: each genuine load may open the
        # navigator once; ROI/overview refreshes (other code paths) do not re-arm.
        self._navigator_auto_opened = False
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
        # New image/ROI loaded -> no BG run yet: button back to "▶ Process", clear
        # stale results/dirty so a param change won't read as "Re-process" (Topic 1).
        self._computed_channels = set()
        self._preview_cache = {}
        self._process_completed = False
        if hasattr(self, "_btn_process"):
            self._reset_process_button()
        # (#5) the BG run-progress widgets (_bg_pbar/_bg_start_status) were removed
        # with the standalone "Run BG correction" button; Save uses its own
        # progress dialog.
        self._patch_warning.setVisible(False)

        self.overview.loader = self.loader
        self.overview.nuc_ch = self.nucleus_channel
        self.overview.full_wsi_mode = self._is_full_wsi_mode()
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

        # v14.2b: keep the single ROI model in lockstep with this reset and the
        # newly-loaded loader/nucleus; re-feed the popup overview if it exists.
        self._roi_model.adopt(
            rois=[], patches=[], full_wsi_mode=False,
            loader=self.loader, nucleus_channel=self.nucleus_channel)
        self._feed_popup_from_model()

        self._load_existing_config()
        self._rebuild_channel_list()
        self._rebuild_patch_buttons()
        self._load_status.setText(
            f"Loaded: {self.loader.shape[0]:,}x{self.loader.shape[1]:,} px  |  {len(self.loader.ch_map)} channels"
        )

        # Auto-open the Tissue Navigator once on a successful load (ROI/patch
        # drawing lives there since #10). Reuses the existing open path; guarded
        # to fire once per load.
        self._auto_open_tissue_navigator()

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

    def _is_full_wsi_mode(self):
        return str(getattr(self, "_analysis_region_mode", "roi")) == "full_wsi"

    def _on_analysis_region_changed(self, idx):
        self._analysis_region_mode = "full_wsi" if int(idx) == 1 else "roi"
        full = self._is_full_wsi_mode()
        for btn in (
            getattr(self, "_btn_mode_roi", None),
            getattr(self, "_btn_del_roi", None),
            getattr(self, "_btn_rename_roi", None),
        ):
            if btn is not None:
                btn.setEnabled(not full)
        if full:
            self._set_draw_mode("patch")
            if self.overview:
                self.overview.full_wsi_mode = True
                self.overview.status.setText(
                    "Full WSI mode: drag patch rectangles anywhere. ROI drawing is disabled."
                )
        else:
            if self.overview:
                self.overview.full_wsi_mode = False
            if self.overview:
                self.overview.status.setText("ROI mode: draw ROI vertices or patch rectangles inside a ROI.")

    def _full_wsi_roi(self):
        h, w = (self.loader.shape if self.loader is not None else (0, 0))
        return {
            "name": "Full WSI",
            "display_name": "Full WSI",
            "type": "full_wsi",
            "analysis_region_type": "full_wsi",
            "bbox_fullres": [0, int(h), 0, int(w)],
            "polygon_fullres": None,
            "shape": [int(h), int(w)],
            "patch_indices": list(range(len(self.overview._patches if self.overview else []))),
        }

    def _roi_context_signature(self, rois):
        """Identity of the current analysis region, used to decide whether the
        existing roi_context can be reused across Saves (#1). Full-WSI is keyed by
        the image shape; ROI mode by the first ROI's full-res bbox. A change here
        (mode switch or a redrawn ROI) forces a fresh roi_context."""
        if self._is_full_wsi_mode():
            shp = tuple(int(v) for v in (self.loader.shape if self.loader else (0, 0)))
            return ("full_wsi", shp)
        bbox = tuple(int(v) for v in ((rois[0].get("bbox_fullres") if rois else None) or []))
        return ("roi", bbox)

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
        # Patches changed (drawn/deleted in the navigator) -> (re)start the
        # background preload of all patches × channels (cancels any running one,
        # invalidates the cache), then refresh the conditioning view for the new
        # current patch (defect B). _maybe_refresh is a no-op until conditioning
        # has been engaged.
        self._start_preload()
        self._maybe_refresh_conditioning()

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

    def _drawing_overview(self):
        """The overview the toolbar should drive: the Tissue Navigator popup's
        (the one the user actually sees/draws on) when it exists, else the Step0
        model-view overview. Edits mirror across both via the v14.2b bridge."""
        pop = getattr(self, "_tissue_navigator_popup", None)
        return pop.overview if pop is not None else self.overview

    def _set_draw_mode(self, mode):
        """切换绘制模式，同步按钮状态"""
        self._btn_mode_roi.setChecked(mode == "roi")
        self._btn_mode_patch.setChecked(mode == "patch")
        ov = self._drawing_overview()
        if mode == "roi":
            # 自动生成下一个不重名的默认ROI名，写入输入框，不弹对话框
            existing = {r["name"] for r in ov._rois}
            n = len(ov._rois) + 1
            next_name = f"ROI_{n}"
            while next_name in existing:
                n += 1
                next_name = f"ROI_{n}"
            ov._roi_name_edit.setText(next_name)
            ov._set_mode("roi")
            ov.status.setText(
                "Draw ROI vertices on the overview, then press Enter or right-click to close.")
        else:
            ov._set_mode("patch")
            ov.status.setText("Drag to draw a patch rectangle inside a ROI.")

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
        # v15: a previous run's final methods are kept only as reference — they
        # no longer pre-seed this session's assignments. Until the user assigns
        # a method (checkbox / combo / decision panel), rows display the global
        # Method box value (default Both) via _refresh_channel_row.
        self._prior_channel_decisions = {
            k: ("original" if v == "both" else v) for k, v in raw_decisions.items()}
        self._channel_decisions = {}
        # Restore per-channel param overrides (int-normalized); channels absent
        # fall back to the global method_params.
        self._channel_params = {}
        for ch, cp in ((self._loaded_config or {}).get("channel_params") or {}).items():
            cp = cp or {}
            try:
                self._channel_params[str(ch)] = {
                    "tophat_radius": int(cp["tophat_radius"]),
                    "cucim_sigma": int(cp["cucim_sigma"]),
                }
            except (KeyError, TypeError, ValueError):
                continue
        params = (self._loaded_config or {}).get("method_params") or {}
        self._tophat_slider.blockSignals(True)
        self._tophat_slider.setValue(int(params.get("tophat_radius", TOPHAT_RADIUS_DEFAULT)))
        self._tophat_slider.blockSignals(False)
        self._cucim_slider.blockSignals(True)
        self._cucim_slider.setValue(int(params.get("cucim_sigma", CUCIM_SIGMA_DEFAULT)))
        self._cucim_slider.blockSignals(False)
        self._refresh_slider_labels()

    def _rebuild_channel_list(self):
        # v15: rebuilt through the shared-dock adapter; the legacy row
        # construction below is retained only as the pre-adapter fallback.
        if getattr(self, "_dock_adapter", None) is not None:
            self._dock_adapter.rebuild()
            return
        current = self.current_channel
        self._channel_rows.clear()
        self._channel_order = []
        self._channel_list.clear()
        if not self.loader:
            return

        for ch in self.loader.channel_names():
            item = QtWidgets.QListWidgetItem(self._channel_list)
            item.setSizeHint(QtCore.QSize(300, 29))   # 4/5 of the old 36
            row = QWidget()
            lay = QHBoxLayout(row)
            lay.setContentsMargins(4, 1, 4, 1)
            lay.setSpacing(4)

            is_nucleus = (ch == self.nucleus_channel)

            # 勾选框
            cb = QtWidgets.QCheckBox()
            cb.setChecked(False)
            cb.setEnabled(not is_nucleus)
            cb.stateChanged.connect(lambda state, name=ch: self._on_channel_checkbox_toggled(name, state))
            lay.addWidget(cb)

            # 通道名 — no stretch, so the Method dropdown sits right next to it.
            label = QLabel(ch if not is_nucleus else f"{ch} ★")
            label.setStyleSheet("color:#ddd;font-size:11px;")
            label.setMinimumWidth(48)
            lay.addWidget(label)

            # 方法下拉（nucleus锁定）— immediately after the channel name. Also the
            # single source of truth for the ASSIGNED method: "Original" folds in the
            # old separate decision badge (tophat/cucim/original), so no extra widget.
            method_cb = QtWidgets.QComboBox()
            method_cb.addItems(["TopHat", "cucim", "Both", "Original"])
            method_cb.setEnabled(not is_nucleus)
            method_cb.setFixedWidth(64)
            method_cb.setStyleSheet(
                "QComboBox{background:#1a1a1a;color:#ddd;border:1px solid #444;"
                "border-radius:3px;padding:1px 2px;font-size:10px;}"
                "QComboBox::drop-down{border:none;}"
                "QComboBox:disabled{color:#555;}"
            )
            saved = self._channel_decisions.get(ch) or self._channel_methods.get(ch, "both")
            method_cb.setCurrentIndex(self._METHOD_IDX.get(saved, 2))  # default Both
            method_cb.currentTextChanged.connect(
                lambda txt, name=ch: self._on_channel_method_changed(name, txt))
            lay.addWidget(method_cb)

            lay.addStretch(1)   # push the status icon to the right edge

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

    def _resolve_channel_params(self, ch):
        """(tophat_radius, cucim_sigma) for a channel — its OWN per-channel value.

        Fully isolated from the global Method Parameters: an untuned channel falls
        back to the module defaults (NOT the live global sliders), so the global
        box can never bleed into / overwrite a channel's per-channel params."""
        cp = self._channel_params.get(ch) or {}
        tr = int(cp.get("tophat_radius", TOPHAT_RADIUS_DEFAULT))
        cs = int(cp.get("cucim_sigma", CUCIM_SIGMA_DEFAULT))
        return tr, cs

    def _current_dec_method(self):
        if self._dec_top.isChecked():
            return "tophat"
        if self._dec_cu.isChecked():
            return "cucim"
        return "original"

    def _sync_dec_param_enabled(self):
        # Both inputs editable whenever a real channel is selected — the user sets
        # params freely; the method radio only picks WHICH one is used at Process.
        ch = self.current_channel
        ok = bool(ch and ch != self.nucleus_channel and ch in self._channel_rows)
        self._dec_radius.setEnabled(ok)
        self._dec_sigma.setEnabled(ok)

    def _on_dec_method_toggled(self, checked):
        # QRadioButton.toggled fires for both the off and on button; act on 'on'.
        # No compute here — the preview already renders TopHat & cucim; the radio
        # only records which one is this channel's decision (committed on Apply/
        # Process). NEVER auto-recompute.
        if not checked or getattr(self, "_loading_decision", False):
            return
        self._sync_dec_param_enabled()

    def _on_dec_param_changed(self, _val=None):
        # Persist the typed value into the ISOLATED per-channel store immediately
        # (so it survives patch/channel switches and is never overwritten by the
        # global box). Do NOT compute — Enter or the Process button triggers the
        # run. This is the fix for the auto-recompute + global-sync bug.
        if getattr(self, "_loading_decision", False):
            return
        ch = self.current_channel
        if not ch or ch == self.nucleus_channel:
            return
        self._channel_params[ch] = {
            "tophat_radius": int(self._dec_radius.value()),
            "cucim_sigma": int(self._dec_sigma.value()),
        }

    def _on_dec_param_entered(self):
        """Enter pressed in a per-channel param box -> process this channel's
        ALL patches with its params (same as the Process button)."""
        if getattr(self, "_loading_decision", False):
            return
        self._process_current_channel()

    def _update_decision_ui(self):
        ch = self.current_channel
        enabled = bool(ch and ch != self.nucleus_channel and ch in self._channel_rows)
        self._loading_decision = True
        try:
            self._apply_btn.setEnabled(enabled)
            self._dec_process_btn.setEnabled(enabled and bool(self.patches))
            self._dec_radius.setEnabled(enabled)
            self._dec_sigma.setEnabled(enabled)
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
            # Load THIS channel's own params into the inputs (isolated: override
            # if set, else module default — never the global box's values).
            tr, cs = self._resolve_channel_params(ch)
            self._dec_radius.setValue(tr)
            self._dec_sigma.setValue(cs)
            self._sync_dec_param_enabled()
            if decision != "original":
                self._decision_status.setText(f"Saved: {ch} {decision}  (r={tr}, σ={cs})")
            else:
                self._decision_status.setText(
                    f"{ch}: set radius/sigma, pick a method, press Enter or Process.")
        finally:
            self._loading_decision = False

    def _refresh_channel_row(self, ch):
        row = self._channel_rows.get(ch)
        if not row:
            return
        cb = row["checkbox"]
        method_cb = row.get("method_cb")
        cb.blockSignals(True)
        if ch == self.nucleus_channel:
            cb.setChecked(False)
            row["status_lbl"].setText("★")
            row["status_lbl"].setStyleSheet("color:#56b6c2;font-size:12px;")
        else:
            decision = self._channel_decisions.get(ch)
            cb.setChecked(bool(decision) and decision != "original")
            # The Method combo IS the assigned-method display now (folds in the old
            # decision badge). Unassigned channels (no decision THIS session)
            # mirror the global Method box (default Both) instead of a stale
            # previous-run decision.
            if method_cb is not None:
                if decision:
                    idx = self._METHOD_IDX.get(decision, 3)
                else:
                    idx = self._METHOD_IDX.get(
                        self._method_all.currentText().lower(), 2)
                method_cb.blockSignals(True)
                method_cb.setCurrentIndex(idx)
                method_cb.blockSignals(False)
            if row["status_lbl"].text() != "⟳":   # don't clobber a running spinner
                row["status_lbl"].setText("")
        cb.blockSignals(False)

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

    def _all_patch_rows(self):
        """Every patch-button row to keep in sync: the BG tab's Preview Patch row
        and (when built) the Channel Conditioning tab's mirror row."""
        rows = []
        for attr in ("_patch_buttons_row", "_cond_patch_buttons_row"):
            row = getattr(self, attr, None)
            if row is not None:
                rows.append(row)
        return rows

    def _rebuild_patch_buttons(self):
        for row in self._all_patch_rows():
            while row.count():
                item = row.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()
        if not self.patches:
            self.current_patch_idx = 0
            self._patch_info.setText("No patch ROI available yet. Draw a patch in Section B first.")
            return
        self.current_patch_idx = min(self.current_patch_idx, len(self.patches) - 1)
        for row in self._all_patch_rows():
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
                row.addWidget(btn)
            row.addStretch()
        self._update_patch_info()

    def _sync_patch_buttons(self):
        for row in self._all_patch_rows():
            for i in range(row.count()):
                widget = row.itemAt(i).widget()
                if isinstance(widget, QPushButton):
                    widget.setChecked(widget.text() == f"P{self.current_patch_idx+1}")

    def _repaint_patch_buttons(self):
        """Force an immediate synchronous repaint of the patch buttons so the
        check-state change is visible before any blocking IO runs."""
        for row in self._all_patch_rows():
            for i in range(row.count()):
                widget = row.itemAt(i).widget()
                if isinstance(widget, QPushButton):
                    widget.repaint()

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
            if not row:
                continue
            if row["checkbox"].isChecked():
                self._channel_methods[ch] = method
                self._channel_decisions[ch] = method
            elif ch in self._channel_decisions:
                # explicitly assigned (e.g. Original) — leave it alone
                continue
            # checked rows adopt the method; unassigned rows just mirror the
            # global box in their display (no decision is written for them)
            row["method_cb"].blockSignals(True)
            row["method_cb"].setCurrentIndex(self._METHOD_IDX.get(method, 0))
            row["method_cb"].blockSignals(False)

    def _on_channel_method_changed(self, ch, txt):
        """Single channel method dropdown change. The combo now also carries the
        assigned decision: "Original" means no correction (channel unchecked)."""
        m = txt.lower()
        self._channel_decisions[ch] = m
        if m == "original":
            self._channel_methods.pop(ch, None)
        else:
            self._channel_methods[ch] = m
        row = self._channel_rows.get(ch)
        if row:
            cb = row["checkbox"]
            cb.blockSignals(True)
            cb.setChecked(m != "original")   # original = raw = not corrected
            cb.blockSignals(False)

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
            channel_params=self._channel_params,   # each channel uses its own params
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

    def _reset_process_button(self):
        """Return the run button to the idle '▶ Process' look + clear the dirty flag.
        A subsequent param change (after a completed run) flips it to Re-process."""
        self._params_dirty = False
        self._btn_process.setText("▶ Process")
        self._btn_process.setStyleSheet(
            "QPushButton{background:#1a5c2a;color:#6bffa0;border:1px solid #4a9;"
            "border-radius:4px;padding:6px 14px;font-size:12px;font-weight:bold;}"
            "QPushButton:hover{background:#2a7c3a;}"
            "QPushButton:disabled{background:#222;color:#555;border-color:#333;}"
        )

    def _on_batch_all_done(self):
        self._proc_pbar.setValue(100)
        self._proc_status.setText("✓ All done. Click a channel to view results.")
        self._proc_status.setStyleSheet("color:#6bffa0;font-size:10px;font-weight:bold;")
        self._btn_process.setEnabled(True)
        self._btn_stop_process.setEnabled(False)
        self._process_completed = True   # 解锁按需计算
        # Not auto "Re-process": only a param change after this flips it (Topic 1).
        self._reset_process_button()

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
        # (#4 patch-local viewport) Save the LEAVING patch's conditioning zoom/pan
        # before switching, so returning restores it. Must read it while that
        # patch is still displayed (before current_patch_idx changes).
        self._save_conditioning_viewport(self.current_patch_idx)
        self.current_patch_idx = idx
        self._patch_selected_idx = idx
        if self._patch_list.count() > idx:
            self._patch_list.setCurrentRow(idx)
        self._sync_patch_buttons()
        # Highlight fix: paint the un-highlight of the old button + highlight of
        # the new one NOW, before any IO below. Otherwise the queued repaint is
        # starved by the synchronous work and both buttons look highlighted.
        self._repaint_patch_buttons()
        self._update_patch_info()
        if self.current_channel and self._has_any_cache(self.current_channel):
            self._show_channel_from_cache(self.current_channel)
        # Keep the conditioning workbench in sync with the active patch (no-op
        # until the workbench is actually in use).
        self._maybe_refresh_conditioning()
        # (#4) Viewport is patch-LOCAL: restore the entered patch's saved zoom/pan,
        # or fit-to-view if it was never visited (never inherit the prior patch's
        # zoom). Remap params (Min/Max/Gamma) stay channel-global, untouched here.
        self._restore_or_fit_conditioning_viewport()
        # v14.2c: current patch changed → remap the Tissue Navigator view rect.
        self._update_tissue_view_rect()

    # ── conditioning patch-local viewport (zoom/pan) ─────────────────────────
    def _conditioning_patch_key(self, idx):
        """Stable per-patch key (the patch bbox) for the viewport cache; None if
        the index is out of range."""
        if idx is None or not (0 <= idx < len(self.patches)):
            return None
        return tuple(int(v) for v in self.patches[idx])

    def _save_conditioning_viewport(self, idx):
        """Remember the conditioning viewer's current zoom/pan for patch `idx`."""
        if not getattr(self, "_conditioning_in_use", False):
            return
        wb = getattr(self, "_cond_workbench", None)
        key = self._conditioning_patch_key(idx)
        if wb is None or key is None or not wb.has_channel_data():
            return
        rect = wb.viewer.get_viewport_rect()
        if rect is not None:
            self._conditioning_patch_viewports[key] = rect

    def _restore_or_fit_conditioning_viewport(self):
        """Restore the current patch's saved zoom/pan, or fit-to-view if it has
        none (a never-visited patch must not inherit the previous patch's zoom)."""
        if not getattr(self, "_conditioning_in_use", False):
            return
        wb = getattr(self, "_cond_workbench", None)
        if wb is None or not wb.has_channel_data():
            return
        rect = self._conditioning_patch_viewports.get(
            self._conditioning_patch_key(self.current_patch_idx))
        if rect is not None:
            wb.viewer.set_view_region(rect)
        else:
            wb.fit_view()

    # ══ Params dirty tracking ════════════════════════════════════════

    def _on_slider_changed(self):
        self._refresh_slider_labels()
        # Only a param change AFTER a completed run (with data loaded) means the
        # existing result is stale -> "Re-process". Before any run (or no data),
        # the button stays "▶ Process" (this is the initial run, not a re-run).
        if not (self._process_completed and self.loader and self.patches):
            return
        if not self._params_dirty:
            self._params_dirty = True
            self._btn_process.setText("↺ Re-process (params changed)")
            self._btn_process.setStyleSheet(
                "QPushButton{background:#5c3a1a;color:#ffb86c;border:1px solid #c87;"
                "border-radius:4px;padding:6px 14px;font-size:12px;font-weight:bold;}"
                "QPushButton:hover{background:#7c5a2a;}"
            )

    def _refresh_slider_labels(self):
        # No-op: the QSpinBox input boxes display their own value now (the old
        # separate value labels were removed when sliders became input boxes).
        # Kept as a stable hook for its existing callers.
        pass

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
        # Preview uses the Per-Channel Decision box's LIVE values (the current
        # channel's tuning), falling back to the global defaults it was seeded with.
        _pv_r = self._dec_radius.value() if hasattr(self, "_dec_radius") else self._tophat_slider.value()
        _pv_s = self._dec_sigma.value() if hasattr(self, "_dec_sigma") else self._cucim_slider.value()
        self._preview_worker = BackgroundPreviewWorker(
            req_id,
            self.loader,
            self.current_channel,
            roi,
            _pv_r,
            _pv_s,
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
        decision = self._current_dec_method()
        self._channel_decisions[ch] = decision
        # persist this channel's own params (override of the global defaults)
        self._channel_params[ch] = {
            "tophat_radius": int(self._dec_radius.value()),
            "cucim_sigma": int(self._dec_sigma.value()),
        }
        self._refresh_channel_row(ch)
        self._decision_status.setText(
            f"Saved: {ch} {decision}  (r={self._dec_radius.value()}, "
            f"σ={self._dec_sigma.value()})")

    def _process_current_channel(self):
        """Recompute ONE channel across all patches with its Per-Channel params.

        Computes BOTH TopHat and cucim (method='both') so the user can compare the
        two results in the preview and THEN pick the final one via the radio. The
        radio is the final-result selector, NOT a prerequisite for recomputing
        (radius drives TopHat, sigma drives cucim — both are always available)."""
        ch = self.current_channel
        if not ch or ch == self.nucleus_channel:
            return
        if not self.patches:
            QMessageBox.information(self, "No patches",
                                    "Draw at least one patch in the navigator first.")
            return
        if self._batch_worker is not None and self._batch_worker.isRunning():
            QMessageBox.information(self, "Busy", "A process run is already in progress.")
            return
        # persist this channel's current params, then recompute just it (fresh cache)
        self._channel_params[ch] = {
            "tophat_radius": int(self._dec_radius.value()),
            "cucim_sigma": int(self._dec_sigma.value()),
        }
        self._preview_cache = {k: v for k, v in self._preview_cache.items() if k[0] != ch}
        self._computed_channels.discard(ch)
        params = {ch: dict(self._channel_params.get(ch) or {})}
        self._set_channel_computing(ch)
        self._proc_pbar.setVisible(True)
        self._proc_pbar.setValue(0)
        self._btn_stop_process.setEnabled(True)
        self._process_completed = False
        self._batch_worker = BatchProcessWorker(
            self.loader, self.patches, {ch: "both"}, self.nucleus_channel,
            self._tophat_slider.value(), self._cucim_slider.value(),
            channel_params=params, max_gpu_workers=4,
        )
        self._batch_worker.channel_patch_done.connect(self._on_batch_patch_done)
        self._batch_worker.channel_done.connect(self._on_batch_channel_done)
        self._batch_worker.all_done.connect(self._on_batch_all_done)
        self._batch_worker.progress.connect(self._on_batch_progress)
        self._batch_worker.error_signal.connect(self._on_batch_error)
        self._batch_worker.canceled.connect(self._on_batch_canceled)
        self._batch_worker.start()

    def _build_config(self):
        decisions = {}
        for ch in self._channel_order:
            if ch == self.nucleus_channel:
                continue
            d = self._channel_decisions.get(ch, "original")
            decisions[ch] = "original" if d == "both" else d
        # Per-channel param overrides (only channels the user tuned individually);
        # channels absent here use method_params. Kept minimal + int-normalized.
        channel_params = {}
        for ch in decisions:
            cp = self._channel_params.get(ch)
            if cp:
                channel_params[ch] = {
                    "tophat_radius": int(cp.get("tophat_radius", self._tophat_slider.value())),
                    "cucim_sigma": int(cp.get("cucim_sigma", self._cucim_slider.value())),
                }
        return {
            "method_params": {
                "tophat_radius": int(self._tophat_slider.value()),
                "cucim_sigma": int(self._cucim_slider.value()),
            },
            "channel_decisions": decisions,
            "channel_params": channel_params,
        }

    def _stop_bg_workers(self):
        for worker in self._bg_workers:
            if worker.isRunning():
                worker.stop()
        self._bg_workers = []

    # (#5) The standalone "Run BG correction" preview-batch handlers
    # (_on_start_bg_correction / _bg_run_next / _finish_bg_start) were removed
    # with that button; the BG-tab Save (_save_and_continue) is the single
    # entry that runs correction + writes outputs + the handoff.

    def _save_and_continue(self):
        if self.loader is None:
            QMessageBox.warning(self, "Validation", "Please load an OME-TIFF first.")
            return
        if not self.patches:
            QMessageBox.warning(self, "Validation", "Please define at least 1 preview patch before continuing.")
            return

        if self._is_full_wsi_mode():
            rois = [self._full_wsi_roi()]
            self.rois = rois
            print("[Step0] analysis_region_type=full_wsi")
        else:
            rois = list(self.overview.get_rois() if self.overview else self.rois)
            if not rois:
                QMessageBox.warning(self, "Validation", "No ROI found. Draw ROI first, or choose Full WSI mode.")
                return
            self.rois = rois
            print("[Step0] analysis_region_type=roi")
        if not rois:
            QMessageBox.warning(self, "Validation", "No ROI found. Draw ROI first.")
            return

        # (#1) REUSE the existing roi_context (-> same step0_dir / zarr_path)
        # when the analysis region is unchanged since the last Save. Otherwise
        # create_full_wsi_context / create_roi_context mint a fresh timestamped
        # roi_id every Save -> a new empty dir -> read_corrected_zarr_state never
        # finds the prior zarr -> incremental save can never fire. Create a fresh
        # context only on the first Save or when the mode / ROI bbox changes.
        self._project_output_dir = self.output_dir
        sig = self._roi_context_signature(rois)
        if (getattr(self, "_roi_context", None) is not None
                and getattr(self, "_roi_context_sig", None) == sig):
            print(f"[Step0] reusing roi_context roi_id={self._roi_context['roi_id']} "
                  f"(analysis region unchanged)")
        else:
            if self._is_full_wsi_mode():
                self._roi_context = create_full_wsi_context(
                    self._project_output_dir, self.loader.shape, self.ome_path)
            else:
                self._roi_context = create_roi_context(
                    self._project_output_dir, rois[0], self.ome_path)
            self._roi_context_sig = sig
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
            # No channel assigned TopHat/cuCIM -> nothing to background-correct.
            # This is a VALID choice (not an error): record the "no correction"
            # decision + handoff so downstream still works, and tell the user
            # plainly there is nothing to save + where to go next.
            self._ensure_empty_corrected_zarr(zarr_path, rois)
            self.loader.set_corrected_zarr_store(None, {})
            self._emit_complete(config, zarr_path, {})
            QMessageBox.information(
                self, "No background correction",
                "No channel is assigned TopHat or cuCIM, so there is no "
                "background correction to save.\n\n"
                "That's fine — click OK, then use the Channel Remap tab to "
                "adjust channels manually, or continue to Step1.")
            return

        # Incremental save: skip channels already in the corrected zarr with the
        # same (method, method-specific parameter), when the ROI set is unchanged.
        # Process only new/changed channels; merge into (not overwrite) the zarr.
        mp = config.get("method_params") or {}
        cp_all = config.get("channel_params") or {}
        def _cur_sig(ch, method):
            pname = "tophat_radius" if method == "tophat" else "cucim_sigma"
            pdefault = (TOPHAT_RADIUS_DEFAULT if method == "tophat"
                        else CUCIM_SIGMA_DEFAULT)
            cp = cp_all.get(ch) or {}
            return (method, int(cp.get(pname, mp.get(pname, pdefault))))
        current_sigs = {ch: _cur_sig(ch, m) for ch, m in corrected.items()}

        existing_sigs, existing_bboxes = read_corrected_zarr_state(zarr_path)
        current_bboxes = sorted(
            tuple(int(v) for v in (r.get("bbox_fullres") or []))
            for r in rois if len(r.get("bbox_fullres") or []) == 4)
        rois_match = bool(existing_sigs) and existing_bboxes == current_bboxes
        if not rois_match:
            existing_sigs = {}           # no zarr / ROI set changed -> reprocess all
        to_process = {ch: m for ch, m in corrected.items()
                      if existing_sigs.get(ch) != current_sigs[ch]}
        for ch, m in corrected.items():
            if ch not in to_process:
                print(f"[Step0] incremental save: skipping channel={ch} "
                      f"(already corrected with {current_sigs[ch]})")

        if not to_process:
            # Everything already saved with the same method -> no reprocessing.
            # The zarr already holds every channel; just (re)wire the handoff.
            self.loader.set_corrected_zarr_store(zarr_path, corrected)
            self._emit_complete(config, zarr_path, corrected)
            return

        # Hot-swap after the worker should touch ONLY the channels we reprocess.
        self._incremental_processed = set(to_process)
        self._btn_continue.setEnabled(False)
        self._btn_load.setEnabled(False)
        self._wsi_dialog = _WsiCorrectionProgressDialog(self)
        self._wsi_worker = WsiCorrectionWorker(
            self.loader, step0_dir, config, rois=rois, parent=self,
            process_channels=set(to_process), incremental=rois_match,
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
        # Hot-swap: corrected channels now read corrected pixels from the loader;
        # replace their preload-cache entries so the conditioning overlay shows
        # the corrected data (and refresh if conditioning is engaged). On an
        # incremental save only the channels actually REprocessed this run need a
        # re-read — skipped channels were already corrected in the cache.
        only = getattr(self, "_incremental_processed", None)
        self._incremental_processed = None
        self._hotswap_corrected(decisions, only=only)
        self._emit_complete(config, zarr_path, decisions)

    def _hotswap_corrected(self, decisions, only=None):
        """Re-read the corrected channels (per patch) into the preload cache.

        After set_corrected_zarr_store, loader.read_region returns CORRECTED
        pixels for the corrected channels; uncorrected channels are untouched.
        `only` (a set) restricts the re-read to the channels reprocessed this run
        (incremental save); None re-reads all corrected channels.
        Synchronous (few channels × few patches)."""
        if not decisions or not self.loader or not self.patches:
            return
        corrected = [ch for ch, m in decisions.items()
                     if str(m).lower() not in ("", "original", "none")
                     and (only is None or ch in only)]
        if not corrected:
            return
        for pidx, bbox in enumerate(self.patches):
            try:
                y0, y1, x0, x1 = bbox
            except Exception:
                continue
            pc = self._preload_cache.setdefault(pidx, {})
            for ch in corrected:
                try:
                    arr = self.loader.read_region(ch, y0, y1, x0, x1,
                                                  normalize=False)
                    arr = np.asarray(arr, dtype=np.float32)
                    if arr.ndim == 3 and arr.shape[2] == 1:
                        arr = arr[:, :, 0]
                    pc[ch] = arr
                except Exception:
                    continue
        # Drop the workbench's stale raw for corrected channels + repaint.
        self._maybe_refresh_conditioning()

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
        channel_params = {}
        for ch, cp in (cfg.get("channel_params") or {}).items():
            cp = cp or {}
            channel_params[str(ch)] = {
                "tophat_radius": int(cp.get("tophat_radius", params.get("tophat_radius", TOPHAT_RADIUS_DEFAULT))),
                "cucim_sigma": int(cp.get("cucim_sigma", params.get("cucim_sigma", CUCIM_SIGMA_DEFAULT))),
            }
        return {
            "method_params": {
                "tophat_radius": int(params.get("tophat_radius", TOPHAT_RADIUS_DEFAULT)),
                "cucim_sigma": int(params.get("cucim_sigma", CUCIM_SIGMA_DEFAULT)),
            },
            "channel_decisions": decisions,
            "channel_params": channel_params,
        }

    @staticmethod
    def _roi_shape_from_bbox(bbox):
        if not bbox or len(bbox) != 4:
            return [0, 0]
        y0, y1, x0, x1 = [int(v) for v in bbox]
        return [max(0, y1 - y0), max(0, x1 - x0)]

    def _standard_rois(self):
        src = [self._full_wsi_roi()] if self._is_full_wsi_mode() else list(self.overview._rois if self.overview else self.rois)
        rois = []
        for idx, roi in enumerate(src, start=1):
            bbox = list(roi.get("bbox_fullres") or [])
            item = {
                "name": str(roi.get("name") or f"ROI_{idx}"),
                "display_name": str(roi.get("name") or f"ROI_{idx}"),
                "bbox_fullres": [int(v) for v in bbox] if len(bbox) == 4 else [],
                "polygon_fullres": None if roi.get("type") == "full_wsi" else (roi.get("polygon_fullres") or []),
                "shape": self._roi_shape_from_bbox(bbox),
            }
            if roi.get("type"):
                item["type"] = roi.get("type")
            if roi.get("analysis_region_type"):
                item["analysis_region_type"] = roi.get("analysis_region_type")
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
        root.attrs["analysis_region_type"] = "full_wsi" if self._is_full_wsi_mode() else "roi"
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
            group.attrs["analysis_region_type"] = "full_wsi" if self._is_full_wsi_mode() else "roi"
            group.attrs["bbox_fullres"] = roi.get("bbox_fullres") or []
            group.attrs["polygon_fullres"] = roi.get("polygon_fullres") or []
            group.attrs["shape"] = roi.get("shape") or self._roi_shape_from_bbox(roi.get("bbox_fullres"))

    def _refresh_bg_corrected_status(self, report):
        """Update the corrected-output status label from a corrected_zarr_report.

        Empty/invalid output is flagged as NOT a valid corrected output — a
        directory existing is never reported as success."""
        if not hasattr(self, "_bg_corrected_status"):
            return
        if not report or not report.get("exists"):
            self._bg_corrected_status.setText(
                "corrected_channels.zarr: not written.")
            self._bg_corrected_status.setStyleSheet("color:#888;font-size:11px;")
        elif report.get("non_empty"):
            n = report["n_channel_arrays"]
            self._bg_corrected_status.setText(
                f"✓ corrected_channels.zarr written — {n} channel "
                f"array{'s' if n != 1 else ''}.")
            self._bg_corrected_status.setStyleSheet(
                "color:#6bffa0;font-size:11px;font-weight:bold;")
        else:
            # No channels assigned -> a valid "no correction" choice, not an error.
            self._bg_corrected_status.setText(
                "No background correction applied (no channels assigned). "
                "Use Channel Remap or continue to Step1.")
            self._bg_corrected_status.setStyleSheet("color:#888;font-size:11px;")

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
        analysis_region_type = "full_wsi" if self._is_full_wsi_mode() else "roi"

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
                root.attrs["analysis_region_type"] = analysis_region_type
                root.attrs["source_ome"] = os.path.abspath(self.ome_path)
                root.attrs["output_dir"] = os.path.abspath(step0_dir)
                root.attrs["project_output_dir"] = os.path.abspath(project_dir)
                root.attrs["roi_id"] = roi_id
                root.attrs["roi_dir"] = os.path.abspath(roi_dir) if roi_dir else ""
                root.attrs["roi_names"] = [r.get("name", f"ROI_{i}") for i, r in enumerate(rois, start=1)]
                root.attrs["created_by"] = "Step0"
                # v14.4: honest preprocessing provenance (NOT step2_ready).
                stamp_corrected_zarr_provenance(root)
                for roi in rois:
                    name = str(roi.get("name") or "")
                    group = root[name] if name and name in root else None
                    if group is None:
                        for group_name in root.group_keys():
                            candidate = root[group_name]
                            if str(candidate.attrs.get("roi_name") or group_name) == name:
                                group = candidate
                                break
                    if group is not None:
                        group.attrs["roi_name"] = name
                        group.attrs["analysis_region_type"] = analysis_region_type
                        group.attrs["bbox_fullres"] = roi.get("bbox_fullres") or []
                        group.attrs["polygon_fullres"] = roi.get("polygon_fullres") or []
                        group.attrs["shape"] = roi.get("shape") or self._roi_shape_from_bbox(roi.get("bbox_fullres"))
            except Exception as e:
                print(f"[Step0] failed to update corrected zarr attrs: {e}")

        # v14.4: validate the corrected output (a directory existing is NOT proof
        # of a valid corrected zarr) and report it honestly to the UI + manifest.
        corrected_report = corrected_zarr_report(corrected_path)
        self._refresh_bg_corrected_status(corrected_report)

        manifest = {
            "version": "v6_roi_handoff_1",
            "created_from_step": CREATED_FROM_STEP0_BACKGROUND_CORRECTION,
            "output_kind": CORRECTED_ZARR_OUTPUT_KIND,
            "corrected_zarr_valid": bool(corrected_report["non_empty"]),
            "corrected_zarr_n_channel_arrays": int(corrected_report["n_channel_arrays"]),
            "roi_id": roi_id,
            "display_name": rois[0]["name"] if rois else "",
            "analysis_region_type": analysis_region_type,
            "mode": "full_wsi" if analysis_region_type == "full_wsi" else "roi_only",
            "project_output_dir": os.path.abspath(project_dir),
            "roi_dir": os.path.abspath(roi_dir) if roi_dir else "",
            "step0_dir": os.path.abspath(step0_dir),
            "step1_dir": os.path.abspath(self._roi_context["step_dirs"]["step1"]) if self._roi_context else "",
            "step2_dir": os.path.abspath(self._roi_context["step_dirs"]["step2"]) if self._roi_context else "",
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
            "analysis_region_type": manifest.get("analysis_region_type", "roi"),
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
