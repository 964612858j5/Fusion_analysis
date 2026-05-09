"""
block01/ui/main_window.py — MainWindow.
"""

import os
import gc
import glob
import json
import time
import traceback
import multiprocessing as mp
from queue import Empty

import numpy as np
import pyqtgraph as pg
import zarr

from PyQt5 import QtWidgets, QtCore, QtGui
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QStackedWidget, QMessageBox, QProgressBar,
    QApplication, QSplitter, QCheckBox, QDialog,
)

from ..config import (
    OME_TIFF_FILE, OUTPUT_DIR, PREVIEW_DOWNSAMPLE,
    NORM_LOW, NORM_HIGH, PATCH_COLORS,
)
from ..core.fusion_engine import FusionEngine
from ..core.io_loader import OMETIFFLoader
from ..utils.segmentation_config import (
    CELLPOSE_NUCLEI_DAPI,
    CELLPOSE_WHOLECELL_FUSION,
    STARDIST_NUCLEI_DAPI,
    STARDIST_NUCLEI_EXPANSION,
    normalize_segmentation_config,
)
from ..workers.cellpose_worker import PreviewLoaderThread, run_cellpose_process
from .step0.step0_page import Step0Page
from .step0.config_panel import ConfigPanel
from .step0.search_ctrl import SearchCtrlPanel
from .step0.result_grid import ResultGridPanel
from .step0.overview_panel import OverviewPanel, TileSelectDialog, FullFusionWorker
from .step1_5_bg_page import Step15BackgroundCorrectionPage
from .step2_page import Step2Page
from .step3_page import Step3Page
from .step4_page import Step4Page

