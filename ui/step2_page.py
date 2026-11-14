"""
block01/ui/step2_page.py — Step2Page (Segmentation & Merge).
"""

import os
import json
import time

import numpy as np
import zarr

from ..qt import QtWidgets, QtCore, QtGui
from ..qt.QtCore import Qt, QRectF, QThread, Signal
from ..qt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QGroupBox, QSplitter, QProgressBar, QMessageBox, QFileDialog,
    QDoubleSpinBox, QScrollArea, QComboBox, QCheckBox,
)
import pyqtgraph as pg

from .theme import widgets as theme_widgets
from ..config import OUTPUT_DIR

#: Step2's Tile Status overview: the longest side in pixels (block S2T).
OVERVIEW_LONG_SIDE = 4096
from ..utils.segmentation_config import (
    CELLPOSE_NUCLEI_DAPI,
    CELLPOSE_NUCLEI_EXPANSION,
    CELLPOSE_WHOLECELL_FUSION,
    CELLPOSE_WHOLECELL_NUCLEI,
    HYBRID_CELLPOSE_STARDIST,
    SEGMENTATION_METHODS,
    MESMER_WHOLE_CELL,
    MESMER_NUCLEI,
    MESMER_NUCLEAR_GUIDED,
    STARDIST_NUCLEI_DAPI,
    STARDIST_NUCLEI_EXPANSION,
    available_segmentation_methods,
    get_segmentation_method_config,
    normalize_segmentation_config,
)
from ..utils.segmentation_params import (
    PARAM_INDEX,
    params_index_path,
)
from ..utils.tile_strategy import suggest_tile_strategy
from ..utils.roi_project import mark_roi_step
from ..utils.mesmer_utils import parse_channel_list
from ..workers.segment_merge_worker import SegmentMergeWorker
from ..core import preseg_contract
from ..seg_runner import engines as seg_engines
from ..utils import segmentation_param_schema as param_schema


NUCLEUS_PASS_KEYS = ("nucleus_diameter", "nucleus_flow_threshold",
                     "nucleus_cellprob_threshold", "nucleus_min_size")


def _gpu_on(value):
    """A config's use_gpu (a bool, or an old Mesmer 'auto'/'gpu'/'cpu')."""
    return str(value if value is not None else True).strip().lower() not in {
        "0", "false", "no", "off", "cpu"}
from .step_frame import FRAME_TAB_QSS, StepFrame, StepFrameMetrics, free_tab_bar

# ══════════════════════════════════════════════════════════════════════
#  Step 2 Page  (Segmentation & Merge)
# ══════════════════════════════════════════════════════════════════════


#: the theme's colours for status and hint text (docs/v17_desktop_ui_plan.md §10)
_TK = theme_widgets.t()

