"""
block01/ui/step3_page.py — _RoiRect, _ThumbLoader, _RegionLoader, Step3Page.
"""

import os
import random
import glob
import json
import re
import traceback

import numpy as np
import tifffile
import zarr

from PyQt5 import QtWidgets, QtCore, QtGui
from PyQt5.QtCore import Qt, QTimer, QRectF, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QGroupBox, QSplitter, QSlider, QCheckBox, QScrollArea,
    QMessageBox, QFileDialog,
)
import pyqtgraph as pg

from ..config import OUTPUT_DIR, OME_TIFF_FILE
from ..core.io_loader import OMETIFFLoader
from ..utils.mask_renderer import render_mask_overlay
from ..utils.roi_project import (
    project_roi_index_path,
    roi_manifest_path,
    roi_index_path,
    load_json,
)

# ══════════════════════════════════════════════════════════════════════
#  Step 3 Page  — Segmentation QC Viewer
# ══════════════════════════════════════════════════════════════════════

class _RoiRect(QtWidgets.QGraphicsObject):
    """
    Rectangle drawn on the DAPI thumbnail to select a QC region.

    Movement and deletion are handled externally by Step3Page.eventFilter
    (pyqtgraph ViewBox intercepts Qt mouse events so ItemIsMovable does not
    work reliably).  This class just draws the rect and converts positions
    to full-image coordinates.

    Signals
    -------
    roi_changed(y0, y1, x0, x1) — emitted whenever position/size changes
    """
    roi_changed = pyqtSignal(int, int, int, int)

    _PEN_IDLE  = pg.mkPen('#f0a030', width=2)
    _PEN_HOVER = pg.mkPen('#ffd060', width=2, style=Qt.DashLine)
    _FILL      = QtGui.QColor(240, 160, 48, 40)
    _FILL_HOV  = QtGui.QColor(240, 160, 48, 70)

    def __init__(self, x_ds, y_ds, w_ds, h_ds, ds, full_h, full_w):
        super().__init__()
        # rect is stored in thumbnail (downsampled) coordinates
        self._x  = float(x_ds)
        self._y  = float(y_ds)
        self._w  = float(w_ds)
        self._h  = float(h_ds)
        self._ds       = ds
        self._full_h   = full_h
        self._full_w   = full_w
        self._hovered  = False
        self.setAcceptHoverEvents(True)

    # ── geometry ─────────────────────────────────────────────────────

    def rect_ds(self):
        """Return (x, y, w, h) in thumbnail coords."""
        return self._x, self._y, self._w, self._h

    def move_by_ds(self, dx, dy):
        """Translate the rect by (dx, dy) in thumbnail coords, clamped to image."""
        th = self._full_h / self._ds
        tw = self._full_w / self._ds
        self._x = max(0.0, min(tw - self._w, self._x + dx))
        self._y = max(0.0, min(th - self._h, self._y + dy))
        self.prepareGeometryChange()
        self._emit()

    def set_rect_ds(self, x, y, w, h):
        self._x, self._y, self._w, self._h = float(x), float(y), float(w), float(h)
        self.prepareGeometryChange()
        self._emit()

    def contains_ds(self, px, py):
        """Return True if thumbnail-coord point (px,py) is inside the rect."""
        return (self._x <= px <= self._x + self._w and
                self._y <= py <= self._y + self._h)

    # ── QGraphicsItem interface ───────────────────────────────────────

    def boundingRect(self):
        return QtCore.QRectF(self._x - 2, self._y - 2,
                             self._w + 4, self._h + 4)

    def paint(self, painter, option, widget=None):
        r = QtCore.QRectF(self._x, self._y, self._w, self._h)
        painter.setPen(self._PEN_HOVER if self._hovered else self._PEN_IDLE)
        painter.setBrush(self._FILL_HOV if self._hovered else self._FILL)
        painter.drawRect(r)

        # Corner hint text when hovered
        if self._hovered:
            painter.setPen(pg.mkPen('#ffd060'))
            painter.setFont(QtGui.QFont('monospace', 7))
            painter.drawText(
                QtCore.QPointF(self._x + 3, self._y + 10),
                'drag=move  right-click=delete'
            )

    def hoverEnterEvent(self, ev):
        self._hovered = True
        self.update()

    def hoverLeaveEvent(self, ev):
        self._hovered = False
        self.update()

    # ── coordinate conversion ─────────────────────────────────────────

    def _emit(self):
        self.update()
        x0 = int(max(0, self._x) * self._ds)
        y0 = int(max(0, self._y) * self._ds)
        x1 = int(min(self._full_w, (self._x + self._w) * self._ds))
        y1 = int(min(self._full_h, (self._y + self._h) * self._ds))
        if x1 > x0 and y1 > y0:
            self.roi_changed.emit(y0, y1, x0, x1)


class _ThumbLoader(QThread):
    """Load global DAPI thumbnail in background."""
    done  = pyqtSignal(object)   # float32 ndarray
    error = pyqtSignal(str)

    def __init__(self, dapi_path, ds):
        super().__init__()
        self.dapi_path = dapi_path
        self.ds        = ds
        self._stop     = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            if self._stop:
                return
            tif   = tifffile.TiffFile(self.dapi_path)
            store = tif.aszarr()
            try:
                z = zarr.open(store, mode='r')
                if isinstance(z, zarr.hierarchy.Group):
                    z0 = z[0] if '0' in z else next(iter(z.values()))
                else:
                    z0 = z
                if z0.ndim == 3:
                    arr = np.array(z0[0, ::self.ds, ::self.ds])
                else:
                    arr = np.array(z0[::self.ds, ::self.ds])
            finally:
                store.close()
                tif.close()
            if self._stop:
                return
            arr  = arr.astype(np.float32)
            nz   = arr[arr > 0]
            if nz.size > 100:
                lo, hi = np.percentile(nz, [1.0, 99.5])
                if hi > lo:
                    arr = np.clip((arr - lo) / (hi - lo), 0.0, 1.0)
                else:
                    arr = np.zeros_like(arr)
            if not self._stop:
                self.done.emit(arr)
        except Exception as e:
            if not self._stop:
                self.error.emit(traceback.format_exc())


class _RegionLoader(QThread):
    """
    Load a rectangular ROI from global_dapi + global_mask.

    Signals:
        done(dapi_rgb, fusion_rgb_or_none, mask_labels, n_cells, h, w, fusion_source)
    """
    done  = pyqtSignal(object, object, object, int, int, int, str)
    error = pyqtSignal(str)

    def __init__(self, dapi_path, mask_path, y0, y1, x0, x1,
                 sub=1, fused_zarr_path=None, fusion_roi=None,
                 tile_infos=None):
        super().__init__()
        self.dapi_path = dapi_path
        self.mask_path = mask_path
        self.y0, self.y1, self.x0, self.x1 = y0, y1, x0, x1
        self.sub = sub
        self.fused_zarr_path = fused_zarr_path
        self.fusion_roi = fusion_roi
        self.tile_infos = tile_infos or []
        self._stop = False

    def stop(self):
        self._stop = True

    @staticmethod
    def _read_region(path, y0, y1, x0, x1, sub):
        tif   = tifffile.TiffFile(path)
        store = tif.aszarr()
        try:
            z  = zarr.open(store, mode='r')
            if isinstance(z, zarr.hierarchy.Group):
                z0 = z[0] if '0' in z else next(iter(z.values()))
            else:
                z0 = z
            if z0.ndim == 2:
                arr = np.array(z0[y0:y1:sub, x0:x1:sub])
            else:
                arr = np.array(z0[0, y0:y1:sub, x0:x1:sub])
        finally:
            store.close()
            tif.close()
        return arr

    @staticmethod
    def _norm_u8(arr):
        a  = arr.astype(np.float32)
        nz = a[a > 0]
        if nz.size > 100:
            lo, hi = np.percentile(nz, [1.0, 99.5])
            if hi > lo:
                a = np.clip((a - lo) / (hi - lo), 0.0, 1.0)
            else:
                a = np.zeros_like(a)
        else:
            a = np.zeros_like(a)
        return (a * 255).astype(np.uint8)

    @staticmethod
    def _read_fusion_rgb(zarr_path, roi, sub):
        if not zarr_path or not os.path.exists(zarr_path) or roi is None:
            return None
        y0, y1, x0, x1 = roi
        print(f"[Step3] reading fusion crop local_bbox={[y0, y1, x0, x1]}")
        z = zarr.open(zarr_path, mode='r')
        patch = np.asarray(z[y0:y1:sub, x0:x1:sub, :])
        if patch.ndim != 3 or patch.shape[2] not in (2, 3):
            return None
        print(f"[Step3] fusion crop shape={patch.shape}")
        if patch.size:
            print(f"[Step3] fusion min/max={float(np.nanmin(patch))}/{float(np.nanmax(patch))}")
        if patch.shape[2] == 2:
            ch0_min, ch0_max = float(np.nanmin(patch[:, :, 0])), float(np.nanmax(patch[:, :, 0]))
            ch1_min, ch1_max = float(np.nanmin(patch[:, :, 1])), float(np.nanmax(patch[:, :, 1]))
            print(f"[Step3] fusion ch0 min/max={ch0_min}/{ch0_max}")
            print(f"[Step3] fusion ch1 min/max={ch1_min}/{ch1_max}")
            if ch0_max <= 0:
                print("[Step3] fusion cyto channel is empty")
            cyto = _RegionLoader._norm_u8(patch[:, :, 0])
            nuc = _RegionLoader._norm_u8(patch[:, :, 1])
            return np.stack([cyto, np.zeros_like(cyto, dtype=np.uint8), nuc], axis=-1)
        for i in range(3):
            print(
                f"[Step3] fusion ch{i} min/max="
                f"{float(np.nanmin(patch[:, :, i]))}/{float(np.nanmax(patch[:, :, i]))}"
            )
        chans = [_RegionLoader._norm_u8(patch[:, :, i]) for i in range(3)]
        return np.stack(chans, axis=-1)

    @staticmethod
    def _relabel_mask(mask):
        binary = np.asarray(mask) > 0
        if not np.any(binary):
            return np.zeros(binary.shape, dtype=np.uint32)
        try:
            from skimage.measure import label
            return label(binary, connectivity=1).astype(np.uint32)
        except Exception:
            pass
        try:
            from scipy import ndimage
            labels, _ = ndimage.label(binary)
            return labels.astype(np.uint32)
        except Exception:
            return binary.astype(np.uint32)

    @staticmethod
    def _read_stitched_tiles(patch_roi, tile_infos, sub):
        py0, py1, px0, px1 = patch_roi
        ph, pw = py1 - py0, px1 - px0
        if ph <= 0 or pw <= 0:
            raise ValueError("Patch outside ROI.")
        dapi_canvas = np.zeros((ph, pw), dtype=np.uint16)
        mask_canvas = np.zeros((ph, pw), dtype=np.uint32)
        used = []

        for tile in tile_infos:
            ty0, ty1, tx0, tx1 = [int(v) for v in tile["bbox_local"]]
            iy0 = max(py0, ty0)
            iy1 = min(py1, ty1)
            ix0 = max(px0, tx0)
            ix1 = min(px1, tx1)
            if iy1 <= iy0 or ix1 <= ix0:
                continue

            dapi_path = tile.get("dapi_path")
            mask_path = tile.get("mask_path")
            if not dapi_path or not mask_path or not os.path.exists(dapi_path) or not os.path.exists(mask_path):
                raise FileNotFoundError("Missing tile input.")

            tile_y0 = iy0 - ty0
            tile_y1 = iy1 - ty0
            tile_x0 = ix0 - tx0
            tile_x1 = ix1 - tx0
            patch_y0 = iy0 - py0
            patch_y1 = iy1 - py0
            patch_x0 = ix0 - px0
            patch_x1 = ix1 - px0

            dapi_crop = _RegionLoader._read_region(
                dapi_path, tile_y0, tile_y1, tile_x0, tile_x1, 1
            )
            mask_crop = _RegionLoader._read_region(
                mask_path, tile_y0, tile_y1, tile_x0, tile_x1, 1
            )
            dapi_canvas[patch_y0:patch_y1, patch_x0:patch_x1] = dapi_crop.astype(np.uint16)
            mask_canvas[patch_y0:patch_y1, patch_x0:patch_x1] = mask_crop.astype(np.uint32)
            used.append(tile)

        if not used:
            raise ValueError("Patch outside ROI.")

        if sub > 1:
            dapi_canvas = dapi_canvas[::sub, ::sub]
            mask_canvas = mask_canvas[::sub, ::sub]
        mask_canvas = _RegionLoader._relabel_mask(mask_canvas)
        return dapi_canvas, mask_canvas, used

    def run(self):
        try:
            y0, y1, x0, x1, sub = self.y0, self.y1, self.x0, self.x1, self.sub
            if self._stop:
                return

            if self.tile_infos:
                dapi_raw, mask_u32, used_tiles = self._read_stitched_tiles(
                    (y0, y1, x0, x1), self.tile_infos, sub
                )
                print(f"[Step3] patch intersects n_tiles={len(used_tiles)}")
                for tile in used_tiles:
                    print(
                        f"[Step3] using tile r={tile.get('row')} c={tile.get('col')} "
                        f"bbox={tile.get('bbox_local')}"
                    )
            else:
                print("[Step3] using direct ROI OME crop")
                dapi_raw = self._read_region(self.dapi_path, y0, y1, x0, x1, sub)
                if self._stop:
                    return
                mask_raw = self._read_region(self.mask_path, y0, y1, x0, x1, sub)
                mask_u32 = mask_raw.astype(np.uint32)
            if self._stop:
                return

            grey_u8  = self._norm_u8(dapi_raw)
            dapi_rgb = np.stack([grey_u8, grey_u8, grey_u8], axis=-1)
            fusion_rgb = None
            fusion_source = "unavailable"
            try:
                fusion_rgb = self._read_fusion_rgb(
                    self.fused_zarr_path, self.fusion_roi, sub
                )
                if fusion_rgb is not None:
                    fusion_source = self.fused_zarr_path
            except Exception:
                fusion_rgb = None
                fusion_source = "unavailable"

            n_cells  = int(mask_u32.max())

            h, w = grey_u8.shape
            print(f"[Step3] dapi crop shape={dapi_rgb.shape}")
            print(f"[Step3] mask crop shape={mask_u32.shape}")
            print(f"[Step3] stitched dapi shape={dapi_rgb.shape}")
            print(f"[Step3] stitched mask shape={mask_u32.shape}")
            print(f"[Step3] relabeled cells={n_cells}")
            print(f"[Step3] fusion crop source={fusion_source}")
            if not self._stop:
                self.done.emit(dapi_rgb, fusion_rgb, mask_u32, n_cells, h, w, fusion_source)
        except Exception as e:
            if not self._stop:
                self.error.emit(traceback.format_exc())


