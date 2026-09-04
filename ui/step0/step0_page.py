"""
block01/ui/step0/step0_page.py — Step0Page (main Step 0 QWidget).
"""

import os
import gc
import math
import json
import shutil
import time
import traceback
import multiprocessing as mp
from queue import Empty

import numpy as np
import zarr

from PyQt5 import QtWidgets, QtCore, QtGui
from PyQt5.QtCore import Qt, QTimer, QRectF, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QGroupBox, QSlider,
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
from .step0_explore_tab import Step0ExploreTab
from ...core.display_mapping import build_display_lut, seed_display_range
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
from ..widgets.channel_workbench import (
    ChannelWorkbench,
    _PALETTE as CHANNEL_PALETTE,
    _hex_to_rgb01 as _channel_hex_to_rgb01,
)
from ..widgets.tissue_navigator_popup import TissueNavigatorPopup
from .roi_context_model import RoiContextModel
from ...utils.channel_remap_config import (
    save_channel_remap_config,
    normalize_channel_remap_params,
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

# (v15) The DAPI/nucleus overlay is a display AID, not the subject of the
# Background Correction page: it starts hidden in BOTH views (compare panels
# and full image) and the nucleus row's checkbox in the Channels list is what
# turns it on. One constant so the two hidden state holders, the full-image
# toolbar button and the dataset reload can never disagree.
DAPI_LAYER_DEFAULT_ON = False


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



class CompareSnapshotWorker(QThread):
    """One compare snapshot, computed off the GUI thread.

    Reads a raw crop of ONE pyramid level through the viewer's own
    `RawTileProvider` -- not the page's `OMETIFFLoader`, which is a
    single-handle GUI-thread object whose pixels change under
    `set_corrected_zarr_store` -- and corrects it with the viewer's own
    `CorrectionCompute`. That is the same object, the same kernels and the
    same level-scaled parameter the viewport tiles are computed with, so a
    panel shows what the full image would show for that method at that spot:

    * the parameter is `effective_param(base, level, downsample)`, exactly
      as `ExploreController._make_correction_key` derives it;
    * the crop is read with the method's own halo (`halo_for`, 2*radius for
      top-hat, ceil(4*sigma) for cuCIM) and cropped back afterwards, exactly
      as `CorrectionCompute.compute` does per tile.

    Which leaves ONE difference from a tile, and it is the favourable one: a
    tile's halo is filled from neighbouring tiles and clamped at the image
    edge, and so is this crop's, so interior pixels agree; only pixels whose
    halo runs off the SLIDE can differ, and there neither answer has data.

    `cached` is whatever the caller could already prove was in the viewer's
    caches (see `Step0Page._snapshot_from_caches`); those entries are used
    as-is and not recomputed.

    Nothing here writes: no cache, no zarr, no channel state. The worker
    produces arrays and hands them back.
    """

    done = pyqtSignal(int, dict)
    failed = pyqtSignal(int, str)

    def __init__(self, req_id, provider, compute, channel, nucleus_channel,
                 level, y0, x0, h, w, tophat_radius, cucim_sigma,
                 cached=None, parent=None):
        super().__init__(parent)
        self.req_id = int(req_id)
        self.provider = provider
        self.compute = compute
        self.channel = channel
        self.nucleus_channel = nucleus_channel
        self.level = int(level)
        self.y0, self.x0, self.h, self.w = (int(y0), int(x0), int(h), int(w))
        self.tophat_radius = int(tophat_radius)
        self.cucim_sigma = int(cucim_sigma)
        self.cached = dict(cached or {})

    def _read(self, channel, halo=0):
        """`(array, (row0, col0))` for the crop grown by `halo` on every
        side, clamped to the level and in float32."""
        arr, origin = self.provider.read_region(
            channel, self.level,
            self.y0 - halo, self.y0 + self.h + halo,
            self.x0 - halo, self.x0 + self.w + halo)
        return np.asarray(arr).astype(np.float32, copy=False), origin

    def _corrected(self, method, base):
        from ...viewer.correction_compute import halo_for
        from ...viewer.tile_types import effective_param

        ds = float(self.provider.level_downsample(self.level))
        param = effective_param(int(base), self.level, ds)
        halo = halo_for(method, param)
        padded, (ry0, rx0) = self._read(self.channel, halo)
        out = self.compute.correct_array(padded, method, param)
        cy0, cx0 = self.y0 - int(ry0), self.x0 - int(rx0)
        crop = out[cy0:cy0 + self.h, cx0:cx0 + self.w]
        return np.ascontiguousarray(crop.astype(np.float32, copy=False))

    def run(self):
        from ...core.bg_correction import _compute_bg_metrics
        try:
            payload = {}
            raw = self.cached.get("original_raw")
            if raw is None:
                raw, _origin = self._read(self.channel)
            payload["original_raw"] = raw
            for method, base in (("tophat", self.tophat_radius),
                                 ("cucim", self.cucim_sigma)):
                key = f"{method}_raw"
                arr = self.cached.get(key)
                payload[key] = (arr if arr is not None
                                else self._corrected(method, base))
            if self.nucleus_channel:
                nuc, _origin = self._read(self.nucleus_channel)
                payload["nucleus_raw"] = nuc
            for key in ("original", "tophat", "cucim"):
                payload[f"{key}_metrics"] = _compute_bg_metrics(
                    payload[f"{key}_raw"])
        except Exception as exc:                            # noqa: BLE001
            traceback.print_exc()
            self.failed.emit(self.req_id, str(exc))
            return
        self.done.emit(self.req_id, payload)



# The compare strip's height, in pixels, when a right-click expands it. Big
# enough that a 3-across snapshot is worth looking at, small enough that the
# full image above it stays the main view.
COMPARE_STRIP_HEIGHT = 260

# Which of the three compare results the full image shows. Ordered to match
# the compare panels left to right.
FULL_IMAGE_SOURCES = ("original", "tophat", "cucim")
# Viewport size, in level-0 pixels, for a Tissue Preview jump made before the
# full image has reported a viewport of its own.
FULL_IMAGE_JUMP_DEFAULT_SIZE = 2048
# A viewport at least this fraction of the slide in either dimension counts as
# "the whole slide": a jump from it zooms in to the default size instead of
# keeping a size that would make the jump invisible.
FULL_IMAGE_JUMP_WHOLE_SLIDE_FRACTION = 0.9
FULL_IMAGE_METHOD = {"original": None, "tophat": "tophat", "cucim": "cucim"}
# Labels for the full image's own method switch. Separate from the compare
# panels' TITLES tuple: those name three PANELS, these name three previews.
FULL_IMAGE_SOURCE_LABELS = {"original": "Original", "tophat": "TopHat",
                            "cucim": "cuCIM"}
FULL_IMAGE_SOURCE_TIPS = {
    "original": "Show the raw channel.",
    "tophat": "Preview a top-hat correction of the current viewport with "
              "this channel's radius. Nothing is written and nothing is "
              "marked computed.",
    "cucim": "Preview a cuCIM correction of the current viewport with this "
             "channel's sigma. Nothing is written and nothing is marked "
             "computed.",
}
# From this pyramid level up, the on-the-fly correction is computed on
# box-downsampled pixels with a level-scaled radius/sigma, which is NOT the
# level-0 correction Save writes. The user is told so rather than left to
# infer it from a preview that looks subtly different after a zoom.
FULL_IMAGE_COARSE_LEVEL = 2


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
        # Which compare result the full image shows. The user's choice
        # survives a move to the nucleus channel (shown as Original) and is
        # restored on the way back.
        self._full_image_source = "original"
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
        # The floating "Intensity" window (the Channel Remap inspector,
        # re-parented) and the widget it hosts. Both lazily created.
        self._intensity_window = None
        self._intensity_panel = None
        # Display mapping bookkeeping. `_display_mapping_for` may be asked for
        # a channel before the Channels box is built, so both live here.
        self._display_fallback = {}      # channel -> (lo, hi, gamma) when the workbench has no entry
        self._display_seeded = set()     # channels whose slide-wide seed was applied
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
        # Dataset generation ("epoch"). Bumped once per COMMITTED dataset switch.
        # Every worker callback is bound to the generation that was current when
        # the worker was created (see _gen_slot), so a worker started for the
        # previous dataset can never write this page's state after the switch.
        self._dataset_gen = 0
        # 预览结果缓存（供toggle复用）和zoom联动防循环flag
        self._last_payload = None
        # The compare panels hold a SNAPSHOT (`_take_compare_snapshot`):
        # what they show, where and at what scale is decided when it is
        # taken and never again, so there is no panel camera to keep in
        # step with anything and at most one request in flight.
        self._compare_snapshot = None
        self._compare_snapshot_worker = None
        self._compare_snapshot_req = 0
        # 预览结果缓存：key=(channel, patch_idx) → payload dict
        self._preview_cache: dict = {}
        # 通道颜色：key=channel_name → (R,G,B) float 0-1
        self._channel_colors: dict = {}
        # The channel the floating Intensity window EDITS, when that is not the
        # channel the page displays. Only ever the nucleus/DAPI channel: its row
        # is a REFERENCE selection ("now edit DAPI's mapping"), not a display
        # switch, so `current_channel` -- what the compare panels, the full
        # image and the method combo follow -- stays on the last marker.
        # None = the inspector follows `current_channel` like every other row.
        self._inspector_channel = None
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
        # What PRODUCED the cached result of a channel: {ch: signature}, with
        # signature = (method, tophat_radius, cucim_sigma, sorted patch bboxes).
        # Process compares the would-be signature against this and skips the
        # channels whose evidence says they are already up to date.
        self._computed_signatures: dict = {}
        # Signature of the run currently in flight per channel; promoted into
        # _computed_signatures as that channel's results land.
        self._pending_signatures: dict = {}
        # 参数是否被修改（提示需要重新Process）
        self._params_dirty: bool = False
        # True once a Process run has completed. Purely informational now
        # (it used to unlock on-demand computing, which no longer exists).
        self._process_completed: bool = False
        # Kept empty: on-demand computing is gone (see the removal note near
        # the batch handlers), but `production_correction_busy` and teardown
        # still read this list unconditionally.
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
        # The full-image viewer is NOT a top-level tab. It lives inside
        # Background Correction, as the second page of the Patch Preview
        # area, and is built there (Section C) -- one instance, owned by
        # that stack. The trial third tab it replaced is gone.
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

        # There is NO analysis-region selector any more (user request: it
        # duplicated the ROI button). The rule is the drawing itself: no ROI
        # drawn -> the whole slide is the analysis region; an ROI drawn ->
        # ROI mode. See `_is_full_wsi_mode`.

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

        # Each button toggles its own mode; clicking the active one turns
        # drawing OFF altogether (neither checked), which is the navigate/pan
        # mode of the overview. Never both on.
        self._btn_mode_roi.clicked.connect(lambda: self._on_mode_button("roi"))
        self._btn_mode_patch.clicked.connect(lambda: self._on_mode_button("patch"))
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
        # One compact button opens the floating "Intensity" window -- the
        # Channel Remap tab's inspector itself (histogram, Min/Max, Gamma,
        # Auto, Reset), re-parented. It belongs next to the channel list,
        # not in the Patch Preview header: it edits the SELECTED CHANNEL.
        self._btn_intensity_window = QPushButton("Intensity…")
        self._btn_intensity_window.setToolTip(
            "Open the floating Intensity window: the Channel Remap "
            "histogram, Min/Max, Gamma, Auto and Reset for the selected "
            "channel. Display only — it never changes the h5ad.")
        self._btn_intensity_window.setStyleSheet(
            "QPushButton{color:#c678dd;border:1px solid #c678dd;border-radius:3px;"
            "padding:1px 6px;font-size:10px;background:#1a1a1a;}"
            "QPushButton:hover{background:#2a1a33;}")
        self._btn_intensity_window.clicked.connect(self.show_intensity_window)

        all_row.addWidget(self._cb_all)
        all_row.addStretch()
        all_row.addWidget(self._btn_intensity_window)
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
        # The MODEL's selection, not the list's `currentRowChanged`. A click
        # on a row goes `ChannelRowBase.mousePressEvent` -> `model.select`
        # -> `ChannelDock._on_model_selection`, which moves the list's
        # current item with its signals BLOCKED -- so `currentRowChanged`
        # never fired for a mouse click, and the page kept showing the
        # channel it had (measured: the list's current row moved, the page's
        # `current_channel` did not). A programmatic `setCurrentRow` reaches
        # the model too (`ChannelDock._on_current_item`), so this one
        # connection covers both paths, and the model's own de-duplication
        # means the handler runs once per actual change.
        self._dock_adapter.model.selection_changed.connect(
            self._on_channel_selected_by_id)
        # The model is the third writer of a channel's colour (after this page
        # and the Channel Remap layer list). Listening here is what makes the
        # Intensity window's histogram follow a swatch change live, whichever
        # of the three did the writing.
        self._dock_adapter.model.color_changed.connect(
            self._on_model_color_changed)
        # One display mapping per channel (core/display_mapping.py). Its
        # SOURCE is the Channel Remap workbench's params (see
        # `_display_mapping_for`); `wb.params_changed` is what tells the
        # compare panels, the full image and its DAPI overlay to follow. The
        # model's display fields are only a mirror, so nothing is connected
        # to `display_changed` -- one source, one signal.
        self._display_fallback = {}      # channel -> (lo, hi, gamma) when the workbench has no entry
        self._display_seeded = set()     # channels whose slide-wide seed was applied
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

        prev_box = QGroupBox("Compare snapshot  —  Original | TopHat | cucim")
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

        # Nucleus / Marker display colours. The COLOUR PICKERS are no longer
        # here: every channel (the nucleus included) carries its own swatch in
        # the Channels list, which is where the user picks a colour now.
        self._nuc_color = (0.0, 0.5, 1.0)      # 默认蓝色
        self._marker_color = (0.0, 1.0, 0.3)   # 默认绿色

        # The two layer switches are not IN the header either: they are
        # hidden, out-of-layout state holders whose checked state
        # `_refresh_preview_display` reads. The nucleus one is flipped by the
        # DAPI row's checkbox in the Channels list; the marker one is driven
        # by code (and by tests). Keeping them as QPushButtons (rather than
        # plain booleans) keeps every existing `toggled` connection intact.
        self._btn_show_nucleus = QPushButton("Nucleus", self)
        self._btn_show_nucleus.setCheckable(True)
        # (v15) The DAPI layer starts OFF: the page is about the MARKER
        # channel the user selected, and a nucleus layer added on top of it
        # from the first frame is a second signal nobody asked for. The
        # nucleus row's checkbox in the Channels list turns it on.
        self._btn_show_nucleus.setChecked(DAPI_LAYER_DEFAULT_ON)
        self._btn_show_nucleus.setVisible(False)

        self._btn_show_marker = QPushButton("Marker", self)
        self._btn_show_marker.setCheckable(True)
        self._btn_show_marker.setChecked(True)
        self._btn_show_marker.setVisible(False)

        # The header carries what a SNAPSHOT needs and nothing else. There
        # are no view controls left: the panels are not a viewer any more,
        # they are three crops of one frame whose scale and place the full
        # image already chose, so lock-zoom, reset-all and the per-panel
        # resets had nothing left to reset. Colours live on the channel
        # swatches, display mapping in the floating "Intensity" window.
        self._compare_where_lbl = QLabel(
            "Right-click the full image to take a snapshot here.")
        self._compare_where_lbl.setStyleSheet("color:#888;font-size:10px;")
        ctrl_row.addWidget(self._compare_where_lbl)
        ctrl_row.addStretch()

        # "downsampled xN" -- the same fact the full image's own header
        # states, measured the same way (the provider's level shapes, never
        # 2**level), because a snapshot taken at a coarse level IS the
        # coarse correction and must not be read as the one Save writes.
        self._compare_level_lbl = QLabel("")
        self._compare_level_lbl.setStyleSheet("color:#e5c07b;font-size:10px;")
        self._compare_level_lbl.setVisible(False)
        ctrl_row.addWidget(self._compare_level_lbl)

        self._btn_snapshot_patch = QPushButton("＋ Save as patch")
        self._btn_snapshot_patch.setToolTip(
            "Add this snapshot's level-0 rectangle to the patch list, so "
            "Step 1 can be seeded from what you are looking at.")
        self._btn_snapshot_patch.setStyleSheet(
            "QPushButton{color:#98c379;border:1px solid #4a6f43;"
            "border-radius:3px;padding:2px 8px;font-size:10px;"
            "background:#1a1a1a;}"
            "QPushButton:hover{color:#fff;border-color:#98c379;}"
            "QPushButton:disabled{color:#555;border-color:#333;}"
        )
        self._btn_snapshot_patch.setEnabled(False)
        self._btn_snapshot_patch.clicked.connect(self._save_snapshot_as_patch)
        ctrl_row.addWidget(self._btn_snapshot_patch)

        self._preview_ctrl_row = ctrl_row   # exposed for the header's own tests

        self._btn_show_nucleus.toggled.connect(
            lambda _: self._on_compare_nucleus_toggled())
        self._btn_show_marker.toggled.connect(lambda _: self._refresh_preview_display(keep_zoom=True))
        pvl.addLayout(ctrl_row)

        # ── Display mapping (min, max, gamma) ──────────────────────────
        # Raw intensity in, screen value out: shown = clip((v - min)/(max -
        # min))**gamma, per channel, shared with the full image. Display
        # only: never the h5ad or the corrected zarr.
        # The controls are the Channel Remap tab's "Intensity" inspector
        # ITSELF -- histogram + Min/Max/Gamma + Auto/Reset -- re-parented
        # into a floating window (`show_intensity_window`). The workbench's
        # per-channel params ARE this mapping (`_display_mapping_for`), so
        # there is one set of numbers and one set of controls for both tabs.

        # ── 三联图（同一GraphicsLayoutWidget，保证同步repaint）────────
        self._preview_vbs  = []
        self._preview_imgs = []
        # One nucleus ImageItem per panel, drawn ON TOP of the marker item
        # and ADDED to it (CompositionMode_Plus): marker_colour*i +
        # nucleus_colour*j, per channel, saturating -- the same sum
        # `_make_colored_rgb` computes in numpy, done by the painter
        # instead. See `_refresh_preview_display`. Created on FIRST USE
        # (`_nuc_item`), not here: three more scene items per page made the
        # known pyqtgraph/offscreen crash of the Step0 test suite -- which
        # builds dozens of pages in one process -- deterministic, exactly
        # as constructing the full-image viewer eagerly once did.
        self._preview_nuc_imgs = [None, None, None]
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
            # axisOrder EXPLICITLY, never the process-global
            # `pg.setConfigOptions(imageAxisOrder=...)`: pyqtgraph captures it
            # once, in ImageItem.__init__, and its library default is
            # col-major. Inheriting the global would make this panel's world
            # coordinates x=row / y=column for any entry point that does not
            # run main.py's setConfigOptions first -- and the full-image
            # drill-down converts those coordinates to level-0, so a silent
            # transposition would land the user in the wrong part of the
            # slide. viewer/explore_view.py sets it per item for the same
            # reason.
            # No mouse: a snapshot is a FIXED frame at the full image's own
            # scale. Zooming a panel would put a second, silently different
            # magnification next to the one the header describes, and
            # panning would ask for margin the crop does not contain.
            vb.setMouseEnabled(False, False)
            item = pg.ImageItem(axisOrder="row-major")
            vb.addItem(item)
            self._preview_vbs.append(vb)
            self._preview_imgs.append(item)


        pvl.addWidget(self._preview_gv, stretch=1)

        # 别名兼容
        self._orig_vb,  self._orig_img  = self._preview_vbs[0], self._preview_imgs[0]
        self._top_vb,   self._top_img   = self._preview_vbs[1], self._preview_imgs[1]
        self._cu_vb,    self._cu_img    = self._preview_vbs[2], self._preview_imgs[2]

        self._preview_status = QLabel(
            "Right-click anywhere on the full image to snapshot that spot."
        )
        self._preview_status.setAlignment(Qt.AlignCenter)
        self._preview_status.setWordWrap(True)
        self._preview_status.setStyleSheet("color:#aaa;font-size:10px;")
        pvl.addWidget(self._preview_status)

        # -- the full image, with the compare strip BESIDE it -------------
        #
        # A splitter, not a stack. The two used to be pages of one
        # QStackedWidget, which made them alternatives: looking at a
        # snapshot meant not looking at the slide it came from, and a
        # snapshot's whole meaning is "this spot, in the image above". So
        # the full image is permanently the top pane and the three panels
        # are a strip under it, collapsed until the first right-click and
        # collapsible again from the same toolbar button.
        #
        # Collapsed is `setVisible(False)` on the strip rather than a zero
        # splitter size: a hidden pane cannot be dragged back open by
        # accident, so the button is the only thing that decides.
        self._compare_strip = prev_box
        self._preview_split = QSplitter(Qt.Vertical)
        self._preview_split.addWidget(self._build_full_image_page())
        self._preview_split.addWidget(prev_box)
        self._preview_split.setChildrenCollapsible(False)
        self._preview_split.setStretchFactor(0, 3)
        self._preview_split.setStretchFactor(1, 1)
        prev_box.setVisible(False)
        crl.addWidget(self._preview_split, stretch=3)

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
        # There is no per-channel Process button any more. Computing is the
        # row's checkbox plus the one Process button in Method Parameters,
        # and a second entry that ran ONE channel meant two answers to
        # "what did this page compute" sitting side by side. Apply is what
        # this panel does now: it saves the channel's method and parameters.

        self._apply_btn = QPushButton("Apply")
        self._apply_btn.setToolTip("Save this channel's method + params (no run).")
        self._apply_btn.setStyleSheet(
            "QPushButton{background:#255;color:white;border-radius:4px;"
            "padding:5px 12px;font-weight:bold;}"
            "QPushButton:hover{background:#377;}"
            "QPushButton:disabled{background:#333;color:#555;}"
        )
        self._apply_btn.clicked.connect(self._apply_current_channel_decision)
        btn_row.addWidget(self._apply_btn, stretch=1)
        dl.addLayout(btn_row)

        self._decision_status = QLabel("No decision saved yet.")
        self._decision_status.setWordWrap(True)
        self._decision_status.setStyleSheet("color:#aaa;font-size:10px;")
        dl.addWidget(self._decision_status)

        # v15 mutual visibility: the current channel's LIVE remap state
        # (from the Channel Remap tab) shown on the correction side.
        self._remap_state_lbl = QLabel("Remap: —")
        self._remap_state_lbl.setWordWrap(True)
        self._remap_state_lbl.setStyleSheet("color:#879bb1;font-size:10px;")
        dl.addWidget(self._remap_state_lbl)
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
        # A colour picked in the Channel Remap layer list is the SAME colour
        # store the Background Correction swatches read (and vice versa).
        self._cond_workbench.channel_color_changed.connect(
            self._on_workbench_color_changed)
        # v15: single data-access seam with the SAVE boundary — remap eats
        # only loader-served pixels (saved corrected after Save, raw before,
        # honestly labeled). Lazy per-channel fetch preserved.
        from .preview_source_provider import (
            Step0PreviewSourceProvider, STAGE_CORRECTED)
        self._preview_provider = Step0PreviewSourceProvider(self)
        self._cond_workbench.set_pixel_provider(
            lambda name: self._preview_provider.get_pixels(name, STAGE_CORRECTED))
        self._preview_provider.stage_invalidated.connect(
            self._on_stage_invalidated)
        # Mutual visibility: remap edits surface live in the BG tab's
        # Per-Channel Decision panel.
        self._cond_workbench.params_changed.connect(
            self._on_remap_params_changed)
        # SINGLE SOURCE OF TRUTH: those same params are the display mapping.
        # Every Min/Max/Gamma/Auto/Reset move in the inspector lands here and
        # redraws the compare panels + the full image and its DAPI overlay.
        self._cond_workbench.params_changed.connect(
            self._on_display_mapping_changed)
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

    # ── production correction vs the Explore stack ──────────────────────
    #
    # Both run background correction on the GPU, and cupy is not safe to
    # drive from two places at once (see BatchProcessWorker's own note).
    # So the two are kept apart by a gate in both directions: a production
    # run releases Explore before it starts, and Explore refuses to build
    # while a production run is going.
    #
    # Only the paths that actually reach a GPU worker today are listed.
    # `PreloadWorker` is excluded on purpose -- it is a plain reader (its
    # docstring: "background reader"), no correction. `BackgroundPreviewWorker`
    # is excluded because it is currently UNREACHABLE: its only trigger,
    # `_queue_preview`, has no caller anywhere in the repo. Whoever
    # reconnects it must add it here in the same change.

    # ── full image (Patch Preview page 1) ───────────────────────────────

    def _build_full_image_page(self):
        """The full-image page: a fixed toolbar plus the ONE Explore tab.

        This is the LANDING view of the Background Correction workspace: a
        loaded dataset opens here, on the whole slide, with no patch and no
        Process. The compare panels are the other page of the same stack,
        expanded on demand by the "Compare panels" button.

        The Explore tab is created here and nowhere else -- this page owns
        it. The toolbar lives inside the page, so it is visible exactly when
        the page is, and the way back to the panels can never be hidden by
        the viewer's own placeholder swapping.
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        btn_style = (
            "QPushButton{color:#ddd;border:1px solid #555;border-radius:3px;"
            "padding:2px 8px;font-size:10px;background:#1a1a1a;}"
            "QPushButton:hover{border-color:#aaa;}"
            "QPushButton:disabled{color:#555;border-color:#333;}"
        )
        # The compare panels are no longer the place the user starts from,
        # so this reads as EXPANDING an area rather than going "back".
        back = QPushButton("⊞ Compare panels")
        back.setCheckable(True)
        back.setToolTip("Show or hide the three-panel compare strip under "
                        "the image. A right-click on the image opens it by "
                        "itself and fills it with a snapshot of that spot.")
        back.setStyleSheet(btn_style)
        back.toggled.connect(self._on_compare_strip_toggled)
        self._btn_show_compare = back
        bar.addWidget(back)

        # ── the method switch, on top of the image it changes ───────────
        #
        # A PREVIEW, and nothing else: Original serves raw pixels, TopHat and
        # cuCIM are computed on the fly for the viewport with the channel's
        # current parameters. Nothing is written, no channel becomes
        # "computed", and no checkbox or Method combo is consulted -- every
        # marker channel can be looked at corrected before deciding whether
        # to correct it, which is the whole point of a full-image-first page.
        method_style = (
            "QPushButton{color:#9aa7b4;border:1px solid #3a4a5c;"
            "border-left-width:0px;padding:2px 10px;font-size:10px;"
            "background:#161c25;}"
            "QPushButton:hover{color:#dce5ef;border-color:#61afef;}"
            "QPushButton:checked{color:#0f1620;background:#61afef;"
            "border-color:#61afef;font-weight:bold;}"
            "QPushButton:disabled{color:#4a545e;border-color:#2a323c;}"
        )
        self._full_method_group = QButtonGroup(self)
        self._full_method_group.setExclusive(True)
        self._full_method_buttons = {}
        for source in FULL_IMAGE_SOURCES:
            label = FULL_IMAGE_SOURCE_LABELS[source]
            btn_m = QPushButton(label)
            btn_m.setCheckable(True)
            btn_m.setStyleSheet(method_style)
            btn_m.setToolTip(FULL_IMAGE_SOURCE_TIPS[source])
            btn_m.clicked.connect(
                lambda _checked, src=source: self._on_full_method_clicked(src))
            self._full_method_group.addButton(btn_m)
            self._full_method_buttons[source] = btn_m
            bar.addWidget(btn_m)
        self._full_method_buttons["original"].setChecked(True)

        # The correction of a COARSE pyramid level is not the correction of
        # level 0 -- the structuring element / sigma is scaled with the
        # level, so the background it removes is a different background. The
        # preview is still worth looking at; it just must not be mistaken
        # for the result Save would write.
        self._full_level_hint = QLabel("")
        self._full_level_hint.setStyleSheet("color:#e5c07b;font-size:10px;")
        self._full_level_hint.setVisible(False)
        bar.addWidget(self._full_level_hint)

        self._btn_full_reopen = QPushButton("Reopen full image")
        self._btn_full_reopen.setToolTip(
            "Rebuild the full image for the current channel and the current "
            "parameters. Needed after a correction run released it.")
        self._btn_full_reopen.setStyleSheet(btn_style)
        self._btn_full_reopen.clicked.connect(self._reopen_full_image)
        bar.addWidget(self._btn_full_reopen)

        self._btn_full_fit = QPushButton("⤢ Fit whole slide")
        self._btn_full_fit.setToolTip(
            "Reset THIS view to the whole slide. Separate from going back "
            "to the comparison.")
        self._btn_full_fit.setStyleSheet(btn_style)
        self._btn_full_fit.clicked.connect(self._fit_full_image)
        bar.addWidget(self._btn_full_fit)

        # The two LAYER toggles share one style with an explicit :checked
        # rule. `btn_style` has none, and under it a checked and an
        # unchecked button were pixel-identical -- measured on a live
        # session where both layers had been switched off and the viewer
        # was a black canvas with two buttons that looked switched on. The
        # glyph changes too (● on / ○ off, see `_set_layer_toggle_text`),
        # so the state is readable without relying on colour alone.
        toggle_style = (
            "QPushButton{color:#777;border:1px solid #444;border-radius:3px;"
            "padding:2px 8px;font-size:10px;background:#1a1a1a;}"
            "QPushButton:hover{border-color:#aaa;}"
            "QPushButton:checked{color:#eee;background:#264d26;"
            "border-color:#6bffa0;font-weight:bold;}"
            "QPushButton:disabled{color:#555;border-color:#333;}"
        )
        # Marker on/off. A checkable button rather than a menu entry: it is
        # a two-state thing the user flips while looking at the image.
        self._btn_full_marker = QPushButton("● Marker")
        self._btn_full_marker.setCheckable(True)
        self._btn_full_marker.setChecked(True)
        self._btn_full_marker.setToolTip(
            "Show or hide the marker channel in the full image. Hiding is "
            "display-only: the tiles stay loaded, so turning it back on is "
            "instant and reads nothing.")
        self._btn_full_marker.setStyleSheet(toggle_style)
        self._btn_full_marker.toggled.connect(self._on_full_marker_toggled)
        bar.addWidget(self._btn_full_marker)

        # Nucleus overlay on/off. Separate from the marker switch on
        # purpose: the two layers are added together, so either can be
        # looked at alone.
        self._btn_full_nucleus = QPushButton(
            ("● " if DAPI_LAYER_DEFAULT_ON else "○ ") + "DAPI")
        self._btn_full_nucleus.setCheckable(True)
        # Same default as the compare panels' holder: one state, both views.
        self._btn_full_nucleus.setChecked(DAPI_LAYER_DEFAULT_ON)
        self._btn_full_nucleus.setToolTip(
            "Show or hide the nucleus channel in the full image. It is drawn "
            "ON TOP of the marker and ADDED to it, the same way the compare "
            "panels combine the two. Hiding it also stops requesting its "
            "tiles; showing it again resumes from the current view.")
        self._btn_full_nucleus.setStyleSheet(toggle_style)
        self._btn_full_nucleus.toggled.connect(self._on_full_nucleus_toggled)
        bar.addWidget(self._btn_full_nucleus)

        self._full_source_lbl = QLabel("—")
        self._full_source_lbl.setStyleSheet("color:#61afef;font-size:10px;")
        # What the label says about the SOURCE, kept apart from the hidden-
        # layer hint that `_update_full_source_label` may append to it.
        self._full_source_base_text = "—"
        bar.addWidget(self._full_source_lbl)
        bar.addStretch(1)
        layout.addLayout(bar)

        # The viewer widget itself is created on FIRST USE, not here.
        # Constructing this page is on the critical path of building
        # Step0 -- every construction of the page would otherwise also
        # construct the viewer widget, for a user who may never open the
        # full image. (It also measurably worsened an existing
        # pyqtgraph/offscreen crash in the Step0 test suite, which builds
        # 32 pages in one process.) No new state is needed to defer it:
        # the dataset path it needs already lives in `self.ome_path`.
        self._explore_tab = None
        self._full_image_host = layout
        return page

    def _ensure_explore_tab(self):
        """Create the ONE Explore tab, on first use."""
        if self._explore_tab is not None:
            return self._explore_tab
        self._explore_tab = Step0ExploreTab(
            self, busy_probe=self.production_correction_busy)
        self._full_image_host.addWidget(self._explore_tab, stretch=1)
        # Catch up on the dataset: `set_dataset` may have run before this
        # existed. `ome_path` is None until a slide is loaded, which is
        # exactly the placeholder state the tab starts in anyway.
        if getattr(self, "ome_path", None):
            self._explore_tab.set_dataset(self.ome_path)
        return self._explore_tab

    def _full_image_selection(self, channel=None):
        """`(source, method, params)` for the full image right now.

        The parameters come from the provider's `effective_*` fields -- what
        a preview of that channel would actually use -- never from
        `_channel_params` or a default constant, so there is one answer to
        "what radius is this".

        The nucleus channel is excluded from correction, so it is SHOWN as
        Original whatever the chosen source is; `_full_image_source` keeps
        the user's choice, and moving to an ordinary channel goes back to
        it.
        """
        channel = channel or self.current_channel
        source = self._full_image_source
        if channel and channel == self.nucleus_channel:
            source = "original"
        method = FULL_IMAGE_METHOD[source]
        if method is None:
            return source, None, ()

        provider = self.preview_source_provider
        correction = provider.describe(channel)["correction"] if provider else {}
        key = ("effective_tophat_radius" if method == "tophat"
               else "effective_cucim_sigma")
        value = correction.get(key)
        if value is None:
            # No provider (a half-built page) -- show Original rather than
            # inventing a radius.
            return "original", None, ()
        return source, method, (int(value),)

    def _describe_full_source(self, source, method, params):
        if method is None:
            label = "Original"
        elif method == "tophat":
            label = f"Top-hat radius {params[0]}"
        else:
            label = f"cuCIM sigma {params[0]}"
        if (self.current_channel
                and self.current_channel == self.nucleus_channel
                and self._full_image_source != "original"):
            label += "  (nucleus is excluded from correction)"
        return f"{self.current_channel or '—'} · {label}"

    def _full_image_tint(self, channel=None):
        """The colour the full image should draw `channel` in.

        THE SAME source the compare panels use -- `_channel_colors`, with
        `_marker_color` as the fallback -- so a channel looks the same in
        both places and there is one answer to "what colour is this
        channel". Returns None when there is no channel to colour.
        """
        channel = channel or self.current_channel
        if not channel:
            return None
        return self._channel_color(channel)

    def _on_full_marker_toggled(self, checked):
        """Marker layer on/off, applied to the live stack only.

        Nothing is rebuilt and nothing is requested: the controller fades
        its layers, the tiles stay pooled and cached. If no stack exists
        the button state is simply remembered for the next build.
        """
        self._set_layer_toggle_text(self._btn_full_marker, "Marker", checked)
        self._update_full_source_label()
        explore_tab = getattr(self, "_explore_tab", None)
        stack = explore_tab.stack if explore_tab is not None else None
        if stack is None:
            return
        stack.controller.set_marker_visible(bool(checked))

    def _apply_full_image_display(self, stack):
        """Give a stack the current channel's and the nucleus channel's
        display mapping -- the same numbers the compare panels use. A
        freshly built stack otherwise shows its own percentile seed."""
        if stack is None:
            return
        ch = self.current_channel
        set_marker = getattr(getattr(stack, "controller", None), "set_display_mapping", None)
        if ch and set_marker is not None:
            lo, hi, gamma = self._display_mapping_for(ch)
            set_marker(lo, hi, gamma, channel=ch)
        overlay = getattr(stack, "overlay", None)
        set_nuc = getattr(overlay, "set_display_mapping", None)
        # A hidden overlay gets no mapping: asking for one seeds the nucleus
        # channel's window from the WHOLE SLIDE (a ~165 ms pyramid read) on
        # the GUI thread, for a layer that is drawing nothing. Turning the
        # layer back on applies it then -- see `_on_full_nucleus_toggled`.
        if (set_nuc is not None and self.nucleus_channel
                and self._nucleus_layer_visible()):
            lo, hi, gamma = self._display_mapping_for(self.nucleus_channel)
            set_nuc(lo, hi, gamma)

    @staticmethod
    def _set_layer_toggle_text(button, name, checked):
        """● when the layer is on, ○ when it is off. The stylesheet's
        :checked rule carries the same information in colour; the glyph
        makes it legible without it."""
        button.setText(("● " if checked else "○ ") + name)

    def _hidden_full_layers(self):
        """Names of the full-image layers whose toggle is OFF."""
        hidden = []
        marker = getattr(self, "_btn_full_marker", None)
        if marker is not None and not marker.isChecked():
            hidden.append("Marker")
        nucleus = getattr(self, "_btn_full_nucleus", None)
        if nucleus is not None and not nucleus.isChecked():
            hidden.append("DAPI")
        return hidden

    def _update_full_source_label(self, source=None, method=None, params=()):
        """The toolbar label: what is shown, and -- when a layer toggle is
        off -- which layers are hidden.

        The second part exists because of what a viewer with both layers
        off looks like: a black canvas. Without a word about it that reads
        as "nothing loaded", and the user reaches for Reopen, which
        rebuilds the stack and shows the same black canvas again.
        """
        lbl = getattr(self, "_full_source_lbl", None)
        if lbl is None:
            return
        if method is None and params == () and source is None:
            base = self._full_source_base_text
        else:
            base = self._describe_full_source(source, method, params)
            self._full_source_base_text = base
        hidden = self._hidden_full_layers()
        if len(hidden) == 2:
            base += "   ⚠ both layers hidden -- switch Marker or DAPI on"
        elif hidden:
            base += f"   ({hidden[0]} hidden)"
        lbl.setText(base)

    def _full_image_nucleus_args(self):
        """The nucleus overlay's construction arguments, or `{}` when this
        dataset has no nucleus channel to overlay.

        The colour is `_nuc_color` -- the same one the compare panels
        composite the nucleus with, so the two views agree.
        """
        nucleus = self.nucleus_channel
        if not nucleus:
            return {}
        checked = (self._btn_full_nucleus.isChecked()
                   if hasattr(self, "_btn_full_nucleus") else False)
        return {
            "nucleus_channel": nucleus,
            "nucleus_tint": getattr(self, "_nuc_color", (0.0, 0.5, 1.0)),
            "nucleus_enabled": bool(checked),
        }

    def _full_image_overlay(self):
        """The live nucleus overlay, or None when no stack holds one."""
        explore_tab = getattr(self, "_explore_tab", None)
        stack = explore_tab.stack if explore_tab is not None else None
        return getattr(stack, "overlay", None) if stack is not None else None

    def _on_full_nucleus_toggled(self, checked):
        """Nucleus layer on/off on the live stack.

        Turning it OFF also stops its requests -- an overlay nobody is
        looking at must not keep the IO workers busy -- and turning it back
        ON resumes from the viewer's CURRENT viewport, so no pan is needed
        to get pixels back.
        """
        self._set_layer_toggle_text(self._btn_full_nucleus, "DAPI", checked)
        self._update_full_source_label()
        self._sync_nucleus_row_checkbox(checked)
        overlay = self._full_image_overlay()
        if overlay is None:
            return
        explore_tab = self._explore_tab
        overlay.set_enabled(bool(checked),
                            host=explore_tab.stack.controller)
        if checked:
            # The mapping was skipped while the layer was hidden; give it now,
            # before the first tile of it is painted.
            self._apply_full_image_display(explore_tab.stack)

    # -- the compare strip -------------------------------------------------
    #
    # There is no drill-down left to support. The panels used to be a small
    # viewer you zoomed and then EXPANDED into the full image, which needed
    # the panel's region (`_compare_viewport_l0`), its exact camera
    # (`_compare_panel_camera`, `_vb_screen_geometry`), the geometry that
    # continued it in a differently-sized widget
    # (`_full_view_range_for_panel`) and the deferred chase that re-applied
    # it as Qt handed out that widget's size (`_match_full_image_to_panel`).
    # All five are gone with the gesture: the full image is now the view the
    # user is already in and the panels are a snapshot OF it, so the
    # direction of travel is the other way and needs no camera matching.

    def _compare_strip_visible(self):
        """True when the three compare panels are expanded."""
        strip = getattr(self, "_compare_strip", None)
        return strip is not None and not strip.isHidden()

    def _set_compare_strip_visible(self, visible):
        """Expand or collapse the strip, keeping the toolbar button in step.

        Cheap either way, and it recomputes nothing: the panels keep
        whatever snapshot they hold while collapsed, and the full image
        above them is not touched.
        """
        strip = getattr(self, "_compare_strip", None)
        if strip is None:
            return
        visible = bool(visible)
        strip.setVisible(visible)
        button = getattr(self, "_btn_show_compare", None)
        if button is not None:
            was = button.blockSignals(True)
            button.setChecked(visible)
            button.blockSignals(was)
            button.setText("⊟ Compare panels" if visible
                           else "⊞ Compare panels")
        split = getattr(self, "_preview_split", None)
        if visible and split is not None:
            total = max(split.height(), COMPARE_STRIP_HEIGHT * 2)
            split.setSizes([total - COMPARE_STRIP_HEIGHT,
                            COMPARE_STRIP_HEIGHT])
            # Qt hands geometry out over several event-loop turns, and the
            # panel's SIZE is the size of the crop the very next line of
            # `_take_compare_snapshot` cuts. Measured on the real slide: the
            # first right-click after an expand read the strip's stale
            # geometry and cut a 799x214 frame where 1221x328 was wanted,
            # so the panels showed it magnified 1.5x and the header's
            # "same scale as the image" was false for exactly one click.
            # `activate()` sets the children's geometry NOW, and a visible
            # widget's setGeometry delivers its resize event synchronously,
            # which is what gives the pyqtgraph ViewBoxes their rect.
            layout = strip.layout()
            if layout is not None:
                layout.activate()

    def _on_compare_strip_toggled(self, checked):
        self._set_compare_strip_visible(checked)

    def _on_compare_nucleus_toggled(self):
        """The DAPI checkbox drives the panels as it drives the full image.

        Redrawing is enough for a payload that already carries the nucleus
        channel. A SNAPSHOT cut while the layer was off does not carry it --
        it is not read then, exactly as the full image stops requesting its
        tiles -- so turning the layer on retakes the snapshot at the same
        point rather than leaving the panels claiming there is no nucleus
        there. Turning it off retakes too, so the panels never keep a layer
        the switch says is gone.
        """
        self._refresh_preview_display(keep_zoom=True)
        snapshot = getattr(self, "_compare_snapshot", None)
        payload = self._last_payload
        if not snapshot or not payload or not payload.get("snapshot"):
            return
        carries = payload.get("nucleus_raw") is not None
        wanted = bool(self.nucleus_channel and self._nucleus_layer_visible()
                      and self.nucleus_channel != snapshot.get("channel"))
        if carries == wanted:
            return
        x_l0, y_l0 = snapshot["center_l0"]
        self._take_compare_snapshot(x_l0, y_l0)



    # -- the compare snapshot ---------------------------------------------
    #
    # A right-click on the full image at slide point P fills the three
    # panels with a STATIC crop centred on P, at the pyramid level the full
    # image is on and at its screen scale, each panel showing exactly the
    # region that fits its own widget. Nothing about it is a viewer: there
    # is no camera to move and no zoom to lose, and the second right-click
    # replaces it wholesale.
    #
    # RESIZING the strip therefore does not rescale a snapshot already
    # taken -- the crop was cut for the panel size at the moment of the
    # click, and a resize simply shows it larger or smaller. The next
    # right-click cuts a new one for the new size. That is a deliberate
    # choice over re-cropping from a padded cache: it keeps "what you see is
    # the frame you asked for" true, and there is exactly one code path.
    #
    # And it computes NOTHING in the page's sense: no BatchProcessWorker, no
    # `_preview_cache` entry, no row state, no signature. The metrics panel
    # does show the snapshot's numbers, labelled as a preview, because the
    # question "is this radius helping" is a question about numbers.

    # A panel widget that has never been laid out reports no size. Rather
    # than inventing a scale, the snapshot is cut for this square and the
    # panels then show it fitted -- honest about being approximate, and only
    # reachable in a page that was never shown (offscreen tests).
    _COMPARE_PANEL_FALLBACK_PX = 256.0

    def _full_image_scale(self):
        """Screen pixels per LEVEL-0 slide pixel in the full image, or None.

        Read off the live ViewBox the same way `ExploreController.
        _on_range_changed` reads it -- widget width over world width -- so
        the number the snapshot is cut at is the number the image is drawn
        at.
        """
        explore_tab = getattr(self, "_explore_tab", None)
        stack = getattr(explore_tab, "stack", None) if explore_tab else None
        view = getattr(stack, "view", None)
        vb = getattr(view, "view_box", None)
        if vb is None:
            return None
        try:
            (vx0, vx1), _yr = vb.viewRange()
            width_px = float(vb.width())
        except Exception:                                   # noqa: BLE001
            return None
        world = float(vx1) - float(vx0)
        if not (world > 0 and width_px > 0):
            return None
        scale = width_px / world
        return scale if math.isfinite(scale) and scale > 0 else None

    def _compare_panel_px(self):
        """One compare panel's size in screen pixels, as `(w, h)`."""
        vbs = getattr(self, "_preview_vbs", None) or ()
        if vbs:
            try:
                rect = vbs[0].rect()
                w, h = float(rect.width()), float(rect.height())
                if w > 1.0 and h > 1.0:
                    return (w, h)
            except Exception:                               # noqa: BLE001
                pass
        gv = getattr(self, "_preview_gv", None)
        if gv is not None and gv.width() > 3 and gv.height() > 3:
            return (gv.width() / 3.0, float(gv.height()))
        return (self._COMPARE_PANEL_FALLBACK_PX,
                self._COMPARE_PANEL_FALLBACK_PX)

    def _effective_correction_params(self, channel):
        """`(tophat_radius, cucim_sigma)` for `channel` -- the numbers a
        preview of it would use, from the provider's `effective_*` fields,
        with the global sliders as the fallback for a half-built page."""
        provider = getattr(self, "preview_source_provider", None)
        radius, sigma = None, None
        if provider is not None:
            try:
                correction = provider.describe(channel)["correction"]
                radius = correction.get("effective_tophat_radius")
                sigma = correction.get("effective_cucim_sigma")
            except Exception:                               # noqa: BLE001
                radius = sigma = None
        if radius is None:
            radius = self._tophat_slider.value()
        if sigma is None:
            sigma = self._cucim_slider.value()
        return int(radius), int(sigma)

    def _snapshot_from_caches(self, stack, channel, level, y0, x0, h, w):
        """Whatever the viewer already holds for this exact region.

        The fast path: the tiles under the click were, for the most part,
        read or computed to put them on screen in the first place. A key is
        only trusted when it is the controller's OWN key for that tile --
        same source identity, channel, method, level-scaled parameter and
        quality -- so nothing from another selection can be pasted in, and
        a single missing tile abandons that array rather than leaving a
        hole. Returns `{}` when nothing whole could be assembled.
        """
        out = {}
        controller = getattr(stack, "controller", None)
        scheduler = getattr(stack, "scheduler", None)
        if controller is None or scheduler is None:
            return out
        grid = getattr(controller, "grid", None)
        tile_size = int(getattr(grid, "tile_size", 0) or 0)
        if tile_size <= 0:
            return out
        if getattr(controller, "channel", None) != channel:
            return out

        def assemble(cache, key_for):
            if cache is None:
                return None
            canvas = None
            for ty in range(y0 // tile_size, (y0 + h - 1) // tile_size + 1):
                for tx in range(x0 // tile_size, (x0 + w - 1) // tile_size + 1):
                    try:
                        arr = cache.get(key_for(tx, ty))
                    except Exception:                       # noqa: BLE001
                        return None
                    if arr is None:
                        return None
                    arr = np.asarray(arr)
                    if canvas is None:
                        canvas = np.zeros((h, w), np.float32)
                    ty0, tx0 = ty * tile_size, tx * tile_size
                    sy0, sx0 = max(0, y0 - ty0), max(0, x0 - tx0)
                    dy0, dx0 = max(0, ty0 - y0), max(0, tx0 - x0)
                    ph = min(arr.shape[0] - sy0, h - dy0)
                    pw = min(arr.shape[1] - sx0, w - dx0)
                    if ph <= 0 or pw <= 0:
                        return None
                    canvas[dy0:dy0 + ph, dx0:dx0 + pw] = (
                        arr[sy0:sy0 + ph, sx0:sx0 + pw])
            return canvas

        raw = assemble(getattr(scheduler, "raw_cache", None),
                       lambda tx, ty: controller._make_raw_key(tx, ty, level))
        if raw is not None:
            out["original_raw"] = raw.astype(np.float32, copy=False)
        method = getattr(controller, "method", None)
        if method in ("tophat", "cucim"):
            radius, sigma = self._effective_correction_params(channel)
            base = radius if method == "tophat" else sigma
            if tuple(int(v) for v in getattr(controller, "params", ())) == (base,):
                arr = assemble(
                    getattr(scheduler, "corrected_cache", None),
                    lambda tx, ty: controller._make_correction_key(
                        tx, ty, level))
                if arr is not None:
                    out[f"{method}_raw"] = arr
        return out

    def _connect_full_image_right_click(self, stack):
        """Once per stack: a right-click on the image takes a snapshot."""
        if stack is None or getattr(stack, "_snapshot_connected", False):
            return
        try:
            stack.view.sigRightClicked.connect(self._on_full_image_right_click)
        except (AttributeError, RuntimeError, TypeError):
            return
        stack._snapshot_connected = True

    def _on_full_image_right_click(self, x_l0, y_l0):
        self._take_compare_snapshot(float(x_l0), float(y_l0))

    def _take_compare_snapshot(self, x_l0, y_l0, _expanded=False):
        """Fill the three panels with a static crop centred on `(x, y)`.

        Order matters: the strip is expanded FIRST, because the crop's SIZE
        is the panel's size and a collapsed panel has none. Then the level,
        the scale and the centre fix the rectangle, and the only thing left
        to do is read and correct it.

        Expanding is not instantaneous, which is what `_expanded` is for. Qt
        hands geometry out over event-loop turns, and pyqtgraph re-lays its
        ViewBoxes out when the graphics widget is resized -- so on the click
        that OPENS the strip, the panels still report the size they had
        while collapsed. Measured on the real slide: the first right-click
        cut a 799x214 level-1 frame where 1221x328 was wanted, and the
        panels showed it 1.5x magnified while the header said "the full
        image's own scale". So that click expands the strip, gives Qt one
        turn, and comes back. Every later click, with the strip already
        open, runs straight through -- there is no timer in the common
        path, and this one is a single `singleShot(0)`, not a wait loop.
        """
        explore_tab = getattr(self, "_explore_tab", None)
        stack = getattr(explore_tab, "stack", None) if explore_tab else None
        channel = self.current_channel
        if stack is None or not channel:
            return None
        busy = self.production_correction_busy()
        if busy:
            self._preview_status.setText(
                f"A {busy} run is using the GPU — snapshot not taken.")
            return None
        if not self._compare_strip_visible() and not _expanded:
            self._set_compare_strip_visible(True)
            self._preview_status.setText("Taking snapshot…")
            QTimer.singleShot(0, lambda: self._take_compare_snapshot(
                x_l0, y_l0, _expanded=True))
            return None
        provider, controller = stack.provider, stack.controller
        scale = self._full_image_scale()
        if scale is None:
            return None

        self._set_compare_strip_visible(True)
        pw_px, ph_px = self._compare_panel_px()

        level = int(getattr(controller, "level", 0) or 0)
        try:
            ds = float(provider.level_downsample(level))
            lh, lw = (int(v) for v in provider.level_shape(level))
        except Exception:                                   # noqa: BLE001
            return None
        if not (ds > 0 and lh > 0 and lw > 0):
            return None
        # The panel's own extent, in the pixels the crop is read in:
        # `scale * ds` is screen pixels per LEVEL pixel.
        #
        # Capped at the level, which is the one case where the panel does
        # not come out at the image's scale -- there is simply no more
        # slide to put in it. On the real slide, fitted to the whole thing
        # at level 3 (a x64 level, 555 px wide), the panel wants 901 and
        # gets all 555, so it shows the whole width magnified rather than
        # inventing background. Anywhere the level is bigger than the
        # panel -- every case a user tunes a radius in -- the two scales
        # agree to the rounding below.
        w = max(1, min(lw, int(round(pw_px / (scale * ds)))))
        h = max(1, min(lh, int(round(ph_px / (scale * ds)))))
        x0 = int(round(x_l0 / ds)) - w // 2
        y0 = int(round(y_l0 / ds)) - h // 2
        # Near an edge the frame slides back INSIDE the level rather than
        # being shrunk: a smaller crop would be a different scale, which is
        # the one thing this must not change. The centre moves instead,
        # which is visible.
        x0 = max(0, min(x0, lw - w))
        y0 = max(0, min(y0, lh - h))
        rect_l0 = (x0 * ds, y0 * ds, w * ds, h * ds)

        radius, sigma = self._effective_correction_params(channel)
        nucleus = (self.nucleus_channel
                   if (self.nucleus_channel and self._nucleus_layer_visible()
                       and self.nucleus_channel != channel)
                   else None)
        cached = self._snapshot_from_caches(stack, channel, level,
                                            y0, x0, h, w)
        self._compare_snapshot = {
            "rect_l0": rect_l0, "level": level, "channel": channel,
            "center_l0": (float(x_l0), float(y_l0)),
            "region": (y0, x0, h, w), "cached": sorted(cached),
        }
        self._compare_where_lbl.setText(
            f"Snapshot at ({int(y_l0)}, {int(x_l0)}) · level {level} · "
            f"{w}×{h} px")
        self._update_compare_level_label(level)
        self._preview_status.setText("Taking snapshot…")

        self._cancel_compare_snapshot()
        self._compare_snapshot_req += 1
        worker = CompareSnapshotWorker(
            self._compare_snapshot_req, provider, controller.compute,
            channel, nucleus, level, y0, x0, h, w, radius, sigma,
            cached=cached, parent=self)
        worker.done.connect(self._gen_slot(self._on_compare_snapshot_done))
        worker.failed.connect(self._gen_slot(self._on_compare_snapshot_failed))
        self._compare_snapshot_worker = worker
        worker.start()
        return self._compare_snapshot

    def _cancel_compare_snapshot(self):
        """Let a snapshot in flight finish and stop listening to it.

        There is nothing to interrupt -- one read plus two kernels -- so it
        is disconnected and waited for rather than killed, which keeps the
        provider alive until its last reader is done.
        """
        worker = getattr(self, "_compare_snapshot_worker", None)
        self._compare_snapshot_worker = None
        if worker is None:
            return
        try:
            worker.done.disconnect()
            worker.failed.disconnect()
        except (TypeError, RuntimeError):
            pass
        if worker.isRunning():
            worker.wait(5000)

    def _on_compare_snapshot_failed(self, req_id, message):
        if int(req_id) != int(self._compare_snapshot_req):
            return
        self._preview_status.setText(f"Snapshot failed: {message}")

    def _on_compare_snapshot_done(self, req_id, payload):
        """Paint a finished snapshot. Bookkeeping-free on purpose.

        `_last_payload` is what the panels draw, so it is set; the
        `_preview_cache`, `_computed_channels` and signature bookkeeping --
        everything Process and Save read as a RESULT -- is not touched, and
        the metrics say "preview" so their numbers cannot be mistaken for a
        computed channel's.
        """
        if int(req_id) != int(self._compare_snapshot_req):
            return
        snapshot = self._compare_snapshot or {}
        payload = dict(payload)
        payload["snapshot"] = True
        payload["snapshot_rect_l0"] = snapshot.get("rect_l0")
        payload["snapshot_level"] = snapshot.get("level")
        self._last_payload = payload
        self._refresh_preview_display()
        for label, key, name in (
                (self._metrics_original, "original_metrics", "Original"),
                (self._metrics_tophat, "tophat_metrics", "TopHat"),
                (self._metrics_cucim, "cucim_metrics", "cucim")):
            metrics = payload.get(key)
            label.setText(self._metric_text(name, metrics) + "   (preview)"
                          if metrics else f"{name} → —")
        self._btn_snapshot_patch.setEnabled(True)
        rect = snapshot.get("rect_l0") or (0, 0, 0, 0)
        self._preview_status.setText(
            "Snapshot — a preview of this spot, not a computed result. "
            f"Level-0 rectangle {int(rect[1])}..{int(rect[1] + rect[3])} × "
            f"{int(rect[0])}..{int(rect[0] + rect[2])}.")

    def _update_compare_level_label(self, level):
        """"downsampled xN" on the strip, exactly while it is true."""
        label = getattr(self, "_compare_level_lbl", None)
        if label is None:
            return
        if level is None or int(level) < 1:
            label.setVisible(False)
            label.setText("")
            return
        n = self._full_image_level_downsample(int(level))
        label.setText(f"⚠ downsampled ×{n}")
        label.setToolTip(
            f"This snapshot was cut from pyramid level {level}: one pixel "
            f"covers {n}×{n} level-0 pixels, and the correction ran on those "
            "pixels with a radius/sigma scaled to match. Zoom the full image "
            "in and right-click again to compare at full resolution.")
        label.setVisible(True)

    def _save_snapshot_as_patch(self):
        """Add the snapshot's level-0 rectangle to the patch list.

        The one place a snapshot leaves anything behind, and it leaves a
        BOOKMARK: a patch is where Step 1 is seeded from, so "I want to work
        here" is worth recording. It computes nothing and marks nothing.
        """
        snapshot = getattr(self, "_compare_snapshot", None)
        rect = (snapshot or {}).get("rect_l0")
        if not rect:
            return None
        x0, y0, w, h = (int(round(float(v))) for v in rect)
        coords = (y0, y0 + h, x0, x0 + w)
        overview = getattr(self, "overview", None)
        adder = getattr(overview, "add_patch_rect", None)
        if callable(adder):
            adder(*coords)
        else:
            self._on_patches_changed(list(self.patches) + [coords])
        self._preview_status.setText(
            f"Saved as patch P{len(self.patches)}  "
            f"[{coords[1] - coords[0]}x{coords[3] - coords[2]}px]")
        return coords


    def _enter_full_image_landing(self):
        """Open the full image as the LANDING view of a freshly loaded slide.

        Raw pixels, the first marker channel, the whole slide -- and no patch
        and no Process anywhere in the path. This is the one entry that does
        not come from a user gesture, so it is also the one that must not
        assume anything is already set up: `_rebuild_channel_list` has just
        chosen `current_channel`, and everything else (decision panel, ⤢
        buttons, method switch) is brought into line here rather than being
        left to whichever handler happens to run next.

        Silent when there is nothing to show (no loader, no channel, a
        correction run holding the GPU): `_show_full_image` refuses on its
        own terms and the placeholder keeps saying why.
        """
        if getattr(self, "_preview_split", None) is None:
            return
        self._full_image_source = "original"

        self._update_decision_ui()
        self._update_full_method_buttons()
        self._show_full_image()

    def _reopen_full_image(self):
        """Rebuild the full image for the current channel and parameters.

        Needed only when there is no stack to show (a failed build, or a
        view that was never opened from a compare panel); a production run
        no longer takes the stack away, it suspends and resumes it in
        place, so after a run this is a no-op re-sync. The camera is left
        where it is.
        """
        self._show_full_image()

    def _watch_production_worker(self, worker):
        """Bring the full image back when `worker` -- a production run that
        released it -- has finished, whichever way it finished.

        The PHYSICAL end of the thread, not the worker's own
        all_done/canceled/error trio: those are emitted from INSIDE `run()`,
        while `isRunning()` is still true, so a rebuild started from them
        would be refused by the busy gate. `QThread.finished` is emitted as
        the thread winds down, after `isRunning()` has gone false, and it
        fires exactly once whether the run completed, was stopped or failed.

        Bound off `QtCore.QThread` EXPLICITLY rather than as
        `worker.finished`. `WsiCorrectionWorker` declares a business signal
        of its own called `finished(str, dict)`, which shadows the base
        class's: `worker.finished` on that worker is the business signal,
        emitted from inside `run()` with `isRunning()` still true, so
        connecting to it would rebuild into the busy gate's refusal and
        never get another chance. Reaching past the subclass attribute is
        the whole point of this helper, and it costs the other workers
        nothing -- they do not shadow it, and the bound base signal is the
        same object they would have given anyway.
        """
        physical_finished = QtCore.QThread.finished.__get__(
            worker, type(worker))
        physical_finished.connect(
            self._gen_slot(self._on_production_worker_finished))

    def _on_production_worker_finished(self):
        """A production run that suspended the viewer is over: resume it.

        The stack was never torn down, so resuming is cheap wherever the
        view is -- it unlocks the camera and re-issues only the tiles the
        current viewport is missing, which after an unchanged viewport is
        none. Nothing is rebuilt and nothing flashes. Then, if the
        full-image page is on screen, the current selection is re-applied
        through `_show_full_image`: an Apply changes the parameters, and a
        parameter change is a `set_selection` on the live stack, not a new
        stack.

        Nothing happens while ANOTHER production run is still going -- the
        gate that suspended the viewer still holds -- or when the viewer was
        not suspended in the first place (never opened, or torn down for a
        dataset switch).
        """
        explore_tab = getattr(self, "_explore_tab", None)
        if explore_tab is None or not getattr(explore_tab, "released", False):
            return
        if self.production_correction_busy():
            return
        explore_tab.resume_from_production()
        if self._full_image_visible():
            self._show_full_image()

    def _fit_full_image(self):
        """Reset the full-image view to the whole slide.

        Only this view, and only when there is one -- it is not "back to
        compare" under another name.
        """
        explore_tab = self._explore_tab
        stack = explore_tab.stack if explore_tab is not None else None
        if stack is None:
            return
        h0, w0 = stack.provider.level_shape(0)
        stack.view.view_box.setRange(xRange=(0, w0), yRange=(0, h0),
                                     padding=0)

    def _full_image_visible(self):
        """True when the full image is a live thing on screen.

        It no longer means "the stack is on page 1": the full image is not a
        page any more, it is the permanent top pane of the Background
        Correction workspace, so the only thing that can make it absent is
        having no viewer stack -- never opened, torn down for a dataset
        switch, or a build that was refused. Every caller wants exactly
        that: do not sync a channel into, publish a viewport rectangle from,
        or navigate, a viewer that does not exist.
        """
        explore_tab = getattr(self, "_explore_tab", None)
        return (explore_tab is not None
                and getattr(explore_tab, "stack", None) is not None)


    def _show_full_image(self, viewport_l0=None):
        """Ask the viewer for the current selection, if it may be asked.

        `viewport_l0` is passed ONLY by the ⤢ button (see
        `_enter_full_image`). The channel-sync and reopen callers leave it
        None on purpose: the user may have panned somewhere inside the full
        image, and a channel change or a post-run rebuild must not yank
        them back to the compare patch.

        While a production correction is running the viewer has been
        released and must not be rebuilt; its placeholder already says so,
        and `busy_probe` would refuse anyway. Returning early keeps that
        message rather than replacing it with a second refusal.
        """
        if not self.current_channel:
            self._update_full_method_buttons()
            return
        source, method, params = self._full_image_selection()
        self._update_full_source_label(source, method, params)
        self._update_full_method_buttons()
        if self.production_correction_busy():
            return
        explore_tab = self._ensure_explore_tab()
        accepted = explore_tab.show_source(self.current_channel, method,
                                           params,
                                           viewport_l0=viewport_l0,
                                           tint=self._full_image_tint(),
                                           nucleus=self._full_image_nucleus_args())
        # The marker toggle is page state, so a freshly built stack has to
        # be told about it -- a build always comes up visible.
        stack = explore_tab.stack if accepted else None
        if stack is not None and hasattr(self, "_btn_full_marker"):
            stack.controller.set_marker_visible(
                self._btn_full_marker.isChecked())
        overlay = getattr(stack, "overlay", None) if stack is not None else None
        if overlay is not None and hasattr(self, "_btn_full_nucleus"):
            overlay.set_enabled(self._btn_full_nucleus.isChecked(),
                                host=stack.controller)
        self._apply_full_image_display(stack)
        # The Tissue Preview follows the full image's camera while it is up.
        self._connect_full_image_right_click(stack)
        self._connect_full_image_view_rect(stack)
        self._update_full_image_view_rect()
        # The pyramid level the new selection lands on decides whether the
        # coarse-preview hint applies.
        self._update_full_level_hint()

    def _sync_full_image_to_channel(self):
        """Called at the END of a channel change.

        Only when the full image is actually on screen: switching a hidden
        viewer costs a synchronous overview read for something nobody is
        looking at. `_show_full_image` refuses on its own while a production
        run holds the GPU, so a rebuild can never undo the release that run
        was given.
        """
        if not self._full_image_visible():
            return
        self._show_full_image()

    # ── the full image's own method switch ──────────────────────────────

    def _on_full_method_clicked(self, source):
        """Original / TopHat / cuCIM on top of the full image.

        The camera is deliberately NOT touched: `_show_full_image` is called
        without a viewport, so the controller keeps the view where it is and
        only swaps the selection. Where that method's corrected tiles or its
        corrected floor are still in the stack's caches (they are keyed by
        method + parameter, not merely by channel), the swap reuses them and
        nothing is recomputed.
        """
        if source not in FULL_IMAGE_METHOD:
            return
        if source != "original" and self._full_image_preview_blocked():
            # Nothing to preview a correction OF. Put the switch back where
            # it was rather than leaving it claiming a state that is not on
            # screen.
            self._update_full_method_buttons()
            return
        self._full_image_source = source
        self._update_full_method_buttons()
        self._show_full_image()

    def _full_image_preview_blocked(self):
        """True when a corrected preview cannot be shown for the current
        channel: no channel at all, or the nucleus reference channel, which
        is never background-corrected."""
        ch = self.current_channel
        return (not ch) or ch == self.nucleus_channel

    def _update_full_method_buttons(self):
        """Reflect `_full_image_source` and what the current channel allows.

        Checked state is driven from the page's state, never left to the
        click: `_show_full_image` can refuse (a busy GPU, no channel), and a
        button that stayed pressed would be claiming something false.
        """
        buttons = getattr(self, "_full_method_buttons", None)
        if not buttons:
            return
        blocked = self._full_image_preview_blocked()
        ch = self.current_channel
        shown = "original" if blocked else self._full_image_source
        group = getattr(self, "_full_method_group", None)
        if group is not None:
            group.setExclusive(False)
        for source, button in buttons.items():
            enabled = bool(ch) and (source == "original" or not blocked)
            button.setEnabled(enabled)
            button.setChecked(source == shown)
            if source != "original" and blocked and ch:
                button.setToolTip(
                    "The nucleus channel is the reference overlay and is "
                    "never background-corrected.")
            else:
                button.setToolTip(FULL_IMAGE_SOURCE_TIPS[source])
        if group is not None:
            group.setExclusive(True)
        self._update_full_level_hint()

    def _full_image_level(self):
        """The pyramid level the full image is currently displaying, or
        None when there is no live stack to ask."""
        explore_tab = getattr(self, "_explore_tab", None)
        stack = explore_tab.stack if explore_tab is not None else None
        controller = getattr(stack, "controller", None)
        level = getattr(controller, "level", None)
        return None if level is None else int(level)

    def _full_image_level_downsample(self, level):
        """How many level-0 pixels one pixel of `level` covers, as an int.

        Measured from the provider's own level shapes rather than assumed to
        be 2**level: a pyramid may be built with any factor, and the number
        in the hint has to be the one the user is actually looking through.
        """
        explore_tab = getattr(self, "_explore_tab", None)
        stack = explore_tab.stack if explore_tab is not None else None
        provider = getattr(stack, "provider", None)
        shape = getattr(provider, "level_shape", None)
        if shape is None:
            return 2 ** int(level)
        try:
            h0 = float(shape(0)[0])
            hl = float(shape(int(level))[0])
            if hl > 0:
                return max(1, int(round(h0 / hl)))
        except Exception:                               # noqa: BLE001
            pass
        return 2 ** int(level)

    def _update_full_level_hint(self):
        """Show the coarse-level warning exactly while it is true.

        Only for a CORRECTED preview: Original at level 3 is simply a
        downsampled image of the same pixels, which needs no warning.
        """
        hint = getattr(self, "_full_level_hint", None)
        if hint is None:
            return
        level = self._full_image_level()
        corrected = (self._full_image_source != "original"
                     and not self._full_image_preview_blocked())
        if not corrected or level is None or level < FULL_IMAGE_COARSE_LEVEL:
            hint.setVisible(False)
            hint.setText("")
            return
        n = self._full_image_level_downsample(level)
        hint.setText(f"⚠ downsampled ×{n} preview — zoom in for "
                     f"full-resolution correction")
        hint.setToolTip(
            f"At pyramid level {level} the correction runs on pixels "
            f"box-downsampled ×{n}, with the radius/sigma scaled to match. "
            "That is a different background from the level-0 correction "
            "Save writes; zoom in to compare like with like.")
        hint.setVisible(True)

    def production_correction_busy(self):
        """Name of the production correction task now running, else None.

        Read-only; starts nothing and cancels nothing. Checked per worker
        with `isRunning()` rather than by the presence of a handle:
        `_ondemand_workers` only ever grew (nothing removed finished
        entries), so its length said nothing about whether work is live.
        It is empty for good now -- on-demand computing is gone -- but the
        loop stays: proving no handle can ever land there is a stronger
        claim than checking.
        """
        worker = getattr(self, "_batch_worker", None)
        if worker is not None and worker.isRunning():
            return "patch background correction"
        for worker in getattr(self, "_ondemand_workers", ()) or ():
            if worker.isRunning():
                return "on-demand background correction"
        worker = getattr(self, "_wsi_worker", None)
        if worker is not None and worker.isRunning():
            return "whole-slide correction (Save)"
        return None

    def _release_explore_for_production(self, reason):
        """Suspend the Explore stack and WAIT for it, before a GPU run.

        `ExploreController.suspend_for_production` must not return while a
        corrected-floor computation is still on the GPU, which is the whole
        point of the hand-off. It therefore blocks the GUI thread -- a
        floor job takes 0.4-1.1s, so a hand-off landing early in one blocks
        for most of it. No progress UI is attempted: repainting during a
        synchronous block would need event re-entry, and re-entering the
        event loop here is how a hand-off turns into a second GPU user.

        Idempotent, and a no-op when no stack exists. The stack is resumed
        in place by `_on_production_worker_finished` when the run ends.
        """
        explore_tab = getattr(self, "_explore_tab", None)
        if explore_tab is None or explore_tab.stack is None:
            return
        t0 = time.perf_counter()
        explore_tab.release_for_production(reason)
        print(f"[step0] suspended Explore for {reason} in "
              f"{(time.perf_counter() - t0) * 1000:.0f} ms", flush=True)

    @property
    def preview_source_provider(self):
        """The page's ONE `Step0PreviewSourceProvider`, read-only.

        The single data-access seam between this page and anything that
        wants to know what correction thinks about a channel (see that
        module's docstring). Exposed so a consumer does not have to reach
        for `_preview_provider`, and as a property with no setter so it
        cannot be swapped for a second instance -- there is exactly one, and
        the remap workbench's pixel source is bound to it.

        Returns None before the conditioning tab has been built (the
        provider is created there, partway through `__init__`), which is the
        same guard the page's own `_refresh_remap_state_label` uses.
        """
        return getattr(self, "_preview_provider", None)

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
        # v15 user feedback: the BG Channels column starts at 4/3 of the old
        # default (was 2x, then trimmed to 2/3 of that). Still draggable and
        # width-synced with the Remap tab. Row-internal geometry (name/combo
        # spacing) is independent of this and unchanged.
        self._left_col_width = (4 * (max(wa, wb, 120) + 4)) // 3
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

    # ── v15 live correction<->remap interaction ────────────────────────────
    def _on_stage_invalidated(self, channel, _stage):
        """A live corrected preview landed or a method changed: the remap view
        must re-pull that channel from the provider (no Save involved)."""
        wb = getattr(self, "_cond_workbench", None)
        if wb is not None and wb.has_channel_data():
            wb.invalidate_channel_pixels(channel)

    def _on_remap_params_changed(self, channel):
        """Remap edits are visible live on the correction side."""
        self._refresh_remap_state_label(channel)

    def _refresh_remap_state_label(self, channel=None):
        lbl = getattr(self, "_remap_state_lbl", None)
        provider = getattr(self, "_preview_provider", None)
        if lbl is None or provider is None:
            return
        ch = self.current_channel
        if not ch or (channel is not None and channel != ch):
            return
        info = provider.describe(ch)
        r = info["remap"]
        if r["min"] is None and r["max"] is None:
            lbl.setText("Remap: not tuned yet")
            return
        fmt = lambda v: "—" if v is None else f"{v:.4g}"
        adj = " (user-adjusted)" if r["user_adjusted"] else ""
        lbl.setText(
            f"Remap: min {fmt(r['min'])} · max {fmt(r['max'])} · "
            f"γ {fmt(r['gamma'])}{adj} — on "
            f"{info['served_corrected_stage']}")

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
        # Generation is captured HERE, at connection time (see _gen_slot); the
        # worker's own `gen` argument only guards a preload restart within the
        # SAME dataset, not a dataset switch.
        worker.channel_loaded.connect(self._gen_slot(self._on_preload_channel))
        worker.finished_gen.connect(self._gen_slot(self._on_preload_finished))
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
        # ONE colour store: the page answers for every channel (a user pick if
        # there is one, else the same palette-by-index default the workbench
        # would have chosen, DAPI's own blue for the nucleus). Passing the whole
        # map means the two lists cannot drift apart on the first load either.
        colors = {ch: self._channel_color_hex(ch) for ch in channels}
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
                # Honest per-channel note shown in the workbench: saved
                # corrected vs raw-unsaved (the Save boundary, v15 contract).
                "source_note": (self._preview_provider.source_note(ch)
                                if getattr(self, "_preview_provider", None)
                                else ""),
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
            # A click on the tissue -> the full image jumps there.
            popup.overview.navigate_requested.connect(self._on_tissue_navigate)
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

    # ── the floating "Intensity" window ─────────────────────────────────────
    #  A SECOND, separate floating window (never inside the Tissue Navigator,
    #  which must stay navigation-only). What it hosts is NOT a re-implementation
    #  of the Channel Remap inspector -- it IS that inspector: the workbench
    #  hands its "Intensity" panel over (`detach_inspector`) and keeps driving
    #  it, so histogram, Min/Max/Gamma, Auto and Reset behave here exactly as
    #  they do in the Channel Remap tab, on the same params.
    def _ensure_intensity_window(self):
        win = getattr(self, "_intensity_window", None)
        if win is not None:
            return win
        wb = getattr(self, "_cond_workbench", None)
        if wb is None:
            return None
        # Feed the workbench the current dataset/patch first: opening this
        # window is engaging the Channel Remap machinery, exactly as entering
        # its tab is (see `_engage_conditioning_workbench`).
        self._engage_conditioning_workbench()
        panel = wb.detach_inspector()
        if panel is None:
            return None
        win = QWidget(
            self,
            Qt.Window
            | Qt.WindowMinimizeButtonHint
            | Qt.WindowMaximizeButtonHint
            | Qt.WindowCloseButtonHint
            | Qt.WindowStaysOnTopHint,
        )
        win.setWindowTitle("Intensity")
        win.setStyleSheet("background:#1c1c1c;")
        win.setMinimumWidth(260)
        win.resize(320, 460)
        lay = QVBoxLayout(win)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(panel)
        self._intensity_window = win
        self._intensity_panel = panel
        self._sync_intensity_to_channel()
        return win

    def intensity_panel(self):
        """The workbench inspector widget hosted by the floating window, or
        None while the window has never been opened."""
        return getattr(self, "_intensity_panel", None)

    def show_intensity_window(self):
        win = self._ensure_intensity_window()
        if win is None:
            return None
        self._sync_intensity_to_channel()
        win.show()
        win.raise_()
        return win

    def toggle_intensity_window(self):
        win = self._ensure_intensity_window()
        if win is None:
            return None
        if win.isVisible():
            win.hide()
        else:
            self._sync_intensity_to_channel()
            win.show()
            win.raise_()
        return win

    def _engage_conditioning_workbench(self):
        """Populate the Channel Remap workbench for the current dataset/patch
        WITHOUT showing its tab.

        The workbench used to be fed only when its tab was entered
        (`_on_step0_tab_changed`), so on a real slide the floating Intensity
        window opened empty: no channel set, no params, no histogram pixels,
        and `_display_mapping_for` silently fell back to the page-level
        entry -- i.e. the single source of truth was not in effect.

        This is the tab's OWN engagement path, not a copy of it: the same
        guard (`has_channel_data`) and the same loader
        (`_sync_step0_to_workbench`, which sets `_conditioning_in_use` so
        later patch switches keep it fresh). It reads pixels only -- eagerly
        for the active channel, lazily for the rest through the workbench's
        pixel provider -- and starts no background correction.
        """
        wb = getattr(self, "_cond_workbench", None)
        if wb is None or not self.loader or not self.patches:
            return False
        if not wb.has_channel_data():
            self._sync_step0_to_workbench()
        return wb.has_channel_data()

    def _sync_intensity_to_channel(self):
        """Point the inspector at the channel the user is editing: the channel
        the Background Correction tab is SHOWING, or DAPI while its reference
        row is selected (`_inspector_channel`).

        The histogram it draws is the WORKBENCH's own pixels for that channel,
        i.e. the saved-corrected-or-raw preview patch served by
        `Step0PreviewSourceProvider` -- not the compare panels' payload.
        Activating the channel is what pulls those pixels in (the workbench's
        `_ensure_loaded` calls the provider), so the histogram fills itself.
        """
        wb = getattr(self, "_cond_workbench", None)
        ch = self._inspector_channel or self.current_channel
        if wb is None or not ch:
            return
        # Engage the workbench when the Intensity window is up (its contents
        # must follow the selection) or when it is already carrying data.
        if getattr(self, "_intensity_window", None) is not None:
            self._engage_conditioning_workbench()
        # The histogram is filled with the channel's colour by the activation
        # itself, so the store's answer has to be in the workbench BEFORE it
        # activates -- otherwise the curve would come up in whatever colour the
        # workbench happened to hold.
        self._push_color_to_workbench(ch, self._channel_color(ch))
        wb.set_active_channel(ch)

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

    def _on_tissue_navigate(self, y, x):
        """A click on the Tissue Preview at full-image `(y, x)`.

        With the full image on screen: move its camera so the clicked spot is
        centred, keeping the current viewport SIZE (the user's zoom), clamped
        to the slide. Nothing else changes -- selection, layers, parameters.
        Ignored while a production run holds the camera (`suspended`), and
        ignored when the full image is not on screen: the compare panels show
        a fixed patch and have no camera to move.
        """
        if not self._full_image_visible():
            return
        explore_tab = getattr(self, "_explore_tab", None)
        stack = explore_tab.stack if explore_tab is not None else None
        if stack is None:
            return
        controller = stack.controller
        if getattr(controller, "suspended", False):
            return
        h0, w0 = (int(v) for v in stack.provider.level_shape(0))
        bbox = getattr(controller, "_current_bbox", None)
        if bbox is not None:
            by0, bx0, by1, bx1 = (int(v) for v in bbox)
            h, w = max(1, by1 - by0), max(1, bx1 - bx0)
        else:
            h, w = FULL_IMAGE_JUMP_DEFAULT_SIZE, FULL_IMAGE_JUMP_DEFAULT_SIZE
        if h >= FULL_IMAGE_JUMP_WHOLE_SLIDE_FRACTION * h0 \
                or w >= FULL_IMAGE_JUMP_WHOLE_SLIDE_FRACTION * w0:
            # The view is (nearly) the whole slide: keeping that size would
            # make the jump invisible. A click on the whole slide means
            # "zoom in there".
            h, w = FULL_IMAGE_JUMP_DEFAULT_SIZE, FULL_IMAGE_JUMP_DEFAULT_SIZE
        h, w = min(h, h0), min(w, w0)
        y0 = min(max(0, int(y) - h // 2), max(0, h0 - h))
        x0 = min(max(0, int(x) - w // 2), max(0, w0 - w))
        controller.jump_to(y0, x0, w, h)
        self._update_full_image_view_rect()

    def _connect_full_image_view_rect(self, stack):
        """Once per stack: every camera move of the full image redraws its
        viewport on the Tissue Preview -- and re-asks whether the pyramid
        level it settled on still deserves the coarse-preview hint, since a
        zoom is precisely what changes that. The connection dies with the
        view."""
        if stack is None or getattr(stack, "_nav_rect_connected", False):
            return
        try:
            stack.view.view_box.sigRangeChanged.connect(
                lambda *_: self._on_full_image_camera_moved())
        except (AttributeError, RuntimeError, TypeError):
            return
        stack._nav_rect_connected = True

    def _on_full_image_camera_moved(self):
        self._update_full_image_view_rect()
        self._update_full_level_hint()

    def _update_full_image_view_rect(self):
        """Draw the full image's current viewport on the Tissue Preview --
        only while the full image is what the user is looking at."""
        popup = getattr(self, "_tissue_navigator_popup", None)
        if popup is None or not self._full_image_visible():
            return
        explore_tab = getattr(self, "_explore_tab", None)
        stack = explore_tab.stack if explore_tab is not None else None
        bbox = getattr(getattr(stack, "controller", None), "_current_bbox", None)
        if bbox is None:
            popup.overview.clear_current_view_rect()
            return
        y0, x0, y1, x1 = (float(v) for v in bbox)
        popup.overview.set_current_view_rect((y0, y1, x0, x1))

    def _update_tissue_view_rect(self):
        """Refresh the popup's current-view rectangle from the active viewer.

        Clears the rectangle when: no popup, split view (mixed geometry), no
        viewer image yet, or no current patch. Never creates ROIs/files.
        While the FULL IMAGE is on screen the rectangle is its viewport
        instead (`_update_full_image_view_rect`)."""
        popup = self._tissue_navigator_popup
        if popup is None:
            return
        if self._full_image_visible():
            self._update_full_image_view_rect()
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
        """Load a dataset, transactionally.

        Nothing that belongs to the CURRENT dataset is touched until (a) the
        new loader has been built successfully and (b) the current dataset's
        background workers are confirmed finished. The switch is committed in
        one block after both checks, and only then does the dataset
        generation move.

        What a pre-commit failure guarantees, precisely: the current
        dataset's IDENTITY (loader, ome_path, output dir), its on-screen
        PIXELS and metrics, and the dataset GENERATION are unchanged. It is
        not a full rollback -- reaching check (b) already asked the running
        background workers to stop, and a stop cannot be un-asked, so an
        in-flight batch / on-demand / preload run may have to be restarted
        by the user.
        """
        global OME_TIFF_FILE, OUTPUT_DIR

        ome = self._ome_path_edit.text().strip()
        outd = self._out_path_edit.text().strip()
        panel_csv = self._panel_csv_edit.text().strip()

        if not ome or not os.path.exists(ome):
            QMessageBox.warning(self, "File not found", f"OME-TIFF not found:\n{ome}")
            return

        # The whole-slide correction worker is the one worker we cannot stop
        # here: its only save-consistent stop (stop_after_current_channel) can
        # take a full channel of a whole slide to be observed, and forcing it
        # would leave a half-written corrected zarr. Refuse the switch instead
        # of racing it. (Unreachable from the UI -- Save disables the Load
        # button and runs a modal dialog -- but the invariant is stated here
        # rather than inferred from the dialog's modality.)
        wsi = getattr(self, "_wsi_worker", None)
        if wsi is not None and wsi.isRunning():
            QMessageBox.warning(
                self, "Correction in progress",
                "A whole-slide background correction is still running.\n"
                "Wait for it to finish, or cancel it, before loading another "
                "dataset.")
            return

        new_output_dir = outd if outd else os.path.dirname(ome)
        try:
            os.makedirs(new_output_dir, exist_ok=True)
            new_loader = OMETIFFLoader(ome, CHANNEL_NAME_MAP)
        except Exception as e:
            # Nothing has been mutated yet: the current dataset is untouched.
            QMessageBox.critical(self, "Load error", str(e))
            return

        # The old dataset's workers hold its loader, the GPU and open reads.
        # Stop AND wait: a stop that has not been observed yet is not a
        # finished thread. If a thread refuses to finish we keep its handle
        # and abort the switch rather than destroy a running QThread.
        stuck = self._stop_bg_workers()
        if stuck:
            QMessageBox.critical(
                self, "Load error",
                "Background workers did not stop (%s).\n"
                "The current dataset is still loaded and displayed; the "
                "background computation was asked to stop, so it may need to "
                "be restarted. Try loading again."
                % ", ".join(sorted(set(stuck))))
            return

        # ── commit ───────────────────────────────────────────────────────
        # From here on the switch cannot fail back to the old dataset.
        # The generation moves FIRST: every callback still bound to the old
        # generation (including signals Qt has already queued) is dropped
        # from this line on.
        self._dataset_gen += 1
        # Explore: tear the PREVIOUS dataset's stack down BEFORE self.ome_path
        # moves, so neither its pixels nor its source identity can survive --
        # and so a Full Image that is open right now is unbound before
        # anything of the new dataset exists.
        explore_tab = getattr(self, "_explore_tab", None)
        if explore_tab is not None:
            explore_tab.set_dataset(None)
        # Drop the old dataset's on-screen pixels, metrics and caches.
        self._reset_dataset_view_state()
        old_loader = getattr(self, "loader", None)
        if old_loader is not None and old_loader is not new_loader:
            try:
                # The only handle the loader caches is the corrected zarr store
                # (the OME-TIFF itself is reopened per read); release it.
                old_loader.set_corrected_zarr_store(None, {})
            except Exception:
                pass
        self.loader = new_loader
        # Re-arm the per-load auto-open guard: each genuine load may open the
        # navigator once; ROI/overview refreshes (other code paths) do not re-arm.
        self._navigator_auto_opened = False

        OME_TIFF_FILE = ome
        OUTPUT_DIR = new_output_dir
        self._out_path_edit.setText(OUTPUT_DIR)

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

        # Bind Explore to the NEW dataset (the stack itself is built lazily,
        # on the next activation of the Explore view).
        if explore_tab is not None:
            explore_tab.set_dataset(self.ome_path)
        self.current_patch_idx = 0
        self.current_channel = None
        self._channel_decisions.clear()
        # New image/ROI loaded -> no BG run yet: button back to "▶ Process".
        # (The stale-result clearing itself lives in _reset_dataset_view_state,
        # which ran above, before the new loader was bound.)
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

        # The workspace is full-image-first: a loaded slide LANDS on the whole
        # slide, not on three empty compare panels.
        self._enter_full_image_landing()

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

    def _roi_count(self):
        """ROIs drawn on the overview the user draws on (the navigator's when
        it exists, else the page's), falling back to `self.rois`."""
        try:
            ov = self._drawing_overview()
            if ov is not None:
                return len(ov.get_rois())
        except Exception:
            pass
        return len(getattr(self, "rois", None) or [])

    def _is_full_wsi_mode(self):
        """The analysis region is the whole slide exactly when no ROI has
        been drawn. Derived, never chosen: the former ROI-vs-Full-WSI
        selector duplicated the ROI button and is gone."""
        return self._roi_count() == 0

    def _on_analysis_region_changed(self, idx):
        """Kept for callers of the old selector; the region is derived now
        (`_is_full_wsi_mode`), so there is nothing to set."""
        return None

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

    def _on_mode_button(self, mode):
        """A mode button was clicked. QPushButton has already flipped its
        checked state: on -> that mode; off -> no drawing mode at all."""
        btn = self._btn_mode_roi if mode == "roi" else self._btn_mode_patch
        self._set_draw_mode(mode if btn.isChecked() else None)

    def _set_draw_mode(self, mode):
        """切换绘制模式，同步按钮状态。

        `mode` is "roi", "patch" or None. None is the navigate/pan mode: no
        drawing tool active, a click on the tissue jumps the full image
        there and a left-drag pans the overview. The two modes are never on
        together; both off is allowed (user request).
        """
        self._btn_mode_roi.setChecked(mode == "roi")
        self._btn_mode_patch.setChecked(mode == "patch")
        ov = self._drawing_overview()
        if mode is None:
            ov._set_mode(None)
            return
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
        else:
            ov._set_mode("patch")
        # No mode sentence in the overview's status line: it took a row
        # under the tissue that a click meant for the tissue landed on
        # (user report). The mode's hint line already says what the mouse
        # does.

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
        self._refresh_channel_state(ch)

    def _set_channel_done(self, ch):
        """A channel's result landed: green tick, row highlighted.

        The checkbox stays ENABLED. It used to be locked ("computed" was
        treated as final), but the checkbox is now the ONE selector Process
        and Save read: locked, a computed channel could never be turned back
        into a raw one, and `_raw_save_channels` would have a set the user
        cannot leave. Green says "computed", not "frozen".
        """
        self._computed_channels.add(ch)
        row = self._channel_rows.get(ch)
        if not row:
            return
        cb = row["checkbox"]
        cb.blockSignals(True)
        cb.setChecked(True)
        cb.setEnabled(True)
        cb.setStyleSheet(
            "QCheckBox::indicator{border:1px solid #6bffa0;border-radius:2px;"
            "background:#6bffa0;}"
            "QCheckBox::indicator:checked{background:#6bffa0;border:1px solid #6bffa0;}"
        )
        cb.blockSignals(False)
        row["status_lbl"].setText("")
        row["row_widget"].setStyleSheet("background:#1a2e1a;border-radius:3px;")
        self._refresh_channel_state(ch)

    # ── per-channel display colour: ONE store, shared with Channel Remap ──
    #  `_channel_colors` holds the USER's choices; anything not in it answers
    #  from the Channel Remap palette by channel index, which is exactly the
    #  rule `ChannelWorkbench.set_channel_images` uses. So a channel wears the
    #  same colour in the Background Correction list, the Channel Remap layer
    #  list, the compare panels and the full image -- before anyone picks a
    #  colour at all -- and a pick on either side moves both.
    def _palette_channel_order(self):
        """The channel order the palette is dealt over: exactly the list
        `_sync_step0_to_workbench` hands the workbench, so index i here is
        index i there. `_channel_order` is empty mid-rebuild, hence the
        loader fallback."""
        names = list(getattr(self, "_channel_order", ()) or ())
        if not names and getattr(self, "loader", None) is not None:
            try:
                names = list(self.loader.channel_names())
            except Exception:                       # noqa: BLE001
                names = []
        return [ch for ch in names if not _is_non_marker_channel(ch)]

    def _default_channel_color(self, ch):
        """The palette default for `ch` as (r, g, b) floats 0-1."""
        order = self._palette_channel_order()
        try:
            i = order.index(ch)
        except ValueError:
            return getattr(self, "_marker_color", (0.0, 1.0, 0.3))
        return _channel_hex_to_rgb01(CHANNEL_PALETTE[i % len(CHANNEL_PALETTE)])

    def _channel_color(self, ch):
        """THE colour of `ch` as (r, g, b) floats 0-1 -- the one answer every
        view asks for. A user pick wins; the nucleus falls back to its own
        colour; every other channel to its palette default."""
        if not ch:
            return getattr(self, "_marker_color", (0.0, 1.0, 0.3))
        rgb = self._channel_colors.get(ch)
        if rgb is not None:
            return rgb
        if ch == self.nucleus_channel:
            return getattr(self, "_nuc_color", (0.0, 0.5, 1.0))
        return self._default_channel_color(ch)

    def _channel_color_hex(self, ch):
        rgb = self._channel_color(ch)
        return QtGui.QColor(int(rgb[0] * 255), int(rgb[1] * 255),
                            int(rgb[2] * 255)).name()

    def _channel_swatch_hex(self, ch):
        """`#rrggbb` for a channel's swatch (the dock adapter asks this)."""
        return self._channel_color_hex(ch)

    def _push_color_to_workbench(self, ch, rgb):
        """Mirror a colour into the Channel Remap layer list. Silent: the
        workbench's own setter does not re-emit, so this cannot loop."""
        wb = getattr(self, "_cond_workbench", None)
        setter = getattr(wb, "set_channel_color", None)
        if setter is None:
            return
        setter(ch, QtGui.QColor(int(rgb[0] * 255), int(rgb[1] * 255),
                                int(rgb[2] * 255)).name())

    def _on_model_color_changed(self, ch, hexc):
        """The channel model's colour changed: the one store follows, and with
        it the compare panels, the full image and the Intensity window's
        histogram. Our own writes echo back through here; comparing against the
        store's current answer swallows the echo instead of looping."""
        if not ch or QtGui.QColor(hexc).name() == self._channel_color_hex(ch):
            return
        c = QtGui.QColor(hexc)
        rgb = (c.red() / 255.0, c.green() / 255.0, c.blue() / 255.0)
        if ch == self.nucleus_channel:
            self._apply_nucleus_color(rgb)
        else:
            self._apply_channel_color(ch, rgb)

    def _on_workbench_color_changed(self, ch, hexc):
        """A swatch was picked in the Channel Remap layer list: the same
        colour is now this page's colour for that channel."""
        c = QtGui.QColor(hexc)
        rgb = (c.red() / 255.0, c.green() / 255.0, c.blue() / 255.0)
        if ch and ch == self.nucleus_channel:
            self._apply_nucleus_color(rgb, push_workbench=False)
        else:
            self._apply_channel_color(ch, rgb, push_workbench=False)

    def _on_channel_swatch_clicked(self, ch):
        """A swatch in the Channels list was clicked: pick that channel's
        display colour. The nucleus channel drives `_nuc_color` and the DAPI
        overlay; every other channel drives `_channel_colors` (the compare
        panels) and the full image's tint."""
        if ch and ch == self.nucleus_channel:
            self._pick_nucleus_color()
        else:
            self._pick_channel_color(ch)

    def _apply_channel_color(self, ch, rgb, push_workbench=True):
        """Record `rgb` for `ch` and push it to every view: the swatch (via
        the channel model), the Channel Remap layer list, the compare panels
        and the full image's tint."""
        self._channel_colors[ch] = rgb
        if push_workbench:
            self._push_color_to_workbench(ch, rgb)
        model = self._display_model()
        if model is not None and model.get(ch) is not None:
            model.set_color(ch, QtGui.QColor(
                int(rgb[0] * 255), int(rgb[1] * 255), int(rgb[2] * 255)).name())
        # Invalidate this channel's cached composites, then redraw.
        for k in [k for k in self._preview_cache if k[0] == ch]:
            del self._preview_cache[k]
        if ch == self.current_channel and self._last_payload is not None:
            self._rebuild_payload_rgb(ch)
            self._refresh_preview_display(keep_zoom=True)
        # The full image draws the SAME channel in the SAME colour: a lookup
        # table swap, no re-read and no request.
        if ch == self.current_channel:
            explore_tab = getattr(self, "_explore_tab", None)
            stack = explore_tab.stack if explore_tab is not None else None
            set_tint = getattr(getattr(stack, "controller", None), "set_tint", None)
            if set_tint is not None:
                set_tint(self._full_image_tint(ch))

    def _pick_channel_color(self, ch, btn=None):
        """弹颜色对话框，让用户选择通道显示颜色。"""
        from PyQt5.QtWidgets import QColorDialog
        if not ch:
            return
        rgb = self._channel_color(ch)
        init_color = QtGui.QColor(int(rgb[0]*255), int(rgb[1]*255), int(rgb[2]*255))
        color = QColorDialog.getColor(init_color, self, f"Color for {ch}")
        if not color.isValid():
            return
        new_rgb = (color.red()/255.0, color.green()/255.0, color.blue()/255.0)
        if btn is not None:                     # legacy callers with their own swatch
            btn.setStyleSheet(
                f"QPushButton{{background:{color.name()};border:1px solid #555;"
                f"border-radius:2px;}}"
                f"QPushButton:hover{{border:1px solid #aaa;}}")
        if ch == self.current_channel:
            self._marker_color = new_rgb
        self._apply_channel_color(ch, new_rgb)

    def _pick_nucleus_color(self):
        """弹颜色对话框，让用户选择 nucleus 叠加显示颜色。"""
        from PyQt5.QtWidgets import QColorDialog
        rgb = self._channel_color(self.nucleus_channel)
        init_color = QtGui.QColor(int(rgb[0] * 255), int(rgb[1] * 255), int(rgb[2] * 255))
        color = QColorDialog.getColor(init_color, self, "Color for nucleus")
        if not color.isValid():
            return
        self._apply_nucleus_color((color.red() / 255.0, color.green() / 255.0,
                                   color.blue() / 255.0))

    def _apply_nucleus_color(self, rgb, push_workbench=True):
        """Record the DAPI overlay colour and push it to every view."""
        self._nuc_color = rgb
        nuc = self.nucleus_channel
        if nuc:
            self._channel_colors[nuc] = self._nuc_color
            if push_workbench:
                self._push_color_to_workbench(nuc, self._nuc_color)
            model = self._display_model()
            if model is not None and model.get(nuc) is not None:
                model.set_color(nuc, self._channel_color_hex(nuc))
        if self._last_payload is not None and self.current_channel:
            self._rebuild_payload_rgb(self.current_channel)
            self._refresh_preview_display(keep_zoom=True)
        # The full image draws the nucleus in this same colour; swapping the
        # lookup table is all it takes -- no re-read, no re-quantisation and
        # no request. With no stack up, the new colour is simply what the
        # next build starts from.
        overlay = self._full_image_overlay()
        if overlay is not None:
            overlay.set_tint(self._nuc_color)

    # ── the DAPI layer switch (the nucleus row's checkbox) ───────────────
    def _sync_nucleus_row_checkbox(self, on):
        """Reflect the DAPI layer state in the nucleus row's checkbox, and in
        the compare panels' hidden holder, without re-entering."""
        if getattr(self, "_nucleus_vis_syncing", False):
            return
        self._nucleus_vis_syncing = True
        try:
            on = bool(on)
            holder = getattr(self, "_btn_show_nucleus", None)
            if holder is not None and holder.isChecked() != on:
                holder.setChecked(on)
            row = (self._channel_rows or {}).get(self.nucleus_channel)
            cb = row.get("checkbox") if row else None
            if cb is not None and cb.isChecked() != on:
                cb.blockSignals(True)
                cb.setChecked(on)
                cb.blockSignals(False)
        finally:
            self._nucleus_vis_syncing = False

    def _nucleus_layer_visible(self):
        btn = getattr(self, "_btn_show_nucleus", None)
        return DAPI_LAYER_DEFAULT_ON if btn is None else bool(btn.isChecked())

    def _reset_nucleus_layer_default(self):
        """Put the DAPI layer back to its default (off) in BOTH views.

        Called on a dataset (re)load. It goes through the same toggle that
        the checkbox uses, so the compare panels, the full-image toolbar
        button and the nucleus row's checkbox all follow -- there is no
        second path that could leave one of them out of step.
        """
        holder = getattr(self, "_btn_show_nucleus", None)
        if holder is None:
            return
        if holder.isChecked() != DAPI_LAYER_DEFAULT_ON:
            holder.setChecked(DAPI_LAYER_DEFAULT_ON)
        self._on_nucleus_visibility_toggled(DAPI_LAYER_DEFAULT_ON)
        self._sync_nucleus_row_checkbox(DAPI_LAYER_DEFAULT_ON)

    def _on_nucleus_visibility_toggled(self, on):
        """The nucleus row's checkbox is the DAPI layer's show/hide switch --
        ONE state driving both views: the compare panels (via the hidden
        `_btn_show_nucleus` holder) and the full image (`_btn_full_nucleus`).

        It is purely a display switch: DAPI never enters Process/Apply/
        on-demand/Save, and no method is recorded for it.
        """
        if getattr(self, "_nucleus_vis_syncing", False):
            return
        self._nucleus_vis_syncing = True
        try:
            on = bool(on)
            for btn in (getattr(self, "_btn_show_nucleus", None),
                        getattr(self, "_btn_full_nucleus", None)):
                if btn is not None and btn.isChecked() != on:
                    btn.setChecked(on)          # each has its own view handler
            print(f"[step0] DAPI layer {'shown' if on else 'hidden'}", flush=True)
        finally:
            self._nucleus_vis_syncing = False

    def _rebuild_payload_rgb_from(self, payload, ch, nucleus_rgb, marker_rgb):
        """Record `marker_rgb` as the channel's colour.

        This used to composite three float32 RGB previews into the payload
        (~215 ms for a 2720x2336 patch, and it ran twice per channel switch).
        Nothing read them: the panels are coloured by a lookup table at paint
        time now (`_refresh_preview_display`), so the only thing left to do
        is remember the colour. Kept as the one place every caller already
        goes through; any stale composites are dropped.
        """
        if payload is None:
            return
        self._channel_colors[ch] = marker_rgb
        for rgb_key in ("original_rgb", "tophat_rgb", "cucim_rgb"):
            payload.pop(rgb_key, None)

    _black_lut = np.zeros((256, 3), dtype=np.uint8)

    def _nuc_item(self, idx):
        """Panel `idx`'s nucleus ImageItem, created on first use: additive
        (CompositionMode_Plus), above the marker item, row-major."""
        item = self._preview_nuc_imgs[idx]
        if item is None:
            item = pg.ImageItem(axisOrder="row-major")
            item.setZValue(1)
            item.setCompositionMode(QtGui.QPainter.CompositionMode_Plus)
            item.setVisible(False)
            self._preview_vbs[idx].addItem(item)
            self._preview_nuc_imgs[idx] = item
        return item

    def _rebuild_payload_rgb(self, ch):
        """用当前通道颜色重新合成_last_payload中的RGB图。"""
        payload = self._last_payload
        if payload is None:
            return
        self._rebuild_payload_rgb_from(
            payload,
            ch,
            self._channel_color(self.nucleus_channel),
            self._channel_color(ch),
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

    def _refresh_preview_display(self, keep_zoom=False):
        """按当前显示开关、颜色和显示映射刷新三联预览。

        The panels show RAW intensity -- the original and the corrected
        results as the worker produced them, no per-patch normalisation --
        through the channel's display mapping (core/display_mapping.py):
        `levels=(min, max)` on the image item and a lookup table carrying
        gamma and colour. The nucleus is a second item ADDED on top
        (CompositionMode_Plus) under its own channel's mapping. The same
        mapping drives the full image, so a patch looks the same in both,
        and the three panels share one mapping, so a corrected result that
        is darker IS darker. Nothing per pixel happens here; a mapping,
        colour or switch change is a levels/table swap.
        """
        payload = self._last_payload
        if payload is None:
            return

        prev_ranges = [vb.viewRange() for vb in self._preview_vbs] if keep_zoom else None
        ch = self.current_channel or next(iter(self._channel_colors.keys()), "")
        marker_rgb = self._channel_color(ch)
        nuc_rgb = self._channel_color(self.nucleus_channel)
        marker_on = self._btn_show_marker.isChecked()
        nucleus_on = self._btn_show_nucleus.isChecked()

        m_lo, m_hi, m_gamma = self._display_mapping_for(ch, payload=payload)
        n_lo, n_hi, n_gamma = self._display_mapping_for(
            self.nucleus_channel, payload=payload, nucleus=True)
        marker_lut = build_display_lut(marker_rgb, m_gamma)
        nuc_lut = build_display_lut(nuc_rgb, n_gamma)
        nucleus = self._payload_array(payload, "nucleus")
        markers = [self._payload_array(payload, key)
                   for key in ("original", "tophat", "cucim")]

        _blank_shape = next((m.shape[:2] for m in markers if m is not None), (64, 64))
        _blank = np.zeros(_blank_shape, dtype=np.uint8)

        for idx, m in enumerate(markers):
            item = self._preview_imgs[idx]
            nuc_item = self._preview_nuc_imgs[idx]      # None until first used
            if m is None:
                item.setImage(_blank, autoLevels=False, levels=(0, 255))
                item.setLookupTable(None)
                if nuc_item is not None:
                    nuc_item.setVisible(False)
                continue
            # "Marker off" is an all-black TABLE, not opacity 0: the item
            # must stay opaque so the panel background is not added under
            # the nucleus.
            item.setImage(m, autoLevels=False, levels=(m_lo, m_hi))
            self._place_preview_item(item, m, payload)
            item.setLookupTable(marker_lut if marker_on else self._black_lut)
            if nucleus_on and nucleus is not None and nucleus.shape[:2] == m.shape[:2]:
                nuc_item = self._nuc_item(idx)
                nuc_item.setImage(nucleus, autoLevels=False, levels=(n_lo, n_hi))
                self._place_preview_item(nuc_item, nucleus, payload)
                nuc_item.setLookupTable(nuc_lut)
                nuc_item.setVisible(True)
            elif nuc_item is not None:
                nuc_item.setVisible(False)

        # WHERE the panels look. A snapshot names its own level-0 rectangle
        # and every panel is pinned to exactly it, so the three are
        # registered to each other and to the full image they were taken
        # from, at that image's own scale. Anything else -- a Process
        # result, a test fixture -- keeps the previous patch-pixel
        # behaviour.
        rect = payload.get("snapshot_rect_l0")
        for idx, vb in enumerate(self._preview_vbs):
            if rect is not None:
                rx, ry, rw, rh = (float(v) for v in rect)
                vb.setRange(xRange=(rx, rx + rw), yRange=(ry, ry + rh),
                            padding=0)
            elif keep_zoom and prev_ranges is not None:
                xr, yr = prev_ranges[idx]
                vb.setRange(xRange=xr, yRange=yr, padding=0)
            else:
                vb.autoRange()

    def _place_preview_item(self, item, arr, payload):
        """Put `item`'s pixels where the payload says they are.

        A snapshot's arrays are a crop of a PYRAMID LEVEL, but the panels
        work in level-0 slide coordinates -- that is what makes "the same
        scale as the full image" a statement one can check -- so the item is
        stretched onto the snapshot's level-0 rectangle. Everything else
        (Process results, fixtures) is placed 1:1 at the origin, which is
        the identity transform those payloads always had.
        """
        rect = payload.get("snapshot_rect_l0")
        h, w = (int(v) for v in np.asarray(arr).shape[:2])
        if rect is None:
            box = QRectF(0.0, 0.0, float(w), float(h))
        else:
            rx, ry, rw, rh = (float(v) for v in rect)
            box = QRectF(rx, ry, rw, rh)
        try:
            item.setRect(box)
        except Exception:                                   # noqa: BLE001
            pass


    @staticmethod
    def _payload_array(payload, key):
        """The RAW array for `key` ("original" / "tophat" / "cucim" /
        "nucleus"): `<key>_raw` from the worker, else the legacy normalised
        `<key>_disp` (older payloads and test fixtures), else None."""
        arr = payload.get(f"{key}_raw")
        if arr is None:
            arr = payload.get(f"{key}_disp")
        return None if arr is None else np.asarray(arr)

    # ── display mapping: one per channel, shared by every view ─────────

    def _display_model(self):
        adapter = getattr(self, "_dock_adapter", None)
        return getattr(adapter, "model", None)

    def _workbench_params(self, ch):
        """The Channel Remap workbench's params dict for `ch`, or None when
        the workbench does not (yet) know that channel."""
        wb = getattr(self, "_cond_workbench", None)
        params = getattr(wb, "_params", None) if wb is not None else None
        if params is None or ch not in params:
            return None
        return params[ch]

    def _display_mapping_for(self, ch, payload=None, nucleus=False):
        """`(min, max, gamma)` for `ch` -- the Channel Remap params.

        ONE source of truth: the workbench's per-channel remap params ARE
        this page's display mapping, so the floating Intensity window, the
        compare panels, the full image and the Channel Remap tab can never
        disagree. Brightness/contrast have no Step0 UI and are pinned to the
        neutral 0.0 / 1.0 where remap semantics equal a display window.

        A channel the workbench has never seen (no dataset, fake loaders)
        falls back to a per-page entry seeded the same way.

        Seeding: the FIRST time a channel is shown its min/max come from the
        whole slide's tissue (`_seed_display_mapping`) -- a slide-wide window
        that stays valid as the user pans. That is deliberately NOT what the
        inspector's own "Auto" button does: Auto is QuPath auto-contrast on
        the CURRENT PREVIEW PATCH, and it stays that way because the floating
        window is a 1:1 replica of the Channel Remap inspector.
        """
        if not ch:
            return (0.0, 1.0, 1.0)
        p = self._workbench_params(ch)
        if p is not None:
            wb = self._cond_workbench
            # The workbench has taken this channel over: drop any entry the
            # pre-engagement fallback left behind, so no stale second copy of
            # the numbers shadows the source of truth.
            self._display_fallback.pop(ch, None)
            if ch not in self._display_seeded:
                self._display_seeded.add(ch)
                if not wb._user_adjusted.get(ch):
                    lo, hi = self._seed_display_mapping(
                        ch, payload=payload, nucleus=nucleus)
                    p.update({"min": float(lo), "max": float(hi), "gamma": 1.0,
                              "brightness": 0.0, "contrast": 1.0, "auto": True})
                    wb._params[ch] = normalize_channel_remap_params(p)
                    p = wb._params[ch]
                    if wb.active_channel() == ch:
                        # Numbers only: this wrote a new window over the same
                        # pixels, so the density curve does not have to be
                        # rebuilt from the whole patch a second time.
                        wb._load_params_into_controls(
                            ch, rebuild_histogram=False)
            p["brightness"], p["contrast"] = 0.0, 1.0
            return (float(p["min"]), float(p["max"]), float(p["gamma"]))
        entry = self._display_fallback.get(ch)
        if entry is None:
            lo, hi = self._seed_display_mapping(ch, payload=payload, nucleus=nucleus)
            entry = (lo, hi, 1.0)
            self._display_fallback[ch] = entry
            self._set_display_silently(ch, *entry)
        return entry

    def _seed_display_mapping(self, ch, payload=None, nucleus=False):
        """QuPath-style automatic window over the whole slide's tissue
        pixels (the viewer's overview level, zeros excluded) -- the same
        rule and level the full image seeds with. Falls back to the
        payload's raw pixels when the loader cannot read a pyramid."""
        loader = getattr(self, "loader", None)
        read = getattr(loader, "read_region_lowres", None)
        shape = getattr(loader, "shape", None)
        if read is not None and shape and len(shape) >= 2 and int(shape[0]) > 0:
            try:
                ds = loader.overview_downsample() if hasattr(loader, "overview_downsample") else 32
                arr = read(ch, 0, int(shape[0]), 0, int(shape[1]), ds, normalize=False)
                return seed_display_range(arr)
            except Exception as exc:                   # noqa: BLE001 - fall back below
                print(f"[step0] display seed from slide failed for {ch!r}: {exc}", flush=True)
        payload = payload if payload is not None else self._last_payload
        if payload is not None:
            arr = self._payload_array(payload, "nucleus" if nucleus else "original")
            if arr is not None:
                return seed_display_range(arr)
        return (0.0, 1.0)

    def _set_display_silently(self, ch, lo, hi, gamma):
        """Mirror a mapping into the channel model without the model
        re-broadcasting it. The model's display fields are a MIRROR only --
        nothing reads them back as the mapping (see `_display_mapping_for`);
        they are kept so the shared dock/model stays a faithful description
        of the channel set."""
        model = self._display_model()
        if model is None or model.get(ch) is None:
            return
        model.blockSignals(True)
        try:
            model.set_display(ch, lo, hi, gamma)
        finally:
            model.blockSignals(False)

    def set_display_mapping(self, ch, lo, hi, gamma=None):
        """Public: set a channel's display mapping. Every view follows.

        Writes the workbench's remap params -- the single source of truth --
        and emits its `params_changed`, which is the one signal that redraws
        the compare panels and pushes the numbers to the full image.
        """
        if not ch:
            return
        cur = self._display_mapping_for(ch)
        gamma = cur[2] if gamma is None else gamma
        self._set_display_silently(ch, lo, hi, gamma)     # model mirror
        wb = getattr(self, "_cond_workbench", None)
        if self._workbench_params(ch) is not None:
            p = dict(wb._params[ch])
            p.update({"min": float(lo), "max": float(hi), "gamma": float(gamma),
                      "brightness": 0.0, "contrast": 1.0, "auto": False})
            wb._params[ch] = normalize_channel_remap_params(p)
            wb._user_adjusted[ch] = True     # explicit numbers survive a patch switch
            self._display_seeded.add(ch)     # and are never re-seeded over
            if wb.active_channel() == ch:
                wb._load_params_into_controls(ch)
                wb._refresh_preview()
            wb.params_changed.emit(ch)       # -> _on_display_mapping_changed
            return
        self._display_fallback[ch] = (float(lo), float(hi), float(gamma))
        self._on_display_mapping_changed(ch)

    def _on_display_mapping_changed(self, cid):
        """A channel's mapping changed: the compare panels redraw (a levels
        and table swap), the full image and its overlay get the numbers."""
        if cid in (self.current_channel, self.nucleus_channel):
            if self._last_payload is not None and hasattr(self, "_preview_imgs"):
                self._refresh_preview_display(keep_zoom=True)
        explore_tab = getattr(self, "_explore_tab", None)
        stack = explore_tab.stack if explore_tab is not None else None
        if stack is None:
            return
        if cid == self.current_channel:
            set_marker = getattr(stack.controller, "set_display_mapping", None)
            if set_marker is not None:
                lo, hi, gamma = self._display_mapping_for(cid)
                set_marker(lo, hi, gamma, channel=cid)
        if cid == self.nucleus_channel:
            set_nuc = getattr(getattr(stack, "overlay", None), "set_display_mapping", None)
            if set_nuc is not None:
                lo, hi, gamma = self._display_mapping_for(cid, nucleus=True)
                set_nuc(lo, hi, gamma)

    def _on_display_auto(self, prefix):
        """Re-seed a role's mapping from the SLIDE (not the patch). `prefix`
        is "marker" / "nucleus" (the legacy "nuc" spelling still works). The
        inspector's own Auto button is the patch-based one."""
        nuc = prefix in ("nuc", "nucleus")
        ch = self.nucleus_channel if nuc else self.current_channel
        if not ch:
            return
        lo, hi = self._seed_display_mapping(ch, nucleus=nuc)
        self.set_display_mapping(ch, lo, hi, 1.0)

    @staticmethod
    def _payload_shape(payload):
        """(h, w) of the pixels a preview payload would put on screen, from
        whichever result it carries, or None for an empty payload."""
        for key in ("original_disp", "tophat_disp", "cucim_disp"):
            arr = (payload or {}).get(key)
            if arr is not None:
                return tuple(int(v) for v in arr.shape[:2])
        return None

    def _recompute_keeps_zoom(self, shown, incoming):
        """Whether a freshly computed payload should keep the panels' zoom.

        A result that REPLACES what is on screen -- the same patch, so the
        same pixel grid, computed again with a changed parameter -- must not
        move the camera: the user zoomed in to judge exactly that spot, and
        pressing Enter on a radius used to throw the zoom away and show the
        whole patch again. A result for a different-sized patch, or the
        first result when nothing is shown, gets the auto-range as before,
        because the previous view rectangle means nothing on a new grid.
        """
        shown_shape = self._payload_shape(shown)
        return (shown_shape is not None
                and shown_shape == self._payload_shape(incoming))

    def _resolve_channel_params(self, ch):
        """(tophat_radius, cucim_sigma) for a channel — its OWN per-channel value.

        Fully isolated from the global Method Parameters: an untuned channel falls
        back to the module defaults (NOT the live global sliders), so the global
        box can never bleed into / overwrite a channel's per-channel params."""
        cp = self._channel_params.get(ch) or {}
        tr = int(cp.get("tophat_radius", TOPHAT_RADIUS_DEFAULT))
        cs = int(cp.get("cucim_sigma", CUCIM_SIGMA_DEFAULT))
        return tr, cs

    def _patch_signature(self):
        """The patch list as a hashable, order-independent key."""
        return tuple(sorted(tuple(int(v) for v in p) for p in self.patches))

    def _channel_signature(self, ch, method, params=None):
        """Everything a channel's result depends on, as one comparable tuple.

        (method, tophat_radius, cucim_sigma, patches). Anything else the worker
        sees (loader, nucleus channel) changes only on a dataset switch, which
        wipes the signatures wholesale."""
        tr, cs = params if params is not None else self._resolve_channel_params(ch)
        return (str(method), int(tr), int(cs), self._patch_signature())

    def _channel_is_up_to_date(self, ch, sig):
        """True when the cache really holds THIS channel's result for THIS sig.

        Evidence, not a flag: the recorded signature must match, the channel
        must be marked done, and every current patch must have a payload."""
        have = self._computed_signatures.get(ch)
        if have is None:
            return False
        if have != sig:
            # A "both" result carries the TopHat AND the cuCIM output for the
            # same params and patches, so it satisfies a request for either.
            covers = (have[0] == "both" and sig[0] in ("tophat", "cucim")
                      and have[1:] == sig[1:])
            if not covers:
                return False
        if ch not in self._computed_channels:
            return False
        return all((ch, p_idx) in self._preview_cache
                   for p_idx in range(len(self.patches)))

    def _record_channel_signature(self, ch):
        """Promote the in-flight signature of `ch` once its results arrive."""
        sig = self._pending_signatures.get(ch)
        if sig is not None:
            self._computed_signatures[ch] = sig

    def _invalidate_channel_signature(self, ch):
        self._computed_signatures.pop(ch, None)
        self._pending_signatures.pop(ch, None)

    # ── per-row compute state (the glyph beside each checkbox) ──────────
    #
    # DERIVED, never stored: the answer comes from the same evidence the
    # incremental Process consults (`_computed_signatures` +
    # `_computed_channels` + `_preview_cache`), so the glyph and the run
    # cannot disagree about which channels are up to date. A second flag
    # would be a second answer, and it is exactly the kind of flag that
    # drifts -- the reason `_channel_is_up_to_date` takes evidence rather
    # than `_params_dirty` in the first place.

    def _channel_row_method(self, ch):
        """The method a Process run would use for `ch` right now.

        The row's Method combo first (it is the assigned-method control),
        then the recorded decision, then "both" -- the same precedence
        `_on_process_clicked` uses when it reads the ticked rows.
        """
        row = self._channel_rows.get(ch)
        combo = (row or {}).get("method_cb")
        if combo is not None:
            method = combo.currentText().lower()
            if method in {"tophat", "cucim", "both", "original"}:
                return method
        return self._channel_decisions.get(ch) or "both"

    def _channel_compute_state(self, ch):
        """`nucleus` / `computing` / `not-computed` / `computed` / `stale`.

        `stale` is deliberately distinct from `not-computed`: a channel
        whose parameters moved after a run still HAS a result on screen,
        and the user needs to know the pixels they are looking at were made
        with the old numbers -- which "not computed" would not say.
        """
        if ch == self.nucleus_channel:
            return "nucleus"
        if ch in self._pending_signatures:
            return "computing"
        if ch not in self._computed_signatures or ch not in self._computed_channels:
            return "not-computed"
        method = self._channel_row_method(ch)
        if method == "original":
            # No correction is assigned, so there is nothing for the cached
            # result to be current WITH; report what actually exists.
            method = self._computed_signatures[ch][0]
        sig = self._channel_signature(ch, method)
        return "computed" if self._channel_is_up_to_date(ch, sig) else "stale"

    def _refresh_channel_state(self, ch):
        """Push `ch`'s derived compute state onto its row."""
        state = self._channel_compute_state(ch)
        adapter = getattr(self, "_dock_adapter", None)
        if adapter is not None and ch in adapter.model:
            adapter.model.set_status(ch, state)
        # Directly too: `set_status` is a no-op when the value is unchanged,
        # which is exactly the case on a fresh rebuild (the state was seeded
        # into the model before the row existed).
        row = self._channel_rows.get(ch)
        setter = getattr((row or {}).get("row_widget"), "set_state", None)
        if setter is not None:
            setter(state)

    def _refresh_all_channel_states(self):
        for ch in list(self._channel_order):
            self._refresh_channel_state(ch)

    def _raw_save_channels(self):
        """Marker channels Save would write as RAW: unticked, assigned
        Original, or ticked but with no current computed result.

        Order follows the channel list, so the confirmation reads in the
        same order as the rows the user just looked at.
        """
        raw = []
        for ch in self._channel_order:
            if ch == self.nucleus_channel:
                continue
            row = self._channel_rows.get(ch)
            checked = bool(row and row["checkbox"].isChecked())
            if not checked or self._channel_row_method(ch) == "original":
                raw.append(ch)
            elif self._channel_compute_state(ch) != "computed":
                raw.append(ch)
        return raw

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
        # The recorded signature no longer matches these numbers, so the
        # row's glyph turns `stale`. Nothing is recomputed and nothing is
        # thrown away: the old result stays visible until a Process replaces
        # it, and the glyph is what says it was made with other parameters.
        self._refresh_channel_state(ch)

    def _on_dec_param_entered(self):
        """Enter pressed in a per-channel param box.

        Records the value and marks the channel stale -- it no longer starts
        a run. A parameter change is not a request to compute: the checkbox
        plus the Process button are the only path to a correction run, so
        that pressing Enter while tuning a number cannot put the GPU to work
        behind the user's back.
        """
        if getattr(self, "_loading_decision", False):
            return
        self._on_dec_param_changed()

    def _update_decision_ui(self):
        ch = self.current_channel
        enabled = bool(ch and ch != self.nucleus_channel and ch in self._channel_rows)
        self._loading_decision = True
        try:
            self._apply_btn.setEnabled(enabled)
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
        self._refresh_remap_state_label()

    def _refresh_channel_row(self, ch):
        row = self._channel_rows.get(ch)
        if not row:
            return
        cb = row["checkbox"]
        method_cb = row.get("method_cb")
        cb.blockSignals(True)
        if ch == self.nucleus_channel:
            # The nucleus checkbox is the DAPI show/hide switch, not a
            # processing checkbox -- it follows the layer's state, not a
            # correction decision (there is none for DAPI).
            cb.setChecked(self._nucleus_layer_visible())
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
        self._refresh_channel_state(ch)

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
        self._refresh_all_channel_states()

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
        self._refresh_all_channel_states()

    def _on_channel_method_changed(self, ch, txt):
        """Single channel method dropdown change. The combo now also carries the
        assigned decision: "Original" means no correction (channel unchecked).

        Records the choice and re-derives the row's state -- and starts
        NOTHING. A method change is a change to what the next Process would
        do, so a channel already computed with the other method simply
        becomes `stale`.
        """
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
        self._refresh_channel_state(ch)

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
        self._refresh_channel_state(ch)

    def _on_channel_selected_by_id(self, cid):
        """The shared dock's selection, as a channel id, routed to the
        legacy row handler. `""` is the model's "nothing selected"."""
        if not cid:
            self._on_channel_row_changed(-1)
            return
        try:
            row = self._channel_order.index(cid)
        except ValueError:
            return
        self._on_channel_row_changed(row)

    def _on_channel_row_changed(self, row):
        if row < 0 or row >= len(self._channel_order):
            self.current_channel = None
            self._inspector_channel = None
            self._apply_btn.setEnabled(False)
            return
        ch = self._channel_order[row]
        if ch and ch == self.nucleus_channel:
            # DAPI is a REFERENCE channel, never a displayed one. Selecting its
            # row means one thing only: the Intensity window now edits DAPI's
            # min/max/gamma -- the mapping of the additive overlay the compare
            # panels and the full image draw when its checkbox is on. The
            # displayed channel, the method combo and the sigma controls stay
            # on the marker the user was working on, so nothing is dropped and
            # no correction is started. Selecting a marker row afterwards hands
            # the inspector back to that marker.
            self._inspector_channel = ch
            self._sync_intensity_to_channel()
            return
        self._inspector_channel = None
        self.current_channel = ch
        self._update_decision_ui()
        # The floating Intensity window edits the channel the user is looking
        # at: point the workbench's inspector at it (the nucleus included).
        self._sync_intensity_to_channel()
        # The full image follows the channel -- and it is the landing view,
        # so this is the main thing a row click does.
        self._sync_full_image_to_channel()

        if not self.patches:
            self._preview_status.setText(
                "Full image only — draw a patch in the Tissue Navigator to "
                "use the compare panels.")
            self._preview_status.setStyleSheet("color:#aaa;font-size:10px;")
            return

        ch = self.current_channel
        if ch == self.nucleus_channel:
            self._preview_status.setText("Nucleus channel is excluded from correction.")
            return

        # Selecting a row DISPLAYS; it never computes. The on-demand run that
        # used to start here (a `BatchProcessWorker` for any unticked channel
        # clicked after the first Process) is gone: it put the GPU to work on
        # a channel the user had not selected for processing, took the full
        # image away while it ran, and made "which channels did I compute?"
        # depend on the order rows happened to be clicked in. The checkbox
        # plus the Process button are now the only way to a correction run;
        # the row's own state glyph says whether a result exists.
        if self._has_any_cache(ch):
            self._show_channel_from_cache(ch)
        elif ch in self._computed_channels:
            self._preview_status.setText(f"No result for {ch}. Try re-processing.")
        else:
            self._preview_status.setText(
                f"{ch}: not computed. Tick it and press Process to fill the "
                f"compare panels. The full image previews it either way.")
            self._preview_status.setStyleSheet("color:#aaa;font-size:10px;")

    # ══ Process 按钮逻辑 ══════════════════════════════════════════════

    def _on_process_clicked(self):
        """▶ Process 按钮。只要勾选就跑，method只有tophat/cucim/both。"""
        busy = self.production_correction_busy()
        if busy:
            # Reachable during a Save now that its progress dialog is not
            # modal; two GPU users at once is what this gate prevents.
            QMessageBox.information(
                self, "Busy", f"A {busy} run is already in progress.")
            return
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

        # Incremental Process: only NEW or CHANGED channels are recomputed.
        # "Changed" is decided from evidence -- the signature (method, this
        # channel's params, the patch list) that produced the cached result --
        # not from the dirty flag, which cannot say WHICH channel changed.
        to_process, skipped = {}, []
        for ch, method in selected.items():
            sig = self._channel_signature(ch, method)
            if self._channel_is_up_to_date(ch, sig):
                skipped.append(ch)
                continue
            to_process[ch] = method
            self._pending_signatures[ch] = sig
        for ch in skipped:
            print(f"[Step0] Process: {ch} is up to date (unchanged method/params/"
                  f"patches) — skipped", flush=True)
        self._params_dirty = False

        if not to_process:
            n = len(selected)
            print(f"[Step0] Process: nothing to do — all {n} selected channel(s) "
                  f"are up to date", flush=True)
            self._proc_status.setText(
                f"All {n} selected channel{'s' if n != 1 else ''} are up to date.")
            self._proc_status.setStyleSheet("color:#6bffa0;font-size:10px;")
            self._btn_process.setEnabled(True)
            self._btn_stop_process.setEnabled(False)
            self._process_completed = True
            self._reset_process_button()
            return

        # 清掉待重算通道的缓存（method/params/patches 变了）
        stale = set(to_process.keys())
        self._preview_cache = {k: v for k, v in self._preview_cache.items()
                               if k[0] not in stale}
        self._computed_channels -= stale
        for ch in stale:
            self._computed_signatures.pop(ch, None)
        self._process_completed = False

        self._btn_process.setEnabled(False)
        self._btn_stop_process.setEnabled(True)
        self._proc_pbar.setVisible(True)
        self._proc_pbar.setValue(0)
        self._proc_status.setText("Starting…")

        # 将待计算通道标记为"计算中"
        for ch in to_process:
            self._set_channel_computing(ch)

        self._batch_worker = BatchProcessWorker(
            self.loader, self.patches, to_process,
            self.nucleus_channel,
            self._tophat_slider.value(),
            self._cucim_slider.value(),
            channel_params=self._channel_params,   # each channel uses its own params
            max_gpu_workers=4,
        )
        self._batch_worker.channel_patch_done.connect(self._gen_slot(self._on_batch_patch_done))
        self._batch_worker.channel_done.connect(self._gen_slot(self._on_batch_channel_done))
        self._batch_worker.all_done.connect(self._gen_slot(self._on_batch_all_done))
        self._batch_worker.progress.connect(self._gen_slot(self._on_batch_progress))
        self._batch_worker.error_signal.connect(self._gen_slot(self._on_batch_error))
        self._batch_worker.canceled.connect(self._gen_slot(self._on_batch_canceled))
        self._release_explore_for_production("patch background correction")
        self._watch_production_worker(self._batch_worker)
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
        # Evidence for the incremental Process: remember WHAT produced this.
        self._record_channel_signature(ch)
        # 如果当前正在查看这个通道的这个patch，立刻刷新
        if ch == self.current_channel and p_idx == self.current_patch_idx:
            keep_zoom = self._recompute_keeps_zoom(self._last_payload, payload)
            self._last_payload = payload
            self._rebuild_payload_rgb_from(
                payload, ch,
                self._channel_color(self.nucleus_channel),
                self._channel_color(ch))
            self._refresh_preview_display(keep_zoom=keep_zoom)
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
        self._record_channel_signature(ch)
        self._pending_signatures.pop(ch, None)
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
        self._process_completed = True
        # Not auto "Re-process": only a param change after this flips it (Topic 1).
        self._reset_process_button()
        # Every row is re-asked, not just the ones that ran: a skipped
        # up-to-date channel and a channel whose run was stopped both have
        # something to say now.
        self._clear_pending_signatures()
        self._refresh_all_channel_states()

    def _on_batch_canceled(self):
        self._proc_status.setText("Stopped.")
        self._proc_status.setStyleSheet("color:#ffb86c;font-size:10px;")
        self._btn_process.setEnabled(True)
        self._btn_stop_process.setEnabled(False)
        self._clear_pending_signatures()

    def _on_batch_error(self, ch, p_idx, msg):
        print(f"[Batch Error] ch={ch} p_idx={p_idx}\n{msg}")
        if ch == "__global__":
            self._proc_status.setText(f"Error (see terminal): {msg[:60]}")
            self._proc_status.setStyleSheet("color:#ff6b6b;font-size:10px;")
            self._btn_process.setEnabled(True)
            self._btn_stop_process.setEnabled(False)
            self._clear_pending_signatures()

    def _clear_pending_signatures(self):
        """A run ended without delivering: drop what it PROMISED to produce.

        `_pending_signatures` is what makes a row read `computing`; a stop or
        a global failure leaves entries behind that no result will ever
        promote, and the row would spin for the rest of the session. The
        channels that DID finish are unaffected -- `_on_batch_channel_done`
        has already moved theirs into `_computed_signatures`.
        """
        if not self._pending_signatures:
            return
        stalled = list(self._pending_signatures)
        self._pending_signatures.clear()
        for ch in stalled:
            row = self._channel_rows.get(ch)
            if row and row["status_lbl"].text() == "⟳":
                row["status_lbl"].setText("")
            self._refresh_channel_state(ch)

    # ══ on-demand computing: REMOVED ═════════════════════════════════
    #
    # `_start_ondemand` used to launch a `BatchProcessWorker` whenever an
    # uncomputed channel's row was clicked after the first Process. It is
    # gone, not merely unwired: a helper whose only job is to start a
    # production run outside the Process button is the exact thing the
    # "only the checkbox and Process compute" rule forbids, and leaving it
    # in the class is an invitation to call it again.
    #
    # `_ondemand_workers` survives as an empty list. It is read by
    # `production_correction_busy` and by teardown, and keeping the reads
    # unconditional is cheaper than proving no worker can ever land there.

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
        nc = self._channel_color(self.nucleus_channel)
        mc = self._channel_color(ch)
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
        self._preview_worker.finished.connect(self._gen_slot(self._on_preview_ready))
        self._preview_worker.error.connect(self._gen_slot(self._on_preview_error))
        self._preview_worker.start()

    def _on_preview_ready(self, req_id, payload):
        if req_id != self._preview_req_id:
            return
        self._preview_cache[(self.current_channel, self.current_patch_idx)] = payload
        keep_zoom = self._recompute_keeps_zoom(self._last_payload, payload)
        self._last_payload = payload
        self._rebuild_payload_rgb_from(
            payload,
            self.current_channel,
            self._channel_color(self.nucleus_channel),
            self._channel_color(self.current_channel),
        )
        self._refresh_preview_display(keep_zoom=keep_zoom)
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
        self._refresh_channel_row(ch)      # re-derives the row's state too
        self._decision_status.setText(
            f"Saved: {ch} {decision}  (r={self._dec_radius.value()}, "
            f"σ={self._dec_sigma.value()})")

    def _process_current_channel(self):
        """Recompute ONE channel across all patches with its Per-Channel params.

        NO LONGER REACHABLE FROM THE UI. The "Process" button that lived in
        the Per-Channel Decision panel is gone: the checkbox plus the one
        Process button in Method Parameters is the only way a correction run
        starts, so that "which channels did this page compute" has a single
        answer. What is left here is the incremental single-channel run
        itself, kept because it is the shortest honest driver of that worker
        path and several suites (dataset switch, WSI cancel, incremental
        Process, the GPU hand-off parametrization) exercise it as one.
        Nothing in the page calls it.

        Computes BOTH TopHat and cucim (method='both') so the two results can be
        compared and the final one picked via the radio. The radio is the
        final-result selector, NOT a prerequisite for recomputing (radius drives
        TopHat, sigma drives cucim — both are always available)."""
        ch = self.current_channel
        if not ch or ch == self.nucleus_channel:
            return
        if not self.patches:
            QMessageBox.information(self, "No patches",
                                    "Draw at least one patch in the navigator first.")
            return
        busy = self.production_correction_busy()
        if busy:
            QMessageBox.information(
                self, "Busy", f"A {busy} run is already in progress.")
            return
        # persist this channel's current params, then recompute just it (fresh cache)
        self._channel_params[ch] = {
            "tophat_radius": int(self._dec_radius.value()),
            "cucim_sigma": int(self._dec_sigma.value()),
        }
        self._preview_cache = {k: v for k, v in self._preview_cache.items() if k[0] != ch}
        self._computed_channels.discard(ch)
        # Apply always recomputes: drop the old evidence and register what this
        # run will produce, so a later Process sees it as up to date.
        self._invalidate_channel_signature(ch)
        self._pending_signatures[ch] = self._channel_signature(ch, "both")
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
        self._batch_worker.channel_patch_done.connect(self._gen_slot(self._on_batch_patch_done))
        self._batch_worker.channel_done.connect(self._gen_slot(self._on_batch_channel_done))
        self._batch_worker.all_done.connect(self._gen_slot(self._on_batch_all_done))
        self._batch_worker.progress.connect(self._gen_slot(self._on_batch_progress))
        self._batch_worker.error_signal.connect(self._gen_slot(self._on_batch_error))
        self._batch_worker.canceled.connect(self._gen_slot(self._on_batch_canceled))
        self._release_explore_for_production("patch background correction")
        self._watch_production_worker(self._batch_worker)
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

    def teardown(self):
        """Explicit cleanup entry point for the page.

        The Explore stack owns worker threads and open file handles; a host
        that closes or replaces this page must be able to release them
        deterministically rather than waiting for Python GC. Idempotent.
        (The tab also tears itself down on the page's `destroyed` signal and
        on `aboutToQuit`, so a host that forgets this call still does not
        leak -- this is the deterministic path, not the only one.)
        """
        self._stop_bg_workers()
        # Before the explore tab: a snapshot in flight is reading through
        # that stack's provider, and `teardown` closes it.
        self._cancel_compare_snapshot()
        explore_tab = getattr(self, "_explore_tab", None)
        if explore_tab is not None:
            explore_tab.teardown()

    # ══ Dataset generation + worker lifetime ══════════════════════════
    #
    # Two independent mechanisms, neither a substitute for the other:
    #
    #   _gen_slot        rejects LATE SIGNALS from a previous dataset's
    #                    workers, so they cannot mutate this page's state.
    #   _stop_bg_workers releases the REAL RESOURCES (threads, GPU, IO) those
    #                    workers still hold.
    #
    # Generation alone would leave threads running against a stale loader;
    # stopping alone would still let already-queued signals land after the
    # switch, because `stop()` is only a flag checked between work units.

    def _gen_slot(self, slot):
        """Bind `slot` to the dataset generation current AT CONNECTION TIME.

        The captured generation lives in the closure -- it is never re-read
        from page state when the signal arrives. That is the whole point: a
        worker created for dataset A carries A's generation for its entire
        life, so once the page commits a switch to B every one of that
        worker's done/error/cancel/progress signals is dropped, including the
        ones Qt had already queued at switch time.

        The worker's own signal protocol is untouched; this is a page-side
        forwarding slot.
        """
        gen = self._dataset_gen

        def forward(*args):
            if gen != self._dataset_gen:
                return
            return slot(*args)

        return forward

    def _live_bg_workers(self):
        """(label, worker, stop_callable) for every worker handle that is a
        real, still-running thread. Finished on-demand workers are pruned
        here -- the list used to only ever grow."""
        live = []
        preload = getattr(self, "_preload_worker", None)
        if preload is not None and preload.isRunning():
            live.append(("preload", preload, preload.cancel))
        batch = getattr(self, "_batch_worker", None)
        if batch is not None and batch.isRunning():
            live.append(("batch", batch, batch.stop))
        kept = []
        for worker in list(getattr(self, "_ondemand_workers", ()) or ()):
            if worker.isRunning():
                kept.append(worker)
                live.append(("ondemand", worker, worker.stop))
        self._ondemand_workers = kept
        preview = getattr(self, "_preview_worker", None)
        if preview is not None and preview.isRunning():
            live.append(("preview", preview, preview.stop))
        return live

    def _stop_bg_workers(self, *, wait_ms=5000):
        """Request a stop on every live worker, then WAIT for the threads.

        "Stop requested" and "thread finished" are different states: every
        worker's stop is a flag checked between work units, so the thread is
        still running (and still touching the loader / the GPU) when stop()
        returns. This method does both halves and reports what did not
        finish, so a caller can refuse to proceed rather than leave a QThread
        destroyed while running.

        The whole-slide correction worker is deliberately NOT stopped here:
        its only save-consistent stop is `stop_after_current_channel`, which
        can take a full channel of a whole slide to observe. Callers that
        must not race it check `production_correction_busy()` first.

        Returns the list of labels still running after the wait (empty ==
        everything released).
        """
        live = self._live_bg_workers()
        for _label, _worker, stop in live:
            try:
                stop()
            except Exception:
                pass
        stuck = []
        for label, worker, _stop in live:
            if not worker.wait(int(wait_ms)):
                stuck.append(label)
                print(f"[step0] worker '{label}' did not finish within "
                      f"{int(wait_ms)} ms after stop")
        # Drop the handles we have released; a stuck thread keeps its handle so
        # nothing destroys a running QThread.
        if getattr(self, "_preload_worker", None) is not None and "preload" not in stuck:
            self._preload_worker = None
        if getattr(self, "_batch_worker", None) is not None and "batch" not in stuck:
            self._batch_worker = None
        if getattr(self, "_preview_worker", None) is not None and "preview" not in stuck:
            self._preview_worker = None
        if "ondemand" not in stuck:
            self._ondemand_workers = []
        return stuck

    def _reset_dataset_view_state(self):
        """Drop every pixel, metric and cache that belongs to the OLD dataset.

        Called on a committed dataset switch, BEFORE anything of the new
        dataset is bound, so nothing of the previous dataset can survive on
        screen -- not even until the first new result arrives.
        """
        self._last_payload = None
        for img in (getattr(self, "_orig_img", None),
                    getattr(self, "_top_img", None),
                    getattr(self, "_cu_img", None)):
            if img is not None:
                img.clear()
        if hasattr(self, "_metrics_original"):
            self._metrics_original.setText("Original  → SNR: —  BG-CV: —")
            self._metrics_tophat.setText("TopHat    → SNR: —  BG-CV: —")
            self._metrics_cucim.setText("cucim     → SNR: —  BG-CV: —")
        if hasattr(self, "_preview_status"):
            self._preview_status.setText(
                "Select a channel and patch ROI to preview background correction."
            )
            self._preview_status.setStyleSheet("color:#aaa;font-size:10px;")
        self._preview_cache = {}
        self._preload_cache = {}
        self._computed_channels = set()
        # The per-channel "what produced this" evidence belongs to the OLD
        # dataset's pixels; channel names repeat across datasets, so keeping it
        # would let Process skip a channel of B that was only ever computed for A.
        self._computed_signatures = {}
        self._pending_signatures = {}
        # This session's batch selection: which channels are ticked and with
        # which method. Channel NAMES repeat across datasets (both slides have
        # a CD3), so keeping this would silently carry A's ticks and methods
        # into B. `_channel_decisions` / `_channel_params` are re-seeded later
        # in the load by _load_existing_config; this one had no owner.
        self._channel_methods = {}
        # Explicit per-channel raw_ome / corrected_zarr override. Keyed by
        # channel name, so B's CD3 would inherit A's forced source and skip
        # auto-detection (_resolve... consults this BEFORE availability on
        # disk). Cleared so B starts from auto-detection again.
        self._channel_source_requests = {}
        # Analysis-region identity + the minted roi_context it belongs to.
        # The signature is (mode, shape) or (mode, first ROI bbox) -- neither
        # mentions the dataset, so B drawing an ROI at A's bbox would REUSE
        # A's roi_context and write B's outputs into A's roi_dir.
        self._roi_context = None
        self._roi_context_sig = None
        # The exact remap config path Step0 last wrote; MainWindow hands it to
        # Step1 in preference to re-resolving. It points inside A's output
        # tree, so it must not survive into B.
        self._last_saved_remap_path = ""
        # Conditioning viewer zoom/pan, keyed by patch BBOX. A patch of B with
        # the same bbox as one of A is not the same patch and must not inherit
        # its viewport.
        self._conditioning_patch_viewports = {}
        # NOT cleared here: `_channel_colors` is a per-channel-name display
        # preference -- not pixels, not source identity, not an output path.
        # The slide-wide display seed IS per-dataset: a new slide must re-seed.
        self._display_fallback = {}
        self._display_seeded = set()
        # The DAPI layer is per-dataset display state: a new slide starts from
        # the default (off) rather than inheriting the previous slide's switch.
        self._reset_nucleus_layer_default()
        self._process_completed = False
        self._params_dirty = False
        self._preview_req_id = 0
        self._applied_corrected_decisions = {}
        self._incremental_processed = None
        # Full Image shows one source of the OLD dataset, and the compare
        # strip a snapshot of it. Both are dropped here, BEFORE the new
        # loader is bound: the viewer stack is torn down by the explore tab
        # itself, and a snapshot of the previous slide left on screen under
        # the new slide's name is the worst kind of wrong picture.
        # `_enter_full_image_landing`, at the end of the load, opens the new
        # slide's full image -- the landing state.
        self._full_image_source = "original"
        self._cancel_compare_snapshot()
        self._compare_snapshot = None
        if hasattr(self, "_btn_snapshot_patch"):
            self._btn_snapshot_patch.setEnabled(False)
            self._compare_where_lbl.setText(
                "Right-click the full image to take a snapshot here.")
            self._update_compare_level_label(None)
        if getattr(self, "_preview_split", None) is not None:
            self._set_compare_strip_visible(False)

        self._refresh_all_channel_states()

    # (#5) The standalone "Run BG correction" preview-batch handlers
    # (_on_start_bg_correction / _bg_run_next / _finish_bg_start) were removed
    # with that button; the BG-tab Save (_save_and_continue) is the single
    # entry that runs correction + writes outputs + the handoff.

    def _confirm_raw_channels(self):
        """Name the channels Save will write as RAW, once, and ask.

        Save is the moment the decision becomes an artifact, and the set it
        writes raw is not visible anywhere else in one place: it is the
        union of "never ticked", "assigned Original" and "ticked but never
        actually computed" -- three different reasons, three different
        corners of the UI. A run that silently writes half the panel raw is
        the kind of thing found weeks later in Step 2.

        Once: one dialog per Save press, listing channels, not one prompt
        per channel. Returns True to go ahead.
        """
        raw = self._raw_save_channels()
        if not raw:
            return True
        shown = ", ".join(raw[:12])
        if len(raw) > 12:
            shown += f", … (+{len(raw) - 12} more)"
        answer = QMessageBox.question(
            self, "Channels saved as raw",
            f"{len(raw)} channel{'s' if len(raw) != 1 else ''} will be saved "
            f"WITHOUT background correction:\n\n{shown}\n\n"
            "A channel is saved raw when it is unticked, assigned Original, "
            "or ticked but not computed. Continue?",
            QMessageBox.Ok | QMessageBox.Cancel, QMessageBox.Ok)
        return answer == QMessageBox.Ok

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

        # What will NOT be corrected, said once, before anything is written.
        if not self._confirm_raw_channels():
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
            # Reconciles the cache: previously corrected channels revert to raw.
            self._apply_corrected_store(None, {})
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
            from ...core.bg_correction import BG_CORRECTION_ALGO_VERSION
            pname = "tophat_radius" if method == "tophat" else "cucim_sigma"
            pdefault = (TOPHAT_RADIUS_DEFAULT if method == "tophat"
                        else CUCIM_SIGMA_DEFAULT)
            cp = cp_all.get(ch) or {}
            # (method, param, algorithm version): a channel saved by an older
            # numeric version never matches, so incremental save reprocesses it.
            return (method, int(cp.get(pname, mp.get(pname, pdefault))),
                    BG_CORRECTION_ALGO_VERSION)
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
            # The zarr already holds every channel; (re)wire the handoff and
            # reconcile the cache (corrected re-read; withdrawn revert to raw).
            self._apply_corrected_store(zarr_path, corrected)
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
        self._wsi_worker.progress.connect(self._gen_slot(self._on_wsi_progress))
        self._wsi_worker.finished.connect(self._gen_slot(
            lambda path, decisions: self._on_wsi_finished(config, path, decisions)))
        self._wsi_worker.canceled.connect(self._gen_slot(self._on_wsi_canceled))
        self._wsi_worker.error.connect(self._gen_slot(self._on_wsi_error))
        self._wsi_dialog.cancel_requested.connect(self._wsi_worker.stop_after_current_channel)
        self._release_explore_for_production("whole-slide correction (Save)")
        # Before start(), like the other three production paths: a worker
        # that finished before the connection was made would never announce
        # it, and the full image would stay released for good.
        self._watch_production_worker(self._wsi_worker)
        self._wsi_worker.start()
        # `show()`, not `exec_()`: a modal dialog froze every other window
        # for the whole run (the Tissue Preview could not even be closed).
        # Nothing follows this call that needed the modal loop to return;
        # the run's end is handled by the worker's signals.
        self._wsi_dialog.show()

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
        # Store swap + cache reconcile in one place: corrected channels re-read
        # corrected pixels; channels whose correction was withdrawn re-read raw.
        # On an incremental save only channels REprocessed this run re-read as
        # corrected — skipped channels were already corrected in the cache.
        only = getattr(self, "_incremental_processed", None)
        self._incremental_processed = None
        self._apply_corrected_store(zarr_path, decisions, only=only)
        self._emit_complete(config, zarr_path, decisions)

    def _apply_corrected_store(self, zarr_path, decisions, only=None):
        """Single entry point for swapping the corrected store.

        Snapshots the OLD decisions, applies the new store, then reconciles
        the preload cache with the decision DIFF: added/changed channels are
        re-read as corrected, and channels REMOVED from the corrected set
        (switched to Original, or the store cleared) are re-read as raw — so
        the cache can never keep serving stale corrected pixels for a channel
        whose correction was withdrawn. Both sets are invalidated."""
        old = dict(getattr(self, "_applied_corrected_decisions", {}) or {})
        self.loader.set_corrected_zarr_store(zarr_path, decisions)
        new = ({ch: m for ch, m in (decisions or {}).items()
                if str(m).strip().lower() in {"tophat", "cucim"}}
               if zarr_path else {})
        self._applied_corrected_decisions = new
        removed = set(old) - set(new)
        self._hotswap_corrected(new, only=only, removed=removed)

    def _hotswap_corrected(self, decisions, only=None, removed=None):
        """Re-read changed channels (per patch) into the preload cache.

        After set_corrected_zarr_store, loader.read_region returns CORRECTED
        pixels for channels in the store's decisions and RAW pixels for
        everything else. `only` (a set) restricts the corrected re-read to the
        channels reprocessed this run (incremental save); None re-reads all
        corrected channels. `removed` channels lost their corrected status and
        are re-read (now raw). Synchronous (few channels × few patches)."""
        if not self.loader or not self.patches:
            return
        decisions = decisions or {}
        corrected = [ch for ch, m in decisions.items()
                     if str(m).lower() not in ("", "original", "none")
                     and (only is None or ch in only)]
        stale = list(dict.fromkeys(corrected + sorted(removed or ())))
        if not stale:
            return
        for pidx, bbox in enumerate(self.patches):
            try:
                y0, y1, x0, x1 = bbox
            except Exception:
                continue
            pc = self._preload_cache.setdefault(pidx, {})
            for ch in stale:
                try:
                    arr = self.loader.read_region(ch, y0, y1, x0, x1,
                                                  normalize=False)
                    arr = np.asarray(arr, dtype=np.float32)
                    if arr.ndim == 3 and arr.shape[2] == 1:
                        arr = arr[:, :, 0]
                    pc[ch] = arr
                except Exception:
                    continue
        # Announce the store change (the ONLY corrected-stage invalidation
        # trigger) for BOTH gained and lost channels, then drop the
        # workbench's stale pixels + repaint.
        if getattr(self, "_preview_provider", None) is not None:
            for ch in stale:
                self._preview_provider.invalidate(ch)
        self._maybe_refresh_conditioning()

    def _on_wsi_canceled(self, zarr_path):
        if os.path.exists(zarr_path):
            shutil.rmtree(zarr_path, ignore_errors=True)
        if self._wsi_dialog is not None:
            self._wsi_dialog.allow_close()
            self._wsi_dialog.reject()
        self._btn_continue.setEnabled(True)
        self._btn_load.setEnabled(True)
        QMessageBox.information(
            self, "Canceled",
            "Background correction was canceled. The channel that was being "
            "written has been removed; nothing partial was kept.")

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
