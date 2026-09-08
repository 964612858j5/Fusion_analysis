"""
block01/ui/step0/overview_panel.py — TileSelectDialog, FullFusionWorker, OverviewPanel.
"""

import os
import sys
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
    QGroupBox, QSizePolicy,
    QDialog, QTableWidget, QTableWidgetItem, QHeaderView,
    QMessageBox, QFileDialog,
)
import pyqtgraph as pg

from ...config import (
    OUTPUT_DIR, ROI_COLORS, PATCH_COLORS, OVERVIEW_DOWNSAMPLE,
)
from ...workers.cellpose_worker import OverviewLoaderThread
from ...core.fusion_engine import (
    FusionEngine, FUSION_FORMULA_VERSION, fuse_channels,
)
from ...core.channel_remap import apply_channel_remap

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
        # Per-channel manual remap params from Step0 Channel Remap ({ch: {min,
        # max,gamma,...}}). When present for a channel, its intensities are
        # conditioned with apply_channel_remap instead of the plain percentile
        # window — so the fused output reflects the user's manual adjustments.
        self._remap_params = dict(fusion_cfg.get("channel_remap_params") or {})
        self._unmapped_reported = set()

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

    def _channel_norm(self, ch, arr):
        """One channel as a [0,1] signal, through its COMMITTED window.

        The window is the one frozen at Save — a user's Min/Max/Gamma, or the
        automatic one computed from the whole slide. It is never derived from
        the tile in hand: a percentile taken per tile gave every tile its own
        scale, so the same data saved with a different tile grid came out
        different. A channel with no committed window returns None and takes no
        part, rather than being given a scale of its own.
        """
        params = self._remap_params.get(ch)
        if not params:
            if ch not in self._unmapped_reported:
                self._unmapped_reported.add(ch)
                print(f"[Fusion] {ch} has no committed display window; "
                      "it takes no part in this fusion")
            return None
        return apply_channel_remap(arr, params).astype(np.float32)

    def _fuse_tile(self, raw_cache, groups, group_weights,
                   nucleus_ch, nucleus_w):
        """Fuse a pre-loaded channel cache into (H,W,2) uint16.

        The arithmetic is `fuse_channels`, the same call the screen makes. This
        method owns the mapping and the quantisation around it and nothing
        else: no per-group, per-tile or global renormalisation, which is what
        used to cancel the group and nucleus weights on their way to disk.
        """
        wanted = {nucleus_ch} if nucleus_ch else set()
        for ch_weights in (groups or {}).values():
            wanted.update(ch_weights.keys())
        signals = {}
        for ch in wanted:
            arr = raw_cache.get(ch)
            if arr is None:
                continue
            mapped = self._channel_norm(ch, arr)
            if mapped is not None:
                signals[ch] = mapped

        cyto, nucleus = fuse_channels(signals, groups, group_weights,
                                      nucleus_ch, nucleus_w)
        if cyto is None:
            shape = next(iter(raw_cache.values())).shape
            cyto = np.zeros(shape, dtype=np.float32)
            nucleus = np.zeros(shape, dtype=np.float32)

        result = np.stack([
            (cyto    * 65535).astype(np.uint16),
            (nucleus * 65535).astype(np.uint16),
        ], axis=-1)
        del cyto, nucleus, signals
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
                # Self-describing: a consumer can tell which arithmetic made
                # these pixels without consulting a sidecar file.
                out_zarr.attrs["fusion_formula_version"] = FUSION_FORMULA_VERSION

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
                        nucleus_ch, nucleus_w,
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


# ── Direct patch editing on the tissue thumbnail ──────────────────────
# The eight resize grips of the selected patch, as (dy, dx) directions:
# four corners and four edge midpoints. dy/dx of -1 moves the low edge,
# +1 the high edge, 0 leaves that axis alone.
PATCH_HANDLE_DIRS = (
    (-1, -1), (-1, 0), (-1, 1),
    (0, -1),           (0, 1),
    (1, -1),  (1, 0),  (1, 1),
)
PATCH_HANDLE_MIN_OV = 2.0    # grip half-width, overview px: still grabbable
PATCH_HANDLE_MAX_OV = 5.0    #                              never enormous
PATCH_MIN_FULL_PX   = 32     # a patch smaller than this is not a patch
PATCH_DRAG_SLOP_PX  = 3      # widget px before a press counts as a drag
# How near a patch's outline a click still counts as "on the border": the
# band that ENTERS the adjust state. Measured in screen pixels (it is a
# fingertip tolerance, not a distance on the slide) and converted to overview
# pixels at the current zoom, with a floor so it survives being zoomed out.
PATCH_BORDER_TOL_PX = 4.0
PATCH_BORDER_TOL_OV = 1.5


# ── the middle-drag pan's diagnostics and its one tolerance ──────────
#
# MID_PAN_BLANK_MOVE_TOLERANCE is NOT a delay and NOT a distance
# threshold: it does not decide WHEN a drag may start (a drag starts at
# its press and pans on its first move, always). It decides only how much
# missing evidence is allowed to accumulate before an open gesture is
# presumed abandoned -- how many consecutive moves that carry NO button
# information at all may pass, AFTER the platform has once reported the
# middle button down in this same gesture, before we conclude the release
# was never delivered. One such move is an anomaly; a run of them with a
# platform that has proven it can report the button is a lost release.
MID_PAN_BLANK_MOVE_TOLERANCE = 2

# The one diagnostic switch, default off. Set BLOCK01_MIDPAN_DEBUG=1 in the
# environment of a REAL desktop run to get one line per middle-button event
# on stderr; see `_mid_pan_log`. It is read per event rather than captured
# at import so it can be turned on for a single run without a code change,
# and so the tests can drive it.
MID_PAN_DEBUG_ENV = "BLOCK01_MIDPAN_DEBUG"


def _mid_pan_debug_enabled():
    return os.environ.get(MID_PAN_DEBUG_ENV, "") not in ("", "0", "false", "no")


def _mid_pan_button_name(buttons):
    try:
        buttons = int(buttons)
    except (TypeError, ValueError):
        return repr(buttons)
    if not buttons:
        return "NoButton"
    names = []
    for bit, name in ((int(Qt.LeftButton), "Left"),
                      (int(Qt.RightButton), "Right"),
                      (int(Qt.MiddleButton), "Middle")):
        if buttons & bit:
            names.append(name)
            buttons &= ~bit
    if buttons:
        names.append(hex(buttons))
    return "|".join(names)


def _mid_pan_widget_name(w):
    if w is None:
        return "None"
    try:
        return f"{type(w).__name__}@{hex(id(w))}"
    except Exception:                                       # noqa: BLE001
        return "<unreadable>"


def _view_range_moved(before, after, rel=1e-9):
    """Did the camera actually go somewhere between these two view ranges?

    Not `!=`. Re-setting an aspect-locked ViewBox to the range it is
    already in comes back differing in the last bit or two of a double
    (measured: 5.7e-14 view units on a 982-unit span, one part in 1e16),
    so an exact comparison calls a zero-delta wheel a zoom. The bound is
    relative to the span and enormously smaller than anything an eye or a
    tile request could tell apart: on a thousand-pixel view it is a
    millionth of a pixel.
    """
    for (b0, b1), (a0, a1) in zip(before, after):
        tol = abs(b1 - b0) * rel
        if abs(a0 - b0) > tol or abs(a1 - b1) > tol:
            return True
    return False


# ══════════════════════════════════════════════════════════════════════
#  Overview Panel  (ROI polygon + Patch rectangle dual-mode)
# ══════════════════════════════════════════════════════════════════════

class _MidPanReleaseWatch(QtCore.QObject):
    """The application-wide half of the middle-drag pan, alive only while a
    drag is.

    One job: see the RELEASE, wherever it is delivered. The viewport's own
    filter sees it whenever Qt's implicit grab holds, and that is the normal
    case -- but a grab that was refused, transferred, or broken by a popup
    sends the release to whatever is under the cursor instead, and a gesture
    that ends only on a release it never receives is a gesture that sticks.
    Because the drag no longer ends itself on the first move that fails to
    carry the button, this is what guarantees it ends at all.

    Installed on the application at the press and removed at the cancel, so
    it filters nothing at all when no middle button is down. Parented to the
    panel, so a panel that is torn down takes it with it and Qt removes a
    destroyed filter itself: there is no path on which it outlives the
    thing it speaks for.
    """

    def __init__(self, panel):
        super().__init__(panel)
        self._panel = panel

    def eventFilter(self, obj, event):
        panel = self._panel
        try:
            t = event.type()
        except RuntimeError:                                # noqa: BLE001
            return False
        try:
            if t == QtCore.QEvent.MouseButtonRelease:
                if event.button() == Qt.MiddleButton:
                    panel._mid_pan_log("app-release", event, obj=obj)
                    panel._middle_pan_cancel(
                        "the release, caught by the application filter")
            elif t in (QtCore.QEvent.MouseMove,
                       QtCore.QEvent.MouseButtonPress):
                panel._mid_pan_check_viewport()
        except RuntimeError:                                # noqa: BLE001
            pass
        # Never consumed: this filter watches, it does not answer.
        return False


