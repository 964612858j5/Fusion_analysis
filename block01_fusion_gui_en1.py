"""
block01_fusion_gui_en.py
══════════════════════════════════════════════════════════════════════
CODEX / PhenoCycler Fusion — Channel Fusion Configuration + Cellpose Grid Search

Features:
  1. Load OME-TIFF, display DAPI overview (1/32 downsampling)
  2. Drag on overview to add 3~4 patch ROIs
  3. Right panel: configure channel groups and weights (slider+spinbox linked), real-time fusion preview
  4. Two-phase Cellpose grid search:
       Phase 1: fixed model=cyto3, search diameter
       Phase 2: fixed diameter, search flow_threshold × cellprob_threshold
  5. Result grid display (rows=patches, cols=param combos), click to select best params
  6. Save fusion_config.json + cellpose_params.json

Dependencies:
  pip install PyQt5 pyqtgraph tifffile zarr numpy opencv-python-headless cellpose

Usage:
  python block01_fusion_gui_en.py
══════════════════════════════════════════════════════════════════════
"""

import sys
import os
import gc
import json
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
import threading
from datetime import datetime
import numpy as np
import tifffile
import zarr
import xml.etree.ElementTree as ET

from PyQt5 import QtWidgets, QtCore, QtGui
from PyQt5.QtCore import Qt, QTimer, QRectF, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QSplitter,
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QGroupBox, QSlider, QDoubleSpinBox,
    QInputDialog, QMessageBox, QFileDialog,
    QComboBox, QFrame, QProgressBar, QSizePolicy,
    QDialog, QRadioButton, QButtonGroup, QTableWidget,
    QTableWidgetItem, QHeaderView,
)
import pyqtgraph as pg

pg.setConfigOptions(antialias=True, imageAxisOrder="row-major")


# ══════════════════════════════════════════════════════════════════════
#  ★ User Configuration
# ══════════════════════════════════════════════════════════════════════

OME_TIFF_FILE = (
    "/nvme0n1p1/2025.12.20_Final_17209_16_Slice4/Scan1/"
    "2025.12.20_Final_17209_16_Slice4_Scan1.ome.tif"
)
OUTPUT_DIR = (
    "/nvme0n1p1/2025.12.20_Final_17209_16_Slice4/Scan1/"
    "pipeline_v2"
)

CHANNEL_NAME_MAP    = {}   # {"OME raw name": "display name"}
OVERVIEW_DOWNSAMPLE = 32
PREVIEW_DOWNSAMPLE  = 4
NORM_LOW            = 1.0
NORM_HIGH           = 99.5

INITIAL_GROUPS = {
    "epithelial": {
        "CK19": 1.5, "Gp3": 1.0, "HsBAg": 1.0,
    },
    "immune": {
        "CD3D": 1.0, "CD4": 0.8, "CD8": 0.8,
        "CD68": 1.0, "CD163": 0.8, "CD11b": 0.5,
        "CD11c": 0.5, "CD14": 0.5, "CD22": 0.5,
        "CCR7": 0.5, "TIM3": 0.5, "CD45RA": 0.5,
        "CD45RO": 0.5, "HLA-DR": 0.5,
    },
    "endothelial": {"CD31": 1.5},
}
NUCLEUS_CONFIG = {"channel": "DAPI", "weight": 1.0}

PHASE1_DIAMETERS = [20, 30, 40, 50, 70]
PHASE2_FLOW      = [0.2, 0.4, 0.6]
PHASE2_CELLPROB  = [-1.0, 0.0, 0.5, 1.0]
# Cellpose 4.0.1+: model_type argument is ignored; only cpsam is used.
DEFAULT_MODEL    = "cpsam"  # kept for JSON output only

PATCH_COLORS = ["#ff4444", "#44ff88", "#4488ff", "#ffdd44"]
ROI_COLORS   = [
    "#ff6b6b", "#ffd93d", "#6bcb77", "#4d96ff",
    "#ff922b", "#cc5de8", "#20c997", "#f06595",
]
# Default ROI name list (extended on demand)
DEFAULT_ROI_NAMES = ["ROI_1", "ROI_2", "ROI_3", "ROI_4",
                     "ROI_5", "ROI_6", "ROI_7", "ROI_8"]


# ══════════════════════════════════════════════════════════════════════
#  OME-TIFF Loader
# ══════════════════════════════════════════════════════════════════════

class OMETIFFLoader:
    """
    OME-TIFF Loader.
    Key fix: uses tifffile zarr interface for lazy loading, reading only the tiles
    covered by the ROI. The old tif.pages[idx].asarray() reads the full page (~3GB/page),
    19 channels = 57GB IO. zarr interface reads only the tiles covering the ROI,
    reducing IO by 100x or more.
    """

    def __init__(self, filepath, name_map=None):
        self.filepath = filepath
        self.name_map = name_map or {}
        self.ch_map   = {}
        self.shape    = (0, 0)
        self._zarr_array = None   # lazy init, avoid resource usage at startup
        self._parse()

    def _parse(self):
        with tifffile.TiffFile(self.filepath) as tif:
            root = ET.fromstring(tif.ome_metadata)
            p    = tif.pages[0]
            self.shape = (p.imagelength, p.imagewidth)
        ns = {"ome": "http://www.openmicroscopy.org/Schemas/OME/2016-06"}
        for i, ch in enumerate(root.findall(".//ome:Channel", ns)):
            raw  = ch.get("Name", f"ch_{i:02d}")
            disp = self.name_map.get(raw, raw)
            self.ch_map[disp] = i
        print(f"[Loader] {self.shape[0]}×{self.shape[1]} px  {len(self.ch_map)} channels")

    def channel_names(self):
        return list(self.ch_map.keys())

    def _get_zarr(self):
        """
        Lazy-initialize zarr array.
        tifffile.TiffFile.aszarr() returns a zarr store that only reads from disk
        when specific slices are accessed, without preloading entire pages.
        The store must be used within a TiffFile context, so each call reopens the file.
        (tifffile >= 2021.x supports this interface)
        """
        return None  # marker: no caching, open inside each read_region call

    def read_region(self, channel_name, y0, y1, x0, x1, downsample=1):
        """
        Read the ROI region of the specified channel, normalized to float32 [0,1].

        Implementation: prefer zarr lazy loading (reads only ROI tiles),
        raises exception on failure.
        """
        if channel_name not in self.ch_map:
            raise KeyError(f"Channel '{channel_name}' not found")
        page_idx = self.ch_map[channel_name]

        region = self._read_roi_zarr(page_idx, y0, y1, x0, x1)

        if downsample > 1:
            region = region[::downsample, ::downsample]
        return self._norm(region)

    def _read_roi_zarr(self, page_idx, y0, y1, x0, x1):
        """
        Read only the ROI region using the zarr interface.
        zarr translates the request into reads of the corresponding tiles,
        avoiding loading the full page.

        OME-TIFF zarr structure is typically:
          - Multi-series: z[series][level][page, y, x] or z[series][level][y, x]
          - Single-series: z[level] is (C, Y, X) or (Y, X) etc.

        For page-by-page OME-TIFF, tifffile zarr's top-level z[0] is
        usually an array of shape (n_pages, H, W).
        """
        try:
            with tifffile.TiffFile(self.filepath) as tif:
                store = tif.aszarr()
                z = zarr.open(store, mode='r')

                # Probe zarr structure
                if isinstance(z, zarr.hierarchy.Group):
                    # Multi-level or multi-series structure, take first array
                    # Keys are typically '0', '1', ... for pyramid levels
                    z0 = z[0] if '0' in z else next(iter(z.values()))
                else:
                    z0 = z

                if z0.ndim == 3:
                    # (n_pages, H, W) — most common OME-TIFF layout
                    region = np.array(z0[page_idx, y0:y1, x0:x1])
                elif z0.ndim == 4:
                    # (T, C, H, W) or (Z, C, H, W)
                    region = np.array(z0[0, page_idx, y0:y1, x0:x1])
                elif z0.ndim == 2:
                    # Single-channel image
                    region = np.array(z0[y0:y1, x0:x1])
                else:
                    raise ValueError(f"Unknown zarr dimensions: {z0.ndim}")

            return region.copy()

        except Exception as e:
            # On zarr read failure, raise exception to notify the caller.
            # Never fallback to full-page read here:
            # 59040×35520 uint16 = 4GB/page, 19 pages = 76GB IO → system crash
            raise RuntimeError(
                f"zarr read failed: {e}\n"
                f"Please ensure zarr is installed: pip install zarr\n"
                f"And tifffile >= 2021.x: pip install --upgrade tifffile"
            ) from e

    @staticmethod
    def _norm(arr):
        arr = arr.astype(np.float32)
        nz  = arr[arr > 0]
        if nz.size < 100:
            return np.zeros_like(arr)
        lo, hi = np.percentile(nz, [NORM_LOW, NORM_HIGH])
        if hi <= lo:
            return np.zeros_like(arr)
        return np.clip((arr - lo) / (hi - lo), 0.0, 1.0)


# ══════════════════════════════════════════════════════════════════════
#  Fusion Engine
# ══════════════════════════════════════════════════════════════════════

class FusionEngine:

    @staticmethod
    def _normalize_intensity(img):
        # FIX: intensity normalization
        arr = np.asarray(img, dtype=np.float32)
        if arr.size == 0:
            return arr
        mn = float(np.min(arr))
        mx = float(np.max(arr))
        eps = 1e-6
        return np.clip((arr - mn) / (mx - mn + eps), 0.0, 1.0)

    def compute(self, cache, groups, group_weights, nuc_ch, nuc_w):
        """Returns (cyto, nucleus) float32 [0,1]"""
        if not cache:
            return None, None
        shape = next(iter(cache.values())).shape
        signals = []
        for gname, ch_weights in groups.items():
            gw    = float(np.clip(group_weights.get(gname, 1.0), 0.0, 1.0))
            accum = np.zeros(shape, dtype=np.float32)
            for ch, w in ch_weights.items():
                if ch in cache and w > 0:
                    accum += self._normalize_intensity(cache[ch]) * float(np.clip(w, 0.0, 1.0))
            accum *= gw
            np.clip(accum, 0.0, 1.0, out=accum)
            signals.append(accum)

        cyto = np.zeros(shape, dtype=np.float32)
        for s in signals:
            np.maximum(cyto, s, out=cyto)
        np.clip(cyto, 0.0, 1.0, out=cyto)

        nucleus = np.zeros(shape, dtype=np.float32)
        if nuc_ch and nuc_ch in cache and nuc_w > 0:
            nucleus = self._normalize_intensity(cache[nuc_ch]) * float(np.clip(nuc_w, 0.0, 1.0))
            np.clip(nucleus, 0.0, 1.0, out=nucleus)

        return cyto, nucleus

    def fuse_fullres(self, loader, y0, y1, x0, x1,
                     groups, group_weights, nuc_ch, nuc_w):
        """Full-resolution fusion, returns (H,W,2) uint16 for Cellpose"""
        needed = set(nuc_ch)
        for cw in groups.values():
            needed.update(cw.keys())
        needed.add(nuc_ch)

        cache = {}
        for ch in needed:
            if ch in loader.ch_map:
                cache[ch] = loader.read_region(ch, y0, y1, x0, x1, downsample=1)

        cyto, nucleus = self.compute(cache, groups, group_weights, nuc_ch, nuc_w)
        result = np.stack([
            (cyto    * 65535).astype(np.uint16),
            (nucleus * 65535).astype(np.uint16),
        ], axis=-1)
        del cache
        gc.collect()
        return result

    @staticmethod
    def to_rgb(cyto, nucleus):
        r = (np.clip(cyto,    0, 1) * 255).astype(np.uint8)
        g = np.zeros_like(r)
        b = (np.clip(nucleus, 0, 1) * 255).astype(np.uint8)
        return np.stack([r, g, b], axis=-1)

    @staticmethod
    def overlay_mask(rgb, mask):
        import cv2
        out     = rgb.copy()
        n_cells = int(mask.max())
        if n_cells == 0:
            return out

        # ── Per-cell color (semi-transparent fill) ──────────────────────
        rng    = np.random.RandomState(42)
        colors = rng.randint(80, 255, size=(n_cells + 1, 3), dtype=np.uint8)
        colors[0] = [0, 0, 0]   # background color (unused)

        cell_area = mask > 0
        if cell_area.any():
            mask_clipped = np.clip(mask, 0, n_cells).astype(np.int32)
            fill_color   = colors[mask_clipped]          # (H, W, 3)
            alpha        = 0.30
            out[cell_area] = (
                out[cell_area].astype(np.float32) * (1.0 - alpha)
                + fill_color[cell_area].astype(np.float32) * alpha
            ).astype(np.uint8)

        # ── Thick green boundary (dilate−erode) ─────────────────────────
        bin_mask = (mask > 0).astype(np.uint8)
        kernel   = np.ones((5, 5), np.uint8)
        boundary = (cv2.dilate(bin_mask, kernel, iterations=1)
                    - cv2.erode(bin_mask, kernel, iterations=1))
        out[boundary > 0] = [0, 255, 80]
        return out


# ══════════════════════════════════════════════════════════════════════
#  Background IO Threads (prevent main thread blocking)
# ══════════════════════════════════════════════════════════════════════

class OverviewLoaderThread(QThread):
    """Background thread to load overview (DAPI full image downsampled)"""
    done  = pyqtSignal(object)   # float32 ndarray
    error = pyqtSignal(str)

    def __init__(self, loader, nuc_ch, downsample):
        super().__init__()
        self.loader     = loader
        self.nuc_ch     = nuc_ch
        self.downsample = downsample

    def run(self):
        try:
            arr = self.loader.read_region(
                self.nuc_ch,
                0, self.loader.shape[0],
                0, self.loader.shape[1],
                downsample=self.downsample,
            )
            self.done.emit(arr)
        except Exception as e:
            self.error.emit(str(e))


class PreviewLoaderThread(QThread):
    """Background thread to load all channels for one patch ROI.
    patch_idx is carried through all signals so multiple concurrent threads
    can be distinguished when their results arrive.
    """
    done     = pyqtSignal(int, dict)         # (patch_idx, {ch_name: ndarray})
    progress = pyqtSignal(int, int, int, str) # (patch_idx, done, total, ch_name)
    error    = pyqtSignal(int, str)           # (patch_idx, msg)

    def __init__(self, patch_idx, loader, channels, y0, y1, x0, x1, downsample):
        super().__init__()
        self.patch_idx  = patch_idx
        self.loader     = loader
        self.channels   = channels
        self.y0, self.y1, self.x0, self.x1 = y0, y1, x0, x1
        self.downsample = downsample
        self._stop      = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            cache = {}
            total = len(self.channels)
            for i, ch in enumerate(self.channels):
                if self._stop:
                    return
                self.progress.emit(self.patch_idx, i, total, ch)
                if ch in self.loader.ch_map:
                    cache[ch] = self.loader.read_region(
                        ch,
                        self.y0, self.y1,
                        self.x0, self.x1,
                        downsample=self.downsample,
                    )
            if not self._stop:
                self.done.emit(self.patch_idx, cache)
        except Exception as e:
            if not self._stop:
                self.error.emit(self.patch_idx, str(e))


# ══════════════════════════════════════════════════════════════════════
#  Cellpose-style mask overlay  (shared by grid search + Step 3 QC)
# ══════════════════════════════════════════════════════════════════════

def cellpose_mask_overlay(img_grey_u8, masks):
    """
    Cellpose-style mask overlay, fully vectorised.

    img_grey_u8 : uint8 2-D array (H, W) — single-channel DAPI / grey
    masks        : uint32 2-D array (H, W) — 0=background, 1..N=cell labels

    Returns uint8 RGB array (H, W, 3):
      • background → brightened grey  (S=0)
      • cells      → uniformly-spaced hues, S=1
                     brightness = max(img*1.5, 0.15) so cytoplasm regions
                     with weak DAPI signal remain visibly coloured
      • NO outlines / borders of any kind
    """
    n_cells = int(masks.max())

    # V channel: image brightness ×1.5 for background pixels.
    # All mask pixels get full brightness (1.0) regardless of DAPI signal —
    # cytoplasm and nucleus regions appear equally bright.
    V_base = np.clip(img_grey_u8.astype(np.float32) / 255.0 * 1.5, 0.0, 1.0)
    V = np.where(masks > 0, 1.0, V_base)

    # Build per-label H/S lookup tables (index 0 = background)
    H_lut = np.zeros(n_cells + 1, dtype=np.float32)
    S_lut = np.zeros(n_cells + 1, dtype=np.float32)
    if n_cells > 0:
        hues = np.linspace(0.0, 1.0, n_cells + 1)[
            np.random.permutation(n_cells)
        ]
        H_lut[1:] = hues          # background stays H=0
        S_lut[1:] = 1.0           # background stays S=0

    # Map every pixel to its H and S via the LUT — O(H×W), no Python loop
    mask_idx = np.clip(masks, 0, n_cells).astype(np.int32)
    H = H_lut[mask_idx]
    S = S_lut[mask_idx]

    # Vectorised HSV → RGB
    h6 = H * 6.0
    i  = h6.astype(np.int32) % 6
    f  = h6 - np.floor(h6)
    p  = V * (1.0 - S)
    q  = V * (1.0 - f * S)
    t  = V * (1.0 - (1.0 - f) * S)

    # Build R, G, B by selecting the correct formula per sector
    R = np.select([i==0, i==1, i==2, i==3, i==4, i==5], [V,q,p,p,t,V])
    G = np.select([i==0, i==1, i==2, i==3, i==4, i==5], [t,V,V,q,p,p])
    B = np.select([i==0, i==1, i==2, i==3, i==4, i==5], [p,p,t,V,V,q])

    RGB = np.stack([R, G, B], axis=-1)
    return (RGB * 255).astype(np.uint8)


# ══════════════════════════════════════════════════════════════════════
#  Cellpose Background Thread
# ══════════════════════════════════════════════════════════════════════

class CellposeWorker(QThread):
    # rgb_overlay = DAPI grey + cellpose-style mask overlay (no borders)
    # rgb_raw     = DAPI grey only (no mask)
    result_ready   = pyqtSignal(int, dict, object, object)  # patch_idx, params, rgb_overlay, rgb_raw
    progress       = pyqtSignal(int, int, str)               # done, total, msg
    finished_all   = pyqtSignal()
    error_occurred = pyqtSignal(str)

    def __init__(self, tasks, loader, fusion,
                 groups, group_weights, nuc_ch, nuc_w):
        super().__init__()
        self.tasks         = tasks   # [(patch_idx, (y0,y1,x0,x1), params), ...]
        self.loader        = loader
        self.fusion        = fusion
        self.groups        = groups
        self.group_weights = group_weights
        self.nuc_ch        = nuc_ch
        self.nuc_w         = nuc_w
        self._stop         = False

    @staticmethod
    def _make_dapi_rgb(dapi_f32):
        """Convert float32 [0,1] DAPI channel to uint8 2-D grey array."""
        return (np.clip(dapi_f32, 0, 1) * 255).astype(np.uint8)

    def stop(self):
        self._stop = True

    def run(self):
        try:
            from cellpose import models
            import torch

            use_gpu = torch.cuda.is_available()
            device  = torch.device("cuda" if use_gpu else "cpu")
            if use_gpu:
                free, total_vram = torch.cuda.mem_get_info(0)
                print(f"[Cellpose] GPU: {torch.cuda.get_device_name(0)}  "
                      f"VRAM free {free/1e9:.1f}/{total_vram/1e9:.1f} GB")
            else:
                print("[Cellpose] ⚠ CUDA not available, using CPU (slower)")
            # Cellpose 4.0.1+: model_type is ignored, always loads cpsam
            model = models.CellposeModel(device=device)
            total = len(self.tasks)

            for done, (patch_idx, roi, params) in enumerate(self.tasks):
                if self._stop:
                    break

                y0, y1, x0, x1 = roi
                self.progress.emit(
                    done, total,
                    f"Patch {patch_idx+1}  "
                    f"diam={params.get('diameter')}  "
                    f"flow={params.get('flow_threshold')}  "
                    f"prob={params.get('cellprob_threshold')}"
                )

                fused = self.fusion.fuse_fullres(
                    self.loader, y0, y1, x0, x1,
                    self.groups, self.group_weights,
                    self.nuc_ch, self.nuc_w,
                )

                print(f"[Cellpose] fused: shape={fused.shape} dtype={fused.dtype} "
                      f"min={fused.min()} max={fused.max()}")

                # cpsam (Cellpose 4) input: normalised float32, shape (H,W,2)
                fused_f32 = np.array(fused.astype(np.float32) / 65535.0)

                print(f"[Cellpose] fused_f32: type={type(fused_f32)} "
                      f"shape={fused_f32.shape} "
                      f"ch0 range=[{fused_f32[:,:,0].min():.3f},{fused_f32[:,:,0].max():.3f}] "
                      f"ch1 range=[{fused_f32[:,:,1].min():.3f},{fused_f32[:,:,1].max():.3f}]")

                try:
                    # Pass only the nucleus channel (ch1) as a plain 2-D array.
                    # cpsam handles single-channel input natively and doesn't
                    # require the deprecated channels= argument.
                    nuc_2d = np.ascontiguousarray(fused_f32[:, :, 1])
                    print(f"[Cellpose] nuc_2d: type={type(nuc_2d)} shape={nuc_2d.shape} "
                          f"dtype={nuc_2d.dtype} contiguous={nuc_2d.flags['C_CONTIGUOUS']}")
                    masks, _, _ = model.eval(
                        nuc_2d,
                        diameter           = params.get("diameter"),
                        flow_threshold     = params.get("flow_threshold", 0.4),
                        cellprob_threshold = params.get("cellprob_threshold", 0.0),
                        min_size           = 15,
                        do_3D              = False,
                    )
                    mask = masks.astype(np.uint32)
                    print(f"[Cellpose] ✓ masks: shape={masks.shape} "
                          f"n_cells={mask.max()} unique={len(np.unique(mask))}")
                except Exception as e:
                    print(f"[Cellpose] ✗ model.eval failed: {e}")
                    import traceback as _tb
                    print(_tb.format_exc())
                    self.error_occurred.emit(str(e))
                    mask = np.zeros(fused.shape[:2], dtype=np.uint32)

                # Display: DAPI (nucleus, ch1) as grey background
                dapi_u8  = (np.clip(fused_f32[:, :, 1], 0, 1) * 255).astype(np.uint8)
                print(f"[Cellpose] dapi_u8: min={dapi_u8.min()} max={dapi_u8.max()} "
                      f"mean={dapi_u8.mean():.1f}")
                rgb_raw  = np.stack([dapi_u8]*3, axis=-1)
                rgb_ov   = cellpose_mask_overlay(dapi_u8, mask)
                n_cells  = int(mask.max())
                print(f"[Cellpose] rgb_ov shape={rgb_ov.shape} "
                      f"same_as_raw={np.array_equal(rgb_ov, rgb_raw)}")

                del fused, fused_f32, mask, dapi_u8
                gc.collect()
                torch.cuda.empty_cache()

                self.result_ready.emit(patch_idx, params, rgb_ov, rgb_raw)
                self.progress.emit(done + 1, total,
                                   f"✓ Patch {patch_idx+1}  cells={n_cells}")

            del model
            gc.collect()
            torch.cuda.empty_cache()

        except Exception:
            self.error_occurred.emit(traceback.format_exc())

        self.finished_all.emit()



