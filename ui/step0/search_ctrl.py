"""
block01/ui/step0/search_ctrl.py — SearchCtrlPanel and background workers.
"""

import os
import gc
import json
import time
import shutil
import traceback

import numpy as np
import tifffile
import zarr

from PyQt5 import QtWidgets, QtCore
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QGroupBox, QDoubleSpinBox, QProgressBar, QComboBox,
    QMessageBox, QFileDialog, QDialog, QFrame,
)
import pyqtgraph as pg

from ...config import (
    OUTPUT_DIR, TOPHAT_RADIUS_DEFAULT, CUCIM_SIGMA_DEFAULT,
    TOPHAT_RADIUS_RANGE, CUCIM_SIGMA_RANGE, BG_CORR_MAX_TILE,
    PHASE2_FLOW, PHASE2_CELLPROB,
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
)
from ...core.io_loader import OMETIFFLoader
from ...utils.segmentation_config import (
    CELLPOSE_WHOLECELL_FUSION,
    STARDIST_NUCLEI_EXPANSION,
    available_segmentation_methods,
    get_segmentation_method_config,
    normalize_segmentation_config,
)

class SearchCtrlPanel(QWidget):
    """
    Cellpose grid-search control panel.

    Phase 1: Auto-diameter preview (diameter=None, cpsam estimates internally).
             Optional override spinbox for manual diameter.
    Phase 2: Fixed diameter, search flow × cellprob.

    Three ways to obtain parameters (any one unlocks the Save button):
      1. Run Phase 1 → Phase 2 and select from result grid
      2. Load an existing cellpose_params.json
      3. Manually fill in the spinboxes and click "Use These Params"
    """

    run_p1       = pyqtSignal(list)   # [diameter_or_None] — single-element list
    run_p2       = pyqtSignal(dict)   # {diameter, flow, prob}
    stop         = pyqtSignal()
    params_ready = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self._p2_diam     = None
        self._p2_diam_set = False   # True after Phase 1 completes
        self._setup_ui()

    def _setup_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(5)

        method_box = QGroupBox("Segmentation Method")
        method_box.setStyleSheet(
            "QGroupBox{border:1px solid #666;border-radius:4px;"
            "font-weight:bold;color:#ccc;font-size:11px;}"
        )
        method_lay = QVBoxLayout(method_box)
        method_row = QHBoxLayout()
        method_row.addWidget(QLabel("Method:"))
        self._method_combo = QComboBox()
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
        exp_row.addWidget(QLabel("expansion_distance:"))
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
        method_lay.addWidget(self._method_hint)
        lay.addWidget(method_box)
        self._on_method_changed()

        # ── Phase 1 ───────────────────────────────────────────────────
        p1 = QGroupBox("Phase 1 — Auto-diameter preview  (cpsam)")
        p1.setStyleSheet(
            "QGroupBox{border:1px solid #666;border-radius:4px;"
            "font-weight:bold;color:#ccc;font-size:11px;}"
        )
        pl1 = QVBoxLayout(p1)

        info_lbl = QLabel(
            "cpsam (Cellpose 4) estimates cell size automatically.\n"
            "Leave override = 0 to use auto-diameter (recommended)."
        )
        info_lbl.setStyleSheet("color:#aaa;font-size:10px;")
        info_lbl.setWordWrap(True)
        pl1.addWidget(info_lbl)

        ov_row = QHBoxLayout()
        ov_row.addWidget(QLabel("Override diameter (0 = auto):"))
        self._p1_override = QDoubleSpinBox()
        self._p1_override.setRange(0, 500)
        self._p1_override.setValue(0)
        self._p1_override.setSingleStep(5)
        self._p1_override.setDecimals(1)
        self._p1_override.setSpecialValueText("auto")
        self._p1_override.setFixedWidth(80)
        self._p1_override.setStyleSheet("font-size:11px;")
        self._p1_override.setToolTip(
            "0 = let cpsam estimate diameter automatically (recommended)\n"
            "Set a positive value only if auto results look wrong."
        )
        ov_row.addWidget(self._p1_override)
        ov_row.addStretch()
        pl1.addLayout(ov_row)

        self.btn_p1 = QPushButton("▶ Run Phase 1  (auto-diameter preview)")
        self.btn_p1.setStyleSheet(
            "QPushButton{background:#2a5;color:white;"
            "border-radius:4px;padding:5px;font-weight:bold;}"
            "QPushButton:hover{background:#3b6;}"
            "QPushButton:disabled{background:#333;color:#555;}"
        )
        self.btn_p1.clicked.connect(self._emit_p1)
        pl1.addWidget(self.btn_p1)
        lay.addWidget(p1)

        # ── Phase 2 ───────────────────────────────────────────────────
        p2 = QGroupBox("Phase 2 — Fine search: flow × cellprob")
        p2.setStyleSheet(
            "QGroupBox{border:1px solid #666;border-radius:4px;"
            "font-weight:bold;color:#ccc;font-size:11px;}"
        )
        pl2 = QVBoxLayout(p2)
        self.p2_diam_lbl = QLabel("diameter: run Phase 1 first")
        self.p2_diam_lbl.setStyleSheet("color:#888;font-size:10px;")
        pl2.addWidget(self.p2_diam_lbl)
        fr = QHBoxLayout()
        fr.addWidget(QLabel("flow:"))
        self.flow_edit = QtWidgets.QLineEdit(
            ", ".join(str(f) for f in PHASE2_FLOW)
        )
        fr.addWidget(self.flow_edit)
        pl2.addLayout(fr)
        pr = QHBoxLayout()
        pr.addWidget(QLabel("cellprob:"))
        self.prob_edit = QtWidgets.QLineEdit(
            ", ".join(str(p) for p in PHASE2_CELLPROB)
        )
        pr.addWidget(self.prob_edit)
        pl2.addLayout(pr)
        self.btn_p2 = QPushButton("▶ Run Phase 2")
        self.btn_p2.setEnabled(False)
        self.btn_p2.setStyleSheet(
            "QPushButton{background:#2a5;color:white;"
            "border-radius:4px;padding:5px;font-weight:bold;}"
            "QPushButton:hover{background:#3b6;}"
            "QPushButton:disabled{background:#333;color:#555;}"
        )
        self.btn_p2.clicked.connect(self._emit_p2)
        pl2.addWidget(self.btn_p2)
        lay.addWidget(p2)

        # ── Divider ───────────────────────────────────────────────────
        div = QFrame()
        div.setFrameShape(QFrame.HLine)
        div.setStyleSheet("color:#444;")
        lay.addWidget(div)
        or_lbl = QLabel("— or skip grid search —")
        or_lbl.setAlignment(Qt.AlignCenter)
        or_lbl.setStyleSheet("color:#555;font-size:10px;")
        lay.addWidget(or_lbl)

        # ── Load existing params ───────────────────────────────────────
        load_box = QGroupBox("Load existing cellpose_params.json")
        load_box.setStyleSheet(
            "QGroupBox{border:1px solid #888;border-radius:4px;"
            "font-weight:bold;color:#aaa;font-size:11px;}"
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
        man_box = QGroupBox("Manual entry (skip grid search)")
        man_box.setStyleSheet(
            "QGroupBox{border:1px solid #888;border-radius:4px;"
            "font-weight:bold;color:#aaa;font-size:11px;}"
        )
        ml = QVBoxLayout(man_box)

        def _spin_row(label, lo, hi, step, dec, val):
            r = QHBoxLayout()
            l = QLabel(label)
            l.setFixedWidth(130)
            r.addWidget(l)
            sp = QDoubleSpinBox()
            sp.setRange(lo, hi); sp.setSingleStep(step)
            sp.setDecimals(dec); sp.setValue(val)
            sp.setStyleSheet("font-size:11px;")
            r.addWidget(sp)
            return r, sp

        r1, self._man_diam  = _spin_row("diameter (px):",  0, 500, 5,   1, 30)
        r2, self._man_flow  = _spin_row("flow_threshold:", 0,   3, 0.05, 2, 0.4)
        r3, self._man_prob  = _spin_row("cellprob_threshold:", -6, 6, 0.1, 2, 0.0)
        self._man_diam.setSpecialValueText("auto")
        for r in (r1, r2, r3):
            ml.addLayout(r)

        self.btn_use_manual = QPushButton("✓ Use These Params")
        self.btn_use_manual.setStyleSheet(
            "QPushButton{background:#444;color:#ccc;"
            "border-radius:4px;padding:4px;font-size:11px;}"
            "QPushButton:hover{background:#258;color:white;}"
        )
        self.btn_use_manual.clicked.connect(self._use_manual_params)
        ml.addWidget(self.btn_use_manual)
        lay.addWidget(man_box)

        # ── Progress ──────────────────────────────────────────────────
        self.pbar = QProgressBar()
        self.pbar.setRange(0, 100)
        self.pbar.setStyleSheet(
            "QProgressBar{border:1px solid #444;border-radius:3px;"
            "text-align:center;color:#fff;height:16px;}"
            "QProgressBar::chunk{background:#2a5;}"
        )
        lay.addWidget(self.pbar)

        self.plbl = QLabel("")
        self.plbl.setStyleSheet("color:#aaa;font-size:10px;")
        self.plbl.setWordWrap(True)
        lay.addWidget(self.plbl)

        self.btn_stop = QPushButton("⏹ Stop")
        self.btn_stop.setEnabled(False)
        self.btn_stop.setStyleSheet(
            "QPushButton{background:#722;color:white;"
            "border-radius:4px;padding:4px;}"
            "QPushButton:hover{background:#944;}"
            "QPushButton:disabled{background:#333;color:#555;}"
        )
        self.btn_stop.clicked.connect(self.stop.emit)
        lay.addWidget(self.btn_stop)
        lay.addStretch()

    # ── Helpers ───────────────────────────────────────────────────────

    def _parse(self, txt):
        try:
            return [float(x.strip()) for x in txt.split(",") if x.strip()]
        except ValueError:
            return []

    def _emit_p1(self):
        override = self._p1_override.value()
        # None = auto (diameter=0 in spinbox), positive = manual override
        diam = None if override <= 0 else override
        self.run_p1.emit([diam])

    def _emit_p2(self):
        if not self._p2_diam_set:
            QMessageBox.warning(
                self, "Info",
                "Please run Phase 1 first to confirm the diameter."
            )
            return
        flows = self._parse(self.flow_edit.text())
        probs = self._parse(self.prob_edit.text())
        if not flows or not probs:
            QMessageBox.warning(self, "Error", "Invalid flow/cellprob format")
            return
        payload = {
            "diameter": self._p2_diam,   # None = auto, float = override
            "flow": flows, "prob": probs,
        }
        payload.update(self.get_selected_method_config())
        self.run_p2.emit(payload)

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
            d = p.get("diameter") or 0
            fl = p.get("flow_threshold", 0.4)
            cp = p.get("cellprob_threshold", 0.0)
            self._man_diam.setValue(d)
            self._man_flow.setValue(fl)
            self._man_prob.setValue(cp)
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
                self._expand_dist.setValue(float(p.get("expand_distance")))
                data["params"]["expand_distance"] = float(p.get("expand_distance"))
            params = normalize_segmentation_config(data)
            self.params_ready.emit(params)
        except Exception as e:
            QMessageBox.warning(self, "Error", "Failed to load params:\n" + str(e))

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

    def set_p2_diam(self, d):
        self._p2_diam     = d
        self._p2_diam_set = True
        label = "auto (cpsam)" if d is None else str(d)
        self.p2_diam_lbl.setText(f"diameter = {label}  (from Phase 1)")
        self.btn_p2.setEnabled(True)

    def set_running(self, running):
        self.btn_p1.setEnabled(not running)
        self.btn_p2.setEnabled(not running and self._p2_diam_set)
        self.btn_stop.setEnabled(running)

    def update_progress(self, done, total, msg):
        if total > 0:
            self.pbar.setValue(int(done / total * 100))
        self.plbl.setText(f"[{done}/{total}]  {msg}")

    def get_current_params(self):
        """Return spinbox values regardless of source."""
        d  = self._man_diam.value()
        fl = self._man_flow.value()
        cp = self._man_prob.value()
        params = {
            "diameter":           d if d > 0 else None,
            "flow_threshold":     fl,
            "cellprob_threshold": cp,
        }
        params.update(self.get_selected_method_config())
        return params

    def get_selected_method_config(self):
        method = self._method_combo.currentData() or CELLPOSE_WHOLECELL_FUSION
        cfg = get_segmentation_method_config(method)
        params = dict(cfg.get("params") or {})
        if method == STARDIST_NUCLEI_EXPANSION:
            params["expand_distance"] = self._expand_dist.value()
        return {"method": method, "params": params}

    def _on_method_changed(self):
        method = self._method_combo.currentData() or CELLPOSE_WHOLECELL_FUSION
        cfg = get_segmentation_method_config(method)
        self._expand_dist.setEnabled(method == STARDIST_NUCLEI_EXPANSION)
        self._method_hint.setText(
            f"{method}  |  input={cfg.get('input_type')}  output={cfg.get('output_type')}"
        )


# ══════════════════════════════════════════════════════════════════════
#  Main Window
# ══════════════════════════════════════════════════════════════════════

class RawDataPrefetchWorker(QThread):
    """后台IO worker：批量读取指定通道的所有patch原始数据，写入raw_cache。
    不做任何计算，只负责磁盘→内存的IO，尽快完成。
    """
    finished = pyqtSignal(str, int, object)   # (channel, patch_idx, raw_array)
    error    = pyqtSignal(str, int, str)      # (channel, patch_idx, msg)

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
            self.finished.emit(self.channel_name, self.patch_idx, raw)
        except Exception:
            if not self._stop:
                self.error.emit(self.channel_name, self.patch_idx, traceback.format_exc())


class BgComputeWorker(QThread):
    """纯GPU计算worker：接受已读好的raw数组，不做任何IO。
    tophat + cucim + norm + metrics，全部在GPU上完成。
    """
    finished = pyqtSignal(int, dict)
    error    = pyqtSignal(int, str)

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
                "original_metrics": _compute_bg_metrics(raw),
                "tophat_metrics":   _compute_bg_metrics(tophat),
                "cucim_metrics":    _compute_bg_metrics(cucim),
            }
            self.finished.emit(self.request_id, payload)
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
    channel_patch_done = pyqtSignal(str, int, dict)   # (channel, patch_idx, payload)
    channel_done       = pyqtSignal(str)               # channel全部patches完成
    all_done           = pyqtSignal()
    progress           = pyqtSignal(int, int, str)     # (done, total, msg)
    error_signal       = pyqtSignal(str, int, str)     # (channel, patch_idx, msg)
    canceled           = pyqtSignal()

    def __init__(self, loader, patches, methods, nucleus_channel,
                 tophat_radius, cucim_sigma, max_gpu_workers=4, parent=None):
        super().__init__(parent)
        self.loader           = loader
        self.patches          = list(patches)
        self.methods          = dict(methods)          # {ch: "original"|"tophat"|"cucim"|"both"}
        self.nucleus_channel  = nucleus_channel
        self.tophat_radius    = int(tophat_radius)
        self.cucim_sigma      = int(cucim_sigma)
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

                # GPU计算（并行，max_gpu_workers个同时跑）
                # 用默认参数固定捕获method和raws，防止闭包引用变化
                def _compute_one(p_idx, _method=method, _raws=raws):
                    if self._stop:
                        return p_idx, None
                    raw = _raws.get(p_idx)
                    if raw is None:
                        return p_idx, None
                    nuc_raw  = nuc_raws.get(p_idx)
                    nuc_norm = OMETIFFLoader._norm(nuc_raw) if nuc_raw is not None else None
                    orig_norm = OMETIFFLoader._norm(raw)

                    tophat_norm = cucim_norm = None
                    print(f"[DEBUG _compute_one] p_idx={p_idx} _method={_method!r}")
                    if _method in ("tophat", "both"):
                        th = _apply_tophat_gpu_or_cpu(raw, self.tophat_radius)
                        tophat_norm = OMETIFFLoader._norm(th)
                        tophat_raw  = th
                        print(f"[DEBUG] tophat done, norm shape={tophat_norm.shape}")
                    else:
                        tophat_raw = None
                    if _method in ("cucim", "both"):
                        print(f"[DEBUG] starting cucim...")
                        cu = _apply_cucim_or_cpu(raw, self.cucim_sigma, prefer_gpu=CUCIM_AVAILABLE)
                        cucim_norm = OMETIFFLoader._norm(cu)
                        cucim_raw  = cu
                        print(f"[DEBUG] cucim done, norm shape={cucim_norm.shape}")
                    else:
                        cucim_raw = None
                        print(f"[DEBUG] cucim skipped")
                    print(f"[DEBUG] payload cucim_norm is None: {cucim_norm is None}")

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
    finished = pyqtSignal(int, dict)
    error = pyqtSignal(int, str)

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
            self.finished.emit(self.request_id, payload)
        except Exception:
            if not self._stop:
                self.error.emit(self.request_id, traceback.format_exc())