class _PanViewBox(pg.ViewBox):
    """The Tissue Preview's ViewBox.

    It owns the camera and nothing else. The thumbnail's LEFT button is
    spoken for in every mode -- it draws rectangles, adds polygon vertices,
    navigates, and edits the selected rectangle -- so this box keeps
    `setMouseEnabled(False, False)`: pyqtgraph must not pan or zoom it
    behind the panel's back.

    The middle-button pan is NOT here, and the reason is measured. A drag
    only reaches `ViewBox.mouseDragEvent` if pyqtgraph's `GraphicsScene`
    decides to build a `MouseDragEvent` for it, which depends on which item
    accepted the press and on click/drag bookkeeping that is written around
    the left button. Logging the real hierarchy -- the assembled page's own
    thumbnail and the Tissue Navigator popup's -- with a four-move middle
    drag delivered to the WINDOW showed:

        gview.viewport()   press=1  move=4  release=1
        window / gview     press=1  move=0  release=0
        scene              press=0  move=0  release=0
        ViewBox.mouseDragEvent(middle) reached: 2 times

    Two things follow. The viewport is the ONLY object that sees the whole
    gesture -- once the press lands, Qt's implicit grab sends every move and
    the release straight to it, so a release outside the widget cannot be
    lost. And the scene's drag dispatch DROPPED half the moves: four moves
    produced two translations, which is the stutter, and on the user's
    machine it produced none at all for the popup. So the gesture is owned
    at the viewport, in `OverviewPanel.eventFilter`, where the events
    provably are; see `_middle_pan_*` there.
    """


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
    patch_selection_changed(int) — which patch is highlighted (-1 = none)

    There is no limit on how many patches an ROI may hold, and a patch is
    editable where it is drawn. Clicking its BORDER or its LABEL (in any
    mode) puts it under adjustment: dragging its body moves it, dragging a
    grip resizes it, Delete removes it, and a click anywhere else ends the
    adjustment and does nothing else -- see `_patch_edge_hit_test`,
    `_selected_patch_hit_test` and `_commit_patch_geometry`.
    """

    patches_changed = pyqtSignal(list)   # [(y0,y1,x0,x1), ...]
    rois_changed    = pyqtSignal(list)   # [roi_dict, ...]
    # A plain click (press + release without a drag) in patch mode, or a
    # Ctrl+click in ROI mode: "take me there". (y, x) in FULL-IMAGE pixels.
    # Emitted only; what "there" means is the host's business (Step 0 jumps
    # the full image to it). Neither gesture had a meaning before: a
    # drag shorter than 3 overview pixels was discarded as too small for a
    # patch, and Ctrl+click added no vertex.
    navigate_requested = pyqtSignal(int, int)
    # The patch the thumbnail highlights changed (index, or -1 for none).
    # Selection is a VIEW state, not part of the patch model, so it travels
    # on its own signal and never disturbs `patches_changed`.
    patch_selection_changed = pyqtSignal(int)

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

        # ── Edit policy ───────────────────────────────────────────────
        # Which edits this panel accepts AT ALL.  The gate sits on the actions
        # themselves, not on button styling, so a blocked edit never reaches
        # the model and never has to be undone afterwards.
        self._roi_create_allowed = True
        self._roi_delete_allowed = True
        self._patch_edit_allowed = True

        # ── Drawing state ─────────────────────────────────────────────
        self._mode            = 'patch'
        self._drag_start      = None
        self._nav_press       = None   # navigate/pan mode: (pos, r, c) of the press
        self._nav_moved       = False
        # An in-flight move/resize of the selected patch (mode None only):
        # {"idx", "handle", "start" (row,col in overview px), "press" (widget
        #  pos), "coords" (the patch rect the gesture started from), "moved"}.
        self._patch_drag      = None
        # Set when a press LEFT the adjust state: that whole gesture is
        # swallowed, so letting go does not also navigate or draw.
        self._adjust_swallow  = False
        self._right_press_pos = None

        # ROI in-progress drawing
        self._cur_pts     = []
        self._cur_line    = None
        self._cur_preview = None

        # pyqtgraph items parallel to _rois / _patches
        self._roi_artists   = []
        self._patch_artists = []

        # v14.2c current-view rectangle overlay (NOT an ROI; never stored in
        # _rois). A single non-interactive outline showing the main viewer's
        # current viewport, in full-image pixels.
        self._current_view_item = None
        self._current_view_full = None   # (y0, y1, x0, x1) full-image px, or None

        # The thumbnail's pixels. `_overview_arr` is the DAPI overview this
        # panel loads for itself; `_channel_rgb` is an RGB image a HOST has
        # pushed in (Step0 pushes the channel the user is working on, in its
        # own colour). The host's image WINS whenever there is one, and both
        # are kept, so an overview landing after a push does not silently
        # replace the picture the page put there -- the two arrive in either
        # order and the load is asynchronous.
        self._overview_arr = None
        self._channel_rgb = None
        self._thumb_fitted = None    # the (w, h) the view was last fitted to
        # Whether the USER has moved this panel's camera on the CURRENT
        # dataset. One bit, per panel -- the page's thumbnail and the
        # Tissue Navigator popup's are two instances of this class and each
        # answers for its own camera.
        #
        # It is the answer to "whose camera is this", and everything that
        # arrives after the first picture reads it: the real overview
        # landing, a channel picture pushed in, a patch drawn or moved or
        # deleted, the page/popup model sync. None of those is a request to
        # go anywhere, so none of them may re-fit a view the user placed by
        # hand. See `_apply_thumbnail`.
        #
        # Set ONLY where the camera measurably CHANGED -- a wheel zoom that
        # altered the range, a middle-drag step that translated by a
        # non-zero amount -- and never merely because a button went down: a
        # stray click on a half-loaded thumbnail must not cancel that
        # slide's one automatic fit. Cleared in `_load_overview` (a new
        # dataset is the panel's camera again) and by the double-click
        # reset (the user handing it back).
        self._thumbnail_camera_touched = False
        self._mode_hint = ("", "")   # the hint the current mode wants shown

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
        self.vb = _PanViewBox()
        self.gview.addItem(self.vb, row=0, col=0)
        self.vb.setAspectLocked(True)
        self.vb.invertY(True)
        self.vb.setMouseEnabled(x=False, y=False)
        self.vb.setMenuEnabled(False)
        self.img_item = pg.ImageItem()
        self.vb.addItem(self.img_item)

        # The middle-drag pan's whole state.
        #
        # `_mid_pan_last` is the last point the gesture was seen at, in
        # SCENE coordinates, or None when no middle press is being held.
        # Scene and not view coordinates -- see `_middle_pan_move`, which
        # is where that distinction is the whole correctness of the
        # gesture.
        #
        # `_mid_pan_grab` is the widget this panel called `grabMouse()` on,
        # or None. The two are set and cleared together, and the grab is
        # what makes the gesture reliable: see `_middle_pan_press`.
        #
        # `_mid_pan_confirmed` remembers that the platform has, at least
        # once during THIS gesture, reported the middle button down in a
        # move. Until it has, a move carrying no buttons at all says
        # nothing -- see `_middle_pan_move`.
        #
        # `_mid_pan_blank` counts consecutive such uninformative moves.
        #
        # `_mid_pan_watch` is the application-wide filter that catches the
        # release wherever it lands; installed for the gesture's lifetime
        # only.
        self._mid_pan_last = None
        self._mid_pan_grab = None
        self._mid_pan_confirmed = False
        self._mid_pan_blank = 0
        self._mid_pan_watch = None
        self._mid_pan_seq = 0
        self._mid_pan_t0 = None

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

    # ── Overview loading ──────────────────────────────────────────────

    def _load_overview(self):
        if self.loader is None or self.full_h == 0:
            self.status.setText("Please select an OME-TIFF and click Load.")
            return
        self.status.setText("Loading overview, please wait...")
        # A load is a new slide: whatever is on screen is about to be
        # replaced, and the view is re-fitted to what arrives. The camera
        # goes back to the panel with it -- a zoom the user made into the
        # PREVIOUS slide is not a place on this one, and inheriting the
        # ownership would leave the new slide's first picture unfitted.
        self._thumb_fitted = None
        self._thumbnail_camera_touched = False
        self._t0 = time.time()
        self._ov_thread = OverviewLoaderThread(
            self.loader, self.nuc_ch, self.ds
        )
        self._ov_thread.done.connect(self._on_overview_loaded)
        self._ov_thread.error.connect(
            lambda e: self.status.setText(f"Overview load failed: {e}")
        )
        self._ov_thread.start()

    def set_channel_image(self, rgb):
        """Show `rgb` -- an (H, W, 3) uint8 image of the WHOLE slide -- as the
        thumbnail, in place of the DAPI overview.

        The host renders it (Step0: the current channel through its display
        mapping and colour, with DAPI composited additively when its layer is
        on), because the host is the only thing that knows what "the current
        channel" and "its colour" mean. This panel only has to draw it.

        `rgb` need not have the overview's own shape -- Step0's whole-slide
        arrays come off a different pyramid level -- so it is stretched onto
        the overview's rectangle rather than replacing it. Everything else
        here (ROIs, patches, the viewport rectangle, click-to-navigate)
        works in overview pixels, and none of it moves.

        `None` gives the DAPI overview back.
        """
        self._channel_rgb = None if rgb is None else np.asarray(rgb)
        self._apply_thumbnail()

    def _thumb_rect(self):
        """The overview's own rectangle, in overview pixels.

        Derived from the slide and the downsample when no overview has
        landed yet: the load is asynchronous and a host may push a channel
        image first, and every coordinate on this panel is already defined
        by `ds` rather than by the array that happens to be on screen.
        """
        if not hasattr(self, "ov_h"):
            if not (self.full_h and self.full_w and self.ds):
                return None
            self.ov_h = max(1, int(round(self.full_h / float(self.ds))))
            self.ov_w = max(1, int(round(self.full_w / float(self.ds))))
        return QRectF(0, 0, self.ov_w, self.ov_h)

    def _apply_thumbnail(self):
        """Draw whichever thumbnail is current, and fit the view once."""
        rect = self._thumb_rect()
        if rect is None:
            return
        rgb = self._channel_rgb
        if rgb is not None:
            self.img_item.setImage(rgb, autoLevels=False)
            self.img_item.setRect(rect)
        elif self._overview_arr is not None:
            self.img_item.setImage(self._overview_arr, autoLevels=True)
            self.img_item.setRect(rect)
        else:
            return
        # Fit ONCE per overview geometry, and only while the camera is
        # still the PANEL's.
        #
        # "Once per geometry" alone was not enough, and the gap is one
        # pixel of arithmetic. Before an overview lands, `_thumb_rect`
        # ESTIMATES the geometry as `round(full / ds)` so that a
        # host-pushed channel picture has a rectangle to be stretched onto;
        # the real overview is `read_region(..., ds)`, i.e. `arr[::ds,
        # ::ds]`, whose shape is `ceil(full / ds)`. On a slide whose height
        # or width is not an exact multiple of the downsample those differ,
        # so `_thumb_fitted` "changed" and the late overview re-fitted the
        # whole slide -- over the zoom or pan the user had made in the
        # meantime, while the status line still said "Loading". That is the
        # reported "the thumbnail is there but the wheel does nothing": the
        # wheel did work, and was undone a moment later.
        #
        # The geometry is recorded either way, so the two pieces of state
        # never contradict each other: after this call `_thumb_fitted`
        # means "the panel has seen this geometry", and
        # `_thumbnail_camera_touched` alone decides whose the camera is. A
        # double-click reset clears the bit, which is what earns a later
        # shape correction its one exact fit.
        if self._thumb_fitted != (self.ov_w, self.ov_h):
            self._thumb_fitted = (self.ov_w, self.ov_h)
            if not self._thumbnail_camera_touched:
                self.vb.setRange(rect, padding=0.01)

    def _on_overview_loaded(self, arr):
        self.ov_h, self.ov_w = arr.shape
        self._overview_arr = arr
        self._apply_thumbnail()
        self.status.setText(
            f"Full image {self.full_h}×{self.full_w} px  |  "
            f"Overview {self.ov_h}×{self.ov_w} px  "
            f"({time.time()-self._t0:.1f}s)"
        )

    # ── Coordinate helpers ────────────────────────────────────────────

    def _ov_pos(self, scene_pos):
        if not hasattr(self, 'ov_h'):
            return 0, 0
        # Through the VIEW, not through the image item: the item now
        # carries a rect (a host-pushed channel image is a different shape
        # from the overview and is stretched onto the overview's rectangle),
        # so its local coordinates are its own pixels rather than overview
        # ones. The view's coordinates ARE overview pixels, always -- which
        # is what every other coordinate here is in.
        p = self.vb.mapSceneToView(scene_pos)
        r = int(np.clip(p.y(), 0, self.ov_h - 1))
        c = int(np.clip(p.x(), 0, self.ov_w - 1))
        return r, c   # (row, col)

    def _ov_pos_f(self, scene_pos):
        """Like `_ov_pos`, but WITHOUT rounding to whole overview pixels.

        A drag has to be measured at better than one overview pixel (one
        overview pixel is `self.ds` = 32 full-image pixels), so the patch
        editing maths works in these floats and rounds only once, at the
        end, when it writes level-0 integers back into the model.
        """
        if not hasattr(self, 'ov_h'):
            return 0.0, 0.0
        p = self.vb.mapSceneToView(scene_pos)
        return float(p.y()), float(p.x())

    def _to_fullres(self, r, c):
        """Overview (row,col) → full-res (y, x)."""
        return int(r * self.ds), int(c * self.ds)

    # ── Mode switching ────────────────────────────────────────────────

    def set_edit_policy(self, *, roi_create=None, roi_delete=None,
                        patch_edit=None):
        """Narrow the edits this panel accepts.  Only the given flags change.

        The panel is shared between steps, so the policy is a property of the
        step that is showing it, not of the panel.  Nothing here duplicates an
        editing implementation: the existing actions simply refuse.
        """
        if roi_create is not None:
            self._roi_create_allowed = bool(roi_create)
        if roi_delete is not None:
            self._roi_delete_allowed = bool(roi_delete)
        if patch_edit is not None:
            self._patch_edit_allowed = bool(patch_edit)
        # A tool that is no longer allowed cannot stay armed: an ROI polygon
        # half drawn under the old policy is dropped rather than left to be
        # closed by a keystroke.
        if not self._roi_create_allowed and self._mode == 'roi':
            self._cur_pts.clear()
            self._redraw_cur_polygon()
            self._set_mode('patch' if self._patch_edit_allowed else None)

    def edit_policy(self):
        return {"roi_create": self._roi_create_allowed,
                "roi_delete": self._roi_delete_allowed,
                "patch_edit": self._patch_edit_allowed}

    def _set_mode(self, mode):
        """'roi', 'patch', or None -- None is navigate/pan: no drawing tool,
        a click emits `navigate_requested`, a left-drag pans."""
        if mode == 'roi' and not self._roi_create_allowed:
            self.status.setText(
                "Drawing a new ROI is not available in this step.")
            return
        if mode == 'patch' and not self._patch_edit_allowed:
            self.status.setText("Patch editing is not available in this step.")
            return
        self._mode = mode
        # _btn_roi / _btn_patch 已从 OverviewPanel 移除，
        # 模式切换状态由 Step0Page 工具栏按钮负责，这里只更新内部状态和UI
        self._roi_ctrl.setVisible(mode == 'roi')
        self._patch_ctrl.setVisible(mode == 'patch')
        if mode == 'roi':
            self._mode_hint = (
                "Left-click = Add vertex (inside an existing ROI: jump there)  |  "
                "Enter/Right-click = Close ROI  |  Z = Undo  |  D = Delete  "
                "|  Scroll = Zoom  |  Middle-drag = Pan  |  Double-click = Reset",
                "color:#6bcb77;font-size:10px;")
        elif mode is None:
            self._mode_hint = (
                "Click = Jump the full image there  |  Click a patch's edge or "
                "label = Adjust it  |  Scroll = Zoom  "
                "|  Middle-drag = Pan  |  Double-click = Reset",
                "color:#19e0e0;font-size:10px;")
        else:
            self._mode_hint = (
                "Left-drag = Add Patch  |  Right-click = Delete last  |  Click a "
                "patch's edge or label = Adjust it  "
                "|  Scroll = Zoom  |  Right-drag = Pan  |  Double-click = Reset",
                "color:#777;font-size:10px;")
        self._refresh_hint()
        # Abort any in-progress polygon when switching away
        if mode != 'roi' and self._cur_pts:
            self._cur_pts.clear()
            self._redraw_cur_polygon()

    def _on_overview_click(self, event):
        """Reset view to full image on double-click.

        This is the user handing the camera BACK: the whole thumbnail is
        fitted now, and the panel owns it again, so the state left behind
        is exactly the state a slide that had never been touched would be
        in -- fitted to the geometry that is on screen, not touched. A
        later shape correction (the real overview landing after an
        estimate) therefore still gets its one exact fit, and nothing else
        moves the view until the user does.
        """
        if event.double():
            if hasattr(self, 'ov_h') and hasattr(self, 'ov_w'):
                self.vb.setRange(
                    QRectF(0, 0, self.ov_w, self.ov_h), padding=0.01
                )
                self._thumbnail_camera_touched = False
                self._thumb_fitted = (self.ov_w, self.ov_h)

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
        if not self._roi_create_allowed:
            self.status.setText(
                "Drawing a new ROI is not available in this step.")
            return
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
        if not self._roi_delete_allowed:
            self.status.setText("Deleting an ROI is not available in this step.")
            return
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
        if not self._roi_delete_allowed:
            self.status.setText("Deleting an ROI is not available in this step.")
            return
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

    def _patches_in_roi(self, roi_idx):
        return sum(1 for p in self._patches if p["roi_idx"] == roi_idx)

    def _add_patch(self, fy0, fy1, fx0, fx1, rmin, rmax, cmin, cmax, roi_idx):
        """Add a new patch, draw its visual, update info.

        There is no cap on how many patches an ROI (or the bare canvas) may
        hold: how many places on the slide are worth measuring is the user's
        judgement, not this panel's.
        """
        if not self._patch_edit_allowed:
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

    def add_patch_rect(self, fy0, fy1, fx0, fx1, roi_idx=None):
        """Add a patch from a FULL-RESOLUTION rectangle, with no drawing.

        The public form of `_add_patch` for callers that already know the
        level-0 rectangle they want -- Step 0's "Save as patch" on a compare
        snapshot -- rather than a drag on this canvas. It goes through the
        same list, the same artists and the same `patches_changed` signal,
        so a patch made this way is indistinguishable from a drawn one
        everywhere downstream.

        """
        coords = (int(fy0), int(fy1), int(fx0), int(fx1))
        self._patches.append({"roi_idx": roi_idx, "coords": coords})
        if roi_idx is not None and 0 <= roi_idx < len(self._rois):
            self._rois[roi_idx]["patch_indices"].append(len(self._patches) - 1)
        self._rebuild_patch_artists()
        self._update_info()
        self.patches_changed.emit(self._patch_coords())
        return coords

    def _remove_last_patch(self):
        if not self._patch_edit_allowed:
            return
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
        if not self._patch_edit_allowed:
            return
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
        # Deleting the patch under adjustment ENDS the adjustment: there is
        # nothing left to drag, and silently promoting a neighbour into the
        # role would hand the next click to a rectangle nobody picked.
        was_adjusting = self._selected_patch_idx >= 0
        self._selected_patch_idx = -1
        self._rebuild_patch_artists()
        self._update_info()
        self._refresh_hint()
        if was_adjusting:
            self.patch_selection_changed.emit(-1)
        self.patches_changed.emit(self._patch_coords())

    def _select_patch_artist(self, patch_idx):
        """Highlight one patch (-1 = none) and tell the host about it."""
        if patch_idx < -1 or patch_idx >= len(self._patches):
            patch_idx = -1
        if patch_idx == self._selected_patch_idx:
            return
        self._selected_patch_idx = patch_idx
        self._rebuild_patch_artists()
        self._refresh_hint()
        self.patch_selection_changed.emit(patch_idx)

    # ── Direct patch editing on the thumbnail ─────────────────
    #
    # A patch is a rectangle on the tissue, so the honest way to move or
    # resize one is to drag it where it is drawn. The panel does its own
    # hit-testing rather than leaning on interactive pyqtgraph items: the
    # viewport event filter below swallows the mouse before the scene ever
    # sees it (it has to — it owns drawing, panning and navigation), so an
    # item that "handles clicks itself" would simply never be told.
    #
    # Selecting a patch is an explicit STATE, not a side effect of a click
    # that happened to land on tissue. A patch is picked up by clicking its
    # BORDER or its LABEL -- in any mode, because the drawing-mode buttons
    # stay pressed all day and the user should not have to disarm one to nudge
    # a rectangle. While that state is on, the thumbnail is an editor and
    # nothing else: drawing and navigation are suspended, whatever the mode
    # buttons say, until a click somewhere else ends it.

    def _patch_display_rect(self, patch_idx):
        """The patch's rectangle in overview (display) coordinates, as
        (left, top, width, height) floats."""
        fy0, fy1, fx0, fx1 = self._patches[patch_idx]["coords"]
        ds = float(self.ds or 1)
        return (fx0 / ds, fy0 / ds, (fx1 - fx0) / ds, (fy1 - fy0) / ds)

    def _patch_handle_size(self, w, h):
        """Half-width of a resize handle, in overview pixels: big enough to
        grab on a small patch, never big enough to swallow the patch."""
        return float(min(PATCH_HANDLE_MAX_OV,
                         max(PATCH_HANDLE_MIN_OV, min(w, h) * 0.18)))

    @staticmethod
    def _patch_handle_center(x, y, w, h, dy, dx):
        return (x + (dx + 1) * 0.5 * w, y + (dy + 1) * 0.5 * h)

    def _ov_per_screen_px(self):
        """Overview pixels per screen pixel at the current zoom."""
        try:
            vr = self.vb.viewRange()
            vpw = max(1, self.gview.viewport().width())
            return max(1e-6, abs(vr[0][1] - vr[0][0]) / float(vpw))
        except Exception:
            return 1.0

    def _patch_border_tol(self):
        """How far off a patch's outline a click still counts as ON it."""
        return max(PATCH_BORDER_TOL_OV,
                   PATCH_BORDER_TOL_PX * self._ov_per_screen_px())

    def _patch_label_rect(self, patch_idx):
        """The "P3" tag's box in overview coords, (x0, y0, x1, y1).

        The label is drawn with anchor (0, 1) at the patch's top-left corner,
        so it hangs ABOVE the rectangle; it ignores the view transform, hence
        the screen-to-overview conversion of its own bounding box.
        """
        x, y, w, h = self._patch_display_rect(patch_idx)
        s = self._ov_per_screen_px()
        lw, lh = 20.0 * s, 16.0 * s
        if 0 <= patch_idx < len(self._patch_artists):
            try:
                br = self._patch_artists[patch_idx][1].boundingRect()
                if br.width() > 0 and br.height() > 0:
                    lw, lh = br.width() * s, br.height() * s
            except Exception:
                pass
        return (x, y - lh, x + lw, y)

    def _patch_edge_hit_test(self, r, c):
        """Which patch's BORDER or LABEL is under (row, col)? -- or None.

        This is the gesture that ENTERS the adjust state, and it deliberately
        ignores the interior: the middle of a patch is still tissue, so a
        click there still draws or still navigates, exactly as the mode says.
        Only the outline and the tag are the patch's own furniture. Patches
        are tested from the top of the stack down.
        """
        tol = self._patch_border_tol()
        for i in range(len(self._patches) - 1, -1, -1):
            lx0, ly0, lx1, ly1 = self._patch_label_rect(i)
            if lx0 - tol <= c <= lx1 + tol and ly0 - tol <= r <= ly1 + tol:
                return i
            x, y, w, h = self._patch_display_rect(i)
            if not (x - tol <= c <= x + w + tol and y - tol <= r <= y + h + tol):
                continue
            if (abs(c - x) <= tol or abs(c - (x + w)) <= tol
                    or abs(r - y) <= tol or abs(r - (y + h)) <= tol):
                return i
        return None

    def _selected_patch_hit_test(self, r, c):
        """What of the patch under adjustment is at (row, col)?

        Returns (patch_idx, handle) -- handle is a (dy, dx) direction pair for
        a resize grip, None for the body -- or None for anywhere else, which
        is the click that ends the adjustment. The grips win over the body:
        they stick out past the outline.
        """
        sel = self._selected_patch_idx
        if not (0 <= sel < len(self._patches)):
            return None
        x, y, w, h = self._patch_display_rect(sel)
        hs = self._patch_handle_size(w, h)
        for dy, dx in PATCH_HANDLE_DIRS:
            hx, hy = self._patch_handle_center(x, y, w, h, dy, dx)
            if abs(c - hx) <= hs and abs(r - hy) <= hs:
                return sel, (dy, dx)
        if x <= c <= x + w and y <= r <= y + h:
            return sel, None
        lx0, ly0, lx1, ly1 = self._patch_label_rect(sel)
        if lx0 <= c <= lx1 and ly0 <= r <= ly1:
            return sel, None
        return None

    def _begin_patch_drag(self, idx, handle, r, c, press_pos):
        """Take hold of a patch: the press that starts a move or a resize."""
        if not self._patch_edit_allowed:
            return
        self._patch_drag = {
            "idx":    idx,
            "handle": handle,
            "start":  (r, c),
            "press":  press_pos,
            "coords": tuple(self._patches[idx]["coords"]),
            "moved":  False,
        }
        self._nav_press = None
        self._nav_moved = False
        self._drag_start = None

    def _patch_geometry_for_drag(self, drag, r, c):
        """The level-0 rect a move/resize gesture has reached, clamped to the
        slide and to a minimum size, rounded to whole level-0 pixels."""
        y0, y1, x0, x1 = drag["coords"]
        ds = float(self.ds or 1)
        dy = (r - drag["start"][0]) * ds
        dx = (c - drag["start"][1]) * ds
        m = PATCH_MIN_FULL_PX
        H, W = int(self.full_h), int(self.full_w)

        if drag["handle"] is None:            # move the whole rectangle
            dy = int(round(dy))
            dx = int(round(dx))
            dy = max(-y0, min(H - y1, dy))
            dx = max(-x0, min(W - x1, dx))
            return (y0 + dy, y1 + dy, x0 + dx, x1 + dx)

        hy, hx = drag["handle"]
        ny0, ny1, nx0, nx1 = y0, y1, x0, x1
        if hy < 0:
            ny0 = min(max(0, int(round(y0 + dy))), ny1 - m)
        elif hy > 0:
            ny1 = max(min(H, int(round(y1 + dy))), ny0 + m)
        if hx < 0:
            nx0 = min(max(0, int(round(x0 + dx))), nx1 - m)
        elif hx > 0:
            nx1 = max(min(W, int(round(x1 + dx))), nx0 + m)
        # A minimum size that would push an edge off the slide pulls the
        # OTHER edge in instead, so the rect never leaves the picture.
        if ny1 > H:
            ny1, ny0 = H, min(ny0, H - m)
        if nx1 > W:
            nx1, nx0 = W, min(nx0, W - m)
        return (max(0, ny0), ny1, max(0, nx0), nx1)

    def _commit_patch_geometry(self, patch_idx, coords, revert_to=None):
        """Write an edited rectangle back into the patch model.

        The single write-back path for every geometry edit (drag, resize and
        the pyqtgraph-ROI callback): it clamps, re-homes the patch in whatever
        ROI now contains its centre, relinks `patch_indices`, redraws and emits
        `patches_changed` exactly ONCE — the same signal a drawn patch emits,
        so the patch list, Step 1 and every other listener see no difference.
        Returns True when the edit was accepted.
        """
        if not self._patch_edit_allowed:
            return False
        if patch_idx < 0 or patch_idx >= len(self._patches):
            return False
        fy0, fy1, fx0, fx1 = [int(round(v)) for v in coords]
        fy0 = max(0, min(int(self.full_h), fy0))
        fy1 = max(0, min(int(self.full_h), fy1))
        fx0 = max(0, min(int(self.full_w), fx0))
        fx1 = max(0, min(int(self.full_w), fx1))
        if fy1 <= fy0 or fx1 <= fx0:
            if revert_to is not None:
                self._patches[patch_idx]["coords"] = tuple(revert_to)
                self._rebuild_patch_artists()
            return False
        ds = float(self.ds or 1)
        roi_idx = self._patches[patch_idx].get("roi_idx")
        if self._rois and not getattr(self, "full_wsi_mode", False):
            roi_idx = self._find_roi_for_patch(
                ((fy0 + fy1) / 2.0) / ds, ((fx0 + fx1) / 2.0) / ds)
            if roi_idx is None:
                self.status.setText("⚠ Patch centre is outside all ROIs")
                if revert_to is not None:
                    self._patches[patch_idx]["coords"] = tuple(revert_to)
                self._rebuild_patch_artists()
                return False
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
        self._rebuild_patch_artists()
        self._update_info()
        self.patches_changed.emit(self._patch_coords())
        return True

    def _preview_patch_geometry(self, patch_idx, coords):
        """Show where a gesture currently is, without telling anybody.

        Mid-drag the model carries the provisional rectangle so the redraw
        stays trivial; nothing is emitted until the button comes up.
        """
        if 0 <= patch_idx < len(self._patches):
            self._patches[patch_idx]["coords"] = tuple(int(v) for v in coords)
            self._rebuild_patch_artists()

    def _on_patch_roi_changed(self, patch_idx, rect):
        """A pyqtgraph RectROI reported new geometry (kept for hosts that put
        their own interactive items over this panel)."""
        if patch_idx < 0 or patch_idx >= len(self._patches):
            return
        pos = rect.pos()
        size = rect.size()
        c0 = max(0.0, float(pos.x()))
        r0 = max(0.0, float(pos.y()))
        c1 = min(float(getattr(self, "ov_w", 1)), c0 + max(1.0, float(size.x())))
        r1 = min(float(getattr(self, "ov_h", 1)), r0 + max(1.0, float(size.y())))
        self._commit_patch_geometry(
            patch_idx,
            (round(r0 * self.ds), round(r1 * self.ds),
             round(c0 * self.ds), round(c1 * self.ds)),
        )

    def _rebuild_patch_artists(self):
        """Remove all patch visuals and redraw from scratch (after any delete/renumber)."""
        for rect, lbl in self._patch_artists:
            self.vb.removeItem(rect)
            self.vb.removeItem(lbl)
        self._patch_artists.clear()

        for i, pd in enumerate(self._patches):
            x, y, w, h = self._patch_display_rect(i)
            color = PATCH_COLORS[i % len(PATCH_COLORS)]
            selected = (i == self._selected_patch_idx)
            # A plain rect item, not an interactive pyqtgraph ROI: every patch
            # but the selected one is pure decoration, and the selected one is
            # driven by this panel's own event filter (_selected_patch_hit_test).
            rect = QtWidgets.QGraphicsRectItem(QRectF(x, y, w, h))
            rect.setPen(pg.mkPen(color, width=3 if selected else 2))
            rect.setBrush(pg.mkBrush(color + ("33" if selected else "00")))
            rect.setZValue(20)
            rect.setAcceptedMouseButtons(Qt.NoButton)
            if selected:
                # The grips are CHILDREN of the outline, so they follow it and
                # are torn down with it — no second bookkeeping list to leak.
                hs = self._patch_handle_size(w, h)
                for dy, dx in PATCH_HANDLE_DIRS:
                    hx, hy = self._patch_handle_center(x, y, w, h, dy, dx)
                    grip = QtWidgets.QGraphicsRectItem(
                        QRectF(hx - hs, hy - hs, hs * 2, hs * 2), rect)
                    grip.setPen(pg.mkPen(color, width=1))
                    grip.setBrush(pg.mkBrush(color))
                    grip.setZValue(22)
                    grip.setAcceptedMouseButtons(Qt.NoButton)
            lbl = pg.TextItem(f"P{i+1}", color=color, anchor=(0, 1))
            lbl.setPos(x, y)
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

    def _refresh_hint(self):
        """The hint says what the NEXT click will do -- so while a patch is
        under adjustment it says that, and not what the mode button says."""
        if not hasattr(self, "hint"):
            return
        idx = self._selected_patch_idx
        if 0 <= idx < len(self._patches):
            self.hint.setText(
                f"Adjusting P{idx + 1} — drag = Move  |  handles = Resize  |  "
                f"Del = Remove  |  click elsewhere to finish"
            )
            self.hint.setStyleSheet("color:#ffd166;font-size:10px;")
            return
        text, style = getattr(self, "_mode_hint", ("", ""))
        self.hint.setText(text)
        self.hint.setStyleSheet(style)

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
                    f"patches:{n_p}"
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

    def _emit_navigate(self, r, c):
        """Overview (row, col) -> full-image (y, x), clamped to the slide,
        then `navigate_requested`."""
        ds = float(self.ds or 1)
        y = int(min(max(0, self.full_h - 1), max(0, r * ds)))
        x = int(min(max(0, self.full_w - 1), max(0, c * ds)))
        self.navigate_requested.emit(y, x)

    # ── v14.2c current-view rectangle overlay (not an ROI) ───────────────────
    def set_current_view_rect(self, full_rect):
        """Draw the main viewer's current viewport as a non-interactive outline.

        full_rect : (y0, y1, x0, x1) in FULL-IMAGE pixels (same convention as
        patch coords), or None to clear. Converted to overview display coords by
        dividing by self.ds — the same full-image->overview transform the patch/
        ROI artists use (cx = x/ds, cy = y/ds). This overlay is purely visual:
        it is never added to self._rois and does not interfere with ROI editing.
        """
        self.clear_current_view_rect()
        if full_rect is None:
            return
        y0, y1, x0, x1 = (float(v) for v in full_rect)
        ds = float(self.ds or 1)
        xs = [x0 / ds, x1 / ds, x1 / ds, x0 / ds, x0 / ds]
        ys = [y0 / ds, y0 / ds, y1 / ds, y1 / ds, y0 / ds]
        item = pg.PlotDataItem(
            xs, ys, pen=pg.mkPen("#19e0e0", width=2, style=Qt.DashLine))
        item.setZValue(50)            # above ROI/patch artists
        item.setAcceptedMouseButtons(Qt.NoButton)   # never grabs ROI-edit clicks
        self.vb.addItem(item)
        self._current_view_item = item
        self._current_view_full = (y0, y1, x0, x1)

    def clear_current_view_rect(self):
        if self._current_view_item is not None:
            try:
                self.vb.removeItem(self._current_view_item)
            except Exception:
                pass
            self._current_view_item = None
        self._current_view_full = None

    def current_view_rect(self):
        """Return the stored current-view rect (y0,y1,x0,x1) full-image px, or None."""
        return self._current_view_full

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

    def select_patch(self, patch_idx):
        """Enter the adjust state on `patch_idx` -- or leave it, with -1."""
        if -1 <= patch_idx < len(self._patches):
            self._select_patch_artist(patch_idx)

    def is_adjusting_patch(self):
        """True while a patch is selected, i.e. while the thumbnail is an
        editor for that rectangle and neither a drawing surface nor a map."""
        return self._selected_patch_idx >= 0

    # ── Event filter ─────────────────────────────────────────────────

    # ── the middle-drag pan ───────────────────────────────────────────
    #
    # Three short handlers rather than one, so that "a press starts it, each
    # move pans once, the release ends it" is a property of the code and not
    # of a state machine that has to be read to be believed.

    def _middle_pan_press(self, event):
        """Open the gesture. From here until the release this viewport owns
        the pointer, and no other branch of the filter runs for it.

        The gesture is OPEN from the moment this anchor is recorded, and
        from nothing else. `_mid_pan_last` is set BEFORE the grab is
        attempted and is not conditional on it, so a press whose grab is
        refused -- the offscreen platform says so out loud, and a
        real X server can refuse or transfer one at any moment -- still
        starts a drag that the moves and the release complete. That
        ordering is the whole fix for "the middle button only pans after I
        left-click first": the gesture now depends on this viewport's own
        press, not on a process-wide singleton some popup owns.

        `grabMouse()` is then an ENHANCEMENT, taken best-effort: it routes
        moves and the release here even after the cursor has left the
        window, which is what a drag that runs off the edge of a small
        thumbnail needs. Failure to take it costs that and nothing else.

        The grabbed widget is REMEMBERED rather than re-derived at release
        time: `gview.viewport()` can be replaced under us (Qt does it when
        a viewport is swapped), and releasing a different widget than the
        one that was grabbed would leave the grab standing forever, which
        is a frozen application. Anything that ends the gesture goes
        through `_middle_pan_cancel`, which releases exactly what it took.
        """
        # Defensive: a press arriving while a previous gesture is somehow
        # still held must not leak that grab.
        self._middle_pan_cancel("a new press arrived on an open gesture")
        viewport = self._mid_pan_viewport()
        self._mid_pan_last = self._mid_pan_scene_pos(event)
        self._mid_pan_confirmed = False
        self._mid_pan_blank = 0
        self._mid_pan_seq += 1
        self._mid_pan_t0 = time.monotonic()
        if viewport is not None:
            try:
                viewport.grabMouse()
            except RuntimeError:
                viewport = None
            else:
                self._mid_pan_grab = viewport
        self._mid_pan_watch_start()
        self._mid_pan_log("press", event)
        try:
            event.accept()
        except (AttributeError, RuntimeError):
            pass
        return True

    # ── the diagnostic switch ────────────────────────────────────────
    #
    # Default off, one env var, one line per event, monotonic timestamps.
    # It exists because the failure it is aimed at cannot be reproduced
    # offscreen: the tests below can CONSTRUCT the sequence the desk
    # reported, but only a real X server can say which of the possible
    # causes actually produces it. So the machine that has the bug is the
    # instrument, and this is its readout.

    def _mid_pan_log(self, what, event=None, obj=None, reason=None):
        if not _mid_pan_debug_enabled():
            return
        try:
            self._mid_pan_log_line(what, event, obj, reason)
        except Exception as exc:                            # noqa: BLE001
            # A diagnostic that can break the gesture it is diagnosing is
            # worse than no diagnostic.
            try:
                print(f"MIDPAN log-failed what={what} err={exc!r}",
                      file=sys.stderr, flush=True)
            except Exception:                               # noqa: BLE001
                pass

    def _mid_pan_log_line(self, what, event, obj, reason):
        now = time.monotonic()
        t0 = self._mid_pan_t0
        since = "n/a" if t0 is None else f"{(now - t0) * 1000.0:.1f}ms"
        app = QtWidgets.QApplication.instance()
        ev_type = ev_btn = ev_buttons = "n/a"
        if event is not None:
            try:
                ev_type = int(event.type())
            except (AttributeError, RuntimeError):
                pass
            try:
                ev_btn = _mid_pan_button_name(event.button())
            except (AttributeError, RuntimeError):
                pass
            try:
                ev_buttons = _mid_pan_button_name(event.buttons())
            except (AttributeError, RuntimeError):
                pass
        try:
            app_buttons = _mid_pan_button_name(
                QtWidgets.QApplication.mouseButtons())
        except Exception:                                   # noqa: BLE001
            app_buttons = "n/a"
        try:
            scene_grab = _mid_pan_widget_name(
                self.gview.scene().mouseGrabberItem())
        except Exception:                                   # noqa: BLE001
            scene_grab = "n/a"
        try:
            focus = _mid_pan_widget_name(app.focusWidget() if app else None)
        except Exception:                                   # noqa: BLE001
            focus = "n/a"
        try:
            active = self.window().isActiveWindow()
        except Exception:                                   # noqa: BLE001
            active = "n/a"
        fields = [
            f"MIDPAN t={now:.6f}",
            f"gid={self._mid_pan_seq}",
            f"panel={_mid_pan_widget_name(self)}",
            f"what={what}",
            f"obj={_mid_pan_widget_name(obj) if obj is not None else 'viewport'}",
            f"type={ev_type}",
            f"button={ev_btn}",
            f"buttons={ev_buttons}",
            f"app_buttons={app_buttons}",
            f"grabber={_mid_pan_widget_name(QtWidgets.QWidget.mouseGrabber())}",
            f"scene_grab={scene_grab}",
            f"focus={focus}",
            f"active={active}",
            f"last={'set' if self._mid_pan_last is not None else 'None'}",
            f"grab={_mid_pan_widget_name(self._mid_pan_grab)}",
            f"confirmed={self._mid_pan_confirmed}",
            f"blank={self._mid_pan_blank}",
            f"watch={self._mid_pan_watch is not None}",
            f"sel_patch={getattr(self, '_selected_patch_idx', 'n/a')}",
            f"patch_drag={getattr(self, '_patch_drag', None) is not None}",
            f"drag_start={getattr(self, '_drag_start', None)!r}",
            f"swallow={getattr(self, '_adjust_swallow', 'n/a')}",
            f"since_press={since}",
        ]
        if reason is not None:
            fields.append(f"closed_by={reason}")
        print(" ".join(str(f) for f in fields), file=sys.stderr, flush=True)

    def _mid_pan_viewport(self):
        """The viewport widget the gesture is owned at, or None once the
        graphics view has been destroyed."""
        gview = getattr(self, "gview", None)
        if gview is None:
            return None
        try:
            return gview.viewport()
        except RuntimeError:
            return None

    def _middle_pan_holding(self):
        """True while a middle press taken at THIS viewport is still open.

        One fact, and it is this panel's own: a press was seen here and
        neither a release nor a cancellation has closed it. What may then
        end it is weighed in `_middle_pan_move` -- a release, a positively
        contradicting button state, or a sustained absence of one -- and
        never a single missing flag on a single move.

        `QWidget.mouseGrabber()` is deliberately NOT consulted. It used to
        be the second half of this test, and it is the wrong kind of fact
        to hang a gesture on: a process-wide singleton, describing the
        application rather than the event in hand, that this panel neither
        owns nor can keep. Any other widget calling `grabMouse()` replaces
        it -- Qt releases the previous grabber first -- and a platform grab
        can be refused or transferred, which the offscreen plugin says out
        loud every time it records one it could not take. In each of those
        cases the middle moves still ARRIVE here, with the middle button
        still down, and the gate threw them away from the very first one,
        so the drag did nothing at all. Discarding an event this widget
        legitimately received, because of a global that says nothing about
        that event, is never right; a move that is not ours does not reach
        us in the first place.

        (Qt's own popups are NOT among the thieves, measured rather than
        assumed: a menu or a combo box opened over a standing grab takes
        it and gives it back to the same widget on the way out. What is
        left is enough -- the gate is unsound whether or not any one
        caller trips it.)

        The grab is still TAKEN at the press -- see `_middle_pan_press` --
        because it is what keeps the drag alive once the cursor leaves the
        window. It is an enhancement, and enhancements are not
        preconditions.
        """
        return self._mid_pan_last is not None

    def _mid_pan_watch_start(self):
        """Watch the whole application for this gesture's release.

        Installed at the press and removed at the cancel, so outside a
        drag there is no application-wide filter at all. The watcher object
        itself is created once and parented to this panel, which is what
        makes "removed on destroy" true without a single line of teardown:
        Qt drops a destroyed filter by itself.
        """
        app = QtWidgets.QApplication.instance()
        if app is None:
            return
        if self._mid_pan_watch is not None:
            return
        watch = getattr(self, "_mid_pan_watch_obj", None)
        if watch is None:
            watch = _MidPanReleaseWatch(self)
            self._mid_pan_watch_obj = watch
        try:
            app.installEventFilter(watch)
        except RuntimeError:                                # noqa: BLE001
            return
        self._mid_pan_watch = watch

    def _mid_pan_watch_stop(self):
        watch, self._mid_pan_watch = self._mid_pan_watch, None
        if watch is None:
            return
        app = QtWidgets.QApplication.instance()
        if app is None:
            return
        try:
            app.removeEventFilter(watch)
        except RuntimeError:                                # noqa: BLE001
            pass

    def _mid_pan_check_viewport(self):
        """End an open gesture whose viewport has been swapped underneath.

        A graphics view can replace its viewport at any time, and a gesture
        anchored to a widget that is no longer the panel's own can never be
        completed by anything the panel will see again. Checked from the
        application watcher rather than on a timer, so it costs nothing
        except while a drag is open.
        """
        if self._mid_pan_last is None:
            return
        held = self._mid_pan_grab
        if held is None:
            return
        current = self._mid_pan_viewport()
        if current is not None and held is not current:
            self._middle_pan_cancel("the viewport was replaced under the drag")

    def _middle_pan_cancel(self, reason=None):
        """End the gesture and give the pointer back, from wherever.

        The one place the grab is released, so there is exactly one answer
        to "who releases it" -- the release event, a lost button, a hidden
        or deactivated or closed window, a dataset reload that replaces the
        viewport, and teardown all come here. Safe to call unconditionally.

        The release is CONDITIONAL on still BEING the grabber, and that is
        measured rather than assumed: `QWidget.releaseMouse()` clears the
        application's one grabber even when called on a widget that is not
        holding it. An unconditional release here would therefore take the
        pointer away from whoever took it from us and leave that widget
        waiting for events it will never get. We give back exactly what we
        still hold, and nothing else.
        """
        was_open = self._mid_pan_last is not None or self._mid_pan_grab is not None
        if was_open and reason is not None:
            self._mid_pan_log("cancel", reason=reason)
        held, self._mid_pan_grab = self._mid_pan_grab, None
        self._mid_pan_last = None
        self._mid_pan_confirmed = False
        self._mid_pan_blank = 0
        self._mid_pan_watch_stop()
        if held is None:
            return
        try:
            if QtWidgets.QWidget.mouseGrabber() is held:
                held.releaseMouse()
        except RuntimeError:
            pass

    def _middle_pan_move(self, event):
        """Pan by exactly this move's displacement.

        The anchor is kept in SCENE coordinates and both ends of the step
        are mapped into view coordinates HERE, in the same frame, just
        before the translation. That ordering is the whole correctness of
        the gesture: view coordinates are defined by the camera, so an
        anchor stored as a view point silently means something different
        once the camera has moved, and the drag comes out longer than the
        cursor travelled. Measured, storing the view point overshot a 60 px
        drag by a quarter of itself.

        Panning the camera by the negative of the step is what makes the
        picture follow the cursor. The anchor then becomes this point, so
        the next move measures from here: each move translates once, and the
        steps telescope to exactly the whole drag.

        WHICH moves count is the whole of the "I have to hold the button
        for a second before it will drag" report, and the rule this used to
        apply was:

            if not (event.buttons() & Qt.MiddleButton): cancel()

        -- one move without the flag killed the gesture permanently. That
        is exactly one bit of evidence, taken as decisive, and the desk's
        symptom is what a missing bit looks like: press, move at once,
        nothing ever again; press, WAIT, move, and it works. Waiting does
        not enable anything in this code -- there is no timer here -- but
        it does give the platform time to settle the button state it
        reports on the first moves after a press. So the suspicion under
        test is that those first moves arrive with NoButton and the old
        line threw the whole drag away on the first of them.

        The rule now ranks the evidence instead of taking the first bit
        that comes:

          * `buttons()` (or the application's own view of the buttons)
            carries the middle button -> the drag is confirmed and pans.
          * `buttons()` is non-empty but has no middle button -- some OTHER
            button is down and ours is not -- that is a POSITIVE
            contradiction, reported by a platform that is demonstrably
            telling us about buttons. The drag is over.
          * `buttons()` is empty: the platform is telling us nothing at
            all. A press we saw ourselves outranks an absence, so the move
            pans. Only a RUN of such moves, and only after the middle
            button has once been reported down in this same gesture, is
            read as a release we never received.

        A drag that is never confirmed therefore never ends on a move --
        it ends on its release, which `_MidPanReleaseWatch` catches even
        when it lands on another widget entirely. That is what makes it
        safe to stop trusting a single blank move.

        `QApplication.mouseButtons()` is consulted only as a SECOND way to
        confirm, never to contradict. Whether it is more reliable than
        `event.buttons()` on the affected machine is UNVERIFIED here: no
        offscreen test can produce the disagreement, and only the log from
        `BLOCK01_MIDPAN_DEBUG=1` on the real desk can say. Used this way
        it cannot make things worse if it is wrong in the same direction,
        because it can only ever add a reason to keep panning.
        """
        buttons = event.buttons()
        if buttons & Qt.MiddleButton:
            self._mid_pan_confirmed = True
            self._mid_pan_blank = 0
        elif buttons:
            self._mid_pan_log("move-contradicted", event)
            self._middle_pan_cancel(
                "a move reported other buttons down and not the middle one")
            return True
        else:
            app_buttons = Qt.NoButton
            try:
                app_buttons = QtWidgets.QApplication.mouseButtons()
            except Exception:                               # noqa: BLE001
                pass
            if app_buttons & Qt.MiddleButton:
                self._mid_pan_confirmed = True
                self._mid_pan_blank = 0
            else:
                self._mid_pan_blank += 1
                self._mid_pan_log("move-blank", event)
                if (self._mid_pan_confirmed
                        and self._mid_pan_blank > MID_PAN_BLANK_MOVE_TOLERANCE):
                    self._middle_pan_cancel(
                        "a run of moves carried no buttons after the middle "
                        "button had been reported down")
                    return True
        self._mid_pan_log("move", event)
        last_scene = self._mid_pan_last
        now_scene = self._mid_pan_scene_pos(event)
        if last_scene is None or now_scene is None:
            self._mid_pan_last = now_scene
            return True
        try:
            last = self.vb.mapSceneToView(last_scene)
            now = self.vb.mapSceneToView(now_scene)
        except Exception:                                   # noqa: BLE001
            self._mid_pan_last = now_scene
            return True
        dx = now.x() - last.x()
        dy = now.y() - last.y()
        self._mid_pan_last = now_scene
        if dx or dy:
            self.vb._resetTarget()
            self.vb.translateBy(x=-dx, y=-dy)
            self.vb.sigRangeChangedManually.emit((True, True))
            # A step that translated is a camera the user has moved. The
            # PRESS is not: a middle click that never moved anywhere leaves
            # the panel owning the camera, so a stray click on a
            # half-loaded thumbnail does not cost that slide its one
            # automatic fit.
            self._thumbnail_camera_touched = True
        return True

    def _middle_pan_release(self, event):
        """Give the pointer back and forget the anchor.

        The anchor is cleared as well as the grab, so the NEXT gesture
        measures from its own press: an inherited anchor would make the
        first move of the second drag jump by the distance between the two
        drags.
        """
        self._mid_pan_log("release", event)
        self._middle_pan_cancel("the middle button's release at the viewport")
        try:
            event.accept()
        except (AttributeError, RuntimeError):
            pass
        return True

    def hideEvent(self, event):
        """A hidden panel is not holding anything.

        The panel, not only its viewport: the Tissue Navigator popup is
        hidden as a WINDOW, and a window hidden with a grab still standing
        takes the pointer away from the whole application.
        """
        self._middle_pan_cancel("the panel was hidden")
        super().hideEvent(event)

    def closeEvent(self, event):
        self._middle_pan_cancel("the panel was closed")
        super().closeEvent(event)

    def _mid_pan_scene_pos(self, event):
        """`event`'s position in SCENE coordinates.

        The scene is the frame the gesture is anchored in because it does
        not move when the camera does -- which is exactly what a pan makes
        happen between one move and the next.
        """
        try:
            return self.gview.mapToScene(event.pos())
        except Exception:                                   # noqa: BLE001
            return None

    def eventFilter(self, obj, event):
        if obj is not self._mid_pan_viewport():
            return super().eventFilter(obj, event)

        t = event.type()

        # A held middle drag must not survive the viewport going away from
        # under it: an application that keeps a mouse grab on a hidden or
        # closed widget answers no further input at all. Checked before
        # every other branch, and NOT consumed -- these are the viewport's
        # own events and it still has to process them.
        #
        # `FocusOut` is deliberately NOT one of them. Which widget holds the
        # keyboard is not a fact about the pointer, and cancelling a live
        # middle drag on a focus change is the same mistake the grabber gate
        # was: it ends a gesture the user is still making, for a reason that
        # has nothing to do with the gesture. A window going away or going
        # inactive is different -- there the pointer really is gone.
        if t in (QtCore.QEvent.Hide, QtCore.QEvent.Close,
                 QtCore.QEvent.WindowDeactivate):
            self._middle_pan_cancel(
                "the viewport was hidden, closed or deactivated")

        # ── Key press (ROI mode) ──────────────────────────────────────
        if t == QtCore.QEvent.KeyPress and self._mode == 'roi':
            key = event.key()
            if key == Qt.Key_Z:
                self._undo_vertex(); return True
            elif key == Qt.Key_D:
                self._delete_last_roi(); return True
            elif key in (Qt.Key_Return, Qt.Key_Enter):
                self._finish_roi(); return True

        # A patch under adjustment is deleted by Delete, in EVERY mode, and
        # through the very same removal path the toolbar's Del button uses.
        if t == QtCore.QEvent.KeyPress and self._selected_patch_idx >= 0:
            if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
                self._remove_patch(self._selected_patch_idx)
                return True

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
            ip = self.vb.mapSceneToView(sp)     # overview pixels (see _ov_pos)
            cx, cy = ip.x(), ip.y()
            vr = self.vb.viewRange()
            self.vb.disableAutoRange()
            self.vb.setRange(
                xRange=[cx + (vr[0][0]-cx)/factor, cx + (vr[0][1]-cx)/factor],
                yRange=[cy + (vr[1][0]-cy)/factor, cy + (vr[1][1]-cy)/factor],
                padding=0,
            )
            # The camera is the user's from the first turn of the wheel that
            # actually MOVED it. Compared against the range this branch
            # started from, not assumed from the event: a wheel with no
            # delta, or one an aspect-locked box clamps to where it already
            # was, changed nothing and is not a claim on the camera.
            if _view_range_moved(vr, self.vb.viewRange()):
                self._thumbnail_camera_touched = True
            return True

        # ── The middle button pans, in every mode ─────────────────────
        #
        # ONE handler, above every mode branch and above the adjust state,
        # in the ONE class both thumbnails are instances of -- the page's
        # own and the Tissue Navigator popup's. The thumbnail is a map you
        # have to be able to move under a zoom, and the left button is
        # spoken for everywhere: it draws rectangles in patch mode, adds
        # vertices in ROI mode, navigates with no tool out, and edits the
        # selected rectangle in the adjust state. The middle button is the
        # one gesture that means the same thing in all four, so it is the
        # one that pans.
        #
        # It is owned HERE, at the viewport, and not one layer down in
        # `ViewBox.mouseDragEvent`, because this is where the events
        # measurably are. A four-move middle drag delivered to the window in
        # the real hierarchy reaches this viewport as press=1 move=4
        # release=1, while pyqtgraph's scene built a drag event for only two
        # of those four moves -- and on the user's machine, for the popup,
        # none. The press opening the gesture is what makes this reliable:
        # the moves and the release that arrive HERE are the gesture, and no
        # global state gets a vote on whether they count. A grab is taken as
        # well, so that a drag which runs off the edge of the thumbnail keeps
        # coming here -- but it is an enhancement, not a precondition.
        #
        # Consumed rather than passed on: every branch below is then reached
        # only with the middle button up, so none of them has to know this
        # gesture exists, and `_adjust_swallow` is left alone -- it belongs
        # to the press that ENDS an adjustment, and no middle press is that.
        if t == QtCore.QEvent.MouseButtonPress \
                and event.button() == Qt.MiddleButton:
            return self._middle_pan_press(event)
        if t == QtCore.QEvent.MouseMove and self._middle_pan_holding():
            # A move that reached this viewport with our own press still
            # open IS this gesture's move. `_middle_pan_move` decides on the
            # one thing that can still contradict that -- the middle button
            # no longer being down -- and on nothing else.
            return self._middle_pan_move(event)
        if t == QtCore.QEvent.MouseButtonRelease \
                and event.button() == Qt.MiddleButton:
            return self._middle_pan_release(event)

        # ── Mouse press ───────────────────────────────────────────────
        # `if`, not `elif`: the wheel branch above returns unconditionally,
        # and the middle-button handler sits between the two.
        if t == QtCore.QEvent.MouseButtonPress:
            sp = self.gview.mapToScene(event.pos())
            r, c = self._ov_pos(sp)
            fr, fc = self._ov_pos_f(sp)

            # ── The adjust state owns the mouse ───────────────────────
            # While a patch is selected the thumbnail is that patch's editor.
            # A press on it (or on a grip) edits it; a press anywhere else
            # ends the adjustment and does nothing whatsoever -- it does not
            # also navigate, and it does not also start a rectangle. The mode
            # buttons get their say back on the NEXT click.
            if self._selected_patch_idx >= 0:
                hit = self._selected_patch_hit_test(fr, fc)
                if hit is not None:
                    self._begin_patch_drag(hit[0], hit[1], fr, fc, event.pos())
                    return True
                self._select_patch_artist(-1)
                self._adjust_swallow = True
                self._nav_press = None
                self._drag_start = None
                return True

            # ── Entering the adjust state ─────────────────────────────
            # The border and the label are the patch's own furniture, so a
            # click there is about the patch whatever tool is out. Its
            # INTERIOR is still tissue: a click there draws or navigates.
            if event.button() == Qt.LeftButton:
                idx = self._patch_edge_hit_test(fr, fc)
                if idx is not None:
                    self._select_patch_artist(idx)
                    self._begin_patch_drag(idx, None, fr, fc, event.pos())
                    return True

            if self._mode is None:
                # Navigate: a left press that is released where it began
                # (< 3 px) is a click, and the release navigates. A left
                # drag does nothing (no left-drag pan, by request); the
                # middle button pans as in the other modes.
                if event.button() == Qt.LeftButton:
                    self._nav_press = (event.pos(), r, c)
                    self._nav_moved = False
                return True

            if self._mode == 'roi':
                if event.button() == Qt.LeftButton and (
                        event.modifiers() & Qt.ControlModifier
                        or (not self._cur_pts
                            and self._find_roi_for_patch(r, c) is not None)):
                    # Inside an ROI that already exists there is nothing to
                    # draw, so a click there is "take me there" (also always
                    # with Ctrl). While a polygon is being drawn, clicks add
                    # vertices wherever they land.
                    self._emit_navigate(r, c)
                    return True
                if not self._roi_create_allowed:
                    return True
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

            else:  # patch mode
                if event.button() == Qt.LeftButton:
                    self._drag_start = (r, c)
                    return True
                elif event.button() == Qt.RightButton:
                    self._right_press_pos = event.pos()
                    return True

        # ── Mouse move ────────────────────────────────────────────────
        elif t == QtCore.QEvent.MouseMove:
            if self._adjust_swallow:
                return True
            sp = self.gview.mapToScene(event.pos())
            r, c = self._ov_pos(sp)

            drag = self._patch_drag
            if drag is not None and (event.buttons() & Qt.LeftButton):
                if (event.pos() - drag["press"]).manhattanLength() \
                        >= PATCH_DRAG_SLOP_PX:
                    drag["moved"] = True
                if drag["moved"]:
                    fr, fc = self._ov_pos_f(sp)
                    self._preview_patch_geometry(
                        drag["idx"],
                        self._patch_geometry_for_drag(drag, fr, fc))
                return True

            if self._mode is None:
                press = getattr(self, "_nav_press", None)
                if press is not None and (event.pos() - press[0]).manhattanLength() >= 3:
                    self._nav_moved = True
                return True

            if self._mode == 'roi':
                if self._cur_pts:
                    self._update_preview_line(r, c)
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

        # ── Mouse release ─────────────────────────────────────────────
        elif t == QtCore.QEvent.MouseButtonRelease:
            if self._adjust_swallow:
                # The press that ended the adjustment ate the whole gesture.
                self._adjust_swallow = False
                return True
            if event.button() == Qt.LeftButton and self._patch_drag is not None:
                drag = self._patch_drag
                self._patch_drag = None
                # The gesture commits on release, once: a patch that is
                # dragged through ten mouse-moves is still one edit.
                if drag["moved"]:
                    sp = self.gview.mapToScene(event.pos())
                    fr, fc = self._ov_pos_f(sp)
                    self._commit_patch_geometry(
                        drag["idx"],
                        self._patch_geometry_for_drag(drag, fr, fc),
                        revert_to=drag["coords"])
                return True

            if self._mode is None:
                if event.button() == Qt.LeftButton:
                    press = getattr(self, "_nav_press", None)
                    self._nav_press = None
                    if press is not None and not getattr(self, "_nav_moved", False):
                        # A click on bare tissue means "take me there".
                        self._emit_navigate(press[1], press[2])
                return True

            if self._mode == 'roi':
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
                    if abs(r - r0) < 3 and abs(c - c0) < 3:
                        # A click, not a drag: navigate to the clicked spot.
                        self._emit_navigate(r, c)
                        return True
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
                    return True

        return False


# ══════════════════════════════════════════════════════════════════════
#  Result Grid Panel
# ══════════════════════════════════════════════════════════════════════