class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("CODEX Pipeline  |  Fusion + Segmentation")
        self.resize(1800, 960)

        self.loader = None   # loaded on demand when user clicks "Load"
        self.fusion = FusionEngine()
        self.worker = None

        self._p1_diam            = None
        self._p2_params          = None
        self._preview_patch_idx  = -1
        self._all_patches        = []
        self._patch_channel_cache: dict = {}
        self._patch_loaders: dict = {}
        self._patch_load_ready: set = set()
        self._roi_patch_items: list = []
        self._fused_zarr_path    = None
        self._rois               = []
        self._active_roi         = None
        self._corrected_zarr_path = ""
        self._corrected_zarr_mode = ""
        self._corrected_decisions = {}
        self._params_source      = None  # 'phase2'|'loaded'|'manual' — tracks how params were set
        self.proc                = None
        self._proc_queue         = None
        self._proc_stop_flag     = None
        self._proc_stopped       = False
        self.is_sequential_flow  = False
        self.step0_output        = {}
        self.step1_output        = None
        self.step2_output        = None
        self.step3_output        = None
        self.step0_done          = False
        self.step1_done          = False
        self.step2_done          = False
        self.step3_done          = False
        self.step4_done          = False
        self._current_step       = 0

        self._preload_debounce = QTimer()
        self._preload_debounce.setSingleShot(True)
        self._preload_debounce.timeout.connect(self._preload_all_patches)

        self._prev_timer = QTimer()
        self._prev_timer.setSingleShot(True)
        self._prev_timer.timeout.connect(self._render_current_patch)

        self._proc_poll_timer = QTimer()
        self._proc_poll_timer.setInterval(100)
        self._proc_poll_timer.timeout.connect(self._poll_cellpose_process)

        self._step1_restore_active = False
        self._step1_session_timer = QTimer()
        self._step1_session_timer.setSingleShot(True)
        self._step1_session_timer.timeout.connect(self._save_step1_session)

        self._build_ui()

    # ── UI ──────────────────────────────────────────────────────────

    def _build_ui(self):
        outer_w = QWidget()
        self.setCentralWidget(outer_w)
        outer_lay = QVBoxLayout(outer_w)
        outer_lay.setContentsMargins(0, 0, 0, 0)
        outer_lay.setSpacing(0)

        step_bar = QHBoxLayout()
        step_bar.setContentsMargins(8, 4, 8, 4)
        self._step0_lbl = QLabel("● Step 0: Setup")
        self._step0_lbl.setStyleSheet(
            "font-size:12px;font-weight:bold;color:#61afef;padding:4px 12px;"
            "background:#1a2a3a;border-radius:4px;"
        )
        self._step1_lbl = QLabel("○ Step 1: Fusion")
        self._step1_lbl.setStyleSheet(
            "font-size:12px;color:#555;padding:4px 12px;"
        )
        self._step2_lbl = QLabel("○ Step 2: Segmentation & Merge")
        self._step2_lbl.setStyleSheet(
            "font-size:12px;color:#555;padding:4px 12px;"
        )
        self._step3_lbl = QLabel("○ Step 3: QC Viewer")
        self._step3_lbl.setStyleSheet(
            "font-size:12px;color:#555;padding:4px 12px;"
        )
        self._step4_lbl = QLabel("○ Step 4: Feature Extraction")
        self._step4_lbl.setStyleSheet(
            "font-size:12px;color:#555;padding:4px 12px;"
        )
        for lbl, handler in (
            (self._step0_lbl, self._go_to_step0),
            (self._step1_lbl, self._go_to_step1),
            (self._step2_lbl, self._go_to_step2),
            (self._step3_lbl, self._go_to_step3),
            (self._step4_lbl, self._go_to_step4),
        ):
            lbl.setCursor(Qt.PointingHandCursor)
            lbl.mousePressEvent = lambda _ev, fn=handler: fn()
        step_bar.addWidget(self._step0_lbl)
        step_bar.addWidget(QLabel("  →  "))
        step_bar.addWidget(self._step1_lbl)
        step_bar.addWidget(QLabel("  →  "))
        step_bar.addWidget(self._step2_lbl)
        step_bar.addWidget(QLabel("  →  "))
        step_bar.addWidget(self._step3_lbl)
        step_bar.addWidget(QLabel("  →  "))
        step_bar.addWidget(self._step4_lbl)
        step_bar.addStretch()
        self._btn_skip = QPushButton("Skip → Step 2")
        self._btn_skip.setStyleSheet(
            "QPushButton{background:#2a2a2a;color:#bbbbbb;font-size:10px;"
            "border:1px solid #444;border-radius:3px;padding:3px 10px;}"
            "QPushButton:hover{background:#3a3a3a;color:#dddddd;border-color:#555;}"
        )
        self._btn_skip.clicked.connect(self._skip_to_step2)
        step_bar.addWidget(self._btn_skip)

        self._btn_skip3 = QPushButton("Skip → Step 3")
        self._btn_skip3.setStyleSheet(
            "QPushButton{background:#2a2a2a;color:#bbbbbb;font-size:10px;"
            "border:1px solid #444;border-radius:3px;padding:3px 10px;}"
            "QPushButton:hover{background:#3a3a3a;color:#dddddd;border-color:#555;}"
        )
        self._btn_skip3.clicked.connect(self._skip_to_step3)
        step_bar.addWidget(self._btn_skip3)

        self._btn_skip4 = QPushButton("Skip → Step 4")
        self._btn_skip4.setStyleSheet(
            "QPushButton{background:#2a2a2a;color:#bbbbbb;font-size:10px;"
            "border:1px solid #444;border-radius:3px;padding:3px 10px;}"
            "QPushButton:hover{background:#3a3a3a;color:#dddddd;border-color:#555;}"
        )
        self._btn_skip4.clicked.connect(self._skip_to_step4)
        step_bar.addWidget(self._btn_skip4)

        self._btn_next = QPushButton("Next")
        self._btn_next.setEnabled(False)
        self._btn_next.clicked.connect(self._go_next_step)
        step_bar.addWidget(self._btn_next)
        self._update_next_button()
        outer_lay.addLayout(step_bar)

        self._stack = QtWidgets.QStackedWidget()
        outer_lay.addWidget(self._stack, stretch=1)

        self._step0 = Step0Page()
        self._step0.step0_complete.connect(self._on_step0_complete)
        self._stack.addWidget(self._step0)

        self._ome_path_edit = self._step0._ome_path_edit
        self._out_path_edit = self._step0._out_path_edit
        self._panel_csv_edit = self._step0._panel_csv_edit

        page1_w = QWidget()
        root = QVBoxLayout(page1_w)
        root.setContentsMargins(6, 4, 6, 6)
        root.setSpacing(4)

        title = self._make_label("Step 1 — Channel Fusion + Cellpose Grid Search", bold=True)
        root.addWidget(title)

        main_split = QSplitter(Qt.Horizontal)

        # Left: read-only ROI/patch overview for Step1. ROI and patches come
        # from Step0; drawing/editing remains owned by Step0.
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(4)
        ll.addWidget(self._make_label("① ROI / Patch Overview", bold=True))
        self.roi_gv = pg.GraphicsLayoutWidget()
        self.roi_gv.setBackground("#111")
        self.roi_vb = self.roi_gv.addViewBox()
        self.roi_vb.setAspectLocked(True)
        self.roi_vb.invertY(True)
        self.roi_img = pg.ImageItem()
        self.roi_vb.addItem(self.roi_img)
        ll.addWidget(self.roi_gv, stretch=1)
        self.roi_status = QLabel("No ROI loaded")
        self.roi_status.setAlignment(Qt.AlignCenter)
        self.roi_status.setWordWrap(True)
        self.roi_status.setStyleSheet("color:#888;font-size:10px;")
        ll.addWidget(self.roi_status)
        main_split.addWidget(left)

        mid = QSplitter(Qt.Vertical)
        pw = QWidget()
        pl = QVBoxLayout(pw)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.addWidget(self._make_label(
            "② Fusion Preview  Red=cyto  Blue=nucleus  (real-time update)",
            bold=True,
        ))

        sel_row = QHBoxLayout()
        sel_row.addWidget(QLabel("Preview patch:"))
        self._patch_sel_btns = []
        self._patch_sel_container = QHBoxLayout()
        self._patch_sel_container.setSpacing(4)
        sel_row.addLayout(self._patch_sel_container)
        sel_row.addStretch()
        pl.addLayout(sel_row)

        status_row = QHBoxLayout()
        self.prev_status = QLabel("Please define a patch in Step 0 first")
        self.prev_status.setAlignment(Qt.AlignCenter)
        self.prev_status.setStyleSheet("color:#777;font-size:10px;")
        self.prev_status.setWordWrap(True)
        status_row.addWidget(self.prev_status, stretch=1)

        btn_load_step0 = QPushButton("Load Step0 ROI Result")
        btn_load_step0.setStyleSheet(
            "QPushButton{color:#6bcb77;font-size:10px;"
            "border:1px solid #6bcb77;border-radius:3px;padding:2px 8px;}"
            "QPushButton:hover{background:#13251a;}"
        )
        btn_load_step0.clicked.connect(self._load_step0_roi_result)
        status_row.addWidget(btn_load_step0)

        btn_load_session = QPushButton("Load Previous Step1 Session")
        btn_load_session.setStyleSheet(
            "QPushButton{color:#8cf;font-size:10px;"
            "border:1px solid #8cf;border-radius:3px;padding:2px 8px;}"
            "QPushButton:hover{background:#122333;}"
        )
        btn_load_session.clicked.connect(self._load_previous_step1_session)
        status_row.addWidget(btn_load_session)

        btn_save_session = QPushButton("Save Session")
        btn_save_session.setStyleSheet(
            "QPushButton{color:#aaa;font-size:10px;"
            "border:1px solid #555;border-radius:3px;padding:2px 8px;}"
            "QPushButton:hover{background:#222;}"
        )
        btn_save_session.clicked.connect(self._save_step1_session)
        status_row.addWidget(btn_save_session)

        btn_update = QPushButton("⟳ Update")
        btn_update.setFixedWidth(72)
        btn_update.setStyleSheet(
            "QPushButton{color:#fa8;font-size:10px;"
            "border:1px solid #fa8;border-radius:3px;padding:2px;}"
            "QPushButton:hover{background:#321;}"
        )
        btn_update.setToolTip(
            "Force reload all patch channel data from disk.\n"
            "Use when you have changed channel groups/weights\n"
            "and want the cache to reflect the new set of channels."
        )
        btn_update.clicked.connect(self._force_update_all)
        status_row.addWidget(btn_update)
        pl.addLayout(status_row)
        self.prev_gv = pg.GraphicsLayoutWidget()
        self.prev_gv.setBackground("#111")
        self.prev_vb = self.prev_gv.addViewBox()
        self.prev_vb.setAspectLocked(True)
        self.prev_vb.invertY(True)
        self.prev_img = pg.ImageItem()
        self.prev_vb.addItem(self.prev_img)
        pl.addWidget(self.prev_gv, stretch=1)
        mid.addWidget(pw)

        # Fusion config
        self.config = ConfigPanel([])
        self.config.config_changed.connect(self._on_cfg_changed)
        mid.addWidget(self.config)

        mid.setStretchFactor(0, 1)
        mid.setStretchFactor(1, 1)
        main_split.addWidget(mid)

        right = QSplitter(Qt.Vertical)
        self.search = SearchCtrlPanel()
        self.search.run_p1.connect(self._run_p1)
        self.search.run_p2.connect(self._run_p2)
        self.search.run_preview.connect(self._run_direct_patch_preview)
        self.search.stop.connect(self._stop)
        self.search.params_ready.connect(self._on_params_ready)
        self.search.method_changed.connect(self._on_step1_segmentation_mode_changed)
        right.addWidget(self.search)

        self.result_grid = ResultGridPanel()
        self.result_grid.param_selected.connect(self._on_param_sel)
        right.addWidget(self.result_grid)

        right.setStretchFactor(0, 0)
        right.setStretchFactor(1, 1)
        main_split.addWidget(right)

        main_split.setStretchFactor(0, 2)
        main_split.setStretchFactor(1, 3)
        main_split.setStretchFactor(2, 4)
        root.addWidget(main_split, stretch=1)

        bot = QHBoxLayout()
        self._btn_back_to_step0 = QPushButton("← Back to Step 0")
        self._btn_back_to_step0.setStyleSheet(
            "QPushButton{color:#fa8;border:1px solid #fa8;border-radius:4px;padding:6px 16px;}"
            "QPushButton:hover{background:#321;}"
        )
        self._btn_back_to_step0.clicked.connect(self._go_to_step0)
        bot.addWidget(self._btn_back_to_step0)
        bot.addStretch()
        self.btn_save = QPushButton("💾  Save Config  &  Generate fused.zarr")
        self.btn_save.setEnabled(False)
        self.btn_save.setStyleSheet(
            "QPushButton{background:#246;color:white;"
            "border-radius:5px;padding:8px 20px;"
            "font-size:13px;font-weight:bold;}"
            "QPushButton:enabled{background:#258;}"
            "QPushButton:enabled:hover{background:#36a;}"
            "QPushButton:disabled{background:#333;color:#555;}"
        )
        self.btn_save.clicked.connect(self._save)
        bot.addWidget(self.btn_save)
        root.addLayout(bot)

        self._fusion_bar_widget = QWidget()
        fbl = QVBoxLayout(self._fusion_bar_widget)
        fbl.setContentsMargins(0, 2, 0, 2)
        fbl.setSpacing(2)

        self._fusion_pbar = QProgressBar()
        self._fusion_pbar.setRange(0, 100)
        self._fusion_pbar.setValue(0)
        self._fusion_pbar.setStyleSheet(
            "QProgressBar{border:1px solid #444;border-radius:3px;"
            "text-align:center;color:#fff;height:18px;}"
            "QProgressBar::chunk{background:#2a5;border-radius:3px;}"
        )
        fbl.addWidget(self._fusion_pbar)

        self._fusion_lbl = QLabel("")
        self._fusion_lbl.setAlignment(Qt.AlignCenter)
        self._fusion_lbl.setStyleSheet("color:#aaa;font-size:10px;")
        self._fusion_lbl.setWordWrap(True)
        fbl.addWidget(self._fusion_lbl)

        self._fusion_bar_widget.setVisible(False)
        root.addWidget(self._fusion_bar_widget)
        self._stack.addWidget(page1_w)

        self._step2 = Step2Page()
        self._step2.go_back.connect(self._go_to_step1)
        self._step2.segmentation_done.connect(self._go_to_step3)
        self._stack.addWidget(self._step2)

        self._step3 = Step3Page()
        self._step3.go_back.connect(self._go_to_step2)
        self._step3.go_step4.connect(self._go_to_step4)
        self._stack.addWidget(self._step3)

        self._step4 = Step4Page()
        self._step4.go_back.connect(self._go_to_step3)
        self._stack.addWidget(self._step4)

        self._stack.setCurrentIndex(0)
        self._set_step_active(0)

    def _go_to_step0(self):
        if self._current_step == 1:
            self._stop_all_loaders()
        self._stack.setCurrentIndex(0)
        self._set_step_active(0)

    def _on_step0_complete(self, payload):
        global OME_TIFF_FILE, OUTPUT_DIR
        self.step0_done = True
        self.step0_output = dict(payload or {})
        self.loader = self.step0_output.get("loader")
        self._all_patches = list(self.step0_output.get("patches") or [])
        self._rois = list(self.step0_output.get("rois") or [])
        OME_TIFF_FILE = self.step0_output.get("ome_tiff_path", OME_TIFF_FILE)
        OUTPUT_DIR = self.step0_output.get("output_dir", OUTPUT_DIR)

        correction_config = self.step0_output.get("correction_config")
        corrected_zarr_path = self.step0_output.get("corrected_zarr_path")
        corrected_decisions = self.step0_output.get("corrected_decisions") or {}
        if self.loader is not None:
            self.loader.set_correction_config(correction_config)
            self.loader.set_corrected_zarr_store(corrected_zarr_path, corrected_decisions)
        self._corrected_zarr_path = corrected_zarr_path or ""
        self._corrected_decisions = dict(corrected_decisions)

        self._stop_all_loaders()
        self._patch_channel_cache.clear()
        self._patch_load_ready.clear()
        self._preview_patch_idx = -1
        self.prev_img.clear()
        self.prev_status.setText("Preloading selected patches…")

        if self.loader is not None:
            self.config.all_channels = self.loader.channel_names()
            self.config.nuc_combo.clear()
            self.config.nuc_combo.addItems(self.loader.channel_names())
            self.config.nuc_combo.setCurrentIndex(-1)
            self.config.nuc_row.spin.setValue(0.0)
            self.config.load_panel(
                self.step0_output.get("panel_groups") or {},
                self.step0_output.get("panel_nucleus"),
            )

        self._load_step0_roi_result(auto=True)
        self.step1_done = False
        self._step2._out_edit.setText(OUTPUT_DIR)
        self._step4._ome_edit.setText(OME_TIFF_FILE)
        self._step4._out_edit.setText(OUTPUT_DIR)
        self._stack.setCurrentIndex(1)
        self._set_step_active(1)

    def _load_step0_roi_result(self, _checked=False, auto=False):
        global OME_TIFF_FILE, OUTPUT_DIR
        print("[Step1] loading Step0 ROI result")
        if self.loader is None:
            if not self._bootstrap_step1_context_from_disk(auto=auto):
                if not auto:
                    QMessageBox.warning(
                        self,
                        "Step1",
                        "Load Step0 first, or select a Step0 output directory "
                        "that contains correction_config.json / roi_config.json / corrected_channels.zarr.",
                    )
                return

        out_dir = self.step0_output.get("output_dir") or OUTPUT_DIR
        manifest_path = os.path.join(out_dir, "step0_roi_result.json")
        manifest = {}
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
                out_dir = manifest.get("output_dir") or out_dir
            except Exception as e:
                print(f"[Step1] failed to load step0_roi_result.json: {e}")
        print(f"[Step1] manifest found={bool(manifest)}")

        raw_ome = manifest.get("raw_ome_path")
        if raw_ome and os.path.exists(raw_ome) and (
            self.loader is None or getattr(self.loader, "filepath", "") != raw_ome
        ):
            try:
                OME_TIFF_FILE = raw_ome
                OUTPUT_DIR = out_dir
                self.loader = OMETIFFLoader(raw_ome)
                self.step0_output["loader"] = self.loader
                self.step0_output["ome_tiff_path"] = raw_ome
            except Exception:
                print(f"[Step1] failed to load raw OME from manifest:\n{traceback.format_exc()}")
        print(f"[Step1] loader initialized={self.loader is not None}")
        if self.loader is not None:
            print(f"[Step1] loader channels count={len(self.loader.channel_names())}")

        corr_path = (
            manifest.get("corrected_zarr_path")
            or self.step0_output.get("corrected_zarr_path")
            or self._corrected_zarr_path
            or os.path.join(out_dir, "corrected_channels.zarr")
        )
        cfg_path = manifest.get("correction_config_path") or os.path.join(out_dir, "correction_config.json")
        roi_path = manifest.get("roi_config_path") or os.path.join(out_dir, "roi_config.json")
        patch_path = manifest.get("patch_config_path") or os.path.join(out_dir, "patch_config.json")
        print(f"[Step1] raw_ome={getattr(self.loader, 'filepath', raw_ome or '')}")
        print(f"[Step1] corrected_zarr={corr_path}")

        correction_config = self.step0_output.get("correction_config")
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    correction_config = json.load(f)
            except Exception as e:
                print(f"[Step1] failed to load correction_config.json: {e}")

        rois = list(self.step0_output.get("rois") or self._rois or [])
        if os.path.exists(roi_path):
            try:
                with open(roi_path, "r", encoding="utf-8") as f:
                    rois = json.load(f)
            except Exception as e:
                print(f"[Step1] failed to load roi_config.json: {e}")

        corrected_mode = ""
        if corr_path and os.path.exists(corr_path):
            try:
                root = zarr.open(corr_path, mode="r")
                corrected_mode = str(root.attrs.get("mode", "")).strip().lower()
                if corrected_mode == "roi_only" and not rois:
                    rois = []
                    for group_name in root.group_keys():
                        group = root[group_name]
                        rois.append({
                            "name": group.attrs.get("roi_name", group_name),
                            "bbox_fullres": list(group.attrs.get("bbox_fullres", [])),
                            "polygon_fullres": group.attrs.get("polygon_fullres"),
                            "patch_indices": [],
                        })
            except Exception as e:
                print(f"[Step1] failed to inspect corrected zarr: {e}")
        self._corrected_zarr_path = corr_path if corr_path and os.path.exists(corr_path) else ""
        self._corrected_zarr_mode = corrected_mode
        print(f"[Step1] corrected_zarr_mode={corrected_mode or 'none'}")

        decisions = {}
        if correction_config:
            decisions = {
                str(ch): str(method).strip().lower()
                for ch, method in (correction_config.get("channel_decisions") or {}).items()
                if str(method).strip().lower() in {"tophat", "cucim"}
            }
        decisions.update(self.step0_output.get("corrected_decisions") or {})
        self._corrected_decisions = decisions
        self.loader.set_correction_config(correction_config)
        self.loader.set_corrected_zarr_store(self._corrected_zarr_path, decisions)

        self._rois = list(rois or [])
        self._active_roi = self._rois[0] if self._rois else None
        if corrected_mode == "roi_only" and not self._active_roi:
            msg = "No ROI found for ROI-only corrected zarr."
            print(f"[Step1] {msg}")
            if not auto:
                QMessageBox.warning(self, "Step1", msg)
            return

        patches = list(self.step0_output.get("patches") or self._all_patches or [])
        if os.path.exists(patch_path):
            try:
                with open(patch_path, "r", encoding="utf-8") as f:
                    patch_cfg = json.load(f)
                patches = []
                for item in patch_cfg:
                    if isinstance(item, dict):
                        coords = item.get("coords") or item.get("bbox_fullres")
                    else:
                        coords = item
                    if coords and len(coords) == 4:
                        patches.append(tuple(int(v) for v in coords))
            except Exception as e:
                print(f"[Step1] failed to load patch_config.json: {e}")
        print(f"[Step1] rois={len(rois or [])}")
        print(f"[Step1] patches={len(patches or [])}")
        if self._active_roi:
            patches = self._filter_patches_to_roi(patches, self._active_roi)
            print(f"[Step1] active_roi={self._active_roi.get('name', 'ROI_1')}")
            print(f"[Step1] roi_bbox={self._active_roi.get('bbox_fullres')}")
            print(f"[Step1] n_patches={len(patches)}")
            if self._corrected_zarr_path:
                print(
                    "[Step1] loading channels from "
                    f"corrected_channels.zarr/{self._active_roi.get('name', 'ROI_1')}"
                )
        elif not auto:
            print("[Step1] No ROI found. Load full WSI mode.")

        if self.loader is not None:
            channels = self.loader.channel_names()
            nucleus_channel = self._choose_step1_nucleus_channel(
                channels,
                manifest=manifest,
                correction_config=correction_config,
            )
            panel_groups = self.step0_output.get("panel_groups") or {}
            source = "step0_panel"
            if not panel_groups:
                panel_groups = {
                    "markers": {
                        ch: 0.0
                        for ch in channels
                        if ch != nucleus_channel
                    }
                }
                source = "default_all_channels"
            self.config.all_channels = channels
            self.config.nuc_combo.clear()
            self.config.nuc_combo.addItems(channels)
            self.config.load_panel(panel_groups, nucleus_channel)
            idx = self.config.nuc_combo.findText(nucleus_channel)
            if idx >= 0:
                self.config.nuc_combo.setCurrentIndex(idx)
            self.config.nuc_row.spin.setValue(0.0)
            self._zero_marker_weights()
            print(f"[Step1] nucleus_channel={nucleus_channel}")
            print(f"[Step1] panel_groups source={source}")
            print(f"[Step1] config panel initialized={bool(self.config._panels)}")

        self._stop_all_loaders()
        self._patch_channel_cache.clear()
        self._patch_load_ready.clear()
        self._preview_patch_idx = -1
        self._on_rois_changed(self._rois)
        self._on_patches(patches)
        self._show_active_roi_preview()
        print("[Step1] preloading patches...")
        if not auto:
            self.prev_status.setText("Step0 ROI result loaded.")
        self._schedule_step1_session_save()

    @staticmethod
    def _choose_step1_nucleus_channel(channels, manifest=None, correction_config=None):
        channels = list(channels or [])
        manifest = manifest or {}
        correction_config = correction_config or {}
        for candidate in (
            manifest.get("nucleus_channel"),
            correction_config.get("nucleus_channel"),
        ):
            if candidate and candidate in channels:
                return candidate
        for candidate in channels:
            if candidate == "DAPI":
                return candidate
        for candidate in channels:
            if "dapi" in candidate.lower():
                return candidate
        for candidate in channels:
            if "nuc" in candidate.lower():
                return candidate
        return channels[0] if channels else ""

    def _bootstrap_step1_context_from_disk(self, auto=False):
        """Create the minimum Step1 context from Step0 files on disk.

        This supports GUI restart → Step1 → Load Step0 ROI Result without an
        in-memory Step0 payload.  It does not invent patches; patch_config.json
        must exist if the user wants previous patches restored.
        """
        global OME_TIFF_FILE, OUTPUT_DIR

        out_candidates = []
        for p in (
            self._out_path_edit.text().strip() if hasattr(self, "_out_path_edit") else "",
            OUTPUT_DIR,
            os.path.join(os.getcwd(), "outcome"),
            "/sda1/Fusion/analysis_pipline/outcome",
        ):
            if p and p not in out_candidates:
                out_candidates.append(p)

        out_dir = ""
        if auto:
            for p in out_candidates:
                if os.path.exists(os.path.join(p, "corrected_channels.zarr")) or os.path.exists(os.path.join(p, "roi_config.json")):
                    out_dir = p
                    break
        else:
            out_dir = QtWidgets.QFileDialog.getExistingDirectory(
                self,
                "Select Step0 output directory",
                OUTPUT_DIR if os.path.isdir(OUTPUT_DIR) else os.getcwd(),
            )
        if not out_dir:
            return False

        manifest = {}
        manifest_path = os.path.join(out_dir, "step0_roi_result.json")
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
                out_dir = manifest.get("output_dir") or out_dir
            except Exception as e:
                print(f"[Step1] failed to load step0_roi_result.json during bootstrap: {e}")

        ome_candidates = []
        for p in (
            manifest.get("raw_ome_path"),
            self._ome_path_edit.text().strip() if hasattr(self, "_ome_path_edit") else "",
            OME_TIFF_FILE,
        ):
            if p and p not in ome_candidates:
                ome_candidates.append(p)
        parent = os.path.dirname(out_dir)
        if parent and os.path.isdir(parent):
            ome_candidates.extend(glob.glob(os.path.join(parent, "*.ome.tif")))
            ome_candidates.extend(glob.glob(os.path.join(parent, "*.ome.tiff")))

        ome_path = next((p for p in ome_candidates if p and os.path.exists(p)), "")
        if not ome_path and not auto:
            ome_path, _ = QtWidgets.QFileDialog.getOpenFileName(
                self,
                "Select raw OME-TIFF for Step1",
                parent if parent and os.path.isdir(parent) else os.getcwd(),
                "OME-TIFF (*.ome.tif *.ome.tiff *.tif *.tiff)",
            )
        if not ome_path or not os.path.exists(ome_path):
            if not auto:
                QMessageBox.warning(self, "Step1", "Raw OME-TIFF path missing.")
            return False

        try:
            self.loader = OMETIFFLoader(ome_path)
        except Exception:
            print(f"[Step1] failed to create OME loader:\n{traceback.format_exc()}")
            if not auto:
                QMessageBox.warning(self, "Step1", "Failed to load raw OME-TIFF. See terminal.")
            return False

        OUTPUT_DIR = out_dir
        OME_TIFF_FILE = ome_path
        self.step0_output = {
            "output_dir": out_dir,
            "ome_tiff_path": ome_path,
            "loader": self.loader,
            "corrected_zarr_path": manifest.get("corrected_zarr_path") or os.path.join(out_dir, "corrected_channels.zarr"),
        }
        self.step0_done = True
        self._step2._out_edit.setText(out_dir)
        self._step4._ome_edit.setText(ome_path)
        self._step4._out_edit.setText(out_dir)
        print(f"[Step1] bootstrapped from disk output_dir={out_dir}")
        print(f"[Step1] raw_ome={ome_path}")
        if not os.path.exists(os.path.join(out_dir, "patch_config.json")):
            print("[Step1] patch_config.json not found; previous patches cannot be restored from Step0 output.")
        return True

    def _step1_session_path(self, output_dir=None):
        base = output_dir or (self.step0_output or {}).get("output_dir") or OUTPUT_DIR
        return os.path.join(base, "step1_session.json")

    def _find_step1_session(self):
        candidates = []
        for base in {
            OUTPUT_DIR,
            (self.step0_output or {}).get("output_dir"),
            self._out_path_edit.text().strip() if hasattr(self, "_out_path_edit") else "",
        }:
            if base:
                candidates.append(os.path.join(base, "step1_session.json"))
        parent = os.path.dirname(OUTPUT_DIR)
        if parent and os.path.isdir(parent):
            candidates.extend(glob.glob(os.path.join(parent, "*", "step1_session.json")))
        existing = [p for p in candidates if p and os.path.exists(p)]
        if not existing:
            return ""
        return max(existing, key=lambda p: os.path.getmtime(p))

    def _step1_session_payload(self):
        if self.loader is None:
            return None
        out_dir = (self.step0_output or {}).get("output_dir") or OUTPUT_DIR
        active_roi = self._active_roi or (self._rois[0] if self._rois else None)
        roi_bbox = active_roi.get("bbox_fullres") if active_roi else None
        ry0, _, rx0, _ = [0, 0, 0, 0]
        if roi_bbox and len(roi_bbox) == 4:
            ry0, _, rx0, _ = [int(v) for v in roi_bbox]
        patches = []
        for i, patch in enumerate(self._all_patches):
            y0, y1, x0, x1 = [int(v) for v in patch]
            item = {
                "name": f"P{i+1}",
                "bbox_fullres": [y0, y1, x0, x1],
            }
            if roi_bbox and len(roi_bbox) == 4:
                item["bbox_local"] = [y0 - ry0, y1 - ry0, x0 - rx0, x1 - rx0]
            patches.append(item)

        fusion_cfg = self.config.get_full_config()
        channel_weights = {}
        channel_visibility = {}
        for gdata in fusion_cfg.get("groups", {}).values():
            for ch, weight in (gdata.get("channels") or {}).items():
                channel_weights[ch] = float(weight)
                channel_visibility[ch] = float(weight) > 0
        nuc = fusion_cfg.get("nucleus") or {}
        if nuc.get("channel"):
            channel_weights[nuc["channel"]] = float(nuc.get("weight", 0.0))
            channel_visibility[nuc["channel"]] = float(nuc.get("weight", 0.0)) > 0

        return {
            "version": 1,
            "mode": "roi" if active_roi else "full_wsi",
            "active_roi": active_roi.get("name", "ROI_1") if active_roi else "",
            "roi_bbox": roi_bbox,
            "rois": self._rois,
            "patches": patches,
            "selected_patch": f"P{self._preview_patch_idx+1}" if 0 <= self._preview_patch_idx < len(self._all_patches) else "",
            "corrected_zarr_path": self._corrected_zarr_path or os.path.join(out_dir, "corrected_channels.zarr"),
            "corrected_zarr_mode": self._corrected_zarr_mode,
            "fusion_zarr_path": self._fused_zarr_path or (self.step1_output or {}).get("zarr_path", ""),
            "raw_ome_path": getattr(self.loader, "filepath", OME_TIFF_FILE),
            "output_dir": out_dir,
            "fusion_config": fusion_cfg,
            "channel_weights": channel_weights,
            "channel_visibility": channel_visibility,
            "channel_colors": {},
            "p2_params": self._p2_params,
            "p1_diam": self._p1_diam,
            "params_source": self._params_source,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _schedule_step1_session_save(self):
        if self._step1_restore_active:
            return
        if self.loader is None:
            return
        self._step1_session_timer.start(500)

    def _save_step1_session(self):
        if self._step1_restore_active:
            return
        payload = self._step1_session_payload()
        if not payload:
            return
        out_dir = payload.get("output_dir") or OUTPUT_DIR
        try:
            os.makedirs(out_dir, exist_ok=True)
            path = self._step1_session_path(out_dir)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            print("[Step1] session autosaved")
        except Exception:
            print(f"[Step1] failed to autosave session:\n{traceback.format_exc()}")

    def _apply_step1_fusion_config(self, cfg):
        cfg = dict(cfg or {})
        nucleus_cfg = cfg.get("nucleus") or {}
        groups_cfg = cfg.get("groups") or {}
        for name in list(self.config._panels.keys()):
            self.config._del_group(name)

        nuc_ch = str(nucleus_cfg.get("channel") or "")
        self.config.nuc_combo.clear()
        self.config.nuc_combo.addItems(self.loader.channel_names() if self.loader else [])
        idx = self.config.nuc_combo.findText(nuc_ch)
        self.config.nuc_combo.setCurrentIndex(idx if idx >= 0 else -1)
        self.config.nuc_row.spin.setValue(float(nucleus_cfg.get("weight", 0.0) or 0.0))

        for gname, gdata in groups_cfg.items():
            channels = {
                str(ch): float(w)
                for ch, w in (gdata.get("channels") or {}).items()
                if not self.loader or ch in self.loader.channel_names()
            }
            self.config._add_group(str(gname), channels)
            panel = self.config._panels.get(str(gname))
            if panel:
                panel.gw_row.spin.setValue(float(gdata.get("group_weight", 1.0)))
        self.config.config_changed.emit()

    def _fusion_config_from_flat_weights(self, sess):
        weights = dict(sess.get("channel_weights") or {})
        if not weights:
            return {}
        channels = self.loader.channel_names() if self.loader else []
        nuc_ch = "DAPI" if "DAPI" in weights else (channels[0] if channels else "")
        nuc_weight = float(weights.get(nuc_ch, 0.0)) if nuc_ch else 0.0
        marker_weights = {
            ch: float(w)
            for ch, w in weights.items()
            if ch != nuc_ch and (not channels or ch in channels)
        }
        return {
            "nucleus": {"channel": nuc_ch, "weight": nuc_weight},
            "groups": {
                "restored": {
                    "group_weight": 1.0,
                    "channels": marker_weights,
                }
            },
        }

    def _load_previous_step1_session(self, _checked=False, auto=False, path=None):
        global OME_TIFF_FILE, OUTPUT_DIR
        print("[Step1] searching previous session")
        session_path = path or self._find_step1_session()
        if not session_path and not auto:
            session_path, _ = QtWidgets.QFileDialog.getOpenFileName(
                self,
                "Load Previous Step1 Session",
                OUTPUT_DIR,
                "Step1 Session (step1_session.json);;JSON (*.json)",
            )
        print(f"[Step1] session found={bool(session_path and os.path.exists(session_path))}")
        if not session_path or not os.path.exists(session_path):
            if not auto:
                QMessageBox.information(self, "Step1", "No previous Step1 session found.")
            return False

        self._step1_restore_active = True
        try:
            print(f"[Step1] loading session={session_path}")
            with open(session_path, "r", encoding="utf-8") as f:
                sess = json.load(f)

            out_dir = sess.get("output_dir") or os.path.dirname(session_path)
            raw_ome = sess.get("raw_ome_path") or OME_TIFF_FILE
            if not raw_ome or not os.path.exists(raw_ome):
                QMessageBox.warning(self, "Step1", "Raw OME-TIFF path missing. Please load Step0 or edit the session.")
                return False
            OUTPUT_DIR = out_dir
            OME_TIFF_FILE = raw_ome
            self.step0_output = {
                "output_dir": out_dir,
                "ome_tiff_path": raw_ome,
            }
            self.loader = OMETIFFLoader(raw_ome)

            corr_path = sess.get("corrected_zarr_path") or os.path.join(out_dir, "corrected_channels.zarr")
            if corr_path and not os.path.exists(corr_path):
                QMessageBox.warning(self, "Step1", "Corrected channel source missing.")
                corr_path = ""
            correction_config = None
            cfg_path = os.path.join(out_dir, "correction_config.json")
            if os.path.exists(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    correction_config = json.load(f)
            decisions = {}
            if correction_config:
                decisions = {
                    str(ch): str(method).strip().lower()
                    for ch, method in (correction_config.get("channel_decisions") or {}).items()
                    if str(method).strip().lower() in {"tophat", "cucim"}
                }
            self.loader.set_correction_config(correction_config)
            self.loader.set_corrected_zarr_store(corr_path, decisions)
            self._corrected_zarr_path = corr_path
            self._corrected_decisions = decisions
            self._corrected_zarr_mode = str(sess.get("corrected_zarr_mode") or "")

            self.config.all_channels = self.loader.channel_names()
            fusion_cfg = sess.get("fusion_config") or self._fusion_config_from_flat_weights(sess)
            self._apply_step1_fusion_config(fusion_cfg)

            rois = list(sess.get("rois") or [])
            if not rois and sess.get("roi_bbox"):
                rois = [{
                    "name": sess.get("active_roi") or "ROI_1",
                    "bbox_fullres": sess.get("roi_bbox"),
                    "polygon_fullres": sess.get("polygon_fullres"),
                    "patch_indices": [],
                }]
            self._rois = rois
            active_name = sess.get("active_roi") or (rois[0].get("name") if rois else "")
            self._active_roi = next((r for r in rois if r.get("name") == active_name), rois[0] if rois else None)
            print(f"[Step1] active_roi={active_name or 'none'}")

            patches = []
            ry0, _, rx0, _ = [0, 0, 0, 0]
            if self._active_roi and self._active_roi.get("bbox_fullres"):
                ry0, _, rx0, _ = [int(v) for v in self._active_roi["bbox_fullres"]]
            for p in sess.get("patches") or []:
                if p.get("bbox_fullres"):
                    patches.append(tuple(int(v) for v in p["bbox_fullres"]))
                elif p.get("bbox_local"):
                    y0, y1, x0, x1 = [int(v) for v in p["bbox_local"]]
                    patches.append((ry0 + y0, ry0 + y1, rx0 + x0, rx0 + x1))
            self._p2_params = sess.get("p2_params")
            self._p1_diam = sess.get("p1_diam")
            self._params_source = sess.get("params_source")
            self._fused_zarr_path = sess.get("fusion_zarr_path") or None
            self.step1_output = {
                "fusion_config_path": os.path.join(out_dir, "fusion_config.json"),
                "correction_config_path": os.path.join(out_dir, "correction_config.json"),
                "zarr_path": self._fused_zarr_path,
                "roi_info": self._rois,
                "output_dir": out_dir,
                "ome_tiff_path": raw_ome,
            }

            self._stop_all_loaders()
            self._patch_channel_cache.clear()
            self._patch_load_ready.clear()
            self._preview_patch_idx = -1
            self._on_rois_changed(self._rois)
            self._on_patches(patches)
            selected_name = sess.get("selected_patch")
            if selected_name and selected_name.startswith("P"):
                try:
                    sel_idx = int(selected_name[1:]) - 1
                    if 0 <= sel_idx < len(self._all_patches):
                        self._select_preview_patch(sel_idx)
                except Exception:
                    pass
            self._show_active_roi_preview()
            self.step0_done = True
            self.step1_done = bool(self._fused_zarr_path)
            self._step2._out_edit.setText(out_dir)
            self._step4._ome_edit.setText(raw_ome)
            self._step4._out_edit.setText(out_dir)
            self._update_next_button()
            self.prev_status.setText("Loaded previous Step1 session.")
            print(f"[Step1] restored patches={len(patches)}")
            print(f"[Step1] restored channel weights count={len(sess.get('channel_weights') or {})}")
            return True
        except Exception:
            tb = traceback.format_exc()
            print(f"[Step1] failed to load session:\n{tb}")
            if not auto:
                QMessageBox.warning(self, "Step1", f"Failed to load Step1 session:\n{tb}")
            return False
        finally:
            self._step1_restore_active = False

    def _go_to_step2(self):
        if self._current_step == 3 and hasattr(self._step3, "_stop_loaders"):
            self._step3._stop_loaders()
        if self._current_step == 1:
            self._stop_all_loaders()
        if self.is_sequential_flow and self.step1_output:
            zarr_path = self.step1_output.get("zarr_path")
            if zarr_path:
                self._step2.set_zarr_path(zarr_path)
            self._step2._out_edit.setText(
                self.step1_output.get("output_dir", OUTPUT_DIR)
            )
            self._step2._fusion_config_path = self.step1_output.get("fusion_config_path")
            cfg_path = self.step1_output.get("fusion_config_path")
            seg_cfg_path = os.path.join(self.step1_output.get("output_dir", OUTPUT_DIR), "segmentation_config.json")
            if os.path.exists(seg_cfg_path):
                try:
                    with open(seg_cfg_path, "r", encoding="utf-8") as f:
                        seg_cfg = normalize_segmentation_config(json.load(f))
                    if hasattr(self._step2, "_apply_seg_config_to_ui"):
                        self._step2._apply_seg_config_to_ui(seg_cfg)
                    print(f"[Step2] segmentation method={seg_cfg.get('method')}")
                    print(f"[Step2] input_type={seg_cfg.get('input_type')}")
                except Exception:
                    print(f"[Step2] failed to load segmentation_config.json:\n{traceback.format_exc()}")
            if cfg_path:
                self._step2._zarr_info.setText(
                    (self._step2._zarr_info.text() or "") +
                    f"\nConfig: {os.path.basename(cfg_path)}"
                )
            rois = self.step0_output.get("rois") or self.step1_output.get("roi_info")
            if rois:
                self._step2.set_rois(rois)
        self._stack.setCurrentIndex(2)
        self._set_step_active(2)

    def _go_to_step1(self):
        if not self.step0_done and self.loader is None:
            if not self._load_previous_step1_session(auto=True):
                self.prev_status.setText(
                    "No previous Step1 session found. Use Load Previous Step1 Session or run Step0 first."
                )
        self._stack.setCurrentIndex(1)
        self._set_step_active(1)

    def _go_to_step3(self, output_dir=None):
        if self._current_step == 1:
            self._stop_all_loaders()
        if output_dir:
            self.step2_done = True
            self.step2_output = {
                "output_dir": output_dir,
            }
            self._step3.set_channel_context(
                loader=self.loader,
                corrected_zarr_path=self._corrected_zarr_path,
                rois=self._rois,
            )
            if self.is_sequential_flow:
                self._step3.set_output_dir(output_dir)
                self.step3_output = dict(self.step2_output)
            self._step3.set_output_dir(output_dir)
        self._stack.setCurrentIndex(3)
        self._set_step_active(3)

    def _go_to_step4(self, output_dir=None):
        if self._current_step == 3 and hasattr(self._step3, "_stop_loaders"):
            self._step3._stop_loaders()
        if self._current_step == 1:
            self._stop_all_loaders()
        if self._current_step == 3:
            self.step3_done = True
        if self.is_sequential_flow and self.step3_output:
            seq_out = self.step3_output.get("output_dir")
            if seq_out:
                mask = os.path.join(seq_out, 'global_mask.dat')
                if not os.path.exists(mask):
                    mask = os.path.join(seq_out, 'global_mask.ome.tiff')
                self._step4.set_paths(
                    mask_path=mask if os.path.exists(mask) else '',
                    ome_tiff_path=(self.step1_output or {}).get("ome_tiff_path", OME_TIFF_FILE),
                    output_dir=seq_out,
                )
        if output_dir:
            mask = os.path.join(output_dir, 'global_mask.dat')
            if not os.path.exists(mask):
                mask = os.path.join(output_dir, 'global_mask.ome.tiff')
            self._step4.set_paths(
                mask_path     = mask if os.path.exists(mask) else '',
                ome_tiff_path = OME_TIFF_FILE,
                output_dir    = output_dir,
            )
        self._stack.setCurrentIndex(4)
        self._set_step_active(4)

    def _go_next_step(self):
        self.is_sequential_flow = True
        if not self._btn_next.isEnabled():
            return
        if self._current_step == 0:
            self._go_to_step1()
        elif self._current_step == 1:
            self._go_to_step2()
        elif self._current_step == 2:
            self._go_to_step3(getattr(self._step2, "_last_output_dir", None))
        elif self._current_step == 3:
            self._go_to_step4()

    def _skip_to_step2(self):
        self.is_sequential_flow = False
        self._go_to_step2()

    def _skip_to_step3(self):
        self.is_sequential_flow = False
        self._go_to_step3()

    def _skip_to_step4(self):
        self.is_sequential_flow = False
        self._go_to_step4()

    def _update_next_button(self):
        done_map = {
            0: self.step0_done,
            1: self.step1_done,
            2: self.step2_done,
            3: self.step3_done,
            4: self.step4_done,
        }
        enabled = (self._current_step != 4) and bool(done_map.get(self._current_step, False))
        self._btn_next.setEnabled(enabled)
        if enabled:
            self._btn_next.setStyleSheet(
                'QPushButton{background:#2a5;color:white;font-size:12px;'
                'font-weight:bold;border-radius:4px;padding:4px 14px;}'
                'QPushButton:hover{background:#3b6;}'
            )
        else:
            self._btn_next.setStyleSheet(
                'QPushButton{background:#333;color:#555;font-size:12px;'
                'font-weight:bold;border-radius:4px;padding:4px 14px;'
                'border:1px solid #3f3f3f;}'
            )

    def _set_step_active(self, active):
        self._current_step = active
        _on = ("font-size:12px;font-weight:bold;color:#61afef;padding:4px 12px;"
               "background:#1a2a3a;border-radius:4px;")
        _off = "font-size:12px;color:#555;padding:4px 12px;"
        self._step0_lbl.setStyleSheet(_on if active == 0 else _off)
        self._step1_lbl.setStyleSheet(_on  if active == 1 else _off)
        self._step2_lbl.setStyleSheet(_on  if active == 2 else _off)
        self._step3_lbl.setStyleSheet(_on  if active == 3 else _off)
        self._step4_lbl.setStyleSheet(_on  if active == 4 else _off)
        self._update_next_button()

    @staticmethod
    def _make_label(text, bold=False):
        lbl = QLabel(text)
        lbl.setAlignment(Qt.AlignCenter)
        style = "font-size:12px;color:#ddd;background:#1a1a1a;padding:4px;"
        if bold:
            style += "font-weight:bold;"
        lbl.setStyleSheet(style)
        return lbl

    def _zero_marker_weights(self):
        for panel in self.config._panels.values():
            for row in panel._rows.values():
                row.spin.setValue(0.0)
        self.config.config_changed.emit()

    @staticmethod
    def _patch_inside_roi_bbox(patch, roi):
        bbox = roi.get("bbox_fullres") if roi else None
        if not bbox or len(bbox) != 4:
            return True
        y0, y1, x0, x1 = [int(v) for v in patch]
        ry0, ry1, rx0, rx1 = [int(v) for v in bbox]
        return ry0 <= y0 and y1 <= ry1 and rx0 <= x0 and x1 <= rx1

    def _filter_patches_to_roi(self, patches, roi):
        kept = [tuple(int(v) for v in p) for p in patches if self._patch_inside_roi_bbox(p, roi)]
        dropped = len(patches) - len(kept)
        if dropped:
            print(f"[Step1] dropped {dropped} patch(es) outside active ROI bbox")
        return kept

    def _clear_roi_patch_items(self):
        for item in getattr(self, "_roi_patch_items", []):
            try:
                self.roi_vb.removeItem(item)
            except Exception:
                pass
        self._roi_patch_items = []

    def _show_active_roi_preview(self):
        self._clear_roi_patch_items()
        roi = self._active_roi
        if self.loader is None or not roi:
            self.roi_status.setText("No ROI loaded")
            return
        bbox = roi.get("bbox_fullres")
        if not bbox or len(bbox) != 4:
            self.roi_status.setText("No ROI bbox")
            return
        nuc_ch, _ = self.config.get_nucleus()
        if not nuc_ch or nuc_ch not in self.loader.ch_map:
            self.roi_status.setText("No nucleus channel selected")
            return
        ry0, ry1, rx0, rx1 = [int(v) for v in bbox]
        roi_h, roi_w = ry1 - ry0, rx1 - rx0
        if roi_h <= 0 or roi_w <= 0:
            return
        ds = max(PREVIEW_DOWNSAMPLE, int(max(roi_h, roi_w) / 1800) + 1)
        try:
            arr = self.loader.read_region(nuc_ch, ry0, ry1, rx0, rx1, downsample=ds)
            grey = (np.clip(arr, 0, 1) * 255).astype(np.uint8)
            rgb = np.stack([grey, grey, grey], axis=-1)
            self.roi_img.setImage(rgb, autoLevels=False)
            self.roi_vb.autoRange()
            print(f"[Step1] roi preview loaded shape={rgb.shape}")

            for idx, patch in enumerate(self._all_patches):
                if not self._patch_inside_roi_bbox(patch, roi):
                    continue
                y0, y1, x0, x1 = [int(v) for v in patch]
                lx0 = (x0 - rx0) / ds
                lx1 = (x1 - rx0) / ds
                ly0 = (y0 - ry0) / ds
                ly1 = (y1 - ry0) / ds
                color = PATCH_COLORS[idx % len(PATCH_COLORS)]
                xs = [lx0, lx1, lx1, lx0, lx0]
                ys = [ly0, ly0, ly1, ly1, ly0]
                item = pg.PlotDataItem(xs, ys, pen=pg.mkPen(color, width=2))
                self.roi_vb.addItem(item)
                self._roi_patch_items.append(item)

            self.roi_status.setText(
                f"ROI preview: {roi.get('name', 'ROI_1')}  "
                f"{roi_h}×{roi_w}px  patches={len(self._all_patches)}"
            )
        except Exception as e:
            self.roi_status.setText(f"⚠ ROI preview failed: {e}")
            print(f"[Step1] ROI preview failed: {e}")

    # ── Patch selector button management ────────────────────────────

    def _rebuild_patch_buttons(self, patches):
        """Rebuild P1/P2/… selector buttons; preserve load-state styling."""
        for btn in self._patch_sel_btns:
            self._patch_sel_container.removeWidget(btn)
            btn.deleteLater()
        self._patch_sel_btns.clear()

        for i in range(len(patches)):
            color = PATCH_COLORS[i % len(PATCH_COLORS)]
            btn = QPushButton(f"P{i+1}")
            btn.setCheckable(True)
            btn.setFixedSize(42, 22)
            btn.clicked.connect(lambda _, idx=i: self._select_preview_patch(idx))
            self._patch_sel_container.addWidget(btn)
            self._patch_sel_btns.append(btn)
            # Apply correct state style immediately
            state = ('ready' if i in self._patch_load_ready
                     else 'loading' if i in self._patch_loaders
                     else 'idle')
            self._set_patch_btn_state(i, state)

        if 0 <= self._preview_patch_idx < len(self._patch_sel_btns):
            self._patch_sel_btns[self._preview_patch_idx].setChecked(True)

    def _set_patch_btn_state(self, idx, state: str):
        """Update button label+style for states: idle / loading / ready / error."""
        if idx >= len(self._patch_sel_btns):
            return
        btn   = self._patch_sel_btns[idx]
        color = PATCH_COLORS[idx % len(PATCH_COLORS)]
        labels = {'idle': f'P{idx+1}', 'loading': f'P{idx+1} ⟳',
                  'ready': f'P{idx+1} ✓', 'error': f'P{idx+1} ✗'}
        styles = {
            'idle': (
                f"QPushButton{{color:#666;border:1px solid #444;"
                f"border-radius:3px;font-size:10px;font-weight:bold;background:#1a1a1a;}}"
                f"QPushButton:checked{{background:#333;color:#aaa;}}"
            ),
            'loading': (
                f"QPushButton{{color:#fa8;border:1px solid #fa8;"
                f"border-radius:3px;font-size:10px;font-weight:bold;background:#1a1a1a;}}"
                f"QPushButton:checked{{background:#321;color:#fa8;}}"
            ),
            'ready': (
                f"QPushButton{{color:{color};border:1px solid {color};"
                f"border-radius:3px;font-size:10px;font-weight:bold;background:#1a1a1a;}}"
                f"QPushButton:checked{{background:{color};color:#111;}}"
                f"QPushButton:hover{{background:#2a2a2a;}}"
            ),
            'error': (
                f"QPushButton{{color:#f44;border:1px solid #f44;"
                f"border-radius:3px;font-size:10px;font-weight:bold;background:#1a1a1a;}}"
                f"QPushButton:checked{{background:#311;color:#f88;}}"
            ),
        }
        btn.setText(labels.get(state, f'P{idx+1}'))
        btn.setStyleSheet(styles.get(state, styles['idle']))

    # ── ROI changes ─────────────────────────────────────────────────

    def _on_rois_changed(self, rois):
        self._rois = rois
        # Pass ROIs to Step 2 if already there
        if hasattr(self, '_step2'):
            self._step2.set_rois(rois)

    # ── Patch list changes ───────────────────────────────────────────

    def _on_patches(self, patches):
        old_rois = [p for p in self._all_patches]
        self._all_patches = list(patches)
        self._rebuild_patch_buttons(patches)

        if not patches:
            self._stop_all_loaders()
            self._patch_channel_cache.clear()
            self._patch_load_ready.clear()
            self._preview_patch_idx = -1
            self.prev_img.clear()
            self.prev_status.setText("No patch selected")
            self._schedule_step1_session_save()
            return

        # Drop cache for patches whose ROI coordinates changed
        for idx, roi in enumerate(patches):
            if idx < len(old_rois) and roi != old_rois[idx]:
                self._patch_channel_cache.pop(idx, None)
                self._patch_load_ready.discard(idx)
                self._stop_loader_for(idx)

        # Auto-select newest patch
        new_idx = len(patches) - 1
        if new_idx != self._preview_patch_idx:
            self._preview_patch_idx = new_idx
            for i, btn in enumerate(self._patch_sel_btns):
                btn.setChecked(i == new_idx)

        # Show cached render instantly if available; otherwise wait for preload
        if self._preview_patch_idx in self._patch_load_ready:
            self._render_current_patch(reset_view=True)
            self.prev_status.setText(
                f"P{self._preview_patch_idx+1} (cached) — "
                f"preloading remaining patches in background…"
            )
        else:
            self.prev_img.clear()
            self.prev_status.setText("Loading patch...")

        # Debounce: start background preload 400 ms after last patch change
        self._preload_debounce.start(400)
        self._schedule_step1_session_save()

    def _select_preview_patch(self, idx):
        """User clicked a patch button — render from cache if ready, else show status."""
        if idx < 0 or idx >= len(self._all_patches):
            return
        for i, btn in enumerate(self._patch_sel_btns):
            btn.setChecked(i == idx)
        self._preview_patch_idx = idx
        self._schedule_step1_session_save()

        if idx in self._patch_load_ready:
            self._render_current_patch(reset_view=True)
        elif idx in self._patch_loaders:
            self.prev_img.clear()
            self.prev_status.setText("Loading patch...")
        else:
            # Not yet started — kick off a single loader for this patch immediately
            self.prev_img.clear()
            self.prev_status.setText("Loading patch...")
            self._start_loader_for(idx)

    # ── Background preloading ────────────────────────────────────────

    def _needed_channels(self):
        """Return all loadable channel names for Step1 patch caches."""
        if self.loader is None:
            return []
        return list(self.loader.channel_names())

    def _preload_all_patches(self):
        """Launch a background loader for every patch not yet in cache.
        Multiple threads run concurrently — each opens its own TiffFile handle
        and reads a different tile region, so they do not interfere.
        """
        needed = self._needed_channels()
        if not needed:
            self.prev_status.setText(
                "⚠ No loadable channels — check channel names match OME-TIFF\n"
                f"Available: {self.loader.channel_names()}"
            )
            return

        for idx, roi in enumerate(self._all_patches):
            if idx in self._patch_load_ready:
                continue   # already cached
            if idx in self._patch_loaders and self._patch_loaders[idx].isRunning():
                continue   # already loading
            self._start_loader_for(idx, needed=needed)

    def _start_loader_for(self, idx, needed=None):
        """Start (or restart) the loader thread for patch `idx`."""
        if idx >= len(self._all_patches):
            return
        if needed is None:
            needed = self._needed_channels()
        if not needed:
            return

        # Stop any existing loader for this patch before replacing it.
        if not self._stop_loader_for(idx):
            self.prev_status.setText(f"P{idx+1} is still stopping… please wait")
            return

        y0, y1, x0, x1 = self._all_patches[idx]
        if self._active_roi and not self._patch_inside_roi_bbox((y0, y1, x0, x1), self._active_roi):
            self.prev_status.setText(f"⚠ P{idx+1} is outside active ROI")
            return
        t = PreviewLoaderThread(idx, self.loader, needed,
                                y0, y1, x0, x1,
                                downsample=PREVIEW_DOWNSAMPLE)
        t.done.connect(self._on_patch_loaded)
        t.progress.connect(self._on_patch_progress)
        t.error.connect(self._on_patch_error)
        t.finished.connect(lambda idx=idx, thread=t: self._on_patch_loader_finished(idx, thread))
        self._patch_loaders[idx] = t
        self._set_patch_btn_state(idx, 'loading')
        t.start()

    def _on_patch_progress(self, patch_idx, done, total, ch):
        if patch_idx == self._preview_patch_idx:
            self.prev_status.setText(
                f"P{patch_idx+1} loading ({done+1}/{total}): {ch}"
            )

    def _on_patch_loaded(self, patch_idx, cache):
        self._patch_channel_cache[patch_idx] = cache
        self._patch_load_ready.add(patch_idx)
        self._set_patch_btn_state(patch_idx, 'ready')

        nuc_ch, _ = self.config.get_nucleus()
        y0, y1, x0, x1 = self._all_patches[patch_idx]
        h = (y1 - y0) // PREVIEW_DOWNSAMPLE
        w = (x1 - x0) // PREVIEW_DOWNSAMPLE
        local_txt = ""
        if self._active_roi and self._active_roi.get("bbox_fullres"):
            ry0, _, rx0, _ = [int(v) for v in self._active_roi["bbox_fullres"]]
            local_txt = (
                f" local=[{y0-ry0},{y1-ry0},{x0-rx0},{x1-rx0}]"
            )
        print(f"[Preview] P{patch_idx+1} ready — {len(cache)} ch, {h}×{w} px{local_txt}")

        # If this is the currently viewed patch, render it immediately
        if patch_idx == self._preview_patch_idx:
            nuc_ok = "✓" if nuc_ch in cache else "✗(not found!)"
            cyto_n = len([c for c in cache if c != nuc_ch])
            self.prev_status.setText(
                f"P{patch_idx+1} ready  nucleus({nuc_ch}){nuc_ok}  "
                f"cyto: {cyto_n} ch  {h}×{w} px (1/{PREVIEW_DOWNSAMPLE})"
            )
            self._render_current_patch(reset_view=True)

        # Update global status once all patches are loaded
        n_ready = len(self._patch_load_ready)
        n_total = len(self._all_patches)
        if n_ready == n_total:
            self.prev_status.setText(
                f"All {n_total} patches cached ✓  "
                f"Click P1–P{n_total} to switch instantly"
            )

    def _on_patch_error(self, patch_idx, msg):
        self._set_patch_btn_state(patch_idx, 'error')
        if patch_idx == self._preview_patch_idx:
            self.prev_status.setText(f"⚠ P{patch_idx+1} load error: {msg}")
        print(f"[Preview] P{patch_idx+1} error: {msg}")

    def _on_patch_loader_finished(self, patch_idx, thread):
        if self._patch_loaders.get(patch_idx) is thread:
            self._patch_loaders.pop(patch_idx, None)

    def _stop_loader_for(self, idx, timeout_ms=3000):
        thread = self._patch_loaders.get(idx)
        if thread is None:
            return True
        if thread.isRunning():
            thread.stop()
            thread.wait(timeout_ms)
        if not thread.isRunning() and self._patch_loaders.get(idx) is thread:
            self._patch_loaders.pop(idx, None)
            return True
        return not thread.isRunning()

    def _stop_all_loaders(self):
        self._preload_debounce.stop()
        survivors = {}
        for idx, t in list(self._patch_loaders.items()):
            if t.isRunning():
                t.stop()
                t.wait(3000)
            try:
                t.done.disconnect()
                t.progress.disconnect()
                t.error.disconnect()
            except Exception:
                pass
            if t.isRunning():
                survivors[idx] = t
                print(f"[Preview] loader P{idx+1} still running after stop request; keeping reference")
        self._patch_loaders = survivors

    def closeEvent(self, event):
        self._stop_all_loaders()
        if self._patch_loaders:
            event.ignore()
            self.prev_status.setText("Waiting for preview loaders to stop…")
            QtCore.QTimer.singleShot(500, self.close)
            return
        super().closeEvent(event)

    # ── Force-update (clears all caches, re-loads everything) ───────

    def _force_update_all(self):
        """Update button: wipe all caches and restart preload for all patches."""
        if not self._all_patches:
            self.prev_status.setText("⚠ No patches yet — draw at least one patch first")
            return
        self._stop_all_loaders()
        self._patch_channel_cache.clear()
        self._patch_load_ready.clear()
        for i in range(len(self._all_patches)):
            self._set_patch_btn_state(i, 'idle')
        self.prev_status.setText("Cache cleared — reloading all patches…")
        self._preload_all_patches()

    # ── Rendering (pure numpy, no disk IO) ──────────────────────────

    def _render_current_patch(self, reset_view=False):
        """Compute and display fusion for the currently selected patch from cache.

        reset_view=True  → call autoRange() (used when switching to a new patch).
        reset_view=False → keep the current zoom/pan (used on weight changes).
        """
        idx = self._preview_patch_idx
        if idx not in self._patch_load_ready:
            if idx < 0 or idx >= len(self._all_patches):
                self.prev_img.clear()
                self.prev_status.setText("No patch selected")
            else:
                self.prev_status.setText("Loading patch...")
            return
        cache = self._patch_channel_cache.get(idx)
        if not cache:
            self.prev_status.setText("Loading patch...")
            return
        groups = self.config.get_groups()
        group_weights = self.config.get_group_weights()
        nuc_ch, nuc_w = self.config.get_nucleus()
        shape = next(iter(cache.values())).shape

        # FIX: initial weights
        # FIX: intensity normalization
        cyto = np.zeros(shape, dtype=np.float32)
        for gname, ch_weights in groups.items():
            group_signal = np.zeros(shape, dtype=np.float32)
            gw = float(np.clip(group_weights.get(gname, 1.0), 0.0, 1.0))
            if gw <= 0:
                continue
            for ch, w in ch_weights.items():
                w = float(np.clip(w, 0.0, 1.0))
                if w <= 0 or ch not in cache:
                    continue
                group_signal += self.fusion._normalize_intensity(cache[ch]) * w
            group_signal *= gw
            np.maximum(cyto, np.clip(group_signal, 0.0, 1.0), out=cyto)

        nuc = np.zeros(shape, dtype=np.float32)
        if nuc_ch and nuc_ch in cache and nuc_w > 0:
            nuc = self.fusion._normalize_intensity(cache[nuc_ch]) * float(np.clip(nuc_w, 0.0, 1.0))
            np.clip(nuc, 0.0, 1.0, out=nuc)

        if float(cyto.max()) <= 0.0 and float(nuc.max()) <= 0.0 and nuc_ch and nuc_ch in cache:
            nuc = self.fusion._normalize_intensity(cache[nuc_ch])
            self.prev_status.setText("All marker weights are 0. Showing nucleus channel fallback.")

        if cyto is None and nuc is None:
            return
        rgb = self.fusion.to_rgb(cyto, nuc)

        # Only reset view on first load or explicit patch switch;
        # weight/group changes preserve the current zoom & pan.
        first_load = (self.prev_img.image is None)
        self.prev_img.setImage(rgb, autoLevels=False)
        if first_load or reset_view:
            self.prev_vb.autoRange()

    def _on_cfg_changed(self):
        """Weight/group changed: re-render from cache (no disk IO) after 300 ms debounce."""
        if self._preview_patch_idx in self._patch_load_ready:
            self._prev_timer.start(300)
        self._schedule_step1_session_save()

    # ── Phase 1 ─────────────────────────────────────────────────────

    def _run_p1(self, diameters):
        """
        diameters is a single-element list: [None] for auto, [float] for override.
        Phase 1 now runs ONE inference per patch (not a grid search),
        showing the auto-diameter result for the user to visually confirm.
        After completion, diameter is automatically passed to Phase 2.
        """
        patches = list(self._all_patches)
        if not patches:
            QMessageBox.warning(self, "Info", "Please add at least one Patch first")
            return

        diam = diameters[0]   # None or float
        method_cfg = self.search.get_selected_method_config()
        param_list = [
            {"diameter": diam,
             "flow_threshold": 0.4,
             "cellprob_threshold": 0.0,
             "_phase": 1,
             **method_cfg}
        ]
        tasks = [
            (pi, roi, dict(param_list[0]))
            for pi, roi in enumerate(patches)
        ]
        diam_str = "auto" if diam is None else f"{diam} px"
        method = param_list[0].get("method", CELLPOSE_WHOLECELL_FUSION)
        self.result_grid.setup_grid(
            len(patches), param_list,
            f"Phase 1 — {method}  (diameter={diam_str},"
            f"  {len(patches)} patch(es))"
        )
        self._launch_worker(tasks)

    # ── Phase 2 ─────────────────────────────────────────────────────

    def _run_p2(self, cfg):
        patches = list(self._all_patches)
        if not patches:
            QMessageBox.warning(self, "Info", "Please add Patches first")
            return

        diam  = cfg["diameter"]
        method_cfg = {
            "method": cfg.get("method", CELLPOSE_WHOLECELL_FUSION),
            "params": dict(cfg.get("params") or {}),
        }
        param_list = [
            {"diameter": diam,
             "flow_threshold": f,
             "cellprob_threshold": p,
             "_phase": 2,
             **method_cfg}
            for f in cfg["flow"]
            for p in cfg["prob"]
        ]
        tasks = [
            (pi, roi, dict(p))
            for pi, roi in enumerate(patches)
            for p in param_list
        ]
        method = method_cfg["method"]
        self.result_grid.setup_grid(
            len(patches), param_list,
            f"Phase 2 — {method}  flow × cellprob  diameter={diam}  ({len(tasks)} inferences)"
        )
        self._launch_worker(tasks)

    def _run_direct_patch_preview(self, params):
        method = (params or {}).get("method", CELLPOSE_WHOLECELL_FUSION)
        if method in (CELLPOSE_WHOLECELL_FUSION, CELLPOSE_NUCLEI_DAPI):
            QMessageBox.information(
                self, "Segmentation mode",
                "Use Phase 1 / Phase 2 for Cellpose patch preview."
            )
            return
        patches = list(self._all_patches)
        if not patches:
            QMessageBox.warning(self, "Info", "Please add at least one Patch first")
            return
        patch_idx = self._preview_patch_idx
        if patch_idx < 0 or patch_idx >= len(patches):
            patch_idx = 0
            self._preview_patch_idx = 0
        params = normalize_segmentation_config(params or {})
        self._p2_params = params
        self._params_source = "direct_patch_preview"
        self._check_save_unlock()

        nuc_ch, _ = self.config.get_nucleus()
        print(f"[Step1] patch preview mode={method}")
        print(f"[Step1] patch=P{patch_idx + 1}")
        print(f"[Step1] input=DAPI channel={nuc_ch}")
        print(f"[Step1] params={params.get('params') or params}")

        self.result_grid.setup_grid(
            len(patches),
            [dict(params)],
            f"Patch Preview — {method}  (P{patch_idx + 1})"
        )
        self._launch_worker([(patch_idx, patches[patch_idx], dict(params))])

    # ── Worker ──────────────────────────────────────────────────────

    def _launch_worker(self, tasks):
        if self.proc is not None and self.proc.is_alive():
            return
        nuc_ch, nuc_w = self.config.get_nucleus()
        self._proc_stopped = False
        self._proc_queue = mp.Queue()
        self._proc_stop_flag = mp.Event()
        args = {
            "tasks": tasks,
            "ome_path": self.loader.filepath,
            "name_map": self.loader.name_map,
            "correction_config": self.loader.correction_config,
            "groups": self.config.get_groups(),
            "group_weights": self.config.get_group_weights(),
            "nuc_ch": nuc_ch,
            "nuc_w": nuc_w,
            "corrected_zarr_path": self._corrected_zarr_path,
            "corrected_decisions": self._corrected_decisions,
        }
        self.proc = mp.Process(
            target=run_cellpose_process,
            args=(args, self._proc_queue, self._proc_stop_flag),
            daemon=True,
        )
        self.search.set_running(True)
        self.search.update_progress(0, len(tasks), "Starting...")
        self.proc.start()
        self._proc_poll_timer.start()

    def _stop(self):
        if self.proc is not None:
            self._proc_stopped = True
            if self._proc_stop_flag is not None:
                self._proc_stop_flag.set()
            self.proc.terminate()
            self.proc.join(timeout=1.0)
            if self.proc.is_alive():
                self.proc.kill()
                self.proc.join(timeout=1.0)
            self._cleanup_cellpose_process()
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
            gc.collect()
            self.search.set_running(False)
            self.search.update_progress(0, 100, "Stopped")

    def _on_done(self):
        self.search.set_running(False)
        self.search.update_progress(100, 100, "Done! Click to select params in the result grid")
        # Delay auto-select so all queued result_ready signals are processed
        # before we call _select(0), which triggers param_selected → Phase 2 unlock.
        QTimer.singleShot(200, self._auto_select_p1)

    def _auto_select_p1(self):
        """Auto-select column 0 after Phase 1, only if nothing is selected yet."""
        if self.result_grid.get_selected() is None and self.result_grid._params:
            self.result_grid._select(0)

    def _poll_cellpose_process(self):
        if self._proc_queue is None:
            return
        while True:
            try:
                item = self._proc_queue.get_nowait()
            except Empty:
                break

            kind = item.get("type")
            if kind == "result":
                masks = item.get("masks")
                if masks is not None:
                    params = item.get("params") or {}
                    method = params.get("method")
                    if method and method != CELLPOSE_WHOLECELL_FUSION:
                        print(f"[Step1] cells={int(np.asarray(masks).max())}")
                self.result_grid.add_result(
                    item["patch_idx"],
                    item["params"],
                    item["rgb_overlay"],
                    item["rgb_raw"],
                    item.get("masks"),
                )
            elif kind == "progress":
                self.search.update_progress(
                    item.get("done", 0),
                    item.get("total", 0),
                    item.get("msg", ""),
                )
            elif kind == "error":
                print(f"[Worker] {item.get('msg', '')}")
            elif kind == "finished":
                self._cleanup_cellpose_process()
                if not self._proc_stopped:
                    self._on_done()
                return

        if self.proc is not None and not self.proc.is_alive():
            self._cleanup_cellpose_process()
            if not self._proc_stopped:
                self._on_done()

    def _cleanup_cellpose_process(self):
        self._proc_poll_timer.stop()
        if self.proc is not None:
            try:
                self.proc.close()
            except Exception:
                pass
        self.proc = None
        if self._proc_queue is not None:
            try:
                self._proc_queue.close()
                self._proc_queue.join_thread()
            except Exception:
                pass
        self._proc_queue = None
        self._proc_stop_flag = None

    def _on_params_ready(self, params):
        """Called when user loads a JSON file or clicks 'Use These Params'."""
        self._p2_params    = params
        self._params_source = params.get("_source", "manual")
        self._check_save_unlock()
        self._schedule_step1_session_save()

    def _on_step1_segmentation_mode_changed(self, method):
        method = method or CELLPOSE_WHOLECELL_FUSION
        is_whole = method == CELLPOSE_WHOLECELL_FUSION
        phase_required = method in (CELLPOSE_WHOLECELL_FUSION, CELLPOSE_NUCLEI_DAPI)
        is_stardist = method in (STARDIST_NUCLEI_DAPI, STARDIST_NUCLEI_EXPANSION)
        workflow = {
            CELLPOSE_WHOLECELL_FUSION: "wholecell_phase1_phase2",
            CELLPOSE_NUCLEI_DAPI: "nuclei_cellpose",
            STARDIST_NUCLEI_DAPI: "stardist",
            STARDIST_NUCLEI_EXPANSION: "stardist_expansion",
        }.get(method, "unknown")
        print(f"[Step1] segmentation mode selected={method}")
        print(f"[Step1] segmentation mode={method}")
        print(f"[Step1] workflow={workflow}")
        print(f"[Step1] phase1_required={phase_required}")
        print("[Step1] channel_weight_panel_visible=True")
        print("[Step1] fusion_preview_enabled=True")
        print("[Step1] layout resize avoided=True")
        print("[Step1] channel_panel_visible=True")

        self.config.setVisible(True)
        self.config.setEnabled(True)
        self.result_grid.setVisible(True)
        if is_whole:
            self.btn_save.setText("💾  Save Config  &  Generate fused.zarr")
        else:
            self.btn_save.setText("💾  Save segmentation config  &  Generate DAPI input zarr")

        if phase_required:
            if self._p2_params is not None and self._p2_params.get("method") != method:
                self._p2_params = None
                self._params_source = None
                self.btn_save.setEnabled(False)
                self._fusion_bar_widget.setVisible(False)
        elif is_stardist:
            self._p2_params = self.search.get_current_params()
            self._params_source = "direct_method"
            self._check_save_unlock()
        self._schedule_step1_session_save()

    def _on_param_sel(self, params):
        if params.get("_phase") == 1:
            # Auto-unlock Phase 2 with the diameter used in Phase 1
            # (may be None for auto-diameter mode)
            diam = params.get("diameter")   # None or float
            self._p1_diam = diam
            self.search.set_p2_diam(diam)
        else:
            # Phase 2 grid selection
            self._p2_params    = params
            self._params_source = "phase2"
            self._check_save_unlock()
        self._schedule_step1_session_save()

    def _check_save_unlock(self):
        """Unlock the Save button whenever valid params are available."""
        if self._p2_params is not None:
            self.btn_save.setEnabled(True)
            src = self._params_source or "phase2"
            src_lbl = {
                "phase2":  "Phase 2 grid search",
                "loaded":  "loaded from JSON",
                "manual":  "manual entry",
            }.get(src, src)
            d  = self._p2_params.get("diameter", "auto")
            fl = self._p2_params.get("flow_threshold", 0.4)
            cp = self._p2_params.get("cellprob_threshold", 0.0)
            method = self._p2_params.get("method", CELLPOSE_WHOLECELL_FUSION)
            if method == CELLPOSE_WHOLECELL_FUSION:
                msg = (
                    f"Params ready ({src_lbl})  "
                    f"diam={d}  flow={fl}  prob={cp}  "
                    f"→ click Save to generate fused.zarr"
                )
            else:
                msg = (
                    f"Params ready ({src_lbl})  method={method}  "
                    f"→ click Save to generate DAPI input zarr"
                )
            self._fusion_lbl.setText(msg)
            self._fusion_bar_widget.setVisible(True)

    # ── Lock / unlock UI during fusion ──────────────────────────────

    def _lock_ui(self):
        """Disable all interactive elements during fusion."""
        self.btn_save.setEnabled(False)
        self.config.setEnabled(False)
        self.search.setEnabled(False)
        self._btn_back_to_step0.setEnabled(False)

    def _unlock_ui(self):
        """Re-enable UI after fusion completes or errors."""
        self.config.setEnabled(True)
        self.search.setEnabled(True)
        self._btn_back_to_step0.setEnabled(True)
        # Only re-enable save if we still have valid params
        if self._p2_params is not None:
            self.btn_save.setEnabled(True)

    # ── Fusion worker callbacks ───────────────────────────────────────

    def _on_fusion_progress(self, done, total, msg):
        pct = int(done / total * 100) if total > 0 else 0
        self._fusion_pbar.setValue(pct)
        self._fusion_lbl.setText(msg)

    def _on_fusion_done(self, zarr_path):
        self._fusion_pbar.setValue(100)
        method = CELLPOSE_WHOLECELL_FUSION
        if self._p2_params:
            method = self._p2_params.get("method", CELLPOSE_WHOLECELL_FUSION)
        is_wholecell = method == CELLPOSE_WHOLECELL_FUSION
        result_name = "Fusion" if is_wholecell else "DAPI input zarr"
        self._fusion_lbl.setText(f"✓  {result_name} complete → {zarr_path}")
        self._fused_zarr_path = zarr_path
        self.step1_output = {
            "fusion_config_path": os.path.join(OUTPUT_DIR, "fusion_config.json"),
            "correction_config_path": os.path.join(OUTPUT_DIR, "correction_config.json"),
            "zarr_path": zarr_path,
            "roi_info": self._rois if self._rois else [],
            "output_dir": OUTPUT_DIR,
            "ome_tiff_path": OME_TIFF_FILE,
        }
        self.step1_done = True
        self._update_next_button()
        self._save_step1_session()
        self._unlock_ui()
        # Count per-ROI zarrs from meta
        meta_path = os.path.join(OUTPUT_DIR, "fusion_meta.json")
        n_zarrs = 1
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            n_zarrs = len(meta.get("regions", [1]))
        except Exception:
            pass
        QMessageBox.information(
            self, f"{result_name} complete",
            f"{'ROI' if self._rois else 'Full WSI'} {result_name.lower()} done  "
            f"({n_zarrs} zarr(s))\n\n"
            f"First zarr → {zarr_path}\n\n"
            f"Click  [Next → Step 2]  to proceed to segmentation."
        )

    def _on_fusion_error(self, msg):
        self._fusion_lbl.setText(f"✗  Fusion error — see terminal for details")
        self._unlock_ui()
        QMessageBox.critical(self, "Fusion Error", msg)
        print(f"[Fusion Error]\n{msg}")

    # ── Save ────────────────────────────────────────────────────────

    def _save(self):
        current_method = self.search._method_combo.currentData() or CELLPOSE_WHOLECELL_FUSION
        if self._p2_params is None and current_method in (STARDIST_NUCLEI_DAPI, STARDIST_NUCLEI_EXPANSION):
            self._p2_params = self.search.get_current_params()
            self._params_source = "direct_method"
        if self._p2_params is None:
            # Give user a hint about all three ways to get params
            QMessageBox.warning(
                self, "No parameters available",
                "Please do one of the following before saving:\n\n"
                "1. Run Phase 1 → Phase 2 and select params from the result grid\n"
                "2. Click 📂 Browse to load an existing cellpose_params.json\n"
                "3. Fill in the manual spinboxes and click ✓ Use These Params"
            )
            return

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        if self._corrected_zarr_mode == "roi_only" and not self._rois:
            QMessageBox.warning(
                self, "ROI required",
                "Step0 corrected output is ROI-only, but no ROI is loaded."
            )
            return

        # ── Write fusion_config.json ──────────────────────────────────
        fcfg = self.config.get_full_config()
        fcfg.update({
            "ome_tiff":   OME_TIFF_FILE,
            "output_dir": OUTPUT_DIR,
            "norm_low":   NORM_LOW,
            "norm_high":  NORM_HIGH,
            "saved_at":   time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        fp1 = os.path.join(OUTPUT_DIR, "fusion_config.json")
        with open(fp1, "w", encoding="utf-8") as f:
            json.dump(fcfg, f, indent=2, ensure_ascii=False)

        # ── Write cellpose_params.json ────────────────────────────────
        method_cfg = self.search.get_selected_method_config()
        selected_method = (
            self._p2_params.get("method")
            or method_cfg.get("method")
            or CELLPOSE_WHOLECELL_FUSION
        )
        params_block = dict(method_cfg.get("params") or {})
        params_block.update(dict(self._p2_params.get("params") or {}))
        cpcfg = normalize_segmentation_config({
            "method":             selected_method,
            "params":             params_block,
            "model_type":         "cpsam",
            "diameter":           self._p2_params.get("diameter"),
            "flow_threshold":     self._p2_params.get("flow_threshold", 0.4),
            "cellprob_threshold": self._p2_params.get("cellprob_threshold", 0.0),
            "min_size":           15,
            "phase1_diameter":    self._p1_diam,
            "params_source":      self._params_source or "unknown",
            "saved_at":           time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        fp2 = os.path.join(OUTPUT_DIR, "cellpose_params.json")
        with open(fp2, "w", encoding="utf-8") as f:
            json.dump(cpcfg, f, indent=2, ensure_ascii=False)
        fp_seg = os.path.join(OUTPUT_DIR, "segmentation_config.json")
        with open(fp_seg, "w", encoding="utf-8") as f:
            json.dump(cpcfg, f, indent=2, ensure_ascii=False)
        print(f"[Save] {fp1}")
        print(f"[Save] {fp2}")
        print(f"[Save] {fp_seg}")
        print(f"[Step1] saved segmentation_config={fp_seg}")

        # ── Tile selection dialog ─────────────────────────────────────
        # Count active channels for RAM estimate
        is_wholecell = selected_method == CELLPOSE_WHOLECELL_FUSION
        worker_fcfg = dict(fcfg)
        if not is_wholecell:
            nuc_ch = fcfg.get("nucleus", {}).get("channel") or self.config.nuc_combo.currentText()
            worker_fcfg = dict(fcfg)
            worker_fcfg["nucleus"] = {"channel": nuc_ch, "weight": 1.0}
            worker_fcfg["groups"] = {}
        active_ch = set([worker_fcfg["nucleus"]["channel"]])
        for gdata in worker_fcfg["groups"].values():
            active_ch.update(gdata["channels"].keys())
        n_channels = len([ch for ch in active_ch if ch in self.loader.ch_map])

        try:
            import psutil
            sys_ram_gb = int(psutil.virtual_memory().total / 1e9)
        except ImportError:
            sys_ram_gb = 128   # conservative default

        tile_h, tile_w = self.loader.shape
        if self._active_roi and self._active_roi.get("bbox_fullres"):
            ry0, ry1, rx0, rx1 = [int(v) for v in self._active_roi["bbox_fullres"]]
            tile_h, tile_w = ry1 - ry0, rx1 - rx0

        dlg = TileSelectDialog(
            tile_h,
            tile_w,
            n_channels,
            sys_ram_gb=sys_ram_gb,
            parent=self,
        )
        if dlg.exec_() != QDialog.Accepted:
            return   # user cancelled — JSONs already saved, that's fine
        sel = dlg.get_selection()
        if sel is None:
            return
        n_rows, n_cols = sel

        # ── Start FullFusionWorker ────────────────────────────────────
        self._fusion_worker = FullFusionWorker(
            loader     = self.loader,
            fusion_cfg = worker_fcfg,
            n_rows     = n_rows,
            n_cols     = n_cols,
            rois       = self._rois if self._rois else None,
        )
        self._fusion_worker.progress.connect(self._on_fusion_progress)
        self._fusion_worker.finished.connect(self._on_fusion_done)
        self._fusion_worker.error.connect(self._on_fusion_error)

        self._lock_ui()
        self._fusion_bar_widget.setVisible(True)
        self._fusion_pbar.setValue(0)
        job_name = "fusion" if is_wholecell else "DAPI input zarr"
        self._fusion_lbl.setText(
            f"Starting {job_name}  {n_rows}×{n_cols} = {n_rows*n_cols} tiles…"
        )
        self._fusion_worker.start()



# ══════════════════════════════════════════════════════════════════════
#  Segment + Merge Worker  (block03 + block04 combined)
# ══════════════════════════════════════════════════════════════════════