class _CompactCombo(QComboBox):
    """Sized by a short minimum instead of its longest item (block L2): the
    box shows the choice elided when narrow; the list still opens as wide as
    its items."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(3)
        self.view().setTextElideMode(Qt.ElideNone)

    def showPopup(self):
        view = self.view()
        view.setMinimumWidth(max(self.width(), view.sizeHintForColumn(0) + 24))
        super().showPopup()


#: The narrowest a row's control gets (block L2).
_CONTROL_MIN_W = 48


def _let_shrink(widget):
    """Let a row's control give way to its label (block L2): a drop-down
    shows its choice elided, a number or text box narrows, a check box's own
    text is cut. Its value and meaning are unchanged."""
    if isinstance(widget, QComboBox):
        widget.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        widget.setMinimumContentsLength(3)
        # ...while its list still opens as wide as its items.
        view = widget.view()
        view.setTextElideMode(Qt.ElideNone)

        def fit_list(*_a, combo=widget):
            combo.view().setMinimumWidth(combo.view().sizeHintForColumn(0) + 24)
        fit_list()
        widget.model().rowsInserted.connect(fit_list)
        widget.model().modelReset.connect(fit_list)
    if isinstance(widget, (QComboBox, QtWidgets.QAbstractSpinBox, QtWidgets.QLineEdit,
                           QCheckBox, QLabel)):
        widget.setMinimumWidth(_CONTROL_MIN_W)
        policy = widget.sizePolicy()
        policy.setHorizontalPolicy(QtWidgets.QSizePolicy.Expanding
                                   if not isinstance(widget, (QCheckBox, QLabel))
                                   else QtWidgets.QSizePolicy.Preferred)
        widget.setSizePolicy(policy)


class _ElidedLabel(QLabel):
    """A one-line label that elides to its width; the whole text is the
    tooltip (block L2)."""

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self._full = text
        self.setToolTip(text)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)

    def setText(self, text):
        self._full = text
        self.setToolTip(text)
        self._elide()

    def text(self):
        return self._full

    def minimumSizeHint(self):
        return QtCore.QSize(40, super().minimumSizeHint().height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def _elide(self):
        m = self.contentsMargins()
        width = max(0, self.width() - m.left() - m.right() - 10)
        QLabel.setText(self, self.fontMetrics().elidedText(self._full, Qt.ElideRight, width))


class _TileRect(pg.RectROI):
    """A Tile Status tile: the RectROI outline, with the skip look under it
    (docs/v17_step2_skip_tiles_ux_plan.md §2) -- a translucent fill and, for
    every skipped tile, diagonal hatching drawn in screen pixels so its
    density does not change with zoom."""

    fill = None                        # QColor, or None
    hatch = None                       # QColor of the hatching, or None
    frame = None                       # QPen of the Shift+drag box frame over it, or None

    def paint(self, p, opt, widget):
        if self.fill is not None or self.hatch is not None:
            r = QRectF(0, 0, self.state['size'][0], self.state['size'][1]).normalized()
            p.save()
            p.setPen(Qt.NoPen)
            if self.fill is not None:
                p.setBrush(self.fill)
                p.drawRect(r)
            if self.hatch is not None:
                screen = p.transform().mapRect(r)
                p.resetTransform()
                p.setBrush(QtGui.QBrush(self.hatch, Qt.BDiagPattern))
                p.drawRect(screen)
            p.restore()
        p.save()                                   # RectROI.paint scales the painter to a unit square
        super().paint(p, opt, widget)
        p.restore()
        if self.frame is not None:                 # the box's frame, over the result look
            r = QRectF(0, 0, self.state['size'][0], self.state['size'][1]).normalized()
            p.save()
            p.setPen(self.frame)
            p.setBrush(Qt.NoBrush)
            p.drawRect(r)
            p.restore()


class Step2Page(QWidget):
    """Full Step 2 UI: zarr input, tile grid, segmentation params, progress."""

    go_back           = Signal()
    segmentation_done = Signal(str)   # emits output_dir when done
    open_qc_requested = Signal(str)   # user explicitly chose Step3/QC
    # Block RM (§5, §9): the Input drop-down -- a fuse run chosen / its ×.
    fuse_chosen = Signal(str)
    fuse_delete_requested = Signal(str)
    _overview_ready = Signal(object)   # (token, array or None, error)

    # Tile status colours
    _COL_IDLE    = (80,  80,  80)
    _COL_RUNNING = (255, 200,  50)
    _COL_DONE    = ( 60, 200,  80)
    _COL_ERROR   = (220,  60,  60)

    def __init__(self, parent=None, metrics=None, title_bar=None):
        super().__init__(parent)
        self._ov_token = None
        self._overview_ready.connect(self._on_overview_ready, Qt.QueuedConnection)
        # The page frame (block A1b S3): the window hands over the one
        # `StepFrameMetrics` and a title bar built like Step3's; a page
        # standing on its own (the page-level tests) measures its own.
        self._frame_metrics_in = metrics
        self._title_bar_in = title_bar
        self._zarr_path      = None
        self._seg_config     = {}
        self._worker         = None
        self._tile_rects     = {}
        self._tile_status    = {}
        # The run's own progress, independent of the drawn grid: {"grid":
        # (rows, cols), "state": {tile_index: "running" | "done" | "skipped"}}.
        # The grid follows the spin boxes; this record follows the run.
        self._run_progress   = None
        self._run_active     = False
        # Block S2T: the skip proposal for the drawn grid (None = off) and the
        # slide tissue masks already computed {(slide, channel): SlideTissue}
        self._skip_plan      = None
        self._tissue_cache   = {}
        self._n_rows         = 3
        self._n_cols         = 4
        self._full_h         = 0
        self._full_w         = 0
        self._total_cells    = 0
        self._ov_thread      = None
        self._rois           = []
        self._last_output_dir = None   # set when segmentation finishes
        self._seg_param_file = ""
        self._seg_params_index = {}
        self._seg_params_dir = ""
        self._loading_index_selection = False
        self._parameter_source = "manual"
        self._applied_param_file = ""
        self._roi_id = ""
        self._roi_dir = ""
        self._step2_dir = ""
        # User ruling 2026-10-03: a workspace handed over while a run is
        # going (Step0 Save as) waits here; Step2 switches when the run ends.
        self._pending_context = None
        # Called once that waiting workspace is switched to (MainWindow then
        # gives the page that workspace's saved parameters).
        self._after_pending_context = None
        # Block RM: each region's fused product in the chosen fuse run.
        self._region_fused_paths = {}
        self._suggested_tile_strategy = {}

        self._build_ui()

    def set_roi_context(self, roi_id="", roi_dir="", step2_dir=""):
        # User ruling 2026-10-03: while a run is going, Step2 stays on the
        # workspace the run was started for. Another workspace waits and is
        # switched to when the run ends (completed, failed or stopped).
        token = (self._run_progress or {}).get("token") if self._run_active else None
        if token is not None:
            if (roi_id or "", roi_dir or "") != (token.get("roi_id", ""), token.get("roi_dir", "")):
                self._pending_context = (roi_id or "", roi_dir or "", step2_dir or "")
                print(f"[Step2] workspace {roi_id or '(none)'} will be opened when the "
                      f"running segmentation ends")
            return
        self._roi_id = roi_id or ""
        self._roi_dir = roi_dir or ""
        self._step2_dir = step2_dir or ""
        if self._step2_dir:
            os.makedirs(self._step2_dir, exist_ok=True)
            self._out_edit.setText(self._step2_dir)
        print(f"[Step2] roi_id={self._roi_id}")
        print(f"[Step2] output_base={self._step2_dir or self._out_edit.text().strip()}")
        # Block A6: a running run's record is drawn again when the page is
        # back on its context (or the older-run line shown when it is not).
        self._refresh_run_view()

    def _sync_output_dir_from_zarr_path(self, zarr_path):
        """Use the ROI step2 directory when the input zarr comes from ROI step1."""
        path = os.path.abspath(zarr_path or "")
        parts = path.split(os.sep)
        if "rois" not in parts or "step1" not in parts:
            return
        step1_idx = parts.index("step1")
        if step1_idx < 2 or parts[step1_idx - 2] != "rois":
            return
        roi_dir = os.sep + os.path.join(*parts[:step1_idx])
        roi_id = parts[step1_idx - 1]
        step2_dir = os.path.join(roi_dir, "step2")
        current = os.path.abspath(self._step2_dir or self._out_edit.text().strip() or OUTPUT_DIR)
        if current != os.path.abspath(step2_dir):
            self.set_roi_context(roi_id=roi_id, roi_dir=roi_dir, step2_dir=step2_dir)

    # ── UI construction ───────────────────────────────────────────────

    def _build_ui(self):
        # ONE `StepFrame` (block A1b S3), assembled at the end of this method.
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── The frame's right tab, `Tile Status`: overview + progress ──
        left = QWidget()
        ll   = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(4)

        # The tab's tool row (placed at assembly).
        self._tile_status_label = self._lbl('Tile Status Overview', bold=True)

        self._ov_gv  = pg.GraphicsLayoutWidget()
        self._ov_gv.setBackground('#111')
        self._ov_vb  = self._ov_gv.addViewBox()
        self._ov_vb.setAspectLocked(True)
        self._ov_vb.invertY(True)
        self._ov_vb.setMenuEnabled(False)
        self._ov_img = pg.ImageItem()
        self._ov_vb.addItem(self._ov_img)
        ll.addWidget(self._ov_gv, stretch=1)
        self._ov_layout = ll
        self._tile_colour_state = {}
        # A Shift+drag box over the tiles (docs/v17_step2_skip_tiles_ux_plan.md
        # §3): {"start": (r, c), "end": (r, c), "skip": bool} while dragged,
        # the boxed tiles' preview {(r, c): state}, and the overview press.
        self._box = None
        self._box_preview = {}
        self._ov_press = None
        self._ov_gv.viewport().installEventFilter(self)
        # Tiles collected while Shift is held (plan §13): {index: skip?},
        # committed with one question when Shift is let go -- seen by a
        # 100 ms check that runs only while something is collected.
        self._pending = {}
        self._shift_timer = QtCore.QTimer(self)
        self._shift_timer.setInterval(100)
        self._shift_timer.timeout.connect(self._shift_poll)
        self.dapi_viewer = None               # the zoomable DAPI picture (set by the main window)
        self.input_shown = None               # () -> None: another input is on screen
        # The TMA cores over the overview (docs/f5_tma_display_plan.md part 2):
        # the switch, and the source of the last Save's cores (set by the
        # main window: the same as Step1's).
        from .step1_button_styles import MODE_BUTTON_QSS
        self.btn_tma_cores = QPushButton("TMA cores")
        self.btn_tma_cores.setCheckable(True)
        self.btn_tma_cores.setChecked(True)
        self.btn_tma_cores.setStyleSheet(MODE_BUTTON_QSS)
        self.btn_tma_cores.setToolTip("Show the TMA cores: green = analysed by this run, "
                                      "yellow = not analysed; during a run amber = being segmented, "
                                      "filled green = done, red = failed.\n"
                                      "Names (from the map sheet) appear when zoomed in.")
        self.btn_tma_cores.setVisible(False)
        self.btn_tma_cores.toggled.connect(lambda _on: self.refresh_core_outlines())
        # The exclusions (blur, fold) of the same source, hatched as in
        # Regions, with their own switch (backlog 21).
        self.btn_exclusions = QPushButton("Exclusions")
        self.btn_exclusions.setCheckable(True)
        self.btn_exclusions.setChecked(True)
        self.btn_exclusions.setStyleSheet(MODE_BUTTON_QSS)
        self.btn_exclusions.setToolTip("Show the Regions exclusions (blur, fold, ...), hatched: "
                                       "those of the run on screen, else of the last Regions Save.")
        self.btn_exclusions.setVisible(False)
        self.btn_exclusions.toggled.connect(lambda _on: self.refresh_core_outlines())
        self.saved_core_items = None          # () -> overlay items of the last Regions Save
        self.saved_exclusion_items = None     # () -> the last Regions Save's exclusions (items)
        self.manual_regions = None            # (method, fused, rois, ask) -> (request, refusal), 9.2
        self._run_core_snapshot = None        # the snapshot the run on screen froze
        #: how many cores a run started now would follow (0: a grid run);
        #: docs/v17_step2_core_by_core_plan.md §3.6
        self._cores_preview = 0
        from .regions.core_outlines import CoreOutlines
        import types
        self._core_outlines = CoreOutlines(
            lambda: types.SimpleNamespace(graphics=self._ov_gv, view_box=self._ov_vb),
            self._core_items, self.btn_tma_cores.isChecked, to_view=self._ov_map)
        self._exclusion_outlines = CoreOutlines(
            lambda: types.SimpleNamespace(graphics=self._ov_gv, view_box=self._ov_vb),
            self._exclusion_items, self.btn_exclusions.isChecked, to_view=self._ov_map)

        self._ov_status = QLabel('No zarr loaded')
        self._ov_status.setStyleSheet(f'color:{_TK["text3"]};font-size:10px;')
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
        # Block A6 (user ruling 2026-10-02): a run started for another
        # workspace / dataset is still computing, or has just finished: said
        # here, with where it writes. Its numbers are never shown above.
        self._old_run_lbl = QLabel('')
        self._old_run_lbl.setStyleSheet(
            'color:#e5c07b;font-size:10px;padding:2px 4px;')
        self._old_run_lbl.setWordWrap(True)
        self._old_run_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._old_run_lbl.setVisible(False)
        ll.addWidget(self._old_run_lbl)

        self._prog_bar = QProgressBar()
        self._prog_bar.setRange(0, 100)
        self._prog_bar.setStyleSheet(
            'QProgressBar{border:1px solid #444;border-radius:3px;'
            'text-align:center;color:#fff;height:16px;}'
            'QProgressBar::chunk{background:#2a5;border-radius:3px;}'
        )
        ll.addWidget(self._prog_bar)

        self._cells_lbl = QLabel('Total cells detected: —')
        self._cells_lbl.setStyleSheet(f'color:{_TK["text3"]};font-size:11px;')
        ll.addWidget(self._cells_lbl)

        # ── THE public Channels panel's host ──────────────────────────
        # The same frame Step0 draws, holding the same component: Step2
        # CONSUMES the display answers (which channels are drawn, in what
        # colour) and edits them where every other step does. Its own
        # segmentation controls stay on the right.
        from .widgets.channel_dock import template as _channel_template
        self._channels_box = QGroupBox('Channels')
        self._channels_box.setStyleSheet(_channel_template.frame_qss())
        self._channels_host = QVBoxLayout(self._channels_box)
        self._channels_host.setContentsMargins(4, 4, 4, 4)
        self._channels_host.setSpacing(4)
        ll.addWidget(self._channels_box, stretch=1)
        # Not on screen in Step2 (block L2, user ruling 2026-09-25): the
        # frame and the ONE dock mounted into it are kept, only hidden.
        self._channels_box.setVisible(False)
        self._overview_panel = left

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

        # Block RM (§5): the Step1 results (fuse runs) of the chain being
        # viewed, newest first; the chosen one fills fused.zarr below.
        fr = QHBoxLayout()
        fr.addWidget(QLabel('Input:'))
        from .step3_mask_bar import TaggedItemDelegate
        self._fuse_combo = QComboBox()
        self._fuse_combo.setStyleSheet('font-size:11px;')
        fuse_delegate = TaggedItemDelegate(self._fuse_combo)
        fuse_delegate.close_clicked.connect(self._on_fuse_close)
        self._fuse_combo.setItemDelegate(fuse_delegate)
        self._fuse_combo.activated.connect(self._on_fuse_activated)
        self._fuse_items = []
        fr.addWidget(self._fuse_combo, stretch=1)
        inl.addLayout(fr)

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

        # User ruling 2026-10-03 (option a): Input Data shows only the
        # fused.zarr. The parameters come with the data version (shown under
        # Segmentation Parameters); the index row is kept for the code that
        # reads it, never shown.
        self._seg_params_row = QtWidgets.QWidget()
        pr = QHBoxLayout(self._seg_params_row)
        pr.setContentsMargins(0, 0, 0, 0)
        pr.addWidget(QLabel('Index:'))       # was 'Segmentation Index:' (block L2)
        self._seg_params_edit = QtWidgets.QLineEdit()
        self._seg_params_edit.setPlaceholderText('segmentation_params/ or segmentation_params_index.json')
        self._seg_params_edit.setStyleSheet('font-size:11px;')
        pr.addWidget(self._seg_params_edit, stretch=1)
        btn_params = QPushButton('Browse')
        btn_params.setFixedWidth(64)
        btn_params.clicked.connect(self._browse_seg_params)
        pr.addWidget(btn_params)
        inl.addWidget(self._seg_params_row)
        self._seg_params_row.setVisible(False)

        btn_load = QPushButton('Load zarr info && overview')
        # only Run is green; padding one less: the role's 1 px border keeps the size
        theme_widgets.button(btn_load, "panel", "padding:3px;")
        btn_load.clicked.connect(self._load_zarr_info)
        inl.addWidget(btn_load)

        self._zarr_info = QLabel('—')
        self._zarr_info.setStyleSheet(f'color:{_TK["text3"]};font-size:10px;')
        self._zarr_info.setWordWrap(True)
        inl.addWidget(self._zarr_info)
        rl.addWidget(inp_box)

        # The Step0 saved remap reaches Step2 through the Step1 fused input.

        # Tile grid
        tile_box = QGroupBox('Tile Grid (for segmentation)')
        tile_box.setStyleSheet(self._box_style('#e5c07b'))
        til = QVBoxLayout(tile_box)

        self._auto_tile_strategy = QCheckBox('Auto tile strategy')
        self._auto_tile_strategy.setChecked(False)
        self._auto_tile_strategy.stateChanged.connect(self._on_auto_tile_strategy_changed)
        til.addWidget(self._auto_tile_strategy)

        self._tile_suggestion_lbl = QLabel('Suggested: —')
        self._tile_suggestion_lbl.setStyleSheet(f'color:{_TK["text3"]};font-size:10px;')
        self._tile_suggestion_lbl.setWordWrap(True)
        til.addWidget(self._tile_suggestion_lbl)

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

        # Block S2T: skip the tiles whose read window holds no tissue (raw
        # slide, ~16x level). Off by default; a tile is clicked to change it.
        self._skip_empty_cb = QCheckBox('Skip tiles without tissue')
        self._skip_empty_cb.setChecked(False)
        self._skip_empty_cb.setToolTip(
            'Tiles whose read window (with its overlap) holds no tissue on the slide '
            'are not segmented. Click a tile in Tile Status to change it, or hold Shift '
            'and drag over several; skipping a tile with tissue asks first. A skipped '
            'tile has no cells.')
        self._skip_empty_cb.stateChanged.connect(self._on_skip_toggled)
        til.addWidget(self._skip_empty_cb)
        # Two shortcuts under the checkbox (user request, plan §3.4).
        self._skip_btn_row = QWidget()
        sbr = QHBoxLayout(self._skip_btn_row)
        sbr.setContentsMargins(0, 0, 0, 0)
        self._skip_all_empty_btn = QPushButton('Skip all empty')
        self._skip_all_empty_btn.setToolTip('Back to the automatic proposal: every tile '
                                            'without tissue is skipped; your own changes are cleared.')
        self._skip_all_empty_btn.clicked.connect(self._skip_all_empty)
        self._skip_none_btn = QPushButton('Skip none')
        self._skip_none_btn.setToolTip('Segment every tile.')
        self._skip_none_btn.clicked.connect(self._skip_none)
        sbr.addWidget(self._skip_all_empty_btn)
        sbr.addWidget(self._skip_none_btn)
        sbr.addStretch()
        self._skip_btn_row.setVisible(False)
        til.addWidget(self._skip_btn_row)
        self._skip_hint_lbl = QLabel('')
        self._skip_hint_lbl.setStyleSheet(f'color:{_TK["text3"]};font-size:10px;')
        self._skip_hint_lbl.setWordWrap(True)
        self._skip_hint_lbl.setVisible(False)
        til.addWidget(self._skip_hint_lbl)

        self._tile_ram_lbl = QLabel('')
        self._tile_ram_lbl.setStyleSheet(f'color:{_TK["text3"]};font-size:10px;')
        til.addWidget(self._tile_ram_lbl)

        # While a run computes: which grid shows its progress.
        self._run_grid_lbl = QLabel('')
        self._run_grid_lbl.setStyleSheet(f'color:{_TK["warning"]};font-size:10px;')
        self._run_grid_lbl.setVisible(False)
        til.addWidget(self._run_grid_lbl)

        self._rows_spin.valueChanged.connect(self._update_tile_info)
        self._cols_spin.valueChanged.connect(self._update_tile_info)
        # a grid typed by hand is checked when the typing is done, not per digit
        self._rows_spin.editingFinished.connect(self._keep_stardist_tiles_safe)
        self._cols_spin.editingFinished.connect(self._keep_stardist_tiles_safe)
        self._overlap_spin.valueChanged.connect(self._update_tile_info)
        rl.addWidget(tile_box)

        # Segmentation params
        cp_box = QGroupBox('Segmentation Parameters')
        cp_box.setStyleSheet(self._box_style('#c678dd'))
        cpl = QVBoxLayout(cp_box)

        def _param_row(label, widget):
            r = QHBoxLayout()
            l = QLabel(label)
            # Block L2 (user ruling 2026-09-25): the label is exactly as wide
            # as its text and never cut; the control after it is what gives
            # way when the shared channel column is narrow.
            l.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Preferred)
            r.addWidget(l)
            r.addWidget(widget, 1)
            _let_shrink(widget)
            return r, l, widget

        self._param_source_combo = _CompactCombo()
        self._param_source_combo.addItem('None / Manual default', 'manual')
        self._param_source_combo.addItem('From segmentation_params index', 'index')
        self._param_source_combo.currentIndexChanged.connect(self._on_param_source_changed)
        r, self._param_source_label, _ = _param_row('Source:', self._param_source_combo)
        cpl.addLayout(r)

        self._index_method_combo = QComboBox()
        self._index_method_combo.currentIndexChanged.connect(self._on_index_method_changed)
        r, self._index_method_label, _ = _param_row('Method:', self._index_method_combo)
        cpl.addLayout(r)

        self._history_combo = QComboBox()
        self._history_combo.currentIndexChanged.connect(self._on_history_changed)
        r, self._history_label, _ = _param_row('Version:', self._history_combo)
        cpl.addLayout(r)

        self._apply_index_btn = QPushButton('Apply Selected Params')
        self._apply_index_btn.clicked.connect(self._apply_selected_index_params)
        theme_widgets.button(self._apply_index_btn, "panel", "padding:3px;")
        cpl.addWidget(self._apply_index_btn)

        self._resolved_param_edit = QtWidgets.QLineEdit()
        self._resolved_param_edit.setReadOnly(True)
        self._resolved_param_edit.setPlaceholderText('Resolved parameter JSON path')
        self._resolved_param_edit.setStyleSheet('font-size:10px;color:#aaa;')
        r, self._resolved_param_label, _ = _param_row('Resolved path:', self._resolved_param_edit)
        cpl.addLayout(r)

        self._method_combo = _CompactCombo()
        self._method_combo.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        for method in available_segmentation_methods():
            cfg = get_segmentation_method_config(method)
            self._method_combo.addItem(cfg["display_name"], method)
        self._method_combo.setCurrentIndex(
            max(0, self._method_combo.findData(CELLPOSE_WHOLECELL_FUSION))
        )
        self._method_combo.currentIndexChanged.connect(self._on_method_changed)
        r, self._method_label, _ = _param_row('Method:', self._method_combo)
        cpl.addLayout(r)

        # Cellpose 4.0.1+: model_type is ignored — only cpsam is used
        self._cp_model_lbl = _ElidedLabel('cpsam  (Cellpose 4.0.1+: only model, model_type ignored)')
        self._cp_model_lbl.setStyleSheet(
            'color:#fa8;font-size:11px;padding:2px 4px;'
            'background:#221;border-radius:3px;'
        )
        r, self._cp_model_label, _ = _param_row('Model:', self._cp_model_lbl)
        cpl.addLayout(r)

        self._cp_diam = QDoubleSpinBox()
        self._cp_diam.setRange(0, 300)
        self._cp_diam.setValue(30)
        self._cp_diam.setSpecialValueText('auto')
        r, self._cp_diam_label, _ = _param_row('diameter (0=auto):', self._cp_diam)
        cpl.addLayout(r)

        self._cp_flow = QDoubleSpinBox()
        self._cp_flow.setRange(0.0, 3.0)
        self._cp_flow.setSingleStep(0.05)
        self._cp_flow.setValue(0.4)
        r, self._cp_flow_label, _ = _param_row('flow_threshold:', self._cp_flow)
        cpl.addLayout(r)

        self._cp_prob = QDoubleSpinBox()
        self._cp_prob.setRange(-6.0, 6.0)
        self._cp_prob.setSingleStep(0.1)
        self._cp_prob.setValue(0.0)
        r, self._cp_prob_label, _ = _param_row('cellprob_threshold:', self._cp_prob)
        cpl.addLayout(r)

        self._cp_minsize = QtWidgets.QSpinBox()
        self._cp_minsize.setRange(1, 10000)
        self._cp_minsize.setValue(15)
        r, self._cp_minsize_label, _ = _param_row('min_size (px²):', self._cp_minsize)
        cpl.addLayout(r)

        # Cellpose whole-cell + nuclei (docs/v17_cellpose_cell_nucleus_plan.md):
        # the nucleus pass's own values, shown for that method only.
        self._nuc_diam = QDoubleSpinBox()
        self._nuc_diam.setRange(0, 300)
        self._nuc_diam.setValue(0)
        self._nuc_diam.setSpecialValueText('auto')
        r, self._nuc_diam_label, _ = _param_row('nucleus diameter (0=auto):', self._nuc_diam)
        cpl.addLayout(r)
        self._nuc_flow = QDoubleSpinBox()
        self._nuc_flow.setRange(0.0, 3.0)
        self._nuc_flow.setSingleStep(0.05)
        self._nuc_flow.setValue(0.4)
        r, self._nuc_flow_label, _ = _param_row('nucleus flow_threshold:', self._nuc_flow)
        cpl.addLayout(r)
        self._nuc_prob = QDoubleSpinBox()
        self._nuc_prob.setRange(-6.0, 6.0)
        self._nuc_prob.setSingleStep(0.1)
        self._nuc_prob.setValue(0.0)
        r, self._nuc_prob_label, _ = _param_row('nucleus cellprob_threshold:', self._nuc_prob)
        cpl.addLayout(r)
        self._nuc_minsize = QtWidgets.QSpinBox()
        self._nuc_minsize.setRange(1, 10000)
        self._nuc_minsize.setValue(15)
        r, self._nuc_minsize_label, _ = _param_row('nucleus min_size (px²):', self._nuc_minsize)
        cpl.addLayout(r)
        # plan §15 (v1.4): both cell + nucleus methods -- a cell holding two
        # or more similar nuclei becomes one cell per nucleus (on by default)
        self._split_cells = QCheckBox('Split a cell holding two similar nuclei')
        self._split_cells.setChecked(True)
        self._split_cells.setToolTip(
            'A nucleus at least half the size of the largest in its cell and at least '
            '40 px splits the cell (each pixel goes to its nearest such nucleus). Turn off '
            'for tissue with real binucleate cells.')
        r, self._split_cells_label, _ = _param_row('split cells:', self._split_cells)
        cpl.addLayout(r)

        # Every method, manual or from the index (block M): unchecked, the
        # engine runs on the CPU (a broken GPU setup, or the user's choice).
        self._cp_gpu = QCheckBox('Use GPU if available')
        self._cp_gpu.setChecked(True)
        r, self._cp_gpu_label, _ = _param_row('GPU:', self._cp_gpu)
        cpl.addLayout(r)

        # The engine loads this model only (block M): shown, not editable.
        self._sd_model = QtWidgets.QLineEdit(preseg_contract.STARDIST_MODEL)
        self._sd_model.setReadOnly(True)
        self._sd_model.setStyleSheet('font-size:11px;')
        r, self._sd_model_label, _ = _param_row('StarDist model:', self._sd_model)
        cpl.addLayout(r)

        self._sd_prob = QDoubleSpinBox()
        self._sd_prob.setRange(-1.0, 1.0)
        self._sd_prob.setSingleStep(0.05)
        self._sd_prob.setValue(-1.0)
        self._sd_prob.setSpecialValueText('auto')
        r, self._sd_prob_label, _ = _param_row('prob_thresh:', self._sd_prob)
        cpl.addLayout(r)

        self._sd_nms = QDoubleSpinBox()
        self._sd_nms.setRange(-1.0, 1.0)
        self._sd_nms.setSingleStep(0.05)
        self._sd_nms.setValue(-1.0)
        self._sd_nms.setSpecialValueText('auto')
        r, self._sd_nms_label, _ = _param_row('nms_thresh:', self._sd_nms)
        cpl.addLayout(r)

        self._sd_expand = QDoubleSpinBox()
        self._sd_expand.setRange(0, 200)
        self._sd_expand.setSingleStep(1)
        self._sd_expand.setValue(8)
        r, self._sd_expand_label, _ = _param_row('expansion_distance:', self._sd_expand)
        cpl.addLayout(r)

        self._mesmer_widgets = []
        def _mesmer_row(label, widget):
            r, l, w = _param_row(label, widget)
            cpl.addLayout(r)
            self._mesmer_widgets.extend([l, w])
            return w

        self._mesmer_section = QLabel('Mesmer parameters')
        self._mesmer_section.setStyleSheet('color:#56b6c2;font-size:11px;font-weight:bold;padding-top:4px;')
        cpl.addWidget(self._mesmer_section)
        self._mesmer_widgets.append(self._mesmer_section)
        # The parameters Step1's table has for Mesmer (block M); the input and
        # the preprocessing are fixed by the contract (plan 7.11).
        self._mesmer_maxima = _mesmer_row('maxima_threshold:', QDoubleSpinBox())
        self._mesmer_maxima.setDecimals(3)
        self._mesmer_maxima.setRange(0, 1)
        self._mesmer_maxima.setSingleStep(0.005)
        self._mesmer_maxima.setValue(0.075)
        self._mesmer_interior = _mesmer_row('interior_threshold:', QDoubleSpinBox())
        self._mesmer_interior.setDecimals(3)
        self._mesmer_interior.setRange(0, 1)
        self._mesmer_interior.setSingleStep(0.01)
        self._mesmer_interior.setValue(0.2)
        self._mesmer_mpp = _mesmer_row('image_mpp:', QDoubleSpinBox())
        # 3 decimals like Step1's method editor (0.325 at 20x), so a chosen
        # value is not rounded on its way through this box.
        self._mesmer_mpp.setDecimals(3)
        self._mesmer_mpp.setRange(0.01, 10)
        self._mesmer_mpp.setSingleStep(0.05)
        self._mesmer_mpp.setValue(0.5)
        self._mesmer_min_size = _mesmer_row('postprocess_min_size:', QtWidgets.QSpinBox())
        self._mesmer_min_size.setRange(0, 100000)
        self._mesmer_min_size.setValue(0)
        self._method_hint = QLabel('')
        self._method_hint.setStyleSheet(f'color:{_TK["text3"]};font-size:10px;')
        self._method_hint.setWordWrap(True)
        cpl.addWidget(self._method_hint)

        rl.addWidget(cp_box)
        self._update_param_source_ui()
        self._on_method_changed()

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
            'Each run writes: segmentation_results/<timestamp>_<method>/\n'
            'Global: global_mask.zarr | global_mask.ome.tiff | global_dapi.ome.tiff\n'
            'Per-tile: tile_masks/tile_r*_c*_dapi.ome.tiff | tile_r*_c*_raw_mask.ome.tiff'
        )
        out_info.setStyleSheet(f'color:{_TK["text3"]};font-size:10px;')
        out_info.setWordWrap(True)      # block L2: fits the shared column
        outl.addWidget(out_info)
        rl.addWidget(out_box)

        # Recovery mode (collapsed by default)
        # Block L2 (user ruling 2026-09-25): a short title; the long one is its tooltip.
        rec_box = QGroupBox('Recovery from .npy')
        rec_box.setToolTip('Recovery Mode (merge from saved .npy files)')
        rec_box.setStyleSheet(self._box_style('#e06c75'))
        rec_box.setCheckable(True)
        rec_box.setChecked(False)
        recl = QVBoxLayout(rec_box)

        rec_info = QLabel(
            'Use this if a previous run saved tile masks to disk.\n'
            'Select the directory containing tile_r_c.npy files.'
        )
        rec_info.setStyleSheet(f'color:{_TK["text3"]};font-size:10px;')
        rec_info.setWordWrap(True)      # block L2: fits the shared column
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
        # Block L2 (user ruling 2026-09-25): the controls take the LEFT
        # column, as wide as Step0's and Step1's channel column (one share,
        # kept by the main window), and the tile overview the rest. A
        # horizontal scroll bar appears only when the column is dragged
        # narrower than the controls.
        self._params_scroll = right_scroll

        # ── Bottom navigation: the frame's bottom slot ───────────────
        nav = QHBoxLayout()

        # (no "Back to Step 1": the step bar is the way back -- user ruling
        # 2026-11-03)
        nav.addStretch()
        # REMOVE: redundant navigation button (use top-right Next only)

        self._btn_run = QPushButton('▶  Run Segmentation && Merge')
        theme_widgets.button(self._btn_run, "run",
                             "padding:7px 22px;font-size:13px;font-weight:bold;")
        self._btn_run.clicked.connect(self._run)
        nav.addWidget(self._btn_run)

        self._btn_stop = QPushButton('⏹ Stop')
        self._btn_stop.setEnabled(False)
        theme_widgets.button(self._btn_stop, "danger", "padding:7px 14px;")
        self._btn_stop.clicked.connect(self._stop)
        nav.addWidget(self._btn_stop)

        self._assemble_frame(root, nav)

    def _assemble_frame(self, root, nav):
        """Put the page into its `StepFrame` (block A1b S3).

            title slot   `Step 2 — Segmentation & Merge`
            left tab     `Parameters`: the parameter panel
            right tab    `Tile Status`: tool row = `Tile Status Overview`;
                         the overview and the progress below it
            bottom slot  stretch | Run | Stop

        Every widget is the one built above, moved into its slot.
        """
        title_bar = self._title_bar_in
        if title_bar is None:
            title_bar = QLabel('Step 2 — Segmentation & Merge')
            title_bar.setStyleSheet('font-size:12px;font-weight:bold;color:#eee;'
                                    'padding:4px 8px;')
        metrics = self._frame_metrics_in
        if metrics is None:
            title_bar.ensurePolished()
            metrics = StepFrameMetrics(
                title_height=title_bar.sizeHint().height(),
                bottom_height=max(nav.sizeHint().height(), 38),
                tab_qss=FRAME_TAB_QSS)
        frame = StepFrame(metrics, free_tab_bar)
        self._frame = frame
        frame.set_title(title_bar)
        frame.left_tabs.addTab(self._params_scroll, 'Parameters')
        frame.right_tabs.addTab(self._overview_panel, 'Tile Status')
        row = QHBoxLayout()
        row.addWidget(self._tile_status_label)
        row.addStretch()
        row.addWidget(self.btn_tma_cores)
        row.addWidget(self.btn_exclusions)
        frame.tool_row(self._overview_panel.layout(), row)
        frame.bottom_layout.addLayout(nav)
        self._main_split = frame.splitter
        root.addWidget(frame)

    def left_panel(self):
        """The parameter column -- held at the shared channel-column floor."""
        return getattr(self, "_params_scroll", None)

    # ── utilities ─────────────────────────────────────────────────────

    def channel_column_splitter(self):
        """The handle that shares Step0's and Step1's channel-column width."""
        return getattr(self, "_main_split", None)

    def channels_host(self):
        """Where the ONE public channel panel goes in this page."""
        return getattr(self, "_channels_host", None)

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
        # margin-top + positioned ::title so the title sits in the border gap and
        # is never occluded by the first body widget (styled-QGroupBox title bug).
        return theme_widgets.panel_box_qss(color)

    # ── zarr loading ──────────────────────────────────────────────────

    def set_zarr_path(self, path):
        """Called from MainWindow when Step 1 finishes generating fused.zarr."""
        if path and os.path.exists(path):
            self._zarr_edit.setText(path)
            self._sync_output_dir_from_zarr_path(path)
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

    # ── block RM: the Input drop-down ─────────────────────────────────

    def set_fuse_runs(self, items, selected=""):
        """`items` = [{"run": dir, "label", "tag", "tooltip", "zarr", "regions"}],
        newest first; the one whose run is `selected` is chosen and fills
        fused.zarr (and the region paths)."""
        from .step3_mask_bar import TAG_ROLE, CLOSE_ROLE
        self._fuse_items = list(items or [])
        combo = self._fuse_combo
        combo.blockSignals(True)
        combo.clear()
        if not self._fuse_items:
            combo.addItem('No Step1 result for this Step0 result', '')
            combo.setEnabled(False)
        else:
            for item in self._fuse_items:
                combo.addItem(item["label"], item["run"])
                row = combo.count() - 1
                if item.get("tag"):
                    combo.setItemData(row, item["tag"], TAG_ROLE)
                combo.setItemData(row, item.get("tooltip") or item["label"], Qt.ToolTipRole)
                combo.setItemData(row, True, CLOSE_ROLE)
            combo.setEnabled(not self._run_active)
            index = combo.findData(selected) if selected else 0
            combo.setCurrentIndex(max(0, index))
        from .step3_mask_bar import fit_popup
        fit_popup(combo)
        combo.blockSignals(False)
        self._use_fuse_item(combo.currentIndex())

    def _use_fuse_item(self, index):
        if not (0 <= index < len(self._fuse_items)):
            if self._zarr_edit.text().strip():
                self._zarr_edit.setText('')
            self._zarr_path = None
            self._forget_overview()
            self.set_region_inputs({})
            return
        item = self._fuse_items[index]
        # §7: the regions this fuse run froze, not the ones edited now (none:
        # the whole image)
        self.set_rois(item.get("rois") or [])
        # this input's regions FIRST: loading it sizes the grid (Auto, the
        # StarDist cap) over every region of the run (codex 2026-10-25)
        self.set_region_inputs(item.get("regions") or {})
        if os.path.abspath(self._zarr_edit.text().strip() or '.') != os.path.abspath(item["zarr"]):
            self._zarr_edit.setText(item["zarr"])
            self._load_zarr_info()

    def selected_fuse_run(self):
        return str(self._fuse_combo.currentData() or '')

    def _on_fuse_activated(self, index):
        self._use_fuse_item(index)
        run = self._fuse_combo.itemData(index) or ''
        if run:
            self.fuse_chosen.emit(run)

    def _on_fuse_close(self, row):
        run = self._fuse_combo.itemData(row) or ''
        if run:
            self._fuse_combo.hidePopup()
            self.fuse_delete_requested.emit(run)

    # ── block RM §6: Step2's draft (method, parameters, their file) ──

    def draft(self):
        """What Step2 holds now, for session.json, or None."""
        try:
            cfg = self.get_seg_config()
        except Exception:                                   # noqa: BLE001
            return None
        # without the hand-over's frozen Regions (§11/§12.2: not used, and it
        # made every session write ~75 KB larger)
        cfg = self._without_handover_regions(json.loads(json.dumps(cfg, default=str)))
        return {"segmentation_config": cfg,
                "param_file": self._seg_param_file or "",
                "parameter_source": self._parameter_source}

    def apply_draft(self, draft):
        """Put a saved Step2 draft back on screen."""
        try:
            cfg = normalize_segmentation_config((draft or {}).get("segmentation_config") or {})
            if cfg:
                self._check_method_offered(cfg.get("method"))
        except ValueError as exc:
            # Refused whole: nothing of the draft is applied (plan §1, §6b).
            print(f"[Step2] Step2 draft not restored: {exc}")
            QMessageBox.warning(self, "Step2", f"The saved Step2 settings were not restored.\n\n{exc}")
            return False
        if not cfg:
            return False
        cfg = self._without_handover_regions(dict(cfg))   # an older draft may still carry them
        # A draft that ran a saved parameter file (source index) comes back as
        # that file, chosen again (user 2026-10-25); it falls back to the
        # manual copy below when the file or its index is gone.
        pfile = str((draft or {}).get("param_file") or "")
        if (draft or {}).get("parameter_source") == "index" and pfile and os.path.isfile(pfile) \
                and self._restore_index_file(pfile, cfg.get("method")):
            if self._same_effective_settings(cfg, self.get_seg_config()):
                print(f"[Step2] Step2 draft restored as its parameter file ({os.path.basename(pfile)})")
                return True
            # edited after the file was chosen: the edits win, as typed (codex 2026-10-25)
            print("[Step2] the Step2 draft differs from its parameter file: restored as edited")
        # The draft IS the configuration that was on screen: it runs as typed
        # (source manual), so no index file is reloaded over it at Run.
        self._set_param_source("manual")
        # (acceptance 2026-10-04: without this guard the method change at the
        # end of the apply put every parameter back to the method's default)
        old = self._loading_index_selection
        self._loading_index_selection = True
        try:
            self._apply_seg_config_to_ui(cfg)
        finally:
            self._loading_index_selection = old
        self._seg_config = cfg
        self._seg_param_file = ""
        self._resolved_param_edit.setText(str((draft or {}).get("param_file") or ""))
        print(f"[Step2] unsaved Step2 draft restored (method={cfg.get('method')})")
        return True

    @staticmethod
    def _same_effective_settings(a, b):
        """Do two Step2 configurations run the same: method and parameters
        (normalised), whatever their bookkeeping fields?"""
        try:
            na, nb = normalize_segmentation_config(dict(a or {})), normalize_segmentation_config(dict(b or {}))
        except ValueError:                       # an unknown method is never "the same"
            return False
        return na.get("method") == nb.get("method") and \
            dict(na.get("params") or {}) == dict(nb.get("params") or {})

    def clear_params_index(self):
        """Forget a parameter index (e.g. another workspace's): manual source."""
        self._seg_params_index = {}
        self._seg_params_dir = ""
        self._seg_params_edit.setText("")
        self._seg_param_file = ""
        self._applied_param_file = ""
        self._loading_index_selection = True
        try:
            self._index_method_combo.clear()
            self._history_combo.clear()
        finally:
            self._loading_index_selection = False
        self._resolved_param_edit.setText("")
        self._set_param_source("manual")

    def _restore_index_file(self, path, method):
        """Choose saved parameter file `path` (method `method`) through its
        folder's index, as a user would; False when it cannot be."""
        if not method or not self._load_seg_params_index(os.path.dirname(path), silent=True,
                                                         apply_active=False):
            return False
        idx = self._index_method_combo.findData(method)
        if idx < 0:
            return False
        old = self._loading_index_selection
        self._loading_index_selection = True
        try:
            self._index_method_combo.setCurrentIndex(idx)
        finally:
            self._loading_index_selection = old
        if not self._apply_index_method(method, os.path.basename(path), silent=True):
            return False
        self._set_param_source("index")
        self._applied_param_file = self._seg_param_file
        self._update_resolved_param_path()
        return os.path.abspath(self._seg_param_file or "") == os.path.abspath(path)

    def _browse_zarr(self):
        path = QFileDialog.getExistingDirectory(self, 'Select fused.zarr directory')
        run = os.path.dirname(os.path.abspath(path)) if path else ""
        index = self._fuse_combo.findData(run) if run else -1
        if index >= 0 and self._fuse_items:
            # a listed Step1 result: chosen the same way as in Input
            self._fuse_combo.setCurrentIndex(index)
            self._on_fuse_activated(index)
            return
        if path:
            self._zarr_edit.setText(path)
            self._sync_output_dir_from_zarr_path(path)
            self._load_zarr_info()

    def _browse_seg_params(self):
        dlg = QFileDialog(self, 'Select segmentation_params directory or index JSON', OUTPUT_DIR)
        dlg.setOption(QFileDialog.DontUseNativeDialog, True)
        dlg.setFileMode(QFileDialog.AnyFile)
        dlg.setNameFilter('Segmentation params index (segmentation_params_index.json);;JSON (*.json);;All files (*)')
        if dlg.exec():
            selected = dlg.selectedFiles()
            if selected:
                self.set_segmentation_params_path(selected[0], load=True)

    def set_segmentation_params_path(self, path, load=False):
        display_path = os.path.abspath(path) if path else ""
        self._seg_params_edit.setText(display_path)
        if not load:
            self._seg_param_file = display_path
            return
        # (a failed load has already said why: one dialog, not two)
        if display_path and self._load_seg_params_index(display_path, silent=False, apply_active=False):
            self._set_param_source("index")

    def _resolve_seg_params_index_path(self, path):
        path = os.path.abspath(path or "")
        if not path:
            return ""
        if os.path.isfile(path):
            return path if os.path.basename(path) == PARAM_INDEX else ""
        if os.path.isdir(path):
            direct = os.path.join(path, PARAM_INDEX)
            if os.path.exists(direct):
                return direct
            nested = params_index_path(path)
            if os.path.exists(nested):
                return nested
        return ""

    def _param_path_from_index(self, rel_or_abs):
        rel_or_abs = rel_or_abs or ""
        if not rel_or_abs:
            return ""
        return os.path.abspath(rel_or_abs) if os.path.isabs(rel_or_abs) else os.path.abspath(os.path.join(self._seg_params_dir, rel_or_abs))

    def _method_display_name(self, method):
        try:
            return get_segmentation_method_config(method).get("display_name") or method
        except Exception:
            return method

    def _load_seg_params_index(self, path, silent=True, apply_active=False):
        index_path = self._resolve_seg_params_index_path(path)
        if not index_path:
            if not silent:
                QMessageBox.warning(self, 'Error', 'Select segmentation_params/ or segmentation_params_index.json.')
            return False
        try:
            with open(index_path, 'r', encoding='utf-8') as f:
                index = json.load(f)
            if not isinstance(index, dict):
                raise ValueError('segmentation_params_index.json is not a JSON object.')
            methods = index.get("methods") or {}
            if not methods:
                raise ValueError('segmentation_params_index.json has no methods.')
            self._seg_params_index = index
            self._seg_params_dir = os.path.dirname(index_path)
            self._seg_params_edit.setText(index_path)
            active_method = index.get("active_method") or next(iter(methods))
            if active_method not in methods:
                active_method = next(iter(methods))
            active_file = index.get("active_param_file") if active_method == index.get("active_method") else ""
            active_file = active_file or (methods.get(active_method) or {}).get("latest") or ""

            self._loading_index_selection = True
            try:
                self._index_method_combo.clear()
                for method in methods.keys():
                    self._index_method_combo.addItem(self._method_display_name(method), method)
                idx = self._index_method_combo.findData(active_method)
                self._index_method_combo.setCurrentIndex(idx if idx >= 0 else 0)
                self._populate_history_for_method(active_method, active_file)
                self._update_resolved_param_path()
            finally:
                self._loading_index_selection = False

            ok = True
            if apply_active:
                ok = self._apply_index_method(active_method, active_file, silent=silent)
            if ok:
                print("[Step2] loaded segmentation params index")
                print(f"[Step2] index={index_path}")
            return ok
        except Exception as e:
            if not silent:
                QMessageBox.warning(self, 'Error', str(e))
            else:
                print(f'[Step2] failed to load segmentation params index: {e}')
            return False

    def _populate_history_for_method(self, method, selected_file=""):
        info = (self._seg_params_index.get("methods") or {}).get(method) or {}
        latest = info.get("latest") or ""
        history = []
        if latest:
            history.append(latest)
        for item in list(info.get("history") or []):
            if item and item not in history:
                history.append(item)
        selected_file = selected_file or latest
        self._history_combo.blockSignals(True)
        try:
            self._history_combo.clear()
            for filename in history:
                label = f"latest: {filename}" if filename == latest else filename
                self._history_combo.addItem(label, filename)
            idx = self._history_combo.findData(selected_file)
            self._history_combo.setCurrentIndex(idx if idx >= 0 else max(0, self._history_combo.findData(latest)))
            has_history = self._history_combo.count() > 0
            self._history_combo.setVisible(has_history)
            self._history_label.setVisible(has_history)
        finally:
            self._history_combo.blockSignals(False)

    def _load_index_method(self, method, filename="", silent=True):
        return self._apply_index_method(method, filename, silent=silent)

    def _apply_index_method(self, method, filename="", silent=True):
        info = (self._seg_params_index.get("methods") or {}).get(method) or {}
        filename = filename or info.get("latest") or ""
        path = self._param_path_from_index(filename)
        if not path or not os.path.exists(path):
            if not silent:
                QMessageBox.warning(self, 'Error', f'Segmentation params file not found:\n{path}')
            return False
        self._populate_history_for_method(method, filename)
        old = self._loading_index_selection
        self._loading_index_selection = True
        try:
            return self._load_seg_params_file(path, silent=silent)
        finally:
            self._loading_index_selection = old

    def _load_seg_params_file(self, path, silent=True):
        if not path or not os.path.exists(path):
            if not silent:
                QMessageBox.warning(self, 'Error', 'Segmentation params file not found.')
            return False
        try:
            with open(path, 'r', encoding='utf-8') as f:
                # Block RM (§16.3-6): its paths are project-relative on disk.
                from ..utils import run_store
                cfg = normalize_segmentation_config(run_store.from_records(
                    json.load(f), run_store.project_dir_of(path)))
            # A new-format file is checked by its version, never guessed at.
            preseg_contract.validate(cfg)
            self._apply_seg_config_to_ui(cfg)
            self._seg_config = cfg
            self._seg_param_file = os.path.abspath(path)
            self.refresh_core_outlines()                  # its hand-over's cores, if any
            self._resolved_param_edit.setText(self._seg_param_file)
            print("[Step2] loaded active segmentation params")
            print(f"[Step2] method={cfg.get('method')}")
            print(f"[Step2] param_file={path}")
            return True
        except Exception as e:
            if not silent:
                QMessageBox.warning(self, 'Error', str(e))
            else:
                print(f'[Step2] failed to load segmentation params: {e}')
            return False

    def _on_history_changed(self):
        if self._loading_index_selection:
            return
        self._update_resolved_param_path()

    def _on_index_method_changed(self):
        if self._loading_index_selection:
            return
        method = self._index_method_combo.currentData()
        if method:
            self._populate_history_for_method(method)
            self._update_resolved_param_path()

    def _set_param_source(self, source):
        idx = self._param_source_combo.findData(source)
        if idx >= 0:
            self._param_source_combo.setCurrentIndex(idx)
        self._parameter_source = source
        self._update_param_source_ui()

    def _on_param_source_changed(self):
        source = self._param_source_combo.currentData() or "manual"
        self._parameter_source = source
        if source == "manual":
            self._applied_param_file = ""
            self._seg_param_file = ""
            self._resolved_param_edit.setText("")
        elif source == "index" and not self._seg_params_index:
            path = self._seg_params_edit.text().strip()
            if path:
                self._load_seg_params_index(path, silent=True, apply_active=False)
        self._update_param_source_ui()

    def _update_param_source_ui(self):
        is_index = (self._param_source_combo.currentData() or "manual") == "index"
        if hasattr(self, "_core_outlines"):
            QtCore.QTimer.singleShot(0, self.refresh_core_outlines)   # the source's cores
        for w in (
            self._index_method_label, self._index_method_combo,
            self._history_label, self._history_combo,
            self._apply_index_btn,
            self._resolved_param_label, self._resolved_param_edit,
        ):
            w.setVisible(is_index)
        # One `Method:` row on screen (block L2, user ruling 2026-09-25): from
        # the index, the index's method (above) is the one shown; Step2's own
        # method box is kept, hidden -- it still holds the applied method.
        for w in (self._method_label, self._method_combo):
            w.setVisible(not is_index)

    def _update_resolved_param_path(self):
        method = self._index_method_combo.currentData()
        filename = self._history_combo.currentData()
        path = self._param_path_from_index(filename) if method and filename else ""
        self._resolved_param_edit.setText(path)
        return path

    def _apply_selected_index_params(self):
        if not self._seg_params_index:
            path = self._seg_params_edit.text().strip()
            if not self._load_seg_params_index(path, silent=False, apply_active=False):
                return False
        method = self._index_method_combo.currentData()
        filename = self._history_combo.currentData()
        if not method or not filename:
            QMessageBox.warning(self, 'Error', 'No indexed parameter version selected.')
            return False
        ok = self._apply_index_method(method, filename, silent=False)
        if ok:
            self._set_param_source("index")
            self._applied_param_file = self._seg_param_file
            self._resolved_param_edit.setText(self._seg_param_file)
        return ok

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
            self._sync_output_dir_from_zarr_path(path)
            self._zarr_info.setText(
                f'Shape: {self._full_h:,} × {self._full_w:,} px  '
                f'| dtype: {z.dtype}  '
                f'| ch0={attrs.get("channel_0","?")}  '
                f'ch1={attrs.get("channel_1","?")}'
            )
            self._update_tile_info()
            if self._auto_tile_strategy.isChecked():
                self._refresh_tile_suggestion(apply=True)   # Auto follows the new input's size
            self._keep_stardist_tiles_safe()             # a new input: StarDist's tiles must fit
            self._load_overview_from_zarr(z)
        except Exception as e:
            self._zarr_info.setText(f'⚠ Failed to open zarr: {e}')

    def _load_overview_from_zarr(self, z):
        """The nucleus overview under the tile grid (block PA-5b fix 2b).

        Its geometry follows from the store's shape and is set at once, so
        the grid is drawn now. Its pixels are the overview Generate sampled
        and saved beside the store (`core.nucleus_overview`), or -- for a
        store saved before that -- the old strided read of the store; either
        way loaded and contrast-stretched OFF the GUI thread (the strided
        read froze Step2's entry for 4.7 s on the A5 synthetic slide). A
        result for a store no longer shown is dropped."""
        from ..core import nucleus_overview as nov
        ds = nov.stride(z.shape)
        self._ov_h, self._ov_w = nov.overview_shape(z.shape)
        self._ov_ds = ds
        self._ov_img.clear()
        self._ov_vb.setRange(QRectF(0, 0, self._ov_w, self._ov_h), padding=0.01)
        from ..core.tile_tissue import TissueUnavailable, fused_region
        try:                                     # the input's place on the slide
            bbox = fused_region(self._zarr_path)[0]
            self._ov_origin = (int(bbox[0]), int(bbox[2]))
        except (TissueUnavailable, OSError, ValueError, KeyError):
            self._ov_origin = (0, 0)
        self._draw_tile_grid()
        if self._zarr_path != getattr(self, "_run_core_input", None):
            self._run_core_snapshot = None       # another input: not the run's any more
        self.refresh_core_outlines()
        if callable(self.input_shown):
            self.input_shown()                   # the zoomable picture follows (or steps aside)
        self._ov_status.setText('Loading overview…')
        path, shape = self._zarr_path, tuple(z.shape)
        token = self._ov_token = object()

        def _work():
            try:
                arr = nov.load(path, shape)
                if arr is None:
                    arr = nov.read_from_store(z)
                    nov.remember(path, arr)       # this session only; old runs stay as saved
                result = (token, nov.normalised(arr), None)
            except Exception as exc:              # noqa: BLE001 -- shown, not raised
                result = (token, None, exc)
            self._overview_ready.emit(result)

        import threading
        threading.Thread(target=_work, name="step2-overview", daemon=True).start()

    def _forget_overview(self):
        """No store any more: a pending overview is dropped and the old
        picture goes (codex PA-5b) -- the zoomable one too."""
        self._ov_token = None
        self._ov_img.clear()
        self._ov_status.setText('')
        if callable(self.input_shown):
            self.input_shown()                   # no input: the zoomable picture closes

    def _on_overview_ready(self, result):
        token, arr, exc = result
        if token is not getattr(self, "_ov_token", None):
            return                                 # another store is shown now
        if exc is not None or arr is None:
            self._ov_status.setText(f'Overview failed: {exc}')
            print(f'[Step2 overview error] {exc}')
            return
        # pyqtgraph requires levels=[min,max] when dtype is float
        self._ov_img.setImage(arr, autoLevels=False, levels=[0.0, 1.0])
        self._ov_status.setText(
            f'Overview {self._ov_h}×{self._ov_w} px (1/{self._ov_ds})'
        )

    # ── tile grid overlay ─────────────────────────────────────────────

    def _update_tile_info(self):
        if self._full_h == 0:
            return
        self._refresh_tile_suggestion(apply=False)
        nr  = self._rows_spin.value()
        nc  = self._cols_spin.value()
        th  = -(-self._full_h // nr)
        tw  = -(-self._full_w // nc)
        # No VRAM estimate (block L2, user ruling 2026-09-25: it was not
        # accurate).
        self._tile_ram_lbl.setText(
            f'{nr}×{nc} = {nr*nc} tiles  |  '
            f'tile size: {th:,}×{tw:,} px'
        )
        if self._skip_empty_cb.isChecked() and not self._run_active:
            self._refresh_skip_plan()
        self._draw_tile_grid()

    #: StarDist runs a whole tile through its network and NMS at once (32 ray
    #: distances per pixel); a 19640 x 7600 tile (~150 MP read) crashed the
    #: engine with std::bad_alloc on 2026-10-25 while ~65 MP tiles ran. Safe
    #: cap on one tile's READ area (own + overlap), below 2^31 / 32 elements.
    STARDIST_MAX_TILE_PX = 60_000_000

    def _is_stardist(self):
        method = self._method_combo.currentData() if hasattr(self, "_method_combo") else None
        # the hybrid's StarDist part reads the same tiles (hybrid plan §2)
        return method in (STARDIST_NUCLEI_DAPI, STARDIST_NUCLEI_EXPANSION,
                          HYBRID_CELLPOSE_STARDIST)

    def _run_input_shapes(self):
        """(h, w) of every input the run will segment with this ONE grid: each
        region's fused product (multi-ROI), else the shown input."""
        shapes = []
        for path in (getattr(self, "_region_fused_paths", None) or {}).values():
            try:
                shapes.append(tuple(int(v) for v in zarr.open(path, mode="r").shape[:2]))
            except Exception:                               # noqa: BLE001 -- the worker reports it
                continue
        return shapes or [(int(self._full_h), int(self._full_w))]

    def _largest_tile_px(self, n_rows, n_cols):
        """The largest read window (own + overlap) of that grid over every
        input of the run, in pixels."""
        from ..utils.tile_scheduler import TileScheduler
        best = 0
        for h, w in self._run_input_shapes():
            for t in TileScheduler(h, w, int(n_rows), int(n_cols), int(self._overlap_spin.value())).tiles:
                best = max(best, (t.read_bbox[1] - t.read_bbox[0]) * (t.read_bbox[3] - t.read_bbox[2]))
        return best

    def _stardist_safe_grid(self, n_rows, n_cols):
        """(rows, cols): the given grid, split further (the longer tile side of
        the largest input first) until no tile's read window exceeds
        STARDIST_MAX_TILE_PX."""
        nr, nc = max(1, int(n_rows)), max(1, int(n_cols))
        h, w = max(self._run_input_shapes(), key=lambda s: s[0] * s[1])
        while self._largest_tile_px(nr, nc) > self.STARDIST_MAX_TILE_PX:
            if h / nr >= w / nc:
                nr += 1
            else:
                nc += 1
        return nr, nc

    def _set_grid(self, n_rows, n_cols):
        """Put the grid on the boxes (their maximum raised when it is needed)."""
        self._rows_spin.blockSignals(True)
        self._cols_spin.blockSignals(True)
        try:
            self._rows_spin.setMaximum(max(self._rows_spin.maximum(), int(n_rows)))
            self._cols_spin.setMaximum(max(self._cols_spin.maximum(), int(n_cols)))
            self._rows_spin.setValue(int(n_rows))
            self._cols_spin.setValue(int(n_cols))
        finally:
            self._rows_spin.blockSignals(False)
            self._cols_spin.blockSignals(False)
        self._update_tile_info()

    def _stardist_grid_ok_for_run(self):
        """The last grid check before the run's configuration is taken (so the
        skip plan follows a changed grid) and before anything is created:
        StarDist's tiles must fit (2026-10-25). Asks; False = do not run."""
        if not self._is_stardist() or self.__dict__.get("_cores_preview"):
            return True                              # a cores run splits a too-large core itself
        # (no Auto re-application here: it would reset the skip choices even
        # when nothing changes; the grid on screen is the one checked)
        nr0, nc0 = self._rows_spin.value(), self._cols_spin.value()
        safe = self._stardist_safe_grid(nr0, nc0)
        if safe != (nr0, nc0):
            big = self._largest_tile_px(nr0, nc0) / 1e6
            answer = QMessageBox.question(
                self, "Tile grid",
                f"The tile grid {nr0}×{nc0} makes tiles of up to {big:.0f} MP. StarDist runs a whole "
                f"tile at once and crashes on tiles this big.\n\nUse {safe[0]}×{safe[1]} instead "
                f"(tiles ≤ {self.STARDIST_MAX_TILE_PX // 1_000_000} MP)?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if answer != QMessageBox.Yes:
                return False
            self._set_grid(*safe)
        applied = (self._rows_spin.value(), self._cols_spin.value())
        if self._largest_tile_px(*applied) > self.STARDIST_MAX_TILE_PX:   # never run it anyway
            QMessageBox.warning(self, "Tile grid",
                                f"The tile grid {applied[0]}×{applied[1]} still makes tiles too big for "
                                "StarDist; the run is not started.")
            return False
        return True

    def _keep_stardist_tiles_safe(self):
        """With StarDist, a grid whose tiles are too big is split further at
        once (user ruling 2026-10-25); the suggestion line says so."""
        if not self._is_stardist() or self._full_h <= 0 or self._full_w <= 0 \
                or self.__dict__.get("_cores_preview"):
            return False
        nr, nc = self._rows_spin.value(), self._cols_spin.value()
        safe = self._stardist_safe_grid(nr, nc)
        if safe == (nr, nc):
            return False
        self._set_grid(*safe)
        self._tile_suggestion_lbl.setText(
            self._tile_suggestion_lbl.text() + f"  ·  StarDist: grid {nr}×{nc} → {safe[0]}×{safe[1]} "
            f"(tiles ≤ {self.STARDIST_MAX_TILE_PX // 1_000_000} MP)")
        print(f"[Step2] StarDist tile grid {nr}x{nc} -> {safe[0]}x{safe[1]} (tile read area cap "
              f"{self.STARDIST_MAX_TILE_PX:,} px)")
        return True

    def _on_auto_tile_strategy_changed(self):
        self._refresh_tile_suggestion(apply=self._auto_tile_strategy.isChecked())

    def _refresh_tile_suggestion(self, apply=False):
        if self._full_h <= 0 or self._full_w <= 0:
            return
        if self.__dict__.get("_cores_preview"):
            return                                   # no grid to suggest; Auto never moves the overlap
        method = self._method_combo.currentData() if hasattr(self, "_method_combo") else CELLPOSE_WHOLECELL_FUSION
        cfg = self.get_seg_config() if hasattr(self, "_method_combo") else {}
        channel_count = 2
        if method in (MESMER_WHOLE_CELL, MESMER_NUCLEI, MESMER_NUCLEAR_GUIDED):
            channel_count = max(2, len(parse_channel_list(cfg.get("membrane_channels") or [])) + 1)
        suggestion = suggest_tile_strategy(
            self._full_h,
            self._full_w,
            method,
            vram_gb=self._detect_vram_gb(),
            channel_count=channel_count,
        )
        self._suggested_tile_strategy = suggestion
        self._tile_suggestion_lbl.setText(
            f"Suggested: {suggestion['n_rows']}×{suggestion['n_cols']} grid "
            f"(~{suggestion['estimated_tile_mpx']:.0f} MP/tile)"
        )
        if apply:
            self._rows_spin.blockSignals(True)
            self._cols_spin.blockSignals(True)
            self._overlap_spin.blockSignals(True)
            try:
                self._rows_spin.setValue(int(suggestion["n_rows"]))
                self._cols_spin.setValue(int(suggestion["n_cols"]))
                self._overlap_spin.setValue(int(suggestion["overlap"]))
            finally:
                self._rows_spin.blockSignals(False)
                self._cols_spin.blockSignals(False)
                self._overlap_spin.blockSignals(False)
            self._update_tile_info()

    _vram_gb_cache = []          # (block PA-5b) asked once per process

    @staticmethod
    def _detect_vram_gb():
        """The first GPU's memory in GiB, or None. Block PA-5b: `import torch`
        on the GUI thread just to ask this froze the first Step2 entry for
        7.4 s (measured), so torch is used only when something already loaded
        it; otherwise `nvidia-smi` answers (MiB, the same number to within the
        driver's rounding -- the tile rule compares it with 16 and 40 GB)."""
        cache = Step2Page._vram_gb_cache
        if cache:
            return cache[0]
        value = None
        try:
            import sys
            torch = sys.modules.get("torch")
            if torch is not None and torch.cuda.is_available():
                value = torch.cuda.get_device_properties(0).total_memory / (1024.0 ** 3)
        except Exception:
            value = None
        if value is None:
            # CUDA's device 0 is the first of CUDA_VISIBLE_DEVICES (an index
            # or a UUID, both of which `nvidia-smi -i` takes); none visible ->
            # no GPU (codex PA-5)
            import os
            import subprocess
            visible = os.environ.get("CUDA_VISIBLE_DEVICES")
            first = "0" if visible is None else visible.split(",")[0].strip()
            if first and first != "-1":
                try:
                    out = subprocess.run(
                        ["nvidia-smi", "-i", first, "--query-gpu=memory.total",
                         "--format=csv,noheader,nounits"],
                        capture_output=True, text=True, timeout=3).stdout
                    value = float(out.strip().splitlines()[0]) / 1024.0
                except (OSError, subprocess.SubprocessError, ValueError, IndexError):
                    value = None
        cache.append(value)
        return value

    @staticmethod
    def _without_handover_regions(seg_config):
        """Drop the cores a Step1 hand-over froze (user ruling 2026-10-19:
        the parameters are decoupled from Regions); the rest of the
        hand-over is kept as it is."""
        block = seg_config.get(preseg_contract.CONTRACT_KEY)
        if isinstance(block, dict) and "regions" in block:
            seg_config[preseg_contract.CONTRACT_KEY] = {k: v for k, v in block.items() if k != "regions"}
        return seg_config

    def _core_source_items(self):
        """The cores this page shows, in the Regions colours (green: runs,
        yellow: does not): while a run is on, the snapshot it froze; else
        the last Regions Save (as Step1 shows it) -- what a run started now
        follows, whatever the parameters' source. (A finished run's snapshot
        no longer hides a newer Save: 15A acceptance, 2026-11-12.)"""
        from ..core.regions import display as RD
        if self._run_core_snapshot is not None and self.__dict__.get("_run_active"):
            return RD.snapshot_items(self._run_core_snapshot)
        return list(self.saved_core_items() or []) if callable(self.saved_core_items) else []

    def _exclusion_items(self):
        """The exclusions of the same source as the cores (backlog 21): the
        running run's frozen snapshot, else the last Regions Save."""
        from ..core.regions import display as RD
        if self._run_core_snapshot is not None and self.__dict__.get("_run_active"):
            return RD.exclusion_items(self._run_core_snapshot)
        return list(self.saved_exclusion_items() or []) if callable(self.saved_exclusion_items) else []

    #: A core being segmented: the running tile's colour, a translucent fill.
    CORE_RUNNING_FILL = '#59ffc832'
    #: A core done: its green outline over a light green fill (backlog 28).
    CORE_DONE_FILL = '#4043a047'

    def _core_items(self):
        """The cores in the one scheme of Step 0.5 to 3 (backlog 28,
        docs/v17_core_colours_plan.md): green analysed (waiting), yellow
        not; during a run the core being segmented amber with a
        translucent fill, a core whose units are all done green over a
        light green fill, a failed one red (15B)."""
        from ..core.regions import display as RD
        states = self._core_states()
        out = []
        for it in self._core_source_items():
            uid = str(it.get("id", "")).split("#")[0]
            if it.get("color") != RD.GREEN:
                it = dict(it, color=RD.YELLOW, width=RD.CORE_WIDTH)
            elif states.get(uid) == 'running':
                it = dict(it, color=self.TILE_COLOURS['running'], width=4, fill=self.CORE_RUNNING_FILL)
            elif states.get(uid) == 'done':
                it = dict(it, color=RD.GREEN, width=RD.CORE_WIDTH, fill=self.CORE_DONE_FILL)
            elif states.get(uid) == 'failed':                # 15B: red, labelled on its first piece
                it = dict(it, color=self.TILE_COLOURS['error'], width=RD.CORE_WIDTH)
                if str(it.get("id", "")).endswith("#0"):
                    it["label"] = " · ".join(t for t in (it.get("label"), "failed") if t)
            else:
                it = dict(it, color=RD.GREEN, width=RD.CORE_WIDTH)
            out.append(it)
        return out

    def _core_states(self):
        """{core uid: 'running' | 'done' | 'failed'} of the run on screen (a
        cores run): 'running' while one of its units is being segmented,
        'done' once all its units are, 'failed' when the run ended with an
        error in one of them (15B) -- done and failed kept after the run, as
        done tiles are, until the next run. From the units the worker made
        and the run's progress."""
        rp = self._run_progress
        if rp is None or not self._run_view_live():
            return {}
        if rp.get("unit_cores") is None:                    # copied once the worker has made them
            worker = self.__dict__.get("_worker")
            if worker is None or worker is rp.get("prev_worker"):
                return {}
            meta = getattr(worker, "_units_meta", None) or {}
            if not meta:
                return {}
            rp["unit_cores"] = [u["core_uid"] for u in list(meta.values())[-1]]
        cores = rp["unit_cores"]
        done = rp.get("done_units", set())
        states = {}
        for uid in set(cores):
            mine = [i for i, c in enumerate(cores) if c == uid]
            if all(i in done for i in mine):
                states[uid] = 'done'
            elif any(rp["state"].get(i) == 'failed' for i in mine):
                states[uid] = 'failed'                      # 15B: the run ended with an error there
        if self.__dict__.get("_run_active"):
            for i, s in rp["state"].items():
                if s == 'running' and i not in done and 0 <= i < len(cores):
                    states[cores[i]] = 'running'
        return states

    def _mark_failed_cores(self, msg):
        """A cores run ended with an error: the units being segmented then
        are failed (15B). A Stop is not a failure: they go back to grey."""
        rp = self._run_progress
        if rp is None or str(msg or "").strip() == "Stopped by user.":
            return
        self._core_states()                                 # the unit -> core list, if not yet copied
        if rp.get("unit_cores") is None:
            return                                          # a grid run, or no unit made yet
        done = rp.get("done_units", set())
        for i, st in list(rp["state"].items()):
            if st == 'running' and i not in done:
                rp["state"][i] = 'failed'

    def _cores_finish_text(self):
        """A cores run's finish dialog: (the lines replacing the per-tile
        outputs, the per-core list for "Show Details…"), or None for a grid
        run (15B §2.3)."""
        rp = self._run_progress
        self._core_states()
        cores = (rp or {}).get("unit_cores")
        if not cores:
            return None
        order = list(dict.fromkeys(cores))
        names = {str(it.get("id", "")).split("#")[0]: it.get("label")
                 for it in self._core_source_items() if str(it.get("id", "")).endswith("#0")}
        states = self._core_states()
        n = len(order)
        lines = (f'Cores: {n} run ({len(cores)} unit{"s" if len(cores) != 1 else ""})\n\n'
                 f'Per-unit outputs:\n'
                 f'  tile_masks/\n'
                 f'    tile_r<k>_c0_dapi.ome.tiff / _raw_mask.ome.tiff  (unit k;\n'
                 f'    segmentation_meta.json "units" names its core)')
        details = "\n".join(f'{names.get(u) or f"core {k + 1} of {n}"}: {states.get(u, "not done")}'
                             for k, u in enumerate(order))
        return lines, details

    def _refresh_core_looks(self):
        """Redraw the cores' colours (both views) without re-reading the input."""
        if not self.__dict__.get("_cores_preview"):
            return
        for co in (self._core_outlines, getattr(self.dapi_viewer, "cores", None)):
            if co is not None and getattr(co, "overlay", None) is not None:
                try:
                    co.refresh()
                except Exception as exc:                    # noqa: BLE001 -- a look only
                    print(f"[TMA cores] step2: not redrawn ({type(exc).__name__}: {exc})")

    def _ov_map(self, x, y):
        """Slide pixels to the overview's: minus the shown input's origin, over its stride."""
        oy, ox = getattr(self, "_ov_origin", (0, 0))
        ds = float(getattr(self, "_ov_ds", 1) or 1)
        return (x - ox) / ds, (y - oy) / ds

    def refresh_core_outlines(self):
        """Draw the TMA cores, and under them the exclusions (backlog 21),
        over the overview now (the input's origin is read from the input
        itself); each switch shows only while there is something to draw."""
        n = m = 0
        if getattr(self, "_zarr_path", None) and hasattr(self, "_ov_h"):
            from ..core.tile_tissue import TissueUnavailable, fused_region
            try:
                bbox = fused_region(self._zarr_path)[0]
                self._ov_origin = (int(bbox[0]), int(bbox[2]))
                try:
                    m = self._exclusion_outlines.refresh()  # first: the cores are drawn above
                except Exception as exc:                    # noqa: BLE001 -- a look only
                    print(f"[Exclusions] step2: not drawn ({type(exc).__name__}: {exc})")
                    self._exclusion_outlines.detach()
                n = self._core_outlines.refresh()
            except (TissueUnavailable, OSError, ValueError, KeyError) as exc:
                print(f"[TMA cores] step2: not drawn ({exc})")
                self._core_outlines.detach()
                self._exclusion_outlines.detach()
            except Exception as exc:                        # noqa: BLE001
                print(f"[TMA cores] step2: not drawn ({type(exc).__name__}: {exc})")
                self._core_outlines.detach()
        else:
            self._core_outlines.detach()
            self._exclusion_outlines.detach()
        self._apply_cores_layout()                          # before the viewer draws (no grid for cores)
        if self.dapi_viewer is not None:
            n = max(n, self.dapi_viewer.refresh())         # the cores over the zoomable picture too
            m = max(m, int(getattr(self.dapi_viewer, "exclusions_count", 0) or 0))
        self.btn_tma_cores.setVisible(n > 0)
        self.btn_exclusions.setVisible(m > 0)
        return n

    def cores_run_count(self):
        """How many cores a run started now follows: the green (run) cores of
        the run on screen's frozen snapshot, else of the last Regions Save.
        0: no cores, a grid run."""
        from ..core.regions import display as RD
        try:
            items = self._core_source_items()
        except Exception:                                   # noqa: BLE001 -- a preview only
            items = []
        return len({str(it.get("id", "")).split("#")[0] for it in items if it.get("color") == RD.GREEN})

    def _apply_cores_layout(self, count=None):
        """A run that follows cores runs core by core: Rows / Cols / Auto and
        the tile skipping step aside, no grid is drawn; the overlap stays (it
        is the overlap between the parts of a core too large for one call).
        Without cores the box is as before. The frozen snapshot decides at
        Run; this is its preview (plan §3.0, §3.6)."""
        n = int(self.cores_run_count()) if count is None else int(count)
        was = bool(self._cores_preview)
        self._cores_preview = n
        cores = n > 0
        running = bool(self.__dict__.get("_run_active"))
        for w in (self._rows_spin, self._cols_spin, self._auto_tile_strategy, self._skip_btn_row):
            w.setEnabled(not cores)
        # the skip box is also locked while a run is on (it is the run's plan)
        self._skip_empty_cb.setEnabled(not cores and not running)
        if cores:
            self._tile_suggestion_lbl.setText(
                f"Runs core by core ({n} core{'s' if n != 1 else ''}); no tile grid. The overlap is used "
                "only between the parts of a core too large for one engine call.")
        if cores != was:
            if not cores:
                self._refresh_tile_suggestion()
            self._draw_tile_grid()

    def _draw_tile_grid(self):
        """Overlay tile rectangles on the overview (none for a run that
        follows cores: it has no grid)."""
        if not hasattr(self, '_ov_h') or self._full_h == 0:
            return
        if self.__dict__.get("_cores_preview"):
            self._box = None
            self._box_preview = {}
            self._pending = {}
            for item in list(self._tile_rects.values()):
                self._ov_vb.removeItem(item)
            self._tile_rects.clear()
            self._tile_status.clear()
            self._tile_colour_state.clear()
            return
        nr = self._rows_spin.value()
        nc = self._cols_spin.value()

        # Remove old rects; a box being dragged and the tiles collected end
        # (they named these tiles)
        self._box = None
        self._box_preview = {}
        self._pending = {}
        if self.__dict__.get('_shift_timer') is not None:
            self._shift_timer.stop()
        for item in list(self._tile_rects.values()):
            self._ov_vb.removeItem(item)
        self._tile_rects.clear()
        self._tile_status.clear()
        self._tile_colour_state.clear()

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
                rect = _TileRect(
                    [ox0 / ds, oy0 / ds],
                    [(ox1 - ox0) / ds, (oy1 - oy0) / ds],
                    pen=pg.mkPen('#808080', width=1),
                    movable=False, resizable=False,
                )
                rect.setAcceptedMouseButtons(Qt.LeftButton)
                rect.sigClicked.connect(lambda _roi, _ev=None, r=r, c=c:
                                        self._on_tile_clicked(r, c))
                self._ov_vb.addItem(rect)
                rect.base_z = rect.zValue()                # the layers stack on it (plan §2)
                self._tile_rects[(r, c)] = rect
                self._tile_status[(r, c)] = 'idle'
        if self._run_active or self._skip_plan is None:
            self._apply_run_progress()
        else:
            self._apply_skip_plan()
        if self.dapi_viewer is not None:
            self.dapi_viewer.refresh()                     # the new grid over the zoomable picture

    def _shows_run_grid(self):
        """True when the drawn grid is the running (or last) run's grid. Its
        progress is drawn only then: on another grid a tile index names
        another part of the image."""
        rp = self._run_progress
        return (rp is not None and bool(self._tile_rects)
                and (self._rows_spin.value(), self._cols_spin.value()) == rp["grid"]
                and self._run_view_live())

    def _run_view_live(self):
        """The page shows the context the run was started for (block A6;
        real-machine finding 2026-10-03). A run's states are RECORDED
        whatever the page shows; they are DRAWN only while this holds."""
        rp = self._run_progress
        token = (rp or {}).get("token")
        return token is None or self._run_identity() == token

    def _clear_run_view(self):
        """No run on this page any more: grey tiles, empty bar and count."""
        self._run_progress = None
        self._total_cells = 0
        for key in list(self._tile_status):
            self._tile_status[key] = 'idle'
            self._set_tile_colour(key[0], key[1], 'idle')
        self._prog_bar.setValue(0)
        self._prog_lbl.setText('—')
        self._cells_lbl.setText('Total cells detected: 0')
        self._old_run_lbl.setVisible(False)

    def _refresh_run_view(self):
        """The page's context moved: draw the run's record if it is the
        run's context again, else say an older run is computing."""
        rp = self._run_progress
        if rp is None or rp.get("token") is None:
            return
        token = rp["token"]
        if (not self._run_active
                and (self._roi_id, self._roi_dir) != (token.get("roi_id", ""),
                                                      token.get("roi_dir", ""))):
            # User ruling 2026-10-03: Step2 is now on ANOTHER workspace and the
            # recorded run was the previous one's -- its tiles, bar and cell
            # count go; the overview picture stays.
            self._clear_run_view()
            return
        if self._run_view_live():
            self._old_run_lbl.setVisible(False)
            self._apply_run_progress()
            pct, msg = rp.get("progress") or (None, None)
            if pct is not None:
                self._prog_bar.setValue(pct)
                self._prog_lbl.setText(msg)
            self._cells_lbl.setText(f'Total cells detected: {self._total_cells:,}')
        elif self._run_active:
            for key in list(self._tile_status):
                self._tile_status[key] = 'idle'
                self._set_tile_colour(key[0], key[1], 'idle')
            self._say_old_run(rp["token"], self._worker, "is still computing")

    def _apply_run_progress(self):
        """Draw the run's recorded progress onto a freshly built grid."""
        if not self._shows_run_grid():
            return
        nc = self._run_progress["grid"][1]
        for idx, state in self._run_progress["state"].items():
            r, c = divmod(int(idx), nc)
            if (r, c) in self._tile_rects:
                self._set_tile_colour(r, c, state)
                self._tile_status[(r, c)] = state

    # ── block S2T: the skip plan ──────────────────────────────────────

    def _plan_skips(self):
        """The tiles the next run leaves out: proposed (no tissue) minus kept,
        plus those the user forced (they hold tissue)."""
        plan = self._skip_plan
        if plan is None:
            return set()
        return (set(plan["auto"]) - plan["user_kept"]) | set(plan["user_forced"])

    def _plan_matches_drawn_grid(self):
        plan = self._skip_plan
        return (plan is not None
                and plan["grid"] == [self._rows_spin.value(), self._cols_spin.value()]
                and plan["overlap"] == self._overlap_spin.value())

    def _apply_skip_plan(self):
        if not self._plan_matches_drawn_grid():
            return
        nc = self._skip_plan["grid"][1]
        for idx in self._plan_skips():
            r, c = divmod(int(idx), nc)
            state = 'plan_forced' if idx in self._skip_plan["user_forced"] else 'plan_skip'
            if (r, c) in self._tile_rects:
                self._set_tile_colour(r, c, state)
                self._tile_status[(r, c)] = state

    def _skip_hint(self):
        plan = self._skip_plan
        if plan is None:
            return
        n = len(plan["fractions"])
        forced = len(plan["user_forced"])
        text = (f'{len(self._plan_skips())} of {n} tiles skipped '
                f'({len(plan["auto"])} have no tissue'
                + (f'; {forced} with tissue skipped by you' if forced else '')
                + ') — click a tile to change it; hold Shift to select several')
        if plan.get("reset_note"):
            text = plan["reset_note"] + ' ' + text
        self._skip_hint_lbl.setText(text)
        self._skip_hint_lbl.setVisible(True)
        self._sync_skip_buttons()

    def _sync_skip_buttons(self):
        """The two buttons: shown with the count line, locked while a run computes."""
        self._skip_btn_row.setVisible(self._skip_plan is not None
                                      and self._skip_empty_cb.isChecked())
        self._skip_all_empty_btn.setEnabled(not self._run_active)
        self._skip_none_btn.setEnabled(not self._run_active)

    def _edit_plan(self, user_kept):
        """A button: the user's own changes replaced in one go, no question."""
        plan = self._skip_plan
        if plan is None or self._run_active or not self._plan_matches_drawn_grid():
            return
        self._box_cancel()
        plan["user_kept"] = set(user_kept)
        plan["user_forced"] = {}
        plan.pop("reset_note", None)
        self._draw_tile_grid()
        self._skip_hint()

    def _skip_all_empty(self):
        """Back to the automatic proposal (every tile without tissue skipped)."""
        if self._skip_plan is not None:
            self._edit_plan(())

    def _skip_none(self):
        """Every tile is segmented."""
        if self._skip_plan is not None:
            self._edit_plan(self._skip_plan["auto"])

    def _on_skip_toggled(self, *_):
        self._refresh_skip_plan(reset_note=False)
        self._draw_tile_grid()

    def _refresh_skip_plan(self, reset_note=True):
        """(Re)compute the proposal for the drawn grid. A new grid or overlap
        drops the user's own choices -- they named tiles of another grid."""
        self._box_cancel()
        if not self._skip_empty_cb.isChecked() or not self._zarr_path:
            self._skip_plan = None
            self._skip_hint_lbl.setVisible(False)
            self._sync_skip_buttons()
            return
        from ..core import tile_tissue
        had_choices = bool(self._skip_plan and self._plan_skips())
        try:
            bbox, slide, channel = tile_tissue.fused_region(self._zarr_path)
            key = (slide, channel)
            if key not in self._tissue_cache:
                self._tissue_cache[key] = tile_tissue.SlideTissue(slide, channel)
            plan = tile_tissue.tile_plan(
                self._tissue_cache[key], (self._full_h, self._full_w), (bbox[0], bbox[2]),
                self._rows_spin.value(), self._cols_spin.value(), self._overlap_spin.value())
        except Exception as exc:
            self._skip_plan = None
            self._skip_hint_lbl.setText(f'Skip unavailable: {exc}')
            self._skip_hint_lbl.setVisible(True)
            self._sync_skip_buttons()
            return
        # plan §13: ticking skips nothing -- every proposed tile starts kept;
        # "Skip all empty" applies the proposal
        plan["user_kept"] = set(plan["auto"])
        plan["user_forced"] = {}
        if reset_note and had_choices:
            plan["reset_note"] = 'The grid changed: your tile choices were reset.'
        self._skip_plan = plan
        self._skip_hint()

    def _on_tile_clicked(self, r, c):
        """Toggle one tile of the plan at once. Skipping a tile that holds
        tissue is asked first (the user's own decision, recorded as such)."""
        plan = self._skip_plan
        if plan is None or self._run_active or not self._plan_matches_drawn_grid():
            return
        idx = int(r) * plan["grid"][1] + int(c)
        if idx >= len(plan["fractions"]):
            return
        if self._pending:
            self._commit_pending()
        self._toggle_tiles([idx])

    def _toggle_tiles(self, tiles):
        """Toggle these tiles of the plan; one question for all of those that
        would be skipped although they hold tissue."""
        plan = self._skip_plan
        skips = self._plan_skips()
        tissue = {}
        for idx in tiles:
            if idx in skips:                             # back to segmenting: no question
                plan["user_forced"].pop(idx, None)
                if idx in plan["auto"]:
                    plan["user_kept"].add(idx)
            elif float(plan["fractions"][idx]) == 0.0:
                plan["user_kept"].discard(idx)
            else:
                tissue[idx] = float(plan["fractions"][idx])
        if len(tissue) == 1:
            (idx, fraction), = tissue.items()
            if self._confirm_forced_skip(idx, fraction):
                plan["user_forced"][idx] = fraction
        elif tissue and self._confirm_forced_skips(tissue):
            plan["user_forced"].update(tissue)
        plan.pop("reset_note", None)
        self._draw_tile_grid()
        self._skip_hint()

    def _set_tiles(self, tiles, skip):
        """Set these tiles skipped (`skip`) or segmented; the tiles already
        so are left alone. One question for the tissue tiles to skip."""
        skips = self._plan_skips()
        todo = [i for i in tiles if (i in skips) != bool(skip)]
        if todo:
            self._toggle_tiles(todo)

    def _shift_held(self):
        """Shift is down NOW: the system's live state, not the last event's
        (Shift let go in another window must still be seen). A seam:
        offscreen tests press Shift by replacing it."""
        return bool(QtWidgets.QApplication.queryKeyboardModifiers() & Qt.ShiftModifier)

    # ── the Shift+drag box (plan §3.2): one commit per gesture ──────────

    def _box_begin(self, r, c, shift=None):
        """A press on tile (r, c): a box starts when Shift is held and the
        plan can be edited. Its direction is the start tile's (user
        decision A): segmented -> skip the boxed tiles, skipped -> restore.
        `shift`: whether the press itself carried Shift (what the system sent
        with it, the reliable answer); None asks the keyboard now."""
        plan = self._skip_plan
        held = self._shift_held() if shift is None else bool(shift)
        if (plan is None or self._run_active or not self._skip_empty_cb.isChecked()
                or not self._plan_matches_drawn_grid() or not held):
            return False
        idx = int(r) * plan["grid"][1] + int(c)
        if idx >= len(plan["fractions"]):
            return False
        self._box = {"start": (int(r), int(c)), "end": (int(r), int(c)),
                     "skip": not self._skipped_after(idx)}
        self._show_box()
        return True

    def _box_tiles(self, box):
        """The plan's tile indices in the rows x columns the box spans."""
        plan = self._skip_plan
        nc = plan["grid"][1]
        (r0, c0), (r1, c1) = box["start"], box["end"]
        return [r * nc + c for r in range(min(r0, r1), max(r0, r1) + 1)
                for c in range(min(c0, c1), max(c0, c1) + 1)
                if r * nc + c < len(plan["fractions"])]

    def _box_move(self, r, c):
        """The pointer is over tile (r, c) (clamped): redrawn only when the
        boxed range changes."""
        box = self._box
        if box is None or (int(r), int(c)) == box["end"]:
            return
        box["end"] = (int(r), int(c))
        self._show_box()

    def _skipped_after(self, idx):
        """Whether tile `idx` would be skipped: its collected state, else the plan's."""
        if idx in self._pending:
            return self._pending[idx]
        return idx in self._plan_skips()

    def _collect(self, idx, skip):
        """Tile `idx` to end up skipped (`skip`) -- collected, or dropped from
        the collection when that is what the plan already says."""
        if (idx in self._plan_skips()) == bool(skip):
            self._pending.pop(idx, None)
        else:
            self._pending[idx] = bool(skip)

    def _box_click(self):
        """Shift + click without moving: the start tile alone is collected
        toggled (a second click takes it back out). Nothing is asked yet."""
        box, self._box = self._box, None
        if box is None:
            return
        r, c = box["start"]
        idx = r * self._skip_plan["grid"][1] + c
        self._collect(idx, not self._skipped_after(idx))
        self._after_collect()

    def _box_finish(self):
        """The release: the boxed tiles are collected in the box's direction."""
        box, self._box = self._box, None
        if box is None:
            return
        for idx in self._box_tiles(box):
            self._collect(idx, box["skip"])
        self._after_collect()

    def _after_collect(self):
        self._show_box()
        if self._pending:
            self._shift_timer.start()
        else:
            self._shift_timer.stop()             # the last tile taken back out
        if not self._shift_held():
            self._commit_pending()

    def _shift_poll(self):
        """Shift let go (and no box held): what was collected is committed."""
        if self._box is None and not self._shift_held():
            self._commit_pending()

    def _commit_pending(self):
        """Everything collected at once, ONE question for the tissue tiles."""
        self._shift_timer.stop()
        pending, self._pending = self._pending, {}
        self._show_box()
        if not pending or self._skip_plan is None:
            return
        self._set_tiles([i for i, skip in pending.items() if not skip], False)
        self._set_tiles([i for i, skip in pending.items() if skip], True)

    def _box_cancel(self):
        """A box being dragged, and the tiles collected, end with no change
        (grid, option, run, button)."""
        self._shift_timer.stop()
        if self._box is None and not self._box_preview and not self._pending:
            return
        self._box = None
        self._pending = {}
        self._show_box()

    def _show_box(self):
        """Draw the collected tiles and the boxed ones as they would end up,
        framed in white."""
        old, new = self._box_preview, {}
        box, plan = self._box, self._skip_plan
        if plan is not None:
            nc = plan["grid"][1]
            skips = self._plan_skips()
            wanted = dict(self._pending)
            if box is not None:
                wanted.update({idx: box["skip"] for idx in self._box_tiles(box)})
            for idx, skip in wanted.items():
                rc = divmod(idx, nc)
                if not skip:
                    new[rc] = 'idle'
                elif idx in skips:
                    new[rc] = self._tile_colour_state.get(rc, 'plan_skip')
                else:
                    new[rc] = 'plan_forced' if float(plan["fractions"][idx]) > 0.0 else 'plan_skip'
        self._box_preview = new
        for rc in set(old) | set(new):
            self._style_tile(*rc)
        if self.dapi_viewer is not None:
            self.dapi_viewer.refresh()

    def _overview_tile(self, pos, clamp=False):
        """(row, col) of the drawn tile under a point of the overview's viewport."""
        if not self._tile_rects or not self._full_h:
            return None
        q = self._ov_vb.mapSceneToView(self._ov_gv.mapToScene(pos))
        ds = self._ov_ds
        return self._tile_index_at(q.y() * ds, q.x() * ds, clamp)

    def eventFilter(self, obj, event):
        """The overview's Shift+drag box (plan §3.2): a Shift + left press on
        a tile that starts a box is consumed with its moves and release (the
        tile's RectROI never sees them); any other press goes on as before."""
        gv = self.__dict__.get('_ov_gv')
        if gv is not None and obj is gv.viewport():
            t = event.type()
            if (t in (QtCore.QEvent.MouseButtonPress, QtCore.QEvent.MouseButtonDblClick)
                    and event.button() == Qt.LeftButton and self._ov_press is None):
                hit = self._overview_tile(event.pos())
                shift = bool(event.modifiers() & Qt.ShiftModifier)     # as sent with the press
                if hit is not None and self._box_begin(*hit, shift=shift):
                    self._ov_press = QtCore.QPoint(event.pos())
                    return True
            elif t == QtCore.QEvent.MouseMove and self._ov_press is not None:
                hit = self._overview_tile(event.pos(), clamp=True)
                if hit is not None:
                    self._box_move(*hit)
                return True
            elif (t == QtCore.QEvent.MouseButtonRelease and event.button() == Qt.LeftButton
                  and self._ov_press is not None):
                from .regions.overlay import CLICK_SLOP_PX
                start, self._ov_press = self._ov_press, None
                if self._box is not None:
                    hit = self._overview_tile(event.pos(), clamp=True)
                    if hit is not None:
                        self._box_move(*hit)
                    if (event.pos() - start).manhattanLength() > CLICK_SLOP_PX:
                        self._box_finish()
                    else:
                        self._box_click()
                return True
        return super().eventFilter(obj, event)

    def _confirm_forced_skips(self, tissue):
        """One question for several tiles with tissue (a seam like
        `_confirm_forced_skip`)."""
        nc = self._skip_plan["grid"][1]
        names = [f'row {i // nc + 1}, column {i % nc + 1} ({f:.1%})'
                 for i, f in sorted(tissue.items())]
        shown = '\n'.join(names[:12]) + (f'\n... and {len(names) - 12} more' if len(names) > 12 else '')
        answer = QMessageBox.question(
            self, 'Skip tiles with tissue',
            f'{len(tissue)} of the chosen tiles hold tissue (share of each window on '
            f'the slide):\n\n{shown}\n\nA skipped tile is not segmented: it will have '
            f'no cells. Skip them anyway?',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return answer == QMessageBox.Yes

    def _confirm_forced_skip(self, idx, fraction):
        """The dialog (a seam: offscreen tests answer it)."""
        r, c = divmod(idx, self._skip_plan["grid"][1])
        answer = QMessageBox.question(
            self, 'Skip a tile with tissue',
            f'Tile row {r + 1}, column {c + 1} holds tissue ({fraction:.1%} of its '
            f'window on the slide).\n\nA skipped tile is not segmented: it will have '
            f'no cells. Skip it anyway?',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return answer == QMessageBox.Yes

    def skip_tiles_config(self):
        """What the worker gets (`seg_config["skip_tiles"]`), or None."""
        plan = self._skip_plan
        if plan is None or not self._skip_empty_cb.isChecked():
            return None
        return {"grid": list(plan["grid"]), "overlap": int(plan["overlap"]),
                "auto": sorted(plan["auto"]), "user_kept": sorted(plan["user_kept"]),
                "user_forced": [{"tile": int(i), "tissue_fraction": float(f)}
                                for i, f in sorted(plan["user_forced"].items())],
                "fractions": [float(f) for f in plan["fractions"]], "mask": plan["mask"]}

    def _on_tile_skipped(self, tile_idx, n_tiles):
        forced = (self._run_progress or {}).get("forced", ())
        self._record_tile(tile_idx, 'skipped_forced' if int(tile_idx) in forced else 'skipped')

    def _begin_run_progress(self, nr, nc, skipped=()):
        """A new run: a new progress record; every drawn tile back to idle,
        except the tiles this run skips (`skipped`): recorded skipped at
        once, in the same turn, so their purple never goes away on screen
        (backlog 20, the user's acceptance 2026-11-13). The tissue tiles the
        user chose to skip are kept with it, so they stay hatched while the
        run computes."""
        self._run_active = True
        self._skip_empty_cb.setEnabled(False)
        self._sync_skip_buttons()
        plan = self._skip_plan
        forced = ({int(i) for i in plan["user_forced"]}
                  if plan is not None and self._skip_empty_cb.isChecked() else set())
        self._run_progress = {"grid": (int(nr), int(nc)), "state": {}, "forced": forced}
        # its cores come from this run's worker, never the one before (15A)
        self._run_progress["prev_worker"] = self.__dict__.get("_worker")
        for key in self._tile_status:
            self._tile_status[key] = 'idle'
            self._set_tile_colour(key[0], key[1], 'idle')
        for i in sorted(int(k) for k in skipped):
            self._record_tile(i, 'skipped_forced' if i in forced else 'skipped')
        self._run_grid_lbl.setText(f'Running grid: {nr}×{nc} — its progress shows on '
                                   'this grid only')
        self._run_grid_lbl.setVisible(True)

    def _record_tile(self, tile_idx, state):
        """Record one tile's state for the run, and draw it only if the
        drawn grid is the run's grid."""
        if self._run_progress is None:
            return
        self._run_progress["state"][int(tile_idx)] = state
        if state == 'done':                    # a core's highlight ends here, whatever comes after
            self._run_progress.setdefault("done_units", set()).add(int(tile_idx))
        self._refresh_core_looks()
        if self._shows_run_grid():
            r, c = divmod(int(tile_idx), self._run_progress["grid"][1])
            self._set_tile_colour(r, c, state)
            self._tile_status[(r, c)] = state

    SKIP_PURPLE = '#c678dd'
    TILE_COLOURS = {
        'idle':    '#808080',
        'running': '#ffc832',
        'done':    '#3cc850',
        'error':   '#dc3c3c',
        'skipped': SKIP_PURPLE,        # block S2T: left out by the plan
        'skipped_forced': SKIP_PURPLE,  # ... a tile with tissue the user chose
        'plan_skip': SKIP_PURPLE,      # the next run will leave it out
        'plan_forced': SKIP_PURPLE,    # ... although it holds tissue (the user's choice)
    }
    #: The skip look (plan §2): translucent purple fill, 4 px border and the
    #: purple hatching -- every skipped tile, whether "Skip all empty" chose
    #: it or the user did (the user's ruling 2026-11-13, backlog 20; it used
    #: to be the user's tissue tiles only).
    SKIP_STATES = ('plan_skip', 'plan_forced', 'skipped', 'skipped_forced')
    HATCHED_STATES = SKIP_STATES
    SKIP_FILL = '#4dc678dd'            # SKIP_PURPLE at alpha 0.30 (#AARRGGBB)
    SKIP_WIDTH = 4.0
    PREVIEW_COLOUR = '#ffffff'         # the frame of a tile under a Shift+drag box
    PREVIEW_WIDTH = 2.0

    def _tile_look(self, row, col):
        """(colour, width or None, fill or None, hatched, layer, framed) of a
        drawn tile. Layers (plan §2): 0 idle, 1 skipped, 2 running / done /
        error, 3 under the box. A tile under the box looks like its result
        (purple, hatched or not) with a white frame over it. Width None =
        the view's own default."""
        preview = self._box_preview.get((row, col))
        state = preview if preview is not None else self._tile_colour_state.get((row, col), 'idle')
        if state in self.SKIP_STATES:
            look = [self.SKIP_PURPLE, self.SKIP_WIDTH, self.SKIP_FILL,
                    state in self.HATCHED_STATES, 1]
        else:
            look = [self.TILE_COLOURS.get(state, '#808080'), None, None, False,
                    0 if state == 'idle' else 2]
        if preview is not None:
            look[4] = 3
        return tuple(look) + (preview is not None,)

    def _style_tile(self, row, col):
        """Apply a tile's look to its overview RectROI."""
        rect = self._tile_rects.get((row, col))
        if rect is None:
            return
        colour, width, fill, hatched, layer, framed = self._tile_look(row, col)
        rect.fill = QtGui.QColor(fill) if fill else None
        rect.hatch = QtGui.QColor(colour) if hatched else None
        rect.frame = pg.mkPen(self.PREVIEW_COLOUR, width=self.PREVIEW_WIDTH) if framed else None
        rect.setPen(pg.mkPen(colour, width=width or 2))
        rect.setZValue(getattr(rect, 'base_z', 0.0) + layer)
        rect.update()

    def _set_tile_colour(self, row, col, state):
        rect = self._tile_rects.get((row, col))
        if rect is None:
            return
        self._tile_colour_state[(row, col)] = state
        self._style_tile(row, col)
        if self.dapi_viewer is not None:
            self.dapi_viewer.refresh()                     # the grid over the zoomable picture

    # ── the grid in slide pixels (the zoomable DAPI viewer, plan part 3) ──
    def overview_layout(self):
        return self._ov_layout

    def overview_widget(self):
        return self._ov_gv

    def _tile_geometry(self):
        """(rows, cols, tile h, tile w, origin y, origin x) of the drawn grid."""
        nr, nc = self._rows_spin.value(), self._cols_spin.value()
        oy, ox = getattr(self, "_ov_origin", (0, 0))
        return nr, nc, -(-self._full_h // nr), -(-self._full_w // nc), oy, ox

    def tile_items(self):
        """The drawn grid as overlay items in slide pixels, in its colours
        (none for a run that follows cores)."""
        if not self._tile_rects or not self._full_h or self.__dict__.get("_cores_preview"):
            return []
        nr, nc, th, tw, oy, ox = self._tile_geometry()
        items = []
        for (r, c) in sorted(self._tile_rects):
            y0, x0 = oy + r * th, ox + c * tw
            y1, x1 = oy + min((r + 1) * th, self._full_h), ox + min((c + 1) * tw, self._full_w)
            colour, width, fill, hatched, layer, framed = self._tile_look(r, c)
            poly = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
            item = {"id": f"tile_{r}_{c}", "polygon": poly, "color": colour}
            if width is not None:
                item["width"] = width
            if fill:
                item["fill"] = fill
            if hatched:
                item["hatch"] = True
            items.append((layer, item))
            if framed:                                     # the box's white frame, over it
                items.append((layer + 0.5, {"id": f"tile_{r}_{c}_box", "polygon": poly,
                                            "color": self.PREVIEW_COLOUR, "width": self.PREVIEW_WIDTH}))
        # drawn in layer order: purple under running / done, the box on top
        return [it for _layer, it in sorted(items, key=lambda li: li[0])]

    def tile_at(self, y, x):
        """(row, col) of the drawn tile under slide pixel (y, x), or None."""
        if not self._tile_rects or not self._full_h:
            return None
        nr, nc, th, tw, oy, ox = self._tile_geometry()
        return self._tile_index_at(y - oy, x - ox)

    def tile_at_clamped(self, y, x):
        """(row, col) of the drawn tile nearest slide pixel (y, x): a point
        outside the grid is clamped to its edge tiles (a Shift+drag box)."""
        if not self._tile_rects or not self._full_h:
            return None
        nr, nc, th, tw, oy, ox = self._tile_geometry()
        return self._tile_index_at(y - oy, x - ox, clamp=True)

    def _tile_index_at(self, ly, lx, clamp=False):
        """(row, col) under a point in the region's own pixels."""
        nr, nc, th, tw, _oy, _ox = self._tile_geometry()
        if clamp:
            rc = (min(max(int(ly // th), 0), nr - 1), min(max(int(lx // tw), 0), nc - 1))
        elif not (0 <= ly < self._full_h and 0 <= lx < self._full_w):
            return None
        else:
            rc = (int(ly // th), int(lx // tw))
        return rc if rc in self._tile_rects else None

    # ── Segmentation params loading ───────────────────────────────────

    def load_step1_active_params(self, output_dir):
        print("[Step2] entered from Step1")
        if not self._load_seg_params_index(output_dir, silent=True, apply_active=True):
            print("[Step2] failed to auto-load Step1 active segmentation params")
            return False
        self._set_param_source("index")
        self._applied_param_file = self._seg_param_file
        cfg = self._seg_config or {}
        print(f"[Step2] auto-loaded active method={cfg.get('method')}")
        print(f"[Step2] active_param_file={self._seg_param_file}")
        return True

    def use_manual_default(self, index_dir=""):
        """A Generate without a chosen pre-segmentation result (docs/
        v17_step1_generate_decouple_plan.md §2, user ruling §5 Q1): source
        "None / Manual default", the method's defaults on screen. The
        parameter index (`index_dir`'s, when none is loaded yet) is loaded
        but not applied, so an older file can still be chosen on purpose."""
        if index_dir and not self._seg_params_edit.text().strip():
            self._load_seg_params_index(index_dir, silent=True, apply_active=False)
        self._set_param_source("manual")
        self._seg_config = {}
        method = self._method_combo.currentData() or CELLPOSE_WHOLECELL_FUSION
        old = self._loading_index_selection
        self._loading_index_selection = True
        try:
            self._apply_method_defaults_to_ui(method)
        finally:
            self._loading_index_selection = old
        print(f"[Step2] manual default (method={method}): no Step1 parameters chosen")

    def _input_fusion_settings_hash(self):
        """The fusion settings hash of the fuse run Step2's input comes from
        (its params.json), or "" outside a workspace / for an older image."""
        from ..utils import run_store
        if not self._zarr_path:
            return ""
        run_dir = os.path.dirname(os.path.abspath(self._zarr_path))
        if run_store.kind_of(run_dir) != "fuse":
            return ""
        try:
            params = run_store.read_params(run_dir) or {}
        except Exception:                                   # noqa: BLE001
            return ""
        return str(((params.get("fusion_settings") or {}).get("hash")) or "")

    def _confirm_fusion_settings_match(self, seg_config):
        """Parameters found on other fusion settings than the input's (user
        ruling 2026-11-10, plan §2): asked before the run. Returns
        `(run, record)`; `record` goes into the run's seg_config only (the
        worker does not check it). Nothing is asked without both hashes."""
        try:
            block = preseg_contract.validate(seg_config)
        except preseg_contract.ContractError:
            return True, None                   # the contract check has said why
        params_hash = str((block or {}).get("fusion_settings_hash") or "")
        input_hash = self._input_fusion_settings_hash()
        if not params_hash or not input_hash or params_hash == input_hash:
            return True, None
        print(f"[Step2] parameters from other fusion settings: "
              f"{params_hash[:12]} (params) vs {input_hash[:12]} (input)")
        box = QMessageBox(self)
        box.setWindowTitle('Segmentation params')
        box.setIcon(QMessageBox.Warning)
        box.setText(
            "These parameters were found on other fusion settings than the ones the "
            "input image was generated with, so the result may differ from the Step1 "
            "preview.\n\nRun anyway? This is recorded with the result.")
        run_btn = box.addButton('Run anyway', QMessageBox.AcceptRole)
        box.addButton('Cancel', QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not run_btn:
            return False, None
        return True, {"params_hash": params_hash, "input_hash": input_hash,
                      "confirmed_at": time.strftime("%Y-%m-%d %H:%M:%S")}

    def _check_method_offered(self, method):
        """Raise ValueError unless `method` is one the method box offers: an
        unlisted one is refused, never silently replaced by the method on
        screen."""
        if self._method_combo.findData(method) < 0:
            raise ValueError(f"Unknown segmentation method: {method}")

    def _apply_seg_config_to_ui(self, p):
        method = p.get('method', CELLPOSE_WHOLECELL_FUSION)
        self._check_method_offered(method)          # before any widget changes
        idx = self._method_combo.findData(method)
        if idx >= 0:
            self._method_combo.setCurrentIndex(idx)
            # model_type ignored in Cellpose 4.0.1+; skip UI update
            self._cp_diam.setValue(p.get('diameter') or 0)
            self._cp_flow.setValue(p.get('flow_threshold', 0.4))
            self._cp_prob.setValue(p.get('cellprob_threshold', 0.0))
            self._cp_minsize.setValue(p.get('min_size', 15))
            self._set_nucleus_pass(p)
        self._cp_gpu.setChecked(_gpu_on(p.get('use_gpu', True)))
        self._sd_model.setText(preseg_contract.STARDIST_MODEL)
        self._sd_prob.setValue(-1.0 if p.get('prob_thresh') is None else float(p.get('prob_thresh')))
        self._sd_nms.setValue(-1.0 if p.get('nms_thresh') is None else float(p.get('nms_thresh')))
        self._sd_expand.setValue(float(p.get('expand_distance', 8) or 0))
        if method in (MESMER_WHOLE_CELL, MESMER_NUCLEI, MESMER_NUCLEAR_GUIDED):
            maxima = p.get("maxima_threshold")
            interior = p.get("interior_threshold")
            self._mesmer_maxima.setValue(float(maxima) if maxima is not None
                                         else (0.1 if method == MESMER_NUCLEI else 0.075))
            self._mesmer_interior.setValue(float(interior) if interior is not None else 0.2)
            self._mesmer_mpp.setValue(float(p.get("image_mpp", p.get("pixel_size", 0.5)) or 0.5))
            self._mesmer_min_size.setValue(int(p.get("postprocess_min_size", 0) or 0))
        self._on_method_changed()

    def _set_nucleus_pass(self, p):
        """The nucleus pass's four values (Cellpose whole-cell + nuclei);
        a missing key is the registry's default, None = auto diameter."""
        d = SEGMENTATION_METHODS[CELLPOSE_WHOLECELL_NUCLEI]["params"]
        diam = p.get('nucleus_diameter', d['nucleus_diameter'])
        self._nuc_diam.setValue(float(diam or 0))
        self._nuc_flow.setValue(float(p.get('nucleus_flow_threshold', d['nucleus_flow_threshold'])))
        self._nuc_prob.setValue(float(p.get('nucleus_cellprob_threshold',
                                            d['nucleus_cellprob_threshold'])))
        self._nuc_minsize.setValue(int(p.get('nucleus_min_size', d['nucleus_min_size'])))
        self._split_cells.setChecked(bool(p.get('split_cells_by_nuclei', True)))

    def _apply_method_defaults_to_ui(self, method):
        cfg = normalize_segmentation_config({"method": method})
        self._cp_diam.setValue(cfg.get('diameter') or 0)
        self._cp_flow.setValue(cfg.get('flow_threshold', 0.4))
        self._cp_prob.setValue(cfg.get('cellprob_threshold', 0.0))
        self._cp_minsize.setValue(cfg.get('min_size', 15))
        self._set_nucleus_pass(cfg)
        self._cp_gpu.setChecked(_gpu_on(cfg.get('use_gpu', True)))
        self._sd_model.setText(preseg_contract.STARDIST_MODEL)
        self._sd_prob.setValue(-1.0 if cfg.get('prob_thresh') is None else float(cfg.get('prob_thresh')))
        self._sd_nms.setValue(-1.0 if cfg.get('nms_thresh') is None else float(cfg.get('nms_thresh')))
        self._sd_expand.setValue(float(cfg.get('expand_distance', 8) or 0))
        if method in (MESMER_WHOLE_CELL, MESMER_NUCLEI, MESMER_NUCLEAR_GUIDED):
            maxima = cfg.get("maxima_threshold")
            interior = cfg.get("interior_threshold")
            self._mesmer_maxima.setValue(float(maxima) if maxima is not None
                                         else (0.1 if method == MESMER_NUCLEI else 0.075))
            self._mesmer_interior.setValue(float(interior) if interior is not None else 0.2)
            self._mesmer_mpp.setValue(float(cfg.get("image_mpp", cfg.get("pixel_size", 0.5)) or 0.5))
            self._mesmer_min_size.setValue(int(cfg.get("postprocess_min_size", 0) or 0))

    def get_cp_params(self):
        return self.get_seg_config()

    def get_seg_config(self):
        method = self._method_combo.currentData() or CELLPOSE_WHOLECELL_FUSION
        diam = self._cp_diam.value()
        data = dict(self._seg_config or {})
        params = dict(get_segmentation_method_config(method).get("params") or {})
        if method in (MESMER_WHOLE_CELL, MESMER_NUCLEI, MESMER_NUCLEAR_GUIDED):
            mode = {
                MESMER_WHOLE_CELL: "whole_cell",
                MESMER_NUCLEI: "nuclei",
                MESMER_NUCLEAR_GUIDED: "nuclear_guided",
            }.get(method, "whole_cell")
            params.update({
                "mesmer_mode": mode,
                "input_mode": preseg_contract.mesmer_input_mode(method),
                "compartment": "nuclear" if method == MESMER_NUCLEI else "whole-cell",
                "maxima_threshold": self._mesmer_maxima.value(),
                "interior_threshold": self._mesmer_interior.value(),
                "image_mpp": self._mesmer_mpp.value(),
                "pixel_size": self._mesmer_mpp.value(),
                "postprocess_min_size": self._mesmer_min_size.value(),
            })
            # The contract's fixed rules (no extra stretch; the thresholds'
            # target); the settings the engine does not use are dropped.
            params.update(preseg_contract.fixed_rules(method))
            for key in ("use_gpu", "nuclear_channel", "membrane_channels", "tile_size",
                        "overlap", "batch_size", "percentile_low", "percentile_high"):
                params.pop(key, None)
                data.pop(key, None)
        params.update({
            'model_name':         preseg_contract.STARDIST_MODEL,
            'prob_thresh':        None if self._sd_prob.value() < 0 else self._sd_prob.value(),
            'nms_thresh':         None if self._sd_nms.value() < 0 else self._sd_nms.value(),
            'expand_distance':    self._sd_expand.value(),
        })
        data.update({
            'method':             method,
            'params':             params,
            'model_type':         'cpsam',  # Cellpose 4.0.1+: always cpsam
            'diameter':           None if diam == 0 else diam,
            # (hybrid plan §2) the page's StarDist values, as the flat keys
            # are now normalised: a loaded file's own must not win over them
            'model_name':         params.get('model_name'),
            'prob_thresh':        params.get('prob_thresh'),
            'nms_thresh':         params.get('nms_thresh'),
            'flow_threshold':     self._cp_flow.value(),
            'cellprob_threshold': self._cp_prob.value(),
            'min_size':           self._cp_minsize.value(),
            'use_gpu':            self._cp_gpu.isChecked(),
            'tile_strategy_mode': 'auto' if self._auto_tile_strategy.isChecked() else 'manual',
            'suggested_tile_strategy': dict(self._suggested_tile_strategy or {}),
            'enable_tile_prefetch': True,
            'prefetch_queue_size': 2,
            'channel_cache_items': 32,
        })
        skip = self.skip_tiles_config()           # block S2T: only when chosen
        if skip is not None:
            data['skip_tiles'] = skip
        if method in (MESMER_WHOLE_CELL, MESMER_NUCLEI, MESMER_NUCLEAR_GUIDED):
            data.update(params)
        if method in (CELLPOSE_WHOLECELL_NUCLEI, HYBRID_CELLPOSE_STARDIST):
            # plan §15: the split switch and the pairing rules' version
            pair = dict(preseg_contract.fixed_rules(method),
                        split_cells_by_nuclei=self._split_cells.isChecked())
            data.update(pair)
            params.update(pair)
        else:
            for key in ('split_cells_by_nuclei', 'pairing_rules'):
                data.pop(key, None)
                params.pop(key, None)
        if method != CELLPOSE_WHOLECELL_NUCLEI:
            # a file / draft of that method loaded before: its nucleus values
            # do not belong to this one (codex review 2026-11-10)
            for key in NUCLEUS_PASS_KEYS:
                data.pop(key, None)
                params.pop(key, None)
        else:
            nd = self._nuc_diam.value()
            data.update({
                'nucleus_diameter':           None if nd == 0 else nd,
                'nucleus_flow_threshold':     self._nuc_flow.value(),
                'nucleus_cellprob_threshold': self._nuc_prob.value(),
                'nucleus_min_size':           self._nuc_minsize.value(),
            })
        cfg = normalize_segmentation_config(data)
        return cfg

    def _on_method_changed(self):
        method = self._method_combo.currentData() or CELLPOSE_WHOLECELL_FUSION
        if (
            not self._loading_index_selection
            and (self._param_source_combo.currentData() or "manual") == "manual"
        ):
            self._apply_method_defaults_to_ui(method)
        is_cellpose = method in (CELLPOSE_WHOLECELL_FUSION, CELLPOSE_WHOLECELL_NUCLEI,
                                 HYBRID_CELLPOSE_STARDIST, CELLPOSE_NUCLEI_DAPI, CELLPOSE_NUCLEI_EXPANSION)
        is_stardist = method in (STARDIST_NUCLEI_DAPI, STARDIST_NUCLEI_EXPANSION,
                                 HYBRID_CELLPOSE_STARDIST)
        is_expansion = method in (CELLPOSE_NUCLEI_EXPANSION, STARDIST_NUCLEI_EXPANSION)
        is_mesmer = method in (MESMER_WHOLE_CELL, MESMER_NUCLEI, MESMER_NUCLEAR_GUIDED)
        for w in (
            self._cp_model_label, self._cp_model_lbl,
            self._cp_diam_label, self._cp_diam,
            self._cp_flow_label, self._cp_flow,
            self._cp_prob_label, self._cp_prob,
            self._cp_minsize_label, self._cp_minsize,
        ):
            w.setVisible(is_cellpose)
        for w in (
            self._sd_model_label, self._sd_model,
            self._sd_prob_label, self._sd_prob,
            self._sd_nms_label, self._sd_nms,
        ):
            w.setVisible(is_stardist)
        for w in (self._nuc_diam_label, self._nuc_diam, self._nuc_flow_label, self._nuc_flow,
                  self._nuc_prob_label, self._nuc_prob, self._nuc_minsize_label,
                  self._nuc_minsize):
            w.setVisible(method == CELLPOSE_WHOLECELL_NUCLEI)
        for w in (self._split_cells_label, self._split_cells):
            w.setVisible(method in (CELLPOSE_WHOLECELL_NUCLEI, HYBRID_CELLPOSE_STARDIST))
        self._sd_expand_label.setVisible(is_expansion)
        self._sd_expand.setVisible(is_expansion)
        for w in self._mesmer_widgets:
            w.setVisible(is_mesmer)
        cfg = get_segmentation_method_config(method)
        self._method_hint.setText(
            f'{method} | input={cfg.get("input_type")} | output={cfg.get("output_type")}'
        )
        print("[Step2] method changed:", method)
        if getattr(self, "_full_h", 0):
            self._refresh_tile_suggestion(apply=getattr(self, "_auto_tile_strategy", None) is not None and self._auto_tile_strategy.isChecked())
            self._keep_stardist_tiles_safe()             # StarDist chosen: its tiles must fit (2026-10-25)

    # ── run / stop ────────────────────────────────────────────────────

    def _check_preseg_contract(self, seg_config):
        """True when `seg_config` may run. A Step1 pre-segmentation hand-over
        must reach the engine unchanged and run with the HALO it was computed
        with (plan 7.11.3); the worker then runs it through the engine
        process (Step2 hook-up, step 3)."""
        try:
            block = preseg_contract.validate(seg_config)
        except preseg_contract.ContractError as exc:
            QMessageBox.warning(self, 'Segmentation params', f'{exc}.')
            return False
        if block is None:
            return True
        # What the worker reads: the normalised config, whose top level mirrors
        # `params` (params win) and keeps keys Step2 has no control for
        # (Mesmer's thresholds).
        effective = normalize_segmentation_config(seg_config)
        # A fixed parameter (the StarDist model) is not a mismatch: the file is
        # run with the fixed value, said before the run (block M).
        fixed = {spec.key for spec in param_schema.specs(block["method"]) if spec.fixed}
        diff = [d for d in preseg_contract.mismatches(block, effective) if d[0] not in fixed]
        if diff:
            lines = "\n".join(f"  {k}: Step1 {want!r}, Step2 {got!r}" for k, want, got in diff)
            QMessageBox.warning(
                self, 'Segmentation params',
                "The parameters in Step2 are not the ones chosen in Step1:\n"
                f"{lines}\n\nReload the saved parameters, or choose again in Step1.")
            return False
        halo = int(block["halo_px"])
        # a run that follows cores has no grid: the hand-over's HALO is its
        # grid's (docs/v17_step2_core_by_core_plan.md §3.1); the worker decides
        # again from the frozen snapshot
        if halo != self._overlap_spin.value() and not self.__dict__.get("_cores_preview"):
            QMessageBox.warning(
                self, 'Segmentation params',
                f"The Step1 result was computed with a HALO of {halo} px, but the Tile "
                f"Grid overlap is {self._overlap_spin.value()} px.\n\n"
                f"Set the overlap to {halo} px to run it.")
            return False
        # (The hand-over's own Regions are not used any more -- §11: the cores
        # are frozen at Run from the last Save, with the fused input's OWN
        # Fusion settings -- so nothing here ties the input to them.)
        return True

    def _confirm_ignored_settings(self, path):
        """Block M: a params file's settings the engine does not use are listed
        with the reason before the run; the file runs by the contract or not
        at all. True when there is nothing to say or the user runs it."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return True            # loading already reported an unreadable file
        ignored = preseg_contract.ignored_settings(raw)
        if not ignored:
            return True
        lines = "\n".join(f"  {key} = {value!r}: {reason}" for key, value, reason in ignored)
        print(f"[Step2] settings the engine does not use ({os.path.basename(path)}):\n{lines}")
        box = QMessageBox(self)
        box.setWindowTitle('Segmentation params')
        box.setIcon(QMessageBox.Information)
        box.setText(
            f"{os.path.basename(path)} has settings the segmentation engine does not use:\n"
            f"{lines}\n\nThe run uses the contract instead.")
        run_btn = box.addButton('Run per contract', QMessageBox.AcceptRole)
        box.addButton('Cancel', QMessageBox.RejectRole)
        box.exec()
        return box.clickedButton() is run_btn

    def _confirm_engine_identity(self, seg_config):
        """Block A0.5: when the engine this run would use differs from the one
        the Step1 result ran on -- its behaviour version or its model -- the
        user is asked before the run. Returns `(run, confirmation)`;
        `confirmation` goes to the worker, which refuses any difference that
        was not confirmed. A legacy (pre-v2) Step1 identity is only recorded
        by the worker; another engine kind is refused by the worker."""
        block = preseg_contract.validate(seg_config)
        if block is None:
            return True, None
        engine = seg_engines.METHOD_ENGINE.get(block["method"])
        if engine not in seg_engines.BEHAVIOR_VERSION:
            return True, None
        cmp = seg_engines.compare_identity(block["engine_identity"],
                                           seg_engines.behavior_identity(engine))
        if cmp["engine_differs"] or cmp["legacy_identity"] or not cmp["differences"]:
            return True, None
        said = {"behavior_version": "the {e} segmentation behaviour has changed since the "
                                    "Step1 run (version {a} -> {b})",
                "model_id": "the {e} model has changed since the Step1 run ({a} -> {b})"}
        lines = "\n".join("  - " + said.get(k, "{k}: {a} -> {b}").format(e=engine, k=k, a=a, b=b)
                          for k, a, b in cmp["differences"])
        print(f"[Step2] engine differs from the Step1 run:\n{lines}")
        box = QMessageBox(self)
        box.setWindowTitle('Segmentation engine')
        box.setIcon(QMessageBox.Warning)
        box.setText(
            "The engine this run would use is not the one the Step1 result ran on:\n"
            f"{lines}\n\nThe Step1 preview was made with the old one, so the whole-slide "
            "result may differ from it. Run anyway? The difference is recorded with the result.")
        run_btn = box.addButton('Run anyway', QMessageBox.AcceptRole)
        box.addButton('Cancel', QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not run_btn:
            return False, None
        return True, {"differences": cmp["differences"],
                      "confirmed_at": time.strftime("%Y-%m-%d %H:%M:%S")}

    def set_region_inputs(self, region_fused_paths=None):
        """Block RM: each region's fused product in the chosen fuse run."""
        self._region_fused_paths = {str(k): str(v) for k, v in
                                    (region_fused_paths or {}).items() if v}

    def _run(self):
        # backlog 20: where the wait after Run goes, one line per step
        t_click = time.perf_counter()
        laps, lines = [t_click], []             # the lines go to the run log too (worker)

        def lap(what):
            now = time.perf_counter()
            line = (f"[Step2 timing] {what}: {(now - laps[-1]) * 1000:.0f} ms "
                    f"({(now - t_click) * 1000:.0f} ms after Run)")
            print(line, flush=True)
            lines.append(line)
            laps.append(now)
        self._box_cancel()                      # a box still being dragged changes nothing
        if not self._zarr_path or not os.path.exists(self._zarr_path):
            QMessageBox.warning(self, 'No data',
                                'Please load a fused.zarr first.')
            return
        # Block RM (§3): inside a workspace a run's input is a Step1 result
        # (a published fuse run) -- its one upstream.
        from ..utils import run_store
        run_dir = os.path.dirname(os.path.abspath(self._zarr_path))
        if self._roi_dir and run_store.project_dir_of(self._roi_dir) and not (
                run_store.kind_of(run_dir) == "fuse" and run_store.is_done(run_dir)
                and os.path.realpath(run_store.roi_dir_of(run_dir))
                == os.path.realpath(self._roi_dir)
                and os.path.abspath(run_dir) == os.path.abspath(self.selected_fuse_run() or ".")):
            QMessageBox.warning(self, 'Step2',
                                'Choose a Step1 result in Input: inside a project, Step2 '
                                'runs on the result chosen there (one Step1 published in '
                                'this workspace).')
            return
        source = self._param_source_combo.currentData() or "manual"
        if source == "index":
            if not self._seg_params_index:
                path = self._seg_params_edit.text().strip()
                if path and not self._load_seg_params_index(path, silent=False, apply_active=False):
                    return
            resolved = self._update_resolved_param_path()
            if not resolved or not os.path.exists(resolved):
                QMessageBox.warning(self, 'Error', 'No indexed segmentation parameter file selected.')
                return
            current_method = self._method_combo.currentData() or CELLPOSE_WHOLECELL_FUSION
            selected_method = self._index_method_combo.currentData()
            if (
                not self._applied_param_file
                or os.path.abspath(self._applied_param_file) != os.path.abspath(resolved)
                or (selected_method and selected_method != current_method)
            ):
                if not self._apply_selected_index_params():
                    return
        seg_config = self.get_seg_config()
        lap("input and parameters")
        if self._region_fused_paths:                # block RM: the chosen fuse run's
            seg_config["region_fused_paths"] = dict(self._region_fused_paths)

        # docs/f5_tma_display_plan.md 9.2: a TMA project's run follows its cores
        # whatever the parameters' source -- frozen here, before anything is
        # created, so a refusal or a No leaves no run behind.
        # User ruling 2026-10-19 (third round): the parameters are decoupled
        # from Regions -- whatever their source, the cores are the last Regions
        # Save's, frozen now; a Step1 hand-over's own frozen cores are not used.
        # docs/v17_step2_core_by_core_plan.md §3.0: frozen FIRST -- the layout
        # (cores or grid) follows this snapshot, and the checks below depend on it.
        self._without_handover_regions(seg_config)
        regions_request = None
        if callable(self.manual_regions):
            def ask(text):
                return QMessageBox.question(self, "TMA cores", text, QMessageBox.Yes | QMessageBox.No,
                                            QMessageBox.No) == QMessageBox.Yes
            regions_request, why = self.manual_regions(seg_config.get("method"), self._zarr_path,
                                                       self._rois, ask)
            if why is None:
                return                                    # the user said No
            if why:
                QMessageBox.warning(self, "Regions", f"This run cannot follow the Regions result: {why}.")
                return
        lap("TMA cores frozen (with any question answered)")
        from ..core.regions import analysis as RA
        frozen = (regions_request or {}).get("snapshot") if regions_request else None
        self._apply_cores_layout(count=len(frozen.get("cores") or []) if frozen and RA.has_cores(frozen) else 0)
        if not self._stardist_grid_ok_for_run():
            return
        identity_confirmation = None
        if source == "index":
            if not self._check_preseg_contract(seg_config):
                return
            if not self._confirm_ignored_settings(resolved):
                return
            run_it, identity_confirmation = self._confirm_engine_identity(seg_config)
            if not run_it:
                return
            run_it, settings_record = self._confirm_fusion_settings_match(seg_config)
            if not run_it:
                return
            if settings_record is not None:
                seg_config["settings_mismatch_confirmed"] = settings_record
        else:
            # Manual parameters are the user's own, not a Step1 hand-over.
            seg_config.pop(preseg_contract.CONTRACT_KEY, None)
        lap("checks (with any question answered)")
        recovery_dir = None
        if self._rec_box.isChecked():
            recovery_dir = self._rec_edit.text().strip() or None
        if recovery_dir and seg_config.get("method") in seg_engines.CELL_AND_NUCLEUS_METHODS:
            # docs/v17_cellpose_cell_nucleus_plan.md §2: recovered tiles have
            # no nuclei, so the cells could not be paired
            QMessageBox.warning(self, 'Recovery Mode',
                                'Recovery Mode cannot run this method: the saved tiles have '
                                'no nuclei, so cells without a nucleus could not be dropped.\n\n'
                                'Untick Recovery Mode to run it.')
            return

        if self._auto_tile_strategy.isChecked() and not self._is_stardist():
            # (StarDist: the grid checked by `_stardist_grid_ok_for_run` -- over
            # every region -- is the one that runs; Auto would re-size it from
            # the shown input alone)
            self._refresh_tile_suggestion(apply=True)

        nr  = self._rows_spin.value()
        nc  = self._cols_spin.value()
        self._n_rows = nr
        self._n_cols = nc
        # the skip plan of the grid that runs (the StarDist check or Auto may
        # have changed the grid after the configuration was read)
        skip = self.skip_tiles_config()
        if skip is not None:
            seg_config["skip_tiles"] = skip
        else:
            seg_config.pop("skip_tiles", None)
        lap("tile grid and skip plan")
        param_file = ""
        if source == "index" and self._applied_param_file:
            param_file = self._applied_param_file

        # the tiles the worker will skip (its `_tile_skip_set`); none on a cores run
        cores_run = bool(frozen and RA.has_cores(frozen))
        skipped = set()
        if skip is not None and not cores_run and not recovery_dir:
            forced_tiles = {int(e["tile"]) for e in skip["user_forced"]}
            skipped = ((set(skip["auto"]) - set(skip["user_kept"])) | forced_tiles) & set(range(nr * nc))
        self._begin_run_progress(nr, nc, skipped)
        self._total_cells = 0
        self._cells_lbl.setText('Total cells detected: 0')

        self._worker = SegmentMergeWorker(
            zarr_path        = self._zarr_path,
            seg_config       = seg_config,
            n_rows           = nr,
            n_cols           = nc,
            overlap_px       = self._overlap_spin.value(),
            output_dir       = self._step2_dir or self._out_edit.text().strip() or OUTPUT_DIR,
            recovery_npy_dir = recovery_dir,
            rois             = self._rois if self._rois else None,
            param_file        = param_file,
            parameter_source  = "index" if param_file else "manual",
            identity_confirmation = identity_confirmation,
            regions_request  = regions_request,
        )
        self._worker.run_clicked_at = t_click       # its own steps are timed from the click
        self._worker.page_timings = lines           # written to its log when the log exists
        lap("run prepared")
        # the cores this run follows (its hand-over's frozen snapshot), drawn as frozen
        reg = regions_request
        self._run_core_snapshot = reg["snapshot"] if reg and reg.get("snapshot") else None
        self._run_core_input = self._zarr_path
        self.refresh_core_outlines()
        seg_cfg = seg_config
        print(f"[Step2] segmentation method={seg_cfg.get('method')}")
        print(f"[Step2] input_type={seg_cfg.get('input_type')}")
        if self._roi_id:
            print(f"[Step2] roi_id={self._roi_id}")
            print(f"[Step2] output_base={self._step2_dir or self._out_edit.text().strip()}")
        # Block A6 G2: the run carries what it was started for; its answer is
        # checked against what the page shows when it arrives -- its progress
        # and tile states too (user ruling 2026-10-02).
        token = self._run_identity()
        worker = self._worker
        self._run_token = token
        if self._run_progress is not None:
            self._run_progress["token"] = token
        self._old_run_lbl.setVisible(False)
        self._worker.progress.connect(
            lambda *a, t=token, wk=worker: self._run_signal(t, wk, self._on_progress, a))
        self._worker.tile_done.connect(
            lambda *a, t=token, wk=worker: self._run_signal(t, wk, self._on_tile_done, a))
        self._worker.tile_skipped.connect(
            lambda *a, t=token, wk=worker: self._run_signal(t, wk, self._on_tile_skipped, a))
        self._worker.job_finished.connect(
            lambda out, n, t=token, wk=worker: self._on_run_finished(t, wk, out, n))
        self._worker.error.connect(
            lambda m, t=token, wk=worker: self._on_run_error(t, wk, m))

        self._btn_run.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._prog_bar.setValue(0)
        # Block A9 §36: a hidden image viewer gives its GPU textures back
        # (down to its coarse) before segmentation needs the memory
        from . import gpu_memory
        gpu_memory.release_hidden("segmentation")
        lap("drawn, hidden viewers released")
        self._worker.start()

        # Mark the first tile that runs as running (not a skipped one)
        first = next((i for i in range(max(1, nr * nc)) if i not in skipped), None)
        if first is not None:
            self._record_tile(first, 'running')

    def _stop(self):
        if self._worker:
            self._worker.stop()

    def stop_background_jobs(self):
        """Window closing: ask the segmentation to stop, never wait for it.
        True while the worker is still running (the window then retries)."""
        worker = self._worker
        if worker is None or not worker.isRunning():
            return False
        worker.stop()
        return worker.isRunning()

    def _on_progress(self, done, total, msg):
        pct = int(done / total * 100) if total > 0 else 0
        if self._run_progress is not None:
            self._run_progress["progress"] = (pct, msg)
        if self._run_view_live():
            self._prog_bar.setValue(pct)
            self._prog_lbl.setText(msg)

        # Mark next tile as running -- not a skipped one: the run tells every
        # skipped tile before its first tile (backlog 20), and a finished
        # tile still announces the index after it
        if done < total and not (self._run_progress is not None
                                 and str(self._run_progress["state"].get(int(done), "")).startswith('skipped')):
            self._record_tile(done, 'running')

    def _on_tile_done(self, tile_idx, n_tiles, n_cells):
        self._record_tile(tile_idx, 'done')
        self._total_cells += n_cells
        if self._run_view_live():
            self._cells_lbl.setText(
                f'Total cells detected: {self._total_cells:,}'
            )

    def _run_identity(self):
        """Block A6 G2: the dataset generation (the window's display
        generation) and the workspace this page is bound to."""
        win = self.window()
        return {
            "dataset_gen": int(getattr(win, "_dataset_gen_seen", 0) or 0),
            "roi_dir": self._roi_dir or "",
            "roi_id": self._roi_id or "",
            "zarr_path": self._zarr_path or "",
        }

    def _run_signal(self, token, worker, handler, args):
        """A progress / tile signal: always recorded for its run (only for
        the CURRENT run -- a replaced worker's late signal is dropped), drawn
        only while the page shows the run's context."""
        if worker is not self._worker:
            return
        handler(*args)
        if self._run_identity() != token:
            self._say_old_run(token, worker, "is still computing")

    def _say_old_run(self, token, worker, what):
        where = os.path.abspath(str(getattr(worker, "output_dir", "") or ""))
        self._old_run_lbl.setText(
            f"An older run (workspace {token.get('roi_id') or '(none)'}) {what}; "
            f"its results are not shown here. It writes to:\n{where}")
        self._old_run_lbl.setVisible(True)

    def _on_run_finished(self, token, worker, output_dir, total_cells):
        if self._run_identity() == token:
            self._on_finished(output_dir, total_cells)
            return
        # The run finished in ITS workspace; the page has moved on. Its own
        # workspace is still told; this page shows nothing of it.
        self._mark_run_workspace(token)
        self._late_run_ended(token, worker, f"finished ({total_cells:,} cells "
                                            f"→ {output_dir})")

    def _on_run_error(self, token, worker, msg):
        if self._run_identity() == token:
            self._on_error(msg)
            return
        first = str(msg or "").strip().splitlines()[0] if msg else ""
        self._late_run_ended(token, worker, f"failed: {first}")

    def _late_run_ended(self, token, worker, what):
        """A run started for another dataset / workspace has ended: log it,
        and let go of the Run / Stop state it was holding. No result text, no
        signal to the window, no box."""
        print(f"[Step2] a run for workspace {token.get('roi_id') or '(none)'} "
              f"(dataset generation {token.get('dataset_gen')}) {what}; "
              f"the page now shows another context, so nothing is shown here")
        self._say_old_run(token, worker, "has finished" if what.startswith("finished")
                          else "has stopped")
        if worker is not self._worker:
            return
        self._run_grid_lbl.setVisible(False)
        self._run_active = False
        self._skip_empty_cb.setEnabled(not self.__dict__.get("_cores_preview"))   # a cores run has no grid
        self.refresh_core_outlines()             # no highlight; the last Save's cores again
        self._sync_skip_buttons()
        self._btn_run.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._apply_pending_context()

    @staticmethod
    def _mark_run_workspace(token):
        """Step2 done, in the run's OWN workspace (not the page's current)."""
        roi_id, roi_dir = token.get("roi_id"), token.get("roi_dir")
        if not (roi_id and roi_dir):
            return
        try:
            project_dir = os.path.dirname(os.path.dirname(roi_dir))
            mark_roi_step(project_dir, roi_id, "step2", "done")
        except Exception as e:
            print(f"[Step2] failed to update ROI step2 status: {e}")

    def _apply_pending_context(self):
        """The run ended: switch to the workspace handed over meanwhile."""
        pending, self._pending_context = self._pending_context, None
        follow, self._after_pending_context = self._after_pending_context, None
        if pending is not None:
            print(f"[Step2] switching to workspace {pending[0] or '(none)'}")
            self.set_roi_context(*pending)
            if follow is not None:
                follow()

    def _on_finished(self, output_dir, total_cells):
        self._run_grid_lbl.setVisible(False)
        self._run_active = False
        self._skip_empty_cb.setEnabled(not self.__dict__.get("_cores_preview"))   # a cores run has no grid
        self.refresh_core_outlines()             # no highlight; the last Save's cores again
        self._sync_skip_buttons()
        runtime = {}
        try:
            if self._worker is not None and hasattr(self._worker, "runtime_summary"):
                runtime = self._worker.runtime_summary()
        except Exception:
            runtime = {}
        runtime_text = ""
        if runtime:
            runtime_text = (
                f"Elapsed: {runtime.get('elapsed') or 'N/A'}  |  "
                f"Peak RAM: {runtime.get('peak_ram') or 'N/A'}  |  "
                f"Peak VRAM: {runtime.get('peak_vram') or 'N/A'}"
            )
        self._prog_bar.setValue(100)
        self._prog_lbl.setText(
            f'✓ Done!  {total_cells:,} cells'
            + (f'  |  {runtime_text}' if runtime_text else '')
            + f'  →  {output_dir}'
        )
        self._btn_run.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._last_output_dir = output_dir
        self.segmentation_done.emit(output_dir)
        # Block A6 G2: the workspace the run was started for.
        self._mark_run_workspace(getattr(self, "_run_token", None)
                                 or self._run_identity())

        msg = QMessageBox(self)
        msg.setWindowTitle('Segmentation Complete')
        runtime_block = ""
        if runtime_text:
            runtime_block = (
                f'Runtime summary:\n'
                f'  {runtime_text}\n\n'
            )
        cores_text = self._cores_finish_text()             # 15B: a cores run names its cores
        msg.setText(
            f'Total cells: {total_cells:,}\n\n'
            f'{runtime_block}'
            f'Output directory:\n  {output_dir}\n\n'
            f'Global outputs:\n'
            f'  global_mask.zarr\n'
            f'  global_mask.ome.tiff   (merged mask, float32)\n'
            f'  global_dapi.ome.tiff   (DAPI, uint16)\n'
            f'  run_segmentation_params.json\n'
            f'  segmentation_meta.json\n\n'
            + (cores_text[0] if cores_text else
               f'Per-tile outputs:\n'
               f'  tile_masks/\n'
               f'    tile_r*_c*_dapi.ome.tiff\n'
               f'    tile_r*_c*_raw_mask.ome.tiff')
            + (f'\n\nStep2 now switches to the new workspace: {self._pending_context[0]}'
               if self._pending_context else '')
        )
        if cores_text:
            msg.setDetailedText(cores_text[1])
        btn_qc = msg.addButton('→ Open QC Viewer (Step 3)',
                               QMessageBox.AcceptRole)
        btn_feat = msg.addButton('→ Feature Extraction (Step 4)',
                                 QMessageBox.AcceptRole)
        msg.addButton('OK', QMessageBox.RejectRole)
        msg.exec()
        self._apply_pending_context()          # whichever button was pressed
        if msg.clickedButton() is btn_qc:
            self.open_qc_requested.emit(output_dir)
        elif msg.clickedButton() is btn_feat:
            self.open_qc_requested.emit(output_dir)   # step3 auto-loads too

    def _on_error(self, msg):
        self._mark_failed_cores(msg)
        self._run_grid_lbl.setVisible(False)
        self._run_active = False
        self._skip_empty_cb.setEnabled(not self.__dict__.get("_cores_preview"))   # a cores run has no grid
        self.refresh_core_outlines()             # no highlight; the last Save's cores again
        self._sync_skip_buttons()
        self._prog_lbl.setText('✗ Error — see terminal')
        self._btn_run.setEnabled(True)
        self._btn_stop.setEnabled(False)
        QMessageBox.critical(self, 'Error', msg)
        print(f'[Step2 Error]\n{msg}')
        self._apply_pending_context()


# ══════════════════════════════════════════════════════════════════════
#  Step 3 Page  — Segmentation QC Viewer
# ══════════════════════════════════════════════════════════════════════