class Step3Page(QWidget):
    """
    Step 3 — Segmentation QC Viewer.

    Left  : global DAPI thumbnail with draggable ROI rectangle.
    Right : zoomed ROI view — DAPI grey + mask overlay, zoom/pan,
            random-sample button, cell count, show/hide mask toggle.
    """

    go_back  = pyqtSignal()
    go_step4 = pyqtSignal()   # user clicked → Step 4

    _OV_DS = 32    # thumbnail downsample

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dapi_path       = None
        self._mask_path       = None
        self._fused_zarr_path = None   # for cyto+nucleus background
        self._fusion_zarr_path = None
        self._output_dir      = None
        self._loader          = None
        self._raw_ome_path    = None
        self._raw_loader      = None
        self._corrected_zarr_path = None
        self._corrected_zarr = None
        self._corrected_channel_names = []
        self._raw_channel_names = []
        self._channel_sources = {}
        self._project_dir = None
        self._roi_entries = []
        self._segmentation_runs = {}
        self._selected_run_meta = {}
        self._selected_meta_path = ""
        self._rois           = []
        self._mode            = "full_wsi"
        self._active_roi_name = None
        self._active_tiles_dir = None
        self._active_bbox     = None
        self._tile_grid       = None
        self._tile_shape      = None
        self._tile_infos      = []
        self._patch_source    = "unknown"
        self._roi_global_ome_available = False
        self._full_h      = 0
        self._full_w      = 0
        self._thumb_arr   = None   # float32 normalised thumbnail
        self._thumb_h     = 0
        self._thumb_w     = 0

        self._roi_rect       = None    # _RoiRect item on scene
        self._roi_draw_start = None    # QPointF while rubber-band drawing (on empty area)
        self._rect_drag_last = None    # QPointF while dragging existing rect

        # Current ROI in full-res coords
        self._roi = None   # (y0,y1,x0,x1) or None

        # Zoom/pan state for ROI view
        self._pan_last    = None

        # Current displayed arrays
        self._patch_dapi_rgb = None
        self._patch_fusion_rgb = None
        self._patch_fusion_source = None
        self._patch_fusion_available = False
        self._mask_labels = None   # uint32 (H,W), 0=background, >0=cells
        self._mask_alpha  = 0.35
        self._show_outline = True
        self._show_fusion  = True
        self._background_mode = "DAPI"
        self._available_channels = []
        self._channel_settings = {}
        self._channel_rows = {}
        self._patch_channel_cache = {}
        self._last_patch_bbox = None

        self._thumb_loader  = None
        self._region_loader = None

        # Debounce: only fire region load 400 ms after last ROI change
        self._load_debounce = QTimer()
        self._load_debounce.setSingleShot(True)
        self._load_debounce.timeout.connect(self._load_region)

        self._build_ui()
        self._restore_saved_input_sources()

    # ── public API ────────────────────────────────────────────────────

    def set_channel_context(self, loader=None, corrected_zarr_path=None, rois=None):
        self._loader = loader
        self._corrected_zarr_path = corrected_zarr_path if corrected_zarr_path and os.path.exists(corrected_zarr_path) else None
        if loader is not None and getattr(loader, "filepath", None):
            self._raw_ome_path = loader.filepath
            self._raw_loader = loader
        self._corrected_zarr = None
        self._rois = list(rois or [])
        self._sync_active_roi_from_input_paths()
        self._refresh_channel_sources()

    def _restore_saved_input_sources(self):
        cfg = self._load_input_files_config(self._output_dir or OUTPUT_DIR)
        if not cfg:
            self._corrected_zarr_path = self._resolve_channel_source_path(None)
            self._raw_ome_path = self._resolve_raw_ome_path(None)
        else:
            self._dapi_path = cfg.get("dapi") or self._dapi_path
            self._mask_path = cfg.get("mask") or self._mask_path
            self._fusion_zarr_path = cfg.get("fusion_source") or self._fusion_zarr_path
            self._fused_zarr_path = self._fusion_zarr_path
            self._corrected_zarr_path = self._resolve_channel_source_path(cfg.get("channel_source"))
            self._raw_ome_path = self._resolve_raw_ome_path(cfg.get("raw_ome"))
            self._dapi_edit.setText(self._dapi_path or "")
            self._mask_edit.setText(self._mask_path or "")
            self._fusion_edit.setText(self._fusion_zarr_path or "")
            self._channel_source_edit.setText(self._corrected_zarr_path or "")
            self._raw_ome_edit.setText(self._raw_ome_path or "")
        self._sync_active_roi_from_input_paths()
        self._refresh_channel_sources()

    def set_output_dir(self, output_dir):
        """Called from MainWindow when Step 2 finishes."""
        self._output_dir = output_dir
        print("[Step3] entering QC viewer")
        print("[Step3] loading previous result")
        print(f"[Step3] output_dir={output_dir}")
        try:
            self._stop_loaders()
            self._clear_region_cache()
            roi_dir = self._infer_roi_dir_from_output(output_dir)
            if roi_dir:
                try:
                    with open(os.path.join(roi_dir, "roi_manifest.json"), "r", encoding="utf-8") as f:
                        roi_manifest = json.load(f)
                    print(f"[Step3] roi_id={roi_manifest.get('roi_id')}")
                    print("[Step3] validating source roi_id / bbox")
                except Exception:
                    print(f"[Step3] failed to read ROI manifest:\n{traceback.format_exc()}")
                meta_path = os.path.join(output_dir, "segmentation_meta.json")
                meta = load_json(meta_path, {}) or {}
                if meta.get("roi_id"):
                    project_dir = os.path.dirname(os.path.dirname(roi_dir))
                    if hasattr(self, "_project_edit"):
                        self._project_edit.setText(project_dir)
                    self._project_dir = project_dir
                    self.load_project(project_dir)
                    rid = meta.get("run_id") or os.path.basename(output_dir)
                    if hasattr(self, "_run_combo"):
                        i = self._run_combo.findData(rid)
                        if i >= 0:
                            self._run_combo.setCurrentIndex(i)
                    return
            input_cfg = self._load_input_files_config(output_dir)
            self._show_fusion = True
            if hasattr(self, "_chk_fusion"):
                self._chk_fusion.blockSignals(True)
                self._chk_fusion.setEnabled(True)
                self._chk_fusion.setChecked(True)
                self._chk_fusion.blockSignals(False)
            print("[Step3] searching fusion source...")
            roi_inputs = self._find_roi_inputs(output_dir)
            if roi_inputs is not None:
                self._mode = "roi"
                self._active_roi_name = roi_inputs["roi_name"]
                self._active_tiles_dir = roi_inputs["tiles_dir"]
                self._active_bbox = roi_inputs.get("bbox")
                self._tile_grid = roi_inputs.get("tile_grid")
                self._tile_infos = roi_inputs.get("tiles") or []
                self._fused_zarr_path = roi_inputs.get("fused_zarr")
                self._fusion_zarr_path = self._fused_zarr_path
                self._dapi_path = input_cfg.get("dapi") or roi_inputs["global_dapi"]
                self._mask_path = input_cfg.get("mask") or roi_inputs["global_mask"]
                if input_cfg.get("fusion_source"):
                    self._fusion_zarr_path = input_cfg.get("fusion_source")
                    self._fused_zarr_path = self._fusion_zarr_path
                self._corrected_zarr_path = self._resolve_channel_source_path(input_cfg.get("channel_source"))
                self._raw_ome_path = self._resolve_raw_ome_path(input_cfg.get("raw_ome"))
                self._sync_active_roi_from_input_paths()
                self._dapi_edit.setText(self._dapi_path or "")
                self._mask_edit.setText(self._mask_path or "")
                self._fusion_edit.setText(self._fusion_zarr_path or "")
                self._channel_source_edit.setText(self._corrected_zarr_path or "")
                self._raw_ome_edit.setText(self._raw_ome_path or "")
                self._refresh_channel_sources()
                print("[Step3] QC mode=roi")
                print(f"[Step3] active_roi={self._active_roi_name}")
                print(f"[Step3] roi_shape={roi_inputs.get('roi_shape')}")
                if self._tile_grid:
                    print(f"[Step3] tile_grid={self._tile_grid[0]}x{self._tile_grid[1]}")
                print("[Step3] showing ROI preview")
                print("[Step3] waiting for user patch")
                print(f"[Step3] tiles_dir={self._active_tiles_dir}")
                print(f"[Step3] selected fusion_zarr={self._fused_zarr_path}")
                print(f"[Step3] exists={bool(self._fused_zarr_path and os.path.exists(self._fused_zarr_path))}")
                print(f"[Step3] show_fusion default={self._show_fusion}")
                self._load_thumbnail()
                return

            self._mode = "full_wsi"
            self._active_roi_name = None
            self._active_tiles_dir = None
            self._active_bbox = None
            self._tile_grid = None
            self._tile_infos = []
            summary_path = os.path.join(output_dir, "segmentation_meta.json")
            summary_meta = None
            print(f"[Step3] segmentation_meta exists={os.path.exists(summary_path)}")
            if os.path.exists(summary_path):
                try:
                    with open(summary_path, "r", encoding="utf-8") as f:
                        summary_meta = json.load(f)
                except Exception:
                    print(f"[Step3] failed to read segmentation_meta.json:\n{traceback.format_exc()}")
            self._fused_zarr_path = self._find_fusion_zarr(output_dir, None, summary_meta, summary_meta)
            self._fusion_zarr_path = self._fused_zarr_path
            print(f"[Step3] metadata fused_zarr_path={summary_meta.get('fused_zarr_path') if isinstance(summary_meta, dict) else None}")
            print("[Step3] mode=full_wsi")
            print(f"[Step3] selected fusion_zarr={self._fused_zarr_path}")
            print(f"[Step3] exists={bool(self._fused_zarr_path and os.path.exists(self._fused_zarr_path))}")
            print(f"[Step3] show_fusion default={self._show_fusion}")

            dapi = os.path.join(output_dir, 'global_dapi.ome.tiff')
            mask = os.path.join(output_dir, 'global_mask.ome.tiff')
            if os.path.exists(dapi) and os.path.exists(mask):
                self._dapi_path = input_cfg.get("dapi") or dapi
                self._mask_path = input_cfg.get("mask") or mask
                if input_cfg.get("fusion_source"):
                    self._fusion_zarr_path = input_cfg.get("fusion_source")
                    self._fused_zarr_path = self._fusion_zarr_path
                self._corrected_zarr_path = self._resolve_channel_source_path(input_cfg.get("channel_source"))
                self._raw_ome_path = self._resolve_raw_ome_path(input_cfg.get("raw_ome"))
                self._sync_active_roi_from_input_paths()
                self._dapi_edit.setText(self._dapi_path or "")
                self._mask_edit.setText(self._mask_path or "")
                self._fusion_edit.setText(self._fusion_zarr_path or "")
                self._channel_source_edit.setText(self._corrected_zarr_path or "")
                self._raw_ome_edit.setText(self._raw_ome_path or "")
                self._refresh_channel_sources()
                self._load_thumbnail()
            else:
                self._show_input_error(
                    f'Step3 input not found.\nPlease check Step2 outputs.\n\n'
                    f'Searched in: {output_dir}\n'
                    f'Expected full-WSI files:\n'
                    f'  global_dapi.ome.tiff\n'
                    f'  global_mask.ome.tiff'
                )
        except Exception:
            tb = traceback.format_exc()
            print(f"[Step3] set_output_dir failed:\n{tb}")
            self._show_input_error(
                "Step3 input not found.\nPlease check Step2 outputs."
            )

    def _find_roi_inputs(self, output_dir):
        summary_path = os.path.join(output_dir, "segmentation_meta.json")
        print(f"[Step3] segmentation_meta exists={os.path.exists(summary_path)}")
        summary_meta = None
        if os.path.exists(summary_path):
            try:
                with open(summary_path, "r", encoding="utf-8") as f:
                    summary_meta = json.load(f)
            except Exception:
                print(f"[Step3] failed to read segmentation_meta.json:\n{traceback.format_exc()}")

        summary_roi = None
        if isinstance(summary_meta, dict):
            rois_meta = summary_meta.get("rois") or []
            if rois_meta:
                summary_roi = rois_meta[0]
                roi_name_hint = str(summary_roi.get("roi_name") or "ROI_1")
                for item in rois_meta:
                    if str(item.get("roi_name") or "") == roi_name_hint:
                        summary_roi = item
                        break
                meta_paths = sorted(glob.glob(os.path.join(output_dir, f"segmentation_meta_{roi_name_hint}.json")))
            else:
                meta_paths = []
        else:
            meta_paths = sorted(glob.glob(os.path.join(output_dir, "segmentation_meta_*.json")))

        if not meta_paths and summary_roi is None:
            roi_cfg = os.path.join(output_dir, "roi_config.json")
            if not os.path.exists(roi_cfg):
                return None
            tile_dirs = sorted(glob.glob(os.path.join(output_dir, "tiles_*")))
            if not tile_dirs:
                return None
            roi_name = os.path.basename(tile_dirs[0]).replace("tiles_", "", 1)
            meta = {"roi_name": roi_name, "tile_dir": tile_dirs[0], "bbox": None}
        elif summary_roi is not None and not meta_paths:
            meta = dict(summary_roi)
            roi_name = str(meta.get("roi_name") or "ROI_1")
            meta.setdefault("tile_dir", meta.get("tiles_dir") or os.path.join(output_dir, f"tiles_{roi_name}"))
            meta.setdefault("bbox", meta.get("bbox_fullres"))
        else:
            with open(meta_paths[0], "r", encoding="utf-8") as f:
                meta = json.load(f)
            if summary_roi is not None:
                merged = dict(summary_roi)
                merged.update(meta)
                for key in ("fused_zarr_path", "input_zarr", "source_zarr"):
                    if summary_roi.get(key):
                        merged[key] = summary_roi.get(key)
                if summary_roi.get("tiles_dir"):
                    merged["tile_dir"] = summary_roi.get("tiles_dir")
                if summary_roi.get("bbox_fullres"):
                    merged["bbox"] = summary_roi.get("bbox_fullres")
                meta = merged

        roi_name = str(meta.get("roi_name") or "ROI_1")
        tile_dir = meta.get("tile_dir") or os.path.join(output_dir, f"tiles_{roi_name}")
        if not os.path.isabs(tile_dir):
            tile_dir = os.path.join(output_dir, tile_dir)
        global_dapi = meta.get("global_dapi") or os.path.join(output_dir, f"global_dapi_{roi_name}.ome.tiff")
        global_mask = meta.get("ome_tiff") or os.path.join(output_dir, f"global_mask_{roi_name}.ome.tiff")
        if not os.path.isabs(global_dapi):
            global_dapi = os.path.join(output_dir, global_dapi)
        if not os.path.isabs(global_mask):
            global_mask = os.path.join(output_dir, global_mask)
        if not os.path.exists(global_dapi) or not os.path.exists(global_mask):
            self._show_input_error(
                "Step3 input not found.\n\n"
                f"Missing ROI-level DAPI/mask:\n{global_dapi}\n{global_mask}"
            )
            return None
        fused_zarr = self._find_fusion_zarr(output_dir, roi_name, meta, summary_meta)
        print(f"[Step3] metadata fused_zarr_path={meta.get('fused_zarr_path') or meta.get('input_zarr') or meta.get('source_zarr')}")
        print(f"[Step3] fused_zarr exists={bool(fused_zarr and os.path.exists(fused_zarr))}")
        if fused_zarr:
            try:
                z = zarr.open(fused_zarr, mode="r")
                print(f"[Step3] fused_zarr shape={getattr(z, 'shape', None)}")
            except Exception:
                print(f"[Step3] fused_zarr shape=<unreadable>")
        dapi_tiles = sorted(glob.glob(os.path.join(tile_dir, "tile_r*_c*_dapi.ome.tiff")))
        mask_tiles = sorted(glob.glob(os.path.join(tile_dir, "tile_r*_c*_raw_mask.ome.tiff")))
        if not dapi_tiles or not mask_tiles:
            self._show_input_error(
                "Step3 input not found.\nPlease check Step2 outputs.\n\n"
                f"Missing ROI tile files in:\n{tile_dir}"
            )
            return None
        tile_stats = meta.get("tile_stats") or []
        if tile_stats:
            n_rows = max(int(t.get("row", 0)) for t in tile_stats) + 1
            n_cols = max(int(t.get("col", 0)) for t in tile_stats) + 1
        else:
            coords = []
            for p in dapi_tiles:
                base = os.path.basename(p)
                try:
                    rc = base.split("_dapi", 1)[0]
                    r = int(rc.split("tile_r", 1)[1].split("_c", 1)[0])
                    c = int(rc.split("_c", 1)[1])
                    coords.append((r, c))
                except Exception:
                    pass
            n_rows = max((r for r, _ in coords), default=0) + 1
            n_cols = max((c for _, c in coords), default=0) + 1
        roi_shape = None
        try:
            with tifffile.TiffFile(global_dapi) as tif:
                p = tif.pages[0]
                roi_shape = [int(p.imagelength), int(p.imagewidth)]
        except Exception:
            pass
        tile_infos = self._build_tile_infos(
            tile_dir,
            roi_shape,
            [n_rows, n_cols],
            tile_stats,
        )
        return {
            "roi_name": roi_name,
            "tiles_dir": tile_dir,
            "bbox": meta.get("bbox"),
            "global_dapi": global_dapi,
            "global_mask": global_mask,
            "fused_zarr": fused_zarr,
            "tile_grid": [n_rows, n_cols],
            "roi_shape": roi_shape,
            "tiles": tile_infos,
        }

    def _build_tile_infos(self, tile_dir, roi_shape, tile_grid, tile_stats):
        n_rows, n_cols = [int(v) for v in tile_grid]
        if n_rows <= 0 or n_cols <= 0:
            return []
        if roi_shape:
            full_h, full_w = [int(v) for v in roi_shape]
        else:
            full_h, full_w = int(self._full_h), int(self._full_w)
        stats_by_rc = {
            (int(t.get("row", 0)), int(t.get("col", 0))): t
            for t in (tile_stats or [])
        }
        tile_h = -(-full_h // n_rows)
        tile_w = -(-full_w // n_cols)
        infos = []
        for row in range(n_rows):
            for col in range(n_cols):
                stat = stats_by_rc.get((row, col), {})
                bbox = stat.get("bbox_local")
                if not bbox:
                    ty0 = row * tile_h
                    ty1 = min(ty0 + tile_h, full_h)
                    tx0 = col * tile_w
                    tx1 = min(tx0 + tile_w, full_w)
                    bbox = [ty0, ty1, tx0, tx1]
                dapi_path = stat.get("dapi_path") or os.path.join(
                    tile_dir, f"tile_r{row}_c{col}_dapi.ome.tiff"
                )
                mask_path = stat.get("mask_path") or os.path.join(
                    tile_dir, f"tile_r{row}_c{col}_raw_mask.ome.tiff"
                )
                if not os.path.isabs(dapi_path):
                    dapi_path = os.path.join(tile_dir, dapi_path)
                if not os.path.isabs(mask_path):
                    mask_path = os.path.join(tile_dir, mask_path)
                infos.append({
                    "row": row,
                    "col": col,
                    "bbox_local": [int(v) for v in bbox],
                    "dapi_path": dapi_path,
                    "mask_path": mask_path,
                })
        return infos

    def _input_files_meta_path(self, output_dir=None):
        base = output_dir or self._output_dir or OUTPUT_DIR
        return os.path.join(base, "step3_input_files.json")

    def _save_input_files_config(self):
        path = self._input_files_meta_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "dapi": self._dapi_path or self._dapi_edit.text().strip(),
                        "mask": self._mask_path or self._mask_edit.text().strip(),
                        "fusion_source": self._fusion_zarr_path or self._fusion_edit.text().strip(),
                        "channel_source": self._corrected_zarr_path or self._channel_source_edit.text().strip(),
                        "raw_ome": self._raw_ome_path or self._raw_ome_edit.text().strip(),
                    },
                    f,
                    indent=2,
                )
        except Exception:
            print(f"[Step3] failed to save input file config:\n{traceback.format_exc()}")

    def _load_input_files_config(self, output_dir):
        path = self._input_files_meta_path(output_dir)
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            print(f"[Step3] loaded input file config={path}")
            return cfg if isinstance(cfg, dict) else {}
        except Exception:
            print(f"[Step3] failed to load input file config:\n{traceback.format_exc()}")
            return {}

    def _load_manual_fusion_source(self, output_dir, roi_name=None):
        input_cfg = self._load_input_files_config(output_dir)
        manual_from_inputs = input_cfg.get("fusion_source")
        if manual_from_inputs:
            print(f"[Step3] loaded manual fusion source={manual_from_inputs}")
            return manual_from_inputs
        return None

    def _browse_project(self):
        path = QFileDialog.getExistingDirectory(
            self,
            "Select project directory",
            self._project_dir or OUTPUT_DIR,
        )
        if path:
            self.load_project(path)

    def _refresh_project_selection(self):
        path = self._project_edit.text().strip() if hasattr(self, "_project_edit") else ""
        if path:
            self.load_project(path)
        elif self._project_dir:
            self.load_project(self._project_dir)
        else:
            QMessageBox.information(self, "Step3", "Select a project directory first.")

    def _view_resolved_paths(self):
        text = "\n\n".join([
            f"Run ID:\n{self._run_combo.currentData() if hasattr(self, '_run_combo') else ''}",
            f"DAPI:\n{self._dapi_path or ''}",
            f"MASK:\n{self._mask_path or ''}",
            f"Fusion:\n{self._fusion_zarr_path or ''}",
            f"Corrected channels:\n{self._corrected_zarr_path or ''}",
            f"Raw OME:\n{self._raw_ome_path or ''}",
            f"segmentation_meta:\n{self._selected_meta_path or ''}",
        ])
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Resolved Step3 Paths")
        dlg.resize(900, 520)
        lay = QVBoxLayout(dlg)
        edit = QtWidgets.QTextEdit()
        edit.setReadOnly(True)
        edit.setPlainText(text)
        lay.addWidget(edit)
        btn = QPushButton("Close")
        btn.clicked.connect(dlg.accept)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(btn)
        lay.addLayout(row)
        dlg.exec_()

    def load_project(self, project_dir):
        self._project_dir = os.path.abspath(project_dir)
        if hasattr(self, "_project_edit"):
            self._project_edit.setText(self._project_dir)
        idx = load_json(project_roi_index_path(self._project_dir), {}) or {}
        entries = list(idx.get("rois") or [])
        if not entries:
            rois_dir = os.path.join(self._project_dir, "rois")
            if os.path.isdir(rois_dir):
                for roi_id in sorted(os.listdir(rois_dir)):
                    rdir = os.path.join(rois_dir, roi_id)
                    manifest = load_json(roi_manifest_path(rdir), {}) or {}
                    if not manifest:
                        continue
                    entries.append({
                        "roi_id": manifest.get("roi_id") or roi_id,
                        "display_name": manifest.get("display_name") or roi_id,
                        "created_at": manifest.get("created_at", ""),
                        "status": manifest.get("status", "active"),
                        "manifest": os.path.relpath(roi_manifest_path(rdir), self._project_dir),
                    })
        self._roi_entries = entries
        print(f"[Step3] project loaded={self._project_dir}")
        if hasattr(self, "_roi_combo"):
            self._roi_combo.blockSignals(True)
            self._roi_combo.clear()
            for item in entries:
                label = f"{item.get('display_name') or item.get('roi_id')}  ({item.get('roi_id')})"
                self._roi_combo.addItem(label, item.get("roi_id"))
            active = idx.get("active_roi_id")
            if active:
                i = self._roi_combo.findData(active)
                if i >= 0:
                    self._roi_combo.setCurrentIndex(i)
            self._roi_combo.blockSignals(False)
        if entries:
            self._resolved_paths_lbl.setText(f"Loaded {len(entries)} ROI(s).")
            self._on_project_roi_changed()
        else:
            self._resolved_paths_lbl.setText("No ROI found in project.")

    def _current_selected_roi_id(self):
        if not hasattr(self, "_roi_combo"):
            return ""
        return self._roi_combo.currentData() or ""

    def _on_project_roi_changed(self, *_args):
        roi_id = self._current_selected_roi_id()
        if not roi_id or not self._project_dir:
            return
        self._active_roi_name = None
        rdir = os.path.join(self._project_dir, "rois", roi_id)
        manifest = load_json(roi_manifest_path(rdir), {}) or {}
        self._active_roi_name = manifest.get("display_name") or "ROI_1"
        self._active_bbox = manifest.get("bbox_fullres")
        self._segmentation_runs = {}
        roi_idx = load_json(roi_index_path(rdir), {}) or {}
        runs = dict(roi_idx.get("segmentation_runs") or {})
        runs.update(self._scan_roi_segmentation_runs(rdir))
        self._segmentation_runs = runs
        print(f"[Step3] selected roi_id={roi_id}")
        print(f"[Step3] loaded roi_index={roi_index_path(rdir)}")
        print(f"[Step3] available segmentation runs={list(runs)}")
        if hasattr(self, "_resolved_paths_lbl"):
            self._resolved_paths_lbl.setText(f"Loaded ROI {self._active_roi_name}.")
        self._populate_run_combo()

    def _scan_roi_segmentation_runs(self, roi_dir):
        found = {}
        search_dirs = [
            os.path.join(roi_dir, "step2", "segmentation_runs"),
            os.path.join(roi_dir, "step2", "segmentation_results"),
        ]
        print("[Step3] searching results in:")
        for d in search_dirs:
            print(f"  {d}")
            if not os.path.isdir(d):
                continue
            for name in sorted(os.listdir(d)):
                run_dir = os.path.join(d, name)
                if not os.path.isdir(run_dir):
                    continue
                seg_meta_path = os.path.join(run_dir, "segmentation_meta.json")
                run_meta_path = os.path.join(run_dir, "run_metadata.json")
                if not (os.path.exists(seg_meta_path) or os.path.exists(run_meta_path)):
                    continue
                meta = load_json(seg_meta_path, {}) or load_json(run_meta_path, {}) or {}
                config = load_json(os.path.join(run_dir, "run_segmentation_params.json"), {}) or {}
                run_id = str(meta.get("run_id") or meta.get("result_id") or name)
                method = str(meta.get("method") or config.get("method") or self._method_from_run_id(name))
                created = str(meta.get("created_at") or self._created_from_run_id(name))
                found[run_id] = {
                    "run_id": run_id,
                    "method": method,
                    "created_at": created,
                    "path": os.path.relpath(run_dir, roi_dir),
                    "status": "done",
                    "meta_path": os.path.relpath(seg_meta_path if os.path.exists(seg_meta_path) else run_meta_path, roi_dir),
                }
                print(f"[Step3] run metadata={seg_meta_path if os.path.exists(seg_meta_path) else run_meta_path}")
        print(f"[Step3] found run dirs={list(found)}")
        return found

    def _run_display_label(self, run):
        method = str(run.get("method") or "")
        created = str(run.get("created_at") or "")
        if not created:
            created = self._created_from_run_id(run.get("run_id"))
        return f"{method} — {created[:16].replace('T', ' ')}"

    @staticmethod
    def _method_from_run_id(run_id):
        text = str(run_id or "")
        m = re.match(r"(?:seg_)?\d{8}_\d{6}_(.+)", text)
        return m.group(1) if m else text

    @staticmethod
    def _created_from_run_id(run_id):
        m = re.search(r"(\d{8}_\d{6})", str(run_id or ""))
        return m.group(1) if m else ""

    def _populate_run_combo(self):
        if not hasattr(self, "_run_combo"):
            return
        roi_id = self._current_selected_roi_id()
        if not roi_id or not self._project_dir:
            return
        rdir = os.path.join(self._project_dir, "rois", roi_id)
        roi_idx = load_json(roi_index_path(rdir), {}) or {}
        runs = dict(roi_idx.get("segmentation_runs") or {})
        runs.update(self._scan_roi_segmentation_runs(rdir))
        if self._latest_only_chk.isChecked():
            wanted = set((roi_idx.get("latest_by_method") or {}).values())
            runs = {rid: run for rid, run in runs.items() if rid in wanted}
        items = sorted(runs.items(), key=lambda kv: kv[1].get("created_at", ""), reverse=True)
        active = roi_idx.get("active_segmentation_run")
        self._run_combo.blockSignals(True)
        self._run_combo.clear()
        for rid, run in items:
            self._run_combo.addItem(self._run_display_label(run), rid)
        if active:
            i = self._run_combo.findData(active)
            if i >= 0:
                self._run_combo.setCurrentIndex(i)
        self._run_combo.blockSignals(False)
        print(f"[Step3] result dropdown count={len(items)}")
        if items:
            self._on_segmentation_run_changed()
        elif hasattr(self, "_resolved_paths_lbl"):
            self._resolved_paths_lbl.setText("No segmentation results found for selected ROI.")

    def _on_segmentation_run_changed(self, *_args):
        roi_id = self._current_selected_roi_id()
        run_id = self._run_combo.currentData() if hasattr(self, "_run_combo") else ""
        if not roi_id or not run_id or not self._project_dir:
            return
        rdir = os.path.join(self._project_dir, "rois", roi_id)
        run = self._segmentation_runs.get(run_id) or (load_json(roi_index_path(rdir), {}) or {}).get("segmentation_runs", {}).get(run_id, {})
        run_path = run.get("path") or os.path.join("step2", "segmentation_runs", run_id)
        run_dir = run_path if os.path.isabs(run_path) else os.path.join(rdir, run_path)
        meta_path = os.path.join(run_dir, "segmentation_meta.json")
        meta = load_json(meta_path, {}) or {}
        if not meta:
            meta_path = os.path.join(run_dir, "run_metadata.json")
            meta = load_json(meta_path, {}) or {}
        if not self._apply_segmentation_meta(rdir, roi_id, run_id, meta, meta_path):
            return
        self._stop_loaders()
        self._clear_region_cache()
        self._patch_channel_cache.clear()
        self._load_thumbnail()

    def _apply_segmentation_meta(self, roi_dir, roi_id, run_id, meta, meta_path):
        manifest = load_json(roi_manifest_path(roi_dir), {}) or {}
        paths = dict(meta.get("paths") or {})
        if meta.get("roi_id") and str(meta.get("roi_id")) != str(roi_id):
            QMessageBox.warning(self, "Step3", "Segmentation result ROI id mismatch.")
            print("[Step3] validation passed=False")
            return False
        bbox_meta = meta.get("roi_bbox_fullres")
        bbox_manifest = manifest.get("bbox_fullres")
        if bbox_meta and bbox_manifest and [int(v) for v in bbox_meta] != [int(v) for v in bbox_manifest]:
            QMessageBox.warning(self, "Step3", "Segmentation result ROI bbox mismatch.")
            print("[Step3] validation passed=False")
            return False
        self._mode = "roi"
        self._output_dir = os.path.dirname(meta_path)
        self._selected_run_meta = meta
        self._selected_meta_path = meta_path
        self._active_roi_name = meta.get("roi_display_name") or manifest.get("display_name") or "ROI_1"
        self._active_bbox = bbox_manifest or bbox_meta
        run_dir = os.path.dirname(meta_path)
        self._dapi_path = (
            paths.get("dapi_ome")
            or meta.get("global_dapi")
            or meta.get("dapi_path")
            or self._first_existing_run_file(run_dir, [
                f"global_dapi_{roi_id}.ome.tiff",
                f"global_dapi_{self._active_roi_name}.ome.tiff",
                "global_dapi.ome.tiff",
            ])
        )
        self._mask_path = (
            paths.get("mask_ome")
            or meta.get("global_mask")
            or meta.get("mask_path")
            or meta.get("ome_tiff")
            or self._first_existing_run_file(run_dir, [
                f"global_mask_{roi_id}.ome.tiff",
                f"global_mask_{self._active_roi_name}.ome.tiff",
                "global_mask.ome.tiff",
            ])
        )
        self._fusion_zarr_path = (
            paths.get("fusion_zarr")
            or meta.get("fused_zarr_path")
            or meta.get("input_zarr")
            or meta.get("source_zarr")
        )
        if self._fusion_zarr_path and not os.path.isabs(str(self._fusion_zarr_path)):
            self._fusion_zarr_path = os.path.join(os.path.dirname(meta_path), str(self._fusion_zarr_path))
        if not (self._fusion_zarr_path and os.path.exists(self._fusion_zarr_path)):
            self._fusion_zarr_path = self._find_fusion_zarr(
                os.path.dirname(meta_path), self._active_roi_name, meta, meta
            )
        self._fused_zarr_path = self._fusion_zarr_path
        self._corrected_zarr_path = paths.get("corrected_channels_zarr") or os.path.join(roi_dir, "step0", "corrected_channels.zarr")
        self._raw_ome_path = paths.get("raw_ome") or manifest.get("source_ome")
        if not (self._dapi_path and os.path.exists(self._dapi_path) and self._mask_path and os.path.exists(self._mask_path)):
            msg = "Selected segmentation run has no DAPI/MASK outputs.\nPlease rerun Step2 or check run output."
            self._resolved_paths_lbl.setText(msg)
            QMessageBox.warning(self, "Step3", msg)
            print("[Step3] validation passed=False")
            print(f"[Step3] missing dapi={self._dapi_path}")
            print(f"[Step3] missing mask={self._mask_path}")
            return False
        dapi_shape = None
        mask_shape = None
        try:
            with tifffile.TiffFile(self._dapi_path) as tif:
                p = tif.pages[0]
                dapi_shape = (int(p.imagelength), int(p.imagewidth))
        except Exception:
            print(f"[Step3] failed to probe dapi shape:\n{traceback.format_exc()}")
        try:
            with tifffile.TiffFile(self._mask_path) as tif:
                p = tif.pages[0]
                mask_shape = (int(p.imagelength), int(p.imagewidth))
        except Exception:
            print(f"[Step3] failed to probe mask shape:\n{traceback.format_exc()}")
        self._roi_global_ome_available = bool(dapi_shape and mask_shape)
        self._patch_source = "roi_global_ome" if self._roi_global_ome_available else "tile_fallback"
        if dapi_shape:
            self._full_h, self._full_w = dapi_shape
        self._tile_grid = meta.get("tile_grid")
        self._active_tiles_dir = meta.get("tiles_dir") or meta.get("tile_dir") or os.path.join(self._output_dir, "tile_masks", self._active_roi_name)
        tile_stats = meta.get("tile_stats") or ((meta.get("rois") or [{}])[0].get("tile_stats") if meta.get("rois") else [])
        roi_shape = meta.get("roi_shape") or meta.get("image_shape")
        if not roi_shape and dapi_shape:
            roi_shape = list(dapi_shape)
        if not roi_shape and self._dapi_path and os.path.exists(self._dapi_path):
            try:
                with tifffile.TiffFile(self._dapi_path) as tif:
                    p = tif.pages[0]
                    roi_shape = [int(p.imagelength), int(p.imagewidth)]
            except Exception:
                pass
        if self._roi_global_ome_available:
            self._tile_infos = []
        elif not self._tile_grid and tile_stats:
            self._tile_grid = [
                max(int(t.get("row", 0)) for t in tile_stats) + 1,
                max(int(t.get("col", 0)) for t in tile_stats) + 1,
            ]
        if self._roi_global_ome_available:
            pass
        elif self._active_tiles_dir and self._tile_grid:
            self._tile_infos = self._build_tile_infos(self._active_tiles_dir, roi_shape, self._tile_grid, tile_stats)
        else:
            self._tile_infos = []
        for edit, value in (
            (getattr(self, "_dapi_edit", None), self._dapi_path),
            (getattr(self, "_mask_edit", None), self._mask_path),
            (getattr(self, "_fusion_edit", None), self._fusion_zarr_path),
            (getattr(self, "_channel_source_edit", None), self._corrected_zarr_path),
            (getattr(self, "_raw_ome_edit", None), self._raw_ome_path),
        ):
            if edit is not None:
                edit.setText(value or "")
        self._refresh_channel_sources()
        method_label = str(meta.get("display_name") or meta.get("method") or "segmentation result")
        self._resolved_paths_lbl.setText(
            f"Input status: Loaded {self._active_roi_name} | {method_label} | ready"
        )
        print(f"[Step3] selected run_id={run_id}")
        print(f"[Step3] dapi_ome={self._dapi_path}")
        print(f"[Step3] mask_ome={self._mask_path}")
        print(f"[Step3] dapi_shape={dapi_shape}")
        print(f"[Step3] mask_shape={mask_shape}")
        print(f"[Step3] patch_source={self._patch_source}")
        print(f"[Step3] tile_infos ignored={self._roi_global_ome_available}")
        print(f"[Step3] method={meta.get('method')}")
        print(f"[Step3] resolved fusion_zarr={self._fusion_zarr_path}")
        print(f"[Step3] fusion exists={bool(self._fusion_zarr_path and os.path.exists(self._fusion_zarr_path))}")
        print(f"[Step3] resolved dapi={self._dapi_path}")
        print(f"[Step3] resolved mask={self._mask_path}")
        print(f"[Step3] resolved fusion={self._fusion_zarr_path}")
        print(f"[Step3] resolved corrected={self._corrected_zarr_path}")
        print(f"[Step3] resolved raw_ome={self._raw_ome_path}")
        print("[Step3] validation passed=True")
        return True

    @staticmethod
    def _first_existing_run_file(run_dir, names):
        for name in names:
            path = os.path.join(run_dir, name)
            if os.path.exists(path) or os.path.islink(path):
                return path
        return ""

    def _resolve_channel_source_path(self, manual_path=None):
        candidates = []
        if manual_path:
            candidates.append(manual_path)
        if self._corrected_zarr_path:
            candidates.append(self._corrected_zarr_path)
        base_dir = self._output_dir or OUTPUT_DIR
        if base_dir:
            candidates.extend([
                os.path.join(base_dir, "corrected_channels.zarr"),
                os.path.join(os.path.dirname(base_dir), "corrected_channels.zarr"),
            ])
            roi_dir = self._infer_roi_dir_from_output(base_dir)
            if roi_dir:
                candidates.extend([
                    os.path.join(roi_dir, "step0", "corrected_channels.zarr"),
                    os.path.join(roi_dir, "step1", "corrected_channels.zarr"),
                ])
        for cand in candidates:
            if cand and os.path.exists(cand):
                return os.path.abspath(cand)
        return None

    def _infer_roi_dir_from_output(self, output_dir=None):
        cur = os.path.abspath(output_dir or self._output_dir or OUTPUT_DIR)
        while cur and cur != os.path.dirname(cur):
            if os.path.exists(os.path.join(cur, "roi_manifest.json")):
                return cur
            cur = os.path.dirname(cur)
        return None

    def _resolve_raw_ome_path(self, manual_path=None):
        candidates = []
        if manual_path:
            candidates.append(manual_path)
        if self._raw_ome_path:
            candidates.append(self._raw_ome_path)
        if self._loader is not None and getattr(self._loader, "filepath", None):
            candidates.append(self._loader.filepath)
        base_dir = self._output_dir or OUTPUT_DIR
        if base_dir:
            for name in ("fusion_config.json", "correction_config.json", "roi_config.json", "step0_output.json"):
                meta_path = os.path.join(base_dir, name)
                if not os.path.exists(meta_path):
                    continue
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    for key in ("ome_tiff", "ome_tiff_path", "raw_ome"):
                        if isinstance(meta, dict) and meta.get(key):
                            candidates.append(meta.get(key))
                except Exception:
                    pass
            candidates.extend(glob.glob(os.path.join(base_dir, "*.ome.tif")))
            candidates.extend(glob.glob(os.path.join(base_dir, "*.ome.tiff")))
            parent = os.path.dirname(base_dir)
            candidates.extend(glob.glob(os.path.join(parent, "*.ome.tif")))
            candidates.extend(glob.glob(os.path.join(parent, "*.ome.tiff")))
            roi_dir = self._infer_roi_dir_from_output(base_dir)
            if roi_dir:
                for meta_name in ("roi_manifest.json", os.path.join("step0", "step0_roi_result.json")):
                    meta_path = os.path.join(roi_dir, meta_name)
                    if os.path.exists(meta_path):
                        try:
                            with open(meta_path, "r", encoding="utf-8") as f:
                                meta = json.load(f)
                            for key in ("source_ome", "raw_ome_path"):
                                if isinstance(meta, dict) and meta.get(key):
                                    candidates.append(meta.get(key))
                        except Exception:
                            pass
        candidates.append(OME_TIFF_FILE)
        for cand in candidates:
            if not cand:
                continue
            base = os.path.basename(str(cand)).lower()
            if base.startswith(("global_", "tile_")):
                continue
            if os.path.exists(cand):
                return os.path.abspath(cand)
        return None

    def _validate_fusion_zarr(self, path):
        if not path or not os.path.exists(path):
            raise ValueError("Selected fusion zarr path does not exist.")
        z = zarr.open(path, mode="r")
        shape = getattr(z, "shape", None)
        if not shape or len(shape) != 3 or int(shape[2]) not in (2, 3):
            raise ValueError(
                "Fusion zarr must have shape H x W x 2 or H x W x 3."
            )
        return z, tuple(int(v) for v in shape)

    def _channel_config_path(self):
        return os.path.join(self._output_dir or OUTPUT_DIR, "step3_channel_overlay_config.json")

    def _default_channel_color(self, ch):
        u = str(ch).upper()
        if "FUSION" in u:
            return "#ff3333"
        if "DAPI" in u:
            return "#3366ff"
        if "NUC" in u:
            return "#3366ff"
        if "CK19" in u or "EPCAM" in u or "PANCK" in u or "KRT" in u:
            return "#ff0000"
        if "CD3" in u or "CD45" in u or "CD68" in u or "CD4" in u or "CD8" in u:
            return "#00ff00"
        if "CD31" in u or "PECAM" in u:
            return "#00ffff"
        return "#ffffff"

    def _default_channel_visible(self, ch):
        if str(ch) in ("__layer_dapi__", "__layer_fusion__"):
            return True
        return False

    def _default_channel_settings(self, ch):
        return {
            "visible": self._default_channel_visible(ch),
            "color": self._default_channel_color(ch),
            "opacity": 1.0,
            "contrast": [1.0, 99.5],
        }

    @staticmethod
    def _roi_name_from_path(path):
        if not path:
            return None
        # Match display names like ROI_2 without accidentally parsing immutable
        # ROI ids such as roi_20260512_153012_a8f3 as ROI_20260512.
        m = re.search(r"(?<![A-Za-z0-9])ROI[_-]?(\d{1,4})(?![A-Za-z0-9])", str(path), flags=re.IGNORECASE)
        if not m:
            return None
        return f"ROI_{int(m.group(1))}"

    def _sync_active_roi_from_input_paths(self):
        """Keep ROI-specific input files, corrected zarr groups, and bbox aligned."""
        inferred = None
        for path in (self._dapi_path, self._mask_path, self._fusion_zarr_path, self._fused_zarr_path):
            inferred = self._roi_name_from_path(path)
            if inferred:
                break
        if inferred and inferred != self._active_roi_name:
            print(f"[Step3] active ROI inferred from input files: {inferred} (was {self._active_roi_name})")
            self._active_roi_name = inferred

        bbox = self._lookup_active_roi_bbox()
        if bbox and self._active_bbox != bbox:
            print(f"[Step3] active_bbox resolved for {self._active_roi_name}: {bbox}")
            self._active_bbox = bbox

    def _zarr_group_names(self, root):
        try:
            if hasattr(root, "group_keys"):
                return [str(k) for k in root.group_keys()]
            return [str(k) for k in root.keys() if not hasattr(root[k], "shape")]
        except Exception:
            return []

    def _lookup_active_roi_bbox(self):
        if not self._active_roi_name or not self._corrected_zarr_path or not os.path.exists(self._corrected_zarr_path):
            bbox = self._lookup_active_roi_bbox_from_roi_config()
            return bbox
        try:
            root = zarr.open(self._corrected_zarr_path, mode="r")
            if self._active_roi_name in root:
                attrs = root[self._active_roi_name].attrs
                bbox = attrs.get("bbox_fullres") or attrs.get("bbox")
                if bbox and len(bbox) == 4:
                    return [int(v) for v in bbox]
        except Exception:
            print(f"[Step3] failed to sync active ROI from corrected zarr:\n{traceback.format_exc()}")
        return self._lookup_active_roi_bbox_from_roi_config()

    def _lookup_active_roi_bbox_from_roi_config(self):
        if not self._active_roi_name:
            return None
        base = self._output_dir or OUTPUT_DIR
        candidates = [
            os.path.join(base, "roi_config.json"),
            os.path.join(os.path.dirname(base), "roi_config.json") if base else None,
        ]
        for path in candidates:
            if not path or not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                rois = cfg.get("rois") if isinstance(cfg, dict) else cfg
                for roi in rois or []:
                    if str(roi.get("name") or roi.get("roi_name") or "") != self._active_roi_name:
                        continue
                    bbox = roi.get("bbox_fullres") or roi.get("bbox")
                    if bbox and len(bbox) == 4:
                        print(f"[Step3] active_bbox from roi_config={path}")
                        return [int(v) for v in bbox]
            except Exception:
                print(f"[Step3] failed to read ROI bbox from {path}:\n{traceback.format_exc()}")
        return None

    def _is_canonical_dapi_channel(self, ch):
        name = str(ch or "").strip().lower()
        return name == "dapi" or "dapi" in name or "nuc" in name

    def _patch_dapi_channel_array(self):
        if self._patch_dapi_rgb is None:
            return None
        dapi = np.asarray(self._patch_dapi_rgb)
        if dapi.ndim == 3:
            dapi = dapi[:, :, 0]
        dapi = dapi.astype(np.float32, copy=False)
        if dapi.size and float(np.nanmax(dapi)) > 1.0:
            dapi = dapi / 255.0
        return dapi

    def _target_patch_shape(self):
        if self._mask_labels is not None:
            return tuple(int(v) for v in self._mask_labels.shape[:2])
        if self._patch_dapi_rgb is not None:
            return tuple(int(v) for v in self._patch_dapi_rgb.shape[:2])
        return None

    def _match_channel_shape(self, ch, arr, source):
        arr = np.asarray(arr, dtype=np.float32)
        target = self._target_patch_shape()
        mask_shape = None if self._mask_labels is None else tuple(int(v) for v in self._mask_labels.shape[:2])
        match = target is None or tuple(arr.shape[:2]) == target
        print(f"[Step3] channel crop shape={tuple(arr.shape[:2])}")
        print(f"[Step3] mask shape={mask_shape}")
        print(f"[Step3] shape match={match}")
        if target is None or match:
            return arr

        th, tw = target
        out = np.zeros((th, tw), dtype=np.float32)
        mh = min(th, int(arr.shape[0]))
        mw = min(tw, int(arr.shape[1]))
        if mh > 0 and mw > 0:
            out[:mh, :mw] = arr[:mh, :mw]
        print(
            f"[Step3] warning: channel shape adjusted for {ch} from "
            f"{tuple(arr.shape[:2])} to {target} source={source}"
        )
        return out

    def _load_channel_config(self):
        path = self._channel_config_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            self._background_mode = cfg.get("background_mode", self._background_mode)
            saved = cfg.get("channels") or {}
            for ch, st in saved.items():
                cur = self._channel_settings.setdefault(ch, self._default_channel_settings(ch))
                for key in ("color", "opacity", "contrast", "visible"):
                    if key in st:
                        cur[key] = st[key]
        except Exception:
            print(f"[Step3] failed to load channel overlay config:\n{traceback.format_exc()}")

    def _save_channel_config(self):
        try:
            with open(self._channel_config_path(), "w", encoding="utf-8") as f:
                json.dump({
                    "background_mode": self._background_mode,
                    "channels": self._channel_settings,
                }, f, indent=2)
        except Exception:
            print(f"[Step3] failed to save channel overlay config:\n{traceback.format_exc()}")

    def _refresh_channel_sources(self):
        corrected_channels = []
        raw_channels = []
        self._channel_sources = {}
        old_corrected_path = self._corrected_zarr_path
        self._corrected_zarr_path = self._resolve_channel_source_path(
            self._channel_source_edit.text().strip() if hasattr(self, "_channel_source_edit") else None
        )
        if old_corrected_path != self._corrected_zarr_path:
            self._corrected_zarr = None
        self._raw_ome_path = self._resolve_raw_ome_path(
            self._raw_ome_edit.text().strip() if hasattr(self, "_raw_ome_edit") else None
        )
        self._sync_active_roi_from_input_paths()

        if self._corrected_zarr_path and os.path.exists(self._corrected_zarr_path):
            try:
                root = zarr.open(self._corrected_zarr_path, mode="r")
                self._corrected_zarr = root
                roi_name = self._active_roi_name or "ROI_1"
                root_mode = str(root.attrs.get("mode", "")).lower()
                if roi_name in root and self._array_names(root[roi_name]):
                    group = root[roi_name]
                    corrected_channels.extend(self._array_names(group))
                elif root_mode != "roi_only":
                    corrected_channels.extend(self._array_names(root))
                else:
                    print(f"[Step3] corrected zarr ROI group not found for active_roi={roi_name}")
            except Exception:
                print(f"[Step3] failed to inspect corrected zarr:\n{traceback.format_exc()}")

        raw_loader = self._raw_loader
        if self._raw_ome_path and (
            raw_loader is None or getattr(raw_loader, "filepath", None) != self._raw_ome_path
        ):
            try:
                raw_loader = OMETIFFLoader(self._raw_ome_path)
                self._raw_loader = raw_loader
            except Exception:
                print(f"[Step3] failed to load Raw OME channels:\n{traceback.format_exc()}")
                raw_loader = None
        elif raw_loader is None:
            raw_loader = self._loader

        if raw_loader is not None:
            try:
                raw_channels.extend(raw_loader.channel_names())
            except Exception:
                print(f"[Step3] failed to inspect raw OME channels:\n{traceback.format_exc()}")

        self._corrected_channel_names = sorted(set(map(str, corrected_channels)))
        self._raw_channel_names = sorted(set(map(str, raw_channels)))
        merged = sorted(set(self._corrected_channel_names) | set(self._raw_channel_names))
        self._available_channels = merged
        for ch in merged:
            self._channel_sources[ch] = "corrected" if ch in self._corrected_channel_names else "raw"
        for ch in self._available_channels:
            self._channel_settings.setdefault(ch, self._default_channel_settings(ch))
        self._load_channel_config()
        if self._background_mode not in {"DAPI", "Fusion", "Channels"}:
            self._background_mode = "DAPI"
        if hasattr(self, "_bg_mode_combo"):
            self._bg_mode_combo.blockSignals(True)
            self._bg_mode_combo.setCurrentText(self._background_mode if self._background_mode in {"DAPI", "Fusion", "Channels"} else "DAPI")
            self._bg_mode_combo.blockSignals(False)
        if hasattr(self, "_channel_source_edit"):
            self._channel_source_edit.setText(self._corrected_zarr_path or "")
        if hasattr(self, "_raw_ome_edit"):
            self._raw_ome_edit.setText(self._raw_ome_path or "")
        self._rebuild_channel_panel()
        print("[Step3] channel overlay panel initialized")
        print(f"[Step3] active_roi={self._active_roi_name or 'ROI_1'}")
        print(f"[Step3] fusion_source={self._fusion_zarr_path}")
        print(f"[Step3] channel_source={self._corrected_zarr_path}")
        print(f"[Step3] raw_ome={self._raw_ome_path}")
        print(f"[Step3] corrected_channels={self._corrected_channel_names}")
        print(f"[Step3] raw_channels count={len(self._raw_channel_names)}")
        print(f"[Step3] merged_channels count={len(self._available_channels)}")

    def _array_names(self, group):
        try:
            if hasattr(group, "array_keys"):
                return [str(k) for k in group.array_keys()]
            return [str(k) for k in group.keys() if hasattr(group[k], "shape")]
        except Exception:
            return []

    def _rebuild_channel_panel(self):
        if not hasattr(self, "_channel_lay"):
            return
        while self._channel_lay.count():
            item = self._channel_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                while item.layout().count():
                    sub = item.layout().takeAt(0)
                    if sub.widget():
                        sub.widget().deleteLater()
        self._channel_rows.clear()

        def _add_row(key, label, source):
            st = self._channel_settings.setdefault(key, self._default_channel_settings(key))
            row = QHBoxLayout()
            cb = QCheckBox()
            cb.setChecked(bool(st.get("visible", False)))
            cb.stateChanged.connect(lambda _v, name=key: self._on_channel_visibility_changed(name))
            row.addWidget(cb)
            name_lbl = QLabel(label)
            name_lbl.setMinimumWidth(80)
            row.addWidget(name_lbl)
            src_lbl = QLabel(source)
            src_lbl.setFixedWidth(90)
            src_lbl.setStyleSheet("color:#888;font-size:9px;")
            row.addWidget(src_lbl)
            color_btn = QPushButton()
            color_btn.setFixedSize(24, 18)
            color_btn.setStyleSheet(f"background:{st.get('color', '#ffffff')};border:1px solid #777;")
            color_btn.clicked.connect(lambda _=False, name=key: self._choose_channel_color(name))
            row.addWidget(color_btn)
            op = QSlider(Qt.Horizontal)
            op.setRange(0, 200)
            op.setValue(int(float(st.get("opacity", 1.0)) * 100))
            op.setFixedWidth(90)
            op.valueChanged.connect(lambda _v, name=key: self._on_channel_opacity_changed(name))
            row.addWidget(op)
            auto_btn = QPushButton("Auto")
            auto_btn.setFixedWidth(44)
            auto_btn.clicked.connect(lambda _=False, name=key: self._auto_channel_contrast(name))
            row.addWidget(auto_btn)
            self._channel_lay.addLayout(row)
            self._channel_rows[key] = {
                "checkbox": cb,
                "color_btn": color_btn,
                "opacity": op,
                "label": label,
                "source": source,
            }

        self._apply_background_defaults()
        dapi_lbl = QLabel("DAPI Overlay")
        dapi_lbl.setStyleSheet("color:#56b6c2;font-weight:bold;font-size:10px;padding-top:4px;")
        self._channel_lay.addWidget(dapi_lbl)
        _add_row("__layer_dapi__", "DAPI", "canonical")

        fusion_lbl = QLabel("Fusion Overlay")
        fusion_lbl.setStyleSheet("color:#56b6c2;font-weight:bold;font-size:10px;padding-top:4px;")
        self._channel_lay.addWidget(fusion_lbl)
        _add_row("__layer_fusion__", "Fusion", "fused ch0")
        if hasattr(self, "_chk_fusion"):
            self._show_fusion = bool(
                self._channel_settings.setdefault("__layer_fusion__", self._default_channel_settings("__layer_fusion__")).get("visible", False)
            )
            self._chk_fusion.setEnabled(bool(self._patch_fusion_available or self._fusion_zarr_path))
            self._chk_fusion.setToolTip("" if self._chk_fusion.isEnabled() else "Fusion source not available.")
            self._chk_fusion.blockSignals(True)
            self._chk_fusion.setChecked(self._show_fusion)
            self._chk_fusion.blockSignals(False)

        marker_lbl = QLabel("Marker Channels")
        marker_lbl.setStyleSheet("color:#aaa;font-weight:bold;font-size:10px;padding-top:6px;")
        self._channel_lay.addWidget(marker_lbl)
        marker_channels = self._marker_channels()
        if not marker_channels:
            msg = QLabel("No channels found.\nSet Channel Source / Raw OME.")
            msg.setWordWrap(True)
            msg.setStyleSheet("color:#aaa;font-size:10px;")
            self._channel_lay.addWidget(msg)
            self._channel_lay.addStretch()
            self._log_overlay_rows(marker_channels)
            return
        for ch in marker_channels:
            _add_row(ch, ch, self._channel_sources.get(ch, "raw"))
        self._channel_lay.addStretch()
        self._log_overlay_rows(marker_channels)

    def _apply_background_defaults(self):
        dapi = self._channel_settings.setdefault("__layer_dapi__", self._default_channel_settings("__layer_dapi__"))
        fusion = self._channel_settings.setdefault("__layer_fusion__", self._default_channel_settings("__layer_fusion__"))
        mode = str(self._background_mode)
        if getattr(self, "_overlay_defaults_mode", None) == mode:
            self._show_fusion = bool(fusion.get("visible", False))
            return
        if self._background_mode == "DAPI":
            dapi["visible"] = True
            fusion["visible"] = False
        elif self._background_mode == "Fusion":
            dapi["visible"] = True
            fusion["visible"] = True
        self._overlay_defaults_mode = mode
        self._show_fusion = bool(fusion.get("visible", False))

    def _marker_channels(self):
        return [ch for ch in self._available_channels if not self._is_canonical_dapi_channel(ch)]

    def _log_overlay_rows(self, marker_channels=None):
        rows = [v.get("label", k) for k, v in self._channel_rows.items()]
        print(f"[Step3] background_mode={self._background_mode}")
        print(f"[Step3] overlay rows={rows}")
        print("[Step3] overlay sections:")
        print("  DAPI shared=True")
        print("  Fusion shared=True")
        print(f"  marker_channels_count={len(marker_channels if marker_channels is not None else self._marker_channels())}")
        for key, label in (("__layer_dapi__", "DAPI"), ("__layer_fusion__", "Fusion")):
            if key in self._channel_rows:
                st = self._channel_settings.setdefault(key, self._default_channel_settings(key))
                print(
                    f"[Step3] {label} color={st.get('color')} "
                    f"intensity={float(st.get('opacity', 1.0))}"
                )

    def _make_channel_overlay_panel(self):
        ch_box = QGroupBox('Channel Overlay')
        ch_box.setStyleSheet(
            'QGroupBox{border:1px solid #56b6c2;border-radius:5px;'
            'margin-top:4px;font-weight:bold;color:#56b6c2;font-size:11px;}'
        )
        ch_lay = QVBoxLayout(ch_box)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel('Background:'))
        self._bg_mode_combo = QtWidgets.QComboBox()
        self._bg_mode_combo.addItems(['DAPI', 'Fusion', 'Channels'])
        self._bg_mode_combo.setCurrentText(self._background_mode)
        self._bg_mode_combo.currentTextChanged.connect(self._on_background_mode_changed)
        mode_row.addWidget(self._bg_mode_combo)
        btn_clear = QPushButton('Clear All')
        btn_clear.clicked.connect(self._reset_channel_visibility)
        mode_row.addWidget(btn_clear)
        btn_reset_colors = QPushButton('Reset Colors')
        btn_reset_colors.clicked.connect(self._reset_channel_colors)
        mode_row.addWidget(btn_reset_colors)
        mode_row.addStretch()
        ch_lay.addLayout(mode_row)

        self._channel_scroll = QScrollArea()
        self._channel_scroll.setWidgetResizable(True)
        self._channel_scroll.setMinimumHeight(220)
        self._channel_scroll.setMaximumHeight(420)
        self._channel_w = QWidget()
        self._channel_lay = QVBoxLayout(self._channel_w)
        self._channel_lay.setContentsMargins(2, 2, 2, 2)
        self._channel_lay.setSpacing(2)
        self._channel_scroll.setWidget(self._channel_w)
        ch_lay.addWidget(self._channel_scroll)
        self._rebuild_channel_panel()
        return ch_box

    def _find_fusion_zarr(self, output_dir, roi_name=None, seg_meta=None, summary_meta=None):
        candidates = []

        def _add(path):
            if not path:
                return
            p = str(path)
            if not os.path.isabs(p):
                p = os.path.join(output_dir, p)
            if p not in candidates:
                candidates.append(p)

        manual_path = self._load_manual_fusion_source(output_dir, roi_name)
        if manual_path:
            _add(manual_path)

        if seg_meta:
            for key in ("fused_zarr_path", "fused_zarr", "input_zarr", "source_zarr", "fusion_zarr"):
                _add(seg_meta.get(key))

        if summary_meta:
            if roi_name:
                for item in summary_meta.get("rois") or []:
                    if str(item.get("roi_name") or "") == str(roi_name):
                        for key in ("fused_zarr_path", "input_zarr", "source_zarr"):
                            _add(item.get(key))
            for key in ("fused_zarr_path", "input_zarr", "source_zarr"):
                _add(summary_meta.get(key))

        fusion_meta_path = os.path.join(output_dir, "fusion_meta.json")
        if os.path.exists(fusion_meta_path):
            try:
                with open(fusion_meta_path, "r", encoding="utf-8") as f:
                    fusion_meta = json.load(f)
                regions = fusion_meta.get("regions") or []
                matched = None
                for reg in regions:
                    name = str(reg.get("roi_name") or reg.get("name") or "")
                    if roi_name and name == str(roi_name):
                        matched = reg
                        break
                if matched is None and not roi_name and regions:
                    matched = regions[0]
                if matched is not None:
                    _add(matched.get("zarr_path"))
                for reg in regions:
                    _add(reg.get("zarr_path"))
            except Exception:
                print(f"[Step3] failed to read fusion_meta.json:\n{traceback.format_exc()}")

        _add(os.path.join(output_dir, "fused.zarr"))
        if roi_name:
            _add(os.path.join(output_dir, f"fused_{roi_name}.zarr"))
            _add(os.path.join(output_dir, roi_name, "fused.zarr"))
        roi_dir = self._infer_roi_dir_from_output(output_dir)
        if roi_dir:
            _add(os.path.join(roi_dir, "step1", "fused.zarr"))
            if roi_name:
                _add(os.path.join(roi_dir, "step1", f"fused_{roi_name}.zarr"))
            _add(os.path.join(roi_dir, "step1", "fusion.zarr"))
        _add(os.path.join(output_dir, "fusion.zarr"))

        for cand in candidates:
            exists = os.path.exists(cand)
            print(f"[Step3] fusion candidate={cand} exists={exists}")
            if exists:
                return cand
        print("[Step3] selected fusion_zarr=None")
        return None

    def load_from_paths(self, dapi_path, mask_path):
        """Manual load via Browse buttons."""
        try:
            self._clear_region_cache()
            if not os.path.exists(dapi_path):
                self._show_input_error(f'Step3 input not found.\nPlease check Step2 outputs.\n\nDAPI file not found:\n{dapi_path}')
                return
            if not os.path.exists(mask_path):
                self._show_input_error(f'Step3 input not found.\nPlease check Step2 outputs.\n\nMask file not found:\n{mask_path}')
                return
            self._mode = "full_wsi"
            fusion_path = self._fusion_edit.text().strip()
            if fusion_path:
                try:
                    _z, shape = self._validate_fusion_zarr(fusion_path)
                    self._fusion_zarr_path = os.path.abspath(fusion_path)
                    self._fused_zarr_path = self._fusion_zarr_path
                    print(f"[Step3] manual fusion zarr shape={shape}")
                except Exception as e:
                    QMessageBox.warning(self, 'Invalid fusion source', str(e))
                    return
            else:
                self._fused_zarr_path = None
                self._fusion_zarr_path = None
            self._corrected_zarr_path = self._resolve_channel_source_path(
                self._channel_source_edit.text().strip()
            )
            self._raw_ome_path = self._resolve_raw_ome_path(
                self._raw_ome_edit.text().strip()
            )
            self._dapi_path = dapi_path
            self._mask_path = mask_path
            self._refresh_channel_sources()
            self._save_input_files_config()
            self._load_thumbnail()
        except Exception:
            tb = traceback.format_exc()
            print(f"[Step3] load_from_paths failed:\n{tb}")
            self._show_input_error("Step3 input not found.\nPlease check Step2 outputs.")

    # ── UI construction ───────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        title = QLabel('Step 3 — Segmentation QC Viewer')
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            'font-size:16px;font-weight:bold;color:#eee;'
            'background:#1a1a1a;padding:6px;border-radius:4px;'
        )
        root.addWidget(title)

        # ── Main split ────────────────────────────────────────────────
        split = QSplitter(Qt.Horizontal)

        # ── LEFT: thumbnail + controls ────────────────────────────────
        left_w  = QWidget()
        left_lay = QVBoxLayout(left_w)
        left_lay.setContentsMargins(0, 0, 4, 0)
        left_lay.setSpacing(6)

        # Project / ROI / segmentation run input box
        project_box = QGroupBox('Input')
        project_box.setStyleSheet(
            'QGroupBox{border:1px solid #61afef;border-radius:5px;'
            'margin-top:4px;font-weight:bold;color:#61afef;font-size:11px;}'
        )
        pl = QVBoxLayout(project_box)
        proj_row = QHBoxLayout()
        proj_row.addWidget(QLabel('Project directory:'))
        self._project_edit = QtWidgets.QLineEdit()
        self._project_edit.setPlaceholderText('Select project directory…')
        self._project_edit.setStyleSheet('font-size:10px;')
        proj_row.addWidget(self._project_edit, stretch=1)
        btn_project = QPushButton('Browse')
        btn_project.setFixedWidth(56)
        btn_project.clicked.connect(self._browse_project)
        proj_row.addWidget(btn_project)
        pl.addLayout(proj_row)

        sel_row = QHBoxLayout()
        sel_row.addWidget(QLabel('ROI:'))
        self._roi_combo = QtWidgets.QComboBox()
        self._roi_combo.currentIndexChanged.connect(self._on_project_roi_changed)
        sel_row.addWidget(self._roi_combo, stretch=1)
        sel_row.addWidget(QLabel('Result:'))
        self._run_combo = QtWidgets.QComboBox()
        self._run_combo.currentIndexChanged.connect(self._on_segmentation_run_changed)
        sel_row.addWidget(self._run_combo, stretch=2)
        pl.addLayout(sel_row)

        self._latest_only_chk = QCheckBox('Latest only')
        self._latest_only_chk.setChecked(False)
        self._latest_only_chk.toggled.connect(lambda _v: self._populate_run_combo())
        pl.addWidget(self._latest_only_chk)
        action_row = QHBoxLayout()
        btn_refresh_project = QPushButton('Load / Refresh')
        btn_refresh_project.clicked.connect(self._refresh_project_selection)
        action_row.addWidget(btn_refresh_project)
        btn_view_paths = QPushButton('View resolved paths')
        btn_view_paths.clicked.connect(self._view_resolved_paths)
        action_row.addWidget(btn_view_paths)
        action_row.addStretch()
        pl.addLayout(action_row)
        self._resolved_paths_lbl = QLabel('No project loaded')
        self._resolved_paths_lbl.setWordWrap(True)
        self._resolved_paths_lbl.setStyleSheet('color:#aaa;font-size:10px;')
        pl.addWidget(self._resolved_paths_lbl)
        left_lay.addWidget(project_box)

        dev_btn = QPushButton('Developer options')
        dev_btn.setCheckable(True)
        dev_btn.setChecked(False)
        left_lay.addWidget(dev_btn)

        # Advanced file input box
        file_box = QGroupBox('Advanced Overrides (optional)')
        file_box.setStyleSheet(
            'QGroupBox{border:1px solid #61afef;border-radius:5px;'
            'margin-top:4px;font-weight:bold;color:#61afef;font-size:11px;}'
        )
        file_box.setCheckable(True)
        file_box.setChecked(False)
        file_box.setVisible(False)
        dev_btn.toggled.connect(file_box.setVisible)
        fl = QVBoxLayout(file_box)

        def _file_row(label, attr_edit):
            r = QHBoxLayout()
            r.addWidget(QLabel(label))
            ed = QtWidgets.QLineEdit()
            ed.setPlaceholderText('Path…')
            ed.setStyleSheet('font-size:10px;')
            r.addWidget(ed, stretch=1)
            btn = QPushButton('Browse')
            btn.setFixedWidth(56)
            r.addWidget(btn)
            return r, ed, btn

        r1, self._dapi_edit, btn_dapi = _file_row('DAPI:', '_dapi_edit')
        r2, self._mask_edit, btn_mask = _file_row('MASK:', '_mask_edit')
        r3, self._fusion_edit, btn_fusion = _file_row('Fusion Source:', '_fusion_edit')
        r4, self._channel_source_edit, btn_channel_source = _file_row('Channel Source:', '_channel_source_edit')
        r5, self._raw_ome_edit, btn_raw_ome = _file_row('Raw OME:', '_raw_ome_edit')
        fl.addLayout(r1)
        fl.addLayout(r2)
        fl.addLayout(r3)
        fl.addLayout(r4)
        fl.addLayout(r5)

        btn_dapi.clicked.connect(lambda: self._browse('dapi'))
        btn_mask.clicked.connect(lambda: self._browse('mask'))
        btn_fusion.clicked.connect(lambda: self._browse('fusion'))
        btn_channel_source.clicked.connect(lambda: self._browse('channel_source'))
        btn_raw_ome.clicked.connect(lambda: self._browse('raw_ome'))

        btn_load = QPushButton('▶  Load Files & Thumbnail')
        btn_load.setStyleSheet(
            'QPushButton{background:#255;color:white;border-radius:3px;padding:4px;}'
            'QPushButton:hover{background:#377;}'
        )
        btn_load.clicked.connect(self._manual_load)
        fl.addWidget(btn_load)
        left_lay.addWidget(file_box)

        # The lower-left area is split into the ROI preview and a compact
        # control column, matching the QC workflow: preview, then controls
        # immediately to its right, before the enlarged patch result.
        left_body = QWidget()
        left_body_lay = QHBoxLayout(left_body)
        left_body_lay.setContentsMargins(0, 0, 0, 0)
        left_body_lay.setSpacing(6)

        preview_w = QWidget()
        preview_lay = QVBoxLayout(preview_w)
        preview_lay.setContentsMargins(0, 0, 0, 0)
        preview_lay.setSpacing(4)

        controls_w = QWidget()
        controls_lay = QVBoxLayout(controls_w)
        controls_lay.setContentsMargins(0, 0, 0, 0)
        controls_lay.setSpacing(6)

        # Thumbnail canvas
        preview_lay.addWidget(QLabel(
            '  Thumbnail  '
            '(drag empty area=draw ROI  |  drag rect=move  |  right-click rect=delete  |  dbl-click=reset view)'
        ))

        self._ov_gv = pg.GraphicsLayoutWidget()
        self._ov_gv.setBackground('#111')
        self._ov_gv.setMinimumWidth(320)
        self._ov_vb = self._ov_gv.addViewBox()
        self._ov_vb.setAspectLocked(True)
        self._ov_vb.invertY(True)
        self._ov_vb.setMenuEnabled(False)
        self._ov_vb.setMouseEnabled(x=False, y=False)
        self._ov_img = pg.ImageItem()
        self._ov_vb.addItem(self._ov_img)
        # Draw ROI via mouse events on the viewport
        self._ov_gv.viewport().installEventFilter(self)
        preview_lay.addWidget(self._ov_gv, stretch=1)

        self._thumb_status = QLabel('No files loaded')
        self._thumb_status.setStyleSheet('color:#888;font-size:10px;')
        self._thumb_status.setAlignment(Qt.AlignCenter)
        self._thumb_status.setWordWrap(True)
        preview_lay.addWidget(self._thumb_status)

        # Sampling controls
        samp_box = QGroupBox('ROI / Sampling')
        samp_box.setStyleSheet(
            'QGroupBox{border:1px solid #e5c07b;border-radius:5px;'
            'margin-top:4px;font-weight:bold;color:#e5c07b;font-size:11px;}'
        )
        sl = QVBoxLayout(samp_box)

        # Sub-sampling
        sub_row = QHBoxLayout()
        sub_row.addWidget(QLabel('Sub-sample (1=full res):'))
        self._sub_spin = QtWidgets.QSpinBox()
        self._sub_spin.setRange(1, 16)
        self._sub_spin.setValue(2)
        self._sub_spin.setToolTip(
            '1 = full resolution (slow for large ROIs)\n'
            '2 = half resolution (recommended)\n'
            '4 = quarter resolution (fast)'
        )
        sub_row.addWidget(self._sub_spin)
        sub_row.addStretch()
        sl.addLayout(sub_row)

        # Random sample button
        btn_rnd = QPushButton('🎲  Random ROI')
        btn_rnd.setStyleSheet(
            'QPushButton{background:#333;color:#e5c07b;border:1px solid #e5c07b;'
            'border-radius:3px;padding:4px;font-size:11px;}'
            'QPushButton:hover{background:#443;}'
        )
        btn_rnd.clicked.connect(self._random_roi)
        sl.addWidget(btn_rnd)

        # ROI size spinboxes
        sz_row = QHBoxLayout()
        sz_row.addWidget(QLabel('ROI size (px):'))
        self._roi_h_spin = QtWidgets.QSpinBox()
        self._roi_h_spin.setRange(256, 20000)
        self._roi_h_spin.setValue(2048)
        self._roi_h_spin.setSingleStep(256)
        sz_row.addWidget(self._roi_h_spin)
        sz_row.addWidget(QLabel('×'))
        self._roi_w_spin = QtWidgets.QSpinBox()
        self._roi_w_spin.setRange(256, 20000)
        self._roi_w_spin.setValue(2048)
        self._roi_w_spin.setSingleStep(256)
        sz_row.addWidget(self._roi_w_spin)
        sz_row.addStretch()
        sl.addLayout(sz_row)

        controls_lay.addWidget(self._make_channel_overlay_panel())
        controls_lay.addWidget(samp_box)
        controls_lay.addStretch()

        left_body_lay.addWidget(preview_w, stretch=7)
        left_body_lay.addWidget(controls_w, stretch=3)
        left_lay.addWidget(left_body, stretch=1)
        split.addWidget(left_w)

        # ── RIGHT: ROI zoom viewer ────────────────────────────────────
        right_w  = QWidget()
        right_lay = QVBoxLayout(right_w)
        right_lay.setContentsMargins(4, 0, 0, 0)
        right_lay.setSpacing(4)

        # Toolbar
        toolbar = QHBoxLayout()

        self._roi_info = QLabel('Draw a rectangle on the thumbnail to inspect a region')
        self._roi_info.setStyleSheet('color:#aaa;font-size:11px;')
        self._roi_info.setWordWrap(True)
        toolbar.addWidget(self._roi_info, stretch=1)

        toolbar.addWidget(QLabel('Alpha:'))
        self._alpha_slider = QSlider(Qt.Horizontal)
        self._alpha_slider.setRange(0, 100)
        self._alpha_slider.setValue(int(round(self._mask_alpha * 100)))
        self._alpha_slider.setFixedWidth(110)
        self._alpha_slider.setToolTip('Mask opacity (0% = transparent, 100% = opaque)')
        self._alpha_slider.setStyleSheet(
            'QSlider::groove:horizontal{height:4px;background:#333;border-radius:2px;}'
            'QSlider::handle:horizontal{width:12px;height:12px;margin:-4px 0;'
            'background:#8e8;border-radius:6px;}'
            'QSlider::sub-page:horizontal{background:#4a7;border-radius:2px;}'
        )
        self._alpha_lbl = QLabel(f'{int(round(self._mask_alpha * 100))}%')
        self._alpha_lbl.setFixedWidth(34)
        self._alpha_lbl.setStyleSheet('color:#8e8;font-size:10px;')

        def _on_alpha(v):
            self._mask_alpha = v / 100.0
            self._alpha_lbl.setText(f'{v}%')
            self._render_roi()

        self._alpha_slider.valueChanged.connect(_on_alpha)
        toolbar.addWidget(self._alpha_slider)
        toolbar.addWidget(self._alpha_lbl)

        self._chk_outline = QCheckBox('Show Outline')
        self._chk_outline.setChecked(self._show_outline)
        self._chk_outline.stateChanged.connect(self._on_render_controls_changed)
        toolbar.addWidget(self._chk_outline)

        self._chk_fusion = QCheckBox('Show Fusion')
        self._chk_fusion.setChecked(self._show_fusion)
        self._chk_fusion.stateChanged.connect(self._on_render_controls_changed)
        toolbar.addWidget(self._chk_fusion)

        btn_reset = QPushButton('Reset View')
        btn_reset.setStyleSheet(
            'QPushButton{background:#333;color:#bbb;border:1px solid #555;'
            'border-radius:3px;font-size:11px;padding:3px 8px;}'
            'QPushButton:hover{background:#444;color:#fff;}'
        )
        btn_reset.clicked.connect(self._reset_roi_view)
        toolbar.addWidget(btn_reset)

        hint_lbl = QLabel('Scroll=Zoom  Drag=Pan  Dbl-click=Reset')
        hint_lbl.setStyleSheet('color:#444;font-size:10px;')
        toolbar.addWidget(hint_lbl)

        right_lay.addLayout(toolbar)

        # ROI image canvas
        self._roi_gv = pg.GraphicsLayoutWidget()
        self._roi_gv.setBackground('#0a0a0a')
        self._roi_vb = self._roi_gv.addViewBox()
        self._roi_vb.setAspectLocked(True)
        self._roi_vb.invertY(True)
        self._roi_vb.setMenuEnabled(False)
        self._roi_vb.setMouseEnabled(x=False, y=False)
        self._roi_vb.setLimits(xMin=None, xMax=None, yMin=None, yMax=None,
                               minXRange=None, maxXRange=None,
                               minYRange=None, maxYRange=None)
        self._roi_img = pg.ImageItem()
        self._roi_vb.addItem(self._roi_img)
        self._roi_gv.viewport().installEventFilter(self)
        right_lay.addWidget(self._roi_gv, stretch=1)

        # Status bar below canvas
        bot_bar = QHBoxLayout()
        self._roi_status = QLabel('—')
        self._roi_status.setStyleSheet('color:#888;font-size:10px;')
        self._roi_status.setWordWrap(True)
        bot_bar.addWidget(self._roi_status, stretch=1)

        self._cell_count_lbl = QLabel('')
        self._cell_count_lbl.setStyleSheet(
            'color:#4af;font-size:11px;font-weight:bold;'
        )
        bot_bar.addWidget(self._cell_count_lbl)
        right_lay.addLayout(bot_bar)

        split.addWidget(right_w)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 4)
        root.addWidget(split, stretch=1)

        # ── Bottom nav ────────────────────────────────────────────────
        nav = QHBoxLayout()
        btn_back = QPushButton('← Back to Step 2')
        btn_back.setStyleSheet(
            'QPushButton{color:#fa8;border:1px solid #fa8;'
            'border-radius:4px;padding:6px 16px;}'
            'QPushButton:hover{background:#321;}'
        )
        btn_back.clicked.connect(self.go_back.emit)
        nav.addWidget(btn_back)
        nav.addStretch()
        # REMOVE: redundant navigation button (use top-right Next only)
        root.addLayout(nav)

    # ── Thumbnail loading ─────────────────────────────────────────────

    def _load_thumbnail(self):
        self._thumb_status.setText('Loading thumbnail…')
        self._ov_img.clear()
        self._roi_rect_clear()

        # Probe image dimensions first
        try:
            with tifffile.TiffFile(self._dapi_path) as tif:
                store = tif.aszarr()
                z     = zarr.open(store, mode='r')
                if isinstance(z, zarr.hierarchy.Group):
                    z0 = z[0] if '0' in z else next(iter(z.values()))
                else:
                    z0 = z
                if z0.ndim == 3:
                    self._full_h, self._full_w = z0.shape[1], z0.shape[2]
                else:
                    self._full_h, self._full_w = z0.shape[0], z0.shape[1]
        except Exception as e:
            tb = traceback.format_exc()
            print(f"[Step3] Cannot read DAPI:\n{tb}")
            self._show_input_error("Step3 input not found.\nPlease check Step2 outputs.")
            return

        # Update edit boxes
        self._dapi_edit.setText(self._dapi_path)
        self._mask_edit.setText(self._mask_path)

        # Start background loader
        if self._thumb_loader and self._thumb_loader.isRunning():
            self._thumb_loader.stop()
            self._thumb_loader.wait(3000)
            if self._thumb_loader.isRunning():
                self._thumb_status.setText("Previous thumbnail loader is still stopping…")
                return

        self._thumb_loader = _ThumbLoader(self._dapi_path, self._OV_DS)
        self._thumb_loader.done.connect(self._on_thumb_loaded)
        self._thumb_loader.error.connect(self._on_thumb_error)
        self._thumb_loader.finished.connect(lambda thread=self._thumb_loader: self._on_thumb_finished(thread))
        self._thumb_loader.start()

    def _on_thumb_loaded(self, arr):
        self._thumb_arr = arr
        self._thumb_h, self._thumb_w = arr.shape[:2]
        self._ov_img.setImage(arr, autoLevels=False, levels=[0.0, 1.0])
        self._ov_vb.setRange(
            QRectF(0, 0, self._thumb_w, self._thumb_h), padding=0.02
        )
        self._thumb_status.setText(
            f'Thumbnail {self._thumb_h}×{self._thumb_w} px  '
            f'(1/{self._OV_DS})   '
            f'Full image: {self._full_h:,}×{self._full_w:,} px\n'
            f'Drag on thumbnail to select a QC region'
        )
        if self._mode == "roi":
            self._thumb_status.setText(
                f'ROI mode: {self._active_roi_name}  '
                f'ROI preview {self._full_h:,}×{self._full_w:,} px '
                f'(1/{self._OV_DS})\n'
                f'Draw a QC patch inside the ROI preview'
            )

    def _on_thumb_error(self, msg):
        print(f"[Step3] Thumbnail error:\n{msg}")
        self._show_input_error("Step3 input not found.\nPlease check Step2 outputs.")

    def _on_thumb_finished(self, thread):
        if self._thumb_loader is thread:
            self._thumb_loader = None

    # ── ROI rectangle management ──────────────────────────────────────

    def _roi_rect_clear(self):
        if self._roi_rect is not None:
            scene = self._ov_vb.scene()
            if scene and self._roi_rect.scene() == scene:
                scene.removeItem(self._roi_rect)
            self._roi_rect = None

    def _roi_rect_set(self, x_ds, y_ds, w_ds, h_ds):
        """Place / replace the ROI rectangle (thumbnail-coord units)."""
        self._roi_rect_clear()
        rect = _RoiRect(x_ds, y_ds, w_ds, h_ds,
                        self._OV_DS, self._full_h, self._full_w)
        rect.roi_changed.connect(self._on_roi_changed)
        self._ov_vb.addItem(rect)
        self._roi_rect = rect
        # Emit immediately to update info label
        rect._emit()

    def _on_roi_changed(self, y0, y1, x0, x1):
        self._roi = (y0, y1, x0, x1)
        h = y1 - y0
        w = x1 - x0
        sub = self._sub_spin.value()
        self._roi_info.setText(
            f'ROI  y=[{y0:,}:{y1:,}]  x=[{x0:,}:{x1:,}]  '
            f'{h:,}×{w:,} px   sub={sub}×  '
            f'→  {h//sub:,}×{w//sub:,} px loaded'
        )
        # Debounce: wait 400 ms of inactivity before loading (avoids firing
        # on every pixel while the ROI rect is being dragged)
        self._load_debounce.start(400)

    # ── Region loading ────────────────────────────────────────────────

    def _load_region(self):
        if self._roi is None or self._dapi_path is None:
            return
        y0, y1, x0, x1 = self._roi
        sub = self._sub_spin.value()
        full_h, full_w = int(self._full_h or 0), int(self._full_w or 0)
        if full_h > 0 and full_w > 0:
            y0 = max(0, min(int(y0), full_h))
            y1 = max(0, min(int(y1), full_h))
            x0 = max(0, min(int(x0), full_w))
            x1 = max(0, min(int(x1), full_w))
        if y1 <= y0 or x1 <= x0:
            self._roi_status.setText("Patch outside image bounds.")
            print(f"[Step3] patch local bbox={[y0, y1, x0, x1]}")
            print(f"[Step3] full_h={self._full_h}")
            print(f"[Step3] full_w={self._full_w}")
            print("[Step3] Patch outside image bounds.")
            return

        self._clear_region_cache(clear_image=False)
        self._patch_channel_cache.clear()
        self._last_patch_bbox = (y0, y1, x0, x1)
        self._roi_status.setText('Loading…')
        self._cell_count_lbl.setText('')

        if self._region_loader and self._region_loader.isRunning():
            self._region_loader.stop()
            self._region_loader.wait(3000)
            if self._region_loader.isRunning():
                self._roi_status.setText("Previous region loader is still stopping…")
                return
        dapi_path = self._dapi_path
        mask_path = self._mask_path
        read_y0, read_y1, read_x0, read_x1 = y0, y1, x0, x1
        fusion_roi = (y0, y1, x0, x1)
        tile_infos = None
        if self._mode == "roi":
            print(f"[Step3] patch local bbox={[y0, y1, x0, x1]}")
            print(f"[Step3] full_h={self._full_h}")
            print(f"[Step3] full_w={self._full_w}")
            print(f"[Step3] active_roi={self._active_roi_name}")
            print(f"[Step3] active_bbox global={self._active_bbox}")
            if self._roi_global_ome_available:
                tile_infos = []
                print("[Step3] using direct ROI OME crop")
            else:
                tile_infos = self._intersect_patch_tiles(y0, y1, x0, x1)
                if not tile_infos:
                    self._roi_status.setText("Patch outside ROI.")
                    return
                print(f"[Step3] patch intersects n_tiles={len(tile_infos)}")
        else:
            print(f"[Step3] patch local bbox={[y0, y1, x0, x1]}")
        print(f"[Step3] reading fusion crop from={self._fused_zarr_path}")
        print(f"[Step3] patch bbox={[y0, y1, x0, x1]}")
        print(f"[Step3] background_mode={self._background_mode}")

        self._region_loader = _RegionLoader(
            dapi_path, mask_path,
            read_y0, read_y1, read_x0, read_x1, sub=sub,
            fused_zarr_path=self._fused_zarr_path,
            fusion_roi=fusion_roi,
            tile_infos=tile_infos,
        )
        self._region_loader.done.connect(self._on_region_loaded)
        self._region_loader.error.connect(self._on_region_error)
        self._region_loader.finished.connect(lambda thread=self._region_loader: self._on_region_finished(thread))
        self._region_loader.start()

    def _on_region_loaded(self, dapi_rgb, fusion_rgb, mask_labels, n_cells, h, w, fusion_source):
        self._patch_dapi_rgb = np.asarray(dapi_rgb, dtype=np.uint8)
        self._patch_fusion_rgb = (
            None if fusion_rgb is None else np.asarray(fusion_rgb, dtype=np.uint8)
        )
        self._patch_fusion_source = str(fusion_source or "unavailable")
        self._patch_fusion_available = self._patch_fusion_rgb is not None
        self._mask_labels = np.asarray(mask_labels, dtype=np.uint32)
        self._chk_fusion.blockSignals(True)
        self._chk_fusion.setEnabled(True)
        self._chk_fusion.setToolTip("")
        self._chk_fusion.setChecked(self._show_fusion)
        self._chk_fusion.blockSignals(False)
        sub = self._sub_spin.value()
        if n_cells > 0:
            self._cell_count_lbl.setText(f'Cells in ROI: {n_cells:,}')
        else:
            self._cell_count_lbl.setText('No cells detected.')
        self._roi_status.setText(f'{h:,}×{w:,} px  (sub ×{sub})')
        print("[Step3] patch loaded")
        print(f"[Step3] patch cells={n_cells}")
        print("[Step3] dapi available=True")
        print(f"[Step3] fusion available={self._patch_fusion_available}")
        print(f"[Step3] rendering background={self._current_background_kind()}")
        print(f"[Step3] cells={n_cells}")
        print(f"[Step3] alpha={self._mask_alpha:.2f}")
        print(f"[Step3] outline={self._show_outline}")
        print(f"[Step3] fusion={self._show_fusion}")
        print(f"[Step3] fusion_rgb source={self._patch_fusion_source}")
        if not self._patch_fusion_available:
            print("[Step3] fusion source unavailable, using DAPI fallback")
            if self._show_fusion:
                self._roi_status.setText(
                    f'{h:,}×{w:,} px  (sub ×{sub})  |  Fusion Source not set; showing DAPI.'
                )
        if self._patch_fusion_rgb is not None:
            print(f"[Step3] fusion crop shape={self._patch_fusion_rgb.shape}")
            print(f"[Step3] fusion_rgb shape={self._patch_fusion_rgb.shape}")
            print(
                f"[Step3] fusion_rgb min/max="
                f"{int(self._patch_fusion_rgb.min())}/{int(self._patch_fusion_rgb.max())}"
            )
        print(f"[Step3] dapi_rgb shape={self._patch_dapi_rgb.shape}")
        print(f"[Step3] dapi_rgb min/max={int(self._patch_dapi_rgb.min())}/{int(self._patch_dapi_rgb.max())}")
        print(f"[Step3] dapi shape={self._patch_dapi_rgb.shape[:2]}")
        print(f"[Step3] mask shape={self._mask_labels.shape}")
        print(f"[Step3] show_fusion={self._show_fusion}")
        print(f"[Step3] mask cells={n_cells}")
        self._load_visible_patch_channels()
        self._render_roi(reset_view=True)

    def _locate_patch_tile(self, y0, y1, x0, x1):
        if not self._active_tiles_dir or not self._tile_grid:
            return None
        n_rows, n_cols = [int(v) for v in self._tile_grid]
        if n_rows <= 0 or n_cols <= 0:
            return None
        tile_h = -(-self._full_h // n_rows)
        tile_w = -(-self._full_w // n_cols)
        row0 = min(n_rows - 1, max(0, y0 // tile_h))
        row1 = min(n_rows - 1, max(0, (y1 - 1) // tile_h))
        col0 = min(n_cols - 1, max(0, x0 // tile_w))
        col1 = min(n_cols - 1, max(0, (x1 - 1) // tile_w))
        if row0 != row1 or col0 != col1:
            return None
        tile_y0 = row0 * tile_h
        tile_y1 = min(tile_y0 + tile_h, self._full_h)
        tile_x0 = col0 * tile_w
        tile_x1 = min(tile_x0 + tile_w, self._full_w)
        dapi_path = os.path.join(self._active_tiles_dir, f"tile_r{row0}_c{col0}_dapi.ome.tiff")
        mask_path = os.path.join(self._active_tiles_dir, f"tile_r{row0}_c{col0}_raw_mask.ome.tiff")
        if not os.path.exists(dapi_path) or not os.path.exists(mask_path):
            self._roi_status.setText("Step3 input not found.")
            print(f"[Step3] missing tile files:\n{dapi_path}\n{mask_path}")
            return None
        return row0, col0, tile_y0, tile_y1, tile_x0, tile_x1, dapi_path, mask_path

    def _intersect_patch_tiles(self, y0, y1, x0, x1):
        tiles = self._tile_infos or self._build_tile_infos(
            self._active_tiles_dir,
            [self._full_h, self._full_w],
            self._tile_grid or [0, 0],
            [],
        )
        hits = []
        for tile in tiles:
            ty0, ty1, tx0, tx1 = [int(v) for v in tile["bbox_local"]]
            iy0 = max(y0, ty0)
            iy1 = min(y1, ty1)
            ix0 = max(x0, tx0)
            ix1 = min(x1, tx1)
            if iy1 <= iy0 or ix1 <= ix0:
                continue
            if not os.path.exists(tile.get("dapi_path", "")) or not os.path.exists(tile.get("mask_path", "")):
                self._roi_status.setText("Missing tile input.")
                QMessageBox.warning(self, "Missing tile input", "Missing tile input.")
                print(
                    "[Step3] Missing tile input.\n"
                    f"dapi={tile.get('dapi_path')}\n"
                    f"mask={tile.get('mask_path')}"
                )
                return []
            hits.append(tile)
        return hits

    def _on_region_error(self, msg):
        print(f"[Step3] Region load error:\n{msg}")
        self._roi_status.setText(
            "Step3 input not found.\nPlease check Step2 outputs."
        )

    def _on_region_finished(self, thread):
        if self._region_loader is thread:
            self._region_loader = None

    def _clear_region_cache(self, clear_image=True):
        self._patch_dapi_rgb = None
        self._patch_fusion_rgb = None
        self._patch_fusion_source = None
        self._patch_fusion_available = False
        self._mask_labels = None
        self._patch_channel_cache.clear()
        if clear_image and hasattr(self, "_roi_img"):
            self._roi_img.clear()

    @staticmethod
    def _overlay_mask_on_dapi(dapi_rgb, mask):
        """Compatibility wrapper: Step3 rendering is centralized in mask_renderer."""
        return render_mask_overlay(dapi_rgb, mask)

    def _render_roi(self, reset_view=False):
        if self._patch_dapi_rgb is None:
            return
        background = self._current_background_rgb()
        if self._mask_labels is None:
            display = background
        else:
            display = render_mask_overlay(
                background,
                self._mask_labels,
                alpha=self._mask_alpha,
                show_outline=self._show_outline,
                show_fusion=True,
            )
        first = (self._roi_img.image is None)
        self._roi_img.setImage(display, autoLevels=False)
        if first or reset_view:
            self._roi_vb.autoRange()

    def _reset_roi_view(self):
        if hasattr(self, "_roi_vb") and self._roi_vb is not None:
            self._roi_vb.autoRange()

    def _on_render_controls_changed(self, *_):
        self._show_outline = bool(self._chk_outline.isChecked())
        self._show_fusion = bool(self._chk_fusion.isChecked())
        fusion_st = self._channel_settings.setdefault("__layer_fusion__", self._default_channel_settings("__layer_fusion__"))
        fusion_st["visible"] = self._show_fusion
        row = self._channel_rows.get("__layer_fusion__")
        if row:
            cb = row.get("checkbox")
            if cb is not None:
                cb.blockSignals(True)
                cb.setChecked(self._show_fusion)
                cb.blockSignals(False)
        dapi_st = self._channel_settings.setdefault("__layer_dapi__", self._default_channel_settings("__layer_dapi__"))
        print(f"[Step3] show_fusion={self._show_fusion}")
        if self._background_mode == "Channels":
            print(f"[Step3] Channels mode fusion overlay={self._show_fusion}")
        print(f"[Step3] fusion visible={self._show_fusion}")
        print(f"[Step3] dapi visible={bool(dapi_st.get('visible', True))}")
        print(f"[Step3] DAPI color unchanged={dapi_st.get('color', '#3366ff')}")
        self._save_channel_config()
        self._render_roi(reset_view=False)

    def _on_background_mode_changed(self, mode):
        self._background_mode = str(mode)
        self._save_channel_config()
        self._rebuild_channel_panel()
        self._load_visible_patch_channels()
        print(f"[Step3] background_mode={self._background_mode}")
        if self._background_mode == "Channels":
            selected = [
                ch for ch in self._marker_channels()
                if self._channel_settings.get(ch, {}).get("visible", False)
            ]
            print(f"[Step3] selected_channels={selected}")
            print(f"[Step3] Channels mode fusion overlay={self._show_fusion}")
        print("[Step3] rerender only")
        self._render_roi(reset_view=False)

    def _on_channel_visibility_changed(self, ch):
        row = self._channel_rows.get(ch)
        if not row:
            return
        val = bool(row["checkbox"].isChecked())
        self._channel_settings.setdefault(ch, self._default_channel_settings(ch))["visible"] = val
        if ch == "__layer_fusion__":
            self._show_fusion = val
            if hasattr(self, "_chk_fusion"):
                self._chk_fusion.blockSignals(True)
                self._chk_fusion.setChecked(val)
                self._chk_fusion.blockSignals(False)
        self._save_channel_config()
        print(f"[Step3] channel visibility changed: {ch}={val}")
        print(f"[Step3] channel toggled: {ch}={val}")
        if val and ch not in self._patch_channel_cache:
            self._load_patch_channel(ch)
        print("[Step3] rerender only")
        self._render_roi(reset_view=False)

    def _on_channel_opacity_changed(self, ch):
        row = self._channel_rows.get(ch)
        if not row:
            return
        self._channel_settings.setdefault(ch, self._default_channel_settings(ch))["opacity"] = row["opacity"].value() / 100.0
        self._save_channel_config()
        print("[Step3] rerender only")
        self._render_roi(reset_view=False)

    def _choose_channel_color(self, ch):
        cur = QtGui.QColor(self._channel_settings.get(ch, {}).get("color", "#ffffff"))
        color = QtWidgets.QColorDialog.getColor(cur, self, f"Channel color: {ch}")
        if not color.isValid():
            return
        hex_color = color.name()
        self._channel_settings.setdefault(ch, self._default_channel_settings(ch))["color"] = hex_color
        row = self._channel_rows.get(ch)
        if row:
            row["color_btn"].setStyleSheet(f"background:{hex_color};border:1px solid #777;")
        self._save_channel_config()
        print(f"[Step3] channel color changed: {ch}={hex_color}")
        print("[Step3] rerender only")
        self._render_roi(reset_view=False)

    def _auto_channel_contrast(self, ch):
        arr = self._patch_channel_cache.get(ch)
        if arr is None and ch in ("__layer_dapi__", "__layer_fusion__"):
            arr = self._layer_array_for_key(ch)
        if arr is not None:
            nz = arr[np.isfinite(arr)]
            if nz.size:
                self._channel_settings.setdefault(ch, self._default_channel_settings(ch))["contrast"] = [
                    float(np.percentile(nz, 1.0)),
                    float(np.percentile(nz, 99.5)),
                ]
        else:
            self._channel_settings.setdefault(ch, self._default_channel_settings(ch))["contrast"] = [1.0, 99.5]
        self._save_channel_config()
        self._render_roi(reset_view=False)

    def _layer_array_for_key(self, key):
        if key == "__layer_dapi__":
            return self._dapi_layer_array()
        if key == "__layer_fusion__":
            fusion, _dapi, _source = self._fusion_layer_arrays()
            return fusion
        return None

    def _reset_channel_visibility(self):
        for ch in self._available_channels:
            self._channel_settings.setdefault(ch, self._default_channel_settings(ch))["visible"] = False
        self._rebuild_channel_panel()
        self._save_channel_config()
        self._load_visible_patch_channels()
        self._render_roi(reset_view=False)

    def _reset_channel_colors(self):
        for ch in self._available_channels:
            self._channel_settings.setdefault(ch, self._default_channel_settings(ch))["color"] = self._default_channel_color(ch)
        self._rebuild_channel_panel()
        self._save_channel_config()
        self._render_roi(reset_view=False)

    def _current_background_rgb(self):
        rgb = self._render_layer_stack()
        return rgb if rgb is not None else self._patch_dapi_rgb

    def _current_background_kind(self):
        if self._background_mode == "DAPI":
            return "dapi"
        if self._background_mode == "Fusion":
            return "fusion" if self._patch_fusion_available else "dapi"
        if self._background_mode == "Channels" and self._available_channels:
            return "channels"
        return "dapi"

    def _hex_to_rgb(self, color):
        q = QtGui.QColor(str(color))
        if not q.isValid():
            q = QtGui.QColor("#ffffff")
        return np.array([q.red(), q.green(), q.blue()], dtype=np.float32) / 255.0

    def _normalize_channel_for_display(self, ch, arr):
        st = self._channel_settings.setdefault(ch, self._default_channel_settings(ch))
        contrast = st.get("contrast", [1.0, 99.5])
        a = np.asarray(arr, dtype=np.float32)
        if len(contrast) == 2 and max(contrast) <= 100.0:
            nz = a[np.isfinite(a)]
            if nz.size:
                lo, hi = np.percentile(nz, [float(contrast[0]), float(contrast[1])])
            else:
                lo, hi = 0.0, 1.0
        else:
            lo, hi = float(contrast[0]), float(contrast[1])
        if hi <= lo:
            mx = float(np.nanmax(a)) if a.size else 0.0
            if mx > 0:
                return np.ones_like(a, dtype=np.float32)
            hi = lo + 1.0
        return np.clip((a - lo) / (hi - lo), 0.0, 1.0)

    def _colorize_layer(self, key, arr, label, source):
        if arr is None:
            return None
        st = self._channel_settings.setdefault(key, self._default_channel_settings(key))
        if not st.get("visible", False):
            return None
        norm = self._normalize_channel_for_display(key, arr)
        color = self._hex_to_rgb(st.get("color", "#ffffff"))
        intensity = float(np.clip(st.get("opacity", 1.0), 0.0, 2.0))
        print(
            f"[Step3] {label} source={source} min/max="
            f"{float(np.nanmin(arr)) if np.size(arr) else 0.0}/"
            f"{float(np.nanmax(arr)) if np.size(arr) else 0.0}"
        )
        return norm[:, :, None] * color[None, None, :] * intensity

    def _compose_layer_rgb(self, layer_specs):
        target = self._target_patch_shape()
        if target is None:
            return None
        canvas = np.zeros((target[0], target[1], 3), dtype=np.float32)
        rendered = []
        for key, label, source, arr in layer_specs:
            layer = self._colorize_layer(key, arr, label, source)
            if layer is None:
                continue
            if layer.shape[:2] != target:
                layer = self._match_rgb_shape(layer, target, label)
            canvas += layer
            rendered.append(label)
        rgb = np.clip(canvas * 255.0, 0, 255).astype(np.uint8)
        print(f"[Step3] render layers={rendered}")
        print(f"[Step3] output rgb min/max={int(rgb.min())}/{int(rgb.max())}")
        return rgb

    @staticmethod
    def _match_rgb_shape(arr, target, label):
        th, tw = target
        out = np.zeros((th, tw, 3), dtype=np.float32)
        mh = min(th, int(arr.shape[0]))
        mw = min(tw, int(arr.shape[1]))
        if mh > 0 and mw > 0:
            out[:mh, :mw, :] = arr[:mh, :mw, :]
        print(f"[Step3] warning: layer shape adjusted for {label} to {target}")
        return out

    def _dapi_layer_array(self):
        arr = self._patch_dapi_channel_array()
        return None if arr is None else arr * 255.0

    def _fusion_layer_arrays(self):
        if self._patch_fusion_rgb is None:
            return None, self._dapi_layer_array(), "canonical_dapi"
        fusion_rgb = np.asarray(self._patch_fusion_rgb, dtype=np.float32)
        fusion = fusion_rgb[:, :, 0]
        if fusion_rgb.shape[2] > 2 and np.nanmax(fusion_rgb[:, :, 2]) > 0:
            dapi = fusion_rgb[:, :, 2]
            dapi_source = "fused_zarr_ch1"
        else:
            dapi = self._dapi_layer_array()
            dapi_source = "canonical_dapi"
        if np.size(fusion) and float(np.nanmax(fusion)) <= 0:
            print("[Step3] Fusion channel 0 is empty.")
        return fusion, dapi, dapi_source

    def _base_layer_specs(self):
        specs = [("__layer_dapi__", "DAPI", "canonical_dapi", self._dapi_layer_array())]
        fusion, dapi, dapi_source = self._fusion_layer_arrays()
        if fusion is None and self._channel_settings.get("__layer_fusion__", {}).get("visible", False):
            print("[Step3] Fusion source not available.")
            if hasattr(self, "_roi_status"):
                self._roi_status.setText("Fusion source not available. Falling back to DAPI.")
        specs.append(("__layer_fusion__", "Fusion", "fused_zarr_ch0", fusion))
        return specs

    def _render_layer_stack(self):
        if self._patch_dapi_rgb is None:
            return None
        target = self._target_patch_shape() or self._patch_dapi_rgb.shape[:2]
        canvas = np.zeros((target[0], target[1], 3), dtype=np.float32)
        rendered_layers = self._add_layer_specs_to_canvas(canvas, self._base_layer_specs(), target)
        visible = []
        for ch in self._marker_channels():
            st = self._channel_settings.get(ch, {})
            if not st.get("visible", False):
                continue
            arr = self._patch_channel_cache.get(ch)
            if arr is None:
                continue
            if arr.shape[:2] != target:
                arr = self._match_channel_shape(ch, arr, self._channel_sources.get(ch, "raw"))
            norm = self._normalize_channel_for_display(ch, arr)
            color = self._hex_to_rgb(st.get("color", "#ffffff"))
            opacity = float(np.clip(st.get("opacity", 1.0), 0.0, 2.0))
            canvas += norm[:, :, None] * color[None, None, :] * opacity
            visible.append(ch)
        rgb = np.clip(canvas * 255.0, 0, 255).astype(np.uint8)
        print(f"[Step3] background_mode={self._background_mode}")
        print(f"[Step3] selected marker channels={visible}")
        print(f"[Step3] show_fusion={self._show_fusion}")
        print(f"[Step3] fusion source={self._patch_fusion_source}")
        print(f"[Step3] render layers={rendered_layers + visible}")
        print(f"[Step3] rendered channels: {visible}")
        print(f"[Step3] rendered multichannel overlay shape={rgb.shape}")
        return rgb

    def _add_layer_specs_to_canvas(self, canvas, layer_specs, target):
        rendered = []
        for key, label, source, arr in layer_specs:
            layer = self._colorize_layer(key, arr, label, source)
            if layer is None:
                continue
            if layer.shape[:2] != target:
                layer = self._match_rgb_shape(layer, target, label)
            canvas += layer
            rendered.append(label)
        return rendered

    def _load_visible_patch_channels(self):
        if self._last_patch_bbox is None or self._background_mode != "Channels":
            return
        visible = [
            ch for ch in self._marker_channels()
            if self._channel_settings.get(ch, {}).get("visible", False)
        ]
        print(f"[Step3] available_channels={self._available_channels}")
        print(f"[Step3] visible_channels={visible}")
        for ch in visible:
            if ch not in self._patch_channel_cache:
                self._load_patch_channel(ch)

    def _load_patch_channel(self, ch):
        if self._last_patch_bbox is None:
            return
        y0, y1, x0, x1 = self._last_patch_bbox
        sub = self._sub_spin.value() if hasattr(self, "_sub_spin") else 1
        print(f"[Step3] loading channel={ch}")
        print(f"[Step3] loading channel crop: {ch}")
        self._sync_active_roi_from_input_paths()
        print(f"[Step3] active_roi={self._active_roi_name}")
        print(f"[Step3] active_bbox global={self._active_bbox}")
        print(f"[Step3] patch_local={[y0, y1, x0, x1]}")
        arr = None
        source = None

        if self._is_canonical_dapi_channel(ch):
            arr = self._patch_dapi_channel_array()
            if arr is not None:
                source = "canonical_step3_dapi"
                print(f"[Step3] channel={ch} source={source}")
                self._patch_channel_cache[ch] = self._match_channel_shape(ch, arr, source)
                return

        if self._corrected_zarr_path and os.path.exists(self._corrected_zarr_path):
            try:
                if self._corrected_zarr is None:
                    self._corrected_zarr = zarr.open(self._corrected_zarr_path, mode="r")
                root = self._corrected_zarr
                roi_name = self._active_roi_name or "ROI_1"
                root_mode = str(root.attrs.get("mode", "")).lower()
                groups = self._zarr_group_names(root)
                print(f"[Step3] corrected groups={groups}")
                print(f"[Step3] selected corrected group={roi_name}")
                if roi_name in root and ch in root[roi_name]:
                    arr = np.asarray(root[roi_name][ch][y0:y1:sub, x0:x1:sub], dtype=np.float32)
                    source = "corrected_zarr roi_local"
                    print(f"[Step3] channel={ch} source=corrected_zarr")
                    print(f"[Step3] corrected_roi={roi_name}")
                elif root_mode != "roi_only" and ch in root:
                    arr = np.asarray(root[ch][y0:y1:sub, x0:x1:sub], dtype=np.float32)
                    source = "corrected_zarr full_wsi"
                elif root_mode == "roi_only":
                    print(f"[Step3] channel={ch} not found in corrected zarr active_roi={roi_name}")
            except Exception as e:
                print(f"[Step3] corrected channel load failed {ch}: {e}")

        raw_loader = self._raw_loader
        if arr is None and self._raw_ome_path and (
            raw_loader is None or getattr(raw_loader, "filepath", None) != self._raw_ome_path
        ):
            try:
                raw_loader = OMETIFFLoader(self._raw_ome_path)
                self._raw_loader = raw_loader
            except Exception as e:
                print(f"[Step3] failed to initialize Raw OME loader for {ch}: {e}")
                raw_loader = None
        if arr is None and raw_loader is None:
            raw_loader = self._loader
        if arr is None and raw_loader is not None:
            try:
                if self._mode == "roi" and self._active_bbox and len(self._active_bbox) == 4:
                    gy0 = int(self._active_bbox[0]) + y0
                    gy1 = int(self._active_bbox[0]) + y1
                    gx0 = int(self._active_bbox[2]) + x0
                    gx1 = int(self._active_bbox[2]) + x1
                elif self._mode == "roi":
                    print(f"[Step3] raw OME channel skipped; missing active ROI bbox for {ch}")
                    return
                else:
                    gy0, gy1, gx0, gx1 = y0, y1, x0, x1
                print(f"[Step3] channel={ch} source=raw_ome")
                print(f"[Step3] raw_global_bbox={[gy0, gy1, gx0, gx1]}")
                arr = raw_loader.read_region(ch, gy0, gy1, gx0, gx1, downsample=sub, normalize=True)
                source = f"raw_ome global_bbox={[gy0, gy1, gx0, gx1]}"
            except Exception as e:
                print(f"[Step3] channel missing/skipped {ch}: {e}")
                return
        if arr is not None:
            print(f"[Step3] channel={ch} source={source or self._channel_sources.get(ch, 'unknown')}")
            self._patch_channel_cache[ch] = self._match_channel_shape(
                ch, arr, source or self._channel_sources.get(ch, "unknown")
            )

    # ── Random ROI ───────────────────────────────────────────────────

    def _random_roi(self):
        if self._full_h == 0:
            return
        rh = min(self._roi_h_spin.value(), self._full_h)
        rw = min(self._roi_w_spin.value(), self._full_w)
        import random
        y0 = random.randint(0, max(0, self._full_h - rh))
        x0 = random.randint(0, max(0, self._full_w - rw))
        y1 = min(y0 + rh, self._full_h)
        x1 = min(x0 + rw, self._full_w)
        # Place rect on thumbnail
        x_ds = x0 / self._OV_DS
        y_ds = y0 / self._OV_DS
        w_ds = (x1 - x0) / self._OV_DS
        h_ds = (y1 - y0) / self._OV_DS
        self._roi_rect_set(x_ds, y_ds, w_ds, h_ds)

    # ── Browse helpers ────────────────────────────────────────────────

    def _browse(self, which):
        if which == 'fusion':
            path = QFileDialog.getExistingDirectory(
                self,
                'Select Fusion Source zarr folder',
                self._output_dir or OUTPUT_DIR,
            )
            if not path:
                return
            try:
                _z, shape = self._validate_fusion_zarr(path)
            except Exception as e:
                QMessageBox.warning(self, 'Invalid fusion source', str(e))
                return
            selected = os.path.abspath(path)
            self._fusion_edit.setText(selected)
            self._fusion_zarr_path = selected
            self._fused_zarr_path = selected
            self._sync_active_roi_from_input_paths()
            print(f"[Step3] manual fusion source selected: {selected}")
            print(f"[Step3] manual fusion zarr shape={shape}")
            self._save_input_files_config()
            self._show_fusion = True
            self._chk_fusion.blockSignals(True)
            self._chk_fusion.setChecked(True)
            self._chk_fusion.setEnabled(True)
            self._chk_fusion.blockSignals(False)
            if self._roi is not None:
                self._load_region()
            else:
                self._roi_status.setText(f'Fusion source loaded: {selected}')
            return

        if which == 'channel_source':
            path = QFileDialog.getExistingDirectory(
                self,
                'Select corrected_channels.zarr folder',
                self._output_dir or OUTPUT_DIR,
            )
            if not path:
                return
            try:
                root = zarr.open(path, mode="r")
                roi_name = self._active_roi_name or "ROI_1"
                if str(root.attrs.get("mode", "")).lower() == "roi_only" and roi_name not in root:
                    raise ValueError(f"ROI group not found in corrected zarr: {roi_name}")
            except Exception as e:
                QMessageBox.warning(self, 'Invalid Channel Source', str(e))
                return
            selected = os.path.abspath(path)
            self._channel_source_edit.setText(selected)
            self._corrected_zarr_path = selected
            self._corrected_zarr = None
            self._sync_active_roi_from_input_paths()
            self._save_input_files_config()
            self._refresh_channel_sources()
            self._load_visible_patch_channels()
            self._render_roi(reset_view=False)
            return

        if which == 'raw_ome':
            path, _ = QFileDialog.getOpenFileName(
                self,
                'Select Raw OME-TIFF',
                self._output_dir or OUTPUT_DIR,
                'OME-TIFF (*.tif *.tiff)'
            )
            if not path:
                return
            try:
                loader = OMETIFFLoader(path)
            except Exception as e:
                QMessageBox.warning(self, 'Invalid Raw OME', str(e))
                return
            selected = os.path.abspath(path)
            self._raw_ome_edit.setText(selected)
            self._raw_ome_path = selected
            self._raw_loader = loader
            self._sync_active_roi_from_input_paths()
            self._save_input_files_config()
            self._refresh_channel_sources()
            self._load_visible_patch_channels()
            self._render_roi(reset_view=False)
            return

        path, _ = QFileDialog.getOpenFileName(
            self, f'Select {"DAPI" if which == "dapi" else "Mask"} OME-TIFF',
            OUTPUT_DIR, 'OME-TIFF (*.tiff *.tif)'
        )
        if path:
            if which == 'dapi':
                self._dapi_edit.setText(path)
                self._dapi_path = path
            else:
                self._mask_edit.setText(path)
                self._mask_path = path
            self._sync_active_roi_from_input_paths()
            self._save_input_files_config()

    def _manual_load(self):
        self.load_from_paths(
            self._dapi_edit.text().strip(),
            self._mask_edit.text().strip(),
        )

    def _status(self, msg):
        self._thumb_status.setText(msg)

    def _show_input_error(self, msg):
        self._thumb_status.setText(msg)
        self._roi_status.setText(msg)
        self._roi_info.setText(msg)

    def _stop_loaders(self):
        self._load_debounce.stop()
        for attr in ("_thumb_loader", "_region_loader"):
            thread = getattr(self, attr, None)
            if thread is None:
                continue
            if thread.isRunning():
                if hasattr(thread, "stop"):
                    thread.stop()
                thread.wait(3000)
            if not thread.isRunning():
                setattr(self, attr, None)

    def closeEvent(self, event):
        self._stop_loaders()
        if ((self._thumb_loader and self._thumb_loader.isRunning()) or
                (self._region_loader and self._region_loader.isRunning())):
            event.ignore()
            self._status("Waiting for Step3 loaders to stop…")
            QtCore.QTimer.singleShot(500, self.close)
            return
        super().closeEvent(event)

    # ── Event filter: thumbnail rubber-band + ROI-view pan/zoom ───────

    def _img_pos(self, viewport_pos):
        """Convert viewport QPoint → image (thumbnail) QPointF."""
        sp = self._ov_gv.mapToScene(viewport_pos)
        return self._ov_img.mapFromScene(sp)

    def eventFilter(self, obj, event):
        # ── Thumbnail viewport ────────────────────────────────────────
        if obj is self._ov_gv.viewport():
            t = event.type()

            if t == QtCore.QEvent.MouseButtonPress:
                ip = self._img_pos(event.pos())
                px, py = ip.x(), ip.y()

                # Right-click on existing rect → delete it
                if event.button() == Qt.RightButton:
                    if (self._roi_rect is not None and
                            self._roi_rect.contains_ds(px, py)):
                        self._roi_rect_clear()
                        self._roi      = None
                        self._clear_region_cache(clear_image=True)
                        self._roi_info.setText(
                            'Draw a rectangle on the thumbnail to inspect a region'
                        )
                        self._cell_count_lbl.setText('')
                        self._roi_status.setText('—')
                    return True

                if event.button() == Qt.LeftButton and self._thumb_arr is not None:
                    # Left-click inside existing rect → start moving it
                    if (self._roi_rect is not None and
                            self._roi_rect.contains_ds(px, py)):
                        self._rect_drag_last = event.pos()
                    else:
                        # Left-click on empty area → start drawing new rect
                        self._roi_draw_start = ip
                        self._rect_drag_last = None
                    return True

            elif t == QtCore.QEvent.MouseMove:
                ip = self._img_pos(event.pos())

                if event.buttons() & Qt.LeftButton:
                    # Moving existing rect
                    if self._rect_drag_last is not None and self._roi_rect is not None:
                        dp   = event.pos() - self._rect_drag_last
                        self._rect_drag_last = event.pos()
                        vr   = self._ov_vb.viewRange()
                        vpw  = max(1, self._ov_gv.viewport().width())
                        vph  = max(1, self._ov_gv.viewport().height())
                        # dp in viewport px → thumbnail coords
                        dx = dp.x() * (vr[0][1] - vr[0][0]) / vpw
                        dy = dp.y() * (vr[1][1] - vr[1][0]) / vph
                        self._roi_rect.move_by_ds(dx, dy)
                        return True

                    # Drawing new rect (rubber-band preview)
                    if self._roi_draw_start is not None and self._roi_rect is None:
                        p0 = self._roi_draw_start
                        x0 = min(p0.x(), ip.x())
                        y0 = min(p0.y(), ip.y())
                        w  = max(1.0, abs(ip.x() - p0.x()))
                        h  = max(1.0, abs(ip.y() - p0.y()))
                        # Live preview: create/update rect without loading
                        if self._roi_rect is None:
                            rect = _RoiRect(x0, y0, w, h,
                                            self._OV_DS, self._full_h, self._full_w)
                            rect.roi_changed.connect(self._on_roi_changed)
                            self._ov_vb.addItem(rect)
                            self._roi_rect = rect
                        else:
                            self._roi_rect.set_rect_ds(x0, y0, w, h)
                        return True

            elif t == QtCore.QEvent.MouseButtonRelease:
                if event.button() == Qt.LeftButton:
                    ip = self._img_pos(event.pos())

                    if self._rect_drag_last is not None:
                        # Finished moving — trigger region load
                        self._rect_drag_last = None
                        if self._roi_rect is not None:
                            self._roi_rect._emit()
                        return True

                    if self._roi_draw_start is not None:
                        p0  = self._roi_draw_start
                        self._roi_draw_start = None
                        x0  = min(p0.x(), ip.x())
                        y0  = min(p0.y(), ip.y())
                        w   = abs(ip.x() - p0.x())
                        h   = abs(ip.y() - p0.y())
                        if w >= 10 and h >= 10:
                            # Finalise the live-preview rect
                            if self._roi_rect is not None:
                                self._roi_rect.set_rect_ds(x0, y0, w, h)
                            else:
                                self._roi_rect_set(x0, y0, w, h)
                        else:
                            # Too small — discard
                            self._roi_rect_clear()
                        return True

            elif t == QtCore.QEvent.MouseButtonDblClick:
                self._ov_vb.autoRange()
                return True

            elif t == QtCore.QEvent.Wheel:
                delta  = event.angleDelta().y()
                factor = 1.15 ** (delta / 120.0)
                sp = self._ov_gv.mapToScene(event.pos())
                ip = self._ov_img.mapFromScene(sp)
                cx, cy = ip.x(), ip.y()
                vr = self._ov_vb.viewRange()
                self._ov_vb.disableAutoRange()
                self._ov_vb.setRange(
                    xRange=[cx + (vr[0][0] - cx) / factor,
                             cx + (vr[0][1] - cx) / factor],
                    yRange=[cy + (vr[1][0] - cy) / factor,
                             cy + (vr[1][1] - cy) / factor],
                    padding=0,
                )
                return True

        # ── ROI view viewport: pan + zoom ─────────────────────────────
        elif obj is self._roi_gv.viewport():
            t = event.type()

            if t == QtCore.QEvent.Wheel:
                delta  = event.angleDelta().y()
                factor = 1.15 ** (delta / 120.0)
                sp = self._roi_gv.mapToScene(event.pos())
                ip = self._roi_img.mapFromScene(sp)
                cx, cy = ip.x(), ip.y()
                vr = self._roi_vb.viewRange()
                self._roi_vb.disableAutoRange()
                self._roi_vb.setRange(
                    xRange=[cx + (vr[0][0] - cx) / factor,
                             cx + (vr[0][1] - cx) / factor],
                    yRange=[cy + (vr[1][0] - cy) / factor,
                             cy + (vr[1][1] - cy) / factor],
                    padding=0,
                )
                return True

            elif t == QtCore.QEvent.MouseButtonPress:
                if event.button() in (Qt.LeftButton, Qt.MiddleButton,
                                      Qt.RightButton):
                    self._pan_last = event.pos()
                    return True

            elif t == QtCore.QEvent.MouseMove:
                if (event.buttons() & (Qt.LeftButton | Qt.MiddleButton |
                                       Qt.RightButton)
                        and self._pan_last is not None):
                    dp  = event.pos() - self._pan_last
                    self._pan_last = event.pos()
                    vr  = self._roi_vb.viewRange()
                    vpw = max(1, self._roi_gv.viewport().width())
                    vph = max(1, self._roi_gv.viewport().height())
                    dx  = -dp.x() * (vr[0][1] - vr[0][0]) / vpw
                    dy  = -dp.y() * (vr[1][1] - vr[1][0]) / vph
                    self._roi_vb.disableAutoRange()
                    self._roi_vb.setRange(
                        xRange=[vr[0][0] + dx, vr[0][1] + dx],
                        yRange=[vr[1][0] + dy, vr[1][1] + dy],
                        padding=0,
                    )
                    return True

            elif t == QtCore.QEvent.MouseButtonRelease:
                if event.button() in (Qt.LeftButton, Qt.MiddleButton,
                                      Qt.RightButton):
                    self._pan_last = None
                    return True

            elif t == QtCore.QEvent.MouseButtonDblClick:
                self._roi_vb.autoRange()
                return True

        return super().eventFilter(obj, event)


# ══════════════════════════════════════════════════════════════════════
#  Step 4 — Cell Feature Extraction Worker
# ══════════════════════════════════════════════════════════════════════
