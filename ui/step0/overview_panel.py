"""
block01/ui/step0/overview_panel.py — TileSelectDialog, FullFusionWorker, OverviewPanel.
"""

import os
import gc
import json
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import numpy as np
import tifffile
import zarr

from PyQt5 import QtWidgets, QtCore, QtGui
from PyQt5.QtCore import Qt, QRectF, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QGroupBox, QSizePolicy,
    QDialog, QTableWidget, QTableWidgetItem, QHeaderView,
    QMessageBox, QFileDialog,
    QComboBox, QDoubleSpinBox, QFrame, QInputDialog, QSlider,
)
import pyqtgraph as pg

from ...config import (
    OUTPUT_DIR, ROI_COLORS, PATCH_COLORS, OVERVIEW_DOWNSAMPLE,
    NUCLEUS_CONFIG,
)
from ...workers.cellpose_worker import OverviewLoaderThread
from ...core.fusion_engine import FusionEngine

class TileSelectDialog(QDialog):
    """
    Shows a table of preset tile grid options with RAM estimates.
    User selects a row and confirms to get (n_rows, n_cols).
    """

    # Preset grid options: (n_rows, n_cols)
    PRESETS = [
        (1, 1),
        (2, 2),
        (2, 3),
        (3, 3),
        (3, 4),
        (4, 4),
        (4, 6),
        (6, 6),
    ]

    def __init__(self, full_h, full_w, n_channels, sys_ram_gb=128, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select Tile Grid")
        self.setModal(True)
        self.setMinimumWidth(620)
        self.full_h     = full_h
        self.full_w     = full_w
        self.n_channels = n_channels
        self.sys_ram_gb = sys_ram_gb
        self._selected  = None   # (n_rows, n_cols)
        self._build_ui()

    def _ram_gb(self, n_rows, n_cols):
        """
        Peak RAM estimate per tile.
        Each channel is read as uint16 then converted to float32 for fusion.
        Peak = tile area × n_channels × (2B uint16 + 4B float32) + 2×float32 accum arrays
             ≈ tile_h × tile_w × n_channels × 6  (conservative)
        Plus output uint16 (H×W×2×2B) = tile_h × tile_w × 4
        """
        th = -(-self.full_h // n_rows)   # ceil div
        tw = -(-self.full_w // n_cols)
        return (th * tw * self.n_channels * 6 + th * tw * 4) / 1e9

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(8)

        # Header info
        info = QLabel(
            f"<b>Image:</b> {self.full_h:,} × {self.full_w:,} px  &nbsp;|&nbsp; "
            f"<b>Active channels:</b> {self.n_channels}  &nbsp;|&nbsp; "
            f"<b>System RAM:</b> {self.sys_ram_gb} GB"
        )
        info.setStyleSheet("font-size:12px;color:#ddd;padding:4px;")
        lay.addWidget(info)

        # Table
        cols = ["Grid", "Tiles", "Tile size (px)", "Peak RAM / tile", "Status"]
        self.table = QTableWidget(len(self.PRESETS), len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setStyleSheet(
            "QTableWidget{background:#1a1a1a;color:#ddd;gridline-color:#333;"
            "font-size:12px;border:1px solid #444;}"
            "QTableWidget::item:selected{background:#246;}"
            "QHeaderView::section{background:#2a2a2a;color:#aaa;"
            "padding:4px;border:none;font-size:11px;}"
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)

        safe_limit = self.sys_ram_gb * 0.6   # use 60% RAM as safe threshold

        default_row = 0
        for row, (nr, nc) in enumerate(self.PRESETS):
            th = -(-self.full_h // nr)
            tw = -(-self.full_w // nc)
            n_tiles = nr * nc
            ram     = self._ram_gb(nr, nc)

            if ram <= safe_limit * 0.5:
                status, color = "✓  Safe", "#4c4"
            elif ram <= safe_limit:
                status, color = "△  OK", "#fc4"
            elif ram <= self.sys_ram_gb * 0.9:
                status, color = "⚠  Caution", "#f84"
            else:
                status, color = "✗  Risky", "#f44"

            data = [
                f"{nr} × {nc}",
                str(n_tiles),
                f"{th:,} × {tw:,}",
                f"{ram:.1f} GB",
                status,
            ]
            for col, txt in enumerate(data):
                item = QTableWidgetItem(txt)
                item.setTextAlignment(Qt.AlignCenter)
                if col == 4:
                    item.setForeground(QtGui.QColor(color))
                self.table.setItem(row, col, item)

            # Default selection: first row where RAM ≤ safe_limit
            if ram <= safe_limit and default_row == 0 and row > 0:
                default_row = row

        self.table.selectRow(default_row)
        self.table.doubleClicked.connect(self._accept)
        lay.addWidget(self.table)

        # Custom input row
        custom_box = QGroupBox("Custom grid")
        custom_box.setStyleSheet(
            "QGroupBox{border:1px solid #555;border-radius:4px;"
            "color:#aaa;font-size:11px;margin-top:4px;}"
        )
        cl = QHBoxLayout(custom_box)
        cl.addWidget(QLabel("Rows:"))
        self._custom_rows = QtWidgets.QSpinBox()
        self._custom_rows.setRange(1, 20)
        self._custom_rows.setValue(3)
        cl.addWidget(self._custom_rows)
        cl.addWidget(QLabel("Cols:"))
        self._custom_cols = QtWidgets.QSpinBox()
        self._custom_cols.setRange(1, 20)
        self._custom_cols.setValue(4)
        cl.addWidget(self._custom_cols)
        self._custom_ram = QLabel("")
        self._custom_ram.setStyleSheet("color:#aaa;font-size:11px;")
        cl.addWidget(self._custom_ram)
        cl.addStretch()
        btn_use = QPushButton("Use Custom")
        btn_use.setStyleSheet(
            "QPushButton{background:#255;color:white;border-radius:3px;"
            "padding:3px 10px;font-size:11px;}"
        )
        btn_use.clicked.connect(self._use_custom)
        cl.addWidget(btn_use)
        self._custom_rows.valueChanged.connect(self._update_custom_ram)
        self._custom_cols.valueChanged.connect(self._update_custom_ram)
        self._update_custom_ram()
        lay.addWidget(custom_box)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("Cancel")
        btn_cancel.setStyleSheet(
            "QPushButton{color:#c44;border:1px solid #c44;"
            "border-radius:4px;padding:5px 16px;}"
        )
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)

        self.btn_ok = QPushButton("▶  Start Fusion")
        self.btn_ok.setStyleSheet(
            "QPushButton{background:#2a5;color:white;border-radius:4px;"
            "padding:6px 20px;font-weight:bold;font-size:12px;}"
            "QPushButton:hover{background:#3b6;}"
        )
        self.btn_ok.clicked.connect(self._accept)
        btn_row.addWidget(self.btn_ok)
        lay.addLayout(btn_row)

    def _update_custom_ram(self):
        nr = self._custom_rows.value()
        nc = self._custom_cols.value()
        ram = self._ram_gb(nr, nc)
        safe = self.sys_ram_gb * 0.6
        color = "#4c4" if ram <= safe * 0.5 else "#fc4" if ram <= safe else "#f44"
        th = -(-self.full_h // nr)
        tw = -(-self.full_w // nc)
        self._custom_ram.setText(
            f"→ {nr*nc} tiles, {th:,}×{tw:,} px, "
            f"<span style='color:{color}'>{ram:.1f} GB / tile</span>"
        )
        self._custom_ram.setTextFormat(Qt.RichText)

    def _use_custom(self):
        self._selected = (self._custom_rows.value(), self._custom_cols.value())
        self.accept()

    def _accept(self):
        rows_sel = self.table.currentRow()
        if rows_sel >= 0:
            self._selected = self.PRESETS[rows_sel]
        self.accept()

    def get_selection(self):
        """Returns (n_rows, n_cols) or None if cancelled."""
        return self._selected


# ══════════════════════════════════════════════════════════════════════
#  Full Fusion Worker  (block02 logic, inlined)
# ══════════════════════════════════════════════════════════════════════

class FullFusionWorker(QThread):
    """
    Runs the full-image channel fusion in a background thread.

    Fusion logic (mirrors FusionEngine.compute exactly):
      group signal = weighted sum of channels → normalise → × group_weight
      cyto = per-pixel max across groups → normalise
      nucleus = nucleus_channel × weight → normalise

    IO optimisation (方案一): for each tile, all required channels are
    read in parallel using a ThreadPoolExecutor. Each thread opens its
    own TiffFile handle and reads only the tile region via zarr, so
    there is no file-handle contention and NVMe queue depth is fully
    utilised.

    Output: (H, W, 2) uint16 zarr written chunk-by-chunk. The full
    fused image never lives in RAM simultaneously.
    """

    progress   = pyqtSignal(int, int, str)   # done, total, msg
    finished   = pyqtSignal(str)             # zarr_path on success
    error      = pyqtSignal(str)             # traceback string

    # Max parallel IO threads per tile (tune to NVMe queue depth)
    MAX_IO_WORKERS = 8

    def __init__(self, loader, fusion_cfg, n_rows, n_cols,
                 zarr_chunk=1024, preview_ds=16, rois=None):
        super().__init__()
        self.loader      = loader
        self.fusion_cfg  = fusion_cfg
        self.n_rows      = n_rows
        self.n_cols      = n_cols
        self.zarr_chunk  = zarr_chunk
        self.preview_ds  = preview_ds
        self.rois        = rois   # list of ROI dicts, or None (full WSI)
        self._stop       = False

    def stop(self):
        self._stop = True

    # ── helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _read_one_channel(loader, ch_name, y0, y1, x0, x1):
        """Read one channel tile through the loader so corrected ROI zarr is honored."""
        region = loader.read_region(
            ch_name, y0, y1, x0, x1,
            downsample=1,
            normalize=False,
        )
        return ch_name, region.copy()

    @staticmethod
    def _norm(arr, low, high):
        arr  = arr.astype(np.float32)
        nz   = arr[arr > 0]
        if nz.size < 100:
            return np.zeros_like(arr)
        lo, hi = np.percentile(nz, [low, high])
        if hi <= lo:
            return np.zeros_like(arr)
        return np.clip((arr - lo) / (hi - lo), 0.0, 1.0)

    def _fuse_tile(self, raw_cache, groups, group_weights,
                   nucleus_ch, nucleus_w, norm_low, norm_high):
        """Fuse a pre-loaded channel cache into (H,W,2) uint16."""
        shape = next(iter(raw_cache.values())).shape

        # ── cyto ──────────────────────────────────────────────────────
        cyto = np.zeros(shape, dtype=np.float32)
        for gname, ch_weights in groups.items():
            gw    = group_weights.get(gname, 1.0)
            accum = np.zeros(shape, dtype=np.float32)
            for ch, w in ch_weights.items():
                if ch in raw_cache and w > 0:
                    norm  = self._norm(raw_cache[ch], norm_low, norm_high)
                    accum += norm * float(w)
            mx = accum.max()
            if mx > 0:
                accum = (accum / mx) * float(gw)
            np.maximum(cyto, accum, out=cyto)
            del accum
        mx = cyto.max()
        if mx > 0:
            cyto /= mx

        # ── nucleus ───────────────────────────────────────────────────
        nucleus = np.zeros(shape, dtype=np.float32)
        if nucleus_ch and nucleus_ch in raw_cache:
            nucleus = self._norm(raw_cache[nucleus_ch], norm_low, norm_high)
            nucleus *= float(nucleus_w)
            mx = nucleus.max()
            if mx > 0:
                nucleus /= mx

        result = np.stack([
            (cyto    * 65535).astype(np.uint16),
            (nucleus * 65535).astype(np.uint16),
        ], axis=-1)
        del cyto, nucleus
        return result

    # ── main run ──────────────────────────────────────────────────────

    @staticmethod
    def _poly_mask(polygon_fullres, bbox_y0, bbox_x0, h, w):
        """
        Create a boolean mask (h, w) = True inside the polygon.
        polygon_fullres: [(x, y), ...] in full-res coords.
        bbox_y0, bbox_x0: top-left corner of the bounding box region.
        Uses cv2.fillPoly for efficiency.
        """
        import cv2 as _cv2
        mask = np.zeros((h, w), dtype=np.uint8)
        pts  = np.array(
            [[int(x - bbox_x0), int(y - bbox_y0)]
             for x, y in polygon_fullres],
            dtype=np.int32,
        )
        _cv2.fillPoly(mask, [pts], color=1)
        return mask.astype(bool)

    def run(self):
        try:
            cfg        = self.fusion_cfg
            ome_path   = cfg["ome_tiff"]
            output_dir = cfg["output_dir"]
            norm_low   = cfg.get("norm_low",  1.0)
            norm_high  = cfg.get("norm_high", 99.5)
            nucleus_ch = cfg["nucleus"]["channel"]
            nucleus_w  = cfg["nucleus"]["weight"]
            groups     = {
                gname: gdata["channels"]
                for gname, gdata in cfg["groups"].items()
            }
            group_weights = {
                gname: gdata["group_weight"]
                for gname, gdata in cfg["groups"].items()
            }

            ch_map = self.loader.ch_map
            full_h, full_w = self.loader.shape

            all_channels = set([nucleus_ch])
            for cw in groups.values():
                all_channels.update(cw.keys())
            all_channels = [ch for ch in all_channels if ch in ch_map]

            os.makedirs(output_dir, exist_ok=True)

            # ── Determine regions to fuse ─────────────────────────────
            # If ROIs defined: generate one zarr per ROI (bounding box)
            # If no ROIs:      generate one full-WSI zarr (original behaviour)
            if self.rois:
                regions = []
                for roi in self.rois:
                    bb = roi["bbox_fullres"]   # [y0, y1, x0, x1]
                    regions.append({
                        "name":    roi["name"],
                        "y0": bb[0], "y1": bb[1],
                        "x0": bb[2], "x1": bb[3],
                        "polygon_fullres": roi["polygon_fullres"],
                        "zarr_name": f"fused_{roi['name']}.zarr",
                    })
                mode_desc = f"{len(regions)} ROI(s)"
            else:
                regions = [{
                    "name": "full",
                    "y0": 0, "y1": full_h,
                    "x0": 0, "x1": full_w,
                    "polygon_fullres": None,
                    "zarr_name": "fused.zarr",
                }]
                mode_desc = "full WSI"

            self.progress.emit(0, len(regions),
                               f"Starting fusion — {mode_desc}")

            zarr_paths = {}   # {name: zarr_path}
            all_meta   = []

            for reg_i, region in enumerate(regions):
                if self._stop:
                    self.error.emit("Fusion stopped by user.")
                    return

                rname  = region["name"]
                ry0, ry1 = region["y0"], region["y1"]
                rx0, rx1 = region["x0"], region["x1"]
                rh     = ry1 - ry0
                rw     = rx1 - rx0
                zarr_path = os.path.join(output_dir, region["zarr_name"])

                self.progress.emit(
                    reg_i, len(regions),
                    f"[{rname}]  bbox y=[{ry0},{ry1}) x=[{rx0},{rx1})  "
                    f"({rh}×{rw} px)  creating zarr…"
                )

                out_zarr = zarr.open(
                    zarr_path, mode="w",
                    shape=(rh, rw, 2),
                    dtype="uint16",
                    chunks=(self.zarr_chunk, self.zarr_chunk, 2),
                )
                out_zarr.attrs["channel_0"]         = "cyto_weighted_max_projection"
                out_zarr.attrs["channel_1"]         = "nucleus"
                out_zarr.attrs["cellpose_channels"] = [1, 2]
                out_zarr.attrs["roi_name"]          = rname
                out_zarr.attrs["bbox_fullres"]      = [ry0, ry1, rx0, rx1]
                out_zarr.attrs["created_at"]        = datetime.now().isoformat()

                # Tile the region
                tile_h = -(-rh // self.n_rows)
                tile_w = -(-rw // self.n_cols)
                tiles  = []
                for tr in range(self.n_rows):
                    for tc in range(self.n_cols):
                        ty0 = ry0 + tr * tile_h
                        ty1 = min(ty0 + tile_h, ry1)
                        tx0 = rx0 + tc * tile_w
                        tx1 = min(tx0 + tile_w, rx1)
                        tiles.append((ty0, ty1, tx0, tx1))
                n_tiles    = len(tiles)
                tile_times = []

                for i, (ty0, ty1, tx0, tx1) in enumerate(tiles):
                    if self._stop:
                        self.error.emit("Fusion stopped by user.")
                        return

                    self.progress.emit(
                        reg_i, len(regions),
                        f"[{rname}] Tile [{i+1}/{n_tiles}]  "
                        f"reading {len(all_channels)} channels…"
                    )
                    t0 = time.time()

                    # Parallel channel IO
                    raw_cache = {}
                    with ThreadPoolExecutor(max_workers=self.MAX_IO_WORKERS) as pool:
                        futures = {
                            pool.submit(
                                self._read_one_channel,
                                self.loader, ch, ty0, ty1, tx0, tx1
                            ): ch
                            for ch in all_channels
                        }
                        for fut in as_completed(futures):
                            if self._stop:
                                break
                            ch_name, arr = fut.result()
                            raw_cache[ch_name] = arr

                    if self._stop:
                        self.error.emit("Fusion stopped by user.")
                        return

                    fused = self._fuse_tile(
                        raw_cache, groups, group_weights,
                        nucleus_ch, nucleus_w, norm_low, norm_high,
                    )
                    del raw_cache
                    gc.collect()

                    # Write to zarr (relative coords within this region)
                    lty0 = ty0 - ry0
                    lty1 = ty1 - ry0
                    ltx0 = tx0 - rx0
                    ltx1 = tx1 - rx0
                    out_zarr[lty0:lty1, ltx0:ltx1, :] = fused
                    del fused
                    gc.collect()

                    elapsed = time.time() - t0
                    tile_times.append(elapsed)
                    avg = sum(tile_times) / len(tile_times)
                    eta = avg * (n_tiles - i - 1)
                    self.progress.emit(
                        reg_i, len(regions),
                        f"[{rname}] ✓ Tile [{i+1}/{n_tiles}]  "
                        f"{elapsed:.1f}s  ETA {eta/60:.1f} min"
                    )

                # Apply polygon mask (zero out pixels outside polygon)
                if region["polygon_fullres"] is not None:
                    self.progress.emit(
                        reg_i, len(regions),
                        f"[{rname}] Applying polygon mask…"
                    )
                    poly_mask = self._poly_mask(
                        region["polygon_fullres"], ry0, rx0, rh, rw
                    )
                    # Zero outside polygon, chunk by chunk to save RAM
                    for cy in range(0, rh, self.zarr_chunk):
                        cy1 = min(cy + self.zarr_chunk, rh)
                        chunk = np.array(out_zarr[cy:cy1, :, :])
                        m     = poly_mask[cy:cy1, :]
                        chunk[~m] = 0
                        out_zarr[cy:cy1, :, :] = chunk
                    del poly_mask
                    gc.collect()

                # Preview PNG for this region
                try:
                    import cv2
                    ds = self.preview_ds
                    cyto_ds = out_zarr[::ds, ::ds, 0].astype(np.float32) / 65535.0
                    nuc_ds  = out_zarr[::ds, ::ds, 1].astype(np.float32) / 65535.0
                    r_ = (np.clip(cyto_ds, 0, 1) * 255).astype(np.uint8)
                    g_ = np.zeros_like(r_)
                    b_ = (np.clip(nuc_ds,  0, 1) * 255).astype(np.uint8)
                    rgb = np.stack([r_, g_, b_], axis=-1)
                    prev_name = region["zarr_name"].replace(".zarr", "_preview.png")
                    prev_path = os.path.join(output_dir, prev_name)
                    cv2.imwrite(prev_path, cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
                except Exception as e:
                    print(f"[Fusion] Preview failed ({rname}): {e}")

                zarr_paths[rname] = zarr_path
                all_meta.append({
                    "roi_name":   rname,
                    "zarr_path":  zarr_path,
                    "zarr_shape": [rh, rw, 2],
                    "bbox":       [ry0, ry1, rx0, rx1],
                    "grid":       [self.n_rows, self.n_cols],
                    "avg_tile_s": round(sum(tile_times)/len(tile_times), 1) if tile_times else 0,
                })

                self.progress.emit(
                    reg_i + 1, len(regions),
                    f"✓ [{rname}] fusion complete → {zarr_path}"
                )

            # Meta JSON
            meta = {
                "mode":       "roi" if self.rois else "full_wsi",
                "regions":    all_meta,
                "created_at": datetime.now().isoformat(),
            }
            meta_path = os.path.join(output_dir, "fusion_meta.json")
            with open(meta_path, "w") as f:
                json.dump(meta, f, indent=2)

            # Save ROI config alongside meta
            if self.rois:
                roi_cfg_path = os.path.join(output_dir, "roi_config.json")
                with open(roi_cfg_path, "w", encoding="utf-8") as f:
                    json.dump(self.rois, f, indent=2, ensure_ascii=False)

            # Return first zarr path for "Next" button
            first_zarr = list(zarr_paths.values())[0] if zarr_paths else ""
            self.finished.emit(first_zarr)

        except Exception:
            self.error.emit(traceback.format_exc())


# ══════════════════════════════════════════════════════════════════════
#  Channel Weight Row
# ══════════════════════════════════════════════════════════════════════

class ChannelWeightRow(QWidget):
    changed          = pyqtSignal()
    remove_requested = pyqtSignal(str)

    def __init__(self, ch_name, weight=1.0,
                 show_label=True, show_delete=True):
        super().__init__()
        self.ch_name = ch_name
        self._busy   = False

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 1, 0, 1)
        lay.setSpacing(4)

        if show_label:
            lbl = QLabel(ch_name)
            lbl.setFixedWidth(82)
            lbl.setStyleSheet("font-size:11px;")
            lay.addWidget(lbl)

        self.slider = QSlider(Qt.Horizontal)
        # FIX: intensity normalization
        self.slider.setRange(0, 100)
        self.slider.setValue(int(weight * 100))
        self.slider.setFixedHeight(16)
        lay.addWidget(self.slider, stretch=1)

        self.spin = QDoubleSpinBox()
        # FIX: intensity normalization
        self.spin.setRange(0.0, 1.0)
        self.spin.setSingleStep(0.05)
        self.spin.setDecimals(2)
        self.spin.setValue(weight)
        self.spin.setFixedWidth(56)
        self.spin.setStyleSheet("font-size:11px;")
        lay.addWidget(self.spin)

        if show_delete:
            btn = QPushButton("✕")
            btn.setFixedSize(20, 20)
            btn.setStyleSheet(
                "QPushButton{color:#c44;border:none;font-size:12px;}"
                "QPushButton:hover{color:#f66;}"
            )
            btn.clicked.connect(lambda: self.remove_requested.emit(self.ch_name))
            lay.addWidget(btn)

        self.slider.valueChanged.connect(self._sl)
        self.spin.valueChanged.connect(self._sp)

    def _sl(self, v):
        if self._busy:
            return
        self._busy = True
        self.spin.setValue(v / 100.0)
        self._busy = False
        self.changed.emit()

    def _sp(self, v):
        if self._busy:
            return
        self._busy = True
        self.slider.setValue(int(v * 100))
        self._busy = False
        self.changed.emit()

    def weight(self):
        return self.spin.value()


# ══════════════════════════════════════════════════════════════════════
#  Group Panel
# ══════════════════════════════════════════════════════════════════════

_GROUP_COLORS = ["#e06c75", "#98c379", "#61afef", "#e5c07b", "#c678dd"]
_grp_color_idx = 0

class GroupPanel(QGroupBox):
    config_changed = pyqtSignal()
    remove_group   = pyqtSignal(str)

    def __init__(self, group_name, ch_weights, all_channels):
        global _grp_color_idx
        super().__init__()
        self.group_name   = group_name
        self.all_channels = all_channels
        self._rows        = {}

        color = _GROUP_COLORS[_grp_color_idx % len(_GROUP_COLORS)]
        _grp_color_idx += 1
        self.setStyleSheet(
            f"QGroupBox{{border:1px solid {color};"
            f"border-radius:5px;margin-top:2px;}}"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 4, 6, 4)
        outer.setSpacing(2)

        # Header row
        hdr = QHBoxLayout()
        dot = QLabel("●")
        dot.setStyleSheet(f"color:{color};font-size:13px;")
        hdr.addWidget(dot)
        hdr.addWidget(QLabel(group_name))
        hdr.addStretch()
        hdr.addWidget(QLabel("Group weight:"))
        self.gw_row = ChannelWeightRow("", 1.0, show_label=False, show_delete=False)
        self.gw_row.changed.connect(self.config_changed.emit)
        self.gw_row.setFixedWidth(155)
        hdr.addWidget(self.gw_row)
        btn_del = QPushButton("Delete")
        btn_del.setFixedSize(44, 20)
        btn_del.setStyleSheet(
            "QPushButton{color:#c44;font-size:10px;"
            "border:1px solid #c44;border-radius:3px;}"
        )
        btn_del.clicked.connect(lambda: self.remove_group.emit(self.group_name))
        hdr.addWidget(btn_del)
        outer.addLayout(hdr)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet(f"color:{color};")
        outer.addWidget(line)

        self.ch_lay = QVBoxLayout()
        self.ch_lay.setContentsMargins(0, 0, 0, 0)
        self.ch_lay.setSpacing(0)
        outer.addLayout(self.ch_lay)

        for ch, w in ch_weights.items():
            self._add_row(ch, w)

        btn_add = QPushButton("＋ Add Channel")
        btn_add.setStyleSheet(
            "QPushButton{color:#7a7;font-size:10px;border:none;text-align:left;}"
            "QPushButton:hover{color:#afa;}"
        )
        btn_add.clicked.connect(self._add_dialog)
        outer.addWidget(btn_add)

    def _add_row(self, ch, w=1.0):
        if ch in self._rows:
            return
        row = ChannelWeightRow(ch, w)
        row.changed.connect(self.config_changed.emit)
        row.remove_requested.connect(self._del_row)
        self._rows[ch] = row
        self.ch_lay.addWidget(row)

    def _del_row(self, ch):
        if ch not in self._rows:
            return
        w = self._rows.pop(ch)
        self.ch_lay.removeWidget(w)
        w.deleteLater()
        self.config_changed.emit()

    def _add_dialog(self):
        avail = [c for c in self.all_channels if c not in self._rows]
        if not avail:
            QMessageBox.information(self, "Info", "All channels are already in this group")
            return
        ch, ok = QInputDialog.getItem(
            self, f"Add Channel → {self.group_name}", "Select:", avail, 0, False
        )
        if ok and ch:
            self._add_row(ch, 1.0)
            self.config_changed.emit()

    def channel_weights(self):
        return {ch: r.weight() for ch, r in self._rows.items()}

    def group_weight(self):
        return self.gw_row.weight()


# ══════════════════════════════════════════════════════════════════════
#  Config Panel (Fusion Groups)
# ══════════════════════════════════════════════════════════════════════

class ConfigPanel(QWidget):
    config_changed = pyqtSignal()

    def __init__(self, all_channels):
        super().__init__()
        self.all_channels = all_channels
        self._panels      = {}
        self._setup_ui()
        # No groups loaded by default — user loads panel CSV via file bar

    def _setup_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)

        btn_row = QHBoxLayout()

        # NEW: reset all channel weights button
        btn_reset = QPushButton("Reset")
        btn_reset.setStyleSheet(
            "QPushButton{background:#553;color:white;"
            "border-radius:4px;padding:4px;}"
            "QPushButton:hover{background:#775;}"
        )
        btn_reset.clicked.connect(self._reset_all_channel_weights)
        btn_row.addWidget(btn_reset)

        # NEW: load channel weights from file
        btn_load_weights = QPushButton("Load Weights")
        btn_load_weights.setStyleSheet(
            "QPushButton{background:#255;color:white;"
            "border-radius:4px;padding:4px;}"
            "QPushButton:hover{background:#377;}"
        )
        btn_load_weights.clicked.connect(self._load_weights_from_file)
        btn_row.addWidget(btn_load_weights)

        lay.addLayout(btn_row)

        # Nucleus
        nuc_box = QGroupBox("Nucleus Channel")
        nuc_box.setStyleSheet(
            "QGroupBox{border:1px solid #666;border-radius:4px;"
            "font-weight:bold;font-size:11px;}"
        )
        nl = QHBoxLayout(nuc_box)
        nl.addWidget(QLabel("Channel:"))
        self.nuc_combo = QComboBox()
        self.nuc_combo.addItems(self.all_channels)
        idx = self.nuc_combo.findText(NUCLEUS_CONFIG["channel"])
        if idx >= 0:
            self.nuc_combo.setCurrentIndex(idx)
        self.nuc_combo.currentTextChanged.connect(self.config_changed.emit)
        nl.addWidget(self.nuc_combo)
        nl.addWidget(QLabel("Weight:"))
        self.nuc_row = ChannelWeightRow("", NUCLEUS_CONFIG["weight"],
                                        show_label=False, show_delete=False)
        self.nuc_row.changed.connect(self.config_changed.emit)
        nl.addWidget(self.nuc_row, stretch=1)
        lay.addWidget(nuc_box)

        # Groups (scrollable)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea{border:none;}")
        self.grp_cont = QWidget()
        self.grp_lay  = QVBoxLayout(self.grp_cont)
        self.grp_lay.setContentsMargins(0, 0, 0, 0)
        self.grp_lay.setSpacing(5)
        self.grp_lay.addStretch()
        scroll.setWidget(self.grp_cont)
        lay.addWidget(scroll, stretch=1)

        btn_new = QPushButton("＋ New Group")
        btn_new.setStyleSheet(
            "QPushButton{background:#255;color:white;"
            "border-radius:4px;padding:4px;}"
            "QPushButton:hover{background:#377;}"
        )
        btn_new.clicked.connect(self._new_group)
        lay.addWidget(btn_new)

    def _add_group(self, name, cw=None):
        if name in self._panels:
            return
        p = GroupPanel(name, cw or {}, self.all_channels)
        p.config_changed.connect(self.config_changed.emit)
        p.remove_group.connect(self._del_group)
        self._panels[name] = p
        self.grp_lay.insertWidget(self.grp_lay.count() - 1, p)

    def _del_group(self, name):
        if name not in self._panels:
            return
        p = self._panels.pop(name)
        self.grp_lay.removeWidget(p)
        p.deleteLater()
        self.config_changed.emit()

    def _new_group(self):
        name, ok = QInputDialog.getText(self, "New Group", "Group name:")
        if ok and name.strip():
            self._add_group(name.strip())
            self.config_changed.emit()

    def _reset_all_channel_weights(self):
        # NEW: reset all channel weights button
        # UPDATE: reset keeps nucleus channel
        nuc_ch = self.nuc_combo.currentText().strip()
        for panel in self._panels.values():
            for row in panel._rows.values():
                if row.ch_name != nuc_ch:
                    row.spin.setValue(0.0)
        self.config_changed.emit()

    def _load_weights_from_file(self):
        # NEW: load channel weights from file
        # FIX: load weights dialog starts from current output directory
        mw = self.window()
        out_dir = ""
        if hasattr(mw, "_out_path_edit"):
            out_dir = mw._out_path_edit.text().strip()
        start_dir = out_dir if out_dir and os.path.exists(out_dir) else os.getcwd()
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Channel Weights", start_dir, "JSON Files (*.json)"
        )
        if not path:
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            QMessageBox.warning(self, "Load Weights", f"Invalid JSON file:\n{e}")
            return

        try:
            nucleus_cfg = cfg.get("nucleus") or {}
            groups_cfg = cfg.get("groups") or {}

            nuc_ch = nucleus_cfg.get("channel")
            if nuc_ch:
                idx = self.nuc_combo.findText(str(nuc_ch))
                if idx >= 0:
                    self.nuc_combo.setCurrentIndex(idx)
            self.nuc_row.spin.setValue(float(nucleus_cfg.get("weight", 0.0)))

            file_groups = {}
            for gname, gdata in groups_cfg.items():
                if not isinstance(gdata, dict):
                    continue
                ch_cfg = gdata.get("channels") or {}
                file_groups[gname] = {
                    "group_weight": float(gdata.get("group_weight", 0.0)),
                    "channels": {
                        str(ch): float(w)
                        for ch, w in ch_cfg.items()
                    },
                }

            for gname in file_groups:
                if gname not in self._panels:
                    self._add_group(gname, {})

            for gname, panel in self._panels.items():
                gdata = file_groups.get(gname, {})
                panel.gw_row.spin.setValue(float(gdata.get("group_weight", 0.0)))
                channels_cfg = gdata.get("channels", {})

                for ch in channels_cfg:
                    if ch in self.all_channels and ch not in panel._rows:
                        panel._add_row(ch, 0.0)

                for ch, row in panel._rows.items():
                    row.spin.setValue(float(channels_cfg.get(ch, 0.0)))

            self.config_changed.emit()

        except Exception as e:
            QMessageBox.warning(
                self, "Load Weights",
                f"Failed to apply weight configuration:\n{e}"
            )

    def get_groups(self):
        return {n: p.channel_weights() for n, p in self._panels.items()}

    def get_group_weights(self):
        return {n: p.group_weight() for n, p in self._panels.items()}

    def get_nucleus(self):
        return self.nuc_combo.currentText(), self.nuc_row.weight()

    def load_panel(self, groups, nuc_ch):
        """
        Load panel from parsed CSV.
        groups: {'group_name': {'CH': 1.0, ...}, ...}
        nuc_ch: str or None
        """
        # Clear existing groups
        for name in list(self._panels.keys()):
            self._del_group(name)

        # Set nucleus channel
        # FIX: initial weights
        if nuc_ch:
            idx = self.nuc_combo.findText(nuc_ch)
            if idx >= 0:
                self.nuc_combo.setCurrentIndex(idx)
                self.nuc_row.spin.setValue(1.0)
            else:
                self.nuc_combo.setCurrentIndex(-1)
                self.nuc_row.spin.setValue(0.0)
        else:
            self.nuc_combo.setCurrentIndex(-1)
            self.nuc_row.spin.setValue(0.0)

        # Add groups from CSV
        for gname, channels in groups.items():
            zeroed = {ch: 0.0 for ch in channels.keys()}
            self._add_group(gname, zeroed)

        self.config_changed.emit()

    def get_full_config(self):
        nuc_ch, nuc_w = self.get_nucleus()
        return {
            "nucleus": {"channel": nuc_ch, "weight": nuc_w},
            "groups":  {
                n: {"group_weight": p.group_weight(),
                    "channels":     p.channel_weights()}
                for n, p in self._panels.items()
            },
        }


# ══════════════════════════════════════════════════════════════════════
#  Overview Panel  (ROI polygon + Patch rectangle dual-mode)
# ══════════════════════════════════════════════════════════════════════

class OverviewPanel(QWidget):
    """
    Left panel showing the DAPI overview.

    Two independent drawing tools, switched via toolbar buttons:
      🔲 ROI   — click vertices to draw a polygon ROI; Enter/right-click closes it
      📍 Patch — drag a rectangle; if ROIs exist the patch centre must be inside one

    Data model
    ──────────
    _rois    : [{"name", "color", "polygon_display", "polygon_fullres",
                 "downsample", "bbox_fullres", "patch_indices": [int,…]}, …]
    _patches : [{"roi_idx": int|None, "coords": (y0,y1,x0,x1)}, …]

    Patch numbering is always 1-based and contiguous (renumbered on delete).

    Signals
    ───────
    patches_changed(list)  — list of (y0,y1,x0,x1) tuples, one per patch
    rois_changed(list)     — list of roi dicts
    """

    patches_changed = pyqtSignal(list)   # [(y0,y1,x0,x1), ...]
    rois_changed    = pyqtSignal(list)   # [roi_dict, ...]

    def __init__(self, loader, nuc_ch: str, lazy: bool = False):
        super().__init__()
        self.setMinimumSize(260, 300)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.loader  = loader
        self.nuc_ch  = nuc_ch
        self.ds      = OVERVIEW_DOWNSAMPLE
        self.full_h  = loader.shape[0] if loader else 0
        self.full_w  = loader.shape[1] if loader else 0

        # ── Data model ────────────────────────────────────────────────
        self._rois    = []
        self._patches = []
        self._selected_patch_idx = -1

        # ── Drawing state ─────────────────────────────────────────────
        self._mode            = 'patch'
        self._drag_start      = None
        self._pan_last        = None
        self._right_press_pos = None

        # ROI in-progress drawing
        self._cur_pts     = []
        self._cur_line    = None
        self._cur_preview = None

        # pyqtgraph items parallel to _rois / _patches
        self._roi_artists   = []
        self._patch_artists = []

        self._setup_ui()
        if not lazy:
            self._load_overview()

    def sizeHint(self):
        return QtCore.QSize(360, 380)

    def minimumSizeHint(self):
        return QtCore.QSize(260, 300)

    # ── UI construction ───────────────────────────────────────────────

    def _setup_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(3)

        # ── Hint label ────────────────────────────────────────────────
        self.hint = QLabel(
            "Left-drag = Draw ROI/Patch  |  Scroll-wheel/Middle-drag = Pan  "
            "|  Scroll = Zoom  |  Double-click = Reset"
        )
        self.hint.setAlignment(Qt.AlignCenter)
        self.hint.setStyleSheet("color:#777;font-size:10px;")
        lay.addWidget(self.hint)

        # ── pyqtgraph canvas ──────────────────────────────────────────
        self.gview = pg.GraphicsLayoutWidget()
        self.gview.setBackground("#111")
        self.gview.setMinimumSize(240, 260)
        self.gview.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.vb = self.gview.addViewBox(row=0, col=0)
        self.vb.setAspectLocked(True)
        self.vb.invertY(True)
        self.vb.setMouseEnabled(x=False, y=False)
        self.vb.setMenuEnabled(False)
        self.img_item = pg.ImageItem()
        self.vb.addItem(self.img_item)

        self.gview.viewport().installEventFilter(self)
        self.gview.scene().sigMouseClicked.connect(self._on_overview_click)

        # Temp rect for patch drag
        self._temp = pg.RectROI(
            [0, 0], [1, 1],
            pen=pg.mkPen("#fff", width=1, style=Qt.DashLine),
            movable=False, resizable=False,
        )
        self._temp.setVisible(False)
        self.vb.addItem(self._temp)
        lay.addWidget(self.gview)

        # ── Status + info ─────────────────────────────────────────────
        self.status = QLabel("Loading...")
        self.status.setAlignment(Qt.AlignCenter)
        self.status.setStyleSheet("color:#888;font-size:10px;")
        lay.addWidget(self.status)

        self._info_lbl = QLabel("")
        self._info_lbl.setStyleSheet("color:#bbb;font-size:10px;padding:2px;")
        self._info_lbl.setWordWrap(True)
        lay.addWidget(self._info_lbl)

        # ── ROI controls (shown in ROI mode) ─────────────────────────
        self._roi_ctrl = QWidget()
        rc = QVBoxLayout(self._roi_ctrl)
        rc.setContentsMargins(0, 0, 0, 0)
        rc.setSpacing(2)

        roi_row = QHBoxLayout()
        roi_row.addWidget(QLabel("ROI name:"))
        self._roi_name_edit = QtWidgets.QLineEdit("ROI_1")
        self._roi_name_edit.setFixedWidth(72)
        self._roi_name_edit.setStyleSheet("font-size:11px;")
        roi_row.addWidget(self._roi_name_edit)

        btn_close = QPushButton("✓ Close (Enter)")
        btn_close.setStyleSheet(
            "QPushButton{color:#6bcb77;font-size:10px;"
            "border:1px solid #6bcb77;border-radius:3px;padding:2px 5px;}"
        )
        btn_close.clicked.connect(self._finish_roi)
        roi_row.addWidget(btn_close)

        btn_undo = QPushButton("Z Undo")
        btn_undo.setStyleSheet(
            "QPushButton{color:#ffd93d;font-size:10px;"
            "border:1px solid #ffd93d;border-radius:3px;padding:2px 5px;}"
        )
        btn_undo.clicked.connect(self._undo_vertex)
        roi_row.addWidget(btn_undo)

        btn_del_roi = QPushButton("D Del ROI")
        btn_del_roi.setStyleSheet(
            "QPushButton{color:#c44;font-size:10px;"
            "border:1px solid #c44;border-radius:3px;padding:2px 5px;}"
        )
        btn_del_roi.clicked.connect(self._delete_last_roi)
        roi_row.addWidget(btn_del_roi)
        roi_row.addStretch()
        rc.addLayout(roi_row)

        sl_row = QHBoxLayout()
        for label, slot, color in [
            ("📂 Load ROIs",  self.load_rois,  "#4d96ff"),
            ("✕ Clear ROIs", self.clear_rois, "#c44"),
        ]:
            btn = QPushButton(label)
            btn.setStyleSheet(
                f"QPushButton{{color:{color};font-size:10px;"
                f"border:1px solid {color};border-radius:3px;"
                f"padding:2px 5px;}}"
            )
            btn.clicked.connect(slot)
            sl_row.addWidget(btn)
        sl_row.addStretch()
        rc.addLayout(sl_row)
        self._roi_ctrl.setVisible(False)
        lay.addWidget(self._roi_ctrl)

        # ── Patch controls (shown in Patch mode) ─────────────────────
        self._patch_ctrl = QWidget()
        pc = QHBoxLayout(self._patch_ctrl)
        pc.setContentsMargins(0, 0, 0, 0)
        btn_clr_p = QPushButton("✕ Clear Patches")
        btn_clr_p.setStyleSheet(
            "QPushButton{color:#c44;font-size:11px;"
            "border:1px solid #c44;border-radius:3px;padding:2px;}"
        )
        btn_clr_p.clicked.connect(self.clear_patches)
        pc.addWidget(btn_clr_p)
        pc.addStretch()
        lay.addWidget(self._patch_ctrl)

        self.gview.viewport().installEventFilter(self)

    # ── Overview loading ──────────────────────────────────────────────

    def _load_overview(self):
        if self.loader is None or self.full_h == 0:
            self.status.setText("Please select an OME-TIFF and click Load.")
            return
        self.status.setText("Loading overview, please wait...")
        self._t0 = time.time()
        self._ov_thread = OverviewLoaderThread(
            self.loader, self.nuc_ch, self.ds
        )
        self._ov_thread.done.connect(self._on_overview_loaded)
        self._ov_thread.error.connect(
            lambda e: self.status.setText(f"Overview load failed: {e}")
        )
        self._ov_thread.start()

    def _on_overview_loaded(self, arr):
        self.ov_h, self.ov_w = arr.shape
        self.img_item.setImage(arr, autoLevels=True)
        self.vb.setRange(
            QRectF(0, 0, self.ov_w, self.ov_h), padding=0.01
        )
        self.status.setText(
            f"Full image {self.full_h}×{self.full_w} px  |  "
            f"Overview {self.ov_h}×{self.ov_w} px  "
            f"({time.time()-self._t0:.1f}s)"
        )

    # ── Coordinate helpers ────────────────────────────────────────────

    def _ov_pos(self, scene_pos):
        if not hasattr(self, 'ov_h'):
            return 0, 0
        p = self.img_item.mapFromScene(scene_pos)
        r = int(np.clip(p.y(), 0, self.ov_h - 1))
        c = int(np.clip(p.x(), 0, self.ov_w - 1))
        return r, c   # (row, col)

    def _to_fullres(self, r, c):
        """Overview (row,col) → full-res (y, x)."""
        return int(r * self.ds), int(c * self.ds)

    # ── Mode switching ────────────────────────────────────────────────

    def _set_mode(self, mode):
        self._mode = mode
        # _btn_roi / _btn_patch 已从 OverviewPanel 移除，
        # 模式切换状态由 Step0Page 工具栏按钮负责，这里只更新内部状态和UI
        self._roi_ctrl.setVisible(mode == 'roi')
        self._patch_ctrl.setVisible(mode == 'patch')
        if mode == 'roi':
            self.hint.setText(
                "Left-click = Add vertex  |  Enter/Right-click = Close ROI  |  Z = Undo  |  D = Delete  "
                "|  Scroll = Zoom  |  Right-drag = Pan  |  Double-click = Reset"
            )
            self.hint.setStyleSheet("color:#6bcb77;font-size:10px;")
        else:
            self.hint.setText(
                "Left-drag = Add Patch  |  Right-click = Delete last  "
                "|  Scroll = Zoom  |  Right-drag = Pan  |  Double-click = Reset"
            )
            self.hint.setStyleSheet("color:#777;font-size:10px;")
        # Abort any in-progress polygon when switching away
        if mode != 'roi' and self._cur_pts:
            self._cur_pts.clear()
            self._redraw_cur_polygon()

    def _on_overview_click(self, event):
        """Reset view to full image on double-click."""
        if event.double():
            if hasattr(self, 'ov_h') and hasattr(self, 'ov_w'):
                self.vb.setRange(
                    QRectF(0, 0, self.ov_w, self.ov_h), padding=0.01
                )

    # ── ROI polygon helpers ───────────────────────────────────────────

    @staticmethod
    def _point_in_polygon(px, py, poly_ov):
        """
        Ray-casting point-in-polygon test.
        poly_ov: [(col, row), …] in overview pixel coords.
        px, py: (col, row) to test.
        """
        n      = len(poly_ov)
        inside = False
        xp, yp = px, py
        j      = n - 1
        for i in range(n):
            xi, yi = poly_ov[i]
            xj, yj = poly_ov[j]
            if ((yi > yp) != (yj > yp)) and \
               (xp < (xj - xi) * (yp - yi) / (yj - yi + 1e-12) + xi):
                inside = not inside
            j = i
        return inside

    def _find_roi_for_patch(self, r, c):
        """Return index of the first ROI whose polygon contains (col=c, row=r)."""
        for i, roi in enumerate(self._rois):
            poly = roi.get("polygon_display", [])
            if poly and self._point_in_polygon(c, r, poly):
                return i
        return None

    def _redraw_cur_polygon(self):
        if self._cur_line is not None:
            self.vb.removeItem(self._cur_line)
            self._cur_line = None
        if self._cur_preview is not None:
            self.vb.removeItem(self._cur_preview)
            self._cur_preview = None
        if len(self._cur_pts) >= 1:
            xs = [p[0] for p in self._cur_pts]
            ys = [p[1] for p in self._cur_pts]
            self._cur_line = pg.PlotDataItem(
                xs, ys,
                pen=pg.mkPen('#ffff00', width=1.5, style=Qt.DashLine),
                symbol='o', symbolSize=5,
                symbolPen=pg.mkPen('#ffff00'),
                symbolBrush=pg.mkBrush('#ffff00'),
            )
            self.vb.addItem(self._cur_line)

    def _update_preview_line(self, r, c):
        if not self._cur_pts:
            return
        lc, lr = self._cur_pts[-1]
        if self._cur_preview is not None:
            self.vb.removeItem(self._cur_preview)
        self._cur_preview = pg.PlotDataItem(
            [lc, c], [lr, r],
            pen=pg.mkPen('#ffff0077', width=1, style=Qt.DotLine),
        )
        self.vb.addItem(self._cur_preview)

    def _finish_roi(self):
        if len(self._cur_pts) < 3:
            self.status.setText("⚠ ROI needs at least 3 vertices")
            return
        # 自动生成默认名，保证不重名
        base = self._roi_name_edit.text().strip() or f"ROI_{len(self._rois)+1}"
        existing = {r["name"] for r in self._rois}
        name = base
        suffix = 2
        while name in existing:
            name = f"{base}_{suffix}"
            suffix += 1
        idx   = len(self._rois)
        color = ROI_COLORS[idx % len(ROI_COLORS)]

        pts_closed = self._cur_pts + [self._cur_pts[0]]
        xs = [p[0] for p in pts_closed]
        ys = [p[1] for p in pts_closed]
        poly_item = pg.PlotDataItem(
            xs, ys,
            pen=pg.mkPen(color, width=2),
            fillLevel=0,
            brush=pg.mkBrush(color + '33'),
        )
        cx = np.mean([p[0] for p in self._cur_pts])
        cy = np.mean([p[1] for p in self._cur_pts])
        lbl_item = pg.TextItem(name, color=color, anchor=(0.5, 0.5))
        lbl_item.setPos(cx, cy)
        self.vb.addItem(poly_item)
        self.vb.addItem(lbl_item)
        self._roi_artists.append([poly_item, lbl_item])

        poly_fullres = [
            (int(c * self.ds), int(r * self.ds))
            for c, r in self._cur_pts
        ]
        xs_fr = [p[0] for p in poly_fullres]
        ys_fr = [p[1] for p in poly_fullres]
        bbox  = [
            max(0, min(ys_fr)),
            min(self.full_h, max(ys_fr)),
            max(0, min(xs_fr)),
            min(self.full_w, max(xs_fr)),
        ]
        self._rois.append({
            "name":            name,
            "color":           color,
            "polygon_display": list(self._cur_pts),
            "polygon_fullres": poly_fullres,
            "downsample":      self.ds,
            "bbox_fullres":    bbox,
            "patch_indices":   [],
        })

        self._cur_pts.clear()
        self._redraw_cur_polygon()

        # Advance default name
        next_n = len(self._rois) + 1
        self._roi_name_edit.setText(f"ROI_{next_n}")

        self._update_info()
        self.rois_changed.emit(list(self._rois))
        self.status.setText(
            f"✓ ROI '{name}' added  ({len(self._rois)} total)"
        )

    def _undo_vertex(self):
        if self._cur_pts:
            self._cur_pts.pop()
            self._redraw_cur_polygon()
            self.status.setText(
                f"Vertex removed  ({len(self._cur_pts)} pts remaining)"
            )

    def _delete_last_roi(self):
        if not self._rois:
            return
        roi = self._rois.pop()
        for a in self._roi_artists.pop():
            self.vb.removeItem(a)
        # Cascade: remove all patches belonging to this ROI
        dead_indices = set(roi.get("patch_indices", []))
        self._patches = [
            p for i, p in enumerate(self._patches)
            if i not in dead_indices
        ]
        self._rebuild_patch_artists()
        self._update_info()
        self.rois_changed.emit(list(self._rois))
        self.patches_changed.emit(self._patch_coords())
        self.status.setText(
            f"ROI '{roi['name']}' deleted (and its patches)"
        )

    def clear_rois(self):
        for arts in self._roi_artists:
            for a in arts:
                self.vb.removeItem(a)
        self._rois.clear()
        self._roi_artists.clear()
        self._cur_pts.clear()
        self._redraw_cur_polygon()
        # Remove all ROI-bound patches
        self._patches = [p for p in self._patches if p["roi_idx"] is None]
        self._rebuild_patch_artists()
        self._update_info()
        self.rois_changed.emit([])
        self.patches_changed.emit(self._patch_coords())

    def save_rois(self):
        if not self._rois:
            QMessageBox.information(self, "No ROIs", "No ROIs to save.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save ROI Config",
            os.path.join(OUTPUT_DIR, "roi_config.json"),
            "JSON (*.json)"
        )
        if not path:
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = [{k: v for k, v in r.items() if k != "patch_indices"}
                for r in self._rois]
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        QMessageBox.information(
            self, "Saved",
            f"{len(self._rois)} ROIs saved to:\n{path}"
        )

    def load_rois(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load ROI Config",
            os.path.join(OUTPUT_DIR, "roi_config.json"),
            "JSON (*.json)"
        )
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, encoding='utf-8') as f:
                rois = json.load(f)
            self.clear_rois()
            for roi in rois:
                roi["patch_indices"] = []
                self._rois.append(roi)
                color = roi.get('color', ROI_COLORS[0])
                pts   = roi.get('polygon_display', [])
                if not pts:
                    continue
                pts_c = pts + [pts[0]]
                poly_item = pg.PlotDataItem(
                    [p[0] for p in pts_c], [p[1] for p in pts_c],
                    pen=pg.mkPen(color, width=2),
                    fillLevel=0, brush=pg.mkBrush(color + '33'),
                )
                cx = np.mean([p[0] for p in pts])
                cy = np.mean([p[1] for p in pts])
                lbl_item = pg.TextItem(
                    roi['name'], color=color, anchor=(0.5, 0.5)
                )
                lbl_item.setPos(cx, cy)
                self.vb.addItem(poly_item)
                self.vb.addItem(lbl_item)
                self._roi_artists.append([poly_item, lbl_item])
            self._update_info()
            self.rois_changed.emit(list(self._rois))
            self._set_mode('roi')
            QMessageBox.information(
                self, "Loaded",
                f"{len(rois)} ROIs loaded from:\n{path}"
            )
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to load ROIs:\n{e}")

    # ── Patch helpers ─────────────────────────────────────────────────

    def _max_patches_for_roi(self, roi_idx):
        """Max 4 per ROI (or total 4 if no ROIs)."""
        if roi_idx is None:
            return 4
        return 4

    def _patches_in_roi(self, roi_idx):
        return sum(1 for p in self._patches if p["roi_idx"] == roi_idx)

    def _add_patch(self, fy0, fy1, fx0, fx1, rmin, rmax, cmin, cmax, roi_idx):
        """Add a new patch, draw its visual, update info."""
        # Check per-ROI limit
        if self._patches_in_roi(roi_idx) >= self._max_patches_for_roi(roi_idx):
            who = f"ROI {self._rois[roi_idx]['name']}" if roi_idx is not None else "the canvas"
            self.status.setText(
                f"⚠ Maximum 4 patches reached for {who}"
            )
            return

        self._patches.append({
            "roi_idx": roi_idx,
            "coords":  (fy0, fy1, fx0, fx1),
        })
        if roi_idx is not None:
            self._rois[roi_idx]["patch_indices"].append(
                len(self._patches) - 1
            )

        self._rebuild_patch_artists()
        self._update_info()
        self.patches_changed.emit(self._patch_coords())

    def _remove_last_patch(self):
        if not self._patches:
            return
        removed_idx = len(self._patches) - 1
        removed = self._patches.pop()
        # Remove from ROI's patch_indices
        if removed["roi_idx"] is not None:
            ri = removed["roi_idx"]
            if 0 <= ri < len(self._rois):
                self._rois[ri]["patch_indices"] = [
                    i for i in self._rois[ri]["patch_indices"]
                    if i != removed_idx
                ]
        self._rebuild_patch_artists()
        self._update_info()
        self.patches_changed.emit(self._patch_coords())

    def _remove_patch(self, patch_idx):
        if patch_idx < 0 or patch_idx >= len(self._patches):
            return
        removed = self._patches.pop(patch_idx)
        if removed["roi_idx"] is not None:
            ri = removed["roi_idx"]
            if 0 <= ri < len(self._rois):
                self._rois[ri]["patch_indices"] = [
                    i for i in self._rois[ri].get("patch_indices", [])
                    if i != patch_idx
                ]
        for roi in self._rois:
            roi["patch_indices"] = [
                (i - 1 if i > patch_idx else i)
                for i in roi.get("patch_indices", [])
                if i != patch_idx
            ]
        self._selected_patch_idx = min(patch_idx, len(self._patches) - 1)
        self._rebuild_patch_artists()
        self._update_info()
        self.patches_changed.emit(self._patch_coords())

    def _select_patch_artist(self, patch_idx):
        self._selected_patch_idx = patch_idx
        self._rebuild_patch_artists()

    def _on_patch_roi_changed(self, patch_idx, rect):
        if patch_idx < 0 or patch_idx >= len(self._patches):
            return
        pos = rect.pos()
        size = rect.size()
        c0 = max(0.0, float(pos.x()))
        r0 = max(0.0, float(pos.y()))
        c1 = min(float(getattr(self, "ov_w", 1)), c0 + max(1.0, float(size.x())))
        r1 = min(float(getattr(self, "ov_h", 1)), r0 + max(1.0, float(size.y())))
        fy0 = int(max(0, round(r0 * self.ds)))
        fy1 = int(min(self.full_h, round(r1 * self.ds)))
        fx0 = int(max(0, round(c0 * self.ds)))
        fx1 = int(min(self.full_w, round(c1 * self.ds)))
        if fy1 <= fy0 or fx1 <= fx0:
            return
        roi_idx = None
        if self._rois and not getattr(self, "full_wsi_mode", False):
            roi_idx = self._find_roi_for_patch((r0 + r1) / 2.0, (c0 + c1) / 2.0)
            if roi_idx is None:
                self.status.setText("⚠ Patch centre is outside all ROIs")
                self._rebuild_patch_artists()
                return
        self._patches[patch_idx] = {
            "roi_idx": roi_idx,
            "coords": (fy0, fy1, fx0, fx1),
        }
        for roi in self._rois:
            roi["patch_indices"] = []
        for i, patch in enumerate(self._patches):
            ri = patch.get("roi_idx")
            if ri is not None and 0 <= ri < len(self._rois):
                self._rois[ri].setdefault("patch_indices", []).append(i)
        self._update_info()
        self.patches_changed.emit(self._patch_coords())

    def _rebuild_patch_artists(self):
        """Remove all patch visuals and redraw from scratch (after any delete/renumber)."""
        for rect, lbl in self._patch_artists:
            self.vb.removeItem(rect)
            self.vb.removeItem(lbl)
        self._patch_artists.clear()

        for i, pd in enumerate(self._patches):
            fy0, fy1, fx0, fx1 = pd["coords"]
            rmin = fy0 // self.ds
            rmax = fy1 // self.ds
            cmin = fx0 // self.ds
            cmax = fx1 // self.ds
            color = PATCH_COLORS[i % len(PATCH_COLORS)]
            width = 3 if i == self._selected_patch_idx else 2
            rect  = pg.RectROI(
                [cmin, rmin], [cmax - cmin, rmax - rmin],
                pen=pg.mkPen(color, width=width),
                movable=True, resizable=True,
            )
            rect.setZValue(20)
            rect.addScaleHandle([1, 1], [0, 0])
            rect.addScaleHandle([0, 0], [1, 1])
            rect.sigRegionChangeFinished.connect(
                lambda roi, idx=i: self._on_patch_roi_changed(idx, roi)
            )
            rect.sigClicked.connect(lambda _roi, _ev, idx=i: self._select_patch_artist(idx))
            lbl = pg.TextItem(f"P{i+1}", color=color, anchor=(0, 1))
            lbl.setPos(cmin, rmin)
            lbl.setZValue(21)
            self.vb.addItem(rect)
            self.vb.addItem(lbl)
            self._patch_artists.append((rect, lbl))

    def clear_patches(self):
        for rect, lbl in self._patch_artists:
            self.vb.removeItem(rect)
            self.vb.removeItem(lbl)
        self._patch_artists.clear()
        self._patches.clear()
        self._selected_patch_idx = -1
        for roi in self._rois:
            roi["patch_indices"] = []
        self._update_info()
        self.patches_changed.emit([])

    def _patch_coords(self):
        """Return list of (y0,y1,x0,x1) — compatible with existing MainWindow code."""
        return [p["coords"] for p in self._patches]

    # ── Info label ────────────────────────────────────────────────────

    def _update_info(self):
        lines = []
        if self._rois:
            for roi in self._rois:
                c     = roi.get('color', '#aaa')
                n_p   = len(roi.get('patch_indices', []))
                bb    = roi.get('bbox_fullres', [0,0,0,0])
                lines.append(
                    f"<span style='color:{c}'>"
                    f"▶ {roi['name']}  "
                    f"{len(roi['polygon_display'])} pts  "
                    f"{bb[1]-bb[0]}×{bb[3]-bb[2]}px  "
                    f"patches:{n_p}/4"
                    f"</span>"
                )
        if self._patches:
            lines.append("<span style='color:#bbb'>Patches: "
                         + "  ".join(
                             f"<span style='color:{PATCH_COLORS[i%len(PATCH_COLORS)]}'>"
                             f"P{i+1}</span>"
                             for i in range(len(self._patches))
                         ) + "</span>")
        if not lines:
            self._info_lbl.setText("")
        else:
            self._info_lbl.setText("<br>".join(lines))

    # ── Public API ────────────────────────────────────────────────────

    def get_patches(self):
        """Return list of (y0,y1,x0,x1) coords — backward compatible."""
        return self._patch_coords()

    def get_rois(self):
        return list(self._rois)

    def set_rois_and_patches(self, rois, patches, full_wsi_mode=False):
        """Replace ROI/patch model and redraw using the Step0 patch machinery."""
        self.full_wsi_mode = bool(full_wsi_mode)
        for arts in self._roi_artists:
            for a in arts:
                self.vb.removeItem(a)
        self._roi_artists.clear()

        self._rois = []
        for idx, src in enumerate(rois or []):
            roi = dict(src or {})
            roi["patch_indices"] = []
            roi.setdefault("color", ROI_COLORS[idx % len(ROI_COLORS)])
            self._rois.append(roi)

            pts = roi.get("polygon_display") or []
            if not pts and roi.get("polygon_fullres"):
                pts = [
                    (int(x) / float(self.ds), int(y) / float(self.ds))
                    for x, y in roi.get("polygon_fullres") or []
                ]
                roi["polygon_display"] = pts
            if pts:
                pts_c = list(pts) + [pts[0]]
                color = roi.get("color", ROI_COLORS[idx % len(ROI_COLORS)])
                poly_item = pg.PlotDataItem(
                    [p[0] for p in pts_c], [p[1] for p in pts_c],
                    pen=pg.mkPen(color, width=2),
                    fillLevel=0,
                    brush=pg.mkBrush(color + "33"),
                )
                cx = np.mean([p[0] for p in pts])
                cy = np.mean([p[1] for p in pts])
                lbl_item = pg.TextItem(
                    roi.get("name", f"ROI_{idx + 1}"),
                    color=color,
                    anchor=(0.5, 0.5),
                )
                lbl_item.setPos(cx, cy)
                self.vb.addItem(poly_item)
                self.vb.addItem(lbl_item)
                self._roi_artists.append([poly_item, lbl_item])

        self._patches = []
        for patch in patches or []:
            y0, y1, x0, x1 = [int(v) for v in patch]
            cy = ((y0 + y1) / 2.0) / float(self.ds)
            cx = ((x0 + x1) / 2.0) / float(self.ds)
            roi_idx = None
            if self._rois and not self.full_wsi_mode:
                roi_idx = self._find_roi_for_patch(cy, cx)
            self._patches.append({
                "roi_idx": roi_idx,
                "coords": (y0, y1, x0, x1),
            })
        for i, patch in enumerate(self._patches):
            ri = patch.get("roi_idx")
            if ri is not None and 0 <= ri < len(self._rois):
                self._rois[ri].setdefault("patch_indices", []).append(i)

        if self._selected_patch_idx >= len(self._patches):
            self._selected_patch_idx = len(self._patches) - 1
        self._rebuild_patch_artists()
        self._update_info()

    def set_background_crop(self, arr, y0, y1, x0, x1, downsample):
        """Show a pre-read crop without changing Step0's global scene coords."""
        if not hasattr(self, "_step1_bg_item"):
            self._step1_bg_item = pg.ImageItem()
            self.vb.addItem(self._step1_bg_item)
            self._step1_bg_item.setZValue(-10)
        self._step1_bg_item.setImage(arr, autoLevels=False)
        ds = max(1, int(downsample or self.ds))
        self.ds = ds
        self.full_h = max(1, int(y1 - y0))
        self.full_w = max(1, int(x1 - x0))
        self._step1_bg_item.setRect(QRectF(0, 0, max(1, (x1 - x0) / ds), max(1, (y1 - y0) / ds)))
        self.ov_h = max(1, int(np.ceil(self.full_h / float(self.ds))))
        self.ov_w = max(1, int(np.ceil(self.full_w / float(self.ds))))
        self.vb.setRange(
            QRectF(0, 0, max(1, (x1 - x0) / ds), max(1, (y1 - y0) / ds)),
            padding=0.02,
        )

    def add_center_patch(self, roi=None, size_px=512):
        """Add a centered patch through the same Step0 add/rebuild path."""
        if roi and roi.get("bbox_fullres"):
            y0, y1, x0, x1 = [int(v) for v in roi["bbox_fullres"]]
        else:
            y0, y1, x0, x1 = 0, int(self.full_h), 0, int(self.full_w)
        if y1 <= y0 or x1 <= x0:
            return
        size = int(min(size_px, max(64, y1 - y0), max(64, x1 - x0)))
        cy, cx = (y0 + y1) // 2, (x0 + x1) // 2
        fy0 = max(y0, cy - size // 2)
        fy1 = min(y1, fy0 + size)
        fx0 = max(x0, cx - size // 2)
        fx1 = min(x1, fx0 + size)
        fy0 = max(y0, fy1 - size)
        fx0 = max(x0, fx1 - size)
        roi_idx = None
        if self._rois and not getattr(self, "full_wsi_mode", False):
            roi_idx = self._find_roi_for_patch(
                ((fy0 + fy1) / 2.0) / float(self.ds),
                ((fx0 + fx1) / 2.0) / float(self.ds),
            )
        self._add_patch(
            fy0, fy1, fx0, fx1,
            fy0 // self.ds, max(1, fy1 // self.ds),
            fx0 // self.ds, max(1, fx1 // self.ds),
            roi_idx,
        )
        self._selected_patch_idx = len(self._patches) - 1
        self._rebuild_patch_artists()

    def delete_selected_or_last_patch(self):
        idx = self._selected_patch_idx
        if idx < 0:
            idx = len(self._patches) - 1
        self._remove_patch(idx)

    def select_patch(self, patch_idx):
        if 0 <= patch_idx < len(self._patches):
            self._select_patch_artist(patch_idx)

    # ── Event filter ─────────────────────────────────────────────────

    def eventFilter(self, obj, event):
        if obj is not self.gview.viewport():
            return super().eventFilter(obj, event)

        t = event.type()

        # ── Key press (ROI mode) ──────────────────────────────────────
        if t == QtCore.QEvent.KeyPress and self._mode == 'roi':
            key = event.key()
            if key == Qt.Key_Z:
                self._undo_vertex(); return True
            elif key == Qt.Key_D:
                self._delete_last_roi(); return True
            elif key in (Qt.Key_Return, Qt.Key_Enter):
                self._finish_roi(); return True

        if t == QtCore.QEvent.KeyPress and self._mode == 'patch':
            key = event.key()
            if key in (Qt.Key_Delete, Qt.Key_Backspace, Qt.Key_D):
                idx = self._selected_patch_idx
                if idx < 0:
                    idx = len(self._patches) - 1
                self._remove_patch(idx)
                return True

        # ── Scroll zoom ───────────────────────────────────────────────
        if t == QtCore.QEvent.Wheel:
            delta  = event.angleDelta().y()
            factor = 1.15 ** (delta / 120.0)
            sp = self.gview.mapToScene(event.pos())
            ip = self.img_item.mapFromScene(sp)
            cx, cy = ip.x(), ip.y()
            vr = self.vb.viewRange()
            self.vb.disableAutoRange()
            self.vb.setRange(
                xRange=[cx + (vr[0][0]-cx)/factor, cx + (vr[0][1]-cx)/factor],
                yRange=[cy + (vr[1][0]-cy)/factor, cy + (vr[1][1]-cy)/factor],
                padding=0,
            )
            return True

        # ── Mouse press ───────────────────────────────────────────────
        elif t == QtCore.QEvent.MouseButtonPress:
            sp = self.gview.mapToScene(event.pos())
            r, c = self._ov_pos(sp)

            if self._mode == 'roi':
                if event.button() == Qt.LeftButton:
                    self._cur_pts.append((c, r))
                    self._redraw_cur_polygon()
                    self.status.setText(
                        f"Vertex added ({len(self._cur_pts)} pts) "
                        f"— Enter or right-click to close"
                    )
                    return True
                elif event.button() == Qt.RightButton:
                    if len(self._cur_pts) >= 3:
                        self._finish_roi()
                    return True
                elif event.button() == Qt.MiddleButton:
                    self._pan_last = event.pos()
                    return True

            else:  # patch mode
                if event.button() == Qt.LeftButton:
                    self._drag_start = (r, c)
                    return True
                elif event.button() == Qt.RightButton:
                    self._right_press_pos = event.pos()
                    return True
                elif event.button() == Qt.MiddleButton:
                    self._pan_last = event.pos()
                    return True

        # ── Mouse move ────────────────────────────────────────────────
        elif t == QtCore.QEvent.MouseMove:
            sp = self.gview.mapToScene(event.pos())
            r, c = self._ov_pos(sp)

            if self._mode == 'roi':
                if self._cur_pts:
                    self._update_preview_line(r, c)
                # Middle-drag pan
                if event.buttons() & Qt.MiddleButton and self._pan_last:
                    self._do_pan(event)
                return True

            else:  # patch mode
                if (event.buttons() & Qt.LeftButton) and self._drag_start:
                    r0, c0 = self._drag_start
                    rmin, rmax = min(r0,r), max(r0,r)
                    cmin, cmax = min(c0,c), max(c0,c)
                    self._temp.setPos([cmin, rmin])
                    self._temp.setSize([max(1,cmax-cmin), max(1,rmax-rmin)])
                    self._temp.setVisible(True)
                    return True
                elif event.buttons() & Qt.MiddleButton:
                    if self._pan_last:
                        self._do_pan(event)
                    return True

        # ── Mouse release ─────────────────────────────────────────────
        elif t == QtCore.QEvent.MouseButtonRelease:
            if self._mode == 'roi':
                if event.button() == Qt.MiddleButton:
                    self._pan_last = None
                return True

            else:  # patch mode
                if event.button() == Qt.LeftButton:
                    self._temp.setVisible(False)
                    if self._drag_start is None:
                        return True
                    sp = self.gview.mapToScene(event.pos())
                    r, c = self._ov_pos(sp)
                    r0, c0 = self._drag_start
                    self._drag_start = None

                    rmin = max(0, min(r0, r))
                    rmax = min(getattr(self,'ov_h',1), max(r0, r))
                    cmin = max(0, min(c0, c))
                    cmax = min(getattr(self,'ov_w',1), max(c0, c))
                    if (rmax-rmin) < 3 or (cmax-cmin) < 3:
                        return True

                    # Centre of the patch in overview coords
                    cr = (rmin + rmax) // 2
                    cc = (cmin + cmax) // 2

                    # ROI constraint check
                    if self._rois and not getattr(self, "full_wsi_mode", False):
                        roi_idx = self._find_roi_for_patch(cr, cc)
                        if roi_idx is None:
                            self.status.setText(
                                "⚠ Patch centre is outside all ROIs — "
                                "draw the patch inside a ROI"
                            )
                            return True
                    else:
                        roi_idx = None

                    fy0 = int(rmin * self.ds)
                    fy1 = int(min(self.full_h, rmax * self.ds))
                    fx0 = int(cmin * self.ds)
                    fx1 = int(min(self.full_w, cmax * self.ds))
                    self._add_patch(
                        fy0, fy1, fx0, fx1,
                        rmin, rmax, cmin, cmax,
                        roi_idx,
                    )
                    return True

                elif event.button() == Qt.RightButton:
                    if self._right_press_pos is not None:
                        dp = event.pos() - self._right_press_pos
                        if abs(dp.x()) < 6 and abs(dp.y()) < 6:
                            self._remove_last_patch()
                    self._right_press_pos = None
                    self._pan_last = None
                    return True

                elif event.button() == Qt.MiddleButton:
                    self._pan_last = None
                    return True

        return False

    # ── Pan helper ────────────────────────────────────────────────────

    def _do_pan(self, event):
        dp  = event.pos() - self._pan_last
        self._pan_last = event.pos()
        vr  = self.vb.viewRange()
        vpw = max(1, self.gview.viewport().width())
        vph = max(1, self.gview.viewport().height())
        dx  = -dp.x() * (vr[0][1]-vr[0][0]) / vpw
        dy  = -dp.y() * (vr[1][1]-vr[1][0]) / vph
        self.vb.disableAutoRange()
        self.vb.setRange(
            xRange=[vr[0][0]+dx, vr[0][1]+dx],
            yRange=[vr[1][0]+dy, vr[1][1]+dy],
            padding=0,
        )



# ══════════════════════════════════════════════════════════════════════
#  Result Grid Panel
# ══════════════════════════════════════════════════════════════════════
