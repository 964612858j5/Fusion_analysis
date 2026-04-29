"""
block01/ui/step3_page.py — _RoiRect, _ThumbLoader, _RegionLoader, Step3Page.
"""

import os
import random

import numpy as np
import tifffile
import zarr

from PyQt5 import QtWidgets, QtCore, QtGui
from PyQt5.QtCore import Qt, QTimer, QRectF, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QGroupBox, QSplitter, QSlider, QMessageBox, QFileDialog,
)
import pyqtgraph as pg

from ..config import OUTPUT_DIR
from ..workers.cellpose_worker import cellpose_mask_overlay

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