class WsiCorrectionWorker(QThread):
    progress = pyqtSignal(int, int, int, int, str, str, int)
    finished = pyqtSignal(str, dict)
    canceled = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, loader, output_dir, correction_config, rois=None, parent=None):
        super().__init__(parent)
        self.loader = loader
        self.output_dir = output_dir
        self.correction_config = _normalize_correction_config(correction_config) or {}
        self.rois = list(rois or [])
        self._cancel_requested = False

    def stop_after_current_channel(self):
        self._cancel_requested = True

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

    def run(self):
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
            channels = [
                (
                    ch,
                    method,
                    int(
                        params.get(
                            "tophat_radius" if method == "tophat" else "cucim_sigma",
                            TOPHAT_RADIUS_DEFAULT if method == "tophat" else CUCIM_SIGMA_DEFAULT,
                        )
                    ),
                )
                for ch, method in decisions.items()
                if method in {"tophat", "cucim"} and ch in self.loader.ch_map
            ]
            zarr_path = os.path.join(self.output_dir, "corrected_channels.zarr")

            if os.path.exists(zarr_path):
                shutil.rmtree(zarr_path, ignore_errors=True)

            if not channels:
                self.finished.emit("", {})
                return

            os.makedirs(self.output_dir, exist_ok=True)
            root = zarr.open_group(zarr_path, mode="w")
            root.attrs["mode"] = "roi_only"
            root.attrs["source_ome"] = os.path.abspath(getattr(self.loader, "filepath", "") or "")
            root.attrs["output_dir"] = os.path.abspath(self.output_dir)
            root.attrs["created_by"] = "Step0"
            root.attrs["roi_names"] = [str(r.get("name") or f"ROI_{i}") for i, r in enumerate(rois, start=1)]
            root.attrs["correction_config"] = self.correction_config
            corrected_decisions = {}
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
                for _, _, param in channels:
                    tile_counts.append(len(list(_tile_slices(roi_h, roi_w, 4096, max(1, 2 * param)))))
            total_units = sum(tile_counts)
            started = time.time()
            completed_units = 0
            progress_idx = 0

            for info in roi_infos:
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
                    overlap = max(1, 2 * param)
                    tiles = list(_tile_slices(roi_h, roi_w, 4096, overlap))
                    ds = group.create_dataset(
                        ch_name,
                        shape=(roi_h, roi_w),
                        chunks=(min(1024, roi_h), min(1024, roi_w)),
                        dtype=np.float32,
                        overwrite=True,
                    )

                    for tile_idx, (core, padded, crop) in enumerate(tiles, start=1):
                        y0, y1, x0, x1 = core
                        py0, py1, px0, px1 = padded
                        cy0, cy1, cx0, cx1 = crop
                        raw = self.loader._read_roi_zarr(
                            self.loader.ch_map[ch_name],
                            roi_y0 + py0, roi_y0 + py1,
                            roi_x0 + px0, roi_x0 + px1,
                        ).astype(np.float32, copy=False)
                        if method == "tophat":
                            corr = _apply_tophat_cpu(raw, param)
                        else:
                            corr = _apply_cucim_or_cpu(raw, param, prefer_gpu=CUCIM_AVAILABLE)
                        out = corr[cy0:cy1, cx0:cx1].astype(np.float32, copy=False)
                        if poly_mask is not None:
                            out = out.copy()
                            out[~poly_mask[y0:y1, x0:x1]] = 0
                        ds[y0:y1, x0:x1] = out
                        elapsed = max(0.001, time.time() - started)
                        done_units = completed_units + tile_idx
                        remain = int((total_units - done_units) * (elapsed / done_units))
                        self.progress.emit(
                            progress_idx,
                            channel_total,
                            tile_idx,
                            len(tiles),
                            f"{info['group_name']}/{ch_name}",
                            method,
                            remain,
                        )

                    corrected_decisions[ch_name] = method
                    completed_units += len(tiles)
                    if self._cancel_requested:
                        shutil.rmtree(zarr_path, ignore_errors=True)
                        self.canceled.emit(zarr_path)
                        return

            self.finished.emit(zarr_path, corrected_decisions)
        except Exception:
            self.error.emit(traceback.format_exc())


class _WsiCorrectionProgressDialog(QDialog):
    cancel_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._allow_close = False
        self.setModal(True)
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
        self._cancel.setStyleSheet(
            "QPushButton{background:#722;color:white;border-radius:4px;padding:6px 18px;}"
            "QPushButton:hover{background:#944;}"
        )
        self._cancel.clicked.connect(self._on_cancel)
        lay.addWidget(self._cancel, alignment=Qt.AlignRight)

    def _on_cancel(self):
        self._cancel.setEnabled(False)
        self._label.setText("Cancellation requested. The current channel will finish before cleanup.")
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