# ══════════════════════════════════════════════════════════════════════
#  Tile Selection Dialog
# ══════════════════════════════════════════════════════════════════════

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
    def _read_one_channel(filepath, ch_map, ch_name, y0, y1, x0, x1):
        """Read a single channel tile region via zarr (used by thread pool)."""
        page_idx = ch_map[ch_name]
        with tifffile.TiffFile(filepath) as tif:
            store = tif.aszarr()
            z     = zarr.open(store, mode="r")
            if isinstance(z, zarr.hierarchy.Group):
                z0 = z[0] if "0" in z else next(iter(z.values()))
            else:
                z0 = z
            if z0.ndim == 3:
                region = np.array(z0[page_idx, y0:y1, x0:x1])
            elif z0.ndim == 4:
                region = np.array(z0[0, page_idx, y0:y1, x0:x1])
            else:
                region = np.array(z0[y0:y1, x0:x1])
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
                                ome_path, ch_map, ch, ty0, ty1, tx0, tx1
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
        self.loader  = loader
        self.nuc_ch  = nuc_ch
        self.ds      = OVERVIEW_DOWNSAMPLE
        self.full_h  = loader.shape[0] if loader else 0
        self.full_w  = loader.shape[1] if loader else 0

        # ── Data model ────────────────────────────────────────────────
        self._rois    = []
        self._patches = []

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

    # ── UI construction ───────────────────────────────────────────────

    def _setup_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(3)

        # ── Tool-bar: ROI | Patch ─────────────────────────────────────
        tb = QHBoxLayout()

        self._btn_roi = QPushButton("🔲 ROI")
        self._btn_roi.setCheckable(True)
        self._btn_roi.setStyleSheet(
            "QPushButton{font-size:11px;border:1px solid #6bcb77;"
            "border-radius:3px;padding:3px 10px;color:#6bcb77;}"
            "QPushButton:checked{background:#1a3a1a;color:#aaffaa;"
            "font-weight:bold;}"
        )
        self._btn_patch = QPushButton("📍 Patch")
        self._btn_patch.setCheckable(True)
        self._btn_patch.setChecked(True)
        self._btn_patch.setStyleSheet(
            "QPushButton{font-size:11px;border:1px solid #4488ff;"
            "border-radius:3px;padding:3px 10px;color:#4488ff;}"
            "QPushButton:checked{background:#1a2a4a;color:#88bbff;"
            "font-weight:bold;}"
        )
        self._btn_roi.clicked.connect(lambda: self._set_mode('roi'))
        self._btn_patch.clicked.connect(lambda: self._set_mode('patch'))
        tb.addWidget(self._btn_roi)
        tb.addWidget(self._btn_patch)
        tb.addStretch()
        lay.addLayout(tb)

        # ── Hint label ────────────────────────────────────────────────
        self.hint = QLabel(
            "Left-drag = Add Patch  |  Right-click = Delete last  "
            "|  Scroll = Zoom  |  Mid-drag = Pan"
        )
        self.hint.setAlignment(Qt.AlignCenter)
        self.hint.setStyleSheet("color:#777;font-size:10px;")
        lay.addWidget(self.hint)

        # ── pyqtgraph canvas ──────────────────────────────────────────
        self.gview = pg.GraphicsLayoutWidget()
        self.gview.setBackground("#111")
        self.vb = self.gview.addViewBox(row=0, col=0)
        self.vb.setAspectLocked(True)
        self.vb.invertY(True)
        # Disable pyqtgraph's built-in mouse drag (conflicts with ROI drawing)
        # We implement middle-drag pan manually via event filter
        self.vb.setMouseEnabled(x=False, y=False)
        self.vb.setMenuEnabled(False)
        self.img_item = pg.ImageItem()
        self.vb.addItem(self.img_item)

        # Install event filter for scroll zoom + double-click reset
        # (middle-drag pan is already handled in eventFilter)
        self.gview.viewport().installEventFilter(self)

        # Double-click on canvas → reset view to full image
        self.gview.scene().sigMouseClicked.connect(self._on_overview_click)

        # Hint update
        self.hint.setText(
            "左键拖动=画ROI/Patch  |  滚轮中键拖动=平移  "
            "|  滚轮=缩放  |  双击=复位"
        )

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
        self._info_lbl.setStyleSheet(
            "color:#bbb;font-size:10px;padding:2px;"
        )
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
            ("💾 Save ROIs",  self.save_rois,  "#4d96ff"),
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
        self._btn_roi.setChecked(mode == 'roi')
        self._btn_patch.setChecked(mode == 'patch')
        self._roi_ctrl.setVisible(mode == 'roi')
        self._patch_ctrl.setVisible(mode == 'patch')
        if mode == 'roi':
            self.hint.setText(
                "左键=添加顶点  |  Enter/右键=闭合ROI  |  Z=撤销  |  D=删除  "
                "|  滚轮=缩放  |  右键拖动=平移  |  双击=复位"
            )
            self.hint.setStyleSheet("color:#6bcb77;font-size:10px;")
        else:
            self.hint.setText(
                "左键拖动=添加Patch  |  右键点击=删除最后一个  "
                "|  滚轮=缩放  |  右键拖动=平移  |  双击=复位"
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
        name  = self._roi_name_edit.text().strip() or f"ROI_{len(self._rois)+1}"
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
            rect  = pg.RectROI(
                [cmin, rmin], [cmax - cmin, rmax - rmin],
                pen=pg.mkPen(color, width=2),
                movable=False, resizable=False,
            )
            lbl = pg.TextItem(f"P{i+1}", color=color, anchor=(0, 1))
            lbl.setPos(cmin, rmin)
            self.vb.addItem(rect)
            self.vb.addItem(lbl)
            self._patch_artists.append((rect, lbl))

    def clear_patches(self):
        for rect, lbl in self._patch_artists:
            self.vb.removeItem(rect)
            self.vb.removeItem(lbl)
        self._patch_artists.clear()
        self._patches.clear()
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
                    if self._rois:
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

class ResultGridPanel(QWidget):
    param_selected = pyqtSignal(dict)

    IMG_W = 190   # display width for each cell (px)

    def __init__(self):
        super().__init__()
        self._params       = []
        self._n_patches    = 0
        self._cell_widgets = {}   # (row, col) → QLabel widget
        self._col_btns     = []
        self._selected_col = -1
        self._full_results = {}   # (row, col) → rgb_overlay ndarray (with mask)
        self._raw_results  = {}   # (row, col) → rgb_raw ndarray (without mask)
        self._phase_desc   = ""
        self._show_mask    = True
        self._setup_ui()

    def _setup_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)

        hdr = QHBoxLayout()
        self.phase_lbl = QLabel("③ Search Results (click column header to select params)")
        self.phase_lbl.setAlignment(Qt.AlignCenter)
        self.phase_lbl.setStyleSheet(
            "font-weight:bold;font-size:13px;color:#ddd;"
        )
        hdr.addWidget(self.phase_lbl, stretch=1)

        self.btn_toggle_mask = QPushButton("Hide Mask")
        self.btn_toggle_mask.setEnabled(False)
        self.btn_toggle_mask.setCheckable(True)
        self.btn_toggle_mask.setStyleSheet(
            "QPushButton{background:#353;color:#8e8;"
            "border:1px solid #8e8;border-radius:3px;"
            "font-size:11px;padding:3px 8px;}"
            "QPushButton:checked{background:#533;color:#e88;"
            "border:1px solid #e88;}"
            "QPushButton:hover{background:#464;}"
            "QPushButton:disabled{background:#222;color:#444;}"
        )
        self.btn_toggle_mask.clicked.connect(self._toggle_mask)
        hdr.addWidget(self.btn_toggle_mask)

        self.btn_fullscreen = QPushButton("⛶ Fullscreen View")
        self.btn_fullscreen.setEnabled(False)
        self.btn_fullscreen.setStyleSheet(
            "QPushButton{background:#335;color:#8cf;"
            "border:1px solid #8cf;border-radius:3px;"
            "font-size:11px;padding:3px 8px;}"
            "QPushButton:hover{background:#446;}"
            "QPushButton:disabled{background:#222;color:#444;}"
        )
        self.btn_fullscreen.clicked.connect(self._open_fullscreen)
        hdr.addWidget(self.btn_fullscreen)
        lay.addLayout(hdr)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea{border:none;}")
        self.grid_w  = QWidget()
        self.grid_lay = QtWidgets.QGridLayout(self.grid_w)
        self.grid_lay.setSpacing(4)
        scroll.setWidget(self.grid_w)
        lay.addWidget(scroll, stretch=1)

        self.sel_lbl = QLabel("Not selected")
        self.sel_lbl.setAlignment(Qt.AlignCenter)
        self.sel_lbl.setStyleSheet(
            "color:#4af;font-size:11px;padding:3px;"
            "border:1px solid #4af;border-radius:3px;"
        )
        lay.addWidget(self.sel_lbl)

    def setup_grid(self, n_patches, param_list, phase_desc):
        # Clear
        while self.grid_lay.count():
            item = self.grid_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._cell_widgets.clear()
        self._col_btns.clear()
        self._full_results.clear()
        self._raw_results.clear()
        self._selected_col = -1
        self._params      = param_list
        self._n_patches   = n_patches
        self._phase_desc  = phase_desc
        self._show_mask   = True
        self.btn_toggle_mask.setEnabled(False)
        self.btn_toggle_mask.setChecked(False)
        self.btn_toggle_mask.setText("Hide Mask")
        self.btn_fullscreen.setEnabled(False)
        self.phase_lbl.setText(phase_desc)

        W = self.IMG_W

        for col, params in enumerate(param_list):
            # Parameter label
            lbl = QLabel(self._pstr(params))
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setWordWrap(True)
            lbl.setFixedWidth(W + 4)
            lbl.setStyleSheet(
                "font-size:10px;color:#ccc;padding:2px;"
                "border:1px solid #444;border-radius:3px;"
            )
            self.grid_lay.addWidget(lbl, 0, col + 1)

            # Select button (last row)
            btn = QPushButton("Select")
            btn.setFixedWidth(W + 4)
            btn.setStyleSheet(
                "QPushButton{background:#333;color:#aaa;"
                "border:1px solid #555;border-radius:3px;"
                "font-size:11px;padding:2px;}"
                "QPushButton:hover{background:#445;color:#fff;}"
            )
            btn.clicked.connect(lambda _, c=col: self._select(c))
            self._col_btns.append(btn)
            self.grid_lay.addWidget(btn, n_patches + 1, col + 1)

        for row in range(n_patches):
            lbl = QLabel(f"P{row+1}")
            lbl.setStyleSheet(
                f"color:{PATCH_COLORS[row % len(PATCH_COLORS)]};"
                f"font-weight:bold;font-size:11px;"
            )
            lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.grid_lay.addWidget(lbl, row + 1, 0)

            for col in range(len(param_list)):
                ph = QLabel("Waiting...")
                ph.setAlignment(Qt.AlignCenter)
                ph.setFixedSize(W + 4, W + 4)
                ph.setStyleSheet(
                    "background:#1a1a1a;color:#444;font-size:10px;"
                )
                self.grid_lay.addWidget(ph, row + 1, col + 1)
                self._cell_widgets[(row, col)] = ph

    @staticmethod
    def _to_pixmap(rgb_arr, max_size):
        """numpy uint8 RGB → QPixmap, downsampled to max_size px."""
        h, w  = rgb_arr.shape[:2]
        step  = max(1, max(h, w) // max_size)
        small = np.ascontiguousarray(rgb_arr[::step, ::step])
        sh, sw = small.shape[:2]
        qimg  = QtGui.QImage(small.data, sw, sh, 3 * sw,
                             QtGui.QImage.Format_RGB888).copy()
        return QtGui.QPixmap.fromImage(qimg)

    def add_result(self, patch_idx, params, rgb_overlay, rgb_raw):
        pkey = self._pkey(params)
        col  = next(
            (i for i, p in enumerate(self._params) if self._pkey(p) == pkey),
            None,
        )
        if col is None:
            # Fallback: if only one column exists, use it regardless of key match.
            # This handles edge cases where diameter=None serialisation differs.
            if len(self._params) == 1:
                col = 0
            else:
                print(f"[ResultGrid] add_result: no matching column for params={params}")
                print(f"[ResultGrid]   pkey={pkey}")
                print(f"[ResultGrid]   grid params={[self._pkey(p) for p in self._params]}")
                return
        key = (patch_idx, col)
        old = self._cell_widgets.get(key)
        if old:
            self.grid_lay.removeWidget(old)
            old.deleteLater()

        W = self.IMG_W

        # ── QLabel + QPixmap: much faster rendering than pyqtgraph widget ──
        pm_mask = self._to_pixmap(rgb_overlay, W)
        pm_raw  = self._to_pixmap(rgb_raw,     W)

        lbl = QLabel()
        lbl.setFixedSize(W + 4, W + 4)
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet("background:#000;border:1px solid #333;")
        lbl.setCursor(Qt.PointingHandCursor)
        lbl.setToolTip("Click to zoom (scroll to zoom in/out)")
        lbl.setProperty("pm_mask", pm_mask)
        lbl.setProperty("pm_raw",  pm_raw)
        lbl.setPixmap(pm_mask if self._show_mask else pm_raw)

        # Click → open single image zoom window
        _ov  = rgb_overlay
        _raw = rgb_raw
        _sm  = self._show_mask

        def _on_click(ev, ov=_ov, raw=_raw, grid=self):
            if ev.button() == Qt.LeftButton:
                dlg = ImageZoomDialog(ov, raw, show_mask=grid._show_mask,
                                      parent=grid)
                dlg.show()

        lbl.mousePressEvent = _on_click

        self.grid_lay.addWidget(lbl, patch_idx + 1, col + 1)
        self._cell_widgets[key] = lbl

        # Save full results for fullscreen view + mask toggle
        self._full_results[(patch_idx, col)] = rgb_overlay
        self._raw_results[(patch_idx, col)]  = rgb_raw

        n_total = self._n_patches * max(len(self._params), 1)
        if len(self._full_results) >= 1:
            self.btn_toggle_mask.setEnabled(True)
            self.btn_fullscreen.setEnabled(True)

    def _select(self, col):
        self._selected_col = col
        params = self._params[col]
        for i, btn in enumerate(self._col_btns):
            if i == col:
                btn.setStyleSheet(
                    "QPushButton{background:#246;color:#fff;"
                    "border:2px solid #4af;border-radius:3px;"
                    "font-size:11px;padding:2px;font-weight:bold;}"
                )
                btn.setText("✓ Selected")
            else:
                btn.setStyleSheet(
                    "QPushButton{background:#333;color:#aaa;"
                    "border:1px solid #555;border-radius:3px;"
                    "font-size:11px;padding:2px;}"
                    "QPushButton:hover{background:#445;color:#fff;}"
                )
                btn.setText("Select")
        self.sel_lbl.setText("Selected: " + self._pstr(params))
        self.param_selected.emit(params)

    def get_selected(self):
        if self._selected_col < 0:
            return None
        return self._params[self._selected_col]

    @staticmethod
    def _pstr(p):
        parts = []
        if "diameter" in p:
            dv = p['diameter']
            parts.append(f"diam={'auto' if dv is None else dv}")
        if "flow_threshold" in p:
            parts.append(f"flow={p['flow_threshold']}")
        if "cellprob_threshold" in p:
            parts.append(f"prob={p['cellprob_threshold']}")
        return "\n".join(parts)

    @staticmethod
    def _pkey(p):
        return json.dumps(p, sort_keys=True)

    def _toggle_mask(self):
        """Toggle mask boundary display in thumbnails."""
        self._show_mask = not self.btn_toggle_mask.isChecked()
        self.btn_toggle_mask.setText(
            "Show Mask" if not self._show_mask else "Hide Mask"
        )
        # Update all displayed thumbnails
        for key, lbl in self._cell_widgets.items():
            if not isinstance(lbl, QLabel):
                continue
            pm = (lbl.property("pm_mask") if self._show_mask
                  else lbl.property("pm_raw"))
            if pm is not None:
                lbl.setPixmap(pm)

    def _open_fullscreen(self):
        if not self._full_results:
            return
        win = ResultViewWindow(
            self._n_patches, self._params,
            self._full_results, self._raw_results,
            self._phase_desc, parent=self,
        )
        win.show()


# ══════════════════════════════════════════════════════════════════════
#  Fullscreen Result View Window
# ══════════════════════════════════════════════════════════════════════

class ImageZoomDialog(QtWidgets.QDialog):
    """Interactive zoom window for a single image (scroll to zoom / mid-right drag to pan / mask toggle)."""

    def __init__(self, rgb_overlay, rgb_raw, show_mask=True, parent=None):
        super().__init__(parent,
                         QtCore.Qt.Window |
                         QtCore.Qt.WindowMinMaxButtonsHint |
                         QtCore.Qt.WindowCloseButtonHint)
        self.setWindowTitle("Image Zoom View  (Scroll=Zoom  Mid/Right-drag=Pan)")
        self._rgb_ov   = rgb_overlay
        self._rgb_raw  = rgb_raw
        self._show_mask = show_mask
        self._pan_last  = None
        self._build_ui()
        self.resize(900, 700)

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)

        # Toolbar
        bar = QHBoxLayout()
        self.btn_mask = QPushButton(
            "Hide Mask" if self._show_mask else "Show Mask"
        )
        self.btn_mask.setCheckable(True)
        self.btn_mask.setChecked(not self._show_mask)
        self.btn_mask.setStyleSheet(
            "QPushButton{background:#353;color:#8e8;"
            "border:1px solid #8e8;border-radius:3px;padding:4px 10px;}"
            "QPushButton:checked{background:#533;color:#e88;"
            "border:1px solid #e88;}"
        )
        self.btn_mask.clicked.connect(self._toggle_mask)
        bar.addWidget(self.btn_mask)
        bar.addStretch()

        hint = QLabel("Scroll=Zoom  Drag=Pan  Double-click=Reset")
        hint.setStyleSheet("color:#555;font-size:10px;")
        bar.addWidget(hint)

        btn_close = QPushButton("✕ Close")
        btn_close.setStyleSheet(
            "QPushButton{color:#c44;padding:3px 10px;"
            "border:1px solid #c44;border-radius:3px;}"
        )
        btn_close.clicked.connect(self.close)
        bar.addWidget(btn_close)
        lay.addLayout(bar)

        # pyqtgraph canvas (single, lightweight)
        self.gv = pg.GraphicsLayoutWidget()
        self.gv.setBackground("#000")
        self.vb = self.gv.addViewBox()
        self.vb.setAspectLocked(True)
        self.vb.invertY(True)
        self.vb.setMouseEnabled(x=False, y=False)   # fully handled by eventFilter
        # Remove all boundary limits so panning beyond the image edge
        # never triggers an unwanted autoRange reset.
        self.vb.setLimits(xMin=None, xMax=None, yMin=None, yMax=None,
                          minXRange=None, maxXRange=None,
                          minYRange=None, maxYRange=None)
        self.ii = pg.ImageItem()
        self.vb.addItem(self.ii)
        lay.addWidget(self.gv, stretch=1)

        self._set_image(reset_view=True)
        self.gv.viewport().installEventFilter(self)

    def _set_image(self, reset_view=False):
        data = self._rgb_ov if self._show_mask else self._rgb_raw
        self.ii.setImage(data, autoLevels=False)
        if reset_view:
            self.vb.autoRange()

    def _toggle_mask(self):
        self._show_mask = not self.btn_mask.isChecked()
        self.btn_mask.setText("Hide Mask" if self._show_mask else "Show Mask")
        self._set_image(reset_view=False)   # preserve zoom & pan

    def mouseDoubleClickEvent(self, ev):
        """Double-click to reset view."""
        self.vb.autoRange()

    # ── Smooth zoom / pan (same strategy as OverviewPanel) ───────────

    def eventFilter(self, obj, event):
        if obj is not self.gv.viewport():
            return super().eventFilter(obj, event)
        t = event.type()

        if t == QtCore.QEvent.Wheel:
            delta  = event.angleDelta().y()
            factor = 1.15 ** (delta / 120.0)
            sp = self.gv.mapToScene(event.pos())
            ip = self.ii.mapFromScene(sp)
            cx, cy = ip.x(), ip.y()
            vr = self.vb.viewRange()
            self.vb.disableAutoRange()
            self.vb.setRange(
                xRange=[cx + (vr[0][0] - cx) / factor,
                         cx + (vr[0][1] - cx) / factor],
                yRange=[cy + (vr[1][0] - cy) / factor,
                         cy + (vr[1][1] - cy) / factor],
                padding=0,
            )
            return True

        elif t == QtCore.QEvent.MouseButtonPress:
            if event.button() in (Qt.LeftButton, Qt.MiddleButton, Qt.RightButton):
                self._pan_last = event.pos()
                return True

        elif t == QtCore.QEvent.MouseMove:
            if (event.buttons() & (Qt.LeftButton | Qt.MiddleButton | Qt.RightButton)
                    and self._pan_last is not None):
                dp  = event.pos() - self._pan_last
                self._pan_last = event.pos()
                vr  = self.vb.viewRange()
                vpw = max(1, self.gv.viewport().width())
                vph = max(1, self.gv.viewport().height())
                dx  = -dp.x() * (vr[0][1] - vr[0][0]) / vpw
                dy  = -dp.y() * (vr[1][1] - vr[1][0]) / vph
                self.vb.disableAutoRange()
                self.vb.setRange(
                    xRange=[vr[0][0] + dx, vr[0][1] + dx],
                    yRange=[vr[1][0] + dy, vr[1][1] + dy],
                    padding=0,
                )
                return True

        elif t == QtCore.QEvent.MouseButtonRelease:
            if event.button() in (Qt.LeftButton, Qt.MiddleButton, Qt.RightButton):
                self._pan_last = None
                return True

        elif t == QtCore.QEvent.MouseButtonDblClick:
            self.vb.autoRange()
            return True

        return False


