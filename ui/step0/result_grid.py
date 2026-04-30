"""
block01/ui/step0/result_grid.py — ResultGridPanel, ImageZoomDialog, ResultViewWindow.
"""

import json

import numpy as np

from PyQt5 import QtWidgets, QtCore, QtGui
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QSizePolicy, QMainWindow, QDialog,
    QProgressBar,
)
import pyqtgraph as pg

from ...config import PATCH_COLORS

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