class ResultViewWindow(QMainWindow):
    """Maximized window, static QLabel+QPixmap grid (fast), click any image to open ImageZoomDialog."""

    CELL_SIZE = 460   # cell display edge length (px)

    def __init__(self, n_patches, param_list,
                 full_results, raw_results, phase_desc, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Fullscreen Result View — {phase_desc}")
        self._n_patches  = n_patches
        self._params     = param_list
        self._results    = full_results   # {(row,col): ndarray} with mask
        self._raw        = raw_results    # {(row,col): ndarray} without mask
        self._phase_desc = phase_desc
        self._show_mask  = True
        self._cell_labels = {}            # (row,col) → QLabel
        self._build_ui()
        self.showMaximized()

    def _build_ui(self):
        cw  = QWidget()
        self.setCentralWidget(cw)
        lay = QVBoxLayout(cw)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)

        # ── Title bar ───────────────────────────────────────────────
        hdr = QHBoxLayout()
        ttl = QLabel(self._phase_desc)
        ttl.setStyleSheet("font-size:14px;font-weight:bold;color:#eee;padding:2px;")
        hdr.addWidget(ttl, stretch=1)

        self.btn_mask = QPushButton("Hide Mask")
        self.btn_mask.setCheckable(True)
        self.btn_mask.setStyleSheet(
            "QPushButton{background:#353;color:#8e8;"
            "border:1px solid #8e8;border-radius:3px;padding:3px 10px;}"
            "QPushButton:checked{background:#533;color:#e88;"
            "border:1px solid #e88;}"
        )
        self.btn_mask.clicked.connect(self._toggle_mask)
        hdr.addWidget(self.btn_mask)

        hint = QLabel("Click image to zoom")
        hint.setStyleSheet("color:#555;font-size:10px;")
        hdr.addWidget(hint)

        btn_close = QPushButton("✕  Close")
        btn_close.setStyleSheet(
            "QPushButton{color:#c44;padding:4px 14px;"
            "border:1px solid #c44;border-radius:4px;}"
            "QPushButton:hover{background:#411;}"
        )
        btn_close.clicked.connect(self.close)
        hdr.addWidget(btn_close)
        lay.addLayout(hdr)

        # ── Scrollable grid (pure QLabel — no pyqtgraph overhead) ───
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea{border:none;background:#0d0d0d;}")
        grid_w = QWidget()
        grid_w.setStyleSheet("background:#0d0d0d;")
        self._grid_lay = QtWidgets.QGridLayout(grid_w)
        self._grid_lay.setSpacing(8)
        scroll.setWidget(grid_w)
        lay.addWidget(scroll, stretch=1)

        W = self.CELL_SIZE

        # Column headers
        for col, params in enumerate(self._params):
            lbl = QLabel(ResultGridPanel._pstr(params))
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setWordWrap(True)
            lbl.setFixedWidth(W + 8)
            lbl.setStyleSheet(
                "font-size:11px;color:#ccc;padding:4px;"
                "background:#1c1c1c;border:1px solid #444;border-radius:3px;"
            )
            self._grid_lay.addWidget(lbl, 0, col + 1)

        # Rows: each patch
        for row in range(self._n_patches):
            pl = QLabel(f"P{row+1}")
            pl.setStyleSheet(
                f"color:{PATCH_COLORS[row % len(PATCH_COLORS)]};"
                f"font-weight:bold;font-size:16px;"
            )
            pl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._grid_lay.addWidget(pl, row + 1, 0)

            for col in range(len(self._params)):
                rgb_ov = self._results.get((row, col))
                rgb_rw = self._raw.get((row, col))

                lbl = QLabel()
                lbl.setFixedSize(W + 8, W + 8)
                lbl.setAlignment(Qt.AlignCenter)
                lbl.setStyleSheet(
                    "background:#111;border:1px solid #2a2a2a;"
                )

                if rgb_ov is not None:
                    pm_ov = ResultGridPanel._to_pixmap(rgb_ov, W)
                    pm_rw = ResultGridPanel._to_pixmap(rgb_rw, W) if rgb_rw is not None else pm_ov
                    lbl.setProperty("pm_mask", pm_ov)
                    lbl.setProperty("pm_raw",  pm_rw)
                    lbl.setPixmap(pm_ov)
                    lbl.setCursor(Qt.PointingHandCursor)
                    lbl.setToolTip("Click to zoom (scroll to zoom, drag to pan)")

                    _ov, _rw, _win = rgb_ov, rgb_rw, self
                    def _click(ev, ov=_ov, rw=_rw, win=_win):
                        if ev.button() == Qt.LeftButton:
                            dlg = ImageZoomDialog(
                                ov, rw,
                                show_mask=win._show_mask,
                                parent=win,
                            )
                            dlg.show()
                    lbl.mousePressEvent = _click
                else:
                    lbl.setText("Waiting...")
                    lbl.setStyleSheet(
                        "background:#111;color:#444;font-size:12px;"
                        "border:1px solid #2a2a2a;"
                    )

                self._grid_lay.addWidget(lbl, row + 1, col + 1)
                self._cell_labels[(row, col)] = lbl

    def _toggle_mask(self):
        self._show_mask = not self.btn_mask.isChecked()
        self.btn_mask.setText("Show Mask" if not self._show_mask else "Hide Mask")
        for lbl in self._cell_labels.values():
            pm = (lbl.property("pm_mask") if self._show_mask
                  else lbl.property("pm_raw"))
            if pm is not None:
                lbl.setPixmap(pm)


# ══════════════════════════════════════════════════════════════════════
#  Search Control Panel
# ══════════════════════════════════════════════════════════════════════

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
        self.run_p2.emit({
            "diameter": self._p2_diam,   # None = auto, float = override
            "flow": flows, "prob": probs,
        })

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
                p = json.load(f)
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
            params = {
                "diameter":           d if d > 0 else None,
                "flow_threshold":     fl,
                "cellprob_threshold": cp,
                "_source":            "loaded",
            }
            self.params_ready.emit(params)
        except Exception as e:
            QMessageBox.warning(self, "Error", "Failed to load params:\n" + str(e))

    def _use_manual_params(self):
        """Use the manually entered spinbox values."""
        d  = self._man_diam.value()
        fl = self._man_flow.value()
        cp = self._man_prob.value()
        params = {
            "diameter":           d if d > 0 else None,
            "flow_threshold":     fl,
            "cellprob_threshold": cp,
            "_source":            "manual",
        }
        self._loaded_lbl.setText(
            f"✓ Manual  diam={d}  flow={fl}  prob={cp}"
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
        return {
            "diameter":           d if d > 0 else None,
            "flow_threshold":     fl,
            "cellprob_threshold": cp,
        }


# ══════════════════════════════════════════════════════════════════════
#  Main Window
# ══════════════════════════════════════════════════════════════════════

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
        self._fused_zarr_path    = None
        self._rois               = []
        self._params_source      = None  # 'phase2'|'loaded'|'manual' — tracks how params were set

        self._preload_debounce = QTimer()
        self._preload_debounce.setSingleShot(True)
        self._preload_debounce.timeout.connect(self._preload_all_patches)

        self._prev_timer = QTimer()
        self._prev_timer.setSingleShot(True)
        self._prev_timer.timeout.connect(self._render_current_patch)

        self._build_ui()

    # ── UI ──────────────────────────────────────────────────────────

    def _build_ui(self):
        # Outer container holds the QStackedWidget + bottom step indicator
        outer_w = QWidget()
        self.setCentralWidget(outer_w)
        outer_lay = QVBoxLayout(outer_w)
        outer_lay.setContentsMargins(0, 0, 0, 0)
        outer_lay.setSpacing(0)

        # Step indicator bar
        step_bar = QHBoxLayout()
        step_bar.setContentsMargins(8, 4, 8, 4)
        self._step1_lbl = QLabel('● Step 1: Channel Fusion')
        self._step1_lbl.setStyleSheet(
            'font-size:12px;font-weight:bold;color:#61afef;padding:4px 12px;'
            'background:#1a2a3a;border-radius:4px;'
        )
        self._step2_lbl = QLabel('○ Step 2: Segmentation & Merge')
        self._step2_lbl.setStyleSheet(
            'font-size:12px;color:#555;padding:4px 12px;'
        )
        self._step3_lbl = QLabel('○ Step 3: QC Viewer')
        self._step3_lbl.setStyleSheet(
            'font-size:12px;color:#555;padding:4px 12px;'
        )
        self._step4_lbl = QLabel('○ Step 4: Feature Extraction')
        self._step4_lbl.setStyleSheet(
            'font-size:12px;color:#555;padding:4px 12px;'
        )
        step_bar.addWidget(self._step1_lbl)
        step_bar.addWidget(QLabel('  →  '))
        step_bar.addWidget(self._step2_lbl)
        step_bar.addWidget(QLabel('  →  '))
        step_bar.addWidget(self._step3_lbl)
        step_bar.addWidget(QLabel('  →  '))
        step_bar.addWidget(self._step4_lbl)
        step_bar.addStretch()
        # Skip button: go directly to Step 2 without running Step 1
        self._btn_skip = QPushButton('Skip → Go to Step 2')
        self._btn_skip.setStyleSheet(
            'QPushButton{color:#888;font-size:11px;border:1px solid #555;'
            'border-radius:3px;padding:3px 10px;}'
            'QPushButton:hover{color:#ccc;border-color:#888;}'
        )
        self._btn_skip.clicked.connect(self._go_to_step2)
        step_bar.addWidget(self._btn_skip)

        self._btn_skip3 = QPushButton('Skip → Go to Step 3')
        self._btn_skip3.setStyleSheet(
            'QPushButton{color:#888;font-size:11px;border:1px solid #555;'
            'border-radius:3px;padding:3px 10px;}'
            'QPushButton:hover{color:#ccc;border-color:#888;}'
        )
        self._btn_skip3.clicked.connect(self._go_to_step3)
        step_bar.addWidget(self._btn_skip3)

        self._btn_skip4 = QPushButton('Skip → Go to Step 4')
        self._btn_skip4.setStyleSheet(
            'QPushButton{color:#888;font-size:11px;border:1px solid #555;'
            'border-radius:3px;padding:3px 10px;}'
            'QPushButton:hover{color:#ccc;border-color:#888;}'
        )
        self._btn_skip4.clicked.connect(self._go_to_step4)
        step_bar.addWidget(self._btn_skip4)

        self._btn_next = QPushButton('Next → Step 2')
        self._btn_next.setEnabled(False)
        self._btn_next.setStyleSheet(
            'QPushButton{background:#2a5;color:white;font-size:12px;'
            'font-weight:bold;border-radius:4px;padding:4px 14px;}'
            'QPushButton:hover{background:#3b6;}'
            'QPushButton:disabled{background:#333;color:#555;}'
        )
        self._btn_next.clicked.connect(self._go_to_step2)
        step_bar.addWidget(self._btn_next)
        outer_lay.addLayout(step_bar)

        # Stacked widget
        self._stack = QtWidgets.QStackedWidget()
        outer_lay.addWidget(self._stack, stretch=1)

        # ── Page 0: Step 1 content (wrap in QWidget) ──────────────────
        page1_w = QWidget()
        page1_lay = QVBoxLayout(page1_w)
        page1_lay.setContentsMargins(6, 4, 6, 6)
        page1_lay.setSpacing(4)

        # ── File selection bar ────────────────────────────────────────
        file_bar = QWidget()
        file_bar.setStyleSheet(
            'background:#1a1a2a;border-radius:4px;padding:2px;'
        )
        fb_lay = QHBoxLayout(file_bar)
        fb_lay.setContentsMargins(8, 4, 8, 4)
        fb_lay.setSpacing(6)

        # OME-TIFF
        fb_lay.addWidget(QLabel('OME-TIFF:'))
        self._ome_path_edit = QtWidgets.QLineEdit(OME_TIFF_FILE)
        self._ome_path_edit.setStyleSheet(
            'font-size:11px;background:#111;color:#ddd;'
            'border:1px solid #444;border-radius:3px;padding:2px 4px;'
        )
        self._ome_path_edit.setMinimumWidth(300)
        fb_lay.addWidget(self._ome_path_edit, stretch=2)
        btn_ome = QPushButton('Browse')
        btn_ome.setFixedWidth(58)
        btn_ome.setStyleSheet(
            'QPushButton{font-size:10px;color:#8cf;border:1px solid #8cf;'
            'border-radius:3px;padding:2px 6px;}'
            'QPushButton:hover{background:#1a2a4a;}'
        )
        btn_ome.clicked.connect(self._browse_ome)
        fb_lay.addWidget(btn_ome)

        # Output dir
        fb_lay.addWidget(QLabel('Output dir:'))
        self._out_path_edit = QtWidgets.QLineEdit(OUTPUT_DIR)
        self._out_path_edit.setStyleSheet(
            'font-size:11px;background:#111;color:#ddd;'
            'border:1px solid #444;border-radius:3px;padding:2px 4px;'
        )
        self._out_path_edit.setMinimumWidth(200)
        fb_lay.addWidget(self._out_path_edit, stretch=1)
        btn_out = QPushButton('Browse')
        btn_out.setFixedWidth(58)
        btn_out.setStyleSheet(
            'QPushButton{font-size:10px;color:#8cf;border:1px solid #8cf;'
            'border-radius:3px;padding:2px 6px;}'
            'QPushButton:hover{background:#1a2a4a;}'
        )
        btn_out.clicked.connect(self._browse_out_dir)
        fb_lay.addWidget(btn_out)

        # Panel CSV
        fb_lay.addWidget(QLabel('Panel CSV:'))
        self._panel_csv_edit = QtWidgets.QLineEdit()
        self._panel_csv_edit.setPlaceholderText('panel.csv  (marker, group)')
        self._panel_csv_edit.setStyleSheet(
            'font-size:11px;background:#111;color:#ddd;'
            'border:1px solid #444;border-radius:3px;padding:2px 4px;'
        )
        self._panel_csv_edit.setMinimumWidth(200)
        fb_lay.addWidget(self._panel_csv_edit, stretch=1)
        btn_panel = QPushButton('Browse')
        btn_panel.setFixedWidth(58)
        btn_panel.setStyleSheet(
            'QPushButton{font-size:10px;color:#8cf;border:1px solid #8cf;'
            'border-radius:3px;padding:2px 6px;}'
            'QPushButton:hover{background:#1a2a4a;}'
        )
        btn_panel.clicked.connect(self._browse_panel_csv)
        fb_lay.addWidget(btn_panel)

        # Load button
        btn_load = QPushButton('▶  Load')
        btn_load.setFixedWidth(70)
        btn_load.setStyleSheet(
            'QPushButton{background:#2a5;color:white;font-weight:bold;'
            'font-size:11px;border-radius:3px;padding:3px 8px;}'
            'QPushButton:hover{background:#3b6;}'
        )
        btn_load.clicked.connect(self._reload_from_paths)
        fb_lay.addWidget(btn_load)

        page1_lay.addWidget(file_bar)

        # ── build Step 1 internals into page1_lay ─────────────────────
        # (Previously this was the entire _build_ui body up to root.addLayout(bot))
        # We inject the main_split + bottom bar into page1_lay instead of root

        cw   = page1_w      # alias so existing code writes into page1_w
        root = page1_lay    # alias so existing code uses page1_lay

        main_split = QSplitter(Qt.Horizontal)

        # Left: overview — lazy=True means no thumbnail load until user clicks Load
        # Pass a placeholder loader; real loader is set in _reload_from_paths()
        _dummy_loader = type('_DummyLoader', (), {
            'shape': (0, 0), 'ch_map': {}, 'channel_names': lambda s: []
        })()
        self.overview = OverviewPanel(
            _dummy_loader, NUCLEUS_CONFIG["channel"], lazy=True
        )
        self.overview.patches_changed.connect(self._on_patches)
        self.overview.rois_changed.connect(self._on_rois_changed)
        self.overview.setMinimumWidth(320)
        main_split.addWidget(self.overview)

        # Middle: preview + fusion config
        mid = QSplitter(Qt.Vertical)

        # Preview
        pw = QWidget()
        pl = QVBoxLayout(pw)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.addWidget(self._make_label(
            "② Fusion Preview  Red=cyto  Blue=nucleus  (real-time update)",
            bold=True,
        ))

        # ── Patch selector row ───────────────────────────────────────
        sel_row = QHBoxLayout()
        sel_row.addWidget(QLabel("Preview patch:"))
        self._patch_sel_btns = []   # list of QPushButton, one per patch
        self._patch_sel_container = QHBoxLayout()
        self._patch_sel_container.setSpacing(4)
        sel_row.addLayout(self._patch_sel_container)
        sel_row.addStretch()
        pl.addLayout(sel_row)

        # Status bar + manual refresh button (same row)
        status_row = QHBoxLayout()
        self.prev_status = QLabel("Please drag to add a Patch on the left first")
        self.prev_status.setAlignment(Qt.AlignCenter)
        self.prev_status.setStyleSheet("color:#777;font-size:10px;")
        self.prev_status.setWordWrap(True)
        status_row.addWidget(self.prev_status, stretch=1)

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
        self.prev_gv  = pg.GraphicsLayoutWidget()
        self.prev_gv.setBackground("#111")
        self.prev_vb  = self.prev_gv.addViewBox()
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

        # Right: search control + result grid
        right = QSplitter(Qt.Vertical)
        self.search = SearchCtrlPanel()
        self.search.run_p1.connect(self._run_p1)
        self.search.run_p2.connect(self._run_p2)
        self.search.stop.connect(self._stop)
        self.search.params_ready.connect(self._on_params_ready)
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

        # Bottom: save button + fusion progress bar
        bot = QHBoxLayout()
        bot.addStretch()
        self.btn_save = QPushButton(
            "💾  Save Config  &  Generate fused.zarr"
        )
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

        # Fusion progress area (hidden until fusion starts)
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

        # ── Finalise Step 1 page ──────────────────────────────────────
        self._stack.addWidget(page1_w)

        # ── Page 1: Step 2 ────────────────────────────────────────────
        self._step2 = Step2Page()
        self._step2.go_back.connect(self._go_to_step1)
        self._step2.segmentation_done.connect(self._go_to_step3)
        self._stack.addWidget(self._step2)

        # ── Page 2: Step 3 ────────────────────────────────────────────
        self._step3 = Step3Page()
        self._step3.go_back.connect(self._go_to_step2)
        self._step3.go_step4.connect(self._go_to_step4)
        self._stack.addWidget(self._step3)

        # ── Page 3: Step 4 ────────────────────────────────────────────
        self._step4 = Step4Page()
        self._step4.go_back.connect(self._go_to_step3)
        self._stack.addWidget(self._step4)

        # Start on Step 1
        self._stack.setCurrentIndex(0)
        self._set_step_active(1)

    # ── File path selection ──────────────────────────────────────────

    @staticmethod
    def _parse_panel_csv(path):
        """
        Parse a panel CSV with flexible columns such as:
        channel_name, marker, role, group

        Returns (groups_dict, nuc_ch_str)
        groups_dict: {'group_name': {'CH_NAME': 1.0, ...}, ...}
        """
        import csv
        groups = {}
        nucleus_rows = []
        dapi_fallback = None

        def _norm_key(v):
            return (v or "").strip().lower()

        def _is_dapi(*values):
            return any(_norm_key(v) == "dapi" for v in values)

        with open(path, newline='', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return {}, None

            for row in reader:
                row = {
                    (k or "").strip().lower(): (v or "").strip()
                    for k, v in row.items()
                }
                ch_name = (
                    row.get('channel_name')
                    or row.get('channel')
                    or row.get('name')
                    or row.get('marker')
                    or ""
                ).strip()
                marker = (row.get('marker') or "").strip()
                role = _norm_key(row.get('role'))
                group = (
                    row.get('group')
                    or row.get('category')
                    or row.get('class')
                    or ""
                ).strip()

                if not ch_name:
                    continue

                group_norm = _norm_key(group)
                is_nucleus = role == 'nucleus' or group_norm in ('nucleus', 'dapi', 'nuclear')

                # FIX: nucleus selection logic
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
            self, 'Select OME-TIFF',
            os.path.dirname(self._ome_path_edit.text()),
            'OME-TIFF (*.tif *.tiff)'
        )
        if path:
            self._ome_path_edit.setText(path)

    def _browse_panel_csv(self):
        csv_dir = os.path.dirname(self._panel_csv_edit.text()) \
                  if self._panel_csv_edit.text().strip() \
                  else os.path.dirname(self._ome_path_edit.text())
        path, _ = QFileDialog.getOpenFileName(
            self, 'Select Panel CSV', csv_dir, 'CSV (*.csv)'
        )
        if path:
            self._panel_csv_edit.setText(path)

    def _browse_out_dir(self):
        path = QFileDialog.getExistingDirectory(
            self, 'Select Output Directory',
            self._out_path_edit.text()
        )
        if path:
            self._out_path_edit.setText(path)

    def _reload_from_paths(self):
        """Reload OMETIFFLoader and overview from the selected file paths."""
        global OME_TIFF_FILE, OUTPUT_DIR

        ome  = self._ome_path_edit.text().strip()
        outd = self._out_path_edit.text().strip()

        if not ome or not os.path.exists(ome):
            QMessageBox.warning(self, 'File not found',
                                f'OME-TIFF not found:\n{ome}')
            return

        OME_TIFF_FILE = ome
        OUTPUT_DIR    = outd if outd else os.path.dirname(ome)
        self._out_path_edit.setText(OUTPUT_DIR)
        os.makedirs(OUTPUT_DIR, exist_ok=True)

        # Reload loader
        try:
            self.loader = OMETIFFLoader(OME_TIFF_FILE, CHANNEL_NAME_MAP)
        except Exception as e:
            QMessageBox.critical(self, 'Load error', str(e))
            return

        # Update config panel channel list
        self.config.all_channels = self.loader.channel_names()
        self.config.nuc_combo.clear()
        self.config.nuc_combo.addItems(self.loader.channel_names())
        # FIX: initial weights
        self.config.nuc_combo.setCurrentIndex(-1)
        self.config.nuc_row.spin.setValue(0.0)
        self.config.load_panel({}, None)

        # ── Panel CSV: load existing or auto-generate template ────────
        panel_csv = self._panel_csv_edit.text().strip()
        if panel_csv and os.path.exists(panel_csv):
            # Load existing CSV
            try:
                groups, nuc_ch = self._parse_panel_csv(panel_csv)
                self.config.load_panel(groups, nuc_ch)
            except Exception as e:
                QMessageBox.warning(self, 'Panel CSV error',
                                    f'Failed to parse panel CSV:\n{e}')
        else:
            # Auto-generate template from OME-TIFF channel names
            template_path = os.path.join(OUTPUT_DIR, 'panel.csv')
            try:
                import csv as _csv
                os.makedirs(OUTPUT_DIR, exist_ok=True)
                with open(template_path, 'w', newline='', encoding='utf-8') as f:
                    w = _csv.writer(f)
                    w.writerow(['channel_name', 'marker', 'role', 'group'])
                    for ch in self.loader.channel_names():
                        w.writerow([ch, ch, '', ''])
                self._panel_csv_edit.setText(template_path)
                panel_csv = template_path
                QMessageBox.information(
                    self, 'Panel template generated',
                    f'Panel CSV template created:\n{template_path}\n\n'
                    f'{len(self.loader.channel_names())} channels listed.\n\n'
                    f'Please open the file, fill in the "role" and/or "group" columns\n'
                    f'(use role="nucleus" for nucleus channels, any group name for others),\n'
                    f'then click Load again to apply.'
                )
            except Exception as e:
                QMessageBox.warning(self, 'Template generation failed', str(e))

        # Clear patch cache
        self._stop_all_loaders()
        self._patch_channel_cache.clear()
        self._patch_load_ready.clear()
        self._all_patches.clear()
        self._preview_patch_idx = -1
        self.prev_status.setText("Please drag to add a Patch on the left first")
        self.prev_img.clear()

        # Update overview panel with new loader and dimensions
        self.overview.loader  = self.loader
        self.overview.full_h  = self.loader.shape[0]
        self.overview.full_w  = self.loader.shape[1]
        self.overview._rois.clear()
        self.overview._patches.clear()
        self.overview.img_item.clear()
        self.overview._load_overview()   # reload thumbnail

        # Update Step 2 / Step 4 default paths
        if hasattr(self, '_step2'):
            self._step2._out_edit.setText(OUTPUT_DIR)
        if hasattr(self, '_step4'):
            self._step4._ome_edit.setText(OME_TIFF_FILE)
            self._step4._out_edit.setText(OUTPUT_DIR)

        panel_info = (f'Panel CSV: {os.path.basename(panel_csv)}'
                      if panel_csv and os.path.exists(panel_csv)
                      else 'Panel CSV: template generated (fill in groups, then Load again)')
        QMessageBox.information(
            self, 'Loaded',
            f'OME-TIFF: {os.path.basename(OME_TIFF_FILE)}\n'
            f'{self.loader.shape[0]:,}×{self.loader.shape[1]:,} px  '
            f'{len(self.loader.ch_map)} channels\n\n'
            f'Output dir: {OUTPUT_DIR}\n'
            f'{panel_info}'
        )

    def _go_to_step2(self):
        """Switch to Step 2 page; pass fused.zarr path and ROIs if available."""
        if self._fused_zarr_path:
            self._step2.set_zarr_path(self._fused_zarr_path)
        if self._rois:
            self._step2.set_rois(self._rois)
        self._stack.setCurrentIndex(1)
        self._set_step_active(2)

    def _go_to_step1(self):
        """Switch back to Step 1; all state is preserved."""
        self._stack.setCurrentIndex(0)
        self._set_step_active(1)

    def _go_to_step3(self, output_dir=None):
        """Switch to Step 3 QC viewer."""
        if output_dir:
            self._step3.set_output_dir(output_dir)
        self._stack.setCurrentIndex(2)
        self._set_step_active(3)

    def _go_to_step4(self, output_dir=None):
        """Switch to Step 4 feature extraction."""
        if output_dir:
            mask = os.path.join(output_dir, 'global_mask.dat')
            if not os.path.exists(mask):
                mask = os.path.join(output_dir, 'global_mask.ome.tiff')
            self._step4.set_paths(
                mask_path     = mask if os.path.exists(mask) else '',
                ome_tiff_path = OME_TIFF_FILE,
                output_dir    = output_dir,
            )
        self._stack.setCurrentIndex(3)
        self._set_step_active(4)

    def _set_step_active(self, active):
        _on  = ('font-size:12px;font-weight:bold;color:#61afef;padding:4px 12px;'
                'background:#1a2a3a;border-radius:4px;')
        _off = 'font-size:12px;color:#555;padding:4px 12px;'
        self._step1_lbl.setStyleSheet(_on  if active == 1 else _off)
        self._step2_lbl.setStyleSheet(_on  if active == 2 else _off)
        self._step3_lbl.setStyleSheet(_on  if active == 3 else _off)
        self._step4_lbl.setStyleSheet(_on  if active == 4 else _off)

    @staticmethod
    def _make_label(text, bold=False):
        lbl = QLabel(text)
        lbl.setAlignment(Qt.AlignCenter)
        style = "font-size:12px;color:#ddd;background:#1a1a1a;padding:4px;"
        if bold:
            style += "font-weight:bold;"
        lbl.setStyleSheet(style)
        return lbl

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
            self.prev_status.setText("Please drag to add a Patch on the left first")
            return

        # Drop cache for patches whose ROI coordinates changed
        for idx, roi in enumerate(patches):
            if idx < len(old_rois) and roi != old_rois[idx]:
                self._patch_channel_cache.pop(idx, None)
                self._patch_load_ready.discard(idx)
                if idx in self._patch_loaders:
                    self._patch_loaders[idx].stop()
                    self._patch_loaders.pop(idx, None)

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
            self.prev_status.setText("Preloading all patches in background…")

        # Debounce: start background preload 400 ms after last patch change
        self._preload_debounce.start(400)

    def _select_preview_patch(self, idx):
        """User clicked a patch button — render from cache if ready, else show status."""
        if idx < 0 or idx >= len(self._all_patches):
            return
        for i, btn in enumerate(self._patch_sel_btns):
            btn.setChecked(i == idx)
        self._preview_patch_idx = idx

        if idx in self._patch_load_ready:
            self._render_current_patch(reset_view=True)
        elif idx in self._patch_loaders:
            self.prev_status.setText(f"P{idx+1} still loading… please wait")
        else:
            # Not yet started — kick off a single loader for this patch immediately
            self._start_loader_for(idx)

    # ── Background preloading ────────────────────────────────────────

    def _needed_channels(self):
        """Return the list of channel names required by the current config."""
        needed_cfg = set()
        for cw in self.config.get_groups().values():
            needed_cfg.update(cw.keys())
        nuc_ch, _ = self.config.get_nucleus()
        needed_cfg.add(nuc_ch)
        needed  = [ch for ch in needed_cfg if ch in self.loader.ch_map]
        missing = needed_cfg - set(needed)
        if missing:
            print(f"[Preview] Channels not found in OME-TIFF (skipped): {sorted(missing)}")
        return needed

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

        # Stop any existing loader for this patch
        old = self._patch_loaders.pop(idx, None)
        if old is not None:
            old.stop()
            try:
                old.done.disconnect()
                old.progress.disconnect()
                old.error.disconnect()
            except Exception:
                pass

        y0, y1, x0, x1 = self._all_patches[idx]
        t = PreviewLoaderThread(idx, self.loader, needed,
                                y0, y1, x0, x1,
                                downsample=PREVIEW_DOWNSAMPLE)
        t.done.connect(self._on_patch_loaded)
        t.progress.connect(self._on_patch_progress)
        t.error.connect(self._on_patch_error)
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
        self._patch_loaders.pop(patch_idx, None)
        self._set_patch_btn_state(patch_idx, 'ready')

        nuc_ch, _ = self.config.get_nucleus()
        y0, y1, x0, x1 = self._all_patches[patch_idx]
        h = (y1 - y0) // PREVIEW_DOWNSAMPLE
        w = (x1 - x0) // PREVIEW_DOWNSAMPLE
        print(f"[Preview] P{patch_idx+1} ready — {len(cache)} ch, {h}×{w} px")

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
        self._patch_loaders.pop(patch_idx, None)
        self._set_patch_btn_state(patch_idx, 'error')
        if patch_idx == self._preview_patch_idx:
            self.prev_status.setText(f"⚠ P{patch_idx+1} load error: {msg}")
        print(f"[Preview] P{patch_idx+1} error: {msg}")

    def _stop_all_loaders(self):
        for t in self._patch_loaders.values():
            t.stop()
            try:
                t.done.disconnect()
                t.progress.disconnect()
                t.error.disconnect()
            except Exception:
                pass
        self._patch_loaders.clear()

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
            return
        cache = self._patch_channel_cache.get(idx)
        if not cache:
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

    # ── Phase 1 ─────────────────────────────────────────────────────

    def _run_p1(self, diameters):
        """
        diameters is a single-element list: [None] for auto, [float] for override.
        Phase 1 now runs ONE inference per patch (not a grid search),
        showing the auto-diameter result for the user to visually confirm.
        After completion, diameter is automatically passed to Phase 2.
        """
        patches = self.overview.get_patches()
        if not patches:
            QMessageBox.warning(self, "Info", "Please add at least one Patch first")
            return

        diam = diameters[0]   # None or float
        param_list = [
            {"diameter": diam,
             "flow_threshold": 0.4,
             "cellprob_threshold": 0.0,
             "_phase": 1}
        ]
        tasks = [
            (pi, roi, dict(param_list[0]))
            for pi, roi in enumerate(patches)
        ]
        diam_str = "auto" if diam is None else f"{diam} px"
        self.result_grid.setup_grid(
            len(patches), param_list,
            f"Phase 1 — auto-diameter preview  (diameter={diam_str},"
            f"  {len(patches)} patch(es))"
        )
        self._launch_worker(tasks)

    # ── Phase 2 ─────────────────────────────────────────────────────

    def _run_p2(self, cfg):
        patches = self.overview.get_patches()
        if not patches:
            QMessageBox.warning(self, "Info", "Please add Patches first")
            return

        diam  = cfg["diameter"]
        param_list = [
            {"diameter": diam,
             "flow_threshold": f,
             "cellprob_threshold": p,
             "_phase": 2}
            for f in cfg["flow"]
            for p in cfg["prob"]
        ]
        tasks = [
            (pi, roi, dict(p))
            for pi, roi in enumerate(patches)
            for p in param_list
        ]
        self.result_grid.setup_grid(
            len(patches), param_list,
            f"Phase 2 — flow × cellprob  diameter={diam}  ({len(tasks)} inferences)"
        )
        self._launch_worker(tasks)

    # ── Worker ──────────────────────────────────────────────────────

    def _launch_worker(self, tasks):
        if self.worker and self.worker.isRunning():
            return
        nuc_ch, nuc_w = self.config.get_nucleus()
        self.worker = CellposeWorker(
            tasks, self.loader, self.fusion,
            self.config.get_groups(),
            self.config.get_group_weights(),
            nuc_ch, nuc_w,
        )
        self.worker.result_ready.connect(
            lambda pi, p, rgb_ov, rgb_raw: self.result_grid.add_result(pi, p, rgb_ov, rgb_raw)
        )
        self.worker.progress.connect(self.search.update_progress)
        self.worker.finished_all.connect(self._on_done)
        self.worker.error_occurred.connect(
            lambda msg: print(f"[Worker] {msg}")
        )
        self.search.set_running(True)
        self.search.update_progress(0, len(tasks), "Starting...")
        self.worker.start()

    def _stop(self):
        if self.worker:
            self.worker.stop()

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

    def _on_params_ready(self, params):
        """Called when user loads a JSON file or clicks 'Use These Params'."""
        self._p2_params    = params
        self._params_source = params.get("_source", "manual")
        self._check_save_unlock()

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
            self._fusion_lbl.setText(
                f"Params ready ({src_lbl})  "
                f"diam={d}  flow={fl}  prob={cp}  "
                f"→ click Save to generate fused.zarr"
            )
            self._fusion_bar_widget.setVisible(True)

    # ── Lock / unlock UI during fusion ──────────────────────────────

    def _lock_ui(self):
        """Disable all interactive elements during fusion."""
        self.btn_save.setEnabled(False)
        self.config.setEnabled(False)
        self.search.setEnabled(False)
        self.overview.setEnabled(False)

    def _unlock_ui(self):
        """Re-enable UI after fusion completes or errors."""
        self.config.setEnabled(True)
        self.search.setEnabled(True)
        self.overview.setEnabled(True)
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
        self._fusion_lbl.setText(f"✓  Fusion complete → {zarr_path}")
        self._fused_zarr_path = zarr_path
        self._btn_next.setEnabled(True)
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
            self, "Fusion complete",
            f"{'ROI' if self._rois else 'Full WSI'} fusion done  "
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
        cpcfg = {
            "model_type":         "cpsam",
            "diameter":           self._p2_params.get("diameter"),
            "flow_threshold":     self._p2_params.get("flow_threshold", 0.4),
            "cellprob_threshold": self._p2_params.get("cellprob_threshold", 0.0),
            "min_size":           15,
            "phase1_diameter":    self._p1_diam,
            "params_source":      self._params_source or "unknown",
            "saved_at":           time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        fp2 = os.path.join(OUTPUT_DIR, "cellpose_params.json")
        with open(fp2, "w", encoding="utf-8") as f:
            json.dump(cpcfg, f, indent=2, ensure_ascii=False)
        print(f"[Save] {fp1}")
        print(f"[Save] {fp2}")

        # ── Tile selection dialog ─────────────────────────────────────
        # Count active channels for RAM estimate
        active_ch = set([fcfg["nucleus"]["channel"]])
        for gdata in fcfg["groups"].values():
            active_ch.update(gdata["channels"].keys())
        n_channels = len([ch for ch in active_ch if ch in self.loader.ch_map])

        try:
            import psutil
            sys_ram_gb = int(psutil.virtual_memory().total / 1e9)
        except ImportError:
            sys_ram_gb = 128   # conservative default

        dlg = TileSelectDialog(
            self.loader.shape[0],
            self.loader.shape[1],
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
            fusion_cfg = fcfg,
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
        self._fusion_lbl.setText(
            f"Starting fusion  {n_rows}×{n_cols} = {n_rows*n_cols} tiles…"
        )
        self._fusion_worker.start()



# ══════════════════════════════════════════════════════════════════════
#  Segment + Merge Worker  (block03 + block04 combined)
# ══════════════════════════════════════════════════════════════════════

class SegmentMergeWorker(QThread):
    """
    Runs Cellpose tile-by-tile on fused.zarr, then streams results into
    a global numpy memmap (no intermediate .npy files in normal mode).

    Tile ownership:
      Each tile is read with OVERLAP_PX padding on all sides.
      After inference, only cells whose centroid falls inside the tile's
      "own" region (without overlap) are kept.  This guarantees every
      cell is counted exactly once and no cell is truncated.

    Output:
      <output_dir>/global_mask.dat       — numpy memmap uint32
      <output_dir>/global_mask.ome.tiff  — OME-TIFF (for QuPath)
      <output_dir>/global_mask.zarr      — zarr (for downstream)
      <output_dir>/segmentation_meta.json
    """

    tile_done  = pyqtSignal(int, int, int, int, int, int)
    # (row, col, y0_own, y1_own, x0_own, x1_own, n_cells_in_tile) — 7 args
    # Actually emit as separate signal:
    tile_done  = pyqtSignal(int, int, int)   # tile_idx, n_tiles, n_cells_this_tile
    progress   = pyqtSignal(int, int, str)   # done, total, message
    finished   = pyqtSignal(str, int)        # output_dir, total_cells
    error      = pyqtSignal(str)

    def __init__(self, zarr_path, cp_params, n_rows, n_cols,
                 overlap_px, output_dir, recovery_npy_dir=None, rois=None):
        super().__init__()
        self.zarr_path        = zarr_path
        self.cp_params        = cp_params
        self.n_rows           = n_rows
        self.n_cols           = n_cols
        self.overlap_px       = overlap_px
        self.output_dir       = output_dir
        self.recovery_npy_dir = recovery_npy_dir
        self.rois             = rois
        self._stop            = False
        self._logger          = None
        self._mem_timer       = None

    def stop(self):
        self._stop = True

    # ── helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _drop_caches():
        """
        Tell the Linux kernel to drop page cache, dentries and inodes.
        This is the only reliable way to prevent OS page cache from
        silently accumulating until OOM killer fires.

        Requires root. Safe to call at any time — data is on disk already
        (we always call mmap.flush() or del mmap before this).

        Falls back silently if not root or not Linux.
        """
        try:
            # sync: flush all pending writes to disk first
            os.system('sync')
            with open('/proc/sys/vm/drop_caches', 'w') as f:
                f.write('3\n')
        except Exception:
            pass   # non-root or non-Linux — ignore silently

    # ── logging helpers ──────────────────────────────────────────────

    def _setup_logger(self):
        """
        Create a per-run log file:
          <output_dir>/segmentation_<YYYYMMDD_HHMMSS>.log
        Logs to file (DEBUG) and stdout (INFO).
        """
        os.makedirs(self.output_dir, exist_ok=True)
        ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = os.path.join(self.output_dir, f"segmentation_{ts}.log")

        logger = logging.getLogger(f"seg_{ts}")
        logger.setLevel(logging.DEBUG)
        logger.handlers.clear()

        fmt     = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(fmt)
        logger.addHandler(ch)

        logger.info(f"Log file: {log_path}")
        logger.info(f"zarr: {self.zarr_path}")
        logger.info(f"Grid: {self.n_rows}×{self.n_cols}  overlap={self.overlap_px}px")
        logger.info(f"Cellpose params: {self.cp_params}")
        return logger, log_path

    @staticmethod
    def _mem_snapshot():
        """
        Return a formatted string with current RAM and VRAM usage.
        Called both on-demand and by the periodic timer.
        """
        parts = []
        try:
            import psutil
            m   = psutil.virtual_memory()
            used = (m.total - m.available) / 1e9
            tot  = m.total / 1e9
            parts.append(f"RAM {used:.1f}/{tot:.1f}GB ({m.percent:.0f}%)")
        except ImportError:
            parts.append("RAM (psutil not installed)")

        try:
            import torch
            if torch.cuda.is_available():
                for i in range(torch.cuda.device_count()):
                    alloc  = torch.cuda.memory_allocated(i)  / 1e9
                    reserv = torch.cuda.memory_reserved(i)   / 1e9
                    total  = torch.cuda.get_device_properties(i).total_memory / 1e9
                    parts.append(
                        f"GPU{i} alloc={alloc:.1f}GB "
                        f"reserved={reserv:.1f}GB "
                        f"total={total:.1f}GB"
                    )
        except Exception:
            pass

        return "  |  ".join(parts)

    def _start_mem_logger(self, interval_s=10):
        """
        Start a background daemon thread that logs RAM + VRAM every
        `interval_s` seconds until _stop_mem_logger() is called.
        """
        self._mem_log_active = True

        def _loop():
            while self._mem_log_active and not self._stop:
                if self._logger:
                    self._logger.debug(f"[MEM] {self._mem_snapshot()}")
                import time as _t
                _t.sleep(interval_s)

        t = threading.Thread(target=_loop, daemon=True)
        t.start()
        self._mem_timer = t

    def _stop_mem_logger(self):
        self._mem_log_active = False

    def _read_dapi_from_zarr(self, z, y0, y1, x0, x1):
        """
        Read the nucleus (DAPI) channel directly from fused zarr channel index 1.
        fused zarr shape: (H, W, 2)  ch0=cyto  ch1=nucleus(DAPI)  dtype=uint16
        Returns uint16 ndarray (H, W).
        """
        return np.array(z[y0:y1, x0:x1, 1])

    @staticmethod
    def _write_tile_ometiff(path, arr, description=""):
        """Write a 2-D array as a tiled OME-TIFF (single IFD, LZW, 512×512 tiles)."""
        with tifffile.TiffWriter(path, bigtiff=True) as tif:
            tif.write(
                arr,
                tile=(512, 512),
                compression='lzw',
                photometric='minisblack',
                metadata=None,
                description=description,
            )

    def _segment_one_zarr(self, zarr_path, out_prefix,
                          model, use_gpu, log,
                          poly_fullres=None, bbox=None):
        """
        Segment one zarr file (one ROI or full WSI).

        Per-tile outputs (inside <output_dir>/tiles_<out_prefix>/):
          tile_r{r}_c{c}_dapi.ome.tiff      — DAPI uint16 (own region, no overlap)
          tile_r{r}_c{c}_raw_mask.ome.tiff  — raw Cellpose mask float32 (own region, no overlap)

        Global outputs:
          global_mask_<out_prefix>.dat       — memmap uint32
          global_mask_<out_prefix>.zarr      — zarr uint32
          global_mask_<out_prefix>.ome.tiff  — merged mask float32 (QuPath-compatible)
          global_dapi_<out_prefix>.ome.tiff  — full-region DAPI uint16 (tiled)

        Returns total cell count.
        """
        import torch
        z      = zarr.open(zarr_path, mode='r')
        full_h = z.shape[0]
        full_w = z.shape[1]
        log.info(f"  zarr: {full_h}×{full_w} px")

        tile_h = -(-full_h // self.n_rows)
        tile_w = -(-full_w // self.n_cols)

        tiles = []
        for r in range(self.n_rows):
            for c in range(self.n_cols):
                oy0 = r * tile_h
                oy1 = min(oy0 + tile_h, full_h)
                ox0 = c * tile_w
                ox1 = min(ox0 + tile_w, full_w)
                ry0 = max(0, oy0 - self.overlap_px)
                ry1 = min(full_h, oy1 + self.overlap_px)
                rx0 = max(0, ox0 - self.overlap_px)
                rx1 = min(full_w, ox1 + self.overlap_px)
                tiles.append({
                    'row': r, 'col': c,
                    'own':  (oy0, oy1, ox0, ox1),
                    'read': (ry0, ry1, rx0, rx1),
                })
        n_tiles = len(tiles)

        mmap_path = os.path.join(
            self.output_dir, f'global_mask_{out_prefix}.dat'
        )
        mmap = np.memmap(mmap_path, dtype='uint32', mode='w+',
                         shape=(full_h, full_w))
        mmap[:] = 0

        # Per-tile output directory
        tile_dir = os.path.join(self.output_dir, f'tiles_{out_prefix}')
        os.makedirs(tile_dir, exist_ok=True)

        # DAPI global memmap (uint16) for tiling later
        dapi_mmap_path = os.path.join(
            self.output_dir, f'global_dapi_{out_prefix}.dat'
        )
        dapi_mmap = np.memmap(dapi_mmap_path, dtype='uint16', mode='w+',
                              shape=(full_h, full_w))
        dapi_mmap[:] = 0

        global_id_offset = 0
        tile_stats = []

        for i, tile in enumerate(tiles):
            if self._stop:
                del mmap, dapi_mmap
                return 0

            row, col           = tile['row'], tile['col']
            oy0, oy1, ox0, ox1 = tile['own']
            ry0, ry1, rx0, rx1 = tile['read']
            own_h = oy1 - oy0
            own_w = ox1 - ox0

            log.info(
                f"  [{out_prefix}] Tile [{i+1}/{n_tiles}] "
                f"row={row} col={col}"
            )
            self.progress.emit(
                i, n_tiles,
                f"[{out_prefix}] Tile [{i+1}/{n_tiles}]  "
                f"({ry1-ry0}×{rx1-rx0}px)"
            )

            tile_data = np.array(z[ry0:ry1, rx0:rx1, :])

            # ── Read DAPI for own region directly from fused zarr ch1 ──
            # fused zarr ch1 = nucleus (DAPI), already in local ROI coords.
            # No offset needed — zarr coords match tile own coords exactly.
            dapi_own = self._read_dapi_from_zarr(z, oy0, oy1, ox0, ox1)
            dapi_mmap[oy0:oy1, ox0:ox1] = dapi_own[:own_h, :own_w]

            if self.recovery_npy_dir is not None:
                npy_path = os.path.join(
                    self.recovery_npy_dir,
                    f'tile_{out_prefix}_{row}_{col}.npy'
                )
                if not os.path.exists(npy_path):
                    log.warning(f"  Missing: {npy_path}, skipping")
                    del tile_data
                    continue
                local_mask = np.load(npy_path)
            else:
                try:
                    # Normalise uint16 → float32 [0,1] for cpsam.
                    # cpsam (CP4) ignores channels= arg; float32 input gives
                    # correct inference. Shape (H,W,2): ch0=cyto, ch1=nucleus.
                    tile_f32 = tile_data.astype(np.float32) / 65535.0
                    masks, _, _ = model.eval(
                        tile_f32,
                        diameter           = self.cp_params.get('diameter'),
                        flow_threshold     = self.cp_params.get('flow_threshold', 0.4),
                        cellprob_threshold = self.cp_params.get('cellprob_threshold', 0.0),
                        min_size           = self.cp_params.get('min_size', 15),
                        do_3D              = False,
                    )
                    del tile_f32
                    local_mask = masks.astype(np.uint32)
                except Exception as e:
                    log.error(f"  Tile [{row},{col}] failed: {e}")
                    local_mask = np.zeros((ry1-ry0, rx1-rx0), dtype=np.uint32)
                if use_gpu:
                    torch.cuda.empty_cache()

            del tile_data

            # Check stop immediately after inference (fastest exit point)
            if self._stop:
                del local_mask, dapi_own
                mmap.flush()
                dapi_mmap.flush()
                del mmap, dapi_mmap
                log.info(f"  [{out_prefix}] Stopped by user after tile [{i+1}/{n_tiles}].")
                return 0

            # ── Extract own region from padded local_mask ─────────────
            # local_mask covers read region (ry0..ry1, rx0..rx1),
            # own region starts at (oy0-ry0, ox0-rx0) within local_mask.
            local_oy0 = oy0 - ry0
            local_oy1 = oy1 - ry0
            local_ox0 = ox0 - rx0
            local_ox1 = ox1 - rx0
            raw_own_mask = local_mask[local_oy0:local_oy1,
                                      local_ox0:local_ox1].copy()

            # ── Save per-tile DAPI OME-TIFF ───────────────────────────
            dapi_tile_path = os.path.join(
                tile_dir, f'tile_r{row}_c{col}_dapi.ome.tiff'
            )
            try:
                self._write_tile_ometiff(
                    dapi_tile_path,
                    dapi_own[:own_h, :own_w].astype(np.uint16),
                    description=f'DAPI  row={row} col={col}  '
                                f'own=({oy0},{oy1},{ox0},{ox1})',
                )
                log.info(f"    dapi tile → {dapi_tile_path}")
            except Exception as e:
                log.warning(f"    dapi tile write failed: {e}")

            # ── Save per-tile RAW mask OME-TIFF ───────────────────────
            raw_mask_tile_path = os.path.join(
                tile_dir, f'tile_r{row}_c{col}_raw_mask.ome.tiff'
            )
            try:
                self._write_tile_ometiff(
                    raw_mask_tile_path,
                    raw_own_mask.astype(np.float32),
                    description=f'raw Cellpose mask  row={row} col={col}  '
                                f'n_cells={int(raw_own_mask.max())}',
                )
                log.info(f"    raw mask tile → {raw_mask_tile_path}")
            except Exception as e:
                log.warning(f"    raw mask tile write failed: {e}")
            del raw_own_mask

            # ── Centroid ownership filter → global memmap ─────────────
            n_raw = int(local_mask.max())
            if n_raw == 0:
                self.tile_done.emit(i, n_tiles, 0)
                del local_mask
                gc.collect()
                self._drop_caches()
                continue

            cy, cx = self._centroids_vectorised(local_mask)

            keep_labels = []
            for label_idx in range(n_raw):
                lcy, lcx = cy[label_idx], cx[label_idx]
                if (lcy >= local_oy0 and lcy < local_oy1 and
                        lcx >= local_ox0 and lcx < local_ox1):
                    keep_labels.append(label_idx + 1)

            if not keep_labels:
                self.tile_done.emit(i, n_tiles, 0)
                del local_mask, cy, cx
                gc.collect()
                self._drop_caches()
                continue

            lut = np.zeros(n_raw + 1, dtype=np.uint32)
            for new_id, lab in enumerate(keep_labels, start=1):
                lut[lab] = new_id + global_id_offset

            remapped = lut[local_mask]
            del local_mask, lut, cy, cx

            dst = mmap[ry0:ry1, rx0:rx1]
            np.copyto(dst, remapped, where=(remapped > 0))
            del remapped

            n_kept = len(keep_labels)
            global_id_offset += n_kept
            tile_stats.append({'row': row, 'col': col, 'n_cells': n_kept})

            self.tile_done.emit(i, n_tiles, n_kept)
            log.info(f"  ✓ [{out_prefix}] Tile [{i+1}/{n_tiles}] kept={n_kept}")
            gc.collect()
            self._drop_caches()

        # ── Flush memmaps ─────────────────────────────────────────────
        mmap.flush()
        dapi_mmap.flush()
        total_cells = int(global_id_offset)
        log.info(f"  [{out_prefix}] total_cells={total_cells:,}")

        del mmap, dapi_mmap
        gc.collect()
        self._drop_caches()

        # ── Early exit if stopped ─────────────────────────────────────
        if self._stop:
            log.info(f"  [{out_prefix}] Stopped by user — skipping TIFF/zarr output.")
            return 0

        CHUNK = 4096
        mmap_ro      = np.memmap(mmap_path,      dtype='uint32', mode='r',
                                 shape=(full_h, full_w))
        dapi_mmap_ro = np.memmap(dapi_mmap_path, dtype='uint16', mode='r',
                                 shape=(full_h, full_w))

        # ── zarr output (global merged mask) ─────────────────────────
        out_zarr_path = os.path.join(
            self.output_dir, f'global_mask_{out_prefix}.zarr'
        )
        out_z = zarr.open(
            out_zarr_path, mode='w',
            shape=(full_h, full_w), dtype='uint32',
            chunks=(1024, 1024),
        )
        for y in range(0, full_h, CHUNK):
            if self._stop:
                log.info(f"  [{out_prefix}] Stopped during zarr write.")
                del mmap_ro, dapi_mmap_ro
                return 0
            out_z[y:y+CHUNK, :] = mmap_ro[y:y+CHUNK, :]
        self._drop_caches()

        # ── OME-TIFF: global merged mask ──────────────────────────────
        if self._stop:
            del mmap_ro, dapi_mmap_ro
            return 0
        ome_path = os.path.join(
            self.output_dir, f'global_mask_{out_prefix}.ome.tiff'
        )
        with tifffile.TiffWriter(ome_path, bigtiff=True) as tif:
            tif.write(
                mmap_ro.astype(np.float32),
                tile=(512, 512),
                compression='lzw',
                photometric='minisblack',
                metadata=None,
            )
        self._drop_caches()

        # ── OME-TIFF: global DAPI ─────────────────────────────────────
        if self._stop:
            del mmap_ro, dapi_mmap_ro
            return 0
        global_dapi_path = os.path.join(
            self.output_dir, f'global_dapi_{out_prefix}.ome.tiff'
        )
        with tifffile.TiffWriter(global_dapi_path, bigtiff=True) as tif:
            tif.write(
                np.array(dapi_mmap_ro),   # uint16
                tile=(512, 512),
                compression='lzw',
                photometric='minisblack',
                metadata=None,
            )
        self._drop_caches()

        del mmap_ro, dapi_mmap_ro
        gc.collect()

        # Meta JSON
        meta = {
            'roi_name':        out_prefix,
            'zarr_path':       out_zarr_path,
            'ome_tiff':        ome_path,
            'global_dapi':     global_dapi_path,
            'tile_dir':        tile_dir,
            'mmap_path':       mmap_path,
            'total_cells':     total_cells,
            'tile_stats':      tile_stats,
            'cp_params':       self.cp_params,
            'bbox':            list(bbox) if bbox else None,  # [y0,y1,x0,x1]
            'created_at':      datetime.now().isoformat(),
        }
        with open(
            os.path.join(self.output_dir,
                         f'segmentation_meta_{out_prefix}.json'), 'w'
        ) as f:
            json.dump(meta, f, indent=2)

        log.info(f"  [{out_prefix}] outputs written")
        return total_cells

    @staticmethod
    def _centroids_vectorised(mask):
        """
        Return arrays cy, cx for each label 1..max_label.
        Labels not present get cy=cx=-1.
        Uses bincount (fast, no Python loops).
        """
        n = int(mask.max())
        if n == 0:
            return np.array([]), np.array([])
        h, w  = mask.shape
        flat  = mask.ravel()
        ys    = np.repeat(np.arange(h, dtype=np.float32), w)
        xs    = np.tile  (np.arange(w, dtype=np.float32), h)
        cnts  = np.bincount(flat, minlength=n + 2)
        sum_y = np.bincount(flat, weights=ys, minlength=n + 2)
        sum_x = np.bincount(flat, weights=xs, minlength=n + 2)
        valid = cnts[1:n+1] > 0
        cy = np.where(valid, sum_y[1:n+1] / np.maximum(cnts[1:n+1], 1), -1)
        cx = np.where(valid, sum_x[1:n+1] / np.maximum(cnts[1:n+1], 1), -1)
        return cy, cx   # length n, index i → label i+1

    # ── main run ──────────────────────────────────────────────────────


    def run(self):
        try:
            import torch
            from cellpose import models as cp_models

            # ── logger + periodic memory monitor ──────────────────────
            self._logger, log_path = self._setup_logger()
            log = self._logger
            log.info("=== Segmentation started ===")

            # Warn if psutil is missing — RAM monitoring will be blind
            try:
                import psutil
            except ImportError:
                log.warning(
                    "psutil not installed — RAM usage cannot be monitored. "
                    "Run: pip install psutil"
                )
                print("[WARNING] pip install psutil  — RAM monitoring disabled")

            log.info(f"Initial memory: {self._mem_snapshot()}")
            self._start_mem_logger(interval_s=10)

            # ── Cellpose model (shared across ROIs) ───────────────────
            if self.recovery_npy_dir is None:
                use_gpu = torch.cuda.is_available()
                device  = torch.device('cuda' if use_gpu else 'cpu')
                model   = cp_models.CellposeModel(device=device)
            else:
                model   = None
                use_gpu = False

            # ── ROI mode: segment each ROI independently ───────────────
            if self.rois:
                log.info(f"ROI mode: {len(self.rois)} ROI(s)")
                total_cells_all = 0
                for roi_i, roi in enumerate(self.rois):
                    if self._stop:
                        break
                    roi_name = roi["name"]
                    roi_zarr = os.path.join(
                        self.output_dir, f"fused_{roi_name}.zarr"
                    )
                    if not os.path.exists(roi_zarr):
                        log.warning(f"ROI zarr not found: {roi_zarr} — skipping")
                        continue
                    log.info(
                        f"=== ROI [{roi_i+1}/{len(self.rois)}]: {roi_name} ==="
                    )
                    self.progress.emit(
                        roi_i, len(self.rois),
                        f"Segmenting ROI [{roi_i+1}/{len(self.rois)}]: {roi_name}…"
                    )
                    n_cells = self._segment_one_zarr(
                        zarr_path    = roi_zarr,
                        out_prefix   = roi_name,
                        model        = model,
                        use_gpu      = use_gpu,
                        log          = log,
                        poly_fullres = roi.get("polygon_fullres"),
                        bbox         = roi.get("bbox_fullres"),
                    )
                    total_cells_all += n_cells
                    self.progress.emit(
                        roi_i + 1, len(self.rois),
                        f"✓ ROI {roi_name}: {n_cells:,} cells  "
                        f"(cumulative: {total_cells_all:,})"
                    )

                if model is not None:
                    del model
                    gc.collect()
                    if use_gpu and torch.cuda.is_available():
                        torch.cuda.empty_cache()
                    self._drop_caches()

                log.info(
                    f"=== All ROIs done  total_cells={total_cells_all:,} ==="
                )
                self._stop_mem_logger()
                self.finished.emit(self.output_dir, total_cells_all)
                return

            # ── Full WSI mode ─────────────────────────────────────────
            # ── open zarr ────────────────────────────────────────────
            z       = zarr.open(self.zarr_path, mode='r')
            full_h  = z.shape[0]
            full_w  = z.shape[1]
            log.info(f"Input zarr: {full_h}×{full_w} px")

            # ── tile grid ────────────────────────────────────────────
            tile_h = -(-full_h // self.n_rows)
            tile_w = -(-full_w // self.n_cols)

            tiles = []
            for r in range(self.n_rows):
                for c in range(self.n_cols):
                    oy0 = r * tile_h
                    oy1 = min(oy0 + tile_h, full_h)
                    ox0 = c * tile_w
                    ox1 = min(ox0 + tile_w, full_w)
                    ry0 = max(0, oy0 - self.overlap_px)
                    ry1 = min(full_h, oy1 + self.overlap_px)
                    rx0 = max(0, ox0 - self.overlap_px)
                    rx1 = min(full_w, ox1 + self.overlap_px)
                    tiles.append({
                        'row': r, 'col': c,
                        'own':  (oy0, oy1, ox0, ox1),
                        'read': (ry0, ry1, rx0, rx1),
                    })
            n_tiles = len(tiles)

            # ── output memmaps ────────────────────────────────────────
            os.makedirs(self.output_dir, exist_ok=True)

            # Per-tile output directory
            tile_dir = os.path.join(self.output_dir, 'tiles_full')
            os.makedirs(tile_dir, exist_ok=True)

            mmap_path = os.path.join(self.output_dir, 'global_mask.dat')
            mmap = np.memmap(mmap_path, dtype='uint32', mode='w+',
                             shape=(full_h, full_w))
            mmap[:] = 0

            # DAPI global memmap (uint16)
            dapi_mmap_path = os.path.join(self.output_dir, 'global_dapi.dat')
            dapi_mmap = np.memmap(dapi_mmap_path, dtype='uint16', mode='w+',
                                  shape=(full_h, full_w))
            dapi_mmap[:] = 0

            global_id_offset = 0
            tile_stats = []

            for i, tile in enumerate(tiles):
                if self._stop:
                    self.error.emit('Stopped by user.')
                    return

                row, col            = tile['row'], tile['col']
                oy0, oy1, ox0, ox1  = tile['own']
                ry0, ry1, rx0, rx1  = tile['read']
                own_h = oy1 - oy0
                own_w = ox1 - ox0

                _msg = (
                    f"Tile [{i+1}/{n_tiles}]  row={row} col={col}  "
                    f"read=({ry1-ry0}×{rx1-rx0}px)  own=({own_h}×{own_w}px)"
                )
                self.progress.emit(i, n_tiles, _msg)
                log.info(_msg)
                log.debug(f"  [MEM before inference] {self._mem_snapshot()}")

                # ── Read DAPI for own region directly from fused zarr ch1 ──
                dapi_own = self._read_dapi_from_zarr(z, oy0, oy1, ox0, ox1)
                dapi_mmap[oy0:oy1, ox0:ox1] = dapi_own[:own_h, :own_w]

                # ── Read fused tile from zarr ─────────────────────────
                if self.recovery_npy_dir is not None:
                    npy_path = os.path.join(
                        self.recovery_npy_dir, f'tile_{row}_{col}.npy'
                    )
                    if not os.path.exists(npy_path):
                        self.progress.emit(
                            i, n_tiles,
                            f'  ⚠ {npy_path} not found, skipping'
                        )
                        continue
                    local_mask = np.load(npy_path)
                else:
                    tile_data = np.array(z[ry0:ry1, rx0:rx1, :])
                    try:
                        tile_f32 = tile_data.astype(np.float32) / 65535.0
                        masks, _, _ = model.eval(
                            tile_f32,
                            diameter           = self.cp_params.get('diameter'),
                            flow_threshold     = self.cp_params.get('flow_threshold', 0.4),
                            cellprob_threshold = self.cp_params.get('cellprob_threshold', 0.0),
                            min_size           = self.cp_params.get('min_size', 15),
                            do_3D              = False,
                        )
                        local_mask = masks.astype(np.uint32)
                        del tile_f32
                    except Exception as e:
                        self.error.emit(f'Tile [{row},{col}] inference failed: {e}')
                        local_mask = np.zeros((ry1-ry0, rx1-rx0), dtype=np.uint32)
                    del tile_data
                    if use_gpu:
                        torch.cuda.empty_cache()

                # ── Extract own region raw mask (no centroid filter) ──
                local_oy0 = oy0 - ry0
                local_oy1 = oy1 - ry0
                local_ox0 = ox0 - rx0
                local_ox1 = ox1 - rx0
                raw_own_mask = local_mask[local_oy0:local_oy1,
                                          local_ox0:local_ox1].copy()

                # ── Save per-tile DAPI OME-TIFF ───────────────────────
                dapi_tile_path = os.path.join(
                    tile_dir, f'tile_r{row}_c{col}_dapi.ome.tiff'
                )
                try:
                    self._write_tile_ometiff(
                        dapi_tile_path,
                        dapi_own[:own_h, :own_w].astype(np.uint16),
                        description=f'DAPI row={row} col={col} '
                                    f'own=({oy0},{oy1},{ox0},{ox1})',
                    )
                except Exception as e:
                    log.warning(f"  dapi tile write failed: {e}")

                # ── Save per-tile RAW mask OME-TIFF ───────────────────
                raw_mask_tile_path = os.path.join(
                    tile_dir, f'tile_r{row}_c{col}_raw_mask.ome.tiff'
                )
                try:
                    self._write_tile_ometiff(
                        raw_mask_tile_path,
                        raw_own_mask.astype(np.float32),
                        description=f'raw mask row={row} col={col} '
                                    f'n_cells={int(raw_own_mask.max())}',
                    )
                except Exception as e:
                    log.warning(f"  raw mask tile write failed: {e}")
                del raw_own_mask, dapi_own

                # ── Centroid-based ownership filter ───────────────────
                n_raw = int(local_mask.max())
                if n_raw == 0:
                    self.tile_done.emit(i, n_tiles, 0)
                    del local_mask
                    gc.collect()
                    self._drop_caches()
                    continue

                cy, cx = self._centroids_vectorised(local_mask)

                keep_labels = []
                for label_idx in range(n_raw):
                    lcy = cy[label_idx]
                    lcx = cx[label_idx]
                    if (lcy >= local_oy0 and lcy < local_oy1 and
                            lcx >= local_ox0 and lcx < local_ox1):
                        keep_labels.append(label_idx + 1)

                if not keep_labels:
                    self.tile_done.emit(i, n_tiles, 0)
                    del local_mask, cy, cx
                    gc.collect()
                    self._drop_caches()
                    continue

                # ── Remap IDs + write to global memmap ────────────────
                lut = np.zeros(n_raw + 1, dtype=np.uint32)
                for new_id, lab in enumerate(keep_labels, start=1):
                    lut[lab] = new_id + global_id_offset

                remapped = lut[local_mask]
                del local_mask, lut, cy, cx

                # Write full read region (incl. overlap) so cells that
                # extend across tile borders are not truncated.
                dst = mmap[ry0:ry1, rx0:rx1]
                np.copyto(dst, remapped, where=(remapped > 0))
                del remapped

                n_kept = len(keep_labels)
                global_id_offset += n_kept
                tile_stats.append({'row': row, 'col': col, 'n_cells': n_kept})

                self.tile_done.emit(i, n_tiles, n_kept)
                _done_msg = (
                    f"✓ Tile [{i+1}/{n_tiles}]  kept={n_kept} cells  "
                    f"total so far={global_id_offset}"
                )
                self.progress.emit(i + 1, n_tiles, _done_msg)
                log.info(_done_msg)
                log.debug(f"  [MEM after write] {self._mem_snapshot()}")
                gc.collect()
                if self.recovery_npy_dir is None and torch.cuda.is_available():
                    torch.cuda.empty_cache()
                self._drop_caches()
                log.debug("  [MEM after drop_caches] " + self._mem_snapshot())

            if self.recovery_npy_dir is None:
                del model
                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                self._drop_caches()
                log.info(f"All inference done. {self._mem_snapshot()}")

            # ── Flush memmaps ─────────────────────────────────────────
            mmap.flush()
            dapi_mmap.flush()
            total_cells = int(global_id_offset)
            _out_msg = f"Inference done. Total cells: {total_cells:,}. Writing outputs…"
            self.progress.emit(n_tiles, n_tiles, _out_msg)
            log.info(_out_msg)
            self._stop_mem_logger()

            del mmap, dapi_mmap
            gc.collect()
            self._drop_caches()

            mmap_ro      = np.memmap(mmap_path,      dtype='uint32', mode='r',
                                     shape=(full_h, full_w))
            dapi_mmap_ro = np.memmap(dapi_mmap_path, dtype='uint16', mode='r',
                                     shape=(full_h, full_w))

            CHUNK_ROWS = 4096
            n_chunks   = -(-full_h // CHUNK_ROWS)

            # ── Output 1: global mask zarr ────────────────────────────
            out_zarr_path = os.path.join(self.output_dir, 'global_mask.zarr')
            out_z = zarr.open(
                out_zarr_path, mode='w',
                shape=(full_h, full_w),
                dtype='uint32',
                chunks=(1024, 1024),
            )
            for ci, y in enumerate(range(0, full_h, CHUNK_ROWS)):
                y1 = min(y + CHUNK_ROWS, full_h)
                out_z[y:y1, :] = mmap_ro[y:y1, :]
                self.progress.emit(n_tiles, n_tiles,
                                   f'Writing mask zarr… chunk {ci+1}/{n_chunks}')
            self.progress.emit(n_tiles, n_tiles, '✓ mask zarr written')
            log.info(f"zarr → {out_zarr_path}  {self._mem_snapshot()}")
            self._drop_caches()

            # ── Output 2: global mask OME-TIFF (float32, tiled) ───────
            ome_path = os.path.join(self.output_dir, 'global_mask.ome.tiff')
            self.progress.emit(n_tiles, n_tiles,
                               'Writing global mask OME-TIFF…')
            with tifffile.TiffWriter(ome_path, bigtiff=True) as tif:
                tif.write(
                    mmap_ro.astype(np.float32),
                    tile=(512, 512),
                    compression='lzw',
                    photometric='minisblack',
                    metadata=None,
                )
            self.progress.emit(n_tiles, n_tiles, '✓ global mask OME-TIFF written')
            log.info(f"mask OME-TIFF → {ome_path}  {self._mem_snapshot()}")
            self._drop_caches()

            # ── Output 3: global DAPI OME-TIFF (uint16, tiled) ────────
            global_dapi_path = os.path.join(self.output_dir, 'global_dapi.ome.tiff')
            self.progress.emit(n_tiles, n_tiles,
                               'Writing global DAPI OME-TIFF…')
            with tifffile.TiffWriter(global_dapi_path, bigtiff=True) as tif:
                tif.write(
                    np.array(dapi_mmap_ro),   # uint16 — no cast needed
                    tile=(512, 512),
                    compression='lzw',
                    photometric='minisblack',
                    metadata=None,
                )
            self.progress.emit(n_tiles, n_tiles, '✓ global DAPI OME-TIFF written')
            log.info(f"DAPI OME-TIFF → {global_dapi_path}  {self._mem_snapshot()}")

            del mmap_ro, dapi_mmap_ro
            gc.collect()
            self._drop_caches()
            log.debug(f"[MEM final drop_caches] {self._mem_snapshot()}")

            # ── Meta JSON ─────────────────────────────────────────────
            meta = {
                'zarr_path':      out_zarr_path,
                'ome_tiff':       ome_path,
                'global_dapi':    global_dapi_path,
                'tile_dir':       tile_dir,
                'mmap_path':      mmap_path,
                'total_cells':    total_cells,
                'image_shape':    [full_h, full_w],
                'tile_grid':      [self.n_rows, self.n_cols],
                'overlap_px':     self.overlap_px,
                'tile_stats':     tile_stats,
                'cp_params':      self.cp_params,
                'created_at':     datetime.now().isoformat(),
            }
            with open(os.path.join(self.output_dir,
                                   'segmentation_meta.json'), 'w') as f:
                json.dump(meta, f, indent=2)

            log.info(
                f"=== Segmentation complete ===  "
                f"total_cells={total_cells:,}  "
                f"output={self.output_dir}"
            )
            log.info(f"Final memory: {self._mem_snapshot()}")
            self.finished.emit(self.output_dir, total_cells)

        except Exception:
            tb = traceback.format_exc()
            if self._logger:
                self._logger.critical("FATAL ERROR:\n" + tb)
                try:
                    self._logger.critical(
                        f"Memory at crash: {self._mem_snapshot()}"
                    )
                except Exception:
                    pass
            self._stop_mem_logger()
            self.error.emit(tb)


# ══════════════════════════════════════════════════════════════════════
#  Step 2 Page  (Segmentation & Merge)
# ══════════════════════════════════════════════════════════════════════

class Step2Page(QWidget):
    """Full Step 2 UI: zarr input, tile grid, Cellpose params, progress."""

    go_back           = pyqtSignal()
    segmentation_done = pyqtSignal(str)   # emits output_dir when done

    # Tile status colours
    _COL_IDLE    = (80,  80,  80)
    _COL_RUNNING = (255, 200,  50)
    _COL_DONE    = ( 60, 200,  80)
    _COL_ERROR   = (220,  60,  60)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._zarr_path      = None
        self._cp_params      = {}
        self._worker         = None
        self._tile_rects     = {}
        self._tile_status    = {}
        self._n_rows         = 3
        self._n_cols         = 4
        self._full_h         = 0
        self._full_w         = 0
        self._total_cells    = 0
        self._ov_thread      = None
        self._rois           = []
        self._last_output_dir = None   # set when segmentation finishes

        self._build_ui()

    # ── UI construction ───────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # Title
        title = QLabel('Step 2 — Cellpose Segmentation & Merge')
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            'font-size:16px;font-weight:bold;color:#eee;'
            'background:#1a1a1a;padding:6px;border-radius:4px;'
        )
        root.addWidget(title)

        # Main split: left overview | right controls
        split = QSplitter(Qt.Horizontal)

        # ── LEFT: overview + progress ─────────────────────────────────
        left = QWidget()
        ll   = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(4)

        ll.addWidget(self._lbl('Tile Status Overview', bold=True))

        self._ov_gv  = pg.GraphicsLayoutWidget()
        self._ov_gv.setBackground('#111')
        self._ov_vb  = self._ov_gv.addViewBox()
        self._ov_vb.setAspectLocked(True)
        self._ov_vb.invertY(True)
        self._ov_vb.setMenuEnabled(False)
        self._ov_img = pg.ImageItem()
        self._ov_vb.addItem(self._ov_img)
        ll.addWidget(self._ov_gv, stretch=1)

        self._ov_status = QLabel('No zarr loaded')
        self._ov_status.setStyleSheet('color:#888;font-size:10px;')
        self._ov_status.setAlignment(Qt.AlignCenter)
        self._ov_status.setWordWrap(True)
        ll.addWidget(self._ov_status)

        # Progress text
        ll.addWidget(self._lbl('Progress', bold=True))
        self._prog_lbl = QLabel('—')
        self._prog_lbl.setStyleSheet(
            'color:#ccc;font-size:11px;padding:4px;'
            'background:#111;border-radius:3px;'
        )
        self._prog_lbl.setWordWrap(True)
        ll.addWidget(self._prog_lbl)

        self._prog_bar = QProgressBar()
        self._prog_bar.setRange(0, 100)
        self._prog_bar.setStyleSheet(
            'QProgressBar{border:1px solid #444;border-radius:3px;'
            'text-align:center;color:#fff;height:16px;}'
            'QProgressBar::chunk{background:#2a5;border-radius:3px;}'
        )
        ll.addWidget(self._prog_bar)

        self._cells_lbl = QLabel('Total cells detected: —')
        self._cells_lbl.setStyleSheet('color:#aaa;font-size:11px;')
        ll.addWidget(self._cells_lbl)

        split.addWidget(left)

        # ── RIGHT: all controls ───────────────────────────────────────
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setStyleSheet('QScrollArea{border:none;}')
        right_w = QWidget()
        rl = QVBoxLayout(right_w)
        rl.setSpacing(8)

        # Data input
        inp_box = QGroupBox('Input Data')
        inp_box.setStyleSheet(self._box_style('#61afef'))
        inl = QVBoxLayout(inp_box)

        zr = QHBoxLayout()
        zr.addWidget(QLabel('fused.zarr:'))
        self._zarr_edit = QtWidgets.QLineEdit()
        self._zarr_edit.setPlaceholderText('Path to fused.zarr …')
        self._zarr_edit.setStyleSheet('font-size:11px;')
        zr.addWidget(self._zarr_edit, stretch=1)
        btn_browse = QPushButton('Browse')
        btn_browse.setFixedWidth(64)
        btn_browse.clicked.connect(self._browse_zarr)
        zr.addWidget(btn_browse)
        inl.addLayout(zr)

        btn_load = QPushButton('Load zarr info & overview')
        btn_load.setStyleSheet(
            'QPushButton{background:#255;color:white;border-radius:3px;padding:4px;}'
            'QPushButton:hover{background:#377;}'
        )
        btn_load.clicked.connect(self._load_zarr_info)
        inl.addLayout(zr)
        inl.addWidget(btn_load)

        self._zarr_info = QLabel('—')
        self._zarr_info.setStyleSheet('color:#aaa;font-size:10px;')
        self._zarr_info.setWordWrap(True)
        inl.addWidget(self._zarr_info)
        rl.addWidget(inp_box)

        # Tile grid
        tile_box = QGroupBox('Tile Grid (for segmentation)')
        tile_box.setStyleSheet(self._box_style('#e5c07b'))
        til = QVBoxLayout(tile_box)

        grid_row = QHBoxLayout()
        grid_row.addWidget(QLabel('Rows:'))
        self._rows_spin = QtWidgets.QSpinBox()
        self._rows_spin.setRange(1, 20)
        self._rows_spin.setValue(3)
        grid_row.addWidget(self._rows_spin)
        grid_row.addWidget(QLabel('Cols:'))
        self._cols_spin = QtWidgets.QSpinBox()
        self._cols_spin.setRange(1, 20)
        self._cols_spin.setValue(4)
        grid_row.addWidget(self._cols_spin)
        grid_row.addStretch()
        til.addLayout(grid_row)

        ovlp_row = QHBoxLayout()
        ovlp_row.addWidget(QLabel('Overlap (px):'))
        self._overlap_spin = QtWidgets.QSpinBox()
        self._overlap_spin.setRange(50, 1000)
        self._overlap_spin.setValue(200)
        self._overlap_spin.setSingleStep(50)
        ovlp_row.addWidget(self._overlap_spin)
        ovlp_row.addStretch()
        til.addLayout(ovlp_row)

        self._tile_ram_lbl = QLabel('')
        self._tile_ram_lbl.setStyleSheet('color:#aaa;font-size:10px;')
        til.addWidget(self._tile_ram_lbl)

        self._rows_spin.valueChanged.connect(self._update_tile_info)
        self._cols_spin.valueChanged.connect(self._update_tile_info)
        rl.addWidget(tile_box)

        # Cellpose params
        cp_box = QGroupBox('Cellpose Parameters')
        cp_box.setStyleSheet(self._box_style('#c678dd'))
        cpl = QVBoxLayout(cp_box)

        def _param_row(label, widget):
            r = QHBoxLayout()
            l = QLabel(label)
            l.setFixedWidth(160)
            r.addWidget(l)
            r.addWidget(widget)
            return r

        # Cellpose 4.0.1+: model_type is ignored — only cpsam is used
        self._cp_model_lbl = QLabel('cpsam  (Cellpose 4.0.1+: only model, model_type ignored)')
        self._cp_model_lbl.setStyleSheet(
            'color:#fa8;font-size:11px;padding:2px 4px;'
            'background:#221;border-radius:3px;'
        )
        cpl.addLayout(_param_row('Model:', self._cp_model_lbl))

        self._cp_diam = QDoubleSpinBox()
        self._cp_diam.setRange(0, 300)
        self._cp_diam.setValue(30)
        self._cp_diam.setSpecialValueText('auto')
        cpl.addLayout(_param_row('diameter (0=auto):', self._cp_diam))

        self._cp_flow = QDoubleSpinBox()
        self._cp_flow.setRange(0.0, 3.0)
        self._cp_flow.setSingleStep(0.05)
        self._cp_flow.setValue(0.4)
        cpl.addLayout(_param_row('flow_threshold:', self._cp_flow))

        self._cp_prob = QDoubleSpinBox()
        self._cp_prob.setRange(-6.0, 6.0)
        self._cp_prob.setSingleStep(0.1)
        self._cp_prob.setValue(0.0)
        cpl.addLayout(_param_row('cellprob_threshold:', self._cp_prob))

        self._cp_minsize = QtWidgets.QSpinBox()
        self._cp_minsize.setRange(1, 10000)
        self._cp_minsize.setValue(15)
        cpl.addLayout(_param_row('min_size (px²):', self._cp_minsize))

        btn_load_cp = QPushButton('↓ Load from cellpose_params.json')
        btn_load_cp.setStyleSheet(
            'QPushButton{color:#8cf;font-size:10px;border:1px solid #8cf;'
            'border-radius:3px;padding:3px;}'
        )
        btn_load_cp.clicked.connect(self._load_cp_params)
        cpl.addWidget(btn_load_cp)
        rl.addWidget(cp_box)

        # Output settings
        out_box = QGroupBox('Output')
        out_box.setStyleSheet(self._box_style('#98c379'))
        outl = QVBoxLayout(out_box)

        outr = QHBoxLayout()
        outr.addWidget(QLabel('Output dir:'))
        self._out_edit = QtWidgets.QLineEdit(OUTPUT_DIR)
        self._out_edit.setStyleSheet('font-size:11px;')
        outr.addWidget(self._out_edit, stretch=1)
        btn_out = QPushButton('Browse')
        btn_out.setFixedWidth(64)
        btn_out.clicked.connect(self._browse_out)
        outr.addWidget(btn_out)
        outl.addLayout(outr)

        out_info = QLabel(
            'Global: global_mask.zarr | global_mask.ome.tiff | global_dapi.ome.tiff\n'
            'Per-tile: tiles_*/tile_r*_c*_dapi.ome.tiff | tile_r*_c*_raw_mask.ome.tiff'
        )
        out_info.setStyleSheet('color:#888;font-size:10px;')
        outl.addWidget(out_info)
        rl.addWidget(out_box)

        # Recovery mode (collapsed by default)
        rec_box = QGroupBox('Recovery Mode (merge from saved .npy files)')
        rec_box.setStyleSheet(self._box_style('#e06c75'))
        rec_box.setCheckable(True)
        rec_box.setChecked(False)
        recl = QVBoxLayout(rec_box)

        rec_info = QLabel(
            'Use this if a previous run saved tile masks to disk.\n'
            'Select the directory containing tile_r_c.npy files.'
        )
        rec_info.setStyleSheet('color:#aaa;font-size:10px;')
        recl.addWidget(rec_info)

        rr = QHBoxLayout()
        self._rec_edit = QtWidgets.QLineEdit()
        self._rec_edit.setPlaceholderText('Directory with .npy tile masks…')
        rr.addWidget(self._rec_edit, stretch=1)
        btn_rec = QPushButton('Browse')
        btn_rec.setFixedWidth(64)
        btn_rec.clicked.connect(
            lambda: self._rec_edit.setText(
                QFileDialog.getExistingDirectory(self, 'Select .npy directory')
            )
        )
        rr.addWidget(btn_rec)
        recl.addLayout(rr)
        self._rec_box = rec_box
        rl.addWidget(rec_box)

        rl.addStretch()
        right_scroll.setWidget(right_w)
        split.addWidget(right_scroll)

        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 1)
        root.addWidget(split, stretch=1)

        # ── Bottom navigation ─────────────────────────────────────────
        nav = QHBoxLayout()

        self._btn_back = QPushButton('← Back to Step 1')
        self._btn_back.setStyleSheet(
            'QPushButton{color:#fa8;border:1px solid #fa8;'
            'border-radius:4px;padding:6px 16px;}'
            'QPushButton:hover{background:#321;}'
        )
        self._btn_back.clicked.connect(self.go_back.emit)
        nav.addWidget(self._btn_back)
        nav.addStretch()

        self._btn_to_step3 = QPushButton('→ QC Viewer (Step 3)')
        self._btn_to_step3.setEnabled(False)
        self._btn_to_step3.setStyleSheet(
            'QPushButton{background:#246;color:white;border-radius:4px;'
            'padding:7px 18px;font-size:12px;font-weight:bold;}'
            'QPushButton:hover{background:#358;}'
            'QPushButton:disabled{background:#333;color:#555;}'
        )
        self._btn_to_step3.clicked.connect(
            lambda: self.segmentation_done.emit(self._last_output_dir or OUTPUT_DIR)
        )
        nav.addWidget(self._btn_to_step3)

        self._btn_run = QPushButton('▶  Run Segmentation & Merge')
        self._btn_run.setStyleSheet(
            'QPushButton{background:#2a5;color:white;border-radius:4px;'
            'padding:7px 22px;font-size:13px;font-weight:bold;}'
            'QPushButton:hover{background:#3b6;}'
            'QPushButton:disabled{background:#333;color:#555;}'
        )
        self._btn_run.clicked.connect(self._run)
        nav.addWidget(self._btn_run)

        self._btn_stop = QPushButton('⏹ Stop')
        self._btn_stop.setEnabled(False)
        self._btn_stop.setStyleSheet(
            'QPushButton{background:#722;color:white;border-radius:4px;padding:7px 14px;}'
            'QPushButton:hover{background:#944;}'
            'QPushButton:disabled{background:#333;color:#555;}'
        )
        self._btn_stop.clicked.connect(self._stop)
        nav.addWidget(self._btn_stop)

        root.addLayout(nav)

    # ── utilities ─────────────────────────────────────────────────────

    @staticmethod
    def _lbl(text, bold=False):
        l = QLabel(text)
        l.setAlignment(Qt.AlignCenter)
        s = 'font-size:12px;color:#ddd;background:#1a1a1a;padding:3px;'
        if bold:
            s += 'font-weight:bold;'
        l.setStyleSheet(s)
        return l

    @staticmethod
    def _box_style(color):
        return (
            f'QGroupBox{{border:1px solid {color};border-radius:5px;'
            f'margin-top:4px;font-weight:bold;color:{color};font-size:11px;}}'
        )

    # ── zarr loading ──────────────────────────────────────────────────

    def set_zarr_path(self, path):
        """Called from MainWindow when Step 1 finishes generating fused.zarr."""
        if path and os.path.exists(path):
            self._zarr_edit.setText(path)
            self._load_zarr_info()

    def set_rois(self, rois):
        """Receive ROI list from Step 1 for per-ROI segmentation."""
        self._rois = rois if rois else []
        if self._rois:
            names = [r["name"] for r in self._rois]
            self._zarr_info.setText(
                (self._zarr_info.text() or "") +
                f"\nROIs: {names}  (will segment each independently)"
            )

    def _browse_zarr(self):
        path = QFileDialog.getExistingDirectory(self, 'Select fused.zarr directory')
        if path:
            self._zarr_edit.setText(path)

    def _browse_out(self):
        path = QFileDialog.getExistingDirectory(self, 'Select output directory')
        if path:
            self._out_edit.setText(path)

    def _load_zarr_info(self):
        path = self._zarr_edit.text().strip()
        if not path or not os.path.exists(path):
            self._zarr_info.setText('⚠ Path not found')
            return
        try:
            z = zarr.open(path, mode='r')
            self._full_h, self._full_w = z.shape[0], z.shape[1]
            attrs = dict(z.attrs)
            self._zarr_path = path
            self._zarr_info.setText(
                f'Shape: {self._full_h:,} × {self._full_w:,} px  '
                f'| dtype: {z.dtype}  '
                f'| ch0={attrs.get("channel_0","?")}  '
                f'ch1={attrs.get("channel_1","?")}'
            )
            self._update_tile_info()
            self._load_overview_from_zarr(z)
        except Exception as e:
            self._zarr_info.setText(f'⚠ Failed to open zarr: {e}')

    def _load_overview_from_zarr(self, z):
        """Load nucleus channel (index 1) downsampled as overview."""
        self._ov_status.setText('Loading overview…')
        try:
            ds  = 32
            # Read as uint16 then normalise — avoids float setImage issue
            arr_raw = z[::ds, ::ds, 1]
            arr     = arr_raw.astype(np.float32)
            nz      = arr[arr > 0]
            if nz.size > 100:
                lo, hi = np.percentile(nz, [1, 99.5])
                if hi > lo:
                    arr = np.clip((arr - lo) / (hi - lo), 0.0, 1.0)
                else:
                    arr = np.zeros_like(arr)
            else:
                arr = np.zeros_like(arr)

            self._ov_h  = arr.shape[0]
            self._ov_w  = arr.shape[1]
            self._ov_ds = ds

            # pyqtgraph requires levels=[min,max] when dtype is float
            self._ov_img.setImage(arr, autoLevels=False, levels=[0.0, 1.0])
            self._ov_vb.setRange(
                QRectF(0, 0, self._ov_w, self._ov_h), padding=0.01
            )
            self._ov_status.setText(
                f'Overview {self._ov_h}×{self._ov_w} px (1/{ds})'
            )
            self._draw_tile_grid()
        except Exception as e:
            import traceback as _tb
            self._ov_status.setText(f'Overview failed: {e}')
            print(f'[Step2 overview error]\n{_tb.format_exc()}')

    # ── tile grid overlay ─────────────────────────────────────────────

    def _update_tile_info(self):
        if self._full_h == 0:
            return
        nr  = self._rows_spin.value()
        nc  = self._cols_spin.value()
        th  = -(-self._full_h // nr)
        tw  = -(-self._full_w // nc)
        # RAM estimate: tile_h × tile_w × 2 channels × 4 bytes float32 × 2 (accum)
        ram = th * tw * 2 * 4 * 2 / 1e9
        self._tile_ram_lbl.setText(
            f'{nr}×{nc} = {nr*nc} tiles  |  '
            f'tile size: {th:,}×{tw:,} px  |  '
            f'est. VRAM/tile: {ram:.1f} GB'
        )
        self._draw_tile_grid()

    def _draw_tile_grid(self):
        """Overlay tile rectangles on the overview."""
        if not hasattr(self, '_ov_h') or self._full_h == 0:
            return
        nr = self._rows_spin.value()
        nc = self._cols_spin.value()

        # Remove old rects
        for item in list(self._tile_rects.values()):
            self._ov_vb.removeItem(item)
        self._tile_rects.clear()
        self._tile_status.clear()

        th  = -(-self._full_h // nr)
        tw  = -(-self._full_w // nc)
        ds  = self._ov_ds

        for r in range(nr):
            for c in range(nc):
                oy0 = r * th
                oy1 = min(oy0 + th, self._full_h)
                ox0 = c * tw
                ox1 = min(ox0 + tw, self._full_w)
                # convert to overview coords
                rect = pg.RectROI(
                    [ox0 / ds, oy0 / ds],
                    [(ox1 - ox0) / ds, (oy1 - oy0) / ds],
                    pen=pg.mkPen('#808080', width=1),
                    movable=False, resizable=False,
                )
                self._ov_vb.addItem(rect)
                self._tile_rects[(r, c)] = rect
                self._tile_status[(r, c)] = 'idle'

    def _set_tile_colour(self, row, col, state):
        rect = self._tile_rects.get((row, col))
        if rect is None:
            return
        colours = {
            'idle':    '#808080',
            'running': '#ffc832',
            'done':    '#3cc850',
            'error':   '#dc3c3c',
        }
        rect.setPen(pg.mkPen(colours.get(state, '#808080'), width=2))

    # ── Cellpose params loading ───────────────────────────────────────

    def _load_cp_params(self):
        cp_path = os.path.join(OUTPUT_DIR, 'cellpose_params.json')
        if not os.path.exists(cp_path):
            cp_path, _ = QFileDialog.getOpenFileName(
                self, 'Select cellpose_params.json', OUTPUT_DIR, 'JSON (*.json)'
            )
        if not cp_path or not os.path.exists(cp_path):
            return
        try:
            with open(cp_path) as f:
                p = json.load(f)
            # model_type ignored in Cellpose 4.0.1+; skip UI update
            self._cp_diam.setValue(p.get('diameter') or 0)
            self._cp_flow.setValue(p.get('flow_threshold', 0.4))
            self._cp_prob.setValue(p.get('cellprob_threshold', 0.0))
            self._cp_minsize.setValue(p.get('min_size', 15))
            self._cp_params = p
            QMessageBox.information(self, 'Loaded',
                                    f'Parameters loaded from:\n{cp_path}')
        except Exception as e:
            QMessageBox.warning(self, 'Error', str(e))

    def get_cp_params(self):
        diam = self._cp_diam.value()
        return {
            'model_type':         'cpsam',  # Cellpose 4.0.1+: always cpsam
            'diameter':           None if diam == 0 else diam,
            'flow_threshold':     self._cp_flow.value(),
            'cellprob_threshold': self._cp_prob.value(),
            'min_size':           self._cp_minsize.value(),
        }

    # ── run / stop ────────────────────────────────────────────────────

    def _run(self):
        if not self._zarr_path or not os.path.exists(self._zarr_path):
            QMessageBox.warning(self, 'No data',
                                'Please load a fused.zarr first.')
            return

        recovery_dir = None
        if self._rec_box.isChecked():
            recovery_dir = self._rec_edit.text().strip() or None

        nr  = self._rows_spin.value()
        nc  = self._cols_spin.value()
        self._n_rows = nr
        self._n_cols = nc

        # Reset tile colours
        for key in self._tile_status:
            self._tile_status[key] = 'idle'
            self._set_tile_colour(key[0], key[1], 'idle')
        self._total_cells = 0
        self._cells_lbl.setText('Total cells detected: 0')

        self._worker = SegmentMergeWorker(
            zarr_path        = self._zarr_path,
            cp_params        = self.get_cp_params(),
            n_rows           = nr,
            n_cols           = nc,
            overlap_px       = self._overlap_spin.value(),
            output_dir       = self._out_edit.text().strip() or OUTPUT_DIR,
            recovery_npy_dir = recovery_dir,
            rois             = self._rois if self._rois else None,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.tile_done.connect(self._on_tile_done)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)

        self._btn_run.setEnabled(False)
        self._btn_back.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._prog_bar.setValue(0)
        self._worker.start()

        # Mark first tile as running
        if self._tile_rects:
            self._set_tile_colour(0, 0, 'running')
            self._tile_status[(0, 0)] = 'running'

    def _stop(self):
        if self._worker:
            self._worker.stop()

    def _on_progress(self, done, total, msg):
        pct = int(done / total * 100) if total > 0 else 0
        self._prog_bar.setValue(pct)
        self._prog_lbl.setText(msg)

        # Mark next tile as running
        nr = self._n_rows
        nc = self._n_cols
        if done < total:
            r = done // nc
            c = done  % nc
            self._set_tile_colour(r, c, 'running')

    def _on_tile_done(self, tile_idx, n_tiles, n_cells):
        nr = self._n_rows
        nc = self._n_cols
        r  = tile_idx // nc
        c  = tile_idx  % nc
        self._set_tile_colour(r, c, 'done')
        self._tile_status[(r, c)] = 'done'
        self._total_cells += n_cells
        self._cells_lbl.setText(
            f'Total cells detected: {self._total_cells:,}'
        )

    def _on_finished(self, output_dir, total_cells):
        self._prog_bar.setValue(100)
        self._prog_lbl.setText(
            f'✓ Done!  {total_cells:,} cells  →  {output_dir}'
        )
        self._btn_run.setEnabled(True)
        self._btn_back.setEnabled(True)
        self._btn_stop.setEnabled(False)

        # Enable the "Go to QC Viewer" button
        self._btn_to_step3.setEnabled(True)
        self._last_output_dir = output_dir

        msg = QMessageBox(self)
        msg.setWindowTitle('Segmentation Complete')
        msg.setText(
            f'Total cells: {total_cells:,}\n\n'
            f'Output directory:\n  {output_dir}\n\n'
            f'Global outputs:\n'
            f'  global_mask.zarr\n'
            f'  global_mask.ome.tiff   (merged mask, float32)\n'
            f'  global_dapi.ome.tiff   (DAPI, uint16)\n'
            f'  segmentation_meta.json\n\n'
            f'Per-tile outputs:\n'
            f'  tiles_full/  (or tiles_<ROI>/)\n'
            f'    tile_r*_c*_dapi.ome.tiff\n'
            f'    tile_r*_c*_raw_mask.ome.tiff'
        )
        btn_qc = msg.addButton('→ Open QC Viewer (Step 3)',
                               QMessageBox.AcceptRole)
        btn_feat = msg.addButton('→ Feature Extraction (Step 4)',
                                 QMessageBox.AcceptRole)
        msg.addButton('OK', QMessageBox.RejectRole)
        msg.exec_()
        if msg.clickedButton() is btn_qc:
            self.segmentation_done.emit(output_dir)
        elif msg.clickedButton() is btn_feat:
            self.segmentation_done.emit(output_dir)   # step3 auto-loads too

    def _on_error(self, msg):
        self._prog_lbl.setText('✗ Error — see terminal')
        self._btn_run.setEnabled(True)
        self._btn_back.setEnabled(True)
        self._btn_stop.setEnabled(False)
        QMessageBox.critical(self, 'Error', msg)
        print(f'[Step2 Error]\n{msg}')


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

    def run(self):
        try:
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
            arr  = arr.astype(np.float32)
            nz   = arr[arr > 0]
            if nz.size > 100:
                lo, hi = np.percentile(nz, [1.0, 99.5])
                if hi > lo:
                    arr = np.clip((arr - lo) / (hi - lo), 0.0, 1.0)
                else:
                    arr = np.zeros_like(arr)
            self.done.emit(arr)
        except Exception as e:
            self.error.emit(str(e))


class _RegionLoader(QThread):
    """
    Load a rectangular ROI from global_dapi + global_mask, compute the
    cellpose-style overlay in the background thread.

    Signals:
        done(rgb_overlay, dapi_grey_rgb, n_cells, h, w)
    """
    done  = pyqtSignal(object, object, int, int, int)
    error = pyqtSignal(str)

    def __init__(self, dapi_path, mask_path, y0, y1, x0, x1,
                 sub=1, fused_zarr_path=None):
        super().__init__()
        self.dapi_path = dapi_path
        self.mask_path = mask_path
        self.y0, self.y1, self.x0, self.x1 = y0, y1, x0, x1
        self.sub = sub

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

    def run(self):
        try:
            y0, y1, x0, x1, sub = self.y0, self.y1, self.x0, self.x1, self.sub

            dapi_raw = self._read_region(self.dapi_path, y0, y1, x0, x1, sub)
            mask_raw = self._read_region(self.mask_path, y0, y1, x0, x1, sub)

            grey_u8  = self._norm_u8(dapi_raw)
            dapi_rgb = np.stack([grey_u8, grey_u8, grey_u8], axis=-1)

            mask_u32 = mask_raw.astype(np.uint32)
            n_cells  = int(mask_u32.max())

            # cellpose_mask_overlay enforces a minimum brightness of 0.15
            # on mask pixels — cytoplasm with weak DAPI signal stays visible
            rgb_overlay = cellpose_mask_overlay(grey_u8, mask_u32)

            h, w = grey_u8.shape
            self.done.emit(rgb_overlay, dapi_rgb, n_cells, h, w)
        except Exception as e:
            self.error.emit(str(e))


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
        self._dapi_rgb    = None   # uint8 (H,W,3)  grey RGB
        self._mask_rgb    = None   # uint8 (H,W,3)  cellpose overlay (precomputed, fully opaque)
        self._show_mask   = True
        self._mask_alpha  = 0.6    # default opacity: 60%

        self._thumb_loader  = None
        self._region_loader = None

        # Debounce: only fire region load 400 ms after last ROI change
        self._load_debounce = QTimer()
        self._load_debounce.setSingleShot(True)
        self._load_debounce.timeout.connect(self._load_region)

        self._build_ui()

    # ── public API ────────────────────────────────────────────────────

    def set_output_dir(self, output_dir):
        """Called from MainWindow when Step 2 finishes."""
        import glob as _glob

        # Try exact names first (full-WSI mode)
        dapi = os.path.join(output_dir, 'global_dapi.ome.tiff')
        mask = os.path.join(output_dir, 'global_mask.ome.tiff')

        # Fallback: ROI mode produces global_dapi_<ROI>.ome.tiff
        if not os.path.exists(dapi):
            candidates = sorted(_glob.glob(
                os.path.join(output_dir, 'global_dapi_*.ome.tiff')
            ))
            if candidates:
                dapi = candidates[0]   # take first ROI

        if not os.path.exists(mask):
            candidates = sorted(_glob.glob(
                os.path.join(output_dir, 'global_mask_*.ome.tiff')
            ))
            if candidates:
                mask = candidates[0]

        if os.path.exists(dapi) and os.path.exists(mask):
            self._dapi_path = dapi
            self._mask_path = mask
            self._load_thumbnail()
        else:
            self._status(
                f'\u26a0  global_dapi.ome.tiff / global_mask.ome.tiff not found.\n'
                f'Searched in: {output_dir}\n'
                f'Also tried: global_dapi_*.ome.tiff'
            )

    def load_from_paths(self, dapi_path, mask_path):
        """Manual load via Browse buttons."""
        if not os.path.exists(dapi_path):
            self._status(f'\u26a0  DAPI file not found:\n{dapi_path}')
            return
        if not os.path.exists(mask_path):
            self._status(f'\u26a0  Mask file not found:\n{mask_path}')
            return
        self._dapi_path = dapi_path
        self._mask_path = mask_path
        self._load_thumbnail()

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

        # File input box
        file_box = QGroupBox('Input Files')
        file_box.setStyleSheet(
            'QGroupBox{border:1px solid #61afef;border-radius:5px;'
            'margin-top:4px;font-weight:bold;color:#61afef;font-size:11px;}'
        )
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
        r2, self._mask_edit, btn_mask = _file_row('Mask:', '_mask_edit')
        fl.addLayout(r1)
        fl.addLayout(r2)

        btn_dapi.clicked.connect(lambda: self._browse('dapi'))
        btn_mask.clicked.connect(lambda: self._browse('mask'))

        btn_load = QPushButton('▶  Load Files & Thumbnail')
        btn_load.setStyleSheet(
            'QPushButton{background:#255;color:white;border-radius:3px;padding:4px;}'
            'QPushButton:hover{background:#377;}'
        )
        btn_load.clicked.connect(self._manual_load)
        fl.addWidget(btn_load)
        left_lay.addWidget(file_box)

        # Thumbnail canvas
        left_lay.addWidget(QLabel(
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
        left_lay.addWidget(self._ov_gv, stretch=1)

        self._thumb_status = QLabel('No files loaded')
        self._thumb_status.setStyleSheet('color:#888;font-size:10px;')
        self._thumb_status.setAlignment(Qt.AlignCenter)
        self._thumb_status.setWordWrap(True)
        left_lay.addWidget(self._thumb_status)

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

        left_lay.addWidget(samp_box)
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

        self._btn_mask_toggle = QPushButton('Hide Mask')
        self._btn_mask_toggle.setCheckable(True)
        self._btn_mask_toggle.setStyleSheet(
            'QPushButton{background:#353;color:#8e8;border:1px solid #8e8;'
            'border-radius:3px;font-size:11px;padding:3px 8px;}'
            'QPushButton:checked{background:#533;color:#e88;border:1px solid #e88;}'
        )
        self._btn_mask_toggle.clicked.connect(self._toggle_mask)
        toolbar.addWidget(self._btn_mask_toggle)

        # Mask opacity slider
        toolbar.addWidget(QLabel('Opacity:'))
        self._alpha_slider = QSlider(Qt.Horizontal)
        self._alpha_slider.setRange(0, 100)
        self._alpha_slider.setValue(60)
        self._alpha_slider.setFixedWidth(110)
        self._alpha_slider.setToolTip('Mask opacity (0% = transparent, 100% = opaque)')
        self._alpha_slider.setStyleSheet(
            'QSlider::groove:horizontal{height:4px;background:#333;border-radius:2px;}'
            'QSlider::handle:horizontal{width:12px;height:12px;margin:-4px 0;'
            'background:#8e8;border-radius:6px;}'
            'QSlider::sub-page:horizontal{background:#4a7;border-radius:2px;}'
        )
        self._alpha_lbl = QLabel('60%')
        self._alpha_lbl.setFixedWidth(34)
        self._alpha_lbl.setStyleSheet('color:#8e8;font-size:10px;')

        def _on_alpha(v):
            self._mask_alpha = v / 100.0
            self._alpha_lbl.setText(f'{v}%')
            self._render_roi()

        self._alpha_slider.valueChanged.connect(_on_alpha)
        toolbar.addWidget(self._alpha_slider)
        toolbar.addWidget(self._alpha_lbl)

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
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 2)
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

        btn_next = QPushButton('Next → Step 4: Feature Extraction')
        btn_next.setStyleSheet(
            'QPushButton{background:#246;color:white;border-radius:4px;'
            'padding:7px 18px;font-size:12px;font-weight:bold;}'
            'QPushButton:hover{background:#358;}'
        )
        btn_next.clicked.connect(self.go_step4.emit)
        nav.addWidget(btn_next)
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
            self._thumb_status.setText(f'⚠  Cannot read DAPI: {e}')
            return

        # Update edit boxes
        self._dapi_edit.setText(self._dapi_path)
        self._mask_edit.setText(self._mask_path)

        # Start background loader
        if self._thumb_loader and self._thumb_loader.isRunning():
            self._thumb_loader.terminate()

        self._thumb_loader = _ThumbLoader(self._dapi_path, self._OV_DS)
        self._thumb_loader.done.connect(self._on_thumb_loaded)
        self._thumb_loader.error.connect(
            lambda e: self._thumb_status.setText(f'⚠  Thumbnail error: {e}')
        )
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

        self._roi_status.setText('Loading…')
        self._cell_count_lbl.setText('')

        if self._region_loader and self._region_loader.isRunning():
            self._region_loader.terminate()
            self._region_loader.wait(200)

        self._region_loader = _RegionLoader(
            self._dapi_path, self._mask_path,
            y0, y1, x0, x1, sub=sub,
            fused_zarr_path=self._fused_zarr_path,
        )
        self._region_loader.done.connect(self._on_region_loaded)
        self._region_loader.error.connect(
            lambda e: self._roi_status.setText(f'⚠  {e}')
        )
        self._region_loader.start()

    def _on_region_loaded(self, rgb_overlay, dapi_rgb, n_cells, h, w):
        # Both arrays are already computed in the background thread.
        # Main thread only stores them and calls setImage — no heavy work here.
        self._mask_rgb  = rgb_overlay   # precomputed cellpose overlay
        self._dapi_rgb  = dapi_rgb      # plain grey RGB (for "hide mask" mode)
        sub = self._sub_spin.value()
        self._cell_count_lbl.setText(f'Cells in ROI: {n_cells:,}')
        self._roi_status.setText(f'{h:,}×{w:,} px  (sub ×{sub})')
        self._render_roi(reset_view=True)

    @staticmethod
    def _overlay_mask_on_dapi(dapi_rgb, mask):
        """Kept for API compatibility — actual overlay is precomputed in _RegionLoader."""
        return cellpose_mask_overlay(dapi_rgb[:, :, 0], mask)

    def _render_roi(self, reset_view=False):
        if self._dapi_rgb is None:
            return
        if self._show_mask and self._mask_rgb is not None:
            a = self._mask_alpha
            if a >= 0.999:
                display = self._mask_rgb
            elif a <= 0.001:
                display = self._dapi_rgb
            else:
                # Fast vectorised alpha blend: overlay * a + dapi * (1-a)
                display = (
                    self._mask_rgb.astype(np.float32) * a
                    + self._dapi_rgb.astype(np.float32) * (1.0 - a)
                ).astype(np.uint8)
        else:
            display = self._dapi_rgb
        first = (self._roi_img.image is None)
        self._roi_img.setImage(display, autoLevels=False)
        if first or reset_view:
            self._roi_vb.autoRange()

    def _toggle_mask(self):
        self._show_mask = not self._btn_mask_toggle.isChecked()
        self._btn_mask_toggle.setText(
            'Show Mask' if not self._show_mask else 'Hide Mask'
        )
        # Instant toggle — just swap the already-computed array, no calculation
        self._render_roi(reset_view=False)

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
        path, _ = QFileDialog.getOpenFileName(
            self, f'Select {"DAPI" if which == "dapi" else "Mask"} OME-TIFF',
            OUTPUT_DIR, 'OME-TIFF (*.tiff *.tif)'
        )
        if path:
            if which == 'dapi':
                self._dapi_edit.setText(path)
            else:
                self._mask_edit.setText(path)

    def _manual_load(self):
        self.load_from_paths(
            self._dapi_edit.text().strip(),
            self._mask_edit.text().strip(),
        )

    def _status(self, msg):
        self._thumb_status.setText(msg)

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
                        self._dapi_rgb = None
                        self._mask     = None
                        self._roi_img.clear()
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

class FeatureExtractWorker(QThread):
    """
    Extract per-cell intensity + morphology features from:
      • global_mask  (uint32 memmap or OME-TIFF)
      • original OME-TIFF (all channels, lazy region read via zarr)

    Strategy: full-image pass.
      1. Load global_mask as memmap (uint32, ~8 GB for 59k×35k).
      2. Use skimage.measure.regionprops on the mask once to get
         morphology features + per-cell bounding boxes.
      3. For each channel, read the full page via zarr, then use
         scipy.ndimage.mean / median / sum with the label array.
         Peak memory ≈ mask (8 GB) + one channel (4 GB) = 12 GB.
      4. Write cell_features.csv + cell_features.h5ad.

    Signals:
        progress(done_channels, total_channels, msg)
        finished(output_dir)
        error(traceback_str)
    """

    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(str, str)   # (output_dir, base_name)
    error    = pyqtSignal(str)

    def __init__(self, mask_path, ome_tiff_path, output_dir,
                 channel_names=None, statistics=None, file_prefix=None):
        super().__init__()
        self.mask_path     = mask_path
        self.ome_tiff_path = ome_tiff_path
        self.output_dir    = output_dir
        self.channel_names = channel_names
        self.statistics    = statistics if statistics else ['mean']
        # base filename: "{prefix}_cell_features" or "cell_features"
        p = file_prefix.strip() if file_prefix else ''
        self.base_name = f'{p}_cell_features' if p else 'cell_features'
        self._stop     = False

    def stop(self):
        self._stop = True

    # ── helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _load_mask(mask_path, full_h, full_w):
        """Load mask as read-only uint32 memmap (flat .dat) or via OME-TIFF."""
        if mask_path.endswith('.dat'):
            return np.memmap(mask_path, dtype='uint32', mode='r',
                             shape=(full_h, full_w))
        # OME-TIFF path: load via zarr, materialise into memmap-like array
        tif   = tifffile.TiffFile(mask_path)
        store = tif.aszarr()
        try:
            z  = zarr.open(store, mode='r')
            z0 = z[0] if isinstance(z, zarr.hierarchy.Group) else z
            if z0.ndim == 3:
                arr = np.array(z0[0], dtype='uint32')
            else:
                arr = np.array(z0, dtype='uint32')
        finally:
            store.close()
            tif.close()
        return arr

    @staticmethod
    def _read_channel(ome_tiff_path, page_idx, full_h, full_w,
                      y0=0, x0=0, h=None, w=None):
        """
        Read one channel from OME-TIFF, return float32.
        If h/w are given, read only the sub-region [y0:y0+h, x0:x0+w]
        (ROI mode).  Otherwise read the full page.
        """
        tif   = tifffile.TiffFile(ome_tiff_path)
        store = tif.aszarr()
        try:
            z  = zarr.open(store, mode='r')
            z0 = z[0] if isinstance(z, zarr.hierarchy.Group) else z
            y1 = (y0 + h) if h is not None else full_h
            x1 = (x0 + w) if w is not None else full_w
            if z0.ndim == 3:
                arr = np.array(z0[page_idx, y0:y1, x0:x1], dtype=np.float32)
            elif z0.ndim == 4:
                arr = np.array(z0[0, page_idx, y0:y1, x0:x1], dtype=np.float32)
            else:
                arr = np.array(z0[y0:y1, x0:x1], dtype=np.float32)
        finally:
            store.close()
            tif.close()
        return arr

    # ── main ──────────────────────────────────────────────────────────

    def run(self):
        try:
            import xml.etree.ElementTree as ET
            from scipy.ndimage import (
                mean               as nd_mean,
                sum                as nd_sum,
                median             as nd_median,
                standard_deviation as nd_std,
                minimum            as nd_minimum,
                maximum            as nd_maximum,
                labeled_comprehension as nd_lc,
            )
            import pandas as pd   # only needed for h5ad obs table (imported lazily below)
            _ = pd  # suppress unused warning; actual use is inside h5ad block

            self.progress.emit(0, 1, 'Parsing OME-TIFF metadata…')

            # ── Channel list ──────────────────────────────────────────
            with tifffile.TiffFile(self.ome_tiff_path) as tif:
                root  = ET.fromstring(tif.ome_metadata)
                page0 = tif.pages[0]
                full_h = page0.imagelength
                full_w = page0.imagewidth

            ns = {'ome': 'http://www.openmicroscopy.org/Schemas/OME/2016-06'}
            ch_nodes = root.findall('.//ome:Channel', ns)
            if self.channel_names and len(self.channel_names) == len(ch_nodes):
                ch_names = list(self.channel_names)
            else:
                ch_names = [ch.get('Name', f'ch_{i:02d}')
                            for i, ch in enumerate(ch_nodes)]
            n_ch = len(ch_names)

            # ── Load mask — get TRUE shape from the file itself ───────
            # In ROI mode the mask is the size of the ROI bounding box,
            # NOT the full OME-TIFF.  Always trust the mask's own shape.
            self.progress.emit(0, n_ch + 1, 'Loading mask…')
            mask = self._load_mask(self.mask_path, full_h, full_w)
            mask_h, mask_w = mask.shape   # actual mask dimensions

            # ── ROI bbox: where the mask sits inside the full OME-TIFF ─
            # Priority:
            #   1. segmentation_meta_<ROI>.json  → 'bbox': [y0,y1,x0,x1]
            #   2. fused_<ROI>.zarr attrs        → 'bbox_fullres': [y0,y1,x0,x1]
            #   3. global_mask_<ROI>.zarr attrs  → 'bbox_fullres'
            #   4. fallback to (0,0) for full-WSI mode
            bbox_y0, bbox_y1, bbox_x0, bbox_x1 = 0, mask_h, 0, mask_w
            mask_dir = os.path.dirname(self.mask_path)

            # 1. Try segmentation meta JSON
            import glob as _glob
            meta_candidates = (
                _glob.glob(os.path.join(mask_dir, 'segmentation_meta*.json')) +
                _glob.glob(os.path.join(mask_dir, '*_segmentation_meta.json'))
            )
            bbox_found = False
            for mc in meta_candidates:
                try:
                    with open(mc) as _f:
                        _m = json.load(_f)
                    bb = _m.get('bbox') or _m.get('bbox_fullres')
                    if bb and len(bb) >= 4:
                        bbox_y0, bbox_y1 = int(bb[0]), int(bb[1])
                        bbox_x0, bbox_x1 = int(bb[2]), int(bb[3])
                        bbox_found = True
                        break
                except Exception:
                    pass

            # 2. Try fused zarr attrs
            if not bbox_found:
                for zarr_name in _glob.glob(os.path.join(mask_dir, 'fused*.zarr')):
                    try:
                        _z = zarr.open(zarr_name, mode='r')
                        bb = _z.attrs.get('bbox_fullres')
                        if bb and len(bb) >= 4:
                            bbox_y0, bbox_y1 = int(bb[0]), int(bb[1])
                            bbox_x0, bbox_x1 = int(bb[2]), int(bb[3])
                            bbox_found = True
                            break
                    except Exception:
                        pass

            # 3. Try mask zarr attrs
            if not bbox_found:
                mask_zarr = self.mask_path.replace('.ome.tiff', '.zarr').replace('.dat', '.zarr')
                if os.path.exists(mask_zarr):
                    try:
                        _z = zarr.open(mask_zarr, mode='r')
                        bb = _z.attrs.get('bbox_fullres') or _z.attrs.get('bbox')
                        if bb and len(bb) >= 4:
                            bbox_y0, bbox_y1 = int(bb[0]), int(bb[1])
                            bbox_x0, bbox_x1 = int(bb[2]), int(bb[3])
                            bbox_found = True
                    except Exception:
                        pass

            if bbox_found:
                self.progress.emit(0, n_ch + 1,
                    f'ROI bbox: y=[{bbox_y0},{bbox_y1}) x=[{bbox_x0},{bbox_x1})  '
                    f'mask: {mask_h}×{mask_w} px')
            else:
                self.progress.emit(0, n_ch + 1,
                    f'No bbox found — assuming full-WSI mode  '
                    f'mask: {mask_h}×{mask_w} px')

            n_cells = int(mask.max())
            if n_cells == 0:
                self.error.emit('Mask is empty — no cells found.')
                return
            labels = np.arange(1, n_cells + 1)

            self.progress.emit(0, n_ch + 1,
                               f'Computing morphology for {n_cells:,} cells…')

            # ── Morphology — pure numpy/scipy, no scikit-image ────────
            # Coordinate grids in mask-local coordinates (float32)
            ys = np.repeat(
                np.arange(mask_h, dtype=np.float32), mask_w
            ).reshape(mask_h, mask_w)
            xs = np.tile(
                np.arange(mask_w, dtype=np.float32), mask_h
            ).reshape(mask_h, mask_w)

            # area (px²)
            ones  = (mask > 0).astype(np.float32)
            area  = nd_sum(ones, mask, labels)          # shape (n_cells,)

            # centroid (mask-local coords + bbox offset = full-image coords)
            cy_local = nd_mean(ys, mask, labels)
            cx_local = nd_mean(xs, mask, labels)
            cy = cy_local + bbox_y0
            cx = cx_local + bbox_x0

            # Second-order central moments → inertia ellipse
            # mu20 = E[(x - cx)²],  mu02 = E[(y - cy)²],  mu11 = E[(x-cx)(y-cy)]
            # Using numpy broadcasting-free approach: compute raw moments then shift
            # raw moments:
            m20 = nd_mean(xs * xs, mask, labels)
            m02 = nd_mean(ys * ys, mask, labels)
            m11 = nd_mean(xs * ys, mask, labels)
            del ys, xs, ones
            gc.collect()

            # Central moments (Steiner's theorem)
            mu20 = m20 - cx * cx
            mu02 = m02 - cy * cy
            mu11 = m11 - cx * cy
            del m20, m02, m11

            # Inertia eigenvalues  →  axis lengths (same formula as skimage)
            tmp   = np.sqrt(np.maximum(0.0, (mu20 - mu02)**2 + 4.0 * mu11**2))
            lam1  = 0.5 * (mu20 + mu02 + tmp)   # larger eigenvalue
            lam2  = 0.5 * (mu20 + mu02 - tmp)   # smaller eigenvalue
            lam2  = np.maximum(lam2, 0.0)        # numerical safety
            del tmp, mu20, mu02, mu11

            # skimage convention: axis_length = 4 * sqrt(eigenvalue)
            major_axis = 4.0 * np.sqrt(lam1)
            minor_axis = 4.0 * np.sqrt(lam2)

            # eccentricity = sqrt(1 - (b/a)²), 0 for circle, →1 for line
            with np.errstate(invalid='ignore', divide='ignore'):
                ecc = np.where(
                    lam1 > 0,
                    np.sqrt(np.maximum(0.0, 1.0 - lam2 / lam1)),
                    0.0,
                )
            del lam1, lam2

            # perimeter — boundary pixel count (4-connectivity approximation)
            # A boundary pixel has at least one background neighbour.
            # This is ~O(H*W) but still much faster than regionprops loop.
            from scipy.ndimage import binary_erosion as _bin_erode
            bin_mask = (mask > 0)
            eroded   = _bin_erode(bin_mask, structure=np.ones((3, 3), dtype=bool))
            boundary = (bin_mask & ~eroded).astype(np.float32)
            del bin_mask, eroded
            gc.collect()
            perimeter = nd_sum(boundary.astype(np.float32), mask, labels)
            del boundary

            if self._stop:
                self.error.emit('Stopped by user.')
                return

            stats   = self.statistics     # e.g. ['mean', 'sum', 'p90']
            intensity_cols = {}

            for ci, ch_name in enumerate(ch_names):
                if self._stop:
                    self.error.emit('Stopped by user.')
                    return

                stat_str = ' / '.join(stats)
                self.progress.emit(
                    ci + 1, n_ch + 1,
                    f'[{ci+1}/{n_ch}]  {ch_name}  —  {stat_str}…'
                )

                ch_data = self._read_channel(
                    self.ome_tiff_path, ci, full_h, full_w,
                    y0=bbox_y0, x0=bbox_x0, h=mask_h, w=mask_w,
                )
                safe = ch_name.replace('/', '_').replace(' ', '_')

                def _f64(arr):
                    return np.asarray(arr, dtype=np.float64)

                if 'mean'   in stats:
                    intensity_cols[f'{safe}_mean']   = _f64(nd_mean(ch_data, mask, labels))
                if 'sum'    in stats:
                    intensity_cols[f'{safe}_sum']    = _f64(nd_sum(ch_data, mask, labels))
                if 'median' in stats:
                    intensity_cols[f'{safe}_median'] = _f64(nd_median(ch_data, mask, labels))
                if 'std'    in stats:
                    intensity_cols[f'{safe}_std']    = _f64(nd_std(ch_data, mask, labels))
                if 'min'    in stats:
                    intensity_cols[f'{safe}_min']    = _f64(nd_minimum(ch_data, mask, labels))
                if 'max'    in stats:
                    intensity_cols[f'{safe}_max']    = _f64(nd_maximum(ch_data, mask, labels))
                if 'p90'    in stats:
                    intensity_cols[f'{safe}_p90']    = _f64(nd_lc(
                        ch_data, mask, labels,
                        lambda v: float(np.percentile(v, 90)),
                        float, default=0.0,
                    ))

                del ch_data
                gc.collect()

            if self._stop:
                self.error.emit('Stopped by user.')
                return

            # ── Assemble and write outputs (bypass pandas constructor bug) ─
            self.progress.emit(n_ch + 1, n_ch + 1, 'Writing outputs…')
            os.makedirs(self.output_dir, exist_ok=True)

            # Column names and data arrays (all float64)
            morph_cols = ['cell_id', 'area', 'centroid_y', 'centroid_x',
                          'perimeter', 'major_axis', 'minor_axis', 'eccentricity']
            morph_arrays = [
                labels.astype(np.float64),
                np.asarray(area,       dtype=np.float64),
                np.asarray(cy,         dtype=np.float64),
                np.asarray(cx,         dtype=np.float64),
                np.asarray(perimeter,  dtype=np.float64),
                np.asarray(major_axis, dtype=np.float64),
                np.asarray(minor_axis, dtype=np.float64),
                np.asarray(ecc,        dtype=np.float64),
            ]
            del area, cy, cx, perimeter, major_axis, minor_axis, ecc
            gc.collect()

            intens_col_names = list(intensity_cols.keys())
            intens_arrays = [np.asarray(v, dtype=np.float64)
                             for v in intensity_cols.values()]
            del intensity_cols
            gc.collect()

            all_cols   = morph_cols + intens_col_names
            all_arrays = morph_arrays + intens_arrays

            # Stack into one (n_cells, n_features) matrix
            data_matrix = np.column_stack(all_arrays)  # float64
            del morph_arrays, intens_arrays, all_arrays
            gc.collect()

            # ── CSV (written with numpy — no pandas needed) ───────────
            csv_path = os.path.join(self.output_dir, f'{self.base_name}.csv')
            header = ','.join(all_cols)
            np.savetxt(csv_path, data_matrix, delimiter=',',
                       header=header, comments='', fmt='%.6g')
            self.progress.emit(n_ch + 1, n_ch + 1,
                               f'CSV written  ({n_cells:,} cells × '
                               f'{len(all_cols)} features)  →  {csv_path}')

            # ── h5ad (AnnData) ────────────────────────────────────────
            try:
                import anndata as ad

                primary_stat = stats[0]
                # Find column indices for each statistic
                ch_safe = [ch.replace('/', '_').replace(' ', '_') for ch in ch_names]

                x_cols_idx = [all_cols.index(f'{s}_{primary_stat}')
                              for s in ch_safe
                              if f'{s}_{primary_stat}' in all_cols]
                X = data_matrix[:, x_cols_idx].astype(np.float32)

                morph_idx = {c: all_cols.index(c) for c in morph_cols}
                import pandas as _pd
                obs = _pd.DataFrame({
                    'cell_id':      data_matrix[:, morph_idx['cell_id']].astype(int).tolist(),
                    'centroid_y':   data_matrix[:, morph_idx['centroid_y']].tolist(),
                    'centroid_x':   data_matrix[:, morph_idx['centroid_x']].tolist(),
                    'area':         data_matrix[:, morph_idx['area']].tolist(),
                    'perimeter':    data_matrix[:, morph_idx['perimeter']].tolist(),
                    'major_axis':   data_matrix[:, morph_idx['major_axis']].tolist(),
                    'minor_axis':   data_matrix[:, morph_idx['minor_axis']].tolist(),
                    'eccentricity': data_matrix[:, morph_idx['eccentricity']].tolist(),
                })
                obs.index = obs['cell_id'].astype(str)
                obs.index.name = None

                import pandas as _pd2
                adata = ad.AnnData(
                    X   = X,
                    obs = obs,
                    var = _pd2.DataFrame(index=ch_names),
                )
                for s in stats:
                    if s == primary_stat:
                        continue
                    s_idx = [all_cols.index(f'{c}_{s}')
                             for c in ch_safe
                             if f'{c}_{s}' in all_cols]
                    if s_idx:
                        adata.obsm[s] = data_matrix[:, s_idx].astype(np.float32)

                adata.uns['ome_tiff']    = self.ome_tiff_path
                adata.uns['mask_path']   = self.mask_path
                adata.uns['n_cells']     = int(n_cells)
                adata.uns['statistics']  = stats
                adata.uns['x_statistic'] = primary_stat

                h5ad_path = os.path.join(self.output_dir, f'{self.base_name}.h5ad')
                adata.write_h5ad(h5ad_path)
                self.progress.emit(n_ch + 1, n_ch + 1,
                                   f'h5ad written  →  {h5ad_path}')
            except ImportError:
                self.progress.emit(n_ch + 1, n_ch + 1,
                                   '⚠  anndata not installed — h5ad skipped. '
                                   'Run: pip install anndata')

            del data_matrix

            del mask
            gc.collect()

            self.finished.emit(self.output_dir, self.base_name)

        except Exception:
            self.error.emit(traceback.format_exc())


# ══════════════════════════════════════════════════════════════════════
#  Step 4 Page  — Cell Feature Extraction
# ══════════════════════════════════════════════════════════════════════

class Step4Page(QWidget):

    go_back = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker = None
        self._build_ui()

    # ── public API ────────────────────────────────────────────────────

    def set_paths(self, mask_path, ome_tiff_path, output_dir):
        """Called from MainWindow after Step 2 finishes."""
        if mask_path:
            self._mask_edit.setText(mask_path)
        if ome_tiff_path:
            self._ome_edit.setText(ome_tiff_path)
        if output_dir:
            self._out_edit.setText(output_dir)

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

        self._mask_edit = _file_row(
            il, 'Mask (.dat/.ome.tiff):',
            'global_mask.dat  or  global_mask.ome.tiff',
            lambda: self._browse_file(self._mask_edit,
                                      'Mask file (*.dat *.tiff *.tif)')
        )
        self._ome_edit = _file_row(
            il, 'OME-TIFF (original):',
            'original multichannel OME-TIFF',
            lambda: self._browse_file(self._ome_edit,
                                      'OME-TIFF (*.tif *.tiff)')
        )
        self._ome_edit.setText(OME_TIFF_FILE)
        root.addWidget(inp)

        # ── Statistics selection ───────────────────────────────────────
        stat_box = _box('Intensity Statistics  (multi-select)', '#e5c07b')
        stl = QVBoxLayout(stat_box)

        # Note label
        note = QLabel(
            'The first checked item becomes the X matrix in h5ad (scanpy default).\n'
            'All checked items are saved in CSV.  p90 is slowest.'
        )
        note.setStyleSheet('color:#888;font-size:10px;')
        stl.addWidget(note)

        # Checkboxes — (key, display_label, default_checked)
        _stat_defs = [
            ('mean',   'Mean',            True),
            ('median', 'Median',          False),
            ('sum',    'Sum (total int.)', False),
            ('std',    'Std dev',         False),
            ('min',    'Min',             False),
            ('max',    'Max',             False),
            ('p90',    '90th percentile', False),
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

        # "X matrix =" info label, updates when checkboxes change
        self._x_lbl = QLabel('X matrix in h5ad:  mean')
        self._x_lbl.setStyleSheet('color:#e5c07b;font-size:10px;')
        stl.addWidget(self._x_lbl)

        def _update_x_lbl():
            first = next(
                (k for k, cb in self._stat_checks.items() if cb.isChecked()),
                None
            )
            self._x_lbl.setText(
                f'X matrix in h5ad:  {first}' if first
                else '⚠  Select at least one statistic'
            )
        for cb in self._stat_checks.values():
            cb.stateChanged.connect(_update_x_lbl)

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
        self._out_edit.setText(OUTPUT_DIR)

        # Filename prefix row
        prefix_row = QHBoxLayout()
        prefix_row.addWidget(QLabel('Filename prefix (optional):'))
        self._prefix_edit = QtWidgets.QLineEdit()
        self._prefix_edit.setPlaceholderText('留空则使用默认名: cell_features.csv / .h5ad')
        self._prefix_edit.setStyleSheet('font-size:11px;')
        prefix_row.addWidget(self._prefix_edit, stretch=1)
        ol.addLayout(prefix_row)

        self._prefix_info = QLabel('Outputs:  cell_features.csv   cell_features.h5ad')
        self._prefix_info.setStyleSheet('color:#888;font-size:10px;')
        ol.addWidget(self._prefix_info)

        def _update_prefix_info():
            p = self._prefix_edit.text().strip()
            base = f'{p}_cell_features' if p else 'cell_features'
            self._prefix_info.setText(f'Outputs:  {base}.csv   {base}.h5ad')

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
        nav.addWidget(self._btn_run)

        root.addLayout(nav)

    # ── helpers ───────────────────────────────────────────────────────

    def _browse_file(self, edit, filt):
        p, _ = QFileDialog.getOpenFileName(self, 'Select file',
                                            OUTPUT_DIR, filt)
        if p:
            edit.setText(p)

    def _run(self):
        mask_path = self._mask_edit.text().strip()
        ome_path  = self._ome_edit.text().strip()
        out_dir   = self._out_edit.text().strip() or OUTPUT_DIR

        if not mask_path or not os.path.exists(mask_path):
            QMessageBox.warning(self, 'Missing input', 'Please select a valid mask file.')
            return
        if not ome_path or not os.path.exists(ome_path):
            QMessageBox.warning(self, 'Missing input', 'Please select the original OME-TIFF.')
            return

        stats = [k for k, cb in self._stat_checks.items() if cb.isChecked()]
        if not stats:
            QMessageBox.warning(self, 'No statistics selected',
                                'Please select at least one intensity statistic.')
            return

        prefix = self._prefix_edit.text().strip()

        self._worker = FeatureExtractWorker(
            mask_path     = mask_path,
            ome_tiff_path = ome_path,
            output_dir    = out_dir,
            statistics    = stats,
            file_prefix   = prefix,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)

        self._btn_run.setEnabled(False)
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
        self._btn_run.setEnabled(True)
        self._btn_stop.setEnabled(False)
        csv_p = os.path.join(out_dir, f'{base_name}.csv')
        h5_p  = os.path.join(out_dir, f'{base_name}.h5ad')
        QMessageBox.information(
            self, 'Feature extraction complete',
            f'Outputs:\n\n'
            f'  {csv_p}\n'
            f'  {h5_p}\n\n'
            f'Load {base_name}.h5ad in scanpy for downstream analysis.'
        )

    def _on_error(self, msg):
        self._prog_bar.setValue(0)
        self._btn_run.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._prog_lbl.setText('✗ Error — see terminal')
        QMessageBox.critical(self, 'Error', msg)
        print(f'[Step4 Error]\n{msg}')


# ══════════════════════════════════════════════════════════════════════
#  Entry Point
# ══════════════════════════════════════════════════════════════════════

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    pal = QtGui.QPalette()
    pal.setColor(QtGui.QPalette.Window,          QtGui.QColor(28, 28, 28))
    pal.setColor(QtGui.QPalette.WindowText,      QtGui.QColor(220, 220, 220))
    pal.setColor(QtGui.QPalette.Base,            QtGui.QColor(18, 18, 18))
    pal.setColor(QtGui.QPalette.AlternateBase,   QtGui.QColor(38, 38, 38))
    pal.setColor(QtGui.QPalette.Text,            QtGui.QColor(220, 220, 220))
    pal.setColor(QtGui.QPalette.Button,          QtGui.QColor(48, 48, 48))
    pal.setColor(QtGui.QPalette.ButtonText,      QtGui.QColor(220, 220, 220))
    pal.setColor(QtGui.QPalette.Highlight,       QtGui.QColor(42, 130, 218))
    pal.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor(0, 0, 0))
    app.setPalette(pal)

    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
